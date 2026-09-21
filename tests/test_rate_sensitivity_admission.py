"""Legacy rate estimates must not leak into owner-facing conclusions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from macro_store import fetch_sensitivities
from report.sections.p3_data import load_macro_sensitivities


def test_legacy_rate_rows_are_preserved_but_quarantined(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "rates.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO macro_sensitivities(ticker,series_id,beta,r_squared,lookback_window_days,computed_at) VALUES ('NU','us_10y',999,0.9,252,'2026-09-19')"
        )
    assert fetch_sensitivities(ticker="NU", db_path=db) == []
    assert load_macro_sensitivities("NU", db_path=db) == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT beta FROM macro_sensitivities").fetchone() == (999,)


def test_versioned_estimate_roundtrip_units_history_and_consumers(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    import math
    from datetime import date, timedelta

    import pytest

    from integrations.portfolio_tracker_client import PositionCorrelationRow
    from macro_store import persist_rate_sensitivity, rate_shock_log_return
    from pipeline.portfolio_panel import rate_betas_for_rows

    db = migrated_db(tmp_path / "rates.db")
    dates = [date.today() - timedelta(days=7 * (30 - i)) for i in range(31)]
    rates = [(day, 4 + (i % 3) * 0.1 + i * 0.01) for i, day in enumerate(dates)]
    prices = [(day, 100 * math.exp(-0.2 * (level - 4))) for day, level in rates]
    row = persist_rate_sensitivity(
        ticker="NU", series_id="us_10y", ticker_prices=prices, series_points=rates, db_path=db
    )
    assert row is not None
    assert (
        persist_rate_sensitivity(
            ticker="NU", series_id="us_10y", ticker_prices=prices, series_points=rates, db_path=db
        )
        == row
    )
    estimate = fetch_sensitivities(ticker="NU", db_path=db)[0]
    from ask.packs import load_packs
    from db_paths import db_path_context
    from macro_scenarios import SCENARIOS
    from synthesis.lenses.portfolio_macro_stress import build_portfolio_macro_stress_context

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tracked_companies(ticker,name,list_type) VALUES ('NU','Synthetic','portfolio')"
        )
    pack = str(load_packs(["macro"], db_path=db, focus_tickers=["NU"]))
    assert estimate.input_sha and estimate.input_sha in pack
    assert "log return per +100 bps" in pack
    with db_path_context(db):
        context = build_portfolio_macro_stress_context(
            scenario_obj=SCENARIOS["fed_cuts_50bps"], repo_root=tmp_path
        )
    assert context is not None
    # -25 bps = -0.25pp, beta=-0.2 -> +0.05 log return (5 log-return points).
    assert "us_10y: β=-0.20→+5.0% log return" in context.template_kwargs["stress_grid"]
    assert estimate.input_sha in context.template_kwargs["stress_grid"]
    assert estimate.metric_version == "v2_rate_diff"
    assert estimate.shock_unit == "percentage_point"
    assert estimate.beta == pytest.approx(-0.2)
    assert rate_shock_log_return(estimate, basis_points=100) == pytest.approx(-0.2)
    assert rate_shock_log_return(estimate, basis_points=-50) == pytest.approx(0.1)
    # A separate historical backfill must not displace the current estimate.
    old_rates = [(day - timedelta(days=365), level) for day, level in rates]
    old_prices = [(day - timedelta(days=365), value) for day, value in prices]
    assert (
        persist_rate_sensitivity(
            ticker="NU",
            series_id="us_10y",
            ticker_prices=old_prices,
            series_points=old_rates,
            db_path=db,
        )
        != row
    )
    assert fetch_sensitivities(ticker="NU", db_path=db)[0].id == row
    report = load_macro_sensitivities("NU", db_path=db)[0]
    assert report.input_sha == estimate.input_sha
    assert report.source_as_of == date.today().isoformat()
    assert rate_betas_for_rows(
        [
            PositionCorrelationRow(
                ticker="NU",
                security_id=None,
                name=None,
                value=None,
                weight_pct=100,
                sample_size=None,
                correlation_spy=None,
                beta_spy=None,
                correlation_qqq=None,
                beta_qqq=None,
                correlation_policy=None,
                beta_policy=None,
            )
        ],
        db,
    )["NU"] == pytest.approx(-0.2)
    changed_prices = [(day, value**1.1) for day, value in prices]
    assert (
        persist_rate_sensitivity(
            ticker="NU",
            series_id="us_10y",
            ticker_prices=changed_prices,
            series_points=rates,
            db_path=db,
        )
        != row
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM macro_sensitivity_estimates").fetchone() == (3,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE macro_sensitivity_estimates SET beta=0")


def test_stale_versioned_estimates_and_legacy_snapshot_are_quarantined(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    import math
    from datetime import date, timedelta

    from macro_store import persist_rate_sensitivity
    from portfolio_risk_snapshot_store import RiskSnapshot, read_latest_snapshot, write_snapshot

    db = migrated_db(tmp_path / "rates.db")
    rates = [
        (date.today() - timedelta(days=100 + 7 * (30 - i)), 4 + (i % 3) * 0.1) for i in range(31)
    ]
    prices = [(day, 100 * math.exp(-0.2 * (level - 4))) for day, level in rates]
    assert persist_rate_sensitivity(
        ticker="NU", series_id="us_10y", ticker_prices=prices, series_points=rates, db_path=db
    )
    assert fetch_sensitivities(ticker="NU", db_path=db) == []
    assert write_snapshot(RiskSnapshot(rate_beta_10y=999), db_path=db)
    snap = read_latest_snapshot(db_path=db)
    assert snap is not None and snap.rate_beta_10y is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT rate_beta_10y FROM portfolio_risk_snapshots").fetchone() == (
            999,
        )
