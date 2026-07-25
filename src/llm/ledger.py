"""
src/llm/ledger.py
-----------------
Best-effort ledger writes for every LLM call (Phase 0 telemetry, migration
0034). Records one row per call into the ``llm_calls`` table with cost +
token usage + cache stats + error / fallback metadata.

Two entry points:
    record_llm_call(...) — primary write. Used by both the Claude success
        path and the Claude failure path (the latter passes error= and
        leaves response/meta None).
    fallback_call_logged(prompt, claude_error, ...) — wraps the Gemini
        fallback with its own ledger row so the fallback's latency and
        success/failure are observable. The fallback row is tagged
        fallback_used='gemini'.

Both are best-effort: failures here never break the LLM call (the inner
``llm_call_ledger.record_call`` already swallows DB errors; the outer guard
here catches anything more exotic).

Extracted from src/llm_client.py during the llm subpackage split (PURE
refactor — zero behavior change).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import cast

from llm.fallback import GEMINI_FALLBACK_MODEL, try_gemini_fallback

log = logging.getLogger(__name__)


def record_llm_call(
    *,
    started_at: datetime,
    elapsed_ms: int,
    model: str,
    prompt_sha: str,
    prompt_chars: int,
    purpose: str | None,
    ticker: str | None,
    scope: str | None,
    run_id: str | None,
    response_text: str | None = None,
    meta: dict[str, object] | None = None,
    error: str | None = None,
    fallback_used: str | None = None,
    prompt: object | None = None,
) -> None:
    """Best-effort write of one row into llm_calls. Never raises.

    On Claude success: pass the response_text + parsed CLI meta so usage/cost
    fields populate. On failure: pass error= and leave response/meta None — the
    ledger row still records the attempt and its latency. The fallback path
    records a SECOND row with fallback_used='gemini'.

    ``prompt`` (P0, llm.prompt_registry): pass the prompt OBJECT when in scope
    — if it is a ``RenderedPrompt`` the row carries the template identity
    (template_id / version / vars sha); a plain string carries NULLs, the
    honest mark of an unmigrated call site.
    """
    try:
        from llm.prompt_registry import template_meta
        from llm.tracectx import context_fields
        from llm_call_ledger import (
            LlmCallRecord,
            record_call,
            sha256_text,
            usage_from_json_meta,
        )

        template_id, template_version, vars_sha = template_meta(prompt)
        trace_id, span_id, parent_span_id, stage = context_fields()
        usage = usage_from_json_meta(meta) if meta else {}
        record_call(
            LlmCallRecord(
                called_at=started_at,
                model=model,
                prompt_sha256=prompt_sha,
                prompt_chars=prompt_chars,
                elapsed_ms=elapsed_ms,
                purpose=purpose,
                ticker=ticker,
                scope=scope,
                run_id=run_id,
                response_sha256=sha256_text(response_text) if response_text else None,
                response_chars=len(response_text) if response_text else None,
                input_tokens=cast("int | None", usage.get("input_tokens")),
                cache_creation_input_tokens=cast(
                    "int | None", usage.get("cache_creation_input_tokens")
                ),
                cache_read_input_tokens=cast("int | None", usage.get("cache_read_input_tokens")),
                output_tokens=cast("int | None", usage.get("output_tokens")),
                cost_estimate_usd=cast("float | None", usage.get("cost_estimate_usd")),
                fallback_used=fallback_used,
                error=error,
                template_id=template_id,
                template_version=template_version,
                template_vars_sha256=vars_sha,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                stage=stage,
            )
        )
    except Exception as exc:  # ImportError, unexpected attribute errors, …
        # Best-effort — the ledger module's record_call already swallows DB
        # errors; this outer guard catches anything more exotic so the LLM call
        # itself is never blocked by telemetry.
        log.debug({"event": "llm_call_ledger_record_failed", "error": str(exc)})


def fallback_call_logged(
    prompt: str,
    claude_error: Exception,
    *,
    prompt_sha: str,
    purpose: str | None,
    ticker: str | None,
    scope: str | None,
    run_id: str | None,
) -> str:
    """Wrap try_gemini_fallback with its own ledger row.

    Gemini's google-generativeai SDK doesn't surface per-call cost/token
    counts in a stable shape, so the row records latency + response_chars
    only; usage/cost stay NULL. That's still enough to track *how often*
    fallback fires and how much latency it adds.

    A ledger row is written ONLY when a Gemini attempt actually fires. When
    the fallback is disabled or unconfigured, ``try_gemini_fallback`` raises
    without attempting anything — recording a ``model='gemini-2.5-flash'``
    error row for that non-attempt fabricated 3,496 phantom rows in July 2026
    (every Claude failure double-counted, half of them against a model that
    was never called), which doubled the apparent platform error rate.
    """
    from llm.fallback import fallback_available

    if not fallback_available():
        return try_gemini_fallback(prompt, claude_error)  # raises; no phantom row
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        text = try_gemini_fallback(prompt, claude_error)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=GEMINI_FALLBACK_MODEL,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            response_text=text,
            fallback_used="gemini",
            prompt=prompt,
        )
        return text
    except Exception as gemini_err:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=GEMINI_FALLBACK_MODEL,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            error=f"{type(gemini_err).__name__}: {str(gemini_err)[:500]}",
            fallback_used="gemini",
            prompt=prompt,
        )
        raise
