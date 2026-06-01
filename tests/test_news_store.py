"""Tests for src/news/store.py — the ``NewsRow`` timestamp-format gate and the
``upsert_news_rows`` (ticker, url) dedup/idempotency contract (plan §6.3, §6.4).

Independent of the migration PR: the ``news`` table is created inline in the
fixture below (the six trigger-read columns from
``tests/test_trigger_material_news.py:50-59`` plus the ingestion bookkeeping
columns the migration adds), so this suite needs no Alembic run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from news.store import NewsRow, upsert_news_rows

# The news table as migration 0065 creates it: the six trigger-read columns plus
# source / source_feed / fetched_at bookkeeping and the UNIQUE (ticker, url)
# dedup key that upsert_news_rows relies on.
_CREATE_NEWS_TABLE = (
    "CREATE TABLE news ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "ticker TEXT NOT NULL, "
    "headline TEXT NOT NULL, "
    "url TEXT NOT NULL, "
    "published_at TEXT NOT NULL, "
    "snippet TEXT, "
    "source TEXT, "
    "source_feed TEXT NOT NULL DEFAULT 'fmp_stock_news', "
    "fetched_at TEXT NOT NULL, "
    "UNIQUE (ticker, url)"
    ")"
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """A temp in-memory SQLite DB with the ``news`` table pre-created."""
    connection = sqlite3.connect(":memory:")
    _ = connection.execute(_CREATE_NEWS_TABLE)
    try:
        yield connection
    finally:
        connection.close()


def _row(ticker: str, url: str, *, published_at: str = "2026-05-30 14:00:00") -> NewsRow:
    return NewsRow(
        ticker=ticker,
        headline="h",
        url=url,
        published_at=published_at,
        source_feed="fmp_stock_news",
    )


def _count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM news").fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# NewsRow.published_at validator — the §2.3 UTC-format invariant (§6.3)
# ---------------------------------------------------------------------------


def test_newsrow_accepts_canonical_utc_timestamp() -> None:
    row = NewsRow(
        ticker="GOOG",
        headline="Alphabet beats on cloud",
        url="https://example.com/a",
        published_at="2026-05-30 14:00:00",
        source_feed="fmp_stock_news",
    )
    assert row.published_at == "2026-05-30 14:00:00"


@pytest.mark.parametrize(
    "bad",
    [
        "2026-05-30T14:00:00",  # ISO 'T' separator — sorts after the space form, skews the window
        "2026-05-30 14:00:00.123",  # fractional seconds
        "",  # empty string
        "2026-05-30 14:00:00+00:00",  # tz suffix
    ],
)
def test_newsrow_rejects_noncanonical_timestamp(bad: str) -> None:
    with pytest.raises(ValidationError):
        _ = NewsRow(
            ticker="GOOG",
            headline="h",
            url="https://example.com/a",
            published_at=bad,
            source_feed="fmp_stock_news",
        )


# ---------------------------------------------------------------------------
# upsert_news_rows — (ticker, url) dedup / idempotency (§6.4)
# ---------------------------------------------------------------------------


def test_upsert_inserts_new_rows(conn: sqlite3.Connection) -> None:
    inserted, deduped = upsert_news_rows(
        conn, [_row("GOOG", "https://x/1"), _row("GOOG", "https://x/2")]
    )
    assert (inserted, deduped) == (2, 0)
    assert _count(conn) == 2


def test_upsert_idempotent_across_runs(conn: sqlite3.Connection) -> None:
    # Same (ticker, url) inserted in two separate runs -> exactly one row; the
    # re-run inserts nothing and reports it as deduped.
    first = upsert_news_rows(conn, [_row("GOOG", "https://x/1")])
    assert first == (1, 0)

    second = upsert_news_rows(conn, [_row("GOOG", "https://x/1")])
    assert second == (0, 1)

    assert _count(conn) == 1


def test_upsert_dedups_duplicates_within_one_batch(conn: sqlite3.Connection) -> None:
    # Two identical (ticker, url) rows in a single call -> one row stored,
    # inserted counts the winner, deduped counts the collision.
    inserted, deduped = upsert_news_rows(
        conn, [_row("GOOG", "https://x/dup"), _row("GOOG", "https://x/dup")]
    )
    assert (inserted, deduped) == (1, 1)
    assert _count(conn) == 1


def test_upsert_same_url_two_tickers_keeps_both(conn: sqlite3.Connection) -> None:
    # A syndicated URL under two tickers is two distinct rows: the dedup key is
    # (ticker, url), NOT url alone.
    inserted, deduped = upsert_news_rows(
        conn, [_row("GOOG", "https://x/shared"), _row("MSFT", "https://x/shared")]
    )
    assert (inserted, deduped) == (2, 0)
    assert _count(conn) == 2
