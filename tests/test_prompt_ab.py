"""Tests for the prompt A/B harness + the Q1 auto-apply (src/llm/prompt_ab.py —
PR5 of meta_eval_governance.md §4/§10). All LLM calls are DI'd fakes /
monkeypatched; the suite never spends."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from llm.prompt_ab import (
    AB_HOLD,
    AB_INSUFFICIENT,
    KEEP_BASELINE,
    PROMOTE_VARIANT,
    VARIANT_ERRORED,
    EditAnchorError,
    PromptEdit,
    active_prompt_override,
    apply_edits,
    apply_prompt_override,
    create_experiment,
    deactivate_prompt_override,
    decide_ab,
    edits_from_json,
    edits_to_json,
    promotion_ready,
    propose_variant,
    record_ab_verdict,
    validate_edits_against,
    write_prompt_override,
)


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE prompt_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL, baseline_prompt_version TEXT NOT NULL,
            variant_label TEXT NOT NULL, hypothesis TEXT NOT NULL,
            edits_json TEXT NOT NULL, frozen_model TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed', decision TEXT,
            created_at TEXT NOT NULL, decided_at TEXT, notes TEXT
        );
        CREATE TABLE prompt_ab_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
            purpose TEXT NOT NULL, run_id TEXT NOT NULL, n_cases INTEGER,
            variant_wins INTEGER, baseline_wins INTEGER, ties INTEGER,
            win_rate REAL, judge_agreement REAL, recommendation TEXT NOT NULL,
            reason TEXT NOT NULL, summary_json TEXT, recorded_at TEXT NOT NULL
        );
        CREATE TABLE prompt_pin_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT, purpose TEXT NOT NULL,
            edits_json TEXT NOT NULL, experiment_id TEXT NOT NULL,
            set_by TEXT NOT NULL, set_at TEXT NOT NULL, reason_json TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


_EDITS = (PromptEdit(find="Answer briefly.", replace="Answer in exactly three bullets."),)


# ---------------------------------------------------------------------------
# apply_edits — the §4.1 invariant
# ---------------------------------------------------------------------------


def test_apply_edits_exact_once() -> None:
    out = apply_edits("Do the task. Answer briefly. Thanks.", _EDITS)
    assert out == "Do the task. Answer in exactly three bullets. Thanks."


def test_apply_edits_rejects_missing_and_duplicate_anchor() -> None:
    with pytest.raises(EditAnchorError):
        apply_edits("No anchor here.", _EDITS)
    with pytest.raises(EditAnchorError):
        apply_edits("Answer briefly. Answer briefly.", _EDITS)


def test_apply_edits_ordered() -> None:
    edits = (
        PromptEdit(find="AAA", replace="BBB"),
        PromptEdit(find="BBB CCC", replace="DDD"),  # anchors on edit-1's output
    )
    assert apply_edits("AAA CCC", edits) == "DDD"


def test_validate_edits_against_template_and_renders() -> None:
    template = "SCAFFOLD Answer briefly. {data}"
    renders = ["SCAFFOLD Answer briefly. NU data", "SCAFFOLD Answer briefly. MELI data"]
    assert validate_edits_against(_EDITS, template, renders) is True
    # An edit anchored in the DATA region can't hold across renders.
    data_edit = (PromptEdit(find="NU data", replace="x"),)
    assert validate_edits_against(data_edit, template, renders) is False
    assert validate_edits_against((), template, renders) is False


def test_edits_json_roundtrip() -> None:
    assert edits_from_json(edits_to_json(_EDITS)) == _EDITS
    assert edits_from_json("not json") == ()
    assert edits_from_json('[{"find": "", "replace": "x"}]') == ()


# ---------------------------------------------------------------------------
# Proposal validation
# ---------------------------------------------------------------------------


def test_propose_variant_validates_shape() -> None:
    def struct(prompt: str, **kwargs: object) -> object:
        assert kwargs.get("purpose") == "prompt_variant_propose"
        assert kwargs.get("scope") == "meta_eval"
        return {
            "hypothesis": "schema at the end cuts format misses",
            "edits": [{"find": "Answer briefly.", "replace": "Answer tersely."}],
            "expected_effect": "format facet",
        }

    p = propose_variant(
        purpose="bear_case",
        template="T Answer briefly.",
        rendered_example="T Answer briefly. DATA",
        improvement_signal="format losses",
        struct=struct,
    )
    assert p is not None
    assert p.edits == (PromptEdit(find="Answer briefly.", replace="Answer tersely."),)


def test_propose_variant_fails_closed() -> None:
    def bad_struct(prompt: str, **kwargs: object) -> object:
        return {"hypothesis": "x", "edits": [{"find": "", "replace": "y"}]}

    assert (
        propose_variant(
            purpose="p",
            template="t",
            rendered_example="r",
            improvement_signal="s",
            struct=bad_struct,
        )
        is None
    )

    def raising(prompt: str, **kwargs: object) -> object:
        raise RuntimeError("down")

    assert (
        propose_variant(
            purpose="p",
            template="t",
            rendered_example="r",
            improvement_signal="s",
            struct=raising,
        )
        is None
    )


# ---------------------------------------------------------------------------
# decide_ab — the per-run verdict
# ---------------------------------------------------------------------------


def test_decide_ab_promotes_on_strict_wins() -> None:
    rec, _ = decide_ab(
        per_judge={"claude": (8, 1, 3), "gemini": (9, 1, 2)},
        judge_agreement=0.8,
        n_cases_attempted=12,
        n_variant_errors=0,
    )
    assert rec == PROMOTE_VARIANT


def test_decide_ab_zero_regression_guard() -> None:
    # 60%+ wins but baseline takes 25% -> held (churn guard).
    rec, _ = decide_ab(
        per_judge={"claude": (8, 3, 1)},
        judge_agreement=0.9,
        n_cases_attempted=12,
        n_variant_errors=0,
    )
    assert rec == AB_HOLD


def test_decide_ab_keep_baseline_and_errors() -> None:
    rec, _ = decide_ab(
        per_judge={"claude": (2, 8, 2)},
        judge_agreement=0.9,
        n_cases_attempted=12,
        n_variant_errors=0,
    )
    assert rec == KEEP_BASELINE
    rec2, _ = decide_ab(
        per_judge={"claude": (1, 5, 0)},
        judge_agreement=1.0,
        n_cases_attempted=12,
        n_variant_errors=6,
    )
    assert rec2 == VARIANT_ERRORED
    rec3, _ = decide_ab(
        per_judge={"claude": (2, 0, 0)},
        judge_agreement=1.0,
        n_cases_attempted=2,
        n_variant_errors=0,
    )
    assert rec3 == AB_INSUFFICIENT


# ---------------------------------------------------------------------------
# The pooled promotion bar (§4.4)
# ---------------------------------------------------------------------------


def _seed_run(db_path: Path, experiment_id: str, rec: str, n_cases: int) -> None:
    record_ab_verdict(
        db_path,
        experiment_id=experiment_id,
        purpose="bear_case",
        run_id="r",
        n_cases=n_cases,
        variant_wins=n_cases - 1,
        baseline_wins=0,
        ties=1,
        win_rate=0.9,
        judge_agreement=0.9,
        recommendation=rec,
        reason="seeded",
    )


def test_promotion_ready_pooled_bar(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    exp = create_experiment(
        db_path,
        purpose="bear_case",
        baseline_prompt_version="v2",
        hypothesis="h",
        edits=_EDITS,
        frozen_model="claude-sonnet-4-6",
    )
    ready, _ = promotion_ready(db_path, exp)
    assert ready is False  # no runs yet
    _seed_run(db_path, exp, PROMOTE_VARIANT, 6)
    ready, _ = promotion_ready(db_path, exp)
    assert ready is False  # 1 run / 6 cases
    _seed_run(db_path, exp, PROMOTE_VARIANT, 6)
    ready, why = promotion_ready(db_path, exp)
    assert ready is True and "12 pooled cases" in why
    # Any KEEP_BASELINE poisons promotion.
    _seed_run(db_path, exp, KEEP_BASELINE, 6)
    ready, why = promotion_ready(db_path, exp)
    assert ready is False and "KEEP_BASELINE" in why


# ---------------------------------------------------------------------------
# prompt_pin_overrides — the Q1 auto-apply
# ---------------------------------------------------------------------------


def test_override_write_read_demote(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    write_prompt_override("bear_case", _EDITS, experiment_id="exp1", db_path=db_path)
    assert active_prompt_override("bear_case", db_path=db_path) == _EDITS
    # One active row per purpose: a second write supersedes.
    edits2 = (PromptEdit(find="Answer briefly.", replace="Answer at length."),)
    write_prompt_override("bear_case", edits2, experiment_id="exp2", db_path=db_path)
    assert active_prompt_override("bear_case", db_path=db_path) == edits2
    # The audit trail keeps the git-reconciliation diff.
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT active, reason_json FROM prompt_pin_overrides ORDER BY id"
    ).fetchall()
    conn.close()
    assert [int(a) for a, _r in rows] == [0, 1]
    reason = json.loads(str(rows[1][1]))
    assert reason["experiment_id"] == "exp2"
    assert reason["edits"][0]["find"] == "Answer briefly."
    # Demote.
    assert deactivate_prompt_override("bear_case", db_path=db_path) is True
    assert active_prompt_override("bear_case", db_path=db_path) is None
    assert deactivate_prompt_override("bear_case", db_path=db_path) is False  # idempotent


def test_apply_hook_production_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _db(tmp_path)
    write_prompt_override("bear_case", _EDITS, experiment_id="exp1", db_path=db_path)
    import llm.prompt_ab as pab

    def fake_active(purpose: str, *, db_path: object = None) -> tuple[PromptEdit, ...] | None:
        return _EDITS if purpose == "bear_case" else None

    monkeypatch.setattr(pab, "active_prompt_override", fake_active)
    prompt = "Do the task. Answer briefly."
    # Production scope (None / ticker-ish): edits apply.
    assert "three bullets" in apply_prompt_override("bear_case", None, prompt)
    assert "three bullets" in apply_prompt_override("bear_case", "ticker:NU", prompt)
    # Eval/meta scopes: byte-identical passthrough (I1).
    for scope in ("model_eval", "backend_judge", "eval", "prompt_ab", "meta_eval"):
        assert apply_prompt_override("bear_case", scope, prompt) == prompt
    # No purpose / no override: passthrough.
    assert apply_prompt_override(None, None, prompt) == prompt
    assert apply_prompt_override("other_purpose", None, prompt) == prompt


def test_apply_hook_fails_open_on_anchor_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import llm.prompt_ab as pab

    def fake_active(purpose: str, *, db_path: object = None) -> tuple[PromptEdit, ...] | None:
        return _EDITS

    monkeypatch.setattr(pab, "active_prompt_override", fake_active)
    drifted = "The template changed and the anchor is gone."
    assert apply_prompt_override("bear_case", None, drifted) == drifted  # fail-open


def test_call_llm_hook_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production hook is reachable from call_llm: with a monkeypatched
    override the transported prompt carries the edit; eval scope does not."""
    import llm.cli as cli
    import llm.prompt_ab as pab

    def fake_active(purpose: str, *, db_path: object = None) -> tuple[PromptEdit, ...] | None:
        return _EDITS if purpose == "zz_test_purpose" else None

    monkeypatch.setattr(pab, "active_prompt_override", fake_active)
    sent: list[str] = []

    def fake_claude(prompt: str, **kwargs: object) -> str:
        sent.append(prompt)
        return "ok"

    monkeypatch.setattr(cli, "_call_claude", fake_claude)
    cli.call_llm("Task. Answer briefly.", purpose="zz_test_purpose")
    assert sent and "three bullets" in sent[0]
    sent.clear()
    cli.call_llm("Task. Answer briefly.", purpose="zz_test_purpose", scope="model_eval")
    assert sent and sent[0] == "Task. Answer briefly."
