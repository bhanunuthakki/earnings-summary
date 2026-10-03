"""Tests for src/research/investment_decision_card.py — the P1.1 governed
generate + persist pipeline (PRD §8.1/§10.5).

``assess_eligibility`` and ``load_holdings_spec`` are monkeypatched to fixed
values (the deterministic-input layers they front are covered by their own
test modules — tests/test_allocation_eligibility.py etc.) so these tests
exercise only the card-generation seam: the LLM call, schema + grounding
validation, the corrective retry, the deterministic post-pass overwrite, the
deterministic fallback, and persistence. ``call_llm_structured_with_raw`` is
monkeypatched at the module seam per the repo's never-spend convention — no
real LLM call anywhere in this file.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import llm.structured as structured
import llm_artifact_store
import research.investment_decision_card as ridc
from allocation.eligibility import DecisionReadyAssessment
from compute.thesis_evaluator import HoldingsSpec
from identity import DEFAULT_USER_ID
from llm.cli import LLMBudgetExceeded
from llm.structured import StructuredCallResult

TICKER = "ACME"


@pytest.fixture(autouse=True)
def _block_unstubbed_card_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> str:
        raise AssertionError("card tests must stub the governed provider seam")

    monkeypatch.setattr(structured, "call_llm", blocked)


_DDL = """
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    list_type TEXT NOT NULL,
    archived_at TEXT
);
CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
    purpose VARCHAR(64) NOT NULL,
    fiscal_period VARCHAR(10),
    content_md TEXT,
    content_json TEXT,
    input_sha256 VARCHAR(64) NOT NULL,
    output_sha256 VARCHAR(64),
    model VARCHAR(64),
    prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    generated_at DATETIME NOT NULL,
    expires_at DATETIME,
    superseded_by_id INTEGER,
    dirty BOOLEAN NOT NULL DEFAULT 0,
    dirty_reason VARCHAR(128),
    source_doc_ids TEXT,
    parent_artifact_ids TEXT,
    llm_call_id INTEGER
);
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    valuation_date TEXT,
    npv_per_share REAL,
    live_price REAL,
    live_price_at TEXT,
    sanity_flag TEXT,
    is_latest INTEGER DEFAULT 1,
    segment_name TEXT,
    created_at TEXT
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    recommendation_kind TEXT
);
"""


def _make_db(tmp_path: Path, *, dcf_sanity_flag: str | None = None) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, list_type) VALUES (?, ?, 'evaluation')",
        (DEFAULT_USER_ID, TICKER),
    )
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price, "
        "live_price_at, sanity_flag, is_latest, created_at) "
        "VALUES (?, '2026-07-01', 50.0, 40.0, ?, ?, 1, '2026-07-01')",
        (TICKER, datetime.now(UTC).replace(tzinfo=None).isoformat(), dcf_sanity_flag),
    )
    conn.commit()
    conn.close()
    return db_path


def _assessment(
    *,
    eligible: bool = True,
    blocking: tuple[str, ...] = (),
    warning: tuple[str, ...] = (),
    input_sha: str = "assessment-sha-1",
) -> DecisionReadyAssessment:
    return DecisionReadyAssessment(
        ticker=TICKER,
        list_type="evaluation",
        eligible=eligible,
        blocking_reasons=blocking,
        warning_reasons=warning,
        source_freshness={"price": "2026-07-01", "dcf": "2026-07-01", "thesis": "2026-07-01"},
        hypothesis_origin="user_authored",
        portfolio_fit_status="scored",
        checks={},
        input_sha=input_sha,
    )


def _spec() -> HoldingsSpec:
    return HoldingsSpec(
        ticker=TICKER,
        thesis="ACME dominates checkout via network-effect attach.",
        break_rules=[],
        business_model_rules=[],
        soft_rules=[],
    )


def _llm_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "company_hypothesis": {
            "directional_thesis": "ACME grows via subscription attach to checkout volume.",
            "operating_mechanism": "Recurring take-rate on GMV processed through the network.",
            "key_kpis": ["ARR", "take rate"],
            "confirming_evidence": ["ARR up 20% YoY"],
            "disconfirming_evidence": [],
        },
        "security_setup": {
            "valuation_range": "fair value $50 vs live price $40",
            "appears_priced_in": "the market assumes flat take-rate through next year",
            "caveats": [],
        },
        "portfolio_fit": {
            "expected_role": "growth sleeve diversifier vs. the book's value tilt",
            "candidate_fit_summary": "fit score 1.1x, mildly accretive",
            "correlated_exposure": "low overlap with existing payments exposure",
        },
        "investment_profile": {
            "labels": ["long_term_compounder", "growth_inflection"],
            "summary": "A durable core engine with a still-improving growth curve.",
            "moat": {
                "level": "core_business",
                "evidence_coverage": "sufficient",
                "rationale": "Checkout integration and network density defend the core engine.",
                "supporting_evidence": ["Recurring take-rate on processed GMV"],
                "counter_evidence": ["Competitor pricing pressure"],
            },
        },
        "disconfirming_case": {
            "bear_hypothesis": "A larger competitor undercuts pricing, compressing take rate.",
            "evidence_that_would_confirm_it": "gross take-rate compression over 2 quarters",
            "next_proof_point": "next quarterly earnings call",
        },
        "suggested_disposition": "research_further",
        "uncertainty": {
            "confidence_verbal": "moderate",
            "justification": "The main reason I could be wrong is thin visibility into churn.",
            "what_would_change_it": "a full quarter of churn data",
        },
        "source_refs": ["price", "dcf"],
    }
    base.update(overrides)
    return base


def _patch_inputs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    assessment: DecisionReadyAssessment | None = None,
    spec: HoldingsSpec | None = None,
) -> None:
    fixed_assessment = assessment or _assessment()

    def _fake_assess_eligibility(*a: object, **kw: object) -> DecisionReadyAssessment:
        return fixed_assessment

    def _empty_dict(repo_root: Path) -> dict[str, object]:
        return {}

    monkeypatch.setattr(ridc, "assess_eligibility", _fake_assess_eligibility)
    monkeypatch.setattr(ridc, "read_materialized_candidate_fit", _empty_dict)
    monkeypatch.setattr(ridc, "read_materialized_fit_meta", _empty_dict)
    monkeypatch.setattr(ridc, "read_materialized_weights", _empty_dict)
    if spec is not None:
        fixed_spec = spec

        def _fake_load_holdings_spec(holdings_dir: Path, ticker: str) -> HoldingsSpec:
            return fixed_spec

        monkeypatch.setattr(ridc, "load_holdings_spec", _fake_load_holdings_spec)
    else:

        def _raise_not_found(holdings_dir: Path, ticker: str) -> HoldingsSpec:
            raise FileNotFoundError(ticker)

        monkeypatch.setattr(ridc, "load_holdings_spec", _raise_not_found)


def _fake_call_factory(payload: dict[str, object]):
    def fake_call(prompt: str, **kw: object) -> object:
        return StructuredCallResult(
            value=dict(payload),
            raw_response=json.dumps(payload),
            prompt=prompt,
        )

    return fake_call


# --------------------------------------------------------------------------- #
# Structural checks (§10.5)
# --------------------------------------------------------------------------- #


def test_required_sections_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))

    result = ridc.generate_card(db_path, tmp_path, TICKER)

    assert result.failure_reason is None
    assert result.selection_mode == "llm"
    card = result.card
    assert card is not None
    assert card.prompt_version == "v3"
    assert card.company_hypothesis.directional_thesis
    assert card.security_setup.appears_priced_in
    assert card.portfolio_fit.expected_role
    assert [label.value for label in card.investment_profile.labels] == [
        "long_term_compounder",
        "growth_inflection",
    ]
    assert card.investment_profile.moat.level is ridc.MoatLevel.CORE_BUSINESS
    assert card.disconfirming_case.bear_hypothesis
    assert card.evidence_readiness is not None
    assert card.uncertainty.justification
    assert card.suggested_disposition in ("pass", "watch", "research_further", "promote")


def test_persisted_markdown_keeps_actionable_decision_card_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))

    result = ridc.generate_card(db_path, tmp_path, TICKER)

    assert result.artifact_id is not None
    artifact = llm_artifact_store.read_current(
        ticker=TICKER, purpose=ridc.PURPOSE, scope="ticker", db_path=db_path
    )
    assert artifact is not None
    markdown = artifact.content_md or ""
    for expected in (
        "Recurring take-rate on GMV processed through the network.",
        "ARR, take rate",
        "fit score 1.1x, mildly accretive",
        "low overlap with existing payments exposure",
        "gross take-rate compression over 2 quarters",
        "next quarterly earnings call",
        "a full quarter of churn data",
    ):
        assert expected in markdown


def test_prompt_assigns_qualitative_profile_but_reserves_valuation_labels_for_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, spec=_spec())
    captured: list[str] = []

    def fake_call(prompt: str, **kw: object) -> object:
        captured.append(prompt)
        return _fake_call_factory(_llm_payload())(prompt)

    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", fake_call)
    result = ridc.generate_card(db_path, tmp_path, TICKER)

    assert result.selection_mode == "llm"
    assert captured
    prompt = captured[0]
    assert "multi_business" in prompt
    assert "core_business" in prompt
    assert "narrow_conditional" in prompt
    assert "none_demonstrated" in prompt
    assert "Do not assign garp or elite_growth_expensive" in prompt
    assert "operating expectations the market" in prompt
    assert "must identify the next report, event, or measurement" in prompt
    assert "must state a concrete, company-specific observation" in prompt


def test_company_security_portfolio_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload that repeats the same sentence across all three top-level
    judgments fails grounding twice and is forced to the deterministic
    fallback — the acceptance criterion is never a persisted artifact with
    indistinct sections."""
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, spec=_spec())
    duplicated = "This is a great business with a great moat."
    bad_payload = _llm_payload(
        company_hypothesis={
            "directional_thesis": duplicated,
            "operating_mechanism": "x",
            "key_kpis": [],
            "confirming_evidence": [],
            "disconfirming_evidence": [],
        },
        security_setup={
            "valuation_range": "n/a",
            "appears_priced_in": duplicated,
            "caveats": [],
        },
        portfolio_fit={
            "expected_role": duplicated,
            "candidate_fit_summary": "n/a",
            "correlated_exposure": "n/a",
        },
    )
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(bad_payload))

    result = ridc.generate_card(db_path, tmp_path, TICKER)

    assert result.selection_mode == "deterministic_fallback"
    assert any("distinct" in r for r in result.degraded_reasons)


def test_source_refs_must_resolve(tmp_path: Path) -> None:
    card = ridc.InvestmentDecisionCard.model_validate(
        {
            "ticker": TICKER,
            "as_of": "2026-07-01T00:00:00",
            "input_sha": "sha",
            "hypothesis_origin": "user_authored",
            "company_hypothesis": {"directional_thesis": "a", "operating_mechanism": "b"},
            "security_setup": {"appears_priced_in": "c"},
            "portfolio_fit": {"expected_role": "d"},
            "disconfirming_case": {"bear_hypothesis": "e"},
            "evidence_readiness": {"decision_ready": False},
            "suggested_disposition": "watch",
            "uncertainty": {"confidence_verbal": "low", "justification": "f"},
            "source_refs": ["price", "an invented citation nobody gathered"],
        }
    )
    reasons = card.validate_grounding(allowed_refs={"price", "dcf"})
    assert any("invented citation" in r for r in reasons)

    ok_reasons = card.model_copy(update={"source_refs": ["price"]}).validate_grounding(
        allowed_refs={"price", "dcf"}
    )
    assert not ok_reasons


@pytest.mark.parametrize(
    ("reference", "allowed"),
    [
        ("", ""),
        (" ", " "),
        ("pri", "price"),
        ("ACME dominates", "ACME dominates checkout via network-effect attach."),
        ("network-effect", "ACME dominates checkout via network-effect attach."),
    ],
)
def test_source_refs_reject_blank_and_partial_references(reference: str, allowed: str) -> None:
    card = ridc.InvestmentDecisionCard.model_validate(
        {
            **_llm_payload(source_refs=[reference]),
            "ticker": TICKER,
            "as_of": "2026-07-01T00:00:00",
            "input_sha": "sha",
            "hypothesis_origin": "user_authored",
            "evidence_readiness": {"decision_ready": False},
        }
    )
    assert card.validate_grounding(allowed_refs={allowed})


def test_partial_reference_retries_then_persists_labelled_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, spec=_spec())
    prompts: list[str] = []

    def fake_call(prompt: str, **kwargs: object) -> object:
        prompts.append(prompt)
        return _fake_call_factory(_llm_payload(source_refs=["pri"]))(prompt)

    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", fake_call)
    result = ridc.generate_card(db_path, tmp_path, TICKER)
    assert len(prompts) == 2
    assert "source_refs cites 'pri'" in prompts[1]
    assert result.selection_mode == "deterministic_fallback"
    assert any("not present in the gathered inputs" in reason for reason in result.degraded_reasons)
    assert result.card is not None and result.card.research_context is not None
    context = result.card.research_context
    assert len(context.exchanges) == 2
    assert context.exchanges[0].prompt == prompts[0]
    assert context.exchanges[1].prompt == prompts[1]
    assert context.exchanges[0].validation_reasons
    assert context.exchanges[1].validation_reasons
    assert context.exchanges[0].raw_response == json.dumps(_llm_payload(source_refs=["pri"]))
    assert context.selection_mode == "deterministic_fallback"
    assert result.artifact_id is not None
    assert result.card is not None
    assert result.card.source_refs == []
    assert ridc.read_card_record(result.artifact_id, db_path=db_path) == result.card


# --------------------------------------------------------------------------- #
# The hard gate: evidence_readiness is NEVER LLM-authored
# --------------------------------------------------------------------------- #


def test_dcf_outlier_forces_not_decision_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path, dcf_sanity_flag="unit_mismatch")
    _patch_inputs(monkeypatch, assessment=_assessment(eligible=True), spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))

    result = ridc.generate_card(db_path, tmp_path, TICKER)

    assert result.card is not None
    assert result.card.evidence_readiness.decision_ready is False
    assert any("sanity_flag" in b for b in result.card.evidence_readiness.blockers)
    assert any("sanity_flag" in c for c in result.card.security_setup.caveats)


def test_llm_claiming_decision_ready_is_overwritten_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the LLM's raw JSON smuggles in evidence_readiness.decision_ready
    = True while the assessment is blocked, the persisted card's
    evidence_readiness is entirely the deterministic overwrite — the LLM can
    NEVER set decision_ready=True when the assessment says blocked."""
    db_path = _make_db(tmp_path)
    blocked = _assessment(eligible=False, blocking=("no KPI coverage on file",))
    _patch_inputs(monkeypatch, assessment=blocked, spec=_spec())
    malicious = _llm_payload(
        evidence_readiness={
            "decision_ready": True,
            "blockers": [],
            "available_source_classes": ["everything"],
            "stale_or_missing": [],
        }
    )
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(malicious))

    result = ridc.generate_card(db_path, tmp_path, TICKER)

    assert result.card is not None
    assert result.card.evidence_readiness.decision_ready is False
    assert "no KPI coverage on file" in result.card.evidence_readiness.blockers


def test_blocked_evaluation_persists_explicit_blocker_without_llm_or_thesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    blocked = _assessment(
        eligible=False,
        blocking=("no thesis on file", "no KPI coverage on file"),
    )
    _patch_inputs(monkeypatch, assessment=blocked, spec=None)

    def _must_not_call(*args: object, **kwargs: object) -> object:
        raise AssertionError("blocked evaluations must not spend an LLM call")

    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _must_not_call)
    result = ridc.generate_card(db_path, tmp_path, TICKER)

    assert result.selection_mode == "deterministic_fallback"
    assert result.card is not None
    assert result.card.evidence_readiness.decision_ready is False
    assert result.card.company_hypothesis.directional_thesis.startswith(
        "No directional hypothesis is on file"
    )
    assert "no thesis on file" in result.card.evidence_readiness.blockers
    assert result.card.research_context is not None
    assert result.card.research_context.exchanges == ()
    assert result.card.research_context.selection_mode == "deterministic_fallback"
    assert result.artifact_id is not None
    assert ridc.read_card_record(result.artifact_id, db_path=db_path) == result.card


# --------------------------------------------------------------------------- #
# Generation never mutates disposition state
# --------------------------------------------------------------------------- #


def test_generation_never_mutates_disposition_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))

    ridc.generate_card(db_path, tmp_path, TICKER)

    conn = sqlite3.connect(str(db_path))
    try:
        n_decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        assert n_decisions == 0
        row = conn.execute(
            "SELECT list_type, archived_at FROM tracked_companies WHERE ticker = ?", (TICKER,)
        ).fetchone()
        assert row == ("evaluation", None)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Cache-hit / supersession
# --------------------------------------------------------------------------- #


def test_unchanged_inputs_reuse_the_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))

    first = ridc.generate_card(db_path, tmp_path, TICKER)
    second = ridc.generate_card(db_path, tmp_path, TICKER)

    assert first.artifact_id is not None
    assert first.artifact_id == second.artifact_id
    assert second.cache_hit is True

    artifact = llm_artifact_store.read_current(
        ticker=TICKER, purpose=ridc.PURPOSE, scope="ticker", db_path=db_path
    )
    assert artifact is not None
    assert artifact.id == first.artifact_id


def test_renderer_engine_version_invalidates_the_artifact_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))

    first = ridc.generate_card(db_path, tmp_path, TICKER)
    monkeypatch.setattr(ridc, "ENGINE_VERSION", "v4-test")
    second = ridc.generate_card(db_path, tmp_path, TICKER)

    assert first.artifact_id is not None
    assert second.artifact_id is not None
    assert second.artifact_id != first.artifact_id
    assert second.cache_hit is False
    assert second.card is not None
    assert second.card.engine_version == "v4-test"


def test_input_change_creates_a_new_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, assessment=_assessment(), spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))
    first = ridc.generate_card(db_path, tmp_path, TICKER)

    changed = _assessment(blocking=("a new blocker appeared",), input_sha="assessment-sha-2")
    _patch_inputs(monkeypatch, assessment=changed, spec=_spec())
    second = ridc.generate_card(db_path, tmp_path, TICKER)

    assert first.artifact_id is not None
    assert second.artifact_id is not None
    assert first.artifact_id != second.artifact_id
    assert second.cache_hit is False

    current = llm_artifact_store.read_current(
        ticker=TICKER, purpose=ridc.PURPOSE, scope="ticker", db_path=db_path
    )
    assert current is not None
    assert current.id == second.artifact_id


def _retention_db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    database = migrated_db(tmp_path / "card-context.db")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO tracked_companies (user_id,ticker,name,list_type) "
            "VALUES (?,?,'Synthetic card issuer','evaluation')",
            (DEFAULT_USER_ID, TICKER),
        )
        connection.execute(
            "INSERT INTO dcf_runs (ticker,valuation_date,horizon_years,revenue_growths_json,"
            "fcf_margin,wacc,terminal_growth,npv,npv_per_share,live_price,live_price_at) "
            "VALUES (?,'2026-07-01',5,'[]',0.1,0.1,0.02,500,50,40,'2026-07-01')",
            (TICKER,),
        )
    return database


def test_card_original_context_reopens_offline_after_inputs_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    database = _retention_db(tmp_path, migrated_db)
    _patch_inputs(monkeypatch, spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))
    bear_id, _ = llm_artifact_store.upsert(
        llm_artifact_store.UpsertRequest(
            ticker=TICKER,
            purpose="bear_case",
            content_md="Original bear evidence.",
            cache_inputs=["original bear"],
        ),
        db_path=database,
    )
    first = ridc.generate_card(database, tmp_path, TICKER)
    assert first.artifact_id is not None
    first_artifact = llm_artifact_store.read_artifact(first.artifact_id, db_path=database)
    assert first_artifact is not None
    assert first_artifact.parent_artifact_ids == [bear_id]
    assert first_artifact.llm_call_id is None
    assert first_artifact.model is None
    original = ridc.read_card_record(first.artifact_id, db_path=database)
    assert original is not None
    assert original.research_context is not None
    context = original.research_context
    assert context.inputs.holdings_spec is not None
    assert context.inputs.holdings_spec.thesis == _spec().thesis
    assert context.inputs.bear_case is not None
    assert context.inputs.bear_case.artifact_id == bear_id
    assert context.inputs.bear_case.content_md == "Original bear evidence."
    assert context.exchanges[0].prompt == context.base_prompt
    assert json.loads(context.exchanges[0].raw_response or "null") == _llm_payload()
    assert context.configuration.call_attribution == "unavailable_from_structured_api"
    assert context.used_refs == tuple(original.source_refs)

    _patch_inputs(monkeypatch, spec=_spec().model_copy(update={"thesis": "Changed owner thesis."}))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE dcf_runs SET live_price=45,live_price_at='2026-07-02'")
    second = ridc.generate_card(database, tmp_path, TICKER)
    assert second.artifact_id is not None and second.artifact_id != first.artifact_id

    def offline(*args: object, **kwargs: object) -> object:
        raise AssertionError("saved records must not read current inputs or call a provider")

    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", offline)
    monkeypatch.setattr(ridc, "_gather_inputs", offline)
    reopened = ridc.read_card_record(first.artifact_id, db_path=database)
    assert reopened == original
    changed = ridc.read_card_record(second.artifact_id, db_path=database)
    assert changed is not None and changed.research_context is not None
    assert changed.research_context.inputs.holdings_spec is not None
    assert changed.research_context.inputs.holdings_spec.thesis == "Changed owner thesis."
    assert original.security_setup.current_price == 40
    assert changed.security_setup.current_price == 45


@pytest.mark.parametrize("change", ["assessment", "price", "rule", "bear", "fit", "comp", "zone"])
def test_actual_card_input_changes_invalidate_opaque_cache_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path], change: str
) -> None:
    database = _retention_db(tmp_path, migrated_db)
    _patch_inputs(monkeypatch, spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))
    first = ridc.generate_card(database, tmp_path, TICKER)
    if change == "assessment":
        changed_assessment = replace(_assessment(), warning_reasons=("New evidence warning",))
        _patch_inputs(monkeypatch, spec=_spec(), assessment=changed_assessment)
    elif change == "price":
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE dcf_runs SET live_price=41")
    elif change == "rule":
        from compute.thesis_evaluator import BreakRule

        rule = BreakRule.model_validate(
            {
                "rule_id": "new-rule",
                "kpi_name": "Revenue",
                "comparator": "lt",
                "threshold": 1,
                "unit": "actual",
                "consecutive_periods": 1,
                "tier": "business_model",
                "narrative": "New threshold",
            }
        )
        _patch_inputs(monkeypatch, spec=_spec().model_copy(update={"break_rules": [rule]}))
    elif change == "bear":
        llm_artifact_store.upsert(
            llm_artifact_store.UpsertRequest(
                ticker=TICKER,
                purpose="bear_case",
                content_md="New bear context.",
                cache_inputs=["new bear"],
            ),
            db_path=database,
        )
    elif change == "fit":
        from allocation.candidate_fit import CandidateFit

        fit = CandidateFit(ticker=TICKER, factors=[], fit=1, why="Changed fit text", partial=False)

        def changed_fit(repo_root: Path) -> dict[str, CandidateFit]:
            return {TICKER: fit}

        monkeypatch.setattr(ridc, "read_materialized_candidate_fit", changed_fit)
    elif change == "comp":
        import report.sections.comp_set_context as comp_module

        comp = comp_module.CompSetContextSection(
            ticker=TICKER,
            comparable_set_id="synthetic",
            method_version=1,
            metric_class="operating",
            as_of_date=None,
            stale=False,
            n_members=3,
            primary_metrics=(),
            secondary_metrics=(),
            industry="Changed industry",
            sector=None,
            industry_scope=None,
            sector_scope=None,
            benchmark_etf="SYNTH",
            benchmark_sector_etf=None,
            benchmark_note="",
            members=(),
        )

        def changed_comp(*args: object, **kwargs: object) -> comp_module.CompSetContextSection:
            return comp

        monkeypatch.setattr(comp_module, "load_comp_set_context", changed_comp)
    else:

        def changed_zone(repo_root: Path, ticker: str) -> str:
            return "Changed zone"

        monkeypatch.setattr(ridc, "_expected_zone_if_funded", changed_zone)
    second = ridc.generate_card(database, tmp_path, TICKER)
    assert first.artifact_id is not None and second.artifact_id is not None
    assert first.artifact_id != second.artifact_id
    assert second.cache_hit is False


def test_rejected_exchange_and_corrective_exchange_are_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    database = _retention_db(tmp_path, migrated_db)
    _patch_inputs(monkeypatch, spec=_spec())
    prompts: list[str] = []
    raw_responses = [
        "\n" + json.dumps(_llm_payload(source_refs=["invented"])) + "\n",
        "```json\n" + json.dumps(_llm_payload()) + "\n```",
    ]

    def fake_call(prompt: str, **kwargs: object) -> structured.StructuredCallResult[object]:
        prompts.append(prompt)
        raw = raw_responses[len(prompts) - 1]
        return StructuredCallResult(
            value=structured.parse_json_payload(raw), raw_response=raw, prompt=prompt
        )

    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", fake_call)
    result = ridc.generate_card(database, tmp_path, TICKER)
    assert result.selection_mode == "llm" and result.artifact_id is not None
    saved = ridc.read_card_record(result.artifact_id, db_path=database)
    assert saved is not None and saved.research_context is not None
    exchanges = saved.research_context.exchanges
    assert len(exchanges) == 2
    assert exchanges[0].raw_response == raw_responses[0]
    assert exchanges[0].validation_reasons
    assert exchanges[1].raw_response == raw_responses[1]
    assert exchanges[1].requested_prompt == prompts[1]
    assert "YOUR PRIOR ATTEMPT FAILED" in prompts[1]
    assert exchanges[1].validation_reasons == ()


def test_final_schema_repair_exchange_retains_exact_available_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    database = _retention_db(tmp_path, migrated_db)
    _patch_inputs(monkeypatch, spec=_spec())
    prompts: list[str] = []
    raw = "  ```json\n" + json.dumps(_llm_payload(), indent=2) + "\n```  "

    def fake_provider(prompt: str, **kwargs: object) -> str:
        prompts.append(prompt)
        return "not JSON" if len(prompts) == 1 else raw

    monkeypatch.setattr(structured, "call_llm", fake_provider)
    result = ridc.generate_card(database, tmp_path, TICKER)
    assert result.artifact_id is not None
    saved = ridc.read_card_record(result.artifact_id, db_path=database)
    assert saved is not None and saved.research_context is not None
    context = saved.research_context
    assert len(context.exchanges) == 1
    assert context.exchanges[0].requested_prompt == prompts[0] == context.base_prompt
    assert context.exchanges[0].prompt == prompts[1]
    assert context.exchanges[0].raw_response == raw
    assert context.exchanges[0].schema_repair_reason
    assert context.configuration.exchange_coverage == "final_schema_valid_exchange_per_call"


@pytest.mark.parametrize("failure_type", [RuntimeError, LLMBudgetExceeded])
def test_failed_call_fallback_does_not_hide_later_success_with_same_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
    failure_type: type[RuntimeError],
) -> None:
    database = _retention_db(tmp_path, migrated_db)
    _patch_inputs(monkeypatch, spec=_spec())

    def failed_call(*args: object, **kwargs: object) -> object:
        raise failure_type("synthetic call failure")

    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", failed_call)
    fallback = ridc.generate_card(database, tmp_path, TICKER)
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))
    success = ridc.generate_card(database, tmp_path, TICKER)
    assert fallback.artifact_id is not None and success.artifact_id is not None
    assert fallback.artifact_id != success.artifact_id
    assert success.selection_mode == "llm" and not success.cache_hit
    assert fallback.card is not None and fallback.card.research_context is not None
    exchange = fallback.card.research_context.exchanges[0]
    assert exchange.failure_class == failure_type.__name__
    assert exchange.prompt is None and exchange.raw_response is None
    assert ridc.read_card_record(fallback.artifact_id, db_path=database) == fallback.card


@pytest.mark.parametrize(
    "damage", ["missing", "corrupt", "missing_marker", "output", "artifact_refs"]
)
def test_new_card_context_fails_explicitly_when_missing_or_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path], damage: str
) -> None:
    database = _retention_db(tmp_path, migrated_db)
    _patch_inputs(monkeypatch, spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))
    result = ridc.generate_card(database, tmp_path, TICKER)
    assert result.artifact_id is not None
    with sqlite3.connect(database) as connection:
        raw = connection.execute(
            "SELECT content_json FROM llm_artifacts WHERE id=?", (result.artifact_id,)
        ).fetchone()[0]
        payload = json.loads(raw)
        if damage == "missing":
            payload.pop("research_context", None)
        elif damage == "corrupt":
            payload["research_context_sha256"] = "0" * 64
        elif damage == "missing_marker":
            payload.pop("record_format", None)
            payload.pop("research_context", None)
            payload.pop("research_context_sha256", None)
        elif damage == "output":
            payload["company_hypothesis"]["directional_thesis"] = "Altered output"
        else:
            connection.execute(
                "UPDATE llm_artifacts SET parent_artifact_ids='[999]' WHERE id=?",
                (result.artifact_id,),
            )
        connection.execute(
            "UPDATE llm_artifacts SET content_json=? WHERE id=?",
            (json.dumps(payload), result.artifact_id),
        )
    with pytest.raises(ValueError, match="context"):
        ridc.read_card_record(result.artifact_id, db_path=database)


def test_legacy_card_context_is_explicitly_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    database = _retention_db(tmp_path, migrated_db)
    _patch_inputs(monkeypatch, spec=_spec())
    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", _fake_call_factory(_llm_payload()))
    result = ridc.generate_card(database, tmp_path, TICKER)
    assert result.card is not None
    legacy = result.card.model_dump(mode="json")
    legacy["engine_version"] = "v4"
    for name in ("record_format", "research_context", "research_context_sha256"):
        legacy.pop(name, None)
    artifact_id, _ = llm_artifact_store.upsert(
        llm_artifact_store.UpsertRequest(
            ticker=TICKER, purpose=ridc.PURPOSE, content_json=legacy, cache_inputs=["legacy"]
        ),
        db_path=database,
    )
    assert artifact_id is not None
    reopened = ridc.read_card_record(artifact_id, db_path=database)
    assert reopened is not None
    assert reopened.record_format == "legacy_unavailable"
    assert reopened.research_context is None
    from io import StringIO

    from report.renderers.workspace_sections.chrome import _decision_card_strip
    from report.sections.investment_decision_card import build

    with sqlite3.connect(database) as connection:
        section = build(TICKER, tmp_path, conn=connection)
    assert section is not None
    assert section.company_hypothesis is not None
    assert (
        section.company_hypothesis.directional_thesis
        == reopened.company_hypothesis.directional_thesis
    )
    assert section.source_refs == reopened.source_refs
    body = StringIO()
    _decision_card_strip(body, section, ticker=TICKER)
    assert reopened.company_hypothesis.directional_thesis in body.getvalue()
    assert f'data-artifact-id="{artifact_id}"' in body.getvalue()


# --------------------------------------------------------------------------- #
# Fallback behavior
# --------------------------------------------------------------------------- #


def test_no_thesis_on_file_is_an_explicit_failure_not_a_fabrication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD §10.6: do not call the LLM as a substitute for missing evidence.
    With no thesis on file, even a transient LLM failure must surface an
    explicit CardResult(failure_reason=...) rather than a fabricated card."""
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, spec=None)  # load_holdings_spec raises FileNotFoundError

    def raise_transient(prompt: str, **kw: object) -> object:
        raise RuntimeError("simulated transient failure")

    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", raise_transient)

    result = ridc.generate_card(db_path, tmp_path, TICKER)

    assert result.selection_mode == "failed"
    assert result.card is None
    assert result.artifact_id is None
    assert result.failure_reason is not None


def test_budget_forgone_degrades_to_labeled_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _patch_inputs(monkeypatch, spec=_spec())

    class _Check:
        reason = "over cap"

    def _fake_should_skip(purpose: str, **kw: object) -> _Check:
        return _Check()

    monkeypatch.setattr(ridc, "should_skip_for_budget", _fake_should_skip)

    def fail_if_called(prompt: str, **kw: object) -> object:
        raise AssertionError("the LLM must not be called when budget-skipped")

    monkeypatch.setattr(ridc, "call_llm_structured_with_raw", fail_if_called)

    result = ridc.generate_card(db_path, tmp_path, TICKER)

    assert result.selection_mode == "deterministic_fallback"
    assert result.card is not None
    assert any("budget" in r for r in result.degraded_reasons)
    assert result.artifact_id is not None and result.card.research_context is not None
    assert result.card.research_context.exchanges == ()
    assert ridc.read_card_record(result.artifact_id, db_path=db_path) == result.card


# --------------------------------------------------------------------------- #
# Workspace strip: hides when absent, renders disposition buttons when present
# (PRD §8.1 frontend rule — "missing -> nothing (no stub)")
# --------------------------------------------------------------------------- #


def test_workspace_strip_hides_when_no_card() -> None:
    from io import StringIO

    from report.renderers.workspace_sections.chrome import _decision_card_strip

    body = StringIO()
    _decision_card_strip(body, None, ticker=TICKER)
    assert body.getvalue() == ""


def test_workspace_strip_renders_disposition_buttons_when_present() -> None:
    from io import StringIO

    from report.models import (
        DecisionCardDisconfirmingCase,
        DecisionCardEvidenceReadiness,
        DecisionCardHypothesis,
        DecisionCardPortfolioFit,
        DecisionCardSecuritySetup,
        DecisionCardUncertainty,
        InvestmentDecisionCardSection,
        SectionStatus,
    )
    from report.renderers.workspace_sections.chrome import _decision_card_strip

    section = InvestmentDecisionCardSection(
        status=SectionStatus.OK,
        ticker=TICKER,
        artifact_id=42,
        generated_at=datetime(2026, 7, 1, tzinfo=UTC),
        suggested_disposition="watch",
        company_hypothesis=DecisionCardHypothesis(directional_thesis="ACME grows via X."),
        security_setup=DecisionCardSecuritySetup(appears_priced_in="flat growth priced in"),
        portfolio_fit=DecisionCardPortfolioFit(expected_role="growth diversifier"),
        disconfirming_case=DecisionCardDisconfirmingCase(bear_hypothesis="competitor undercuts"),
        evidence_readiness=DecisionCardEvidenceReadiness(decision_ready=True),
        uncertainty=DecisionCardUncertainty(
            confidence_verbal="moderate", justification="thin data"
        ),
    )
    body = StringIO()
    _decision_card_strip(body, section, ticker=TICKER)
    html = body.getvalue()

    assert 'class="l1-decision-card"' in html
    assert "Decision-ready" in html
    for verb in ("pass", "watch", "research_further", "promote"):
        assert f'data-verb="{verb}"' in html
    assert 'data-artifact-id="42"' in html
    assert "ACME grows via X." in html
    assert "growth diversifier" in html
    assert 'href="/?copilot=1&amp;ticker=ACME#screen-workspace"' in html
    assert "/chat/" not in html


def test_workspace_strip_hides_when_artifact_absent_no_stub(tmp_path: Path) -> None:
    """report.sections.investment_decision_card.build returns None (not a
    stub section) when no artifact has been generated yet — the strip must
    render nothing, never a placeholder card."""
    import report.sections.investment_decision_card as section_mod

    section = section_mod.build(TICKER, tmp_path)  # no data/portfolio.db under tmp_path
    assert section is None
