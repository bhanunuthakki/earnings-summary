"""Ingest-path wiring for split normalization + quarantine auto-logging.

Exercises the seams in `execution/save_fmp_data.py` that connect the pure
normalizer (`compute.split_normalization`) to the write loop:
- `_normalize_estimates_body` rescales a spliced series when the income statement
  is on disk (authority present),
- it quarantines (returns a reason, no rewrite) when there is no income statement,
- `FmpAnalystEstimateRecord` gates the shape via `_validate_stable_record`,
- a quarantine auto-logs a deferred-FMP task.

The module reads FMP_DIR at import; we point it at a tmp dir per test via
monkeypatch so nothing touches the real cache.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# `save_fmp_data` sys.exit(1)s at import when FMP_API_KEY is unset (an ingest
# guard). These tests exercise only its PURE helpers — no network — so a dummy
# key is enough to import the module. Set it before the import.
os.environ.setdefault("FMP_API_KEY", "test-key-not-used")

import save_fmp_data  # noqa: E402

from pipeline.deferred_fmp import DeferredStatus, list_tasks  # noqa: E402

# BKNG splice with CONTIGUOUS years across the 2023->2024 split boundary (so the
# guard can see the discontinuity even without the income statement authority):
# pre-split historical actuals + post-split forwards.
_EST_BODY: list[dict[str, object]] = [
    {
        "symbol": "BKNG",
        "date": "2026-12-31",
        "revenueAvg": 29_442_509_888,
        "netIncomeAvg": 8_177_473_800,
        "epsAvg": 10.45799,
    },
    {
        "symbol": "BKNG",
        "date": "2025-12-31",
        "revenueAvg": 26_707_323_167,
        "netIncomeAvg": 7_433_536_338,
        "epsAvg": 9.0994,
    },
    {
        "symbol": "BKNG",
        "date": "2024-12-31",
        "revenueAvg": 23_457_215_643,
        "netIncomeAvg": 5_956_437_992,
        "epsAvg": 7.30722,
    },
    {
        "symbol": "BKNG",
        "date": "2023-12-31",
        "revenueAvg": 22_399_000_000,
        "netIncomeAvg": 118_696_721_492,
        "epsAvg": 145.46822,
    },
    {
        "symbol": "BKNG",
        "date": "2022-12-31",
        "revenueAvg": 16_940_842_287,
        "netIncomeAvg": 648_790_265,
        "epsAvg": 97.14105,
    },
    {
        "symbol": "BKNG",
        "date": "2019-12-31",
        "revenueAvg": 15_011_816_409,
        "netIncomeAvg": 4_732_789_135,
        "epsAvg": 101.5258,
    },
]
_INCOME_BODY: list[dict[str, object]] = [
    {
        "symbol": "BKNG",
        "date": "2019-12-31",
        "eps": 4.5168,
        "weightedAverageShsOutDil": 1_087_725_000,
    },
    {
        "symbol": "BKNG",
        "date": "2022-12-31",
        "eps": 3.068,
        "weightedAverageShsOutDil": 1_001_300_000,
    },
    {"symbol": "BKNG", "date": "2025-12-31", "eps": 6.66, "weightedAverageShsOutDil": 815_975_000},
]


@pytest.fixture
def fmp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "fmp"
    d.mkdir()
    monkeypatch.setattr(save_fmp_data, "FMP_DIR", d)
    return d


def _write_income(fmp_dir: Path) -> None:
    import json

    (fmp_dir / "BKNG_income_statement_annual.json").write_text(
        json.dumps(_INCOME_BODY), encoding="utf-8"
    )


def test_normalizes_when_income_present(fmp_dir: Path) -> None:
    _write_income(fmp_dir)
    body, reason, events = save_fmp_data._normalize_estimates_body("BKNG", _EST_BODY)
    assert reason is None
    assert events, "expected rescale events"
    out = {str(r["date"])[:4]: r for r in body}  # type: ignore[union-attr]
    # 2019 rescaled onto the current basis (~4.52), 2026 forward untouched
    assert out["2019"]["epsAvg"] == pytest.approx(4.5168, rel=1e-3)
    assert out["2026"]["epsAvg"] == pytest.approx(10.45799, rel=1e-3)


def test_quarantines_when_no_income(fmp_dir: Path) -> None:
    # no income file written -> no authority -> quarantine
    body, reason, events = save_fmp_data._normalize_estimates_body("BKNG", _EST_BODY)
    assert reason is not None
    assert events == []
    # body passed through unchanged for the raw dump
    assert body is _EST_BODY


def test_quarantine_auto_logs_deferred(
    fmp_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "deferred.jsonl"
    dump_rel = Path("data/.tmp/BKNG_dump.json")
    # redirect the module's default store to the tmp one
    monkeypatch.setattr(save_fmp_data, "default_store_path", lambda _root: store)
    save_fmp_data._log_deferred_split_quarantine(
        "BKNG", "analyst_estimates_annual", "no authority to reconcile", dump_rel
    )
    open_tasks = list_tasks(store, status=DeferredStatus.OPEN)
    assert len(open_tasks) == 1
    assert open_tasks[0].area == "split_normalization"
    assert open_tasks[0].ticker == "BKNG"
    assert open_tasks[0].blocked_on == "fmp_splits_feed"


def test_analyst_estimate_record_gates_shape() -> None:
    # a well-formed row validates; a shape with the required `date` missing drifts
    good = _EST_BODY
    drift = save_fmp_data._validate_stable_record("analyst-estimates", "stable:", good)
    assert drift is None
    bad = [{"symbol": "BKNG"}]  # no `date`
    drift2 = save_fmp_data._validate_stable_record("analyst-estimates", "stable:", bad)
    assert drift2 is not None


def test_non_stable_rung_not_gated() -> None:
    # v3/v4 fallback rungs are passed through unchecked (kind not stable:)
    bad = [{"symbol": "BKNG"}]
    assert save_fmp_data._validate_stable_record("analyst-estimates", "v3", bad) is None
