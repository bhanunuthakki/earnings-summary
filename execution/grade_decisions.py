"""Grade pending decisions against subsequent realized price moves.

For each decision where outcome_at IS NULL and made_at is older than the
threshold (default 30 days), read admitted, typed adjusted prices at made_at and at the
latest available bar, compute the % change, and write an outcome.

Grading heuristic (price-only, no fundamentals):
  ADD / INITIATE:  +5%   = correct, -5%  = wrong, in between = mixed
  TRIM / SELL:     -5%   = correct, +5%  = wrong, in between = mixed
  HOLD:            ±5% (no big move) = correct; a >15% drawdown = wrong; a big
                   rally = mixed (could have added more)
  AVOID / pass:    the INVERSE of a hold — judged by what was MISSED. A name you
                   passed on that ran +15% = wrong (the error of omission); a
                   decline or flat band = correct (the pass was vindicated); a
                   modest +5..15% = mixed. Passes grade on a LONGER horizon
                   (--avoid-after-days, default 180): an omission needs time to
                   reveal itself, and the longer window keeps the pass's
                   falsifiable conditions open to resurface on news/XBRL.

This is a coarse first pass — a richer grader (DCF rebuild, fundamental
delta) is a future-work iteration. The point here is to close the loop:
get an outcome on the record, however rough, so the LLM has SOMETHING to
calibrate against.

Usage:
    python execution/grade_decisions.py
    python execution/grade_decisions.py --since-days 60
    python execution/grade_decisions.py --threshold-pct 0.07
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from db_paths import configured_db_path, db_path_context, require_db_path
from decision_extractor import OutcomeLabel, pending_for_grading, record_outcome
from llm.calibration import CalibrationScore, record_score
from llm.prompt_versions import prompt_version_for
from sources.decision_grading_prices import decision_price_evidence
from sources.readers import ProviderNeutralDataReader, ReaderUnavailableStatus
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger("grade_decisions")


def _decisions_calibration_score(tally: dict[str, int]) -> float | None:
    """Translate correct/wrong/mixed counts into a 0..1 prompt-quality score.

    'correct' = full credit, 'mixed' = half-credit, 'wrong' = zero,
    'unfalsifiable' = dropped. Returns None when no falsifiable
    decisions landed.
    """
    counted = tally.get("correct", 0) + tally.get("wrong", 0) + tally.get("mixed", 0)
    if counted == 0:
        return None
    return (tally.get("correct", 0) + 0.5 * tally.get("mixed", 0)) / counted


def _verdict(*, kind: str, pct_change: float, threshold_pct: float) -> tuple[str, str]:
    """Map (recommendation_kind, realized_pct_change) → (outcome_label, notes).
    Threshold is the symmetric move that flips correct vs wrong."""
    big_move = abs(pct_change) > threshold_pct
    direction = "up" if pct_change >= 0 else "down"

    if kind == "add" or kind == "initiate":
        if pct_change >= threshold_pct:
            return ("correct", f"price moved +{pct_change * 100:.1f}% — ADD captured upside")
        if pct_change <= -threshold_pct:
            return ("wrong", f"price moved {pct_change * 100:.1f}% — ADD into drawdown")
        return (
            "mixed",
            f"price moved {pct_change * 100:.1f}% — flat band, neither vindicated nor refuted",
        )

    if kind == "trim" or kind == "sell":
        if pct_change <= -threshold_pct:
            return ("correct", f"price moved {pct_change * 100:.1f}% — TRIM/SELL avoided drawdown")
        if pct_change >= threshold_pct:
            return ("wrong", f"price moved +{pct_change * 100:.1f}% — TRIM/SELL gave up upside")
        return (
            "mixed",
            f"price moved {pct_change * 100:.1f}% — flat band, neither vindicated nor refuted",
        )

    if kind == "hold":
        if not big_move:
            return ("correct", f"price moved {pct_change * 100:.1f}% — HOLD was right to not act")
        # Holding through a big drawdown is wrong; holding through a big rally is mixed
        # (could have added more)
        if pct_change <= -threshold_pct * 3:  # 15% drawdown threshold for HOLD-wrong
            return (
                "wrong",
                f"price moved {pct_change * 100:.1f}% — HOLD through significant drawdown",
            )
        return (
            "mixed",
            f"price moved {direction} {abs(pct_change * 100):.1f}% — HOLD missed an action signal",
        )

    if kind == "avoid":
        # A pass is the INVERSE of a hold: judged by what was MISSED. A name you
        # avoided that ran away is the error of omission (opportunity cost); a
        # decline or a flat band vindicates the pass. This is the omission side
        # the calibration loop (L1/L8) was blind to until passes became gradeable.
        if pct_change >= threshold_pct * 3:  # +15%: a meaningful rally you sat out
            return (
                "wrong",
                f"price moved +{pct_change * 100:.1f}% — AVOID missed a meaningful rally (omission)",
            )
        if pct_change > threshold_pct:
            return (
                "mixed",
                f"price moved +{pct_change * 100:.1f}% — AVOID gave up a modest move",
            )
        return (
            "correct",
            f"price moved {pct_change * 100:.1f}% — AVOID dodged the name (no upside missed)",
        )

    return ("unfalsifiable", f"unknown kind={kind}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-days",
        type=int,
        default=30,
        help="Grade decisions made at least this many days ago.",
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=0.05,
        help="Symmetric price-move threshold (fraction) that separates correct/wrong/mixed.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing retained provider source artifacts.",
    )
    parser.add_argument(
        "--avoid-after-days",
        type=int,
        default=180,
        help="Grade pass/avoid decisions only once they are this old — an "
        "omission needs a longer horizon to reveal itself than an add/trim, and "
        "the wider window keeps the pass's falsifiable conditions open to "
        "resurface in the meantime.",
    )
    parser.add_argument("--db-path", "--db", type=Path, help="Explicit existing database override.")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repo_root = args.repo_root.resolve()
    try:
        db_path = require_db_path(args.db_path or configured_db_path(repo_root))
        if db_path == (repo_root / "data" / "portfolio.db").resolve():
            raise RuntimeError("Checkout-default database is prohibited")
        with closing(connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)) as conn:
            conn.execute("SELECT id FROM decisions LIMIT 1").fetchone()
    except (OSError, RuntimeError, sqlite3.Error):
        log.error(
            {"event": "decision_grading_unavailable", "reason": "configured_database_unavailable"}
        )
        return 2
    with db_path_context(db_path):
        return _grade(
            repo_root=repo_root,
            db_path=db_path,
            since_days=args.since_days,
            limit=args.limit,
            avoid_after_days=args.avoid_after_days,
            threshold_pct=args.threshold_pct,
        )


def _grade(
    *,
    repo_root: Path,
    db_path: Path,
    since_days: int,
    limit: int,
    avoid_after_days: int,
    threshold_pct: float,
) -> int:
    pending = pending_for_grading(older_than_days=since_days, limit=limit, db_path=db_path)
    log.info({"event": "pending_decisions", "n": len(pending)})

    tally = {
        "graded": 0,
        "correct": 0,
        "wrong": 0,
        "mixed": 0,
        "unfalsifiable": 0,
        "skipped_no_price": 0,
        "skipped_young_avoid": 0,
    }
    now = datetime.now(UTC)
    now_naive = now.replace(tzinfo=None)
    reader = ProviderNeutralDataReader(repo_root)

    for dec in pending:
        # A pass needs a longer horizon than an add/trim before its outcome means
        # anything; leave young avoids open (and watchable) until they ripen.
        if dec.recommendation_kind == "avoid":
            age_days = (now_naive - dec.made_at.replace(tzinfo=None)).days
            if age_days < avoid_after_days:
                tally["skipped_young_avoid"] += 1
                continue
        evidence = decision_price_evidence(reader, ticker=dec.ticker, made_at=dec.made_at, now=now)
        if isinstance(evidence, ReaderUnavailableStatus):
            tally["skipped_no_price"] += 1
            log.info(
                {
                    "event": "decision_price_unavailable",
                    "decision_id": dec.id,
                    "reason": evidence.reason,
                }
            )
            continue
        pct_change = evidence.pct_change
        label, notes = _verdict(
            kind=dec.recommendation_kind,
            pct_change=pct_change,
            threshold_pct=threshold_pct,
        )
        ok = record_outcome(
            decision_id=dec.id,
            outcome_label=cast("OutcomeLabel", label),
            outcome_pct=pct_change,
            outcome_notes=notes + "\nprice_evidence=" + evidence.model_dump_json(),
            outcome_at=datetime.now(UTC),
            db_path=db_path,
        )
        if ok:
            tally["graded"] += 1
            tally[label] = tally.get(label, 0) + 1

    # Calibration hook: aggregate run-level quality so the dashboard can
    # answer "how is the decision recommendation prompt performing this
    # month?". Recorded once per run (not per decision) because the score
    # is meaningful as a population-level ratio.
    calibration_score = _decisions_calibration_score(tally)
    if calibration_score is not None:
        record_score(
            CalibrationScore(
                purpose="decision_audit",
                prompt_version=prompt_version_for("decision_audit"),
                score=calibration_score,
                reason=(
                    f"correct={tally['correct']} wrong={tally['wrong']} mixed={tally['mixed']}"
                ),
                scored_by="auto:grade_decisions",
            ),
            db_path=db_path,
        )

    # Show summary line even when zero — the user runs this to confirm the
    # pipeline works against fresh artifacts.
    print(
        "Decision grading complete · "
        f"pending={len(pending)} · graded={tally['graded']} · "
        f"correct={tally['correct']} · wrong={tally['wrong']} · "
        f"mixed={tally['mixed']} · skipped_no_price={tally['skipped_no_price']} · "
        f"skipped_young_avoid={tally['skipped_young_avoid']}"
    )
    if len(pending) == 0:
        # Most likely cause: artifacts younger than --since-days. Show the
        # threshold so the operator can re-run with a longer window.
        cutoff = (datetime.now(UTC) - timedelta(days=since_days)).date().isoformat()
        print(f"  (no decisions made on or before {cutoff} are awaiting grading)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
