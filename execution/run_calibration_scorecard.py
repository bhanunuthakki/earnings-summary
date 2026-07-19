"""Generate the owner's monthly calibration scorecard (close_the_loops L8).

The compounding loop's monthly readout: the deterministic substrate (period-
over-period hit-rate trend + realized selection/sizing/timing skill split) plus
the eval-gated coach prose (2-3 NAMED recurring biases evidenced from the
owner's own ledger + one falsifiable behavioural experiment). Grounded entirely
in his own graded history; below the shared min-n floor it renders the substrate
with an honest "too thin to coach yet" note and makes NO LLM call.

The synthesised prose is judged inline against evals/rubrics/calibration_coach.md
BEFORE it is persisted — a coach that misreads the owner's history is worse than
silence, so a below-threshold scorecard is saved with the prose suppressed. The
weekly calibration-grading cron re-audits the saved scorecards over time.

Entry points:
* **Monthly cron** — cron/monthly_calibration_scorecard.task.xml runs this on
  the 2nd of each month (after the 1st's advisor-memo run, so the ledgers are
  fresh).
* **Manual** — run it directly; --period overrides the YYYY-MM default.

Usage:
    python execution/run_calibration_scorecard.py
    python execution/run_calibration_scorecard.py --period 2026-06
    python execution/run_calibration_scorecard.py --no-gate   # skip the eval gate

Exit status: 0 when a scorecard was generated + saved (a thin-ledger substrate
scorecard counts as success — it is the honest output), 2 on a hard stop
(budget block / missing CLI — see llm.cli.is_hard_stop).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

log = logging.getLogger("run_calibration_scorecard")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--code-root",
        type=Path,
        default=PROJECT_ROOT,
        help="checkout whose evals/rubrics + git sha stamp the eval gate",
    )
    parser.add_argument(
        "--period",
        default=None,
        help="scorecard period key (default: this month, YYYY-MM)",
    )
    parser.add_argument("--user-id", default=None, help="defaults to identity.DEFAULT_USER_ID")
    parser.add_argument("--api-url", default=None, help="tracker base URL (default: env/localhost)")
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="skip the eval gate (the prose is saved unjudged — for local dev only)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from calibration_coach import build_scorecard, gate_scorecard, save_scorecard
    from decision_calibration import compute_advice_influence
    from identity import DEFAULT_USER_ID
    from llm.cli import is_hard_stop

    now = datetime.now(UTC).replace(tzinfo=None)
    period = args.period or now.strftime("%Y-%m")
    user_id = args.user_id or DEFAULT_USER_ID

    try:
        card = build_scorecard(
            args.repo_root,
            period=period,
            generated_at=now.isoformat(timespec="seconds"),
            user_id=user_id,
            api_url=args.api_url,
        )
        if card.can_coach and not args.no_gate:
            card = gate_scorecard(card, code_root=args.code_root)
    except Exception as exc:
        if is_hard_stop(exc):
            log.error({"event": "scorecard_hard_stop", "error": f"{type(exc).__name__}: {exc}"})
            return 2
        raise

    path = save_scorecard(args.repo_root, card)
    gate = (
        "ungated"
        if card.coach_quality_ok is None
        else ("passed" if card.coach_quality_ok else "SUPPRESSED")
    )

    # Advice-influence read (tenet-2 Phase 5, §5.2) — a separate, purely
    # descriptive, no-LLM partition riding this same monthly job. Computed
    # over v_decision_journal directly (not through calibration_coach's
    # eval-gated CalibrationScorecard machinery — there is no prose here to
    # gate); persisted as a sibling artifact so it never has to touch that
    # dataclass's manual encode/decode. None when the view doesn't exist yet
    # (pre-0179 DB) — nothing is written or logged for it in that case.
    advice = compute_advice_influence(args.repo_root / "data" / "portfolio.db")
    advice_path: Path | None = None
    if advice is not None:
        advice_path = (
            Path(args.repo_root)
            / "data"
            / "calibration_scorecard"
            / f"{period}_advice_influence.json"
        )
        advice_path.parent.mkdir(parents=True, exist_ok=True)
        advice_path.write_text(
            json.dumps(dataclasses.asdict(advice), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    log.info(
        {
            "event": "scorecard_saved",
            "period": period,
            "path": str(path),
            "can_coach": card.can_coach,
            "n_graded": card.n_graded,
            "biases": len(card.biases),
            "experiment": card.experiment is not None,
            "eval_gate": gate,
            "coach_quality_score": card.coach_quality_score,
            "advice_influence_total_graded": advice.total_graded if advice else None,
            "advice_influence_advice_before_n": advice.advice_before_n if advice else None,
            "advice_influence_path": str(advice_path) if advice_path else None,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
