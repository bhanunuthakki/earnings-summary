"""Provider-neutral request and response envelopes (BHA-56).

Enforces application-owned semantics, normalized telemetry (cost, latency, tokens),
and closed failure codes while isolating provider-specific details inside adapters.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LLMFailureCode(StrEnum):
    """Closed failure codes for provider-neutral degradation routing."""

    UNAVAILABLE = "UNAVAILABLE"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    SCHEMA_VALIDATION_ERROR = "SCHEMA_VALIDATION_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    AUTH_ERROR = "AUTH_ERROR"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    TIMEOUT = "TIMEOUT"
    UNKNOWN_PURPOSE = "UNKNOWN_PURPOSE"


class LLMRequestEnvelope(BaseModel):
    """Provider-neutral envelope for dispatching an LLM operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    purpose: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1)
    prompt_version: str = Field(default="v1", max_length=50)
    schema_version: str = Field(default="v1", max_length=50)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    capabilities_required: tuple[str, ...] = Field(default=())
    trace_id: str | None = Field(default=None, max_length=100)
    system_prompt: str | None = Field(default=None)


class LLMResponseEnvelope(BaseModel):
    """Provider-neutral envelope returned by all adapter execution legs."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    purpose: str = Field(min_length=1, max_length=100)
    content: str = Field(default="")
    parsed_payload: dict[str, Any] | list[Any] | None = Field(default=None)
    model: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=50)
    cost_usd: float = Field(default=0.0, ge=0.0)
    latency_ms: float = Field(default=0.0, ge=0.0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    timestamp_utc: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    failure_code: LLMFailureCode | None = Field(default=None)
    error_message: str | None = Field(default=None)

    @property
    def is_success(self) -> bool:
        """True if execution succeeded without a failure code."""
        return self.failure_code is None and bool(self.content or self.parsed_payload is not None)


__all__ = [
    "LLMFailureCode",
    "LLMRequestEnvelope",
    "LLMResponseEnvelope",
]
