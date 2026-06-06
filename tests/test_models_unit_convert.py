"""Tests for models.unit_convert.convert_unit.

The single reconciliation helper shared by the persist path
(`pipeline.kpi_persistence`) and the break-rule compare path. Within a
dimensional family a value rescales by the ratio of magnitudes; across families
there is no valid conversion (returns None), so a caller never rescales a
money magnitude onto a percentage or a raw count.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from models.facts import Unit
from models.unit_convert import convert_unit


def test_identity_is_exact() -> None:
    # Same unit returns the value unchanged — exact, no float drift.
    assert convert_unit(Decimal("12.5"), Unit.PERCENT, Unit.PERCENT) == Decimal("12.5")
    assert convert_unit(Decimal("0"), Unit.ACTUAL, Unit.ACTUAL) == Decimal("0")


def test_magnitude_family_rescales_by_powers_of_ten() -> None:
    # The RBRK case: raw dollars -> millions (the break-rule's unit).
    assert convert_unit(Decimal("115000000"), Unit.ACTUAL, Unit.MILLIONS) == Decimal("115")
    assert convert_unit(Decimal("115"), Unit.MILLIONS, Unit.ACTUAL) == Decimal("115000000")
    assert convert_unit(Decimal("2"), Unit.BILLIONS, Unit.MILLIONS) == Decimal("2000")
    assert convert_unit(Decimal("500"), Unit.THOUSANDS, Unit.MILLIONS) == Decimal("0.5")


def test_proportion_family_rescales_through_common_base() -> None:
    assert convert_unit(Decimal("17.8"), Unit.PERCENT, Unit.RATIO) == Decimal("0.178")
    assert convert_unit(Decimal("0.178"), Unit.RATIO, Unit.PERCENT) == Decimal("17.800")
    assert convert_unit(Decimal("1"), Unit.PERCENT, Unit.BPS) == Decimal("100")
    assert convert_unit(Decimal("250"), Unit.BPS, Unit.PERCENT) == Decimal("2.50")


@pytest.mark.parametrize(
    ("from_unit", "to_unit"),
    [
        (Unit.ACTUAL, Unit.PERCENT),  # money magnitude vs proportion
        (Unit.PERCENT, Unit.MILLIONS),
        (Unit.COUNT, Unit.MILLIONS),  # raw count vs money magnitude
        (Unit.COUNT, Unit.PERCENT),
        (Unit.MILLIONS, Unit.COUNT),
    ],
)
def test_cross_family_returns_none(from_unit: Unit, to_unit: Unit) -> None:
    # No dimensionally-valid conversion across families — caller must not rescale.
    assert convert_unit(Decimal("1"), from_unit, to_unit) is None


def test_count_only_converts_to_itself() -> None:
    assert convert_unit(Decimal("5"), Unit.COUNT, Unit.COUNT) == Decimal("5")
    assert convert_unit(Decimal("5"), Unit.COUNT, Unit.ACTUAL) is None
