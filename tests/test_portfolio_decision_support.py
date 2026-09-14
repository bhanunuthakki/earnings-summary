"""Tests for the independent Risk Budget and Portfolio Posture views."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from pipeline.portfolio_decision_support import (
    render_portfolio_posture_section,
    render_risk_budget_section,
)

# --------------------------------------------------------------------------- #


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16),
            scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
            purpose VARCHAR(64) NOT NULL,
            fiscal_period VARCHAR(10),
            content_md TEXT,
            content_json TEXT,
            input_sha256 VARCHAR(64) NOT NULL,
            output_sha256 VARCHAR(64),
            model VARCHAR(64),
            prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
            generated_at DATETIME NOT NULL,
            expires_at DATETIME,
            superseded_by_id INTEGER,
            dirty BOOLEAN NOT NULL DEFAULT 0,
            dirty_reason VARCHAR(128),
            source_doc_ids TEXT,
            parent_artifact_ids TEXT,
            llm_call_id INTEGER
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
        CREATE TABLE portfolio_risk_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'bhanu',
            captured_at TEXT NOT NULL,
            window_start TEXT, window_end TEXT, benchmark TEXT,
            beta REAL, alpha_annualized_pct REAL, sharpe REAL, sortino REAL,
            information_ratio REAL, tracking_error_annualized REAL,
            portfolio_volatility_annualized REAL, r_squared REAL,
            weighted_avg_correlation_spy REAL, num_positions INTEGER,
            top1_weight_pct REAL, top5_weight_pct REAL, top10_weight_pct REAL,
            hhi REAL, effective_holdings REAL,
            max_drawdown_pct REAL, current_drawdown_pct REAL,
            drawdown_recovered INTEGER, days_to_recovery INTEGER,
            spy_beta REAL, qqq_beta REAL, growth_tilt REAL,
            avg_correlation_spy REAL, rate_beta_10y REAL,
            names_priced INTEGER, names_total INTEGER,
            metric_version TEXT, rebase_basis TEXT,
            perf_window_start TEXT, perf_observed_from TEXT
        );
        CREATE TABLE portfolio_risk_snapshot_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'bhanu',
            captured_at TEXT NOT NULL,
            window_start TEXT, window_end TEXT, benchmark TEXT,
            beta REAL, alpha_annualized_pct REAL, sharpe REAL, sortino REAL,
            information_ratio REAL, tracking_error_annualized REAL,
            portfolio_volatility_annualized REAL, r_squared REAL,
            weighted_avg_correlation_spy REAL, num_positions INTEGER,
            top1_weight_pct REAL, top5_weight_pct REAL, top10_weight_pct REAL,
            hhi REAL, effective_holdings REAL,
            max_drawdown_pct REAL, current_drawdown_pct REAL,
            drawdown_recovered INTEGER, days_to_recovery INTEGER,
            spy_beta REAL, qqq_beta REAL, growth_tilt REAL,
            avg_correlation_spy REAL, rate_beta_10y REAL,
            names_priced INTEGER, names_total INTEGER,
            input_sha TEXT,
            metric_version TEXT, rebase_basis TEXT,
            perf_window_start TEXT, perf_observed_from TEXT
        );
        CREATE TABLE wealth_context_snapshot_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'bhanu',
            captured_at TEXT NOT NULL,
            as_of TEXT NOT NULL,
            tracker_as_of TEXT, wealthplan_as_of TEXT,
            currency TEXT NOT NULL DEFAULT 'USD',
            net_worth_total REAL, liquid_total REAL, investable_total REAL,
            home_equity REAL,
            snapshot_json TEXT NOT NULL,
            input_sha TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _seed_weights(repo_root: Path, weights: dict[str, float]) -> None:
    cache = repo_root / "data" / "portfolio_weights.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"computed_at": "2026-07-22T08:00:00", "weights": weights}),
        encoding="utf-8",
    )


def _seed_risk_snapshot(db_path: Path, *, table: str, captured_at: str, **metrics: object) -> None:
    conn = sqlite3.connect(str(db_path))
    cols = ["user_id", "captured_at", *metrics.keys()]
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        ("bhanu", captured_at, *metrics.values()),
    )
    conn.commit()
    conn.close()


def _no_hex_colors(html: str) -> bool:
    """A crude local sanity check mirroring the global guard: no raw #hex
    color literal in the emitted markup (the module never sets inline
    style="...#..." colors; kit tone comes from classes)."""
    return re.search(r"#[0-9a-fA-F]{3,8}\b", html) is None


def _no_raw_floats(html: str) -> bool:
    """No unrounded float repr survives into the emitted markup.

    The repo's UI guard (tests/test_ui_controls.py) is token- and
    component-shaped: it checks classes and CSS variables, never VALUES. That
    blind spot let every scalar metric in the Risk Budget's secondary block
    ship as a raw repr (`HHI: 1072.8133108622762`, `R²: 0.0976124298052155`).
    Any number carrying 5+ decimal places is a float that skipped its
    formatter. <style> is excluded — CSS carries its own literals.
    """
    body = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    return re.search(r"\d\.\d{5,}", body) is None


# --------------------------------------------------------------------------- #
# Risk-budget and posture sections
# --------------------------------------------------------------------------- #


def test_risk_budget_no_snapshot_state(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    html = render_risk_budget_section(db_path, tmp_path)
    assert "No risk snapshot yet" in html


def test_risk_budget_four_categories_and_delta(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _seed_weights(tmp_path, {"NU": 0.24, "MELI": 0.10})
    _seed_risk_snapshot(
        db_path,
        table="portfolio_risk_snapshot_history",
        captured_at="2026-07-15T09:00:00",
        top1_weight_pct=20.0,
        hhi=0.12,
        weighted_avg_correlation_spy=0.5,
        current_drawdown_pct=-5.0,
        max_drawdown_pct=-10.0,
        metric_version="v1",
        rebase_basis="observed",
    )
    _seed_risk_snapshot(
        db_path,
        table="portfolio_risk_snapshot_history",
        captured_at="2026-07-22T09:00:00",
        top1_weight_pct=24.0,
        hhi=0.15,
        weighted_avg_correlation_spy=0.62,
        current_drawdown_pct=-8.0,
        max_drawdown_pct=-10.0,
        metric_version="v1",
        rebase_basis="observed",
    )
    html = render_risk_budget_section(db_path, tmp_path)

    assert "1. Single-name concentration" in html
    assert "2. Correlated / shared-driver exposure" in html
    assert "3. Downside / stress" in html
    assert "4. Capacity / liquidity" in html
    assert "NU" in html
    # Matched provenance (§7.1.9): delta vs prior valid snapshot renders,
    # top1 20.0 -> 24.0 = +4.0pp.
    assert "4.0pp vs prior" in html
    assert "Secondary metrics" in html


def test_risk_budget_rounds_every_scalar_metric(tmp_path: Path) -> None:
    """Every scalar metric is rounded for display, not f-stringed raw.

    Seeds the exact prod values that surfaced the bug: the panel formatted its
    percentages via ``_pct`` but interpolated HHI / Sharpe / Sortino / Beta /
    R² / effective-holdings / tilt / rate-beta straight into the markup, so the
    owner's Risk Budget read `Sharpe: -0.3513674average...` at full float
    precision.
    """
    db_path = _make_db(tmp_path)
    _seed_weights(tmp_path, {"VTI": 0.201, "MELI": 0.147})
    _seed_risk_snapshot(
        db_path,
        table="portfolio_risk_snapshot_history",
        captured_at="2026-07-24T09:00:00",
        top1_weight_pct=20.1,
        top5_weight_pct=62.5,
        top10_weight_pct=90.4,
        hhi=1072.8133108622762,
        sharpe=-0.3513674827983,
        sortino=-0.4460646007553607,
        beta=1.4431753558196838,
        r_squared=0.0976124298052155,
        effective_holdings=9.321286284155512,
        growth_tilt=-0.47379622494585705,
        rate_beta_10y=0.018506046022587996,
        current_drawdown_pct=-7.3,
        max_drawdown_pct=-76.1,
        metric_version="v1",
        rebase_basis="observed",
    )
    html = render_risk_budget_section(db_path, tmp_path)

    assert _no_raw_floats(html), "a raw float repr reached the rendered Risk Budget"
    assert "HHI: 1,073" in html
    assert "Sharpe: -0.35" in html
    assert "Sortino: -0.45" in html
    assert "Beta: 1.44" in html
    assert "R&sup2;: 0.10" in html
    assert "Effective holdings: 9.3" in html
    assert "Growth tilt: -0.47" in html
    assert "Rate beta (10y): 0.02" in html


def test_risk_budget_absent_scalars_render_em_dash_not_zero(tmp_path: Path) -> None:
    """Rounding must not turn a NULL metric into 0.00 — §7.1's null-not-zero
    rule applies to the scalar block the same as to the percentages."""
    db_path = _make_db(tmp_path)
    _seed_risk_snapshot(
        db_path,
        table="portfolio_risk_snapshot_history",
        captured_at="2026-07-24T09:00:00",
        top1_weight_pct=20.1,
        metric_version="v1",
        rebase_basis="observed",
    )
    html = render_risk_budget_section(db_path, tmp_path)

    assert "Sharpe: —" in html
    assert "HHI: —" in html
    assert "Rate beta (10y): —" in html
    assert "Sharpe: 0.00" not in html
    assert "HHI: 0" not in html


def test_risk_budget_mismatched_provenance_suppresses_delta(tmp_path: Path) -> None:
    """PRD §7.1.9: a metric-version change must not render a false delta
    against an incomparable prior — the numeric delta is suppressed and the
    reason is surfaced instead."""
    db_path = _make_db(tmp_path)
    _seed_risk_snapshot(
        db_path,
        table="portfolio_risk_snapshot_history",
        captured_at="2026-07-15T09:00:00",
        top1_weight_pct=20.0,
        hhi=0.12,
        current_drawdown_pct=-5.0,
        max_drawdown_pct=-10.0,
        metric_version="v1",
        rebase_basis="observed",
    )
    _seed_risk_snapshot(
        db_path,
        table="portfolio_risk_snapshot_history",
        captured_at="2026-07-22T09:00:00",
        top1_weight_pct=24.0,
        hhi=0.15,
        current_drawdown_pct=-8.0,
        max_drawdown_pct=-10.0,
        metric_version="v2",  # bumped — definition changed
        rebase_basis="observed",
    )
    html = render_risk_budget_section(db_path, tmp_path)

    assert "4.0pp vs prior" not in html  # no false delta across the version change
    assert "3.0pp vs prior" not in html
    assert "metric definition changed (v1 -&gt; v2)" in html
    assert 'k-pill k-pill-warn">metric definition changed' in html


def test_risk_budget_null_metrics_render_as_em_dash_not_zero(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _seed_risk_snapshot(
        db_path,
        table="portfolio_risk_snapshot_history",
        captured_at="2026-07-22T09:00:00",
        top1_weight_pct=None,
    )
    html = render_risk_budget_section(db_path, tmp_path)
    assert "Top-1 weight: —" in html
    assert "Top-1 weight: 0" not in html
    assert "Top-1 weight: 0.0%" not in html


def test_risk_budget_stale_snapshot_visibly_stale(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _seed_risk_snapshot(
        db_path,
        table="portfolio_risk_snapshot_history",
        captured_at="2020-01-01T09:00:00",
        top1_weight_pct=10.0,
    )
    html = render_risk_budget_section(db_path, tmp_path)
    assert 'k-pill k-pill-warn">stale' in html


# --------------------------------------------------------------------------- #
# 4. Portfolio Posture — affirmed-vs-proposed phrasing
# --------------------------------------------------------------------------- #


def test_posture_no_data_state(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    html = render_portfolio_posture_section(db_path, tmp_path)
    assert "Not enough live data" in html


def test_posture_affirmed_constraints_and_actions(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _seed_weights(tmp_path, {"NU": 0.24, "MELI": 0.10})
    _seed_risk_snapshot(
        db_path,
        table="portfolio_risk_snapshots",
        captured_at="2026-07-22T09:00:00",
        growth_tilt=0.3,
        weighted_avg_correlation_spy=0.7,
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO owner_profile_facts (user_id, category, key, value_json, narrative, "
        "provenance, status, created_at) VALUES ('bhanu', 'capacity', 'human_capital.rsu', "
        "'{}', 'No more than 15% of the book in employer stock.', 'owner', 'affirmed', "
        "'2026-07-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    html = render_portfolio_posture_section(db_path, tmp_path)

    # Derived narrative: descriptive, not phrased as an owner-affirmed fact.
    assert "Your book currently reads as" in html
    assert "growth-tilted" in html
    assert "concentrated" in html
    # Affirmed facts ARE labeled as stated constraints, distinctly.
    assert "Stated constraints (affirmed)" in html
    assert "No more than 15% of the book in employer stock." in html
    # Actions: Mostly right (confirm) + Adjust (jumps into Positioning).
    assert 'id="posture-confirm"' in html
    assert 'data-console-jump="csec-positioning"' in html


def test_posture_display_only_mode_has_no_persistent_actions(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _seed_weights(tmp_path, {"NU": 0.24, "MELI": 0.10})

    html = render_portfolio_posture_section(db_path, tmp_path, include_actions=False)

    assert "Your book currently reads as" in html
    assert "governed Positioning workflow" in html
    assert 'id="posture-confirm"' not in html
    assert 'data-console-jump="csec-positioning"' not in html
    assert "/api/positioning/confirm-posture" not in html
