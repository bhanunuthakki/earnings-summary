"""Focused tests for the shared allocation payload digest."""

from __future__ import annotations

from datetime import datetime

import allocation.eligibility as eligibility_mod
import allocation.recommendation as recommendation_mod
from allocation.digest import allocation_payload_sha

NESTED_PAYLOAD: dict[str, object] = {"b": 1, "a": {"d": 4, "c": 3}}
NESTED_SHA = "943d56ce0b02b80a8afcd12d849426226b68f2d8cd2840af8f6f93067f14c360"

CUSTOM_STR_SHA = "57d4aa4593398f163b1fc0a4ad19a99efee68ef1d375f5c5d84ae4f8fc0e8827"
CASH_SHA = "aab0c51847b8485c87043c112916d6c07fbf1b18d3d3d82c600dc30454a4c5ab"


class _Custom:
    def __str__(self) -> str:
        return "custom-obj"


def test_nested_key_ordering_fixed_vector() -> None:
    assert allocation_payload_sha(NESTED_PAYLOAD) == NESTED_SHA
    assert allocation_payload_sha({"a": {"c": 3, "d": 4}, "b": 1}) == NESTED_SHA


def test_default_str_fixed_vector() -> None:
    assert allocation_payload_sha({"z": _Custom(), "a": [3, 2, 1]}) == CUSTOM_STR_SHA


def test_no_private_sha_duplicates_remain() -> None:
    assert not hasattr(eligibility_mod, "_sha")
    assert not hasattr(recommendation_mod, "_sha")


def test_both_consumers_share_helper() -> None:
    assert eligibility_mod.allocation_payload_sha is allocation_payload_sha
    assert recommendation_mod.allocation_payload_sha is allocation_payload_sha
    assert eligibility_mod.allocation_payload_sha(NESTED_PAYLOAD) == NESTED_SHA
    assert recommendation_mod.allocation_payload_sha(NESTED_PAYLOAD) == NESTED_SHA
    assert (
        eligibility_mod.allocation_payload_sha({"z": _Custom(), "a": [3, 2, 1]}) == CUSTOM_STR_SHA
    )
    assert (
        recommendation_mod.allocation_payload_sha({"z": _Custom(), "a": [3, 2, 1]})
        == CUSTOM_STR_SHA
    )


def test_eligibility_cash_assessment_uses_shared_digest() -> None:
    fixed_now = datetime(2026, 1, 1)
    assessment = eligibility_mod.cash_assessment(now=fixed_now)
    expected = allocation_payload_sha(
        {"ticker": "CASH", "cash": True, "as_of": fixed_now.isoformat()}
    )
    assert expected == CASH_SHA
    assert assessment.input_sha == CASH_SHA
