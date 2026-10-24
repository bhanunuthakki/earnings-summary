"""Shared structured-output discipline: parse + retry-with-feedback, loudly.

The repo grew ~15 divergent fence-strip/parse/except blocks around
``call_llm`` (llm_evals_plan §5.4). Two pathologies recur:

* **silent-empty** — a call/parse failure degrades to ``{}``/``[]``/None
  that is indistinguishable from a legitimate "nothing found", so bad data
  ships (e.g. risk factors silently categorized "other" when the classify
  call failed);
* **no retry** — one chatty response (prose preamble, markdown fences)
  fails the whole extraction even though a single re-ask with feedback
  almost always fixes it (the trigger sensors proved the pattern:
  ``src/triggers/earnings_tone._call_llm_with_retry``).

``call_llm_structured`` is the shared replacement: call → parse → on parse
failure, ONE re-ask with an explicit "your previous response was not valid
JSON" preamble → still bad ⇒ raise ``StructuredParseError`` (loud — the
caller decides how to surface it, but it can never quietly become an empty
result). Hard stops (budget cap / missing CLI) propagate untouched per
``is_hard_stop`` — they are configuration, not parse quality.

Adoption is incremental: new call sites should start here; existing ones
migrate opportunistically (the worst silent-empty offender,
``risk_factor_classify``, is converted in the same PR that adds this).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, TypeVar

from pydantic import TypeAdapter, ValidationError

from llm.cli import call_llm
from llm.resolver import CapabilityProfile, structured_capability_profile
from log_redact import redact
from runtime.deterministic import (
    JSON_FENCE_RX as JSON_FENCE_RX,
)
from runtime.deterministic import (
    first_json_value,
)
from runtime.deterministic import (
    parse_json_payload as parse_json_payload,
)

log = logging.getLogger(__name__)
T = TypeVar("T")

_first_json_value = first_json_value


_RETRY_PREAMBLE = (
    "IMPORTANT: your previous response was not the valid JSON requested. "
    "Return ONLY the JSON specified at the end of this prompt — no markdown "
    "fences, no commentary, no prefatory prose.\n\n"
)


def repair_structured_prompt(prompt: str) -> str:
    """The canonical default prompt for one structured-output repair request."""
    return _RETRY_PREAMBLE + prompt


def _safe_raw_head(raw: str, limit: int) -> str:
    """Bound and redact provider output before it reaches logs/exceptions."""
    return redact(raw[:limit])


class StructuredParseError(ValueError):
    """The LLM returned unusable JSON on the first attempt AND the retry.

    Carries the last raw response head so the failure is debuggable from the
    log/exception alone. Deliberately NOT a hard stop — the caller chooses
    section-scope degradation, but must do so visibly (never by returning a
    value indistinguishable from real output).
    """

    def __init__(self, message: str, *, raw_head: str = "") -> None:
        super().__init__(message)
        self.raw_head = raw_head


@dataclass(frozen=True, slots=True)
class StructuredCallResult(Generic[T]):
    """Validated value plus the exact final governed exchange."""

    value: T
    raw_response: str
    prompt: str


def call_llm_structured(
    prompt: str,
    *,
    purpose: str,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    timeout_seconds: int | None = None,
    expect: Literal["object", "array"] = "object",
    required_keys: tuple[str, ...] = (),
    schema: TypeAdapter[T] | None = None,
    domain_guardrail: Callable[[object], tuple[bool, str]] | None = None,
    max_escalation_tier: int = 0,
    db_path: Path | str | None = None,
    capability_profile: CapabilityProfile | None = None,
) -> T | object:
    """``call_llm`` + strict parse + P3 Cascade Router tier escalation, loud on failure.

    Attempts structured extraction using the primary purpose model.
    On schema or domain_guardrail failure, attempts 1 repair.
    If repair fails and max_escalation_tier >= 1, escalates to higher tier model
    (e.g., Sonnet/Pro) to complete extraction.
    """
    effective_profile = structured_capability_profile(capability_profile)
    raw = call_llm(
        prompt,
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        model=model,
        backend=backend,
        timeout_seconds=timeout_seconds,
        db_path=db_path,
        capability_profile=effective_profile,
    )
    try:
        parsed = parse_json_payload(raw, expect=expect, required_keys=required_keys)
        validated = schema.validate_python(parsed) if schema is not None else parsed
        if domain_guardrail is not None:
            ok, g_reason = domain_guardrail(validated)
            if not ok:
                raise ValueError(f"Domain guardrail validation failed: {g_reason}")
        return validated
    except (ValueError, ValidationError) as first_exc:
        log.warning(
            {
                "event": "llm_structured_parse_failed_retrying",
                "purpose": purpose,
                "ticker": ticker,
                "error": redact(first_exc),
                "raw_head": _safe_raw_head(raw, 200),
            }
        )

    # Attempt 2: Same tier with repair preamble
    raw_retry = call_llm(
        repair_structured_prompt(prompt),
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        model=model,
        backend=backend,
        timeout_seconds=timeout_seconds,
        db_path=db_path,
        capability_profile=effective_profile,
    )
    try:
        parsed = parse_json_payload(raw_retry, expect=expect, required_keys=required_keys)
        validated = schema.validate_python(parsed) if schema is not None else parsed
        if domain_guardrail is not None:
            ok, g_reason = domain_guardrail(validated)
            if not ok:
                raise ValueError(f"Domain guardrail validation failed: {g_reason}")
        return validated
    except (ValueError, ValidationError) as retry_exc:
        escalation_models: tuple[str, str] | None = None
        if max_escalation_tier >= 1:
            # Check for P3 Cascade Escalation to higher tier.
            from llm.cli import DEFAULT_MODEL
            from llm.resolver import resolve_model_and_backend

            primary_model, _ = resolve_model_and_backend(
                purpose=purpose,
                model=model,
                capability_profile=effective_profile,
                db_path=db_path,
            )
            if model is None and primary_model != DEFAULT_MODEL:
                escalation_models = (primary_model, DEFAULT_MODEL)

        if escalation_models is not None:
            primary_model, target_model = escalation_models
            log.warning(
                {
                    "event": "p3_cascade_escalation_triggered",
                    "purpose": purpose,
                    "ticker": ticker,
                    "reason": str(retry_exc),
                    "primary_model": primary_model,
                    "target_model": target_model,
                }
            )

            raw_escalated = call_llm(
                repair_structured_prompt(prompt),
                purpose=purpose,
                model=target_model,
                backend=None,
                ticker=ticker,
                scope=scope,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
                db_path=db_path,
                capability_profile=effective_profile,
            )
            try:
                parsed_esc = parse_json_payload(
                    raw_escalated, expect=expect, required_keys=required_keys
                )
                validated_esc = (
                    schema.validate_python(parsed_esc) if schema is not None else parsed_esc
                )
                if domain_guardrail is not None:
                    ok_esc, g_reason_esc = domain_guardrail(validated_esc)
                    if not ok_esc:
                        raise ValueError(
                            f"Domain guardrail validation failed on escalated tier: {g_reason_esc}"
                        )
                return validated_esc
            except (ValueError, ValidationError) as esc_exc:
                log.error(
                    {
                        "event": "llm_structured_parse_failed_after_cascade",
                        "purpose": purpose,
                        "ticker": ticker,
                        "error": redact(esc_exc),
                        "raw_head": _safe_raw_head(raw_escalated, 200),
                    }
                )
                raise StructuredParseError(
                    f"{purpose}: LLM returned unusable JSON on primary and escalated tiers: "
                    f"{redact(esc_exc)}",
                    raw_head=_safe_raw_head(raw_escalated, 500),
                ) from esc_exc

        log.error(
            {
                "event": "llm_structured_parse_failed_twice",
                "purpose": purpose,
                "ticker": ticker,
                "error": redact(retry_exc),
                "raw_head": _safe_raw_head(raw_retry, 200),
            }
        )
        raise StructuredParseError(
            f"{purpose}: LLM returned unusable JSON on both attempts: {redact(retry_exc)}",
            raw_head=_safe_raw_head(raw_retry, 500),
        ) from retry_exc


def call_llm_structured_with_raw(
    prompt: str,
    *,
    purpose: str,
    schema: TypeAdapter[T],
    repair_prompt: Callable[[str], str] | None = None,
    domain_guardrail: Callable[[object], tuple[bool, str]] | None = None,
    raw_response_observer: Callable[[str, str], None] | None = None,
    expect: Literal["object", "array"] = "object",
    model: str | None = None,
    backend: str | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    db_path: Path | str | None = None,
    capability_profile: CapabilityProfile | None = None,
) -> StructuredCallResult[T]:
    """Schema-decode with one governed repair while preserving exact exchange bytes.

    Registered ``RenderedPrompt`` callers supply a repair callback to retain
    prompt identity. Plain-string callers can use the canonical default repair.
    """

    effective_profile = structured_capability_profile(capability_profile)
    current_prompt = prompt
    raw = call_llm(
        current_prompt,
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        db_path=db_path,
        model=model,
        backend=backend,
        capability_profile=effective_profile,
    )
    # Retention is not a formatting failure. An observer failure must stop
    # before parsing or another governed request can consume quota.
    if raw_response_observer is not None:
        raw_response_observer(current_prompt, raw)
    try:
        payload = parse_json_payload(raw, expect=expect)
        value = schema.validate_python(payload)
        if domain_guardrail is not None:
            valid, reason = domain_guardrail(value)
            if not valid:
                raise ValueError(reason)
        return StructuredCallResult(
            value=value,
            raw_response=raw,
            prompt=current_prompt,
        )
    except (ValueError, ValidationError) as first_exc:
        log.warning(
            {
                "event": "llm_structured_parse_failed_retrying",
                "purpose": purpose,
                "ticker": ticker,
                "error": redact(first_exc),
                "raw_head": _safe_raw_head(raw, 200),
            }
        )
        current_prompt = (
            repair_prompt(str(first_exc)) if repair_prompt else repair_structured_prompt(prompt)
        )
    raw = call_llm(
        current_prompt,
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        db_path=db_path,
        model=model,
        backend=backend,
        capability_profile=effective_profile,
    )
    # Retention is not a formatting failure. An observer failure must stop
    # before parsing or another governed request can consume quota.
    if raw_response_observer is not None:
        raw_response_observer(current_prompt, raw)
    try:
        payload = parse_json_payload(raw, expect=expect)
        value = schema.validate_python(payload)
        if domain_guardrail is not None:
            valid, reason = domain_guardrail(value)
            if not valid:
                raise ValueError(reason)
        return StructuredCallResult(
            value=value,
            raw_response=raw,
            prompt=current_prompt,
        )
    except (ValueError, ValidationError) as retry_exc:
        log.error(
            {
                "event": "llm_structured_parse_failed_twice",
                "purpose": purpose,
                "ticker": ticker,
                "error": redact(retry_exc),
                "raw_head": _safe_raw_head(raw, 200),
            }
        )
        raise StructuredParseError(
            f"{purpose}: LLM returned unusable JSON on both attempts: {redact(retry_exc)}",
            raw_head=_safe_raw_head(raw, 500),
        ) from retry_exc
