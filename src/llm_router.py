"""
src/llm_router.py
-----------------
Single LLM-call surface for the project. All synthesis, summarization, and
analysis paths route here so we have one place to control provider, throttle,
billing, and observability.

Provider chain (in priority order):
  1. **Claude Code CLI subprocess** — bills against the user's Pro/Max
     subscription via the canonical wrapper at
     `~/.gemini/snippets/claude_cli.py`. Preferred path; no per-call throttle.
  2. **Gemini API** — fallback when Claude is unavailable (CLI not installed,
     subscription auth not set, transient subprocess failure). Self-throttled
     to GEMINI_MIN_INTERVAL_SEC between calls so the free-tier 15-RPM cap is
     respected without the caller doing anything.

Public API:
  `call_llm(prompt: str) -> str`

Behaviour:
  - First call probes Claude. If setup is missing (no PATH entry, or
    ANTHROPIC_API_KEY is set, or the snippets module isn't importable), the
    router marks Claude unavailable for the session and routes everything to
    Gemini for the rest of the run — no repeated probing.
  - Transient Claude failures (subprocess timeout, empty stdout, etc.) fall
    back to Gemini for that ONE call, then re-try Claude on the next call.
  - If both providers fail, RuntimeError surfaces to the caller.

Why a single entry point:
  - Lets `src/llm_client.py` (the prompt library) stay provider-agnostic.
  - Centralises the 30-second-sleep tax that the Gemini-only path was
    paying inside `src/main.py` — no caller-side throttle needed.
  - Future provider swaps (Anthropic API tier, Groq, etc.) require zero
    changes outside this file.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Discovery: bring the canonical Claude CLI wrapper onto the import path.
# Per the global CLAUDE.md, it lives at ~/.gemini/snippets/claude_cli.py.
# ---------------------------------------------------------------------------

_SNIPPETS_DIR = Path.home() / ".gemini" / "snippets"
if _SNIPPETS_DIR.is_dir() and str(_SNIPPETS_DIR) not in sys.path:
    sys.path.insert(0, str(_SNIPPETS_DIR))

# Throttle for the Gemini fallback path. Free tier is 15 RPM; 4 s/call is the
# minimum safe interval. Claude CLI is not throttled.
GEMINI_MIN_INTERVAL_SEC = 4.0
GEMINI_MODEL_NAME = "gemini-flash-latest"

# Claude CLI: retry transient failures (empty stdout, subprocess timeout,
# network blip) before giving up and falling back. Each retry waits a few
# seconds — usually the second attempt works.
CLAUDE_MAX_ATTEMPTS = 3
CLAUDE_RETRY_BACKOFF_SEC = 5.0

# Web-search-enabled call: longer timeout because Claude may run multiple
# fetches before returning, and tool-allowlist syntax for the CLI.
CLAUDE_WEB_TIMEOUT_SECONDS = 1800
CLAUDE_WEB_TOOLS = "WebSearch WebFetch"

# ---------------------------------------------------------------------------
# Module-level state — small + scoped to this router.
# ---------------------------------------------------------------------------

# Tri-state: None = not yet probed, True = Claude works, False = unavailable for session.
_claude_available: bool | None = None

_gemini_initialised = False
_gemini_model = None
_last_gemini_call_ts: float = 0.0


# ---------------------------------------------------------------------------
# Claude path
# ---------------------------------------------------------------------------


def _is_claude_setup_error(err: BaseException) -> bool:
    """The canonical wrapper raises RuntimeError with a specific message on
    setup problems. Distinguish those (permanent, mark unavailable) from
    transient failures (try fallback, re-probe next call)."""
    msg = str(err).lower()
    return (
        "anthropic_api_key" in msg
        or "not found in path" in msg
        or "claude code cli" in msg
    )


def _try_claude(prompt: str) -> str | None:
    """Returns Claude's response or None if unavailable / transient failure."""
    global _claude_available

    if _claude_available is False:
        return None

    # The canonical claude_cli wrapper refuses to run if ANTHROPIC_API_KEY is
    # in os.environ (it would silently route to metered API billing instead
    # of the user's subscription). The user has explicitly opted into
    # subscription billing here, so we pop the var from THIS process's env
    # only — the parent shell is untouched. The Claude subprocess inherits
    # this Python process's (now-clean) env via subprocess.run.
    if "ANTHROPIC_API_KEY" in os.environ:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        logger.info(
            "Popped ANTHROPIC_API_KEY from this process's env so Claude CLI "
            "uses subscription billing. Parent shell is unaffected."
        )

    try:
        from claude_cli import call_claude  # type: ignore[import-not-found]
    except ImportError as e:
        logger.warning(
            "claude_cli wrapper not importable from %s (%s); session-routing to Gemini.",
            _SNIPPETS_DIR, e,
        )
        _claude_available = False
        return None

    last_err: BaseException | None = None
    for attempt in range(1, CLAUDE_MAX_ATTEMPTS + 1):
        try:
            response = call_claude(prompt)
            if _claude_available is None:
                logger.info("Claude CLI verified — routing primary calls there.")
                _claude_available = True
            if attempt > 1:
                logger.info("Claude CLI succeeded on attempt %d/%d.", attempt, CLAUDE_MAX_ATTEMPTS)
            return response
        except RuntimeError as e:
            if _is_claude_setup_error(e):
                logger.warning(
                    "Claude CLI setup not ready: %s — session-routing to Gemini.", e,
                )
                _claude_available = False
                return None
            last_err = e
        except Exception as e:
            last_err = e
        # Transient — back off and retry unless this was the last attempt.
        if attempt < CLAUDE_MAX_ATTEMPTS:
            logger.warning(
                "Claude CLI transient error on attempt %d/%d (%s: %s) — retrying in %.1fs.",
                attempt, CLAUDE_MAX_ATTEMPTS,
                type(last_err).__name__, last_err, CLAUDE_RETRY_BACKOFF_SEC,
            )
            time.sleep(CLAUDE_RETRY_BACKOFF_SEC)

    logger.warning(
        "Claude CLI failed after %d attempts (last: %s: %s) — Gemini for this call only.",
        CLAUDE_MAX_ATTEMPTS, type(last_err).__name__ if last_err else "?", last_err,
    )
    return None


# ---------------------------------------------------------------------------
# Gemini fallback path
# ---------------------------------------------------------------------------


def _init_gemini() -> None:
    """Lazy-init Gemini client on first fallback call."""
    global _gemini_initialised, _gemini_model
    if _gemini_initialised:
        return

    from dotenv import load_dotenv
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "LLM call failed: Claude CLI unavailable for this call (transient or "
            "session-disabled), and GEMINI_API_KEY not set in .env so the fallback "
            "cannot run. Either fix the Claude CLI issue or set GEMINI_API_KEY."
        )

    import google.generativeai as genai
    genai.configure(api_key=api_key)
    _gemini_model = genai.GenerativeModel(GEMINI_MODEL_NAME)
    _gemini_initialised = True


def _gemini_throttle() -> None:
    """Sleep to respect the free-tier 15-RPM ceiling. Idempotent under load."""
    global _last_gemini_call_ts
    elapsed = time.time() - _last_gemini_call_ts
    if elapsed < GEMINI_MIN_INTERVAL_SEC:
        time.sleep(GEMINI_MIN_INTERVAL_SEC - elapsed)
    _last_gemini_call_ts = time.time()


def _try_gemini(prompt: str) -> str:
    _init_gemini()
    assert _gemini_model is not None
    _gemini_throttle()
    response = _gemini_model.generate_content(prompt)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned empty response")
    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def call_llm(prompt: str) -> str:
    """Single canonical LLM-call entry point.

    Tries the Claude Code CLI first (subscription billing). On any failure,
    falls back to Gemini API (with internal 4 s throttle). Raises RuntimeError
    only if BOTH providers fail.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("call_llm() requires a non-empty prompt string")

    response = _try_claude(prompt)
    if response is not None:
        return response

    return _try_gemini(prompt)


# ---------------------------------------------------------------------------
# Web-search-enabled call: Claude CLI with WebSearch + WebFetch tools.
# Falls back to plain `call_llm()` if web tools fail (Gemini fallback won't
# have web access, but at least the prompt produces output).
# ---------------------------------------------------------------------------


def _claude_subprocess_with_tools(prompt: str, tools: str, timeout: int) -> str:
    """Invoke the claude CLI with --allowedTools enabled. Mirrors the
    canonical wrapper's subprocess invariants (UTF-8, stdin prompt,
    ANTHROPIC_API_KEY popped). Used for paths that need tool access."""
    # Run the canonical wrapper's setup so we share its single source of truth
    # for "claude on PATH? api key not set?". This also pops ANTHROPIC_API_KEY
    # from os.environ if present (subscription billing preserved).
    if "ANTHROPIC_API_KEY" in os.environ:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    from claude_cli import _verify_setup_once, _claude_cli_path  # type: ignore[import-not-found]
    _verify_setup_once()
    # _claude_cli_path is a module-level global in claude_cli; re-import to grab the populated value.
    import claude_cli as _cc  # type: ignore[import-not-found]
    cli_path = _cc._claude_cli_path
    if cli_path is None:
        raise RuntimeError("Claude CLI path not resolved by canonical wrapper")

    import subprocess
    # --allowedTools accepts space- or comma-separated tool names per `claude --help`.
    cmd = [
        cli_path, "-p", "--model", _cc.DEFAULT_MODEL,
        "--allowedTools", *tools.split(),
    ]
    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=timeout,
    )
    text = result.stdout.strip()
    if not text:
        raise RuntimeError(
            f"claude -p with web tools returned empty stdout. stderr: {result.stderr.strip()}"
        )
    return text


def call_llm_with_web(prompt: str) -> str:
    """LLM call with WebSearch + WebFetch enabled (Claude CLI subscription).

    On Claude failure, falls back to plain `call_llm()` — the prompt should be
    written to remain useful even if web research isn't available (i.e. don't
    REQUIRE Claude to cite URLs; just ask it to incorporate any web research
    it can do).

    Use for memo generation, fact-finding on recent news, anything where
    the upstream context is stale and Claude needs to look something up.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("call_llm_with_web() requires a non-empty prompt string")

    # Bail fast if claude_cli isn't importable / Claude already disabled.
    if _claude_available is False:
        logger.info("Claude unavailable for session; web call falls back to call_llm() (no tools).")
        return call_llm(prompt)

    last_err: BaseException | None = None
    for attempt in range(1, CLAUDE_MAX_ATTEMPTS + 1):
        try:
            return _claude_subprocess_with_tools(
                prompt, CLAUDE_WEB_TOOLS, CLAUDE_WEB_TIMEOUT_SECONDS,
            )
        except RuntimeError as e:
            if _is_claude_setup_error(e):
                logger.warning("Claude CLI setup not ready for web call: %s — using call_llm() fallback.", e)
                break
            last_err = e
        except Exception as e:
            last_err = e
        if attempt < CLAUDE_MAX_ATTEMPTS:
            logger.warning(
                "Claude CLI web call attempt %d/%d failed (%s: %s) — retrying in %.1fs.",
                attempt, CLAUDE_MAX_ATTEMPTS,
                type(last_err).__name__, last_err, CLAUDE_RETRY_BACKOFF_SEC,
            )
            time.sleep(CLAUDE_RETRY_BACKOFF_SEC)

    logger.warning(
        "Claude CLI web call exhausted retries (last: %s: %s) — using call_llm() fallback (no web).",
        type(last_err).__name__ if last_err else "?", last_err,
    )
    return call_llm(prompt)


def active_provider() -> str:
    """Best-effort string for logging: which provider would the next call use?"""
    if _claude_available is True:
        return "claude_cli"
    if _claude_available is False:
        return "gemini"
    return "unknown_will_probe_claude"


# ---------------------------------------------------------------------------
# CLI test entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m llm_router <prompt>", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    out = call_llm(" ".join(sys.argv[1:]))
    print(out)
    print(f"\n[provider used: {active_provider()}]", file=sys.stderr)
