"""Grade due predictions using uniquely bound, immutable KPI evidence.

Unbound definitions, incomparable or ambiguous periods, unknown units/currency,
and unavailable source evidence leave the prediction pending with a logged
reason. Existing comparison tolerances and date windows remain unchanged.
Outcomes append an exact source/context manifest to retained notes. Concurrent
or repeated grading cannot replace an already retained outcome.

Requires --db-path/--db or EARNINGS_SUMMARY_DB_PATH; checkout state is prohibited.
Use --dry-run to calculate outcomes without writing outcomes or calibration.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

import predictions_store
from db_paths import configured_db_path, db_path_context, require_db_path
from llm.calibration import CalibrationScore, record_score
from llm.prompt_versions import prompt_version_for
from sources.prediction_evidence import prediction_evidence
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_EQ_ABS_TOL_PCT = 1.0
_EQ_REL_TOL = 0.05
_DEFAULT_WINDOW_DAYS = 45

log = logging.getLogger("grade_predictions")

# Calibration purpose for the management-prediction EXTRACTION prompt. The
# signal is the fraction of due predictions the extraction left well-formed
# enough to grade — see ``_record_extraction_calibration``.
_CALIBRATION_PURPOSE = "management_prediction"


def grade_comparison(
    comparator: str,
    target: float,
    realized: float,
    unit: str | None,
    *,
    eq_abs_tol_pct: float = _EQ_ABS_TOL_PCT,
    eq_rel_tol: float = _EQ_REL_TOL,
) -> tuple[str, float, str] | None:
    """Map ``(comparator, target, realized, unit)`` to ``(outcome, confidence,
    note)``, or ``None`` for an unknown comparator. Directional comparators are
    strict; ``eq`` is met within a tolerance band (absolute pp for percent-unit
    metrics, relative otherwise). Confidence is lower for ``eq`` because the
    band is a judgement call, higher for the unambiguous directional checks."""
    cmp = comparator.strip().lower()
    is_pct = (unit or "").strip().lower().startswith("percent")
    suffix = "pp" if is_pct else ""
    note = f"realized {realized:.4g}{suffix} {cmp} target {target:.4g}{suffix}"
    if cmp in ("ge", "gte", ">="):
        return ("met" if realized >= target else "missed", 0.9, note)
    if cmp in ("gt", ">"):
        return ("met" if realized > target else "missed", 0.9, note)
    if cmp in ("le", "lte", "<="):
        return ("met" if realized <= target else "missed", 0.9, note)
    if cmp in ("lt", "<"):
        return ("met" if realized < target else "missed", 0.9, note)
    if cmp in ("eq", "=="):
        diff = abs(realized - target)
        if is_pct:
            met = diff <= eq_abs_tol_pct
            band = f"|Δ|={diff:.2f}pp tol={eq_abs_tol_pct:.2f}pp"
        else:
            denom = abs(target) or 1.0
            met = (diff / denom) <= eq_rel_tol
            band = f"relΔ={diff / denom:.1%} tol={eq_rel_tol:.0%}"
        return ("met" if met else "missed", 0.7, f"{note} ({band})")
    return None


def extraction_quality_score(tally: dict[str, int]) -> float | None:
    """The management-prediction EXTRACTION quality for this run, in ``[0, 1]``,
    or ``None`` when no prediction was attemptable.

    Measures how often the extraction prompt produced a prediction well-formed
    enough to *grade against realized data* — the fraction
    ``graded / (graded + malformed)`` where "malformed" is the extraction's own
    fault (unstructured target, an unresolvable KPI name, or an unknown
    comparator). ``skipped_no_fact`` is excluded: a clean prediction whose
    realized value simply isn't in yet is a data-availability gap, not an
    extraction defect. The verdict (met vs missed) is deliberately NOT part of
    the score — that measures management, not the LLM. A rewritten extraction
    prompt that produces more gradeable predictions scores higher, which is
    exactly the calibration A/B question.
    """
    malformed = (
        tally["skipped_unstructured"] + tally["skipped_no_kpi"] + tally["skipped_bad_comparator"]
    )
    attemptable = tally["graded"] + malformed
    if attemptable == 0:
        return None
    return tally["graded"] / attemptable


def _record_extraction_calibration(
    tally: dict[str, int], *, ticker: str | None, db_path: Path
) -> None:
    """Best-effort: record one calibration row for this prediction-grading run,
    tagged with the central registry's version for the extraction purpose. A
    no-op when nothing was attemptable or the calibration table is absent."""
    score = extraction_quality_score(tally)
    if score is None:
        return
    malformed = (
        tally["skipped_unstructured"] + tally["skipped_no_kpi"] + tally["skipped_bad_comparator"]
    )
    record_score(
        CalibrationScore(
            purpose=_CALIBRATION_PURPOSE,
            prompt_version=prompt_version_for(_CALIBRATION_PURPOSE),
            score=score,
            ticker=ticker,
            reason=(
                f"extraction gradeable fraction {tally['graded']}/"
                f"{tally['graded'] + malformed} due predictions well-formed enough to grade"
            ),
            scored_by="grade_predictions",
        ),
        db_path=db_path,
    )


def grade_pending(
    repo_root: Path,
    *,
    ticker: str | None = None,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    limit: int = 500,
    dry_run: bool = False,
    eq_abs_tol_pct: float = _EQ_ABS_TOL_PCT,
    eq_rel_tol: float = _EQ_REL_TOL,
    as_of: datetime | None = None,
    record_calibration: bool = False,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Grade every past-due pending prediction we can confidently match against
    a realized fact. Returns a tally; leaves un-matchable rows pending.

    When ``record_calibration`` is set (and not a dry run), one calibration
    score for the extraction prompt is recorded for the run — see
    ``extraction_quality_score``. Off by default so existing callers/tests are
    unchanged; the scheduled grader opts in."""
    db_path = require_db_path(db_path or configured_db_path(repo_root))
    if db_path == (repo_root / "data" / "portfolio.db").resolve():
        raise RuntimeError("The checkout-default portfolio database is prohibited")
    if window_days < 0 or limit < 1:
        raise ValueError("window_days must be nonnegative and limit positive")
    if not all(math.isfinite(value) and value >= 0 for value in (eq_abs_tol_pct, eq_rel_tol)):
        raise ValueError("comparison tolerances must be finite and nonnegative")
    as_of = as_of or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    with closing(connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)) as preflight:
        preflight.execute("SELECT id, outcome FROM predictions LIMIT 0")
    candidates = predictions_store.pending_for_grading(
        ticker=ticker, as_of=as_of, limit=limit, db_path=db_path
    )
    pending: list[predictions_store.Prediction] = []
    for candidate in candidates:
        if candidate.made_at.tzinfo is None or candidate.made_at > as_of:
            log.info(
                {
                    "event": "prediction_unavailable_at_cutoff",
                    "id": candidate.id,
                    "reason": "made_at_timezone_unavailable"
                    if candidate.made_at.tzinfo is None
                    else "prediction_not_yet_made",
                }
            )
            continue
        pending.append(candidate)
    tally: dict[str, int] = {
        "pending": len(pending),
        "graded": 0,
        "met": 0,
        "missed": 0,
        "skipped_unstructured": 0,
        "skipped_no_kpi": 0,
        "skipped_no_fact": 0,
        "skipped_bad_comparator": 0,
        "skipped_unavailable_evidence": 0,
        "skipped_write_failed": 0,
    }
    if not pending:
        return tally
    run_id = f"auto:grade_predictions:{(as_of or datetime.now(UTC)).date().isoformat()}"
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        for p in pending:
            if (
                p.comparator is None
                or p.target_value is None
                or not p.kpi_name
                or p.target_period is None
                or not math.isfinite(p.target_value)
            ):
                tally["skipped_unstructured"] += 1
                continue
            conn.execute("BEGIN")
            try:
                evidence = prediction_evidence(
                    conn,
                    ticker=p.ticker,
                    kpi_name=p.kpi_name,
                    target_period=p.target_period,
                    target_unit=p.target_unit,
                    kpi_concept_id=p.kpi_concept_id,
                    as_of=as_of,
                    window_days=window_days,
                )
            except (sqlite3.Error, ValueError) as exc:
                log.warning(
                    {
                        "event": "prediction_evidence_unavailable",
                        "id": p.id,
                        "reason": type(exc).__name__,
                    }
                )
                tally["skipped_unavailable_evidence"] += 1
                continue
            finally:
                conn.rollback()
            point = evidence.point
            if point is None:
                reason = evidence.reason
                tally[
                    "skipped_" + reason
                    if reason in {"no_kpi", "no_fact"}
                    else "skipped_unavailable_evidence"
                ] += 1
                log.info({"event": "prediction_evidence_unavailable", "id": p.id, "reason": reason})
                continue
            realized = float(point.value)
            if not math.isfinite(realized):
                tally["skipped_unavailable_evidence"] += 1
                continue
            graded = grade_comparison(
                p.comparator,
                p.target_value,
                realized,
                p.target_unit,
                eq_abs_tol_pct=eq_abs_tol_pct,
                eq_rel_tol=eq_rel_tol,
            )
            if graded is None:
                tally["skipped_bad_comparator"] += 1
                continue
            outcome, confidence, note = graded
            if not dry_run:
                written = predictions_store.grade(
                    prediction_id=p.id,
                    outcome=cast("predictions_store.Outcome", outcome),
                    realized_value=realized,
                    realized_doc_id=point.source_document_id,
                    outcome_confidence=confidence,
                    notes=(p.notes or "")
                    + ("\n" if p.notes else "")
                    + json.dumps(
                        {
                            "schema_version": "prediction-grading-evidence/v1",
                            "as_of": as_of.isoformat(),
                            "prediction_id": p.id,
                            "target_period": p.target_period.isoformat(),
                            "target_value": p.target_value,
                            "target_concept_id": p.kpi_concept_id,
                            "identity_source": "unique_bound_definition_from_name",
                            "target_unit": p.target_unit,
                            "comparator": p.comparator,
                            "window_days": window_days,
                            "eq_abs_tol_pct": eq_abs_tol_pct,
                            "eq_rel_tol": eq_rel_tol,
                            "definition_name": evidence.definition_name,
                            "currency": evidence.currency,
                            "definition": (
                                None
                                if evidence.definition is None
                                else evidence.definition.model_dump(mode="json")
                            ),
                            "point": point.model_dump(mode="json"),
                            "comparison": note,
                        },
                        sort_keys=True,
                    ),
                    evaluator_run_id=run_id,
                    db_path=db_path,
                    only_if_pending=True,
                    expected_prediction=p,
                )
                if not written:
                    tally["skipped_write_failed"] += 1
                    continue
            tally["graded"] += 1
            tally[outcome] += 1
            log.debug(
                {
                    "event": "graded",
                    "id": p.id,
                    "ticker": p.ticker,
                    "outcome": outcome,
                    "note": note,
                }
            )
    finally:
        conn.close()
    if record_calibration and not dry_run:
        # After the read/grade connection is closed, so the calibration write
        # uses its own connection without contending with this run's grading.
        with db_path_context(db_path):
            _record_extraction_calibration(tally, ticker=ticker, db_path=db_path)
    return tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ticker", default=None, help="Grade only this ticker.")
    parser.add_argument("--window-days", type=int, default=_DEFAULT_WINDOW_DAYS)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--eq-abs-tol-pct", type=float, default=_EQ_ABS_TOL_PCT)
    parser.add_argument("--eq-rel-tol", type=float, default=_EQ_REL_TOL)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--dry-run", action="store_true", help="Compute outcomes but do not write them."
    )
    parser.add_argument(
        "--no-calibration",
        action="store_true",
        help="Do not record the extraction-quality calibration score for this run.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--db-path", "--db", type=Path, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repo_root = args.repo_root.resolve()

    try:
        tally = grade_pending(
            repo_root,
            ticker=args.ticker,
            window_days=args.window_days,
            limit=args.limit,
            dry_run=args.dry_run,
            eq_abs_tol_pct=args.eq_abs_tol_pct,
            eq_rel_tol=args.eq_rel_tol,
            record_calibration=not args.no_calibration,
            db_path=args.db_path,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        log.error({"event": "prediction_grading_failed", "reason": type(exc).__name__})
        return 2
    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}Prediction grading complete · pending={tally['pending']} · "
        f"graded={tally['graded']} · met={tally['met']} · missed={tally['missed']} · "
        f"skipped(no_fact={tally['skipped_no_fact']}, no_kpi={tally['skipped_no_kpi']}, "
        f"unstructured={tally['skipped_unstructured']}, bad_cmp={tally['skipped_bad_comparator']}, "
        f"unavailable_evidence={tally['skipped_unavailable_evidence']}, write_failed={tally['skipped_write_failed']})"
    )
    if tally["pending"] == 0:
        print("  (no predictions with an elapsed target_period are awaiting grading)")
    return 1 if tally["skipped_write_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
