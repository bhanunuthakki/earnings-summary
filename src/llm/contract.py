"""Neutral contract over the existing governed structured-call path.

This facade does not select providers, retry transports, or reinterpret budgets.
The canonical CLI and structured decoder retain those responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import JsonValue, TypeAdapter, ValidationError

from llm.cli import LLMBudgetExceeded, LLMSetupError
from llm.envelope import (
    LLMCallAttempt,
    LLMCapability,
    LLMFailureCode,
    LLMRequestEnvelope,
    LLMResponseEnvelope,
)
from llm.ledger import capture_call_records
from llm.prompt_registry import template_meta
from llm.structured import (
    StructuredCallResult,
    StructuredParseError,
    call_llm_structured_with_raw,
)
from log_redact import redact

T = TypeVar("T")
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def schema_version(schema: TypeAdapter[T]) -> str:
    """Content identity of the application schema, independent of model choice."""
    return _sha(json.dumps(schema.json_schema(), sort_keys=True, separators=(",", ":")))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ContractStructuredResult(StructuredCallResult[T]):
    contract: LLMResponseEnvelope


@dataclass(frozen=True, slots=True)
class ContractExecution(Generic[T]):
    """Inspect a normalized failure without weakening the caller's exception gate."""

    response: LLMResponseEnvelope
    exchange: StructuredCallResult[T] | None
    error: Exception | None

    def require_exchange(self) -> ContractStructuredResult[T]:
        if self.error is not None:
            raise self.error
        if self.exchange is None or not self.response.is_success:
            raise RuntimeError("LLM contract has no successful exchange")
        return ContractStructuredResult(
            value=self.exchange.value,
            raw_response=self.exchange.raw_response,
            prompt=self.exchange.prompt,
            contract=self.response,
        )


def _failure_code(error: Exception) -> LLMFailureCode:
    if isinstance(error, LLMBudgetExceeded):
        return LLMFailureCode.BUDGET_EXCEEDED
    if isinstance(error, LLMSetupError):
        return LLMFailureCode.CONFIGURATION_ERROR
    if isinstance(error, StructuredParseError):
        return (
            LLMFailureCode.SCHEMA_VALIDATION_ERROR
            if isinstance(error.__cause__, ValidationError)
            else LLMFailureCode.MALFORMED_OUTPUT
        )
    if isinstance(error, PermissionError):
        return LLMFailureCode.AUTH_ERROR
    if isinstance(error, TimeoutError):
        return LLMFailureCode.TIMEOUT
    if isinstance(error, RuntimeError):
        return LLMFailureCode.UNAVAILABLE
    return LLMFailureCode.CONFIGURATION_ERROR


def _sum_known(values: tuple[int | None, ...]) -> int | None:
    return (
        sum(value for value in values if value is not None)
        if values and None not in values
        else None
    )


def call_llm_structured_enveloped(
    request: LLMRequestEnvelope,
    *,
    prompt: str,
    schema: TypeAdapter[T],
    repair_prompt: Callable[[str], str],
    scope: str | None = None,
    db_path: Path | str | None = None,
) -> ContractExecution[T]:
    """Execute a text-only structured contract in the supported meta_eval scope.

    The current sole consumer is the README evaluator. Production scopes can
    apply prompt A/B overrides after validation and are not supported here.
    This boundary does not disable A/B experiments in the canonical CLI.
    """
    if scope != "meta_eval":
        raise ValueError("this facade supports only the explicit meta_eval scope")
    if request.capabilities_required != (LLMCapability.STRUCTURED_OUTPUT,):
        raise ValueError("this facade supports only explicit structured_output capability")
    if (
        request.temperature is not None
        or request.max_tokens is not None
        or request.system_prompt is not None
    ):
        raise ValueError("this facade does not support sampling or system-prompt overrides")
    if request.prompt != prompt or request.schema_version != schema_version(schema):
        raise ValueError("request prompt or schema identity does not match execution")
    template_id, version, _vars = template_meta(prompt)
    if template_id is None or version != request.prompt_version:
        raise ValueError("request must preserve the registered prompt version")

    repair_count = 0
    admitted_prompt_hashes = {_sha(prompt)}

    def repair(error: str) -> str:
        nonlocal repair_count
        repaired = repair_prompt(error)
        repaired_id, repaired_version, _vars = template_meta(repaired)
        if repaired_id != template_id or repaired_version != version:
            raise ValueError("repair must preserve the registered prompt identity")
        repair_count += 1
        admitted_prompt_hashes.add(_sha(repaired))
        return repaired

    started = time.monotonic()
    exchange: StructuredCallResult[T] | None = None
    failure: Exception | None = None
    with capture_call_records() as captured:
        try:
            exchange = call_llm_structured_with_raw(
                prompt,
                purpose=request.purpose,
                schema=schema,
                repair_prompt=repair,
                scope=scope,
                run_id=request.trace_id,
                db_path=db_path,
            )
        except Exception as error:
            failure = error
        records = tuple(
            record
            for record in captured()
            if record.purpose == request.purpose and record.run_id == request.trace_id
        )

    if failure is None and any(
        record.prompt_sha256 not in admitted_prompt_hashes
        or (record.template_version is not None and record.template_version != version)
        or (record.template_id is not None and record.template_id != template_id)
        for record in records
    ):
        failure = ValueError("observed provider prompt identity differs from the contract")

    attempts = tuple(
        LLMCallAttempt(
            model=record.model,
            provider=record.provider,
            transport=record.transport,
            prompt_sha256=record.prompt_sha256,
            response_sha256=record.response_sha256,
            prompt_version=record.template_version,
            cost_usd=record.cost_estimate_usd,
            latency_ms=record.elapsed_ms,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            retry_count=record.retry_count,
            outcome=record.outcome,
            failure_class=record.failure_class,
            fallback_from_provider=record.fallback_from_provider,
            fallback_from_transport=record.fallback_from_transport,
        )
        for record in records
    )
    # Attribute only an actual successful record matching the exact final bytes.
    producing = next(
        (
            record
            for record in reversed(records)
            if exchange is not None
            and record.error is None
            and failure is None
            and record.response_sha256 == _sha(exchange.raw_response)
            and record.prompt_sha256 == _sha(exchange.prompt)
            and record.template_version == version
            and record.template_id == template_id
        ),
        None,
    )
    costs = tuple(attempt.cost_usd for attempt in attempts)
    response = LLMResponseEnvelope(
        purpose=request.purpose,
        content=exchange.raw_response if exchange else "",
        parsed_payload=_JSON.validate_python(schema.dump_python(exchange.value, mode="json"))
        if exchange
        else None,
        model=producing.model if producing else None,
        provider=producing.provider if producing else None,
        cost_usd=sum(value for value in costs if value is not None)
        if costs and None not in costs
        else None,
        latency_ms=(time.monotonic() - started) * 1000,
        input_tokens=_sum_known(tuple(attempt.input_tokens for attempt in attempts)),
        output_tokens=_sum_known(tuple(attempt.output_tokens for attempt in attempts)),
        retry_count=_sum_known(tuple(attempt.retry_count for attempt in attempts)),
        repair_count=repair_count,
        request_sha256=_sha(
            json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        ),
        response_sha256=_sha(exchange.raw_response) if exchange else None,
        # This is observed effective identity, not merely the requested bytes.
        prompt_sha256=producing.prompt_sha256 if producing else None,
        effective_prompt_verified=producing is not None,
        prompt_version=request.prompt_version,
        schema_version=request.schema_version,
        source_evidence_sha256=request.source_evidence_sha256,
        budget_policy=request.budget_policy,
        attempts=attempts,
        failure_code=_failure_code(failure) if failure else None,
        error_message=redact(str(failure))[:500] if failure else None,
    )
    return ContractExecution(response=response, exchange=exchange, error=failure)
