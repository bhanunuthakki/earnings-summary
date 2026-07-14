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

# NOTE: `google.generativeai` is deprecated as of late 2025; Google's path forward
# is `google-genai` with a different API (genai.Client / client.models.generate_content).
# Migrate when convenient — the deprecated package still works and avoids forcing
# users to re-install for the fallback path. See:
# https://github.com/google-gemini/deprecated-generative-ai-python
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)  # silence deprecation noise at import
    import google.generativeai as genai

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
    base = f"{type(claude_error).__name__}: {str(claude_error)[:limit]}"
    tail = _stderr_tail(claude_error)
    return f"{base} | output: {tail}" if tail else base


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


def try_gemini_fallback(prompt: str, claude_error: Exception) -> str:
    """
    Last-resort Gemini call invoked when the Claude CLI fails operationally
    (timeout, non-zero exit, empty stdout, binary missing). Reads GEMINI_API_KEY
    (or GOOGLE_API_KEY) from the environment — populated by load_dotenv() at
    module init.

    Three exit paths:
      1. ``LLM_FALLBACK_DISABLED=1`` — fallback explicitly disabled by operator.
         Raises a clean RuntimeError naming only the Claude failure (no
         misleading Gemini error attached). Use this when the Gemini key is
         known-invalid or fallback is unwanted for any reason.
      2. No key configured at all — same RuntimeError but with setup hint.
      3. Key configured + fallback enabled — fire the Gemini call. If Gemini
         itself errors (bad key, quota, etc.), the exception propagates and
         the caller's ledger writer records it.

    Setup-class Claude errors (``claude`` binary missing) are NOT routed here —
    they propagate from ``_verify_setup_once()`` before the subprocess call
    ever runs.
    """
    if is_fallback_disabled():
        log.info(
            {
                "event": "claude_cli_failed_fallback_disabled",
                "claude_error": f"{type(claude_error).__name__}: {str(claude_error)[:200]}",
                "fallback_state": "disabled_by_env",
            }
        )
        raise RuntimeError(
            f"Claude CLI failed and Gemini fallback is disabled "
            f"(LLM_FALLBACK_DISABLED=1).\n"
            f"Claude error: {_describe(claude_error)}"
        ) from claude_error

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Claude CLI failed AND no Gemini fallback configured. "
            f"Original Claude error: {_describe(claude_error)}\n"
            "Add GEMINI_API_KEY=<your-key> to .env to enable the fallback path "
            "(or set LLM_FALLBACK_DISABLED=1 to explicitly opt out)."
        ) from claude_error

    log.warning(
        {
            "event": "claude_cli_failed_falling_back_to_gemini",
            "claude_error": f"{type(claude_error).__name__}: {str(claude_error)[:200]}",
            "gemini_model": GEMINI_FALLBACK_MODEL,
        }
    )
    genai.configure(api_key=api_key)
    model_obj = genai.GenerativeModel(GEMINI_FALLBACK_MODEL)
    # request_options={"timeout": ...} is the deprecated google-generativeai
    # mechanism; on a future migration to google-genai translate this to the
    # client's request timeout. Without it a hung call blocks forever.
    response = model_obj.generate_content(
        prompt, request_options={"timeout": GEMINI_REQUEST_TIMEOUT_S}
    )
    text = (response.text or "").strip() if hasattr(response, "text") else ""
    if not text:
        raise RuntimeError(
            "Both LLMs failed: Claude CLI errored AND Gemini fallback returned empty response.\n"
            f"Claude error: {_describe(claude_error)}"
        ) from claude_error
    log.info({"event": "gemini_fallback_done", "response_chars": len(text)})
    return text
