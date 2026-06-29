"""execution/seed_themes.py — day-one Ledger seeding from the interview artifact.

Reads ``data/ledger_seed/seed.json`` (produced by the spawned seeding-interview
session) and ingests it DETERMINISTICALLY: musings land as captured musings,
decisions as decision notes, themes as theme insights. Idempotent (per-item
source_ref), so re-running after a richer interview only adds new items.

    python execution/seed_themes.py
    python execution/seed_themes.py --seed-json /path/to/seed.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from synthesis import seed  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--seed-json", type=Path, default=None)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    seed_path = args.seed_json or (repo_root / "data" / "ledger_seed" / "seed.json")
    if not seed_path.exists():
        print(f"seed_themes: no seed artifact at {seed_path}; nothing to do", file=sys.stderr)
        return 0
    counts = seed.seed_from_json(db_path, seed_path)
    print(f"seed_themes: {counts}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
