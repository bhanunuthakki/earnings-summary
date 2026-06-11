"""Tests for the canonical next-earnings store (0082) and its daily refresher.

Store tests run against a REAL alembic-migrated table (stamp at the prior
head, upgrade to head — only 0082 executes) so the upsert hits the actual
``ux_expected_earnings_ticker_date`` unique index. The refresher's source
stack (``next_earnings_date``) is monkeypatched so tests stay offline.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from expected_earnings import record_next_earnings, upcoming_by_ticker
from sources.earnings_calendar import NextEarnings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0081_discovery_candidates"
TODAY = date(2026, 6, 11)

sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import refresh_expected_earnings as ree  # noqa: E402


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "expected_earnings.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    c = sqlite3.connect(str(db))
    yield c
    c.close()


# ----------------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------------


def test_record_and_read_roundtrip(conn: sqlite3.Connection) -> None:
    record_next_earnings(conn, "nu", TODAY + timedelta(days=7), "fmp", today=TODAY)
    conn.commit()
    assert upcoming_by_ticker(conn, TODAY) == {"NU": TODAY + timedelta(days=7)}


def test_upsert_preserves_first_seen_and_refreshes_last_seen(conn: sqlite3.Connection) -> None:
    when = TODAY + timedelta(days=7)
    first = datetime(2026, 6, 10, 5, 45, tzinfo=UTC)
    later = datetime(2026, 6, 11, 5, 45, tzinfo=UTC)
    record_next_earnings(conn, "NU", when, "fmp", today=TODAY, now=first)
    record_next_earnings(conn, "NU", when, "yfinance", today=TODAY, now=later)
    conn.commit()
    rows = conn.execute(
        "SELECT detected_source, first_seen_at, last_seen_at FROM expected_earnings"
    ).fetchall()
    assert rows == [("yfinance", "2026-06-10 05:45:00", "2026-06-11 05:45:00")]


def test_reschedule_prunes_stale_future_rows_keeps_past(conn: sqlite3.Connection) -> None:
    """A moved date must not leave the old future row behind for MIN() readers;
    past rows are history and stay."""
    past = (TODAY - timedelta(days=80)).isoformat()
    conn.execute(
        "INSERT INTO expected_earnings (ticker, expected_date, detected_source) "
        "VALUES ('NU', ?, 'fmp')",
        (past,),
    )
    record_next_earnings(conn, "NU", TODAY + timedelta(days=10), "fmp", today=TODAY)
    record_next_earnings(conn, "NU", TODAY + timedelta(days=20), "yfinance", today=TODAY)
    conn.commit()
    dates = [r[0] for r in conn.execute("SELECT expected_date FROM expected_earnings ORDER BY 1")]
    assert dates == [past, (TODAY + timedelta(days=20)).isoformat()]
    assert upcoming_by_ticker(conn, TODAY) == {"NU": TODAY + timedelta(days=20)}


def test_upcoming_ignores_past_dates_and_picks_min(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO expected_earnings (ticker, expected_date, detected_source) "
        "VALUES (?, ?, 'fmp')",
        [
            ("NU", (TODAY - timedelta(days=1)).isoformat()),
            ("NU", (TODAY + timedelta(days=30)).isoformat()),
            ("NU", (TODAY + timedelta(days=9)).isoformat()),
            ("MELI", (TODAY - timedelta(days=2)).isoformat()),
        ],
    )
    conn.commit()
    assert upcoming_by_ticker(conn, TODAY) == {"NU": TODAY + timedelta(days=9)}


def test_upcoming_degrades_without_table() -> None:
    c = sqlite3.connect(":memory:")
    try:
        assert upcoming_by_ticker(c, TODAY) == {}
    finally:
        c.close()


# ----------------------------------------------------------------------------
# Refresher (execution/refresh_expected_earnings.py)
# ----------------------------------------------------------------------------


def test_refresh_tickers_writes_resolved_and_skips_misses(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stack = {
        "NU": NextEarnings(TODAY + timedelta(days=5), "fmp_cache", confirmed=True),
        "MELI": NextEarnings(TODAY + timedelta(days=12), "yfinance", confirmed=False),
    }
    monkeypatch.setattr(ree, "next_earnings_date", lambda _root, t: stack.get(t))

    summary = ree.refresh_tickers(tmp_path, ["NU", "MELI", "ZZ"], conn, today=TODAY)

    assert summary == {"tickers": 3, "written": 2, "no_source": 1, "errors": 0}
    rows = conn.execute(
        "SELECT ticker, expected_date, detected_source FROM expected_earnings ORDER BY ticker"
    ).fetchall()
    assert rows == [
        ("MELI", (TODAY + timedelta(days=12)).isoformat(), "yfinance"),
        ("NU", (TODAY + timedelta(days=5)).isoformat(), "fmp"),
    ]


def test_refresh_tickers_survives_source_exception(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _boom(_root: Path, ticker: str) -> NextEarnings | None:
        if ticker == "NU":
            raise RuntimeError("yfinance flake")
        return NextEarnings(TODAY + timedelta(days=3), "fmp_cache", confirmed=True)

    monkeypatch.setattr(ree, "next_earnings_date", _boom)

    summary = ree.refresh_tickers(tmp_path, ["NU", "V"], conn, today=TODAY)

    assert summary == {"tickers": 2, "written": 1, "no_source": 0, "errors": 1}
    assert upcoming_by_ticker(conn, TODAY) == {"V": TODAY + timedelta(days=3)}
