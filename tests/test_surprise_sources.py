"""Tests for src/surprise_sources.py — coercion, surprise math, FMP file source,
yfinance source, and the multi-source dispatcher.

yfinance is mocked at the module-injection level (`sys.modules["yfinance"]`)
so the suite stays hermetic — no network calls in CI.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from surprise_sources import (
    SurpriseHit,
    SurpriseSource,
    default_sources,
    fetch_surprises_with_fallback,
    fetch_surprises_with_outcomes,
    fmp_earnings_calendar_records,
    surprise_pct,
    to_decimal,
    yfinance_earnings_dates_records,
)


def test_dispatcher_retains_exact_per_provider_misses() -> None:
    fmp_hit = SurpriseHit(
        ticker="BN",
        release_date=date(2026, 5, 8),
        eps_estimate=Decimal("1"),
        eps_actual=Decimal("1.1"),
        revenue_estimate=None,
        revenue_actual=None,
        eps_surprise_pct=Decimal("10"),
        revenue_surprise_pct=None,
        num_analysts_eps=None,
        num_analysts_revenue=None,
        source_name="fmp_calendar",
        source_url=None,
    )
    sources = [
        SurpriseSource(name="fmp_calendar", fetch_all=lambda _ticker: [fmp_hit]),
        SurpriseSource(name="yfinance", fetch_all=lambda _ticker: []),
    ]

    merged, outcomes = fetch_surprises_with_outcomes("BN", sources=sources)

    assert merged == [fmp_hit]
    assert [(item.source_name, len(item.hits)) for item in outcomes] == [
        ("fmp_calendar", 1),
        ("yfinance", 0),
    ]


# --- _to_decimal -------------------------------------------------------------


def test_to_decimal_none_returns_none() -> None:
    assert to_decimal(None) is None


def test_to_decimal_bool_returns_none() -> None:
    """bool is an int subclass — must be rejected explicitly to avoid True→1.0."""
    assert to_decimal(True) is None
    assert to_decimal(False) is None


def test_to_decimal_int_and_float() -> None:
    assert to_decimal(5) == Decimal("5")
    assert to_decimal(1.55) == Decimal("1.55")


def test_to_decimal_string_numeric() -> None:
    assert to_decimal("3.14") == Decimal("3.14")
    assert to_decimal("  -2.0 ") == Decimal("-2.0")


def test_to_decimal_empty_string() -> None:
    assert to_decimal("") is None
    assert to_decimal("   ") is None


def test_to_decimal_garbage_string() -> None:
    assert to_decimal("not a number") is None


def test_to_decimal_nan_and_inf() -> None:
    """NaN/Inf must not propagate into Decimal arithmetic downstream."""
    assert to_decimal(float("nan")) is None
    assert to_decimal(float("inf")) is None
    assert to_decimal(float("-inf")) is None


# --- _surprise_pct ----------------------------------------------------------


def test_surprise_pct_positive_beat() -> None:
    # actual 1.55, estimate 1.40 -> +10.71%
    pct = surprise_pct(Decimal("1.55"), Decimal("1.40"))
    assert pct == Decimal("10.71")


def test_surprise_pct_miss() -> None:
    pct = surprise_pct(Decimal("1.40"), Decimal("1.55"))
    assert pct == Decimal("-9.68")


def test_surprise_pct_zero_estimate_returns_none() -> None:
    assert surprise_pct(Decimal("1.00"), Decimal("0")) is None


def test_surprise_pct_none_inputs_return_none() -> None:
    assert surprise_pct(None, Decimal("1")) is None
    assert surprise_pct(Decimal("1"), None) is None


def test_surprise_pct_negative_estimate_sign_preserved() -> None:
    """Loss-making company: estimate -0.50, actual -0.30 = beat by 40%.

    Without abs() in the denominator, the sign would flip and the beat would
    read as a miss.
    """
    pct = surprise_pct(Decimal("-0.30"), Decimal("-0.50"))
    assert pct == Decimal("40.00")


# --- FMP earnings_calendar source -------------------------------------------


def _write_fmp_calendar(tmp_path: Path, ticker: str, records: list[dict[str, object]]) -> Path:
    """Write a fixture earnings_calendar.json into tmp_path matching the
    layout the source expects."""
    path = tmp_path / f"{ticker.upper()}_earnings_calendar.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_fmp_source_missing_file_returns_empty(tmp_path: Path) -> None:
    assert fmp_earnings_calendar_records("NOSUCH", tmp_path) == []


def test_fmp_source_parses_reported_records(tmp_path: Path) -> None:
    _write_fmp_calendar(
        tmp_path,
        "WIX",
        [
            {
                "symbol": "WIX",
                "date": "2025-08-06",
                "epsActual": 2.28,
                "epsEstimated": 1.75,
                "revenueActual": 489930000,
                "revenueEstimated": 502532690,
                "lastUpdated": "2025-11-06",
            },
            {
                "symbol": "WIX",
                "date": "2025-11-19",
                "epsActual": 1.68,
                "epsEstimated": 1.54,
                "revenueActual": 505194000,
                "revenueEstimated": 502431131,
                "lastUpdated": "2026-02-18",
            },
        ],
    )
    hits = fmp_earnings_calendar_records("WIX", tmp_path)
    assert len(hits) == 2
    h0 = hits[0]
    assert h0.ticker == "WIX"
    assert h0.release_date == date(2025, 8, 6)
    assert h0.eps_estimate == Decimal("1.75")
    assert h0.eps_actual == Decimal("2.28")
    assert h0.revenue_estimate == Decimal("502532690")
    assert h0.revenue_actual == Decimal("489930000")
    # Surprise % computed: (2.28 - 1.75) / 1.75 * 100 = 30.29
    assert h0.eps_surprise_pct == Decimal("30.29")
    # Revenue miss: (489930000 - 502532690) / 502532690 * 100 = -2.51
    assert h0.revenue_surprise_pct == Decimal("-2.51")
    assert h0.source_name == "fmp_calendar"


def test_fmp_source_skips_forward_dated_rows(tmp_path: Path) -> None:
    """Forward-dated FMP rows have epsActual=null/revenueActual=null — they
    should be skipped so the cache only contains REPORTED quarters."""
    _write_fmp_calendar(
        tmp_path,
        "WIX",
        [
            {
                "symbol": "WIX",
                "date": "2026-08-12",
                "epsActual": None,
                "epsEstimated": 1.54,
                "revenueActual": None,
                "revenueEstimated": 562009700,
            },
            {
                "symbol": "WIX",
                "date": "2025-08-06",
                "epsActual": 2.28,
                "epsEstimated": 1.75,
                "revenueActual": 489930000,
                "revenueEstimated": 502532690,
            },
        ],
    )
    hits = fmp_earnings_calendar_records("WIX", tmp_path)
    assert len(hits) == 1
    assert hits[0].release_date == date(2025, 8, 6)


def test_fmp_source_malformed_json_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "BAD_earnings_calendar.json"
    path.write_text("{not json", encoding="utf-8")
    assert fmp_earnings_calendar_records("BAD", tmp_path) == []


def test_fmp_source_non_list_payload_returns_empty(tmp_path: Path) -> None:
    """FMP rarely but possibly returns a dict wrapper instead of a top-level
    list — guard against schema drift."""
    path = tmp_path / "WIX_earnings_calendar.json"
    path.write_text(json.dumps({"symbol": "WIX", "data": []}), encoding="utf-8")
    assert fmp_earnings_calendar_records("WIX", tmp_path) == []


def test_fmp_source_skips_records_with_bad_date(tmp_path: Path) -> None:
    _write_fmp_calendar(
        tmp_path,
        "WIX",
        [
            {"symbol": "WIX", "date": "bogus", "epsActual": 1.0, "epsEstimated": 0.9},
            {"symbol": "WIX", "date": "2025-08-06", "epsActual": 2.28, "epsEstimated": 1.75},
        ],
    )
    hits = fmp_earnings_calendar_records("WIX", tmp_path)
    assert len(hits) == 1
    assert hits[0].release_date == date(2025, 8, 6)


# --- yfinance source --------------------------------------------------------


class _FakeYfTicker:
    """Stand-in for `yfinance.Ticker`. Returns a fixed DataFrame from
    `earnings_dates`. Tests inject this via sys.modules so the real network
    library is never imported."""

    def __init__(self, ticker: str, df: pd.DataFrame | None) -> None:
        self._df = df

    @property
    def earnings_dates(self) -> pd.DataFrame | None:
        return self._df


def _install_fake_yfinance(monkeypatch: pytest.MonkeyPatch, df: pd.DataFrame | None) -> None:
    """Install a fake yfinance module so `import yfinance` inside the source
    function picks up our test double."""
    fake = types.ModuleType("yfinance")
    fake.Ticker = lambda t: _FakeYfTicker(t, df)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake)


def _make_yf_df(rows: list[tuple[str, float | None, float | None]]) -> pd.DataFrame:
    """Build a DataFrame in the exact shape yfinance returns: tz-aware
    DatetimeIndex named 'Earnings Date', columns 'EPS Estimate' / 'Reported EPS' /
    'Surprise(%)'."""
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d, tz="America/New_York") for d, _, _ in rows],
        name="Earnings Date",
    )
    return pd.DataFrame(
        {
            "EPS Estimate": [r[1] for r in rows],
            "Reported EPS": [r[2] for r in rows],
            "Surprise(%)": [None] * len(rows),
        },
        index=idx,
    )


def test_yfinance_source_no_module_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """When yfinance is not installed, the source returns [] gracefully."""
    monkeypatch.setitem(sys.modules, "yfinance", None)  # type: ignore[arg-type]
    # Force re-import to hit the ImportError branch
    assert yfinance_earnings_dates_records("WIX") == []


def test_yfinance_source_parses_dataframe(monkeypatch: pytest.MonkeyPatch) -> None:
    df = _make_yf_df(
        [
            ("2025-08-06", 1.75, 2.28),
            ("2025-05-21", 1.65, 1.55),
        ]
    )
    _install_fake_yfinance(monkeypatch, df)
    hits = yfinance_earnings_dates_records("WIX")
    assert len(hits) == 2
    by_date = {h.release_date: h for h in hits}
    assert by_date[date(2025, 8, 6)].eps_estimate == Decimal("1.75")
    assert by_date[date(2025, 8, 6)].eps_actual == Decimal("2.28")
    # Revenue fields explicitly None — yfinance doesn't publish them
    assert by_date[date(2025, 8, 6)].revenue_estimate is None
    assert by_date[date(2025, 8, 6)].revenue_actual is None
    assert by_date[date(2025, 8, 6)].source_name == "yfinance"
    # Surprise % computed: (2.28 - 1.75) / 1.75 * 100 = 30.29
    assert by_date[date(2025, 8, 6)].eps_surprise_pct == Decimal("30.29")


def test_yfinance_source_skips_forward_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forward-dated rows (estimate present, Reported EPS NaN) must be skipped."""
    df = _make_yf_df(
        [
            ("2026-08-12", 1.54, None),  # forward — Reported EPS is missing
            ("2025-08-06", 1.75, 2.28),  # reported
        ]
    )
    _install_fake_yfinance(monkeypatch, df)
    hits = yfinance_earnings_dates_records("WIX")
    assert len(hits) == 1
    assert hits[0].release_date == date(2025, 8, 6)


def test_yfinance_source_empty_df_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_yfinance(monkeypatch, pd.DataFrame())
    assert yfinance_earnings_dates_records("WIX") == []


def test_yfinance_source_none_df_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_yfinance(monkeypatch, None)
    assert yfinance_earnings_dates_records("WIX") == []


def test_yfinance_source_handles_ticker_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network errors / Yahoo schema changes must NOT bubble out — the
    dispatcher relies on this isolation."""
    fake = types.ModuleType("yfinance")

    def boom(_t: str) -> object:
        raise RuntimeError("yahoo down")

    fake.Ticker = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    assert yfinance_earnings_dates_records("WIX") == []


# --- Dispatcher (fetch_surprises_with_fallback) -----------------------------


def _hit(release: date, source: str, eps_actual: float | None = 1.0) -> SurpriseHit:
    """Tiny builder so dispatcher tests stay readable."""
    return SurpriseHit(
        ticker="X",
        release_date=release,
        eps_estimate=Decimal("0.9"),
        eps_actual=Decimal(str(eps_actual)) if eps_actual is not None else None,
        revenue_estimate=None,
        revenue_actual=None,
        eps_surprise_pct=None,
        revenue_surprise_pct=None,
        num_analysts_eps=None,
        num_analysts_revenue=None,
        source_name=source,
        source_url=None,
    )


def test_dispatcher_first_source_wins() -> None:
    src1 = SurpriseSource(name="primary", fetch_all=lambda _t: [_hit(date(2025, 8, 6), "primary")])
    src2 = SurpriseSource(name="backup", fetch_all=lambda _t: [_hit(date(2025, 8, 6), "backup")])
    merged, tried = fetch_surprises_with_fallback("X", sources=[src1, src2])
    assert len(merged) == 1
    assert merged[0].source_name == "primary"
    assert tried == ["primary", "backup"]


def test_dispatcher_backup_fills_gaps() -> None:
    """Backup source contributes records the primary doesn't have."""
    src1 = SurpriseSource(name="primary", fetch_all=lambda _t: [_hit(date(2025, 8, 6), "primary")])
    src2 = SurpriseSource(
        name="backup",
        fetch_all=lambda _t: [
            _hit(date(2025, 8, 6), "backup"),
            _hit(date(2024, 8, 6), "backup"),  # not in primary
        ],
    )
    merged, _ = fetch_surprises_with_fallback("X", sources=[src1, src2])
    assert len(merged) == 2
    # Sorted oldest-first
    assert merged[0].release_date == date(2024, 8, 6)
    assert merged[0].source_name == "backup"
    assert merged[1].source_name == "primary"


def test_dispatcher_all_sources_empty() -> None:
    src = SurpriseSource(name="x", fetch_all=lambda _t: [])
    merged, tried = fetch_surprises_with_fallback("X", sources=[src])
    assert merged == []
    assert tried == ["x"]


def test_default_sources_chain_order(tmp_path: Path) -> None:
    """default_sources() emits FMP first, yfinance second — the priority
    contract the user picked."""
    sources = default_sources(fmp_dir=tmp_path)
    assert [s.name for s in sources] == ["fmp_calendar", "yfinance"]


# --- SurpriseHit.to_json (serialization round-trip) -------------------------


def test_to_json_serializes_decimals_as_strings() -> None:
    """JSON has no native Decimal — we serialize as strings to preserve precision."""
    h = _hit(date(2025, 8, 6), "primary", eps_actual=2.28)
    j = h.to_json()
    assert j["release_date"] == "2025-08-06"
    assert j["eps_actual"] == "2.28"
    assert j["eps_estimate"] == "0.9"
    # Decimal-bearing fields that are None stay None (not "None")
    assert j["revenue_estimate"] is None
