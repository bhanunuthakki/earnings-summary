"""
src/llm_client.py
-----------------
LLM client for the earnings-summary pipeline. Two-tier execution:

  PRIMARY: Claude Code CLI (``claude -p``) via subprocess. The CLI honors
  whichever auth is configured in the environment — ``ANTHROPIC_API_KEY`` for
  metered API billing, or ``claude auth login`` for a Pro/Max subscription.

  FALLBACK: Gemini via google-generativeai. Fires automatically when the Claude
  CLI call fails (timeout, non-zero exit, empty output, binary missing).
  Per-call cost on Gemini Flash is sub-penny; the fallback prevents single-ticker
  failures from blocking batch runs.

Setup (one-time, user action required):
1. Install Claude Code CLI: see https://code.claude.com/docs/en/setup
2. Set ``ANTHROPIC_API_KEY`` in the shell / ``.env``, OR run ``claude auth login``
   for the subscription path. Either works.
3. (Optional but recommended) Add ``GEMINI_API_KEY`` to ``.env`` to enable the
   fallback path. Without it, Claude CLI failures surface as hard errors. See
   .env.example.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time

# NOTE: `google.generativeai` is deprecated as of late 2025; Google's path forward
# is `google-genai` with a different API (genai.Client / client.models.generate_content).
# Migrate when convenient — the deprecated package still works and avoids forcing
# users to re-install for the fallback path. See:
# https://github.com/google-gemini/deprecated-generative-ai-python
import warnings
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)  # silence deprecation noise at import
    import google.generativeai as genai

from dotenv import load_dotenv

# Phase 0 ledger (migration 0034). Records one row per LLM call with cost +
# token usage + cache stats. Best-effort: failures here never break the LLM
# call. Imported lazily inside _call_claude so this module stays importable in
# environments without the ledger module (e.g. unrelated scratch scripts).

# Load .env at module init so GEMINI_API_KEY is available without callers having to
# import dotenv themselves. Silent no-op if .env doesn't exist.
load_dotenv()

# Staleness threshold (days). When the most-recent evidence in the corpus is
# older than this vs. the report date, the tracker switches to STALE-CORPUS
# mode (different scorecard columns + conviction cap).
STALE_CORPUS_THRESHOLD_DAYS = 120

# Default Claude model for prompt calls. Sonnet 4.6 chosen as a balance of
# quality and speed across the pipeline's tasks. Per-function overrides via
# the `model` argument on _call_claude or by adding the purpose to LLM_MODELS.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Fast classifier model — used for short, structured calls (intake doc-type
# classification, transcript metadata extraction, batch extractors) where
# Sonnet would be overkill. Haiku 4.5 returns ~5x faster at materially the
# same quality on narrowly-scoped JSON-output tasks.
FAST_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

# Per-purpose model selection. Every public generator below should resolve its
# model via _model_for(purpose) so retuning one section doesn't require touching
# the call site. Keys are stable strings; values are model identifiers Claude
# CLI accepts. Adding a new entry here is the only change needed to retune a
# section's quality/latency tradeoff.
#
# Rationale per entry:
#   sonnet (default): long-context analysis where reasoning matters
#       (transcript summary, thesis tracker, bear case, strategic compare).
#   haiku (FAST_CLASSIFIER_MODEL): short, structured, often-batched calls
#       where latency dominates and the task is narrowly scoped
#       (intake classification, per-line entity extraction).
LLM_MODELS: dict[str, str] = {
    # Long-context analytical writing
    "transcript_summary": DEFAULT_MODEL,
    "press_release_summary": DEFAULT_MODEL,
    "presentation_brief": DEFAULT_MODEL,
    "pairwise_analysis": DEFAULT_MODEL,
    "strategic_analysis": DEFAULT_MODEL,
    "thesis_pass_a": DEFAULT_MODEL,
    "thesis_pass_b": DEFAULT_MODEL,
    "bear_case": DEFAULT_MODEL,
    "event_brief": DEFAULT_MODEL,
    # Investor-deck extraction: long-context structured-output. Decks run
    # ~30-60 pages of dense slide content; Sonnet's reasoning is needed to
    # distinguish forward-looking commitments from historical recap and to
    # bucket each target into the right `target_kind` enum value. Haiku
    # under-counted on this task in scratch experimentation.
    "investor_deck_extraction": DEFAULT_MODEL,
    # Company description is the analytical spine of the memo — Opus follows
    # nuanced instruction-following ("don't write Wikipedia-style", "anchor
    # on thesis pillars") far better than Sonnet on this kind of writeup
    # where the model has a strong prior toward corporate boilerplate.
    "company_description": "claude-opus-4-7",
    # Platform diagram is a narrowly-scoped JSON-output task (one diagram
    # string + one caption string). Sonnet was taking 6-20 min per call and
    # timing out on long 10-Ks; Haiku produces the same shape ~5x faster.
    "platform_diagram": FAST_CLASSIFIER_MODEL,
    "qa_topics": DEFAULT_MODEL,
    "saydo_filter": DEFAULT_MODEL,
    # Valuation multiple selection is a sector/business-model judgment that
    # benefits from Opus's wider sector knowledge (knowing P/TBV is the right
    # bank lens, EV/NTM Revenue for SaaS, P/E for cyclicals, etc.). One call
    # per ticker, cached on disk — cost is bounded.
    "valuation_basis": "claude-opus-4-7",
    # SayDo importance ordering — judgmental sort across many commitments,
    # benefits from Opus's stronger ranking discipline.
    "saydo_importance": "claude-opus-4-7",
    # Short, structured, batch — Haiku for latency
    "intake_classifier": FAST_CLASSIFIER_MODEL,
    "transcript_metadata": FAST_CLASSIFIER_MODEL,
    "market_signals": FAST_CLASSIFIER_MODEL,
    "patent_timeline": FAST_CLASSIFIER_MODEL,
}


def _model_for(purpose: str) -> str:
    """Resolve a purpose key to a model id. Unknown purposes fall back to
    DEFAULT_MODEL so a missing entry doesn't crash the pipeline — but the
    fallback is logged so the gap surfaces in observability."""
    model = LLM_MODELS.get(purpose)
    if model is None:
        log.warning(
            {
                "event": "llm_model_purpose_unknown",
                "purpose": purpose,
                "fallback": DEFAULT_MODEL,
            }
        )
        return DEFAULT_MODEL
    return model


# Default per-call timeout (seconds). Long-context thesis prompts can take
# a few minutes on Sonnet; the cap protects against runaway hangs. 20 min
# leaves headroom for the heaviest cases (4-quarter ticker × dense schema)
# while still catching CLI hangs in a reasonable wall time.
DEFAULT_TIMEOUT_SECONDS = 1200

# Schema fields stripped from the LLM prompt — they are audit-trail metadata
# meant for the human reviewer (why a schema was edited, when it was last
# revised) and bloat the prompt without aiding the analysis. Centralized so
# both passes apply the same redaction.
SCHEMA_LLM_REDACT_FIELDS: frozenset[str] = frozenset(
    {
        "thesis_status_note",
        "schema_revision_notes",
        "last_updated",
    }
)

# Markdown JSON-fence stripper — `claude -p` occasionally wraps structured
# JSON responses in ```json ... ``` fences even when asked not to. Used by
# functions that demand strict JSON output (classify_intake_document, etc.).
JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Intake classifier prompt budget — keep document excerpts bounded so a
# single oversized PDF doesn't blow the prompt. 6 KB matches main's prior
# Gemini-Flash budget; well under any Claude model's context.
INTAKE_TEXT_BUDGET = 6000

# Gemini fallback model. Single Flash variant for both heavy (thesis tracker)
# and light (intake classifier) workloads — quality is good enough for backup
# duty and per-call cost is sub-penny. Override per-call by passing a custom
# model to _try_gemini_fallback if needed.
GEMINI_FALLBACK_MODEL = "gemini-2.5-flash"

log = logging.getLogger(__name__)


class LLMBudgetExceeded(RuntimeError):
    """Raised by `_call_claude` when the per-purpose monthly cap is at/over
    AND the budget row has hard_block=True. Callers can catch this to
    degrade gracefully (skip the section, write a stub, queue for next
    month). Soft caps do NOT raise — they log a warning and the call
    proceeds. See src/llm_budget.py for the enforcement details.

    Attaches the failing BudgetCheck so structured callers can surface
    spend / cap / headroom without re-running the check.
    """

    def __init__(self, message: str, *, check: object | None = None) -> None:
        super().__init__(message)
        self.check = check


_setup_verified: bool = False
_claude_cli_path: str | None = None


def _verify_setup_once() -> None:
    """Resolve and cache the absolute path to the ``claude`` binary on first call.

    Windows-specific: bare ``"claude"`` fails because the npm-installed binary
    is ``claude.cmd`` and Python's subprocess doesn't apply PATHEXT to bare
    names. Cached so repeat calls in a long-running batch are free.
    """
    global _setup_verified, _claude_cli_path
    if _setup_verified:
        return
    resolved = shutil.which("claude")
    if resolved is None:
        raise RuntimeError(
            "Claude Code CLI ('claude') not found in PATH. Install it from "
            "https://code.claude.com/docs/en/setup, then either set "
            "ANTHROPIC_API_KEY in your shell / .env or run `claude auth login`."
        )
    _claude_cli_path = resolved
    _setup_verified = True


def _gemini_fallback_disabled() -> bool:
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


def _try_gemini_fallback(prompt: str, claude_error: Exception) -> str:
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
    if _gemini_fallback_disabled():
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
            f"Claude error: {type(claude_error).__name__}: {str(claude_error)[:300]}"
        ) from claude_error

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Claude CLI failed AND no Gemini fallback configured. "
            f"Original Claude error: {type(claude_error).__name__}: {str(claude_error)[:300]}\n"
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
    response = model_obj.generate_content(prompt)
    text = (response.text or "").strip() if hasattr(response, "text") else ""
    if not text:
        raise RuntimeError(
            "Both LLMs failed: Claude CLI errored AND Gemini fallback returned empty response.\n"
            f"Claude error: {type(claude_error).__name__}: {str(claude_error)[:200]}"
        ) from claude_error
    log.info({"event": "gemini_fallback_done", "response_chars": len(text)})
    return text


def _record_to_ledger(
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
) -> None:
    """Best-effort write of one row into llm_calls. Never raises.

    On Claude success: pass the response_text + parsed CLI meta so usage/cost
    fields populate. On failure: pass error= and leave response/meta None — the
    ledger row still records the attempt and its latency. The fallback path
    records a SECOND row with fallback_used='gemini'.
    """
    try:
        from llm_call_ledger import (
            LlmCallRecord,
            record_call,
            sha256_text,
            usage_from_json_meta,
        )

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
                cache_read_input_tokens=cast(
                    "int | None", usage.get("cache_read_input_tokens")
                ),
                output_tokens=cast("int | None", usage.get("output_tokens")),
                cost_estimate_usd=cast("float | None", usage.get("cost_estimate_usd")),
                fallback_used=fallback_used,
                error=error,
            )
        )
    except Exception as exc:  # ImportError, unexpected attribute errors, …
        # Best-effort — the ledger module's record_call already swallows DB
        # errors; this outer guard catches anything more exotic so the LLM call
        # itself is never blocked by telemetry.
        log.debug({"event": "llm_call_ledger_record_failed", "error": str(exc)})


def _enforce_budget_pre_call(
    purpose: str | None, *, force_budget_bypass: bool
) -> None:
    """Pre-call hook: consult llm_budget.check_budget for `purpose` and:

      * raise LLMBudgetExceeded when over a hard-block cap,
      * log a warning + proceed when over a soft cap,
      * log a warning + record a one-shot alert at the 80% threshold.

    Best-effort throughout — any unexpected error in the budget module
    is swallowed (we'd rather over-spend by one call than block the
    pipeline because of a budget bug). `force_budget_bypass=True` skips
    the check entirely for CLI tools that need to override.
    """
    if force_budget_bypass or purpose is None:
        return
    try:
        from llm_budget import check_budget, record_alert

        check = check_budget(purpose)
    except Exception as exc:
        log.debug(
            {
                "event": "llm_budget_check_skipped",
                "purpose": purpose,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return
    if not check.allowed:
        if check.hard_block:
            log.warning(
                {
                    "event": "llm_budget_hard_block",
                    "purpose": purpose,
                    "spend_usd": str(check.current_spend),
                    "cap_usd": str(check.cap),
                    "reason": check.reason,
                }
            )
            raise LLMBudgetExceeded(
                check.reason or f"{purpose}: monthly cap exceeded", check=check
            )
        log.warning(
            {
                "event": "llm_budget_soft_cap_exceeded",
                "purpose": purpose,
                "spend_usd": str(check.current_spend),
                "cap_usd": str(check.cap),
                "reason": check.reason,
            }
        )
        try:
            record_alert(purpose, 1.0, check.current_spend)
        except Exception:
            pass
    if check.warn:
        log.warning(
            {
                "event": "llm_budget_warn_threshold",
                "purpose": purpose,
                "spend_usd": str(check.current_spend),
                "cap_usd": str(check.cap),
                "headroom_pct": check.headroom_pct,
                "reason": check.reason,
            }
        )
        try:
            record_alert(purpose, 0.80, check.current_spend)
        except Exception:
            pass


def _call_claude(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    *,
    purpose: str | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    force_budget_bypass: bool = False,
) -> str:
    """
    Single-shot LLM call. Tries the Claude Code CLI first. On any operational
    failure — timeout, non-zero exit, empty output, malformed JSON envelope,
    or the binary becoming unavailable mid-run — falls back to Gemini Flash if
    ``GEMINI_API_KEY`` is available in the environment.

    Setup errors (``claude`` binary missing on first call) raise RuntimeError
    directly without invoking the fallback — that needs to be fixed by the
    operator, not papered over.

    Prompts are passed via stdin to avoid Windows CreateProcess command-line
    length limits (32K). Output is ``--output-format json`` so the wrapper can
    capture token usage + Anthropic-computed cost for the llm_calls ledger.

    The optional ``purpose``/``ticker``/``scope``/``run_id`` arguments are
    pass-through metadata for the ledger — they have no effect on the LLM call
    itself but enable cost-attribution queries downstream.

    Pre-call budget enforcement: when ``purpose`` is set, consults
    ``llm_budget.check_budget`` and raises ``LLMBudgetExceeded`` if the
    per-purpose monthly cap is at/over with hard_block=True. Pass
    ``force_budget_bypass=True`` to skip the check (CLI escape hatch).
    """
    _enforce_budget_pre_call(purpose, force_budget_bypass=force_budget_bypass)
    _verify_setup_once()  # setup errors propagate; do NOT route to fallback
    assert _claude_cli_path is not None  # set by _verify_setup_once when it returns successfully
    log.info(
        {
            "event": "llm_call_start",
            "model": model,
            "prompt_chars": len(prompt),
            "purpose": purpose,
        }
    )

    from llm_call_ledger import parse_claude_json_output, sha256_text

    prompt_sha = sha256_text(prompt)
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            [_claude_cli_path, "-p", "--model", model, "--output-format", "json"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",  # Force UTF-8 — Windows otherwise defaults to cp1252 which dies on
            errors="replace",  # common financial-doc Unicode (U+2212 minus, en/em dashes, arrows).
            check=True,
            timeout=timeout_seconds,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        # Parse the JSON envelope. ValueError when malformed → caught below
        # and routed through the Gemini fallback (same as a CLI failure).
        text, meta = parse_claude_json_output(result.stdout.strip())
        text = text.strip()
        if not text:
            raise RuntimeError(
                f"claude -p returned empty `result`. stderr: {result.stderr.strip()[:200]}"
            )
        log.info({"event": "llm_call_done", "response_chars": len(text)})
        _record_to_ledger(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            response_text=text,
            meta=meta,
        )
        return text
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as claude_error:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _record_to_ledger(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
            error=f"{type(claude_error).__name__}: {str(claude_error)[:500]}",
        )
        # Operational failure — try Gemini fallback. _try_gemini_fallback raises
        # if no Gemini key is configured, surfacing both errors together. The
        # fallback writes its own ledger row tagged fallback_used='gemini'.
        return _try_gemini_fallback_logged(
            prompt,
            claude_error,
            prompt_sha=prompt_sha,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
        )


def _try_gemini_fallback_logged(
    prompt: str,
    claude_error: Exception,
    *,
    prompt_sha: str,
    purpose: str | None,
    ticker: str | None,
    scope: str | None,
    run_id: str | None,
) -> str:
    """Wrap _try_gemini_fallback with its own ledger row.

    Gemini's google-generativeai SDK doesn't surface per-call cost/token
    counts in a stable shape, so the row records latency + response_chars
    only; usage/cost stay NULL. That's still enough to track *how often*
    fallback fires and how much latency it adds.
    """
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        text = _try_gemini_fallback(prompt, claude_error)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _record_to_ledger(
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
        )
        return text
    except Exception as gemini_err:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _record_to_ledger(
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
        )
        raise


def call_llm(
    prompt: str,
    *,
    purpose: str | None = None,
    model: str | None = None,
    timeout_seconds: int | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    force_budget_bypass: bool = False,
) -> str:
    """Public single-shot LLM call. CANONICAL entry point for ALL LLM calls in
    this repo — including from `execution/` scripts, `src/report/sections/`, and
    anywhere else that needs a Claude-then-Gemini-fallback round-trip.

    Direct use of `google.generativeai`, the `anthropic` SDK, or any other
    provider client is forbidden outside this module's fallback wiring; route
    through call_llm so retunes (model swap, timeout change, billing change,
    fallback policy) happen in one place.

    Args:
        prompt: The fully-rendered prompt text.
        purpose: Logical key for model selection (see LLM_MODELS). Required
            for new code; the explicit `model` arg overrides it when both
            are passed (escape hatch for one-off retunes during debugging).
        model: Explicit Claude model id. If neither purpose nor model is set,
            falls back to DEFAULT_MODEL with a warning log.
        timeout_seconds: Per-call timeout. None = DEFAULT_TIMEOUT_SECONDS.
        ticker: Optional ticker for ledger attribution. Set when the call is
            scoped to a single name; helps cost queries break out by ticker.
        scope: Optional analytical scope for the ledger (e.g. 'portfolio',
            'segment:cloud'). Free-form; aggregated in the spend report.
        run_id: Optional grouping key — typically a uuid4 hex per logical
            refresh (one build_artifacts invocation, one daily cron) so the
            spend report can show "this run cost $X across N calls".
        force_budget_bypass: When True, skip the per-purpose budget check
            entirely. Use sparingly — CLI tools that need to force a refresh
            past a hard cap should pass this. Soft caps log+proceed anyway,
            so this is only meaningful when the cap is hard-blocked.
    """
    if model is None:
        if purpose is None:
            log.warning({"event": "llm_call_no_purpose", "fallback": DEFAULT_MODEL})
            resolved_model = DEFAULT_MODEL
        else:
            resolved_model = _model_for(purpose)
    else:
        resolved_model = model
    return _call_claude(
        prompt,
        model=resolved_model,
        timeout_seconds=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
        purpose=purpose,
        ticker=ticker,
        scope=scope,
        run_id=run_id,
        force_budget_bypass=force_budget_bypass,
    )


# Web-search-enabled call: same subprocess as _call_claude but with the
# Claude CLI's --allowedTools flag turned on so the model can run WebSearch
# / WebFetch as part of producing its answer. Used by the memo generator
# for the "Recent Developments" section so memos cite real news URLs
# instead of leaning on a stale FMP news pre-pull.
CLAUDE_WEB_TOOLS = "WebSearch WebFetch"
CLAUDE_WEB_TIMEOUT_SECONDS = 1800  # web fetches add round-trips; bigger cap


def call_llm_with_web(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = CLAUDE_WEB_TIMEOUT_SECONDS,
    *,
    purpose: str | None = None,
    ticker: str | None = None,
    scope: str | None = None,
    run_id: str | None = None,
    force_budget_bypass: bool = False,
) -> str:
    """LLM call with Claude WebSearch + WebFetch tools enabled.

    Setup invariants are the same as `_call_claude` (subscription billing
    via the CLI, UTF-8, stdin prompt, JSON output for ledger capture). On
    Claude failure, falls through to plain `_call_claude` (which has its
    own Gemini fallback) so a memo is always produced even when web tools
    are unavailable.

    Use for memo generation, fact-finding on recent news, anything where
    the upstream context is stale and Claude needs to look something up.

    Same per-purpose budget enforcement as `_call_claude`; pass
    ``force_budget_bypass=True`` to skip the check.
    """
    _enforce_budget_pre_call(purpose, force_budget_bypass=force_budget_bypass)
    _verify_setup_once()
    assert _claude_cli_path is not None
    log.info(
        {
            "event": "llm_web_call_start",
            "model": model,
            "prompt_chars": len(prompt),
            "purpose": purpose,
        }
    )

    from llm_call_ledger import parse_claude_json_output, sha256_text

    prompt_sha = sha256_text(prompt)
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    cmd = [
        _claude_cli_path,
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--allowedTools",
        *CLAUDE_WEB_TOOLS.split(),
    ]
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=timeout_seconds,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        text, meta = parse_claude_json_output(result.stdout.strip())
        text = text.strip()
        if not text:
            raise RuntimeError(
                f"claude -p with web tools returned empty `result`. stderr: {result.stderr.strip()[:200]}"
            )
        log.info({"event": "llm_web_call_done", "response_chars": len(text)})
        _record_to_ledger(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope or "web",
            run_id=run_id,
            response_text=text,
            meta=meta,
        )
        return text
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as web_err:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _record_to_ledger(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            model=model,
            prompt_sha=prompt_sha,
            prompt_chars=len(prompt),
            purpose=purpose,
            ticker=ticker,
            scope=scope or "web",
            run_id=run_id,
            error=f"{type(web_err).__name__}: {str(web_err)[:500]}",
        )
        log.warning(
            {
                "event": "llm_web_call_fallback_to_plain",
                "error": f"{type(web_err).__name__}: {web_err}",
            }
        )
        # Fall through to non-web path so the caller still gets output. The
        # plain _call_claude path records its own ledger row(s).
        return _call_claude(
            prompt,
            model=model,
            timeout_seconds=timeout_seconds,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
        )


# ---------------------------------------------------------------------------
# Thesis / bear-case anchor blocks — shared context for analytical prompts
# ---------------------------------------------------------------------------
#
# Several prompts (per-quarter summary, pairwise SayDo, recent developments,
# SayDo filter, event brief) promise "thesis-anchored analysis" but currently
# have no thesis on hand. These helpers pull two small blocks of context that
# can be appended to those prompts so the LLM has the pillars / KPIs / break
# rules / non-consensus risks to anchor against. Both helpers are tolerant:
# missing files → empty string, so watchlist/evaluation tickers (no thesis,
# no cached bear case) still work and the prompt shape is unchanged.

# Hard cap on the assembled anchor blocks so a verbose holdings JSON cannot
# blow the prompt budget on any single prompt. Trim is deterministic
# (truncation at the section boundary), not a smart compressor.
ANCHOR_BLOCK_CHAR_CAP = 3500

_HOLDINGS_DIRNAME = ("micro_thesis", "holdings")
_BEAR_CASE_CACHE_DIRNAME = ("data", "bear_case")


def _load_holdings_json(repo_root: Path, ticker: str) -> dict[str, object] | None:
    """Read ``micro_thesis/holdings/<TICKER>.json`` defensively. Returns None
    for any read or parse failure so callers degrade to no-anchor mode."""
    path = repo_root.joinpath(*_HOLDINGS_DIRNAME) / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        return cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _kpi_anchor_lines(payload: dict[str, object]) -> list[str]:
    """Render `tier_1_kpis` as one-line bullets: name + break condition."""
    raw = payload.get("tier_1_kpis")
    if not isinstance(raw, list):
        return []
    lines: list[str] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        name = e.get("name")
        bc = e.get("break_condition")
        if not isinstance(name, str) or not name.strip():
            continue
        bc_text = bc.strip() if isinstance(bc, str) and bc.strip() else "—"
        lines.append(f"- **{name.strip()}** — breaks if {bc_text}")
    return lines


def _business_rule_anchor_lines(payload: dict[str, object]) -> list[str]:
    """Render quantitative `business_model_rules` as scannable bullets."""
    raw = payload.get("business_model_rules")
    if not isinstance(raw, list):
        return []
    lines: list[str] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        narrative = e.get("narrative")
        if isinstance(narrative, str) and narrative.strip():
            lines.append(f"- {narrative.strip()}")
    return lines


# Canonical financial line items always worth statistically profiling — the
# Week-1 time-series layer runs detect_trend + detect_inflection on these
# so the LLM sees the numerical read, not just the raw rows. Stays a small
# fixed set so the anchor block doesn't balloon; per-ticker tier-1 KPIs
# are added on top via load_kpi_series when available.
_STATS_LINE_ITEMS: tuple[str, ...] = (
    "revenue",
    "operating_income",
    "free_cash_flow",
    "net_income",
)


def _stats_block_from_series(series_label: str, series: list[object]) -> str | None:
    """One-line markdown summary of a series: direction + slope + (inflection
    when present). Returns None when the series is too short to analyze."""
    from timeseries import detect_inflection, detect_trend
    if len(series) < 4:
        return None
    trend = detect_trend(cast("list[object]", series))
    if trend.get("insufficient_data"):
        return None
    direction = str(trend.get("direction") or "?")
    slope_pct = trend.get("slope_pct_of_mean")
    slope_str = (
        f"{float(cast('float', slope_pct)) * 100:+.1f}%/q"
        if isinstance(slope_pct, (int, float))
        else "—"
    )
    sig = " (sig)" if trend.get("statistical_significance") else ""
    line = f"- **{series_label}** — {direction} · slope {slope_str}{sig}"

    # Add inflection callout when the series is long enough
    if len(series) >= 8:
        infl = detect_inflection(cast("list[object]", series))
        if infl.get("inflection_period") and float(cast("float", infl.get("magnitude") or 0)) >= 1.0:
            line += f" · inflection {infl['inflection_period']} (delta={float(cast('float', infl['magnitude'])):.1f}sd)"
    return line


def _statistical_patterns_block(
    repo_root: Path, ticker: str, payload: dict[str, object]
) -> list[str]:
    """Run detect_trend + detect_inflection on the canonical line items and
    any per-ticker registered KPIs. Returns markdown lines, empty when no
    series load (missing DB, unknown ticker)."""
    try:
        from timeseries import load_financial_series, load_kpi_series
    except ImportError:
        return []

    lines: list[str] = []
    for line_item in _STATS_LINE_ITEMS:
        try:
            s = load_financial_series(ticker=ticker, line_item=line_item, repo_root=repo_root)
        except Exception as exc:  # never block anchor build
            log.debug({"event": "stats_load_financial_failed", "ticker": ticker, "lineitem": line_item, "error": str(exc)})
            continue
        if not s:
            continue
        rendered = _stats_block_from_series(line_item.replace("_", " "), cast("list[object]", s))
        if rendered:
            lines.append(rendered)

    # Per-ticker registered KPIs (from kpi_definitions). Tier-1 KPIs from the
    # holdings JSON would be ideal here but holdings names rarely match the
    # registry verbatim — use the registry as the source of truth.
    db_path = repo_root / "data" / "portfolio.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                rows = conn.execute(
                    "SELECT name FROM kpi_definitions WHERE ticker = ? LIMIT 4",
                    (ticker.upper(),),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.debug({"event": "stats_kpi_def_lookup_failed", "error": str(exc)})
            rows = []
        for (kpi_name,) in rows:
            if not isinstance(kpi_name, str) or not kpi_name.strip():
                continue
            try:
                s_kpi = load_kpi_series(ticker=ticker, kpi_name=kpi_name, repo_root=repo_root)
            except Exception as exc:  # best-effort
                log.debug({"event": "stats_load_kpi_failed", "ticker": ticker, "kpi": kpi_name, "error": str(exc)})
                continue
            if not s_kpi:
                continue
            short_label = kpi_name if len(kpi_name) <= 60 else kpi_name[:57] + "…"
            rendered = _stats_block_from_series(short_label, cast("list[object]", s_kpi))
            if rendered:
                lines.append(rendered)

    # Reference payload so linters know it's intentional (reserved for
    # future use: cross-check tier-1 KPI names against the registry)
    _ = payload
    return lines


def load_thesis_anchor(repo_root: Path, ticker: str) -> str:
    """Compose a compact thesis anchor for prompt injection. Empty string
    when no holdings JSON exists. Output is markdown, ~300-1500 chars.

    Since the Week-1 time-series layer landed, the anchor also carries a
    "Recent statistical patterns" subsection — detect_trend + detect_inflection
    over the canonical financial line items + any per-ticker registered KPIs.
    The subsection is best-effort: DB / loader failures degrade silently so
    the original thesis anchor still renders."""
    payload = _load_holdings_json(repo_root, ticker)
    if payload is None:
        return ""

    parts: list[str] = ["## THESIS ANCHOR (analyst's own framing of this name)"]

    thesis = payload.get("thesis")
    if isinstance(thesis, str) and thesis.strip():
        parts.append(f"\n**Thesis statement:**\n{thesis.strip()}")

    key_driver = payload.get("key_driver")
    if isinstance(key_driver, str) and key_driver.strip():
        parts.append(f"\n**Key driver tracked:** {key_driver.strip()}")

    kpi_lines = _kpi_anchor_lines(payload)
    if kpi_lines:
        parts.append("\n**Tier-1 KPIs (with break conditions):**")
        parts.extend(kpi_lines)

    rule_lines = _business_rule_anchor_lines(payload)
    if rule_lines:
        parts.append("\n**Quantitative thesis-breakers:**")
        parts.extend(rule_lines)

    # Best-effort statistical block. Wrapped in a try/except so any failure
    # in the timeseries layer (DB missing, scipy import error, etc.) can't
    # break the anchor for the dozens of prompts that depend on it.
    try:
        stats_lines = _statistical_patterns_block(repo_root, ticker, payload)
    except Exception as exc:  # anchor must keep rendering
        log.debug({"event": "statistical_patterns_block_failed", "ticker": ticker, "error": str(exc)})
        stats_lines = []
    if stats_lines:
        parts.append("\n**Recent statistical patterns (last 8-16 quarters):**")
        parts.extend(stats_lines)

    if len(parts) == 1:  # only the header — no usable content
        return ""

    assembled = "\n".join(parts).strip()
    if len(assembled) > ANCHOR_BLOCK_CHAR_CAP:
        assembled = assembled[:ANCHOR_BLOCK_CHAR_CAP].rstrip() + "\n[...truncated]"
    return assembled


def load_bear_anchor(repo_root: Path, ticker: str) -> str:
    """Compose a compact bear-case anchor from the on-disk cache (written by
    the bear_case section after a successful LLM run). Returns the
    `most_underweighted` paragraph plus the top 3 failure-mode hypotheses so
    the per-quarter summary / news / SayDo can engage with the analyst's
    existing bear framing without re-running the bear case.

    Returns "" when no cache exists (no prior `--enable-llm` run) so the
    first-ever build of a ticker still works without circular dependency.
    """
    path = repo_root.joinpath(*_BEAR_CASE_CACHE_DIRNAME) / f"{ticker.upper()}.json"
    if not path.exists():
        return ""
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return ""

    parts: list[str] = ["## BEAR-CASE ANCHOR (from analyst's prior bear review)"]

    underweighted = payload.get("most_underweighted")
    if isinstance(underweighted, str) and underweighted.strip():
        parts.append(f"\n**Most underweighted by consensus:**\n{underweighted.strip()}")

    fms = payload.get("failure_modes")
    if isinstance(fms, list):
        hyps: list[str] = []
        for entry in cast("list[object]", fms)[:3]:
            if not isinstance(entry, dict):
                continue
            e = cast("dict[str, object]", entry)
            h = e.get("hypothesis")
            if isinstance(h, str) and h.strip():
                hyps.append(f"- {h.strip()}")
        if hyps:
            parts.append("\n**Named failure modes the analyst is tracking:**")
            parts.extend(hyps)

    if len(parts) == 1:
        return ""

    assembled = "\n".join(parts).strip()
    if len(assembled) > ANCHOR_BLOCK_CHAR_CAP:
        assembled = assembled[:ANCHOR_BLOCK_CHAR_CAP].rstrip() + "\n[...truncated]"
    return assembled


def compose_anchor_block(thesis_anchor: str, bear_anchor: str) -> str:
    """Join thesis + bear anchors with a separator, omitting empties.
    Returns "" when both are empty so the caller can conditionally insert."""
    blocks = [b for b in (thesis_anchor, bear_anchor) if b.strip()]
    if not blocks:
        return ""
    return "\n\n---\n\n".join(blocks) + "\n\n---\n\n"


# ---------------------------------------------------------------------------
# Prompt-bearing functions (signatures preserved — callers unchanged)
# ---------------------------------------------------------------------------


def generate_pairwise_analysis(prev_summary, curr_summary, anchor_block: str = "", ticker: str | None = None):
    """
    Generates a specific "Say-Do" analysis comparing two sequential quarters.

    ``anchor_block`` is an optional pre-formatted markdown block (typically
    from ``compose_anchor_block(load_thesis_anchor(...), load_bear_anchor(...))``)
    that anchors the §5 Thesis Impact section against the analyst's actual
    tier-1 KPIs and named bear-case failure modes. Empty string → no anchor
    block injected (back-compat for watchlist tickers).
    """
    prev_q_str = f"{prev_summary['quarter']} {prev_summary['year']}"
    curr_q_str = f"{curr_summary['quarter']} {curr_summary['year']}"

    prompt = f"""
    You are a Strategic Management Consultant and Senior Equity Analyst.
    **Task:** Perform a strict "Say-Do" analysis comparing the **Outlook/Guidance** from the Previous Quarter ({prev_q_str}) against the **Actual Results** reported in the Current Quarter ({curr_q_str}).

    **Hard rules — non-negotiable:**
    - Every numeric figure, dated event, and management quote must be traceable to the input summaries below. If something is not present, write `[not disclosed]`. Do not invent, infer, or back-fill from prior knowledge of this company.
    - Verbatim quotes belong in quotation marks with a source tag like `[Source: {prev_q_str} prepared remarks]` or `[Source: {curr_q_str} Q&A]`.
    - The Attribution call (Execution vs. Exogenous) is a judgment, so it MUST go through the adversarial loop below — no shortcut to a verdict.
    - **Bullet ordering:** Within EVERY section that has bullets (Say, Do, Gap Analysis), order bullets by THESIS IMPACT — most thesis-relevant first, immaterial line items (FX, share count, depreciation timing, tax rate) last. Bullets tied to a TIER-1 KPI from the THESIS ANCHOR (when provided) rank above all others, regardless of magnitude. If you would include a bullet that isn't thesis-relevant AT ALL, drop it instead of relegating it to the bottom.

    {anchor_block}**Input Data:**
    1.  **Previous Quarter ({prev_q_str}) Summary:**
        {prev_summary["text"]}

    2.  **Current Quarter ({curr_q_str}) Summary:**
        {curr_summary["text"]}

    **Output Format (Strict Markdown):**
    ## Analysis: {prev_q_str} vs {curr_q_str}

    ### 1. Say (The Promise — from {prev_q_str})
    *   **Guidance (quantitative):** [Specific numbers/targets, with quotes + source tags. Use `[not disclosed]` if absent.]
    *   **Strategy (qualitative):** [Key initiatives promised, quoted with source tags.]

    ### 2. Do (The Reality — reported in {curr_q_str})
    *   **Performance:** [Actuals with quotes + source tags.]
    *   **Gap Analysis:** [Specific variances vs. the Say above. State each gap as `metric: guided X → actual Y (delta Z%)`.]

    ### 3. Analyst Verdict
    *   **Performance Rating:** **MET** / **MISSED** / **EXCEEDED** (choose one — must be defensible from §2 numbers)

    ### 4. Adversarial Loop — Attribution (Execution vs. Exogenous)
    *   **Primary Thesis:** [Best read of whether the gap is Execution or Exogenous, with the strongest supporting evidence and source tags.]
    *   **Strongest Counter:** [The most credible alternative read of the same prints. Avoid generic "macro could be different" — name a specific contradicting datapoint, mix effect, or management-credibility caveat from the inputs.]
    *   **Resolution:** [Reconcile the two sides.] — **Net Conviction:** High / Medium / Low. **Observable that would flip this verdict:** [specific datapoint to watch next quarter].
    *   **Sensitivity:** [If the primary read is wrong by ±X% on the key variable, what changes about the verdict or thesis impact?]

    ### 5. Thesis Impact
    *   **Structural vs temporary:** [Must follow from §4, not asserted independently.]
    *   **Concrete delta:** [Translate the verdict into a quantified thesis impact: e.g., "raises NPV/share by ~$30 if Cloud op margin expansion sustains 200bps/q for 4 more quarters" / "compresses 2026 FCF estimate by ~$8B if capex re-rates above $185B". Show the working chain; the reader should be able to replicate.]
    *   **Tier-1 KPI implication:** [Reference the THESIS ANCHOR KPIs above by name. For each tier-1 KPI the print materially moves: state the KPI by its exact anchor name + direction (faster / slower / sideways) + how this print changes the distance to its break condition. If no anchor was provided, fall back to "tier-1 KPIs unavailable" rather than inventing KPI names.]
    *   **Bear-case engagement:** [If a BEAR-CASE ANCHOR was provided above, explicitly state which named failure mode this print confirms, refutes, or leaves unchanged. Cite the failure-mode hypothesis verbatim from the anchor so the reader can map the connection. If no bear anchor, write "no prior bear-case anchor on file" and skip — do not fabricate.]
    """

    try:
        return call_llm(prompt, purpose="pairwise_analysis", ticker=ticker)
    except Exception as e:
        log.error(f"Error generating pairwise analysis: {e}")
        return f"Could not generate analysis for {prev_q_str} -> {curr_q_str}."


def generate_summary(text: str, anchor_block: str = "", ticker: str | None = None) -> str:
    """
    Generates an analyst-grade prose summary of an earnings transcript.

    Output is a tight markdown writeup — opens with the analytical takeaway,
    surfaces what's accelerating / decelerating / structurally changing,
    flags management spin vs reality, and forecasts what the next quarter
    looks like given this print. Replaces the templated bullets+tables
    format that produced bureaucratic checklists.

    ``anchor_block`` is an optional markdown block carrying the analyst's
    thesis + tier-1 KPIs + named bear-case failure modes. When provided,
    the opening analytical takeaway and the "Next-quarter setup" section
    are explicitly framed against the anchor. Empty string → no anchor
    (back-compat for non-tracked tickers; the per-quarter narrative is
    still useful in isolation).
    """
    prompt = f"""
    You are writing the per-quarter earnings note for an analyst-grade
    research memo. The reader already has the numbers from the workspace
    renderer (a separate FMP-driven table renders below this writeup) —
    your job is the INTERPRETATION, not the data dump.

    {anchor_block}

    BAR: this should read like a senior buy-side analyst's quarterly note,
    not a Yahoo Finance recap. Think *Stock Market Nerd* per-quarter
    debriefs: opinion-bearing, specific, willing to call out spin, and
    explicitly forward-looking.

    EXPLICITLY FORBIDDEN — these earn an automatic rewrite:
    - Templated section headers like "## 1. Executive Summary" / "## 2.
      Financial Highlights" — write FLOWING PROSE with at most 3-4 H3
      subheads of your own choosing, not a checklist
    - Re-listing the headline financials (Revenue / EPS / Op Margin) — the
      workspace renders these from FMP data adjacent to your writeup
    - "Key Drivers: [text analysis of what drove the numbers]" generic
      paraphrasing — be specific about which lever moved
    - "Strategic Initiatives:" bucket — name a specific initiative, frame
      it analytically, or omit
    - Listing every product launch — only mention launches that move the
      thesis
    - Restating analyst Q&A topic by topic — the workspace shows the full
      parsed Q&A roster separately; pull only the Q&A moments that REVEAL
      something (management dodge, surprising disclosure, contentious
      pushback)

    OUTPUT FORMAT (markdown, no front-matter, no title page):

    Open with a 1-paragraph **analytical takeaway** (NOT a recap): what is
    the single most important thing this quarter, framed as it bears on
    the thesis? Lead with the verdict, then the reasoning. ~3-5 sentences.
    If a THESIS ANCHOR is provided above, the takeaway MUST name at least
    one tier-1 KPI from the anchor and state how this print moves its
    distance to break. If a BEAR-CASE ANCHOR is provided, the takeaway
    must state whether this print confirms or refutes one of the named
    failure modes (cite the failure-mode hypothesis verbatim).

    Then 3-5 H3-led prose paragraphs (use your own headers — don't pick
    from a template). Suggested directions to cover (pick what's relevant,
    skip what's not):

      ### What accelerated
      Which lines / KPIs broke trend vs prior quarter? Quantify deltas
      against the prior 2-4 quarters, not just YoY. Tie each to the
      underlying driver where management explained it.

      ### What decelerated or hit headwinds
      Same treatment for negative deltas. Distinguish secular vs cyclical
      vs one-off. If management's framing differs from the print, say so.

      ### What changed structurally
      New disclosures, mix shifts, segment redefinitions, guidance-shape
      changes. Things that change the modeling baseline, not just the
      print.

      ### What management is / isn't talking about
      Where did management lean in (and why)? What got conspicuously
      light coverage? Call out specific Q&A dodges or topic shifts vs
      the prior call.

      ### Next-quarter setup
      Concrete: what should we expect in the next print given THIS
      quarter's signals? Frame as a 1-quarter-out check on the thesis.
      When a THESIS ANCHOR is provided above, list the specific tier-1
      KPI values to watch (by their anchor names) and what reading
      would confirm or break the thesis next quarter.

    Then a final **Quotes worth keeping** subsection (optional, omit if
    nothing earns its place) — 2-4 verbatim quotes from the transcript
    that EARN inclusion (i.e., couldn't be paraphrased without losing
    signal). Format each as:
      > "verbatim quote text"
      > — Speaker Name · role

    Rules:
    - No invented numbers. Every quantitative claim must be grounded in
      the transcript or derivable from numbers IN the transcript.
    - Verbatim quotes belong in blockquote format above. Inline
      paraphrasing should not use quotation marks.
    - Skip sections that don't have signal — better a tight 4-paragraph
      note than 6 paragraphs padded with filler.
    - The writeup should be 400-900 words total. Anything shorter is
      probably under-developed; anything longer is probably padded.

    Transcript:
    """
    try:
        return call_llm(prompt + text, purpose="transcript_summary", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Summary generation failed: {e}")
        raise


def generate_press_release_summary(text: str, ticker: str | None = None) -> str:
    """
    Generates a structured summary from an earnings press release.
    Press releases are financial-forward — emphasize the numbers table and guidance.
    """
    prompt = """
You are an expert financial analyst. Summarize the following earnings press release.
This is sourced from the company's IR website, so it is the official financial release — be precise.

**STRICT CONSTRAINT:** Do not provide conversational filler. Start your response immediately with the Report Title.

**Output Format (Strict Markdown):**

# Press Release Summary: [Company Ticker] [Quarter] [Year]

## 1. Headline Results
| Metric | Reported | Guidance/Consensus | Beat/Miss |
| :--- | :--- | :--- | :--- |
| **Revenue** | [Value] | [Value if available] | [Result] |
| **EPS (GAAP)** | [Value] | [Value if available] | [Result] |
| **EPS (Non-GAAP)** | [Value] | [Value if available] | [Result] |
| **Operating Income** | [Value] | [Value if available] | [Result] |
| **Free Cash Flow** | [Value if disclosed] | N/A | N/A |

## 2. Key Business Metrics
[List 3–6 company-specific KPIs disclosed in the release (e.g. DAUs, GMV, RPO, cloud revenue, etc.)]

## 3. Guidance (Next Quarter & Full Year)
| Metric | Next Quarter | Full Year |
| :--- | :--- | :--- |
| **Revenue** | [Range] | [Range] |
| **EPS** | [Range] | [Range] |
| **Other** | [Any other metrics guided] | |

## 4. Capital Allocation
[Buybacks, dividends, debt changes disclosed in this release]

Press Release:
"""
    try:
        return call_llm(prompt + text, purpose="press_release_summary", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Press release summary generation failed: {e}")
        raise


def generate_presentation_brief(text: str, ticker: str | None = None) -> str:
    """
    Generates a strategic brief from an earnings presentation slide deck.
    Presentations are typically 20–40 pages of slides; extract the key strategic narrative.
    """
    prompt = """
You are a senior equity research analyst. The following text was extracted from an earnings presentation slide deck.
Extract the key strategic narrative — what story is management telling investors?

**STRICT CONSTRAINT:** Do not provide conversational filler. Start your response immediately with the Report Title.

**Output Format (Strict Markdown):**

# Presentation Brief: [Company Ticker] [Quarter] [Year]

## 1. Management Narrative
[2–3 sentences on the central investor story management is presenting this quarter]

## 2. Highlighted Metrics & Charts
[List key data points, KPIs, or charts that management chose to prominently feature — these signal what they want investors to focus on]

## 3. Strategic Initiatives Featured
[New products, partnerships, market expansions, or strategic pivots highlighted in the deck]

## 4. Forward-Looking Slides
[Any slides about market opportunity, TAM, roadmap, or multi-year targets]

## 5. Analyst Watchpoints
[What is management NOT showing or downplaying? Notable omissions or changed slide topics vs. prior quarters if detectable]

Presentation Text:
"""
    try:
        return call_llm(prompt + text, purpose="presentation_brief", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Presentation brief generation failed: {e}")
        raise


# ---------------------------------------------------------------------------
# Shared building blocks for the thesis-tracker prompt
# ---------------------------------------------------------------------------

# Per-document character cap when assembling quarter context. Keeps the prompt
# bounded while preserving enough text for KPI-value extraction and quote pulls.
PER_DOC_CONTEXT_CAP_CHARS = 3000

# Hard rules and Adversarial Loop format are duplicated across both passes so
# each pass is self-grounded. This block is the single source of truth.
_HARD_RULES_BLOCK = """**Hard rules — non-negotiable:**
1. The Available Evidence above is the source of truth. Do NOT introduce numbers, dates, products, or events that are not present in it. Use `[not disclosed]` for any cell where the value is not in the evidence — never guess, never back-fill from prior knowledge of the issuer.
2. Every numeric KPI value, dated event, and management quote must carry an inline source tag of the form `[Source: <doc type>, <Q# YYYY>, <speaker or section>]`. Tag at the point of claim, including inside table cells.
3. The three judgment surfaces — Thesis Status, Say-Do, and Valuation-Trigger Stress — MUST each include a fully populated Adversarial Loop. A surface that lacks a credible Strongest Counter is under-examined and should be flagged as such with Net Conviction = Low rather than papered over.
4. **Inferred figures audit-trail:** any value you computed yourself rather than reading directly from a source (e.g., Q4 standalone derived as FY minus 9M; ex-items decomposition; YoY delta) MUST carry an audit-trail source tag of the form `[Source: implied = <FY src> minus <9M src>]` or `[Source: implied = <calculation>]`. Use the doc_type marker `[implied]` so inferred figures are searchable separately from primary citations. The reader must be able to reproduce your inference.
5. **Ex-items decomposition required:** for any margin / EPS / FCF / operating-income cell where the source document discloses a one-off (tax credit, restructuring charge, gain on sale, settlement, impairment, provision reversal), report as `headline X% / underlying Y% (excluding $Zm <item> [Source: ...])`. The headline number alone misleads run-rate analysis. If no one-offs are disclosed for a cell, no decomposition is needed.
6. **Methodology consistency:** when comparing the same metric across two periods, verify methodology consistency. If the issuer disclosed a methodology change, footnote restatement, or scope expansion (different geographies, different revenue recognition, different segment definition, managerial-P&L introduction), flag the comparability gap AT the comparison cell — not buried in Analyst Notes. State the prior-method value, new-method value, and approximate delta attributable to methodology vs. underlying.
7. **Prior-period guidance reference for Say-Do:** when the corpus contains the immediately-prior quarter, treat its Outlook/Guidance section as the source of "guided X" values; treat the latest-quarter's printed actuals as "actual Y." When the corpus contains only the latest quarter, Say-Do can only be evaluated if THIS quarter's docs reference prior guidance ranges. If neither condition holds, Say-Do is un-evaluable — state this explicitly and cap Say-Do conviction at Low. Do NOT fall back to trusting management's own self-attestation phrases like "we exceeded guidance across the board."
"""

_PRINCIPLES_BLOCK = """**Investment principles — frame all verdicts and recommendations through these:**

1. **One-page thesis test.** A position only earns capital if there's a coherent paragraph
   answering: (a) what the company does, (b) why the market is mispricing it, (c) what
   specifically catalyzes the re-rating, (d) when. If the report can't generate that
   paragraph from the evidence, the verdict skews toward CUT, not HOLD.
2. **Killer variables.** Identify the 2-3 fundamental drivers that actually move the
   outcome for THIS business (not generic "macro / rates / sentiment"). Frame KPI
   verdicts and Open Questions around those, not the long tail.
3. **Invalidation triggers — fundamental, not price.** Break conditions are about
   business reality (revenue growth thresholds, competitor launches, regulatory rulings),
   never about stock price drawdowns. A 20% price decline is not, by itself, a sell signal.
4. **Sizing by conviction.** Recommendations should be tiered:
     - High conviction (clean thesis + low ambiguity + observable catalysts): up to ~8-10%
     - Standard (thesis intact, some ambiguity): ~3-5%
     - Speculative (asymmetric option, broken thesis with optionality, early stage): ~1-2%
   The Sizing call must reference Net Conviction from the Adversarial Loops, not feel.
5. **Time horizon.** A fundamental thesis is years, not months. Pre-commit to N quarters
   of holding unless an invalidation trigger fires. State the horizon explicitly.
6. **Sell discipline.** Sells are justified by exactly one of: (a) thesis fully realized
   (target valuation hit / re-rating happened), (b) a specific named invalidation trigger
   fired, (c) explicit IRR comparison shows a better opportunity. NOT: bad week, boredom,
   tax-loss harvesting at the cost of the thesis. Reflect this in the verdict framing.
"""


_ADVERSARIAL_LOOP_FORMAT_BLOCK = """**Adversarial Loop format (use these exact field names):**
- **Primary Thesis:** the asserted reading + strongest supporting evidence (with source tags)
- **Strongest Counter:** the most credible name-specific challenge — alternative read, contradicting datapoint, mix/composition effect, management-credibility caveat. Reject generic macro hand-waving.
- **Resolution:** how the two sides reconcile — **Net Conviction: High / Medium / Low**. State the specific observable that would flip the verdict next period.
- **Sensitivity:** quantified impact if the primary read is wrong by ±X% on the key variable.
"""


def _compute_staleness(
    report_date: str, corpus_latest_date: str | None
) -> tuple[int, bool, str, str]:
    """
    Returns (staleness_days, is_stale, staleness_line, staleness_directive).
    Centralized so both passes apply the same staleness regime.
    """
    if corpus_latest_date is None:
        return (
            0,
            False,
            f"Corpus staleness: unknown (no corpus_latest_date provided). Report date {report_date}.",
            "Corpus is current. Standard scorecard format applies.",
        )

    report_dt = date.fromisoformat(report_date)
    latest_dt = date.fromisoformat(corpus_latest_date)
    staleness_days = (report_dt - latest_dt).days
    is_stale = staleness_days > STALE_CORPUS_THRESHOLD_DAYS
    line = f"Corpus staleness: {staleness_days} days (latest evidence {corpus_latest_date}, report date {report_date})"

    if is_stale:
        directive = f"""**STALE-CORPUS MODE** (staleness {staleness_days}d > {STALE_CORPUS_THRESHOLD_DAYS}d threshold). Apply these adaptations:
1. Add a CORPUS STALENESS DISCLAIMER as the first content under the title, naming the gap and what it means for verdict precision.
2. In the Tier-1 KPI Scorecard, REPLACE the 'vs. Break Threshold' column with 'vs. Latest Disclosed Forward Target' — compare current value to management's own most-recent forward commitment, not the schema's quantitative break (which assumes quarterly cadence the corpus does not provide).
3. Add a 'Staleness Adjustment' column on the scorecard noting an explicit ±X% uncertainty band reflecting unobserved drift over the staleness period.
4. Cap Net Conviction across all three Adversarial Loops at Low UNLESS the report explicitly justifies a higher conviction with the specific in-corpus evidence that supports it. State the cap reasoning in the Thesis Status loop's Resolution line."""
    else:
        directive = "Corpus is current. Standard scorecard format applies."

    return staleness_days, is_stale, line, directive


def _serialize_schema_for_llm(schema: dict) -> str:
    """
    Render the holdings schema as JSON for inclusion in an LLM prompt, with
    audit-trail fields (per SCHEMA_LLM_REDACT_FIELDS) removed. Those fields
    document why/when the schema was edited — useful for the human reviewer,
    noise for the model. Stripping them keeps prompt budget on the KPI
    definitions and break conditions that drive the analysis.
    """
    redacted = {k: v for k, v in schema.items() if k not in SCHEMA_LLM_REDACT_FIELDS}
    return json.dumps(redacted, indent=2)


def _format_quarter_context(quarters: list[dict]) -> str:
    """Render the chronological quarter blocks consumed by both passes."""
    blocks = []
    for q in quarters:
        block = f"\n### {q['quarter']} {q['year']}\n"
        for doc_type, text in q["summaries"].items():
            label = {
                "transcript": "Transcript Summary",
                "press_release": "Press Release Summary",
                "presentation": "Presentation Brief",
            }.get(doc_type, doc_type)
            block += f"\n**{label}:**\n{text[:PER_DOC_CONTEXT_CAP_CHARS]}\n"
        blocks.append(block)
    return "\n".join(blocks)


def _build_pass_a_prompt(
    ticker: str,
    schema: dict,
    quarters: list[dict],
    report_date: str,
    staleness_line: str,
    is_stale: bool,
    staleness_directive: str,
    quarters_context: str,
) -> str:
    """Pass A — evidence tables. Schema Hygiene, Tier-1 Scorecard, Key Developments, Breakers, Competitive."""
    thesis_text = _serialize_schema_for_llm(schema)
    scorecard_target_col = (
        "vs. Latest Disclosed Forward Target" if is_stale else "vs. Break Threshold"
    )
    scorecard_staleness_col = "Staleness Adjustment | " if is_stale else ""
    scorecard_staleness_sep = "--- | " if is_stale else ""
    scorecard_distance_phrase = (
        "distance to forward target (state as % or absolute gap) and ±X% uncertainty band reflecting unobserved drift"
        if is_stale
        else "distance to break condition (state as % or absolute gap)"
    )

    return f"""You are a senior fundamental equity analyst tracking a concentrated long position.

**Holding:** {ticker}
**Report date:** {report_date}
**{staleness_line}**

**Thesis & KPI Schema:**
{thesis_text}

**Available Evidence (last {len(quarters)} quarters, chronological):**
{quarters_context}

---

**Task — PASS A of 2: Evidence Tables.** Extract the data layer ONLY. Verdicts and adversarial loops are produced in a separate Pass B downstream — do NOT write a Thesis Status verdict or Say-Do assessment here. Output only the five sections below, in the order shown. The reader will see your output before any verdicts, so it must stand alone as a fact base.

**Corpus mode directive:**
{staleness_directive}

{_HARD_RULES_BLOCK}
{_PRINCIPLES_BLOCK}
**Output Format (Strict Markdown — start directly at `## Schema Hygiene`, no preamble, no title):**

## Schema Hygiene (REQUIRED)
For each Tier-1 KPI in the schema, verify the issuer actually discloses that exact metric in the Available Evidence above.
- If a schema KPI does NOT match a disclosed metric (e.g., schema asks for "Total ARR Growth" but issuer only reports "Subscription ARR" and "Cloud ARR" separately), list it here with: (a) the unmatched KPI name, (b) closest-disclosed proxy if one exists, (c) recommended threshold revision (e.g., flow metric instead of stock; named-proxy substitution).
- If a schema break_condition uses a definition that is structurally always-true or always-false against issuer disclosure (e.g., "NPL 90d+ >8%" when issuer's stock NPL is structurally 16-18%), flag it here with the suggested re-baselining.
- Schema-mismatched KPIs in the scorecard below should be marked `[schema mismatch — see Schema Hygiene]` in the Status column and NOT scored as 🟢/🟡/🔴.

If all schema KPIs match disclosure cleanly, write "No mismatches detected." and proceed.

## Tier 1 KPI Scorecard
| KPI | Latest Value | Trend | {scorecard_target_col} | {scorecard_staleness_col}Status | Source |
| :--- | :--- | :--- | :--- | {scorecard_staleness_sep}:--- | :--- |
[For each tier_1_kpi in the schema: fill latest known value, direction (↑↓→), {scorecard_distance_phrase}, flag 🟢/🟡/🔴 (or `[schema mismatch — see Schema Hygiene]`), and inline source tag. Use `[not disclosed]` if missing.]

## Key Developments This Period
[3–5 bullet points on material changes — new products, macro shifts, competitive moves, management credibility events. Each bullet ends with a source tag.]

## Thesis Breaker Watchlist
| Breaker | Status | Source |
| :--- | :--- | :--- |
[For each thesis_breakers_qualitative if present in schema: current status — Active Risk / Monitoring / Cleared — with source tag for the supporting evidence. If schema has no qualitative breakers, write "N/A — schema has no qualitative breakers" and skip.]

## Competitive Watchlist Update
[Any material developments from the competitive_watchlist if present in schema, with source tags. Use `[not disclosed]` if the evidence does not cover a watchlist item. If schema has no competitive_watchlist, write "N/A".]
"""


def _build_pass_b_prompt(
    ticker: str,
    schema: dict,
    quarters: list[dict],
    report_date: str,
    staleness_line: str,
    staleness_directive: str,
    quarters_context: str,
    pass_a_output: str,
) -> str:
    """Pass B — verdicts & adversarial loops. Anchored on Pass A's KPI table."""
    thesis_text = _serialize_schema_for_llm(schema)

    return f"""You are a senior fundamental equity analyst tracking a concentrated long position.

**Holding:** {ticker}
**Report date:** {report_date}
**{staleness_line}**

**Thesis & KPI Schema:**
{thesis_text}

**Available Evidence (last {len(quarters)} quarters, chronological):**
{quarters_context}

---

**Pass A output (already produced — your verdicts must be anchored on the KPI values and developments listed here, not on a fresh re-read of the evidence):**

{pass_a_output}

---

**Task — PASS B of 2: Verdicts & Adversarial Loops.** The fact base is fixed in Pass A above; do NOT re-emit Schema Hygiene, the KPI Scorecard, Key Developments, Breakers, or Competitive Watchlist. Output ONLY the four sections below, in the order shown. KPI values cited in your loops must match those in Pass A's Tier-1 Scorecard exactly — if you would cite a different value, treat it as a Pass A error and flag it in Analyst Notes rather than silently correcting.

**Corpus mode directive:**
{staleness_directive}

{_HARD_RULES_BLOCK}
{_PRINCIPLES_BLOCK}
{_ADVERSARIAL_LOOP_FORMAT_BLOCK}
**Output Format (Strict Markdown — start directly at `## Thesis Status:`, no preamble, no title):**

## Thesis Status: 🟢 INTACT / 🟡 MONITORING / 🔴 UNDER PRESSURE
[One sentence verdict, derived from the Pass A scorecard.]

### Adversarial Loop — Thesis Status (REQUIRED)
- **Primary Thesis:** ...
- **Strongest Counter:** ...
- **Resolution:** ... — Net Conviction: H / M / L. Flip-the-verdict observable: ...
- **Sensitivity:** ...

## Say-Do Assessment
**Verdict:** MET / MIXED / MISSED / N/A (un-evaluable — see Hard Rule 7) — derived from the gap analysis below.

**Gap Analysis (prior guidance → current actual):**
- [metric]: guided X [Source: prior-Q outlook section] → actual Y [Source: current-Q print] — delta Z%
- For ex-items adjustments per Hard Rule 5: report headline AND underlying.
- ...

### Adversarial Loop — Say-Do Attribution (REQUIRED)
- **Primary Thesis:** Execution vs. Exogenous read of the gaps above, with sourced evidence.
- **Strongest Counter:** ...
- **Resolution:** ... — Net Conviction: H / M / L. Observable that would flip: ...
- **Sensitivity:** ...

## Valuation-Trigger Stress
For each tier_1_kpi within ~15% of its break_condition (read distances from the Pass A scorecard above), AND any trigger that has fired this period, run the loop. If no T1 KPIs are within range, state that explicitly with the closest distance and skip to the next section. (Skip schema-mismatched KPIs entirely — they belong in Schema Hygiene above, not here.)

### [KPI name] — distance to break: [X% / absolute gap]
- **Primary Thesis:** [Is this trigger genuinely about to fire / has fired structurally?]
- **Strongest Counter:** [false-positive risk — single-print artifact, mix effect, FX, calendarization, methodology change per Hard Rule 6, etc.]
- **Resolution:** ... — Net Conviction: H / M / L. Confirm-or-clear observable: ...
- **Sensitivity:** [distance to threshold under ±X% scenarios on the input drivers]

## Open Questions for Next Quarter
[2–3 specific things to listen for / look for in next earnings — each tied to a Resolution flip-observable named above.]

## Portfolio & Thesis Fit
This section operationalizes the investment principles above into a position-management view. Be specific; refuse to write generic content.

**One-paragraph thesis** *(the discipline test — if you can't articulate this in one paragraph drawing only on this report's evidence, the position is mis-defined and the recommendation defaults to CUT/PASS):*
[≤120 words covering: what the company does, why the market is mispricing it (or has correctly priced it — say so), what specifically catalyzes a re-rating (or what would close the gap), expected horizon. No filler.]

**Killer variables (2–3, business-specific):**
- [variable 1 — the actual driver, not "macro"; e.g. "GLP-1 supply ramp + insurance coverage trajectory" not "drug demand"]
- [variable 2 — ditto]
- [variable 3 if needed]

**Invalidation triggers (fundamental, not price):**
- [trigger 1 — specific quarter-level observable, e.g. "FoA revenue growth <10% CC for 2 consecutive quarters"]
- [trigger 2]
- [trigger 3 if relevant — competitive event, regulator action, etc.]

**Sizing recommendation:**
- **Tier:** High conviction (≤8–10%) / Standard (3–5%) / Speculative (1–2%) / Avoid
- **Rationale:** [Tie this to Net Conviction from the three Adversarial Loops above. High conviction requires Net Conviction = High on Thesis Status AND no fired triggers. Speculative is the right call when Net Conviction = Low but the asymmetry is favorable; specify the asymmetry.]

**Time horizon & holding commitment:**
- **Pre-commit horizon:** [N quarters minimum, e.g. 8 quarters / 2 years]
- **Re-evaluation cadence:** every earnings + on any invalidation-trigger fire
- **What would shorten this:** [only the named invalidation triggers; explicitly NOT price-action]

**Sell trigger preview (for use later):**
- **Thesis-realized exit:** [specific scenario — target valuation, named re-rating event]
- **Thesis-broken exit:** [pointer to invalidation triggers above]
- **Better-opportunity exit:** [requires explicit IRR comparison vs an alternative; not "this looks cheaper"]

## Analyst Notes
[Any asymmetries, positioning thoughts, or thesis evolution observations. Mark any that rest on inference (vs. cited evidence) explicitly. Also note here any Pass A figure you believe is wrong, per the anchoring rule above.]
"""


def _assemble_tracker(
    ticker: str,
    report_date: str,
    is_stale: bool,
    staleness_days: int,
    corpus_latest_date: str | None,
    pass_a: str,
    pass_b: str,
) -> str:
    """Stitch title, optional staleness disclaimer, Pass B verdicts, then Pass A evidence tables."""
    lines = [f"# Micro-Thesis Tracker: {ticker} — {report_date}", ""]

    if is_stale:
        lines.extend(
            [
                "> **CORPUS STALENESS DISCLAIMER**: latest evidence in this tracker is "
                f"{corpus_latest_date} — {staleness_days} days stale vs. report date {report_date} "
                f"(threshold: {STALE_CORPUS_THRESHOLD_DAYS} days). Verdicts apply STALE-CORPUS MODE: "
                "scorecard compares vs. latest disclosed forward target rather than break thresholds, "
                "and adversarial-loop conviction is capped at Low absent explicit in-corpus justification.",
                "",
            ]
        )

    lines.append(pass_b.strip())
    lines.append("")
    lines.append(pass_a.strip())
    lines.append("")
    return "\n".join(lines)


def generate_thesis_update(
    ticker: str,
    schema: dict,
    quarters: list[dict],
    report_date: str,
    corpus_latest_date: str | None = None,
) -> str:
    """
    Generate an updated micro-thesis tracker document for a holding.

    Internally split into two sequential LLM passes to keep individual call
    output sizes under the per-call timeout:
      Pass A: Schema Hygiene, Tier-1 Scorecard, Key Developments, Breaker
              Watchlist, Competitive Watchlist Update (evidence tables)
      Pass B: Thesis Status verdict + loop, Say-Do + loop, Valuation-Trigger
              Stress + per-KPI loops, Open Questions, Analyst Notes (verdicts)
    Pass B is anchored on Pass A's KPI scorecard so values stay consistent.
    The function still returns a single Markdown tracker for callers.

    Args:
        ticker: Company ticker (e.g. "GOOG")
        schema: Holdings JSON schema from micro_thesis/holdings/<TICKER>.json
        quarters: List of {year, quarter, summaries: {doc_type: text}} dicts, chronological order
        report_date: ISO date (YYYY-MM-DD) the tracker is being generated for. Used to compute corpus staleness.
        corpus_latest_date: ISO date of the most recent evidence in the corpus.
            When provided, drives staleness detection. The caller should compute this
            from the latest period_end across `quarters`. None = staleness skipped.

    Returns:
        Markdown thesis tracker document.
    """
    staleness_days, is_stale, staleness_line, staleness_directive = _compute_staleness(
        report_date, corpus_latest_date
    )
    quarters_context = _format_quarter_context(quarters)

    pass_a_prompt = _build_pass_a_prompt(
        ticker,
        schema,
        quarters,
        report_date,
        staleness_line,
        is_stale,
        staleness_directive,
        quarters_context,
    )
    log.info({"event": "thesis_pass_start", "ticker": ticker, "pass": "A"})
    try:
        pass_a_output = call_llm(pass_a_prompt, purpose="thesis_pass_a", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Thesis Pass A failed for {ticker}: {e}")
        raise
    log.info(
        {
            "event": "thesis_pass_done",
            "ticker": ticker,
            "pass": "A",
            "output_chars": len(pass_a_output),
        }
    )

    pass_b_prompt = _build_pass_b_prompt(
        ticker,
        schema,
        quarters,
        report_date,
        staleness_line,
        staleness_directive,
        quarters_context,
        pass_a_output,
    )
    log.info({"event": "thesis_pass_start", "ticker": ticker, "pass": "B"})
    try:
        pass_b_output = call_llm(pass_b_prompt, purpose="thesis_pass_b", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Thesis Pass B failed for {ticker}: {e}")
        raise
    log.info(
        {
            "event": "thesis_pass_done",
            "ticker": ticker,
            "pass": "B",
            "output_chars": len(pass_b_output),
        }
    )

    return _assemble_tracker(
        ticker,
        report_date,
        is_stale,
        staleness_days,
        corpus_latest_date,
        pass_a_output,
        pass_b_output,
    )


def generate_strategic_analysis(summaries_list, ticker: str | None = None):
    """
    Generates a strategic analysis comparing performance vs expectations across quarters.
    summaries_list: List of dicts {'quarter': 'Q1', 'year': '2024', 'text': '...'}
    """
    context_str = ""
    for item in summaries_list:
        context_str += f"\n--- {item['quarter']} {item['year']} SUMMARY ---\n{item['text']}\n"

    prompt = """
    You are a Strategic Management Consultant for this company.

    **Goal:** Analyze the provided chronological earnings summaries to track the "Say-Do" ratio of management.
    Specifically, does the company achieve the goals and guidance it sets in one quarter when reported in the next?

    **Hard rules — non-negotiable:**
    - The summaries below are the source of truth. Every number, date, and quote must be traceable to them. Use `[not disclosed]` if a figure is absent — do not invent or back-fill from prior knowledge.
    - Quotes belong in quotation marks with a source tag like `[Source: Q3 2025 prepared remarks]`.
    - Each per-pair Verdict (Hit / Mixed / Miss) MUST go through the adversarial loop on attribution. The Executive Outlook Assessment at the top MUST be a synthesis of the per-pair loops, not an asserted opinion.

    **Input:** A sequence of earnings call summaries.

    **Adversarial Loop format (use these exact field names):**
    - **Primary Thesis:** the asserted attribution + sourced evidence
    - **Strongest Counter:** the most credible name-specific alternative read (not generic macro)
    - **Resolution:** reconciliation — **Net Conviction: High / Medium / Low**. Observable that would flip the verdict next period.
    - **Sensitivity:** quantified impact if the primary read is wrong by ±X%.

    **Output Structure:**

    # Strategic Performance Analysis

    ## Quarter-by-Quarter Track Record

    (Iterate through the timeline, comparing Q(N) Outlook to Q(N+1) Results)

    ### [Quarter N] Guidance vs [Quarter N+1] Reality
    *   **Expectation:** Specific guided numbers/targets from [Quarter N] with source tags. Use `[not disclosed]` if absent.
    *   **Reality:** Reported actuals in [Quarter N+1] with source tags.
    *   **Gap:** [metric: guided X → actual Y (delta Z%)] — list each material gap.
    *   **Verdict:** Hit / Miss / Mixed.
    *   **Adversarial Loop — Attribution:**
        - Primary Thesis: ...
        - Strongest Counter: ...
        - Resolution: ... — Net Conviction: H/M/L. Flip-the-verdict observable: ...
        - Sensitivity: ...

    ## Executive Outlook Assessment (synthesis)
    Roll up the per-pair loops above into a credibility view:
    - Pattern of Hits/Misses/Mixed across the period
    - Whether misses cluster on Execution or Exogenous attribution
    - Net management-credibility read with conviction (H/M/L) and the specific observable that would change the assessment

    ## Key Strategic Shifts
    Material changes in strategy/narrative over this period, each with source tag. Distinguish stated shifts (in transcripts) from inferred shifts (your reading) — label inferred items explicitly.

    **Tone:** Analytical, objective, and critical where necessary.
    """

    try:
        return call_llm(prompt + context_str, purpose="strategic_analysis", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Analysis generation failed: {e}")
        raise


def identify_transcript_metadata(text_snippet):
    """
    Identifies the Company Ticker, Quarter, and Year from the transcript text.
    """
    prompt = """
    Analyze the following text from an earnings call transcript cover page or header.
    Identify the:
    1. Company Ticker (e.g., NVDA, GOOGL, MSFT).
       **IMPORTANT**: Always use the **Primary US Listing Ticker** (NYSE/NASDAQ) if available.
       - Example: For "Taiwan Semiconductor" or "2330.TW", return "TSM".
       - Example: For "Tencent" or "700.HK", return "TCEHY".
    2. Fiscal Quarter (Q1, Q2, Q3, or Q4).
    3. Fiscal Year (e.g., 2025).

    Return the result in this STRICT format:
    TICKER_QX_YYYY

    Example: NVDA_Q1_2026

    If you cannot identify the information with confidence, return "UNKNOWN".

    Text:
    """
    try:
        return call_llm(prompt + text_snippet[:2000], purpose="transcript_metadata").strip()
    except Exception as e:
        log.error(f"Error identifying metadata: {e}")
        return "UNKNOWN"


def generate_event_brief(text: str, anchor_block: str = "", ticker: str | None = None) -> str:
    """
    Generate a structured brief for a non-quarterly IR event: investor day, AGM,
    capital markets day, conference deck, M&A announcement, ad-hoc strategic update.

    Events differ from quarterly artifacts: they are usually long-horizon strategy
    discussions (3-5 year targets, capital allocation philosophy, segment deep-dives,
    M&A rationale) rather than near-term financial results. Skip period numbers unless
    they materially shape the multi-year framework.

    ``anchor_block`` (optional) injects the thesis + tier-1 KPIs so §7 (Thesis
    Read-Through) can name specific pillars rather than write generic
    "strengthens / weakens / neutral" prose.
    """
    prompt = f"""You are a senior equity analyst summarizing an IR event document.

Events differ from quarterly artifacts: they are usually long-horizon strategy
discussions (3-5 year targets, capital allocation philosophy, segment deep-dives,
M&A rationale) rather than near-term financial results. Skip period numbers unless
they materially shape the multi-year framework.

{anchor_block}**STRICT CONSTRAINT:** Start immediately with the title. No conversational filler.

**Output Format (Strict Markdown):**

# Event Brief: [Ticker] [Event Name] [Date]

## 1. Event Type & Context
*   **Type:** [Investor Day / AGM / Capital Markets Day / Conference / M&A announcement / Other]
*   **Setting:** [Date, location if relevant, audience]
*   **Why it matters:** [1-2 sentences — what was the management goal for the event]

## 2. Headline Strategic Messages
[3-5 bullets on the core narratives management was trying to land. Lead with what's NEW
or DIFFERENT vs. prior management framing.]

## 3. Multi-Year Targets & Frameworks
*   **Quantitative targets:** [5-year revenue/FCF/margin targets, capital deployment ranges]
*   **Time horizon:** [Stated horizon — 3yr, 5yr, "through the cycle"]
*   **Comparison to prior:** [If the company previously gave a framework, note shifts]

## 4. Capital Allocation
[Explicit framing on buybacks, dividends, M&A appetite, balance-sheet priorities, payout ratios]

## 5. Segment / Product Deep-Dives
[Material new disclosures by segment — TAM, growth drivers, unit economics, competitive positioning]

## 6. Risks & Watchpoints
*   **Acknowledged:** [What management explicitly flagged as risks]
*   **Unaddressed:** [What investors will want to ask but didn't get clear answers on]

## 7. Thesis Read-Through
[2-3 sentences on whether this strengthens, weakens, or is neutral to a long-term holder's thesis. When a THESIS ANCHOR is provided above, name the specific tier-1 KPIs or business-model rules this event moves and in what direction. Generic "strengthens long-term thesis" earns a rewrite.]

Event Document Text:
"""
    try:
        return call_llm(prompt + text, purpose="event_brief", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Event brief generation failed: {e}")
        raise


def generate_recent_developments(
    ticker: str, news_days: int = 7, anchor_block: str = ""
) -> str:
    """Recent-developments brief sourced via Claude WebSearch + WebFetch.

    Routes through `call_llm_with_web` so the model can pull current news
    from Bloomberg / Reuters / CNBC / FT / WSJ / company press releases and
    cite URLs inline. On Claude failure, `call_llm_with_web` falls back to
    plain `_call_claude` (no web), then to Gemini per the standard chain.

    Used by the §8 Recent developments section. Output is markdown, with
    sources as inline links the section renderer passes through unchanged.

    ``anchor_block`` (optional) injects the thesis + bear-case anchor so the
    "rank by thesis impact" rule and the per-item implication clauses can
    actually reference the analyst's tier-1 KPIs and named bear failures
    rather than guessing.
    """
    prompt = f"""You are a senior equity analyst preparing a recent-developments
brief for {ticker} for an analyst-grade research memo. Bar: every item
must move the thesis or be tracking a specific known catalyst — pure news
recap earns an automatic rewrite.

{anchor_block}Search the web for {ticker} news from the last {news_days} days. Prioritize
Bloomberg, Reuters, CNBC, FT, WSJ, and company press releases. Skip blog
spam, opinion pieces with no new information, recapitulation of older news,
and analyst initiation reports unless they include a non-obvious data point.

RANKING + filtering rules:
- Order each section by THESIS IMPACT (highest first), NOT chronologically.
  When a THESIS ANCHOR is provided above, "thesis impact" means: which
  named tier-1 KPI does this item move, and in what direction relative to
  its break condition? Items that touch a tier-1 KPI rank above items
  that touch a tier-2 KPI; items that touch nothing in the anchor rank
  last (or are dropped).
- For each item: the gloss must explain the IMPLICATION for the investor,
  not just restate the headline. "X happened" is wrong; "X happened, which
  shortens the runway for Y by ~Z months" is right. When the anchor names
  a relevant KPI or failure mode, cite it explicitly in the implication
  clause (e.g., "tightens the GCP-margin trajectory KPI", "partially
  confirms the AI-Mode query-dilution failure mode").
- Skip items that are purely stock-price commentary, sell-side rating
  changes without a new data point, or pure recapitulation of prior news.
- Skip ANY item that's older than {news_days} days. Don't pad.

**Output Format (Strict Markdown):**

### Material news
- **[Headline]** — [1-2 sentences: what happened AND specific implication for the thesis / valuation / KPI trajectory. Quantify the implication where the news supports it.] [Source: outlet, YYYY-MM-DD, URL]
- ... (3-7 items, ranked by thesis impact)

### Sector / regulatory context
- [optional 1-3 items: peer earnings prints, FDA / antitrust decisions affecting peers, sector ETF flows, macro shifts that hit this ticker's specific exposures. Each item must have a "why this matters for {ticker}" clause.]

### Watch this week
- [1-3 items: upcoming earnings calls (this ticker or named peers), scheduled disclosures, investor days, regulatory dockets within the next ~7 days. Format: `**Date · Event** — what to watch for`]

If no material news found in the window, write `*No material news in the last
{news_days} days.*` under "Material news" and skip the other two sections.
Do not pad with stale or low-signal items just to fill the section.
"""
    try:
        return call_llm_with_web(prompt, purpose="recent_developments", ticker=ticker)
    except Exception as e:
        log.error(f"CRITICAL ERROR: Recent-developments generation failed for {ticker}: {e}")
        raise


def _ticker_specific_block(md: str) -> str:
    """Wrap optional per-ticker enhancement context (see Phase 5) for the prompt.

    Empty input → empty output (no extra newlines), so the universal prompt
    shape is unchanged when no per-ticker enhancements exist for this name.
    """
    if not md.strip():
        return ""
    return f"\nTICKER-SPECIFIC CONTEXT (per-ticker research enhancements):\n{md}\n"


def generate_bear_case(
    ticker: str,
    thesis: str,
    break_conditions: list[str],
    last_quarter_summaries: list[str],
    financials_table_md: str,
    segments_table_md: str,
    kpi_status_md: str,
    ticker_specific_md: str = "",
) -> str:
    """
    Generate a structured bear case as a JSON string the caller parses.

    Schema: {failure_modes: list[FailureMode], most_underweighted: str,
    out_of_scope_flags: list[str]}.
    """
    transcripts_block = "\n\n".join(
        f"### Quarter {i + 1} (oldest first)\n{s[:6000]}"
        for i, s in enumerate(last_quarter_summaries)
    )

    prompt = f"""You are a senior fundamental equity analyst writing the bear
case for {ticker}. The bar is a deep-dive newsletter take, not a Yahoo
Finance risks list. You are arguing the SHORT side to a thoughtful portfolio
manager who already knows the bull case.

BAR + framing rules:
- Every failure_mode must be SPECIFIC to {ticker}'s business model. Generic
  risks ("revenue could decelerate", "macro could weaken", "competition")
  earn an automatic rewrite. Tie each risk to a named mechanic of THIS
  business — its pricing, unit economics, regulatory exposure, capex
  profile, channel concentration, switching-cost economics, etc.
- At least TWO of the failure_modes must be NON-CONSENSUS — risks that
  sell-side coverage has NOT broadly flagged or that are systematically
  underweighted by buy-side because of organizational bias, model inertia,
  or framing blind-spots. If you can't think of a non-consensus failure
  mode for this business, you haven't thought hard enough.
- Cite competitive dynamics with NAMED rivals where relevant ("OpenAI
  + Anthropic capture X% of the GenAI query budget that was previously
  Google Search... ", "AWS retains the enterprise-AI workload pipeline
  via Bedrock's distribution lead..."). Quantify where the input data
  supports it; otherwise be honest about the qualitative claim.
- Each `evidence_in_data` MUST cite a specific number or trend from the
  inputs (e.g., "FCF dropped from $24.6B Q4'25 to $5.3B Q2'25 — capex
  inflection running ahead of OCF growth"). Vague "growth is decelerating"
  doesn't qualify.
- `quantitative_impact` must do the actual math: link the failure mode to
  a specific revenue / margin / FCF / NPV-per-share delta with the
  reasoning chain shown. The reader should be able to plug your numbers
  into a model and replicate your scenario.
- Grounded ONLY in the data below. Don't fabricate. If a real risk is
  real but not derivable from these inputs, put it under
  `out_of_scope_flags` with a 1-line explanation of why it's parked.

THESIS:
{thesis}

BREAK CONDITIONS:
{json.dumps(break_conditions, indent=2)}

LAST {len(last_quarter_summaries)}Q SUMMARIES:
{transcripts_block}

QUARTERLY FINANCIALS (12Q):
{financials_table_md}

SEGMENT TRENDS (12Q):
{segments_table_md}

KPI STATUS:
{kpi_status_md}
{_ticker_specific_block(ticker_specific_md)}
---

Produce a JSON object with EXACTLY these keys (no markdown, no commentary):

{{
  "failure_modes": [
    {{
      "hypothesis": "one-sentence concrete failure mode — must name a specific business-model mechanic of {ticker}",
      "evidence_in_data": "cite a specific number or trend from the inputs above (with the value AND the time period). Vague paraphrasing earns an automatic rewrite.",
      "leading_indicator": "what would confirm it in the NEXT 1-2 prints. Must be a numerical / disclosed metric, not a qualitative vibe.",
      "quantitative_impact": "do the math: link the failure mode to a specific revenue / margin / FCF / NPV-per-share delta. Show the reasoning chain so the reader can replicate or stress-test.",
      "refutation_criteria": "what management would have to disclose or demonstrate over the next 2-4Q to neutralize this thesis. Specific and falsifiable."
    }}
  ],
  "most_underweighted": "one-paragraph editorial argument: which of the failure modes above is most underweighted by sell-side / consensus, and WHY consensus is structurally blind to it (e.g., model inertia, organizational bias of legacy bull-side analysts, framing blind-spot, etc.). Don't pick the most-likely failure mode — pick the one that consensus is most-wrongly-pricing relative to its actual probability × impact.",
  "out_of_scope_flags": ["each entry: a real risk that's NOT derivable from the inputs above (regulatory, macro, technological, etc.) — with a brief reason why we're parking it. 1-3 entries max."]
}}

Provide 3 to 5 failure_modes. At least 2 must be non-consensus per the rule
above. Return strictly the JSON object — nothing else.
"""
    try:
        raw = call_llm(prompt, purpose="bear_case", ticker=ticker).strip()
        # Strip ``` fences if Claude wraps the JSON despite the instruction.
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Bear case generation failed for {ticker}: {e}")
        raise


def generate_qa_topics(
    ticker: str,
    quarter_label: str,
    questions: list[dict[str, str]],
) -> str:
    """Generate short topic labels for a batch of analyst Q&A questions.

    ``questions`` is a list of ``{"id": "0", "analyst": str, "question": str}``
    dicts. Returns a JSON list ``[{"id": str, "topic": str, "tag": str}, ...]``
    where ``topic`` is a 4-7 word noun phrase and ``tag`` is one of the
    coarse business-area chips the renderer styles (INFRA, CLOUD, SEARCH,
    MARGIN, CAPEX, AGENT, LEGAL, OTHER BETS, CONSUMER, Q&A as fallback).

    Batched per quarter (one LLM call per quarter instead of one per question)
    so the cost stays in the sub-penny range for the typical 6-10 question
    transcript.
    """
    payload = json.dumps(questions, ensure_ascii=False)
    prompt = f"""You are labelling analyst-call Q&A topics for an equity research dashboard.
For each analyst question below, return a short, scannable topic phrase that
captures the substance — what's actually being asked — NOT throat-clearing or
sequence ("two for me", "one for Anat"). 4-7 words. Use noun phrases, not
sentences. Also pick a coarse business-area tag from this set:
INFRA, CLOUD, SEARCH, MARGIN, CAPEX, AGENT, LEGAL, OTHER BETS, CONSUMER, Q&A.

TICKER: {ticker}
QUARTER: {quarter_label}

QUESTIONS (JSON):
{payload}

Return ONLY a JSON array (no markdown, no commentary):

[
  {{"id": "0", "topic": "Compute allocation across internal vs Cloud", "tag": "INFRA"}},
  ...
]

The "id" must echo back the input id verbatim. The "topic" must read cleanly
on its own — no "asks about" prefix; just the topic itself.
"""
    try:
        raw = call_llm(prompt, purpose="qa_topics", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Q&A topic generation failed for {ticker}: {e}")
        raise


def generate_saydo_filter(
    ticker: str,
    quarter_label: str,
    rows: list[dict[str, str]],
    anchor_block: str = "",
) -> str:
    """Pick the strategically important commitments out of a print-vs-guide table.

    ``rows`` is ``[{"id": str, "metric": str, "guide": str, "actual": str,
    "verdict": str}, ...]``. Returns a JSON list of the IDs that matter for
    the thesis — skips FX trivia, share count noise, and other below-the-line
    items. Up to 6 rows kept.

    ``anchor_block`` (optional) injects the thesis + tier-1 KPI list so the
    "matters for the thesis" judgment can be made against the analyst's
    actual KPIs rather than the model's generic prior about what matters.
    """
    payload = json.dumps(rows, ensure_ascii=False)
    prompt = f"""You are filtering an analyst's print-vs-guide commitments table
for {ticker} {quarter_label}. Most rows in these tables are noise (FX, share
count, tax rate, depreciation timing). The reader wants ONLY the commitments
that move the investment thesis — revenue trajectory, segment growth,
margin direction, capex / FCF, major product or partnership commitments,
balance-sheet structural shifts.

{anchor_block}ROWS (JSON):
{payload}

Return ONLY a JSON array of the IDs to KEEP (up to 6, ordered by importance):

["3", "1", "5"]

Skip anything that's a maintenance/macro line. Prefer rows where the verdict
is EXCEEDED or MISSED (signal) over rows that are clean MET (less signal).

When a THESIS ANCHOR is provided above, prioritize rows whose metric maps
to one of the named tier-1 KPIs or business-model rules. Rows that touch a
tier-1 KPI rank above rows that don't, even if the latter is a bigger
delta. The point is to filter for THESIS-relevant signal, not generic
beats/misses.
"""
    try:
        raw = call_llm(prompt, purpose="saydo_filter", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: SayDo filter failed for {ticker}: {e}")
        raise


def generate_company_description(
    ticker: str,
    profile_description: str,
    sector: str | None,
    industry: str | None,
    form_10k_text: str,
    segment_names: list[str],
    geo_names: list[str],
    fiscal_year: int | None,
    thesis_text: str = "",
    recent_earnings_md: str = "",
    recent_ir_md: str = "",
) -> str:
    """Synthesize the §2 Company description as an analyst-grade business writeup.

    Inputs go beyond the 10-K — the prompt is fed the user's own thesis
    statement, the most recent earnings narrative(s), and recent IR document
    summaries. The intent is to elicit a *thesis-anchored* take on the
    business (what makes it interesting as an INVESTMENT, where the moat
    actually is, what consensus underweights) rather than a 10-K paraphrase.

    Returns a JSON string the caller parses. Schema:
      {
        "elevator_pitch": "1-2 sentence positioning statement",
        "business_overview": "multi-paragraph analytical take on the business",
        "revenue_model": "how the company makes money, with take-rate / unit-economics specifics",
        "segments": [{"name": "Google Services", "description": "..."}, ...],
        "geographies": [{"name": "United States", "description": "..."}, ...]
      }
    """
    segments_block = (
        "\n".join(f"- {n}" for n in segment_names) if segment_names else "(none on file)"
    )
    geos_block = "\n".join(f"- {n}" for n in geo_names) if geo_names else "(none on file)"
    thesis_block = (
        f"INVESTOR THESIS ON FILE (the analyst's own framing of this name):\n"
        f'"""\n{thesis_text.strip()}\n"""\n'
        if thesis_text.strip()
        else ""
    )
    recent_earnings_block = (
        f"RECENT EARNINGS NARRATIVE (most recent management framing — use this to "
        f"anchor the business writeup, not the 10-K boilerplate):\n"
        f'"""\n{recent_earnings_md.strip()[:12000]}\n"""\n'
        if recent_earnings_md.strip()
        else ""
    )
    recent_ir_block = (
        f"RECENT IR DOCUMENT EXCERPTS (press releases, investor day decks):\n"
        f'"""\n{recent_ir_md.strip()[:8000]}\n"""\n'
        if recent_ir_md.strip()
        else ""
    )
    prompt = f"""You are writing the "Company" section of an analyst-grade
investment memo on {ticker}. Voice: a senior buy-side analyst's working
note. Not Wikipedia. Not a 10-K paraphrase.

ANCHOR YOUR WRITEUP ON THE ANALYST'S THESIS (below). Every paragraph
should advance one of the thesis pillars (value driver, moat, pressure
point, optionality) with specific numbers and named competitors.

{thesis_block}{recent_earnings_block}{recent_ir_block}
SEGMENTS this report displays (use these EXACT names):
{segments_block}

GEOGRAPHIES this report displays (use these EXACT names):
{geos_block}

10-K segment-naming excerpts (factual grounding only — do not paraphrase):
\"\"\"
{form_10k_text.strip()[:2500] or "(none)"}
\"\"\"

Profile blurb (third-party — do not copy):
"{profile_description.strip()[:500] or "(none)"}"

Sector/Industry/Source-FY: {sector or "?"} / {industry or "?"} / {fiscal_year or "?"}

---

OUTPUT — a JSON object with EXACTLY these fields. The schema is
**component-based**: you emit analytical pieces, the renderer assembles
the final prose. Do NOT try to write a polished elevator pitch or
business overview yourself — the schema fields are deliberately structured
so the analytical voice is forced by the format, not by your prose
choices.

```json
{{
  "value_driver_phrase": "noun-phrase clause, 6-15 words, that will be CONCATENATED into the string '{ticker}: <phrase>.'. Must read as a continuation of '{ticker}: ', NOT as a standalone sentence. Must name the cash-engine mechanic AND a concrete economic anchor (margin, take rate, scale figure).",
  "central_bet": "10-20 words framing the central bull-vs-bear debate as a TESTABLE quantified hypothesis. Will be concatenated as 'The bet: <central_bet>'. Examples: 'whether GCP margin expansion absorbs the $180B+ 2026 capex before Gemini cannibalization compresses Search', 'whether ARPAC sustains 25%+ CAGR through 2028 once secured-credit saturates Brazil'.",
  "swing_variable": "Optional null-able 1-sentence on what to watch for the next 4 quarters — the single most important number that will resolve the bet. Pass null if no clean swing variable exists.",
  "paragraphs": [
    {{
      "opener": "<ONE OF: 'The cash engine is' / 'The growth optionality is' / 'The structural debate is' / 'The pressure point most analysts underweight is' / 'What is non-obvious is' / 'The competitive dynamic is' / 'The moat depends on'>",
      "body": "Rest of the paragraph, ~80-200 words. MUST cite at least one specific number from the inputs (revenue, margin, growth rate, market share). MUST name at least one specific competitor or comparable company. MUST tie back to a thesis pillar."
    }}
  ],
  "revenue_mechanics": [
    {{
      "topic": "<ONE OF: 'take_rate' / 'unit_economics' / 'capex_intensity' / 'mix_shift' / 'scale_dynamics' / 'cash_conversion'>",
      "body": "1 paragraph (~80-150 words) on the economic mechanic. Use specific numbers + comparison to peers where available. Do NOT write 'X generates revenue through Y' descriptions."
    }}
  ],
  "segments": [
    {{"name": "<exact segment name from the SEGMENTS list above>", "description": "1-2 sentences with a SPECIFIC economic mechanic (margin trajectory, growth rate, contribution to OI) + competitive position. Forbidden phrases: 'includes products such as', 'encompasses', 'generates revenue from'. Immaterial segments: 'immaterial (<X% of revenue), primarily Y, runs Z operating loss/year'."}}
  ],
  "geographies": [
    {{"name": "<exact geography name from the GEOGRAPHIES list above>", "description": "1 sentence with analytical content (concentration, growth differential, regulatory exposure). Skip generic geo-disclosure rows."}}
  ]
}}
```

Field-by-field rules:

- `value_driver_phrase`: must be a NOUN PHRASE. It will be string-
  concatenated as `f"{ticker}: {{value_driver_phrase}}."`. So "a
  search-ads cash engine ($240B run-rate)" is valid; "the company
  operates a search business" is invalid because it parses as a verb
  phrase + the assembled sentence would read awkwardly. Good examples:
  * "a search-ads quasi-monopoly funding the largest first-party AI
    distribution stack in the world (~$240B run-rate, 35%+ op margin)"
  * "the per-customer monetization arc compounding ~3x faster than
    incumbent banks at 1/10th the cost-to-serve"
  * "a semiconductor monopoly capturing ~$0.92 of every hyperscaler GPU
    dollar at 73% gross margin"

- `paragraphs`: emit 3-5 entries. Use openers from the allowed list.
  Don't reuse the same opener twice. Order them by analytical priority
  (cash engine first, then growth optionality, then debates/pressure
  points).

- `revenue_mechanics`: emit 1-3 entries. Pick the topics that actually
  matter for THIS business. If take rate doesn't apply (e.g., for a pure
  consumption-fee model), pick mix_shift or scale_dynamics instead.

- `segments`: include EVERY segment name from the list — even
  immaterial ones (with a 1-line note).

- `geographies`: only entries with analytical signal. Skip rows where
  the only thing to say is "represents Western Europe".

Return ONLY the JSON object. No markdown fence, no prose before or after.
"""
    try:
        raw = call_llm(prompt, purpose="company_description", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Company description generation failed for {ticker}: {e}")
        raise


def generate_platform_diagram(
    ticker: str,
    profile_description: str,
    sector: str | None,
    industry: str | None,
    form_10k_text: str,
    transcript_excerpts: str,
    segment_names: list[str],
    fiscal_year: int | None,
) -> str:
    """Synthesize a monospace platform-overview diagram for §2.

    Reads the same 10-K + profile inputs used by `generate_company_description`
    plus excerpts from the two most recent earnings transcripts (Q&A segments)
    so the visual reflects how management currently frames the platform —
    investor decks aren't on disk, but call language is the closest proxy for
    the "platform diagram slide" they'd use.

    Returns a JSON string the caller parses. Schema:
      {
        "diagram": "<fenced monospace block, <= 80 cols, box-drawing chars>",
        "caption": "1-2 sentence caption explaining the diagram"
      }
    """
    segments_block = (
        "\n".join(f"- {n}" for n in segment_names) if segment_names else "(none on file)"
    )
    prompt = f"""You are an equity analyst building a "platform overview" visual for a research brief on {ticker}.

The goal is a compact ASCII / monospace diagram that captures HOW the
company's platform connects inputs (customers, suppliers, capital) to
outputs (products, revenue). Think of the kind of "platform slide" an
investor deck would show — the one diagram that, on its own, communicates
what the business is.

INPUTS

Sector: {sector or "(unknown)"}
Industry: {industry or "(unknown)"}
Fiscal year of source 10-K: {fiscal_year or "(unknown)"}

FMP profile.json description:
\"\"\"
{profile_description.strip() or "(none)"}
\"\"\"

10-K narrative excerpts (business description / segments):
\"\"\"
{form_10k_text.strip() or "(no 10-K narrative available)"}
\"\"\"

Recent earnings-call Q&A excerpts (how management frames the platform NOW):
\"\"\"
{transcript_excerpts.strip() or "(no transcripts available)"}
\"\"\"

Segment names this report displays (use these where they fit; do not invent new ones):
{segments_block}

---

Produce a JSON object with EXACTLY these keys (no markdown around the JSON, no commentary):

{{
  "diagram": "<a single monospace block, 60-78 columns wide, drawn with Unicode box-drawing chars (┌─┐│└┘├┤┬┴┼) and directional arrows (→ ← ↑ ↓). NO leading or trailing fence markers — the renderer wraps the block in a code fence itself.>",
  "caption": "1-2 sentences (plain prose, no markdown) explaining what the diagram shows and what makes the platform distinctive"
}}

Diagram rules:
- Aim for THREE conceptual columns or layers, e.g. (Inputs/Customers) → (Platform/Core) → (Products/Revenue).
  Use what fits this business; a two-sided marketplace is two columns flowing into a middle, a vertical SaaS is a stack, etc.
- Each box should hold a short label (2-5 words) plus optionally a one-line concrete detail (a KPI, a count, a product name).
- Width: keep every line <= 78 characters. Height: aim for 8-16 lines total. The block must look balanced when rendered in a fixed-width font.
- Use ONLY: ┌ ─ ┐ │ └ ┘ ├ ┤ ┬ ┴ ┼ → ← ↑ ↓ plus ASCII letters, digits, spaces, common punctuation. No emoji, no Markdown bold, no HTML.
- Anchor the labels in concrete facts from the inputs above. If a fact isn't in the inputs, leave the label generic rather than invent.
- Do NOT escape characters inside the JSON string except for required \\n (newlines between diagram rows) and \\". The renderer interprets the string as-is in a code fence.

Caption rules:
- Plain English. Reference the platform's distinctive mechanism (network effect, vertical integration, data flywheel, regulatory moat, etc.) only if the inputs actually support it.
- Do NOT restate the diagram literally; add the "why this shape" insight.

Return strictly the JSON object — no prose before or after, no markdown fence around the JSON.
"""
    try:
        raw = call_llm(prompt, purpose="platform_diagram", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Platform diagram generation failed for {ticker}: {e}")
        raise


def classify_intake_document(filename: str, text: str, hint: dict) -> dict | None:
    """
    Classify a user-dropped IR document.

    Returns a dict with keys (ticker, period_end, doc_type, confidence, reasoning)
    matching `src.intake.IntakeClassification`, or None on any LLM/parse failure.
    Routes through the fast classifier model — this is a short structured call
    that runs ~50x per intake batch, so latency matters more than raw quality.
    """
    prompt = (
        "You are classifying an investor-relations document for a portfolio analyst.\n\n"
        "Given the filename and an excerpt of the document text, return a JSON object with EXACTLY these fields:\n\n"
        "{\n"
        '  "ticker": "<US-listed primary ticker, e.g. NVDA, GOOG, BN, MELI>",\n'
        '  "period_end": "<YYYY-MM-DD, the last calendar day of the fiscal quarter>",\n'
        '  "doc_type": "<one of: ir_press_release, ir_presentation, ir_supplement, ir_investor_update, earnings_call_transcript, ir_event>",\n'
        '  "confidence": <float 0.0 to 1.0>,\n'
        '  "reasoning": "<one sentence explaining your choice>"\n'
        "}\n\n"
        "Doc-type guidance (pick the dominant form, not the topic):\n"
        "- ir_press_release: short text-heavy quarterly earnings announcement / financial results\n"
        "- ir_presentation: slide deck dominated by charts / visuals / bullet slides for a quarter\n"
        "- ir_supplement: detailed financial supplement / data book / spreadsheet-style tables\n"
        "- ir_investor_update: longer letter to shareholders / quarterly update narrative\n"
        "- earnings_call_transcript: speaker-attributed dialogue from the earnings call\n"
        "- ir_event: NON-QUARTERLY IR materials — investor day, AGM, capital markets day,\n"
        "    conference deck, ad-hoc strategic announcement, M&A or stock-split deck.\n"
        "    These are NOT tied to a fiscal quarter. For these, period_end = the EVENT DATE\n"
        "    (the day the event occurred), not a quarter-end. If you can't find an exact day\n"
        "    in the document, use the first day of the relevant month or the cover-page year.\n\n"
        "Period-end mapping (for quarterly doc types only):\n"
        "- Calendar fiscal year (BN, MELI, GOOG, META, NVO, NU, NOW, WIX, AMZN): Q1=03-31, Q2=06-30, Q3=09-30, Q4=12-31.\n"
        "- VEEV / RBRK have January fiscal year-end. FY26 Q1 ends ~04-30, Q2 ~07-31, Q3 ~10-31, Q4 ~01-31 of the next calendar year.\n"
        "- NVO publishes H1 (map to Q2, period_end 06-30) and 9M (map to Q3, 09-30).\n\n"
        "Set confidence < 0.6 if the document is empty, ambiguous, or clearly not an IR document for a tracked holding.\n\n"
        f"Filename hint (pre-extracted, may be wrong): {hint}\n"
        f"Filename: {filename}\n\n"
        "Document text excerpt:\n"
        '"""\n'
        f"{text[:INTAKE_TEXT_BUDGET]}\n"
        '"""\n\n'
        "Return ONLY the JSON object — no prose, no markdown fence."
    )

    try:
        raw = call_llm(prompt, purpose="intake_classifier").strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"classify_intake_document failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Valuation basis (Opus picks ONE multiple per ticker)
# ---------------------------------------------------------------------------

# Allowed multiples — the compute layer knows how to fetch the numeric value
# for each. The LLM's choice MUST be in this set; out-of-set picks get
# rejected by the parser and fall back to a sector-default heuristic.
VALUATION_MULTIPLE_CHOICES: tuple[str, ...] = (
    "EV/NTM Revenue",
    "EV/LTM Revenue",
    "EV/NTM EBITDA",
    "EV/LTM EBITDA",
    "P/E (NTM)",
    "P/E (LTM)",
    "P/B",
    "P/TBV",
    "P/FCF",
    "EV/FCF",
)


def generate_valuation_basis(
    ticker: str,
    sector: str | None,
    industry: str | None,
    thesis_text: str,
    financial_profile_md: str,
    available_estimates_md: str,
) -> str:
    """Pick the ONE valuation multiple that best frames this business.

    Returns a JSON string the caller parses. Schema:
      {
        "multiple": "<one of VALUATION_MULTIPLE_CHOICES>",
        "rationale": "<1-2 sentence why this is the right lens for THIS ticker>",
        "target_band": "<optional qualitative target read, e.g. 'historical range 12-18x; deserves the upper half given GCP margin trajectory'>",
        "notes": "<optional caveat, e.g. 'NTM not available — fell back to LTM'>"
      }

    The LLM picks based on:
    - Sector / industry conventions (banks: P/B or P/TBV; SaaS: EV/NTM Revenue;
      capital-intensive: EV/EBITDA; cyclicals: through-cycle P/E; etc.)
    - The actual investment thesis (if thesis hinges on FCF inflection,
      P/FCF lens; if on top-line, EV/Revenue; etc.)
    - What's computable — only pick NTM-based multiples when analyst
      estimates are listed as available.
    """
    choices_block = "\n".join(f"- {m}" for m in VALUATION_MULTIPLE_CHOICES)
    prompt = f"""You are a senior buy-side analyst picking THE SINGLE multiple
that best frames {ticker} for an investment memo. Not 2-3 multiples. ONE.
The reader will see this number prominently on the report's Valuation tab;
your pick must answer the question "is this stock rich or cheap?" in the
lens that's most diagnostic for THIS specific business.

TICKER: {ticker}
SECTOR / INDUSTRY: {sector or "?"} / {industry or "?"}

THESIS (the analyst's investment case):
\"\"\"
{thesis_text.strip()[:2000] or "(no thesis on file)"}
\"\"\"

FINANCIAL PROFILE (recent quarterly + TTM shape):
\"\"\"
{financial_profile_md.strip()[:2500]}
\"\"\"

AVAILABLE ANALYST ESTIMATES (use to know which NTM multiples are computable):
\"\"\"
{available_estimates_md.strip()[:1200]}
\"\"\"

PICK FROM EXACTLY THESE OPTIONS (verbatim string match):
{choices_block}

Selection guidance:
- Banks / fintech-with-balance-sheet (NU, MELI's credit book, SOFI): P/B or P/TBV is the canonical lens. P/E only if earnings power is the bet.
- SaaS / high-growth software with negative or thin GAAP earnings: EV/NTM Revenue, EV/LTM Revenue as fallback.
- Profitable platforms / GARP (GOOG, META, NOW at scale): EV/NTM EBITDA or P/E (NTM) when consensus EBITDA / EPS is available.
- Capital-intensive / industrial / commodity (BHP, FCX, CGEH): EV/LTM EBITDA — through-cycle.
- FCF-thesis names (mature compounders, royalty/lease businesses): P/FCF or EV/FCF.
- If the thesis is explicitly about FCF inflection or capex moderation, prefer the FCF multiples regardless of sector default.
- Only pick NTM multiples when the AVAILABLE ANALYST ESTIMATES block lists the relevant NTM line.

Return ONLY a JSON object (no markdown fence, no prose):

{{
  "multiple": "<one of the options above, exact string>",
  "rationale": "1-2 sentence why THIS multiple is the diagnostic lens for THIS ticker's thesis. Reference the specific thesis pillar or business-model mechanic that makes it the right pick. Generic 'standard SaaS lens' earns a rewrite.",
  "target_band": "Optional 1-sentence qualitative read of where the multiple SHOULD trade (e.g. 'historical 10-15x range; deserves the upper half if margin expansion sustains', or 'currently in a re-rating window — base-case 4-6x P/TBV'). Pass empty string if no view.",
  "notes": "Optional 1-line caveat, e.g. 'NTM not available, fell back to LTM' or 'historical P/B distorted by 2022 IPO multiple compression'. Empty string if none."
}}
"""
    try:
        raw = call_llm(prompt, purpose="valuation_basis", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: Valuation basis generation failed for {ticker}: {e}")
        raise


# ---------------------------------------------------------------------------
# SayDo importance ranking (Opus orders pairwise SayDo bullets by thesis impact)
# ---------------------------------------------------------------------------


def generate_saydo_importance(
    ticker: str,
    quarter_label: str,
    bullets: list[dict[str, str]],
    anchor_block: str = "",
) -> str:
    """Order a list of SayDo bullet snippets by thesis impact.

    ``bullets`` is ``[{"id": str, "snippet": str}, ...]`` where each snippet
    is one short paragraph or bullet from the pairwise SayDo analysis (e.g.
    "Revenue: guided $X, printed $Y, +5% delta..." or "CLIP rollout pace...").

    Returns a JSON list of the IDs in importance order (most thesis-relevant
    first). Bullets the LLM judges immaterial may be omitted. Up to 8 kept.

    Used by the renderer to sort the pairwise SayDo card before display so
    the top of each card is what actually matters for the thesis, not what
    happened to appear first in the LLM's pairwise output.
    """
    payload = json.dumps(bullets, ensure_ascii=False)
    prompt = f"""You are ranking SayDo (commitments vs. delivery) bullets from
the {ticker} {quarter_label} pairwise analysis BY THESIS IMPACT — what
moves the investment case the most, not what was disclosed first.

{anchor_block}BULLETS (JSON):
{payload}

RANKING rules:
- A bullet that moves a TIER-1 KPI ranks above one that doesn't.
- A bullet that confirms or refutes a named bear-case failure mode ranks
  high regardless of magnitude (it resolves analytical uncertainty).
- Items that are bookkeeping / FX / share-count / tax-rate / one-off
  accounting noise rank LOW and should be dropped if you have more than 8
  bullets.
- Items framed as "what didn't happen" or "what's unchanged" can still be
  important if the unchanged direction confirms a thesis pillar.

Return ONLY a JSON array of the IDs in importance order (up to 8):

["2", "0", "5", "1", ...]

Echo IDs verbatim from input. Do NOT invent IDs. Do NOT include omitted
bullets.
"""
    try:
        raw = call_llm(prompt, purpose="saydo_importance", ticker=ticker).strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        return raw
    except Exception as e:
        log.error(f"CRITICAL ERROR: SayDo importance ranking failed for {ticker}: {e}")
        raise
