"""Tests for src/signals/store.py — the diet-substrate taxonomy + readers.

Covers the news→signals mirror mapping (yf_grades → consensus_rating), the
NON-decaying readers (recency stream + forward agenda), the investor-day
writer's idempotency, the lane predicates, and the CHECK↔store lockstep.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from signals.store import (
    CADENCES,
    DIET_ONLY_TYPES,
    NEWS_MIRRORED_TYPES,
    SCAFFOLD_TYPES,
    SIGNAL_BUYSIDE_RATING,
    SIGNAL_CONSENSUS_RATING,
    SIGNAL_ESTIMATE_REVISION,
    SIGNAL_GENERAL_NEWS,
    SIGNAL_INVESTOR_DAY,
    SIGNAL_MEDIA_APPEARANCE,
    SIGNAL_TYPES,
    SignalRow,
    is_diet_only,
    is_news_mirrored,
    load_diet_signals,
    load_forward_agenda,
    record_investor_day,
    record_media_appearance,
    sync_news_to_signals,
)

from ._signals_fixtures import make_news_then_signals, signals_only

# (ticker, headline, url, published_at, snippet, source, source_feed, fetched_at)
_NEWS = [
    (
        "NU",
        "Nu launches product",
        "http://x/1",
        "2026-06-01 12:00:00",
        "s",
        "Reuters",
        "fmp_stock_news",
        "t",
    ),
    (
        "META",
        "MS upgrades META to Buy",
        "http://x/2",
        "2026-06-11 09:00:00",
        None,
        "MS",
        "yf_grades",
        "t",
    ),
    (
        "NU",
        "Nu beats estimates",
        "http://x/3",
        "2026-06-12 09:00:00",
        "s2",
        "Bloomberg",
        "fmp_stock_news",
        "t",
    ),
]


@pytest.fixture
def db_with_news(tmp_path: Path) -> Path:
    db = tmp_path / "signals.db"
    make_news_then_signals(db, _NEWS)
    return db


# ---------------------------------------------------------------------------
# Backfill / sync mapping
# ---------------------------------------------------------------------------


def test_backfill_maps_yf_grades_to_consensus_rating(db_with_news: Path) -> None:
    rows = load_diet_signals(db_with_news, types=SIGNAL_TYPES)
    by_url = {r.url: r for r in rows}
    # yf_grades story → typed consensus_rating; everything else → general_news.
    assert by_url["http://x/2"].signal_type == SIGNAL_CONSENSUS_RATING
    assert by_url["http://x/2"].weight == pytest.approx(0.8)
    assert by_url["http://x/1"].signal_type == SIGNAL_GENERAL_NEWS
    assert by_url["http://x/1"].weight == pytest.approx(0.5)
    # The news back-reference is preserved (the mirror's dedup key).
    assert all(r.news_id is not None for r in rows)


def test_sync_is_idempotent(db_with_news: Path) -> None:
    conn = sqlite3.connect(str(db_with_news))
    try:
        # The migration already backfilled; a re-sync adds nothing.
        assert sync_news_to_signals(conn) == 0
        before = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert sync_news_to_signals(conn) == 0
        after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert before == after == len(_NEWS)
    finally:
        conn.close()


def test_sync_picks_up_new_news(db_with_news: Path) -> None:
    conn = sqlite3.connect(str(db_with_news))
    try:
        conn.execute(
            "INSERT INTO news (ticker, headline, url, published_at, source_feed, fetched_at) "
            "VALUES ('NU', 'Fresh story', 'http://x/9', '2026-06-13 08:00:00', 'fmp_stock_news', 't')"
        )
        conn.commit()
        assert sync_news_to_signals(conn) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Readers — both NON-decaying
# ---------------------------------------------------------------------------


def test_ingest_stream_is_newest_first_and_clock_independent(db_with_news: Path) -> None:
    rows = load_diet_signals(db_with_news)
    stamps = [r.published_at for r in rows]
    # Ordered by published_at DESC — a pure stored-field sort, no decay of `now`.
    assert stamps == sorted(stamps, reverse=True)
    # default types = the news-backed reading lanes only (no forward-dated rows).
    assert {r.signal_type for r in rows} <= NEWS_MIRRORED_TYPES


def test_forward_agenda_event_date_ascending_and_excludes_past(db_with_news: Path) -> None:
    conn = sqlite3.connect(str(db_with_news))
    try:
        record_investor_day(conn, "META", date(2026, 9, 18), "Analyst Day", firm="Meta IR")
        record_investor_day(conn, "NU", date(2026, 7, 1), "Investor Day", firm="Nu IR")
        record_investor_day(conn, "NU", date(2026, 1, 1), "Old day")  # past
    finally:
        conn.close()
    agenda = load_forward_agenda(db_with_news, on_or_after=date(2026, 6, 13))
    dates = [r.event_date for r in agenda]
    assert dates == ["2026-07-01", "2026-09-18"]  # ASC, past excluded
    assert all(r.signal_type == SIGNAL_INVESTOR_DAY for r in agenda)
    # Forward-dated rows never appear in the recency stream's default lanes.
    stream_urls = {r.url for r in load_diet_signals(db_with_news)}
    assert not any(r.url in stream_urls for r in agenda if r.url)


def test_record_investor_day_is_idempotent(db_with_news: Path) -> None:
    conn = sqlite3.connect(str(db_with_news))
    try:
        assert record_investor_day(conn, "META", date(2026, 9, 18), "Analyst Day") is True
        assert record_investor_day(conn, "META", date(2026, 9, 18), "Analyst Day") is False
        n = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE signal_type = ?", (SIGNAL_INVESTOR_DAY,)
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_record_media_appearance_is_diet_only_and_idempotent(db_with_news: Path) -> None:
    conn = sqlite3.connect(str(db_with_news))
    try:
        first = record_media_appearance(
            conn,
            "nu",
            "David Vélez on Invest Like the Best",
            url="http://pod/ep1",
            firm="Invest Like the Best",
            published_at=None,
        )
        dup = record_media_appearance(conn, "NU", "Re-poll same episode", url="http://pod/ep1")
        assert first is True
        assert dup is False  # idempotent on (ticker, url) among media rows
        row = conn.execute(
            "SELECT ticker, signal_type, cadence, news_id, event_date, weight, firm "
            "FROM signals WHERE signal_type = ?",
            (SIGNAL_MEDIA_APPEARANCE,),
        ).fetchone()
        assert row[0] == "NU"  # ticker upper-cased
        assert row[1] == SIGNAL_MEDIA_APPEARANCE
        assert row[2] == "event"
        assert row[3] is None  # written DIRECT — no news backing
        assert row[4] is None  # not forward-dated
        assert row[5] == pytest.approx(0.7)
        assert row[6] == "Invest Like the Best"
    finally:
        conn.close()
    # It is a pull-lane reading type — surfaces in the stream when requested.
    rows = load_diet_signals(
        db_with_news,
        types=(SIGNAL_GENERAL_NEWS, SIGNAL_CONSENSUS_RATING, SIGNAL_MEDIA_APPEARANCE),
    )
    assert any(r.signal_type == SIGNAL_MEDIA_APPEARANCE for r in rows)


def test_readers_degrade_on_missing_table(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert load_diet_signals(missing) == []
    assert load_forward_agenda(missing, on_or_after=date(2026, 6, 13)) == []
    # An empty (pre-news) signals table reads as empty, never raises.
    empty = tmp_path / "empty.db"
    signals_only(empty)
    assert load_diet_signals(empty) == []


# ---------------------------------------------------------------------------
# Lane taxonomy
# ---------------------------------------------------------------------------


def test_lane_partition_is_total_and_disjoint() -> None:
    assert NEWS_MIRRORED_TYPES | DIET_ONLY_TYPES == SIGNAL_TYPES
    assert not (NEWS_MIRRORED_TYPES & DIET_ONLY_TYPES)
    assert sorted(NEWS_MIRRORED_TYPES) == [SIGNAL_CONSENSUS_RATING, SIGNAL_GENERAL_NEWS]
    assert sorted(DIET_ONLY_TYPES) == sorted(
        [
            SIGNAL_INVESTOR_DAY,
            SIGNAL_BUYSIDE_RATING,
            SIGNAL_ESTIMATE_REVISION,
            SIGNAL_MEDIA_APPEARANCE,
        ]
    )
    # The disclosed fast-follows are a diet-only subset (no free data path).
    assert sorted(SCAFFOLD_TYPES) == [SIGNAL_BUYSIDE_RATING, SIGNAL_ESTIMATE_REVISION]
    assert SCAFFOLD_TYPES <= DIET_ONLY_TYPES


def test_predicates() -> None:
    assert is_news_mirrored(SIGNAL_CONSENSUS_RATING) and is_news_mirrored(SIGNAL_GENERAL_NEWS)
    assert not is_news_mirrored(SIGNAL_INVESTOR_DAY)
    assert is_diet_only(SIGNAL_INVESTOR_DAY) and is_diet_only(SIGNAL_BUYSIDE_RATING)
    assert not is_diet_only(SIGNAL_CONSENSUS_RATING)
    # media_appearance is diet-only (written direct, never a news-backed alert).
    assert is_diet_only(SIGNAL_MEDIA_APPEARANCE)
    assert not is_news_mirrored(SIGNAL_MEDIA_APPEARANCE)


def test_signal_row_is_a_distinct_diet_type() -> None:
    # A diet row is a SignalRow — never an InboxItem (the structural guarantee
    # the diet-guard test leans on).
    row = SignalRow(
        id=1,
        ticker="NU",
        signal_type=SIGNAL_GENERAL_NEWS,
        title="t",
        published_at="2026-06-01 00:00:00",
        weight=0.5,
        cadence="event",
    )
    assert row.__class__.__name__ == "SignalRow"


# ---------------------------------------------------------------------------
# CHECK ↔ store lockstep (the DB enforces exactly the store's vocab)
# ---------------------------------------------------------------------------


def _check_values(schema_sql: str, column: str) -> set[str]:
    """Extract the quoted literals from a ``CHECK (<column> IN ('a','b',…))``."""
    m = re.search(rf"{column} IN \(([^)]*)\)", schema_sql)
    assert m is not None, f"no CHECK on {column} in:\n{schema_sql}"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_check_constraints_match_store_constants(tmp_path: Path) -> None:
    db = tmp_path / "schema.db"
    signals_only(db)
    conn = sqlite3.connect(str(db))
    try:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'signals'").fetchone()[0]
    finally:
        conn.close()
    assert _check_values(sql, "signal_type") == set(SIGNAL_TYPES)
    assert _check_values(sql, "cadence") == set(CADENCES)
