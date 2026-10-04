"""Tests for src/portfolio_weights.py — the materialized weight cache the inbox
render reads instead of the live tracker (directive S12 latency fix)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import portfolio_weights as pw
from integrations.portfolio_tracker_client import LivePortfolio, LivePosition


def _portfolio(available: bool, pct: dict[str, float | None]) -> LivePortfolio:
    return LivePortfolio(
        available=available,
        api_url="http://test",
        positions=[
            LivePosition(
                ticker=t,
                name=t,
                quantity=1.0,
                market_value=None,
                cost_basis=None,
                unrealized_pnl=None,
                percent_of_portfolio=p,
            )
            for t, p in pct.items()
        ],
    )


def test_weights_from_portfolio_percent_to_fraction() -> None:
    p = _portfolio(True, {"NU": 20.0, "meli": 2.5, "ZERO": 0.0, "NONE": None})
    w = pw.weights_from_portfolio(p)
    assert w == {"NU": 0.20, "MELI": 0.025, "ZERO": 0.0}  # upper-cased, /100; None skipped


def test_weights_from_portfolio_clamps_negative() -> None:
    p = _portfolio(True, {"NEG": -5.0})
    assert pw.weights_from_portfolio(p) == {"NEG": 0.0}


def test_materialize_then_read_round_trip(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    n = pw.materialize_weights(tmp_path, _portfolio(True, {"NU": 20.0, "MELI": 5.0}))
    assert n == 2
    assert pw.read_materialized_weights(tmp_path) == {"NU": 0.20, "MELI": 0.05}
    # The payload carries a computed_at stamp + the weights map.
    payload = json.loads((tmp_path / "data" / "portfolio_weights.json").read_text())
    assert "computed_at" in payload and payload["weights"]["NU"] == 0.20


def test_read_absent_cache_is_empty(tmp_path: Path) -> None:
    assert pw.read_materialized_weights(tmp_path) == {}  # no file → equal weighting


def test_offline_snapshot_is_noop_preserving_last_good(tmp_path: Path) -> None:
    """An OFFLINE reconcile must NOT wipe the cache — last-good weights survive
    a transient tracker outage (the render still ranks by position)."""
    (tmp_path / "data").mkdir()
    pw.materialize_weights(tmp_path, _portfolio(True, {"NU": 30.0}))
    assert pw.read_materialized_weights(tmp_path) == {"NU": 0.30}

    n = pw.materialize_weights(tmp_path, _portfolio(False, {"NU": 99.0}))
    assert n == 0  # no-op
    assert pw.read_materialized_weights(tmp_path) == {"NU": 0.30}  # unchanged


def test_read_tolerates_corrupt_cache(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portfolio_weights.json").write_text("{not json")
    assert pw.read_materialized_weights(tmp_path) == {}


def test_materialize_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    pw.materialize_weights(tmp_path, _portfolio(True, {"NU": 10.0}))
    leftovers = list((tmp_path / "data").glob("*.tmp"))
    assert leftovers == []


def test_source_age_and_partial_state_survive_new_materialization(tmp_path: Path) -> None:
    portfolio = _portfolio(True, {"NU": 20.0})
    portfolio.as_of = "2025-01-01"
    portfolio.is_stale = True
    portfolio.is_partial = True
    portfolio.envelope_warnings = ["lagging_accounts"]
    pw.materialize_weights(tmp_path, portfolio)
    snapshot = pw.read_materialized_weight_snapshot(tmp_path)
    assert snapshot is not None
    assert snapshot.source_as_of == "2025-01-01"
    assert snapshot.source_is_stale is True
    assert snapshot.source_is_partial is True
    assert snapshot.source_warnings == ("lagging_accounts",)
    assert snapshot.source_snapshot_id is not None
    assert pw.read_materialized_weights_as_of(tmp_path) != snapshot.source_as_of


def test_legacy_weights_do_not_claim_known_source_age(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portfolio_weights.json").write_text(
        json.dumps({"computed_at": "2026-10-03T12:00:00", "weights": {"NU": 0.2}})
    )
    snapshot = pw.read_materialized_weight_snapshot(tmp_path)
    assert snapshot is not None
    assert snapshot.source_as_of is None
    assert snapshot.source_is_stale is None
    assert snapshot.source_is_partial is None
    assert snapshot.source_snapshot_id is None


def test_source_projection_identity_changes_with_source_not_processing(tmp_path: Path) -> None:
    portfolio = _portfolio(True, {"NU": 20.0})
    portfolio.as_of = "2026-10-01"
    pw.materialize_weights(tmp_path, portfolio)
    first = pw.read_materialized_weight_snapshot(tmp_path)
    pw.materialize_weights(tmp_path, portfolio)
    second = pw.read_materialized_weight_snapshot(tmp_path)
    assert first is not None and second is not None
    assert first.source_snapshot_id == second.source_snapshot_id
    portfolio.is_partial = True
    pw.materialize_weights(tmp_path, portfolio)
    partial = pw.read_materialized_weight_snapshot(tmp_path)
    assert partial is not None and first.source_snapshot_id != partial.source_snapshot_id
    path = tmp_path / "data" / "portfolio_weights.json"
    payload = json.loads(path.read_text())
    payload["weights"]["NU"] = 0.9
    path.write_text(json.dumps(payload))
    assert pw.read_materialized_weight_snapshot(tmp_path) is None
    assert pw.read_materialized_weights(tmp_path) == {}


@pytest.mark.parametrize("percent", [float("nan"), float("inf"), 101.0])
def test_invalid_snapshot_keeps_last_good_weights(tmp_path: Path, percent: float) -> None:
    pw.materialize_weights(tmp_path, _portfolio(True, {"NU": 20.0}))
    path = tmp_path / "data" / "portfolio_weights.json"
    accepted = path.read_bytes()
    with pytest.raises(ValueError, match="finite fractions"):
        pw.materialize_weights(tmp_path, _portfolio(True, {"NU": percent}))
    assert path.read_bytes() == accepted


def test_legacy_display_weights_remain_available_with_unknown_source(tmp_path: Path) -> None:
    path = tmp_path / "data" / "portfolio_weights.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"computed_at": "2026-10-03T12:00:00", "weights": {"NU": 0.2}}))
    assert pw.read_materialized_weights(tmp_path) == {"NU": 0.2}
    snapshot = pw.read_materialized_weight_snapshot(tmp_path)
    assert snapshot is not None and snapshot.source_as_of is None


@pytest.mark.parametrize("source", [None, [], "invalid"])
def test_invalid_typed_source_metadata_never_becomes_legacy(
    tmp_path: Path,
    source: object,
) -> None:
    pw.materialize_weights(tmp_path, _portfolio(True, {"NU": 20.0}))
    path = tmp_path / "data" / "portfolio_weights.json"
    payload = json.loads(path.read_text())
    payload["source"] = source
    path.write_text(json.dumps(payload))
    assert pw.read_materialized_weight_snapshot(tmp_path) is None
    assert pw.read_materialized_weights(tmp_path) == {}
