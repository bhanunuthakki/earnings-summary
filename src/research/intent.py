"""capture_intent — what does an owner musing WANT? (Phase-1 intent gate).

Supersedes the regex-gated wondering detector on the live tap: EVERY owner-
authored musing is classified by the cheap model, with NO lexical pre-gate. A
"curious about the takeaways here …" that the old ``_WONDER_RE`` allowlist
silently dropped (``gate='regex'`` → the LLM never fired) is exactly the recall
this recovers — the maximalist fix is to let semantics, not a keyword list,
decide whether intelligence runs. Only the TRUST-ZONE gate remains: a non-musing
or fetched/quoted body never reaches the LLM (the token-burn + prompt-injection
boundary), the one pre-gate worth keeping.

Four intents:
  * ``observation``     — a flat statement, a decision already made, or a mood.
                          No question, no request (the old ``is_wondering=False``).
  * ``wondering``       — a research-worthy question / doubt / request to look
                          into something NOT tied to an attached artifact (the old
                          ``is_wondering=True``; routes to an inert research chip).
  * ``brief_artifact``  — the musing points at an article / link / deck / "this"
                          and wants its takeaways / a summary / the gist.
  * ``stress_artifact`` — points at such an artifact and wants it challenged,
                          stress-tested, poked at, expanded, or dug into deeper
                          (the analyst has a read and wants it pressure-tested).

``wondering`` routes to the research tap; the two ``*_artifact`` intents route to
the artifact-brief pipeline (Phase 2/3); ``observation`` is inert. The LLM call is
injected (``call=``) so the gate is testable without the CLI; the default routes
through ``call_llm_structured`` (Haiku, purpose ``capture_intent``). The
``capture_intent`` golden set (evals/golden/capture_intent.json) is its bar.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

PURPOSE = "capture_intent"

# The closed intent enum. Order is the golden-set / prompt order, not a ranking.
INTENTS: tuple[str, ...] = ("observation", "wondering", "brief_artifact", "stress_artifact")
_ARTIFACT_INTENTS = frozenset({"brief_artifact", "stress_artifact"})

_NULLISH = frozenset({"", "null", "none", "n/a"})

# (text) -> the LLM's schema-validated dict.
IntentCall = Callable[[str], "dict[str, object]"]


@dataclass(frozen=True, slots=True)
class IntentVerdict:
    intent: str  # one of INTENTS
    claim: str = ""
    ticker: str | None = None
    # Which gate produced the verdict: trust_zone (pre-gate, no LLM) | llm.
    # Observability only — lets the tap distinguish a filtered non-musing from a
    # model-classified observation (they were indistinguishable under one bucket).
    gate: str = ""

    @property
    def is_wondering(self) -> bool:
        return self.intent == "wondering"

    @property
    def is_artifact(self) -> bool:
        return self.intent in _ARTIFACT_INTENTS


def _build_prompt(text: str) -> str:
    return (
        "Classify a captured investor musing by what the analyst WANTS. Return exactly "
        "one intent:\n"
        "- observation: a flat statement of fact, a decision already made, or a mood. No "
        "question and no request.\n"
        "- wondering: a research-worthy question, doubt, or request to look into / "
        "evaluate / add something that is NOT a specific attached article or deck.\n"
        "- brief_artifact: the musing points at an article, link, deck, PDF, or 'this' and "
        "wants its key takeaways, a summary, or the gist.\n"
        "- stress_artifact: the musing points at such an artifact and wants it challenged, "
        "stress-tested, poked at, expanded, or dug into more deeply (the analyst already "
        "has a read and wants it pressure-tested).\n\n"
        "Default to 'observation' when there is no question or request. Choose "
        "'brief_artifact' or 'stress_artifact' only when the musing points at external "
        "content (a URL, a named article/deck, or 'this'/'here'); pick 'stress_artifact' "
        "over 'brief_artifact' when the analyst wants critique/depth rather than a summary.\n\n"
        f"Musing: {text}\n\n"
        'Return JSON ONLY: {"intent": "observation|wondering|brief_artifact|stress_artifact", '
        '"claim": "<the ask in one line>", "ticker": "<TICKER or null>"}'
    )


def _default_call(text: str) -> dict[str, object]:
    from llm.contracts import INTENT_SCHEMA
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        _build_prompt(text),
        purpose=PURPOSE,
        expect="object",
        required_keys=("intent",),
        schema=INTENT_SCHEMA,
    )
    return cast("dict[str, object]", obj) if isinstance(obj, dict) else {}


def _norm_ticker(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned.upper() if cleaned.lower() not in _NULLISH else None


def classify_intent(
    text: str,
    *,
    kind: str = "musing",
    provenance: str = "derived",
    call: IntentCall | None = None,
) -> IntentVerdict:
    """Classify a musing's intent. Trust-zone pre-gate first (zero LLM tokens on a
    non-musing / fetched note); otherwise the governed classifier runs on EVERY
    owner musing (no lexical gate). An unknown/missing intent from the model falls
    back to ``observation`` — fail closed to the inert path, never fabricate an
    artifact action."""
    # Trust zone: only owner-authored musings ever reach the model.
    if kind != "musing" or provenance == "contains_fetched":
        return IntentVerdict(intent="observation", gate="trust_zone")

    raw = (call or _default_call)(text)
    raw_intent = raw.get("intent")
    intent = raw_intent.strip().lower() if isinstance(raw_intent, str) else ""
    if intent not in INTENTS:
        intent = "observation"
    return IntentVerdict(
        intent=intent,
        claim=(str(raw.get("claim") or text))[:500],
        ticker=_norm_ticker(raw.get("ticker")),
        gate="llm",
    )


def classify_intent_for_eval(text: str) -> dict[str, object]:
    """The eval-harness production entry point (golden_classifiers): text → the
    pinned fields the golden set grades."""
    verdict = classify_intent(text)
    return {"intent": verdict.intent, "ticker": verdict.ticker}
