"""Tests for the P2 cross-sectional detrending module.

Weighted toward the failure modes ``docs/design/disclosure_change_build_stack.md``
§P2 calls out:

  * a percentile computed from too few peers is statistically meaningless —
    the gate must degrade honestly (``gate_ran=False``, a reason code) rather
    than report a fabricated 0.0/1.0;
  * ``materiality`` gets written ONLY for item_added/item_removed rows —
    item_reworded's existing P0-owned magnitude must never be touched;
  * a ticker's own row must never appear in its own peer population;
  * the write is idempotent (safe to re-run);
  * a missing table is a hard stop, not a silent empty result.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings import cross_sectional_detrend as csd  # noqa: E402
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
    canonical_id VARCHAR(64),
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
        (ticker, event_type, fiscal_year, fiscal_period, subject, detector_version)
);
"""


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA)
    connection.commit()
    return connection


def _insert_event(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    event_type: str,
    fiscal_year: int,
    fiscal_period: str,
    subject: str,
    canonical_id: str = "risk_factors",
    materiality: float | None = None,
    verdict: str = "unclassified",
) -> None:
    conn.execute(
        """
        INSERT INTO disclosure_events
            (ticker, event_type, fiscal_year, fiscal_period, canonical_id, subject,
             subject_label, evidence_quote, materiality, verdict, detector_version,
             status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'item_diff_v1', 'new', '2026-01-01T00:00:00')
        """,
        (
            ticker,
            event_type,
            fiscal_year,
            fiscal_period,
            canonical_id,
            subject,
            subject,
            "evidence",
            materiality,
            verdict,
        ),
    )


def _insert_section(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_year: int,
    fiscal_period: str,
    char_len: int,
    canonical_id: str = "risk_factors",
) -> None:
    conn.execute(
        """
        INSERT INTO filing_sections
            (ticker, source, source_ref, form, fiscal_year, fiscal_period,
             section_key_raw, section_stem, canonical_id, ordinal, text, text_sha256,
             char_len, extractor_version, created_at)
        VALUES (?, 'edgar_text', ?, '10-K', ?, ?, 'Item 1A', 'item 1a', ?, 0, ?, 'x',
                ?, 'v1', '2026-01-01T00:00:00')
        """,
        (
            ticker,
            f"{ticker}-{fiscal_year}-{fiscal_period}",
            fiscal_year,
            fiscal_period,
            canonical_id,
            "x" * char_len,
            char_len,
        ),
    )


# ---------------------------------------------------------------------------
# Peer-count gate
# ---------------------------------------------------------------------------


def test_insufficient_peers_degrades_honestly(conn: sqlite3.Connection) -> None:
    """With fewer than MIN_CROSS_SECTION_PEERS OTHER tickers in the bucket,
    the score must NOT fabricate a percentile."""
    _insert_event(
        conn,
        ticker="AAA",
        event_type="item_added",
        fiscal_year=2025,
        fiscal_period="Q1",
        subject="s1",
    )
    _insert_event(
        conn,
        ticker="BBB",
        event_type="item_added",
        fiscal_year=2025,
        fiscal_period="Q1",
        subject="s2",
    )
    conn.commit()

    corpus = csd.build_cross_sectional_corpus(conn)
    bucket = csd.PeriodBucket(canonical_id="risk_factors", fiscal_year=2025, fiscal_period="Q1")
    score = csd.score_ticker_bucket(corpus, "AAA", bucket)

    assert score.gate_ran is False
    assert score.combined_percentile is None
    assert score.reason is not None
    assert "insufficient_cross_section_peers" in score.reason


def test_sufficient_peers_computes_percentile(conn: sqlite3.Connection) -> None:
    """AAA has the most item-change volume of 4 tickers -> high percentile."""
    counts = {"AAA": 5, "BBB": 1, "CCC": 2, "DDD": 1}
    for ticker, n in counts.items():
        for i in range(n):
            _insert_event(
                conn,
                ticker=ticker,
                event_type="item_added",
                fiscal_year=2025,
                fiscal_period="Q1",
                subject=f"s{ticker}{i}",
            )
    conn.commit()

    corpus = csd.build_cross_sectional_corpus(conn)
    bucket = csd.PeriodBucket(canonical_id="risk_factors", fiscal_year=2025, fiscal_period="Q1")
    score = csd.score_ticker_bucket(corpus, "AAA", bucket)

    assert score.gate_ran is True
    assert score.peer_count == 3
    assert score.raw_item_change_count == 5
    assert score.item_change_percentile == 1.0
    assert score.combined_percentile == 1.0
    assert score.quintile == 5

    # The lowest-volume ticker should rank at/near the bottom.
    low_score = csd.score_ticker_bucket(corpus, "BBB", bucket)
    assert low_score.item_change_percentile is not None
    assert score.item_change_percentile is not None
    assert low_score.item_change_percentile < score.item_change_percentile


def test_ticker_excluded_from_its_own_peer_population(conn: sqlite3.Connection) -> None:
    for ticker in ("AAA", "BBB", "CCC", "DDD"):
        _insert_event(
            conn,
            ticker=ticker,
            event_type="item_added",
            fiscal_year=2025,
            fiscal_period="Q1",
            subject=f"s{ticker}",
        )
    conn.commit()
    corpus = csd.build_cross_sectional_corpus(conn)
    bucket = csd.PeriodBucket(canonical_id="risk_factors", fiscal_year=2025, fiscal_period="Q1")
    peers = corpus.peers(bucket, exclude_ticker="AAA")
    assert all(p.ticker != "AAA" for p in peers)
    assert len(peers) == 3


# ---------------------------------------------------------------------------
# materiality write scope: item_added/item_removed only, never item_reworded
# ---------------------------------------------------------------------------


def test_write_never_touches_item_reworded_materiality(conn: sqlite3.Connection) -> None:
    """item_reworded's materiality is P0-owned (raw 1-similarity). P2 must
    leave it untouched even when a percentile is computed for the bucket."""
    for ticker in ("AAA", "BBB", "CCC", "DDD"):
        _insert_event(
            conn,
            ticker=ticker,
            event_type="item_added",
            fiscal_year=2025,
            fiscal_period="Q1",
            subject=f"added-{ticker}",
        )
    # AAA also has a reworded event with a pre-existing raw materiality.
    _insert_event(
        conn,
        ticker="AAA",
        event_type="item_reworded",
        fiscal_year=2025,
        fiscal_period="Q1",
        subject="reworded-AAA",
        materiality=0.42,
    )
    conn.commit()

    corpus = csd.build_cross_sectional_corpus(conn)
    scores = csd.score_all(corpus)
    written = csd.write_detrended_materiality(conn, scores)
    assert written > 0

    reworded_materiality = conn.execute(
        "SELECT materiality FROM disclosure_events WHERE subject = 'reworded-AAA'"
    ).fetchone()[0]
    assert reworded_materiality == pytest.approx(0.42)

    added_materiality = conn.execute(
        "SELECT materiality FROM disclosure_events WHERE subject = 'added-AAA'"
    ).fetchone()[0]
    assert added_materiality is not None


def test_write_skips_ungated_scores(conn: sqlite3.Connection) -> None:
    """A score with gate_ran=False (too few peers) must not write anything —
    NULL stays NULL, never a fabricated 0.0."""
    _insert_event(
        conn,
        ticker="AAA",
        event_type="item_added",
        fiscal_year=2025,
        fiscal_period="Q1",
        subject="s1",
    )
    conn.commit()
    corpus = csd.build_cross_sectional_corpus(conn)
    scores = csd.score_all(corpus)
    csd.write_detrended_materiality(conn, scores)
    val = conn.execute("SELECT materiality FROM disclosure_events WHERE subject = 's1'").fetchone()[
        0
    ]
    assert val is None


def test_write_is_idempotent(conn: sqlite3.Connection) -> None:
    for ticker in ("AAA", "BBB", "CCC", "DDD"):
        _insert_event(
            conn,
            ticker=ticker,
            event_type="item_removed",
            fiscal_year=2025,
            fiscal_period="Q1",
            subject=f"s{ticker}",
        )
    conn.commit()
    corpus = csd.build_cross_sectional_corpus(conn)
    scores = csd.score_all(corpus)
    first = csd.write_detrended_materiality(conn, scores)
    second = csd.write_detrended_materiality(conn, scores)
    assert first == second
    values = conn.execute("SELECT materiality FROM disclosure_events ORDER BY subject").fetchall()
    assert all(v[0] is not None for v in values)


# ---------------------------------------------------------------------------
# Section-length deltas
# ---------------------------------------------------------------------------


def test_length_delta_uses_immediately_preceding_stored_period(conn: sqlite3.Connection) -> None:
    _insert_section(conn, ticker="AAA", fiscal_year=2024, fiscal_period="FY", char_len=1000)
    _insert_section(conn, ticker="AAA", fiscal_year=2025, fiscal_period="FY", char_len=1500)
    conn.commit()
    corpus = csd.build_cross_sectional_corpus(conn)
    bucket = csd.PeriodBucket(canonical_id="risk_factors", fiscal_year=2025, fiscal_period="FY")
    mags = corpus.by_bucket[bucket]
    aaa = next(m for m in mags if m.ticker == "AAA")
    assert aaa.length_chars == 1500
    assert aaa.prior_length_chars == 1000
    assert aaa.length_delta_chars == 500

    # The first stored period has no prior -> None, not zero.
    first_bucket = csd.PeriodBucket(
        canonical_id="risk_factors", fiscal_year=2024, fiscal_period="FY"
    )
    first_mag = next(m for m in corpus.by_bucket[first_bucket] if m.ticker == "AAA")
    assert first_mag.prior_length_chars is None
    assert first_mag.length_delta_chars is None


# ---------------------------------------------------------------------------
# Missing table / hard stop
# ---------------------------------------------------------------------------


def test_missing_events_table_is_hard_stop() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE filing_sections (ticker VARCHAR(16), canonical_id VARCHAR(64), "
        "fiscal_year INTEGER, fiscal_period VARCHAR(4), char_len INTEGER);"
    )
    with pytest.raises(HardStopError):
        csd.build_cross_sectional_corpus(conn)


def test_missing_sections_table_is_hard_stop() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE disclosure_events (ticker VARCHAR(16), event_type VARCHAR(32), "
        "canonical_id VARCHAR(64), fiscal_year INTEGER, fiscal_period VARCHAR(4));"
    )
    with pytest.raises(HardStopError):
        csd.build_cross_sectional_corpus(conn)
