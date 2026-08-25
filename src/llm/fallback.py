"""
src/llm/fallback.py
-------------------
Gemini fallback policy + routing. Invoked by the Claude CLI path when the
primary subprocess call fails operationally (timeout, non-zero exit, empty
stdout, malformed JSON envelope, binary missing mid-run).

Public API:
    GEMINI_FALLBACK_MODEL — model id used for the fallback call.
    is_fallback_disabled() — True when LLM_FALLBACK_DISABLED=1 in the env.
    try_gemini_fallback(prompt, claude_error) -> str

Extracted from src/llm_client.py during the llm subpackage split (PURE
refactor — zero behavior change).
"""

from __future__ import annotations

import logging
import os

from llm.resolver import CapabilityProfile
from log_redact import redact

log = logging.getLogger(__name__)


# Gemini fallback model. Single Flash variant for both heavy (thesis tracker)
# and light (intake classifier) workloads — quality is good enough for backup
# duty and per-call cost is sub-penny. Override per-call by passing a custom
# model to try_gemini_fallback if needed.
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"

# Cap the fallback call so a hung Gemini request can't block an unattended
# pipeline (e.g. a scheduled job) forever. Override via GEMINI_TIMEOUT_S; 120s default.
GEMINI_REQUEST_TIMEOUT_S = float(os.environ.get("GEMINI_TIMEOUT_S", "120"))


def _stderr_tail(exc: BaseException, limit: int = 400) -> str:
    """Best-effort tail of a failed subprocess's captured stderr (falls back
    to stdout). ``str(subprocess.CalledProcessError)`` drops both, so the
    RuntimeErrors below used to name only "returned non-zero exit status N"
    with no hint of WHY (quota exhaustion, auth, network). Duplicated from
    ``llm.cli._stderr_tail`` rather than imported — importing ``llm.cli``
    here would cycle back through ``llm.ledger`` -> ``llm.fallback``."""
    stderr = str(getattr(exc, "stderr", None) or "").strip()
    if stderr:
        return stderr[-limit:]
    stdout = str(getattr(exc, "stdout", None) or "").strip()
    return stdout[-limit:] if stdout else ""


def _describe(claude_error: Exception, limit: int = 300) -> str:
    """``{type}: {message}`` plus any captured stderr/stdout tail — the
    single place the three RuntimeErrors below render the Claude root cause."""
    base = redact(f"{type(claude_error).__name__}: {str(claude_error)[:limit]}")
    tail = redact(_stderr_tail(claude_error))
    return f"{base} | output: {tail}" if tail else base


def fallback_available() -> bool:
    """True only when a Gemini attempt would ACTUALLY fire: fallback not
    disabled AND a key is configured. The single gate callers use to decide
    whether the fallback path exists — checking ``is_fallback_disabled`` alone
    misses the no-key case, and both non-attempt cases must behave identically
    (raise the Claude cause; write NO Gemini ledger row)."""
    if is_fallback_disabled():
        return False
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def is_fallback_disabled() -> bool:
    """Returns True when the operator has explicitly disabled the Gemini
    fallback via ``LLM_FALLBACK_DISABLED=1`` in the environment.

    Use case: the user has an invalid GEMINI_API_KEY they don't want to
    delete (so the multi-provider architecture stays documented + revivable)
    but they don't want the system to waste time / log errors trying it on
    every Claude failure. Setting LLM_FALLBACK_DISABLED=1 short-circuits the
    fallback path: a Claude failure propagates as a clean RuntimeError that
    names the Claude root cause, without a misleading Gemini auth error
    attached.

    To re-enable: unset the env var (or set it to 0/false), refresh the key.
    """
    v = (os.environ.get("LLM_FALLBACK_DISABLED") or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def try_gemini_fallback(
    prompt: str,
    claude_error: Exception,
    *,
    capability_profile: CapabilityProfile | None = None,
) -> str:
    """
    Last-resort Gemini call invoked when the primary subscription CLI fails operationally.
    Delegates to ``llm.gemini_backend.call_gemini`` using central model resolution.
    """
    if is_fallback_disabled():
        log.info(
            {
                "event": "cli_failed_fallback_disabled",
                "claude_error": redact(f"{type(claude_error).__name__}: {str(claude_error)[:200]}"),
                "fallback_state": "disabled_by_env",
            }
        )
        raise RuntimeError(
            f"Primary CLI failed and Gemini fallback is disabled "
            f"(LLM_FALLBACK_DISABLED=1).\n"
            f"Primary error: {_describe(claude_error)}"
        ) from claude_error

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Primary CLI failed AND no Gemini fallback configured. "
            f"Original error: {_describe(claude_error)}\n"
            "Add GEMINI_API_KEY=<your-key> to .env to enable the fallback path "
            "(or set LLM_FALLBACK_DISABLED=1 to explicitly opt out)."
        ) from claude_error

    from llm.gemini_backend import call_gemini
    from llm.resolver import resolve_model_and_backend

    effective_profile = capability_profile or CapabilityProfile()
    model, _ = resolve_model_and_backend(
        purpose=None,
        model=GEMINI_FALLBACK_MODEL,
        capability_profile=effective_profile,
    )
    log.warning(
        {
            "event": "primary_cli_failed_falling_back_to_gemini",
            "claude_error": redact(f"{type(claude_error).__name__}: {str(claude_error)[:200]}"),
            "gemini_model": model,
        }
    )
    try:
        return call_gemini(
            prompt,
            model=model,
            timeout_seconds=int(GEMINI_REQUEST_TIMEOUT_S),
            capability_profile=effective_profile,
        )
    except Exception as gemini_err:
        raise RuntimeError(
            "Both LLMs failed: Primary CLI errored AND Gemini fallback call failed.\n"
            f"Gemini error: {redact(f'{type(gemini_err).__name__}: {str(gemini_err)[:200]}')}\n"
            f"Original error: {_describe(claude_error)}"
        ) from claude_error
