"""Phase 2 unit tests: industry/sector pool-wide scope helpers
(docs/design/comparable_sets_bottoms_up.md §4.1/§6/§11 Phase 2).

Covers `compute.comparable_sets.resolve_industry_scope_members` /
`resolve_sector_scope_members` / `pool_industries` / `pool_sectors` — the
pool-wide slice `track_comp_metrics.py` uses for `scope_type='industry'`/
`'sector'` rows, as opposed to a per-subject market-cap-banded comparable set.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comparable_sets import (  # noqa: E402
    load_pool,
    pool_industries,
    pool_sectors,
    resolve_industry_scope_members,
    resolve_sector_scope_members,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tracked_companies (id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, "
        "list_type TEXT, instrument_type TEXT, archived_at TEXT)"
    )
    return conn


def _add_company(
    conn: sqlite3.Connection, ticker: str, list_type: str, instrument_type: str = "equity"
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, list_type, instrument_type) "
        "VALUES ('bhanu', ?, ?, ?)",
        (ticker, list_type, instrument_type),
    )


def _write_profile(
    repo_root: Path,
    ticker: str,
    *,
    sector: str,
    industry: str,
    market_cap: float,
    exchange: str | None = "NASDAQ",
    active: bool = True,
) -> None:
    d = repo_root / "data" / "historical" / "fmp"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}_profile.json").write_text(
        json.dumps(
            [
                {
                    "companyName": ticker,
                    "sector": sector,
                    "industry": industry,
                    "marketCap": market_cap,
                    "exchange": exchange,
                    "isActivelyTrading": active,
                    "isEtf": False,
                    "isFund": False,
                }
            ]
        ),
        encoding="utf-8",
    )


def test_pool_industries_and_sectors_are_distinct_sorted(tmp_path: Path) -> None:
    conn = _make_conn()
    _add_company(conn, "A", "portfolio")
    _add_company(conn, "B", "index_member")
    _add_company(conn, "C", "watchlist")
    _write_profile(
        tmp_path, "A", sector="Technology", industry="Software - Application", market_cap=1e10
    )
    _write_profile(
        tmp_path, "B", sector="Technology", industry="Software - Application", market_cap=2e10
    )
    _write_profile(
        tmp_path, "C", sector="Financial Services", industry="Banks - Regional", market_cap=5e9
    )
    pool = load_pool(conn, tmp_path, user_id="bhanu")

    assert pool_industries(pool) == ["Banks - Regional", "Software - Application"]
    assert pool_sectors(pool) == ["Financial Services", "Technology"]


def test_industry_scope_pulls_every_pool_member_no_market_cap_band(tmp_path: Path) -> None:
    """Unlike Step A (§3.1), industry-scope has no market-cap band -- a
    100x-smaller pool member in the same industry still contributes."""
    conn = _make_conn()
    _add_company(conn, "BIG", "portfolio")
    _add_company(conn, "TINY", "index_member")
    _write_profile(
        tmp_path, "BIG", sector="Technology", industry="Software - Application", market_cap=1e11
    )
    _write_profile(
        tmp_path, "TINY", sector="Technology", industry="Software - Application", market_cap=1e9
    )
    pool = load_pool(conn, tmp_path, user_id="bhanu")

    members = resolve_industry_scope_members(pool, "Software - Application")
    assert members == ["BIG", "TINY"]


def test_industry_scope_excludes_non_us_listed_and_inactive(tmp_path: Path) -> None:
    conn = _make_conn()
    _add_company(conn, "US", "portfolio")
    _add_company(conn, "FOREIGN", "index_member")
    _add_company(conn, "HALTED", "index_member")
    _write_profile(
        tmp_path, "US", sector="Technology", industry="Software - Application", market_cap=1e10
    )
    _write_profile(
        tmp_path,
        "FOREIGN",
        sector="Technology",
        industry="Software - Application",
        market_cap=1e10,
        exchange=None,
    )
    _write_profile(
        tmp_path,
        "HALTED",
        sector="Technology",
        industry="Software - Application",
        market_cap=1e10,
        active=False,
    )
    pool = load_pool(conn, tmp_path, user_id="bhanu")

    members = resolve_industry_scope_members(pool, "Software - Application")
    assert members == ["US"]


def test_sector_scope_pulls_every_matching_pool_member(tmp_path: Path) -> None:
    conn = _make_conn()
    _add_company(conn, "BANK1", "portfolio")
    _add_company(conn, "BANK2", "index_member")
    _add_company(conn, "TECH", "portfolio")
    _write_profile(
        tmp_path, "BANK1", sector="Financial Services", industry="Banks - Regional", market_cap=1e10
    )
    _write_profile(
        tmp_path,
        "BANK2",
        sector="Financial Services",
        industry="Banks - Diversified",
        market_cap=2e11,
    )
    _write_profile(
        tmp_path, "TECH", sector="Technology", industry="Software - Application", market_cap=1e10
    )
    pool = load_pool(conn, tmp_path, user_id="bhanu")

    members = resolve_sector_scope_members(pool, "Financial Services")
    assert members == ["BANK1", "BANK2"]


def test_unknown_industry_returns_empty(tmp_path: Path) -> None:
    conn = _make_conn()
    pool = load_pool(conn, tmp_path, user_id="bhanu")
    assert resolve_industry_scope_members(pool, "Nonexistent Industry") == []
    assert resolve_sector_scope_members(pool, "Nonexistent Sector") == []
