"""Manual news refresh — wholesale brief regen with a fresh WebSearch.

Phase 4 design (Q9b iii): "the news section is refreshable independently;
brief is regenerated wholesale on news refresh." This is the on-demand CLI
that does that. It delegates to `execution/build_artifacts.py` with
`--refresh-news --enable-llm` so the §8 recent_developments section bypasses
the cache and re-queries WebSearch.

Usage:
    python execution/refresh_news.py --ticker META          # one ticker
    python execution/refresh_news.py --all-tracked          # everyone
    python execution/refresh_news.py --ticker META --news-days 14
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    args = _parse_args()
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "execution" / "build_artifacts.py"),
        "--repo-root",
        str(args.repo_root.resolve()),
        "--enable-llm",
        "--refresh-news",
        "--news-days",
        str(args.news_days),
        "--trigger",
        "news_refresh",
        "--allow-untracked",
    ]
    if args.ticker:
        cmd += ["--ticker", args.ticker.upper()]
    else:
        cmd.append("--all-tracked")
    return subprocess.run(cmd, check=False).returncode


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker to refresh news for")
    g.add_argument(
        "--all-tracked", action="store_true", help="Refresh news for every tracked ticker"
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, micro_thesis/. Default: this repo.",
    )
    p.add_argument(
        "--news-days",
        type=int,
        default=7,
        help="WebSearch lookback window in days. Default 7.",
    )
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
