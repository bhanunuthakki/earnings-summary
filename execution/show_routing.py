"""Show the SourcePlan for a ticker (Phase 2.5 diagnostic CLI).

Usage:
    python execution/show_routing.py --ticker GOOG
    python execution/show_routing.py --all       # all classified tracked companies
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.companies import Company, ListType  # noqa: E402
from pipeline.queries import (  # noqa: E402
    ANALYZED_LIST_TYPES,
    open_db,
    tracked_companies_for_user,
)
from pipeline.source_routing import SourcePlan, plan_for_ticker  # noqa: E402


def _format_plan(plan: SourcePlan) -> dict[str, object]:
    return {
        "ticker": plan.ticker,
        "instrument_type": plan.instrument_type.value if plan.instrument_type else None,
        "filing_regime": plan.filing_regime.value if plan.filing_regime else None,
        "sources": sorted(s.value for s in plan.sources),
        "ir_urls": plan.ir_urls,
        "primary_kpi_names": plan.primary_kpi_names,
    }


def _print_plan_for(conn, ticker: str) -> None:
    plan = plan_for_ticker(conn, ticker)
    print(json.dumps(_format_plan(plan), indent=2))


def _print_plans_for_all(conn, *, include_index_members: bool) -> None:
    list_types = frozenset(ListType) if include_index_members else ANALYZED_LIST_TYPES
    companies: list[Company] = tracked_companies_for_user(
        conn, only_classified=True, list_types=list_types
    )
    out: list[dict[str, object]] = []
    for c in companies:
        plan = plan_for_ticker(conn, c.ticker)
        out.append(_format_plan(plan))
    print(json.dumps(out, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single ticker to show routing for")
    group.add_argument("--all", action="store_true", help="All classified tracked companies")
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "data" / "portfolio.db"),
        help="Path to portfolio.db",
    )
    parser.add_argument(
        "--include-index-members",
        action="store_true",
        help="With --all: also show plans for index_member/etf/none (default: "
        "active universe — portfolio+watchlist+evaluation).",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db):
        sys.stderr.write(f"DB not found at {args.db}\n")
        return 2

    conn = open_db(args.db)
    try:
        if args.ticker:
            _print_plan_for(conn, args.ticker)
        else:
            _print_plans_for_all(conn, include_index_members=args.include_index_members)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
