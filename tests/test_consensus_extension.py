"""Tests for src/dcf/consensus_extension.py — the yfinance secondary anchor.

Contract under test: FMP years pass through verbatim, extension years are
provenance-tagged source=yfinance, growth is applied conservatively, and any
missing/absurd input degrades to "no extension" rather than a guess."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dcf.consensus_extension import (
    SOURCE_FMP,
    SOURCE_YFINANCE,
    YfGrowthInputs,
    extend_consensus,
    extension_event,
    load_yf_growth,
)

FC_YEARS = [2026, 2027, 2028, 2029, 2030, 2031]


def _growth(
    rev: float | None = 0.12, eps: float | None = 0.15, ltg: float | None = 0.10
) -> YfGrowthInputs:
    return YfGrowthInputs(
        revenue_growth_next=rev, eps_growth_next=eps, lt_growth=ltg, asof_date="2026-07-18"
    )


def test_fmp_years_pass_through_verbatim() -> None:
    cons_rev = {2026: 100.0, 2027: 112.0}
    cons_ni = {2026: 20.0, 2027: 23.0}
    rev, ni, src = extend_consensus(cons_rev, cons_ni, FC_YEARS, _growth())
    assert rev[2026] == 100.0 and rev[2027] == 112.0
    assert ni[2026] == 20.0 and ni[2027] == 23.0
    assert src[2026] == SOURCE_FMP and src[2027] == SOURCE_FMP
    # inputs not mutated
    assert cons_rev == {2026: 100.0, 2027: 112.0}


def test_extension_years_tagged_and_compounded() -> None:
    rev, ni, src = extend_consensus({2026: 100.0, 2027: 112.0}, {2027: 23.0}, FC_YEARS, _growth())
    # revenue growth = min(0.12, 0.10) = 0.10 (conservative), compounding from 2027
    assert rev[2028] == pytest.approx(112.0 * 1.10)
    assert rev[2029] == pytest.approx(112.0 * 1.10**2)
    assert rev[2030] == pytest.approx(112.0 * 1.10**3)
    assert 2031 not in rev  # MAX_EXTRA_YEARS = 3
    # NI growth = LTG = 0.10
    assert ni[2028] == pytest.approx(23.0 * 1.10)
    assert src[2028] == SOURCE_YFINANCE
    assert src[2029] == SOURCE_YFINANCE
    assert src[2030] == SOURCE_YFINANCE


def test_no_ltg_falls_back_to_next_year_rates() -> None:
    rev, ni, _ = extend_consensus(
        {2027: 112.0}, {2027: 23.0}, FC_YEARS, _growth(rev=0.12, eps=0.15, ltg=None)
    )
    assert rev[2028] == pytest.approx(112.0 * 1.12)
    assert ni[2028] == pytest.approx(23.0 * 1.15)


def test_out_of_bounds_growth_declines_to_extend() -> None:
    """A Yahoo glitch (e.g. 40x 'growth') must not compound into the anchor."""
    rev, ni, src = extend_consensus(
        {2027: 112.0}, {2027: 23.0}, FC_YEARS, _growth(rev=39.0, eps=39.0, ltg=None)
    )
    assert rev == {2027: 112.0}
    assert ni == {2027: 23.0}
    assert all(s == SOURCE_FMP for s in src.values())


def test_no_fmp_base_returns_unchanged() -> None:
    rev, ni, src = extend_consensus({}, {}, FC_YEARS, _growth())
    assert rev == {} and ni == {} and src == {}


def test_extension_only_contiguous_run() -> None:
    """Extension never gap-jumps: if the fc_years list skips the year right
    after the FMP horizon, nothing is extended."""
    rev, _, src = extend_consensus({2027: 112.0}, {}, [2026, 2027, 2029, 2030], _growth())
    assert rev == {2027: 112.0}
    assert all(s == SOURCE_FMP for s in src.values())


def test_ni_only_extended_when_fmp_ni_reaches_horizon() -> None:
    """NI anchoring extends from the FMP NI value at the revenue horizon year;
    without one, only revenue extends (no cross-metric inference)."""
    rev, ni, src = extend_consensus({2027: 112.0}, {2026: 20.0}, FC_YEARS, _growth())
    assert 2028 in rev
    assert 2028 not in ni
    assert src[2028] == SOURCE_YFINANCE


def test_load_yf_growth_reads_persisted_snapshot(tmp_path: Path) -> None:
    payload = {
        "ticker": "AAPL",
        "asof_date": "2026-07-18",
        "fetched_at": "2026-07-18T12:00:00+00:00",
        "source": "yfinance",
        "revenue_estimate": [
            {"period": "0y", "avg": 478.7e9, "growth": 0.1503},
            {"period": "+1y", "avg": 522.9e9, "growth": 0.0924},
        ],
        "earnings_estimate": [{"period": "+1y", "avg": 9.71, "growth": 0.1076}],
        "growth_estimates": [
            {"period": "+1y", "stockTrend": 0.10},
            {"period": "LTG", "stockTrend": 0.122},
        ],
    }
    path = tmp_path / "AAPL_yf_estimates.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    growth = load_yf_growth(path)
    assert growth is not None
    assert growth.revenue_growth_next == pytest.approx(0.0924)
    assert growth.eps_growth_next == pytest.approx(0.1076)
    assert growth.lt_growth == pytest.approx(0.122)
    assert growth.asof_date == "2026-07-18"


def test_load_yf_growth_degrades_on_missing_or_signal_free_file(tmp_path: Path) -> None:
    assert load_yf_growth(tmp_path / "absent.json") is None
    bare = {
        "ticker": "X",
        "asof_date": "2026-07-18",
        "fetched_at": "t",
        "revenue_estimate": [{"period": "0q", "avg": 1.0}],
    }
    path = tmp_path / "X_yf_estimates.json"
    path.write_text(json.dumps(bare), encoding="utf-8")
    assert load_yf_growth(path) is None
    path.write_text("not json", encoding="utf-8")
    assert load_yf_growth(path) is None


def test_extension_event_shape() -> None:
    _rev, _, src = extend_consensus({2027: 112.0}, {}, FC_YEARS, _growth())
    event = extension_event("AMZN", src, _growth())
    assert event["event"] == "consensus_extension"
    assert event["ticker"] == "AMZN"
    assert event["yf_years"] == [2028, 2029, 2030]
    per_year = event["per_year_source"]
    assert isinstance(per_year, dict)
    assert per_year["2027"] == SOURCE_FMP
    assert per_year["2028"] == SOURCE_YFINANCE
    json.dumps(event)  # must be JSON-serializable as logged
