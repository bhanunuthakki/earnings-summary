"""C8 — drift alerts over the append-only risk-snapshot history.

Seams under test:

  * ``compute_drift_findings`` — pure baseline-vs-latest math, no DB;
  * per-metric threshold gating (spy_beta / growth_tilt / top1 / top5 /
    factor legs), including the exact-threshold boundary;
  * ``signature_key_evidence`` / ``_bucket_magnitude`` — dedup stays stable
    within a drift band and changes when the drift crosses into the next one;
  * ``load_drift_inputs`` / ``scan_and_fire`` against a real (alembic-head)
    DB — no-history and one-row-only degrade to no findings, an end-to-end
    scan fires exactly one alert and re-running dedupes it;
  * ``append_factor_vector`` — the writer-extension hook that stamps the C3
    book-level factor vector onto the latest history row, guarded absent when
    there is nothing to snapshot;
  * the 'risk_drift' trigger kind is registered in ``alerts.store.TRIGGER_KINDS``
    AND accepted by the live ``ck_alerts_trigger_kind`` CHECK constraint.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alerts.store import TRIGGER_KINDS, fire_alert
from triggers.risk_drift import (
    FACTOR_LEG_DRIFT_THRESHOLD,
    GROWTH_TILT_DRIFT_THRESHOLD,
    SPY_BETA_DRIFT_THRESHOLD,
    TICKER_SENTINEL,
    TOP1_DRIFT_THRESHOLD_PCT,
    TOP5_DRIFT_THRESHOLD_PCT,
    TRIGGER_KIND,
    DriftFinding,
    _bucket_magnitude,  # pyright: ignore[reportPrivateUsage]
    _HistoryRow,  # pyright: ignore[reportPrivateUsage]
    append_factor_vector,
    compute_drift_findings,
    load_drift_inputs,
    scan_and_fire,
    signature_key_evidence,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Pure baseline math — no DB involved
# --------------------------------------------------------------------------- #


def _row(
    captured_at: str,
    *,
    spy_beta: float | None = None,
    growth_tilt: float | None = None,
    top1: float | None = None,
    top5: float | None = None,
    factors: dict[str, float] | None = None,
) -> _HistoryRow:
    return _HistoryRow(
        captured_at=captured_at,
        metrics={
            "spy_beta": spy_beta,
            "growth_tilt": growth_tilt,
            "top1_weight_pct": top1,
            "top5_weight_pct": top5,
        },
        factor_vector=factors,
    )


def test_no_drift_below_threshold_does_not_fire() -> None:
    latest = _row("2026-07-23T00:00:00", spy_beta=1.10)
    baseline = [_row("2026-07-01T00:00:00", spy_beta=1.00)]  # |Δ|=0.10 < 0.15
    assert compute_drift_findings(latest, baseline) == []


def test_spy_beta_drift_above_threshold_fires() -> None:
    latest = _row("2026-07-23T00:00:00", spy_beta=1.30)
    baseline = [
        _row("2026-07-01T00:00:00", spy_beta=1.00),
        _row("2026-07-10T00:00:00", spy_beta=1.10),
    ]
    findings = compute_drift_findings(latest, baseline)
    assert len(findings) == 1
    f = findings[0]
    assert f.metric == "spy_beta"
    assert f.direction == "up"
    assert f.baseline_n == 2
    assert f.baseline_mean == pytest.approx(1.05)
    assert f.magnitude == pytest.approx(0.25)
    assert f.threshold == SPY_BETA_DRIFT_THRESHOLD


def test_growth_tilt_drift_direction_down() -> None:
    latest = _row("2026-07-23T00:00:00", growth_tilt=0.10)
    baseline = [_row("2026-07-01T00:00:00", growth_tilt=0.40)]  # |Δ|=0.30 >= 0.15
    findings = compute_drift_findings(latest, baseline)
    assert len(findings) == 1
    assert findings[0].metric == "growth_tilt"
    assert findings[0].direction == "down"
    assert findings[0].threshold == GROWTH_TILT_DRIFT_THRESHOLD


def test_top1_and_top5_concentration_drift_fire_independently() -> None:
    latest = _row("2026-07-23T00:00:00", top1=30.0, top5=70.0)
    baseline = [_row("2026-07-01T00:00:00", top1=24.0, top5=59.0)]  # Δ=6pp, Δ=11pp
    findings = {f.metric: f for f in compute_drift_findings(latest, baseline)}
    assert set(findings) == {"top1_weight_pct", "top5_weight_pct"}
    assert findings["top1_weight_pct"].threshold == TOP1_DRIFT_THRESHOLD_PCT
    assert findings["top5_weight_pct"].threshold == TOP5_DRIFT_THRESHOLD_PCT


def test_exact_threshold_boundary_fires() -> None:
    # magnitude == threshold is a fire (strict "< threshold" is the skip test).
    # top1_weight_pct's 5.0pp threshold and these values are exactly
    # representable in binary float, so the subtraction is exact — unlike
    # e.g. 1.15 - 1.00 for spy_beta's 0.15 threshold, which is NOT exact
    # (double-precision decimal-fraction rounding) and would make this
    # boundary assertion flaky.
    latest = _row("2026-07-23T00:00:00", top1=25.0)
    baseline = [_row("2026-07-01T00:00:00", top1=20.0)]  # |Δ|=5.0 exactly
    findings = compute_drift_findings(latest, baseline)
    assert len(findings) == 1
    assert findings[0].metric == "top1_weight_pct"
    assert findings[0].magnitude == pytest.approx(TOP1_DRIFT_THRESHOLD_PCT)


def test_metric_with_no_baseline_observations_is_skipped() -> None:
    latest = _row("2026-07-23T00:00:00", spy_beta=1.50)
    baseline = [_row("2026-07-01T00:00:00", spy_beta=None)]  # all-NULL baseline
    assert compute_drift_findings(latest, baseline) == []


def test_factor_leg_drift_fires() -> None:
    latest = _row("2026-07-23T00:00:00", factors={"Brazil consumer credit": 0.35})
    baseline = [
        _row("2026-07-01T00:00:00", factors={"Brazil consumer credit": 0.20}),
        _row("2026-07-10T00:00:00", factors={"Brazil consumer credit": 0.22}),
    ]
    findings = compute_drift_findings(latest, baseline)
    assert len(findings) == 1
    f = findings[0]
    assert f.metric == "factor:Brazil consumer credit"
    assert f.direction == "up"
    assert f.threshold == FACTOR_LEG_DRIFT_THRESHOLD
    assert f.baseline_mean == pytest.approx(0.21)


def test_factor_leg_below_threshold_does_not_fire() -> None:
    latest = _row("2026-07-23T00:00:00", factors={"AI capex/data-volume": 0.28})
    baseline = [
        _row("2026-07-01T00:00:00", factors={"AI capex/data-volume": 0.22})
    ]  # Δ=0.06 < 0.10
    assert compute_drift_findings(latest, baseline) == []


def test_factor_absent_from_baseline_is_skipped_not_treated_as_zero() -> None:
    latest = _row("2026-07-23T00:00:00", factors={"AI capex/data-volume": 0.50})
    baseline = [
        _row("2026-07-01T00:00:00", factors={"Brazil consumer credit": 0.20})
    ]  # no matching factor
    assert compute_drift_findings(latest, baseline) == []


def test_multiple_breaches_all_returned() -> None:
    latest = _row("2026-07-23T00:00:00", spy_beta=1.40, top1=30.0)
    baseline = [_row("2026-07-01T00:00:00", spy_beta=1.00, top1=20.0)]
    metrics = {f.metric for f in compute_drift_findings(latest, baseline)}
    assert metrics == {"spy_beta", "top1_weight_pct"}


# --------------------------------------------------------------------------- #
# Dedup signature — bucketed magnitude
# --------------------------------------------------------------------------- #


def test_signature_stable_within_same_magnitude_band() -> None:
    f1 = DriftFinding(
        metric="spy_beta",
        latest=1.30,
        baseline_mean=1.05,
        baseline_n=3,
        direction="up",
        magnitude=0.20,
        threshold=0.15,
    )
    f2 = DriftFinding(
        metric="spy_beta",
        latest=1.31,
        baseline_mean=1.06,
        baseline_n=4,
        direction="up",
        magnitude=0.24,
        threshold=0.15,  # same band: 0.20//0.15 == 0.24//0.15 == 1
    )
    assert signature_key_evidence(f1) == signature_key_evidence(f2)


def test_signature_changes_when_drift_crosses_into_next_band() -> None:
    f1 = DriftFinding(
        metric="spy_beta",
        latest=1.30,
        baseline_mean=1.05,
        baseline_n=3,
        direction="up",
        magnitude=0.20,
        threshold=0.15,  # band 1
    )
    f2 = DriftFinding(
        metric="spy_beta",
        latest=1.55,
        baseline_mean=1.05,
        baseline_n=3,
        direction="up",
        magnitude=0.50,
        threshold=0.15,  # band 3
    )
    assert signature_key_evidence(f1) != signature_key_evidence(f2)


def test_signature_differs_by_direction() -> None:
    up = DriftFinding(
        metric="growth_tilt",
        latest=0.5,
        baseline_mean=0.3,
        baseline_n=2,
        direction="up",
        magnitude=0.2,
        threshold=0.15,
    )
    down = DriftFinding(
        metric="growth_tilt",
        latest=0.1,
        baseline_mean=0.3,
        baseline_n=2,
        direction="down",
        magnitude=0.2,
        threshold=0.15,
    )
    assert signature_key_evidence(up) != signature_key_evidence(down)


def test_bucket_magnitude_zero_threshold_is_safe() -> None:
    assert _bucket_magnitude(1.0, 0.0) == 0


# --------------------------------------------------------------------------- #
# Registry — trigger kind lockstep
# --------------------------------------------------------------------------- #


def test_risk_drift_registered_in_trigger_kinds() -> None:
    assert TRIGGER_KIND == "risk_drift"
    assert "risk_drift" in TRIGGER_KINDS


# --------------------------------------------------------------------------- #
# DB integration — alembic head fixture (mirrors test_risk_snapshot_refresh.py)
# --------------------------------------------------------------------------- #


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def head_template(
    tmp_path_factory: pytest.TempPathFactory,
    migrated_db: Callable[..., Path],
) -> Path:
    db = tmp_path_factory.mktemp("risk_drift_tmpl") / "at_head.db"
    return migrated_db(db)


@pytest.fixture
def head_db(head_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "risk_drift.db"
    shutil.copy(head_template, db)
    return db


def _insert_history(
    db_path: Path,
    *,
    captured_at: str,
    user_id: str = "bhanu",
    spy_beta: float | None = None,
    growth_tilt: float | None = None,
    top1_weight_pct: float | None = None,
    top5_weight_pct: float | None = None,
    factor_vector: dict[str, float] | None = None,
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO portfolio_risk_snapshot_history "
            "(user_id, captured_at, spy_beta, growth_tilt, top1_weight_pct, top5_weight_pct, "
            "factor_vector_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                captured_at,
                spy_beta,
                growth_tilt,
                top1_weight_pct,
                top5_weight_pct,
                json.dumps(factor_vector) if factor_vector is not None else None,
            ),
        )
        row_id = int(cur.lastrowid or 0)
        conn.commit()
        return row_id
    finally:
        conn.close()


def test_history_table_carries_factor_vector_column_at_head(head_db: Path) -> None:
    conn = sqlite3.connect(str(head_db))
    try:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(portfolio_risk_snapshot_history)").fetchall()
        }
    finally:
        conn.close()
    assert "factor_vector_json" in cols


def test_risk_drift_trigger_kind_accepted_by_live_check_constraint(head_db: Path) -> None:
    row = fire_alert(
        ticker=TICKER_SENTINEL,
        trigger_kind="risk_drift",
        fired_at=datetime.now(UTC).replace(tzinfo=None),
        evidence_json="{}",
        signature_sha="test-sha-risk-drift",
        db_path=head_db,
    )
    assert row.trigger_kind == "risk_drift"
    assert row.ticker == "PORTFOLIO"


def test_load_drift_inputs_empty_table_returns_none(head_db: Path) -> None:
    latest, baseline = load_drift_inputs(head_db)
    assert latest is None
    assert baseline == []


def test_scan_and_fire_no_history_returns_empty(head_db: Path) -> None:
    assert scan_and_fire(head_db) == []


def test_scan_and_fire_single_row_only_returns_empty(head_db: Path) -> None:
    _insert_history(head_db, captured_at="2026-07-23T00:00:00", spy_beta=1.10)
    latest, baseline = load_drift_inputs(head_db)
    assert latest is not None
    assert baseline == []
    assert scan_and_fire(head_db) == []


def test_scan_and_fire_end_to_end_fires_and_dedupes(head_db: Path) -> None:
    _insert_history(head_db, captured_at="2026-06-24T00:00:00", spy_beta=1.00)
    _insert_history(head_db, captured_at="2026-07-05T00:00:00", spy_beta=1.02)
    _insert_history(head_db, captured_at="2026-07-23T00:00:00", spy_beta=1.30)  # big jump: latest

    fired = scan_and_fire(head_db)
    assert len(fired) == 1

    conn = sqlite3.connect(str(head_db))
    try:
        trigger_kind, ticker, evidence_raw = conn.execute(
            "SELECT trigger_kind, ticker, evidence_json FROM alerts WHERE id = ?", (fired[0],)
        ).fetchone()
    finally:
        conn.close()
    assert trigger_kind == "risk_drift"
    assert ticker == TICKER_SENTINEL
    evidence = json.loads(evidence_raw)
    assert evidence["metric"] == "spy_beta"
    assert evidence["direction"] == "up"
    assert "summary" in evidence

    # Re-run against the SAME history: same finding, same magnitude band ->
    # find_by_signature short-circuits, no duplicate alert.
    assert scan_and_fire(head_db) == []
    conn = sqlite3.connect(str(head_db))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE trigger_kind = 'risk_drift'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_scan_and_fire_factor_leg_drift(head_db: Path) -> None:
    _insert_history(
        head_db,
        captured_at="2026-06-24T00:00:00",
        spy_beta=1.10,
        factor_vector={"GLP-1/obesity reimbursement": 0.15},
    )
    _insert_history(
        head_db,
        captured_at="2026-07-23T00:00:00",
        spy_beta=1.10,  # unchanged — isolates the factor-leg breach
        factor_vector={"GLP-1/obesity reimbursement": 0.32},
    )
    fired = scan_and_fire(head_db)
    assert len(fired) == 1
    conn = sqlite3.connect(str(head_db))
    try:
        evidence_raw = conn.execute(
            "SELECT evidence_json FROM alerts WHERE id = ?", (fired[0],)
        ).fetchone()[0]
    finally:
        conn.close()
    evidence = json.loads(evidence_raw)
    assert evidence["metric"] == "factor:GLP-1/obesity reimbursement"


# --------------------------------------------------------------------------- #
# Writer extension — append_factor_vector
# --------------------------------------------------------------------------- #


def _write_weights_cache(repo_root: Path, weights: dict[str, float]) -> None:
    data_dir = repo_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "portfolio_weights.json").write_text(
        json.dumps({"computed_at": "2026-07-23T00:00:00", "weights": weights}),
        encoding="utf-8",
    )


def _insert_business_factor_exposure(
    db_path: Path, *, ticker: str, factor: str, loading: float
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO business_factor_exposures "
            "(ticker, factor, loading, rationale, provenance, input_sha, owner_edited, "
            "is_latest, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, ?)",
            (
                ticker,
                factor,
                loading,
                "test fixture",
                "segment_derived",
                "test-sha",
                "2026-07-23",
                "2026-07-23",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_append_factor_vector_noop_when_nothing_to_snapshot(head_db: Path, tmp_path: Path) -> None:
    _insert_history(head_db, captured_at="2026-07-23T00:00:00", spy_beta=1.1)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # No weights cache, no business_factor_exposures rows -> book_factor_vector
    # returns an empty vector -> nothing written.
    assert append_factor_vector(head_db, repo_root) is False


def test_append_factor_vector_writes_current_vector_onto_latest_row(
    head_db: Path, tmp_path: Path
) -> None:
    history_id = _insert_history(head_db, captured_at="2026-07-23T00:00:00", spy_beta=1.1)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_weights_cache(repo_root, {"NU": 0.20, "MELI": 0.15})
    _insert_business_factor_exposure(
        head_db, ticker="NU", factor="Brazil consumer credit", loading=0.8
    )

    assert append_factor_vector(head_db, repo_root) is True

    conn = sqlite3.connect(str(head_db))
    try:
        raw = conn.execute(
            "SELECT factor_vector_json FROM portfolio_risk_snapshot_history WHERE id = ?",
            (history_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert raw is not None
    payload = json.loads(raw)
    assert payload["Brazil consumer credit"] == pytest.approx(0.20 * 0.8)
