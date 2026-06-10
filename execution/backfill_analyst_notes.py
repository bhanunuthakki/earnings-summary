"""execution/backfill_analyst_notes.py
--------------------------------------
One-shot (re-runnable) backfill of the durable analyst_notes table from
every on-disk report-comment store.

Comments live per (ticker, report_date) under
``data/report_comments/<TICKER>/<YYYY-MM-DD>.json`` and historically died
with their report build. This walks every store and reconciles it through
``user_state.notes.sync_store_comments`` so years of recorded thinking
land in analyst_notes (alembic 0074). Fully idempotent — the reconciler
upserts on ``source_ref`` — so re-running after new comments arrive is
safe (the live write-path hook in ``src/comments.py`` normally keeps the
table current; this CLI is the catch-up / first-run path).

Usage:
    python execution/backfill_analyst_notes.py
    python execution/backfill_analyst_notes.py --ticker NU
    python execution/backfill_analyst_notes.py --repo-root C:/path/to/repo
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from user_state.notes import SyncStats, sync_store_comments  # noqa: E402


@dataclass(slots=True)
class BackfillResult:
    """Aggregate outcome across every comment store walked."""

    files: int = 0
    bad_files: int = 0
    stats: SyncStats | None = None

    def add(self, s: SyncStats) -> None:
        if self.stats is None:
            self.stats = SyncStats()
        self.stats.created += s.created
        self.stats.updated += s.updated
        self.stats.archived += s.archived
        self.stats.skipped += s.skipped


def backfill(
    repo_root: Path,
    db_path: Path,
    *,
    only_ticker: str | None = None,
    log: bool = True,
) -> BackfillResult:
    """Walk data/report_comments and reconcile every store into analyst_notes."""
    comments_dir = repo_root / "data" / "report_comments"
    result = BackfillResult()
    if not comments_dir.exists():
        if log:
            print(f"no comment stores under {comments_dir} - nothing to backfill")
        return result
    for path in sorted(comments_dir.glob("*/*.json")):
        ticker = path.parent.name.upper()
        if only_ticker is not None and ticker != only_ticker.upper():
            continue
        try:
            report_date = date.fromisoformat(path.stem)
        except ValueError:
            result.bad_files += 1
            if log:
                print(f"  skip {path.name} under {ticker}: filename is not YYYY-MM-DD")
            continue
        stats = sync_store_comments(
            repo_root, ticker=ticker, report_date=report_date, db_path=db_path
        )
        result.files += 1
        result.add(stats)
        if log:
            print(
                f"  {ticker} {report_date}: created={stats.created} updated={stats.updated} "
                f"archived={stats.archived} skipped={stats.skipped}"
            )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite DB path (default: <repo-root>/data/portfolio.db)",
    )
    parser.add_argument("--ticker", default=None, help="limit to one ticker")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    db_path = (args.db or (repo_root / "data" / "portfolio.db")).resolve()
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    print(f"backfilling analyst_notes from {repo_root / 'data' / 'report_comments'}")
    result = backfill(repo_root, db_path, only_ticker=args.ticker)
    s = result.stats or SyncStats()
    print(
        f"done: {result.files} store(s) -> created={s.created} updated={s.updated} "
        f"archived={s.archived} skipped={s.skipped}"
        + (f" ({result.bad_files} unparseable filename(s))" if result.bad_files else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
