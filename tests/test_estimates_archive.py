"""Tests for src/estimates_archive.py — the point-in-time FMP consensus
reader. Fixture snapshot dirs only; honest-gap semantics are the contract
under test: latest snapshot <= asof, not_available before archive start,
never interpolate."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from estimates_archive import (
    SUPPORTED_METRICS,
    estimate_asof,
    snapshot_dates,
    ticker_archive_dates,
)


def _write_snapshot(root: Path, snap_date: str, ticker: str, rows: list[dict[str, object]]) -> None:
    d = root / snap_date
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}_analyst_estimates_annual.json").write_text(json.dumps(rows), encoding="utf-8")


def _row(fy: int, revenue: float, eps: float | None = None) -> dict[str, object]:
    return {
        "symbol": "MELI",
        "date": f"{fy}-12-31",
        "revenueAvg": revenue,
        "epsAvg": eps,
        "numAnalystsRevenue": 12,
    }


@pytest.fixture()
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "fmp_snapshots"
    # 2026-05-19: FY2026+FY2027 rows. 2026-06-10: revised FY2027, adds FY2028.
    _write_snapshot(root, "2026-05-19", "MELI", [_row(2026, 30.0e9), _row(2027, 36.0e9)])
    _write_snapshot(
        root,
        "2026-06-10",
        "MELI",
        [_row(2026, 30.5e9), _row(2027, 37.0e9, eps=41.5), _row(2028, 44.0e9)],
    )
    (root / "not-a-date").mkdir()  # ignored
    return root


def test_snapshot_dates_sorted_and_filtered(archive: Path) -> None:
    assert snapshot_dates(archive) == ["2026-05-19", "2026-06-10"]


def test_ticker_archive_dates_only_dates_with_the_file(archive: Path) -> None:
    _write_snapshot(archive, "2026-06-15", "OTHER", [_row(2026, 1.0e9)])
    assert ticker_archive_dates("MELI", archive) == ["2026-05-19", "2026-06-10"]
    assert ticker_archive_dates("OTHER", archive) == ["2026-06-15"]


def test_asof_between_snapshots_uses_earlier_never_interpolates(archive: Path) -> None:
    """As-of 2026-06-01 the world only knew the 05-19 snapshot: FY2027 must be
    the OLD 36.0B, not the later revision and not a blend."""
    ans = estimate_asof("MELI", "revenueAvg", 2027, date(2026, 6, 1), snapshots_dir=archive)
    assert ans.status == "ok"
    assert ans.snapshot_date == "2026-05-19"
    assert ans.value == pytest.approx(36.0e9)
    assert ans.archive_start == "2026-05-19"


def test_asof_after_latest_uses_latest(archive: Path) -> None:
    ans = estimate_asof("MELI", "revenueAvg", 2027, date(2026, 7, 1), snapshots_dir=archive)
    assert ans.status == "ok"
    assert ans.snapshot_date == "2026-06-10"
    assert ans.value == pytest.approx(37.0e9)


def test_asof_on_snapshot_day_inclusive(archive: Path) -> None:
    ans = estimate_asof("MELI", "revenueAvg", 2026, date(2026, 5, 19), snapshots_dir=archive)
    assert ans.status == "ok"
    assert ans.snapshot_date == "2026-05-19"
    assert ans.value == pytest.approx(30.0e9)


def test_asof_before_archive_start_is_honest_gap(archive: Path) -> None:
    ans = estimate_asof("MELI", "revenueAvg", 2026, date(2026, 4, 1), snapshots_dir=archive)
    assert ans.status == "not_available"
    assert ans.value is None
    assert ans.archive_start == "2026-05-19"
    assert "never interpolated" in (ans.detail or "")


def test_fiscal_year_beyond_snapshot_horizon(archive: Path) -> None:
    """FY2028 only exists in the 06-10 snapshot — as of 06-01 it is a honest
    no_data_for_year (the 10-row cap in action), not a projection."""
    ans = estimate_asof("MELI", "revenueAvg", 2028, date(2026, 6, 1), snapshots_dir=archive)
    assert ans.status == "no_data_for_year"
    assert ans.snapshot_date == "2026-05-19"
    assert ans.value is None


def test_unknown_metric_rejected(archive: Path) -> None:
    ans = estimate_asof("MELI", "priceTargetAvg", 2027, date(2026, 7, 1), snapshots_dir=archive)
    assert ans.status == "unknown_metric"
    assert "revenueAvg" in (ans.detail or "")


def test_unknown_ticker_not_available(archive: Path) -> None:
    ans = estimate_asof("ZZZZ", "revenueAvg", 2027, date(2026, 7, 1), snapshots_dir=archive)
    assert ans.status == "not_available"


def test_field_null_at_source_stays_null(archive: Path) -> None:
    """epsAvg is absent on the 05-19 FY2027 row: status ok, value None, with
    the detail naming the source nullity — never borrowed from another
    snapshot or field."""
    ans = estimate_asof("MELI", "epsAvg", 2027, date(2026, 6, 1), snapshots_dir=archive)
    assert ans.status == "ok"
    assert ans.value is None
    assert ans.detail == "row present but field null at source"


def test_missing_root_dir(tmp_path: Path) -> None:
    ans = estimate_asof(
        "MELI", "revenueAvg", 2027, date(2026, 7, 1), snapshots_dir=tmp_path / "nope"
    )
    assert ans.status == "not_available"


def test_supported_metrics_match_fmp_model_fields() -> None:
    from models.fmp_payloads import FmpAnalystEstimateRecord

    for metric in SUPPORTED_METRICS:
        assert metric in FmpAnalystEstimateRecord.model_fields
