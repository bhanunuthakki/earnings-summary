"""wondering_detect — does an owner musing ask for research? (Phase-1 gate).

A deterministic regex pre-gate decides whether the LLM fires *at all*: a flat
observation ("NU's NPL ticked up") must never be misread as a request and costs
ZERO tokens — the asymmetry is encoded as the default verdict ``is_wondering=False``.

The pre-gate ALSO enforces the trust zone (not just the vocabulary): it fires
only on an OWNER-AUTHORED musing (``kind='musing'`` and provenance is not
``contains_fetched``), so fetched/quoted text — a pasted deck whose body reads
"ingest this into your model" — can never trip the gate. That closes a token-burn
+ prompt-injection vector cheaply, BEFORE any LLM call.

A positive verdict yields a ``research_tasks`` row (Wave W1-3); it NEVER auto-runs
the research pass. The LLM call is injected (``call=``) so the gate is testable
without the CLI; the default routes through ``call_llm_structured`` (Flash-Lite,
budget ``skip``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

PURPOSE = "wondering_detect"

# Action-trigger + interrogative vocabulary. Broad on purpose (the LLM is the
# precision stage); the trust-zone gate below is what makes a broad regex safe.
_WONDER_RE = re.compile(
    r"\b(do .* still|is .* really|why is|what if|i wonder|does .* hold|"
    r"how .* compare|should i|worth it|ingest this|add to watchlist|"
    r"look into|research this|dig into|give me (insights|a read|your take))\b",
    re.IGNORECASE,
)

_NULLISH = frozenset({"", "null", "none", "n/a"})

# (text) -> the LLM's schema-validated dict.
DetectCall = Callable[[str], "dict[str, object]"]


@dataclass(frozen=True, slots=True)
class WonderingVerdict:
    is_wondering: bool
    claim: str = ""
    ticker: str | None = None
    suggested_artifacts: tuple[str, ...] = field(default_factory=tuple)
    # Which gate produced the verdict: trust_zone | regex | llm_no | llm_yes.
    # Observability only — a negative verdict is otherwise indistinguishable
    # from a broken tap (the 2026-07-02 audit found 0 detect calls ever, with
    # no way to tell dormancy from breakage).
    gate: str = ""


def _build_prompt(text: str) -> str:
    return (
        "Decide whether this captured investor musing is a research-worthy WONDERING "
        "or REQUEST (it asks a question, expresses doubt, or asks you to look into / "
        "ingest / evaluate / add something) versus a FLAT OBSERVATION (a statement of "
        "fact with no question or request). Default to NOT a wondering when unsure — a "
        "false positive wastes a research run. A RETROSPECTIVE, post-mortem, or "
        "lesson-learned about an already-resolved trade (a past-tense conclusion such "
        'as "the lesson is...", "I sold too early", "that thesis worked/failed") '
        "is NOT a wondering — it is a belief for the Worldview, not an open, "
        "forward-looking research question.\n\n"
        f"Musing: {text}\n\n"
        'Return JSON ONLY: {"is_wondering": true|false, "claim": "<the question/request '
        'in one line>", "ticker": "<TICKER or null>", "suggested_artifacts": '
        '[<any of "memo","dcf","thesis","view">]}'
    )


def _default_call(text: str) -> dict[str, object]:
    from llm.contracts import WONDERING_SCHEMA
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        _build_prompt(text),
        purpose=PURPOSE,
        expect="object",
        required_keys=("is_wondering", "claim"),
        schema=WONDERING_SCHEMA,
    )
    return cast("dict[str, object]", obj) if isinstance(obj, dict) else {}


def _norm_ticker(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned.upper() if cleaned.lower() not in _NULLISH else None


def detect_wondering(
    text: str,
    *,
    kind: str = "musing",
    provenance: str = "derived",
    call: DetectCall | None = None,
) -> WonderingVerdict:
    """Classify a musing. Trust-zone + regex pre-gate first (zero LLM tokens on a
    non-musing / fetched / non-matching note); only then the governed classifier."""
    # Trust zone: only owner-authored musings can ever burn tokens or be acted on.
    if kind != "musing" or provenance == "contains_fetched":
        return WonderingVerdict(is_wondering=False, gate="trust_zone")
    if not _WONDER_RE.search(text or ""):
        return WonderingVerdict(is_wondering=False, gate="regex")

    raw = (call or _default_call)(text)
    if not raw.get("is_wondering"):
        return WonderingVerdict(
            is_wondering=False, claim=str(raw.get("claim") or ""), gate="llm_no"
        )

    arts = raw.get("suggested_artifacts")
    suggested = (
        tuple(str(a) for a in cast("list[object]", arts) if isinstance(a, str))
        if isinstance(arts, list)
        else ()
    )
    return WonderingVerdict(
        is_wondering=True,
        claim=(str(raw.get("claim") or text))[:500],
        ticker=_norm_ticker(raw.get("ticker")),
        suggested_artifacts=suggested,
        gate="llm_yes",
    )


def detect_for_eval(text: str) -> dict[str, object]:
    """The eval-harness production entry point (golden_classifiers): text → the
    pinned fields the golden set grades."""
    verdict = detect_wondering(text)
    return {
        "is_wondering": verdict.is_wondering,
        "claim": verdict.claim,
        "ticker": verdict.ticker,
    }
