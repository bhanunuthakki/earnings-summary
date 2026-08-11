from __future__ import annotations

import sqlite3
from pathlib import Path


def test_portfolio_tickers_are_loaded_from_active_governed_roster(tmp_path: Path) -> None:
    from execution.run_lens import _portfolio_tickers

    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "portfolio.db")
    try:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?, ?)",
            (
                ("uber", "portfolio", None),
                ("BKNG", "portfolio", None),
                ("OLD", "portfolio", "2026-08-01"),
                ("DUOL", "evaluation", None),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    assert _portfolio_tickers(tmp_path) == ["BKNG", "UBER"]
