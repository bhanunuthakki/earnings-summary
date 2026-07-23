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

B3: the primary gate is now :func:`capture.triage.classify_capture_triage`, not
the regex ``is_answerable_capture`` — a grounded 3-way call (answer_now /
contradiction / plain) that catches a musing CUTTING AGAINST a standing belief
or open decision, which a question-shaped-text regex has no way to see. The
regex survives as triage's own transient-failure fallback.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from ask.context import build_portfolio_pack
from ask.engine import AskTurn, fold_events, respond_turn
from capture.triage import TriageVerdict, classify_capture_triage
from user_state.notes import get_note, patch_note_context

log = logging.getLogger(__name__)

# conflict_kind -> the challenge's kind-label. 'decision' is handled specially
# (needs the ticker); everything else is a flat label lookup.
_CONFLICT_KIND_LABELS: dict[str, str] = {
    "tenet": "tenet",
    "stance": "stance",
    "musing": "recent note",
}

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

    B3: no longer the primary gate — :func:`capture.triage.classify_capture_triage`
    is (a grounded 3-way call that also catches a musing CONTRADICTING a
    standing belief, which this regex can't see). This is now that classifier's
    documented transient-failure fallback: deterministic and conservative — no
    LLM (safe to run when the LLM layer itself is what just failed). A false
    negative just leaves the thought filed as a musing; a false positive spends
    one answer call. Two signals:

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


def will_answer(note_id: int, *, db_path: Path | str) -> bool:
    """Would :func:`answer_capture` attempt to spin up the answer thread for
    this note? B3: triage (not question-shaped text) now decides answer_now /
    contradiction / plain INSIDE :func:`answer_capture`, so this can no longer
    pre-evaluate that decision without the LLM call it exists to avoid. It now
    checks only the cheap, non-LLM gates (enabled / kind / needs_ticker) — the
    web route uses a True here to mark ``ledger_answer_pending`` and hand off
    to a background thread for EVERY answerable-kind capture, trusting triage
    to decide 'plain' and clear the flag when there's nothing to answer."""
    if not answer_enabled():
        return False
    try:
        note = get_note(note_id, db_path=Path(db_path))
        if note is None or note.kind not in _ANSWERABLE_KINDS:
            return False
        return not (note.context or {}).get("needs_ticker")
    except Exception:
        return False


def _compose_challenge(verdict: TriageVerdict) -> str:
    """Deterministic challenge text for a ``contradiction`` triage verdict — NO
    second LLM call. Triage already resolved the conflict's kind/id/body/as_of
    against exactly the rows it showed the model (the grounding gate), so this
    is pure string composition, not another judgment call."""
    if verdict.conflict_kind == "decision":
        label = f"open {verdict.conflict_ticker or '?'} decision"
    else:
        label = _CONFLICT_KIND_LABELS.get(verdict.conflict_kind or "", "prior note")
    since = f" from {verdict.conflict_as_of[:10]}" if verdict.conflict_as_of else ""
    body = verdict.conflict_body[:140].strip()
    why = f" — {verdict.why}" if verdict.why else ""
    return f'⚡ This cuts against your {label}{since}: "{body}"{why}. What changed?'


def answer_capture(
    note_id: int,
    *,
    repo_root: Path | str,
    db_path: Path | str,
) -> str | None:
    """Answer one captured question, challenge one that cuts against a standing
    belief, or leave a plain capture alone — and store the result on the note.

    B3: :func:`capture.triage.classify_capture_triage` is now the primary gate
    (route ``answer_now`` / ``contradiction`` / ``plain``), replacing the
    question-shaped-text regex :func:`is_answerable_capture` (still triage's
    own fallback when the LLM layer degrades).

    Returns the answer/challenge text (for the Telegram reply / an immediate
    web echo), or ``None`` when answers are disabled, the note is missing /
    not answerable, triage routed it ``plain``, or the engine produced
    nothing. NEVER raises — an answer failure is fire-and-forget and must
    never affect the capture that already landed.

    Always clears ``ledger_answer_pending`` on the way out (set by the web
    route before it hands this call to a background thread), so a card can
    never show "Answering…" forever after a failed or empty answer. On an
    unhandled exception, best-effort stamps ``ledger_answer.status='failed'``
    on the note — the 2026-07 zero-fire incident was undiagnosable from data
    because the old handler logged and cleared the flag but left NO trace on
    the note itself of WHY nothing showed up.
    """
    if not answer_enabled():
        return None
    dbp = Path(db_path)
    try:
        rr = Path(repo_root)
        note = get_note(note_id, db_path=dbp)
        if note is None or note.kind not in _ANSWERABLE_KINDS:
            return None
        # An ambiguous capture ("NU vs MELI — add to which?") is a routing
        # question to the set-ticker candidate buttons, not a portfolio question
        # to answer — the disambiguation keyboard is the right response, so skip.
        if (note.context or {}).get("needs_ticker"):
            return None
        verdict = classify_capture_triage(note.body, note_id=note_id, db_path=dbp)
        if verdict.route == "plain":
            log.info({"event": "ledger_answer_triage_plain", "note_id": note_id})
            patch_note_context(note_id, {"ledger_answer_pending": False}, db_path=dbp)
            return None
        if verdict.route == "contradiction":
            challenge = _compose_challenge(verdict)
            patch_note_context(
                note_id,
                {
                    "ledger_answer": {"text": challenge, "status": "ok", "kind": "contradiction"},
                    "tension_ref": {
                        "kind": verdict.conflict_kind,
                        "id": verdict.conflict_id,
                        "why": verdict.why,
                    },
                    "ledger_answer_pending": False,
                },
                db_path=dbp,
            )
            return challenge
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
            patch_note_context(note_id, {"ledger_answer_pending": False}, db_path=dbp)
            return None
        patch_note_context(
            note_id,
            {"ledger_answer": {"text": text, "status": "ok"}, "ledger_answer_pending": False},
            db_path=dbp,
        )
        return text
    except Exception as exc:  # an answer must never break capture
        log.warning({"event": "ledger_answer_failed", "note_id": note_id}, exc_info=True)
        with contextlib.suppress(Exception):
            patch_note_context(
                note_id,
                {
                    "ledger_answer": {"status": "failed", "error": exc.__class__.__name__},
                    "ledger_answer_pending": False,
                },
                db_path=dbp,
            )
        return None


def answer_text(
    text: str,
    *,
    tickers: Sequence[str] = (),
    repo_root: Path | str,
    db_path: Path | str,
) -> str | None:
    """Answer an arbitrary Ledger question through the unified ask engine and
    return the text — no note lookup, no storage.

    The card-reply *chat* path (a follow-up question thumbed under a card on
    Telegram, or typed in the web reply box) calls this so it gets the SAME
    engine answer :func:`answer_capture` stores at capture time — one brain. It
    deliberately skips the answerability heuristic (:func:`is_answerable_capture`)
    because the reply classifier already decided this reply is a question.
    Returns ``None`` when answers are disabled or the engine produced nothing;
    NEVER raises (an answer failure is fire-and-forget)."""
    if not answer_enabled():
        return None
    try:
        rr = Path(repo_root)
        dbp = Path(db_path)
        pack = build_portfolio_pack(rr, dbp)
        turn = AskTurn(text=text, tickers=list(tickers))
        folded = fold_events(respond_turn(turn, pack, db_path=dbp, repo_root=rr))
        out = str(folded.get("text") or folded.get("message") or "").strip()
        if folded.get("status") != "ok" or not out:
            return None
        return out
    except Exception:  # answering must never break the caller
        log.warning({"event": "ledger_answer_text_failed"}, exc_info=True)
        return None


__all__ = [
    "answer_capture",
    "answer_enabled",
    "answer_text",
    "is_answerable_capture",
    "will_answer",
]
