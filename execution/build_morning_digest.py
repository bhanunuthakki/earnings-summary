"""Build the daily morning HTML digest for the dashboard.

Usage:
    python execution/build_morning_digest.py
    python execution/build_morning_digest.py --date 2026-05-27
    python execution/build_morning_digest.py --date 2026-05-01 --out tmp/x.html
    python execution/build_morning_digest.py --user-id bhanu

Writes one HTML file to ``data/dashboard/morning/<YYYY-MM-DD>.html`` by
default and overwrites ``data/dashboard/morning/index.html`` as a copy of
today's digest so an "open the index" workflow stays cheap. Empty-state
digests still produce a valid HTML file — the morning workflow expects a
file at the canonical path every day.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from dashboard import render_morning_digest  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402


def _default_out(repo_root: Path, render_date: date) -> Path:
    return repo_root / "data" / "dashboard" / "morning" / f"{render_date.isoformat()}.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Date to render the digest for (YYYY-MM-DD). Defaults to today (UTC).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output HTML path. Defaults to data/dashboard/morning/<date>.html.",
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help="user_id to scope the digest to. Defaults to $CIO_USER_ID or 'bhanu'.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db. Defaults to the worktree root.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the DB path (else uses data/portfolio.db under --repo-root).",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Skip writing/overwriting the morning/index.html copy.",
    )
    args = parser.parse_args()

    render_date: date = args.date if args.date is not None else datetime.now(UTC).date()
    repo_root: Path = args.repo_root.resolve()
    db_path: Path | None = args.db_path
    if db_path is None:
        candidate = repo_root / "data" / "portfolio.db"
        db_path = candidate if candidate.exists() else None

    out_path: Path = args.out if args.out is not None else _default_out(repo_root, render_date)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    html_text = render_morning_digest(
        date=render_date, user_id=args.user_id, db_path=db_path
    )
    out_path.write_text(html_text, encoding="utf-8")

    if not args.no_index:
        index_path = out_path.parent / "index.html"
        shutil.copyfile(out_path, index_path)

    rel = _relpath(out_path, PROJECT_ROOT)
    sys.stdout.write(
        f"Wrote {rel} ({len(html_text):,} chars; absolute: {out_path})\n"
    )
    return 0


def _relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    sys.exit(main())
