"""Fetch SEC XBRL companyfacts for tracked tickers and persist into financial_facts.

Hits SEC's public `data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` endpoint,
writes raw JSON to `data/historical/sec/{TICKER}_companyfacts.json`, registers
one `documents` row per unique accession number, then inserts `financial_facts`
rows for the curated GAAP/IFRS tag map.

Rate-limited per SEC fair-use policy: ~10 req/sec absolute max; we sleep 0.2s
between tickers (~5 req/sec) to stay polite.

Usage:
    python execution/fetch_sec_xbrl.py                   # all 27 mapped tickers
    python execution/fetch_sec_xbrl.py --ticker NU       # single ticker
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.runs import StageStatus  # noqa: E402
from pipeline.queries import open_db  # noqa: E402
from pipeline.run_accounting import end_run, start_run  # noqa: E402
from pipeline.sec_xbrl import CIK_MAP, ingest_for_ticker  # noqa: E402

_PER_TICKER_DELAY_S = 0.2


def _resolve_tickers(args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    return sorted(CIK_MAP.keys())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Single ticker (must be in CIK_MAP)")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(args)
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
                stats = ingest_for_ticker(conn, ticker=ticker, project_root=PROJECT_ROOT)
            except OSError as e:
                # Transient: network / filesystem. Continue with next ticker.
                rows.append({
                    "ticker": ticker,
                    "error": f"OSError: {e}"[:200],
                    "class": "transient",
                })
                failed += 1
                continue
            except (ValueError, KeyError) as e:
                # Schema drift: the SEC payload didn't parse as expected.
                # Per GEMINI.md, capture the raw response location so the
                # operator can inspect it before re-running.
                raw_path = (
                    PROJECT_ROOT
                    / "data"
                    / "historical"
                    / "sec"
                    / f"{ticker}_companyfacts.json"
                )
                rows.append({
                    "ticker": ticker,
                    "error": f"{type(e).__name__}: {e}"[:200],
                    "class": "schema_drift",
                    "raw_response_path": str(raw_path) if raw_path.exists() else None,
                })
                failed += 1
                continue
            rows.append({
                "ticker": ticker,
                "accessions_registered": stats.accessions_inserted,
                "facts_inserted": stats.facts_inserted,
            })

        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        end_run(conn, run_id, terminal,
                error_summary=f"{failed} tickers failed" if failed else None)

        total_accessions = sum(r.get("accessions_registered", 0) for r in rows)
        total_facts = sum(r.get("facts_inserted", 0) for r in rows)
        print(json.dumps({
            "run_id": run_id, "tickers": len(tickers), "failed": failed,
            "total_accessions_registered": total_accessions,
            "total_facts_inserted": total_facts,
            "rows": rows,
        }, indent=2))
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
