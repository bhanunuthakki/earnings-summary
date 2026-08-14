"""Central acknowledgement and anti-nag state for thesis episodes.

This module owns the lifecycle, actionability predicate, linked-carrier
settlement, and delivery receipt claims. It never writes the thesis ledger and
never commits; callers own the surrounding transaction and writer lock.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class AttentionError(ValueError):
    """Base error for invalid episode-attention transitions."""


class AttentionState(StrEnum):
    UNREVIEWED = "unreviewed"
    ACKNOWLEDGED = "acknowledged"
    ACTED_ON = "acted_on"
    SUPERSEDED = "superseded"


class DeliveryStatus(StrEnum):
    RESERVED = "reserved"
    DELIVERED = "delivered"
    FAILED = "failed"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EpisodeAttention(_FrozenModel):
    episode_id: str
    ticker: str
    state: AttentionState
    acknowledged_at: datetime | None
    acknowledgement_note: str | None
    next_review_at: datetime | None
    acted_on_decision_id: int | None
    superseded_by_episode_id: str | None
    review_cycle_id: str
    actionable: bool


class DeliveryClaim(_FrozenModel):
    receipt_id: str
    episode_id: str
    review_cycle_id: str
    channel: str
    surface: str
    status: DeliveryStatus
    claimed: bool


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AttentionError("attention timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    return _aware_utc(parsed)


def _cycle_id(state: AttentionState, next_review_at: datetime | None, now: datetime) -> str:
    if state is AttentionState.ACKNOWLEDGED and next_review_at is not None:
        normalized = _aware_utc(next_review_at)
        if now >= normalized:
            return f"review:{normalized.isoformat()}"
    return "initial"


def get_attention(
    connection: sqlite3.Connection,
    episode_id: str,
    *,
    now: datetime | None = None,
) -> EpisodeAttention:
    """Return the centralized actionability decision for one episode."""

    observed_at = _aware_utc(now or datetime.now(UTC))
    row = connection.execute(
        "SELECT episode_id,ticker,attention_state,acknowledged_at,"
        "acknowledgement_note,next_review_at,acted_on_decision_id,"
        "superseded_by_episode_id FROM thesis_evaluation_episodes WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    if row is None:
        raise AttentionError(f"unknown thesis episode: {episode_id}")
    state = AttentionState(str(row[2]))
    next_review_at = _parse_time(row[5])
    cycle = _cycle_id(state, next_review_at, observed_at)
    actionable = state is AttentionState.UNREVIEWED or (
        state is AttentionState.ACKNOWLEDGED
        and next_review_at is not None
        and observed_at >= next_review_at
    )
    return EpisodeAttention(
        episode_id=str(row[0]),
        ticker=str(row[1]),
        state=state,
        acknowledged_at=_parse_time(row[3]),
        acknowledgement_note=None if row[4] is None else str(row[4]),
        next_review_at=next_review_at,
        acted_on_decision_id=None if row[6] is None else int(row[6]),
        superseded_by_episode_id=None if row[7] is None else str(row[7]),
        review_cycle_id=cycle,
        actionable=actionable,
    )


def _settle_linked_carriers(
    connection: sqlite3.Connection,
    *,
    episode_id: str,
    settled_at: datetime,
    reason: str,
) -> None:
    stamp = _aware_utc(settled_at).isoformat()
    connection.execute(
        "UPDATE alerts SET status='dismissed',dismissed_at=?,dismiss_reason=? "
        "WHERE thesis_evaluation_episode_id=? AND status='pending'",
        (stamp, reason, episode_id),
    )
    connection.execute(
        "UPDATE coach_pings SET status='acted',updated_at=? "
        "WHERE thesis_evaluation_episode_id=? "
        "AND status IN ('sent','digest','routed_to_brief')",
        (stamp, episode_id),
    )


def acknowledge_episode(
    connection: sqlite3.Connection,
    episode_id: str,
    *,
    acknowledged_at: datetime,
    note: str | None = None,
    next_review_at: datetime | None = None,
) -> EpisodeAttention:
    """Acknowledge one episode and globally quiet its linked carriers."""

    stamp = _aware_utc(acknowledged_at)
    due = None if next_review_at is None else _aware_utc(next_review_at)
    if due is not None and due <= stamp:
        raise AttentionError("next_review_at must be after acknowledged_at")
    current = get_attention(connection, episode_id, now=stamp)
    if current.state in (AttentionState.ACTED_ON, AttentionState.SUPERSEDED):
        return current
    connection.execute(
        "UPDATE thesis_evaluation_episodes SET attention_state='acknowledged',"
        "acknowledged_at=?,acknowledgement_note=?,next_review_at=?,"
        "attention_updated_at=? WHERE episode_id=?",
        (
            stamp.isoformat(),
            note,
            None if due is None else due.isoformat(),
            stamp.isoformat(),
            episode_id,
        ),
    )
    _settle_linked_carriers(
        connection,
        episode_id=episode_id,
        settled_at=stamp,
        reason="thesis episode acknowledged",
    )
    return get_attention(connection, episode_id, now=stamp)


def act_on_episode(
    connection: sqlite3.Connection,
    episode_id: str,
    *,
    decision_id: int,
    acted_at: datetime,
) -> EpisodeAttention:
    """Link a reviewed episode to the durable owner decision that resolved it."""

    stamp = _aware_utc(acted_at)
    current = get_attention(connection, episode_id, now=stamp)
    if current.state is AttentionState.SUPERSEDED:
        raise AttentionError("a superseded episode cannot be acted on")
    if current.state is AttentionState.ACTED_ON:
        if current.acted_on_decision_id != decision_id:
            raise AttentionError("episode is already linked to a different decision")
        return current
    exists = connection.execute("SELECT 1 FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if exists is None:
        raise AttentionError(f"unknown decision: {decision_id}")
    connection.execute(
        "UPDATE thesis_evaluation_episodes SET attention_state='acted_on',"
        "acted_on_decision_id=?,attention_updated_at=? WHERE episode_id=?",
        (decision_id, stamp.isoformat(), episode_id),
    )
    _settle_linked_carriers(
        connection,
        episode_id=episode_id,
        settled_at=stamp,
        reason="thesis episode acted on",
    )
    return get_attention(connection, episode_id, now=stamp)


def supersede_prior(
    connection: sqlite3.Connection,
    *,
    new_episode_id: str,
    superseded_at: datetime,
) -> int:
    """Supersede unresolved prior episodes for the same ticker."""

    stamp = _aware_utc(superseded_at)
    row = connection.execute(
        "SELECT ticker FROM thesis_evaluation_episodes WHERE episode_id=?",
        (new_episode_id,),
    ).fetchone()
    if row is None:
        raise AttentionError(f"unknown thesis episode: {new_episode_id}")
    prior_ids = [
        str(prior[0])
        for prior in connection.execute(
            "SELECT episode_id FROM thesis_evaluation_episodes "
            "WHERE ticker=? AND episode_id<>? "
            "AND attention_state IN ('unreviewed','acknowledged')",
            (str(row[0]), new_episode_id),
        ).fetchall()
    ]
    for prior_id in prior_ids:
        connection.execute(
            "UPDATE thesis_evaluation_episodes SET attention_state='superseded',"
            "superseded_by_episode_id=?,attention_updated_at=? WHERE episode_id=?",
            (new_episode_id, stamp.isoformat(), prior_id),
        )
        _settle_linked_carriers(
            connection,
            episode_id=prior_id,
            settled_at=stamp,
            reason="thesis episode superseded",
        )
    return len(prior_ids)


def should_prompt(
    connection: sqlite3.Connection,
    episode_id: str,
    *,
    channel: str,
    surface: str,
    now: datetime | None = None,
) -> bool:
    attention = get_attention(connection, episode_id, now=now)
    if not attention.actionable:
        return False
    row = connection.execute(
        "SELECT status FROM thesis_evaluation_episode_delivery_receipts "
        "WHERE episode_id=? AND review_cycle_id=? AND channel=? AND surface=?",
        (episode_id, attention.review_cycle_id, channel, surface),
    ).fetchone()
    return row is None or str(row[0]) == DeliveryStatus.FAILED.value


def _delivery_receipt_id(
    *, episode_id: str, review_cycle_id: str, channel: str, surface: str
) -> str:
    payload = "\n".join((episode_id, review_cycle_id, channel, surface))
    return "thesis-episode-delivery:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reserve_delivery(
    connection: sqlite3.Connection,
    episode_id: str,
    *,
    channel: str,
    surface: str,
    reserved_at: datetime,
) -> DeliveryClaim | None:
    """Claim at-most-once delivery for the episode's current review cycle."""

    stamp = _aware_utc(reserved_at)
    attention = get_attention(connection, episode_id, now=stamp)
    if not attention.actionable:
        return None
    receipt_id = _delivery_receipt_id(
        episode_id=episode_id,
        review_cycle_id=attention.review_cycle_id,
        channel=channel,
        surface=surface,
    )
    row = connection.execute(
        "SELECT status FROM thesis_evaluation_episode_delivery_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    existing_status = None if row is None else DeliveryStatus(str(row[0]))
    claimed = existing_status is None or existing_status is DeliveryStatus.FAILED
    if row is None:
        connection.execute(
            "INSERT INTO thesis_evaluation_episode_delivery_receipts "
            "(receipt_id,episode_id,review_cycle_id,channel,surface,status,reserved_at) "
            "VALUES (?,?,?,?,?,'reserved',?)",
            (
                receipt_id,
                episode_id,
                attention.review_cycle_id,
                channel,
                surface,
                stamp.isoformat(),
            ),
        )
    elif claimed:
        connection.execute(
            "UPDATE thesis_evaluation_episode_delivery_receipts SET "
            "status='reserved',reserved_at=?,delivered_at=NULL,failed_at=NULL,"
            "external_ref=NULL,failure_reason=NULL WHERE receipt_id=?",
            (stamp.isoformat(), receipt_id),
        )
    claim_status = DeliveryStatus.RESERVED if claimed else existing_status
    if claim_status is None:  # Defensive: ``claimed`` is true for a new receipt.
        raise AttentionError("delivery claim has no status")
    return DeliveryClaim(
        receipt_id=receipt_id,
        episode_id=episode_id,
        review_cycle_id=attention.review_cycle_id,
        channel=channel,
        surface=surface,
        status=claim_status,
        claimed=claimed,
    )


def complete_delivery(
    connection: sqlite3.Connection,
    receipt_id: str,
    *,
    status: DeliveryStatus,
    completed_at: datetime,
    external_ref: str | None = None,
    failure_reason: str | None = None,
) -> None:
    """Finalize a reserved claim as delivered or failed, idempotently."""

    if status is DeliveryStatus.RESERVED:
        raise AttentionError("delivery completion must be delivered or failed")
    stamp = _aware_utc(completed_at).isoformat()
    row = connection.execute(
        "SELECT status,external_ref,failure_reason FROM "
        "thesis_evaluation_episode_delivery_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    if row is None:
        raise AttentionError(f"unknown delivery receipt: {receipt_id}")
    current = DeliveryStatus(str(row[0]))
    if current is status:
        if row[1] != external_ref or row[2] != failure_reason:
            raise AttentionError("delivery completion conflicts with existing receipt")
        return
    if current is not DeliveryStatus.RESERVED:
        raise AttentionError("only a reserved delivery can be completed")
    connection.execute(
        "UPDATE thesis_evaluation_episode_delivery_receipts SET status=?,"
        "delivered_at=?,failed_at=?,external_ref=?,failure_reason=? WHERE receipt_id=?",
        (
            status.value,
            stamp if status is DeliveryStatus.DELIVERED else None,
            stamp if status is DeliveryStatus.FAILED else None,
            external_ref,
            failure_reason,
            receipt_id,
        ),
    )


__all__ = [
    "AttentionError",
    "AttentionState",
    "DeliveryClaim",
    "DeliveryStatus",
    "EpisodeAttention",
    "acknowledge_episode",
    "act_on_episode",
    "complete_delivery",
    "get_attention",
    "reserve_delivery",
    "should_prompt",
    "supersede_prior",
]
