"""Resolve + freeze comparable sets (docs/design/comparable_sets_bottoms_up.md
sections 3, 8). Phase 1 scope: ``--all-portfolio`` only (portfolio-list subjects,
~15 names, hand-verifiable by the owner) — ``--all-tracked`` (+watchlist+evaluation)
is Phase 2.

Idempotent: skips a ticker whose current-method_version set is ``valid_to IS NULL``
and whose freshly-resolved candidate list is unchanged from the frozen one, unless
``--refresh``.

Usage:
    python execution/build_comparable_sets.py --ticker NU
    python execution/build_comparable_sets.py --all-portfolio
    python execution/build_comparable_sets.py --ticker NU --refresh
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comparable_sets import (  # noqa: E402
    CURRENT_METHOD_VERSION,
    freeze_comparable_set,
    load_pool,
    resolve_comparable_set,
)
from models.companies import ListType  # noqa: E402
from models.runs import StageName, StageStatus  # noqa: E402
from pipeline.queries import open_db, tracked_companies_for_user  # noqa: E402
from pipeline.run_accounting import end_run, record_stage, start_run  # noqa: E402


def _resolve_tickers(conn, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    companies = tracked_companies_for_user(
        conn, only_classified=False, list_types=frozenset({ListType.PORTFOLIO})
    )
    return [c.ticker for c in companies]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single ticker to resolve")
    group.add_argument(
        "--all-portfolio", action="store_true", help="All portfolio-list tickers (Phase 1 scope)"
    )
    parser.add_argument("--refresh", action="store_true", help="Force a re-write even if unchanged")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"))
    args = parser.parse_args()

    conn = open_db(args.db)
    try:
        tickers = _resolve_tickers(conn, args)
        if not tickers:
            print(json.dumps({"warning": "no tickers"}, indent=2))
            return 0

        pool = load_pool(conn, PROJECT_ROOT)
        run_id = start_run(conn, directive="build_comparable_sets", ticker_scope=tickers)

        per_ticker: list[dict[str, object]] = []
        failed = 0
        for ticker in tickers:
            try:
                resolved = resolve_comparable_set(ticker, pool, PROJECT_ROOT)
                outcome = freeze_comparable_set(
                    conn, resolved, refresh=args.refresh, method_version=CURRENT_METHOD_VERSION
                )
            except ValueError as e:
                failed += 1
                record_stage(
                    conn,
                    run_id,
                    ticker,
                    StageName.COMPARABLE_SET_RESOLVE,
                    StageStatus.FAILED,
                    error_msg=str(e)[:500],
                )
                sys.stderr.write(
                    json.dumps({"event": "resolve_failed", "ticker": ticker, "error": str(e)})
                    + "\n"
                )
                continue

            status = StageStatus.OK if outcome.changed else StageStatus.SKIPPED
            record_stage(conn, run_id, ticker, StageName.COMPARABLE_SET_RESOLVE, status)
            per_ticker.append(
                {
                    "ticker": ticker,
                    "comparable_set_id": outcome.comparable_set_id,
                    "changed": outcome.changed,
                    "members_added": outcome.members_added,
                    "members_removed": outcome.members_removed,
                    "n_members": len(resolved.members),
                    "metric_class": resolved.metric_class.value,
                    "source_summary": resolved.source_summary,
                }
            )
            sys.stderr.write(
                json.dumps({"event": "resolved", "ticker": ticker, "changed": outcome.changed})
                + "\n"
            )

        terminal = StageStatus.OK if failed == 0 else StageStatus.FAILED
        end_run(conn, run_id, terminal, error_summary=f"{failed} failed" if failed else None)

        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "tickers_processed": len(per_ticker),
                    "failed": failed,
                    "per_ticker": per_ticker,
                },
                indent=2,
            )
        )
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
