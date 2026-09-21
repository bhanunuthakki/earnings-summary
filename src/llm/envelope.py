"""Provider-neutral, versioned contracts for incrementally migrated LLM consumers."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from llm.resolver import validate_purpose

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class LLMFailureCode(StrEnum):
    """Closed failure codes; a code never independently authorizes fallback."""

    UNAVAILABLE = "UNAVAILABLE"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    SCHEMA_VALIDATION_ERROR = "SCHEMA_VALIDATION_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    AUTH_ERROR = "AUTH_ERROR"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    TIMEOUT = "TIMEOUT"
    UNKNOWN_PURPOSE = "UNKNOWN_PURPOSE"


class LLMCapability(StrEnum):
    STRUCTURED_OUTPUT = "structured_output"
    WEB = "web"
    VISION = "vision"
    REALTIME = "realtime"


class PurposeEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: str = Field(min_length=1, max_length=100)

    @field_validator("purpose")
    @classmethod
    def registered_purpose(cls, value: str) -> str:
        return validate_purpose(value)


class LLMRequestEnvelope(PurposeEnvelope):
    """Application identity and required capabilities, independent of model choice.

    Prompt bytes are deliberately not stripped. Budget enforcement stays with the
    canonical purpose policy in llm.cli, never a second limit in this envelope.
    """

    prompt: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1, max_length=100)
    schema_version: str = Field(min_length=1, max_length=100)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    capabilities_required: tuple[LLMCapability, ...] = ()
    source_evidence_sha256: tuple[Sha256, ...] = ()
    budget_policy: Literal["canonical_purpose_policy"] = "canonical_purpose_policy"
    trace_id: str | None = Field(default=None, max_length=100)
    system_prompt: str | None = None

    @field_validator("prompt_version", "schema_version")
    @classmethod
    def nonblank_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("contract versions must be nonblank")
        return value


class LLMCallAttempt(BaseModel):
    """One actual canonical ledger record; unknown measurements remain null."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    provider: str | None = None
    transport: str | None = None
    prompt_sha256: Sha256
    response_sha256: Sha256 | None = None
    prompt_version: str | None = None
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    retry_count: int | None = Field(default=None, ge=0)
    outcome: str | None = None
    failure_class: str | None = None
    fallback_from_provider: str | None = None
    fallback_from_transport: str | None = None


class LLMResponseAttestation(PurposeEnvelope):
    """Compact receipt metadata; exact exchange bytes are retained by the consumer."""

    model: str | None = Field(default=None, min_length=1, max_length=100)
    provider: str | None = Field(default=None, min_length=1, max_length=50)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    retry_count: int | None = Field(default=None, ge=0)
    repair_count: int = Field(default=0, ge=0, le=1)
    request_sha256: Sha256 | None = None
    response_sha256: Sha256 | None = None
    prompt_sha256: Sha256 | None = None
    effective_prompt_verified: bool = False
    prompt_version: str | None = None
    schema_version: str | None = None
    source_evidence_sha256: tuple[Sha256, ...] = ()
    budget_policy: Literal["canonical_purpose_policy"] = "canonical_purpose_policy"
    attempts: tuple[LLMCallAttempt, ...] = ()
    timestamp_utc: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    failure_code: LLMFailureCode | None = None
    error_message: str | None = None


class LLMResponseEnvelope(LLMResponseAttestation):
    """Neutral response bound to the request and its actual transport attempts."""

    content: str = ""
    parsed_payload: JsonValue = None

    def attestation(self) -> LLMResponseAttestation:
        return LLMResponseAttestation.model_validate(
            self.model_dump(exclude={"content", "parsed_payload"})
        )

    @property
    def is_success(self) -> bool:
        return self.failure_code is None and bool(self.content or self.parsed_payload is not None)
