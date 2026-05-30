"""Unit tests for src/user_state/kpi_polarity.py (auto-seed correctness #2).

Pure functions — no LLM, no DB. Covers:
  * adverse_direction both ways (the #1 rule: adverse = opposite of good).
  * KNOWN_KPI_POLARITY table hits + misses, including case-insensitive
    substring matching and unknown -> None.
"""

from __future__ import annotations

import pytest

from user_state.kpi_polarity import (
    KNOWN_KPI_POLARITY,
    Polarity,
    adverse_direction,
    infer_polarity_from_table,
)

# ---------------------------------------------------------------------------
# adverse_direction — adverse = the OPPOSITE of good
# ---------------------------------------------------------------------------


def test_adverse_direction_higher_is_better_is_below() -> None:
    # NIM-style: higher is good, so the adverse cross fires when it falls.
    assert adverse_direction(Polarity.HIGHER_IS_BETTER) == "below"


def test_adverse_direction_lower_is_better_is_above() -> None:
    # churn-style: lower is good, so the adverse cross fires when it rises.
    assert adverse_direction(Polarity.LOWER_IS_BETTER) == "above"


def test_adverse_direction_covers_every_polarity_value() -> None:
    """Every enum member maps to a valid registry direction string."""
    for polarity in Polarity:
        assert adverse_direction(polarity) in {"above", "below"}


# ---------------------------------------------------------------------------
# infer_polarity_from_table — higher/lower hits, case-insensitive, misses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kpi_name",
    ["NIM", "Net Interest Margin", "Net dollar retention", "NDR", "FCF margin", "GMV"],
)
def test_table_higher_is_better_hits(kpi_name: str) -> None:
    assert infer_polarity_from_table(kpi_name) is Polarity.HIGHER_IS_BETTER


@pytest.mark.parametrize(
    "kpi_name",
    ["Monthly churn", "NPL ratio", "CAC payback (months)", "Net charge-off rate"],
)
def test_table_lower_is_better_hits(kpi_name: str) -> None:
    assert infer_polarity_from_table(kpi_name) is Polarity.LOWER_IS_BETTER


def test_table_is_case_insensitive_substring() -> None:
    # Mixed case + surrounding text still matches the lowercased substring key.
    assert (
        infer_polarity_from_table("Consolidated NET INTEREST MARGIN (annualized)")
        is Polarity.HIGHER_IS_BETTER
    )
    assert infer_polarity_from_table("Gross Churn % (monthly)") is Polarity.LOWER_IS_BETTER


def test_table_unknown_returns_none() -> None:
    assert infer_polarity_from_table("Total customers (millions)") is None
    assert infer_polarity_from_table("") is None
    assert infer_polarity_from_table("Some bespoke KPI with no table entry") is None


def test_polarity_enum_string_values_round_trip() -> None:
    """The enum's string values are exactly the JSON the LLM emits, so a raw
    response value reconstructs the enum member."""
    assert Polarity("higher_is_better") is Polarity.HIGHER_IS_BETTER
    assert Polarity("lower_is_better") is Polarity.LOWER_IS_BETTER
    # And the table only ever holds valid Polarity members.
    assert all(isinstance(v, Polarity) for v in KNOWN_KPI_POLARITY.values())
