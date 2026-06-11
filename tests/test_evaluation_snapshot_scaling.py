"""Regression: eval-screen ratio rows render percent POINTS, not raw fractions.

The `ratios` view stores fractions (operating_income / revenue → 0.062). Before
the PR1 fix the quick-categorization table passed them through with unit "%",
so a 6.2% operating margin rendered as "0.1%" (UBER's real eval screen showed
op margin 0.0/0.1/0.1% and ROE 0.2/0.5/0.4%).
"""

from __future__ import annotations

from report.sections.evaluation_snapshot import (
    _abs_row,  # pyright: ignore[reportPrivateUsage]  # testing the scaling seam
    _ratio_row,  # pyright: ignore[reportPrivateUsage]
)


def test_ratio_row_converts_fractions_to_percent_points() -> None:
    by_period_r: dict[int, dict[str, object]] = {
        2023: {"operating_margin": 0.026},
        2024: {"operating_margin": 0.062},
        2025: {"operating_margin": 0.071},
    }
    row = _ratio_row(
        "Operating margin",
        "%",
        1,
        by_period_r,
        {"operating_margin": 0.0685},
        "operating_margin",
        [2023, 2024, 2025],
    )
    assert row.lfy_minus_2 is not None and abs(row.lfy_minus_2 - 2.6) < 1e-9
    assert row.lfy_minus_1 is not None and abs(row.lfy_minus_1 - 6.2) < 1e-9
    assert row.lfy is not None and abs(row.lfy - 7.1) < 1e-9
    assert row.ttm is not None and abs(row.ttm - 6.85) < 1e-9
    assert row.cagr_3y is None  # ratios never get a CAGR


def test_ratio_row_missing_values_stay_none() -> None:
    row = _ratio_row("ROE", "%", 1, {}, None, "roe", [2023, 2024, 2025])
    assert row.lfy_minus_2 is None
    assert row.lfy is None
    assert row.ttm is None


def test_abs_row_scaling_unchanged() -> None:
    by_period_m: dict[int, dict[str, object]] = {
        2022: {"revenue": 30_000_000_000.0},
        2023: {"revenue": 37_000_000_000.0},
        2024: {"revenue": 41_000_000_000.0},
        2025: {"revenue": 44_000_000_000.0},
    }
    row = _abs_row(
        "Revenue",
        "USD M",
        0,
        by_period_m,
        {"revenue": 45_500_000_000.0},
        "revenue",
        [2023, 2024, 2025],
        2022,
        scale=1_000_000.0,
    )
    assert row.lfy == 44_000.0  # USD M
    assert row.ttm == 45_500.0
    assert row.cagr_3y is not None and abs(row.cagr_3y - ((44 / 30) ** (1 / 3) - 1)) < 1e-9
