"""
src/llm/ — LLM client submodules extracted from src/llm_client.py.

Layout:
    anchors  — thesis / bear-case anchor block builders
               (load_thesis_anchor, load_bear_anchor, compose_anchor_block).
    fallback — Gemini fallback policy + routing
               (try_gemini_fallback, GEMINI_FALLBACK_MODEL).
    cli      — Claude CLI subprocess wiring + the public call_llm /
               call_llm_with_web entry points + LLMBudgetExceeded.
    ledger   — best-effort llm_calls ledger writes
               (record_llm_call, fallback_call_logged).

The user-facing surface stays in src/llm_client.py: per-purpose generators
(generate_summary, generate_thesis_update, generate_bear_case, etc.) and the
prompt-rendering constants still live there. llm_client.py re-exports every
public name below so callers keep using `from llm_client import call_llm`
as before; the names below are equally importable as `from llm import call_llm`
when a new caller wants to skip the legacy module path.
"""

from __future__ import annotations

from llm.anchors import (
    ANCHOR_BLOCK_CHAR_CAP,
    IR_ANCHOR_CHAR_CAP,
    PRIORS_ANCHOR_CHAR_CAP,
    compose_anchor_block,
    load_bear_anchor,
    load_ir_anchor,
    load_priors_anchor,
    load_thesis_anchor,
)
from llm.cli import (
    CLAUDE_WEB_TIMEOUT_SECONDS,
    CLAUDE_WEB_TOOLS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    FAST_CLASSIFIER_MODEL,
    LLM_MODELS,
    LLMBudgetExceeded,
    call_llm,
    call_llm_with_web,
)
from llm.fallback import (
    GEMINI_FALLBACK_MODEL,
    is_fallback_disabled,
    try_gemini_fallback,
)
from llm.ledger import (
    fallback_call_logged,
    record_llm_call,
)
from llm.style import (
    NUMBER_FORMATTING_BLOCK,
    compose_brief_prompt,
    style_block_cache_token,
)

__all__ = [
    "ANCHOR_BLOCK_CHAR_CAP",
    "CLAUDE_WEB_TIMEOUT_SECONDS",
    "CLAUDE_WEB_TOOLS",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "FAST_CLASSIFIER_MODEL",
    "GEMINI_FALLBACK_MODEL",
    "IR_ANCHOR_CHAR_CAP",
    "LLM_MODELS",
    "NUMBER_FORMATTING_BLOCK",
    "PRIORS_ANCHOR_CHAR_CAP",
    "LLMBudgetExceeded",
    "call_llm",
    "call_llm_with_web",
    "compose_anchor_block",
    "compose_brief_prompt",
    "fallback_call_logged",
    "is_fallback_disabled",
    "load_bear_anchor",
    "load_ir_anchor",
    "load_priors_anchor",
    "load_thesis_anchor",
    "record_llm_call",
    "style_block_cache_token",
    "try_gemini_fallback",
]
