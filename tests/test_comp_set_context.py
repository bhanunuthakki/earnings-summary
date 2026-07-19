"""Synthetic-fixture unit tests for report.sections.comp_set_context
(docs/design/comparable_sets_bottoms_up.md §11, Phase 3 render consumer)."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.sections.comp_set_context import load_comp_set_context  # noqa: E402

AS_OF = date.today() - timedelta(days=1)
STALE_AS_OF = date.today() - timedelta(days=30)


def _make_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tracked_companies (id INTEGER PRIMARY KEY, ticker TEXT, name TEXT)")
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
        "method_version INTEGER, method_flags TEXT, computed_at TEXT, locator TEXT)"
    )
    conn.commit()
    return conn


def _write_json(repo_root: Path, ticker: str, suffix: str, payload: object) -> None:
    d = repo_root / "data" / "historical" / "fmp"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}_{suffix}.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_subject_financials(repo_root: Path, ticker: str) -> None:
    """Enough raw cache for compute_comparable_set_metrics to derive a
    subject-side pe_ttm/rev_yoy/fcf_yield_ttm (mirrors test_comp_set_metrics's
    minimal fixture shape)."""
    dates = ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"]
    _write_json(
        repo_root,
        ticker,
        "income_statement_quarterly",
        [
            {
                "date": d,
                "netIncome": 100.0,
                "ebitda": 130.0,
                "revenue": 500.0,
                "reportedCurrency": "USD",
            }
            for d in dates
        ],
    )
    _write_json(
        repo_root,
        ticker,
        "historical_market_cap",
        [{"date": AS_OF.isoformat(), "marketCap": 4000.0}],
    )
    _write_json(
        repo_root,
        ticker,
        "key_metrics_quarterly",
        [
            {
                "date": dates[0],
                "enterpriseValue": 4200.0,
                "marketCap": 4000.0,
                "freeCashFlowYield": 0.01,
                "reportedCurrency": "USD",
            }
        ],
    )
    _write_json(
        repo_root, ticker, "profile", {"industry": "Semiconductors", "sector": "Technology"}
    )


def _insert_scope_row(
    conn: sqlite3.Connection,
    scope_type: str,
    scope_key: str,
    as_of: date,
    metric: str,
    stat_type: str,
    value: float | None,
    n_members: int,
    n_valid: int,
) -> None:
    conn.execute(
        "INSERT INTO comp_set_metrics_daily (scope_type, scope_key, as_of_date, metric, "
        "stat_type, value, n_members, n_valid, coverage_pct, method_version, method_flags, "
        "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '{}', ?)",
        (
            scope_type,
            scope_key,
            as_of.isoformat(),
            metric,
            stat_type,
            value,
            n_members,
            n_valid,
            (n_valid / n_members) if n_members else 0.0,
            as_of.isoformat(),
        ),
    )


def test_no_frozen_set_returns_none(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    _make_db(db_path).close()
    section = load_comp_set_context("ZZZZ", db_path=db_path, repo_root=tmp_path)
    assert section is None


def test_missing_db_returns_none(tmp_path: Path) -> None:
    section = load_comp_set_context("NU", db_path=tmp_path / "nope.db", repo_root=tmp_path)
    assert section is None


def test_populated_set_returns_full_section(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    conn = _make_db(db_path)
    set_id = "NU_1"
    conn.execute(
        "INSERT INTO comparable_sets (comparable_set_id, ticker, method_version, resolved_at, "
        "metric_class, method_flags, source_summary) VALUES (?, 'NU', 1, ?, 'operating', '{}', '{}')",
        (set_id, AS_OF.isoformat()),
    )
    conn.execute(
        "INSERT INTO comparable_set_members (comparable_set_id, member_ticker, "
        "membership_reason, context_only, valid_from, valid_to) VALUES "
        "(?, 'AMD', 'industry_seed', 0, ?, NULL), "
        "(?, 'GRAB', 'llm_ratified', 1, ?, NULL)",
        (set_id, AS_OF.isoformat(), set_id, AS_OF.isoformat()),
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name) VALUES ('AMD', 'Advanced Micro Devices')"
    )
    _insert_scope_row(conn, "comparable_set", set_id, AS_OF, "pe_ttm", "median", 18.4, 1, 1)
    _insert_scope_row(conn, "comparable_set", set_id, AS_OF, "pe_ttm", "aggregate", 19.1, 1, 1)
    _insert_scope_row(conn, "industry", "Semiconductors", AS_OF, "pe_ttm", "median", 22.0, 30, 28)
    _insert_scope_row(conn, "sector", "Technology", AS_OF, "pe_ttm", "median", 25.0, 90, 85)
    conn.commit()
    conn.close()

    _seed_subject_financials(tmp_path, "NU")

    section = load_comp_set_context("NU", db_path=db_path, repo_root=tmp_path)
    assert section is not None
    assert section.comparable_set_id == set_id
    assert section.metric_class == "operating"
    assert section.n_members == 1  # AMD only — GRAB is context_only
    assert section.as_of_date == AS_OF
    assert section.stale is False
    assert section.industry == "Semiconductors"
    assert section.sector == "Technology"
    assert section.industry_scope is not None
    assert section.industry_scope.pe_ttm_median == 22.0
    assert section.sector_scope is not None
    assert section.sector_scope.pe_ttm_median == 25.0
    # Semiconductors is ratified in SECTOR_BENCHMARK_MAP.
    assert section.benchmark_etf == "SMH"
    assert section.benchmark_sector_etf == "XLK"
    # Subject's own pe_ttm derived from the seeded financials (market_cap 4000
    # / ttm_net_income 400 = 10x).
    pe_line = next(m for m in section.primary_metrics if m.metric == "pe_ttm")
    assert pe_line.subject_value == pytest.approx(10.0)
    assert pe_line.median_value == 18.4
    # Roster: both members present, context_only correctly tagged.
    by_ticker = {m.ticker: m for m in section.members}
    assert by_ticker["AMD"].context_only is False
    assert by_ticker["AMD"].name == "Advanced Micro Devices"
    assert by_ticker["GRAB"].context_only is True


def test_stale_flag_set_when_as_of_old(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    conn = _make_db(db_path)
    set_id = "NU_1"
    conn.execute(
        "INSERT INTO comparable_sets (comparable_set_id, ticker, method_version, resolved_at, "
        "metric_class, method_flags, source_summary) VALUES (?, 'NU', 1, ?, 'operating', '{}', '{}')",
        (set_id, STALE_AS_OF.isoformat()),
    )
    conn.execute(
        "INSERT INTO comparable_set_members (comparable_set_id, member_ticker, "
        "membership_reason, context_only, valid_from, valid_to) VALUES "
        "(?, 'AMD', 'industry_seed', 0, ?, NULL)",
        (set_id, STALE_AS_OF.isoformat()),
    )
    _insert_scope_row(conn, "comparable_set", set_id, STALE_AS_OF, "pe_ttm", "median", 18.0, 1, 1)
    conn.commit()
    conn.close()
    _seed_subject_financials(tmp_path, "NU")

    section = load_comp_set_context("NU", db_path=db_path, repo_root=tmp_path)
    assert section is not None
    assert section.stale is True


def test_set_with_no_metrics_yet(tmp_path: Path) -> None:
    """A frozen set with zero comp_set_metrics_daily rows (build ran, track
    hasn't yet) -- section exists but as_of_date is None."""
    db_path = tmp_path / "portfolio.db"
    conn = _make_db(db_path)
    set_id = "NU_1"
    conn.execute(
        "INSERT INTO comparable_sets (comparable_set_id, ticker, method_version, resolved_at, "
        "metric_class, method_flags, source_summary) VALUES (?, 'NU', 1, ?, 'operating', '{}', '{}')",
        (set_id, AS_OF.isoformat()),
    )
    conn.commit()
    conn.close()
    section = load_comp_set_context("NU", db_path=db_path, repo_root=tmp_path)
    assert section is not None
    assert section.as_of_date is None
    assert section.stale is True
