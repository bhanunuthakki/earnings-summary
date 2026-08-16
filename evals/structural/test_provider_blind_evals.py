"""Provider-blind structural golden schema evaluations and adapter contract tests (BHA-57).

Ensures LLM response parsing, structured extraction schemas, and error degradations
remain strictly provider-neutral across Claude, Gemini, and OpenRouter backends.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from llm.envelope import (
    LLMFailureCode,
    LLMRequestEnvelope,
    LLMResponseEnvelope,
)


class MaterialNewsGoldenPayload(BaseModel):
    """Structural golden schema for material news classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_material: bool
    confidence: float = Field(ge=0.0, le=1.0)
    category: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    thesis_impact: str


class DecisionConditionsGoldenPayload(BaseModel):
    """Structural golden schema for decision condition extraction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    action: str
    hurdles: list[str] = Field(default_factory=list)
    invalidation_triggers: list[str] = Field(default_factory=list)


def parse_raw_provider_output(
    raw_provider_payload: dict[str, Any],
    provider: str,
    purpose: str,
) -> LLMResponseEnvelope:
    """Provider-blind ingestion helper that normalizes diverse vendor responses."""
    if provider == "claude_cli":
        # Simulates Claude CLI --json output envelope
        content = raw_provider_payload.get("result", "")
        parsed = raw_provider_payload.get("structured_json")
        tokens_in = raw_provider_payload.get("usage", {}).get("input_tokens", 100)
        tokens_out = raw_provider_payload.get("usage", {}).get("output_tokens", 50)
        cost = (tokens_in * 0.000003) + (tokens_out * 0.000015)
        return LLMResponseEnvelope(
            purpose=purpose,
            content=content,
            parsed_payload=parsed,
            model=raw_provider_payload.get("model", "claude-3-7-sonnet"),
            provider=provider,
            cost_usd=cost,
            latency_ms=raw_provider_payload.get("duration_ms", 350.0),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )
    if provider == "gemini_api":
        # Simulates Gemini generateContent response
        candidates = raw_provider_payload.get("candidates", [])
        text = candidates[0].get("text", "") if candidates else ""
        meta = raw_provider_payload.get("usageMetadata", {})
        tokens_in = meta.get("promptTokenCount", 80)
        tokens_out = meta.get("candidatesTokenCount", 45)
        cost = (tokens_in * 0.000001) + (tokens_out * 0.000004)
        return LLMResponseEnvelope(
            purpose=purpose,
            content=text,
            parsed_payload=raw_provider_payload.get("parsed_json"),
            model=raw_provider_payload.get("modelVersion", "gemini-2.5-flash"),
            provider=provider,
            cost_usd=cost,
            latency_ms=raw_provider_payload.get("latency_ms", 220.0),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )
    if provider == "openrouter_chat":
        # Simulates OpenAI/OpenRouter chat completion
        choices = raw_provider_payload.get("choices", [])
        msg: dict[str, Any] = (
            choices[0].get("message", {}) if (choices and isinstance(choices[0], dict)) else {}
        )
        usage = raw_provider_payload.get("usage", {})
        return LLMResponseEnvelope(
            purpose=purpose,
            content=str(msg.get("content", "")),
            parsed_payload=raw_provider_payload.get("parsed_json"),
            model=raw_provider_payload.get("model", "meta-llama/llama-3.3-70b-instruct"),
            provider=provider,
            cost_usd=float(usage.get("total_cost", 0.0005)),
            latency_ms=raw_provider_payload.get("latency_ms", 290.0),
            input_tokens=usage.get("prompt_tokens", 90),
            output_tokens=usage.get("completion_tokens", 40),
        )

    return LLMResponseEnvelope(
        purpose=purpose,
        model="unknown",
        provider=provider,
        failure_code=LLMFailureCode.UNAVAILABLE,
        error_message=f"Unsupported provider adapter: {provider}",
    )


def test_provider_blind_news_classification_across_vendors() -> None:
    # 1. Claude CLI payload
    claude_raw = {
        "model": "claude-3-7-sonnet-20250219",
        "result": "The news indicates a major product delay affecting FY26 guidance.",
        "structured_json": {
            "is_material": True,
            "confidence": 0.95,
            "category": "product_delay",
            "summary": "Product launch pushed back 6 months.",
            "thesis_impact": "Negative for FY26 revenue trajectory.",
        },
        "usage": {"input_tokens": 120, "output_tokens": 60},
        "duration_ms": 410.0,
    }

    # 2. Gemini API payload
    gemini_raw = {
        "modelVersion": "gemini-2.5-flash-001",
        "candidates": [
            {"text": "The news indicates a major product delay affecting FY26 guidance."}
        ],
        "parsed_json": {
            "is_material": True,
            "confidence": 0.95,
            "category": "product_delay",
            "summary": "Product launch pushed back 6 months.",
            "thesis_impact": "Negative for FY26 revenue trajectory.",
        },
        "usageMetadata": {"promptTokenCount": 115, "candidatesTokenCount": 55},
        "latency_ms": 230.0,
    }

    resp_claude = parse_raw_provider_output(
        claude_raw, "claude_cli", "material_news_classification"
    )
    resp_gemini = parse_raw_provider_output(
        gemini_raw, "gemini_api", "material_news_classification"
    )

    assert resp_claude.is_success
    assert resp_gemini.is_success

    # Both payloads validate into the exact same application-level domain model
    assert resp_claude.parsed_payload is not None
    assert resp_gemini.parsed_payload is not None

    model_claude = MaterialNewsGoldenPayload.model_validate(resp_claude.parsed_payload)
    model_gemini = MaterialNewsGoldenPayload.model_validate(resp_gemini.parsed_payload)

    assert model_claude == model_gemini
    assert model_claude.is_material is True
    assert model_claude.category == "product_delay"


def test_provider_blind_decision_conditions_extraction() -> None:
    openrouter_raw = {
        "model": "meta-llama/llama-3.3-70b-instruct",
        "choices": [{"message": {"content": "Conditions extracted."}}],
        "parsed_json": {
            "ticker": "WIX",
            "action": "TRIM",
            "hurdles": ["Base44 adoption > 25%", "Operating margin >= 18%"],
            "invalidation_triggers": ["Growth slows below 10% YoY"],
        },
        "usage": {"prompt_tokens": 200, "completion_tokens": 80, "total_cost": 0.0008},
        "latency_ms": 310.0,
    }

    resp = parse_raw_provider_output(
        openrouter_raw, "openrouter_chat", "decision_conditions_extract"
    )
    assert resp.is_success
    assert resp.parsed_payload is not None

    extracted = DecisionConditionsGoldenPayload.model_validate(resp.parsed_payload)
    assert extracted.ticker == "WIX"
    assert extracted.action == "TRIM"
    assert len(extracted.hurdles) == 2


def test_provider_adapter_failure_closed_handling() -> None:
    unknown_raw = {"some": "data"}
    resp = parse_raw_provider_output(
        unknown_raw, "unsupported_provider", "material_news_classification"
    )

    assert not resp.is_success
    assert resp.failure_code == LLMFailureCode.UNAVAILABLE
    assert "Unsupported provider adapter" in (resp.error_message or "")


def test_request_envelope_immutability() -> None:
    req = LLMRequestEnvelope(
        purpose="eval_purpose",
        prompt="Analyze earnings transcript",
        temperature=0.2,
        capabilities_required=("fast_inference",),
    )
    assert req.purpose == "eval_purpose"
    assert req.prompt_version == "v1"

    with pytest.raises(ValidationError):
        req.purpose = "mutation_attempt"  # type: ignore[misc]
