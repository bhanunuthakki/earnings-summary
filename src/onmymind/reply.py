"""ledger_reply_intent — route a free-text reply on a feed card.

The overhaul's contextual buttons still made the OWNER the router: each card
type carried its own verb menu (Research it / Save / Worldview / Dismiss…).
Phase B collapses that to ONE universal interaction — a reply box — and lets
the model route the utterance: "dig into this" → research, "keep this one" →
save, "this is how I invest" → worldview, "what changed since?" → an inline
chat turn. The static verb taxonomy was a keyword gate on intelligence
(the owner's LLM-maximalist rule); the reply box removes it.

Closed enum, FAST tier, injected ``call=`` for tests — the ``capture_intent``
recipe (``research/intent.py``) verbatim. Fail-open to ``question``: an
unroutable reply becomes a conversation, never a destructive action.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

PURPOSE = "ledger_reply_intent"

# The closed reply-intent enum. 'question' = converse (an inline chat turn);
# 'note' = an additive owner comment kept on the card, no routing.
REPLY_INTENTS: tuple[str, ...] = (
    "research",
    "save",
    "worldview",
    "dismiss",
    "question",
    "note",
)

# Reply intents that map onto the existing feed action core (one brain: the
# same act_on_feed_item the buttons and the Telegram keyboard call).
ACTION_INTENTS: frozenset[str] = frozenset({"research", "save", "worldview", "dismiss"})

# reply intent → act_on_feed_item ladder verb ('research' is the odd rename).
ACTION_VERB: dict[str, str] = {
    "research": "incorporate",
    "save": "save",
    "worldview": "worldview",
    "dismiss": "dismiss",
}

# (card_text, reply_text) -> the LLM's schema-validated dict.
ReplyCall = Callable[[str, str], "dict[str, object]"]


@dataclass(frozen=True, slots=True)
class ReplyVerdict:
    intent: str  # one of REPLY_INTENTS
    reason: str = ""

    @property
    def is_action(self) -> bool:
        return self.intent in ACTION_INTENTS


def _build_prompt(card_text: str, reply_text: str) -> str:
    return (
        "An investor replied to one card in their captured-thoughts feed. Route the reply.\n"
        "Return exactly one intent:\n"
        "- research: they want this looked into / dug into / evaluated / researched.\n"
        "- save: they want it kept / saved / parked for later.\n"
        "- worldview: they state a durable belief about HOW they invest (a principle or "
        "lesson about their own process, not a fact about one company).\n"
        "- dismiss: they want it dropped / dismissed / cleared / marked done or irrelevant.\n"
        "- question: they ask a question or want to discuss — anything that deserves an "
        "answer in conversation.\n"
        "- note: an additive comment or context worth keeping on the card, with no "
        "question and no routing request.\n\n"
        "When unsure between routing and conversing, choose 'question' — answering is "
        "always safe; acting is not.\n\n"
        f"Card: {card_text[:800]}\n\n"
        f"Reply: {reply_text[:800]}\n\n"
        'Return JSON ONLY: {"intent": "research|save|worldview|dismiss|question|note", '
        '"reason": "<one line>"}'
    )


def _default_call(card_text: str, reply_text: str) -> dict[str, object]:
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        _build_prompt(card_text, reply_text),
        purpose=PURPOSE,
        expect="object",
        required_keys=("intent",),
    )
    return cast("dict[str, object]", obj) if isinstance(obj, dict) else {}


def classify_reply(
    card_text: str,
    reply_text: str,
    *,
    call: ReplyCall | None = None,
) -> ReplyVerdict:
    """Classify one reply. An unknown/missing intent from the model falls back
    to ``question`` — fail open to conversation, never to an action."""
    raw = (call or _default_call)(card_text, reply_text)
    raw_intent = raw.get("intent")
    intent = raw_intent.strip().lower() if isinstance(raw_intent, str) else ""
    if intent not in REPLY_INTENTS:
        intent = "question"
    return ReplyVerdict(intent=intent, reason=str(raw.get("reason") or "")[:200])


__all__ = [
    "ACTION_INTENTS",
    "ACTION_VERB",
    "PURPOSE",
    "REPLY_INTENTS",
    "ReplyVerdict",
    "classify_reply",
]
