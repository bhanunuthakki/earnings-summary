"""Read-only evidence adapter for reviewing latest sizing intents.

This module deliberately reports persisted owner evidence only.  It does not
turn an intent, target band, holdings observation, or price level into an
allocation recommendation; that remains a separate governed decision.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from advisor.price_action_bands import (
    PriceActionBandProjection,
    resolve_price_action_bands,
)
from identity import DEFAULT_USER_ID
from research.owner_decision_checkpoint import (
    CheckpointInvariantError,
    HoldingAvailability,
    OwnerDecisionCheckpointPayload,
    SizingIntentSpec,
    TargetBand,
    TargetVerification,
    payload_sha256,
)
from user_state._db import open_read_conn, parse_dt
from user_state.sizing import PositionSizingIntentRow

__all__ = [
    "SizingIntentReview",
    "SizingIntentReviewEntry",
    "load_sizing_intent_review",
    "load_sizing_intent_review_entry",
    "load_sizing_intent_review_from_connection",
]


@dataclass(frozen=True, slots=True)
class SizingIntentReviewEntry:
    """One latest owner-recorded sizing intent and any persisted checkpoint evidence."""

    intent: PositionSizingIntentRow
    checkpoint_linked: bool
    checkpoint_evidence_available: bool
    checkpoint_id: int | None = None
    checkpoint_schema_version: str | None = None
    checkpoint_payload_sha256: str | None = None
    checkpoint_source_channel: str | None = None
    checkpoint_source_event_id: str | None = None
    checkpoint_confirmed_at: str | None = None
    holdings_source: str | None = None
    holdings_as_of: str | None = None
    holdings_source_content_sha256: str | None = None
    holding_availability: HoldingAvailability | None = None
    observed_weight_pct: float | None = None
    target_verification: TargetVerification | None = None
    target_band: TargetBand | None = None
    price_level: float | None = None
    price_action_bands: PriceActionBandProjection = field(
        default_factory=lambda: resolve_price_action_bands(
            owner_ratified=None,
            source_available=False,
        )
    )


@dataclass(frozen=True, slots=True)
class SizingIntentReview:
    """Latest sizing-intent evidence, with source availability made explicit."""

    sizing_intent_source_available: bool
    checkpoint_link_source_available: bool
    entries: tuple[SizingIntentReviewEntry, ...]
    # Intent evidence is not a governed allocation recommendation.  Keeping
    # this explicitly empty prevents a review consumer from mistaking a target
    # band or an owner-stated intent for a recommendation.
    governed_allocation_recommendation: None = field(default=None, init=False)


@dataclass(frozen=True, slots=True)
class _CheckpointLink:
    checkpoint_id: int
    leg_id: str
    schema_version: str
    payload_sha256: str
    payload_json: str
    confirmed_at: str


def load_sizing_intent_review(
    db_path: Path | str,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> SizingIntentReview:
    """Load latest owner-recorded intents and checkpoint evidence without writing.

    A missing or unreadable ``position_sizing_intent`` source is represented as
    unavailable rather than as an empty history.  When the checkpoint relations
    are unavailable, the intent history remains usable but checkpoint linkage is
    explicitly unavailable.
    """

    try:
        conn = open_read_conn(db_path)
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        return SizingIntentReview(False, False, ())

    try:
        return load_sizing_intent_review_from_connection(conn, user_id=user_id)
    finally:
        conn.close()


def load_sizing_intent_review_from_connection(
    conn: sqlite3.Connection,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> SizingIntentReview:
    """Project sizing evidence through an existing request-scoped connection."""

    intents = _latest_intents(conn, user_id=user_id)
    if intents is None:
        return SizingIntentReview(False, False, ())
    links = _checkpoint_links(conn, intents)
    if links is None:
        return SizingIntentReview(
            True,
            False,
            tuple(
                SizingIntentReviewEntry(
                    intent=intent,
                    checkpoint_linked=False,
                    checkpoint_evidence_available=False,
                )
                for intent in intents
            ),
        )
    return SizingIntentReview(
        True,
        True,
        tuple(_review_entry(intent, links.get(intent.id)) for intent in intents),
    )


def load_sizing_intent_review_entry(
    db_path: Path | str,
    *,
    sizing_intent_id: int,
    user_id: str = DEFAULT_USER_ID,
) -> SizingIntentReview:
    """Load one exact persisted intent with the same evidence verification.

    This is for append-only receipt replay, where a previously confirmed intent
    need not be the current intent for its ticker and kind.
    """

    try:
        conn = open_read_conn(db_path)
    except (FileNotFoundError, RuntimeError, sqlite3.Error):
        return SizingIntentReview(False, False, ())

    try:
        intents = _intents_by_id(conn, sizing_intent_id=sizing_intent_id, user_id=user_id)
        if intents is None:
            return SizingIntentReview(False, False, ())
        links = _checkpoint_links(conn, intents)
        if links is None:
            return SizingIntentReview(
                True,
                False,
                tuple(
                    SizingIntentReviewEntry(
                        intent=intent,
                        checkpoint_linked=False,
                        checkpoint_evidence_available=False,
                    )
                    for intent in intents
                ),
            )
        return SizingIntentReview(
            True,
            True,
            tuple(_review_entry(intent, links.get(intent.id)) for intent in intents),
        )
    finally:
        conn.close()


def _latest_intents(
    conn: sqlite3.Connection, *, user_id: str
) -> tuple[PositionSizingIntentRow, ...] | None:
    has_withdrawals = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='position_sizing_intent_withdrawals'"
    ).fetchone()
    withdrawal_exclusion = ""
    if has_withdrawals is not None:
        withdrawal_exclusion = (
            "AND NOT EXISTS (SELECT 1 FROM position_sizing_intent_withdrawals AS withdrawal "
            "WHERE withdrawal.user_id=position_sizing_intent.user_id "
            "AND withdrawal.sizing_intent_id=position_sizing_intent.id)"
        )
    has_supersessions = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='position_sizing_intent_supersessions'"
    ).fetchone()
    supersession_exclusion = ""
    if has_supersessions is not None:
        supersession_exclusion = (
            "AND NOT EXISTS (SELECT 1 FROM position_sizing_intent_supersessions AS "
            "supersession WHERE supersession.user_id=position_sizing_intent.user_id "
            "AND supersession.superseded_intent_id=position_sizing_intent.id)"
        )
    try:
        query = (
            "SELECT id,user_id,ticker,intent_kind,intent_value,narrative,created_at,updated_at "
            "FROM position_sizing_intent WHERE user_id = ? "
            f"{withdrawal_exclusion} "  # nosec B608 -- exclusion clauses are fixed literals selected only by schema presence
            f"{supersession_exclusion} "
            "ORDER BY created_at DESC, id DESC"
        )
        rows = conn.execute(query, (user_id,)).fetchall()
    except sqlite3.OperationalError:
        return None

    latest: dict[tuple[str, str], PositionSizingIntentRow] = {}
    for row in rows:
        intent = PositionSizingIntentRow(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            ticker=str(row["ticker"]),
            intent_kind=str(row["intent_kind"]),
            intent_value=(None if row["intent_value"] is None else float(row["intent_value"])),
            narrative=None if row["narrative"] is None else str(row["narrative"]),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
        )
        latest.setdefault((intent.ticker.upper(), intent.intent_kind), intent)
    return tuple(latest.values())


def _intents_by_id(
    conn: sqlite3.Connection, *, sizing_intent_id: int, user_id: str
) -> tuple[PositionSizingIntentRow, ...] | None:
    try:
        row = conn.execute(
            """
            SELECT id,user_id,ticker,intent_kind,intent_value,narrative,created_at,updated_at
            FROM position_sizing_intent WHERE id=? AND user_id=?
            """,
            (sizing_intent_id, user_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return ()
    return (
        PositionSizingIntentRow(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            ticker=str(row["ticker"]),
            intent_kind=str(row["intent_kind"]),
            intent_value=(None if row["intent_value"] is None else float(row["intent_value"])),
            narrative=None if row["narrative"] is None else str(row["narrative"]),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
        ),
    )


def _checkpoint_links(
    conn: sqlite3.Connection,
    intents: tuple[PositionSizingIntentRow, ...],
) -> dict[int, _CheckpointLink] | None:
    if not intents:
        try:
            conn.execute(
                "SELECT 1 FROM owner_decision_checkpoint_sizing_intents LIMIT 1"
            ).fetchone()
            conn.execute("SELECT 1 FROM owner_decision_checkpoints LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            return None
        return {}

    placeholders = ",".join("?" for _ in intents)
    query = f"""
        SELECT
            links.sizing_intent_id,
            links.checkpoint_id,
            links.leg_id,
            checkpoints.checkpoint_schema_version,
            checkpoints.payload_sha256,
            checkpoints.payload_json,
            checkpoints.confirmed_at
        FROM owner_decision_checkpoint_sizing_intents AS links
        JOIN owner_decision_checkpoints AS checkpoints ON checkpoints.id = links.checkpoint_id
        WHERE links.sizing_intent_id IN ({placeholders})
        """  # nosec B608 - only placeholder count is interpolated; values stay bound.
    try:
        rows = conn.execute(
            query,
            tuple(intent.id for intent in intents),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    return {
        int(row["sizing_intent_id"]): _CheckpointLink(
            checkpoint_id=int(row["checkpoint_id"]),
            leg_id=str(row["leg_id"]),
            schema_version=str(row["checkpoint_schema_version"]),
            payload_sha256=str(row["payload_sha256"]),
            payload_json=str(row["payload_json"]),
            confirmed_at=str(row["confirmed_at"]),
        )
        for row in rows
    }


def _review_entry(
    intent: PositionSizingIntentRow, link: _CheckpointLink | None
) -> SizingIntentReviewEntry:
    if link is None:
        return SizingIntentReviewEntry(
            intent=intent,
            checkpoint_linked=False,
            checkpoint_evidence_available=False,
            price_action_bands=resolve_price_action_bands(
                owner_ratified=None,
            ),
        )

    try:
        payload = OwnerDecisionCheckpointPayload.model_validate_json(link.payload_json)
        checkpoint_intent = next(
            item for item in payload.sizing_intents if item.leg_id == link.leg_id
        )
        leg = next(item for item in payload.legs if item.leg_id == link.leg_id)
        if not _checkpoint_intent_matches(intent, checkpoint_intent):
            raise ValueError("checkpoint sizing intent does not match its persisted row")
        if payload_sha256(payload) != link.payload_sha256:
            raise ValueError("checkpoint payload digest does not match persisted provenance")
        holding = payload.holdings_basis.position(intent.ticker)
    except (CheckpointInvariantError, StopIteration, ValueError):
        return SizingIntentReviewEntry(
            intent=intent,
            checkpoint_linked=True,
            checkpoint_evidence_available=False,
            checkpoint_id=link.checkpoint_id,
            checkpoint_schema_version=link.schema_version,
            checkpoint_payload_sha256=link.payload_sha256,
            checkpoint_confirmed_at=link.confirmed_at,
            price_action_bands=resolve_price_action_bands(
                owner_ratified=None,
                source_available=False,
            ),
        )

    return SizingIntentReviewEntry(
        intent=intent,
        checkpoint_linked=True,
        checkpoint_evidence_available=True,
        checkpoint_id=link.checkpoint_id,
        checkpoint_schema_version=link.schema_version,
        checkpoint_payload_sha256=link.payload_sha256,
        checkpoint_confirmed_at=link.confirmed_at,
        checkpoint_source_channel=payload.source_channel,
        checkpoint_source_event_id=payload.source_event_id,
        holdings_source=payload.holdings_basis.source,
        holdings_as_of=payload.holdings_basis.as_of,
        holdings_source_content_sha256=payload.holdings_basis.source_content_sha256,
        holding_availability=holding.availability,
        observed_weight_pct=holding.weight_pct,
        target_verification=leg.target_verification,
        target_band=checkpoint_intent.target_band,
        price_level=leg.price_level,
        price_action_bands=resolve_price_action_bands(
            owner_ratified=checkpoint_intent.price_action_bands,
            checkpoint_id=link.checkpoint_id,
            checkpoint_payload_sha256=link.payload_sha256,
        ),
    )


def _checkpoint_intent_matches(
    intent: PositionSizingIntentRow,
    checkpoint_intent: SizingIntentSpec,
) -> bool:
    """Require the linked payload to identify this exact persisted intent."""

    if (
        checkpoint_intent.ticker != intent.ticker.upper()
        or checkpoint_intent.intent_kind != intent.intent_kind
    ):
        return False
    if checkpoint_intent.intent_value is None:
        return True
    return (
        intent.intent_value is not None
        and abs(checkpoint_intent.intent_value - intent.intent_value) <= 1e-9
    )
