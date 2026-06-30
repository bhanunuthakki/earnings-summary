"""Check EDGAR for a watched competitor's IPO S-1 and persist any hit to `news`.

Watches ``micro_thesis/competitive/sec_watch.json`` (Cohesity -> RBRK). When the
competitor files its S-1, a ``news`` row tagged ``edgar_s1_watch`` is written
under the affected holding's ticker, surfacing in that holding's feed and
flipping the competitive KPI in ``RBRK.json``. Until then this is a no-op (the
competitor hasn't filed). Also runs daily as an additive stage inside
``execution/fetch_news.py``.

Usage:
    python execution/check_competitor_s1.py
    python execution/check_competitor_s1.py --db /tmp/x.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from competitive.sec_watch import run as run_s1_watch  # noqa: E402
from db import DB_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="Path to portfolio.db")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root")
    args = parser.parse_args(argv)

    db_path = args.db or DB_PATH
    inserted, deduped = run_s1_watch(args.repo_root.resolve(), db_path=db_path)
    print(json.dumps({"event": "s1_watch_done", "inserted": inserted, "deduped": deduped}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
