"""Grade prior LLM bear-case failure modes against subsequent data.

Two-step:
  1. Materialize failure modes from cached bear_case artifacts into the
     predictions table (idempotent).
  2. For each prediction whose target_period has passed (default 90d after
     made_at), call the LLM grader to emit outcome=met/missed/mixed/
     unfalsifiable.

The output feeds the `mgmt_credibility_score` lens and the dashboard's
predictions-outcomes panel.

Usage:
    python execution/grade_bear_cases.py --ticker GOOG
    python execution/grade_bear_cases.py --all-portfolio
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bear_case_grader import grade_due_predictions, materialize_predictions  # noqa: E402
from llm.calibration import CalibrationScore, record_score  # noqa: E402
from llm.prompt_versions import prompt_version_for  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

log = logging.getLogger("grade_bear_cases")


def _aggregate_to_calibration_score(outcomes: dict[str, int]) -> float | None:
    """Translate met/missed/mixed/unfalsifiable counts into a 0..1 prompt-quality score.

    Convention: a bear-case prediction that 'met' is the prompt being right
    about a failure mode; 'mixed' is half-credit; 'unfalsifiable' is dropped
    (not a signal). Returns None when no falsifiable predictions landed.
    """
    counted = outcomes.get("met", 0) + outcomes.get("missed", 0) + outcomes.get("mixed", 0)
    if counted == 0:
        return None
    score = (outcomes.get("met", 0) + 0.5 * outcomes.get("mixed", 0)) / counted
    return float(score)


def _portfolio_tickers(repo_root: Path) -> list[str]:
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return []
    conn = connect_sqlite(str(db), role=SQLiteConnectionRole.READ_ONLY)
    try:
        return [
            r[0]
            for r in conn.execute(
                """
                SELECT ticker FROM tracked_companies
                WHERE archived_at IS NULL AND list_type = 'portfolio'
                ORDER BY ticker
                """
            )
        ]
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker to grade.")
    g.add_argument(
        "--all-portfolio",
        action="store_true",
        help="Grade every portfolio holding's bear cases.",
    )
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root.")
    parser.add_argument(
        "--skip-materialize",
        action="store_true",
        help="Skip the materialization step (only re-grade existing pending predictions).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    tickers = [args.ticker.upper()] if args.ticker else _portfolio_tickers(args.repo_root)

    total_inserted = 0
    total_graded: dict[str, int] = {"met": 0, "missed": 0, "mixed": 0, "unfalsifiable": 0}
    db_path = args.repo_root / "data" / "portfolio.db"
    for ticker in tickers:
        if not args.skip_materialize:
            inserted = materialize_predictions(ticker=ticker, repo_root=args.repo_root)
            log.info({"event": "materialized", "ticker": ticker, "inserted": inserted})
            total_inserted += inserted
        outcomes = grade_due_predictions(ticker=ticker, repo_root=args.repo_root)
        log.info({"event": "graded", "ticker": ticker, "outcomes": outcomes})
        for k, v in outcomes.items():
            total_graded[k] = total_graded.get(k, 0) + v
        # Calibration hook: durable-log this ticker's quality score so
        # summarize_by_prompt_version can answer "is the bear_case prompt
        # producing better failure modes after the v3 refactor?". Best-effort —
        # missing table or zero falsifiable predictions silently skips.
        per_ticker_score = _aggregate_to_calibration_score(outcomes)
        if per_ticker_score is not None:
            record_score(
                CalibrationScore(
                    purpose="bear_case",
                    prompt_version=prompt_version_for("bear_case"),
                    ticker=ticker,
                    score=per_ticker_score,
                    reason=(
                        f"met={outcomes.get('met', 0)} "
                        f"missed={outcomes.get('missed', 0)} "
                        f"mixed={outcomes.get('mixed', 0)}"
                    ),
                    scored_by="auto:grade_bear_cases",
                ),
                db_path=db_path,
            )

    print(
        f"\nGrading complete · {total_inserted} predictions materialized · "
        f"{sum(total_graded.values())} graded: {total_graded}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
