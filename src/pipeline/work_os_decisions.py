"""Governed owner/model decision projection shared by Work OS readers.

The database deliberately records owner and advisory decisions as independent
revisions.  This module preserves that separation so no consumer can imply
that a model recommendation is the owner's posture, or attach one row's
provenance to the other.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from decision_conditions import conditions_from_json

DecisionRelationship = Literal[
    "agree", "conflict", "owner_only", "model_only", "empty", "unavailable"
]


class DecisionState(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str
    decision_id: int
    revision: str
    made_at: str
    conviction: str | None
    source_lens: str | None
    decided_by: str
    target_weight_pct: float | None


class DecisionProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    owner: DecisionState | None = None
    model: DecisionState | None = None
    relationship: DecisionRelationship
    freshness: Literal["current", "stale", "empty", "unavailable"]
    stale_after_days: int = 90


class DecisionCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    stable_id: str
    decision_id: int
    revision: str
    metric: str
    operator: str
    threshold: float
    unit: str
    for_periods: int
    note: str | None
    evidence_ref: str | None
    origin: Literal["owner", "model"] = "model"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _state(row: sqlite3.Row | None) -> DecisionState | None:
    if row is None:
        return None
    values = dict(row)
    raw_target = values.get("recommendation_value")
    target = float(raw_target) if isinstance(raw_target, (int, float)) else None
    if target is not None and abs(target) <= 1:
        target *= 100
    made_at = str(values["made_at"])
    return DecisionState(
        value=str(values["recommendation_kind"]),
        decision_id=int(values["id"]),
        revision=made_at,
        made_at=made_at,
        conviction=(str(values["conviction"]) if values.get("conviction") is not None else None),
        source_lens=(str(values["source_lens"]) if values.get("source_lens") is not None else None),
        decided_by=str(values["decided_by"]),
        target_weight_pct=target,
    )


def _relationship(owner: DecisionState | None, model: DecisionState | None) -> DecisionRelationship:
    if owner is not None and model is not None:
        return "agree" if owner.value.casefold() == model.value.casefold() else "conflict"
    if owner is not None:
        return "owner_only"
    if model is not None:
        return "model_only"
    return "empty"


def build_decision_projection(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    as_of: datetime | None = None,
) -> tuple[DecisionProjection, list[DecisionCondition], list[str]]:
    """Return independent latest owner/model states plus owner-governed conditions."""

    columns = _table_columns(conn, "decisions")
    required = {
        "id",
        "ticker",
        "recommendation_kind",
        "recommendation_value",
        "conviction",
        "source_lens",
        "decided_by",
        "decision_conditions",
        "made_at",
    }
    if not required <= columns:
        return (
            DecisionProjection(relationship="unavailable", freshness="unavailable"),
            [],
            ["decision_store_unavailable"],
        )
    query = (
        "SELECT id, recommendation_kind, recommendation_value, conviction, "
        "source_lens, decided_by, decision_conditions, made_at FROM decisions "
        "WHERE UPPER(ticker) = ? AND {predicate} "
        "ORDER BY made_at DESC, id DESC LIMIT 1"
    )
    try:
        owner_row = conn.execute(
            query.format(predicate="decided_by = 'owner'"), (ticker,)
        ).fetchone()
        model_row = conn.execute(
            query.format(predicate="decided_by != 'owner'"), (ticker,)
        ).fetchone()
    except sqlite3.Error:
        return (
            DecisionProjection(relationship="unavailable", freshness="unavailable"),
            [],
            ["decision_store_unavailable"],
        )

    owner = _state(owner_row)
    model = _state(model_row)
    relationship = _relationship(owner, model)
    freshness: Literal["current", "stale", "empty", "unavailable"] = "empty"
    states = [state for state in (owner, model) if state is not None]
    if states:
        revisions: list[datetime] = []
        for state in states:
            try:
                parsed = datetime.fromisoformat(state.revision.replace("Z", "+00:00"))
                revisions.append(
                    parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
                )
            except ValueError:
                continue
        reference = (as_of or datetime.now(UTC)).astimezone(UTC)
        freshness = (
            "stale" if not revisions or (reference - max(revisions)).days > 90 else "current"
        )
    projection = DecisionProjection(
        owner=owner,
        model=model,
        relationship=relationship,
        freshness=freshness,
    )
    owner_conditions: list[DecisionCondition] = []
    if owner_row is not None:
        values = dict(owner_row)
        decision_id = int(values["id"])
        revision = str(values["made_at"])
        for index, condition in enumerate(
            conditions_from_json(values.get("decision_conditions")), start=0
        ):
            owner_conditions.append(
                DecisionCondition(
                    stable_id=f"decision:{decision_id}:condition:{index}",
                    decision_id=decision_id,
                    revision=revision,
                    metric=condition.metric,
                    operator=condition.op,
                    threshold=condition.threshold,
                    unit=condition.unit,
                    for_periods=condition.for_periods,
                    note=condition.note,
                    evidence_ref=condition.metric_source,
                    origin="owner",
                )
            )

    model_conditions: list[DecisionCondition] = []
    if model_row is not None:
        values = dict(model_row)
        decision_id = int(values["id"])
        revision = str(values["made_at"])
        for index, condition in enumerate(
            conditions_from_json(values.get("decision_conditions")), start=0
        ):
            model_conditions.append(
                DecisionCondition(
                    stable_id=f"decision:{decision_id}:condition:{index}",
                    decision_id=decision_id,
                    revision=revision,
                    metric=condition.metric,
                    operator=condition.op,
                    threshold=condition.threshold,
                    unit=condition.unit,
                    for_periods=condition.for_periods,
                    note=condition.note,
                    evidence_ref=condition.metric_source,
                    origin="model",
                )
            )

    # Use owner conditions when explicitly populated; otherwise fall back to model conditions
    conditions = owner_conditions if owner_conditions else model_conditions
    return projection, conditions, []


__all__ = [
    "DecisionCondition",
    "DecisionProjection",
    "DecisionRelationship",
    "DecisionState",
    "build_decision_projection",
]
