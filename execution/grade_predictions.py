"""Grade pending predictions against realized ``kpi_facts``.

For each prediction whose ``target_period`` has passed and whose ``outcome`` is
still ``'pending'``, resolve its ``kpi_name`` to the canonical
``kpi_definitions`` row (via the shared ``kpi_resolver`` so a fragmented
duplicate name can't shadow the populated series), find the realized value at —
or nearest to — the target period, and compare it against ``target_value`` with
the ``comparator`` (``eq`` / ``ge`` / ``le`` / ``gt`` / ``lt``). Writes
``met`` / ``missed`` with a confidence + an audit note.

Anything we can't *confidently* match is LEFT pending — the grader never
guesses: an unresolvable KPI, no realized fact within the period window, an
unknown comparator, or a prediction with no structured target all stay pending
and are tallied as skips.

Why pending predictions pile up: like the sibling graders
(``grade_decisions``, ``grade_bear_cases``) this is a manual / cron CLI, not
auto-wired into the daily build — nothing graded the management-commitment
backlog. Run it after a quarter reports to close the loop. (It is safe to
re-run: ``grade`` is an idempotent UPDATE and already-graded rows drop out of
``pending_for_grading``.)

Usage:
    python execution/grade_predictions.py
    python execution/grade_predictions.py --ticker AMAT
    python execution/grade_predictions.py --dry-run --verbose
    python execution/grade_predictions.py --window-days 60 --limit 800
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# `eq` is guidance to a point estimate, so "met" needs a tolerance band rather
# than exact equality: an absolute percentage-point band for percent-unit
# metrics (a 48.4% guide delivered at 48.9% is "in line"), relative otherwise.
_EQ_ABS_TOL_PCT = 1.0  # percentage points
_EQ_REL_TOL = 0.05  # fraction of target
# Quarters sit ~91 days apart, so a half-quarter window uniquely picks the
# intended period_end while absorbing fiscal-vs-calendar target_period drift
# (e.g. a 2025-06-30 target matching AMAT's 2025-07-27 fiscal close).
_DEFAULT_WINDOW_DAYS = 45


def _sync_db_path(repo_root: Path) -> None:
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


import predictions_store  # noqa: E402
from compute.kpi_resolver import resolve_kpi_definition_name  # noqa: E402

log = logging.getLogger("grade_predictions")


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


def _realized_value(
    conn: sqlite3.Connection,
    ticker: str,
    definition_name: str,
    target_period: datetime,
    window_days: int,
) -> tuple[float, str, int | None] | None:
    """Realized ``(value, period_end, source_doc_id)`` for this ticker +
    definition at the ``kpi_facts`` period closest to ``target_period``, or
    ``None`` when the nearest fact is outside ``window_days``. Prefers the
    highest-confidence / most-recent fact at the matched period (handles
    restated supersedes)."""
    tp = target_period.date().isoformat()
    row = conn.execute(
        """
        SELECT kf.value AS value, kf.period_end AS period_end, kf.source_doc_id AS doc_id,
               ABS(julianday(kf.period_end) - julianday(?)) AS dist
        FROM kpi_facts kf
        JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
        WHERE kf.ticker = ? AND kd.name = ?
          AND kf.value IS NOT NULL AND kf.period_end IS NOT NULL
        ORDER BY dist ASC, kf.confidence DESC, kf.id DESC
        LIMIT 1
        """,
        (tp, ticker.upper(), definition_name),
    ).fetchone()
    if row is None or row["dist"] is None or float(row["dist"]) > window_days:
        return None
    return (float(row["value"]), str(row["period_end"]), row["doc_id"])


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
) -> dict[str, int]:
    """Grade every past-due pending prediction we can confidently match against
    a realized fact. Returns a tally; leaves un-matchable rows pending."""
    db_path = repo_root / "data" / "portfolio.db"
    pending = predictions_store.pending_for_grading(
        ticker=ticker, as_of=as_of, limit=limit, db_path=db_path
    )
    tally: dict[str, int] = {
        "pending": len(pending),
        "graded": 0,
        "met": 0,
        "missed": 0,
        "skipped_unstructured": 0,
        "skipped_no_kpi": 0,
        "skipped_no_fact": 0,
        "skipped_bad_comparator": 0,
    }
    if not pending:
        return tally
    run_id = f"auto:grade_predictions:{(as_of or datetime.now(UTC)).date().isoformat()}"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        for p in pending:
            if p.comparator is None or p.target_value is None or not p.kpi_name:
                tally["skipped_unstructured"] += 1
                continue
            definition_name = resolve_kpi_definition_name(conn, p.ticker, p.kpi_name)
            if not definition_name:
                tally["skipped_no_kpi"] += 1
                continue
            realized = (
                _realized_value(conn, p.ticker, definition_name, p.target_period, window_days)
                if p.target_period is not None
                else None
            )
            if realized is None:
                tally["skipped_no_fact"] += 1
                continue
            graded = grade_comparison(
                p.comparator,
                p.target_value,
                realized[0],
                p.target_unit,
                eq_abs_tol_pct=eq_abs_tol_pct,
                eq_rel_tol=eq_rel_tol,
            )
            if graded is None:
                tally["skipped_bad_comparator"] += 1
                continue
            outcome, confidence, note = graded
            if not dry_run:
                predictions_store.grade(
                    prediction_id=p.id,
                    outcome=cast("predictions_store.Outcome", outcome),
                    realized_value=realized[0],
                    realized_doc_id=realized[2],
                    outcome_confidence=confidence,
                    notes=f"{note} @ {realized[1]} (def: {definition_name})",
                    evaluator_run_id=run_id,
                    db_path=db_path,
                )
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
    return tally


def main() -> int:
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
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repo_root = args.repo_root.resolve()
    _sync_db_path(repo_root)

    tally = grade_pending(
        repo_root,
        ticker=args.ticker,
        window_days=args.window_days,
        limit=args.limit,
        dry_run=args.dry_run,
        eq_abs_tol_pct=args.eq_abs_tol_pct,
        eq_rel_tol=args.eq_rel_tol,
    )
    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}Prediction grading complete · pending={tally['pending']} · "
        f"graded={tally['graded']} · met={tally['met']} · missed={tally['missed']} · "
        f"skipped(no_fact={tally['skipped_no_fact']}, no_kpi={tally['skipped_no_kpi']}, "
        f"unstructured={tally['skipped_unstructured']}, bad_cmp={tally['skipped_bad_comparator']})"
    )
    if tally["pending"] == 0:
        print("  (no predictions with an elapsed target_period are awaiting grading)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
