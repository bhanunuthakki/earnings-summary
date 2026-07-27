"""Tests for src/allocation/price_history.py — loader, returns, alignment."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest

from allocation.price_history import (
    build_aligned_returns,
    daily_log_returns,
    load_daily_closes,
)


def _write_chart(repo_root: Path, ticker: str, rows: object) -> None:
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(json.dumps(rows), encoding="utf-8")


def test_load_prefers_adjclose_and_sorts_ascending(tmp_path: Path) -> None:
    # FMP's stable endpoint returns newest-first; adjClose differs from close.
    _write_chart(
        tmp_path,
        "NU",
        [
            {"date": "2026-05-18", "adjClose": 12.29, "close": 99.0},
            {"date": "2026-05-15", "adjClose": 12.40, "close": 99.0},
        ],
    )
    out = load_daily_closes("NU", tmp_path)
    assert out == [(date(2026, 5, 15), 12.40), (date(2026, 5, 18), 12.29)]


def test_load_legacy_wrapper_and_close_fallback(tmp_path: Path) -> None:
    _write_chart(
        tmp_path,
        "OLD",
        {
            "historical": [
                {"date": "2024-01-03", "close": 50.0},
                {"date": "2024-01-02", "close": 49.0},
            ]
        },
    )
    out = load_daily_closes("OLD", tmp_path)
    assert out == [(date(2024, 1, 2), 49.0), (date(2024, 1, 3), 50.0)]


def test_load_skips_unparseable_and_nonpositive_rows(tmp_path: Path) -> None:
    _write_chart(
        tmp_path,
        "JNK",
        [
            {"date": "2024-01-02", "adjClose": 10.0},
            {"date": "2024-01-03", "adjClose": 0.0},  # non-positive
            {"date": "not-a-date", "adjClose": 11.0},
            {"adjClose": 12.0},  # no date
            {"date": "2024-01-04"},  # no value
            {"date": "2024-01-05", "adjClose": "n/a"},  # unparseable value
        ],
    )
    assert load_daily_closes("JNK", tmp_path) == [(date(2024, 1, 2), 10.0)]


def test_load_falls_back_for_legacy_suffixes(tmp_path: Path) -> None:
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    (fmp / "LEG_price_chart_5y.json").write_text(
        json.dumps([{"date": "2024-01-02", "adjClose": 7.0}]), encoding="utf-8"
    )
    assert load_daily_closes("LEG", tmp_path) == [(date(2024, 1, 2), 7.0)]


def test_repeated_legacy_loads_use_cached_directory_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy lookup must not glob the large FMP directory per ticker load."""
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    (fmp / "LEG_price_chart_5y.json").write_text(
        json.dumps([{"date": "2024-01-02", "adjClose": 7.0}]), encoding="utf-8"
    )
    glob_calls: list[tuple[Path, str]] = []
    iterdir_calls: list[Path] = []
    original_glob = Path.glob
    original_iterdir = Path.iterdir

    def counting_glob(path: Path, pattern: str) -> object:
        if path == fmp:
            glob_calls.append((path, pattern))
        return original_glob(path, pattern)

    def counting_iterdir(path: Path) -> Iterator[Path]:
        if path == fmp:
            iterdir_calls.append(path)
        return original_iterdir(path)

    monkeypatch.setattr(Path, "glob", counting_glob)
    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    assert load_daily_closes("LEG", tmp_path) == [(date(2024, 1, 2), 7.0)]
    assert load_daily_closes("LEG", tmp_path) == [(date(2024, 1, 2), 7.0)]
    assert glob_calls == []
    assert iterdir_calls == [fmp]


def test_legacy_manifest_invalidates_when_fmp_directory_changes(tmp_path: Path) -> None:
    """A newly written legacy chart must become visible without a process restart."""
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)

    assert load_daily_closes("NEW", tmp_path) == []
    (fmp / "NEW_price_chart_5y.json").write_text(
        json.dumps([{"date": "2024-01-02", "adjClose": 9.0}]), encoding="utf-8"
    )

    assert load_daily_closes("NEW", tmp_path) == [(date(2024, 1, 2), 9.0)]


def test_load_missing_or_garbage_file_returns_empty(tmp_path: Path) -> None:
    assert load_daily_closes("NONE", tmp_path) == []
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    (fmp / "BAD_price_chart_10y_div_adj.json").write_text("{not json", encoding="utf-8")
    assert load_daily_closes("BAD", tmp_path) == []


def test_daily_log_returns_known_values() -> None:
    prices = [
        (date(2024, 1, 2), 100.0),
        (date(2024, 1, 3), 110.0),
        (date(2024, 1, 4), 99.0),
    ]
    rets = daily_log_returns(prices)
    assert set(rets) == {date(2024, 1, 3), date(2024, 1, 4)}
    assert rets[date(2024, 1, 3)] == math.log(1.10)
    assert rets[date(2024, 1, 4)] == math.log(99.0 / 110.0)


def _calendar(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def test_build_aligned_returns_matrix_shape_and_values() -> None:
    days = _calendar(date(2025, 1, 1), 8)
    a = {d: 0.01 * (i + 1) for i, d in enumerate(days)}
    b = {d: -0.02 * (i + 1) for i, d in enumerate(days)}
    aligned = build_aligned_returns({"B": b, "A": a}, lookback_obs=252, min_overlap_obs=5)
    assert aligned is not None
    assert aligned.tickers == ["A", "B"]  # sorted column order
    assert aligned.dates == days
    assert aligned.matrix.shape == (8, 2)
    assert aligned.matrix[2, 0] == a[days[2]]
    assert aligned.matrix[2, 1] == b[days[2]]
    assert aligned.dropped == {}


def test_build_aligned_returns_trims_to_lookback() -> None:
    days = _calendar(date(2025, 1, 1), 30)
    a = dict.fromkeys(days, 0.01)
    b = dict.fromkeys(days, 0.02)
    aligned = build_aligned_returns({"A": a, "B": b}, lookback_obs=10, min_overlap_obs=5)
    assert aligned is not None
    assert aligned.dates == days[-10:]
    assert aligned.matrix.shape == (10, 2)


def test_build_aligned_returns_drops_thin_own_history() -> None:
    days = _calendar(date(2025, 1, 1), 10)
    a = dict.fromkeys(days, 0.01)
    b = dict.fromkeys(days, 0.02)
    c = dict.fromkeys(days[:3], 0.03)  # only 3 observations of its own
    aligned = build_aligned_returns({"A": a, "B": b, "C": c}, min_overlap_obs=5)
    assert aligned is not None
    assert aligned.tickers == ["A", "B"]
    assert "only 3 daily returns" in aligned.dropped["C"]


def test_build_aligned_returns_greedy_drops_disjoint_calendar() -> None:
    shared = _calendar(date(2025, 1, 1), 10)
    other = _calendar(date(2020, 1, 1), 8)  # enough own obs, zero overlap
    a = dict.fromkeys(shared, 0.01)
    b = dict.fromkeys(shared, 0.02)
    c = dict.fromkeys(other, 0.03)
    aligned = build_aligned_returns({"A": a, "B": b, "C": c}, min_overlap_obs=6)
    assert aligned is not None
    assert aligned.tickers == ["A", "B"]
    assert "calendar overlap" in aligned.dropped["C"]


def test_build_aligned_returns_none_when_unusable() -> None:
    days = _calendar(date(2025, 1, 1), 10)
    assert build_aligned_returns({"A": dict.fromkeys(days, 0.01)}, min_overlap_obs=5) is None
    disjoint = {
        "A": dict.fromkeys(_calendar(date(2025, 1, 1), 10), 0.01),
        "B": dict.fromkeys(_calendar(date(2020, 1, 1), 10), 0.02),
    }
    assert build_aligned_returns(disjoint, min_overlap_obs=5) is None
