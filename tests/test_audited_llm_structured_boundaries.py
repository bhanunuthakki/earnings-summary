"""Regression contracts for the four audited programmatic LLM paths.

All provider calls are replaced. These tests exercise the typed boundary,
closed decisions, and failure/no-change distinction without spending quota.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

import execution.extract_risk_factors as risk_factors
import execution.pressure_test_thesis as pressure_test_thesis
import llm.structured as structured
import llm_client
from llm.contracts import (
    DCF_ASSUMPTIONS_SCHEMA,
    PRESSURE_TEST_SCHEMA,
    DcfAssumptionsPayload,
    DcfSegmentGrowth,
    PressureTestPayload,
    RiskFactorCategory,
    RiskFactorDiffPayload,
    TranscriptMetadataPayload,
)
from llm.structured import StructuredParseError, parse_json_payload


def test_transcript_metadata_uses_typed_unknown_not_provider_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake(_prompt: str, **kwargs: object) -> object:
        calls.append(kwargs)
        return TranscriptMetadataPayload(status="unknown")

    monkeypatch.setattr(llm_client, "call_llm_structured", fake)
    assert llm_client.identify_transcript_metadata("not a transcript") == "UNKNOWN"
    assert calls == [
        {
            "purpose": "transcript_metadata",
            "expect": "object",
            "schema": llm_client.TRANSCRIPT_METADATA_SCHEMA,
            "max_escalation_tier": 0,
        }
    ]


def test_transcript_metadata_provider_failure_does_not_become_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm_client, "call_llm_structured", failed)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        llm_client.identify_transcript_metadata("NVIDIA Q1 2026")


def test_transcript_metadata_schema_closes_status_and_period() -> None:
    with pytest.raises(ValidationError):
        TranscriptMetadataPayload.model_validate(
            {
                "status": "identified",
                "ticker": "NVDA",
                "quarter": "Q5",
                "fiscal_year": 2026,
            }
        )
    with pytest.raises(ValidationError):
        TranscriptMetadataPayload(status="unknown", ticker="NVDA")


def test_pressure_test_routes_through_structured_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = PressureTestPayload(
        strongest_counter="Consensus assumes demand that the corpus does not support.",
        contradicting_evidence=["FY2025 revenue growth slowed to 3%."],
        mgmt_credibility_check="Management missed its prior margin commitment.",
        thesis_assumptions=["Revenue growth reaccelerates above 10%."],
        conviction_rating="low",
        conviction_reasoning="The evidence directly contradicts the growth premise.",
    )

    def fake(_prompt: str, **kwargs: object) -> object:
        assert kwargs["purpose"] == "pressure_test_thesis"
        assert kwargs["ticker"] == "NU"
        assert kwargs["schema"] is PRESSURE_TEST_SCHEMA
        assert kwargs["max_escalation_tier"] == 0
        return expected

    monkeypatch.setattr(pressure_test_thesis, "call_llm_structured", fake)
    assert pressure_test_thesis._call_pressure_test("NU", "thesis", "corpus") == expected


def test_pressure_prompt_contains_schema_valid_strict_json_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = PressureTestPayload(
        strongest_counter="Counter.",
        contradicting_evidence=[],
        evidence_gap="The corpus lacks the needed evidence.",
        mgmt_credibility_check="No commitments were available.",
        thesis_assumptions=["The missing evidence exists."],
        conviction_rating="low",
        conviction_reasoning="The premise is untested.",
    )

    def fake(prompt: str, **_kwargs: object) -> object:
        rendered = prompt.split("Return strictly a JSON object", 1)[1]
        payload = parse_json_payload(rendered, expect="object")
        validated = PRESSURE_TEST_SCHEMA.validate_python(payload)
        assert validated.evidence_gap is None
        return expected

    monkeypatch.setattr(pressure_test_thesis, "call_llm_structured", fake)
    pressure_test_thesis._call_pressure_test("NU", "thesis", "corpus")


def test_transcript_metadata_repairs_once_without_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def malformed(_prompt: str, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return "not json"

    monkeypatch.setattr(structured, "call_llm", malformed)
    with pytest.raises(StructuredParseError):
        llm_client.identify_transcript_metadata("NVIDIA Q1 2026")
    assert calls == 2


def test_pressure_test_schema_rejects_open_conviction_label() -> None:
    with pytest.raises(ValidationError):
        PRESSURE_TEST_SCHEMA.validate_python(
            {
                "strongest_counter": "Specific counter.",
                "contradicting_evidence": ["Evidence."],
                "mgmt_credibility_check": "Mixed delivery.",
                "thesis_assumptions": ["Assumption."],
                "conviction_rating": "very high",
                "conviction_reasoning": "Reason.",
            }
        )


def test_pressure_test_empty_evidence_requires_an_explicit_gap() -> None:
    payload = {
        "strongest_counter": "The corpus does not contain the required demand evidence.",
        "contradicting_evidence": [],
        "mgmt_credibility_check": "No relevant commitments were available.",
        "thesis_assumptions": ["Demand evidence exists outside the corpus."],
        "conviction_rating": "low",
        "conviction_reasoning": "The key premise is currently untestable.",
    }
    with pytest.raises(ValidationError):
        PRESSURE_TEST_SCHEMA.validate_python(payload)
    payload["evidence_gap"] = "No historical demand series was present in the corpus."
    validated = PRESSURE_TEST_SCHEMA.validate_python(payload)
    assert validated.contradicting_evidence == []


def test_risk_classification_requires_closed_categories_and_every_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(_prompt: str, **kwargs: object) -> object:
        schema = cast("TypeAdapter[dict[str, RiskFactorCategory]]", kwargs["schema"])
        with pytest.raises(ValidationError):
            schema.validate_python({"0": "cyber-ish"})
        guard = cast("Callable[[object], tuple[bool, str]]", kwargs["domain_guardrail"])
        assert guard({"0": "technology"}) == (False, "expected ids ['0', '1']; got ['0']")
        assert guard({"0": "technology", "1": "competition"}) == (True, "")
        assert kwargs["max_escalation_tier"] == 0
        return {"0": "technology", "1": "competition"}

    monkeypatch.setattr(risk_factors, "call_llm_structured", fake)
    result = risk_factors._llm_classify_risks(
        ticker="TEST",
        fiscal_year=2025,
        risks=[("Cyber", "breach"), ("Rivals", "competition")],
    )
    assert result == {0: "technology", 1: "competition"}


def test_risk_diff_no_change_is_enum_driven_not_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_change(_prompt: str, **kwargs: object) -> object:
        assert kwargs["purpose"] == "risk_factor_diff"
        assert kwargs["max_escalation_tier"] == 0
        return RiskFactorDiffPayload(outcome="no_material_change", summary=None)

    monkeypatch.setattr(risk_factors, "call_llm_structured", no_change)
    assert risk_factors._llm_diff_one(ticker="TEST", heading="Risk", old="old", new="new") is None

    def material(_prompt: str, **_kwargs: object) -> object:
        return RiskFactorDiffPayload(
            outcome="material_change",
            summary="The company says there was no material rewording in one clause, "
            "but adds a new quantified exposure.",
        )

    monkeypatch.setattr(risk_factors, "call_llm_structured", material)
    assert "new quantified exposure" in cast(
        "str", risk_factors._llm_diff_one(ticker="TEST", heading="Risk", old="old", new="new")
    )


def test_risk_diff_provider_failure_does_not_become_no_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(risk_factors, "call_llm_structured", failed)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        risk_factors._llm_diff_one(ticker="TEST", heading="Risk", old="old", new="new")


def _write_dcf_fixture(repo_root: Path) -> None:
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    income: list[dict[str, object]] = []
    cash_flow: list[dict[str, object]] = []
    for fiscal_year in (2024, 2025):
        for quarter in ("Q1", "Q2", "Q3", "Q4"):
            income.append(
                {
                    "fiscalYear": fiscal_year,
                    "period": quarter,
                    "revenue": 1_000_000_000,
                    "operatingIncome": 200_000_000,
                    "netIncome": 150_000_000,
                    "reportedCurrency": "USD",
                }
            )
            cash_flow.append(
                {
                    "fiscalYear": fiscal_year,
                    "period": quarter,
                    "capitalExpenditure": -100_000_000,
                    "depreciationAndAmortization": 80_000_000,
                }
            )
    (fmp / "TEST_income_statement_quarterly.json").write_text(json.dumps(income), encoding="utf-8")
    (fmp / "TEST_cash_flow_quarterly.json").write_text(json.dumps(cash_flow), encoding="utf-8")
    (fmp / "TEST_product_segments_quarterly.json").write_text("[]", encoding="utf-8")
    (fmp / "TEST_analyst_estimates_annual.json").write_text("[]", encoding="utf-8")
    (fmp / "TEST_profile.json").write_text(
        json.dumps([{"companyName": "Test Co", "sector": "Tech", "industry": "Software"}]),
        encoding="utf-8",
    )


def _load_dcf_module(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    _write_dcf_fixture(repo_root)
    monkeypatch.setenv("DCF_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("DCF_TICKER", "TEST")
    path = Path(__file__).resolve().parents[1] / "execution" / "dcf_opus_assumptions.py"
    spec = importlib.util.spec_from_file_location("dcf_opus_assumptions_audit_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_dcf_payload() -> DcfAssumptionsPayload:
    return DcfAssumptionsPayload(
        dcf_applicable=True,
        business_model="operating",
        valuation_model="fcff_dcf",
        valuation_model_suggestion="",
        segments={"Total company": DcfSegmentGrowth(near_term_growth=0.10, terminal_growth=0.03)},
        near_term_op_margin=0.20,
        terminal_op_margin=0.25,
        tax_rate=0.21,
        capex_pct_revenue_2026=0.05,
        terminal_capex_da=1.05,
        terminal_method="Exit multiple",
        exit_basis="EV/EBITDA",
        exit_multiple=15.0,
        terminal_growth_g=0.03,
        narrative="Margins expand as growth fades toward maturity.",
        reasoning="The path anchors to actual margins and a mature multiple.",
    )


def test_dcf_assumptions_use_structured_schema_and_exact_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_dcf_module(tmp_path, monkeypatch)
    expected = _valid_dcf_payload()

    def fake(_prompt: str, **kwargs: object) -> object:
        assert kwargs["purpose"] == "dcf_assumptions"
        assert kwargs["scope"] == "dcf_assumptions_redesign"
        assert kwargs["schema"] is DCF_ASSUMPTIONS_SCHEMA
        assert kwargs["max_escalation_tier"] == 0
        rendered = _prompt.split("Return ONLY a JSON object", 1)[1]
        example = parse_json_payload(rendered, expect="object")
        DCF_ASSUMPTIONS_SCHEMA.validate_python(example)
        guard = cast("Callable[[object], tuple[bool, str]]", kwargs["domain_guardrail"])
        assert guard(expected) == (True, "")
        missing = expected.model_copy(update={"segments": {}})
        assert guard(missing)[0] is False
        return expected

    monkeypatch.setattr(module, "call_llm_structured", fake)
    assert module._call_dcf_assumptions() == expected


def test_dcf_assumptions_cannot_rewrite_owner_debt_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_dcf_module(tmp_path, monkeypatch)
    cache = tmp_path / "data" / "dcf_assumptions" / "TEST.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "redesign": {"dcf_debt_scope": "debt_and_lease_obligations"},
                "narrative": "existing",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_call_dcf_assumptions", _valid_dcf_payload)

    assert module.main() == 0

    updated = json.loads(cache.read_text(encoding="utf-8"))
    assert updated["redesign"]["dcf_debt_scope"] == "debt_and_lease_obligations"


def test_empty_pressure_evidence_renders_gap_without_empty_section(tmp_path: Path) -> None:
    diligence = tmp_path / "micro_thesis" / "diligence" / "NU.md"
    diligence.parent.mkdir(parents=True)
    diligence.write_text("# NU\n", encoding="utf-8")
    payload = PressureTestPayload(
        strongest_counter="The premise is not supported by the available corpus.",
        contradicting_evidence=[],
        evidence_gap="No historical cohort data was available.",
        mgmt_credibility_check="No relevant commitments were available.",
        thesis_assumptions=["Historical cohort data supports retention."],
        conviction_rating="low",
        conviction_reasoning="The central premise cannot yet be tested.",
    )

    pressure_test_thesis._append_to_diligence(tmp_path, "NU", "thesis", payload)
    rendered = diligence.read_text(encoding="utf-8")
    assert "**Contradicting evidence:**" not in rendered
    assert "**Evidence gap:** No historical cohort data was available." in rendered


def test_pressure_evidence_and_gap_both_render(tmp_path: Path) -> None:
    diligence = tmp_path / "micro_thesis" / "diligence" / "NU.md"
    diligence.parent.mkdir(parents=True)
    diligence.write_text("# NU\n", encoding="utf-8")
    payload = PressureTestPayload(
        strongest_counter="The premise is weaker than expected.",
        contradicting_evidence=["FY2025 cohort retention fell five points."],
        evidence_gap="No competitor cohort data was available.",
        mgmt_credibility_check="Management did not quantify the retention gap.",
        thesis_assumptions=["Retention stabilizes next year."],
        conviction_rating="medium",
        conviction_reasoning="Observed weakness is material but the comparison set is incomplete.",
    )

    pressure_test_thesis._append_to_diligence(tmp_path, "NU", "thesis", payload)
    rendered = diligence.read_text(encoding="utf-8")
    assert "**Contradicting evidence:**" in rendered
    assert "- FY2025 cohort retention fell five points." in rendered
    assert "**Evidence gap:** No competitor cohort data was available." in rendered


def test_dcf_schema_rejects_inconsistent_archetype_and_growth() -> None:
    payload = _valid_dcf_payload().model_dump()
    payload["dcf_applicable"] = False
    with pytest.raises(ValidationError):
        DCF_ASSUMPTIONS_SCHEMA.validate_python(payload)

    payload = _valid_dcf_payload().model_dump()
    payload["segments"]["Total company"] = {
        "near_term_growth": 0.02,
        "terminal_growth": 0.04,
    }
    with pytest.raises(ValidationError):
        DCF_ASSUMPTIONS_SCHEMA.validate_python(payload)


def test_dcf_schema_accepts_representative_bank_archetype() -> None:
    payload = _valid_dcf_payload().model_dump()
    payload.update(
        {
            "dcf_applicable": False,
            "business_model": "bank",
            "valuation_model": "bank_excess_return",
            "valuation_model_suggestion": "",
            "terminal_method": "Perpetuity",
            "exit_basis": "EV/EBIT",
        }
    )
    validated = DCF_ASSUMPTIONS_SCHEMA.validate_python(payload)
    assert validated.business_model == "bank"
    assert validated.valuation_model == "bank_excess_return"


def test_dcf_schema_accepts_new_archetype_with_suggestion() -> None:
    payload = _valid_dcf_payload().model_dump()
    payload.update(
        {
            "dcf_applicable": False,
            "business_model": "insurer",
            "valuation_model": "new",
            "valuation_model_suggestion": "Embedded value - discount distributable earnings.",
        }
    )
    assert DCF_ASSUMPTIONS_SCHEMA.validate_python(payload).valuation_model == "new"
