"""Archive older <DATE>-prefixed artifacts under output/research/<TICKER>/ into
output/research/<TICKER>/archive/, leaving only the latest of each artifact
kind at the top level.

"Kind" is the filename suffix after the `YYYY-MM-DD_` date prefix
(e.g. `workspace.html`, `report.md`, `dcf.xlsx`, `sections.json`). Each
kind is tracked independently, so the freshest file of every kind survives
at the top level even when they were produced on different dates.

Files without a `YYYY-MM-DD_` prefix are ignored — non-dated artifacts (e.g.
a top-level `index.html`) survive any run. The `archive/` subdirectory itself
is never re-archived.

Called as a per-ticker post-step from `daily_fetch_and_brief.py` so each
freshly-rebuilt ticker self-cleans. Also runnable standalone:

    python execution/sweep_output_history.py                # archive every ticker
    python execution/sweep_output_history.py --ticker META  # one ticker
    python execution/sweep_output_history.py --dry-run      # preview only
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_DIRNAME = "archive"

_DATE_PREFIX_RX = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)$")


def main() -> int:
    args = _parse_args()
    research_dir = args.repo_root.resolve() / "output" / "research"
    if not research_dir.exists():
        sys.stderr.write(f"FATAL: no output/research dir at {research_dir}\n")
        return 2

    ticker_dirs = _resolve_ticker_dirs(research_dir, args.ticker)
    if not ticker_dirs:
        print(json.dumps({"event": "no_tickers", "scope": args.ticker or "all"}))
        return 0

    total_archived = 0
    for ticker_dir in ticker_dirs:
        kept, archived = archive_ticker_history(ticker_dir, dry_run=args.dry_run)
        total_archived += archived
        if archived or args.verbose:
            print(
                json.dumps(
                    {
                        "event": "archived_ticker",
                        "ticker": ticker_dir.name,
                        "kept_files": kept,
                        "archived_files": archived,
                    }
                )
            )

    print(
        json.dumps(
            {
                "event": "done",
                "tickers": len(ticker_dirs),
                "archived_files": total_archived,
                "dry_run": args.dry_run,
            }
        )
    )
    return 0


def archive_ticker_history(ticker_dir: Path, *, dry_run: bool) -> tuple[int, int]:
    """Move every dated file except the latest-of-its-kind into <ticker_dir>/archive/.

    Returns (kept_files, archived_files). Files in archive/ itself are not
    re-scanned. Destination collisions overwrite (artifacts are deterministic
    per ticker+date, so a same-named file is the same content).
    """
    if not ticker_dir.is_dir():
        return 0, 0

    files_by_kind: dict[str, list[tuple[str, Path]]] = {}
    for f in ticker_dir.iterdir():
        if not f.is_file():
            continue
        m = _DATE_PREFIX_RX.match(f.name)
        if not m:
            continue
        date, kind = m.group(1), m.group(2)
        files_by_kind.setdefault(kind, []).append((date, f))

    if not files_by_kind:
        return 0, 0

    archive_dir = ticker_dir / ARCHIVE_DIRNAME
    kept = 0
    archived = 0
    for _kind, entries in files_by_kind.items():
        entries.sort(key=lambda t: t[0], reverse=True)
        # Keep the newest date for this kind; archive the rest.
        kept += 1
        for _date, path in entries[1:]:
            if not dry_run:
                _archive_file(path, archive_dir)
            archived += 1

    return kept, archived


def _archive_file(src: Path, archive_dir: Path) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = archive_dir / src.name
    if dst.exists():
        dst.unlink()
    shutil.move(str(src), str(dst))


def _resolve_ticker_dirs(research_dir: Path, ticker: str | None) -> list[Path]:
    if ticker:
        td = research_dir / ticker.upper()
        return [td] if td.is_dir() else []
    return sorted(p for p in research_dir.iterdir() if p.is_dir() and p.name != ARCHIVE_DIRNAME)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--ticker",
        help="Archive only this ticker (default: every dir under output/research/)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be archived without touching anything.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Emit a per-ticker line even when nothing was archived.",
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
