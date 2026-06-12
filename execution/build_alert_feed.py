"""Build the persistent chronological alert feed HTML for the dashboard.

Usage:
    python execution/build_alert_feed.py
    python execution/build_alert_feed.py --ticker GOOG
    python execution/build_alert_feed.py --trigger-kind kpi_inflection
    python execution/build_alert_feed.py --status pending --limit 50
    python execution/build_alert_feed.py --out tmp/feed.html

Default output: ``data/dashboard/feed.html``. Filters compose with AND;
unset filters mean "no constraint" (the feed renders everything for the
user, capped at --limit, newest first).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from dashboard import render_alert_feed  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402


def _default_out(repo_root: Path) -> Path:
    return repo_root / "data" / "dashboard" / "feed.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output HTML path. Defaults to data/dashboard/feed.html.",
    )
    parser.add_argument(
        "--ticker",
        default=None,
        help="Filter alerts to one ticker (e.g. GOOG). Default: no filter.",
    )
    parser.add_argument(
        "--trigger-kind",
        default=None,
        help="Filter to one trigger_kind. Default: no filter.",
    )
    parser.add_argument(
        "--status",
        default=None,
        choices=["pending", "approved", "dismissed", "expired"],
        help="Filter to one alert status. Default: no filter.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum number of alerts to render. Default: 200.",
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help="user_id to scope the feed to. Defaults to $CIO_USER_ID or 'bhanu'.",
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
    args = parser.parse_args()

    repo_root: Path = args.repo_root.resolve()
    db_path: Path | None = args.db_path
    if db_path is None:
        candidate = repo_root / "data" / "portfolio.db"
        db_path = candidate if candidate.exists() else None

    out_path: Path = args.out if args.out is not None else _default_out(repo_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    html_text = render_alert_feed(
        user_id=args.user_id,
        limit=args.limit,
        ticker=args.ticker,
        trigger_kind=args.trigger_kind,
        status=args.status,
        db_path=db_path,
    )
    out_path.write_text(html_text, encoding="utf-8")

    rel = _relpath(out_path, PROJECT_ROOT)
    sys.stdout.write(f"Wrote {rel} ({len(html_text):,} chars; absolute: {out_path})\n")
    return 0


def _relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    sys.exit(main())
