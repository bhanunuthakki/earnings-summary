"""Codex (ChatGPT-membership) backend — a THIRD judge family for llmops
(LLM Quality Program P4, directives/llm_quality_program_2026_07.md).

Why this exists. Every judged verdict on this platform is currently produced
by Claude judging Claude: the Gemini key is invalid (measured 2026-07-25, a
dual-judge backtest returned JUDGE_DEGRADED on 4/8 errored judgments), so the
"dual judge" is effectively single-family. The bias literature is unambiguous
that a judge scores its own family's outputs materially higher, which makes
same-family verdicts the weakest evidence in the whole loop — and the loop's
verdicts are what auto-apply prompt and model changes.

This routes judge calls to the owner's ChatGPT-membership Codex CLI via the
canonical wrapper at ``C:\\Users\\Bhanu\\.gemini\\snippets\\codex_cli.py``,
per the machine rulebook: membership billing, never the metered OpenAI SDK.
The wrapper refuses to run if ``OPENAI_API_KEY``/``CODEX_API_KEY`` is set,
which is the guard that keeps this off metered billing.

Operational notes learned wiring it up (2026-07-25):

* the wrapper verifies ``codex login status`` with a 30s timeout; a COLD CLI
  start exceeds that and raises "authentication could not be verified" even
  though the login is valid. That is a transient setup failure, not a
  credentials problem, so it is classified as such and the judge degrades
  rather than aborting a whole sweep;
* the CLI reports no token usage in its exec events, so ledger rows carry
  latency + response size with NULL token/cost fields. Membership billing has
  no per-call price anyway; what matters is that the call is ATTRIBUTED.
"""

from __future__ import annotations

import importlib
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from log_redact import redact

log = logging.getLogger(__name__)

CODEX = "codex"

# The canonical subscription wrapper (global CLAUDE.md). Importing by path
# keeps this repo from vendoring a copy that could drift from the machine's.
_SNIPPETS_DIR = Path(r"C:\Users\Bhanu\.gemini\snippets")

# Judge default. The wrapper's own DEFAULT_MODEL is authoritative for general
# use; the judge pins a value so a wrapper-side default change cannot silently
# alter what produced a verdict.
CODEX_JUDGE_MODEL = "gpt-5.6-terra"

# Cold-start tolerant: the wrapper's login check alone can take ~30s.
DEFAULT_TIMEOUT_SECONDS = 420


class _CodexCaller(Protocol):
    def __call__(self, prompt: str, *, model: str, timeout_seconds: int) -> _CodexResult: ...


class _CodexUsage(Protocol):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


class _CodexResult(Protocol):
    text: str
    usage: _CodexUsage


def _load_wrapper() -> _CodexCaller:
    """Import ``call_codex_with_usage`` from the machine's snippets dir."""
    if str(_SNIPPETS_DIR) not in sys.path:
        sys.path.insert(0, str(_SNIPPETS_DIR))
    # Resolved at RUNTIME from the machine's snippets dir (outside this repo),
    # so the type checker cannot see it — deliberate, not a missing dep.
    module = importlib.import_module("codex_cli")
    caller = module.call_codex_with_usage
    return cast("_CodexCaller", caller)


def codex_available() -> bool:
    """True when the wrapper imports and membership auth verifies. Never
    raises — callers use it to decide whether to include Codex in a pool."""
    try:
        _load_wrapper()
    except Exception as exc:
        log.info({"event": "codex_backend_unavailable", "reason": str(exc)[:200]})
        return False
    try:
        module = importlib.import_module("codex_cli")
        verify = module._verify_setup_once
        verify()
    except Exception as exc:
        log.info({"event": "codex_backend_auth_unverified", "reason": str(exc)[:200]})
        return False
    return True


def call_codex_llm(
    prompt: str,
    *,
    purpose: str | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    model: str = CODEX_JUDGE_MODEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """One Codex call with a ledger row. Raises on failure (the caller's
    judge wrapper records it as a judge error — infra, never a score)."""
    from llm.ledger import record_llm_call

    started_at = datetime.now(UTC).replace(tzinfo=None)
    t0 = time.monotonic()
    try:
        caller = _load_wrapper()
        result = caller(prompt, model=model, timeout_seconds=timeout_seconds)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        from llm_call_ledger import sha256_text

        record_llm_call(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=f"codex:{model}",
            prompt_sha=sha256_text(prompt),
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            error=f"[codex] {type(exc).__name__}: {redact(exc)[:400]}",
            prompt=prompt,
        )
        raise
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    text = result.text.strip()
    from llm_call_ledger import sha256_text

    meta: dict[str, object] = {
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "cache_read_input_tokens": result.usage.cached_input_tokens,
            "output_tokens": result.usage.output_tokens,
        }
    }
    record_llm_call(
        started_at=started_at,
        elapsed_ms=elapsed_ms,
        model=f"codex:{model}",
        prompt_sha=sha256_text(prompt),
        prompt_chars=len(prompt),
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        response_text=text,
        meta=meta,
        prompt=prompt,
    )
    if not text:
        raise RuntimeError("Codex returned an empty response")
    return text
