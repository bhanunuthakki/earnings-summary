"""Tests for src/compute/transcript_ingest.py — filename parsing, period mapping, speaker segmentation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from compute.transcript_ingest import (
    ParsedFilename,
    QASectionStatus,
    SpeakerTurn,
    _infer_fiscal_period_type,
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
    assert known_speakers_for("WIX") is not None
    assert "Avishai Abrahami" in known_speakers_for("WIX")


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
