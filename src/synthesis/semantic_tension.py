"""Semantic tenet-tension detection (B5) — the shared classifier both distill
paths (``synthesis.tenet_distill``, ``synthesis.session_distill``) call to
catch what the free SLUG-ONLY tension probe (``synthesis.tenets.
current_tenet_for_scope``) structurally cannot: two Tenets that are the same
underlying belief under two DIFFERENT ``scope_key`` slugs.

Prod evidence for the gap this closes: tenets 20
(``tenet:retirement-account-hold-discipline``) and 31
(``tenet:tax-account-holding-discipline``) are semantic duplicates the slug
probe never saw — they never shared a scope_key, so ``current_tenet_for_scope``
returned None for both and each landed as its own belief.

Contract (mirrors ``capture.triage``'s grounding-gate shape): render AT MOST
``_MAX_TENETS`` current tenets as ``[T<id>] (<scope_key>) <body>`` lines, ask
the model which ONE (if any) the candidate belief RESTATES or CONTRADICTS —
i.e. is the same underlying belief-topic despite different wording — and
resolve the answer against the SAME set rendered into the prompt. A token
pointing at nothing shown, an empty/null answer, or ANY exception from the
call layer (parse failure, budget block, missing CLI) all degrade to the
same outcome: None. A missed semantic tension is exactly the pre-B5 status
quo — never a reason to block a distill landing that would otherwise
succeed.

Zero current tenets (after excluding the caller's own scope, if any) is a
deterministic $0 short-circuit: the model never runs when there is nothing
to compare against.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast

from synthesis.insights import InsightRow
from synthesis.tenets import list_tenets

log = logging.getLogger(__name__)

PURPOSE = "tenet_semantic_tension"

# How many current tenets ride into the prompt — a hard cap on prompt size,
# not a quality knob. The Worldview is meant to stay small (tenet_distill's
# own prompt caps proposals to "at most 3"), so this is already generous
# headroom; the cap just keeps the prompt bounded if that assumption slips.
_MAX_TENETS = 15

# (candidate body_md, rendered "[T<id>] (<scope_key>) <body>" block) -> the
# LLM's schema-validated dict. Injected so this module is unit-testable
# without the CLI (mirrors capture.triage.TriageCall).
TensionCall = Callable[[str, str], "dict[str, object]"]


def _render_tenets(tenets: list[InsightRow]) -> tuple[str, dict[str, InsightRow]]:
    """The Shown-tenets block + the token->row map the grounding gate
    validates against — a token that survives resolution is always one the
    model actually saw (the ``capture.triage._render_sections`` pattern)."""
    lines: list[str] = []
    tokens: dict[str, InsightRow] = {}
    for t in tenets:
        tok = f"T{t.id}"
        body = " ".join(t.body_md.split())[:160]
        lines.append(f"[{tok}] ({t.scope_key}) {body}")
        tokens[tok] = t
    return "\n".join(lines), tokens


def _build_prompt(body_md: str, tenets_text: str) -> str:
    return (
        "An investor's Worldview holds the standing beliefs (Tenets) shown "
        "below. A NEW candidate belief is being considered. Does it RESTATE "
        "or CONTRADICT exactly ONE of the shown tenets — i.e. is it the same "
        "underlying belief-topic despite different wording (even under a "
        "different label/scope)? If so, name it; if it's genuinely a "
        "different topic, say null.\n\n"
        "Shown current tenets:\n" + tenets_text + "\n\n"
        f"Candidate belief: {body_md[:400]}\n\n"
        '`tension_with` MUST be one of the id tokens shown above (e.g. "T12"), '
        "or null. Never invent one. When unsure, choose null.\n\n"
        'Return JSON ONLY: {"tension_with": "<id token or null>", '
        '"why": "<one sentence or empty>"}'
    )


def _default_call(body_md: str, tenets_text: str) -> dict[str, object]:
    from llm.structured import call_llm_structured  # lazy: CI needs no CLI

    obj = call_llm_structured(
        _build_prompt(body_md, tenets_text),
        purpose=PURPOSE,
        expect="object",
        required_keys=("tension_with",),
    )
    return cast("dict[str, object]", obj) if isinstance(obj, dict) else {}


def detect_semantic_tension(
    body_md: str,
    *,
    exclude_scope_key: str | None = None,
    db_path: Path | str | None = None,
    call: TensionCall | None = None,
) -> InsightRow | None:
    """Does ``body_md`` restate or contradict a standing Tenet under a
    DIFFERENT scope_key than the caller's own? Returns the matched
    ``InsightRow``, or None.

    ``exclude_scope_key`` drops the row on the caller's own scope — a
    revision landing on the same slug is a SUPERSEDE (the free slug probe's
    job), never a "tension" against itself. Zero remaining tenets
    short-circuits with no LLM call.

    NEVER raises: any exception (call failure, malformed JSON, budget block,
    missing CLI) is logged as ``semantic_tension_transient`` and swallowed —
    a missed tension is the pre-B5 status quo, never a reason to block the
    caller's landing.
    """
    try:
        current = list_tenets(status="current", db_path=db_path)
        if exclude_scope_key is not None:
            current = [t for t in current if t.scope_key != exclude_scope_key]
        current = current[:_MAX_TENETS]
        if not current:
            return None  # deterministic $0 short-circuit — nothing to compare against

        tenets_text, tokens = _render_tenets(current)
        raw = (call or _default_call)(body_md, tenets_text)
        raw_token = raw.get("tension_with") if isinstance(raw, dict) else None
        token = raw_token.strip() if isinstance(raw_token, str) else ""
        if not token:
            return None
        # Grounding gate: the token must name a tenet actually rendered into
        # THIS prompt — never re-derived from the model's own claim about
        # what it saw. Missing/fabricated -> None, same as "no tension".
        return tokens.get(token)
    except Exception as exc:
        log.warning(
            {
                "event": "semantic_tension_transient",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None


__all__ = ["PURPOSE", "TensionCall", "detect_semantic_tension"]
