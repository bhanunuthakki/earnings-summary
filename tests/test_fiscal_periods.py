from __future__ import annotations

from dcf.fiscal_periods import DEFAULT_PERIODS, detect_fy_periods


def _keys(years: list[int], periods: tuple[str, ...]) -> dict[tuple[int, str], object]:
    return {(y, p): {} for y in years for p in periods}


def test_quarterly_cadence_detected() -> None:
    records = _keys([2022, 2023, 2024], ("Q1", "Q2", "Q3", "Q4"))
    assert detect_fy_periods(records) == ("Q1", "Q2", "Q3", "Q4")


def test_semiannual_cadence_detected() -> None:
    records = _keys([2022, 2023, 2024], ("Q2", "Q4"))
    assert detect_fy_periods(records) == ("Q2", "Q4")


def test_partial_current_year_not_mistaken_for_cadence() -> None:
    records = _keys([2022, 2023], ("Q1", "Q2", "Q3", "Q4"))
    records[(2024, "Q1")] = {}
    assert detect_fy_periods(records) == ("Q1", "Q2", "Q3", "Q4")


def test_largest_recurring_set_wins() -> None:
    records = _keys([2022, 2023], ("Q1", "Q2", "Q3", "Q4"))
    records[(2024, "Q1")] = {}
    records[(2025, "Q1")] = {}
    assert detect_fy_periods(records) == ("Q1", "Q2", "Q3", "Q4")


def test_too_little_history_falls_back_to_default() -> None:
    assert detect_fy_periods({}) == ("Q1", "Q2", "Q3", "Q4")
    assert detect_fy_periods({}) == DEFAULT_PERIODS
    single_year = _keys([2024], ("Q2", "Q4"))
    assert detect_fy_periods(single_year) == ("Q1", "Q2", "Q3", "Q4")


def test_custom_default_honored() -> None:
    assert detect_fy_periods({}, default=("H1", "H2")) == ("H1", "H2")
