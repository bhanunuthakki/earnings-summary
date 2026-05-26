"""Extract LLM recommendations from recent five-min-reread artifacts and
upsert them into the `decisions` audit ledger.

Idempotent — re-running on the same artifact is a no-op. The first run on
existing artifacts inserts; subsequent runs find them and skip.

Usage:
    python execution/record_decisions.py
    python execution/record_decisions.py --since-days 90
    python execution/record_decisions.py --llm-fallback  # try Haiku on ambiguous
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _sync_db_path(repo_root: Path) -> None:
    """Point db.DB_PATH at the caller's repo so the recorder lands rows in
    the right database (same pattern as run_lens.py / build_artifacts.py)."""
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


from decision_extractor import record_decisions_from_artifacts  # noqa: E402

log = logging.getLogger("record_decisions")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-days",
        type=int,
        default=30,
        help="Look back this many days for lens artifacts to extract from.",
    )
    parser.add_argument(
        "--llm-fallback",
        action="store_true",
        help="Enable Haiku fallback for artifacts where the regex fails despite "
        "a present 'Recommended action' section.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    _sync_db_path(args.repo_root.resolve())

    tally = record_decisions_from_artifacts(
        repo_root=args.repo_root,
        since_days=args.since_days,
        llm_fallback=args.llm_fallback,
    )
    log.info({"event": "record_decisions_done", **tally})
    print(
        "Decision recorder complete · "
        f"inserted={tally['inserted']} · "
        f"skipped_existing={tally['skipped_existing']} · "
        f"no_recommendation={tally['no_recommendation']} · "
        f"db_unavailable={tally['db_unavailable']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
