"""§2 KPI ledger row enrichment — staleness + trend delta (`workspace_data`).

Turns the already-loaded ``KpiLedgerRow.history`` into the two signals the bare
"latest value" cell can't carry: whether the value is current (``kpi_is_stale``)
and how it moved (``kpi_trend_delta``). Both are pure, so they're pinned here
without a DB.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.renderers.workspace_data import (  # noqa: E402
    kpi_is_stale,
    kpi_trend_delta,
)

_REPORT_DATE = date(2026, 6, 2)


def _series(periods: list[str], values: list[float]) -> list[tuple[str, float | None]]:
    return [(p, v) for p, v in zip(periods, values, strict=True)]


# --- staleness -------------------------------------------------------------


def test_recent_quarter_is_fresh() -> None:
    assert kpi_is_stale("2026-03-31", _REPORT_DATE) is False


def test_year_old_fact_is_stale() -> None:
    assert kpi_is_stale("2025-03-31", _REPORT_DATE) is True


def test_one_quarter_behind_is_still_fresh() -> None:
    # Q4 (Dec 31) viewed in early June ≈ 153 days < the ~2-quarter cut.
    assert kpi_is_stale("2025-12-31", _REPORT_DATE) is False


def test_unparseable_period_never_stale() -> None:
    assert kpi_is_stale("not-a-date", _REPORT_DATE) is False


# --- trend delta -----------------------------------------------------------

_FIVE_Q = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31"]


def test_percentage_metric_reads_as_points_yoy() -> None:
    # 25.4 latest vs 25.0 one year back → +0.4 percentage points.
    label, sign = kpi_trend_delta(_series(_FIVE_Q, [25.0, 24.8, 25.1, 25.2, 25.4]), "%", "ROE")
    assert label == "↑ 0.4pp YoY"
    assert sign == "pos"


def test_level_metric_reads_as_percent_change_yoy() -> None:
    label, sign = kpi_trend_delta(
        _series(_FIVE_Q, [100.0, 105.0, 110.0, 115.0, 120.0]), "count", "Total customers"
    )
    assert label == "↑ 20% YoY"
    assert sign == "pos"


def test_falls_back_to_qoq_without_a_year_ago_point() -> None:
    label, sign = kpi_trend_delta(_series(["2026-03-31", "2026-06-30"], [10.0, 9.0]), "%", "Margin")
    assert label == "↓ 1pp QoQ"
    assert sign == "neg"


def test_bps_unit_uses_bps_suffix() -> None:
    label, sign = kpi_trend_delta(
        _series(["2025-03-31", "2026-03-31"], [120.0, 150.0]), "bps", "Risk-adj NIM change"
    )
    assert label == "↑ 30bps YoY"
    assert sign == "pos"


def test_ratio_unit_uses_absolute_delta_no_suffix() -> None:
    label, sign = kpi_trend_delta(
        _series(["2025-03-31", "2026-03-31"], [16.0, 16.9]), "ratio", "Capital adequacy"
    )
    assert label == "↑ 0.9 YoY"
    assert sign == "pos"


def test_name_hint_classifies_when_unit_absent() -> None:
    # No unit, but "margin" in the name → percentage-point reading.
    label, _ = kpi_trend_delta(
        _series(_FIVE_Q, [40.0, 40.0, 40.0, 40.0, 42.0]), None, "Gross margin"
    )
    assert label == "↑ 2pp YoY"


def test_single_observation_has_no_delta() -> None:
    assert kpi_trend_delta(_series(["2026-03-31"], [10.0]), "%", "X") == (None, "")


def test_zero_change_is_suppressed() -> None:
    label, sign = kpi_trend_delta(_series(["2025-03-31", "2026-03-31"], [10.0, 10.0]), "%", "X")
    assert label is None
    assert sign == ""


def test_nulls_in_history_are_skipped() -> None:
    series: list[tuple[str, float | None]] = [
        ("2025-03-31", 25.0),
        ("2025-06-30", None),
        ("2026-03-31", 25.5),
    ]
    label, _ = kpi_trend_delta(series, "%", "ROE")
    assert label == "↑ 0.5pp YoY"
