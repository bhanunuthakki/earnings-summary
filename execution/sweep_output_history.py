"""Keep the latest N <DATE>-prefixed artifact sets in output/research/<TICKER>/
and delete the rest. Counts distinct dates (not files), so all flavor variants
of the same date (e.g. META's `_portfolio_report.*` + `_eval_report.*`) share
a single retained slot.

Files without a `YYYY-MM-DD_` prefix are ignored — non-dated artifacts like
top-level `index.html` (if reintroduced later) survive any sweep.

Default cadence is destructive; pass `--dry-run` for a preview.

Usage:
    python execution/sweep_output_history.py                # delete all but latest 5 dates per ticker
    python execution/sweep_output_history.py --dry-run      # preview only
    python execution/sweep_output_history.py --keep 10      # keep 10 dates instead
    python execution/sweep_output_history.py --ticker META  # one ticker
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEEP = 5

_DATE_PREFIX_RX = re.compile(r"^(\d{4}-\d{2}-\d{2})_")


def main() -> int:
    args = _parse_args()
    research_dir = args.repo_root.resolve() / "output" / "research"
    if not research_dir.exists():
        sys.stderr.write(f"FATAL: no output/research dir at {research_dir}\n")
        return 2
    if args.keep < 1:
        sys.stderr.write(f"FATAL: --keep must be >= 1 (got {args.keep})\n")
        return 2

    ticker_dirs = _resolve_ticker_dirs(research_dir, args.ticker)
    if not ticker_dirs:
        print(json.dumps({"event": "no_tickers", "scope": args.ticker or "all"}))
        return 0

    total_deleted = 0
    for ticker_dir in ticker_dirs:
        kept, deleted = sweep_ticker(ticker_dir, args.keep, dry_run=args.dry_run)
        total_deleted += deleted
        if deleted or args.verbose:
            print(json.dumps({
                "event": "swept_ticker",
                "ticker": ticker_dir.name,
                "kept_dates": kept,
                "deleted_files": deleted,
            }))

    print(json.dumps({
        "event": "done",
        "tickers": len(ticker_dirs),
        "keep": args.keep,
        "deleted_files": total_deleted,
        "dry_run": args.dry_run,
    }))
    return 0


def sweep_ticker(ticker_dir: Path, keep: int, *, dry_run: bool) -> tuple[int, int]:
    """Sweep one ticker's directory. Returns (kept_dates, deleted_files)."""
    files_by_date: dict[str, list[Path]] = {}
    for f in ticker_dir.iterdir():
        if not f.is_file():
            continue
        m = _DATE_PREFIX_RX.match(f.name)
        if not m:
            continue
        files_by_date.setdefault(m.group(1), []).append(f)

    if not files_by_date:
        return 0, 0

    sorted_dates = sorted(files_by_date.keys(), reverse=True)
    keep_dates = sorted_dates[:keep]
    delete_dates = sorted_dates[keep:]

    deleted = 0
    for d in delete_dates:
        for f in files_by_date[d]:
            if not dry_run:
                f.unlink()
            deleted += 1

    return len(keep_dates), deleted


def _resolve_ticker_dirs(research_dir: Path, ticker: str | None) -> list[Path]:
    if ticker:
        td = research_dir / ticker.upper()
        return [td] if td.is_dir() else []
    return sorted(p for p in research_dir.iterdir() if p.is_dir())


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--ticker",
        help="Sweep only this ticker (default: every dir under output/research/)",
    )
    p.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"Number of latest distinct dates to retain per ticker. Default {DEFAULT_KEEP}.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without touching anything.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Emit a per-ticker line even when nothing was deleted.",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing output/research/. Default: this repo.",
    )
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
