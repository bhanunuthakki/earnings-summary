"""Tests for D2.1 (docs/design/disclosure_intelligence_v1_prd.md) — the
guidance-withdrawal detector that generalizes P1's ``metric_lifecycle``
own-cadence engine to a second subject family.

Weighted toward the failure modes the scoping doc calls out explicitly
(docs/design/disclosure_gap_scoping.md Gap 1):

  * a period with NEITHER a commitment NOR a scan-log row is coverage-unknown
    and must never be counted as either presence or a gap ("the single
    biggest correctness risk" per the scoping doc);
  * a ticker within its own established cadence must not flag, but MUST flag
    once it exceeds its own historical tolerance;
  * a resumption after an abnormal gap must be detected symmetrically;
  * the cross-sectional wave gate must reclassify a synchronized multi-ticker
    stop (the COVID confound) as mechanical, while leaving a genuinely
    isolated stop alone;
  * an MD&A heading with a RECURRING cadence gap (e.g. always absent in Q3)
    must never flag — only a gap beyond ITS OWN precedent should;
  * the accounting-standards-guidance false positive ("Recently Adopted
    Accounting Guidance") must never pass the heading filter;
  * writes are idempotent.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings import guidance_lifecycle as gl  # noqa: E402
from filings.guidance_lifecycle import (  # noqa: E402
    GUIDANCE_WAVE_KEY,
    MDNA_WAVE_KEY,
    apply_wave_suppression,
    candidate_to_event,
    detect_commitment_lifecycle,
    detect_mdna_lifecycle,
    looks_like_guidance_heading,
    write_guidance_events,
)
from filings.metric_lifecycle import StandardTransitionCorpus  # noqa: E402
from filings.models import HardStopError  # noqa: E402

_SCHEMA = """
CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    period_end DATETIME
);
CREATE TABLE transcript_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_id INTEGER NOT NULL
);
CREATE TABLE management_commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    transcript_segment_id INTEGER NOT NULL
);
CREATE TABLE commitment_scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_id INTEGER NOT NULL UNIQUE,
    n_extracted INTEGER NOT NULL
);
CREATE TABLE filing_section_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    section_id INTEGER NOT NULL,
    source_ref VARCHAR(255) NOT NULL,
    form VARCHAR(8) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4) NOT NULL,
    canonical_id VARCHAR(64),
    item_ordinal INTEGER NOT NULL,
    heading TEXT,
    match_key VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    body_sha256 VARCHAR(64) NOT NULL,
    char_len INTEGER NOT NULL,
    extractor_version VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_filing_section_items UNIQUE (section_id, item_ordinal)
);
CREATE TABLE disclosure_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    form VARCHAR(8),
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4),
    prior_fiscal_year INTEGER,
    prior_fiscal_period VARCHAR(4),
    source_ref VARCHAR(255),
    source_doc_id INTEGER,
    canonical_id VARCHAR(64) NOT NULL DEFAULT '',
    subject VARCHAR(255) NOT NULL,
    subject_label TEXT,
    prior_excerpt TEXT,
    current_excerpt TEXT,
    evidence_quote TEXT,
    materiality FLOAT,
    verdict VARCHAR(24) NOT NULL DEFAULT 'unclassified',
    interpretation_md TEXT,
    confidence FLOAT,
    detector_version VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'new',
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_disclosure_events UNIQUE
        (ticker, event_type, fiscal_year, fiscal_period, canonical_id, subject, detector_version)
);
"""


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA)
    connection.commit()
    connection.row_factory = sqlite3.Row
    return connection


def _add_transcript(conn: sqlite3.Connection, ticker: str, period_end: str) -> int:
    cur = conn.execute(
        "INSERT INTO transcripts (ticker, period_end) VALUES (?, ?)", (ticker, period_end)
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def _add_commitment(conn: sqlite3.Connection, ticker: str, transcript_id: int) -> None:
    seg = conn.execute(
        "INSERT INTO transcript_segments (transcript_id) VALUES (?)", (transcript_id,)
    )
    assert seg.lastrowid is not None
    conn.execute(
        "INSERT INTO management_commitments (ticker, transcript_segment_id) VALUES (?, ?)",
        (ticker, int(seg.lastrowid)),
    )


def _mark_scanned(conn: sqlite3.Connection, transcript_id: int, n_extracted: int = 0) -> None:
    conn.execute(
        "INSERT INTO commitment_scan_log (transcript_id, n_extracted) VALUES (?, ?)",
        (transcript_id, n_extracted),
    )


# ---------------------------------------------------------------------------
# Lane A — management_commitments own-cadence
# ---------------------------------------------------------------------------


def test_healthy_quarterly_cadence_never_flags(conn: sqlite3.Connection) -> None:
    """Six straight quarters of commitments, all coverage-known: no gap ever
    exceeds precedent (there is none), so nothing flags."""
    for pe in ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31"]:
        tid = _add_transcript(conn, "FCX", pe)
        _add_commitment(conn, "FCX", tid)

    periods = gl.load_commitment_periods(conn, "FCX")
    result = detect_commitment_lifecycle("FCX", periods)
    assert result.insufficient_history is False
    assert result.withdrawn is None
    assert result.resumed is None
    assert result.n_present_periods == 6


def test_silence_beyond_own_precedent_flags_withdrawn(conn: sqlite3.Connection) -> None:
    """Four present quarters (no gap ever seen), then two coverage-KNOWN
    silent quarters (scanned, zero found) — current_silence=2 > historical
    tolerance of 1 -> must flag guidance_withdrawn."""
    present = ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30"]
    for pe in present:
        tid = _add_transcript(conn, "NU", pe)
        _add_commitment(conn, "NU", tid)
    for pe in ["2025-12-31", "2026-03-31"]:
        tid = _add_transcript(conn, "NU", pe)
        _mark_scanned(conn, tid, n_extracted=0)

    periods = gl.load_commitment_periods(conn, "NU")
    result = detect_commitment_lifecycle("NU", periods)
    assert result.withdrawn is not None
    assert result.withdrawn.kind == "guidance_withdrawn"
    assert result.withdrawn.current_silence == 2
    assert result.withdrawn.historical_max_gap == 1


def test_coverage_unknown_period_never_counted_as_gap_or_presence(
    conn: sqlite3.Connection,
) -> None:
    """A transcript with NEITHER a commitment NOR a scan-log row is
    coverage-unknown. Dropping it must not silently manufacture a gap: four
    known-present quarters, then ONE genuinely-unknown quarter (no scan log,
    no commitment) as the most recent transcript, must NOT flag anything —
    because the as-of reference is the last COVERAGE-KNOWN period, which is
    still the fourth present quarter itself (current_silence == 0)."""
    for pe in ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30"]:
        tid = _add_transcript(conn, "TSM", pe)
        _add_commitment(conn, "TSM", tid)
    # Never scanned, never has a commitment -- coverage-unknown.
    _add_transcript(conn, "TSM", "2025-12-31")

    periods = gl.load_commitment_periods(conn, "TSM")
    known = [p for p in periods if p.coverage_known]
    assert len(known) == 4  # the unknown period is excluded from "known"
    result = detect_commitment_lifecycle("TSM", periods)
    assert result.withdrawn is None


def test_insufficient_history_below_min_observations(conn: sqlite3.Connection) -> None:
    """Only two present quarters is too little history to judge a cadence at
    all — must not flag, must report insufficient_history."""
    for pe in ["2025-09-30", "2025-12-31"]:
        tid = _add_transcript(conn, "VEEV", pe)
        _add_commitment(conn, "VEEV", tid)
    result = detect_commitment_lifecycle("VEEV", gl.load_commitment_periods(conn, "VEEV"))
    assert result.insufficient_history is True
    assert result.withdrawn is None


def test_resumption_detected_symmetrically(conn: sqlite3.Connection) -> None:
    """Four present quarters (max gap 1), a two-quarter silence, then
    present again -- the trailing period IS present, and the gap just
    before it exceeds the cadence established by the earlier periods ->
    guidance_resumed."""
    for pe in ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30"]:
        tid = _add_transcript(conn, "WIX", pe)
        _add_commitment(conn, "WIX", tid)
    for pe in ["2025-12-31", "2026-03-31"]:
        tid = _add_transcript(conn, "WIX", pe)
        _mark_scanned(conn, tid, n_extracted=0)
    tid = _add_transcript(conn, "WIX", "2026-06-30")
    _add_commitment(conn, "WIX", tid)

    result = detect_commitment_lifecycle("WIX", gl.load_commitment_periods(conn, "WIX"))
    assert result.resumed is not None
    assert result.resumed.kind == "guidance_resumed"
    # The trailing silence resolved, so this run's withdrawn candidate is None.
    assert result.withdrawn is None


def test_multi_transcript_same_period_merges_not_gaps(conn: sqlite3.Connection) -> None:
    """Two transcript rows (e.g. aggregator + FactSet) for the SAME
    period_end must be treated as ONE period, not two — and a commitment on
    EITHER counts as presence for that period."""
    _add_transcript(conn, "AVGO", "2025-06-30")
    t2 = _add_transcript(conn, "AVGO", "2025-06-30")
    _add_commitment(conn, "AVGO", t2)  # only the second source has the commitment
    for pe in ["2025-09-30", "2025-12-31", "2026-03-31"]:
        tid = _add_transcript(conn, "AVGO", pe)
        _add_commitment(conn, "AVGO", tid)

    periods = gl.load_commitment_periods(conn, "AVGO")
    assert len(periods) == 4  # not 5 -- the duplicate period_end merged
    result = detect_commitment_lifecycle("AVGO", periods)
    assert result.n_present_periods == 4


# ---------------------------------------------------------------------------
# Lane B — MD&A guidance/outlook heading own-cadence
# ---------------------------------------------------------------------------


def test_accounting_guidance_heading_is_never_forward_guidance() -> None:
    """The confirmed real false positive (AVGO/QCOM): accounting-standard
    footnote headings must never pass the filter."""
    assert not looks_like_guidance_heading("Recently Adopted Accounting Guidance")
    assert not looks_like_guidance_heading("Recent Accounting Guidance")
    assert looks_like_guidance_heading("Business Performance and Outlook")
    assert looks_like_guidance_heading("Fiscal 2026 Guidance")
    assert not looks_like_guidance_heading(None)
    assert not looks_like_guidance_heading("Results of Operations")


def _add_section_item(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    section_id: int,
    fiscal_year: int,
    fiscal_period: str,
    heading: str,
    match_key: str,
) -> None:
    conn.execute(
        """
        INSERT INTO filing_section_items
            (ticker, section_id, source_ref, form, fiscal_year, fiscal_period,
             canonical_id, item_ordinal, heading, match_key, body, body_sha256,
             char_len, extractor_version, created_at)
        VALUES (?, ?, 'ref', '10-Q', ?, ?, 'mdna', 0, ?, ?, 'body text here', 'x', 15, 'v1', '2026-01-01')
        """,
        (ticker, section_id, fiscal_year, fiscal_period, heading, match_key),
    )


def test_recurring_seasonal_gap_never_flags(conn: sqlite3.Connection) -> None:
    """DHR's real pattern: 'Business Performance and Outlook' present every
    quarter EXCEPT Q3, four years running. Its own historical_max_gap already
    reflects that recurring one-quarter skip, so it must NOT flag."""
    sid = 1
    periods = [
        (2022, "Q1"),
        (2022, "Q2"),
        (2022, "Q4"),
        (2023, "Q1"),
        (2023, "Q2"),
        (2023, "Q4"),
        (2024, "Q1"),
        (2024, "Q2"),
        (2024, "Q4"),
        (2025, "Q1"),
        (2025, "Q2"),
    ]
    for fy, fp in periods:
        _add_section_item(
            conn,
            ticker="DHR",
            section_id=sid,
            fiscal_year=fy,
            fiscal_period=fp,
            heading="Business Performance and Outlook",
            match_key="business_performance_and_outlook",
        )
        sid += 1
    candidates = detect_mdna_lifecycle(conn, "DHR")
    assert candidates == []


def test_genuine_new_silence_beyond_own_precedent_flags(conn: sqlite3.Connection) -> None:
    """Same recurring-Q3-skip pattern as above, but this time the heading is
    ALSO missing from Q4 2025 and Q1 2026 -- a genuinely NEW, longer silence
    than its own established one-quarter tolerance -> must flag."""
    sid = 1
    present_periods = [
        (2022, "Q1"),
        (2022, "Q2"),
        (2022, "Q4"),
        (2023, "Q1"),
        (2023, "Q2"),
        (2023, "Q4"),
        (2024, "Q1"),
        (2024, "Q2"),
        (2024, "Q4"),
    ]
    for fy, fp in present_periods:
        _add_section_item(
            conn,
            ticker="DHR",
            section_id=sid,
            fiscal_year=fy,
            fiscal_period=fp,
            heading="Business Performance and Outlook",
            match_key="business_performance_and_outlook",
        )
        sid += 1
    # The ticker's mdna section keeps being filed (as_of advances) but this
    # heading itself is now absent for two straight quarters -- a DIFFERENT,
    # unrelated heading anchors the "last known mdna period" reference.
    for fy, fp in [(2025, "Q1"), (2025, "Q2")]:
        _add_section_item(
            conn,
            ticker="DHR",
            section_id=sid,
            fiscal_year=fy,
            fiscal_period=fp,
            heading="Results of Operations",
            match_key="results_of_operations",
        )
        sid += 1
    candidates = detect_mdna_lifecycle(conn, "DHR")
    assert len(candidates) == 1
    assert candidates[0].subject_key == "business_performance_and_outlook"
    assert candidates[0].current_silence > candidates[0].historical_max_gap


# ---------------------------------------------------------------------------
# Stage "1.5" — cross-sectional wave suppression
# ---------------------------------------------------------------------------


def test_wave_gate_reclassifies_synchronized_stop_as_mechanical() -> None:
    corpus = StandardTransitionCorpus(
        stop_events={
            GUIDANCE_WAVE_KEY: [
                ("PEER1", 8102),
                ("PEER2", 8102),
                ("PEER3", 8101),
            ]
        },
        tickers_covered=["SUBJECT", "PEER1", "PEER2", "PEER3"],
    )
    candidate = gl.GuidanceCandidate(
        ticker="SUBJECT",
        kind="guidance_withdrawn",
        lane="commitments",
        subject_key="SUBJECT",
        subject_label="SUBJECT management guidance practice",
        last_present_period="2025-09-30",
        last_present_rank=8102,
        as_of_period="2026-03-31",
        as_of_rank=8104,
        current_silence=2,
        historical_max_gap=1,
        n_known_periods=6,
    )
    _, is_wave = apply_wave_suppression(candidate, corpus, wave_key=GUIDANCE_WAVE_KEY)
    assert is_wave is True
    assert candidate.standard_transition_other_tickers == 3


def test_isolated_stop_not_reclassified() -> None:
    corpus = StandardTransitionCorpus(
        stop_events={GUIDANCE_WAVE_KEY: [("PEER1", 8102)]},
        tickers_covered=["SUBJECT", "PEER1"],
    )
    candidate = gl.GuidanceCandidate(
        ticker="SUBJECT",
        kind="guidance_withdrawn",
        lane="commitments",
        subject_key="SUBJECT",
        subject_label="SUBJECT management guidance practice",
        last_present_period="2025-09-30",
        last_present_rank=8102,
        as_of_period="2026-03-31",
        as_of_rank=8104,
        current_silence=2,
        historical_max_gap=1,
        n_known_periods=6,
    )
    _, is_wave = apply_wave_suppression(candidate, corpus, wave_key=GUIDANCE_WAVE_KEY)
    assert is_wave is False


def test_no_corpus_never_suppresses() -> None:
    candidate = gl.GuidanceCandidate(
        ticker="SUBJECT",
        kind="guidance_withdrawn",
        lane="mdna_heading",
        subject_key="outlook",
        subject_label="Outlook",
        last_present_period="2025Q3",
        last_present_rank=10103,
        as_of_period="10105",
        as_of_rank=10105,
        current_silence=2,
        historical_max_gap=1,
        n_known_periods=6,
    )
    _, is_wave = apply_wave_suppression(candidate, None, wave_key=MDNA_WAVE_KEY)
    assert is_wave is False


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


def test_write_guidance_events_idempotent(conn: sqlite3.Connection) -> None:
    candidate = gl.GuidanceCandidate(
        ticker="NU",
        kind="guidance_withdrawn",
        lane="commitments",
        subject_key="NU",
        subject_label="NU management guidance practice",
        last_present_period="2025-09-30",
        last_present_rank=8102,
        as_of_period="2026-03-31",
        as_of_rank=8104,
        current_silence=2,
        historical_max_gap=1,
        n_known_periods=6,
    )
    event = candidate_to_event(candidate, is_mechanical_wave=False)
    n1 = write_guidance_events(conn, [event])
    n2 = write_guidance_events(conn, [event])
    assert n1 == 1
    assert n2 == 1
    rows = conn.execute("SELECT COUNT(*) FROM disclosure_events").fetchone()
    assert rows[0] == 1
    row = conn.execute("SELECT * FROM disclosure_events").fetchone()
    assert row["event_type"] == "guidance_withdrawn"
    assert row["verdict"] == "unclassified"
    assert row["evidence_quote"]


def test_write_guidance_events_missing_table_hard_stops() -> None:
    bare_conn = sqlite3.connect(":memory:")
    candidate = gl.GuidanceCandidate(
        ticker="NU",
        kind="guidance_withdrawn",
        lane="commitments",
        subject_key="NU",
        subject_label="NU management guidance practice",
        last_present_period="2025-09-30",
        last_present_rank=8102,
        as_of_period="2026-03-31",
        as_of_rank=8104,
        current_silence=2,
        historical_max_gap=1,
        n_known_periods=6,
    )
    event = candidate_to_event(candidate, is_mechanical_wave=False)
    with pytest.raises(HardStopError):
        write_guidance_events(bare_conn, [event])
