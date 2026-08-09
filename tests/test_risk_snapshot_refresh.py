"""P0.1 risk truth (PRD §7.1): the authoritative scheduled risk writer.

Seams under test:

  * ``snapshot_input_sha`` — content hash independent of write-time stamps;
  * ``write_snapshot`` — identical content dedupes in history (0186 unique
    index + INSERT OR IGNORE) while the latest-view upsert still refreshes;
  * ``RiskBudgetSnapshot`` — the §7.1.5 validation matrix;
  * ``execution/refresh_portfolio_risk_snapshot.py`` — end-to-end: a valid
    capture writes latest + one history row and re-runs dedupe; an invalid
    capture writes NOTHING and fires the data_feed_stale dead-man.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import pytest

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
    RiskBudgetSnapshot,
    RiskSnapshot,
    history_has_sha,
    read_history,
    read_latest_snapshot,
    snapshot_input_sha,
    write_snapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _RefreshModule(Protocol):
    fetch_portfolio_analytics: Callable[..., PortfolioAnalytics]

    def main(self, argv: list[str] | None = None) -> int: ...


@pytest.fixture
def head_db(migrated_db: Callable[..., Path], tmp_path: Path) -> Path:
    """Use the shared active-head template so writer preflight is hermetic."""
    return migrated_db(tmp_path / "risk_refresh.db")


# --------------------------------------------------------------------------- #
# Content-hash idempotency
# --------------------------------------------------------------------------- #
def test_input_sha_ignores_captured_at_and_tracks_content() -> None:
    a = RiskSnapshot(beta=1.1, top1_weight_pct=20.0)
    b = RiskSnapshot(beta=1.1, top1_weight_pct=20.0)
    b.captured_at = "2001-01-01T00:00:00"
    assert snapshot_input_sha(a) == snapshot_input_sha(b)
    c = RiskSnapshot(beta=1.2, top1_weight_pct=20.0)
    assert snapshot_input_sha(a) != snapshot_input_sha(c)


def test_identical_content_dedupes_in_history(head_db: Path) -> None:
    snap = RiskSnapshot(beta=1.1, top1_weight_pct=20.0, max_drawdown_pct=-12.0)
    assert write_snapshot(snap, db_path=head_db) is True
    assert write_snapshot(snap, db_path=head_db) is True  # same content re-run
    conn = sqlite3.connect(str(head_db))
    try:
        n_hist = conn.execute("SELECT COUNT(*) FROM portfolio_risk_snapshot_history").fetchone()[0]
        n_latest = conn.execute("SELECT COUNT(*) FROM portfolio_risk_snapshots").fetchone()[0]
    finally:
        conn.close()
    assert n_hist == 1  # deduped — not a duplicate row
    assert n_latest == 1
    # A genuinely different capture still appends.
    assert write_snapshot(RiskSnapshot(beta=1.4), db_path=head_db) is True
    assert len(read_history(db_path=head_db)) == 2


def test_history_has_sha(head_db: Path) -> None:
    snap = RiskSnapshot(beta=0.9)
    sha = snapshot_input_sha(snap)
    assert history_has_sha(sha, db_path=head_db) is False
    write_snapshot(snap, db_path=head_db)
    assert history_has_sha(sha, db_path=head_db) is True


# --------------------------------------------------------------------------- #
# §7.1.5 validation matrix
# --------------------------------------------------------------------------- #
def _valid_budget(**overrides: object) -> RiskBudgetSnapshot:
    base: dict[str, object] = {
        "as_of": "2026-07-23",
        "num_positions": 11,
        "weights_pct": (20.0, 15.0, 12.0, 10.0, 8.0, 7.0, 6.0, 5.0, 5.0, 4.0, 3.0),
        "source_freshness": {"tracker_analytics": "2026-07-23"},
        "source_errors": (),
    }
    base.update(overrides)
    return RiskBudgetSnapshot.model_validate(base)


def _substantive_snap() -> RiskSnapshot:
    return RiskSnapshot(
        beta=1.1,
        top1_weight_pct=20.0,
        hhi=1400.0,
        effective_holdings=7.1,
        max_drawdown_pct=-18.0,
        current_drawdown_pct=-3.0,
        window_end="2026-07-23",
    )


def test_valid_capture_passes() -> None:
    assert _valid_budget().validate_against(_substantive_snap()) == []


def test_null_concentration_rejected() -> None:
    snap = _substantive_snap()
    snap.top1_weight_pct = None
    snap.hhi = None
    snap.effective_holdings = None
    assert any("concentration" in r for r in _valid_budget().validate_against(snap))


def test_null_downside_rejected() -> None:
    snap = _substantive_snap()
    snap.max_drawdown_pct = None
    snap.current_drawdown_pct = None
    assert any("downside" in r for r in _valid_budget().validate_against(snap))


def test_fraction_weights_mixup_rejected() -> None:
    budget = _valid_budget(weights_pct=(0.20, 0.15, 0.12))  # fractions, not percent
    assert any("normalized" in r for r in budget.validate_against(_substantive_snap()))


def test_missing_freshness_rejected() -> None:
    budget = _valid_budget(source_freshness={})
    assert any("freshness" in r for r in budget.validate_against(_substantive_snap()))


def test_zero_positions_rejected_by_schema() -> None:
    with pytest.raises(Exception):
        _valid_budget(num_positions=0)


# --------------------------------------------------------------------------- #
# End-to-end: the execution script
# --------------------------------------------------------------------------- #
def _load_refresh_module() -> _RefreshModule:
    spec = importlib.util.spec_from_file_location(
        "refresh_portfolio_risk_snapshot",
        PROJECT_ROOT / "execution" / "refresh_portfolio_risk_snapshot.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return cast(_RefreshModule, mod)


def _returning(result: PortfolioAnalytics) -> Callable[..., PortfolioAnalytics]:
    def fetch(**_kwargs: object) -> PortfolioAnalytics:
        return result

    return fetch


def _canned_analytics() -> PortfolioAnalytics:
    points = [
        PerformancePoint(
            date="2026-07-01",
            portfolio_return_pct=0.0,
            spy_return_pct=0.0,
            qqq_return_pct=0.0,
            policy_return_pct=None,
        ),
        PerformancePoint(
            date="2026-07-10",
            portfolio_return_pct=8.0,
            spy_return_pct=4.0,
            qqq_return_pct=5.0,
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
        for i, (t, w) in enumerate(
            [
                ("NU", 20.0),
                ("MELI", 18.0),
                ("NOW", 15.0),
                ("GOOGL", 14.0),
                ("META", 12.0),
                ("VEEV", 11.0),
                ("WIX", 10.0),
            ]
        )
    ]
    return PortfolioAnalytics(
        available=True,
        api_url="http://127.0.0.1:8000",
        performance=PerformanceSeries(
            start_date="2026-01-23",
            end_date="2026-07-23",
            base_value=100_000.0,
            net_external_cashflow_in=0.0,
            backfill_start_unreliable=False,
            points=points,
        ),
        positioning=Positioning(
            snapshot_date="2026-07-23",
            total_value=100_000.0,
            concentration=Concentration(
                num_positions=7,
                top1_weight_pct=20.0,
                top5_weight_pct=79.0,
                top10_weight_pct=100.0,
                hhi=1520.0,
                effective_holdings=6.6,
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


def test_refresh_script_writes_validates_and_dedupes(
    head_db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_refresh_module()
    monkeypatch.setattr(mod, "fetch_portfolio_analytics", _returning(_canned_analytics()))
    assert mod.main(["--db-path", str(head_db)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "ok"
    latest = read_latest_snapshot(db_path=head_db)
    assert latest is not None and latest.beta == pytest.approx(1.12)
    assert len(read_history(db_path=head_db)) == 1
    # Same input re-run: exit 0, no duplicate history row (§7.1 acceptance).
    assert mod.main(["--db-path", str(head_db)]) == 0
    assert len(read_history(db_path=head_db)) == 1


def test_refresh_script_invalid_capture_writes_nothing(
    head_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_refresh_module()
    write_snapshot(RiskSnapshot(beta=0.8), db_path=head_db)  # the standing last-valid
    degenerate = PortfolioAnalytics(
        available=True, api_url="http://x", performance=None, positioning=None
    )
    monkeypatch.setattr(mod, "fetch_portfolio_analytics", _returning(degenerate))
    assert mod.main(["--db-path", str(head_db)]) == 1
    latest = read_latest_snapshot(db_path=head_db)
    assert latest is not None and latest.beta == pytest.approx(0.8)  # untouched
    assert len(read_history(db_path=head_db)) == 1
    # The explicit failed-ingestion event: the data_feed_stale dead-man fired.
    from alerts import store as alerts_store

    conn = sqlite3.connect(str(head_db))
    try:
        n_alerts = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE trigger_kind = 'data_feed_stale'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n_alerts == 1
    assert alerts_store is not None  # imported cleanly against the head schema


def test_refresh_script_tracker_down_exits_nonzero(
    head_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_refresh_module()
    down = PortfolioAnalytics(available=False, api_url="http://x", errors={"beta": "refused"})
    monkeypatch.setattr(mod, "fetch_portfolio_analytics", _returning(down))
    assert mod.main(["--db-path", str(head_db)]) == 1
    assert read_latest_snapshot(db_path=head_db) is None  # nothing invented
