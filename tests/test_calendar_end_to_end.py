"""End-to-end test suite for earnings and general research calendars (BHA-8)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from calendar_clock import calendar_today
from dashboard.upcoming import render_upcoming_strip, upcoming_earnings
from execution.verify_calendars import audit_calendars
from expected_earnings import (
    last_reported_by_ticker,
    record_next_earnings,
    upcoming_by_ticker,
)


def test_calendar_today_honors_pacific_timezone_boundary() -> None:
    # 2026-08-15 03:00:00 UTC is 2026-08-14 20:00:00 Pacific (PDT: UTC-7)
    late_night_utc = datetime(2026, 8, 15, 3, 0, 0, tzinfo=UTC)
    assert calendar_today(late_night_utc) == date(2026, 8, 14)

    # 2026-08-15 08:00:00 UTC is 2026-08-15 01:00:00 Pacific (PDT: UTC-7)
    early_morning_utc = datetime(2026, 8, 15, 8, 0, 0, tzinfo=UTC)
    assert calendar_today(early_morning_utc) == date(2026, 8, 15)


def test_expected_earnings_rescheduling_and_deduplication(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE expected_earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                expected_date VARCHAR(10) NOT NULL,
                fiscal_period_end VARCHAR(10),
                fiscal_period_label VARCHAR(16),
                detected_source VARCHAR(16) NOT NULL,
                first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                fact_seen_at DATETIME
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX ux_expected_earnings_ticker_date ON expected_earnings (ticker, expected_date)"
        )

        today = date(2026, 8, 14)

        # 1. Record past date (2026-05-20)
        record_next_earnings(conn, "SNOW", date(2026, 5, 20), "fmp", today=today)
        # 2. Record initial future date (2026-08-26)
        record_next_earnings(conn, "SNOW", date(2026, 8, 26), "fmp", today=today)
        conn.commit()

        assert upcoming_by_ticker(conn, today) == {"SNOW": date(2026, 8, 26)}
        assert last_reported_by_ticker(conn, today) == {"SNOW": date(2026, 5, 20)}

        # 3. Company reschedules future date to 2026-08-28 -> old future date (08-26) is pruned
        record_next_earnings(conn, "SNOW", date(2026, 8, 28), "yfinance", today=today)
        conn.commit()

        upcoming = upcoming_by_ticker(conn, today)
        assert upcoming == {"SNOW": date(2026, 8, 28)}
        # Past date history remains untouched
        assert last_reported_by_ticker(conn, today) == {"SNOW": date(2026, 5, 20)}


def test_upcoming_strip_renders_correctly_with_calendar_and_estimates(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, archived_at TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE expected_earnings (ticker TEXT, expected_date TEXT, detected_source TEXT, first_seen_at TEXT, last_seen_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE earnings_surprises (ticker TEXT, period_end TEXT, release_date TEXT)"
        )
        conn.execute(
            """
            CREATE TABLE analyst_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                ticker TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                body TEXT NOT NULL,
                anchor_type TEXT,
                anchor_key TEXT,
                fact_ref TEXT,
                source TEXT NOT NULL,
                source_ref TEXT,
                supersedes_id INTEGER,
                resolution_note TEXT,
                context_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                decision_id INTEGER,
                position_entry_id INTEGER,
                link_auto_resolve BOOLEAN DEFAULT 0
            )
            """
        )

        today = date(2026, 8, 14)
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?, ?, NULL)",
            [
                ("NVDA", "Nvidia", "portfolio"),
                ("MDB", "MongoDB", "portfolio"),
                ("SNOW", "Snowflake", "evaluation"),
            ],
        )
        # NVDA has confirmed date in expected_earnings within 14d horizon
        conn.execute(
            "INSERT INTO expected_earnings VALUES ('NVDA', '2026-08-20', 'fmp', '2026-08-01', '2026-08-01')"
        )
        # MDB has no expected_earnings row, but past surprise release 90d ago -> estimated date
        conn.execute(
            "INSERT INTO earnings_surprises VALUES ('MDB', '2026-04-30', '2026-05-20')"
        )
        # Add an open watch note for NVDA
        conn.execute(
            """
            INSERT INTO analyst_notes
                (user_id, ticker, kind, status, body, source, created_at, updated_at)
            VALUES ('bhanu', 'NVDA', 'watch', 'open', 'Blackwell ramp margins', 'manual', '2026-08-10', '2026-08-10')
            """
        )
        conn.commit()

    # Query upcoming earnings
    upcoming = upcoming_earnings(db, today, horizon_days=14)
    tickers = [t for t, _, _ in upcoming]
    assert "NVDA" in tickers
    assert "MDB" in tickers

    # Render HTML strip
    html = render_upcoming_strip(db, today, horizon_days=14)
    assert 'class="up-strip' in html
    assert "NVDA" in html
    assert "2026-08-20" in html
    assert "Blackwell ramp margins" in html
    assert "MDB" in html
    assert "est." in html


def test_verify_calendars_audit_cli_integration(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, archived_at TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE expected_earnings (ticker TEXT, expected_date TEXT, detected_source TEXT, first_seen_at TEXT, last_seen_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE earnings_surprises (ticker TEXT, period_end TEXT, release_date TEXT)"
        )
        conn.execute(
            "CREATE TABLE analyst_notes (user_id TEXT, ticker TEXT, kind TEXT, body TEXT, status TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.execute("INSERT INTO tracked_companies VALUES ('AAPL', 'Apple', 'portfolio', NULL)")
        conn.execute("INSERT INTO expected_earnings VALUES ('AAPL', '2026-08-18', 'fmp', '2026-08-01', '2026-08-01')")
        conn.commit()

    today = date(2026, 8, 14)
    res = audit_calendars(db, today=today)
    assert res.integrity_pass is True
    assert res.tracked_companies_count == 1
    assert res.upcoming_expected_count == 1
    assert len(res.issues) == 0
