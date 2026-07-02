"""Env-gated capture of full LLM prompt/response exchanges.

The ``llm_calls`` ledger stores only the prompt/response **SHA** — deliberate,
because prompts embed thesis and IR content (see ``directives/llm_calls.md``).
But comparing the eval-gated Gemini backend against Claude across the *real*
production surface (every ``--enable-llm`` section, not a hand-built smoke set)
needs the actual prompt TEXT each section sends. This opt-in sink records the
full exchange when ``LLM_CAPTURE_DIR`` is set; it is **OFF by default** and
best-effort — a capture failure never breaks the LLM call.

Consumer: ``execution/compare_backends.py --from-capture`` replays the captured
Claude prompts through Gemini ONLY (the Claude response is already captured), so
a cross-purpose backend corpus costs zero extra Claude spend.

One JSONL line per exchange in ``<LLM_CAPTURE_DIR>/capture_<YYYY-MM-DD>.jsonl``::

    {captured_at, purpose, ticker, scope, model, backend, run_id,
     prompt, response, prompt_sha256}

Filters:
  * ``LLM_CAPTURE_DIR`` unset ⇒ capture is off (the common case).
  * ``LLM_CAPTURE_PURPOSES`` (csv) ⇒ capture only those purposes; unset ⇒ all
    (minus the denylist).
  * The judge/eval purposes are NEVER captured — they read a corpus, and capturing
    them would feed the grader's own traffic back into the next comparison.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

LLM_CAPTURE_DIR_ENV = "LLM_CAPTURE_DIR"
LLM_CAPTURE_PURPOSES_ENV = "LLM_CAPTURE_PURPOSES"

# Never capture eval/judge/steering traffic — it would pollute a comparison
# corpus with the grading calls that consume it (isolation invariant I4,
# meta_eval_governance.md §5). backend_compare_judge = the pairwise judge;
# eval_judge = the general LLM-evals harness's judge; case_difficulty_classify =
# the sweep sampler's difficulty classifier (its prompts EMBED captured
# production prompts — recapturing them would nest corpora).
CAPTURE_DENYLIST: frozenset[str] = frozenset(
    {
        "backend_compare_judge",
        "eval_judge",
        "case_difficulty_classify",
        "optimizer_nominator",
        "model_frontier_research",
        "query_criteria_derive",
        "prompt_variant_propose",
    }
)


def capture_dir() -> Path | None:
    """The capture directory, or None when capture is disabled."""
    raw = os.environ.get(LLM_CAPTURE_DIR_ENV)
    if not raw or not raw.strip():
        return None
    return Path(raw.strip())


def _purpose_allowlist() -> frozenset[str] | None:
    raw = os.environ.get(LLM_CAPTURE_PURPOSES_ENV)
    if not raw or not raw.strip():
        return None
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def should_capture(purpose: str | None) -> bool:
    """Whether this purpose's exchange should be captured right now."""
    if capture_dir() is None:
        return False
    if purpose in CAPTURE_DENYLIST:
        return False
    allow = _purpose_allowlist()
    if allow is None:
        return True
    return purpose is not None and purpose in allow


def capture_exchange(
    *,
    prompt: str,
    response: str,
    purpose: str | None,
    ticker: str | None,
    scope: str | None,
    model: str,
    run_id: str | None,
    backend: str = "claude",
) -> None:
    """Best-effort append of one prompt/response exchange to the capture log.

    A no-op unless ``LLM_CAPTURE_DIR`` is set and ``purpose`` passes the filter.
    Never raises — capture is telemetry, it must not block the LLM call (same
    contract as the ledger writes in ``llm.ledger``).
    """
    try:
        if not should_capture(purpose):
            return
        directory = capture_dir()
        if directory is None:  # re-checked for the type-narrower; should_capture covered it
            return
        directory.mkdir(parents=True, exist_ok=True)

        from llm_call_ledger import sha256_text

        # Repo convention: naive-UTC stamps (project_naive_utc_datetime_convention).
        now = datetime.now(UTC).replace(tzinfo=None)
        record = {
            "captured_at": now.isoformat(),
            "purpose": purpose,
            "ticker": ticker,
            "scope": scope,
            "model": model,
            "backend": backend,
            "run_id": run_id,
            "prompt": prompt,
            "response": response,
            "prompt_sha256": sha256_text(prompt),
        }
        path = directory / f"capture_{now.strftime('%Y-%m-%d')}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # best-effort: telemetry never blocks the call
        log.debug({"event": "llm_capture_failed", "error": str(exc)})
