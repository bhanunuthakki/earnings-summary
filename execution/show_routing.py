"""Show the SourcePlan for a ticker (Phase 2.5 diagnostic CLI).

Usage:
    python execution/show_routing.py --ticker GOOG
    python execution/show_routing.py --all       # all classified tracked companies
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

try:  # direct script invocation
    from _lib import add_database_argument, command_parser
except ImportError:  # pragma: no cover - package import fallback
    from execution._lib import add_database_argument, command_parser

from models.companies import Company, ListType
from pipeline.queries import (
    ANALYZED_LIST_TYPES,
    open_db,
    tracked_companies_for_user,
)
from pipeline.source_routing import SourcePlan, plan_for_ticker


def _format_plan(plan: SourcePlan) -> dict[str, object]:
    return {
        "ticker": plan.ticker,
        "instrument_type": plan.instrument_type.value if plan.instrument_type else None,
        "filing_regime": plan.filing_regime.value if plan.filing_regime else None,
        "sources": sorted(s.value for s in plan.sources),
        "ir_urls": plan.ir_urls,
        "primary_kpi_names": plan.primary_kpi_names,
    }


def _print_plan_for(conn: sqlite3.Connection, ticker: str) -> None:
    plan = plan_for_ticker(conn, ticker)
    print(json.dumps(_format_plan(plan), indent=2))


def _print_plans_for_all(conn: sqlite3.Connection, *, include_index_members: bool) -> None:
    list_types = frozenset(ListType) if include_index_members else ANALYZED_LIST_TYPES
    companies: list[Company] = tracked_companies_for_user(
        conn, only_classified=True, list_types=list_types
    )
    out: list[dict[str, object]] = []
    for c in companies:
        plan = plan_for_ticker(conn, c.ticker)
        out.append(_format_plan(plan))
    print(json.dumps(out, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    add_database_argument(
        parser,
        flag="--db",
        default=Path(__file__).resolve().parents[1] / "data" / "portfolio.db",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single ticker to show routing for")
    group.add_argument("--all", action="store_true", help="All classified tracked companies")
    parser.add_argument(
        "--include-index-members",
        action="store_true",
        help="With --all: also show plans for index_member/etf/none (default: "
        "active universe — portfolio+watchlist+evaluation).",
    )
    args = parser.parse_args(argv)

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
