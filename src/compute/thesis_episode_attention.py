"""Central acknowledgement and anti-nag state for thesis episodes.

This module owns the lifecycle, actionability predicate, linked-carrier
settlement, and delivery receipt claims. It never writes the thesis ledger and
never commits; callers own the surrounding transaction and writer lock.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

_DELIVERY_LEASE = timedelta(minutes=15)
_MAX_DELIVERY_LEASE = timedelta(days=1)
_MAX_ACKNOWLEDGEMENT_NOTE = 1000
_DELIVERY_LABEL = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}\Z")


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
    attention_updated_at: datetime | None
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
    attempt_token: str
    attempt_count: int
    reservation_expires_at: datetime


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


def _delivery_label(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if _DELIVERY_LABEL.fullmatch(normalized) is None:
        raise AttentionError(f"{name} must be a lowercase delivery label")
    return normalized


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
        "superseded_by_episode_id,attention_updated_at "
        "FROM thesis_evaluation_episodes WHERE episode_id=?",
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
        attention_updated_at=_parse_time(row[8]),
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
        "AND status IN ('sent','digest','routed_to_brief','send_failed')",
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
    normalized_note = None if note is None else note.strip() or None
    if normalized_note is not None and len(normalized_note) > _MAX_ACKNOWLEDGEMENT_NOTE:
        raise AttentionError(f"acknowledgement note exceeds {_MAX_ACKNOWLEDGEMENT_NOTE} characters")
    if due is not None and due <= stamp:
        raise AttentionError("next_review_at must be after acknowledged_at")
    current = get_attention(connection, episode_id, now=stamp)
    if current.state in (AttentionState.ACTED_ON, AttentionState.SUPERSEDED):
        return current
    updated = connection.execute(
        "UPDATE thesis_evaluation_episodes SET attention_state='acknowledged',"
        "acknowledged_at=?,acknowledgement_note=?,next_review_at=?,"
        "attention_updated_at=? WHERE episode_id=? "
        "AND attention_state=? AND attention_updated_at IS ?",
        (
            stamp.isoformat(),
            normalized_note,
            None if due is None else due.isoformat(),
            stamp.isoformat(),
            episode_id,
            current.state.value,
            (
                None
                if current.attention_updated_at is None
                else current.attention_updated_at.isoformat()
            ),
        ),
    )
    if updated.rowcount != 1:
        latest = get_attention(connection, episode_id, now=stamp)
        if latest.state in (AttentionState.ACTED_ON, AttentionState.SUPERSEDED):
            return latest
        if (
            latest.state is AttentionState.ACKNOWLEDGED
            and latest.acknowledgement_note == normalized_note
            and latest.next_review_at == due
        ):
            return latest
        raise AttentionError("episode attention changed during acknowledgement")
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
    updated = connection.execute(
        "UPDATE thesis_evaluation_episodes SET attention_state='acted_on',"
        "acted_on_decision_id=?,attention_updated_at=? WHERE episode_id=? "
        "AND attention_state=? AND attention_updated_at IS ?",
        (
            decision_id,
            stamp.isoformat(),
            episode_id,
            current.state.value,
            (
                None
                if current.attention_updated_at is None
                else current.attention_updated_at.isoformat()
            ),
        ),
    )
    if updated.rowcount != 1:
        latest = get_attention(connection, episode_id, now=stamp)
        if latest.state is AttentionState.ACTED_ON and latest.acted_on_decision_id == decision_id:
            return latest
        raise AttentionError("episode attention changed while linking the decision")
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
        "SELECT ticker,first_evaluated_at FROM thesis_evaluation_episodes WHERE episode_id=?",
        (new_episode_id,),
    ).fetchone()
    if row is None:
        raise AttentionError(f"unknown thesis episode: {new_episode_id}")
    priors = [
        (str(prior[0]), AttentionState(str(prior[1])), _parse_time(prior[2]))
        for prior in connection.execute(
            "SELECT episode_id,attention_state,attention_updated_at "
            "FROM thesis_evaluation_episodes "
            "WHERE ticker=? AND episode_id<>? "
            "AND first_evaluated_at<=? "
            "AND attention_state IN ('unreviewed','acknowledged')",
            (str(row[0]), new_episode_id, str(row[1])),
        ).fetchall()
    ]
    changed = 0
    for prior_id, prior_state, prior_updated_at in priors:
        updated = connection.execute(
            "UPDATE thesis_evaluation_episodes SET attention_state='superseded',"
            "superseded_by_episode_id=?,attention_updated_at=? WHERE episode_id=? "
            "AND attention_state=? AND attention_updated_at IS ?",
            (
                new_episode_id,
                stamp.isoformat(),
                prior_id,
                prior_state.value,
                None if prior_updated_at is None else prior_updated_at.isoformat(),
            ),
        )
        if updated.rowcount != 1:
            latest = get_attention(connection, prior_id, now=stamp)
            if latest.state not in (AttentionState.ACTED_ON, AttentionState.SUPERSEDED):
                raise AttentionError("prior episode attention changed during supersession")
            continue
        changed += 1
        _settle_linked_carriers(
            connection,
            episode_id=prior_id,
            settled_at=stamp,
            reason="thesis episode superseded",
        )
    return changed


def ensure_episode_alert(
    connection: sqlite3.Connection,
    episode_id: str,
    *,
    fired_at: datetime,
    user_id: str = "bhanu",
) -> int | None:
    """Ensure one actionable Inbox carrier for an episode/review cycle."""

    from alerts.store import compute_signature_sha

    stamp = _aware_utc(fired_at)
    attention = get_attention(connection, episode_id, now=stamp)
    if not attention.actionable:
        return None
    episode = connection.execute(
        "SELECT overall_status,provenance_completeness,evidence_as_of "
        "FROM thesis_evaluation_episodes WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise AttentionError(f"unknown thesis episode: {episode_id}")
    if str(episode[0]) == "ok":
        return None
    signature = compute_signature_sha(
        "thesis_drift",
        attention.ticker,
        {
            "episode_id": episode_id,
            "review_cycle_id": attention.review_cycle_id,
        },
    )
    existing = connection.execute(
        "SELECT id FROM alerts WHERE user_id=? AND signature_sha=? AND status<>'expired'",
        (user_id, signature),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    evidence_json = json.dumps(
        {
            "episode_id": episode_id,
            "review_cycle_id": attention.review_cycle_id,
            "overall_status": str(episode[0]),
            "provenance_completeness": str(episode[1]),
            "evidence_as_of": None if episode[2] is None else str(episode[2]),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    cursor = connection.execute(
        "INSERT INTO alerts "
        "(user_id,ticker,trigger_kind,fired_at,status,evidence_json,signature_sha,"
        "thesis_evaluation_episode_id,review_cycle_id) "
        "VALUES (?,?,'thesis_drift',?,'pending',?,?,?,?)",
        (
            user_id,
            attention.ticker,
            stamp.isoformat(),
            evidence_json,
            signature,
            episode_id,
            attention.review_cycle_id,
        ),
    )
    return int(cursor.lastrowid or 0)


def should_prompt(
    connection: sqlite3.Connection,
    episode_id: str,
    *,
    channel: str,
    surface: str,
    now: datetime | None = None,
) -> bool:
    observed_at = _aware_utc(now or datetime.now(UTC))
    attention = get_attention(connection, episode_id, now=observed_at)
    if not attention.actionable:
        return False
    row = connection.execute(
        "SELECT status,reservation_expires_at "
        "FROM thesis_evaluation_episode_delivery_receipts "
        "WHERE episode_id=? AND review_cycle_id=? AND channel=? AND surface=?",
        (
            episode_id,
            attention.review_cycle_id,
            _delivery_label(channel, "channel"),
            _delivery_label(surface, "surface"),
        ),
    ).fetchone()
    if row is None or str(row[0]) == DeliveryStatus.FAILED.value:
        return True
    return (
        str(row[0]) == DeliveryStatus.RESERVED.value
        and (_parse_time(row[1]) or observed_at) <= observed_at
    )


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
    lease_for: timedelta = _DELIVERY_LEASE,
) -> DeliveryClaim | None:
    """Claim at-most-once delivery for the episode's current review cycle."""

    stamp = _aware_utc(reserved_at)
    if lease_for <= timedelta(0) or lease_for > _MAX_DELIVERY_LEASE:
        raise AttentionError("delivery lease must be positive and no longer than one day")
    normalized_channel = _delivery_label(channel, "channel")
    normalized_surface = _delivery_label(surface, "surface")
    attention = get_attention(connection, episode_id, now=stamp)
    if not attention.actionable:
        return None
    receipt_id = _delivery_receipt_id(
        episode_id=episode_id,
        review_cycle_id=attention.review_cycle_id,
        channel=normalized_channel,
        surface=normalized_surface,
    )
    row = connection.execute(
        "SELECT status,reservation_expires_at,attempt_token,attempt_count "
        "FROM thesis_evaluation_episode_delivery_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    existing_status = None if row is None else DeliveryStatus(str(row[0]))
    existing_expiry = None if row is None else _parse_time(row[1])
    existing_attempt_token = None if row is None else str(row[2])
    expired = (
        existing_status is DeliveryStatus.RESERVED
        and existing_expiry is not None
        and existing_expiry <= stamp
    )
    claimed = existing_status is None or existing_status is DeliveryStatus.FAILED or expired
    attempt_count = 1 if row is None else int(row[3]) + int(claimed)
    expires_at = stamp + lease_for
    attempt_token = hashlib.sha256(
        f"{receipt_id}\n{attempt_count}\n{stamp.isoformat()}".encode()
    ).hexdigest()
    if row is None:
        try:
            connection.execute(
                "INSERT INTO thesis_evaluation_episode_delivery_receipts "
                "(receipt_id,episode_id,review_cycle_id,channel,surface,status,reserved_at,"
                "reservation_expires_at,attempt_token,attempt_count) "
                "VALUES (?,?,?,?,?,'reserved',?,?,?,1)",
                (
                    receipt_id,
                    episode_id,
                    attention.review_cycle_id,
                    normalized_channel,
                    normalized_surface,
                    stamp.isoformat(),
                    expires_at.isoformat(),
                    attempt_token,
                ),
            )
        except sqlite3.IntegrityError:
            return reserve_delivery(
                connection,
                episode_id,
                channel=normalized_channel,
                surface=normalized_surface,
                reserved_at=stamp,
                lease_for=lease_for,
            )
    elif claimed:
        updated = connection.execute(
            "UPDATE thesis_evaluation_episode_delivery_receipts SET "
            "status='reserved',reserved_at=?,reservation_expires_at=?,attempt_token=?,"
            "attempt_count=?,delivered_at=NULL,failed_at=NULL,external_ref=NULL,"
            "failure_reason=NULL WHERE receipt_id=? AND attempt_count=? "
            "AND (status='failed' OR (status='reserved' AND reservation_expires_at<=?))",
            (
                stamp.isoformat(),
                expires_at.isoformat(),
                attempt_token,
                attempt_count,
                receipt_id,
                int(row[3]),
                stamp.isoformat(),
            ),
        )
        if updated.rowcount != 1:
            current = connection.execute(
                "SELECT status,reservation_expires_at,attempt_token,attempt_count "
                "FROM thesis_evaluation_episode_delivery_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if current is None:
                raise AttentionError("delivery receipt disappeared during reservation")
            return DeliveryClaim(
                receipt_id=receipt_id,
                episode_id=episode_id,
                review_cycle_id=attention.review_cycle_id,
                channel=normalized_channel,
                surface=normalized_surface,
                status=DeliveryStatus(str(current[0])),
                claimed=False,
                attempt_token=str(current[2]),
                attempt_count=int(current[3]),
                reservation_expires_at=_parse_time(current[1]) or stamp,
            )
    claim_status = DeliveryStatus.RESERVED if claimed else existing_status
    if claim_status is None:  # Defensive: ``claimed`` is true for a new receipt.
        raise AttentionError("delivery claim has no status")
    if claimed:
        claim_attempt_token = attempt_token
    elif existing_attempt_token is not None:
        claim_attempt_token = existing_attempt_token
    else:
        raise AttentionError("existing delivery claim has no attempt token")
    return DeliveryClaim(
        receipt_id=receipt_id,
        episode_id=episode_id,
        review_cycle_id=attention.review_cycle_id,
        channel=normalized_channel,
        surface=normalized_surface,
        status=claim_status,
        claimed=claimed,
        attempt_token=claim_attempt_token,
        attempt_count=attempt_count,
        reservation_expires_at=(expires_at if claimed else existing_expiry or stamp),
    )


def complete_delivery(
    connection: sqlite3.Connection,
    receipt_id: str,
    *,
    attempt_token: str,
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
        "SELECT status,attempt_token,external_ref,failure_reason FROM "
        "thesis_evaluation_episode_delivery_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    if row is None:
        raise AttentionError(f"unknown delivery receipt: {receipt_id}")
    current = DeliveryStatus(str(row[0]))
    if current is status:
        if str(row[1]) != attempt_token or row[2] != external_ref or row[3] != failure_reason:
            raise AttentionError("delivery completion conflicts with existing receipt")
        return
    if current is not DeliveryStatus.RESERVED:
        raise AttentionError("only a reserved delivery can be completed")
    if str(row[1]) != attempt_token:
        raise AttentionError("delivery attempt token is stale")
    updated = connection.execute(
        "UPDATE thesis_evaluation_episode_delivery_receipts SET status=?,"
        "delivered_at=?,failed_at=?,external_ref=?,failure_reason=? "
        "WHERE receipt_id=? AND status='reserved' AND attempt_token=?",
        (
            status.value,
            stamp if status is DeliveryStatus.DELIVERED else None,
            stamp if status is DeliveryStatus.FAILED else None,
            external_ref,
            failure_reason,
            receipt_id,
            attempt_token,
        ),
    )
    if updated.rowcount != 1:
        raise AttentionError("delivery state changed during completion")


def deliver_episode_alert(
    connection: sqlite3.Connection,
    episode_id: str,
    *,
    delivered_at: datetime,
    user_id: str = "bhanu",
) -> int | None:
    """Reserve, create, and complete one real local-Inbox delivery."""

    stamp = _aware_utc(delivered_at)
    episode = connection.execute(
        "SELECT overall_status FROM thesis_evaluation_episodes WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    if episode is None:
        raise AttentionError(f"unknown thesis episode: {episode_id}")
    if str(episode[0]) == "ok":
        return None
    claim = reserve_delivery(
        connection,
        episode_id,
        channel="local",
        surface="inbox",
        reserved_at=stamp,
    )
    if claim is None or not claim.claimed:
        return None
    try:
        alert_id = ensure_episode_alert(
            connection,
            episode_id,
            fired_at=stamp,
            user_id=user_id,
        )
    except Exception:
        complete_delivery(
            connection,
            claim.receipt_id,
            attempt_token=claim.attempt_token,
            status=DeliveryStatus.FAILED,
            completed_at=stamp,
            failure_reason="local inbox delivery failed",
        )
        raise
    if alert_id is None:
        raise AttentionError("actionable non-OK episode did not produce an Inbox alert")
    complete_delivery(
        connection,
        claim.receipt_id,
        attempt_token=claim.attempt_token,
        status=DeliveryStatus.DELIVERED,
        completed_at=stamp,
        external_ref=f"alert:{alert_id}",
    )
    return alert_id


def deliver_due_episode_alerts(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    user_id: str = "bhanu",
) -> int:
    """Deliver every due acknowledged episode once for its dated review cycle."""

    stamp = _aware_utc(now)
    episode_ids = [
        str(row[0])
        for row in connection.execute(
            "SELECT episode_id FROM thesis_evaluation_episodes "
            "WHERE attention_state='acknowledged' AND next_review_at IS NOT NULL "
            "AND next_review_at<=? ORDER BY next_review_at,episode_id",
            (stamp.isoformat(),),
        ).fetchall()
    ]
    delivered = 0
    for episode_id in episode_ids:
        if (
            deliver_episode_alert(
                connection,
                episode_id,
                delivered_at=stamp,
                user_id=user_id,
            )
            is not None
        ):
            delivered += 1
    return delivered


__all__ = [
    "AttentionError",
    "AttentionState",
    "DeliveryClaim",
    "DeliveryStatus",
    "EpisodeAttention",
    "acknowledge_episode",
    "act_on_episode",
    "complete_delivery",
    "deliver_due_episode_alerts",
    "deliver_episode_alert",
    "ensure_episode_alert",
    "get_attention",
    "reserve_delivery",
    "should_prompt",
    "supersede_prior",
]
