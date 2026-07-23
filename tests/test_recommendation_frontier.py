"""Tests for src/allocation/recommendation.py — the PRD §7.4 deterministic
Incremental Dollar frontier. No LLM call anywhere on this path (P0.4 is the
governed-selection layer, out of scope here).

Hand-rolled minimal schemas (no alembic-head fixture), modeled on
tests/test_position_guard.py / tests/test_allocation_model.py. The live
tracker fetch (``integrations.portfolio_tracker_client.fetch_live_portfolio``)
is monkeypatched to a fixed book total — these tests never touch the network.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import allocation.recommendation as recommendation
from allocation.recommendation import DeterministicFrontier, build_frontier
from integrations.portfolio_tracker_client import LivePortfolio

_FULL_BREAK_RULE: dict[str, object] = {
    "rule_id": "r1",
    "kpi_name": "Revenue growth YoY",
    "comparator": "lt",
    "threshold": 0,
    "unit": "percent",
    "narrative": "Revenue growth turns negative for a full quarter.",
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(delta: timedelta = timedelta()) -> str:
    return (_now() - delta).isoformat()


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            created_at TEXT,
            is_latest INTEGER,
            segment_name TEXT,
            valuation_date TEXT,
            npv_per_share REAL,
            live_price REAL,
            live_price_at TEXT,
            sanity_flag TEXT
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            name TEXT
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kpi_definition_id INTEGER,
            ticker TEXT
        );
        CREATE TABLE thesis_state (
            ticker TEXT,
            thesis TEXT,
            raw_json TEXT,
            ingested_at TEXT
        );
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            ticker TEXT,
            name TEXT,
            list_type TEXT,
            added_at TEXT,
            sec_validated INTEGER,
            ir_url TEXT,
            instrument_type TEXT,
            filing_regime TEXT,
            fiscal_year_end TEXT,
            fmp_data_saved INTEGER,
            fmp_data_upto TEXT,
            archived_at TEXT
        );
        CREATE TABLE owner_profile_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'bhanu',
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            narrative TEXT NOT NULL,
            provenance TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            affirmed_at TEXT,
            review_horizon_days INTEGER,
            source_detail TEXT,
            created_at TEXT NOT NULL,
            is_latest INTEGER NOT NULL DEFAULT 1,
            superseded_at TEXT,
            superseded_by_id INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_dcf_row(
    db_path: Path,
    ticker: str,
    *,
    live_price_at: str | None,
    valuation_date: str = "2026-07-15",
    npv_per_share: float | None,
    live_price: float | None,
    sanity_flag: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dcf_runs (ticker, created_at, is_latest, segment_name, valuation_date, "
        "npv_per_share, live_price, live_price_at, sanity_flag) "
        "VALUES (?, ?, 1, NULL, ?, ?, ?, ?, ?)",
        (
            ticker,
            "2026-07-15T00:00:00",
            valuation_date,
            npv_per_share,
            live_price,
            live_price_at,
            sanity_flag,
        ),
    )
    conn.commit()
    conn.close()


def _insert_kpi_coverage(db_path: Path, ticker: str, *, n_def: int, n_covered: int) -> None:
    conn = sqlite3.connect(str(db_path))
    def_ids: list[int] = []
    for i in range(n_def):
        cur = conn.execute(
            "INSERT INTO kpi_definitions (ticker, name) VALUES (?, ?)", (ticker, f"kpi_{i}")
        )
        def_ids.append(int(cur.lastrowid or 0))
    for def_id in def_ids[:n_covered]:
        conn.execute(
            "INSERT INTO kpi_facts (kpi_definition_id, ticker) VALUES (?, ?)", (def_id, ticker)
        )
    conn.commit()
    conn.close()


def _insert_thesis(
    db_path: Path, ticker: str, *, thesis: str, ingested_at: str | None = None
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, raw_json, ingested_at) VALUES (?, ?, NULL, ?)",
        (ticker, thesis, ingested_at or _iso()),
    )
    conn.commit()
    conn.close()


def _insert_tracked_company(
    db_path: Path, ticker: str, *, list_type: str, user_id: str = "bhanu"
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type, instrument_type) "
        "VALUES (?, ?, ?, ?, 'equity')",
        (user_id, ticker, ticker, list_type),
    )
    conn.commit()
    conn.close()


def _insert_owner_fact(
    db_path: Path,
    *,
    key: str,
    value: dict[str, object],
    status: str,
    category: str = "capacity",
    user_id: str = "bhanu",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO owner_profile_facts (user_id, category, key, value_json, narrative, "
        "provenance, status, created_at, is_latest) VALUES (?, ?, ?, ?, ?, 'owner', ?, ?, 1)",
        (user_id, category, key, json.dumps(value), f"{key} narrative", status, _iso()),
    )
    conn.commit()
    conn.close()


def _write_holdings(repo_root: Path, ticker: str) -> None:
    d = repo_root / "micro_thesis" / "holdings"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker,
        "thesis": f"{ticker} thesis placeholder.",
        "break_rules": [_FULL_BREAK_RULE],
    }
    (d / f"{ticker}.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_weights_cache(repo_root: Path, weights: dict[str, float], *, computed_at: str) -> None:
    cache_dir = repo_root / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "portfolio_weights.json").write_text(
        json.dumps({"computed_at": computed_at, "weights": weights}), encoding="utf-8"
    )


def _write_candidate_fit_cache(
    repo_root: Path, fits: dict[str, dict[str, object]], *, computed_at: str
) -> None:
    cache_dir = repo_root / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "candidate_fit.json").write_text(
        json.dumps(
            {"version": 2, "computed_at": computed_at, "book": {}, "target": {}, "fits": fits}
        ),
        encoding="utf-8",
    )


def _make_eligible(
    db_path: Path,
    repo_root: Path,
    ticker: str,
    *,
    list_type: str,
    npv_per_share: float,
    live_price: float = 100.0,
) -> None:
    """Wire ``ticker`` to clear every §7.3 eligibility check for ``list_type``."""
    _insert_dcf_row(
        db_path,
        ticker,
        live_price_at=_iso(timedelta(days=1)),
        npv_per_share=npv_per_share,
        live_price=live_price,
    )
    _insert_kpi_coverage(db_path, ticker, n_def=2, n_covered=2)
    _insert_thesis(db_path, ticker, thesis=f"{ticker} grows revenue on strong unit economics.")
    _write_holdings(repo_root, ticker)
    _insert_tracked_company(db_path, ticker, list_type=list_type)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module supplies its own tracker total via
    ``_patch_tracker`` when it wants one; the default is "unavailable" so a
    test that forgets to patch degrades loudly (retain_cash only) rather than
    silently hitting a real localhost:8000."""
    monkeypatch.setattr(
        recommendation,
        "fetch_live_portfolio",
        lambda **_kwargs: LivePortfolio(
            available=False, api_url="http://unused", error="not patched"
        ),
    )


def _patch_tracker(monkeypatch: pytest.MonkeyPatch, *, total_value: float) -> None:
    monkeypatch.setattr(
        recommendation,
        "fetch_live_portfolio",
        lambda **_kwargs: LivePortfolio(
            available=True, api_url="http://test", total_market_value=total_value
        ),
    )


def _plan(frontier: DeterministicFrontier, kind: str):
    return next((p for p in frontier.plans if p.kind == kind), None)


# --------------------------------------------------------------------------- #
# No eligible security
# --------------------------------------------------------------------------- #


def test_no_eligible_security_yields_retain_cash_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _patch_tracker(monkeypatch, total_value=1_000_000.0)
    frontier = build_frontier(db_path, tmp_path, cash_usd=10_000.0)
    assert len(frontier.plans) == 1
    assert frontier.plans[0].kind == "retain_cash"
    assert frontier.plans[0].cash_retained_usd == 10_000.0
    assert frontier.eligible_tickers == ()


# --------------------------------------------------------------------------- #
# Exactly one eligible security
# --------------------------------------------------------------------------- #


def test_exactly_one_eligible_security_produces_a_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _make_eligible(db_path, tmp_path, "SOLE", list_type="portfolio", npv_per_share=150.0)
    _write_weights_cache(tmp_path, {"SOLE": 0.05}, computed_at=_iso(timedelta(hours=1)))
    _patch_tracker(monkeypatch, total_value=1_000_000.0)

    frontier = build_frontier(db_path, tmp_path, cash_usd=10_000.0)
    assert frontier.eligible_tickers == ("SOLE",)
    hr = _plan(frontier, "highest_return")
    assert hr is not None
    assert [a.ticker for a in hr.allocations] == ["SOLE"]
    retain = _plan(frontier, "retain_cash")
    assert retain is not None


# --------------------------------------------------------------------------- #
# Owner-affirmed human-capital caps
# --------------------------------------------------------------------------- #


def test_affirmed_human_capital_cap_caps_allocation_with_rationale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _make_eligible(db_path, tmp_path, "CAPX", list_type="portfolio", npv_per_share=150.0)
    _write_weights_cache(tmp_path, {"CAPX": 0.02}, computed_at=_iso(timedelta(hours=1)))
    _insert_owner_fact(
        db_path,
        key="human_capital.big_tech",
        value={"cap_pct": 5.0, "members": ["CAPX"]},
        status="affirmed",
    )
    _patch_tracker(monkeypatch, total_value=1_000_000.0)

    frontier = build_frontier(db_path, tmp_path, cash_usd=100_000.0)
    hr = _plan(frontier, "highest_return")
    assert hr is not None
    (alloc,) = hr.allocations
    assert alloc.ticker == "CAPX"
    # d_max for a 5% cap off $20k existing / $1m book = (0.05*1e6-2e4)/0.95 ~ $31,578.95
    assert alloc.dollars < 100_000.0
    assert alloc.dollars == pytest.approx(31_578.95, abs=1.0)
    assert any("affirmed human-capital cap" in f for f in hr.rationale_facts)


def test_proposed_human_capital_fact_does_not_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _make_eligible(db_path, tmp_path, "CAPX", list_type="portfolio", npv_per_share=150.0)
    _write_weights_cache(tmp_path, {"CAPX": 0.02}, computed_at=_iso(timedelta(hours=1)))
    _insert_owner_fact(
        db_path,
        key="human_capital.big_tech",
        value={"cap_pct": 5.0, "members": ["CAPX"]},
        status="proposed",
    )
    _patch_tracker(monkeypatch, total_value=1_000_000.0)

    # Cash small enough that the (still-live) zone cap doesn't bind either,
    # so a proposed-only bucket fact being ignored means the FULL cash lands.
    frontier = build_frontier(db_path, tmp_path, cash_usd=50_000.0)
    hr = _plan(frontier, "highest_return")
    assert hr is not None
    (alloc,) = hr.allocations
    assert alloc.dollars == pytest.approx(50_000.0, abs=1.0)
    assert not any("human-capital" in f for f in hr.rationale_facts)


# --------------------------------------------------------------------------- #
# Allocations + retained == cash; resulting-weight math
# --------------------------------------------------------------------------- #


def test_allocations_plus_retained_equal_cash_within_a_cent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _make_eligible(db_path, tmp_path, "AAA", list_type="portfolio", npv_per_share=200.0)
    _make_eligible(db_path, tmp_path, "BBB", list_type="portfolio", npv_per_share=110.0)
    _write_weights_cache(tmp_path, {"AAA": 0.05, "BBB": 0.03}, computed_at=_iso(timedelta(hours=1)))
    _patch_tracker(monkeypatch, total_value=1_000_000.0)

    frontier = build_frontier(db_path, tmp_path, cash_usd=25_000.0)
    for plan in frontier.plans:
        total = sum(a.dollars for a in plan.allocations) + plan.cash_retained_usd
        assert total == pytest.approx(25_000.0, abs=0.01), plan.kind


def test_resulting_weights_recompute_correctly_under_cash_funded_formula(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _make_eligible(db_path, tmp_path, "SOLE", list_type="portfolio", npv_per_share=150.0)
    _write_weights_cache(tmp_path, {"SOLE": 0.04}, computed_at=_iso(timedelta(hours=1)))
    total_value = 1_000_000.0
    _patch_tracker(monkeypatch, total_value=total_value)

    frontier = build_frontier(db_path, tmp_path, cash_usd=10_000.0)
    hr = _plan(frontier, "highest_return")
    assert hr is not None
    (alloc,) = hr.allocations
    v_i = 0.04 * total_value
    expected = (v_i + alloc.dollars) / (total_value + alloc.dollars) * 100.0
    assert alloc.resulting_weight_pct == pytest.approx(expected, abs=1e-6)


# --------------------------------------------------------------------------- #
# Zone stamped at/above the trim-assessment threshold
# --------------------------------------------------------------------------- #


def test_zone_stamped_on_allocation_at_or_above_trim_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _make_eligible(db_path, tmp_path, "BIG", list_type="portfolio", npv_per_share=150.0)
    # 13% current weight (the "concentrated" zone, next boundary at 15%) —
    # a modest add lands well above the 12% trim-assessment threshold
    # without the zone cap itself binding.
    _write_weights_cache(tmp_path, {"BIG": 0.13}, computed_at=_iso(timedelta(hours=1)))
    _patch_tracker(monkeypatch, total_value=1_000_000.0)

    frontier = build_frontier(db_path, tmp_path, cash_usd=20_000.0)
    hr = _plan(frontier, "highest_return")
    assert hr is not None
    (alloc,) = hr.allocations
    assert alloc.resulting_weight_pct >= 12.0
    assert alloc.zone is not None
    assert any("trim-assessment" in f for f in hr.rationale_facts)


# --------------------------------------------------------------------------- #
# Diversifier vs highest-return disagreement
# --------------------------------------------------------------------------- #


def test_diversifier_differs_from_highest_return_when_factors_disagree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    # PORT: best expected return (large DCF upside), held.
    _make_eligible(db_path, tmp_path, "PORT", list_type="portfolio", npv_per_share=200.0)
    # EVAL2: modest expected return but a strong candidate-fit Sharpe delta —
    # the only diversifier signal available (no price history on disk, so the
    # held-name diversification factor is hidden model-wide for both names).
    _make_eligible(db_path, tmp_path, "EVAL2", list_type="evaluation", npv_per_share=110.0)
    _write_candidate_fit_cache(
        tmp_path,
        {
            "EVAL2": {
                "fit": 1.2,
                "why": "test",
                "partial": False,
                "factors": [],
                "sharpe_delta_bps": 80.0,
            }
        },
        computed_at=_iso(timedelta(hours=1)),
    )
    _write_weights_cache(tmp_path, {"PORT": 0.05}, computed_at=_iso(timedelta(hours=1)))
    _patch_tracker(monkeypatch, total_value=1_000_000.0)

    frontier = build_frontier(db_path, tmp_path, cash_usd=10_000.0)
    assert set(frontier.eligible_tickers) == {"PORT", "EVAL2"}
    hr = _plan(frontier, "highest_return")
    div = _plan(frontier, "best_diversifier")
    assert hr is not None
    assert div is not None
    hr_ticker = hr.allocations[0].ticker
    div_ticker = div.allocations[0].ticker
    assert hr_ticker == "PORT"
    assert div_ticker == "EVAL2"
    assert hr_ticker != div_ticker


# --------------------------------------------------------------------------- #
# Degrade-don't-crash
# --------------------------------------------------------------------------- #


def test_missing_weights_cache_degrades_without_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    _make_eligible(db_path, tmp_path, "SOLE", list_type="portfolio", npv_per_share=150.0)
    # The cache FILE is present (so eligibility's own freshness check passes)
    # but carries no weight entries — the frontier's own read degrades.
    _write_weights_cache(tmp_path, {}, computed_at=_iso(timedelta(hours=1)))
    _patch_tracker(monkeypatch, total_value=1_000_000.0)

    frontier = build_frontier(db_path, tmp_path, cash_usd=10_000.0)
    assert any("weights cache" in d for d in frontier.degraded)
    # Still produces a plan — degrade-don't-crash, not degrade-and-vanish.
    hr = _plan(frontier, "highest_return")
    assert hr is not None
