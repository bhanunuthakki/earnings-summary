"""Simplify transcript intake constraints and add segments_sha256 to commitment_scan_log.

Revision ID: 0079_simplify_transcript_intake_and_scan_log
Revises: 0078_reconcile_qa_first_transcript_winners
Create Date: 2026-11-28

1. Adds ``segments_sha256 TEXT`` to ``commitment_scan_log`` and backfills it
   for transcripts that already have a verified ``commitment_scan_receipts``
   row, enabling direct segment-hash scan tracking without a 1,300-LOC Merkle
   receipt ledger.
2. Drops the three rigid ``BEFORE INSERT`` validation triggers (including the
   18-argument Python UDF ``transcript_receipt_valid``) and replaces the closed
   ``CHECK(provider IN (...))`` constraint on ``transcript_acquisition_receipts``
   with a non-empty provider check while preserving all existing rows, indexes,
   append-only update/delete guards, and clean reversibility to revision 0075.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy.engine import Connection

from alembic import op

revision = "0079_simplify_transcript_intake_and_scan_log"
down_revision = "0078_reconcile_qa_first_transcript_winners"
branch_labels = None
depends_on = None

_OLD_PROVIDER_CHECK = (
    "CHECK(provider IN ('issuer_ir', 'roic', 'stockanalysis', 'tickertrends',"
    " 'google_finance_beta'))"
)
_NEW_PROVIDER_CHECK = "CHECK(length(trim(provider)) >= 1)"

_RIGID_RECEIPT_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
        "trg_transcript_acquisition_receipts_validate",
        """
        CREATE TRIGGER trg_transcript_acquisition_receipts_validate
        BEFORE INSERT ON transcript_acquisition_receipts
        BEGIN
            SELECT CASE WHEN transcript_receipt_valid(
                NEW.receipt_id, NEW.idempotency_key, NEW.document_id,
                NEW.canonical_ticker, NEW.fiscal_year, NEW.fiscal_quarter,
                NEW.canonical_document_path, NEW.artifact_sha256,
                NEW.artifact_size_bytes, NEW.source_url, NEW.provider,
                NEW.source_type, NEW.document_type, NEW.source_regime,
                NEW.source_regime_contract_sha256, NEW.authorization_json,
                NEW.artifact_json, NEW.recorded_at
            ) != 1 THEN RAISE(ABORT, 'invalid transcript acquisition receipt') END;
        END
        """,
    ),
    (
        "trg_transcript_acquisition_receipts_stored_target_binding",
        """
        CREATE TRIGGER trg_transcript_acquisition_receipts_stored_target_binding
        BEFORE INSERT ON transcript_acquisition_receipts
        WHEN (
            SELECT COUNT(*) FROM tracked_companies AS tc
            WHERE UPPER(tc.ticker) = NEW.canonical_ticker
              AND tc.archived_at IS NULL
              AND tc.list_type = json_extract(
                  NEW.authorization_json,'$.stored_target.coverage_role'
              )
              AND (
                  CASE
                      WHEN tc.fiscal_year_end GLOB '[0-1][0-9]-[0-3][0-9]'
                      THEN CAST(substr(tc.fiscal_year_end,1,2) AS INTEGER)
                      ELSE NULL
                  END
              ) IS json_extract(
                  NEW.authorization_json,'$.stored_target.fiscal_year_end_month'
              )
        ) != 1
        BEGIN
            SELECT RAISE(ABORT, 'transcript receipt does not match current stored target');
        END
        """,
    ),
    (
        "trg_transcript_acquisition_receipts_document_binding",
        """
        CREATE TRIGGER trg_transcript_acquisition_receipts_document_binding
        BEFORE INSERT ON transcript_acquisition_receipts
        WHEN NEW.document_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM documents AS d
            WHERE d.id = NEW.document_id
              AND UPPER(d.ticker) = NEW.canonical_ticker
              AND d.source_type = 'ir_doc'
              AND d.doc_type = 'ir_transcript'
              AND d.file_path = NEW.canonical_document_path
              AND d.sha256 = NEW.artifact_sha256
              AND d.raw_bytes_size = NEW.artifact_size_bytes
              AND d.source_url IS NEW.source_url
        )
        BEGIN
            SELECT RAISE(ABORT, 'transcript receipt does not match canonical document');
        END
        """,
    ),
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


def _rebuild_receipts(connection: Connection, *, reverse: bool = False) -> None:
    if not _table_exists(connection, "transcript_acquisition_receipts"):
        return
    schema = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type='table' "
        "AND name='transcript_acquisition_receipts'"
    ).scalar_one()
    if not isinstance(schema, str):
        raise RuntimeError("transcript receipt schema is unavailable")
    old_check, new_check = (
        (_NEW_PROVIDER_CHECK, _OLD_PROVIDER_CHECK)
        if reverse
        else (_OLD_PROVIDER_CHECK, _NEW_PROVIDER_CHECK)
    )
    if schema.count(old_check) != 1:
        raise RuntimeError("transcript receipt schema differs from expected predecessor")
    updated_schema = schema.replace(old_check, new_check, 1)
    prefix = "CREATE TABLE transcript_acquisition_receipts"
    quoted_prefix = 'CREATE TABLE "transcript_acquisition_receipts"'
    if updated_schema.startswith(quoted_prefix):
        updated_schema = updated_schema.replace(
            quoted_prefix, "CREATE TABLE transcript_acquisition_receipts_rebuild", 1
        )
    elif updated_schema.startswith(prefix):
        updated_schema = updated_schema.replace(
            prefix, "CREATE TABLE transcript_acquisition_receipts_rebuild", 1
        )
    else:
        raise RuntimeError("transcript receipt table declaration differs from predecessor")

    rigid_names = {name for name, _ in _RIGID_RECEIPT_TRIGGERS}
    dependents = [
        (str(row[0]), str(row[1]))
        for row in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_schema WHERE tbl_name='transcript_acquisition_receipts' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
        ).fetchall()
    ]
    connection.exec_driver_sql("SAVEPOINT simplify_transcript_acquisition_receipts")
    try:
        connection.exec_driver_sql(updated_schema)
        connection.exec_driver_sql(
            "INSERT INTO transcript_acquisition_receipts_rebuild "
            "SELECT * FROM transcript_acquisition_receipts"
        )
        connection.exec_driver_sql("DROP TABLE transcript_acquisition_receipts")
        connection.exec_driver_sql(
            "ALTER TABLE transcript_acquisition_receipts_rebuild "
            "RENAME TO transcript_acquisition_receipts"
        )
        for name, statement in dependents:
            if not reverse and name in rigid_names:
                continue
            connection.exec_driver_sql(statement)
        if reverse:
            for name, trigger_sql in _RIGID_RECEIPT_TRIGGERS:
                if name not in {dep_name for dep_name, _ in dependents}:
                    connection.exec_driver_sql(trigger_sql)
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
    _rebuild_receipts(connection, reverse=False)


def downgrade() -> None:
    connection = op.get_bind()
    _rebuild_receipts(connection, reverse=True)
