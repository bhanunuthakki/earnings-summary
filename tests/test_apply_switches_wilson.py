"""Tests for the pooled Wilson switch gate + streak-neutral labels in
execution/apply_model_switches.py (meta_eval_governance.md §2.4, PR2).

The gate is the statistical half of the switch bar: the verdict streak says
"consistently at parity", the pooled 95% Wilson lower bound (per judge, MIN
across judges) says "on enough evidence". No LLM calls anywhere here — the
module under test only reads/writes SQLite.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_switches() -> Any:
    src = PROJECT_ROOT / "execution" / "apply_model_switches.py"
    spec = importlib.util.spec_from_file_location("apply_model_switches", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys_src = str(PROJECT_ROOT / "src")
    if sys_src not in sys.path:
        sys.path.insert(0, sys_src)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def switches() -> Any:
    return _load_switches()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE model_eval_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, purpose TEXT NOT NULL,
            candidate TEXT NOT NULL, incumbent TEXT NOT NULL, verdict TEXT NOT NULL,
            run_id TEXT NOT NULL, parity_rate REAL, judge_agreement REAL,
            n_cases INTEGER, n_parity INTEGER, summary_json TEXT, recorded_at TEXT NOT NULL
        );
        CREATE TABLE model_pin_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT, purpose TEXT NOT NULL,
            model TEXT NOT NULL, set_by TEXT NOT NULL, set_at TEXT NOT NULL,
            reason_json TEXT, active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


_INCUMBENT = "claude-sonnet-4-6"
_CANDIDATE = "claude-haiku-4-5-20251001"


def _summary_with_cases(*, per_judge_parity: dict[str, tuple[int, int]]) -> str:
    """summary_json whose cases[] yield the given judge -> (parity, total)."""
    cases: list[dict[str, object]] = []
    for judge, (parity, total) in per_judge_parity.items():
        for i in range(total):
            cases.append(
                {
                    "label": f"case{i}",
                    "judge": judge,
                    "winner_model": _CANDIDATE if i < parity else _INCUMBENT,
                    "margin": 0.2,
                }
            )
    return json.dumps({"cases": cases})


def _insert_verdict(
    db_path: Path,
    *,
    verdict: str,
    days_ago: int,
    summary_json: str | None = None,
    n_parity: int = 0,
    n_cases: int = 0,
) -> None:
    at = (datetime.now(UTC) - timedelta(days=days_ago)).replace(tzinfo=None).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO model_eval_verdicts (purpose, candidate, incumbent, verdict, run_id,"
        " n_parity, n_cases, summary_json, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("bear_case", _CANDIDATE, _INCUMBENT, verdict, "r", n_parity, n_cases, summary_json, at),
    )
    conn.commit()
    conn.close()


def _active_override(db_path: Path) -> str | None:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT model FROM model_pin_overrides WHERE purpose='bear_case' AND active=1"
    ).fetchone()
    conn.close()
    return str(row[0]) if row else None


# ---------------------------------------------------------------------------
# wilson_lower_bound — the §2.4 calibration points
# ---------------------------------------------------------------------------


def test_wilson_lower_bound_calibration(switches: Any) -> None:
    # 32/36 pooled (~89% parity) -> LB ~0.75: clears the 0.70 gate.
    strong = switches.wilson_lower_bound(32, 36)
    assert strong == pytest.approx(0.746, abs=0.02)
    assert strong >= switches.WILSON_SWITCH_LB
    # 24/30 (80% on the nose) -> LB ~0.63: correctly held.
    weak = switches.wilson_lower_bound(24, 30)
    assert weak == pytest.approx(0.627, abs=0.02)
    assert weak < switches.WILSON_SWITCH_LB
    # Degenerate inputs fail closed.
    assert switches.wilson_lower_bound(0, 0) == 0.0
    assert switches.wilson_lower_bound(5, 5) < 1.0


# ---------------------------------------------------------------------------
# The gate in evaluate_switches
# ---------------------------------------------------------------------------


def test_strong_pooled_evidence_switches(switches: Any, db: Path) -> None:
    # 3 consecutive SWITCH_DOWN, each with 2 judges at 11/12 parity ->
    # pooled 33/36 per judge, LB ~0.78 >= 0.70 -> switch fires.
    summary = _summary_with_cases(per_judge_parity={"claude": (11, 12), "gemini": (11, 12)})
    for days_ago in (3, 2, 1):
        _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=days_ago, summary_json=summary)
    results = switches.evaluate_switches(
        db, consecutive_switch=3, consecutive_keep=3, dry_run=False
    )
    actions = {r.action for r in results}
    assert "SWITCHED_DOWN" in actions
    assert _active_override(db) == _CANDIDATE


def test_thin_pooled_evidence_is_wilson_held(switches: Any, db: Path) -> None:
    # 3 consecutive SWITCH_DOWN but only 4/5 parity per judge per sweep ->
    # pooled 12/15 per judge, LB ~0.55 < 0.70 -> held, no override written.
    summary = _summary_with_cases(per_judge_parity={"claude": (4, 5), "gemini": (4, 5)})
    for days_ago in (3, 2, 1):
        _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=days_ago, summary_json=summary)
    results = switches.evaluate_switches(
        db, consecutive_switch=3, consecutive_keep=3, dry_run=False
    )
    actions = {r.action for r in results}
    assert "WILSON_HELD" in actions
    assert "SWITCHED_DOWN" not in actions
    assert _active_override(db) is None


def test_min_across_judges_gates(switches: Any, db: Path) -> None:
    # One judge strong (12/12), the other thin (7/12 -> pooled 21/36 LB ~0.42):
    # the MIN across judges gates -> held.
    summary = _summary_with_cases(per_judge_parity={"claude": (12, 12), "gemini": (7, 12)})
    for days_ago in (3, 2, 1):
        _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=days_ago, summary_json=summary)
    results = switches.evaluate_switches(
        db, consecutive_switch=3, consecutive_keep=3, dry_run=False
    )
    assert {r.action for r in results} == {"WILSON_HELD"}


def test_row_level_fallback_when_no_case_audit(switches: Any, db: Path) -> None:
    # Old-shape rows (no cases in summary_json): pooled row-level n_parity/n_cases
    # 12/12 x3 = 36/36 -> LB ~0.90 -> switches.
    for days_ago in (3, 2, 1):
        _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=days_ago, n_parity=12, n_cases=12)
    results = switches.evaluate_switches(
        db, consecutive_switch=3, consecutive_keep=3, dry_run=False
    )
    assert "SWITCHED_DOWN" in {r.action for r in results}


def test_streak_neutral_labels_do_not_reset_streak(switches: Any, db: Path) -> None:
    # SWITCH_DOWN x3 interleaved with INSUFFICIENT_FRAME / INSUFFICIENT_DATA:
    # neutral labels are filtered from the streak window, so the streak holds.
    summary = _summary_with_cases(per_judge_parity={"claude": (11, 12), "gemini": (11, 12)})
    _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=5, summary_json=summary)
    _insert_verdict(db, verdict="INSUFFICIENT_FRAME", days_ago=4)
    _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=3, summary_json=summary)
    _insert_verdict(db, verdict="INSUFFICIENT_DATA", days_ago=2)
    _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=1, summary_json=summary)
    results = switches.evaluate_switches(
        db, consecutive_switch=3, consecutive_keep=3, dry_run=False
    )
    assert "SWITCHED_DOWN" in {r.action for r in results}
    assert _active_override(db) == _CANDIDATE


def test_keep_streak_breaks_switch(switches: Any, db: Path) -> None:
    # A KEEP_INCUMBENT inside the window is REAL evidence (not neutral) and
    # still breaks the switch streak.
    summary = _summary_with_cases(per_judge_parity={"claude": (11, 12), "gemini": (11, 12)})
    _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=3, summary_json=summary)
    _insert_verdict(db, verdict="KEEP_INCUMBENT", days_ago=2)
    _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=1, summary_json=summary)
    results = switches.evaluate_switches(
        db, consecutive_switch=3, consecutive_keep=3, dry_run=False
    )
    assert "SWITCHED_DOWN" not in {r.action for r in results}
    assert _active_override(db) is None


def test_candidate_errored_latest_still_short_circuits(switches: Any, db: Path) -> None:
    # Newest verdict CANDIDATE_ERRORED -> infra path, no streak evaluation.
    summary = _summary_with_cases(per_judge_parity={"claude": (11, 12), "gemini": (11, 12)})
    for days_ago in (4, 3, 2):
        _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=days_ago, summary_json=summary)
    _insert_verdict(db, verdict="CANDIDATE_ERRORED", days_ago=1)
    results = switches.evaluate_switches(
        db, consecutive_switch=3, consecutive_keep=3, dry_run=False
    )
    assert {r.action for r in results} == {"CANDIDATE_ERRORED"}
    assert _active_override(db) is None


def test_manual_lock_blocks_auto_switch(switches: Any, db: Path) -> None:
    # A manual pin (revert_model_switch.py --lock) must never be overwritten by
    # the auto loop, however strong the evidence (§10 Q3 remediation).
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO model_pin_overrides (purpose, model, set_by, set_at, active)"
        " VALUES ('bear_case', 'claude-sonnet-4-6', 'manual:lock', '2026-07-01T00:00:00', 1)"
    )
    conn.commit()
    conn.close()
    summary = _summary_with_cases(per_judge_parity={"claude": (12, 12), "gemini": (12, 12)})
    for days_ago in (3, 2, 1):
        _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=days_ago, summary_json=summary)
    results = switches.evaluate_switches(
        db, consecutive_switch=3, consecutive_keep=3, dry_run=False
    )
    assert {r.action for r in results} == {"MANUAL_LOCKED"}
    assert _active_override(db) == "claude-sonnet-4-6"  # the human pin survives


def test_dry_run_gate_writes_nothing(switches: Any, db: Path) -> None:
    summary = _summary_with_cases(per_judge_parity={"claude": (11, 12), "gemini": (11, 12)})
    for days_ago in (3, 2, 1):
        _insert_verdict(db, verdict="SWITCH_DOWN", days_ago=days_ago, summary_json=summary)
    results = switches.evaluate_switches(db, consecutive_switch=3, consecutive_keep=3, dry_run=True)
    assert "DRY_RUN_SWITCH" in {r.action for r in results}
    assert _active_override(db) is None
