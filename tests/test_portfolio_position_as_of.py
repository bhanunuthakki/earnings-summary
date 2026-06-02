"""Position-tab freshness (`report.sections.portfolio_position`).

The "YOUR POSITION" tab is a build-time snapshot read from the companion
portfolio-tracker SQLite. It must surface the tracker `snapshot_date` as an
"as of" so a stale position can't masquerade as current. Regression: the
query ordered by `snapshot_date` but never SELECTed it, and the section model
carried no timestamp — so the rendered tab had no freshness signal at all.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.sections.portfolio_position import (  # noqa: E402
    _holding_accounts,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
    _parse_snap_date,  # pyright: ignore[reportPrivateUsage]
)


def _tracker_conn() -> sqlite3.Connection:
    """Minimal in-memory portfolio-tracker DB with the tables `_holding_accounts`
    touches (holdings_snapshots / securities / accounts / cost_basis_overrides)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE accounts (account_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE securities (security_id INTEGER PRIMARY KEY, ticker TEXT);
        CREATE TABLE holdings_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER, security_id INTEGER,
            quantity REAL, institution_value REAL, cost_basis REAL,
            snapshot_date TEXT
        );
        CREATE TABLE cost_basis_overrides (
            account_id INTEGER, security_id INTEGER,
            total_cost_basis REAL, source TEXT
        );
        INSERT INTO accounts VALUES (1, 'Taxable');
        INSERT INTO securities VALUES (10, 'NU');
        """
    )
    return conn


def test_holding_accounts_surfaces_latest_snapshot_date() -> None:
    conn = _tracker_conn()
    # Two snapshots for the same (account, security); the latest must win and its
    # snapshot_date must come through on the row.
    conn.executescript(
        """
        INSERT INTO holdings_snapshots
            (account_id, security_id, quantity, institution_value, cost_basis, snapshot_date)
        VALUES (1, 10, 100, 1200.0, 1000.0, '2026-05-20'),
               (1, 10, 110, 1350.0, 1000.0, '2026-06-01');
        """
    )
    rows = _holding_accounts(conn, "NU")
    assert len(rows) == 1
    assert rows[0].quantity == 110  # latest snapshot row wins
    assert rows[0].snapshot_date == date(2026, 6, 1)


def test_parse_snap_date_handles_iso_datetime_and_garbage() -> None:
    assert _parse_snap_date("2026-06-01") == date(2026, 6, 1)
    assert _parse_snap_date("2026-06-01T00:00:00") == date(2026, 6, 1)
    assert _parse_snap_date(date(2026, 6, 1)) == date(2026, 6, 1)
    assert _parse_snap_date(None) is None
    assert _parse_snap_date("not-a-date") is None
