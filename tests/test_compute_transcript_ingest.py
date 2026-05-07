"""Tests for src/compute/transcript_ingest.py — filename parsing, period mapping, speaker segmentation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from compute.transcript_ingest import (
    ParsedFilename,
    SpeakerTurn,
    _infer_fiscal_period_type,
    map_to_period,
    parse_transcript_filename,
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
