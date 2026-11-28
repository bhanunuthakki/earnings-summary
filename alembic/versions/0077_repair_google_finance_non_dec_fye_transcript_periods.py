"""Repair non-December-FYE Google Finance Beta transcript period_end values.

Revision ID: 0077_repair_google_finance_non_dec_fye_transcript_periods
Revises: 0076_purge_quarantined_kpi_facts
Create Date: 2026-11-28

Four ``aggregator_google_finance_beta`` transcripts for non-December-FYE
issuers (``BIRK``, ``TJX``, ``TWST``, ``VFC``) were ingested immediately before
PR #1833 landed, leaving their ``transcripts.period_end`` and linked
``documents.period_end`` columns on calendar-quarter dates instead of their
true fiscal quarter ends.

This migration content-addresses those four exact document hashes, drops
``trg_transcripts_immutable_evidence`` only while updating matched rows inside
a savepoint, guards against unique-index collisions on ``transcripts``, and
recreates the immutability trigger verbatim before releasing the savepoint.
"""

from __future__ import annotations

from sqlalchemy.engine import Connection

from alembic import op

revision = "0077_repair_google_finance_non_dec_fye_transcript_periods"
down_revision = "0076_purge_quarantined_kpi_facts"
branch_labels = None
depends_on = None

_IMMUTABILITY_TRIGGER = "trg_transcripts_immutable_evidence"
_SOURCE = "aggregator_google_finance_beta"
_SAVEPOINT = "repair_google_finance_non_dec_fye_transcript_periods"

# (ticker, fiscal_period_type, document_sha256, old_period_end, new_period_end)
_REPAIRS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "BIRK",
        "Q3",
        "db07a4baa7296844773e934c230a292b1d7f5852cd29ec00ca1f3bd326034222",  # pragma: allowlist secret
        "2026-09-30 00:00:00",
        "2026-06-30 00:00:00",
    ),
    (
        "TJX",
        "Q3",
        "880077dbc9f5d79dce93db440677517e356b76899bef9239d7463b2abeb78dfe",  # pragma: allowlist secret
        "2027-09-30 00:00:00",
        "2026-10-31 00:00:00",
    ),
    (
        "TWST",
        "Q4",
        "510c70fbf15ecfefa844d2259e929a80bc65fbd844ce8c6a4cdf01121281be0e",  # pragma: allowlist secret
        "2026-12-31 00:00:00",
        "2026-09-30 00:00:00",
    ),
    (
        "VFC",
        "Q2",
        "157b4d871e944d437a35638d255f86ddd14ac5779a12ec5b47664b5e1bb03dda",  # pragma: allowlist secret
        "2027-06-30 00:00:00",
        "2026-09-30 00:00:00",
    ),
)

_MATCH_CANDIDATES_SQL = """
SELECT t.id, t.document_id
FROM transcripts AS t
JOIN documents AS d ON d.id = t.document_id
WHERE d.sha256 = ?
  AND d.ticker = ?
  AND t.ticker = ?
  AND t.source = ?
  AND t.fiscal_period_type = ?
  AND t.period_end = ?
  AND NOT EXISTS (
    SELECT 1
    FROM transcripts AS other
    WHERE other.ticker = t.ticker
      AND other.fiscal_period_type = t.fiscal_period_type
      AND other.period_end = ?
      AND other.id != t.id
  )
ORDER BY t.id
"""


def _object_exists(connection: Connection, obj_type: str, name: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ? LIMIT 1",
        (obj_type, name),
    ).fetchone()
    return row is not None


def _capture_immutability_trigger(connection: Connection) -> str | None:
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (_IMMUTABILITY_TRIGGER,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _apply_repairs(*, reverse: bool = False) -> None:
    connection = op.get_bind()
    if not (
        _object_exists(connection, "table", "transcripts")
        and _object_exists(connection, "table", "documents")
    ):
        return

    matched: list[tuple[int, int, str, str]] = []
    for ticker, fiscal_period_type, sha256, old_pe, new_pe in _REPAIRS:
        from_pe, to_pe = (new_pe, old_pe) if reverse else (old_pe, new_pe)
        rows = connection.exec_driver_sql(
            _MATCH_CANDIDATES_SQL,
            (sha256, ticker, ticker, _SOURCE, fiscal_period_type, from_pe, to_pe),
        ).fetchall()
        for row in rows:
            matched.append((int(row[0]), int(row[1]), from_pe, to_pe))

    if not matched:
        return

    trigger_sql = _capture_immutability_trigger(connection)

    connection.exec_driver_sql(f"SAVEPOINT {_SAVEPOINT}")
    try:
        if trigger_sql is not None:
            connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{_IMMUTABILITY_TRIGGER}"')
        try:
            for transcript_id, document_id, from_pe, to_pe in matched:
                connection.exec_driver_sql(
                    "UPDATE transcripts SET period_end = ? WHERE id = ? AND period_end = ?",
                    (to_pe, transcript_id, from_pe),
                )
                connection.exec_driver_sql(
                    "UPDATE documents SET period_end = ? WHERE id = ? AND period_end = ?",
                    (to_pe, document_id, from_pe),
                )
        finally:
            if trigger_sql is not None:
                connection.exec_driver_sql(trigger_sql)

        for tbl in ("transcripts", "documents"):
            if (
                connection.exec_driver_sql(f"PRAGMA foreign_key_check({tbl})").fetchone()
                is not None
            ):
                raise RuntimeError(
                    f"google_finance_beta FYE transcript period repair failed foreign-key check on {tbl}"
                )

        connection.exec_driver_sql(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    except Exception:
        connection.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
        connection.exec_driver_sql(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        raise


def upgrade() -> None:
    _apply_repairs(reverse=False)


def downgrade() -> None:
    _apply_repairs(reverse=True)
