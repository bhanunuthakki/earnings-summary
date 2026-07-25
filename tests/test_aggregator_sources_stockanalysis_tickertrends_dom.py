# pyright: reportPrivateUsage=false
"""Tests for the DOM-structured stockanalysis.com / tickertrends.io transcript
extraction (2026-07-25 fix, Disclosure Intelligence v1 PRD D1.5).

Mirrors `test_aggregator_sources_roic_dom.py`'s coverage for the two sources
the roic fix did not touch (11/308 of the transcript corpus were still going
through `_split_into_speaker_paragraphs`'s flatten-and-guess heuristic).
Verified live 2026-07-25 against WIX's real 1Q26 transcript pages on both
sites: stockanalysis.com renders 51 clean speaker-labeled turns as
`<div class="border-t border-sharp ...">` blocks with the name in a nested
`<div class="text-lg font-bold ...">`; tickertrends.io renders 41 as
`<p class="mb-2"><strong>Name:</strong> body</p>`. Neither shape survives
`_strip_html`'s flattening, which is why the old heuristic degraded them the
same way it degraded roic.
"""

from __future__ import annotations

from aggregator_sources import (
    _parse_stockanalysis_messages,
    _parse_tickertrends_messages,
    _serialize_turns,
    _slice_qa_turns,
)
from compute.transcript_ingest import segment_by_speaker

# --- stockanalysis.com -------------------------------------------------------


def _sa_turn(name: str, body: str, *, role: str | None = None) -> str:
    role_html = f'<div class="text-sm italic text-muted">{role}</div>' if role else ""
    return (
        '<div class="border-t border-sharp pt-5 first:border-t-0 first:pt-0">'
        f'<div class="text-lg font-bold text-default md:text-xl">{name}</div>'
        f"{role_html}"
        f'<p class="text-default mt-2">'
        f'<span class="transcript-sentence" data-start-sec="0" data-end-sec="1">{body}</span>'
        "</p></div>"
    )


def test_parse_stockanalysis_messages_isolates_name_from_role() -> None:
    # The role/title line ("Head of Investor Relations, Wix.com") must never
    # bleed into the body — a real case on WIX's own page.
    html = _sa_turn(
        "Emily Liu", "Thanks, and good morning.", role="Head of Investor Relations, Wix.com"
    )
    turns = _parse_stockanalysis_messages(html)
    assert turns == [("Emily Liu", "Thanks, and good morning.")]


def test_parse_stockanalysis_messages_handles_turn_with_no_role() -> None:
    html = _sa_turn("Operator", "Good day, and thank you for standing by.")
    turns = _parse_stockanalysis_messages(html)
    assert turns == [("Operator", "Good day, and thank you for standing by.")]


def test_parse_stockanalysis_messages_preserves_call_order() -> None:
    html = _sa_turn("Operator", "Welcome everyone.") + _sa_turn(
        "Jane Doe", "Thank you for joining.", role="CFO"
    )
    turns = _parse_stockanalysis_messages(html)
    assert [t[0] for t in turns] == ["Operator", "Jane Doe"]


def test_parse_stockanalysis_messages_returns_empty_when_dom_shape_absent() -> None:
    assert _parse_stockanalysis_messages("<html><body>Not a transcript page</body></html>") == []


# --- tickertrends.io ----------------------------------------------------------


def _tt_turn(name: str, body: str) -> str:
    return f'<p class="mb-2"><strong>{name}:</strong> {body}</p>'


def test_parse_tickertrends_messages_strips_trailing_colon_from_name() -> None:
    html = _tt_turn("Nir Zohar", "Thank you, Emily. Hello, everyone.")
    turns = _parse_tickertrends_messages(html)
    assert turns == [("Nir Zohar", "Thank you, Emily. Hello, everyone.")]


def test_parse_tickertrends_messages_preserves_call_order() -> None:
    html = _tt_turn("Operator", "Welcome everyone.") + _tt_turn(
        "Jane Doe", "Thank you for joining."
    )
    turns = _parse_tickertrends_messages(html)
    assert [t[0] for t in turns] == ["Operator", "Jane Doe"]


def test_parse_tickertrends_messages_ignores_a_second_strong_tag_in_the_body() -> None:
    # Only the FIRST <strong> in a paragraph is the speaker name -- an
    # emphasized word inside the answer must not be mistaken for a new turn.
    html = '<p class="mb-2"><strong>Analyst:</strong> What about <strong>margins</strong> this quarter?</p>'
    turns = _parse_tickertrends_messages(html)
    assert turns == [("Analyst", "What about margins this quarter?")]


def test_parse_tickertrends_messages_returns_empty_when_dom_shape_absent() -> None:
    assert _parse_tickertrends_messages("<html><body>Not a transcript page</body></html>") == []


# --- shared re-splitting, both sources -----------------------------------


def test_stockanalysis_turns_resplit_correctly_by_existing_ingest_regex() -> None:
    turns = [
        ("Operator", "Our first question comes from Jane Analyst."),
        ("Jane Analyst", "What drove the margin expansion this quarter?"),
        ("CEO Name", "Great question — three factors drove it."),
    ]
    scoped, found = _slice_qa_turns(turns)
    assert found is True
    text = _serialize_turns(scoped)
    resplit = segment_by_speaker(text)
    assert [t.speaker for t in resplit] == ["Operator", "Jane Analyst", "CEO Name"]


def test_tickertrends_turns_resplit_correctly_by_existing_ingest_regex() -> None:
    html = (
        _tt_turn("Operator", "Our first question comes from Jane Analyst.")
        + _tt_turn("Jane Analyst", "What drove the margin expansion this quarter?")
        + _tt_turn("CEO Name", "Great question, three factors drove it.")
    )
    turns = _parse_tickertrends_messages(html)
    scoped, found = _slice_qa_turns(turns)
    assert found is True
    text = _serialize_turns(scoped)
    resplit = segment_by_speaker(text)
    assert [t.speaker for t in resplit] == ["Operator", "Jane Analyst", "CEO Name"]
