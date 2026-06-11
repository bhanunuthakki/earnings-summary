"""Tests for the `news` persistence gate (``src/news/store.py``).

Two contracts:

  * **NewsRow validator** — the §2.3 invariant: ``published_at`` must be exactly
    UTC ``'YYYY-MM-DD HH:MM:SS'``. The trigger's recency filter compares it
    lexically, so an ISO-'T', fractional-second, tz-suffixed, or date-only value
    would silently skew the 24h window. The validator rejects (never coerces).

  * **upsert_news_rows dedup/idempotency** — ``INSERT OR IGNORE`` on the table's
    ``UNIQUE (ticker, url)`` constraint: a repeated (ticker, url) is ignored,
    while the same url under two tickers yields two rows (the compound key is the
    point). Exercised against the REAL schema built by migration ``0065_news``,
    not an inline fixture, so the constraint under test is the shipped one.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from news.store import (
    SOURCE_FEED_FMP,
    SOURCE_FEED_WEBSEARCH,
    NewsRow,
    drop_duplicate_stories,
    normalize_headline,
    upsert_news_rows,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0064_queued_actions"
NEWS_HEAD = "0065_news"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def news_db(tmp_path: Path) -> Path:
    """Tmp DB with the real `news` table (migration 0065_news on top of 0064)."""
    db = tmp_path / "news_store.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, NEWS_HEAD)
    return db


def _row(
    *,
    ticker: str = "BN",
    url: str = "https://news.example/1",
    headline: str = "Acme acquires Beta for $2B",
    published_at: str = "2026-05-30 14:00:00",
    snippet: str | None = None,
    source: str | None = None,
    source_feed: str = SOURCE_FEED_FMP,
) -> NewsRow:
    return NewsRow(
        ticker=ticker,
        url=url,
        headline=headline,
        published_at=published_at,
        snippet=snippet,
        source=source,
        source_feed=source_feed,
    )


def _count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM news").fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# NewsRow validator — the UTC fixed-format invariant
# ---------------------------------------------------------------------------


def test_newsrow_accepts_canonical_utc() -> None:
    assert _row(published_at="2026-05-30 14:00:00").published_at == "2026-05-30 14:00:00"
    # Midnight / single-event-of-day boundary stays canonical.
    assert _row(published_at="2026-01-01 00:00:00").published_at == "2026-01-01 00:00:00"


@pytest.mark.parametrize(
    "bad",
    [
        "2026-05-30T14:00:00",  # ISO 'T' separator — sorts after a space, skews the window
        "2026-05-30 14:00:00.5",  # fractional seconds
        "2026-05-30 14:00:00Z",  # zulu zone suffix
        "2026-05-30 14:00:00+00:00",  # explicit offset
        "2026-05-30",  # date only (no time)
        "2026-5-30 14:00:00",  # non-zero-padded month/day (wrong width)
        "",  # empty
        "not a date",  # garbage
    ],
)
def test_newsrow_rejects_noncanonical(bad: str) -> None:
    with pytest.raises(ValidationError):
        _row(published_at=bad)


def test_newsrow_rejects_implausible_dates() -> None:
    """A correctly-shaped but absurd ``published_at`` (pre-2000 epoch artifact, or
    a future-dated story that would fire material_news immediately) is rejected by
    the plausibility gate — the format check alone would let these through."""
    # Pre-2000 floor: a 1970 epoch artifact.
    with pytest.raises(ValidationError):
        _row(published_at="1970-01-01 00:00:00")
    with pytest.raises(ValidationError):
        _row(published_at="1999-12-31 23:59:59")
    # Future ceiling: a story dated well beyond the small clock-skew tolerance.
    far_future = (datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with pytest.raises(ValidationError):
        _row(published_at=far_future)


def test_newsrow_accepts_recent_and_just_now() -> None:
    """A real just-published story (now, within the skew tolerance) still passes —
    the plausibility gate is a sanity bound, not a recency filter."""
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    just_now = now_naive.strftime("%Y-%m-%d %H:%M:%S")
    assert _row(published_at=just_now).published_at == just_now
    recent = (now_naive - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    assert _row(published_at=recent).published_at == recent
    # Year-2000 floor boundary is admitted.
    assert _row(published_at="2000-01-01 00:00:00").published_at == "2000-01-01 00:00:00"


def test_newsrow_forbids_extra_fields() -> None:
    """``extra="forbid"``: a feed that drifts an unexpected field fails loud.

    Built via ``model_validate`` (a dict boundary) so the intentionally-extra
    key is the runtime assertion under test, not a static type error.
    """
    with pytest.raises(ValidationError):
        NewsRow.model_validate(
            {
                "ticker": "BN",
                "url": "https://news.example/1",
                "headline": "h",
                "published_at": "2026-05-30 14:00:00",
                "source_feed": SOURCE_FEED_FMP,
                "unexpected": "x",
            }
        )


# ---------------------------------------------------------------------------
# upsert_news_rows — dedup / idempotency on UNIQUE (ticker, url)
# ---------------------------------------------------------------------------


def test_upsert_inserts_distinct_rows(news_db: Path) -> None:
    conn = sqlite3.connect(str(news_db))
    try:
        inserted, deduped = upsert_news_rows(
            conn, [_row(url="https://news.example/1"), _row(url="https://news.example/2")]
        )
        assert (inserted, deduped) == (2, 0)
        assert _count(conn) == 2
    finally:
        conn.close()


def test_upsert_dedups_same_ticker_url_within_call(news_db: Path) -> None:
    conn = sqlite3.connect(str(news_db))
    try:
        inserted, deduped = upsert_news_rows(
            conn, [_row(url="https://news.example/dup"), _row(url="https://news.example/dup")]
        )
        assert (inserted, deduped) == (1, 1)
        assert _count(conn) == 1
    finally:
        conn.close()


def test_upsert_is_idempotent_across_calls(news_db: Path) -> None:
    conn = sqlite3.connect(str(news_db))
    try:
        assert upsert_news_rows(conn, [_row(url="https://news.example/x")]) == (1, 0)
        # A same-day re-run sees the row already present -> ignored, not duplicated.
        assert upsert_news_rows(conn, [_row(url="https://news.example/x")]) == (0, 1)
        assert _count(conn) == 1
    finally:
        conn.close()


def test_upsert_same_url_different_ticker_kept(news_db: Path) -> None:
    """A syndicated URL legitimately appears under two tickers — both stored,
    proving the dedup key is (ticker, url), not url alone."""
    conn = sqlite3.connect(str(news_db))
    try:
        inserted, deduped = upsert_news_rows(
            conn,
            [
                _row(ticker="BN", url="https://news.example/syndicated"),
                _row(ticker="GOOG", url="https://news.example/syndicated"),
            ],
        )
        assert (inserted, deduped) == (2, 0)
        assert _count(conn) == 2
    finally:
        conn.close()


def test_upsert_persists_all_columns_and_stamps_fetched_at(news_db: Path) -> None:
    conn = sqlite3.connect(str(news_db))
    try:
        _ = upsert_news_rows(
            conn,
            [
                _row(
                    ticker="BN",
                    url="https://news.example/full",
                    headline="Brookfield closes $12B fund",
                    published_at="2026-05-30 09:30:00",
                    snippet="Largest close to date.",
                    source="Reuters",
                    source_feed=SOURCE_FEED_WEBSEARCH,
                )
            ],
        )
        row = conn.execute(
            "SELECT ticker, headline, url, published_at, snippet, source, source_feed, fetched_at "
            "FROM news"
        ).fetchone()
        assert row is not None
        assert row[:7] == (
            "BN",
            "Brookfield closes $12B fund",
            "https://news.example/full",
            "2026-05-30 09:30:00",
            "Largest close to date.",
            "Reuters",
            "websearch_opus",
        )
        # fetched_at is stamped by the writer in the same canonical UTC shape.
        assert isinstance(row[7], str)
        assert len(row[7]) == 19
    finally:
        conn.close()


def test_newsrow_rejects_empty_identity_fields() -> None:
    """The gate rejects an empty ticker / url / headline. An empty url or ticker
    silently degrades the (ticker, url) dedup key, and an empty headline makes a
    material-news alert meaningless — so the row is refused at construction
    rather than persisted with no source link (regrade memo gap #5)."""
    _row()  # baseline: the valid shape constructs fine
    for field in ("ticker", "url", "headline"):
        with pytest.raises(ValidationError):
            _row(**{field: ""})


# ---------------------------------------------------------------------------
# drop_duplicate_stories — the cross-feed (ticker, headline, date) guard
# ---------------------------------------------------------------------------


def test_normalize_headline_collapses_punctuation_and_case() -> None:
    assert normalize_headline("Acme acquires Beta for $2B!") == "acme acquires beta for 2b"
    assert normalize_headline("ACME — acquires   'Beta', for $2B") == "acme acquires beta for 2b"


def test_drop_duplicates_against_queued_rows_and_within_batch(news_db: Path) -> None:
    """A candidate matching a queued primary-feed row (same ticker, normalized
    headline, date — different url/feed) is dropped; so is a within-batch
    repeat. Different ticker or different date survives."""
    primary = _row(url="https://news.example/fmp", headline="Acme acquires Beta for $2B!")
    candidates = [
        _row(  # same story, EDGAR rendering — punctuation/case differ, url differs
            url="https://sec.example/8k",
            headline="ACME acquires Beta, for $2B",
            source_feed="edgar_8k",
        ),
        _row(  # within-batch duplicate of a fresh story
            url="https://a/1", headline="Beta CFO departs", source_feed="edgar_8k"
        ),
        _row(  # the survivor for that story
            url="https://a/2", headline="Beta — CFO departs!", source_feed="edgar_8k"
        ),
        _row(  # same headline as primary but another DATE -> kept
            url="https://a/3",
            headline="Acme acquires Beta for $2B",
            published_at="2026-05-29 14:00:00",
            source_feed="edgar_8k",
        ),
        _row(  # same headline as primary but another TICKER -> kept
            ticker="WPM", url="https://a/4", headline="Acme acquires Beta for $2B"
        ),
    ]
    conn = sqlite3.connect(str(news_db))
    try:
        kept = drop_duplicate_stories(conn, candidates, against=[primary])
    finally:
        conn.close()
    assert [r.url for r in kept] == ["https://a/1", "https://a/3", "https://a/4"]


def test_drop_duplicates_against_already_persisted_rows(news_db: Path) -> None:
    """Yesterday's run persisted the story; today another feed re-renders it
    under a new url — the table lookup catches it."""
    conn = sqlite3.connect(str(news_db))
    try:
        _ = upsert_news_rows(conn, [_row(url="https://news.example/orig")])
        candidate = _row(
            url="https://other.example/copy",
            headline="Acme Acquires Beta for $2B",
            source_feed=SOURCE_FEED_WEBSEARCH,
        )
        kept = drop_duplicate_stories(conn, [candidate])
        assert kept == []
        fresh = _row(url="https://other.example/new", headline="Acme raises guidance")
        assert drop_duplicate_stories(conn, [fresh]) == [fresh]
    finally:
        conn.close()


def test_drop_duplicates_degrades_to_batch_only_without_table(tmp_path: Path) -> None:
    """No `news` table -> the DB check degrades silently to batch-only dedup
    (the subsequent upsert still surfaces the missing table loudly)."""
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    try:
        twice = [_row(url="https://a/1"), _row(url="https://a/2")]
        kept = drop_duplicate_stories(conn, twice)
    finally:
        conn.close()
    assert [r.url for r in kept] == ["https://a/1"]
