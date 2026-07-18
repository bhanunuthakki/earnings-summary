"""Tests for execution/fetch_yf_estimates.py — validation, storage layout,
per-item degrade (a flaky ticker never aborts the batch), and idempotency.

yfinance is never imported: the ``tables_loader`` seam injects fixture tables,
mirroring the fetch_yf_grades test convention. No network in CI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    """Import the fetcher as a module without executing main() — execution/
    scripts aren't on the package path, so load by file path (same pattern as
    test_backfill_earnings_surprises)."""
    src = PROJECT_ROOT / "execution" / "fetch_yf_estimates.py"
    spec = importlib.util.spec_from_file_location("fetch_yf_estimates", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetch_yf_estimates"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fetcher() -> Any:
    return _load_module()


def _good_tables() -> dict[str, list[dict[str, object]]]:
    return {
        "earnings_estimate": [
            {
                "period": "0q",
                "avg": 1.89,
                "low": 1.83,
                "high": 1.99,
                "yearAgoEps": 1.57,
                "numberOfAnalysts": 31,
                "growth": 0.2063,
                "currency": "USD",
            },
            {"period": "+1y", "avg": 9.71, "growth": 0.1076, "currency": "USD"},
        ],
        "revenue_estimate": [
            {
                "period": "0q",
                "avg": 108881511420.0,
                "low": 107501000000.0,
                "numberOfAnalysts": 24,
                "growth": 0.1579,
                "currency": "USD",
            },
            {"period": "+1y", "avg": 522949683990.0, "growth": 0.0924, "currency": "USD"},
        ],
        "growth_estimates": [
            {"period": "0q", "stockTrend": 0.2063, "indexTrend": 0.2188},
            {"period": "LTG", "stockTrend": None, "indexTrend": 0.122},
        ],
        "eps_trend": [
            {
                "period": "0q",
                "current": 1.89396,
                "7daysAgo": 1.89428,
                "30daysAgo": 1.89429,
                "60daysAgo": 1.89429,
                "90daysAgo": 1.72909,
                "currency": "USD",
            }
        ],
        "eps_revisions": [
            {
                "period": "0q",
                "upLast7days": 0,
                "upLast30days": 24,
                "downLast30days": 0,
                "downLast7Days": 0,
                "currency": "USD",
            }
        ],
        "analyst_price_targets": [
            {"current": 211.0, "low": 180.0, "high": 300.0, "mean": 240.5, "median": 235.0}
        ],
    }


# --- build_snapshot ----------------------------------------------------------


def test_build_snapshot_validates_all_tables(fetcher: Any) -> None:
    snap = fetcher.build_snapshot(
        "AAPL", _good_tables(), asof_date="2026-07-18", fetched_at="2026-07-18T12:00:00+00:00"
    )
    assert snap.ticker == "AAPL"
    assert snap.source == "yfinance"
    assert len(snap.revenue_estimate) == 2
    assert snap.revenue_estimate[0].period == "0q"
    assert snap.revenue_estimate[0].numberOfAnalysts == 24
    # digit-prefixed vendor columns land via alias
    assert snap.eps_trend[0].days7ago == pytest.approx(1.89428)
    assert snap.analyst_price_targets is not None
    assert snap.analyst_price_targets.mean == pytest.approx(240.5)
    assert set(snap.table_names_present()) == {
        "earnings_estimate",
        "revenue_estimate",
        "growth_estimates",
        "eps_trend",
        "eps_revisions",
        "analyst_price_targets",
    }


def test_build_snapshot_drops_malformed_rows_individually(fetcher: Any) -> None:
    tables = _good_tables()
    tables["revenue_estimate"].append({"period": "+1q", "avg": "not-a-number"})
    snap = fetcher.build_snapshot(
        "AAPL", tables, asof_date="2026-07-18", fetched_at="2026-07-18T12:00:00+00:00"
    )
    # the malformed row is dropped, the good ones survive
    assert len(snap.revenue_estimate) == 2


def test_build_snapshot_normalizes_nan_to_none(fetcher: Any) -> None:
    """pandas encodes missing cells as float NaN; persisted JSON must carry
    null, not the invalid-strict-JSON NaN literal (verified live on AAPL's
    LTG stockTrend)."""
    nan = float("nan")
    tables = _good_tables()
    tables["growth_estimates"] = [{"period": "LTG", "stockTrend": nan, "indexTrend": 0.122}]
    tables["revenue_estimate"][0]["numberOfAnalysts"] = nan
    snap = fetcher.build_snapshot(
        "AAPL", tables, asof_date="2026-07-18", fetched_at="2026-07-18T12:00:00+00:00"
    )
    assert snap.growth_estimates[0].stockTrend is None
    assert snap.revenue_estimate[0].numberOfAnalysts is None
    # allow_nan=False proves the serialized payload is strict-JSON clean
    json.dumps(snap.model_dump(), allow_nan=False)


def test_frame_to_records_handles_none_and_junk(fetcher: Any) -> None:
    assert fetcher._frame_to_records(None) == []
    assert fetcher._frame_to_records(42) == []
    assert fetcher._frame_to_records("nope") == []


# --- run(): storage layout + degrade + idempotency ---------------------------


def _good_loader(ticker: str) -> dict[str, list[dict[str, object]]]:
    return _good_tables()


def test_run_writes_latest_and_dated_snapshot(fetcher: Any, tmp_path: Path) -> None:
    tally = fetcher.run(
        ["aapl"],
        data_root=tmp_path,
        asof_date="2026-07-18",
        tables_loader=_good_loader,
    )
    assert tally == {"ok": 1, "skipped": 0, "empty": 0, "failed": 0}
    latest = tmp_path / "historical" / "yfinance" / "AAPL_yf_estimates.json"
    snap = tmp_path / "historical" / "yfinance_snapshots" / "2026-07-18" / "AAPL_yf_estimates.json"
    assert latest.exists() and snap.exists()
    payload = json.loads(snap.read_text(encoding="utf-8"))
    assert payload["ticker"] == "AAPL"
    assert payload["asof_date"] == "2026-07-18"
    assert payload["source"] == "yfinance"
    assert payload == json.loads(latest.read_text(encoding="utf-8"))


def test_run_flaky_ticker_degrades_not_aborts(fetcher: Any, tmp_path: Path) -> None:
    """The repo per-item degrade pattern: one ticker raising must not stop the
    batch — it is tallied as failed and the rest persist."""

    def loader(ticker: str) -> dict[str, list[dict[str, object]]]:
        if ticker == "FLKY":
            raise ConnectionError("Yahoo hiccup")
        return _good_tables()

    tally = fetcher.run(
        ["FLKY", "AAPL"], data_root=tmp_path, asof_date="2026-07-18", tables_loader=loader
    )
    assert tally == {"ok": 1, "skipped": 0, "empty": 0, "failed": 1}
    assert (tmp_path / "historical" / "yfinance" / "AAPL_yf_estimates.json").exists()
    assert not (tmp_path / "historical" / "yfinance" / "FLKY_yf_estimates.json").exists()


def test_run_empty_tables_deferred_not_written(fetcher: Any, tmp_path: Path) -> None:
    """A ticker with zero usable tables writes nothing (so a later run retries
    it) and is tallied separately from a hard failure."""
    empty: dict[str, list[dict[str, object]]] = {name: [] for name in _good_tables()}

    def loader(ticker: str) -> dict[str, list[dict[str, object]]]:
        return empty

    tally = fetcher.run(["THIN"], data_root=tmp_path, asof_date="2026-07-18", tables_loader=loader)
    assert tally == {"ok": 0, "skipped": 0, "empty": 1, "failed": 0}
    assert not (
        tmp_path / "historical" / "yfinance_snapshots" / "2026-07-18" / "THIN_yf_estimates.json"
    ).exists()


def test_run_idempotent_skip_and_force(fetcher: Any, tmp_path: Path) -> None:
    calls: list[str] = []

    def loader(ticker: str) -> dict[str, list[dict[str, object]]]:
        calls.append(ticker)
        return _good_tables()

    first = fetcher.run(["AAPL"], data_root=tmp_path, asof_date="2026-07-18", tables_loader=loader)
    second = fetcher.run(["AAPL"], data_root=tmp_path, asof_date="2026-07-18", tables_loader=loader)
    assert first["ok"] == 1
    assert second == {"ok": 0, "skipped": 1, "empty": 0, "failed": 0}
    assert calls == ["AAPL"]  # second run never re-fetched
    forced = fetcher.run(
        ["AAPL"], data_root=tmp_path, asof_date="2026-07-18", force=True, tables_loader=loader
    )
    assert forced["ok"] == 1
    assert calls == ["AAPL", "AAPL"]
