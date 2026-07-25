"""Tests for D2.2 (docs/design/disclosure_intelligence_v1_prd.md) — detrended
document-level YoY similarity, the Lazy Prices construct.

Weighted toward the traps ``docs/design/disclosure_gap_scoping.md`` Gap 4
calls out explicitly:

  * mandatory book-level detrending — a score with too few book-level peers
    must NEVER be persisted un-detrended (skipping this "inverts the
    finding");
  * a ticker's own row must never appear in its own peer population;
  * peer-group (comparable-set) percentile degrades honestly to absent when
    no comparable set is frozen, or too few comp-set peers carry a score in
    the same bucket -- book-level percentile still ships;
  * multiple filing_sections rows for the SAME period (any source/ordinal)
    concatenate into one period text, mirroring item_diff's convention;
  * writes are idempotent;
  * the verbatim evidence quote is genuinely NEW text, not copy-pasted from
    the prior period.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings import section_similarity as ss  # noqa: E402
from filings.models import HardStopError  # noqa: E402

_SCHEMA = """
CREATE TABLE filing_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    source VARCHAR(16) NOT NULL,
    source_ref VARCHAR(255) NOT NULL,
    doc_id INTEGER,
    accession_number VARCHAR(32),
    form VARCHAR(8) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4) NOT NULL,
    period_end DATETIME,
    filing_date VARCHAR(10),
    section_key_raw VARCHAR(255) NOT NULL,
    section_stem VARCHAR(255) NOT NULL,
    canonical_id VARCHAR(64),
    title TEXT,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_sha256 VARCHAR(64) NOT NULL,
    char_len INTEGER NOT NULL,
    key_truncated INTEGER NOT NULL DEFAULT 0,
    extractor_version VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_filing_sections_key UNIQUE (source, source_ref, section_key_raw, ordinal)
);
CREATE TABLE filing_section_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    source VARCHAR(16) NOT NULL,
    form VARCHAR(8) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4) NOT NULL,
    source_ref VARCHAR(255),
    status VARCHAR(24) NOT NULL,
    reason_code VARCHAR(64),
    detail TEXT,
    sections_written INTEGER NOT NULL DEFAULT 0,
    extractor_version VARCHAR(32) NOT NULL,
    checked_at DATETIME NOT NULL
);
CREATE TABLE comparable_set_members (
    comparable_set_id TEXT NOT NULL,
    member_ticker TEXT NOT NULL,
    membership_reason TEXT NOT NULL,
    context_only INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    PRIMARY KEY (comparable_set_id, member_ticker, valid_from)
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


_SEQ = [0]


def _add_section(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    canonical_id: str,
    fiscal_year: int,
    fiscal_period: str,
    text: str,
    source: str = "edgar_text",
) -> None:
    _SEQ[0] += 1
    conn.execute(
        """
        INSERT INTO filing_sections
            (ticker, source, source_ref, doc_id, accession_number, form, fiscal_year,
             fiscal_period, period_end, filing_date, section_key_raw, section_stem,
             canonical_id, title, ordinal, text, text_sha256, char_len, key_truncated,
             extractor_version, created_at)
        VALUES (?, ?, ?, NULL, NULL, '10-K', ?, ?, NULL, NULL, ?, ?, ?, ?, 0, ?, ?, ?, 0, 'v1', '2026-01-01')
        """,
        (
            ticker,
            source,
            f"ref-{_SEQ[0]}",
            fiscal_year,
            fiscal_period,
            canonical_id,
            canonical_id,
            canonical_id,
            canonical_id,
            text,
            "x" * 64,
            len(text),
        ),
    )


def _rev_text(n_unique_words: int, seed: int) -> str:
    """A synthetic section body with ``n_unique_words`` distinct tokens,
    reproducible by ``seed`` -- lets tests dial in an exact Jaccard score."""
    return " ".join(f"word{seed}_{i}" for i in range(n_unique_words)) + ". "


def test_book_percentile_required_before_emission(conn: sqlite3.Connection) -> None:
    """Only ONE ticker in the corpus (itself) -- book-level percentile can
    never be computed (MIN_CROSS_SECTION_PEERS not met), so no event is
    emitted at all, never an un-detrended one."""
    _add_section(
        conn,
        ticker="SOLO",
        canonical_id="risk_factors",
        fiscal_year=2024,
        fiscal_period="FY",
        text=_rev_text(50, 1),
    )
    _add_section(
        conn,
        ticker="SOLO",
        canonical_id="risk_factors",
        fiscal_year=2025,
        fiscal_period="FY",
        text=_rev_text(50, 2),
    )
    corpus = ss.build_similarity_corpus(conn, tickers=["SOLO"])
    scores = ss.score_all(conn, corpus, tickers=["SOLO"])
    assert len(scores) == 1
    event = ss.score_to_event(scores[0])
    assert event is None  # too few book peers -- must not emit un-detrended


def _seed_book(
    conn: sqlite3.Connection, tickers: list[str], *, change_seed_offset: int = 0
) -> None:
    """Every ticker gets an FY2024 and FY2025 risk_factors section; the
    AMOUNT of change (unique new tokens introduced) varies by ticker index
    so the cross-section has real spread to percentile against."""
    for i, t in enumerate(tickers):
        _add_section(
            conn,
            ticker=t,
            canonical_id="risk_factors",
            fiscal_year=2024,
            fiscal_period="FY",
            text=_rev_text(100, seed=1000 + i),
        )
        # Ticker i introduces i*10 brand-new tokens on top of a shared core --
        # more new tokens => lower similarity => higher change_magnitude.
        shared = _rev_text(100, seed=1000 + i)
        novel = _rev_text(i * 10 + change_seed_offset, seed=2000 + i)
        _add_section(
            conn,
            ticker=t,
            canonical_id="risk_factors",
            fiscal_year=2025,
            fiscal_period="FY",
            text=shared + novel,
        )


def test_book_level_percentile_ranks_against_whole_book(conn: sqlite3.Connection) -> None:
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    _seed_book(conn, tickers)
    corpus = ss.build_similarity_corpus(conn, tickers=tickers)
    scores = ss.score_all(conn, corpus, tickers=tickers)
    by_ticker = {s.pair.ticker: s for s in scores}
    # EEE introduced the most novel tokens -> highest change_magnitude ->
    # should rank at or near the top of the book-level percentile.
    eee_pct = by_ticker["EEE"].book_percentile
    aaa_pct = by_ticker["AAA"].book_percentile
    assert eee_pct is not None
    assert aaa_pct is not None
    assert eee_pct >= aaa_pct
    # A ticker's own score must never appear in its own peer population.
    assert by_ticker["AAA"].book_peer_count == len(tickers) - 1


def test_peer_group_percentile_uses_comparable_set_only(conn: sqlite3.Connection) -> None:
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    _seed_book(conn, tickers)
    # AAA's frozen comparable set is only {BBB, CCC, DDD} -- EEE/FFF are book
    # peers but NOT comp-set peers.
    for peer in ("BBB", "CCC", "DDD"):
        conn.execute(
            "INSERT INTO comparable_set_members "
            "(comparable_set_id, member_ticker, membership_reason, context_only, valid_from) "
            "VALUES ('AAA_1', ?, 'industry_seed', 0, '2026-01-01')",
            (peer,),
        )
    corpus = ss.build_similarity_corpus(conn, tickers=tickers)
    scores = ss.score_all(conn, corpus, tickers=["AAA"])
    assert len(scores) == 1
    score = scores[0]
    assert score.comparable_set_id == "AAA_1"
    assert score.peer_count == 3
    assert score.peer_percentile is not None
    # Peer percentile and book percentile are computed over different
    # populations and need not match.
    event = ss.score_to_event(score)
    assert event is not None
    assert event.peer_percentile == score.peer_percentile
    assert "AAA_1" in event.interpretation_md


def test_no_comparable_set_degrades_honestly_book_only(conn: sqlite3.Connection) -> None:
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    _seed_book(conn, tickers)
    corpus = ss.build_similarity_corpus(conn, tickers=tickers)
    scores = ss.score_all(conn, corpus, tickers=["AAA"])
    score = scores[0]
    assert score.peer_percentile is None
    assert score.peer_reason == "no_comparable_set"
    event = ss.score_to_event(score)
    assert event is not None  # book-level percentile alone still ships
    assert "unavailable" in event.interpretation_md


def test_multi_source_same_period_concatenates(conn: sqlite3.Connection) -> None:
    """Two filing_sections rows for the SAME (ticker, canonical_id,
    fiscal_year, fiscal_period) -- e.g. edgar_text + fmp_rfile -- must merge
    into ONE period's text, not two separate periods."""
    _add_section(
        conn,
        ticker="ZZZ",
        canonical_id="mdna",
        fiscal_year=2024,
        fiscal_period="FY",
        text="first part. ",
        source="edgar_text",
    )
    _add_section(
        conn,
        ticker="ZZZ",
        canonical_id="mdna",
        fiscal_year=2024,
        fiscal_period="FY",
        text="second part.",
        source="fmp_rfile",
    )
    _add_section(
        conn,
        ticker="ZZZ",
        canonical_id="mdna",
        fiscal_year=2025,
        fiscal_period="FY",
        text="a wholly different body of text entirely unrelated.",
    )
    periods = ss.load_concatenated_section_periods(conn, "ZZZ", "mdna")
    assert len(periods) == 2
    assert "first part" in periods[0].text
    assert "second part" in periods[0].text


def test_evidence_quote_is_genuinely_novel_text() -> None:
    prior = "The company faces risks related to competition and market volatility in its sector."
    current = (
        "The company faces risks related to competition and market volatility in its sector. "
        "A brand new supply chain disruption emerged this year affecting semiconductor availability."
    )
    quote = ss._novel_excerpt(prior, current)  # pyright: ignore[reportPrivateUsage]
    assert "semiconductor" in quote or "supply chain" in quote


def test_write_similarity_events_idempotent(conn: sqlite3.Connection) -> None:
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    _seed_book(conn, tickers)
    corpus = ss.build_similarity_corpus(conn, tickers=tickers)
    scores = ss.score_all(conn, corpus, tickers=tickers)
    events = [e for e in (ss.score_to_event(s) for s in scores) if e is not None]
    assert events
    n1 = ss.write_similarity_events(conn, events)
    n2 = ss.write_similarity_events(conn, events)
    assert n1 == len(events)
    assert n2 == len(events)
    total = conn.execute("SELECT COUNT(*) FROM disclosure_events").fetchone()[0]
    assert total == len(events)
    row = conn.execute(
        "SELECT * FROM disclosure_events WHERE event_type='section_similarity_shift' LIMIT 1"
    ).fetchone()
    assert row["verdict"] == "unclassified"
    assert row["materiality"] is not None
    assert row["evidence_quote"]


def test_write_similarity_events_missing_table_hard_stops() -> None:
    bare_conn = sqlite3.connect(":memory:")
    event = ss.SimilarityEvent(
        ticker="AAA",
        canonical_id="risk_factors",
        fiscal_year=2025,
        fiscal_period="FY",
        prior_fiscal_year=2024,
        prior_fiscal_period="FY",
        source_ref=None,
        source_doc_id=None,
        evidence_quote="quote",
        prior_excerpt="prior",
        current_excerpt="current",
        book_percentile=0.9,
        peer_percentile=None,
        interpretation_md="note",
    )
    with pytest.raises(HardStopError):
        ss.write_similarity_events(bare_conn, [event])
