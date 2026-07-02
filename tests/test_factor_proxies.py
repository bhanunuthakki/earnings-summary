"""factor_proxies — the ETF style-proxy close store (fetch / persist / read)."""

from __future__ import annotations

import json
import sys
import types
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import factor_proxies
from factor_proxies import (
    PROXY_TICKERS,
    fetch_proxy_series,
    load_proxy_closes,
    load_proxy_returns,
    proxy_path,
    refresh_factor_proxies,
    store_proxy_series,
)


def test_store_and_load_round_trip(tmp_path: Path) -> None:
    rows = [(date(2026, 1, 3), 101.5), (date(2026, 1, 2), 100.0)]  # unsorted in
    path = store_proxy_series(tmp_path, "vtv", rows)
    assert path is not None
    assert path == proxy_path(tmp_path, "VTV") and path.exists()
    loaded = load_proxy_closes(tmp_path, "VTV")
    assert loaded == [(date(2026, 1, 2), 100.0), (date(2026, 1, 3), 101.5)]  # ascending
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ticker"] == "VTV"
    assert payload["fetched_at"]  # stamped


def test_store_empty_is_noop_preserving_last_good(tmp_path: Path) -> None:
    store_proxy_series(tmp_path, "SPY", [(date(2026, 1, 2), 500.0)])
    assert store_proxy_series(tmp_path, "SPY", []) is None
    assert load_proxy_closes(tmp_path, "SPY") == [(date(2026, 1, 2), 500.0)]


def test_load_tolerates_missing_and_garbage(tmp_path: Path) -> None:
    assert load_proxy_closes(tmp_path, "IWM") == []
    p = proxy_path(tmp_path, "IWM")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json", encoding="utf-8")
    assert load_proxy_closes(tmp_path, "IWM") == []
    p.write_text(
        json.dumps(
            {
                "rows": [
                    ["2026-01-02", 100.0],
                    ["garbage-date", 1.0],
                    ["2026-01-03", -5.0],  # non-positive dropped
                    ["2026-01-04", True],  # bool is not a price
                    ["2026-01-05"],  # wrong arity
                    ["2026-01-06", 101.0],
                ]
            }
        ),
        encoding="utf-8",
    )
    assert load_proxy_closes(tmp_path, "IWM") == [
        (date(2026, 1, 2), 100.0),
        (date(2026, 1, 6), 101.0),
    ]


def test_load_proxy_returns_computes_log_returns(tmp_path: Path) -> None:
    store_proxy_series(tmp_path, "MTUM", [(date(2026, 1, 2), 100.0), (date(2026, 1, 3), 110.0)])
    rets = load_proxy_returns(tmp_path, ["MTUM", "SPY"])  # SPY absent — omitted
    assert set(rets) == {"MTUM"}
    assert date(2026, 1, 3) in rets["MTUM"]
    assert abs(rets["MTUM"][date(2026, 1, 3)] - 0.09531017980432486) < 1e-12


def test_refresh_uses_fetch_and_keeps_last_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_proxy_series(tmp_path, "VUG", [(date(2025, 12, 31), 400.0)])

    def fake_fetch(ticker: str, *, period: str = "2y") -> list[tuple[date, float]]:
        if ticker == "VUG":
            return []  # outage — must not clobber the stored series
        return [(date(2026, 1, 2), 1.0), (date(2026, 1, 3), 2.0)]

    monkeypatch.setattr(factor_proxies, "fetch_proxy_series", fake_fetch)
    counts = refresh_factor_proxies(tmp_path, ["SPY", "VUG"])
    assert counts == {"SPY": 2, "VUG": 0}
    assert load_proxy_closes(tmp_path, "VUG") == [(date(2025, 12, 31), 400.0)]
    assert len(load_proxy_closes(tmp_path, "SPY")) == 2


class _FakeSeries:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def tolist(self) -> list[Any]:
        return self._values


class _FakeFrame:
    def __init__(self, stamps: list[Any], closes: list[Any]) -> None:
        self.index = _FakeSeries(stamps)
        self._closes = closes

    def __getitem__(self, key: str) -> _FakeSeries:
        assert key == "Close"
        return _FakeSeries(self._closes)


def test_fetch_proxy_series_parses_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """The yfinance seam: Timestamp-like index + Close column -> sorted pairs,
    NaNs and non-positives dropped."""

    class _Stamp:
        def __init__(self, d: date) -> None:
            self._d = d

        def date(self) -> date:
            return self._d

    frame = _FakeFrame(
        [_Stamp(date(2026, 1, 3)), _Stamp(date(2026, 1, 2)), _Stamp(date(2026, 1, 4))],
        [101.0, 100.0, float("nan")],
    )

    class _FakeTicker:
        def __init__(self, ticker: str) -> None:
            self.ticker = ticker

        def history(self, **kwargs: object) -> _FakeFrame:
            assert kwargs.get("auto_adjust") is True
            return frame

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = _FakeTicker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    rows = fetch_proxy_series("SPY")
    assert rows == [(date(2026, 1, 2), 100.0), (date(2026, 1, 3), 101.0)]


def test_fetch_proxy_series_degrades_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def __init__(self, ticker: str) -> None:
            raise RuntimeError("network down")

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = _Boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    assert fetch_proxy_series("SPY") == []


def test_registry_covers_all_style_spread_legs() -> None:
    assert set(PROXY_TICKERS) == {"SPY", "VTV", "VUG", "IWM", "MTUM"}
