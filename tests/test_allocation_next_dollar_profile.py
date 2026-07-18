"""tenet-2 Phase 2 additions to the next-dollar allocation model
(src/allocation/model.py): profile-driven blend weights (owner decision 5,
§7 of docs/design/tenet2_advisory_program.md) and cash-aware mode.

Duplicates the fixture setup from tests/test_allocation_model.py (three
synthetic tickers with DCF + price history + macro betas, so all three
factors are ACTIVE and the blend proportions are actually visible — with
only one factor active, normalization would mask any difference between the
owner's weights and the hardcoded fallback) per the repo's "duplicate simple
shared logic, don't modularize" convention, plus an ``owner_profile_facts``
table for the appetite-fact tests.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from allocation.model import BLEND_WEIGHTS, build_next_dollar_model
from owner_profile.store import affirm_fact, append_fact

TICKERS = ["AAA", "BBB", "CCC"]

_OWNER_PROFILE_DDL = """
CREATE TABLE owner_profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    category TEXT NOT NULL CHECK (category IN ('capacity','appetite','behavioral')),
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    narrative TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (
        provenance IN ('wealthplan_import','cio_context_import','owner','derived')
    ),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','affirmed','rejected')),
    affirmed_at TEXT,
    review_horizon_days INTEGER,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
    superseded_by_id INTEGER
);
CREATE UNIQUE INDEX ux_owner_profile_facts_latest
    ON owner_profile_facts(user_id, category, key) WHERE is_latest = 1;
"""


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            valuation_date TEXT,
            npv_per_share NUMERIC,
            live_price FLOAT,
            created_at DATETIME
        );
        CREATE TABLE macro_series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id VARCHAR(48) NOT NULL,
            rate_date DATE NOT NULL,
            value NUMERIC(20,8) NOT NULL,
            source VARCHAR(32) NOT NULL DEFAULT 'FMP',
            created_at DATETIME NOT NULL,
            UNIQUE(series_id, rate_date)
        );
        CREATE TABLE macro_sensitivities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            series_id VARCHAR(48) NOT NULL,
            beta FLOAT NOT NULL,
            r_squared FLOAT,
            lookback_window_days INTEGER NOT NULL,
            computed_at DATETIME NOT NULL,
            UNIQUE(ticker, series_id, lookback_window_days)
        );
        """
    )
    conn.executescript(_OWNER_PROFILE_DDL)
    conn.commit()


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    try:
        _schema(conn)
    finally:
        conn.close()
    return tmp_path


def _db(repo_root: Path) -> Path:
    return repo_root / "data" / "portfolio.db"


def _seed_dcf(repo_root: Path, rows: list[tuple[str, float, float, str]]) -> None:
    conn = sqlite3.connect(_db(repo_root))
    try:
        conn.executemany(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(t, vd, fv, px, f"{vd} 00:00:00") for t, fv, px, vd in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_prices(repo_root: Path, n: int = 200) -> None:
    days: list[date] = []
    d = date.today() - timedelta(days=2)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    rng = np.random.default_rng(0)
    base = rng.normal(0.0005, 0.02, n)
    noise = rng.normal(0.0, 0.005, n)
    indep = rng.normal(0.0005, 0.02, n)
    series = {"AAA": base, "BBB": 0.9 * base + noise, "CCC": indep}
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    for ticker, rets in series.items():
        prices = 100.0 * np.exp(np.cumsum(rets))
        rows = [
            {"date": days[i].isoformat(), "adjClose": round(float(prices[i]), 6)} for i in range(n)
        ][::-1]
        (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(
            json.dumps(rows), encoding="utf-8"
        )


def _seed_macro(repo_root: Path, betas: dict[str, float]) -> None:
    conn = sqlite3.connect(_db(repo_root))
    now = datetime.now(UTC).isoformat()
    latest = date.today() - timedelta(days=1)
    cutoff = latest - timedelta(days=90)
    try:
        for k in range(19):
            day = latest - timedelta(days=7 * k)
            value = 5.5 if day > cutoff else 5.0
            conn.execute(
                "INSERT INTO macro_series (series_id, rate_date, value, source, created_at)"
                " VALUES (?, ?, ?, 'FMP', ?)",
                ("usd_brl", day.isoformat(), value, now),
            )
        for ticker, beta in betas.items():
            conn.execute(
                "INSERT INTO macro_sensitivities"
                " (ticker, series_id, beta, r_squared, lookback_window_days, computed_at)"
                " VALUES (?, 'usd_brl', ?, 0.4, 252, ?)",
                (ticker, beta, now),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_full_three_factor_book(repo_root: Path) -> None:
    _seed_prices(repo_root)
    _seed_dcf(
        repo_root,
        [
            ("AAA", 150.0, 100.0, "2026-06-08"),
            ("BBB", 100.0, 100.0, "2026-06-08"),
            ("CCC", 120.0, 100.0, "2026-06-09"),
        ],
    )
    _seed_macro(repo_root, {"AAA": 2.0, "BBB": -1.0, "CCC": 0.5})


def _affirm_blend_weights(repo_root: Path, ret: float, div: float, macro: float) -> None:
    conn = sqlite3.connect(_db(repo_root))
    try:
        fact_id = append_fact(
            conn,
            category="appetite",
            key="next_dollar.blend_weights",
            value={"ret": ret, "div": div, "macro": macro},
            narrative="owner-ratified blend",
            provenance="derived",
            status="proposed",
        )
        affirm_fact(conn, fact_id)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# blend_weights_source — default fallback vs owner-affirmed
# --------------------------------------------------------------------------- #


def test_default_fallback_when_no_owner_profile_table(tmp_path: Path) -> None:
    """A DB predating migration 0159 (no owner_profile_facts table at all) —
    degrades to the hardcoded BLEND_WEIGHTS, never raises."""
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    try:
        _schema(conn)
        conn.execute("DROP TABLE owner_profile_facts")
        conn.commit()
    finally:
        conn.close()
    _seed_full_three_factor_book(tmp_path)
    model = build_next_dollar_model(_db(tmp_path), tmp_path, TICKERS, None)
    assert model is not None
    assert model.blend_weights_source == "default_fallback"
    assert model.blend == [
        ("ret", pytest.approx(BLEND_WEIGHTS["ret"])),
        ("div", pytest.approx(BLEND_WEIGHTS["div"])),
        ("macro", pytest.approx(BLEND_WEIGHTS["macro"])),
    ]


def test_default_fallback_when_fact_only_proposed(repo_root: Path) -> None:
    _seed_full_three_factor_book(repo_root)
    conn = sqlite3.connect(_db(repo_root))
    try:
        append_fact(
            conn,
            category="appetite",
            key="next_dollar.blend_weights",
            value={"ret": 0.7, "div": 0.2, "macro": 0.1},
            narrative="not yet ratified",
            provenance="derived",
            status="proposed",
        )
        conn.commit()
    finally:
        conn.close()
    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None)
    assert model is not None
    assert model.blend_weights_source == "default_fallback"
    assert dict(model.blend) == pytest.approx(BLEND_WEIGHTS)


def test_owner_affirmed_blend_weights_drive_the_model(repo_root: Path) -> None:
    _seed_full_three_factor_book(repo_root)
    _affirm_blend_weights(repo_root, ret=0.7, div=0.2, macro=0.1)
    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None)
    assert model is not None
    assert model.blend_weights_source == "owner_profile"
    assert dict(model.blend) == pytest.approx({"ret": 0.7, "div": 0.2, "macro": 0.1})


def test_invalid_owner_weights_fall_back_to_default(repo_root: Path) -> None:
    """A hand-edited/corrupt fact that doesn't sum to ~1.0 fails
    NextDollarBlendWeights validation — degrades to the fallback rather than
    silently using an unnormalized blend."""
    _seed_full_three_factor_book(repo_root)
    conn = sqlite3.connect(_db(repo_root))
    try:
        fact_id = append_fact(
            conn,
            category="appetite",
            key="next_dollar.blend_weights",
            value={"ret": 0.9, "div": 0.9, "macro": 0.9},  # sums to 2.7
            narrative="corrupt",
            provenance="derived",
            status="proposed",
        )
        affirm_fact(conn, fact_id)
        conn.commit()
    finally:
        conn.close()
    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None)
    assert model is not None
    assert model.blend_weights_source == "default_fallback"


# --------------------------------------------------------------------------- #
# cash-aware mode
# --------------------------------------------------------------------------- #


def test_cash_to_deploy_usd_produces_per_holding_dollar_amounts(repo_root: Path) -> None:
    _seed_full_three_factor_book(repo_root)
    model = build_next_dollar_model(
        _db(repo_root), repo_root, TICKERS, None, cash_to_deploy_usd=10_000.0
    )
    assert model is not None
    assert model.cash_to_deploy_usd == pytest.approx(10_000.0)
    total_cash = 0.0
    for row in model.rows:
        assert row.cash_allocation_usd is not None
        assert row.cash_allocation_usd == pytest.approx(row.allocation_pct / 100.0 * 10_000.0)
        total_cash += row.cash_allocation_usd
    assert total_cash == pytest.approx(10_000.0)


def test_no_cash_to_deploy_usd_leaves_cash_allocation_none(repo_root: Path) -> None:
    """Default (no cash figure) mode is UNCHANGED from pre-Phase-2 behavior."""
    _seed_full_three_factor_book(repo_root)
    model = build_next_dollar_model(_db(repo_root), repo_root, TICKERS, None)
    assert model is not None
    assert model.cash_to_deploy_usd is None
    for row in model.rows:
        assert row.cash_allocation_usd is None
