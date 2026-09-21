"""Real read-path common-drawdown acceptance fixtures; no provider or LLM calls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from etf_sources.profile_evidence import capture_profile_fields
from instrument_store import upsert_etf_holdings, upsert_etf_profile
from macro_regime_playbook import INITIAL_REGIMES, REGIME_REGISTRY, evaluate_holding_regime
from models.instruments import EtfHolding, EtfProfile
from research.qualitative_stress import (
    CommonDrawdownRead,
    paired_wix_avdv_scenario,
    read_common_drawdown,
    review_summary,
)
from risk_factors import TAXONOMY, FactorLoading, compute_input_sha, persist_exposures


def test_registry_uses_actual_factor_vocabulary() -> None:
    assert {f.factor for r in INITIAL_REGIMES for f in r.factors} <= set(TAXONOMY)
    score, _, _ = evaluate_holding_regime(
        {"global travel demand": 0.1}, REGIME_REGISTRY["demand_led_recession"]
    )
    assert round(score, 3) == -0.095


def _golden(name: str, result: CommonDrawdownRead) -> None:
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "common_drawdown" / "acceptance.json").read_text()
    )
    assert {
        "state": result.state,
        "coverage_pct": result.coverage_pct,
        "top_two": [r.regime_id for r in result.top_two],
    } == expected[name]


def _seed(root: Path, db: Path, *, weights: dict[str, float], loading: float = 1) -> datetime:
    now = datetime.now(UTC).replace(microsecond=0)
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "portfolio_weights.json").write_text(
        json.dumps({"weights": weights, "computed_at": now.isoformat()})
    )
    directory = root / "micro_thesis" / "holdings"
    directory.mkdir(parents=True, exist_ok=True)
    for ticker, factor in [("WIX", "SMB web/e-commerce"), ("BKNG", "global travel demand")]:
        path = directory / f"{ticker}.json"
        path.write_text(
            json.dumps({"ticker": ticker, "thesis": "Fixture source", "key_driver": factor})
        )
        sha = compute_input_sha(
            ticker,
            geo_mix=None,
            product_mix=None,
            thesis_sha=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        persist_exposures(
            ticker,
            [FactorLoading(factor, loading, "Fixture evidence")],
            provenance="thesis_derived",
            input_sha=sha,
            db_path=db,
        )
    with sqlite3.connect(db) as conn:
        for ticker, kind in [("WIX", "equity"), ("BKNG", "equity"), ("AVDV", "etf")]:
            conn.execute(
                "INSERT OR REPLACE INTO tracked_companies(ticker,name,instrument_type,list_type) VALUES(?,?,?,'portfolio')",
                (ticker, ticker, kind),
            )
        upsert_etf_profile(
            conn,
            capture_profile_fields(
                EtfProfile(
                    ticker="AVDV",
                    profile_fetched_at=now,
                    asset_class="equity",
                    benchmark_index="Fixture small value",
                    source="fixture",
                )
            ),
        )
        upsert_etf_holdings(
            conn,
            "AVDV",
            now.date() - timedelta(days=30),
            [
                EtfHolding(
                    ticker="AVDV",
                    constituent_ticker="BKNG",
                    weight_pct=0.1,
                    as_of_date=now.date() - timedelta(days=30),
                    fetched_at=now,
                    country="GB",
                    sector="Travel",
                    source="fixture",
                ),
                EtfHolding(
                    ticker="AVDV",
                    constituent_ticker="UNKNOWN",
                    weight_pct=0.9,
                    as_of_date=now.date() - timedelta(days=30),
                    fetched_at=now,
                    country="JP",
                    source="fixture",
                ),
            ],
        )
    return now


def test_golden_full_partial_stale_unavailable_and_actual_reader(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from advisor.position_review import build_risk_context, render_risk_lines

    db = migrated_db(tmp_path / "regime.db")
    now = _seed(tmp_path, db, weights={"WIX": 0.5, "BKNG": 0.5})
    full = read_common_drawdown(db, tmp_path, now=now)
    _golden("full", full)
    assert full.state == "full" and full.coverage_pct == 100
    assert [(r.regime_id, r.before_score, r.before_rating) for r in full.top_two] == [
        ("demand_led_recession", -0.9, "highly_vulnerable"),
        ("saas_multiple_compression", -0.45, "vulnerable"),
    ]
    assert read_common_drawdown(db, tmp_path, now=now).input_sha == full.input_sha
    context = build_risk_context("WIX", db, tmp_path)
    assert context and context.common_drawdown
    assert "Demand-Led Recession" in "\n".join(render_risk_lines(context))
    (tmp_path / "micro_thesis" / "holdings" / "BKNG.json").write_text('{"thesis":"changed"}')
    partial = read_common_drawdown(db, tmp_path, now=now)
    assert (
        partial.state == "partial"
        and partial.coverage_pct == 50
        and partial.input_sha != full.input_sha
    )
    (tmp_path / "micro_thesis" / "holdings" / "WIX.json").write_text('{"thesis":"changed"}')
    stale = read_common_drawdown(db, tmp_path, now=now)
    _golden("stale", stale)
    assert stale.state == "stale" and not stale.top_two and not stale.regimes
    assert "ratings unavailable" in review_summary(stale)
    assert read_common_drawdown(db, tmp_path, now=now + timedelta(days=3)).state == "stale"
    (tmp_path / "data" / "portfolio_weights.json").unlink()
    absent = read_common_drawdown(db, tmp_path, now=now)
    _golden("unavailable", absent)
    assert absent.state == "unavailable" and not absent.top_two


def test_etf_golden_preserves_incidental_loading_and_unknown_source_recency(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "regime.db")
    now = _seed(tmp_path, db, weights={"AVDV": 1})
    result = read_common_drawdown(db, tmp_path, now=now)
    _golden("etf", result)
    assert result.state == "partial" and result.coverage_pct == 10
    etf = result.securities["AVDV"]
    assert etf.factors == {"global travel demand": 0.1}
    assert etf.context["country:GB"] == "0.100000"
    assert etf.context["asset_class"] == "equity"
    assert etf.context["benchmark_index"] == "Fixture small value"
    assert any("unverified" in reason for reason in etf.reasons)
    recession = next(r for r in result.regimes if r.regime_id == "demand_led_recession")
    assert recession.raw_score == -0.095 and recession.coverage_pct == 10
    assert recession.availability == "partial"
    before = result.input_sha
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE etf_holdings SET fetched_at=?", ((now - timedelta(days=8)).isoformat(),)
        )
    result = read_common_drawdown(db, tmp_path, now=now)
    assert result.state == "stale" and not result.top_two and result.input_sha != before


def test_paired_single_after_state_unverified_target_and_cli_read_only(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "regime.db")
    _seed(tmp_path, db, weights={"WIX": 0.1, "BKNG": 0.85, "CASH": 0.05})
    result = paired_wix_avdv_scenario(db, tmp_path, 0.045)
    _golden("paired", result)
    assert result.after_weights == {
        "WIX": 0,
        "BKNG": 0.85,
        "AVDV": 0.045,
        "CASH": pytest.approx(0.105),
    }
    assert not result.target_verified and "4.5-5.0%" in result.scenario_label
    assert len(result.top_two) == 2
    assert (
        result.top_two
        == read_common_drawdown(db, tmp_path, proposed_deltas={"WIX": -0.1, "AVDV": 0.045}).top_two
    )
    assert result.after_coverage_pct == pytest.approx(100 * (0.85 + 0.0045) / 0.895)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "execution" / "common_drawdown.py"),
        "--db-path",
        str(db),
        "--repo-root",
        str(tmp_path),
        "--paired-avdv-target",
        "0.045",
    ]
    cli = subprocess.run(command, capture_output=True, text=True, check=False)
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout)["after_weights"]["WIX"] == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not (tmp_path / "data" / "portfolio.db").exists()
    with pytest.raises(ValueError, match="sale exceeds"):
        read_common_drawdown(db, tmp_path, proposed_deltas={"WIX": -0.2})
    with pytest.raises(ValueError, match="exceeds sale"):
        read_common_drawdown(db, tmp_path, proposed_deltas={"AVDV": 0.2})
    with pytest.raises(ValueError, match="unverified"):
        paired_wix_avdv_scenario(db, tmp_path, 0.06)


def test_missing_after_coverage_cannot_masquerade_as_risk_improvement(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "regime.db")
    _seed(tmp_path, db, weights={"WIX": 1})
    result = read_common_drawdown(db, tmp_path, proposed_deltas={"WIX": -1, "UNKNOWN": 1})
    assert result.after_coverage_pct == 0 and not result.top_two
    assert "no action ranking" in result.reasons[0]


def test_profile_cannot_replace_stale_or_invalid_basket_and_no_history_is_invented(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "regime.db")
    now = _seed(tmp_path, db, weights={"AVDV": 1})
    no_history = paired_wix_avdv_scenario(db, tmp_path, 0.045)
    assert no_history.state == "unavailable" and no_history.after_weights is None
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE etf_holdings SET weight_pct=1")
    result = read_common_drawdown(db, tmp_path, now=now)
    assert result.state == "unavailable" and not result.top_two
    assert "Invalid ETF basket" in result.securities["AVDV"].reasons[0]
