"""
src/llm/cli.py
--------------
Claude Code CLI subprocess wiring + the public ``call_llm`` / ``call_llm_with_web``
entry points + per-purpose budget enforcement.

Primary path: ``claude -p`` via subprocess. The CLI honors whichever auth is
configured in the environment — ``ANTHROPIC_API_KEY`` for metered API billing,
or ``claude auth login`` for a Pro/Max subscription.

On any operational failure (timeout, non-zero exit, empty stdout, malformed
JSON envelope, binary missing mid-run), the call is routed through the Gemini
fallback in ``src/llm/fallback.py``. Setup-class errors (binary missing on
first call) propagate without fallback — they need operator action, not
papering over.

Public API:
    DEFAULT_MODEL, FAST_CLASSIFIER_MODEL — canonical model ids.
    LLM_MODELS — per-purpose model selection table.
    DEFAULT_TIMEOUT_SECONDS, CLAUDE_WEB_TIMEOUT_SECONDS, CLAUDE_WEB_TOOLS.
    LLMBudgetExceeded — raised when a hard per-purpose monthly cap is over.
    call_llm(...) — single-shot LLM call. Canonical entry point.
    call_llm_with_web(...) — same, with Claude WebSearch + WebFetch tools.

Note on module-level state: ``_setup_verified`` and ``_claude_cli_path`` are
intentionally kept as live globals in ``src/llm_client.py`` (read via late
``import llm_client`` inside the functions below) so the existing test
monkeypatch surface — ``monkeypatch.setattr(llm_client, "_setup_verified",
True)`` — continues to work without test modification.

Extracted from src/llm_client.py during the llm subpackage split (PURE
refactor — zero behavior change).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from datetime import UTC, datetime

from llm.ledger import fallback_call_logged, record_llm_call

log = logging.getLogger(__name__)


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

# Web-search-enabled call: same subprocess as _call_claude but with the
# Claude CLI's --allowedTools flag turned on so the model can run WebSearch
# / WebFetch as part of producing its answer. Used by the memo generator
# for the "Recent Developments" section so memos cite real news URLs
# instead of leaning on a stale FMP news pre-pull.
CLAUDE_WEB_TOOLS = "WebSearch WebFetch"
CLAUDE_WEB_TIMEOUT_SECONDS = 1800  # web fetches add round-trips; bigger cap


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


def _verify_setup_once() -> None:
    """Resolve and cache the absolute path to the ``claude`` binary on first call.

    Windows-specific: bare ``"claude"`` fails because the npm-installed binary
    is ``claude.cmd`` and Python's subprocess doesn't apply PATHEXT to bare
    names. Cached so repeat calls in a long-running batch are free.

    State (``_setup_verified`` / ``_claude_cli_path``) lives on the
    ``llm_client`` module so the existing test monkeypatch surface keeps
    working without test changes; see this module's docstring.
    """
    import llm_client  # late import — breaks circular at import time
    if llm_client._setup_verified:
        return
    resolved = shutil.which("claude")
    if resolved is None:
        raise RuntimeError(
            "Claude Code CLI ('claude') not found in PATH. Install it from "
            "https://code.claude.com/docs/en/setup, then either set "
            "ANTHROPIC_API_KEY in your shell / .env or run `claude auth login`."
        )
    llm_client._claude_cli_path = resolved
    llm_client._setup_verified = True


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
    import llm_client  # late import — state lives on llm_client for test compat
    assert llm_client._claude_cli_path is not None  # set by _verify_setup_once when it returns successfully
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
            [llm_client._claude_cli_path, "-p", "--model", model, "--output-format", "json"],
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
        record_llm_call(
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
        record_llm_call(
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
        # Operational failure — try Gemini fallback. fallback_call_logged raises
        # if no Gemini key is configured, surfacing both errors together. The
        # fallback writes its own ledger row tagged fallback_used='gemini'.
        return fallback_call_logged(
            prompt,
            claude_error,
            prompt_sha=prompt_sha,
            purpose=purpose,
            ticker=ticker,
            scope=scope,
            run_id=run_id,
        )


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
    import llm_client  # late import — state lives on llm_client for test compat
    assert llm_client._claude_cli_path is not None
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
        llm_client._claude_cli_path,
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
        record_llm_call(
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
        record_llm_call(
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
