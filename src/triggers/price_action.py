"""Dark, deterministic sensor for owner-ratified BHA-85 price-action bands.

The sensor deliberately consumes only the frozen structured owner bands stored
inside an immutable owner-decision checkpoint.  It does not interpret prose,
derive a missing rung, fetch a quote, queue an action, or place a trade.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar, Literal, cast

from advisor.price_action_bands import (
    PriceActionBands,
    PriceActionProjectionState,
    resolve_price_action_bands,
)
from alerts.store import ALERT_STATUS_EXPIRED, compute_signature_sha, fire_alert
from clock import now_naive_utc, to_naive_utc
from dcf.latest import latest_dcf_row
from identity import DEFAULT_USER_ID
from research.owner_decision_checkpoint import (
    OwnerDecisionCheckpointPayload,
    SizingIntentSpec,
    payload_sha256,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from triggers.base import Cadence, StatefulTriggerResult

PriceAction = Literal["add", "trim", "sell"]
Phase = Literal["clear", "approaching", "breached"]
Side = Literal["at_or_below", "at_or_above"]

MAX_PRICE_AGE = timedelta(days=1)
REARM_BPS = 100


@dataclass(frozen=True, slots=True)
class PriceObservation:
    price: float
    currency: str
    as_of: datetime
    source_ref: str
    source_sha256: str


@dataclass(frozen=True, slots=True)
class PriceActionRung:
    rung_id: str
    action: PriceAction
    side: Side
    level: float
    approach_level: float | None


@dataclass(frozen=True, slots=True)
class PriceActionLadderSnapshot:
    """Validated, versioned input boundary owned by BHA-85 checkpoint evidence."""

    ladder_id: str
    revision_sha256: str
    checkpoint_id: int
    checkpoint_payload_sha256: str
    ticker: str
    currency: str
    rungs: tuple[PriceActionRung, ...]
    observation: PriceObservation


def classify_rung(rung: PriceActionRung, price: float) -> Phase:
    """Classify exact owner thresholds; equality is always a breach."""

    if rung.side == "at_or_above":
        if price >= rung.level:
            return "breached"
        if rung.approach_level is not None and price >= rung.approach_level:
            return "approaching"
    else:
        if price <= rung.level:
            return "breached"
        if rung.approach_level is not None and price <= rung.approach_level:
            return "approaching"
    return "clear"


def rearm_safe(rung: PriceActionRung, price: float) -> bool:
    """Require a material move away from the rung before starting a new cycle."""

    spread = REARM_BPS / 10_000
    if rung.side == "at_or_above":
        reset = rung.level * (1 - spread)
        if rung.approach_level is not None:
            reset = min(reset, rung.approach_level)
        return price < reset
    reset = rung.level * (1 + spread)
    if rung.approach_level is not None:
        reset = max(reset, rung.approach_level)
    return price > reset


class PriceActionTrigger:
    """Stateful alert-only sensor, intentionally excluded from default runs."""

    kind: ClassVar[str] = "price_action"
    cadence: ClassVar[Cadence] = Cadence.DAILY

    def run(
        self,
        *,
        ticker: str,
        db_path: Path,
        user_id: str = DEFAULT_USER_ID,
        dry_run: bool,
        as_of: datetime | None = None,
    ) -> StatefulTriggerResult:
        now = to_naive_utc(as_of or now_naive_utc())
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER)
        try:
            conn.execute("BEGIN IMMEDIATE")
            snapshot = load_price_action_snapshot(conn, ticker=ticker, user_id=user_id, as_of=now)
            if snapshot is None:
                conn.rollback()
                return StatefulTriggerResult(no_candidate=True)
            result = self.advance(
                conn, snapshot=snapshot, user_id=user_id, now=now, dry_run=dry_run
            )
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def advance(
        self,
        conn: sqlite3.Connection,
        *,
        snapshot: PriceActionLadderSnapshot,
        user_id: str,
        now: datetime,
        dry_run: bool,
    ) -> StatefulTriggerResult:
        fired = 0
        dedup = 0
        for rung in snapshot.rungs:
            state = _load_state(conn, user_id=user_id, snapshot=snapshot, rung=rung)
            generation = 0 if state is None else int(state["generation"])
            prior_phase: Phase = "clear" if state is None else _phase(str(state["phase"]))
            phase = classify_rung(rung, snapshot.observation.price)
            if prior_phase != "clear" and rearm_safe(rung, snapshot.observation.price):
                generation += 1
                prior_phase = "clear"
                _record_event(conn, snapshot, rung, generation, "rearmed", now, None, user_id)
            # A breach is terminal for its generation.  Letting a price move
            # back into the approach band without a full rearm emit a second
            # pending alert leaves that alert stale when the original breach
            # deduplicates on a subsequent crossing.
            if prior_phase == "breached" and phase == "approaching":
                phase = "breached"
            if phase == "clear":
                _upsert_state(conn, snapshot, rung, generation, "clear", now, None, None, user_id)
                continue
            if phase == prior_phase:
                _upsert_state(conn, snapshot, rung, generation, phase, now, state, None, user_id)
                dedup += 1
                continue
            signature = _signature(snapshot, rung, phase, generation)
            evidence = _evidence(snapshot, rung, phase, generation, now)
            prior_alert_id = None if state is None else state["last_approaching_alert_id"]
            alert_id: int | None = None
            existing = conn.execute(
                "SELECT id FROM alerts WHERE user_id=? AND signature_sha=? AND status<>? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, signature, ALERT_STATUS_EXPIRED),
            ).fetchone()
            if existing is not None:
                alert_id = int(existing["id"])
                dedup += 1
            elif dry_run:
                fired += 1
            else:
                alert = fire_alert(
                    user_id=user_id,
                    ticker=snapshot.ticker,
                    trigger_kind=self.kind,
                    fired_at=now,
                    evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                    signature_sha=signature,
                    conn=conn,
                )
                alert_id = alert.id
                fired += 1
                if phase == "breached" and prior_alert_id is not None and existing is None:
                    conn.execute(
                        "UPDATE alerts SET status=? WHERE id=? AND status='pending'",
                        (ALERT_STATUS_EXPIRED, int(prior_alert_id)),
                    )
            _upsert_state(conn, snapshot, rung, generation, phase, now, state, alert_id, user_id)
            _record_event(conn, snapshot, rung, generation, phase, now, alert_id, user_id)
        return StatefulTriggerResult(
            alerts_fired=fired, dedup_skips=dedup, dry_run_alerts=fired if dry_run else 0
        )


def load_price_action_snapshot(
    conn: sqlite3.Connection, *, ticker: str, user_id: str, as_of: datetime
) -> PriceActionLadderSnapshot | None:
    """Load only a complete owner-ratified BHA-85 ladder plus persisted price."""

    try:
        row = conn.execute(
            """
            SELECT checkpoints.id, checkpoints.payload_sha256, checkpoints.payload_json
            FROM owner_decision_checkpoints AS checkpoints
            JOIN owner_decision_checkpoint_sizing_intents AS links ON links.checkpoint_id=checkpoints.id
            JOIN position_sizing_intent AS intents ON intents.id=links.sizing_intent_id
            WHERE checkpoints.user_id=? AND UPPER(intents.ticker)=?
            ORDER BY checkpoints.confirmed_at DESC, checkpoints.id DESC LIMIT 1
            """,
            (user_id, ticker.upper()),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = OwnerDecisionCheckpointPayload.model_validate_json(str(row["payload_json"]))
        if payload_sha256(payload) != str(row["payload_sha256"]):
            return None
        linked = conn.execute(
            "SELECT leg_id FROM owner_decision_checkpoint_sizing_intents WHERE checkpoint_id=? ORDER BY leg_ordinal",
            (int(row["id"]),),
        ).fetchall()
        linked_legs = {str(item["leg_id"]) for item in linked}
        intents: tuple[SizingIntentSpec, ...] = payload.sizing_intents
        bands = next(
            (
                intent.price_action_bands
                for intent in intents
                if intent.ticker == ticker.upper()
                and intent.leg_id in linked_legs
                and intent.price_action_bands is not None
            ),
            None,
        )
    except (StopIteration, ValueError, sqlite3.Error):
        return None
    if bands is None:
        return None
    projection = resolve_price_action_bands(
        owner_ratified=bands,
        checkpoint_id=int(row["id"]),
        checkpoint_payload_sha256=str(row["payload_sha256"]),
    )
    if projection.state is not PriceActionProjectionState.RATIFIED or not projection.is_actionable:
        return None
    price_row = latest_dcf_row(conn, ticker)
    if (
        price_row is None
        or price_row.live_price is None
        or price_row.live_price <= 0
        or price_row.live_price_at is None
        or price_row.currency is None
        or price_row.currency.upper() != bands.currency
    ):
        return None
    try:
        observed_at = to_naive_utc(
            datetime.fromisoformat(price_row.live_price_at.replace("Z", "+00:00"))
        )
    except ValueError:
        return None
    if as_of - observed_at > MAX_PRICE_AGE or observed_at > as_of + timedelta(minutes=5):
        return None
    observation = PriceObservation(
        price=price_row.live_price,
        currency=bands.currency,
        as_of=observed_at,
        source_ref=f"dcf_runs:{price_row.id}",
        source_sha256=_sha(
            f"dcf_runs:{price_row.id}|{price_row.live_price}|{price_row.live_price_at}"
        ),
    )
    rungs = _rungs(bands)
    if not rungs:
        return None
    return PriceActionLadderSnapshot(
        ladder_id=f"checkpoint:{int(row['id'])}:price-action-bands",
        revision_sha256=_sha(f"{bands.revision}|{bands.source_content_sha256}"),
        checkpoint_id=int(row["id"]),
        checkpoint_payload_sha256=str(row["payload_sha256"]),
        ticker=ticker.upper(),
        currency=bands.currency,
        rungs=rungs,
        observation=observation,
    )


def _rungs(bands: PriceActionBands) -> tuple[PriceActionRung, ...]:
    assert (
        bands.add_below is not None
        and bands.trim_above is not None
        and bands.sell_above is not None
    )
    approaches = bands.approach_bands
    add_below = bands.add_below
    trim_above = bands.trim_above
    sell_above = bands.sell_above
    rungs = (
        PriceActionRung(
            "add",
            "add",
            "at_or_below",
            add_below,
            None if approaches is None else approaches.add_buy_below,
        ),
        PriceActionRung(
            "trim",
            "trim",
            "at_or_above",
            trim_above,
            None if approaches is None else approaches.trim_above,
        ),
        PriceActionRung(
            "sell",
            "sell",
            "at_or_above",
            sell_above,
            None if approaches is None else approaches.sell_above,
        ),
    )
    for rung in rungs:
        if rung.approach_level is not None and (
            (rung.side == "at_or_above" and rung.approach_level >= rung.level)
            or (rung.side == "at_or_below" and rung.approach_level <= rung.level)
        ):
            return ()
    return rungs


def _signature(
    snapshot: PriceActionLadderSnapshot, rung: PriceActionRung, phase: Phase, generation: int
) -> str:
    return compute_signature_sha(
        "price_action",
        snapshot.ticker,
        {
            "ladder_id": snapshot.ladder_id,
            "ladder_revision_sha256": snapshot.revision_sha256,
            "rung_id": rung.rung_id,
            "action": rung.action,
            "phase": phase,
            "generation": generation,
        },
    )


def _evidence(
    snapshot: PriceActionLadderSnapshot,
    rung: PriceActionRung,
    phase: Phase,
    generation: int,
    now: datetime,
) -> dict[str, object]:
    return {
        "schema_version": "price-action-sensor/v1",
        "ladder_id": snapshot.ladder_id,
        "ladder_revision_sha256": snapshot.revision_sha256,
        "checkpoint_id": snapshot.checkpoint_id,
        "checkpoint_payload_sha256": snapshot.checkpoint_payload_sha256,
        "rung": {
            "id": rung.rung_id,
            "action": rung.action,
            "side": rung.side,
            "level": rung.level,
            "approach_level": rung.approach_level,
        },
        "phase": phase,
        "generation": generation,
        "observation": {
            "price": snapshot.observation.price,
            "currency": snapshot.observation.currency,
            "as_of": snapshot.observation.as_of.isoformat(),
            "source_ref": snapshot.observation.source_ref,
            "source_sha256": snapshot.observation.source_sha256,
        },
        "computed_at": now.isoformat(),
    }


def _load_state(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    snapshot: PriceActionLadderSnapshot,
    rung: PriceActionRung,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM price_action_sensor_state WHERE user_id=? AND ticker=? AND ladder_id=? AND ladder_revision_sha256=? AND rung_id=?",
        (user_id, snapshot.ticker, snapshot.ladder_id, snapshot.revision_sha256, rung.rung_id),
    ).fetchone()


def _upsert_state(
    conn: sqlite3.Connection,
    snapshot: PriceActionLadderSnapshot,
    rung: PriceActionRung,
    generation: int,
    phase: Phase,
    now: datetime,
    prior: sqlite3.Row | None,
    alert_id: int | None,
    user_id: str,
) -> None:
    approach_id = (
        alert_id
        if phase == "approaching" and alert_id is not None
        else (None if prior is None else prior["last_approaching_alert_id"])
    )
    breach_id = (
        alert_id
        if phase == "breached" and alert_id is not None
        else (None if prior is None else prior["last_breached_alert_id"])
    )
    conn.execute(
        """INSERT INTO price_action_sensor_state(user_id,ticker,ladder_id,ladder_revision_sha256,rung_id,action,trigger_side,phase,generation,last_price,last_observed_at,last_source_ref,last_source_sha256,phase_entered_at,last_approaching_alert_id,last_breached_alert_id,rearmed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,ticker,ladder_id,ladder_revision_sha256,rung_id) DO UPDATE SET action=excluded.action,trigger_side=excluded.trigger_side,phase=excluded.phase,generation=excluded.generation,last_price=excluded.last_price,last_observed_at=excluded.last_observed_at,last_source_ref=excluded.last_source_ref,last_source_sha256=excluded.last_source_sha256,phase_entered_at=CASE WHEN price_action_sensor_state.phase=excluded.phase THEN price_action_sensor_state.phase_entered_at ELSE excluded.phase_entered_at END,last_approaching_alert_id=excluded.last_approaching_alert_id,last_breached_alert_id=excluded.last_breached_alert_id,rearmed_at=excluded.rearmed_at,updated_at=excluded.updated_at""",
        (
            user_id,
            snapshot.ticker,
            snapshot.ladder_id,
            snapshot.revision_sha256,
            rung.rung_id,
            rung.action,
            rung.side,
            phase,
            generation,
            snapshot.observation.price,
            snapshot.observation.as_of.isoformat(),
            snapshot.observation.source_ref,
            snapshot.observation.source_sha256,
            now.isoformat(),
            approach_id,
            breach_id,
            now.isoformat()
            if generation > (0 if prior is None else int(prior["generation"]))
            else None,
            now.isoformat(),
        ),
    )


def _record_event(
    conn: sqlite3.Connection,
    snapshot: PriceActionLadderSnapshot,
    rung: PriceActionRung,
    generation: int,
    transition: str,
    now: datetime,
    alert_id: int | None,
    user_id: str,
) -> None:
    event_phase: Phase = "clear" if transition == "rearmed" else _phase(transition)
    evidence = _evidence(snapshot, rung, event_phase, generation, now)
    key = _sha(
        json.dumps(
            {
                "user_id": user_id,
                "ticker": snapshot.ticker,
                "ladder": snapshot.ladder_id,
                "revision": snapshot.revision_sha256,
                "rung": rung.rung_id,
                "generation": generation,
                "transition": transition,
            },
            sort_keys=True,
        )
    )
    conn.execute(
        "INSERT OR IGNORE INTO price_action_sensor_events(event_key,user_id,ticker,ladder_id,ladder_revision_sha256,rung_id,generation,transition,observed_at,source_ref,source_sha256,alert_id,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            key,
            user_id,
            snapshot.ticker,
            snapshot.ladder_id,
            snapshot.revision_sha256,
            rung.rung_id,
            generation,
            transition,
            snapshot.observation.as_of.isoformat(),
            snapshot.observation.source_ref,
            snapshot.observation.source_sha256,
            alert_id,
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            now.isoformat(),
        ),
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _phase(value: str) -> Phase:
    if value not in {"clear", "approaching", "breached"}:
        raise ValueError(f"invalid persisted price-action phase: {value!r}")
    return cast(Phase, value)
