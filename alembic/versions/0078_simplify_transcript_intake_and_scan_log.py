"""Simplify transcript intake constraints and add segments_sha256 to commitment_scan_log.

Revision ID: 0078_simplify_transcript_intake_and_scan_log
Revises: 0077_repair_google_finance_non_dec_fye_transcript_periods
Create Date: 2026-11-28

1. Adds ``segments_sha256 TEXT`` to ``commitment_scan_log`` and backfills it
   for transcripts that already have a verified ``commitment_scan_receipts``
   row, enabling direct segment-hash scan tracking without a 1,300-LOC Merkle
   receipt ledger.
2. Drops the three rigid ``BEFORE INSERT`` validation triggers (including the
   18-argument Python UDF ``transcript_receipt_valid``) and removes closed-enum
   ``CHECK(provider IN ...)`` / ``json_extract`` constraints from
   ``transcript_acquisition_receipts`` while preserving all existing rows and
   append-only update/delete guards.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy.engine import Connection

from alembic import op

revision = "0078_simplify_transcript_intake_and_scan_log"
down_revision = "0077_repair_google_finance_non_dec_fye_transcript_periods"
branch_labels = None
depends_on = None

_RIGID_RECEIPT_TRIGGERS: tuple[str, ...] = (
    "trg_transcript_acquisition_receipts_validate",
    "trg_transcript_acquisition_receipts_stored_target_binding",
    "trg_transcript_acquisition_receipts_document_binding",
)


def _table_exists(connection: Connection, name: str) -> bool:
    return (
        connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: Connection, name: str) -> set[str]:
    return {
        str(row[1]) for row in connection.exec_driver_sql(f'PRAGMA table_info("{name}")').fetchall()
    }


def _compute_segments_sha256(connection: Connection, transcript_id: int) -> str | None:
    has_speaker = "speaker" in _table_columns(connection, "transcript_segments")
    speaker_sql = "speaker" if has_speaker else "NULL"
    rows = connection.exec_driver_sql(
        f"SELECT seq, {speaker_sql}, text FROM transcript_segments "  # nosec B608
        "WHERE transcript_id = ? ORDER BY seq, id",
        (transcript_id,),
    ).fetchall()
    if not rows:
        return None
    payload = [[int(r[0]), None if r[1] is None else str(r[1]), str(r[2])] for r in rows]
    canonical = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _upgrade_commitment_scan_log(connection: Connection) -> None:
    if not _table_exists(connection, "commitment_scan_log"):
        return
    cols = _table_columns(connection, "commitment_scan_log")
    if "segments_sha256" not in cols:
        connection.exec_driver_sql(
            "ALTER TABLE commitment_scan_log ADD COLUMN segments_sha256 TEXT"
        )
    if not _table_exists(connection, "transcript_segments"):
        return
    has_receipts = _table_exists(connection, "commitment_scan_receipts")
    if has_receipts:
        rows = connection.exec_driver_sql(
            "SELECT l.transcript_id FROM commitment_scan_log AS l "
            "WHERE l.segments_sha256 IS NULL "
            "AND EXISTS (SELECT 1 FROM commitment_scan_receipts AS r "
            "WHERE r.transcript_id = l.transcript_id)"
        ).fetchall()
    else:
        rows = connection.exec_driver_sql(
            "SELECT transcript_id FROM commitment_scan_log WHERE segments_sha256 IS NULL"
        ).fetchall()
    for (raw_tid,) in rows:
        tid = int(raw_tid)
        digest = _compute_segments_sha256(connection, tid)
        if digest is not None:
            connection.exec_driver_sql(
                "UPDATE commitment_scan_log SET segments_sha256 = ? WHERE transcript_id = ?",
                (digest, tid),
            )


def _relax_transcript_acquisition_receipts(connection: Connection) -> None:
    if not _table_exists(connection, "transcript_acquisition_receipts"):
        return
    for trigger_name in _RIGID_RECEIPT_TRIGGERS:
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {trigger_name}")

    # Rebuild transcript_acquisition_receipts without closed-enum or json_extract CHECKs
    # while keeping all existing rows, primary key, index, and append-only guards.
    connection.exec_driver_sql("SAVEPOINT simplify_transcript_acquisition_receipts")
    try:
        connection.exec_driver_sql(
            """
            CREATE TABLE transcript_acquisition_receipts_simplified (
                receipt_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL,
                document_id INTEGER REFERENCES documents(id),
                canonical_ticker TEXT NOT NULL,
                fiscal_year INTEGER NOT NULL,
                fiscal_quarter INTEGER NOT NULL,
                canonical_document_path TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL,
                artifact_size_bytes INTEGER NOT NULL,
                source_url TEXT,
                provider TEXT NOT NULL,
                source_type TEXT NOT NULL,
                document_type TEXT NOT NULL,
                source_regime TEXT NOT NULL,
                source_regime_contract_sha256 TEXT NOT NULL,
                authorization_json TEXT NOT NULL,
                artifact_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO transcript_acquisition_receipts_simplified "
            "(receipt_id, idempotency_key, document_id, canonical_ticker, fiscal_year, "
            "fiscal_quarter, canonical_document_path, artifact_sha256, artifact_size_bytes, "
            "source_url, provider, source_type, document_type, source_regime, "
            "source_regime_contract_sha256, authorization_json, artifact_json, recorded_at) "
            "SELECT receipt_id, idempotency_key, document_id, canonical_ticker, fiscal_year, "
            "fiscal_quarter, canonical_document_path, artifact_sha256, artifact_size_bytes, "
            "source_url, provider, source_type, document_type, source_regime, "
            "source_regime_contract_sha256, authorization_json, artifact_json, recorded_at "
            "FROM transcript_acquisition_receipts"
        )
        connection.exec_driver_sql("DROP TABLE transcript_acquisition_receipts")
        connection.exec_driver_sql(
            "ALTER TABLE transcript_acquisition_receipts_simplified "
            "RENAME TO transcript_acquisition_receipts"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_transcript_acquisition_receipts_target "
            "ON transcript_acquisition_receipts(idempotency_key, recorded_at, receipt_id)"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS trg_transcript_acquisition_receipts_no_update "
            "BEFORE UPDATE ON transcript_acquisition_receipts "
            "BEGIN SELECT RAISE(ABORT, 'transcript acquisition receipts are append-only'); END"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS trg_transcript_acquisition_receipts_no_delete "
            "BEFORE DELETE ON transcript_acquisition_receipts "
            "BEGIN SELECT RAISE(ABORT, 'transcript acquisition receipts are append-only'); END"
        )
        for table in ("transcript_acquisition_receipts", "commitment_scan_receipts"):
            if (
                _table_exists(connection, table)
                and connection.exec_driver_sql(f"PRAGMA foreign_key_check({table})").fetchone()
                is not None
            ):
                raise RuntimeError(f"foreign key check failed for {table}")
        connection.exec_driver_sql("RELEASE SAVEPOINT simplify_transcript_acquisition_receipts")
    except Exception:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT simplify_transcript_acquisition_receipts")
        connection.exec_driver_sql("RELEASE SAVEPOINT simplify_transcript_acquisition_receipts")
        raise


def upgrade() -> None:
    connection = op.get_bind()
    _upgrade_commitment_scan_log(connection)
    _relax_transcript_acquisition_receipts(connection)


def downgrade() -> None:
    # Additive column + relaxed CHECK constraints are intentionally one-way safe.
    pass
