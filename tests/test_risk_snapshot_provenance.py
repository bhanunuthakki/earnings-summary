"""P0.1 risk truth, acceptance gap closed (PRD
``docs/design/personal_investment_partner_prd.md`` §7.1 requirement 9,
2026-07-24): "Historical comparisons use snapshots produced by the same
metric/version definition. A metric-version change must be explicit and
must not render a false delta against an incomparable prior."

Seams under test:

  * migration 0199 — adds ``metric_version``/``rebase_basis`` to both
    ``portfolio_risk_snapshots`` and ``portfolio_risk_snapshot_history``,
    backfills pre-existing rows to the current definition, and cleans up
    on downgrade;
  * ``write_snapshot`` — persists provenance on both the latest-view upsert
    and the history append, WITHOUT touching ``snapshot_input_sha`` (the
    existing content-hash dedup contract is pinned unchanged);
  * ``read_history`` / ``read_latest_snapshot`` — return the provenance;
  * ``comparable`` / ``incomparable_reason`` — the None-is-unknown matrix;
  * ``execution/refresh_portfolio_risk_snapshot.py`` — derives
    ``rebase_basis`` by comparing ``PerformanceSeries.start_date`` against
    ``.earliest_observed_date`` (NOT ``backfill_start_unreliable``, which is a
    constant False across both window shapes),
    never hardcoded.
"""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from integrations.portfolio_tracker_client import (
    BetaStats,
    Concentration,
    PerformancePoint,
    PerformanceSeries,
    PortfolioAnalytics,
    PositionCorrelationRow,
    Positioning,
)
from portfolio_risk_snapshot_store import (
    METRIC_VERSION,
    RiskSnapshot,
    comparable,
    incomparable_reason,
    read_history,
    read_latest_snapshot,
    snapshot_input_sha,
    write_snapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0197_decision_drafts"
NEW_HEAD = "0199_risk_snapshot_provenance"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def head_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("risk_prov_tmpl") / "at_head.db"
    import db as dbmod

    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved
    return db


@pytest.fixture
def head_db(head_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "risk_prov.db"
    shutil.copy(head_template, db)
    return db


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """DB built to the revision immediately BEFORE 0199, so pre-existing rows
    can be inserted without provenance columns and the backfill exercised."""
    db = tmp_path_factory.mktemp("risk_prov_prior_tmpl") / "at_prior.db"
    import db as dbmod

    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved
    return db


@pytest.fixture
def prior_db(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "risk_prov_prior.db"
    shutil.copy(prior_template, db)
    return db


# --------------------------------------------------------------------------- #
# Migration: columns added to both tables, backfill, downgrade cleanup
# --------------------------------------------------------------------------- #


def test_migration_adds_columns_to_both_tables(head_db: Path) -> None:
    conn = sqlite3.connect(str(head_db))
    try:
        latest_cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_risk_snapshots)")}
        hist_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(portfolio_risk_snapshot_history)")
        }
    finally:
        conn.close()
    assert {"metric_version", "rebase_basis"} <= latest_cols
    assert {"metric_version", "rebase_basis"} <= hist_cols


def test_migration_backfills_preexisting_rows_to_current_definition(prior_db: Path) -> None:
    conn = sqlite3.connect(str(prior_db))
    try:
        conn.execute(
            "INSERT INTO portfolio_risk_snapshots (user_id, captured_at, beta) "
            "VALUES ('bhanu', '2026-07-01T00:00:00', 1.1)"
        )
        conn.execute(
            "INSERT INTO portfolio_risk_snapshot_history (user_id, captured_at, beta) "
            "VALUES ('bhanu', '2026-07-01T00:00:00', 1.1)"
        )
        conn.commit()
    finally:
        conn.close()

    cfg = _build_config(prior_db)
    command.upgrade(cfg, NEW_HEAD)

    conn = sqlite3.connect(str(prior_db))
    try:
        latest_row = conn.execute(
            "SELECT metric_version, rebase_basis FROM portfolio_risk_snapshots"
        ).fetchone()
        hist_row = conn.execute(
            "SELECT metric_version, rebase_basis FROM portfolio_risk_snapshot_history"
        ).fetchone()
    finally:
        conn.close()
    # Backfilled to the CURRENT definition — not NULL (NULL would mean
    # "unknown", but every pre-existing row was in fact produced by v1/observed).
    assert latest_row == ("v1", "observed")
    assert hist_row == ("v1", "observed")


def test_migration_downgrade_removes_columns_cleanly(head_db: Path) -> None:
    conn = sqlite3.connect(str(head_db))
    conn.execute(
        "INSERT INTO portfolio_risk_snapshots (user_id, captured_at, beta, metric_version, "
        "rebase_basis) VALUES ('bhanu', 'x', 1.0, 'v1', 'observed')"
    )
    conn.commit()
    conn.close()

    cfg = _build_config(head_db)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(head_db))
    try:
        latest_cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_risk_snapshots)")}
        hist_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(portfolio_risk_snapshot_history)")
        }
    finally:
        conn.close()
    assert "metric_version" not in latest_cols and "rebase_basis" not in latest_cols
    assert "metric_version" not in hist_cols and "rebase_basis" not in hist_cols

    # Upgrading back re-adds the columns and re-backfills (idempotent guard).
    command.upgrade(cfg, NEW_HEAD)
    conn = sqlite3.connect(str(head_db))
    try:
        row = conn.execute(
            "SELECT metric_version, rebase_basis FROM portfolio_risk_snapshots"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("v1", "observed")


# --------------------------------------------------------------------------- #
# write_snapshot / read_history / read_latest_snapshot persist + return provenance
# --------------------------------------------------------------------------- #


def test_write_snapshot_persists_provenance_on_latest_and_history(head_db: Path) -> None:
    snap = RiskSnapshot(beta=1.1, top1_weight_pct=20.0)
    assert write_snapshot(
        snap, db_path=head_db, metric_version="v1", rebase_basis="modeled_backfill"
    )
    latest = read_latest_snapshot(db_path=head_db)
    assert latest is not None
    assert latest.metric_version == "v1"
    assert latest.rebase_basis == "modeled_backfill"
    hist = read_history(db_path=head_db)
    assert len(hist) == 1
    assert hist[0].metric_version == "v1"
    assert hist[0].rebase_basis == "modeled_backfill"


def test_write_snapshot_defaults_metric_version_and_none_rebase_basis(head_db: Path) -> None:
    snap = RiskSnapshot(beta=0.9)
    assert write_snapshot(snap, db_path=head_db)  # no explicit provenance kwargs
    latest = read_latest_snapshot(db_path=head_db)
    assert latest is not None
    assert latest.metric_version == METRIC_VERSION
    assert latest.rebase_basis is None


# --------------------------------------------------------------------------- #
# input_sha is UNCHANGED by the new fields — pins the dedup contract
# --------------------------------------------------------------------------- #


def test_input_sha_unaffected_by_metric_version_or_rebase_basis() -> None:
    a = RiskSnapshot(beta=1.1, top1_weight_pct=20.0, metric_version="v1", rebase_basis="observed")
    b = RiskSnapshot(
        beta=1.1, top1_weight_pct=20.0, metric_version="v2", rebase_basis="modeled_backfill"
    )
    assert snapshot_input_sha(a) == snapshot_input_sha(b)


def test_history_dedupes_across_a_metric_version_change(head_db: Path) -> None:
    """Same numbers, different metric_version: still ONE history row (content
    hash is what dedupe keys on, not provenance) — the explicit columns are
    what make the two writes distinguishable for comparability, not for
    dedup. This is the exact contract the task calls out as easy to break."""
    snap = RiskSnapshot(beta=1.3, top1_weight_pct=22.0)
    assert write_snapshot(snap, db_path=head_db, metric_version="v1")
    assert write_snapshot(snap, db_path=head_db, metric_version="v2")  # same content
    conn = sqlite3.connect(str(head_db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM portfolio_risk_snapshot_history").fetchone()[0]
    finally:
        conn.close()
    assert n == 1  # deduped by content, exactly as before this change


# --------------------------------------------------------------------------- #
# comparable() / incomparable_reason() — the None-is-unknown matrix
# --------------------------------------------------------------------------- #


def test_comparable_matches_on_identical_provenance() -> None:
    a = RiskSnapshot(metric_version="v1", rebase_basis="observed")
    b = RiskSnapshot(metric_version="v1", rebase_basis="observed")
    assert comparable(a, b) is True
    assert incomparable_reason(a, b) is None


def test_comparable_false_on_metric_version_mismatch() -> None:
    a = RiskSnapshot(metric_version="v2", rebase_basis="observed")
    b = RiskSnapshot(metric_version="v1", rebase_basis="observed")
    assert comparable(a, b) is False
    assert incomparable_reason(a, b) == "metric definition changed (v1 -> v2)"


def test_comparable_false_on_rebase_basis_mismatch() -> None:
    a = RiskSnapshot(metric_version="v1", rebase_basis="modeled_backfill")
    b = RiskSnapshot(metric_version="v1", rebase_basis="observed")
    assert comparable(a, b) is False
    assert (
        incomparable_reason(a, b) == "analytics window basis changed (observed -> modeled_backfill)"
    )


def test_comparable_false_when_one_side_unknown() -> None:
    a = RiskSnapshot(metric_version="v1", rebase_basis="observed")
    b = RiskSnapshot(metric_version=None, rebase_basis=None)
    assert comparable(a, b) is False
    assert comparable(b, a) is False
    assert incomparable_reason(a, b) is not None


def test_comparable_false_when_both_sides_unknown() -> None:
    """None-vs-None is NOT comparable — two unknown definitions might differ
    from each other; treating them as a match would be the exact false-delta
    risk this function exists to prevent."""
    a = RiskSnapshot(metric_version=None, rebase_basis=None)
    b = RiskSnapshot(metric_version=None, rebase_basis=None)
    assert comparable(a, b) is False
    assert incomparable_reason(a, b) is not None


# --------------------------------------------------------------------------- #
# Refresh script derives rebase_basis from the tracker's own signal
# --------------------------------------------------------------------------- #


def _load_refresh_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "refresh_portfolio_risk_snapshot_prov",
        PROJECT_ROOT / "execution" / "refresh_portfolio_risk_snapshot.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _canned_analytics(
    *, earliest_observed_date: str | None, start_date: str | None = "2026-01-23"
) -> PortfolioAnalytics:
    points = [
        PerformancePoint(
            date="2026-07-01",
            portfolio_return_pct=0.0,
            spy_return_pct=0.0,
            qqq_return_pct=0.0,
            policy_return_pct=None,
        ),
        PerformancePoint(
            date="2026-07-23",
            portfolio_return_pct=5.0,
            spy_return_pct=3.0,
            qqq_return_pct=4.0,
            policy_return_pct=None,
        ),
    ]
    corr = [
        PositionCorrelationRow(
            security_id=i,
            ticker=t,
            name=t,
            value=10_000.0,
            weight_pct=w,
            sample_size=250,
            correlation_spy=0.7,
            beta_spy=1.1,
            correlation_qqq=0.75,
            beta_qqq=1.2,
            correlation_policy=None,
            beta_policy=None,
        )
        for i, (t, w) in enumerate([("NU", 40.0), ("MELI", 30.0), ("NOW", 30.0)])
    ]
    return PortfolioAnalytics(
        available=True,
        api_url="http://127.0.0.1:8000",
        performance=PerformanceSeries(
            start_date=start_date,
            end_date="2026-07-23",
            base_value=100_000.0,
            net_external_cashflow_in=0.0,
            # Constant False in the real API across observed AND
            # heavily-reconstructed series — pinned False here so a test
            # can never accidentally derive the basis from it.
            backfill_start_unreliable=False,
            points=points,
            earliest_observed_date=earliest_observed_date,
        ),
        positioning=Positioning(
            snapshot_date="2026-07-23",
            total_value=100_000.0,
            concentration=Concentration(
                num_positions=3,
                top1_weight_pct=40.0,
                top5_weight_pct=100.0,
                top10_weight_pct=100.0,
                hhi=3400.0,
                effective_holdings=2.9,
            ),
            weighted_avg_correlation_spy=0.71,
            correlations=corr,
        ),
        beta=BetaStats(
            benchmark="SPY",
            start_date="2026-01-23",
            end_date="2026-07-23",
            sample_size=125,
            risk_free_annual=0.04,
            beta=1.12,
            alpha_annualized_pct=3.4,
            alpha_t_stat=None,
            alpha_std_error_annualized_pct=None,
            alpha_significant=None,
            r_squared=0.62,
            correlation=0.79,
            sharpe=1.1,
            sortino=1.4,
            information_ratio=0.5,
            portfolio_volatility_annualized=0.22,
            benchmark_volatility_annualized=0.15,
            tracking_error_annualized=0.09,
        ),
    )


def test_refresh_script_stamps_observed_when_window_starts_at_first_observation(
    head_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_refresh_module()
    monkeypatch.setattr(
        mod,
        "fetch_portfolio_analytics",
        lambda **_: _canned_analytics(start_date="2026-01-23", earliest_observed_date="2026-01-23"),
    )
    assert mod.main(["--db-path", str(head_db)]) == 0
    latest = read_latest_snapshot(db_path=head_db)
    assert latest is not None
    assert latest.rebase_basis == "observed"
    assert latest.metric_version == METRIC_VERSION


def test_refresh_script_stamps_modeled_backfill_when_window_precedes_observation(
    head_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_refresh_module()
    monkeypatch.setattr(
        mod,
        "fetch_portfolio_analytics",
        lambda **_: _canned_analytics(start_date="2025-07-24", earliest_observed_date="2026-05-09"),
    )
    assert mod.main(["--db-path", str(head_db)]) == 0
    latest = read_latest_snapshot(db_path=head_db)
    assert latest is not None
    assert latest.rebase_basis == "modeled_backfill"


def test_refresh_script_summary_json_includes_provenance(
    head_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    mod = _load_refresh_module()
    monkeypatch.setattr(
        mod,
        "fetch_portfolio_analytics",
        lambda **_: _canned_analytics(start_date="2025-07-24", earliest_observed_date="2026-05-09"),
    )
    assert mod.main(["--db-path", str(head_db)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["metric_version"] == METRIC_VERSION
    assert out["rebase_basis"] == "modeled_backfill"


def test_refresh_script_stamps_unknown_when_provider_omits_observation_marker(
    head_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No marker means the basis is genuinely indeterminate. Defaulting to
    'observed' would assert a guarantee nobody verified."""
    mod = _load_refresh_module()
    monkeypatch.setattr(
        mod,
        "fetch_portfolio_analytics",
        lambda **_: _canned_analytics(earliest_observed_date=None),
    )
    assert mod.main(["--db-path", str(head_db)]) == 0
    latest = read_latest_snapshot(db_path=head_db)
    assert latest is not None
    assert latest.rebase_basis == "unknown"


def test_backfill_flag_alone_never_decides_the_basis() -> None:
    """Regression pin for the defect this derivation replaced: the live API
    returns backfill_start_unreliable=False for an observed window AND for a
    walk-back-filled one, so a basis derived from it would be constant. Both
    fixtures below carry the SAME flag value and must still classify
    differently."""
    observed = _canned_analytics(start_date="2026-01-23", earliest_observed_date="2026-01-23")
    walk_back = _canned_analytics(start_date="2025-07-24", earliest_observed_date="2026-05-09")
    assert observed.performance is not None and walk_back.performance is not None
    assert (
        observed.performance.backfill_start_unreliable
        == walk_back.performance.backfill_start_unreliable
    )


def test_two_unknown_basis_captures_are_not_comparable() -> None:
    """Two admissions of ignorance must not compare equal — they may rest on
    different bases, which is the false delta this guard exists to stop."""
    a = RiskSnapshot(metric_version="v1", rebase_basis="unknown")
    b = RiskSnapshot(metric_version="v1", rebase_basis="unknown")
    assert comparable(a, b) is False
    assert incomparable_reason(a, b) == "analytics window basis is unrecorded for these captures"
