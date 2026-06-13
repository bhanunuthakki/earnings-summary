"""execution/summarize_podcast_episodes.py — nightly podcast takeaway pass.

Runs after execution/fetch_podcast_rss.py has written ``media_appearance``
signals with raw RSS copy in the body. This script calls
``signals.takeaway.summarize_pending`` to replace short/absent bodies with
2-4 sentence investment-relevant briefings (Sonnet, purpose
``podcast_takeaway_summary``, $5/month budget cap via migration 0103).

Usage:
    python execution/summarize_podcast_episodes.py
    python execution/summarize_podcast_episodes.py --limit 50
    python execution/summarize_podcast_episodes.py --repo-root /path/to/repo
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.cli import is_hard_stop  # noqa: E402
from signals.takeaway import summarize_pending  # noqa: E402

log = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Nightly podcast takeaway summarizer")
    p.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Project root (default: parent of this script)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max signals to summarize per run (default: 20)",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args()
    repo_root = (args.repo_root or PROJECT_ROOT).resolve()
    db_path = repo_root / "data" / "earnings.db"

    log.info({"event": "summarize_podcast_start", "db": str(db_path), "limit": args.limit})
    try:
        written = summarize_pending(db_path, limit=args.limit)
    except Exception as exc:
        if is_hard_stop(exc):
            log.warning({"event": "summarize_podcast_hard_stop", "error": str(exc)})
            return 0
        raise
    log.info({"event": "summarize_podcast_done", "written": written})
    return 0


if __name__ == "__main__":
    sys.exit(main())
