"""Bounded diagnostics for native dirty-artifact producers; never progress proof."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

NativePurpose = Literal["bear_case", "company_description"]
NativeProducerReason = Literal[
    "generated",
    "thesis_missing",
    "budget_skipped",
    "llm_deferred",
    "llm_operational_failure",
    "llm_response_invalid",
    "source_missing",
    "producer_exception",
    "unsupported_instrument",
]
NativeProducerStatus = Literal["produced", "blocked", "failed", "unsupported"]
_PREFIX = "ES_NATIVE_PRODUCER_OUTCOME "
_MAX_BYTES = 512
_STATUS_BY_REASON: dict[NativeProducerReason, NativeProducerStatus] = {
    "generated": "produced",
    "thesis_missing": "blocked",
    "budget_skipped": "blocked",
    "llm_deferred": "blocked",
    "llm_operational_failure": "failed",
    "llm_response_invalid": "failed",
    "source_missing": "blocked",
    "producer_exception": "failed",
    "unsupported_instrument": "unsupported",
}


class NativeProducerOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["native-producer-v1"]
    purpose: NativePurpose
    status: NativeProducerStatus
    reason: NativeProducerReason

    @model_validator(mode="after")
    def _consistent_status(self) -> NativeProducerOutcome:
        if self.status != _STATUS_BY_REASON[self.reason]:
            raise ValueError("producer outcome status/reason mismatch")
        if (
            self.purpose == "company_description"
            and self.reason not in {"generated", "source_missing", "producer_exception"}
        ) or (self.purpose == "bear_case" and self.reason == "source_missing"):
            raise ValueError("producer outcome purpose/reason mismatch")
        return self


class NativeProducerUnavailableError(RuntimeError):
    def __init__(self, reason: NativeProducerReason) -> None:
        self.reason: NativeProducerReason = reason
        super().__init__(reason)


class NativeProducerProtocolError(ValueError):
    pass


def emit_native_producer_outcome(
    purpose: NativePurpose, reason: NativeProducerReason
) -> NativeProducerOutcome:
    outcome = NativeProducerOutcome(
        schema_version="native-producer-v1",
        purpose=purpose,
        status=_STATUS_BY_REASON[reason],
        reason=reason,
    )
    line = _PREFIX + outcome.model_dump_json()
    if len(line.encode("utf-8")) > _MAX_BYTES:
        raise NativeProducerProtocolError("oversized")
    print(line)
    return outcome


def read_native_producer_outcome(stdout: str, purpose: NativePurpose) -> NativeProducerOutcome:
    lines = [line for line in stdout.splitlines() if line.startswith(_PREFIX)]
    if len(lines) != 1:
        raise NativeProducerProtocolError("missing" if not lines else "duplicate")
    line = lines[0]
    if len(line.encode("utf-8")) > _MAX_BYTES:
        raise NativeProducerProtocolError("oversized")
    try:
        outcome = NativeProducerOutcome.model_validate_json(line[len(_PREFIX) :])
    except ValidationError:
        raise NativeProducerProtocolError("invalid") from None
    if outcome.purpose != purpose:
        raise NativeProducerProtocolError("wrong_purpose")
    return outcome
