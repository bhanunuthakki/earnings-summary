"""Fetch SEC XBRL companyfacts for tracked tickers and persist into financial_facts.

Hits SEC's public `data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` endpoint,
writes raw JSON to `data/historical/sec/{TICKER}_companyfacts.json`, registers
one `documents` row per unique accession number, then inserts `financial_facts`
rows for the curated GAAP/IFRS tag map.

Silent-staleness detector
-------------------------
The brief_dirty SQL triggers (migration 0026) flip brief_dirty=1 on any
INSERT to financial_facts. If an accession registers but extraction emits
zero fact rows (parser fell behind a tag-map change, the filing carried no
mapped tags, all values were YTD aggregates that got skipped, etc.) those
triggers never fire and the next brief render uses stale data. After each
ticker's ingest we check (accessions_inserted > 0 AND facts_inserted == 0)
and force-flip brief_dirty so the daily worker re-attempts the build.

Rate-limited per SEC fair-use policy: ~10 req/sec absolute max; we sleep 0.2s
between tickers (~5 req/sec) to stay polite.

Usage:
    python execution/fetch_sec_xbrl.py                   # tracked universe filtered to CIK_MAP
    python execution/fetch_sec_xbrl.py --all-mapped      # every CIK_MAP ticker
    python execution/fetch_sec_xbrl.py --ticker NU       # single ticker
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm_artifact_store import mark_artifacts_dirty_for_fact_change  # noqa: E402
from models.runs import StageStatus  # noqa: E402
from pipeline.queries import open_db, tracked_companies_for_user  # noqa: E402
from pipeline.run_accounting import end_run, start_run  # noqa: E402
from pipeline.sec_xbrl import CIK_MAP, NO_SEC_FILERS, ingest_for_ticker  # noqa: E402

_PER_TICKER_DELAY_S = 0.2


def _flag_silent_staleness(conn: sqlite3.Connection, ticker: str) -> bool:
    """Force-flip tracked_companies.brief_dirty=1 for `ticker`.

    Returns True if a row was updated. Schema-tolerant: returns False
    silently when the column is missing (synthetic envs without 0022).
    """
    try:
        cur = conn.execute(
            "UPDATE tracked_companies SET brief_dirty = 1 WHERE ticker = ?",
            (ticker.upper(),),
        )
    except sqlite3.OperationalError:
        # brief_dirty column absent (pre-0022) — nothing to flip.
        return False
    conn.commit()
    return cur.rowcount > 0


def _resolve_tickers(args: argparse.Namespace, conn: sqlite3.Connection) -> list[str]:
    """Default scope: the live tracked universe (portfolio + evaluation +
    watchlist) filtered to CIK_MAP, so list changes never need a script edit. Tracked
    names without a CIK are reported on stderr — NO_SEC_FILERS silently
    (documented, expected), unknown ones loudly (CIK_MAP is stale)."""
    if args.ticker:
        return [args.ticker.upper()]
    if args.all_mapped:
        return sorted(CIK_MAP.keys())
    try:
        tracked = [c.ticker.upper() for c in tracked_companies_for_user(conn)]
    except sqlite3.Error:
        # Synthetic env without tracked_companies — fall back to the full map.
        return sorted(CIK_MAP.keys())
    uncovered = sorted(t for t in tracked if t not in CIK_MAP and t not in NO_SEC_FILERS)
    if uncovered:
        sys.stderr.write(
            json.dumps({"event": "sec_cik_map_stale", "tickers_missing_cik": uncovered}) + "\n"
        )
    return sorted(t for t in tracked if t in CIK_MAP)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Single ticker (must be in CIK_MAP)")
    parser.add_argument(
        "--all-mapped",
        action="store_true",
        help="Fetch every CIK_MAP ticker instead of the tracked universe",
    )
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(args, conn)
        unmapped = [t for t in tickers if t not in CIK_MAP]
        if unmapped:
            print(json.dumps({"error": "no CIK for", "tickers": unmapped}, indent=2))
            return 1

        run_id = start_run(conn, directive="fetch_sec_xbrl", ticker_scope=tickers)
        rows: list[dict[str, object]] = []
        failed = 0

        for i, ticker in enumerate(tickers):
            if i > 0:
                time.sleep(_PER_TICKER_DELAY_S)
            try:
                stats = ingest_for_ticker(
                    conn, ticker=ticker, project_root=PROJECT_ROOT, run_id=run_id
                )
            except OSError as e:
                # Transient: network / filesystem. Continue with next ticker.
                rows.append(
                    {
                        "ticker": ticker,
                        "error": f"OSError: {e}"[:200],
                        "class": "transient",
                    }
                )
                failed += 1
                continue
            except (ValueError, KeyError) as e:
                # Schema drift: the SEC payload didn't parse as expected.
                # Per GEMINI.md, capture the raw response location so the
                # operator can inspect it before re-running.
                raw_path = (
                    PROJECT_ROOT / "data" / "historical" / "sec" / f"{ticker}_companyfacts.json"
                )
                rows.append(
                    {
                        "ticker": ticker,
                        "error": f"{type(e).__name__}: {e}"[:200],
                        "class": "schema_drift",
                        "raw_response_path": str(raw_path) if raw_path.exists() else None,
                    }
                )
                failed += 1
                continue
            silent_staleness = stats.accessions_inserted > 0 and stats.facts_inserted == 0
            flagged_dirty = False
            artifacts_flipped = 0
            if silent_staleness:
                flagged_dirty = _flag_silent_staleness(conn, ticker)
                # Also invalidate fact-dependent LLM artifacts so the eventual
                # rebuild regenerates them with the new filing's content.
                artifacts_flipped = mark_artifacts_dirty_for_fact_change(
                    ticker=ticker,
                    reason="sec_silent_staleness",
                    db_path=args.db,
                )
            rows.append(
                {
                    "ticker": ticker,
                    "accessions_registered": stats.accessions_inserted,
                    "facts_inserted": stats.facts_inserted,
                    "silent_staleness": silent_staleness,
                    "brief_dirty_forced": flagged_dirty,
                    "llm_artifacts_marked_dirty": artifacts_flipped,
                }
            )
            if silent_staleness:
                # Emit a watch event so cron logs surface the divergence
                # (filings landed but no facts extracted) for operator review.
                sys.stderr.write(
                    json.dumps(
                        {
                            "event": "sec_silent_staleness_detected",
                            "ticker": ticker,
                            "accessions_registered": stats.accessions_inserted,
                            "brief_dirty_forced": flagged_dirty,
                            "llm_artifacts_marked_dirty": artifacts_flipped,
                        }
                    )
                    + "\n"
                )

        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        end_run(
            conn, run_id, terminal, error_summary=f"{failed} tickers failed" if failed else None
        )

        total_accessions = sum(r.get("accessions_registered", 0) for r in rows)
        total_facts = sum(r.get("facts_inserted", 0) for r in rows)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "tickers": len(tickers),
                    "failed": failed,
                    "total_accessions_registered": total_accessions,
                    "total_facts_inserted": total_facts,
                    "rows": rows,
                },
                indent=2,
            )
        )
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
