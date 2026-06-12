"""Tests for the silent-staleness detector in execution/fetch_sec_xbrl.py.

When an SEC ingest registers new accessions but emits zero financial_facts
rows (parser fell behind a tag-map change, filing carried only YTD
aggregates, etc.), the SQL triggers on financial_facts never fire and
brief_dirty stays 0. The detector force-flips brief_dirty so the daily
worker re-attempts the build.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution.fetch_sec_xbrl import _flag_silent_staleness  # noqa: E402


def _build_schema(conn: sqlite3.Connection, *, with_brief_dirty: bool) -> None:
    if with_brief_dirty:
        conn.execute(
            "CREATE TABLE tracked_companies ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  ticker TEXT NOT NULL UNIQUE,"
            "  brief_dirty INTEGER NOT NULL DEFAULT 0"
            ")"
        )
    else:
        conn.execute(
            "CREATE TABLE tracked_companies ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  ticker TEXT NOT NULL UNIQUE"
            ")"
        )
    conn.execute("INSERT INTO tracked_companies (ticker) VALUES ('AMZN'), ('GOOG')")
    conn.commit()


def test_flag_flips_brief_dirty_for_matching_ticker(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        _build_schema(conn, with_brief_dirty=True)
        updated = _flag_silent_staleness(conn, "AMZN")
        assert updated is True

        row = conn.execute(
            "SELECT brief_dirty FROM tracked_companies WHERE ticker = 'AMZN'"
        ).fetchone()
        assert row[0] == 1

        # GOOG untouched.
        other = conn.execute(
            "SELECT brief_dirty FROM tracked_companies WHERE ticker = 'GOOG'"
        ).fetchone()
        assert other[0] == 0
    finally:
        conn.close()


def test_flag_handles_lowercase_ticker_via_upper(tmp_path: Path) -> None:
    """ingest_for_ticker uppercases internally — the detector mirrors that
    convention so case mismatch in the caller doesn't silently no-op."""
    conn = sqlite3.connect(":memory:")
    try:
        _build_schema(conn, with_brief_dirty=True)
        updated = _flag_silent_staleness(conn, "amzn")
        assert updated is True
    finally:
        conn.close()


def test_flag_returns_false_when_ticker_not_tracked(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        _build_schema(conn, with_brief_dirty=True)
        updated = _flag_silent_staleness(conn, "NVDA")
        assert updated is False
    finally:
        conn.close()


def test_flag_is_schema_tolerant_when_column_missing(tmp_path: Path) -> None:
    """Pre-0022 schemas don't have brief_dirty — the detector must not raise."""
    conn = sqlite3.connect(":memory:")
    try:
        _build_schema(conn, with_brief_dirty=False)
        updated = _flag_silent_staleness(conn, "AMZN")
        assert updated is False
    finally:
        conn.close()
