"""Provider-free contract canaries for the wave-2 structured LLM boundaries."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

import ask.router as ask_router
import capture.decision_draft as capture_decision_draft
import dcf.scenario_prior as dcf_scenario_prior
import decision_conditions as decision_conditions
import filings.boilerplate_triage as filings_boilerplate_triage
import llm_client as llm_client
import risk_factors
from ask.packs import PACK_KEYS
from risk_factors import TAXONOMY

_PACK_ROUTE_ADAPTER = getattr(ask_router, "_PACK_ROUTE_ADAPTER")
_DECISION_DRAFT_ADAPTER = getattr(capture_decision_draft, "_DECISION_DRAFT_ADAPTER")
_SCENARIO_PRIOR_ADAPTER = getattr(dcf_scenario_prior, "_SCENARIO_PRIOR_ADAPTER")
_DECISION_CONDITIONS_ADAPTER = getattr(decision_conditions, "_DECISION_CONDITIONS_ADAPTER")
_TRIAGE_ADAPTER = getattr(filings_boilerplate_triage, "_TRIAGE_ADAPTER")
_INTAKE_CLASSIFICATION_ADAPTER = getattr(llm_client, "_INTAKE_CLASSIFICATION_ADAPTER")
_FACTOR_LOADINGS_ADAPTER = getattr(risk_factors, "_FACTOR_LOADINGS_ADAPTER")


@pytest.mark.parametrize(
    ("adapter", "valid", "invalid"),
    [
        (
            _PACK_ROUTE_ADAPTER,
            {"packs": [PACK_KEYS[0]]},
            {"packs": "not-an-array"},
        ),
        (
            _DECISION_DRAFT_ADAPTER,
            {"intent": "musing", "parse_confidence": 0.4},
            {"intent": "musing", "parse_confidence": 1.4},
        ),
        (
            _SCENARIO_PRIOR_ADAPTER,
            {"bull": 0.25, "base": 0.5, "bear": 0.25, "rationale": "Balanced."},
            {"bull": -1, "base": 0.5, "bear": 0.25, "rationale": "Invalid."},
        ),
        (
            _DECISION_CONDITIONS_ADAPTER,
            [
                {
                    "metric": "ARR",
                    "metric_source": "kpi",
                    "op": "lt",
                    "threshold": 100.0,
                    "unit": "millions",
                    "for_periods": 2,
                    "not_before": None,
                    "note": "ARR below $100M",
                }
            ],
            {"metric": "ARR", "op": "sideways"},
        ),
        (
            _TRIAGE_ADAPTER,
            {
                "7": {
                    "verdict": "substantive",
                    "confidence": 0.8,
                    "rationale": "Names a new customer concentration.",
                }
            },
            {
                "7": {
                    "verdict": "maybe",
                    "confidence": 0.8,
                    "rationale": "Not a closed verdict.",
                }
            },
        ),
        (
            _INTAKE_CLASSIFICATION_ADAPTER,
            {
                "ticker": "META",
                "period_end": "2026-06-30",
                "doc_type": "ir_presentation",
                "confidence": 0.9,
                "reasoning": "Quarterly investor deck.",
            },
            {
                "ticker": "META",
                "period_end": "June 2026",
                "doc_type": "slides",
                "confidence": 1.2,
                "reasoning": "Invalid contract.",
            },
        ),
        (
            _FACTOR_LOADINGS_ADAPTER,
            [{"factor": TAXONOMY[0], "loading": 0.7, "rationale": "Grounded exposure."}],
            [{"factor": "invented factor", "loading": 0.7, "rationale": "Ungrounded."}],
        ),
    ],
)
def test_wave2_schema_accepts_valid_and_rejects_invalid(
    adapter: TypeAdapter[object], valid: object, invalid: object
) -> None:
    adapter.validate_python(valid)
    with pytest.raises(ValidationError):
        adapter.validate_python(invalid)
