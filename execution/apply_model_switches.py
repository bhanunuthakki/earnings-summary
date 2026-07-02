"""Apply automatic model-pin switches from accumulated eval verdicts.

Reads ``model_eval_verdicts`` (written by ``run_model_eval_sweep.py`` or
directly by ``eval_model_downgrade.py``) and decides whether accumulated
evidence justifies:

  * SWITCHING DOWN to a cheaper candidate — when the last
    ``--consecutive-switch`` verdicts for a (purpose, candidate) pair are
    all ``SWITCH_DOWN``.  Writes an active ``model_pin_overrides`` row so
    the NEXT call to ``_model_for(purpose)`` routes to the cheaper model.

  * REVERTING a prior switch — when an active override exists AND the last
    ``--consecutive-keep`` verdicts for (purpose, override_model) are all
    ``KEEP_INCUMBENT``.  Deactivates the override and logs a regression
    alert so the quality drop is visible in the dashboard.

Conservatism is intentional: a single bad run doesn't trigger a switch;
only a streak of consistent verdicts does.  The thresholds are configurable
(``--consecutive-switch``, ``--consecutive-keep``) and default to 3.

Usage (run after each sweep):
    python execution/apply_model_switches.py --repo-root <MAIN>
    python execution/apply_model_switches.py --repo-root <MAIN> \\
        --consecutive-switch 3 --consecutive-keep 3 --dry-run

The script is idempotent: running it twice on the same verdict set produces
the same outcome (already-active overrides are not duplicated; already-
deactivated overrides are not double-deactivated).
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.model_eval import CANDIDATE_ERRORED, KEEP_INCUMBENT, SWITCH_DOWN  # noqa: E402
from llm.model_overrides import (  # noqa: E402
    SET_BY_AUTO,
    active_override,
    deactivate_override,
    write_pin_override,
)

log = logging.getLogger("apply_model_switches")


# ---------------------------------------------------------------------------
# Internal DB helpers (read-only)
# ---------------------------------------------------------------------------


def _recent_verdicts(
    db_path: Path,
    purpose: str,
    candidate: str,
    limit: int,
) -> list[str]:
    """Return the ``limit`` most recent verdict strings for
    (purpose, candidate), newest first.  Empty list if no rows or table
    missing."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            rows = conn.execute(
                """
                SELECT verdict FROM model_eval_verdicts
                WHERE purpose = ? AND candidate = ?
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (purpose, candidate, limit),
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]
    except sqlite3.Error as exc:
        log.warning("_recent_verdicts: %s", exc)
        return []


def _all_evaluated_pairs(db_path: Path) -> list[tuple[str, str]]:
    """Return distinct (purpose, candidate) pairs that have any verdict."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT DISTINCT purpose, candidate FROM model_eval_verdicts"
            ).fetchall()
        finally:
            conn.close()
        return [(r[0], r[1]) for r in rows]
    except sqlite3.Error as exc:
        log.warning("_all_evaluated_pairs: %s", exc)
        return []


def _latest_incumbent(db_path: Path, purpose: str, candidate: str) -> str | None:
    """Retrieve the incumbent model id from the most recent verdict for this
    (purpose, candidate) — needed to label regression alerts."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute(
                """
                SELECT incumbent FROM model_eval_verdicts
                WHERE purpose = ? AND candidate = ?
                ORDER BY recorded_at DESC
                LIMIT 1
                """,
                (purpose, candidate),
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _latest_verdict_with_time(
    db_path: Path, purpose: str, candidate: str
) -> tuple[str, str] | None:
    """(verdict, recorded_at) of the newest verdict for (purpose, candidate),
    or None."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute(
                """
                SELECT verdict, recorded_at FROM model_eval_verdicts
                WHERE purpose = ? AND candidate = ?
                ORDER BY recorded_at DESC
                LIMIT 1
                """,
                (purpose, candidate),
            ).fetchone()
        finally:
            conn.close()
        return (row[0], row[1]) if row else None
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------------------
# Alert helper
# ---------------------------------------------------------------------------


def _fire_switch_alert(
    db_path: Path,
    *,
    purpose: str,
    model: str,
    event: str,
    evidence: dict[str, object],
    sig_extra: str = "",
) -> None:
    """Write a row to the ``alerts`` table (if it exists) so the dashboard
    surfaces the switch event.  Silently skips when the table is absent
    (fixture DBs, early migrations).  ``sig_extra`` differentiates repeatable
    events (e.g. a CANDIDATE_ERRORED alert keyed by verdict date, so a NEW
    breakage after recovery fires again while the same one dedupes)."""
    sig = f"model_pin:{event}:{purpose}:{model}:{sig_extra}"
    import hashlib

    sig_sha = hashlib.sha256(sig.encode()).hexdigest()[:16]
    fired_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
    evidence_json = json.dumps(evidence, ensure_ascii=False)
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "alerts" not in tables:
                return
            conn.execute(
                """
                INSERT OR IGNORE INTO alerts
                    (ticker, trigger_kind, fired_at, status, evidence_json, signature_sha)
                VALUES (?, 'model_pin_switch', ?, 'pending', ?, ?)
                """,
                (f"__model__{purpose}", fired_at, evidence_json, sig_sha),
            )
            conn.commit()
        finally:
            conn.close()
        log.info(
            {
                "event": "model_switch_alert_fired",
                "purpose": purpose,
                "model": model,
                "switch_event": event,
            }
        )
    except sqlite3.Error as exc:
        log.debug("_fire_switch_alert skipped: %s", exc)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _is_consecutive(verdicts: list[str], target: str, n: int) -> bool:
    """True iff the first ``n`` elements of ``verdicts`` (newest-first) are
    all equal to ``target``."""
    if len(verdicts) < n:
        return False
    return all(v == target for v in verdicts[:n])


class SwitchResult:
    __slots__ = ("action", "candidate", "model", "purpose", "reason")

    def __init__(
        self,
        purpose: str,
        candidate: str,
        action: str,
        model: str | None,
        reason: str,
    ) -> None:
        self.purpose = purpose
        self.candidate = candidate
        self.action = action
        self.model = model
        self.reason = reason


def evaluate_switches(
    db_path: Path,
    *,
    consecutive_switch: int,
    consecutive_keep: int,
    dry_run: bool,
) -> list[SwitchResult]:
    """Evaluate all (purpose, candidate) pairs and apply switches.

    Returns a list of SwitchResult objects for logging/summary.
    """
    results: list[SwitchResult] = []
    pairs = _all_evaluated_pairs(db_path)
    if not pairs:
        log.info("no evaluated pairs in model_eval_verdicts — nothing to do")
        return results

    for purpose, candidate in pairs:
        # ---- Infrastructure check first ----
        # A newest verdict of CANDIDATE_ERRORED means the candidate backend is
        # operationally broken (the 2026-06-28 sweep: Gemini CLI failing 60-100%
        # of runs, recorded as quality losses). Surface it as an infra alert and
        # skip streak evaluation — an errored week is neither switch nor keep
        # evidence, and the streak checks would misread it.
        latest = _latest_verdict_with_time(db_path, purpose, candidate)
        if latest is not None and latest[0] == CANDIDATE_ERRORED:
            verdict_date = latest[1][:10]
            if not dry_run:
                _fire_switch_alert(
                    db_path,
                    purpose=purpose,
                    model=candidate,
                    event="CANDIDATE_ERRORED",
                    evidence={
                        "latest_verdict_at": latest[1],
                        "note": "candidate backend failing operationally in the eval sweep; "
                        "fix infra before trusting tallies",
                    },
                    sig_extra=verdict_date,
                )
            results.append(
                SwitchResult(
                    purpose,
                    candidate,
                    "CANDIDATE_ERRORED",
                    None,
                    f"newest verdict {verdict_date}: candidate errored — infra alert fired, "
                    "streaks skipped",
                )
            )
            continue

        # ---- Forward switch check ----
        recent = _recent_verdicts(db_path, purpose, candidate, consecutive_switch)
        if _is_consecutive(recent, SWITCH_DOWN, consecutive_switch):
            current_override = active_override(purpose, db_path=db_path)
            if current_override == candidate:
                results.append(
                    SwitchResult(
                        purpose,
                        candidate,
                        "ALREADY_ACTIVE",
                        candidate,
                        f"override already active for {purpose} → {candidate}",
                    )
                )
                continue
            reason: dict[str, object] = {
                "consecutive_switch_verdicts": consecutive_switch,
                "latest_verdicts": recent,
                "run_id": uuid.uuid4().hex[:8],
                "applied_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            }
            if not dry_run:
                write_pin_override(
                    purpose,
                    candidate,
                    set_by=SET_BY_AUTO,
                    reason=reason,
                    db_path=db_path,
                )
                _fire_switch_alert(
                    db_path,
                    purpose=purpose,
                    model=candidate,
                    event="SWITCH_DOWN",
                    evidence=reason,
                )
            results.append(
                SwitchResult(
                    purpose,
                    candidate,
                    "DRY_RUN_SWITCH" if dry_run else "SWITCHED_DOWN",
                    candidate,
                    f"{consecutive_switch} consecutive SWITCH_DOWN verdicts",
                )
            )
            continue

        # ---- Regression / revert check ----
        # Only relevant if there's an active override for this purpose AND
        # this candidate is the overridden model (or was the incumbent).
        current_override = active_override(purpose, db_path=db_path)
        if current_override is None:
            continue
        # We check regression for verdicts where candidate == current_override
        # (the cheaper model that was switched to is now being evaluated as
        # the INCUMBENT against something even cheaper, or re-evaluated).
        # Also check the inverse: recent verdicts where THIS candidate pair
        # shows KEEP_INCUMBENT, meaning the now-active cheaper model lost.
        if current_override != candidate:
            continue
        keep_recent = _recent_verdicts(db_path, purpose, candidate, consecutive_keep)
        if _is_consecutive(keep_recent, KEEP_INCUMBENT, consecutive_keep):
            incumbent = _latest_incumbent(db_path, purpose, candidate)
            reason_revert: dict[str, object] = {
                "consecutive_keep_verdicts": consecutive_keep,
                "latest_verdicts": keep_recent,
                "reverted_model": current_override,
                "incumbent": incumbent,
                "applied_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            }
            if not dry_run:
                deactivate_override(purpose, db_path=db_path)
                _fire_switch_alert(
                    db_path,
                    purpose=purpose,
                    model=current_override,
                    event="REGRESSION_REVERT",
                    evidence=reason_revert,
                )
            results.append(
                SwitchResult(
                    purpose,
                    candidate,
                    "DRY_RUN_REVERT" if dry_run else "REVERTED",
                    None,
                    f"{consecutive_keep} consecutive KEEP_INCUMBENT after switch to {current_override}; reverted",
                )
            )

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply automatic model-pin switches from accumulated eval verdicts."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="repo root whose data/portfolio.db is used",
    )
    parser.add_argument(
        "--consecutive-switch",
        type=int,
        default=3,
        help="consecutive SWITCH_DOWN verdicts required to activate an override (default 3)",
    )
    parser.add_argument(
        "--consecutive-keep",
        type=int,
        default=3,
        help="consecutive KEEP_INCUMBENT verdicts required to revert an override (default 3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate and print, but write no DB rows",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        log.error("DB not found at %s", db_path)
        return 1

    import db as _db

    _db.set_db_path(db_path)

    log.info(
        "apply_model_switches  db=%s  consecutive_switch=%d  consecutive_keep=%d  dry_run=%s",
        db_path,
        args.consecutive_switch,
        args.consecutive_keep,
        args.dry_run,
    )

    results = evaluate_switches(
        db_path,
        consecutive_switch=args.consecutive_switch,
        consecutive_keep=args.consecutive_keep,
        dry_run=args.dry_run,
    )

    if not results:
        log.info("no actionable verdict streaks — nothing changed")
        return 0

    log.info("")
    log.info("%-30s %-30s %-20s  %s", "purpose", "candidate", "action", "reason")
    for r in results:
        log.info("%-30s %-30s %-20s  %s", r.purpose, r.candidate, r.action, r.reason)

    switched = [r for r in results if r.action in ("SWITCHED_DOWN", "DRY_RUN_SWITCH")]
    reverted = [r for r in results if r.action in ("REVERTED", "DRY_RUN_REVERT")]
    log.info(
        "\nswitched=%d  reverted=%d  already_active=%d  candidate_errored=%d  dry_run=%s",
        len(switched),
        len(reverted),
        sum(1 for r in results if r.action == "ALREADY_ACTIVE"),
        sum(1 for r in results if r.action == "CANDIDATE_ERRORED"),
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
