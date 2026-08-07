"""The meta-eval isolation contract, enforced in one place
(meta_eval_governance.md §5 — the seven testable invariants, PR6).

The owner's hardest requirement: THE GENERATING CALL MUST BE BLIND TO THE
HARNESS. When any eval subsystem causes a response to be generated, the prompt
bytes must be exactly what production would send, over the production
transport. Each earlier PR shipped local guards; this suite is the standing,
consolidated statement of the contract so a future feature that violates it
fails HERE with the invariant named.

I7 (asymmetric knowledge is fine downstream, never upstream) is a design rule,
not a runtime property: the DECISION layer (apply_model_switches, promotion)
may know everything; the GENERATION layer knows nothing; the JUDGE layer knows
only task + outputs + checklist. Any new feature needing the generator to
"know" about the eval is mis-designed — move that knowledge down the chain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"

# Every meta-machinery purpose introduced by the governance build (PR2-PR5)
# that ACTUALLY calls an LLM — i.e. carries a prompt, a model pin, and needs
# capture/coverage isolation for that call. ``model_frontier_research`` is
# deliberately absent: since 2026-08-06 it pulls OpenRouter's public model
# catalog (a plain HTTP GET, no LLM call at all — see llm/frontier.py's module
# docstring), so the prompt/version/pin invariants below don't apply to it. It
# remains excluded from production cost accounting in llm/capture.py and
# evals/coverage.py regardless (still infrastructure, not a user-facing
# purpose) — those lists are supersets and untouched by this change.
META_MACHINERY_PURPOSES = frozenset(
    {
        "case_difficulty_classify",
        "optimizer_nominator",
        "query_criteria_derive",
        "prompt_variant_propose",
    }
)


# ---------------------------------------------------------------------------
# I1 — byte-identity of generation prompts
# ---------------------------------------------------------------------------


def test_i1_replay_sends_case_prompt_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replayed generation call sends EXACTLY the captured prompt — no
    wrappers, no [EVAL] markers, no criteria, no experiment labels."""
    import hashlib

    import llm.model_eval as me

    sent: list[str] = []

    def fake_call_llm(prompt: str, **kwargs: object) -> str:
        sent.append(prompt)
        return "out"

    monkeypatch.setattr(me, "call_llm", fake_call_llm)
    original = "PRODUCTION PROMPT BODY — untouched"
    me.run_model(original, model_id="claude-haiku-4-5-20251001", purpose="p")
    assert len(sent) == 1
    assert (
        hashlib.sha256(sent[0].encode()).hexdigest()
        == hashlib.sha256(original.encode()).hexdigest()
    )


def test_i1_variant_is_exactly_apply_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The A/B variant prompt == apply_edits(baseline): the edit list is the
    ENTIRE intended change (§4.1 diff-coverage)."""
    from llm.prompt_ab import PromptEdit, apply_edits

    baseline = "SCAFFOLD Answer briefly. DATA-REGION xyz"
    edits = (PromptEdit(find="Answer briefly.", replace="Answer in bullets."),)
    variant = apply_edits(baseline, edits)
    # The variant differs from baseline ONLY by the intended replacement.
    assert variant.replace("Answer in bullets.", "Answer briefly.") == baseline


def test_i1_no_eval_sentinels_in_generation_templates() -> None:
    """No meta-machinery sentinel or test-framing marker can reach a generation
    prompt through run_model — it passes its input through verbatim (asserted
    above); belt-and-braces, the judge-side sentinel is not importable into any
    generation template constant."""
    from llm.query_criteria import CRITERIA_SENTINEL

    # The sentinel exists exactly once as a constant, rendered only by
    # render_criteria_block (judge-side).
    hits: list[str] = []
    for py in (_SRC / "llm").glob("*.py"):
        text = py.read_text(encoding="utf-8")
        if CRITERIA_SENTINEL in text and py.name not in ("query_criteria.py", "backend_judge.py"):
            hits.append(py.name)
    assert not hits, f"criteria sentinel leaked into: {hits}"


# ---------------------------------------------------------------------------
# I2 — out-of-band bookkeeping only
# ---------------------------------------------------------------------------


def test_i2_prompt_templates_carry_no_bookkeeping_fields() -> None:
    """Experiment identity travels in ledger COLUMNS (purpose/scope/run_id),
    never in prompt text: no meta template interpolates run/experiment ids."""
    from evals.sampler import _CLASSIFY_INSTRUCTIONS  # pyright: ignore[reportPrivateUsage]
    from llm.nominator import NOMINATOR_PROMPT
    from llm.prompt_ab import PROPOSE_PROMPT
    from llm.query_criteria import DERIVE_PROMPT

    for name, template in {
        "classify": _CLASSIFY_INSTRUCTIONS,
        "nominator": NOMINATOR_PROMPT,
        "propose": PROPOSE_PROMPT,
        "derive": DERIVE_PROMPT,
    }.items():
        for token in ("{run_id", "{experiment_id", "{scope", "{nomination_run_id"):
            assert token not in template, f"{name} template interpolates {token}"


def test_i2_judge_template_carries_no_bookkeeping_fields() -> None:
    from llm.backend_judge import _PROMPT_TEMPLATE  # pyright: ignore[reportPrivateUsage]

    for token in ("{run_id", "{experiment_id", "{scope", "{model", "{candidate"):
        assert token not in _PROMPT_TEMPLATE


# ---------------------------------------------------------------------------
# I3 — production transport, exactly
# ---------------------------------------------------------------------------


def test_i3_replay_kwargs_are_production_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_model goes through call_llm with ONLY the permitted deltas: an
    explicit model (what production would send if the pin changed), an explicit
    backend (so a candidate's failure is ITS failure), the eval scope, and the
    budget bypass (affects WHETHER the call happens, never its content)."""
    import llm.model_eval as me

    seen_kwargs: list[dict[str, object]] = []

    def fake_call_llm(prompt: str, **kwargs: object) -> str:
        seen_kwargs.append(dict(kwargs))
        return "out"

    monkeypatch.setattr(me, "call_llm", fake_call_llm)
    me.run_model("P", model_id="claude-haiku-4-5-20251001", purpose="p", ticker="NU")
    assert set(seen_kwargs[0]) == {
        "purpose",
        "model",
        "backend",
        "ticker",
        "scope",
        "run_id",
        "timeout_seconds",
        "force_budget_bypass",
    }
    assert seen_kwargs[0]["scope"] == "model_eval"
    assert seen_kwargs[0]["force_budget_bypass"] is True
    # No system-prompt injection channel exists on this path at all.
    assert not any("system" in k for k in seen_kwargs[0])


# ---------------------------------------------------------------------------
# I4 — judge/meta quarantine (capture + coverage)
# ---------------------------------------------------------------------------


def test_i4_meta_purposes_in_capture_denylist() -> None:
    from llm.capture import CAPTURE_DENYLIST

    missing = META_MACHINERY_PURPOSES - CAPTURE_DENYLIST
    assert not missing, f"meta purposes capturable (would nest corpora): {missing}"
    # The judges stay denied too.
    assert {"backend_compare_judge", "eval_judge"} <= CAPTURE_DENYLIST


def test_i4_meta_purposes_registered_and_covered() -> None:
    from evals.coverage import META_PURPOSES
    from llm.cli import LLM_MODELS
    from llm.prompt_versions import registered_purposes

    missing_meta = META_MACHINERY_PURPOSES - META_PURPOSES
    assert not missing_meta, f"meta purposes not in META_PURPOSES: {missing_meta}"
    missing_pin = META_MACHINERY_PURPOSES - set(LLM_MODELS)
    assert not missing_pin, f"meta purposes without a model pin: {missing_pin}"
    missing_ver = META_MACHINERY_PURPOSES - registered_purposes()
    assert not missing_ver, f"meta purposes without a prompt version: {missing_ver}"


# ---------------------------------------------------------------------------
# I5 — no self-observation loops
# ---------------------------------------------------------------------------


def test_i5_eval_scopes_cover_all_measurement_scopes() -> None:
    from llm.eval_scopes import EVAL_SCOPES

    assert {"model_eval", "backend_judge", "eval", "prompt_ab", "meta_eval"} <= EVAL_SCOPES


def test_i5_panel_uses_canonical_scopes() -> None:
    from llm.eval_scopes import EVAL_SCOPES
    from pipeline.model_eval_panel import _EVAL_SCOPES  # pyright: ignore[reportPrivateUsage]

    assert set(_EVAL_SCOPES) == set(EVAL_SCOPES)


def test_i5_inventory_and_census_exclude_eval_scopes(tmp_path: Path) -> None:
    """Eval traffic never inflates a purpose's workload or its census."""
    import sqlite3

    from evals.sampler import load_census
    from llm.workload_inventory import build_workload_inventory

    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, called_at TEXT, purpose TEXT,"
        " ticker TEXT, scope TEXT, model TEXT DEFAULT 'm', prompt_sha256 TEXT,"
        " prompt_chars INTEGER DEFAULT 0, cost_estimate_usd REAL)"
    )
    from datetime import UTC, datetime

    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    for scope in ("model_eval", "backend_judge", "eval", "prompt_ab", "meta_eval"):
        conn.execute(
            "INSERT INTO llm_calls (called_at, purpose, scope, prompt_sha256,"
            " prompt_chars, cost_estimate_usd) VALUES (?, 'p', ?, 'x', 10, 99.0)",
            (now, scope),
        )
    conn.commit()
    conn.close()
    assert build_workload_inventory(db_path) == []  # all traffic is measurement
    assert load_census(db_path, "p") == {}


# ---------------------------------------------------------------------------
# I6 — blindness at grade time
# ---------------------------------------------------------------------------


def test_i6_judge_prompt_is_brand_and_role_blind() -> None:
    from llm.backend_judge import build_judge_prompt

    prompt = build_judge_prompt("bear_case", "TASK", "out one", "out two")
    lowered = prompt.lower()
    for forbidden in ("candidate", "incumbent", "variant", "baseline", "downgrade", "experiment"):
        assert forbidden not in lowered, f"judge prompt leaks role framing: {forbidden!r}"
    for forbidden in ("claude", "gemini", "opus", "sonnet", "haiku", "deepseek"):
        assert forbidden not in lowered, f"judge prompt leaks a brand: {forbidden!r}"
    assert "RESPONSE A" in prompt and "RESPONSE B" in prompt


def test_i6_checklist_carries_no_side_information() -> None:
    """The §3 checklist is derived from the task prompt alone — rendering it
    can only contain criterion text, never model/side labels."""
    from llm.query_criteria import Criterion, render_criteria_block

    block = render_criteria_block(
        (Criterion(id="c1", kind="content", weight=2, statement="Names >=2 risks"),)
    )
    lowered = block.lower()
    for forbidden in ("candidate", "incumbent", "variant", "baseline", "claude", "gemini"):
        assert forbidden not in lowered
