"""Offline risk snapshot (L5 PR2): migration + store + degraded render.

The whole-book risk surface caches its last successful tracker read so the Risk
+ Performance panels render stamped cached values when :8000 is offline, instead
of going blank. These cover the migration (real chain), the single-row upsert
store, the persist-on-successful-fetch wiring, and the offline cached render.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from integrations.portfolio_tracker_client import (  # noqa: E402
    BetaStats,
    Concentration,
    LivePortfolio,
    PerformancePoint,
    PerformanceSeries,
    PortfolioAnalytics,
    PositionCorrelationRow,
    Positioning,
)
from portfolio_risk_snapshot_store import (  # noqa: E402
    RiskSnapshot,
    read_latest_snapshot,
    write_snapshot,
)

PRIOR_HEAD = "0104_ask_answer_budget"
NEW_HEAD = "0105_portfolio_risk_snapshot"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One DB built to the real chain head (init_db + alembic upgrade) so the
    migration runs against the production schema, shared across the module."""
    db = tmp_path_factory.mktemp("risk_snap_tmpl") / "at_head.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, NEW_HEAD)
    return db


@pytest.fixture
def migrated_db(migrated_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "risk_snap.db"
    shutil.copy(migrated_template, db)
    return db


# ----- migration -----


def test_migration_creates_single_row_table(migrated_db: Path) -> None:
    conn = sqlite3.connect(str(migrated_db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_risk_snapshots)")}
    assert {
        "user_id",
        "captured_at",
        "beta",
        "max_drawdown_pct",
        "spy_beta",
        "rate_beta_10y",
    } <= cols
    # UNIQUE(user_id) — the single-row-per-user contract.
    conn.execute(
        "INSERT INTO portfolio_risk_snapshots (user_id, captured_at, beta) VALUES ('bhanu', 'x', 1.0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO portfolio_risk_snapshots (user_id, captured_at, beta) VALUES ('bhanu', 'y', 2.0)"
        )
    conn.close()


def test_migration_downgrades(migrated_db: Path) -> None:
    cfg = _build_config(migrated_db)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(migrated_db))
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='portfolio_risk_snapshots'"
    ).fetchone()
    conn.close()
    assert present is None


# ----- store -----


def test_store_upsert_roundtrip(migrated_db: Path) -> None:
    snap = RiskSnapshot(
        benchmark="SPY",
        beta=1.12,
        sharpe=0.85,
        portfolio_volatility_annualized=0.18,
        max_drawdown_pct=-14.2,
        current_drawdown_pct=-3.1,
        spy_beta=1.25,
        qqq_beta=1.4,
        rate_beta_10y=-2.0,
        num_positions=11,
        top5_weight_pct=45.0,
    )
    assert write_snapshot(snap, db_path=migrated_db) is True
    got = read_latest_snapshot(db_path=migrated_db)
    assert got is not None
    assert got.beta == pytest.approx(1.12)
    assert got.max_drawdown_pct == pytest.approx(-14.2)
    assert got.spy_beta == pytest.approx(1.25)
    assert got.rate_beta_10y == pytest.approx(-2.0)
    assert got.num_positions == 11
    assert got.captured_at  # stamped at write time
    # Second write overwrites (single row per user), never appends.
    assert write_snapshot(RiskSnapshot(beta=1.5), db_path=migrated_db) is True
    got2 = read_latest_snapshot(db_path=migrated_db)
    assert got2 is not None and got2.beta == pytest.approx(1.5)
    assert got2.spy_beta is None  # the fresh row replaced the prior one wholesale
    conn = sqlite3.connect(str(migrated_db))
    assert conn.execute("SELECT COUNT(*) FROM portfolio_risk_snapshots").fetchone()[0] == 1
    conn.close()


def test_store_degrades_without_db_or_table(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert write_snapshot(RiskSnapshot(beta=1.0), db_path=missing) is False
    assert read_latest_snapshot(db_path=missing) is None
    # DB exists but lacks the table → still no raise.
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()
    assert write_snapshot(RiskSnapshot(beta=1.0), db_path=bare) is False
    assert read_latest_snapshot(db_path=bare) is None


# ----- persist on a successful fetch -----


def _populated_analytics() -> PortfolioAnalytics:
    perf = PerformanceSeries(
        start_date="2025-06-10",
        end_date="2026-06-10",
        base_value=100000.0,
        net_external_cashflow_in=0.0,
        backfill_start_unreliable=False,
        points=[
            PerformancePoint("2025-06-10", 0.0, None, None, None),
            PerformancePoint("2026-01-01", 10.0, None, None, None),
            PerformancePoint("2026-03-01", -5.0, None, None, None),  # underwater
        ],
        calculation_status="available",
    )
    pos = Positioning(
        snapshot_date="2026-06-10",
        total_value=100000.0,
        concentration=Concentration(
            num_positions=2,
            top1_weight_pct=60.0,
            top5_weight_pct=100.0,
            top10_weight_pct=100.0,
            hhi=5200.0,
            effective_holdings=1.9,
        ),
        weighted_avg_correlation_spy=0.7,
        has_policy=True,
        correlations=[
            PositionCorrelationRow(1, "NU", "Nu", 60.0, 60.0, 250, 0.6, 1.4, 0.7, 1.2, None, None),
            PositionCorrelationRow(
                2, "AAPL", "Apple", 40.0, 40.0, 250, 0.9, 1.0, 0.8, 1.5, None, None
            ),
        ],
    )
    beta = BetaStats(
        benchmark="SPY",
        start_date="2025-06-10",
        end_date="2026-06-10",
        sample_size=250,
        risk_free_annual=0.04,
        beta=1.12,
        alpha_annualized_pct=2.5,
        alpha_t_stat=None,
        alpha_std_error_annualized_pct=None,
        alpha_significant=None,
        r_squared=0.82,
        correlation=0.9,
        sharpe=0.85,
        sortino=1.2,
        information_ratio=0.4,
        portfolio_volatility_annualized=0.18,
        benchmark_volatility_annualized=0.15,
        tracking_error_annualized=0.08,
        notes=[],
        calculation_status="available",
    )
    return PortfolioAnalytics(
        available=True, api_url="http://x", performance=perf, positioning=pos, beta=beta
    )


def test_risk_panel_persists_snapshot_on_successful_fetch(
    migrated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pipeline.portfolio_panel as pp

    # Wave B (B4b): a liveness probe now gates the data walk — mark it up so
    # the patched fetcher is reached.
    def probe_tracker(_api_url: str | None = None) -> tuple[bool, str]:
        return True, "http://x"

    def fetch_analytics(**_kwargs: object) -> PortfolioAnalytics:
        return _populated_analytics()

    monkeypatch.setattr(pp, "probe_tracker", probe_tracker)
    monkeypatch.setattr(pp, "fetch_portfolio_analytics", fetch_analytics)
    html = pp.render_portfolio_risk_panel(api_url="http://x", db_path=migrated_db)
    assert "Factor &amp; style exposure" in html  # live render
    snap = read_latest_snapshot(db_path=migrated_db)
    assert snap is not None
    assert snap.beta == pytest.approx(1.12)
    assert snap.spy_beta == pytest.approx((60 * 1.4 + 40 * 1.0) / 100)  # value-weighted
    assert snap.max_drawdown_pct is not None and snap.max_drawdown_pct < 0  # the -5% leg
    assert snap.num_positions == 2


# ----- offline render of the cached snapshot -----


def test_risk_page_renders_cached_snapshot_offline() -> None:
    from pipeline.portfolio_panel import compose_risk_page

    analytics = PortfolioAnalytics(
        available=False, api_url="http://x", errors={"beta": "ConnectionError: down"}
    )
    snap = RiskSnapshot(
        captured_at="2026-06-12T06:00:00",
        benchmark="SPY",
        beta=1.12,
        sharpe=0.85,
        max_drawdown_pct=-14.2,
        spy_beta=1.25,
        qqq_beta=1.4,
        top5_weight_pct=45.0,
    )
    html = compose_risk_page(
        analytics,
        drawdown=None,
        factor=None,
        scenarios=[("oil_to_50", "Brent crashes to ~$50/bbl")],
        digest="",
        snapshot=snap,
    )
    assert "last-known snapshot" in html and "cached values, not live" in html
    assert "1.12" in html and "1.25" in html
    assert "Max drawdown" in html and "-14.2%" in html
    assert "Whole-book macro stress" in html  # still renders offline
    # No live underwater chart (no live drawdown when offline).
    assert 'class="pfr-uw"' not in html


def test_performance_page_shows_cached_risk_when_analytics_down() -> None:
    from pipeline.portfolio_panel import compose_portfolio_page

    analytics = PortfolioAnalytics(available=False, api_url="http://x", errors={})
    live = LivePortfolio(available=False, api_url="http://x", error="ConnectionError: nope")
    snap = RiskSnapshot(
        captured_at="2026-06-12T06:00:00", benchmark="SPY", beta=1.12, max_drawdown_pct=-14.2
    )
    html = compose_portfolio_page(analytics, live, snapshot=snap)
    assert "Risk &amp; drawdown" in html and "1.12" in html  # the cached recap
    assert "last-known snapshot" in html
    assert "offline-tech" in html  # the live offline note still rides below
    # No snapshot → no cached recap (back to the pre-PR2 shape).
    bare = compose_portfolio_page(analytics, live)
    assert "Risk &amp; drawdown" not in bare
