"""Tests for src/compute/transcript_ingest.py — filename parsing, period mapping, speaker segmentation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from compute.transcript_ingest import (
    ParsedFilename,
    QASectionStatus,
    SpeakerTurn,
    _infer_fiscal_period_type,  # pyright: ignore[reportPrivateUsage]
    detect_qa_section,
    map_to_period,
    parse_transcript_filename,
    qa_status_to_db_value,
    segment_by_speaker,
)
from models.facts import FiscalPeriodType


def test_parse_calendar_filename() -> None:
    """Standard <TICKER>_Q<N>_<YYYY>.{pdf,txt} parses cleanly."""
    parsed = parse_transcript_filename(Path("transcripts/processed/GOOG_Q3_2025.pdf"))
    assert parsed == ParsedFilename(ticker="GOOG", quarter_idx=3, fiscal_year_label=2025)


def test_parse_filename_rejects_master_pdf() -> None:
    """Master compendium PDFs (no quarter pattern) return None."""
    assert parse_transcript_filename(Path("transcripts/master/GOOG_Master_Transcripts.pdf")) is None


def test_parse_filename_rejects_unrelated() -> None:
    """Random filenames return None instead of guessing."""
    assert parse_transcript_filename(Path("readme.txt")) is None
    assert parse_transcript_filename(Path("Q1_2025.pdf")) is None


def test_parse_filename_handles_dotted_ticker() -> None:
    """Dotted tickers like LSPD.TO map cleanly."""
    parsed = parse_transcript_filename(Path("LSPD.TO_Q2_2026.pdf"))
    assert parsed is not None
    assert parsed.ticker == "LSPD.TO"


def test_calendar_period_mapping() -> None:
    """Calendar-FYE filer (e.g. GOOG): Q1 2025 -> 2025-03-31."""
    parsed = ParsedFilename(ticker="GOOG", quarter_idx=1, fiscal_year_label=2025)
    pm = map_to_period(parsed)
    assert pm.period_end == datetime(2025, 3, 31)
    assert pm.fiscal_period_type == FiscalPeriodType.Q1


def test_jan_fye_period_mapping() -> None:
    """VEEV (Jan FYE): FY26 Q1 = quarter ending Apr 30 2025."""
    parsed = ParsedFilename(ticker="VEEV", quarter_idx=1, fiscal_year_label=2026)
    pm = map_to_period(parsed)
    assert pm.period_end == datetime(2025, 4, 30)
    assert pm.fiscal_period_type == FiscalPeriodType.Q1


def test_jan_fye_q4_maps_to_jan() -> None:
    """RBRK (Jan FYE): FY26 Q4 = quarter ending Jan 31 2026."""
    parsed = ParsedFilename(ticker="RBRK", quarter_idx=4, fiscal_year_label=2026)
    pm = map_to_period(parsed)
    assert pm.period_end == datetime(2026, 1, 31)
    assert pm.fiscal_period_type == FiscalPeriodType.Q4


def test_oct_fye_period_mapping() -> None:
    """TOL (Oct FYE): FY26 Q1 = quarter ending Jan 31 2026."""
    parsed = ParsedFilename(ticker="TOL", quarter_idx=1, fiscal_year_label=2026)
    pm = map_to_period(parsed)
    assert pm.period_end == datetime(2026, 1, 31)


def test_segment_by_speaker_colon_inline_format() -> None:
    """NU-style: 'Name : body content' on the same line, body inline."""
    text = (
        "Operator\nGood afternoon, ladies and gentlemen.\n"
        "David Velez : Thank you, J. Good evening, everyone.\n"
        "Guilherme Lago : Thanks David. Good evening.\n"
    )
    turns = segment_by_speaker(text)
    speakers = [t.speaker for t in turns]
    assert "Operator" in speakers
    assert "David Velez" in speakers
    assert "Guilherme Lago" in speakers
    velez = next(t for t in turns if t.speaker == "David Velez")
    assert "Thank you" in velez.text


def test_segment_by_speaker_paragraph_break_format() -> None:
    """PDF-style: 'Name\\n\\nbody' with paragraph break between speakers."""
    text = (
        "Sundar Pichai\n\nThanks, Jim. Hi, everyone.\n\n"
        "Anant Ashkenazi\n\nThanks, Sundar. Q1 revenue grew."
    )
    turns = segment_by_speaker(text)
    speakers = [t.speaker for t in turns]
    assert "Sundar Pichai" in speakers
    assert "Anant Ashkenazi" in speakers


def test_segment_by_speaker_excludes_single_word_capitalized() -> None:
    """'Studio:', 'Total:', 'This:' should NOT be treated as speakers."""
    text = (
        "Some content.\n"
        "Studio: Wix Studio is for agencies.\n"
        "Total: Revenue grew 14%.\n"
        "This: is the bottom line.\n"
    )
    turns = segment_by_speaker(text)
    detected = [t.speaker for t in turns if t.speaker is not None]
    assert "Studio" not in detected
    assert "Total" not in detected
    assert "This" not in detected


def test_segment_by_speaker_no_speakers_falls_back() -> None:
    """Plain text with no speaker markers returns one anonymous turn."""
    text = "This is a transcript with no speaker tags. Just continuous prose."
    turns = segment_by_speaker(text)
    assert len(turns) == 1
    assert turns[0].speaker is None
    assert turns[0].text == text


def test_segment_by_speaker_normalizes_whitespace() -> None:
    """PDF extraction artifact: 'Guilherme  Lago' (double space) collapses to single."""
    text = "Some content.\n Guilherme  Lago : This is the content.\n"
    turns = segment_by_speaker(text)
    detected = [t.speaker for t in turns if t.speaker is not None]
    assert "Guilherme Lago" in detected
    assert "Guilherme  Lago" not in detected


def test_infer_fiscal_period_type_calendar() -> None:
    """Calendar-FYE month mappings."""
    assert _infer_fiscal_period_type(datetime(2025, 3, 31)) == FiscalPeriodType.Q1
    assert _infer_fiscal_period_type(datetime(2025, 6, 30)) == FiscalPeriodType.Q2
    assert _infer_fiscal_period_type(datetime(2025, 9, 30)) == FiscalPeriodType.Q3
    assert _infer_fiscal_period_type(datetime(2025, 12, 31)) == FiscalPeriodType.Q4


def test_infer_fiscal_period_type_jan_fye() -> None:
    """Jan-FYE month mappings (VEEV, RBRK)."""
    assert _infer_fiscal_period_type(datetime(2025, 4, 30)) == FiscalPeriodType.Q1
    assert _infer_fiscal_period_type(datetime(2025, 7, 31)) == FiscalPeriodType.Q2
    assert _infer_fiscal_period_type(datetime(2025, 10, 31)) == FiscalPeriodType.Q3
    assert _infer_fiscal_period_type(datetime(2026, 1, 31)) == FiscalPeriodType.Q4


def test_infer_fiscal_period_type_rejects_non_quarter_end() -> None:
    """Non-quarter-end months raise rather than silently defaulting."""
    with pytest.raises(ValueError, match="non-standard quarter-end"):
        _infer_fiscal_period_type(datetime(2025, 5, 15))


def test_speaker_turn_dataclass_immutable() -> None:
    """SpeakerTurn is frozen — guards against accidental mutation."""
    turn = SpeakerTurn(speaker="X", text="y")
    with pytest.raises((AttributeError, TypeError)):
        turn.speaker = "Z"  # type: ignore[misc]


def test_segment_by_speaker_whitelist_filters_out_product_names() -> None:
    """known_speakers whitelist drops false-positive matches like 'Wix Payments'."""
    text = (
        "Avishai Abrahami\n\nThanks, Operator. Q3 was strong.\n\n"
        "Wix Payments\n\nThis is a product line, not a speaker.\n\n"
        "Lior Shemesh\n\nThanks Avishai. CC bookings grew 14%."
    )
    whitelist = frozenset({"Avishai Abrahami", "Lior Shemesh"})
    turns = segment_by_speaker(text, known_speakers=whitelist)
    detected = {t.speaker for t in turns if t.speaker is not None}
    assert detected == {"Avishai Abrahami", "Lior Shemesh"}
    # The "Wix Payments" content should merge into Avishai's turn.
    avishai = next(t for t in turns if t.speaker == "Avishai Abrahami")
    assert "product line" in avishai.text


def test_segment_by_speaker_whitelist_preserves_operator() -> None:
    """Operator is detected by a separate regex; whitelist doesn't filter it."""
    text = (
        "Operator\n\nGood afternoon. Welcome.\n\n"
        "Random Speaker\n\nshould be filtered\n\n"
        "Avishai Abrahami\n\nThanks operator."
    )
    whitelist = frozenset({"Avishai Abrahami"})
    turns = segment_by_speaker(text, known_speakers=whitelist)
    detected = {t.speaker for t in turns if t.speaker is not None}
    assert "Operator" in detected
    assert "Random Speaker" not in detected
    assert "Avishai Abrahami" in detected


def test_known_speakers_for_returns_none_for_unknown_ticker() -> None:
    """Tickers without a registered whitelist return None (no filtering applied)."""
    from compute.transcript_ingest import known_speakers_for

    assert known_speakers_for("MELI") is None
    wix_speakers = known_speakers_for("WIX")
    assert wix_speakers is not None
    assert "Avishai Abrahami" in wix_speakers


# ---------------------------------------------------------------------------
# Q&A section detection
# ---------------------------------------------------------------------------


def _padded(body: str) -> str:
    """Pad text past the min-length-for-detection threshold so the detector
    actually runs (UNKNOWN is reserved for genuinely too-short stubs)."""
    return body + ("\nfiller line " * 400)


def test_detect_qa_section_callstreet_header() -> None:
    """CallStreet `QUESTION AND ANSWER SECTION` header is sufficient by itself."""
    text = _padded(
        "Bom Kim, CEO\nThanks operator, Q1 was strong.\n"
        "Operator, we are now ready to begin the Q&A.\n\n"
        "QUESTION AND ANSWER SECTION\n"
        "Operator: First question is from Eric Cha with Goldman Sachs.\n"
    )
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.PRESENT
    assert "qa_header" in result.signals


def test_detect_qa_section_analyst_tag() -> None:
    """CallStreet `<Q - Name - Firm>` analyst speaker tag is sufficient.

    The fixture uses real CallStreet en-dashes (U+2013) deliberately —
    that is the format the detector must handle in production PDFs.
    """
    text = _padded(
        "Bom Kim, CEO\nPrepared remarks here.\n"
        "<Q – Eric Cha – Goldman Sachs (Asia) LLC>: Thanks for taking my question.\n"  # noqa: RUF001
    )
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.PRESENT
    assert "analyst_tag" in result.signals


def test_detect_qa_section_operator_introductions() -> None:
    """≥2 operator analyst-introductions trigger PRESENT (no header/tag needed)."""
    text = _padded(
        "Sundar Pichai\nThanks Jim, hi everyone.\n\n"
        "Operator: Our first question is from Eric Sheridan from Goldman Sachs.\n"
        "Eric Sheridan (Goldman Sachs): Thanks for taking my question.\n\n"
        "Operator: Our next question comes from Doug Anmuth from JPMorgan.\n"
        "Doug Anmuth (JPMorgan): Thank you for taking my questions.\n"
    )
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.PRESENT
    assert any(s.startswith("operator_intros=") for s in result.signals)


def test_detect_qa_section_single_operator_intro_below_threshold() -> None:
    """A single operator hand-off line is the boilerplate preamble, not Q&A
    content. Must NOT trigger PRESENT on its own."""
    text = _padded(
        "Welcome to the call. After the speaker presentations, there will be a "
        "question-and-answer session.\n"
        "Avishai Abrahami\nQ1 results were strong.\n"
        "Operator – we are now ready for questions.\n"  # noqa: RUF001
    )
    result = detect_qa_section(text)
    # No operator intro pattern, no tag, no header => should be ABSENT.
    assert result.status is QASectionStatus.ABSENT
    assert result.signals == ()


def test_detect_qa_section_prepared_remarks_only_wix_style() -> None:
    """The WIX-style IR PDF that ends at the hand-off must classify as ABSENT."""
    text = _padded(
        "Avishai Abrahami\nThanks Operator. Q1 bookings grew 14%.\n"
        "Lior Shemesh\nThanks Avishai. Margin expanded 200bps.\n"
        "Nir Zohar\nWe remain committed to increasing shareholder value.\n"
        "Operator – we are now ready for questions.\n"  # noqa: RUF001
    )
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.ABSENT
    assert result.signals == ()


def test_detect_qa_section_short_text_is_unknown() -> None:
    """Below the min-length threshold we cannot tell — return UNKNOWN, not ABSENT."""
    text = "Welcome to the call. Q1 was strong. Thanks."
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.UNKNOWN
    assert result.signals == ()


def test_qa_status_to_db_value_round_trip() -> None:
    """Tri-state -> bool|None mapping for the `transcripts.has_qa_section` column."""
    assert qa_status_to_db_value(QASectionStatus.PRESENT) is True
    assert qa_status_to_db_value(QASectionStatus.ABSENT) is False
    assert qa_status_to_db_value(QASectionStatus.UNKNOWN) is None


# ---------------------------------------------------------------------------
# Google Finance Beta / Quartr verbatim speaker & Q&A parsing
# ---------------------------------------------------------------------------


def test_segment_by_speaker_parses_google_finance_beta_comma_and_unicode_headers() -> None:
    """Multi-comma titles, analyst firm headers, accented Latin names, and [Operator Instructions]."""
    text = (
        "[Operator Instructions]\n"
        "Good day, and welcome to the conference call.\n"
        "Jane Smith, Chief Financial Officer\n"
        "1m 10s\n"
        "Thank you, operator. Revenue grew 18%.\n"
        "Alex Karp, Co-Founder, Chief Executive Officer\n"
        "3m 05s\n"
        "Demand continues to accelerate.\n"
        "David Vélez, Founder, CEO, and Chairman, Nu\n"
        "5m 12s\n"
        "Customer deposits expanded across every market.\n"
        "Hassane El-Khoury, President and CEO, onsemi\n"
        "7m 00s\n"
        "Automotive silicon carbide remains on plan.\n"
        "Luis von Ahn, Co-Founder and CEO, Duolingo\n"
        "9m 20s\n"
        "Daily active users grew 50%.\n"
        "C.C. Wei, Chairman and CEO, Taiwan Semiconductor Manufacturing Company\n"
        "11m 00s\n"
        "AI demand remains strong.\n"
        "[Operator Instructions]\n"
        "14m 10s\n"
        "Our first question comes from Brian Nowak with Morgan Stanley.\n"
        "Brian Nowak, Analyst, Morgan Stanley\n"
        "14m 25s\n"
        "Thanks for taking my question.\n"
        "Mark Mahaney, Analyst, Evercore ISI\n"
        "16m 00s\n"
        "Could you comment on margins?\n"
        "Étienne Ricard, Analyst, BMO Capital Markets\n"
        "18m 05s\n"
        "Where do you see the best capital allocation returns?\n"
        "Søren Hansen, Analyst, Nordic Partners Co.\n"
        "20m 10s\n"
        "Any update on European demand?\n"
        "François Moreau, Analyst, Société Générale\n"
        "22m 00s\n"
        "Thank you for taking my follow-up.\n"
    )
    turns = segment_by_speaker(text)
    speakers = [t.speaker for t in turns]
    assert speakers == [
        "Operator",
        "Jane Smith",
        "Alex Karp",
        "David Vélez",
        "Hassane El-Khoury",
        "Luis von Ahn",
        "C.C. Wei",
        "Operator",
        "Brian Nowak",
        "Mark Mahaney",
        "Étienne Ricard",
        "Søren Hansen",
        "François Moreau",
    ]
    ricard_turn = next(t for t in turns if t.speaker == "Étienne Ricard")
    assert "capital allocation returns" in ricard_turn.text
    assert "BMO Capital Markets" not in ricard_turn.text


def test_segment_by_speaker_does_not_split_ordinary_prose_with_commas() -> None:
    """Prose lines with commas, greetings, or sentence-ending periods must stay inside the turn."""
    text = (
        "Nick Goodman, President, Brookfield\n"
        "12m 5s\n"
        "Thank you, Bruce, and good morning, everyone.\n"
        "John Doe, thanks for the follow-up.\n"
        "Paul Brink, President and CEO of Franco-Nevada.\n"
        "Brian, do you want me to start here, and then you can add on?\n"
        "I will turn it over after [Operator Instructions] finishes.\n"
        "John Doe, Analyst, Morgan Stanley\n"
        "28m 41s\n"
        "Thanks for taking my question.\n"
    )
    turns = segment_by_speaker(text)
    speakers = [t.speaker for t in turns]
    assert speakers == ["Nick Goodman", "John Doe"]
    assert "Paul Brink, President and CEO of Franco-Nevada." in turns[0].text
    assert "Brian, do you want me to start here" in turns[0].text
    assert "[Operator Instructions]" in turns[0].text


def test_detect_qa_section_google_finance_beta_analyst_headers() -> None:
    """Google Finance Beta `<Name>, Analyst, <Bank>` headers trigger PRESENT without operator intros."""
    text = _padded(
        "[Operator Instructions]\n"
        "Good morning, and welcome to the conference call.\n"
        "Bruce Flatt, CEO, Brookfield\n"
        "2m 16s\n"
        "We had a strong third quarter.\n"
        "Étienne Ricard, Analyst, BMO Capital Markets\n"
        "48m 5s\n"
        "In terms of capital allocation, where do you see the best return opportunities?\n"
        "Nick Goodman, President, Brookfield\n"
        "48m 33s\n"
        "Hi, Étienne. We see strong opportunities across both franchises.\n"
    )
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.PRESENT
    assert "analyst_speaker_header" in result.signals


def test_detect_qa_section_senior_analyst_title_variant() -> None:
    """Extended analyst titles like `Senior Analyst of ...` also trigger PRESENT."""
    text = _padded(
        "Dave Ricks, Chair and CEO, Eli Lilly and Company\n"
        "2m 00s\n"
        "Prepared remarks.\n"
        "Chris Schott, Senior Analyst of US Diversified Biopharma, JPMorgan\n"
        "26m 10s\n"
        "Thanks for taking my question.\n"
    )
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.PRESENT
    assert "analyst_speaker_header" in result.signals


def test_detect_qa_section_google_finance_beta_prepared_remarks_only_is_absent() -> None:
    """Prepared-remarks-only transcript with Quartr executive headers must remain ABSENT."""
    text = _padded(
        "[Operator Instructions]\n"
        "After the speaker presentation, there will be a question-and-answer session.\n"
        "Katie Battaglia, VP of Investor Relations, Brookfield\n"
        "36s\n"
        "After our formal comments, we will turn the call over to the operator and take analyst questions.\n"
        "Bruce Flatt, CEO, Brookfield\n"
        "2m 16s\n"
        "We had a strong quarter with 16% distributable earnings growth.\n"
        "Jon Bayer, Managing Partner and COO, Brookfield Wealth Solutions\n"
        "21m 53s\n"
        "Thank you. With that, I will hand the call over to the operator for questions.\n"
    )
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.ABSENT
    assert result.signals == ()
