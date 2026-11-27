"""CLI to print an N-quarter portfolio coverage matrix (filings, PR, deck, transcript, commitments, readout)."""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from compute.portfolio_coverage_matrix import (  # noqa: E402
    CoverageListFilter,
    build_portfolio_coverage_matrix,
    format_matrix_markdown,
    format_matrix_table,
)
from db_paths import require_db_path  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="Explicit portfolio.db path.")
    parser.add_argument(
        "--quarters",
        type=int,
        default=8,
        help="Consecutive fiscal quarters per company (1-20, default: 8).",
    )
    parser.add_argument(
        "--list",
        dest="list_type",
        choices=["portfolio", "evaluation", "watchlist", "all"],
        default="portfolio",
        help="Company list filter (default: portfolio).",
    )
    parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="Restrict output to specific ticker(s) (repeatable).",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=["table", "markdown", "json"],
        default="table",
        help="Output format (default: table).",
    )
    args = parser.parse_args(argv)
    if args.quarters < 1 or args.quarters > 20:
        parser.error("--quarters must be between 1 and 20")

    db_path = require_db_path(args.db)
    list_filter = cast(CoverageListFilter, args.list_type)
    with closing(
        connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=False)
    ) as conn:
        matrix = build_portfolio_coverage_matrix(
            conn,
            quarters=args.quarters,
            list_type=list_filter,
            tickers=args.ticker or None,
        )

    if args.output_format == "json":
        print(matrix.model_dump_json(indent=2))
    elif args.output_format == "markdown":
        print(format_matrix_markdown(matrix))
    else:
        print(format_matrix_table(matrix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
