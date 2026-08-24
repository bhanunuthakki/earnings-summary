"""Typed checkpoint boundary for explicit owner sizing-intent actions.

This is deliberately a narrow adapter around the existing immutable owner
decision checkpoint writer.  It neither derives portfolio values nor executes
trades: callers must supply the already-reviewed decision context and receive
the persisted sizing-intent evidence back after the atomic confirmation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from advisor.sizing_intent_review import (
    SizingIntentReviewEntry,
    load_sizing_intent_review,
    load_sizing_intent_review_entry,
)
from identity import DEFAULT_USER_ID
from research.owner_decision_checkpoint import (
    CheckpointConfirmation,
    CheckpointConflictError,
    CheckpointInvariantError,
    DecisionLeg,
    HoldingsBasis,
    LedgerEntrySpec,
    OwnerDecisionCheckpointPayload,
    SizingIntentSpec,
    TargetBand,
    confirm_owner_decision_checkpoint,
)

_SOURCE_CHANNEL = "localhost_sizing_intent_checkpoint_api"


class SizingIntentCheckpointValidationError(ValueError):
    """The request is structurally valid JSON but not a permitted action."""


class SizingIntentCheckpointConflictError(RuntimeError):
    """The caller's expected sizing-intent revision is no longer current."""


class SizingIntentCheckpointUnavailableError(RuntimeError):
    """The required local checkpoint or projection source is unavailable."""


class SizingIntentCheckpointRequest(BaseModel):
    """One ticker-scoped, idempotent owner action.

    ``expected_prior_sizing_intent_id`` is a compare-and-swap guard.  An add
    requires an empty current state; a revise appends a new immutable intent;
    and a ratify links the already-recorded intent to checkpoint evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["add", "revise", "ratify"]
    source_event_id: str = Field(min_length=1)
    expected_prior_sizing_intent_id: int | None = Field(default=None, ge=1)
    retrospective: bool = False
    holdings_basis: HoldingsBasis
    leg: DecisionLeg
    sizing_intent: SizingIntentSpec
    ledger_entries: tuple[LedgerEntrySpec, ...] = ()

    @field_validator("source_event_id")
    @classmethod
    def _source_event_id(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("source_event_id is required")
        return clean

    @model_validator(mode="after")
    def _action_matches_intent_link(self) -> SizingIntentCheckpointRequest:
        existing = self.sizing_intent.existing_sizing_intent_id
        expected = self.expected_prior_sizing_intent_id
        if self.action == "add":
            if existing is not None or expected is not None:
                raise ValueError("add requires no existing sizing intent or prior revision")
        elif self.action == "revise":
            if existing is not None or expected is None:
                raise ValueError(
                    "revise requires an expected prior revision and a new sizing intent"
                )
        elif existing != expected or expected is None:
            raise ValueError("ratify must link the expected prior sizing intent")
        return self

    def checkpoint_payload(self) -> OwnerDecisionCheckpointPayload:
        """Construct the canonical existing writer payload; no second writer exists."""

        return OwnerDecisionCheckpointPayload(
            source_channel=_SOURCE_CHANNEL,
            source_event_id=self.source_event_id,
            retrospective=self.retrospective,
            holdings_basis=self.holdings_basis,
            legs=(self.leg,),
            sizing_intents=(self.sizing_intent,),
            ledger_entries=self.ledger_entries,
        )


class SizingIntentReadProjection(BaseModel):
    """The exact persisted review entry returned after confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sizing_intent_id: int
    ticker: str
    intent_kind: str
    intent_value: float | None
    narrative: str | None
    checkpoint_id: int | None
    checkpoint_payload_sha256: str | None
    checkpoint_evidence_available: bool
    target_band: TargetBand | None


class SizingIntentCheckpointResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt: CheckpointConfirmation
    projection: SizingIntentReadProjection


def confirm_sizing_intent_checkpoint(
    request: SizingIntentCheckpointRequest,
    *,
    ticker: str,
    db_path: Path | str,
    user_id: str = DEFAULT_USER_ID,
) -> SizingIntentCheckpointResult:
    """Confirm one owner sizing action and return its read-after-write projection."""

    clean_ticker = ticker.strip().upper()
    if not clean_ticker:
        raise SizingIntentCheckpointValidationError("ticker is required")
    if request.leg.ticker != clean_ticker or request.sizing_intent.ticker != clean_ticker:
        raise SizingIntentCheckpointValidationError("route ticker must match leg and sizing intent")
    if any(entry.ticker != clean_ticker for entry in request.ledger_entries):
        raise SizingIntentCheckpointValidationError("ledger entries must match the route ticker")

    _current_entry(request, db_path=db_path, user_id=user_id)
    try:
        receipt = confirm_owner_decision_checkpoint(
            request.checkpoint_payload(),
            db_path=db_path,
            user_id=user_id,
            before_create=lambda conn: _check_expected_prior_in_transaction(
                conn, request=request, user_id=user_id
            ),
        )
    except SizingIntentCheckpointConflictError:
        raise
    except CheckpointConflictError as exc:
        raise SizingIntentCheckpointConflictError(str(exc)) from None
    except CheckpointInvariantError as exc:
        raise SizingIntentCheckpointConflictError(str(exc)) from None
    except sqlite3.IntegrityError as exc:
        raise SizingIntentCheckpointConflictError("checkpoint state changed concurrently") from exc
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        raise SizingIntentCheckpointUnavailableError("checkpoint storage is unavailable") from exc

    if len(receipt.sizing_intent_ids) != 1:
        raise SizingIntentCheckpointUnavailableError(
            "checkpoint receipt has no unique sizing intent"
        )
    intent_id = receipt.sizing_intent_ids[0]
    after = _entry_for_id(intent_id, db_path=db_path, user_id=user_id)
    if after is None or after.intent.id != intent_id:
        raise SizingIntentCheckpointUnavailableError(
            "sizing-intent projection is unavailable after write"
        )
    if after.checkpoint_id != receipt.checkpoint_id:
        raise SizingIntentCheckpointUnavailableError(
            "checkpoint linkage is unavailable after write"
        )
    return SizingIntentCheckpointResult(
        receipt=receipt,
        projection=_projection(after),
    )


def _current_entry(
    request: SizingIntentCheckpointRequest,
    *,
    db_path: Path | str,
    user_id: str,
) -> SizingIntentReviewEntry | None:
    review = load_sizing_intent_review(db_path, user_id=user_id)
    if not review.sizing_intent_source_available:
        raise SizingIntentCheckpointUnavailableError("sizing-intent source is unavailable")
    return next(
        (
            entry
            for entry in review.entries
            if entry.intent.ticker == request.sizing_intent.ticker
            and entry.intent.intent_kind == request.sizing_intent.intent_kind
        ),
        None,
    )


def _entry_for_id(
    sizing_intent_id: int,
    *,
    db_path: Path | str,
    user_id: str,
) -> SizingIntentReviewEntry | None:
    review = load_sizing_intent_review_entry(
        db_path, sizing_intent_id=sizing_intent_id, user_id=user_id
    )
    if not review.sizing_intent_source_available or not review.checkpoint_link_source_available:
        raise SizingIntentCheckpointUnavailableError(
            "sizing-intent projection source is unavailable"
        )
    return review.entries[0] if review.entries else None


def _check_expected_prior_in_transaction(
    conn: sqlite3.Connection,
    *,
    request: SizingIntentCheckpointRequest,
    user_id: str,
) -> None:
    row = conn.execute(
        """
        SELECT id FROM position_sizing_intent
        WHERE user_id=? AND ticker=? AND intent_kind=?
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (user_id, request.sizing_intent.ticker, request.sizing_intent.intent_kind),
    ).fetchone()
    actual = None if row is None else int(row["id"])
    expected = request.expected_prior_sizing_intent_id
    if request.action == "add":
        if actual is not None:
            raise SizingIntentCheckpointConflictError(
                "sizing intent already exists; revise it with its current revision"
            )
        return
    if (
        request.action == "ratify"
        and actual is not None
        and conn.execute(
            "SELECT 1 FROM owner_decision_checkpoint_sizing_intents WHERE sizing_intent_id=?",
            (actual,),
        ).fetchone()
        is not None
    ):
        raise SizingIntentCheckpointConflictError(
            "sizing intent is already linked to checkpoint evidence"
        )
    if actual != expected:
        raise SizingIntentCheckpointConflictError(
            "expected prior sizing-intent revision is not current"
        )


def _projection(entry: SizingIntentReviewEntry) -> SizingIntentReadProjection:
    return SizingIntentReadProjection(
        sizing_intent_id=entry.intent.id,
        ticker=entry.intent.ticker,
        intent_kind=entry.intent.intent_kind,
        intent_value=entry.intent.intent_value,
        narrative=entry.intent.narrative,
        checkpoint_id=entry.checkpoint_id,
        checkpoint_payload_sha256=entry.checkpoint_payload_sha256,
        checkpoint_evidence_available=entry.checkpoint_evidence_available,
        target_band=entry.target_band,
    )
