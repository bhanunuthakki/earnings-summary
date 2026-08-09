# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportMissingParameterType=false
"""Provider-free contract canaries for the wave-2 structured LLM boundaries."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from advisor.position_review import _VERDICT_ADAPTER
from ask.packs import PACK_KEYS
from ask.router import _PACK_ROUTE_ADAPTER
from capture.decision_draft import _DECISION_DRAFT_ADAPTER
from dcf.scenario_prior import _SCENARIO_PRIOR_ADAPTER
from decision_conditions import _DECISION_CONDITIONS_ADAPTER
from filings.boilerplate_triage import _TRIAGE_ADAPTER
from llm_client import _INTAKE_CLASSIFICATION_ADAPTER
from onmymind.reply import _REPLY_ADAPTER
from risk_factors import _FACTOR_LOADINGS_ADAPTER, TAXONOMY


@pytest.mark.parametrize(
    ("adapter", "valid", "invalid"),
    [
        (
            _REPLY_ADAPTER,
            {"intent": "question", "reason": "asks for detail"},
            {"intent": "delete_everything", "reason": "unsafe invented action"},
        ),
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
        (
            _VERDICT_ADAPTER,
            {
                "verdict": "hold",
                "size": "No change",
                "reason": "Valuation and thesis remain aligned.",
                "confidence": "medium",
                "behavioral_check": "Do not react to price alone.",
                "suggested_expression": "do-nothing",
            },
            {
                "verdict": "all-in",
                "size": "Everything",
                "reason": "Invalid stance.",
                "confidence": "certain",
                "behavioral_check": "None",
                "suggested_expression": "leverage",
            },
        ),
    ],
)
def test_wave2_schema_accepts_valid_and_rejects_invalid(adapter, valid, invalid) -> None:
    adapter.validate_python(valid)
    with pytest.raises(ValidationError):
        adapter.validate_python(invalid)
