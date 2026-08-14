"""Atomic, idempotent owner-decision checkpoint persistence.

The checkpoint is the immutable point-in-time owner context.  A confirmation
either creates/links every decision, sizing intent and optional thesis-ledger
entry in one transaction, or creates none of them.  Retries with the same
source event and canonical payload return the existing receipt; a changed
payload for that event fails loudly and requires an explicit amendment event.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from identity import DEFAULT_USER_ID
from user_state._db import now_iso, open_conn

CHECKPOINT_SCHEMA_VERSION = "owner-decision-checkpoint/v1"

DecisionAction = Literal["initiate", "add", "trim", "sell", "hold"]
HoldingAvailability = Literal["observed", "missing_from_snapshot", "source_unavailable"]
ThesisDisposition = Literal["intact", "watch", "broken", "not_the_reason"]
TargetVerification = Literal["verified", "target_unverified", "not_applicable"]


class CheckpointConflictError(RuntimeError):
    """The same source event was retried with a different canonical payload."""


class CheckpointInvariantError(RuntimeError):
    """Persisted rows do not match the checkpoint being confirmed."""


class TargetBand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_pct: float = Field(ge=0, le=100)
    maximum_pct: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> TargetBand:
        if self.minimum_pct > self.maximum_pct:
            raise ValueError("target band minimum must not exceed maximum")
        return self


class HoldingBasisPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    availability: HoldingAvailability
    weight_pct: float | None = Field(default=None, ge=0, le=100)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        clean = value.strip().upper()
        if not clean:
            raise ValueError("ticker is required")
        return clean

    @model_validator(mode="after")
    def _availability_matches_weight(self) -> HoldingBasisPosition:
        if self.availability == "observed" and self.weight_pct is None:
            raise ValueError("observed holdings require weight_pct")
        if self.availability != "observed" and self.weight_pct is not None:
            raise ValueError("unavailable holdings must not invent weight_pct")
        return self


class HoldingsBasis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    as_of: str
    source_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    embedded_positions: tuple[HoldingBasisPosition, ...]
    basis_note: str | None = None

    @model_validator(mode="after")
    def _unique_tickers(self) -> HoldingsBasis:
        tickers = [position.ticker for position in self.embedded_positions]
        if not self.source.strip() or not self.as_of.strip():
            raise ValueError("holdings basis source and as_of are required")
        if len(tickers) != len(set(tickers)):
            raise ValueError("holdings basis tickers must be unique")
        if not tickers:
            raise ValueError("holdings basis requires at least one relevant position")
        return self

    def position(self, ticker: str) -> HoldingBasisPosition:
        clean = ticker.strip().upper()
        for position in self.embedded_positions:
            if position.ticker == clean:
                return position
        raise CheckpointInvariantError(f"holdings basis has no position state for {clean}")


class DecisionLeg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    leg_id: str
    ticker: str
    action: DecisionAction
    existing_decision_id: int | None = Field(default=None, ge=1)
    proposed_delta_pct: float | None = Field(default=None, ge=0, le=100)
    target_band: TargetBand | None = None
    price_level: float | None = Field(default=None, gt=0)
    account: str | None = None
    instrument: str | None = None
    horizon: str
    thesis_state: ThesisDisposition
    thesis_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    thesis_excerpt: str | None = None
    thesis_changed: bool = False
    changed_since_prior: str
    why_now: str
    conviction: str
    falsifier: str
    portfolio_role: str
    qualitative_stress_implication: str
    alternative_use_of_capital: str
    prior_owner_decision_id: int | None = Field(default=None, ge=1)
    adopted_advice_id: int | None = Field(default=None, ge=1)
    alternative_leg_id: str | None = None
    target_verification: TargetVerification = "not_applicable"
    target_delta_mismatch: str | None = None
    made_at: str | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        clean = value.strip().upper()
        if not clean:
            raise ValueError("ticker is required")
        return clean

    @field_validator(
        "leg_id",
        "horizon",
        "changed_since_prior",
        "why_now",
        "conviction",
        "falsifier",
        "portfolio_role",
        "qualitative_stress_implication",
        "alternative_use_of_capital",
    )
    @classmethod
    def _nonempty(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("checkpoint narrative fields must be non-empty")
        return clean

    @model_validator(mode="after")
    def _thesis_fields(self) -> DecisionLeg:
        if self.thesis_changed and self.thesis_state == "not_the_reason":
            raise ValueError("not_the_reason cannot be marked as a thesis change")
        if self.thesis_state != "not_the_reason" and (
            self.thesis_content_sha256 is None or not (self.thesis_excerpt or "").strip()
        ):
            raise ValueError("a thesis disposition requires its content hash and excerpt")
        return self


class SizingIntentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    leg_id: str
    ticker: str
    intent_kind: str
    intent_value: float | None = None
    narrative: str
    existing_sizing_intent_id: int | None = Field(default=None, ge=1)
    target_band: TargetBand | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        clean = value.strip().upper()
        if not clean:
            raise ValueError("ticker is required")
        return clean


class LedgerEntrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    entry_kind: str = "thesis_update"
    body: str
    source_alert_id: int | None = Field(default=None, ge=1)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        clean = value.strip().upper()
        if not clean:
            raise ValueError("ticker is required")
        return clean


class OwnerDecisionCheckpointPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["owner-decision-checkpoint/v1"] = CHECKPOINT_SCHEMA_VERSION
    source_channel: str
    source_event_id: str
    retrospective: bool = False
    holdings_basis: HoldingsBasis
    legs: tuple[DecisionLeg, ...]
    sizing_intents: tuple[SizingIntentSpec, ...] = ()
    ledger_entries: tuple[LedgerEntrySpec, ...] = ()

    @model_validator(mode="after")
    def _coherent(self) -> OwnerDecisionCheckpointPayload:
        if not self.source_channel.strip() or not self.source_event_id.strip():
            raise ValueError("source_channel and source_event_id are required")
        if not self.legs:
            raise ValueError("checkpoint requires at least one decision leg")
        leg_ids = [leg.leg_id for leg in self.legs]
        if len(leg_ids) != len(set(leg_ids)):
            raise ValueError("decision leg IDs must be unique")
        known = set(leg_ids)
        for leg in self.legs:
            self.holdings_basis.position(leg.ticker)
            if leg.alternative_leg_id is not None and leg.alternative_leg_id not in known:
                raise ValueError(f"unknown alternative_leg_id {leg.alternative_leg_id!r}")
            if leg.alternative_leg_id is not None:
                alternative = next(
                    item for item in self.legs if item.leg_id == leg.alternative_leg_id
                )
                if alternative.alternative_leg_id != leg.leg_id:
                    raise ValueError("alternative decision links must be reciprocal")
        for intent in self.sizing_intents:
            if intent.leg_id not in known:
                raise ValueError(f"sizing intent references unknown leg {intent.leg_id!r}")
        changed_tickers = {leg.ticker for leg in self.legs if leg.thesis_changed}
        ledger_tickers = {entry.ticker for entry in self.ledger_entries}
        if ledger_tickers != changed_tickers:
            raise ValueError(
                "ledger entries must exist exactly for decision legs marked thesis_changed"
            )
        return self


class CheckpointConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: int
    payload_sha256: str
    decision_ids: tuple[int, ...]
    sizing_intent_ids: tuple[int, ...]
    ledger_entry_ids: tuple[int, ...]
    created: bool


def canonical_payload_json(payload: OwnerDecisionCheckpointPayload) -> str:
    return json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def payload_sha256(payload: OwnerDecisionCheckpointPayload) -> str:
    return hashlib.sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


def thesis_content_sha256(thesis: str) -> str:
    return hashlib.sha256(thesis.strip().encode("utf-8")).hexdigest()


def _existing_confirmation(
    conn: sqlite3.Connection, checkpoint_id: int, digest: str
) -> CheckpointConfirmation:
    decisions = tuple(
        int(row[0])
        for row in conn.execute(
            "SELECT decision_id FROM owner_decision_checkpoint_decisions "
            "WHERE checkpoint_id=? ORDER BY leg_ordinal",
            (checkpoint_id,),
        )
    )
    intents = tuple(
        int(row[0])
        for row in conn.execute(
            "SELECT sizing_intent_id FROM owner_decision_checkpoint_sizing_intents "
            "WHERE checkpoint_id=? ORDER BY leg_ordinal",
            (checkpoint_id,),
        )
    )
    ledger = tuple(
        int(row[0])
        for row in conn.execute(
            "SELECT ledger_entry_id FROM owner_decision_checkpoint_ledger_entries "
            "WHERE checkpoint_id=? ORDER BY ledger_entry_id",
            (checkpoint_id,),
        )
    )
    return CheckpointConfirmation(
        checkpoint_id=checkpoint_id,
        payload_sha256=digest,
        decision_ids=decisions,
        sizing_intent_ids=intents,
        ledger_entry_ids=ledger,
        created=False,
    )


def _basis_meta(
    *, checkpoint_id: int, digest: str, payload: OwnerDecisionCheckpointPayload, leg: DecisionLeg
) -> dict[str, object]:
    holding = payload.holdings_basis.position(leg.ticker)
    return {
        "owner_decision_checkpoint": {
            "checkpoint_id": checkpoint_id,
            "schema_version": payload.schema_version,
            "source_channel": payload.source_channel,
            "source_event_id": payload.source_event_id,
            "payload_sha256": digest,
            "leg_id": leg.leg_id,
            "retrospective": payload.retrospective,
            "holdings_source": payload.holdings_basis.source,
            "holdings_as_of": payload.holdings_basis.as_of,
            "holding_availability": holding.availability,
            "observed_weight_pct": holding.weight_pct,
            "target_verification": leg.target_verification,
            "target_delta_mismatch": leg.target_delta_mismatch,
            "target_band": (
                None if leg.target_band is None else leg.target_band.model_dump(mode="json")
            ),
        }
    }


def _merge_basis_meta(raw: object, checkpoint_meta: dict[str, object]) -> str:
    if raw is None:
        merged: dict[str, object] = {}
    else:
        decoded_raw = json.loads(str(raw))
        if not isinstance(decoded_raw, dict):
            raise CheckpointInvariantError("existing decisions.basis_meta_json is not an object")
        decoded = cast("dict[object, object]", decoded_raw)
        merged = {str(key): value for key, value in decoded.items()}
    if "owner_decision_checkpoint" in merged:
        raise CheckpointInvariantError("decision already carries an owner checkpoint")
    merged.update(checkpoint_meta)
    return json.dumps(merged, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _link_or_create_decision(
    conn: sqlite3.Connection,
    *,
    checkpoint_id: int,
    digest: str,
    payload: OwnerDecisionCheckpointPayload,
    leg: DecisionLeg,
    leg_ordinal: int,
    stamp: str,
) -> int:
    holding = payload.holdings_basis.position(leg.ticker)
    checkpoint_meta = _basis_meta(
        checkpoint_id=checkpoint_id, digest=digest, payload=payload, leg=leg
    )
    if leg.existing_decision_id is not None:
        row = conn.execute(
            "SELECT * FROM decisions WHERE id=?", (leg.existing_decision_id,)
        ).fetchone()
        if row is None:
            raise CheckpointInvariantError(
                f"existing decision {leg.existing_decision_id} does not exist"
            )
        if (
            str(row["decided_by"]) != "owner"
            or str(row["ticker"]).upper() != leg.ticker
            or str(row["recommendation_kind"]) != leg.action
        ):
            raise CheckpointInvariantError(
                f"existing decision {leg.existing_decision_id} does not match leg {leg.leg_id}"
            )
        if leg.proposed_delta_pct is not None and (
            row["size_pct"] is None or abs(float(row["size_pct"]) - leg.proposed_delta_pct) > 1e-9
        ):
            raise CheckpointInvariantError(
                f"existing decision {leg.existing_decision_id} size does not match checkpoint"
            )
        meta_json = _merge_basis_meta(row["basis_meta_json"], checkpoint_meta)
        conn.execute(
            "UPDATE decisions SET basis_kind='owner_checkpoint', basis_ref_id=?, "
            "basis_value=?, basis_as_of=?, basis_meta_json=? WHERE id=?",
            (
                checkpoint_id,
                holding.weight_pct,
                payload.holdings_basis.as_of,
                meta_json,
                leg.existing_decision_id,
            ),
        )
        decision_id = leg.existing_decision_id
    else:
        meta_json = _merge_basis_meta(None, checkpoint_meta)
        cur = conn.execute(
            """
            INSERT INTO decisions(
                ticker,recommendation_kind,conviction,decided_by,scope,instrument,
                account,size_pct,falsifier,rationale_excerpt,made_at,created_at,
                basis_kind,basis_ref_id,basis_value,basis_as_of,basis_meta_json,
                advice_artifact_id
            ) VALUES (?,?,?,'owner','ticker',?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                leg.ticker,
                leg.action,
                None if leg.conviction == "not_provided" else leg.conviction,
                leg.instrument,
                leg.account,
                leg.proposed_delta_pct,
                None if leg.falsifier == "not_provided" else leg.falsifier,
                leg.why_now[:512],
                leg.made_at or stamp,
                stamp,
                "owner_checkpoint",
                checkpoint_id,
                holding.weight_pct,
                payload.holdings_basis.as_of,
                meta_json,
                leg.adopted_advice_id,
            ),
        )
        decision_id = int(cur.lastrowid or 0)
    conn.execute(
        "INSERT INTO owner_decision_checkpoint_decisions"
        "(checkpoint_id,leg_id,leg_ordinal,decision_id,recorded_at) VALUES (?,?,?,?,?)",
        (checkpoint_id, leg.leg_id, leg_ordinal, decision_id, stamp),
    )
    return decision_id


def _link_or_create_intent(
    conn: sqlite3.Connection,
    *,
    checkpoint_id: int,
    intent: SizingIntentSpec,
    leg_ordinal: int,
    stamp: str,
    user_id: str,
) -> int:
    if intent.existing_sizing_intent_id is not None:
        row = conn.execute(
            "SELECT * FROM position_sizing_intent WHERE id=?",
            (intent.existing_sizing_intent_id,),
        ).fetchone()
        if row is None:
            raise CheckpointInvariantError(
                f"existing sizing intent {intent.existing_sizing_intent_id} does not exist"
            )
        if (
            str(row["user_id"]) != user_id
            or str(row["ticker"]).upper() != intent.ticker
            or str(row["intent_kind"]) != intent.intent_kind
            or (
                intent.intent_value is not None
                and (
                    row["intent_value"] is None
                    or abs(float(row["intent_value"]) - intent.intent_value) > 1e-9
                )
            )
        ):
            raise CheckpointInvariantError(
                f"existing sizing intent {intent.existing_sizing_intent_id} does not match"
            )
        intent_id = intent.existing_sizing_intent_id
    else:
        cur = conn.execute(
            "INSERT INTO position_sizing_intent"
            "(user_id,ticker,intent_kind,intent_value,narrative,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                user_id,
                intent.ticker,
                intent.intent_kind,
                intent.intent_value,
                intent.narrative,
                stamp,
                stamp,
            ),
        )
        intent_id = int(cur.lastrowid or 0)
    conn.execute(
        "INSERT INTO owner_decision_checkpoint_sizing_intents"
        "(checkpoint_id,leg_id,leg_ordinal,sizing_intent_id,recorded_at) VALUES (?,?,?,?,?)",
        (checkpoint_id, intent.leg_id, leg_ordinal, intent_id, stamp),
    )
    return intent_id


def confirm_owner_decision_checkpoint(
    payload: OwnerDecisionCheckpointPayload,
    *,
    db_path: Path | str | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> CheckpointConfirmation:
    """Confirm ``payload`` atomically, with collision-safe idempotency."""

    canonical = canonical_payload_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    conn = open_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id,payload_sha256 FROM owner_decision_checkpoints "
            "WHERE user_id=? AND source_channel=? AND source_event_id=? "
            "AND checkpoint_schema_version=?",
            (
                user_id,
                payload.source_channel,
                payload.source_event_id,
                payload.schema_version,
            ),
        ).fetchone()
        if existing is not None:
            checkpoint_id = int(existing["id"])
            stored_digest = str(existing["payload_sha256"])
            if stored_digest != digest:
                raise CheckpointConflictError(
                    "source event already exists with a different checkpoint payload; "
                    "record an explicit amendment event"
                )
            receipt = _existing_confirmation(conn, checkpoint_id, digest)
            conn.commit()
            return receipt

        stamp = now_iso()
        cur = conn.execute(
            """
            INSERT INTO owner_decision_checkpoints(
                user_id,source_channel,source_event_id,checkpoint_schema_version,
                payload_sha256,payload_json,retrospective,created_at,confirmed_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                user_id,
                payload.source_channel,
                payload.source_event_id,
                payload.schema_version,
                digest,
                canonical,
                int(payload.retrospective),
                stamp,
                stamp,
            ),
        )
        checkpoint_id = int(cur.lastrowid or 0)
        decision_ids = tuple(
            _link_or_create_decision(
                conn,
                checkpoint_id=checkpoint_id,
                digest=digest,
                payload=payload,
                leg=leg,
                leg_ordinal=leg_ordinal,
                stamp=stamp,
            )
            for leg_ordinal, leg in enumerate(payload.legs)
        )
        intent_ids = tuple(
            _link_or_create_intent(
                conn,
                checkpoint_id=checkpoint_id,
                intent=intent,
                leg_ordinal=leg_ordinal,
                stamp=stamp,
                user_id=user_id,
            )
            for leg_ordinal, intent in enumerate(payload.sizing_intents)
        )
        ledger_ids: list[int] = []
        for entry in payload.ledger_entries:
            ledger_cur = conn.execute(
                "INSERT INTO thesis_ledger_entries"
                "(user_id,ticker,entry_kind,body,source_alert_id,created_at,accepted_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    user_id,
                    entry.ticker,
                    entry.entry_kind,
                    entry.body,
                    entry.source_alert_id,
                    stamp,
                    stamp,
                ),
            )
            ledger_id = int(ledger_cur.lastrowid or 0)
            conn.execute(
                "INSERT INTO owner_decision_checkpoint_ledger_entries"
                "(checkpoint_id,ledger_entry_id,recorded_at) VALUES (?,?,?)",
                (checkpoint_id, ledger_id, stamp),
            )
            ledger_ids.append(ledger_id)
        conn.commit()
        return CheckpointConfirmation(
            checkpoint_id=checkpoint_id,
            payload_sha256=digest,
            decision_ids=decision_ids,
            sizing_intent_ids=intent_ids,
            ledger_entry_ids=tuple(ledger_ids),
            created=True,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
