# pyright: reportPrivateUsage=false
"""Tests for the DOM-structured roic.ai transcript extraction (2026-07-25 fix).

`_strip_html` + the old letter-prefix paragraph heuristic silently collapsed
whole calls to a single speaker turn whenever a page didn't spell a name in
the exact "<Letter> <FullName>" shape the heuristic was reverse-engineered
from (verified against a live NU_Q1_2026 page: 55k chars -> ONE "Operator"
turn). roic.ai's actual DOM marks each message with
`data-cy="transcripts_call_message"` and isolates the real speaker name in a
`<p data-transcript-speaker-name="true">` — a genuine structural signal, not a
rendering artifact. These tests pin the new DOM parser, the Q&A boundary
slicing (including the IR-officer handoff variant), and the safe fallback
when the DOM shape doesn't match.
"""

from __future__ import annotations

from aggregator_sources import (
    _parse_roic_messages,
    _serialize_turns,
    _slice_qa_turns,
)
from compute.transcript_ingest import segment_by_speaker


def _message(speaker_name: str, body_html: str, *, avatar_letter: str = "X") -> str:
    return (
        '<div class="flex" data-cy="transcripts_call_message">'
        '<div class="flex w-11/12">'
        '<div data-transcript-avatar="true" class="hidden h-10 w-10">'
        f'<span class="text-2sm">{avatar_letter}</span></div>'
        '<div class="mr-4"></div>'
        '<div class="relative">'
        f'<p data-transcript-speaker-name="true" class="text-heading">{speaker_name}</p>'
        f'<span class="text-2sm">{body_html}</span>'
        "</div></div></div>"
    )


def test_parse_roic_messages_isolates_name_from_avatar_letter() -> None:
    # The avatar's bare initial ("B") must never bleed into the body text —
    # that concatenation ("B Bipul Sinha...") was the whole reason the old
    # heuristic existed and the whole reason it was fragile.
    html = _message("Bipul Sinha", "<p>Thanks for the question.</p>", avatar_letter="B")
    turns = _parse_roic_messages(html)
    assert turns == [("Bipul Sinha", "Thanks for the question.")]


def test_parse_roic_messages_preserves_call_order() -> None:
    html = _message("Operator", "<p>Welcome everyone.</p>", avatar_letter="O") + _message(
        "Jane Doe", "<p>Thank you for joining.</p>", avatar_letter="J"
    )
    turns = _parse_roic_messages(html)
    assert [t[0] for t in turns] == ["Operator", "Jane Doe"]


def test_parse_roic_messages_returns_empty_when_dom_shape_absent() -> None:
    # A page that doesn't use roic's message-block markup at all (e.g. an
    # error page, or a future redesign) must degrade to [] so `_roic_fetch`
    # can fall back rather than silently emitting nothing useful.
    assert _parse_roic_messages("<html><body>Not a transcript page</body></html>") == []


def test_slice_qa_turns_finds_standard_operator_boundary() -> None:
    turns = [
        ("Operator", "Welcome to the call."),
        ("CEO Name", "Thanks everyone for prepared remarks."),
        ("Operator", "Our first question comes from Jane Analyst."),
        ("Jane Analyst", "What about margins?"),
        ("CEO Name", "Margins are strong."),
        ("Operator", "That concludes today's conference call. You may now disconnect."),
    ]
    scoped, found = _slice_qa_turns(turns)
    assert found is True
    assert scoped[0][0] == "Operator"
    assert "first question" in scoped[0][1]
    # QA_TAIL_RE's first-matching alternative ("...concludes...call") wins and
    # truncates there; the rest of that closing sentence is dropped with it.
    assert scoped[-1][1].endswith("conference call")
    assert "CEO Name" in [name for name, _ in scoped[1:]]
    assert scoped[1] == ("Jane Analyst", "What about margins?")


def test_slice_qa_turns_finds_ir_officer_handoff_variant() -> None:
    # NU's convention: the IR officer (not the operator) runs the queue —
    # "could you please open the line for Mr. Jorge Kuri from Morgan Stanley?"
    turns = [
        ("Operator", "Welcome to the call."),
        ("CEO Name", "Prepared remarks go here."),
        ("IR Officer", "Could you please open the line for Mr. Jorge Kuri from Morgan Stanley?"),
        ("Jorge Kuri", "Congrats on the results. My question is about X."),
        ("CEO Name", "Thanks Jorge, on that topic..."),
    ]
    scoped, found = _slice_qa_turns(turns)
    assert found is True
    assert scoped[0][0] == "IR Officer"
    assert scoped[1] == ("Jorge Kuri", "Congrats on the results. My question is about X.")


def test_slice_qa_turns_keeps_whole_call_when_no_boundary_matches() -> None:
    # No recognizable start cue anywhere -> return everything (a visible,
    # logged degrade — see _roic_fetch's log.warning) rather than dropping
    # the transcript entirely.
    turns = [
        ("Operator", "Welcome to the call, unusual script with no standard cue."),
        ("CEO Name", "Some remarks."),
    ]
    scoped, found = _slice_qa_turns(turns)
    assert found is False
    assert scoped == turns


def test_serialized_turns_resplit_correctly_by_existing_ingest_regex() -> None:
    """End-to-end: DOM turns -> serialized text -> the SAME `segment_by_speaker`
    the PDF ingest path already uses recovers every real turn boundary,
    instead of collapsing to one blob."""
    turns = [
        ("Operator", "Our first question comes from Jane Analyst."),
        ("Jane Analyst", "What drove the margin expansion this quarter?"),
        ("CEO Name", "Great question — three factors drove it."),
        ("Jane Analyst", "Understood, thank you."),
    ]
    text = _serialize_turns(turns)
    resplit = segment_by_speaker(text)
    assert [t.speaker for t in resplit] == [
        "Operator",
        "Jane Analyst",
        "CEO Name",
        "Jane Analyst",
    ]
    assert resplit[1].text == "What drove the margin expansion this quarter?"
