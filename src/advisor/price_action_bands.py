"""Typed, fail-closed price-action-band selection for owner sizing evidence.

This module only projects checkpoint-ratified structured bands. It never
parses owner narrative, fills a missing rung, derives a DCF ladder, or turns a
selected band into a trade.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "PriceActionApproachBands",
    "PriceActionBandProjection",
    "PriceActionBands",
    "PriceActionProjectionState",
    "resolve_price_action_bands",
]


class PriceActionProjectionState(StrEnum):
    """Truthful availability state for a selected price-action ladder."""

    RATIFIED = "ratified"
    DRAFT = "draft"
    DERIVED = "derived"
    PARTIAL = "partial"
    STALE = "stale"
    UNENCODED = "unencoded"
    UNAVAILABLE = "unavailable"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PriceActionApproachBands(_FrozenModel):
    """Optional approach thresholds; their absence never implies a threshold."""

    add_buy_below: float | None = Field(default=None, gt=0)
    trim_above: float | None = Field(default=None, gt=0)
    sell_above: float | None = Field(default=None, gt=0)


class PriceActionBands(_FrozenModel):
    """One explicitly encoded price-action ladder and its source evidence."""

    add_below: float | None = Field(default=None, gt=0)
    hold_low: float | None = Field(default=None, gt=0)
    hold_high: float | None = Field(default=None, gt=0)
    trim_above: float | None = Field(default=None, gt=0)
    sell_above: float | None = Field(default=None, gt=0)
    approach_bands: PriceActionApproachBands | None = None
    currency: str
    owner: str
    revision: str
    as_of: datetime
    source_ref: str
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a three-letter ISO code")
        return normalized

    @field_validator("owner", "revision", "source_ref")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("price-action-band provenance fields must be non-empty")
        return normalized

    @field_validator("as_of")
    @classmethod
    def _aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _ordered_known_rungs(self) -> PriceActionBands:
        known = [
            value
            for value in (
                self.add_below,
                self.hold_low,
                self.hold_high,
                self.trim_above,
                self.sell_above,
            )
            if value is not None
        ]
        if known != sorted(known):
            raise ValueError("known price-action rungs must be nondecreasing")
        if (
            self.trim_above is not None
            and self.sell_above is not None
            and self.trim_above >= self.sell_above
        ):
            raise ValueError("sell_above must be strictly greater than trim_above")
        if self.approach_bands is not None:
            if self.approach_bands.add_buy_below is not None and self.add_below is None:
                raise ValueError("add/buy approach requires add_below")
            if self.approach_bands.trim_above is not None and self.trim_above is None:
                raise ValueError("trim approach requires trim_above")
            if self.approach_bands.sell_above is not None and self.sell_above is None:
                raise ValueError("sell approach requires sell_above")
            if (
                self.approach_bands.add_buy_below is not None
                and self.add_below is not None
                and self.approach_bands.add_buy_below <= self.add_below
            ):
                raise ValueError("add/buy approach must occur strictly before add_below")
            if (
                self.approach_bands.trim_above is not None
                and self.trim_above is not None
                and self.approach_bands.trim_above >= self.trim_above
            ):
                raise ValueError("trim approach must occur strictly before trim_above")
            if (
                self.approach_bands.sell_above is not None
                and self.sell_above is not None
                and self.approach_bands.sell_above >= self.sell_above
            ):
                raise ValueError("sell approach must occur strictly before sell_above")
        return self

    @property
    def is_complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.add_below,
                self.hold_low,
                self.hold_high,
                self.trim_above,
                self.sell_above,
            )
        )


class PriceActionBandProjection(_FrozenModel):
    """The one selected ladder, with an explicit no-arm contract."""

    state: PriceActionProjectionState
    source_kind: Literal["structured_owner", "dcf_derived"] | None = None
    approval_state: Literal["owner_ratified", "draft_owner_intent", "not_owner_approved"] | None = (
        None
    )
    add_below: float | None = None
    hold_low: float | None = None
    hold_high: float | None = None
    trim_above: float | None = None
    sell_above: float | None = None
    approach_bands: PriceActionApproachBands | None = None
    currency: str | None = None
    owner: str | None = None
    revision: str | None = None
    as_of: datetime | None = None
    source_ref: str | None = None
    source_content_sha256: str | None = None
    declared_source_ref: str | None = None
    declared_source_content_sha256: str | None = None
    is_actionable: bool
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _actionability(self) -> PriceActionBandProjection:
        complete = all(
            value is not None
            for value in (
                self.add_below,
                self.hold_low,
                self.hold_high,
                self.trim_above,
                self.sell_above,
            )
        )
        if self.is_actionable and (
            self.state is not PriceActionProjectionState.RATIFIED
            or self.source_kind != "structured_owner"
            or self.approval_state != "owner_ratified"
            or not complete
            or self.owner is None
            or self.source_ref is None
            or self.source_content_sha256 is None
        ):
            raise ValueError("only complete checkpoint-ratified owner bands are actionable")
        return self


def resolve_price_action_bands(
    *,
    owner_ratified: PriceActionBands | None,
    checkpoint_id: int | None = None,
    checkpoint_payload_sha256: str | None = None,
    source_available: bool = True,
) -> PriceActionBandProjection:
    """Project verified checkpoint bands without inventing another source.

    Draft owner intent and DCF-derived bands are not present in the canonical
    sizing-intent store yet. They remain explicit future integration states,
    never hidden fallbacks at this boundary.
    """

    if not source_available:
        return PriceActionBandProjection(
            state=PriceActionProjectionState.UNAVAILABLE,
            is_actionable=False,
            reason_codes=("price_action_band_source_unavailable",),
        )
    if owner_ratified is not None:
        if checkpoint_id is None or checkpoint_id < 1:
            raise ValueError("checkpoint-ratified bands require a checkpoint ID")
        if checkpoint_payload_sha256 is None or len(checkpoint_payload_sha256) != 64:
            raise ValueError("checkpoint-ratified bands require a payload SHA-256")
        return _project_owner(
            owner_ratified,
            checkpoint_id=checkpoint_id,
            checkpoint_payload_sha256=checkpoint_payload_sha256,
        )
    return PriceActionBandProjection(
        state=PriceActionProjectionState.UNENCODED,
        is_actionable=False,
        reason_codes=("price_action_bands_unencoded",),
    )


def _project_owner(
    bands: PriceActionBands,
    *,
    checkpoint_id: int,
    checkpoint_payload_sha256: str,
) -> PriceActionBandProjection:
    state = (
        PriceActionProjectionState.RATIFIED
        if bands.is_complete
        else PriceActionProjectionState.PARTIAL
    )
    return PriceActionBandProjection(
        state=state,
        source_kind="structured_owner",
        approval_state="owner_ratified",
        add_below=bands.add_below,
        hold_low=bands.hold_low,
        hold_high=bands.hold_high,
        trim_above=bands.trim_above,
        sell_above=bands.sell_above,
        approach_bands=bands.approach_bands,
        currency=bands.currency,
        owner=bands.owner,
        revision=bands.revision,
        as_of=bands.as_of,
        source_ref=f"owner-decision-checkpoint:{checkpoint_id}",
        source_content_sha256=checkpoint_payload_sha256,
        declared_source_ref=bands.source_ref,
        declared_source_content_sha256=bands.source_content_sha256,
        is_actionable=state is PriceActionProjectionState.RATIFIED,
        reason_codes=("price_action_bands_partial",)
        if state is PriceActionProjectionState.PARTIAL
        else (),
    )
