"""execution/run_ledger_synthesis.py — synthesize per-holding stances from musings.

A daily scheduled task (cron/ledger_synthesis.task.xml). INCREMENTAL: only
holdings whose musings advanced past their last-synthesized watermark call the
LLM (``theme_synthesis``, Sonnet, budget 0119 skip-mode); every other scope reuses
its cached stance at zero cost. Degrade-safe: a per-scope LLM failure leaves the
prior stance standing.

    python execution/run_ledger_synthesis.py
    python execution/run_ledger_synthesis.py --repo-root /path/to/repo
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from synthesis import theme_synth  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    counts = theme_synth.run_synthesis(db_path)
    print(f"run_ledger_synthesis: {counts}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
