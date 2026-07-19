"""CLI-level Phase 2 tests for execution/build_comparable_sets.py and
execution/track_comp_metrics.py (docs/design/comparable_sets_bottoms_up.md
§8/§11 Phase 2): `--all-tracked` subject widening and the new
`scope_type='industry'`/`'sector'` rows.

Hand-rolls the minimal schema each script's SQL touches (same pattern as
tests/test_comparable_sets.py's `_make_conn`) rather than running the full
alembic chain -- faster, and this is a unit/integration test of the CLI's own
logic, not a migration round-trip (that's covered by
tests/test_migration_0170_comparable_sets.py already).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _load_cli(name: str, filename: str) -> Any:
    src = PROJECT_ROOT / "execution" / filename
    spec = importlib.util.spec_from_file_location(name, src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _init_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE tracked_companies (id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, "
        "name TEXT, list_type TEXT, added_at TEXT, sec_validated INTEGER, ir_url TEXT, "
        "instrument_type TEXT, filing_regime TEXT, fiscal_year_end TEXT, "
        "fmp_data_saved INTEGER, fmp_data_upto TEXT, archived_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE comparable_sets (comparable_set_id TEXT PRIMARY KEY, ticker TEXT, "
        "method_version INTEGER, resolved_at TEXT, metric_class TEXT, method_flags TEXT, "
        "source_summary TEXT)"
    )
    conn.execute(
        "CREATE TABLE comparable_set_members (comparable_set_id TEXT, member_ticker TEXT, "
        "membership_reason TEXT, context_only INTEGER, valid_from TEXT, valid_to TEXT, "
        "PRIMARY KEY (comparable_set_id, member_ticker, valid_from))"
    )
    conn.execute(
        "CREATE TABLE comp_set_metrics_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "scope_type TEXT, scope_key TEXT, as_of_date TEXT, metric TEXT, stat_type TEXT, "
        "value REAL, n_members INTEGER, n_valid INTEGER, coverage_pct REAL, "
        "method_version INTEGER, method_flags TEXT, computed_at TEXT, "
        "UNIQUE(scope_type, scope_key, as_of_date, metric, stat_type, method_version))"
    )
    conn.execute(
        "CREATE TABLE ingestion_runs (run_id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    conn.execute(
        "CREATE TABLE stage_transitions (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
        "ticker TEXT, period_end TEXT, stage TEXT, status TEXT, started_at TEXT, ended_at TEXT, "
        "error_msg TEXT)"
    )
    conn.commit()
    conn.close()


def _add_company(
    db_path: Path, ticker: str, list_type: str, instrument_type: str = "equity"
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type, instrument_type, "
        "sec_validated, fmp_data_saved) VALUES ('bhanu', ?, ?, ?, ?, 0, 0)",
        (ticker, ticker, list_type, instrument_type),
    )
    conn.commit()
    conn.close()


def _write_profile(
    repo_root: Path, ticker: str, *, sector: str, industry: str, market_cap: float
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
                    "exchange": "NASDAQ",
                    "isActivelyTrading": True,
                    "isEtf": False,
                    "isFund": False,
                }
            ]
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# build_comparable_sets.py --all-tracked
# ---------------------------------------------------------------------------


def test_build_all_tracked_widens_beyond_portfolio(tmp_path: Path, monkeypatch: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _init_schema(db_path)
    for ticker, lt in (
        ("PORT", "portfolio"),
        ("WATCH", "watchlist"),
        ("EVAL", "evaluation"),
        ("IDX", "index_member"),
    ):
        _add_company(db_path, ticker, lt)
        _write_profile(
            tmp_path,
            ticker,
            sector="Technology",
            industry="Software - Application",
            market_cap=1e10,
        )

    mod = _load_cli("build_comparable_sets_p2", "build_comparable_sets.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_comparable_sets.py",
            "--all-tracked",
            "--db",
            str(db_path),
            "--repo-root",
            str(tmp_path),
        ],
    )
    rc = mod.main()
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    tickers = {r["ticker"] for r in conn.execute("SELECT DISTINCT ticker FROM comparable_sets")}
    # portfolio + watchlist + evaluation are SUBJECTS; index_member is pool-only context.
    assert tickers == {"PORT", "WATCH", "EVAL"}


def test_build_all_portfolio_still_defaults_to_portfolio_only(
    tmp_path: Path, monkeypatch: Any
) -> None:
    db_path = tmp_path / "portfolio.db"
    _init_schema(db_path)
    _add_company(db_path, "PORT", "portfolio")
    _add_company(db_path, "WATCH", "watchlist")
    _write_profile(
        tmp_path, "PORT", sector="Technology", industry="Software - Application", market_cap=1e10
    )
    _write_profile(
        tmp_path, "WATCH", sector="Technology", industry="Software - Application", market_cap=1e10
    )

    mod = _load_cli("build_comparable_sets_p2b", "build_comparable_sets.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_comparable_sets.py",
            "--all-portfolio",
            "--db",
            str(db_path),
            "--repo-root",
            str(tmp_path),
        ],
    )
    rc = mod.main()
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    tickers = {r["ticker"] for r in conn.execute("SELECT DISTINCT ticker FROM comparable_sets")}
    assert tickers == {"PORT"}


# ---------------------------------------------------------------------------
# track_comp_metrics.py industry/sector scope rows + --all-tracked
# ---------------------------------------------------------------------------


def _seed_member_financials(
    tmp_path: Path, ticker: str, market_cap: float, net_income: float
) -> None:
    d = tmp_path / "data" / "historical" / "fmp"
    d.mkdir(parents=True, exist_ok=True)
    dates = ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"]
    (d / f"{ticker}_income_statement_quarterly.json").write_text(
        json.dumps(
            [
                {
                    "date": d_,
                    "netIncome": net_income,
                    "ebitda": net_income * 1.3,
                    "revenue": net_income * 5,
                }
                for d_ in dates
            ]
        ),
        encoding="utf-8",
    )
    (d / f"{ticker}_key_metrics_quarterly.json").write_text(
        json.dumps(
            [
                {
                    "date": d_,
                    "marketCap": market_cap,
                    "enterpriseValue": market_cap * 1.1,
                    "freeCashFlowYield": 0.02,
                }
                for d_ in dates
            ]
        ),
        encoding="utf-8",
    )
    (d / f"{ticker}_historical_market_cap.json").write_text(
        json.dumps([{"date": d_, "marketCap": market_cap} for d_ in dates]), encoding="utf-8"
    )


def test_track_metrics_writes_industry_and_sector_rows(tmp_path: Path, monkeypatch: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _init_schema(db_path)
    _add_company(db_path, "SUBJ", "portfolio")
    _add_company(db_path, "PEER", "index_member")
    for t in ("SUBJ", "PEER"):
        _write_profile(
            tmp_path, t, sector="Technology", industry="Software - Application", market_cap=1e10
        )
        _seed_member_financials(tmp_path, t, market_cap=1e10, net_income=1e8)

    build_mod = _load_cli("build_comparable_sets_p2c", "build_comparable_sets.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_comparable_sets.py",
            "--ticker",
            "SUBJ",
            "--db",
            str(db_path),
            "--repo-root",
            str(tmp_path),
        ],
    )
    assert build_mod.main() == 0

    track_mod = _load_cli("track_comp_metrics_p2", "track_comp_metrics.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "track_comp_metrics.py",
            "--ticker",
            "SUBJ",
            "--date",
            "2026-07-17",
            "--db",
            str(db_path),
            "--repo-root",
            str(tmp_path),
        ],
    )
    assert track_mod.main() == 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT scope_type, scope_key FROM comp_set_metrics_daily WHERE scope_type IN "
        "('industry', 'sector') GROUP BY scope_type, scope_key"
    ).fetchall()
    scopes = {(r["scope_type"], r["scope_key"]) for r in rows}
    assert ("industry", "Software - Application") in scopes
    assert ("sector", "Technology") in scopes


def test_track_metrics_skip_industry_sector_flag(tmp_path: Path, monkeypatch: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _init_schema(db_path)
    _add_company(db_path, "SUBJ", "portfolio")
    _write_profile(
        tmp_path, "SUBJ", sector="Technology", industry="Software - Application", market_cap=1e10
    )
    _seed_member_financials(tmp_path, "SUBJ", market_cap=1e10, net_income=1e8)

    build_mod = _load_cli("build_comparable_sets_p2d", "build_comparable_sets.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_comparable_sets.py",
            "--ticker",
            "SUBJ",
            "--db",
            str(db_path),
            "--repo-root",
            str(tmp_path),
        ],
    )
    assert build_mod.main() == 0

    track_mod = _load_cli("track_comp_metrics_p2b", "track_comp_metrics.py")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "track_comp_metrics.py",
            "--ticker",
            "SUBJ",
            "--skip-industry-sector",
            "--date",
            "2026-07-17",
            "--db",
            str(db_path),
            "--repo-root",
            str(tmp_path),
        ],
    )
    assert track_mod.main() == 0

    conn = sqlite3.connect(str(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM comp_set_metrics_daily WHERE scope_type IN ('industry', 'sector')"
    ).fetchone()[0]
    assert count == 0
