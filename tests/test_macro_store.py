"""Tests for src/macro_store.py and migration 0045 (macro_series).

Covers the write/read surface, idempotency, lookback windowing, and
graceful degradation when the DB is missing or the table isn't present.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from macro_store import (
    fetch_series,
    latest_series_value,
    series_row_count,
    upsert_series_value,
)


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE macro_series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id VARCHAR(48) NOT NULL,
            rate_date DATE NOT NULL,
            value NUMERIC(20,8) NOT NULL,
            source VARCHAR(32) NOT NULL DEFAULT 'FMP',
            created_at DATETIME NOT NULL,
            UNIQUE(series_id, rate_date)
        );
        CREATE TABLE macro_sensitivities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            series_id VARCHAR(48) NOT NULL,
            beta FLOAT NOT NULL,
            r_squared FLOAT,
            lookback_window_days INTEGER NOT NULL,
            computed_at DATETIME NOT NULL,
            UNIQUE(ticker, series_id, lookback_window_days)
        );
        """
    )
    conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "macro.db"
    conn = sqlite3.connect(str(p))
    try:
        _schema(conn)
    finally:
        conn.close()
    return p


def test_upsert_series_inserts_first_time(db: Path) -> None:
    row_id = upsert_series_value(
        series_id="fed_funds",
        rate_date=date(2026, 5, 1),
        value=5.25,
        source="FMP",
        db_path=db,
    )
    assert row_id is not None and row_id > 0


def test_upsert_series_idempotent_overwrites(db: Path) -> None:
    rid_1 = upsert_series_value(
        series_id="fed_funds",
        rate_date=date(2026, 5, 1),
        value=5.25,
        source="FMP",
        db_path=db,
    )
    rid_2 = upsert_series_value(
        series_id="fed_funds",
        rate_date=date(2026, 5, 1),
        value=5.30,  # value changed; row should update in place
        source="FRED",
        db_path=db,
    )
    assert rid_1 == rid_2  # same primary key (in-place update)

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT value, source FROM macro_series WHERE id = ?", (rid_2,)
        ).fetchone()
        assert float(row[0]) == pytest.approx(5.30)
        assert row[1] == "FRED"
    finally:
        conn.close()


def test_upsert_separate_dates_get_separate_rows(db: Path) -> None:
    a = upsert_series_value(series_id="us_10y", rate_date=date(2026, 5, 1), value=4.2, db_path=db)
    b = upsert_series_value(series_id="us_10y", rate_date=date(2026, 5, 2), value=4.21, db_path=db)
    assert a is not None and b is not None and a != b
    assert series_row_count(series_id="us_10y", db_path=db) == 2


def test_fetch_series_returns_newest_first(db: Path) -> None:
    upsert_series_value(series_id="vix", rate_date=date(2026, 1, 5), value=15.5, db_path=db)
    upsert_series_value(series_id="vix", rate_date=date(2026, 3, 5), value=18.0, db_path=db)
    upsert_series_value(series_id="vix", rate_date=date(2026, 2, 5), value=16.0, db_path=db)
    pts = fetch_series(series_id="vix", db_path=db)
    assert [p.rate_date for p in pts] == [
        date(2026, 3, 5),
        date(2026, 2, 5),
        date(2026, 1, 5),
    ]
    assert all(isinstance(p.value, float) for p in pts)


def test_fetch_series_lookback_filters_old_rows(db: Path) -> None:
    today = date.today()
    upsert_series_value(
        series_id="brent", rate_date=today - timedelta(days=400), value=70.0, db_path=db
    )
    upsert_series_value(
        series_id="brent", rate_date=today - timedelta(days=30), value=82.0, db_path=db
    )
    upsert_series_value(
        series_id="brent", rate_date=today - timedelta(days=10), value=85.0, db_path=db
    )
    recent = fetch_series(series_id="brent", lookback_days=90, db_path=db)
    assert len(recent) == 2
    assert all(p.rate_date >= today - timedelta(days=90) for p in recent)


def test_latest_series_value_returns_newest(db: Path) -> None:
    upsert_series_value(series_id="gold", rate_date=date(2026, 4, 1), value=2400.0, db_path=db)
    upsert_series_value(series_id="gold", rate_date=date(2026, 5, 1), value=2455.0, db_path=db)
    latest = latest_series_value(series_id="gold", db_path=db)
    assert latest is not None
    assert latest.rate_date == date(2026, 5, 1)
    assert latest.value == pytest.approx(2455.0)


def test_upsert_returns_none_when_db_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no.db"
    row_id = upsert_series_value(
        series_id="x", rate_date=date(2026, 1, 1), value=1.0, db_path=missing
    )
    assert row_id is None


def test_fetch_returns_empty_when_table_missing(tmp_path: Path) -> None:
    p = tmp_path / "empty.db"
    sqlite3.connect(str(p)).close()  # create empty DB without the table
    assert fetch_series(series_id="x", db_path=p) == []
    assert series_row_count(series_id="x", db_path=p) == 0


def test_upsert_sensitivity_idempotent_and_distinct_lookbacks() -> None:
    """Sensitivities table: same (ticker, series, lookback) overwrites; different lookback creates new row."""
    import tempfile

    from macro_store import fetch_sensitivities, upsert_sensitivity

    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir) / "sens.db"
        conn = sqlite3.connect(str(p))
        try:
            _schema(conn)
        finally:
            conn.close()
        a = upsert_sensitivity(
            ticker="AMZN",
            series_id="us_10y",
            beta=-0.45,
            r_squared=0.32,
            lookback_window_days=252,
            db_path=p,
        )
        b = upsert_sensitivity(
            ticker="AMZN",
            series_id="us_10y",
            beta=-0.50,
            r_squared=0.35,
            lookback_window_days=252,
            db_path=p,
        )
        c = upsert_sensitivity(
            ticker="AMZN",
            series_id="us_10y",
            beta=-0.30,
            r_squared=0.22,
            lookback_window_days=756,
            db_path=p,
        )
        assert a == b  # same lookback → overwrite
        assert a != c  # different lookback → new row
        rows = fetch_sensitivities(ticker="AMZN", db_path=p)
        # Should see two rows: one 252-day, one 756-day; the 252 row carries
        # the *updated* beta (-0.50), not the original (-0.45).
        assert len(rows) == 2
        by_lookback = {r.lookback_window_days: r for r in rows}
        assert by_lookback[252].beta == pytest.approx(-0.50)
        assert by_lookback[756].beta == pytest.approx(-0.30)
        # computed_at must be a datetime, not a string.
        assert isinstance(by_lookback[252].computed_at, datetime)


def test_created_at_is_recent(db: Path) -> None:
    """Smoke: created_at stamp lands within the last few seconds."""
    upsert_series_value(series_id="usd_brl", rate_date=date(2026, 5, 1), value=5.10, db_path=db)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT created_at FROM macro_series WHERE series_id='usd_brl' LIMIT 1"
        ).fetchone()
        ts = datetime.fromisoformat(row[0])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        delta = abs((datetime.now(UTC) - ts).total_seconds())
        assert delta < 10
    finally:
        conn.close()
