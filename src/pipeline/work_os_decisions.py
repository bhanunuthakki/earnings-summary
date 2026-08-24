"""Governed owner/model decision projection shared by Work OS readers.

The database deliberately records owner and advisory decisions as independent
revisions.  This module preserves that separation so no consumer can imply
that a model recommendation is the owner's posture, or attach one row's
provenance to the other.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from compute.thesis_evaluator import KpiObservation, evaluate_rule, fetch_kpi_observations
from decision_conditions import DecisionCondition as StoredDecisionCondition
from decision_conditions import conditions_from_json, fetch_financial_history
from models.kpis import BreachStatus
from triggers.decision_condition import condition_to_rule

DecisionRelationship = Literal[
    "agree", "conflict", "owner_only", "model_only", "empty", "unavailable"
]
ObservationComparison = Literal["higher", "lower", "unchanged", "unavailable"]


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
    status: Literal["OK", "WATCH", "BREACH", "PENDING DATA"]
    latest_value: float | None = None
    observation_period: str | None = None
    observation_unit: str | None = None
    prior_value: float | None = None
    prior_observation_period: str | None = None
    prior_observation_unit: str | None = None
    observation_delta: float | None = None
    observation_comparison: ObservationComparison = "unavailable"
    evidence_ref: str
    status_detail: str | None = None
    origin: Literal["owner", "model"] = "model"


class _DecisionConditionFields(TypedDict):
    """Typed constructor slice shared by every condition outcome."""

    stable_id: str
    decision_id: int
    revision: str
    metric: str
    operator: str
    threshold: float
    unit: str
    for_periods: int
    note: str | None
    origin: Literal["owner", "model"]


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


def _is_future_not_before(not_before: str | None, as_of: datetime) -> bool:
    if not not_before:
        return False
    try:
        return date.fromisoformat(not_before[:10]) > as_of.date()
    except ValueError:
        return True


def _latest_evidence_ref(
    source: str | None, metric: str, observations: tuple[KpiObservation, ...]
) -> str:
    """A stable, fact-level provenance locator rather than a source category."""
    prefix = "kpi_facts" if source == "kpi" else "financial_facts"
    if observations:
        return f"{prefix}:{metric}:{observations[0].period_end.date().isoformat()}"
    return f"{prefix}:{metric}:unobserved"


def _observation_comparison(
    observations: tuple[KpiObservation, ...],
) -> tuple[float | None, str | None, str | None, float | None, ObservationComparison]:
    """Return a unit-safe latest-vs-prior comparison for owner-facing telemetry."""
    if len(observations) < 2:
        return None, None, None, None, "unavailable"
    latest, prior = observations[0], observations[1]
    if latest.unit != prior.unit:
        return (
            float(prior.value),
            prior.period_end.date().isoformat(),
            prior.unit.value,
            None,
            "unavailable",
        )
    delta = latest.value - prior.value
    comparison = "unchanged" if delta == 0 else ("higher" if delta > 0 else "lower")
    return (
        float(prior.value),
        prior.period_end.date().isoformat(),
        prior.unit.value,
        float(delta),
        comparison,
    )


def project_decision_condition(
    *,
    decision_id: int,
    revision: str,
    index: int,
    condition: StoredDecisionCondition,
    origin: Literal["owner", "model"],
    ticker: str,
    conn: sqlite3.Connection,
    as_of: datetime,
) -> DecisionCondition:
    """Project one condition through the trigger's canonical evaluator path."""
    stable_id = f"decision:{decision_id}:condition:{index}"
    base: _DecisionConditionFields = {
        "stable_id": stable_id,
        "decision_id": decision_id,
        "revision": revision,
        "metric": condition.metric,
        "operator": condition.op,
        "threshold": condition.threshold,
        "unit": condition.unit,
        "for_periods": condition.for_periods,
        "note": condition.note,
        "origin": origin,
    }
    if condition.metric_source not in ("kpi", "financial"):
        return DecisionCondition(
            **base,
            status="PENDING DATA",
            evidence_ref=f"condition:unresolved:{condition.metric}",
            status_detail="Metric is unresolved; it cannot yet be evaluated.",
        )

    rule = condition_to_rule(decision_id, index, condition)
    if rule is None:
        return DecisionCondition(
            **base,
            status="PENDING DATA",
            evidence_ref=f"condition:invalid:{condition.metric}",
            status_detail="Condition parameters are invalid; it cannot be evaluated.",
        )
    try:
        observations = (
            fetch_kpi_observations(conn, ticker, condition.metric, condition.for_periods)
            if condition.metric_source == "kpi"
            else fetch_financial_history(conn, ticker, condition.metric, condition.for_periods)
        )
    except sqlite3.Error:
        observations = None
    evaluation = evaluate_rule(rule, observations)
    latest = evaluation.observations[0] if evaluation.observations else None
    evidence_ref = _latest_evidence_ref(
        condition.metric_source, condition.metric, evaluation.observations
    )
    (
        prior_value,
        prior_observation_period,
        prior_observation_unit,
        observation_delta,
        observation_comparison,
    ) = _observation_comparison(evaluation.observations)
    status: Literal["OK", "WATCH", "BREACH", "PENDING DATA"]
    detail = evaluation.detail
    not_before = condition.not_before[:10] if condition.not_before else "an invalid date"
    if _is_future_not_before(condition.not_before, as_of):
        status = "PENDING DATA"
        detail = f"Not evaluable before {not_before}."
    elif evaluation.status is BreachStatus.UNRESOLVED or latest is None:
        status = "PENDING DATA"
    elif (
        condition.baseline_period_end
        and latest.period_end.date().isoformat() <= condition.baseline_period_end[:10]
    ):
        status = "PENDING DATA"
        detail = (
            f"Latest observation {latest.period_end.date().isoformat()} is not newer than the "
            f"decision baseline {condition.baseline_period_end[:10]}."
        )
    elif evaluation.status is BreachStatus.BREACH:
        status = "BREACH"
    elif evaluation.status is BreachStatus.WARN:
        status = "WATCH"
    else:
        status = "OK"
    return DecisionCondition(
        **base,
        status=status,
        latest_value=float(latest.value) if latest is not None else None,
        observation_period=latest.period_end.date().isoformat() if latest is not None else None,
        observation_unit=latest.unit.value if latest is not None else None,
        prior_value=prior_value,
        prior_observation_period=prior_observation_period,
        prior_observation_unit=prior_observation_unit,
        observation_delta=observation_delta,
        observation_comparison=observation_comparison,
        evidence_ref=evidence_ref,
        status_detail=detail,
    )


def _project_conditions(
    values: dict[str, object],
    *,
    origin: Literal["owner", "model"],
    ticker: str,
    conn: sqlite3.Connection,
    as_of: datetime,
) -> list[DecisionCondition]:
    raw_id = values.get("id")
    raw_revision = values.get("made_at")
    raw_conditions = values.get("decision_conditions")
    if not isinstance(raw_id, int) or not isinstance(raw_revision, str):
        return []
    decision_id = raw_id
    revision = raw_revision
    condition_json = raw_conditions if isinstance(raw_conditions, str) else None
    return [
        project_decision_condition(
            decision_id=decision_id,
            revision=revision,
            index=index,
            condition=condition,
            origin=origin,
            ticker=ticker,
            conn=conn,
            as_of=as_of,
        )
        for index, condition in enumerate(conditions_from_json(condition_json))
    ]


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
    reference = (as_of or datetime.now(UTC)).astimezone(UTC)
    owner_conditions: list[DecisionCondition] = []
    if owner_row is not None:
        values = dict(owner_row)
        owner_conditions = _project_conditions(
            values, origin="owner", ticker=ticker, conn=conn, as_of=reference
        )

    model_conditions: list[DecisionCondition] = []
    if model_row is not None:
        values = dict(model_row)
        model_conditions = _project_conditions(
            values, origin="model", ticker=ticker, conn=conn, as_of=reference
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
    "project_decision_condition",
]
