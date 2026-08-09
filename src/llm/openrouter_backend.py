# pyright: reportPrivateUsage=false
#
# This module intentionally calls `llm.cli._enforce_budget_pre_call` (the
# single budget-gate implementation shared by every backend) via a module
# reference so the existing test monkeypatch surface —
# ``monkeypatch.setattr(llm.cli, "_enforce_budget_pre_call", ...)`` — covers
# the OpenRouter path too. The module-level directive silences only that
# cross-module private-usage rule (same precedent as cli.py / gemini_backend.py).
"""
src/llm/openrouter_backend.py
-----------------------------
OpenRouter THIRD backend behind ``call_llm`` — a unified, OpenAI-compatible
gateway to many cheap open-weight models (DeepSeek, Qwen, Llama, Mistral, ...)
through ONE metered API key. Its reason to exist: widen the pareto optimizer's
cheap-candidate pool without a bespoke SDK per provider (contrast the Claude
subprocess wrapper and the Gemini google.genai SDK — each a full
integration; OpenRouter is one HTTP client reaching hundreds of models).

THE MODEL-IDENTITY GUARDRAIL (the eval-integrity requirement).
OpenRouter can route the *same* model id to different upstream providers at
different quantizations and context limits — which would make a graded
"candidate" a moving target: you grade DeepSeek-via-A at fp16 today, production
later serves DeepSeek-via-B at fp8, and the parity verdict silently stops
transferring. Every call therefore pins provider routing
(``provider.allow_fallbacks=false`` + a quantization floor + optional hard
``only`` provider pin) so a candidate model is a STABLE, reproducible thing to
grade. Because eval-time and production-time calls both go through THIS function
with the SAME routing config, a parity verdict earned in the sweep transfers to
production by construction.

Auth: ``OPENROUTER_API_KEY`` (metered). Same posture as the Gemini Developer API
backend after the 2026-07 migration — a metered key, budget-gated, ledgered.
Data governance: ``provider.data_collection`` defaults to ``"deny"`` so prompts
never route to a provider that trains on them (override via
``OPENROUTER_DATA_COLLECTION=allow`` if you need the wider/cheaper provider pool).

Public API:
    OPENROUTER_BACKEND_DEFAULT_MODEL — default model id for forced calls.
    OPENROUTER_MODELS — explicit per-purpose OpenRouter model pins (ships empty).
    OPENROUTER_BACKEND_ALLOWED_PURPOSES — the eval-gated routing allowlist
        (ships EMPTY; routing is model-first, see directives/cheapest_model_routing.md).
    openrouter_model_for(purpose) — purpose → OpenRouter model id.
    call_openrouter(...) — single-shot call (same contract as llm.cli._call_claude
        / gemini_backend.call_gemini).
    usage_meta_from_openrouter(usage, model=...) — OpenRouter usage → claude-shaped
        ledger meta (prefers OpenRouter's REAL charged cost over an estimate).

Setup + routing policy: directives/openrouter_backend.md.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import cast

import requests

from llm import cli as llm_cli
from llm.ledger import record_llm_call

log = logging.getLogger(__name__)

_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# Default model for a forced ``backend="openrouter"`` call with no explicit
# model (e.g. the compare harness). DeepSeek V3's stable slug — the reliably
# cheap structured-extraction workhorse that the classifier purposes target.
# Curate the real candidate set per the sweep; this is only the fallback.
OPENROUTER_BACKEND_DEFAULT_MODEL = "deepseek/deepseek-chat"

# Explicit per-purpose OpenRouter model pins. Ships EMPTY — a purpose reaches
# OpenRouter only after the eval judges grade its output at parity (routing is
# model-first: set an OpenRouter id in LLM_MODELS / model_pin_overrides).
OPENROUTER_MODELS: dict[str, str] = {}

# Legacy-shaped eval gate, kept for symmetry with the Claude/Gemini backends and
# their tests. Routing no longer consults an allowlist (it is model-family-first);
# ships EMPTY and stays empty. See directives/cheapest_model_routing.md.
OPENROUTER_BACKEND_ALLOWED_PURPOSES: frozenset[str] = frozenset()

# Same default cap as the other backends (long analytical prompts), overridable.
OPENROUTER_BACKEND_TIMEOUT_SECONDS = int(
    os.environ.get("OPENROUTER_BACKEND_TIMEOUT_SECONDS", "1200")
)

# One-time key-setup instruction, surfaced verbatim in LLMSetupError on any
# auth-shaped failure.
OPENROUTER_API_KEY_HINT = (
    "The OpenRouter backend needs a valid OPENROUTER_API_KEY in .env — create "
    "one at https://openrouter.ai/keys. It is a metered key (per-token billing "
    "with OpenRouter's real cost recorded per call); nothing bills it until a "
    "purpose is routed to an OpenRouter model or a call forces backend='openrouter'."
)

# App-attribution headers OpenRouter uses for its model/app rankings (optional,
# harmless). Identifies this platform's traffic in the OpenRouter dashboard.
_OPENROUTER_HEADERS_EXTRA = {
    "HTTP-Referer": "https://github.com/bhanunuthakki/earnings-summary",
    "X-Title": "earnings-summary",
}


# THE MODEL-IDENTITY / GOVERNANCE ROUTING CONFIG. Sent as the request's
# ``provider`` object on every call so a candidate model is a stable, graded
# thing (see the module docstring). Knobs:
#   * allow_fallbacks=False — never silently reroute to a provider you didn't
#     approve; a provider outage surfaces as an error (honest) rather than a
#     silent identity swap that would invalidate a parity verdict.
#   * require_parameters=True — only providers that honour every request param.
#   * data_collection — "deny" keeps prompts off providers that train on them
#     (governance default; relax via OPENROUTER_DATA_COLLECTION=allow).
#   * quantizations — a precision FLOOR: exclude the aggressive int4/int8 quants
#     whose quality drifts, so the same id doesn't grade differently across calls.
#   * only — a HARD upstream-provider pin (strongest reproducibility). Empty by
#     default; set OPENROUTER_PROVIDER_ONLY=csv to freeze the exact upstream for
#     a rigorous graded eval.
def _provider_routing() -> dict[str, object]:
    routing: dict[str, object] = {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": os.environ.get("OPENROUTER_DATA_COLLECTION", "deny"),
        "quantizations": ["fp16", "bf16", "fp8", "unknown"],
    }
    only_raw = os.environ.get("OPENROUTER_PROVIDER_ONLY", "")
    only = [p.strip() for p in only_raw.split(",") if p.strip()]
    if only:
        routing["only"] = only
    return routing


# HTTP status codes that mean "the operator must fix the key/account" — a
# deterministic setup problem (every retry fails identically), so LLMSetupError
# (hard stop), never a transient degrade. 402 = out of credits; 401/403 = bad key.
_AUTH_STATUS_CODES = frozenset({401, 402, 403})

# Module-level caches, mirroring the other backends. Tests monkeypatch directly.
_openrouter_setup_verified: bool = False
_openrouter_api_key: str | None = None


def openrouter_model_for(purpose: str | None) -> str:
    """Resolve a purpose to an OpenRouter model id.

    Order: explicit OPENROUTER_MODELS pin → an OpenRouter id already in
    LLM_MODELS (a promoted purpose) → OPENROUTER_BACKEND_DEFAULT_MODEL. Unlike
    Gemini there is no fast/slow tier derivation — the OpenRouter candidate pool
    is heterogeneous and curated explicitly per purpose by the sweep.
    """
    if purpose is not None:
        pinned = OPENROUTER_MODELS.get(purpose)
        if pinned is not None:
            return pinned
        llm_model = llm_cli.LLM_MODELS.get(purpose)
        if llm_model is not None:
            from llm.model_ladder import OPENROUTER as _OPENROUTER
            from llm.model_ladder import family_of

            if family_of(llm_model) == _OPENROUTER or "/" in llm_model:
                return llm_model
    return OPENROUTER_BACKEND_DEFAULT_MODEL


def _verify_openrouter_setup_once() -> None:
    """Resolve and cache OPENROUTER_API_KEY. Missing key → LLMSetupError
    (deterministic, operator-actionable — mirrors the Claude missing-binary and
    Gemini missing-key checks). The key is NOT validated against the API here
    (that costs a call); an invalid/expired key surfaces from the first real
    call's HTTP status via _classify_openrouter_failure."""
    global _openrouter_setup_verified, _openrouter_api_key
    if _openrouter_setup_verified:
        return
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise llm_cli.LLMSetupError(f"No OpenRouter API key configured. {OPENROUTER_API_KEY_HINT}")
    _openrouter_api_key = api_key
    _openrouter_setup_verified = True


def _safe_int(v: object) -> int:
    """One numeric field, defensively (bool is an int subclass)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v)


def usage_meta_from_openrouter(
    usage: dict[str, object] | None, *, model: str | None = None
) -> dict[str, object]:
    """OpenRouter response ``usage`` → the claude-shaped ledger meta the shared
    writer (llm.ledger.record_llm_call → usage_from_json_meta) consumes.

    Token mapping (OpenAI-compatible): ``prompt_tokens`` → input_tokens,
    ``completion_tokens`` → output_tokens. Cost: OpenRouter returns the REAL
    charged ``cost`` (USD) when the request sets ``usage.include=true`` — we use
    that verbatim (more accurate than an estimate). Only if it is absent do we
    fall back to the model-ladder estimate. Defensive throughout: junk/missing
    fields yield zero counts and a 0.0 cost, never a raise.
    """
    u = usage or {}
    prompt_tokens = _safe_int(u.get("prompt_tokens"))
    completion_tokens = _safe_int(u.get("completion_tokens"))

    real_cost = u.get("cost")
    if isinstance(real_cost, (int, float)) and not isinstance(real_cost, bool):
        cost = float(real_cost)
    else:
        from llm.model_ladder import estimated_call_usd

        cost = estimated_call_usd(model, prompt_tokens, completion_tokens)

    return {
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "cache_read_input_tokens": 0,
        },
        "total_cost_usd": cost,
    }


def _classify_openrouter_failure(status_code: int, body: str) -> None:
    """Promote an auth/credit-shaped HTTP failure to LLMSetupError (hard stop).

    401/403 (bad or missing key) and 402 (out of credits) are deterministic and
    operator-actionable — every retry fails identically until the key/account is
    fixed — so they must classify as setup (propagate loudly), not transient
    (degrade + retry), exactly like a missing binary on the Claude path. Every
    other status (429 rate-limit, 5xx, 400 bad-model) returns None and stays on
    the operational path so the caller's failure policy (degrade to Claude, or
    raise for a forced backend='openrouter' call) applies.
    """
    if status_code in _AUTH_STATUS_CODES:
        raise llm_cli.LLMSetupError(
            f"OpenRouter rejected the request (HTTP {status_code}). {OPENROUTER_API_KEY_HINT}\n"
            f"Response: {body[:300]}"
        )


def _parse_openrouter_response(payload: dict[str, object]) -> tuple[str, dict[str, object]]:
    """Extract (text, usage) from an OpenAI-compatible OpenRouter response.

    Raises ValueError on a 200-with-error envelope (OpenRouter can report an
    error inside a 200 body) or a missing/empty message — the caller routes that
    onto the same operational-failure path as a non-2xx status.
    """
    error = payload.get("error")
    if isinstance(error, dict):
        error_dict = cast("dict[str, object]", error)
        raise ValueError(
            f"OpenRouter returned an error envelope: message={error_dict.get('message')!r} "
            f"code={error_dict.get('code')!r}"
        )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"OpenRouter response has no choices: keys={sorted(payload.keys())}")
    first = cast("list[object]", choices)[0]
    if not isinstance(first, dict):
        raise ValueError("OpenRouter choice[0] is not an object")
    message = cast("dict[str, object]", first).get("message")
    if not isinstance(message, dict):
        raise ValueError("OpenRouter choice[0].message is not an object")
    content = cast("dict[str, object]", message).get("content")
    if not isinstance(content, str):
        raise ValueError("OpenRouter choice[0].message.content is not a string")
    usage_obj = payload.get("usage")
    usage = cast("dict[str, object]", usage_obj) if isinstance(usage_obj, dict) else {}
    return content, usage


def call_openrouter(
    prompt: str,
    model: str | None = None,
    timeout_seconds: int | None = None,
    *,
    purpose: str | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    force_budget_bypass: bool = False,
    fallback_used: str | None = None,
    fallback_from_provider: str | None = None,
    fallback_from_transport: str | None = None,
) -> str:
    """Single-shot OpenRouter call. Same contract as ``llm.cli._call_claude`` /
    ``gemini_backend.call_gemini``: per-purpose budget gate up front, one
    best-effort ``llm_calls`` ledger row per attempt (success or failure; rows
    are distinguishable by the ``provider/model`` slash-namespaced model id with
    fallback_used NULL).

    No fallback wiring lives here — routing policy (when an OpenRouter failure
    falls back to Claude) belongs to ``call_llm``. Raises:
      * LLMBudgetExceeded — hard per-purpose cap (propagate; budget gate).
      * LLMSetupError — key missing, or rejected by the API (401/402/403).
      * RuntimeError / ValueError — operational failures (network, non-2xx,
        rate-limit, malformed body, empty content) the caller may degrade.
    """
    llm_cli._enforce_budget_pre_call(purpose, force_budget_bypass=force_budget_bypass)
    _verify_openrouter_setup_once()  # setup errors propagate
    assert _openrouter_api_key is not None  # set by _verify_openrouter_setup_once
    resolved_model = model or openrouter_model_for(purpose)
    resolved_timeout = timeout_seconds or OPENROUTER_BACKEND_TIMEOUT_SECONDS
    log.info(
        {
            "event": "openrouter_backend_call_start",
            "model": resolved_model,
            "prompt_chars": len(prompt),
            "purpose": purpose,
        }
    )

    from llm_call_ledger import sha256_text

    prompt_sha = sha256_text(prompt)
    headers = {
        "Authorization": f"Bearer {_openrouter_api_key}",
        "Content-Type": "application/json",
        **_OPENROUTER_HEADERS_EXTRA,
    }
    # The generating prompt rides through byte-identical to production — NO eval
    # metadata, NO temperature/system injection (the anti-bias / isolation
    # invariant: the machinery that decides to test a model must never leak into
    # the prompt the model sees). ``usage.include`` asks OpenRouter to return the
    # real charged cost; ``provider`` pins model identity (see _provider_routing).
    body: dict[str, object] = {
        "model": resolved_model,
        "messages": [{"role": "user", "content": prompt}],
        "provider": _provider_routing(),
        "usage": {"include": True},
    }

    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        # Serialize explicitly (data=, not json=) so the dict[str, object] body
        # doesn't trip requests' stricter JsonType stub; Content-Type is set above.
        resp = requests.post(
            _OPENROUTER_ENDPOINT,
            headers=headers,
            data=json.dumps(body),
            timeout=resolved_timeout,
        )
        if resp.status_code >= 400:
            # Classify auth/credit failures as hard setup errors; record + raise
            # everything else as operational.
            _classify_openrouter_failure(resp.status_code, resp.text)
            raise RuntimeError(
                f"OpenRouter HTTP {resp.status_code} for model {resolved_model}: {resp.text[:300]}"
            )
        payload = cast("dict[str, object]", resp.json())
        text, usage = _parse_openrouter_response(payload)
        text = text.strip()
        if not text:
            raise RuntimeError(f"OpenRouter returned empty content for model {resolved_model}.")
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info({"event": "openrouter_backend_call_done", "response_chars": len(text)})
        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=resolved_model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            response_text=text,
            meta=usage_meta_from_openrouter(usage, model=resolved_model),
            fallback_used=fallback_used,
            prompt=prompt,
            provider="openrouter",
            transport="metered_api",
            auth_class="api_key_metered",
            attempts=1,
            retries=0,
            fallback_from_provider=fallback_from_provider,
            fallback_from_transport=fallback_from_transport,
        )
        return text
    except (requests.RequestException, RuntimeError, ValueError, OSError) as openrouter_error:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=resolved_model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            error=f"{type(openrouter_error).__name__}: {str(openrouter_error)[:500]}",
            fallback_used=fallback_used,
            prompt=prompt,
            provider="openrouter",
            transport="metered_api",
            auth_class="api_key_metered",
            attempts=1,
            retries=0,
            failure_class="openrouter_transport",
            fallback_from_provider=fallback_from_provider,
            fallback_from_transport=fallback_from_transport,
        )
        raise
