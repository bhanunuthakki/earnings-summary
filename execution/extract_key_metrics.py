"""Generate the LLM "key metrics" preselect for the DIY picker's bubble row.

Usage:
    python execution/extract_key_metrics.py --ticker NU
    python execution/extract_key_metrics.py --ticker NU --refresh
    python execution/extract_key_metrics.py --all
    python execution/extract_key_metrics.py --all --refresh

Caches to `data/key_metrics/{TICKER}.json` keyed by an input sha256 (name +
business description + the picker catalog vocabulary). Re-runs are no-ops unless
the inputs changed or --refresh is passed.

The renderer (`src/pipeline/key_metrics.py`, read by the Explore panel) merges
the cached picks onto the deterministic tier-graded baseline. Absent cache →
the tier-graded baseline alone. See directives/key_metrics_picker.md. Normally
runs automatically on the `--enable-llm` build
(execution/build_artifacts.py::_ensure_key_metrics).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from compute.key_metrics import extract_for_ticker  # noqa: E402


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        print(f"[error] no DB at {db_path}", file=sys.stderr)
        return 1

    tickers = _resolve_tickers(repo_root, args)
    if not tickers:
        print("[]")
        return 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    summary: list[dict[str, object]] = []
    try:
        for ticker in tickers:
            try:
                result = extract_for_ticker(ticker, repo_root, conn, refresh=args.refresh)
            except Exception as e:  # report per-ticker, keep going
                summary.append({"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
                continue
            summary.append(
                {
                    "ticker": ticker,
                    "metrics": [m["token"] for m in result.metrics],
                    "skipped": result.skipped_reason,
                }
            )
    finally:
        conn.close()

    print(json.dumps(summary, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker")
    g.add_argument(
        "--all",
        action="store_true",
        help="All tickers that produce briefs (portfolio + evaluation).",
    )
    p.add_argument("--refresh", action="store_true", help="Ignore cache and re-generate")
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    db_path = repo_root / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    # `--all` is scoped to briefed list types (portfolio + evaluation) — the
    # names that carry a DIY picker key-metrics row.
    cur.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.BRIEFED_LIST_TYPES_SQL} ORDER BY ticker"
    )
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


if __name__ == "__main__":
    raise SystemExit(main())
