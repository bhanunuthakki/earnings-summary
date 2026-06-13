"""Tests for src/portfolio_weights.py — the materialized weight cache the inbox
render reads instead of the live tracker (directive S12 latency fix)."""

from __future__ import annotations

import json
from pathlib import Path

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
