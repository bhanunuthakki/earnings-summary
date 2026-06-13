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
from typing import Literal, cast

from llm.cli import call_llm

log = logging.getLogger(__name__)

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
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
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
    expect: Literal["object", "array"] = "object",
    required_keys: tuple[str, ...] = (),
) -> object:
    """``call_llm`` + strict parse + one retry-with-feedback, loud on failure.

    Returns the parsed dict/list. Raises:
      * whatever ``call_llm`` raises for call failures (hard stops included —
        budget/setup problems must never be reshaped into parse errors);
      * ``StructuredParseError`` when both attempts returned unusable JSON.
    """
    raw = call_llm(
        prompt,
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        model=model,
        backend=backend,
    )
    try:
        return parse_json_payload(raw, expect=expect, required_keys=required_keys)
    except ValueError as first_exc:
        log.warning(
            {
                "event": "llm_structured_parse_failed_retrying",
                "purpose": purpose,
                "ticker": ticker,
                "error": str(first_exc),
                "raw_head": raw[:200],
            }
        )
    raw_retry = call_llm(
        _RETRY_PREAMBLE + prompt,
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        model=model,
        backend=backend,
    )
    try:
        return parse_json_payload(raw_retry, expect=expect, required_keys=required_keys)
    except ValueError as retry_exc:
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
