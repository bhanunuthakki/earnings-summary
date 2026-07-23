"""Tests for allocation.concentration — the PRD §7.2 (P0.2) soft
concentration-zone policy. Pure, no DB/network/LLM.
"""

from __future__ import annotations

from datetime import date

import pytest

from allocation.concentration import (
    ENTRY_APPRECIATION,
    ENTRY_INTENTIONAL,
    TRIM_ASSESSMENT_THRESHOLD_PCT,
    ZONE_BOUNDS,
    classify_entry_method,
    classify_zone,
    zone_at_least,
)

# --------------------------------------------------------------------------- #
# classify_zone — the exact boundary matrix from the PRD §7.2 table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("weight_pct", "expected_zone"),
    [
        (9.99, "ordinary"),
        (10.0, "meaningful"),
        (11.99, "meaningful"),
        (12.0, "concentrated"),
        (14.99, "concentrated"),
        (15.0, "highly_concentrated"),
        (19.99, "highly_concentrated"),
        (20.0, "exceptional"),
        (25.0, "exceptional"),
    ],
)
def test_classify_zone_boundary_matrix(weight_pct: float, expected_zone: str) -> None:
    result = classify_zone(weight_pct)
    assert result is not None
    assert result.zone == expected_zone
    assert result.weight_pct == weight_pct


def test_classify_zone_none_in_none_out() -> None:
    assert classify_zone(None) is None


def test_classify_zone_zero_is_ordinary() -> None:
    result = classify_zone(0.0)
    assert result is not None
    assert result.zone == "ordinary"


# --------------------------------------------------------------------------- #
# trim_assessment_required
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("weight_pct", "expected_required"),
    [
        (9.99, False),
        (11.99, False),
        (12.0, True),
        (14.99, True),
        (20.0, True),
        (25.0, True),
    ],
)
def test_trim_assessment_required_flag(weight_pct: float, expected_required: bool) -> None:
    result = classify_zone(weight_pct)
    assert result is not None
    assert result.trim_assessment_required is expected_required


def test_trim_assessment_threshold_constant() -> None:
    assert TRIM_ASSESSMENT_THRESHOLD_PCT == 12.0


# --------------------------------------------------------------------------- #
# add_evidence_hurdle mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("weight_pct", "expected_hurdle"),
    [
        (5.0, "normal"),
        (11.0, "normal"),
        (13.0, "elevated"),
        (17.0, "high"),
        (22.0, "exceptional"),
    ],
)
def test_add_evidence_hurdle_by_zone(weight_pct: float, expected_hurdle: str) -> None:
    result = classify_zone(weight_pct)
    assert result is not None
    assert result.add_evidence_hurdle == expected_hurdle


def test_treatment_text_is_nonempty_for_every_zone() -> None:
    for lo, _hi, _zone in ZONE_BOUNDS:
        result = classify_zone(lo)
        assert result is not None
        assert result.treatment  # non-empty string


# --------------------------------------------------------------------------- #
# zone_at_least — ordering helper
# --------------------------------------------------------------------------- #


def test_zone_at_least_ordering() -> None:
    order = ["ordinary", "meaningful", "concentrated", "highly_concentrated", "exceptional"]
    for i, floor in enumerate(order):
        for j, zone in enumerate(order):
            assert zone_at_least(zone, floor) is (j >= i)


def test_zone_at_least_same_zone_is_true() -> None:
    assert zone_at_least("concentrated", "concentrated") is True


def test_zone_at_least_unknown_names_fail_closed() -> None:
    assert zone_at_least("bogus", "concentrated") is False
    assert zone_at_least("concentrated", "bogus") is False
    assert zone_at_least("bogus", "also_bogus") is False


# --------------------------------------------------------------------------- #
# classify_entry_method — window edges
# --------------------------------------------------------------------------- #


def test_classify_entry_method_buy_exactly_window_days_ago_is_intentional() -> None:
    # window_days=180 -> floor = as_of - 180 days = 2026-01-24 (inclusive).
    as_of = date(2026, 7, 23)
    floor = date(2026, 1, 24)
    assert classify_entry_method([floor.isoformat()], as_of=as_of, window_days=180) == (
        ENTRY_INTENTIONAL
    )


def test_classify_entry_method_buy_one_day_older_than_window_is_appreciation() -> None:
    as_of = date(2026, 7, 23)
    older = date(2026, 1, 23)  # one day before the floor (2026-01-24)
    assert classify_entry_method([older.isoformat()], as_of=as_of, window_days=180) == (
        ENTRY_APPRECIATION
    )


def test_classify_entry_method_empty_list_is_appreciation() -> None:
    assert classify_entry_method([], as_of=date(2026, 7, 23)) == ENTRY_APPRECIATION


def test_classify_entry_method_buy_today_is_intentional() -> None:
    as_of = date(2026, 7, 23)
    assert classify_entry_method([as_of.isoformat()], as_of=as_of) == ENTRY_INTENTIONAL


def test_classify_entry_method_recent_buy_among_old_ones_is_intentional() -> None:
    as_of = date(2026, 7, 23)
    dates = ["2020-01-01", "2021-06-15", "2026-06-01"]  # last one is recent
    assert classify_entry_method(dates, as_of=as_of, window_days=180) == ENTRY_INTENTIONAL


def test_classify_entry_method_skips_unparseable_dates() -> None:
    as_of = date(2026, 7, 23)
    assert classify_entry_method(["not-a-date", ""], as_of=as_of) == ENTRY_APPRECIATION


def test_classify_entry_method_default_window_is_180_days() -> None:
    as_of = date(2026, 7, 23)
    just_inside = as_of.toordinal() - 180
    just_inside_date = date.fromordinal(just_inside)
    just_outside_date = date.fromordinal(just_inside - 1)
    assert classify_entry_method([just_inside_date.isoformat()], as_of=as_of) == ENTRY_INTENTIONAL
    assert classify_entry_method([just_outside_date.isoformat()], as_of=as_of) == (
        ENTRY_APPRECIATION
    )
