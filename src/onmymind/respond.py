"""The Ledger answer core — the ONE place a captured thought gets answered.

The old Ledger was a filing cabinet: it captured a thought and offered you
buttons to sort it into inert queues, but it never *responded*. A question
like "What's my cost basis on MELI?" got a "WONDERING / IN RESEARCH" badge
instead of an answer — even though the platform already knows the number.

This module closes that gap. When a landed capture reads as a question/request
the assistant can answer now, we run the unified ask engine
(:func:`ask.engine.respond_turn` over a portfolio :class:`ContextPack`) ONCE at
capture time — a fire-and-forget tap beside the wondering / pledge / artifact
taps in both capture entry points — and STORE the answer on the note
(``context_json['ledger_answer']``).

Both surfaces then render the stored answer with NO LLM on the read path: the
web feed card paints ``ledger_answer``; the Telegram poller replies with the
returned text. One brain, two mouths — the same discipline that keeps
``act_on_feed_item`` the single action core for the ladder verbs, and mirrors
``research.brief``'s stored-brief pattern (generate once, render cheap).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ask.context import build_portfolio_pack
from ask.engine import AskTurn, fold_events, respond_turn
from user_state.notes import get_note, patch_note_context

log = logging.getLogger(__name__)

# Answers run by DEFAULT (the payoff of the overhaul); ``LEDGER_ANSWER=0`` is the
# kill switch for the LLM spend each answer incurs — same on-by-default posture
# as the artifact brief (``LEDGER_ARTIFACT_BRIEF``).
_OFF = frozenset({"0", "false", "no", "off", ""})

# Kinds that carry an answerable thought. Readings (docs/links) are briefed, not
# answered — the artifact tap owns them.
_ANSWERABLE_KINDS = frozenset({"musing", "observation"})


def answer_enabled() -> bool:
    return os.environ.get("LEDGER_ANSWER", "1").strip().lower() not in _OFF


def is_answerable_capture(text: str) -> bool:
    """Does this capture read as a question/request the assistant should answer?

    Deterministic and conservative — no LLM in the classifier (this runs on
    every landed capture). A false negative just leaves the thought filed as a
    musing (no worse than the old behavior); a false positive spends one answer
    call. Two signals:

      * an interrogative anywhere in the text — catches both "What's my cost
        basis on MELI?" and "Why can't you tell me my cost basis? The project
        has this detail" (the '?' is mid-string in the second);
      * an interrogative/request *lead* for the '?'-less phrasings ("Explain the
        MELI thesis", "Should I trim NU", "Tell me my cost basis"). Anchored at
        the start so a declarative musing that merely contains "is"/"are" mid-
        sentence ("This is a note to self") never matches.
    """
    t = (text or "").strip()
    if len(t) < 3:
        return False
    if "?" in t:
        return True
    low = t.lower()
    lead_words = (
        "what ",
        "what's",
        "whats ",
        "why ",
        "how ",
        "when ",
        "where ",
        "who ",
        "which ",
        "whose ",
        "should i ",
        "should we ",
        "can you ",
        "could you ",
        "would you ",
        "will you ",
        "do you ",
        "does ",
        "did ",
        "is ",
        "are ",
        "was ",
        "were ",
        "tell me ",
        "explain ",
        "remind me ",
        "show me ",
        "give me ",
    )
    return low.startswith(lead_words)


def answer_capture(
    note_id: int,
    *,
    repo_root: Path | str,
    db_path: Path | str,
) -> str | None:
    """Answer one captured question and store the answer on the note.

    Returns the answer text (for the Telegram reply / an immediate web echo), or
    ``None`` when answers are disabled, the note is missing / not answerable, or
    the engine produced nothing. NEVER raises — an answer failure is
    fire-and-forget and must never affect the capture that already landed.
    """
    if not answer_enabled():
        return None
    try:
        rr = Path(repo_root)
        dbp = Path(db_path)
        note = get_note(note_id, db_path=dbp)
        if note is None or note.kind not in _ANSWERABLE_KINDS:
            return None
        # An ambiguous capture ("NU vs MELI — add to which?") is a routing
        # question to the set-ticker candidate buttons, not a portfolio question
        # to answer — the disambiguation keyboard is the right response, so skip.
        if (note.context or {}).get("needs_ticker"):
            return None
        if not is_answerable_capture(note.body):
            return None
        pack = build_portfolio_pack(rr, dbp)
        # A ticker-scoped capture answers against that name; otherwise the
        # portfolio-wide pack's defaults apply.
        turn = AskTurn(text=note.body, tickers=[note.ticker] if note.ticker else [])
        folded = fold_events(respond_turn(turn, pack, db_path=dbp, repo_root=rr))
        # narrative → 'text'; a chartable question routes to data → 'message'
        # (the reconciled one-line summary of the view). Either is a real answer.
        text = str(folded.get("text") or folded.get("message") or "").strip()
        if folded.get("status") != "ok" or not text:
            log.info({"event": "ledger_answer_empty", "note_id": note_id})
            return None
        patch_note_context(
            note_id, {"ledger_answer": {"text": text, "status": "ok"}}, db_path=dbp
        )
        return text
    except Exception:  # an answer must never break capture
        log.warning({"event": "ledger_answer_failed", "note_id": note_id}, exc_info=True)
        return None


__all__ = ["answer_capture", "answer_enabled", "is_answerable_capture"]
