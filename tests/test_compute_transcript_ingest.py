"""Tests for src/compute/transcript_ingest.py — filename parsing, period mapping, speaker segmentation."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from compute.transcript_ingest import (
    ParsedFilename,
    QASectionStatus,
    SpeakerTurn,
    _infer_fiscal_period_type,  # pyright: ignore[reportPrivateUsage]
    detect_qa_section,
    lookup_fiscal_year_end,
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


def test_parse_filename_accepts_content_addressed_sha8_suffix() -> None:
    """Optional 8-hex-char `__<sha8>` suffix parses to the same canonical period identity."""
    assert parse_transcript_filename(Path("transcripts/raw/BN_Q3_2026__402efbad.txt")) == (
        ParsedFilename(ticker="BN", quarter_idx=3, fiscal_year_label=2026)
    )
    assert parse_transcript_filename(Path("LSPD.TO_Q2_2026__0123abcd.pdf")) == (
        ParsedFilename(ticker="LSPD.TO", quarter_idx=2, fiscal_year_label=2026)
    )


@pytest.mark.parametrize(
    "name",
    [
        "BN_Q3_2026__402efba.txt",  # 7 hex chars
        "BN_Q3_2026__402efbad9.txt",  # 9 hex chars
        "BN_Q3_2026__402EFBAD.txt",  # uppercase hex
        "BN_Q3_2026__nothex12.txt",  # non-hex
        "BN_Q3_2026_402efbad.txt",  # single underscore
        "BN_Q3_2026__extra__402efbad.txt",  # extra segment
    ],
)
def test_parse_filename_rejects_invalid_suffixes(name: str) -> None:
    assert parse_transcript_filename(Path(name)) is None


def test_map_to_period_uses_tracked_fiscal_year_end() -> None:
    """Non-December FYE issuers map via `fiscal_year_end` when not in `FILENAME_CALENDARS`."""
    assert map_to_period(
        ParsedFilename(ticker="BIRK", quarter_idx=3, fiscal_year_label=2026),
        fiscal_year_end="09-30",
    ).period_end == datetime(2026, 6, 30)
    assert map_to_period(
        ParsedFilename(ticker="TJX", quarter_idx=3, fiscal_year_label=2027),
        fiscal_year_end="01-31",
    ).period_end == datetime(2026, 10, 31)
    assert map_to_period(
        ParsedFilename(ticker="TWST", quarter_idx=4, fiscal_year_label=2026),
        fiscal_year_end="09-30",
    ).period_end == datetime(2026, 9, 30)
    assert map_to_period(
        ParsedFilename(ticker="VFC", quarter_idx=2, fiscal_year_label=2027),
        fiscal_year_end="03-31",
    ).period_end == datetime(2026, 9, 30)
    assert map_to_period(
        ParsedFilename(ticker="QCOM", quarter_idx=4, fiscal_year_label=2026),
        fiscal_year_end="09-28",
    ).period_end == datetime(2026, 9, 30)
    # FILENAME_CALENDARS takes precedence even if a caller passes a different FYE.
    assert map_to_period(
        ParsedFilename(ticker="VEEV", quarter_idx=1, fiscal_year_label=2026),
        fiscal_year_end="12-31",
    ).period_end == datetime(2025, 4, 30)
    # Without fiscal_year_end, backward-compatible CALENDAR_YEAR fallback remains.
    assert map_to_period(
        ParsedFilename(ticker="BIRK", quarter_idx=3, fiscal_year_label=2026)
    ).period_end == datetime(2026, 9, 30)


def test_lookup_fiscal_year_end_handles_missing_table_and_nulls() -> None:
    import sqlite3

    empty_conn = sqlite3.connect(":memory:")
    assert lookup_fiscal_year_end(empty_conn, "BIRK") is None
    empty_conn.execute("CREATE TABLE tracked_companies (ticker TEXT, fiscal_year_end TEXT)")
    empty_conn.execute("INSERT INTO tracked_companies VALUES ('BIRK', '09-30')")
    empty_conn.execute("INSERT INTO tracked_companies VALUES ('NULLFYE', NULL)")
    assert lookup_fiscal_year_end(empty_conn, "birk") == "09-30"
    assert lookup_fiscal_year_end(empty_conn, "NULLFYE") is None
    assert lookup_fiscal_year_end(empty_conn, "UNKNOWN") is None


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


def test_detect_qa_section_roic_comma_operator_cues() -> None:
    """ROIC transcripts with 'Your first/next question, from the line of ...' classify PRESENT."""
    text = _padded(
        "Operator: Your first question, from the line of Saket Kalia with Barclays. "
        "Your line is open, please go ahead.\n"
        "Saket Kalia: Thanks for taking my question.\n\n"
        "Operator: Your next question, from the line of Fatima Boolani with Citi. "
        "Your line is open, please go ahead.\n"
        "Fatima Boolani: Thanks for taking my question.\n"
    )
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.PRESENT
    assert "operator_intros=2" in result.signals


def test_detect_qa_section_ir_open_the_line_cues() -> None:
    """ROIC transcripts moderated via 'open the line for <Analyst>' classify PRESENT when >=2."""
    text = _padded(
        "Guilherme Souto: Thank you, operator. Could you please open the line for "
        "Mr. Jorge Kuri from Morgan Stanley?\n"
        "Jorge Kuri: Thanks for the opportunity to ask questions.\n\n"
        "Guilherme Souto: Operator, could you please open the line for "
        "Mr. Yuri Fernandes from JPMorgan?\n"
        "Yuri Fernandes: Congrats on the results.\n"
    )
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.PRESENT
    assert "operator_intros=2" in result.signals


def test_detect_qa_section_single_open_the_line_below_threshold() -> None:
    """A single 'open the line for' phrase stays below the >=2 threshold."""
    text = _padded(
        "Guilherme Souto: Operator, could you please open the line for our first guest?\n"
    )
    result = detect_qa_section(text)
    assert result.status is QASectionStatus.ABSENT
    assert result.signals == ()


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


# ---------------------------------------------------------------------------
# Ingest-time Q&A-first winner selection
# ---------------------------------------------------------------------------


def _qa_first_ingest_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_start TIMESTAMP,
            period_end TIMESTAMP,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            http_code INTEGER,
            raw_bytes_size INTEGER NOT NULL,
            source_url TEXT,
            parent_document_id INTEGER
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL UNIQUE,
            ticker TEXT NOT NULL,
            call_date TIMESTAMP,
            fiscal_period_type TEXT,
            period_end TIMESTAMP,
            source_url TEXT,
            has_qa_section INTEGER,
            source TEXT,
            version_number INTEGER NOT NULL DEFAULT 1,
            is_current INTEGER NOT NULL DEFAULT 1,
            recorded_at TIMESTAMP,
            superseded_at TIMESTAMP,
            superseded_by_transcript_id INTEGER,
            is_active INTEGER NOT NULL DEFAULT 1,
            superseded_by_id INTEGER,
            selection_reason TEXT
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            speaker TEXT,
            speaker_role TEXT,
            time_code_start TEXT,
            time_code_end TEXT,
            text TEXT NOT NULL
        );
        CREATE UNIQUE INDEX uq_transcripts_current_ticker_period_type_end
            ON transcripts(ticker, fiscal_period_type, period_end)
            WHERE is_current = 1;
        """
    )
    return conn


def test_ingest_qa_google_finance_beta_supersedes_no_qa_ir_transcript(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from compute import transcript_ingest

    conn = _qa_first_ingest_conn()
    ir_file = tmp_path / "ir_documents" / "UBER" / "2026-09-30" / "ir_transcript.txt"
    ir_file.parent.mkdir(parents=True)
    ir_body = _padded(
        "Dara Khosrowshahi\nPrepared remarks for Q3 2026.\n"
        "Prashanth Mahendra-Rajah\nStrong free cash flow.\n"
    )
    ir_file.write_text(ir_body, encoding="utf-8")
    old_doc_id = transcript_ingest._insert_document(  # pyright: ignore[reportPrivateUsage]
        conn,
        ticker="UBER",
        file_path=ir_file,
        sha256="1" * 64,
        period_end=datetime(2026, 9, 30),
        project_root=tmp_path,
    )
    old_tid = transcript_ingest._insert_transcript(  # pyright: ignore[reportPrivateUsage]
        conn,
        document_id=old_doc_id,
        ticker="UBER",
        fiscal_period_type=FiscalPeriodType.Q3,
        period_end=datetime(2026, 9, 30),
        has_qa_section=False,
        source="ir_transcript_doc",
    )
    transcript_ingest._insert_segments(  # pyright: ignore[reportPrivateUsage]
        conn,
        transcript_id=old_tid,
        turns=[SpeakerTurn(speaker="Speaker", text="turn") for _ in range(30)],
    )
    conn.commit()

    gfb_body = _padded(
        "=== SYNTHESIZED QUARTERLY UPDATE — Q&A SEGMENT ONLY ===\n"
        "Source:    aggregator_google_finance_beta (https://www.google.com/finance)\n\n"
        "Dara Khosrowshahi, CEO, Uber\n1m 00s\nPrepared remarks.\n"
        "Brian Nowak, Analyst, Morgan Stanley\n20m 00s\nHow is autonomous supply scaling?\n"
    )
    gfb_path = tmp_path / "transcripts" / "raw" / "UBER_Q3_2026.txt"
    gfb_path.parent.mkdir(parents=True)
    gfb_path.write_text(gfb_body, encoding="utf-8")

    result = transcript_ingest._ingest_one(  # pyright: ignore[reportPrivateUsage]
        conn,
        file_path=gfb_path,
        project_root=tmp_path,
        tracked_tickers=frozenset({"UBER"}),
    )

    assert result is not None
    assert not result.skipped_existing
    assert result.source == "aggregator_google_finance_beta"
    assert result.qa_status is QASectionStatus.PRESENT
    assert result.transcript_id != old_tid

    old_row = conn.execute(
        "SELECT is_current, is_active, superseded_by_transcript_id FROM transcripts WHERE id = ?",
        (old_tid,),
    ).fetchone()
    assert tuple(old_row) == (0, 0, result.transcript_id)
    new_row = conn.execute(
        "SELECT is_current, is_active, has_qa_section, source FROM transcripts WHERE id = ?",
        (result.transcript_id,),
    ).fetchone()
    assert tuple(new_row) == (1, 1, 1, "aggregator_google_finance_beta")

    conflict_logs = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()
    ]
    assert len(conflict_logs) == 1
    assert conflict_logs[0]["event"] == "transcript_period_conflict_supersede"
    assert conflict_logs[0]["new_has_qa"] is True
    assert conflict_logs[0]["old_has_qa"] is False


def test_ingest_no_qa_ir_transcript_does_not_replace_qa_aggregator_roic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from compute import transcript_ingest

    conn = _qa_first_ingest_conn()
    roic_path = tmp_path / "transcripts" / "raw" / "META_Q4_2025.txt"
    roic_path.parent.mkdir(parents=True)
    roic_body = _padded(
        "=== SYNTHESIZED QUARTERLY UPDATE — Q&A SEGMENT ONLY ===\n"
        "Source:    aggregator_roic (https://roic.ai)\n\n"
        "QUESTION AND ANSWER SECTION\n"
        "Operator: First question is from Brian Nowak.\n"
        "Mark Zuckerberg\nThanks Brian.\n"
    )
    roic_path.write_text(roic_body, encoding="utf-8")
    first = transcript_ingest._ingest_one(  # pyright: ignore[reportPrivateUsage]
        conn,
        file_path=roic_path,
        project_root=tmp_path,
        tracked_tickers=frozenset({"META"}),
    )
    assert first is not None and not first.skipped_existing
    assert first.transcript_id is not None

    ir_file = tmp_path / "ir_documents" / "META" / "2025-12-31" / "ir_transcript.txt"
    ir_file.parent.mkdir(parents=True)
    ir_body = _padded(
        "Mark Zuckerberg\nPrepared remarks only.\n"
        "Susan Li\nCapex outlook for 2026.\n"
        "Operator – we are now ready for questions.\n"  # noqa: RUF001
    )
    ir_bytes = ir_body.encode("utf-8")
    ir_file.write_bytes(ir_bytes)
    ir_doc_id = transcript_ingest._insert_document(  # pyright: ignore[reportPrivateUsage]
        conn,
        ticker="META",
        file_path=ir_file,
        sha256="2" * 64,
        period_end=datetime(2025, 12, 31),
        project_root=tmp_path,
    )
    conn.commit()

    second = transcript_ingest._ingest_existing_ir_transcript(  # pyright: ignore[reportPrivateUsage]
        conn,
        document_id=ir_doc_id,
        file_path=ir_file,
        snapshot_bytes=ir_bytes,
    )

    assert second.skipped_existing is True
    assert second.transcript_id == first.transcript_id
    winner_row = conn.execute(
        "SELECT id, source, has_qa_section FROM transcripts WHERE is_current = 1"
    ).fetchone()
    assert tuple(winner_row) == (first.transcript_id, "aggregator_roic", 1)
    loser_row = conn.execute(
        "SELECT source, has_qa_section, is_current, superseded_by_transcript_id "
        "FROM transcripts WHERE document_id = ?",
        (ir_doc_id,),
    ).fetchone()
    assert tuple(loser_row) == ("ir_transcript_doc", 0, 0, first.transcript_id)

    conflict_logs = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()
    ]
    assert len(conflict_logs) == 1
    assert conflict_logs[0]["event"] == "transcript_period_conflict_skip"
    assert conflict_logs[0]["new_has_qa"] is False
    assert conflict_logs[0]["old_has_qa"] is True
