"""execution/read_estimate_asof.py — point-in-time consensus lookup CLI.

Answers "what was the consensus <metric> for <ticker> fiscal year <Y> AS OF
<date>?" from the self-owned FMP analyst-estimates snapshot archive
(``data/historical/fmp_snapshots/<YYYY-MM-DD>/``). Thin wrapper over
``src/estimates_archive.py`` — all honesty rules (latest snapshot <= asof,
``not_available`` before archive start, never interpolate) live there.

Prints one JSON object to stdout; JSON-line events to stderr. Exit 0 for any
honest answer (including gaps — a gap is a correct answer); exit 2 for an
unknown metric (a caller bug, not a data gap).

Usage:
    python execution/read_estimate_asof.py --ticker MELI --metric revenueAvg \
        --fiscal-year 2027 --asof 2026-06-01
    python execution/read_estimate_asof.py --ticker WIX --metric epsAvg \
        --fiscal-year 2026 --asof 2026-07-01 --snapshots-dir C:/path/to/fmp_snapshots
    python execution/read_estimate_asof.py --ticker NU --coverage
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from estimates_archive import (  # noqa: E402
    SUPPORTED_METRICS,
    estimate_asof,
    ticker_archive_dates,
)

_DEFAULT_SNAPSHOTS = PROJECT_ROOT / "data" / "historical" / "fmp_snapshots"


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ticker", required=True, help="Ticker symbol, e.g. MELI.")
    parser.add_argument(
        "--metric",
        default="revenueAvg",
        help=f"FMP estimate field (one of: {', '.join(SUPPORTED_METRICS)}).",
    )
    parser.add_argument("--fiscal-year", type=int, default=None, help="Fiscal year, e.g. 2027.")
    parser.add_argument(
        "--asof", type=date.fromisoformat, default=None, help="As-of date YYYY-MM-DD."
    )
    parser.add_argument(
        "--snapshots-dir",
        type=Path,
        default=_DEFAULT_SNAPSHOTS,
        help="fmp_snapshots root (worktree runs pass the main repo's).",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Only list the ticker's archive snapshot dates and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    ticker = cast("str", args.ticker).upper()
    snapshots_dir = cast("Path", args.snapshots_dir)
    if cast("bool", args.coverage):
        dates = ticker_archive_dates(ticker, snapshots_dir)
        print(
            json.dumps(
                {
                    "ticker": ticker,
                    "snapshot_count": len(dates),
                    "archive_start": dates[0] if dates else None,
                    "archive_latest": dates[-1] if dates else None,
                    "dates": dates,
                },
                indent=2,
            )
        )
        return 0
    fiscal_year = cast("int | None", args.fiscal_year)
    asof = cast("date | None", args.asof)
    if fiscal_year is None or asof is None:
        _log("read_estimate_asof_usage_error", detail="--fiscal-year and --asof are required")
        return 2
    answer = estimate_asof(
        ticker,
        cast("str", args.metric),
        fiscal_year,
        asof,
        snapshots_dir=snapshots_dir,
    )
    print(json.dumps(answer.to_json(), indent=2))
    _log("read_estimate_asof_done", ticker=ticker, status=answer.status)
    return 2 if answer.status == "unknown_metric" else 0


if __name__ == "__main__":
    sys.exit(main())
