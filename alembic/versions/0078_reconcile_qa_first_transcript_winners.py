"""Reconcile transcript period winners under the Q&A-first owner policy.

Revision ID: 0078_reconcile_qa_first_transcript_winners
Revises: 0077_repair_google_finance_non_dec_fye_transcript_periods
Create Date: 2026-11-28

Under the owner policy, confirmed analyst Q&A availability
(``has_qa_section = 1``) always outranks source reliability tier when selecting
the winning transcript for a ``(ticker, fiscal_period_type, period_end)``
group. Historical ingests prior to this policy could leave a Q&A-bearing
transcript at ``is_current = 0`` behind a prepared-remarks-only or unknown-QA
``ir_transcript_doc`` / ``manual_pdf`` winner.

This migration promotes the best ``has_qa_section = 1`` candidate in every such
group (ranked by source reliability tier, segment count, ``recorded_at``, and
lowest ``id``) while keeping ``trg_transcripts_lifecycle_update`` and
``trg_transcripts_immutable_evidence`` active throughout.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.engine import Connection

from alembic import op

revision = "0078_reconcile_qa_first_transcript_winners"
down_revision = "0077_repair_google_finance_non_dec_fye_transcript_periods"
branch_labels = None
depends_on = None

_SAVEPOINT = "reconcile_qa_first_transcript_winners"
_SELECTION_REASON = "superseded_by_qa_first_policy"

# Mirrors src/transcripts/source_reliability.py::_RANK so the migration stays
# self-contained even if application modules evolve.
_SOURCE_RANK: dict[str, int] = {
    "ir_transcript_doc": 100,
    "manual_pdf": 90,
    "aggregator_google_finance_beta": 80,
    "google_finance_beta": 80,
    "yt_dlp_whisper_url": 70,
    "yt_dlp_whisper_links": 70,
    "yt_dlp_whisper_search": 60,
    "aggregator_issuer_ir": 50,
    "aggregator_roic": 40,
    "aggregator_stockanalysis": 40,
    "aggregator_tickertrends": 40,
    "unknown_legacy": 10,
}

_GROUPS_TO_RECONCILE_SQL = """
SELECT
    cur.id,
    cur.ticker,
    cur.fiscal_period_type,
    cur.period_end
FROM transcripts AS cur
WHERE cur.is_current = 1
  AND COALESCE(cur.has_qa_section, 0) = 0
  AND EXISTS (
    SELECT 1
    FROM transcripts AS cand
    WHERE cand.ticker = cur.ticker
      AND cand.fiscal_period_type IS cur.fiscal_period_type
      AND cand.period_end IS cur.period_end
      AND cand.id != cur.id
      AND cand.has_qa_section = 1
  )
ORDER BY cur.id
"""

_QA_CANDIDATES_SQL = """
SELECT
    t.id,
    t.source,
    t.recorded_at,
    (SELECT COUNT(*) FROM transcript_segments AS s WHERE s.transcript_id = t.id) AS segment_count
FROM transcripts AS t
WHERE t.ticker = ?
  AND t.fiscal_period_type IS ?
  AND t.period_end IS ?
  AND t.id != ?
  AND t.has_qa_section = 1
ORDER BY t.id
"""


def _object_exists(connection: Connection, obj_type: str, name: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ? LIMIT 1",
        (obj_type, name),
    ).fetchone()
    return row is not None


def _reliability_rank(source: str | None) -> int:
    if source is None:
        return 0
    return _SOURCE_RANK.get(source, 0)


def upgrade() -> None:
    connection = op.get_bind()
    if not (
        _object_exists(connection, "table", "transcripts")
        and _object_exists(connection, "table", "transcript_segments")
    ):
        return

    groups = connection.exec_driver_sql(_GROUPS_TO_RECONCILE_SQL).fetchall()
    if not groups:
        return

    now_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    connection.exec_driver_sql(f"SAVEPOINT {_SAVEPOINT}")
    try:
        for group_row in groups:
            old_winner_id = int(group_row[0])
            ticker = str(group_row[1])
            fiscal_period_type = group_row[2]
            period_end = group_row[3]

            candidates = connection.exec_driver_sql(
                _QA_CANDIDATES_SQL,
                (ticker, fiscal_period_type, period_end, old_winner_id),
            ).fetchall()
            if not candidates:
                continue

            best = max(
                candidates,
                key=lambda row: (
                    _reliability_rank(None if row[1] is None else str(row[1])),
                    int(row[3]),
                    str(row[2] or ""),
                    -int(row[0]),
                ),
            )
            winner_id = int(best[0])

            # 1. Demote the current non-Q&A winner first to free the active
            #    unique-index slot while trg_transcripts_lifecycle_update stays active.
            connection.exec_driver_sql(
                "UPDATE transcripts "
                "SET is_current = 0, "
                "    is_active = 0, "
                "    superseded_by_id = ?, "
                "    superseded_by_transcript_id = ?, "
                "    superseded_at = ?, "
                "    selection_reason = ? "
                "WHERE id = ?",
                (winner_id, winner_id, now_ts, _SELECTION_REASON, old_winner_id),
            )

            # 2. Re-point any other non-current losers in the same period group
            #    that pointed at the demoted winner so they point to winner_id.
            connection.exec_driver_sql(
                "UPDATE transcripts "
                "SET superseded_by_id = ?, "
                "    superseded_by_transcript_id = ?, "
                "    superseded_at = COALESCE(superseded_at, ?) "
                "WHERE ticker = ? "
                "  AND fiscal_period_type IS ? "
                "  AND period_end IS ? "
                "  AND id NOT IN (?, ?) "
                "  AND superseded_by_transcript_id = ?",
                (
                    winner_id,
                    winner_id,
                    now_ts,
                    ticker,
                    fiscal_period_type,
                    period_end,
                    winner_id,
                    old_winner_id,
                    old_winner_id,
                ),
            )

            # 3. Promote the Q&A winner to current/active.
            connection.exec_driver_sql(
                "UPDATE transcripts "
                "SET is_current = 1, "
                "    is_active = 1, "
                "    superseded_by_id = NULL, "
                "    superseded_by_transcript_id = NULL, "
                "    superseded_at = NULL "
                "WHERE id = ?",
                (winner_id,),
            )

        if (
            connection.exec_driver_sql("PRAGMA foreign_key_check(transcripts)").fetchone()
            is not None
        ):
            raise RuntimeError(
                "Q&A-first transcript winner reconciliation failed foreign-key check on transcripts"
            )

        connection.exec_driver_sql(f"RELEASE SAVEPOINT {_SAVEPOINT}")
    except Exception:
        connection.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
        connection.exec_driver_sql(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        raise


def downgrade() -> None:
    # Schema is unchanged; Q&A-first winner reconciliation is a one-way data
    # migration under the active owner policy (restore from a predecessor DB
    # snapshot if pre-policy non-Q&A winners are needed).
    pass
