"""Unit tests for `db.track_company`'s onboard-spawn transition logic.

The spawn fires whenever a ticker *transitions into* the analytical universe
(portfolio/watchlist/evaluation, == `db.ACTIVE_LIST_TYPES`) — both first-time
adds AND promotions from non-onboardable list_types (index_member, etf, none).
Re-adds within the onboardable set must NOT re-spawn.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402
from pipeline.queries import (  # noqa: E402
    ANALYZED_LIST_TYPE_VALUES,
    BRIEFED_LIST_TYPE_VALUES,
)


def test_db_list_type_contract_uses_the_canonical_query_authority() -> None:
    assert db.ACTIVE_LIST_TYPES is ANALYZED_LIST_TYPE_VALUES
    assert db.BRIEFED_LIST_TYPES is BRIEFED_LIST_TYPE_VALUES


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point db.DB_PATH at a fresh sqlite file and recreate the schema there.

    `init_db()` creates the baseline 3 tables only. The columns added by
    alembic migrations 0001-0032 (archived_at, brief_dirty, etc.) are
    appended inline below so this fixture matches the production schema
    without paying alembic's per-test setup cost (would also pull in
    holdings/JSON dependencies migration 0008 needs).
    """
    test_db = tmp_path / "portfolio.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db))
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    db.init_db()

    import sqlite3

    conn = sqlite3.connect(str(test_db))
    try:
        # Columns added by migrations 0001 / 0020 / 0021 / 0022 / 0032.
        # Mirror the column types from those migrations.
        for ddl in (
            "ALTER TABLE tracked_companies ADD COLUMN instrument_type VARCHAR",
            "ALTER TABLE tracked_companies ADD COLUMN filing_regime VARCHAR",
            "ALTER TABLE tracked_companies ADD COLUMN fiscal_year_end VARCHAR(5)",
            "ALTER TABLE tracked_companies ADD COLUMN archived_at TIMESTAMP",
            "ALTER TABLE tracked_companies ADD COLUMN brief_dirty BOOLEAN DEFAULT 0 NOT NULL",
            "ALTER TABLE tracked_companies ADD COLUMN last_built_at TIMESTAMP",
            "ALTER TABLE tracked_companies ADD COLUMN last_brief_hash VARCHAR(64)",
        ):
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()
    return test_db


@pytest.fixture()
def spawn_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the side-effecting helpers with deterministic stubs and capture
    every ticker passed to `_spawn_onboard_async`."""
    calls: list[str] = []
    monkeypatch.setattr(db, "_safe_validate_sec", lambda ticker, name: (True, name))
    monkeypatch.setattr(db, "_safe_find_ir_url", lambda ticker, name: None)
    monkeypatch.setattr(db, "scan_and_sync_artifacts", lambda ticker: None)
    monkeypatch.setattr(db, "_spawn_onboard_async", lambda ticker: calls.append(ticker))
    return calls


def _seed(db_path: Path, ticker: str, list_type: str) -> None:
    """Insert a row directly, bypassing track_company's spawn logic."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type) VALUES (?, ?, ?, ?)",
        (DEFAULT_USER_ID, ticker, f"{ticker} Corp", list_type),
    )
    conn.commit()
    conn.close()


def test_new_add_to_watchlist_spawns(isolated_db: Path, spawn_calls: list[str]) -> None:
    db.track_company("AAPL", "Apple Inc.", "watchlist")
    assert spawn_calls == ["AAPL"]


def test_new_add_to_portfolio_spawns(isolated_db: Path, spawn_calls: list[str]) -> None:
    db.track_company("MSFT", "Microsoft Corp.", "portfolio")
    assert spawn_calls == ["MSFT"]


def test_new_add_to_evaluation_spawns(isolated_db: Path, spawn_calls: list[str]) -> None:
    db.track_company("CCJ", "Cameco Corporation", "evaluation")
    assert spawn_calls == ["CCJ"]


def test_cross_promote_watchlist_to_evaluation_does_not_respawn(
    isolated_db: Path, spawn_calls: list[str]
) -> None:
    """Watchlist → evaluation is a re-classification within ACTIVE_LIST_TYPES;
    data already exists, no need to refetch."""
    _seed(isolated_db, "ABNB", "watchlist")
    db.track_company("ABNB", "Airbnb, Inc.", "evaluation")
    assert spawn_calls == []


def test_new_add_to_index_member_does_not_spawn(isolated_db: Path, spawn_calls: list[str]) -> None:
    db.track_company("XYZ", "Some Index Constituent", "index_member")
    assert spawn_calls == []


def test_re_add_same_watchlist_does_not_respawn(isolated_db: Path, spawn_calls: list[str]) -> None:
    _seed(isolated_db, "AAPL", "watchlist")
    db.track_company("AAPL", "Apple Inc.", "watchlist")
    assert spawn_calls == []


def test_promotion_from_index_member_spawns(isolated_db: Path, spawn_calls: list[str]) -> None:
    """The bug fix: a ticker on the S&P 500 index_member list, then later
    promoted to the watchlist, must trigger the onboarder. Previously this
    silently skipped the spawn because the row already existed."""
    _seed(isolated_db, "LRCX", "index_member")
    db.track_company("LRCX", "Lam Research Corporation", "watchlist")
    assert spawn_calls == ["LRCX"]


def test_promotion_from_none_spawns(isolated_db: Path, spawn_calls: list[str]) -> None:
    _seed(isolated_db, "FOO", "none")
    db.track_company("FOO", "Foo Inc.", "portfolio")
    assert spawn_calls == ["FOO"]


def test_promotion_from_etf_spawns(isolated_db: Path, spawn_calls: list[str]) -> None:
    _seed(isolated_db, "BAR", "etf")
    db.track_company("BAR", "Bar Holdings", "watchlist")
    assert spawn_calls == ["BAR"]


def test_demotion_from_watchlist_to_none_does_not_spawn(
    isolated_db: Path, spawn_calls: list[str]
) -> None:
    _seed(isolated_db, "AAPL", "watchlist")
    db.track_company("AAPL", "Apple Inc.", "none")
    assert spawn_calls == []


def test_cross_promote_watchlist_to_portfolio_does_not_respawn(
    isolated_db: Path, spawn_calls: list[str]
) -> None:
    """Already in the analytical universe — data is present, no need to refetch."""
    _seed(isolated_db, "GOOG", "watchlist")
    db.track_company("GOOG", "Alphabet Inc.", "portfolio")
    assert spawn_calls == []


def test_invalid_list_type_raises(isolated_db: Path, spawn_calls: list[str]) -> None:
    with pytest.raises(ValueError, match="Invalid list_type"):
        db.track_company("AAPL", "Apple Inc.", "bogus")
    assert spawn_calls == []
