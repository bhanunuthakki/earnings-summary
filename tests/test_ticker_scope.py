"""Tests for shared tracked-company capability resolution and callers."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from execution import fetch_news, refresh_expected_earnings
from models.companies import ListType
from news.store import NewsRow
from pipeline.ticker_scope import (
    TickerCapability,
    load_active_capabilities,
    partition_by_capability,
)


def _seed_tracked_db(db_path: Path, *, include_optional_cols: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    if include_optional_cols:
        conn.execute(
            """
            CREATE TABLE tracked_companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER DEFAULT 1,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                list_type TEXT NOT NULL,
                instrument_type TEXT,
                filing_regime TEXT,
                fiscal_year_end TEXT,
                archived_at TIMESTAMP
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO tracked_companies
                (ticker, name, list_type, instrument_type, filing_regime, fiscal_year_end, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("NU", "Nu Holdings", "portfolio", "adr", "20-F", "12-31", None),
                ("META", "Meta Platforms", "portfolio", "equity", "10-K", "12-31", None),
                ("AVDV", "Avantis Intl SCV", "portfolio", "etf", "10-K", None, None),
                ("1548.HK", "GenScript Biotech", "evaluation", "equity", None, None, None),
                ("LVMUY", "LVMH ADR", "evaluation", "adr", None, None, None),
                ("RSP", "Equal Weight ETF", "evaluation", "etf", "10-K", None, None),
                ("NOW", "ServiceNow", "watchlist", "equity", "10-K", "12-31", None),
                ("IDX", "Index Component", "index_member", "equity", "10-K", "12-31", None),
                ("OLD", "Archived Co", "portfolio", "equity", "10-K", "12-31", "2026-01-01"),
            ],
        )
    else:
        conn.execute(
            """
            CREATE TABLE tracked_companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                list_type TEXT NOT NULL,
                archived_at TIMESTAMP
            )
            """
        )
        conn.executemany(
            "INSERT INTO tracked_companies (ticker, name, list_type, archived_at) VALUES (?, ?, ?, ?)",
            [
                ("META", "Meta", "portfolio", None),
                ("NOW", "ServiceNow", "watchlist", None),
                ("OLD", "Archived", "portfolio", "2026-01-01"),
            ],
        )
    conn.commit()
    return conn


def test_capability_flags_and_priority_order(tmp_path: Path) -> None:
    conn = _seed_tracked_db(tmp_path / "caps.db")
    try:
        caps = load_active_capabilities(conn)
    finally:
        conn.close()

    assert [c.ticker for c in caps] == ["AVDV", "META", "NU", "1548.HK", "LVMUY", "RSP", "NOW"]
    by_ticker = {c.ticker: c for c in caps}

    assert by_ticker["META"].is_portfolio_holding is True
    assert by_ticker["META"].supports_sec_xbrl is True
    assert by_ticker["META"].supports_earnings_calendar is True

    assert by_ticker["NU"].supports_sec_xbrl is True
    assert by_ticker["NU"].supports_ir_documents is True

    assert by_ticker["AVDV"].is_portfolio_holding is True
    assert by_ticker["AVDV"].is_corporate_instrument is False
    assert by_ticker["AVDV"].supports_sec_xbrl is False
    assert by_ticker["AVDV"].supports_earnings_calendar is False
    assert by_ticker["AVDV"].supports_earnings_surprises is False
    assert by_ticker["AVDV"].supports_ir_documents is False
    assert by_ticker["AVDV"].supports_corporate_grades is False

    assert by_ticker["1548.HK"].is_corporate_instrument is True
    assert by_ticker["1548.HK"].supports_sec_xbrl is False
    assert by_ticker["1548.HK"].supports_earnings_calendar is True
    assert by_ticker["LVMUY"].supports_sec_xbrl is False
    assert by_ticker["LVMUY"].supports_corporate_grades is True


def test_minimal_schema_without_instrument_type_allows_corporate_defaults(tmp_path: Path) -> None:
    conn = _seed_tracked_db(tmp_path / "min.db", include_optional_cols=False)
    try:
        caps = load_active_capabilities(conn)
        missing = load_active_capabilities(sqlite3.connect(":memory:"))
    finally:
        conn.close()

    assert [c.ticker for c in caps] == ["META", "NOW"]
    assert all(c.is_corporate_instrument for c in caps)
    assert missing == []


def test_partition_by_capability_and_explicit_lookup(tmp_path: Path) -> None:
    conn = _seed_tracked_db(tmp_path / "part.db")
    try:
        caps = load_active_capabilities(conn)
        idx_caps = load_active_capabilities(conn, ticker="idx")
    finally:
        conn.close()

    eligible, skipped = partition_by_capability(
        caps,
        predicate=lambda cap: cap.supports_earnings_calendar,
        skip_reason="ineligible_instrument_type",
    )
    assert [c.ticker for c in eligible] == ["META", "NU", "1548.HK", "LVMUY", "NOW"]
    assert skipped == [
        {
            "ticker": "AVDV",
            "list_type": "portfolio",
            "instrument_type": "etf",
            "reason": "ineligible_instrument_type",
        },
        {
            "ticker": "RSP",
            "list_type": "evaluation",
            "instrument_type": "etf",
            "reason": "ineligible_instrument_type",
        },
    ]
    assert [c.ticker for c in idx_caps] == ["IDX"]
    assert isinstance(TickerCapability("X", ListType.NONE, None, None, None), TickerCapability)


def test_refresh_expected_earnings_skips_etfs_and_resolves_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sources.earnings_calendar import NextEarnings

    state_root = tmp_path / "scratch" / "earnings-summary"
    (state_root / "data").mkdir(parents=True)
    db_path = state_root / "data" / "portfolio.db"
    conn = _seed_tracked_db(db_path)
    conn.close()

    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db_path))

    seen: list[tuple[Path, str]] = []

    def fake_next(root: Path, ticker: str) -> NextEarnings | None:
        seen.append((root, ticker))
        return NextEarnings(date(2026, 12, 1) + timedelta(days=1), "yfinance", confirmed=True)

    def fake_record(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(refresh_expected_earnings.db, "DB_PATH", str(db_path))
    monkeypatch.setattr(refresh_expected_earnings, "next_earnings_date", fake_next)
    monkeypatch.setattr(refresh_expected_earnings, "record_next_earnings", fake_record)
    monkeypatch.setattr(sys, "argv", ["refresh_expected_earnings.py"])

    assert refresh_expected_earnings.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["skipped_ineligible"] == 2
    assert out["written"] == 5
    assert [ticker for _, ticker in seen] == ["1548.HK", "LVMUY", "META", "NOW", "NU"]
    assert all(root == state_root.resolve() for root, _ in seen)


def test_fetch_news_additive_filters_skip_etfs_and_non_sec_edgar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "news.db"
    conn = _seed_tracked_db(db_path)
    conn.execute(
        """
        CREATE TABLE news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            published_at TEXT NOT NULL,
            headline TEXT NOT NULL,
            url TEXT NOT NULL,
            source TEXT,
            publisher TEXT,
            snippet TEXT,
            fetched_at TEXT NOT NULL,
            UNIQUE(ticker, url)
        )
        """
    )
    conn.commit()
    conn.close()

    edgar_calls: list[str] = []
    grades_calls: list[str] = []

    def empty_primary(*_args: object, **_kwargs: object) -> list[NewsRow]:
        return []

    def noop_diet(*_args: object, **_kwargs: object) -> None:
        return None

    def record_edgar(ticker: str, **_kwargs: object) -> list[NewsRow]:
        edgar_calls.append(ticker)
        return []

    def record_grades(ticker: str, **_kwargs: object) -> list[NewsRow]:
        grades_calls.append(ticker)
        return []

    monkeypatch.setattr(fetch_news, "collect_primary", empty_primary)
    monkeypatch.setattr(fetch_news, "_score_diet_quality", noop_diet)
    monkeypatch.setattr(fetch_news.edgarnews, "fetch_edgar_news_for_ticker", record_edgar)
    monkeypatch.setattr(fetch_news.yfgrades, "fetch_grades_for_ticker", record_grades)

    rc = fetch_news.run(
        ["META", "AVDV", "1548.HK", "LVMUY"],
        source="auto",
        db_path=str(db_path),
        days=2,
        limit=5,
        skip_yf_news=True,
        skip_s1_watch=True,
        websearch_scope="none",
    )
    assert rc == 0
    assert edgar_calls == ["META"]
    assert sorted(grades_calls) == ["1548.HK", "LVMUY", "META"]
