"""Unit tests for execution/onboard_pending_tickers.py::apply_ipo_backoff.

Covers the Issue-1 fix: recently-IPO'd tickers (near-zero FMP coverage until
their first 10-Q is ingested) must NOT re-run the full onboard hourly, but the
"auto-onboard the moment data arrives" path must stay intact — so they're
deferred to a daily cadence, never hard-skipped.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Fixed reference instant so the 24h window is deterministic.
_NOW = datetime(2026, 5, 28, 12, 0, 0)


def _load_module():
    src = PROJECT_ROOT / "execution" / "onboard_pending_tickers.py"
    spec = importlib.util.spec_from_file_location("onboard_pending_tickers", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["onboard_pending_tickers"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE fmp_endpoint_status (
            ticker      TEXT NOT NULL,
            endpoint    TEXT NOT NULL,
            period      TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL,
            last_pulled TIMESTAMP,
            PRIMARY KEY (ticker, endpoint, period)
        )
        """
    )
    conn.commit()
    conn.close()


def _set_last_pulled(db_path: Path, ticker: str, when: datetime) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO fmp_endpoint_status (ticker, endpoint, period, status, last_pulled) "
        "VALUES (?, 'income-statement', 'quarter', 'empty', ?)",
        (ticker, when.isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def _write_holdings(holdings_dir: Path, ticker: str, *, recently_ipod: bool) -> None:
    holdings_dir.mkdir(parents=True, exist_ok=True)
    (holdings_dir / f"{ticker}.json").write_text(
        json.dumps({"ticker": ticker, "recently_ipod": recently_ipod}),
        encoding="utf-8",
    )


@pytest.fixture()
def env(tmp_path: Path):
    db = tmp_path / "portfolio.db"
    _make_db(db)
    holdings = tmp_path / "holdings"
    holdings.mkdir(parents=True, exist_ok=True)
    return db, holdings


def test_recently_ipod_within_window_is_deferred(env) -> None:
    db, holdings = env
    mod = _load_module()
    _write_holdings(holdings, "FRVO", recently_ipod=True)
    _set_last_pulled(db, "FRVO", _NOW - timedelta(hours=1))

    to_process, deferred = mod.apply_ipo_backoff(
        [("FRVO", "no_financial_facts")], db, holdings, now=_NOW
    )
    assert to_process == []
    assert deferred == [("FRVO", "no_financial_facts")]


def test_recently_ipod_stale_is_processed(env) -> None:
    """Last fetch > 24h ago -> daily re-check fires (picks up data if it landed)."""
    db, holdings = env
    mod = _load_module()
    _write_holdings(holdings, "FRVO", recently_ipod=True)
    _set_last_pulled(db, "FRVO", _NOW - timedelta(hours=30))

    to_process, deferred = mod.apply_ipo_backoff(
        [("FRVO", "no_financial_facts")], db, holdings, now=_NOW
    )
    assert to_process == [("FRVO", "no_financial_facts")]
    assert deferred == []


def test_recently_ipod_never_fetched_is_processed(env) -> None:
    """No fmp_endpoint_status rows -> first onboard must run (not deferred)."""
    db, holdings = env
    mod = _load_module()
    _write_holdings(holdings, "FRVO", recently_ipod=True)
    # Intentionally no _set_last_pulled.

    to_process, deferred = mod.apply_ipo_backoff(
        [("FRVO", "no_financial_facts")], db, holdings, now=_NOW
    )
    assert to_process == [("FRVO", "no_financial_facts")]
    assert deferred == []


def test_non_ipo_ticker_keeps_hourly_cadence(env) -> None:
    """A normal ticker with a recent fetch is NOT deferred."""
    db, holdings = env
    mod = _load_module()
    _write_holdings(holdings, "MSFT", recently_ipod=False)
    _set_last_pulled(db, "MSFT", _NOW - timedelta(hours=1))

    to_process, deferred = mod.apply_ipo_backoff(
        [("MSFT", "no_financial_facts")], db, holdings, now=_NOW
    )
    assert to_process == [("MSFT", "no_financial_facts")]
    assert deferred == []


def test_missing_holdings_json_not_deferred(env) -> None:
    db, holdings = env
    mod = _load_module()
    _set_last_pulled(db, "XYZ", _NOW - timedelta(hours=1))  # recent, but no holdings flag

    to_process, deferred = mod.apply_ipo_backoff(
        [("XYZ", "no_financial_facts")], db, holdings, now=_NOW
    )
    assert to_process == [("XYZ", "no_financial_facts")]
    assert deferred == []


def test_no_commitments_reason_never_deferred(env) -> None:
    """The commitment-only stage is cheap + FMP-free, so it bypasses the backoff
    even for a recently-IPO'd ticker fetched moments ago."""
    db, holdings = env
    mod = _load_module()
    _write_holdings(holdings, "FRVO", recently_ipod=True)
    _set_last_pulled(db, "FRVO", _NOW - timedelta(hours=1))

    to_process, deferred = mod.apply_ipo_backoff(
        [("FRVO", "no_commitments")], db, holdings, now=_NOW
    )
    assert to_process == [("FRVO", "no_commitments")]
    assert deferred == []


def test_mixed_batch_partitions_correctly(env) -> None:
    db, holdings = env
    mod = _load_module()
    _write_holdings(holdings, "FRVO", recently_ipod=True)
    _set_last_pulled(db, "FRVO", _NOW - timedelta(hours=2))  # deferred
    _write_holdings(holdings, "NSP", recently_ipod=False)
    _set_last_pulled(db, "NSP", _NOW - timedelta(hours=2))  # normal -> processed

    pending = [("FRVO", "no_financial_facts"), ("NSP", "no_dcf_run")]
    to_process, deferred = mod.apply_ipo_backoff(pending, db, holdings, now=_NOW)

    assert to_process == [("NSP", "no_dcf_run")]
    assert deferred == [("FRVO", "no_financial_facts")]


def test_helpers_directly(env) -> None:
    db, holdings = env
    mod = _load_module()
    _write_holdings(holdings, "FRVO", recently_ipod=True)
    _write_holdings(holdings, "MSFT", recently_ipod=False)

    assert mod._is_recently_ipod("FRVO", holdings) is True
    assert mod._is_recently_ipod("MSFT", holdings) is False
    assert mod._is_recently_ipod("NOFILE", holdings) is False

    conn = mod.open_db(db)
    try:
        assert mod._last_fmp_attempt(conn, "NEVER") is None
        _set_last_pulled(db, "FRVO", _NOW - timedelta(hours=3))
    finally:
        conn.close()
    conn = mod.open_db(db)
    try:
        last = mod._last_fmp_attempt(conn, "FRVO")
    finally:
        conn.close()
    assert last == _NOW - timedelta(hours=3)
