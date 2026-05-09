"""Unit tests for execution/onboard_pending_tickers.py.

Tests focus on the SQL `find_pending_tickers` selector — the rest of the
script is subprocess plumbing covered by integration tests in CI.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    """Import execution/onboard_pending_tickers.py without executing main()."""
    src = PROJECT_ROOT / "execution" / "onboard_pending_tickers.py"
    spec = importlib.util.spec_from_file_location("onboard_pending_tickers", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["onboard_pending_tickers"] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_minimal_schema(db_path: Path) -> None:
    """Create only the tables the selector touches; mirrors prod shape."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            instrument_type TEXT,
            UNIQUE(user_id, ticker)
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            period_end TEXT,
            value REAL
        );
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            valuation_date TEXT,
            npv REAL
        );
        """
    )
    conn.commit()
    conn.close()


def _add_ticker(
    db_path: Path,
    ticker: str,
    list_type: str,
    *,
    instrument_type: str | None,
    facts: int = 0,
    dcf: int = 0,
    added_at: str = "2026-05-01",
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type, added_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticker, f"{ticker} Corp", list_type, instrument_type, added_at),
    )
    for i in range(facts):
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, value) VALUES (?, ?, ?)",
            (ticker, "2025-12-31", float(i)),
        )
    for i in range(dcf):
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv) VALUES (?, ?, ?)",
            (ticker, "2025-12-31", float(i)),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    _seed_minimal_schema(p)
    return p


def test_no_pending_when_universe_empty(db: Path) -> None:
    mod = _load_module()
    assert mod.find_pending_tickers(db) == []


def test_fully_onboarded_ticker_is_not_pending(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "GOOG", "portfolio", instrument_type="equity", facts=10, dcf=1)
    assert mod.find_pending_tickers(db) == []


def test_missing_instrument_type_flags_pending(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "MSFT", "watchlist", instrument_type=None, facts=10, dcf=1)
    pending = mod.find_pending_tickers(db)
    assert pending == [("MSFT", "no_instrument_type")]


def test_zero_facts_flags_pending(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "AMD", "watchlist", instrument_type="equity", facts=0, dcf=0)
    pending = mod.find_pending_tickers(db)
    # Reason precedence: no_instrument_type beats no_financial_facts.
    # Here instrument_type is set, so no_financial_facts wins.
    assert pending == [("AMD", "no_financial_facts")]


def test_zero_dcf_flags_pending_when_facts_present(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "BKNG", "watchlist", instrument_type="equity", facts=5, dcf=0)
    pending = mod.find_pending_tickers(db)
    assert pending == [("BKNG", "no_dcf_run")]


def test_excludes_index_member_and_none_rows(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "AAPL", "index_member", instrument_type=None, facts=0, dcf=0)
    _add_ticker(db, "WPM", "none", instrument_type=None, facts=0, dcf=0)
    _add_ticker(db, "MU", "watchlist", instrument_type="equity", facts=10, dcf=1)
    assert mod.find_pending_tickers(db) == []


def test_orders_by_added_at_then_ticker(db: Path) -> None:
    mod = _load_module()
    _add_ticker(db, "ZZZ", "watchlist", instrument_type=None, added_at="2026-05-01")
    _add_ticker(db, "AAA", "watchlist", instrument_type=None, added_at="2026-05-02")
    _add_ticker(db, "MMM", "watchlist", instrument_type=None, added_at="2026-05-01")
    pending = mod.find_pending_tickers(db)
    assert [t for t, _ in pending] == ["MMM", "ZZZ", "AAA"]


def test_reason_precedence(db: Path) -> None:
    """instrument_type wins over facts wins over dcf — matches CASE WHEN ladder."""
    mod = _load_module()
    _add_ticker(db, "T1", "watchlist", instrument_type=None, facts=0, dcf=0)
    _add_ticker(db, "T2", "watchlist", instrument_type="equity", facts=0, dcf=0)
    _add_ticker(db, "T3", "watchlist", instrument_type="equity", facts=5, dcf=0)
    pending = dict(mod.find_pending_tickers(db))
    assert pending["T1"] == "no_instrument_type"
    assert pending["T2"] == "no_financial_facts"
    assert pending["T3"] == "no_dcf_run"
