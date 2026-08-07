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

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, TypeVar, cast

from pydantic import TypeAdapter, ValidationError

from llm.cli import call_llm

log = logging.getLogger(__name__)
T = TypeVar("T")

JSON_FENCE_RX = re.compile(r"^```(?:json)?\s*|\s*```$")

_RETRY_PREAMBLE = (
    "IMPORTANT: your previous response was not the valid JSON requested. "
    "Return ONLY the JSON specified at the end of this prompt — no markdown "
    "fences, no commentary, no prefatory prose.\n\n"
)


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


def _first_json_value(text: str, expect: Literal["object", "array"]) -> object:
    """Decode the first complete JSON value of the expected shape from ``text``,
    ignoring any trailing prose/fences the model appended after it.

    ``raw_decode`` parses one value and reports where it ended, so a chatty
    ``[]\\n```\\n\\n<explanation>`` yields ``[]``. Raises ValueError (naming the
    problem) when no plausible opener is present or the value won't decode — the
    loud contract is preserved."""
    opener = "{" if expect == "object" else "["
    idx = text.find(opener)
    if idx == -1:
        raise ValueError(f"not valid JSON: no {expect} opener found in response")
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[idx:])
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    return payload


def parse_json_payload(
    raw: str,
    *,
    expect: Literal["object", "array"] = "object",
    required_keys: tuple[str, ...] = (),
) -> object:
    """Fence-tolerant strict parse. Raises ValueError naming the problem when
    the text isn't the expected JSON shape or misses a required key."""
    text = raw.strip()
    if text.startswith("```"):
        text = JSON_FENCE_RX.sub("", text).strip()
    try:
        payload: object = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Tolerate trailing prose/fences after an otherwise-valid JSON value: a
        # chatty model emits ``[]\n```\n\nThe section describes ...`` which
        # json.loads rejects as "Extra data". Decode just the first complete
        # value from the expected opener and ignore whatever follows. Genuine
        # garbage (no opener, or an undecodable value) still raises loudly so the
        # retry-with-feedback layer fires.
        payload = _first_json_value(text, expect)
    if expect == "object":
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
        obj = cast("dict[str, object]", payload)
        missing = [k for k in required_keys if k not in obj]
        if missing:
            raise ValueError(f"JSON object missing required key(s): {missing}")
        return obj
    if not isinstance(payload, list):
        raise ValueError(f"expected a JSON array, got {type(payload).__name__}")
    return cast("list[object]", payload)


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
    max_escalation_tier: int = 1,
    db_path: Path | str | None = None,
) -> T | object:
    """``call_llm`` + strict parse + P3 Cascade Router tier escalation, loud on failure.

    Attempts structured extraction using the primary purpose model.
    On schema or domain_guardrail failure, attempts 1 repair.
    If repair fails and max_escalation_tier >= 1, escalates to higher tier model
    (e.g., Sonnet/Pro) to complete extraction.
    """
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
                "error": str(first_exc),
                "raw_head": raw[:200],
            }
        )

    # Attempt 2: Same tier with repair preamble
    raw_retry = call_llm(
        _RETRY_PREAMBLE + prompt,
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        model=model,
        backend=backend,
        timeout_seconds=timeout_seconds,
        db_path=db_path,
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
        # Check for P3 Cascade Escalation to higher tier
        from llm.cli import DEFAULT_MODEL
        from llm.resolver import resolve_model_and_backend

        is_explicit_model = model is not None
        primary_model, _ = resolve_model_and_backend(purpose=purpose, model=model)

        if max_escalation_tier >= 1 and not is_explicit_model and primary_model != DEFAULT_MODEL:
            log.warning(
                {
                    "event": "p3_cascade_escalation_triggered",
                    "purpose": purpose,
                    "ticker": ticker,
                    "reason": str(retry_exc),
                    "primary_model": primary_model,
                    "target_model": DEFAULT_MODEL,
                }
            )

            raw_escalated = call_llm(
                _RETRY_PREAMBLE + prompt,
                purpose=purpose,
                model=DEFAULT_MODEL,
                backend="claude",
                ticker=ticker,
                scope=scope,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
                db_path=db_path,
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
                        "error": str(esc_exc),
                        "raw_head": raw_escalated[:200],
                    }
                )
                raise StructuredParseError(
                    f"{purpose}: LLM returned unusable JSON on primary and escalated tiers: {esc_exc}",
                    raw_head=raw_escalated[:500],
                ) from esc_exc

        log.error(
            {
                "event": "llm_structured_parse_failed_twice",
                "purpose": purpose,
                "ticker": ticker,
                "error": str(retry_exc),
                "raw_head": raw_retry[:200],
            }
        )
        raise StructuredParseError(
            f"{purpose}: LLM returned unusable JSON on both attempts: {retry_exc}",
            raw_head=raw_retry[:500],
        ) from retry_exc


def call_llm_structured_with_raw(
    prompt: str,
    *,
    purpose: str,
    schema: TypeAdapter[T],
    repair_prompt: Callable[[str], str],
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    db_path: Path | str | None = None,
) -> StructuredCallResult[T]:
    """Schema-decode with one governed repair while preserving exact exchange bytes.

    The repair callback is required so a versioned ``RenderedPrompt`` can
    remain versioned on the second attempt; blindly prefixing a string would
    erase the prompt-registry identity from the ledger.
    """

    current_prompt = prompt
    raw = call_llm(
        current_prompt,
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        db_path=db_path,
    )
    try:
        payload = parse_json_payload(raw, expect="object")
        return StructuredCallResult(
            value=schema.validate_python(payload),
            raw_response=raw,
            prompt=current_prompt,
        )
    except (ValueError, ValidationError) as first_exc:
        log.warning(
            {
                "event": "llm_structured_parse_failed_retrying",
                "purpose": purpose,
                "ticker": ticker,
                "error": str(first_exc),
                "raw_head": raw[:200],
            }
        )
        current_prompt = repair_prompt(str(first_exc))
    raw = call_llm(
        current_prompt,
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        db_path=db_path,
    )
    try:
        payload = parse_json_payload(raw, expect="object")
        return StructuredCallResult(
            value=schema.validate_python(payload),
            raw_response=raw,
            prompt=current_prompt,
        )
    except (ValueError, ValidationError) as retry_exc:
        log.error(
            {
                "event": "llm_structured_parse_failed_twice",
                "purpose": purpose,
                "ticker": ticker,
                "error": str(retry_exc),
                "raw_head": raw[:200],
            }
        )
        raise StructuredParseError(
            f"{purpose}: LLM returned unusable JSON on both attempts: {retry_exc}",
            raw_head=raw[:500],
        ) from retry_exc
