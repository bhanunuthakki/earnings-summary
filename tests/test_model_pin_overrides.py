# pyright: reportPrivateUsage=false
"""Tests for the model-pin override system (PR3 of the model-eval loop).

Coverage:
  * model_overrides.active_override: reads the DB, returns None when missing,
    picks most-recently-set active row.
  * model_overrides.write_pin_override / deactivate_override: round-trip.
  * llm.cli._model_for: override wins over LLM_MODELS; code pin is fallback
    when no active override; DB failure degrades silently to code pin.
  * apply_model_switches.evaluate_switches:
    - threshold triggers a SWITCH_DOWN write;
    - mixed streak below threshold → no action;
    - regression triggers a REVERT deactivation;
    - already-active override is left unchanged.

All DB paths are isolated per-test using tmp_path + Alembic to the correct
migration head.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
# Stamp at the revision immediately before ours so `upgrade("head")` only
# runs migration 0084 (our two tables).  Same pattern as test_alerts_store.py.
PRIOR_HEAD = "0083_eval_runs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_alembic_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "test_overrides.db"
    cfg = _build_alembic_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


# ---------------------------------------------------------------------------
# model_overrides.active_override
# ---------------------------------------------------------------------------


def test_active_override_returns_none_when_no_rows(db_path: Path) -> None:
    from llm.model_overrides import active_override

    assert active_override("bear_case", db_path=db_path) is None


def test_active_override_returns_model_when_row_present(db_path: Path) -> None:
    from llm.model_overrides import active_override, write_pin_override

    write_pin_override("bear_case", "claude-haiku-4-5-20251001", db_path=db_path)
    assert active_override("bear_case", db_path=db_path) == "claude-haiku-4-5-20251001"


def test_active_override_ignores_inactive_rows(db_path: Path) -> None:
    from llm.model_overrides import active_override, deactivate_override, write_pin_override

    write_pin_override("bear_case", "claude-haiku-4-5-20251001", db_path=db_path)
    deactivate_override("bear_case", db_path=db_path)
    assert active_override("bear_case", db_path=db_path) is None


def test_active_override_returns_most_recently_set(db_path: Path) -> None:
    from llm.model_overrides import active_override, write_pin_override

    write_pin_override("bear_case", "claude-haiku-4-5-20251001", db_path=db_path)
    # write_pin_override deactivates the first row and inserts a second.
    write_pin_override("bear_case", "gemini-2.5-flash", db_path=db_path)
    assert active_override("bear_case", db_path=db_path) == "gemini-2.5-flash"
    # First row should now be inactive (only one active row).
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT model, active FROM model_pin_overrides WHERE purpose='bear_case' ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows[0] == ("claude-haiku-4-5-20251001", 0), "first row should be inactive"
    assert rows[1] == ("gemini-2.5-flash", 1), "second row should be active"


def test_active_override_returns_none_when_db_missing() -> None:
    from llm.model_overrides import active_override

    # non-existent path → returns None gracefully
    assert active_override("bear_case", db_path=Path("/nonexistent/path.db")) is None


def test_active_override_returns_none_when_no_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When db module is not importable (edge case), active_override returns None."""
    from llm import model_overrides

    def _resolve_none(_: Path | str | None) -> Path | None:
        return None

    monkeypatch.setattr(model_overrides, "resolve_db_path", _resolve_none)
    assert model_overrides.active_override("bear_case") is None


# ---------------------------------------------------------------------------
# write_pin_override / deactivate_override
# ---------------------------------------------------------------------------


def test_write_pin_override_persists_correct_fields(db_path: Path) -> None:
    from llm.model_overrides import write_pin_override

    write_pin_override(
        "bear_case",
        "claude-haiku-4-5-20251001",
        set_by="auto:model_eval_loop",
        reason={"run_id": "abc123", "parity_rate": 0.85},
        db_path=db_path,
    )
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT purpose, model, set_by, active, reason_json FROM model_pin_overrides"
    ).fetchone()
    conn.close()
    assert row is not None
    purpose, model, set_by, active, reason_json = row
    assert purpose == "bear_case"
    assert model == "claude-haiku-4-5-20251001"
    assert set_by == "auto:model_eval_loop"
    assert active == 1
    import json

    reason = json.loads(reason_json)
    assert reason["run_id"] == "abc123"
    assert reason["parity_rate"] == pytest.approx(0.85)


def test_deactivate_override_returns_true_when_row_deactivated(db_path: Path) -> None:
    from llm.model_overrides import deactivate_override, write_pin_override

    write_pin_override("bear_case", "claude-haiku-4-5-20251001", db_path=db_path)
    assert deactivate_override("bear_case", db_path=db_path) is True


def test_deactivate_override_returns_false_when_no_active_row(db_path: Path) -> None:
    from llm.model_overrides import deactivate_override

    assert deactivate_override("bear_case", db_path=db_path) is False


def test_deactivate_override_idempotent(db_path: Path) -> None:
    from llm.model_overrides import active_override, deactivate_override, write_pin_override

    write_pin_override("bear_case", "claude-haiku-4-5-20251001", db_path=db_path)
    deactivate_override("bear_case", db_path=db_path)
    deactivate_override("bear_case", db_path=db_path)  # second call is a no-op
    assert active_override("bear_case", db_path=db_path) is None


# ---------------------------------------------------------------------------
# llm.cli._model_for — override consultation
# ---------------------------------------------------------------------------


def test_model_for_uses_override_over_code_pin(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When an active override exists, _model_for returns it regardless of LLM_MODELS."""
    from llm import model_overrides
    from llm.cli import _model_for

    def _resolve_test_db(_: Path | str | None) -> Path | None:
        return db_path

    monkeypatch.setattr(model_overrides, "resolve_db_path", _resolve_test_db)
    model_overrides.write_pin_override("bear_case", "claude-haiku-4-5-20251001", db_path=db_path)
    assert _model_for("bear_case") == "claude-haiku-4-5-20251001"


def test_model_for_falls_back_to_code_pin_when_no_override(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm import model_overrides
    from llm.cli import LLM_MODELS, _model_for

    def _resolve_test_db(_: Path | str | None) -> Path | None:
        return db_path

    monkeypatch.setattr(model_overrides, "resolve_db_path", _resolve_test_db)
    expected = LLM_MODELS.get("bear_case", "claude-sonnet-4-6")
    assert _model_for("bear_case") == expected


def test_model_for_falls_back_when_db_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB failure must degrade silently — _model_for returns the code pin."""
    from llm import model_overrides
    from llm.cli import LLM_MODELS, _model_for

    def _resolve_missing(_: Path | str | None) -> Path | None:
        return Path("/no/such/db.db")

    monkeypatch.setattr(model_overrides, "resolve_db_path", _resolve_missing)
    expected = LLM_MODELS.get("bear_case", "claude-sonnet-4-6")
    assert _model_for("bear_case") == expected


def test_model_for_falls_back_to_default_for_unknown_purpose(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm import model_overrides
    from llm.cli import DEFAULT_MODEL, _model_for

    def _resolve_test_db(_: Path | str | None) -> Path | None:
        return db_path

    monkeypatch.setattr(model_overrides, "resolve_db_path", _resolve_test_db)
    assert _model_for("totally_unknown_purpose_xyz") == DEFAULT_MODEL


# ---------------------------------------------------------------------------
# apply_model_switches.evaluate_switches
# ---------------------------------------------------------------------------


def _seed_verdicts(
    db_path: Path,
    purpose: str,
    candidate: str,
    incumbent: str,
    verdicts: list[str],
    *,
    n_parity: int = 0,
    n_cases: int = 0,
) -> None:
    """Insert model_eval_verdicts rows (oldest first in the list → newest last
    so the ORDER BY recorded_at DESC query sees list[-1] as the 'latest').

    ``n_parity``/``n_cases`` seed each row's pooled-evidence columns — the PR2
    Wilson switch gate pools them across the streak and fails CLOSED when the
    pool is empty, so a switch-firing test must carry real evidence."""
    from datetime import datetime, timedelta

    conn = sqlite3.connect(str(db_path))
    for i, verdict in enumerate(verdicts):
        ts = (datetime(2026, 6, 10, 10, 0, 0) + timedelta(hours=i)).isoformat()
        conn.execute(
            """
            INSERT INTO model_eval_verdicts
                (purpose, candidate, incumbent, verdict, run_id, n_parity, n_cases, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (purpose, candidate, incumbent, verdict, f"run{i:04d}", n_parity, n_cases, ts),
        )
    conn.commit()
    conn.close()


def test_evaluate_switches_triggers_switch_after_threshold(db_path: Path) -> None:
    """3 consecutive SWITCH_DOWN → SWITCHED_DOWN action, active override written."""
    from apply_model_switches import evaluate_switches

    from llm.model_overrides import active_override

    # 11/12 parity per sweep -> pooled 33/36 -> Wilson LB ~0.78 >= 0.70 (the
    # PR2 gate); the streak alone no longer suffices.
    _seed_verdicts(
        db_path,
        "bear_case",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        ["HOLD", "SWITCH_DOWN", "SWITCH_DOWN", "SWITCH_DOWN"],
        n_parity=11,
        n_cases=12,
    )
    results = evaluate_switches(db_path, consecutive_switch=3, consecutive_keep=3, dry_run=False)
    assert any(r.action == "SWITCHED_DOWN" for r in results)
    assert active_override("bear_case", db_path=db_path) == "claude-haiku-4-5-20251001"


def test_evaluate_switches_no_action_below_threshold(db_path: Path) -> None:
    """Only 2 SWITCH_DOWN when threshold is 3 → no action."""
    from apply_model_switches import evaluate_switches

    from llm.model_overrides import active_override

    _seed_verdicts(
        db_path,
        "bear_case",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        ["KEEP_INCUMBENT", "SWITCH_DOWN", "SWITCH_DOWN"],
    )
    results = evaluate_switches(db_path, consecutive_switch=3, consecutive_keep=3, dry_run=False)
    assert all(r.action not in ("SWITCHED_DOWN", "REVERTED") for r in results)
    assert active_override("bear_case", db_path=db_path) is None


def test_evaluate_switches_revert_on_regression(db_path: Path) -> None:
    """After a switch, 3 consecutive KEEP_INCUMBENT → REVERTED."""
    from apply_model_switches import evaluate_switches

    from llm.model_overrides import active_override, write_pin_override

    # Simulate an already-active override for bear_case → haiku.
    write_pin_override("bear_case", "claude-haiku-4-5-20251001", db_path=db_path)

    # Now seed 3 KEEP_INCUMBENT verdicts where candidate == the switched-to model.
    _seed_verdicts(
        db_path,
        "bear_case",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        ["KEEP_INCUMBENT", "KEEP_INCUMBENT", "KEEP_INCUMBENT"],
    )
    results = evaluate_switches(db_path, consecutive_switch=3, consecutive_keep=3, dry_run=False)
    assert any(r.action == "REVERTED" for r in results)
    assert active_override("bear_case", db_path=db_path) is None


def test_evaluate_switches_dry_run_does_not_write(db_path: Path) -> None:
    """dry_run=True evaluates but writes nothing."""
    from apply_model_switches import evaluate_switches

    from llm.model_overrides import active_override

    # Strong pooled evidence (33/36 -> LB ~0.78) so the dry-run reaches the
    # would-switch branch instead of being Wilson-held.
    _seed_verdicts(
        db_path,
        "bear_case",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        ["SWITCH_DOWN", "SWITCH_DOWN", "SWITCH_DOWN"],
        n_parity=11,
        n_cases=12,
    )
    results = evaluate_switches(db_path, consecutive_switch=3, consecutive_keep=3, dry_run=True)
    assert any(r.action == "DRY_RUN_SWITCH" for r in results)
    assert active_override("bear_case", db_path=db_path) is None  # nothing written


def test_record_verdict_persists_full_audit_trail(db_path: Path) -> None:
    """record_verdict (the single canonical writer) persists the audit columns —
    n_parity + summary_json — so a switch decision is reconstructable from the DB
    alone (the auditability requirement)."""
    import json as _json

    from llm.model_overrides import record_verdict

    audit = {
        "verdict": {"recommendation": "SWITCH_DOWN", "candidate_wins": 4, "ties": 3},
        "cases": [
            {
                "label": "company_description:NU:abc",
                "judge": "claude",
                "winner_model": "tie",
                "margin": 0.0,
                "rationales": ["both grounded", "tie on accuracy"],
            },
            {
                "label": "company_description:NU:abc",
                "judge": "gemini",
                "winner_model": "sonnet",
                "margin": 0.4,
                "rationales": ["sonnet more faithful", "sonnet wins"],
            },
        ],
    }
    record_verdict(
        purpose="company_description",
        candidate="claude-sonnet-4-6",
        incumbent="claude-opus-4-7",
        verdict="HOLD",
        run_id="run-audit-1",
        parity_rate=0.88,
        judge_agreement=0.25,
        n_cases=4,
        n_parity=7,
        summary_json=_json.dumps(audit),
        db_path=db_path,
    )
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT n_parity, summary_json, parity_rate FROM model_eval_verdicts WHERE run_id = ?",
            ("run-audit-1",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == 7  # n_parity persisted
    assert row[2] == 0.88
    restored = _json.loads(row[1])  # summary_json round-trips with per-case rationales
    assert restored["verdict"]["recommendation"] == "SWITCH_DOWN"
    assert len(restored["cases"]) == 2
    assert "sonnet more faithful" in restored["cases"][1]["rationales"]


def test_evaluate_switches_already_active_override_no_duplicate(db_path: Path) -> None:
    """When override is already active for the same candidate, action=ALREADY_ACTIVE."""
    from apply_model_switches import evaluate_switches

    from llm.model_overrides import write_pin_override

    write_pin_override("bear_case", "claude-haiku-4-5-20251001", db_path=db_path)
    _seed_verdicts(
        db_path,
        "bear_case",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        ["SWITCH_DOWN", "SWITCH_DOWN", "SWITCH_DOWN"],
    )
    results = evaluate_switches(db_path, consecutive_switch=3, consecutive_keep=3, dry_run=False)
    actions = [r.action for r in results]
    assert "ALREADY_ACTIVE" in actions
    # No second write.
    conn = sqlite3.connect(str(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM model_pin_overrides WHERE purpose='bear_case' AND active=1"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def _create_alerts_table(db_path: Path) -> None:
    """The alerts table lives in a pre-0083 migration outside this fixture's
    range; create the minimal shape _fire_switch_alert writes to."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            trigger_kind TEXT NOT NULL,
            fired_at TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_json TEXT,
            signature_sha TEXT UNIQUE
        )
        """
    )
    conn.commit()
    conn.close()


def test_evaluate_switches_candidate_errored_skips_streaks_and_alerts(db_path: Path) -> None:
    """A newest CANDIDATE_ERRORED verdict is an infra signal: no switch even
    after a prior SWITCH_DOWN streak, and an alert row is fired so the broken
    backend is visible instead of masquerading as a quality verdict."""
    from apply_model_switches import evaluate_switches

    from llm.model_overrides import active_override

    _create_alerts_table(db_path)
    _seed_verdicts(
        db_path,
        "qa_topics",
        "gemini-3-flash-preview",
        "claude-sonnet-4-6",
        ["SWITCH_DOWN", "SWITCH_DOWN", "SWITCH_DOWN", "CANDIDATE_ERRORED"],
    )
    results = evaluate_switches(db_path, consecutive_switch=3, consecutive_keep=3, dry_run=False)
    assert any(r.action == "CANDIDATE_ERRORED" for r in results)
    assert all(r.action != "SWITCHED_DOWN" for r in results)
    assert active_override("qa_topics", db_path=db_path) is None

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT ticker, trigger_kind FROM alerts WHERE ticker = '__model__qa_topics'"
    ).fetchone()
    conn.close()
    assert row is not None and row[1] == "model_pin_switch"


def test_evaluate_switches_candidate_errored_does_not_revert(db_path: Path) -> None:
    """With an active override, an errored week must not count toward the
    KEEP_INCUMBENT revert streak — the candidate wasn't measured, it broke."""
    from apply_model_switches import evaluate_switches

    from llm.model_overrides import active_override, write_pin_override

    write_pin_override("qa_topics", "gemini-3-flash-preview", db_path=db_path)
    _seed_verdicts(
        db_path,
        "qa_topics",
        "gemini-3-flash-preview",
        "claude-sonnet-4-6",
        ["KEEP_INCUMBENT", "KEEP_INCUMBENT", "CANDIDATE_ERRORED"],
    )
    results = evaluate_switches(db_path, consecutive_switch=3, consecutive_keep=3, dry_run=False)
    assert all(r.action != "REVERTED" for r in results)
    assert active_override("qa_topics", db_path=db_path) == "gemini-3-flash-preview"
