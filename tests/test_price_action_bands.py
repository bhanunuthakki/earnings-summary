"""Fail-closed contracts for the BHA-85 governed price-action ladder."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from advisor.price_action_bands import (
    PriceActionApproachBands,
    PriceActionBands,
    PriceActionProjectionState,
    resolve_price_action_bands,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


def _bands(*, revision: str = "v1", missing_sell: bool = False) -> PriceActionBands:
    approach = PriceActionApproachBands(
        add_buy_below=82.0,
        trim_above=98.0,
        sell_above=None if missing_sell else 118.0,
    )
    return PriceActionBands(
        add_below=80.0,
        hold_low=80.0,
        hold_high=100.0,
        trim_above=100.0,
        sell_above=None if missing_sell else 120.0,
        approach_bands=approach,
        currency="USD",
        owner="owner@example.test",
        revision=revision,
        as_of=NOW,
        source_ref="owner-decision-checkpoint:42",
        source_content_sha256="a" * 64,
    )


def test_meli_checkpoint_ratified_bands_are_the_only_actionable_state() -> None:
    projection = resolve_price_action_bands(
        owner_ratified=_bands(revision="ratified-v2"),
        checkpoint_id=42,
        checkpoint_payload_sha256="b" * 64,
    )

    assert projection.state is PriceActionProjectionState.RATIFIED
    assert projection.revision == "ratified-v2"
    assert projection.source_kind == "structured_owner"
    assert projection.approval_state == "owner_ratified"
    assert projection.source_ref == "owner-decision-checkpoint:42"
    assert projection.source_content_sha256 == "b" * 64
    assert projection.declared_source_ref == "owner-decision-checkpoint:42"
    assert projection.declared_source_content_sha256 == "a" * 64
    assert projection.is_actionable is True


def test_partial_checkpoint_bands_fail_closed_without_fallback() -> None:
    partial = resolve_price_action_bands(
        owner_ratified=_bands(missing_sell=True),
        checkpoint_id=42,
        checkpoint_payload_sha256="b" * 64,
    )

    assert partial.state is PriceActionProjectionState.PARTIAL
    assert partial.sell_above is None
    assert partial.is_actionable is False


def test_missing_bands_are_unencoded_or_unavailable_not_invented() -> None:
    unencoded = resolve_price_action_bands(
        owner_ratified=None,
    )
    unavailable = resolve_price_action_bands(
        owner_ratified=None,
        source_available=False,
    )

    assert unencoded.state is PriceActionProjectionState.UNENCODED
    assert unencoded.is_actionable is False
    assert unavailable.state is PriceActionProjectionState.UNAVAILABLE
    assert unavailable.currency is None
    assert unavailable.is_actionable is False


def test_unambiguous_ladders_require_distinct_trim_and_sell_thresholds() -> None:
    invalid = _bands().model_dump()
    invalid["sell_above"] = 100.0
    with pytest.raises(ValueError, match="sell_above"):
        PriceActionBands.model_validate(invalid)


def test_approach_thresholds_must_precede_their_breach() -> None:
    invalid = _bands().model_dump()
    invalid["approach_bands"] = {"add_buy_below": 80.0}
    with pytest.raises(ValueError, match="add/buy approach"):
        PriceActionBands.model_validate(invalid)
