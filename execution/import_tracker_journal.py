"""One-shot M4 importer: portfolio-tracker journal → earnings-summary.

Phase 3 of docs/design/portfolio_intelligence_consolidation_prd.md, executed
2026-07-23 with owner authorization to migrate by judgment. The tracker's
journal turned out to be nearly empty (trade_decisions: 0 rows), so the whole
import is a single behavioral lesson from trade_tags. Everything else was
deliberately omitted (see the M4 disposition record in the PRD/addendum):

  * trade_decisions (0 rows)          — nothing to migrate
  * action_queue (29 resolved/stale)  — archive-only in the tracker backup
  * chat_sessions/turns (1/9)         — archive-only
  * monthly_briefs (2 HTML blobs)     — archive-only
  * human_capital_overlap (30 rows)   — OMITTED: the same buckets were already
    ratification-REJECTED as owner_profile_facts; importing would override an
    explicit owner ruling
  * policy_weights (4 rows)           — benchmark calculation config; stays in
    the tracker per Phase-0 ruling PT-6

Idempotent on analyst_notes.source_ref. Safe to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

ES_DB = PROJECT_ROOT / "data" / "portfolio.db"
SOURCE_REF = "portfolio-tracker:trade_tags:1"

BODY = (
    "Behavioral lesson (migrated from the portfolio tracker trade journal): "
    "CPNG held 2026-01-21 to 2026-02-12 was tagged bought_with_no_thesis - "
    "a 3-week hold with no clear setup. Pattern to avoid: entering a position "
    "without an explicit directional thesis."
)
CONTEXT_JSON = (
    '{"migrated_from":"portfolio-tracker.trade_tags",'
    '"tag":"bought_with_no_thesis",'
    '"period_start":"2026-01-21","period_end":"2026-02-12",'
    '"original_created_at":"2026-05-09 09:38:05"}'
)
ORIGINAL_TS = "2026-05-09 09:38:05"


def main() -> None:
    conn = connect_sqlite(ES_DB, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        if conn.execute(
            "SELECT 1 FROM analyst_notes WHERE source_ref = ?", (SOURCE_REF,)
        ).fetchone():
            print("already imported - no-op")
            return
        conn.execute(
            "INSERT INTO analyst_notes "
            "(user_id, ticker, kind, status, body, source, source_ref, "
            " context_json, created_at, updated_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "bhanu",
                "CPNG",
                "observation",
                "resolved",
                BODY,
                "manual",
                SOURCE_REF,
                CONTEXT_JSON,
                ORIGINAL_TS,
                ORIGINAL_TS,
                ORIGINAL_TS,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM analyst_notes WHERE source_ref = ?", (SOURCE_REF,)
        ).fetchone()
        print(f"imported note id: {row[0]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
