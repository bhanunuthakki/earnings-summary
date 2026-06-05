"""Unit tests for sources.registry — the source-call provenance log.

Verifies:
  - log_call inserts a row with all fields populated
  - log_call swallows DB errors silently (never raises into the fetch path)
  - CallStatus enum values are stable (queried by analytics)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sources.registry import CallStatus, log_call, set_db_path  # noqa: E402


def _make_table(db_path: Path) -> None:
    """Create just the source_calls table — no need for the full schema."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE source_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name VARCHAR(32) NOT NULL,
            kind VARCHAR(64) NOT NULL,
            ticker VARCHAR(16),
            called_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            latency_ms INTEGER,
            status VARCHAR(24) NOT NULL,
            http_code INTEGER,
            record_count INTEGER,
            notes VARCHAR(256)
        )
        """
    )
    conn.commit()
    conn.close()


def test_log_call_inserts_row(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_table(db_path)
    set_db_path(db_path)
    try:
        log_call(
            source_name="yfinance",
            kind="live_price",
            ticker="GOOG",
            status=CallStatus.OK,
            latency_ms=42,
            record_count=1,
            notes="test row",
        )
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT source_name, kind, ticker, status, latency_ms FROM source_calls").fetchone()
        conn.close()
        assert row == ("yfinance", "live_price", "GOOG", "ok", 42)
    finally:
        # Reset module-level path so other tests aren't affected.
        set_db_path(PROJECT_ROOT / "data" / "portfolio.db")


def test_log_call_swallows_missing_db(tmp_path: Path) -> None:
    """If the DB file doesn't exist, log_call returns silently (never raises)."""
    set_db_path(tmp_path / "does_not_exist.db")
    try:
        log_call(
            source_name="yfinance",
            kind="live_price",
            ticker="GOOG",
            status=CallStatus.OK,
        )  # Should not raise.
    finally:
        set_db_path(PROJECT_ROOT / "data" / "portfolio.db")


def test_log_call_truncates_long_notes(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _make_table(db_path)
    set_db_path(db_path)
    try:
        long_note = "X" * 5000
        log_call(
            source_name="fmp_cache",
            kind="live_price",
            ticker="META",
            status=CallStatus.ERROR,
            notes=long_note,
        )
        conn = sqlite3.connect(str(db_path))
        notes = conn.execute("SELECT notes FROM source_calls").fetchone()[0]
        conn.close()
        assert notes is not None
        assert len(notes) <= 256
    finally:
        set_db_path(PROJECT_ROOT / "data" / "portfolio.db")


def test_call_status_values_are_stable() -> None:
    """The status string values are persisted to the DB and queried by analytics;
    keeping them stable across refactors is part of the contract."""
    assert CallStatus.OK.value == "ok"
    assert CallStatus.NOT_FOUND.value == "not_found"
    assert CallStatus.TIER_RESTRICTED.value == "tier_restricted"
    assert CallStatus.RATE_LIMITED.value == "rate_limited"
    assert CallStatus.ERROR.value == "error"
    assert CallStatus.SKIPPED.value == "skipped"
