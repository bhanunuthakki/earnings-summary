"""Record decisions from memo artifacts + extract their falsifiable conditions.

Three idempotent rungs, in dependency order:

  1. ``record_decisions_from_artifacts`` — parse the "Recommended action"
     verdicts out of recent five-min-reread artifacts into the ``decisions``
     ledger (UNIQUE on source_artifact_id).
  2. ``record_socratic_decisions`` — give Socratic advisor memos (which live
     in advisor_memos, not llm_artifacts) their decisions rows (partial
     UNIQUE on source_memo_id, alembic 0086).
  3. ``attach_conditions`` — extract each new decision's "What would change
     my mind" section into structured ``decision_conditions`` JSON (Haiku via
     the ``decision_conditions_extract`` purpose) so the decision_condition
     trigger can evaluate them against incoming facts.

Re-running is a no-op on already-processed rows. Runs as a morning-pipeline
stage (before triggers, so same-run satisfaction checks see fresh conditions)
and standalone:

Usage:
    python execution/record_decisions.py
    python execution/record_decisions.py --since-days 90
    python execution/record_decisions.py --llm-fallback  # try Haiku on ambiguous
    python execution/record_decisions.py --no-conditions # ledger rungs only
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


from decision_conditions import (  # noqa: E402
    attach_conditions,
    attach_qualitative_conditions,
    record_socratic_decisions,
)
from decision_extractor import record_decisions_from_artifacts  # noqa: E402

log = logging.getLogger("record_decisions")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-days",
        type=int,
        default=30,
        help="Look back this many days for lens artifacts / Socratic memos.",
    )
    parser.add_argument(
        "--llm-fallback",
        action="store_true",
        help="Enable Haiku fallback for artifacts where the regex fails despite "
        "a present 'Recommended action' section.",
    )
    parser.add_argument(
        "--no-conditions",
        action="store_true",
        help="Skip the falsifiable-condition extraction rung (ledger rungs only).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the portfolio DB path (wins over --repo-root derivation).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repo_root = args.repo_root.resolve()
    _sync_db_path(repo_root)
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )
    if args.db_path is not None:
        import db

        db.DB_PATH = str(args.db_path)

    tally = record_decisions_from_artifacts(
        repo_root=repo_root,
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

    socratic = record_socratic_decisions(db_path=db_path, since_days=args.since_days)
    log.info({"event": "record_socratic_decisions_done", **socratic})
    print(
        "Socratic memo recorder complete · "
        f"inserted={socratic['inserted']} · "
        f"skipped_existing={socratic['skipped_existing']} · "
        f"skipped_no_stance={socratic['skipped_no_stance']} · "
        f"db_unavailable={socratic['db_unavailable']}"
    )

    if args.no_conditions:
        return 0

    conditions = attach_conditions(db_path=db_path)
    log.info({"event": "attach_conditions_done", **conditions})
    print(
        "Condition extraction complete · "
        f"extracted={conditions['extracted']} · "
        f"no_conditions={conditions['no_conditions']} · "
        f"no_section={conditions['no_section']} · "
        f"parse_failed={conditions['parse_failed']} · "
        f"db_unavailable={conditions['db_unavailable']}"
    )

    # The qualitative twin (L9 PR2): the non-numeric "what would change my mind"
    # conditions the numeric pass skips, routed to the news/earnings-tone
    # triggers. Separate stamp + own LLM purpose, so it neither blocks nor is
    # blocked by the numeric pass.
    qualitative = attach_qualitative_conditions(db_path=db_path)
    log.info({"event": "attach_qualitative_conditions_done", **qualitative})
    print(
        "Qualitative-condition extraction complete · "
        f"extracted={qualitative['extracted']} · "
        f"no_conditions={qualitative['no_conditions']} · "
        f"no_section={qualitative['no_section']} · "
        f"parse_failed={qualitative['parse_failed']} · "
        f"db_unavailable={qualitative['db_unavailable']}"
    )
    # parse_failed rows stay unstamped and retry next run; surface in the exit
    # code so the morning pipeline's stage status reflects the partial failure.
    return 1 if (conditions["parse_failed"] or qualitative["parse_failed"]) else 0


if __name__ == "__main__":
    sys.exit(main())
