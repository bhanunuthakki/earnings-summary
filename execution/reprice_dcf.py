"""Re-price the persisted DCF over/under against fresh live prices — no rebuild.

``dcf_runs.over_under_pct`` (and the ``live_price`` the cockpit / next-dollar
"ret" factor recompute from) is frozen at the last ``refresh_dcf``. That step is
opt-in with no cron, so a price-only move leaves the trim/sell ladder + the
50%-weight allocation factor stale. This re-divides the *persisted* fair value
(``npv_per_share``, fixed between fundamentals refreshes) by a fresh live price
and rewrites only the three price-leg columns — pure arithmetic, never re-running
the DCF (see ``src/dcf/reprice.py``).

Wired as stage 0e of the morning pipeline (``run_morning_pipeline.py``); also
runnable standalone after a price-cache refresh or on demand:

    python execution/reprice_dcf.py
    python execution/reprice_dcf.py --ticker NU
    python execution/reprice_dcf.py --repo-root . --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf.reprice import reprice_runs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db and data/historical/fmp/ (the "
        "FMP-cache fallback price source). Default: this repo.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the portfolio DB path (wins over --repo-root derivation).",
    )
    parser.add_argument(
        "--ticker",
        action="append",
        default=None,
        metavar="TICKER",
        help="Re-price only this ticker (repeatable). Default: every dcf_runs row.",
    )
    args = parser.parse_args()

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )

    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        results = reprice_runs(conn, repo_root, tickers=args.ticker)
    finally:
        conn.close()

    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    summary = {
        "event": "reprice_dcf",
        "tickers": len(results),
        "by_status": by_status,
        "results": [
            {
                "ticker": r.ticker,
                "status": r.status,
                "over_under_pct": r.over_under_pct,
                "live_price": r.live_price,
                "source": r.source_name,
            }
            for r in results
        ],
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
