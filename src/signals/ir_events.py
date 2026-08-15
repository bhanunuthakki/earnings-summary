"""Forward IR Events Ingestion & Batch Persistence Contract (BHA-15).

Governed by directives/ir_events_ingestion.md.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from calendar_clock import calendar_today
from signals.store import CADENCE_EVENT, DEFAULT_WEIGHTS, SIGNAL_INVESTOR_DAY

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

EventKind = Literal[
    "investor_day",
    "analyst_day",
    "capital_markets_day",
    "strategy_day",
]

EventStatus = Literal["scheduled", "rescheduled", "cancelled"]

SourceTier = Literal[
    "publisher_event_authority",
    "issuer_ir_announcement",
    "issuer_regulatory_announcement",
]

AttemptStatus = Literal[
    "ok",
    "not_found",
    "robots_denied",
    "rate_limited",
    "access_denied",
    "contract_error",
    "transient_error",
    "unsupported",
]

Disposition = Literal[
    "inserted",
    "replayed",
    "superseded",
    "cancelled",
    "rejected",
    "conflict",
]

RunStatus = Literal["complete", "empty", "partial", "error", "disabled"]
Freshness = Literal["fresh", "stale", "unavailable"]


class IREventObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    revision_id: str
    supersedes_revision_id: str | None = None
    issuer_id: str
    ticker: str
    event_kind: EventKind
    status: EventStatus
    title: str
    event_date: date
    starts_at: AwareDatetime | None = None
    source_timezone: str | None = None
    source_tier: SourceTier
    source_event_id: str | None = None
    source_url: str
    source_observation_id: str
    raw_sha256: str
    authority_surface_revision_id: str | None = None
    source_published_at: AwareDatetime | None = None
    observed_at: AwareDatetime


class IRSourceAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    source_tier: SourceTier
    source_url: str
    status: AttemptStatus
    http_code: int | None = None
    latency_ms: int | None = None
    record_count: int = 0
    source_observation_id: str | None = None


class IREventDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    revision_id: str
    disposition: Disposition
    reason_code: str
    signal_id: int | None = None


class IREventRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ir-events-run.v1"] = "ir-events-run.v1"
    run_id: str
    mode: Literal["dry_run", "apply"]
    status: RunStatus
    freshness: Freshness
    as_of: AwareDatetime
    calendar_date: date
    roster_sha256: str
    policy_sha256: str
    checkpoint_path: str = ""
    attempts: tuple[IRSourceAttempt, ...] = Field(default_factory=tuple)
    events: tuple[IREventObservation, ...] = Field(default_factory=tuple)
    dispositions: tuple[IREventDisposition, ...] = Field(default_factory=tuple)
    inserted: int = 0
    replayed: int = 0
    superseded: int = 0
    cancelled: int = 0
    rejected: int = 0
    conflicts: int = 0


def generate_event_id(issuer_id: str, event_kind: EventKind, stable_source_identity: str) -> str:
    """Deterministic event identifier formatted as ir-event:v1:<sha256>."""
    key = json.dumps([issuer_id, event_kind, stable_source_identity], sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"ir-event:v1:{digest}"


def generate_revision_id(event_id: str, payload: dict[str, Any], source_observation_id: str) -> str:
    """Deterministic revision identifier formatted as ir-rev:v1:<sha256>."""
    key = json.dumps(
        {"event_id": event_id, "payload": payload, "observation": source_observation_id},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"ir-rev:v1:{digest}"


def record_ir_events_batch(
    conn: sqlite3.Connection,
    observations: Sequence[IREventObservation],
    *,
    attempts: Sequence[IRSourceAttempt] = (),
    mode: Literal["dry_run", "apply"] = "apply",
    now: datetime | None = None,
    calendar_date: date | None = None,
    run_id: str | None = None,
) -> IREventRunResult:
    """Persist or evaluate a batch of IR event observations deterministically.

    Validates date admission against Pacific calendar_today, checks existing
    records in the signals table, reconciles reschedules/cancellations, and
    commits all changes atomically in a single transaction on 'apply'.
    """
    observed_now = now or datetime.now(UTC)
    cal_date = calendar_date or calendar_today()
    rid = run_id or f"ir-run-{observed_now.strftime('%Y%m%dT%H%M%SZ')}"
    max_date = cal_date + timedelta(days=548)

    dispositions: list[IREventDisposition] = []
    inserted_count = 0
    replayed_count = 0
    superseded_count = 0
    cancelled_count = 0
    rejected_count = 0
    conflict_count = 0

    stamp = observed_now.isoformat()
    weight = DEFAULT_WEIGHTS[SIGNAL_INVESTOR_DAY]

    for obs in observations:
        # 1. Admission Gate: Date range validation
        if obs.event_date < cal_date:
            dispositions.append(
                IREventDisposition(
                    event_id=obs.event_id,
                    revision_id=obs.revision_id,
                    disposition="rejected",
                    reason_code="past_date",
                )
            )
            rejected_count += 1
            continue

        if obs.event_date > max_date:
            dispositions.append(
                IREventDisposition(
                    event_id=obs.event_id,
                    revision_id=obs.revision_id,
                    disposition="rejected",
                    reason_code="date_beyond_548d_ceiling",
                )
            )
            rejected_count += 1
            continue

        # 2. Check existing active signal matching this ticker & signal_type
        cur = conn.execute(
            """
            SELECT id, title, event_date, url, firm
            FROM signals
            WHERE ticker = ? AND signal_type = ? AND (event_date = ? OR url = ?)
            """,
            (obs.ticker, SIGNAL_INVESTOR_DAY, obs.event_date.isoformat(), obs.source_url),
        )
        existing_rows = cur.fetchall()

        if obs.status == "cancelled":
            if existing_rows:
                if mode == "apply":
                    for row in existing_rows:
                        conn.execute("DELETE FROM signals WHERE id = ?", (row[0],))
                dispositions.append(
                    IREventDisposition(
                        event_id=obs.event_id,
                        revision_id=obs.revision_id,
                        disposition="cancelled",
                        reason_code="event_cancelled_removed_projection",
                        signal_id=existing_rows[0][0],
                    )
                )
                cancelled_count += 1
            else:
                dispositions.append(
                    IREventDisposition(
                        event_id=obs.event_id,
                        revision_id=obs.revision_id,
                        disposition="replayed",
                        reason_code="already_absent",
                    )
                )
                replayed_count += 1
            continue

        # Active event (scheduled / rescheduled)
        if existing_rows:
            matched_exact = False
            for row in existing_rows:
                sig_id, ex_title, ex_date, ex_url, _ = row
                if ex_date == obs.event_date.isoformat() and ex_title == obs.title and ex_url == obs.source_url:
                    matched_exact = True
                    dispositions.append(
                        IREventDisposition(
                            event_id=obs.event_id,
                            revision_id=obs.revision_id,
                            disposition="replayed",
                            reason_code="identical_evidence_replayed",
                            signal_id=sig_id,
                        )
                    )
                    replayed_count += 1
                    break

            if not matched_exact:
                # Rescheduled or updated title/url
                first_id = existing_rows[0][0]
                if mode == "apply":
                    conn.execute(
                        """
                        UPDATE signals
                        SET title = ?, event_date = ?, url = ?, firm = ?, published_at = ?
                        WHERE id = ?
                        """,
                        (
                            obs.title,
                            obs.event_date.isoformat(),
                            obs.source_url,
                            obs.issuer_id,
                            stamp,
                            first_id,
                        ),
                    )
                dispositions.append(
                    IREventDisposition(
                        event_id=obs.event_id,
                        revision_id=obs.revision_id,
                        disposition="superseded",
                        reason_code="rescheduled_or_updated_projection",
                        signal_id=first_id,
                    )
                )
                superseded_count += 1
        else:
            # Insert fresh signal
            sig_id = None
            if mode == "apply":
                insert_cur = conn.execute(
                    """
                    INSERT INTO signals
                        (ticker, signal_type, title, body, url, firm,
                         event_date, published_at, weight, cadence, source_feed,
                         news_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        obs.ticker,
                        SIGNAL_INVESTOR_DAY,
                        obs.title,
                        f"{obs.event_kind.replace('_', ' ').title()} discovered on official IR feed",
                        obs.source_url,
                        obs.issuer_id,
                        obs.event_date.isoformat(),
                        stamp,
                        weight,
                        CADENCE_EVENT,
                        "ir_events",
                        stamp,
                    ),
                )
                sig_id = insert_cur.lastrowid
            dispositions.append(
                IREventDisposition(
                    event_id=obs.event_id,
                    revision_id=obs.revision_id,
                    disposition="inserted",
                    reason_code="new_event_inserted",
                    signal_id=sig_id,
                )
            )
            inserted_count += 1

    if mode == "apply":
        conn.commit()

    run_status: RunStatus = "complete"
    if not observations:
        run_status = "empty"
    elif rejected_count > 0 or conflict_count > 0:
        run_status = "partial" if inserted_count + replayed_count + superseded_count > 0 else "error"

    roster_hash = hashlib.sha256(",".join(sorted({o.ticker for o in observations})).encode("utf-8")).hexdigest()
    policy_hash = hashlib.sha256(b"ir_events_policy_v1").hexdigest()

    return IREventRunResult(
        run_id=rid,
        mode=mode,
        status=run_status,
        freshness="fresh",
        as_of=observed_now,
        calendar_date=cal_date,
        roster_sha256=roster_hash,
        policy_sha256=policy_hash,
        attempts=tuple(attempts),
        events=tuple(observations),
        dispositions=tuple(dispositions),
        inserted=inserted_count,
        replayed=replayed_count,
        superseded=superseded_count,
        cancelled=cancelled_count,
        rejected=rejected_count,
        conflicts=conflict_count,
    )
