"""
src/llm/ — LLM client submodules extracted from src/llm_client.py.

Layout:
    anchors  — thesis / bear-case anchor block builders
               (load_thesis_anchor, load_bear_anchor, compose_anchor_block).
    fallback — Gemini fallback policy + routing
               (try_gemini_fallback, GEMINI_FALLBACK_MODEL).
    cli      — Claude CLI subprocess wiring + the public call_llm /
               call_llm_with_web entry points + LLMBudgetExceeded.
    gemini_backend — Gemini Developer API (metered key) second backend,
               eval-gated via an empty-by-default purpose allowlist
               (call_gemini, gemini_allowed_purposes; see
               directives/gemini_backend.md).
    openrouter_backend — OpenRouter (metered key) THIRD backend: an
               OpenAI-compatible gateway to the cheap open-weight candidate
               pool (DeepSeek/Qwen/...) with provider pinning for stable model
               identity (call_openrouter; see directives/openrouter_backend.md).
    backend_judge — pairwise Claude-vs-Gemini judge over the compare corpus;
               grades a purpose for the gemini_backend allowlist
               (judge_pair, aggregate_by_purpose; CLI execution/grade_backends.py).
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
    WORLDVIEW_ANCHOR_CHAR_CAP,
    compose_anchor_block,
    load_bear_anchor,
    load_ir_anchor,
    load_priors_anchor,
    load_thesis_anchor,
    load_worldview_anchor,
)
from llm.backend_judge import (
    JUDGE_PURPOSE as BACKEND_COMPARE_JUDGE_PURPOSE,
)
from llm.backend_judge import (
    aggregate_by_purpose,
    cross_judge_agreement,
    judge_pair,
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
from llm.gemini_backend import (
    GEMINI_BACKEND_ALLOWED_PURPOSES,
    GEMINI_BACKEND_DEFAULT_MODEL,
    GEMINI_BACKEND_FAST_MODEL,
    call_gemini,
    gemini_allowed_purposes,
    gemini_model_for,
)
from llm.ledger import (
    fallback_call_logged,
    record_llm_call,
)
from llm.openrouter_backend import (
    OPENROUTER_BACKEND_ALLOWED_PURPOSES,
    OPENROUTER_BACKEND_DEFAULT_MODEL,
    call_openrouter,
    openrouter_model_for,
)
from llm.style import (
    NUMBER_FORMATTING_BLOCK,
    compose_brief_prompt,
    style_block_cache_token,
)
from llm.untrusted import (
    WEB_CONTENT_NOTICE,
    spotlight,
)

__all__ = [
    "ANCHOR_BLOCK_CHAR_CAP",
    "BACKEND_COMPARE_JUDGE_PURPOSE",
    "CLAUDE_WEB_TIMEOUT_SECONDS",
    "CLAUDE_WEB_TOOLS",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "FAST_CLASSIFIER_MODEL",
    "GEMINI_BACKEND_ALLOWED_PURPOSES",
    "GEMINI_BACKEND_DEFAULT_MODEL",
    "GEMINI_BACKEND_FAST_MODEL",
    "GEMINI_FALLBACK_MODEL",
    "IR_ANCHOR_CHAR_CAP",
    "LLM_MODELS",
    "NUMBER_FORMATTING_BLOCK",
    "OPENROUTER_BACKEND_ALLOWED_PURPOSES",
    "OPENROUTER_BACKEND_DEFAULT_MODEL",
    "PRIORS_ANCHOR_CHAR_CAP",
    "WEB_CONTENT_NOTICE",
    "WORLDVIEW_ANCHOR_CHAR_CAP",
    "LLMBudgetExceeded",
    "aggregate_by_purpose",
    "call_gemini",
    "call_llm",
    "call_llm_with_web",
    "call_openrouter",
    "compose_anchor_block",
    "compose_brief_prompt",
    "cross_judge_agreement",
    "fallback_call_logged",
    "gemini_allowed_purposes",
    "gemini_model_for",
    "is_fallback_disabled",
    "judge_pair",
    "load_bear_anchor",
    "load_ir_anchor",
    "load_priors_anchor",
    "load_thesis_anchor",
    "load_worldview_anchor",
    "openrouter_model_for",
    "record_llm_call",
    "spotlight",
    "style_block_cache_token",
    "try_gemini_fallback",
]
