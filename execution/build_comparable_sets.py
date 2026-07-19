"""Resolve + freeze bottoms-up comparable sets.

docs/design/comparable_sets_bottoms_up.md §8. Phase 1 shipped `--all-portfolio`
(~15 names, hand-verifiable by the owner). Phase 2 (§11) widens to
`--all-tracked` — portfolio + watchlist + evaluation (`pipeline.queries.
ANALYZED_LIST_TYPES`), ~100 names — resolving a comparable set for every name
the owner actually tracks, not just the ~15 portfolio holdings. `index_member`
tickers are never subjects here (they're pool-only context, per §2) even
though `--all-tracked` widens the SUBJECT list, not the pool itself.

Usage:
    python execution/build_comparable_sets.py --ticker NU
    python execution/build_comparable_sets.py --all-portfolio
    python execution/build_comparable_sets.py --all-tracked
    python execution/build_comparable_sets.py --ticker NU --refresh

Wraps the run in start_run / record_stage / end_run
(StageName.COMPARABLE_SET_RESOLVE) so every invocation produces an audit
trail in ingestion_runs and stage_transitions, same pattern as
execution/extract_facts.py.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comparable_sets import (  # noqa: E402
    freeze_comparable_set,
    load_pool,
    resolve_comparable_set,
)
from models.companies import ListType  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.queries import (  # noqa: E402
    ANALYZED_LIST_TYPES,
    open_db,
    tracked_companies_for_user,
)
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    list_types = ANALYZED_LIST_TYPES if args.all_tracked else frozenset({ListType.PORTFOLIO})
    companies = tracked_companies_for_user(conn, only_classified=False, list_types=list_types)
    return sorted({c.ticker for c in companies})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single subject ticker to resolve a comparable set for")
    group.add_argument(
        "--all-portfolio",
        action="store_true",
        help="Resolve for every list_type='portfolio' ticker (Phase 1 scope, ~15 names)",
    )
    group.add_argument(
        "--all-tracked",
        action="store_true",
        help="Resolve for every portfolio+watchlist+evaluation ticker (Phase 2 scope, ~100 names)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-freeze even if the resolved set matches the currently-open one",
    )
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Root containing data/historical/fmp, data/peer_selection, micro_thesis/ "
        "(override to point at a different checkout's data/ for read-only validation)",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root)
    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(conn, args)
        if not tickers:
            sys.stderr.write("No tickers resolved; nothing to do\n")
            return 0

        run_id = start_run(conn, directive="build_comparable_sets", ticker_scope=tickers)
        pool = load_pool(conn, repo_root)

        results: list[dict[str, object]] = []
        failed = 0
        for ticker in tickers:
            try:
                resolution = resolve_comparable_set(ticker, repo_root, conn, pool=pool)
                if resolution.skipped_reason:
                    record_stage(
                        conn,
                        run_id,
                        ticker,
                        StageName.COMPARABLE_SET_RESOLVE,
                        StageStatus.SKIPPED,
                        error_msg=resolution.skipped_reason,
                    )
                    results.append({"ticker": ticker, "skipped_reason": resolution.skipped_reason})
                    continue
                outcome = freeze_comparable_set(conn, resolution, refresh=args.refresh)
                record_stage(conn, run_id, ticker, StageName.COMPARABLE_SET_RESOLVE, StageStatus.OK)
                results.append(
                    {
                        "ticker": ticker,
                        "comparable_set_id": outcome.comparable_set_id,
                        "metric_class": resolution.metric_class,
                        "action": outcome.action,
                        "n_members": outcome.n_members,
                        "opened": outcome.opened,
                        "closed": outcome.closed,
                        "source_summary": resolution.source_summary,
                        "method_flags": resolution.method_flags,
                    }
                )
            except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
                record_stage(
                    conn,
                    run_id,
                    ticker,
                    StageName.COMPARABLE_SET_RESOLVE,
                    StageStatus.FAILED,
                    error_msg=f"{type(e).__name__}: {e}"[:500],
                )
                failed += 1
                sys.stderr.write(f"FAILED {ticker}: {type(e).__name__}: {e}\n")

        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        end_run(
            conn, run_id, terminal, error_summary=f"{failed} tickers failed" if failed else None
        )

        print(
            json.dumps(
                {"run_id": run_id, "tickers": len(tickers), "failed": failed, "results": results},
                indent=2,
            )
        )
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
