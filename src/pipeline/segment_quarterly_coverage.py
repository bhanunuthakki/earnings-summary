"""Writer for ``segment_quarterly_coverage`` (docs/design/
segment_quarterly_framework.md §4.3, Phase 2).

Rationale for a dedicated writer module (mirrors ``pipeline.capture_coverage``'s
role for the run-level JSON log, but this is a queryable per-cell DB table, not
a JSON blob — see the design doc's own §4.3 rationale): both
``compute.segment_quarterly_10q`` (Phase 1's Q2/Q3 one-hop derivation guards,
promoted from log-only to persisted rows in Phase 2) and
``compute.segment_q4_derive`` (Phase 2's Q4 derivation) need to record the
SAME "we looked for this and didn't get it" outcomes, so the write path is
centralized here rather than duplicated in both callers.

Row identity is (ticker, period_end, fiscal_period_type, dim_type, dim_name,
method_version) per the table's own UNIQUE constraint (migration 0168). This
module upserts on that key via an explicit check-then-write rather than SQL
``ON CONFLICT`` — SQLite's NULL handling in a UNIQUE index means two rows
both carrying NULL ``dim_type``/``dim_name`` (a whole-filing-level gap, per
the schema comment) are never considered a conflict by the index itself, so
``ON CONFLICT`` silently fails to dedupe them. The explicit
``IS ?``-based lookup below is NULL-safe and gives the upsert semantics the
design doc's UNIQUE-constraint rationale actually calls for.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

# segment_quarterly_coverage.status vocabulary (migration 0168 / §4.3).
CoverageStatus = str  # "reported" | "derived" | "not_disclosed" | "not_applicable"
# | "unresolved_period_axis" | "source_missing" | "tolerance_breach"
# | "not_computable" -- kept as a plain str alias (not a StrEnum) since the
# audit script (§5.2) and the two writer call sites pass literal reason
# strings already validated at their own call sites; a StrEnum here would be
# the third independent enum shadowing the same vocabulary generic_xbrl_
# capture's skip-reason taxonomy already establishes as free-text.


def _table_exists(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='segment_quarterly_coverage'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def record_coverage(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: str,
    dim_type: str | None,
    dim_name: str | None,
    status: CoverageStatus,
    reason_code: str | None,
    source_doc_id: int | None,
    method_version: str | None,
) -> None:
    """Upsert one segment_quarterly_coverage row.

    Silently no-ops when the table doesn't exist yet (pre-0168 schema, or a
    synthetic test fixture that doesn't carry it) — same degrade-gracefully
    convention ``segment_junction_writer``'s column-presence checks use, so
    callers never need their own guard.
    """
    if not _table_exists(conn):
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    existing = conn.execute(
        """
        SELECT id FROM segment_quarterly_coverage
        WHERE ticker = ? AND period_end = ? AND fiscal_period_type = ?
          AND dim_type IS ? AND dim_name IS ? AND method_version IS ?
        """,
        (ticker.upper(), period_end, fiscal_period_type, dim_type, dim_name, method_version),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE segment_quarterly_coverage
            SET status = ?, reason_code = ?, source_doc_id = ?, checked_at = ?
            WHERE id = ?
            """,
            (status, reason_code, source_doc_id, now, int(existing[0])),
        )
        return
    conn.execute(
        """
        INSERT INTO segment_quarterly_coverage
          (ticker, period_end, fiscal_period_type, dim_type, dim_name,
           status, reason_code, source_doc_id, method_version, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker.upper(),
            period_end,
            fiscal_period_type,
            dim_type,
            dim_name,
            status,
            reason_code,
            source_doc_id,
            method_version,
            now,
        ),
    )


def coverage_rows_for_ticker(conn: sqlite3.Connection, ticker: str) -> list[sqlite3.Row]:
    """All coverage rows for ``ticker``, most-recently-checked first. Used by
    the audit CLI (§5.2) to render the ticker x quarter grid without a second
    ad-hoc query shape."""
    if not _table_exists(conn):
        return []
    return list(
        conn.execute(
            """
            SELECT * FROM segment_quarterly_coverage
            WHERE ticker = ?
            ORDER BY period_end DESC, checked_at DESC
            """,
            (ticker.upper(),),
        ).fetchall()
    )
