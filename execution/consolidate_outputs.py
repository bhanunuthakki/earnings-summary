"""
execution/consolidate_outputs.py
--------------------------------
CLI for src/output_consolidator.consolidate_all().

  python execution/consolidate_outputs.py                # portfolio + watchlist
  python execution/consolidate_outputs.py --portfolio    # portfolio only

Output:
  outputs/index.html                — portfolio + watchlist dashboard
  outputs/<TICKER>/index.html       — per-ticker landing page
  outputs/<TICKER>/memo_*.html      — copy of latest memo
  outputs/<TICKER>/thesis_tracker_*.md — copy of latest tracker
  outputs/<TICKER>/master_transcripts.pdf — copy of latest master PDF

Each artifact is registered in the SQLite `output_artifacts` table with
is_latest=1 (older entries get demoted), so downstream consumers can query:
  SELECT path FROM output_artifacts WHERE ticker=? AND kind='memo' AND is_latest=1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.append(str(SRC_DIR))

from output_consolidator import OUTPUTS_DIR, consolidate_all  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Consolidate per-ticker outputs into outputs/.")
    parser.add_argument("--portfolio", action="store_true",
                        help="Portfolio only (skip watchlist).")
    args = parser.parse_args()

    arts = consolidate_all(include_watchlist=not args.portfolio)
    have_memo = sum(1 for a in arts if a.memo_dest is not None)
    have_tracker = sum(1 for a in arts if a.tracker_dest is not None)
    have_master = sum(1 for a in arts if a.master_dest is not None)
    print(f"[consolidate] {len(arts)} ticker(s) processed -> {OUTPUTS_DIR.relative_to(PROJECT_ROOT)}/")
    print(f"  memos:    {have_memo}/{len(arts)} present")
    print(f"  trackers: {have_tracker}/{len(arts)} present")
    print(f"  master pdfs: {have_master}/{len(arts)} present")
    print(f"  dashboard: outputs/index.html")


if __name__ == "__main__":
    main()
