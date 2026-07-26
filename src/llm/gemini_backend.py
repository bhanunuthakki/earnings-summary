# pyright: reportPrivateUsage=false
#
# This module intentionally calls `llm.cli._enforce_budget_pre_call` (the
# single budget-gate implementation shared by every backend) via a module
# reference so the existing test monkeypatch surface —
# ``monkeypatch.setattr(llm.cli, "_enforce_budget_pre_call", ...)`` — covers
# the Gemini path too. The module-level directive silences only that
# cross-module private-usage rule, preserving every other strict check
# (same precedent as src/llm/cli.py).
"""
src/llm/gemini_backend.py
-------------------------
Gemini second backend behind ``call_llm`` — calls the Gemini Developer API
directly (metered ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``, same key and SDK
``src/llm/fallback.py`` already uses for its emergency path).

HISTORY: this backend originally shelled out to ``gemini-cli`` under the
consumer "Login with Google" OAuth path (Gemini Code Assist for
individuals), deliberately STRIPPING API keys from the subprocess so every
call billed the flat-rate consumer subscription instead of a metered key —
see git history for that design. Google discontinued individual-account
OAuth access to gemini-cli on 2026-06-18 (``IneligibleTierError`` for every
model, unconditionally — confirmed live; not a version/model-id/timeout
issue). The announced replacement, Antigravity CLI (``agy``) / the
``google-antigravity`` SDK, was evaluated and rejected: both are agentic
coding-assistant harnesses with no documented model pinning and no
structured usage output, not a bare completion API — unusable for a
deterministic, model-pinned eval/judge call. The Gemini Developer API was
never part of the shutdown (enterprise/API-key access "remains completely
unaffected" per Google's own migration guidance), so this module now calls
it directly. Real per-token cost, not $0 — see directives/gemini_backend.md
for the 2026-07 migration record and a grounded cost estimate (~$2/mo at
current eval-sweep volume).

Public API:
    GEMINI_BACKEND_DEFAULT_MODEL, GEMINI_BACKEND_FAST_MODEL — model ids.
    GEMINI_MODELS — explicit per-purpose Gemini model pins (ships empty).
    GEMINI_BACKEND_ALLOWED_PURPOSES — the eval-gated routing allowlist
        (ships EMPTY; see gemini_allowed_purposes for the env-var trial hook).
    gemini_allowed_purposes() — allowlist ∪ GEMINI_BACKEND_PURPOSES env csv.
    gemini_model_for(purpose) — purpose → Gemini model id.
    call_gemini(...) — single-shot Gemini API call (same contract as
        llm.cli._call_claude).
    usage_meta_from_response(usage, model=...) — usage_metadata → claude-shaped
        usage meta for the shared ledger writer.
"""

from __future__ import annotations

import logging
import os
import re
import time
import warnings
from datetime import UTC, datetime
from typing import cast

from llm import cli as llm_cli
from llm.ledger import record_llm_call

# NOTE: `google.generativeai` is deprecated (support "has ended" per the
# package's own FutureWarning as of 2026); Google's path forward is
# `google-genai` with a different API (genai.Client / client.models.generate_content).
# Migrate when convenient — the deprecated package still works and matches
# src/llm/fallback.py, which depends on the same package for the same reason.
# See: https://github.com/google-gemini/deprecated-generative-ai-python
with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    import google.generativeai as genai
    from google.api_core import exceptions as google_exceptions

log = logging.getLogger(__name__)

# Gemini API tiers for analytical writing (Pro) vs the short structured calls
# that run on Haiku under the Claude backend (Flash). The tier derivation in
# gemini_model_for keeps these two aligned with LLM_MODELS so retuning a
# purpose's Claude tier automatically retunes its Gemini mirror.
# Pro = gemini-3.1-pro-preview (current Pro baseline; gemini-3-pro-preview was
# discontinued 2026-03 and there is no 3.5 Pro — only 3.5 Flash).
# Flash = gemini-3-flash-preview (current Gemini 3 Flash preview id; self-anneals
# below if the API 404s it — preview aliases rotate without notice).
GEMINI_BACKEND_DEFAULT_MODEL = "gemini-3.1-pro-preview"
GEMINI_BACKEND_FAST_MODEL = "gemini-3-flash-preview"
# Stable GA fallback for the fast tier. If GEMINI_BACKEND_FAST_MODEL (a preview
# alias) returns NotFound, call_gemini self-anneals: it writes this id into
# _effective_fast_model and retries — all subsequent in-process calls then use
# the fallback automatically without operator intervention.
_GEMINI_FAST_MODEL_FALLBACK = "gemini-2.5-flash"

# Explicit per-purpose Gemini model pins. Ships EMPTY: the default tier
# derivation (Claude fast-classifier purposes → Flash, everything else → Pro)
# is right until an eval says otherwise. Add an entry here when the judges
# find a purpose needs a specific Gemini model (e.g. a long-context bulk job
# pinned to a 1M-context model id).
GEMINI_MODELS: dict[str, str] = {}

# THE EVAL GATE. Purposes listed here route to Gemini in production via
# call_llm's backend resolution. Ships EMPTY by design: a purpose may be
# added ONLY after the LLM-evals judges grade its Gemini output quality
# against Claude's on real prompts (execution/compare_backends.py produces
# the side-by-side corpus they grade). Process + sign-off requirements:
# directives/gemini_backend.md.
GEMINI_BACKEND_ALLOWED_PURPOSES: frozenset[str] = frozenset()

# Local-trial escape hatch: a comma-separated purpose list in this env var is
# merged into the allowlist for the current process only. Lets the operator
# trial-route a purpose without a code change; production stays empty.
GEMINI_BACKEND_PURPOSES_ENV_VAR = "GEMINI_BACKEND_PURPOSES"

# Same default cap as the Claude path (long-context analytical prompts), with
# its own env override mirroring CLAUDE_CLI_TIMEOUT_SECONDS. Renamed from
# GEMINI_CLI_TIMEOUT_SECONDS in the 2026-07 API-key migration (this backend is
# no longer a CLI wrapper); the old name was never set operationally.
GEMINI_BACKEND_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_BACKEND_TIMEOUT_SECONDS", "1200"))

# One-time key-setup instruction, surfaced verbatim in LLMSetupError whenever
# the API reports an auth/permission failure.
GEMINI_API_KEY_HINT = (
    "The Gemini backend calls the Gemini Developer API directly and needs a "
    "valid GEMINI_API_KEY (or GOOGLE_API_KEY) in .env — get one at "
    "https://aistudio.google.com/app/apikey. This is the same key "
    "src/llm/fallback.py uses for its emergency path; if that key is also "
    "known-bad, rotate it there too."
)

# An InvalidArgument (400) can mean a genuinely malformed request OR a bad
# API key — the API overloads the same status code for both. Only promote to
# LLMSetupError (hard stop) when the message names an auth-shaped reason;
# Unauthenticated/PermissionDenied are unambiguous and always promoted.
_AUTH_ERROR_MARKERS = (
    "API_KEY_INVALID",
    "API key not valid",
    "PERMISSION_DENIED",
    "UNAUTHENTICATED",
)

# Module-level caches, mirroring llm_client._setup_verified for the Claude
# backend. Tests monkeypatch these directly.
_gemini_setup_verified: bool = False
_gemini_api_key: str | None = None
_effective_fast_model: str | None = (
    None  # None = GEMINI_BACKEND_FAST_MODEL; set by _anneal_fast_model
)


def gemini_allowed_purposes() -> frozenset[str]:
    """Deprecated. Production routing now dispatches by model family (set a
    Gemini model ID in LLM_MODELS / model_pin_overrides to route a purpose to
    Gemini). This function is retained for backward-compat; call_llm no longer
    consults it. Returns the code-level frozenset plus the env-var escape hatch
    (comma-separated list in GEMINI_BACKEND_PURPOSES) — both still empty by
    default."""
    raw = os.environ.get(GEMINI_BACKEND_PURPOSES_ENV_VAR, "")
    env_purposes = {p.strip() for p in raw.split(",") if p.strip()}
    return GEMINI_BACKEND_ALLOWED_PURPOSES | frozenset(env_purposes)


def gemini_model_for(purpose: str | None) -> str:
    """Resolve a purpose to a Gemini model id.

    Order:
    1. Explicit GEMINI_MODELS pin (operator override, ships empty).
    2. If LLM_MODELS already pins the purpose to a Gemini model ID, return it
       directly — promoted purposes land here (Chip 2 PR D+).
    3. Tier derivation: Haiku-class purposes in LLM_MODELS mirror to Flash;
       everything else gets Pro.  Kept for purposes still on Claude tiers so
       `call_gemini` can be invoked without an explicit model.
    4. Pro for unknown/None purposes.
    """
    if purpose is not None:
        pinned = GEMINI_MODELS.get(purpose)
        if pinned is not None:
            return pinned
        llm_model = llm_cli.LLM_MODELS.get(purpose)
        if llm_model is not None:
            from llm.model_ladder import GEMINI as _GEMINI
            from llm.model_ladder import family_of

            # A purpose pinned to a Gemini id (Chip 2 PR D+) is returned
            # verbatim. The prefix check also covers preview aliases like
            # GEMINI_BACKEND_FAST_MODEL ("gemini-3-flash-preview") that the
            # cost ladder deliberately omits (it ranks only verified stable
            # ids; call_gemini self-anneals the alias at runtime).
            if family_of(llm_model) == _GEMINI or llm_model.startswith("gemini-"):
                return llm_model
            if llm_model == llm_cli.FAST_CLASSIFIER_MODEL:
                return _effective_fast_model or GEMINI_BACKEND_FAST_MODEL
    return GEMINI_BACKEND_DEFAULT_MODEL


def _discover_api_flash_model() -> str | None:
    """Query the Gemini Developer API's live model catalog (via the same
    authenticated client, ``genai.list_models()``) for the highest-generation
    Flash model currently available to this key that supports generateContent.
    Returns None on any failure so callers fall through to the hardcoded
    stable fallback without crashing.

    Replaces the pre-migration approach of scraping gemini-cli's GitHub docs:
    that CLI is being retired (see module docstring), so its docs are no
    longer a reliable signal — the API's own catalog is the correct source of
    truth for what a given key can actually call.
    """
    try:
        # google.generativeai's stubs don't declare list_models (same known gap
        # as genai.configure/GenerativeModel in src/llm/fallback.py) — cast at
        # this boundary rather than let Unknown propagate downstream.
        models = cast(
            "list[object]",
            genai.list_models(),  # pyright: ignore[reportPrivateImportUsage]
        )
    except Exception as exc:
        log.warning({"event": "gemini_anneal_list_models_failed", "error": str(exc)[:200]})
        return None
    flash_ids: list[str] = []
    for m in models:
        name = str(getattr(m, "name", "") or "")
        short = name.rsplit("/", 1)[-1]
        if not re.match(r"^gemini-[\d.]+-flash", short):
            continue
        methods: list[object] = getattr(m, "supported_generation_methods", None) or []
        if "generateContent" in methods:
            flash_ids.append(short)
    if not flash_ids:
        log.warning({"event": "gemini_anneal_no_flash_models_found"})
        return None

    # Rank by generation number (3 > 2.5 > 2 …); longer id breaks ties (more
    # specific preview alias preferred over bare stable when both are listed).
    def _rank(mid: str) -> tuple[float, int]:
        m = re.match(r"gemini-([\d.]+)-flash", mid)
        return (float(m.group(1)) if m else 0.0, len(mid))

    best = max(set(flash_ids), key=_rank)
    log.info(
        {"event": "gemini_anneal_discovered_model", "model": best, "all": sorted(set(flash_ids))}
    )
    return best


def _anneal_models(broken: str) -> list[str]:
    """Build the ordered fallback sequence after `broken` returns NotFound.

    1. Query the live API model catalog for the current Flash model id —
       covers the common case where a preview alias is renamed (e.g.
       -preview-05-20 → -preview-06-20) without a code change.
    2. Always append _GEMINI_FAST_MODEL_FALLBACK (stable GA) as last resort.

    Sets _effective_fast_model to the first candidate so all subsequent
    in-process calls skip the broken alias immediately.
    """
    global _effective_fast_model
    candidates: list[str] = []
    discovered = _discover_api_flash_model()
    if discovered and discovered != broken and discovered != _GEMINI_FAST_MODEL_FALLBACK:
        candidates.append(discovered)
    candidates.append(_GEMINI_FAST_MODEL_FALLBACK)
    _effective_fast_model = candidates[0]
    log.warning(
        {
            "event": "gemini_fast_model_annealing",
            "broken": broken,
            "sequence": candidates,
        }
    )
    return candidates


def _verify_gemini_setup_once() -> None:
    """Resolve and cache the Gemini API key.

    Setup errors are deterministic and operator-actionable (LLMSetupError;
    see llm.cli.is_hard_stop) — mirrors the Claude path's missing-binary
    check. The key is NOT validated against the API here (that costs a call);
    an invalid/revoked key surfaces from the first real call's exception via
    _classify_gemini_failure instead.
    """
    global _gemini_setup_verified, _gemini_api_key
    if _gemini_setup_verified:
        return
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise llm_cli.LLMSetupError(f"No Gemini API key configured. {GEMINI_API_KEY_HINT}")
    _gemini_api_key = api_key
    _gemini_setup_verified = True


def usage_meta_from_response(
    usage: dict[str, object] | None, *, model: str | None = None
) -> dict[str, object]:
    """Gemini ``usage_metadata`` → the claude-shaped usage meta the shared
    ledger writer (llm.ledger.record_llm_call → usage_from_json_meta) consumes.

    Token mapping: ``prompt_token_count`` → input_tokens, ``candidates_token_count``
    → output_tokens, ``cached_content_token_count`` → cache_read_input_tokens.
    Cost is computed from the public API list price for ``model`` (pass the
    resolved model id from the caller). Unknown or absent model → 0.0 (cost
    unknown, not free). Defensive throughout: junk/missing fields yield zero
    counts, never a raise.
    """
    u = usage or {}
    prompt_tokens = _safe_int(u.get("prompt_token_count"))
    candidate_tokens = _safe_int(u.get("candidates_token_count"))
    cached_tokens = _safe_int(u.get("cached_content_token_count"))

    from llm.model_ladder import estimated_call_usd

    return {
        "usage": {
            "input_tokens": prompt_tokens,
            "output_tokens": candidate_tokens,
            "cache_read_input_tokens": cached_tokens,
        },
        "total_cost_usd": estimated_call_usd(model, prompt_tokens, candidate_tokens),
    }


def _safe_int(v: object) -> int:
    """One numeric field, defensively (bool is an int subclass)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v)


def _classify_gemini_failure(exc: Exception) -> None:
    """Promote an auth-shaped API failure to LLMSetupError (hard stop).

    A missing/invalid/revoked API key is deterministic and operator-actionable
    — every retry fails identically until someone rotates the key — so it must
    classify as setup (propagate loudly), not transient (degrade + retry),
    exactly like a missing binary on the Claude path. Non-auth failures
    (quota, timeout, service errors, genuinely malformed requests) return None
    and stay on the operational path so the caller's failure policy (degrade
    to Claude, or raise for a forced backend="gemini" call) applies.
    """
    if isinstance(exc, (google_exceptions.Unauthenticated, google_exceptions.PermissionDenied)):
        raise llm_cli.LLMSetupError(
            f"Gemini API key rejected ({type(exc).__name__}). {GEMINI_API_KEY_HINT}\n"
            f"API error: {str(exc)[:300]}"
        ) from exc
    if isinstance(exc, google_exceptions.InvalidArgument) and any(
        marker in str(exc) for marker in _AUTH_ERROR_MARKERS
    ):
        raise llm_cli.LLMSetupError(
            f"Gemini API key invalid. {GEMINI_API_KEY_HINT}\nAPI error: {str(exc)[:300]}"
        ) from exc


def call_gemini(
    prompt: str,
    model: str | None = None,
    timeout_seconds: int | None = None,
    *,
    purpose: str | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    force_budget_bypass: bool = False,
) -> str:
    """Single-shot Gemini Developer API call. Same contract as
    ``llm.cli._call_claude``: per-purpose budget gate up front, one
    best-effort ``llm_calls`` ledger row per attempt (success or failure;
    rows are distinguishable by the ``gemini-`` model prefix with
    fallback_used NULL — fallback-path rows carry fallback_used='gemini').

    No fallback wiring lives here: routing policy (when a Gemini failure
    falls back to Claude) belongs to ``call_llm``. Raises:
      * LLMBudgetExceeded — hard per-purpose cap (propagate; budget gate).
      * LLMSetupError — API key missing or rejected by the API.
      * RuntimeError / ValueError / google.api_core.exceptions.GoogleAPIError —
        operational failures the caller may degrade or reroute.
    """
    llm_cli._enforce_budget_pre_call(purpose, force_budget_bypass=force_budget_bypass)
    _verify_gemini_setup_once()  # setup errors propagate
    assert _gemini_api_key is not None  # set by _verify_gemini_setup_once
    genai.configure(api_key=_gemini_api_key)  # pyright: ignore[reportPrivateImportUsage]
    resolved_model = model or gemini_model_for(purpose)
    resolved_timeout = timeout_seconds or GEMINI_BACKEND_TIMEOUT_SECONDS
    log.info(
        {
            "event": "gemini_backend_call_start",
            "model": resolved_model,
            "prompt_chars": len(prompt),
            "purpose": purpose,
        }
    )

    from llm_call_ledger import sha256_text

    prompt_sha = sha256_text(prompt)

    # Self-annealing retry: _models starts as [resolved_model]. On a
    # NotFound (preview alias expired / rotated), _anneal_models queries the
    # live API catalog for the current Flash model id, then appends
    # [discovered, stable-fallback] so the loop retries in order. Every
    # attempt writes its own ledger row so failures are fully auditable.
    _models: list[str] = [resolved_model]
    _annealed = False
    for _attempt, _try_model in enumerate(_models):
        started_at = datetime.now(UTC)
        t0 = time.monotonic()
        try:
            model_obj = genai.GenerativeModel(_try_model)  # pyright: ignore[reportPrivateImportUsage]
            response = model_obj.generate_content(
                prompt, request_options={"timeout": resolved_timeout}
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            text = (response.text or "").strip() if hasattr(response, "text") else ""
            if not text:
                raise RuntimeError(f"Gemini API returned an empty response for model {_try_model}.")
            log.info({"event": "gemini_backend_call_done", "response_chars": len(text)})
            usage_obj = getattr(response, "usage_metadata", None)
            usage_dict: dict[str, object] = {
                "prompt_token_count": getattr(usage_obj, "prompt_token_count", 0),
                "candidates_token_count": getattr(usage_obj, "candidates_token_count", 0),
                "cached_content_token_count": getattr(usage_obj, "cached_content_token_count", 0),
            }
            record_llm_call(
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                model=_try_model,
                prompt_sha=prompt_sha,
                prompt_chars=len(prompt),
                purpose=purpose,
                ticker=ticker,
                scope=scope,
                run_id=run_id,
                response_text=text,
                meta=usage_meta_from_response(usage_dict, model=_try_model),
                prompt=prompt,
                provider="google",
                transport="metered_api",
                attempts=_attempt + 1,
                retries=_attempt,
            )
            return text
        except (
            google_exceptions.GoogleAPIError,
            RuntimeError,
            ValueError,
            OSError,
        ) as gemini_error:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            record_llm_call(
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                model=_try_model,
                prompt_sha=prompt_sha,
                prompt_chars=len(prompt),
                purpose=purpose,
                ticker=ticker,
                scope=scope,
                run_id=run_id,
                error=f"{type(gemini_error).__name__}: {str(gemini_error)[:500]}",
                prompt=prompt,
                provider="google",
                transport="metered_api",
                attempts=_attempt + 1,
                retries=_attempt,
                failure_class="gemini_transport",
            )
            if isinstance(gemini_error, google_exceptions.NotFound):
                if not _annealed and _try_model != _GEMINI_FAST_MODEL_FALLBACK:
                    # First NotFound: query the live catalog and build the
                    # fallback sequence.
                    _models.extend(_anneal_models(_try_model))
                    _annealed = True
                    continue
                if _attempt + 1 < len(_models):
                    # Discovered model also 404 — advance _effective_fast_model
                    # to the next candidate so future calls skip it too.
                    global _effective_fast_model
                    _effective_fast_model = _models[_attempt + 1]
                    continue
            _classify_gemini_failure(gemini_error)  # auth → LLMSetupError
            raise
    raise RuntimeError("call_gemini: model sequence exhausted")  # unreachable
