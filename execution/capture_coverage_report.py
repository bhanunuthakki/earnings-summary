"""Print the capture-every-number coverage summary.

Rolls up ``data/capture_coverage_log.json`` (written by the Stage-A XBRL walker
and the Stage-B enumerate pass) into headline coverage: how many reported numbers
each pass SAW, how many it captured, and where the rest went (deferred by reason
/ dropped by the persist guards). This is the measurable answer to "did we
actually capture every number, and if not, why not?".

Usage:
    python execution/capture_coverage_report.py
    python execution/capture_coverage_report.py --repo-root /path/to/repo
    python execution/capture_coverage_report.py --ticker NU
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.capture_coverage import load_coverage, summarize  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--ticker", help="Restrict the summary to one ticker.")
    args = parser.parse_args()

    records = load_coverage(args.repo_root.resolve())
    if args.ticker:
        want = args.ticker.upper()
        records = [r for r in records if r.ticker.upper() == want]
    if not records:
        print("No capture coverage records found.")
        return 0

    print(json.dumps(summarize(records), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
