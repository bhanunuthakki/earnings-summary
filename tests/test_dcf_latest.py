"""Tests for src/dcf/latest.py — the canonical "latest DCF run" reader.

Hand-rolled minimal schema (id/ticker/created_at/is_latest/segment_name/
valuation_date/npv_per_share/live_price/live_price_at/over_under_pct/
sanity_flag/assumption_snapshot_json), the versioned (migration 0137+/0182)
shape. A second, column-poor schema exercises the presence-probed degrade
path (mirrors ``model_provenance.basis``'s pre-0137/pre-sanity fixture).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from dcf.latest import (
    latest_dcf_row,
    latest_dcf_row_from_db,
    latest_dcf_rows,
    latest_dcf_rows_from_db,
)

TICKER = "NU"


def _full_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            created_at TEXT,
            is_latest INTEGER,
            segment_name TEXT,
            valuation_date TEXT,
            npv_per_share REAL,
            live_price REAL,
            live_price_at TEXT,
            over_under_pct REAL,
            sanity_flag TEXT,
            assumption_snapshot_json TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert(
    db_path: Path,
    *,
    ticker: str = TICKER,
    created_at: str,
    is_latest: int = 1,
    segment_name: str | None = None,
    valuation_date: str = "2026-07-15",
    npv_per_share: float | None = 120.0,
    live_price: float | None = 100.0,
    live_price_at: str | None = "2026-07-15T00:00:00",
    over_under_pct: float | None = -0.1667,
    sanity_flag: str | None = None,
    assumption_snapshot_json: str | None = None,
) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO dcf_runs (ticker, created_at, is_latest, segment_name, valuation_date, "
        "npv_per_share, live_price, live_price_at, over_under_pct, sanity_flag, "
        "assumption_snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticker,
            created_at,
            is_latest,
            segment_name,
            valuation_date,
            npv_per_share,
            live_price,
            live_price_at,
            over_under_pct,
            sanity_flag,
            assumption_snapshot_json,
        ),
    )
    conn.commit()
    row_id = int(cur.lastrowid or 0)
    conn.close()
    return row_id


def test_segment_row_never_wins_even_with_newer_created_at(tmp_path: Path) -> None:
    """The exact bug PART A fixes: a segment row (segment_name set) landed
    AFTER the consolidated row must not win 'latest' just by created_at."""
    db_path = _full_db(tmp_path)
    _insert(db_path, created_at="2026-07-15T00:00:00", npv_per_share=120.0)
    _insert(
        db_path,
        created_at="2026-07-16T00:00:00",  # newer than the consolidated row
        segment_name="Consumer",
        npv_per_share=999.0,
    )
    conn = sqlite3.connect(str(db_path))
    try:
        row = latest_dcf_row(conn, TICKER)
    finally:
        conn.close()
    assert row is not None
    assert row.npv_per_share == 120.0  # the consolidated row, not the segment one


def test_superseded_row_never_wins_even_with_newer_created_at(tmp_path: Path) -> None:
    """A superseded row (is_latest=0) landed after the current row must not
    win 'latest' just by created_at — the migration-0137 versioning bug."""
    db_path = _full_db(tmp_path)
    _insert(db_path, created_at="2026-07-15T00:00:00", npv_per_share=120.0, is_latest=1)
    _insert(
        db_path,
        created_at="2026-07-16T00:00:00",
        npv_per_share=888.0,
        is_latest=0,  # a superseded/backfilled row that happens to sort newer
    )
    conn = sqlite3.connect(str(db_path))
    try:
        row = latest_dcf_row(conn, TICKER)
    finally:
        conn.close()
    assert row is not None
    assert row.npv_per_share == 120.0


def test_sanity_flag_rides_along_unfiltered(tmp_path: Path) -> None:
    """The reader itself never drops a sanity-flagged row — exclusion policy
    is each caller's decision, not this module's."""
    db_path = _full_db(tmp_path)
    _insert(db_path, created_at="2026-07-15T00:00:00", sanity_flag="outlier")
    conn = sqlite3.connect(str(db_path))
    try:
        row = latest_dcf_row(conn, TICKER)
    finally:
        conn.close()
    assert row is not None
    assert row.sanity_flag == "outlier"


def test_latest_dcf_rows_all_tickers_filters_segments(tmp_path: Path) -> None:
    db_path = _full_db(tmp_path)
    _insert(db_path, ticker="NU", created_at="2026-07-15T00:00:00", npv_per_share=120.0)
    _insert(
        db_path,
        ticker="NU",
        created_at="2026-07-16T00:00:00",
        segment_name="Consumer",
        npv_per_share=999.0,
    )
    _insert(db_path, ticker="V", created_at="2026-07-10T00:00:00", npv_per_share=300.0)
    conn = sqlite3.connect(str(db_path))
    try:
        rows = latest_dcf_rows(conn)
    finally:
        conn.close()
    assert set(rows) == {"NU", "V"}
    assert rows["NU"].npv_per_share == 120.0
    assert rows["V"].npv_per_share == 300.0


def test_missing_table_degrades_to_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()
    assert latest_dcf_rows_from_db(db_path) == {}
    assert latest_dcf_row_from_db(db_path, TICKER) is None


def test_missing_file_degrades_to_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert latest_dcf_rows_from_db(missing) == {}
    assert latest_dcf_row_from_db(missing, TICKER) is None


def test_ticker_not_found_returns_none(tmp_path: Path) -> None:
    db_path = _full_db(tmp_path)
    _insert(db_path, ticker="NU", created_at="2026-07-15T00:00:00")
    conn = sqlite3.connect(str(db_path))
    try:
        assert latest_dcf_row(conn, "ZZZZ") is None
    finally:
        conn.close()


def test_column_presence_probing_degrades_gracefully(tmp_path: Path) -> None:
    """A pre-0137 / pre-sanity schema (no is_latest, segment_name, sanity_flag,
    live_price, live_price_at, over_under_pct, assumption_snapshot_json —
    only the columns model_provenance.basis's own pre-capture fixture ever
    needed) still reads: every row counts as latest+unsegmented, and the
    absent optional columns read None."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR NOT NULL,
            valuation_date VARCHAR(10),
            npv_per_share NUMERIC,
            created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP)
        );
        """
    )
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, created_at) "
        "VALUES ('RBRK', '2026-07-02', 66.45, '2026-07-02 00:00:00')"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(str(db_path))
    try:
        row = latest_dcf_row(conn, "rbrk")
    finally:
        conn.close()
    assert row is not None
    assert row.npv_per_share == 66.45
    assert row.live_price is None
    assert row.sanity_flag is None
    assert row.over_under_pct is None
