"""Cross-session memory — the lightweight per-ticker conclusion STUB (L9).

The standup is the advisor's first persistent voice, so it needs a thread of
memory: when it briefs NU today it should know what it concluded about NU last
time. This is the deliberately-small version of that — a one-line distilled
conclusion stored on each delivered ``standup_messages`` row, recalled into the
NEXT framing for the same name. A real per-ticker conclusion store (with its
own lifecycle and supersession) is a deferred follow-on
(directives/close_the_loops_2026_06.md §4 decision 3).

The hazard the stub must respect: naive memory re-asserts a conclusion the
current evidence has since invalidated. So recall is framed as a PRIOR, never as
ground truth — ``standup.compose`` injects it with an explicit "the current
evidence below may have already moved this; re-derive, do not re-assert" caveat.
The recall here only fetches the prior; the caveat lives at the framing seam.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from identity import DEFAULT_USER_ID
from standup.ledger import STATUS_DELIVERED

# A conclusion is one line; clip the distilled head here.
_MAX_CONCLUSION_CHARS = 240
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class PriorConclusion:
    """A previously-recorded standup read for a ticker — a PRIOR, not truth."""

    conclusion: str
    recorded_on: str  # YYYY-MM-DD
    signal_kind: str


def distill_conclusion(message_text: str) -> str:
    """The one-line distilled read stored as memory. Deterministic stub: the
    lead sentence of the composed brief, whitespace-collapsed and clipped — no
    extra LLM call. Good enough to thread continuity; the deferred full store
    can do better."""
    flat = " ".join((message_text or "").split())
    if not flat:
        return ""
    first = _SENTENCE_END.split(flat, maxsplit=1)[0]
    out = first if len(first) <= _MAX_CONCLUSION_CHARS else first[: _MAX_CONCLUSION_CHARS - 1]
    if len(out) >= _MAX_CONCLUSION_CHARS - 1 and not out.endswith(("…", ".", "!", "?")):
        out = out.rstrip() + "…"
    return out


def recall_conclusion(
    conn: sqlite3.Connection, *, user_id: str = DEFAULT_USER_ID, ticker: str | None
) -> PriorConclusion | None:
    """The most recent delivered standup conclusion for a ticker, or None.
    Book-level (ticker-less) signals have no per-ticker prior to recall."""
    if not ticker:
        return None
    try:
        row = conn.execute(
            "SELECT conclusion, created_at, signal_kind FROM standup_messages "
            "WHERE user_id = ? AND UPPER(ticker) = ? AND status = ? "
            "  AND conclusion IS NOT NULL AND conclusion != '' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, ticker.upper(), STATUS_DELIVERED),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return PriorConclusion(
        conclusion=str(row[0]),
        recorded_on=str(row[1] or "")[:10],
        signal_kind=str(row[2] or ""),
    )


__all__ = ["PriorConclusion", "distill_conclusion", "recall_conclusion"]
