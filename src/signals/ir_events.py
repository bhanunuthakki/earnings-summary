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
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from calendar_clock import calendar_today
from ir_pipeline.authority import IRAuthorityEvidence
from signals.store import CADENCE_SCHEDULED, DEFAULT_WEIGHTS, SIGNAL_INVESTOR_DAY

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
    "stale",
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
    source_locator: str = ""
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
    reason_code: str = ""
    authority_evidence: IRAuthorityEvidence | None = None
    http_code: int | None = None
    latency_ms: int | None = None
    record_count: int = 0
    source_observation_id: str | None = None
    observed_at: AwareDatetime | None = None


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
    attempt_id: str = Field(default_factory=lambda: str(uuid4()))
    tickers: tuple[str, ...] = ()
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
    key = json.dumps(
        [issuer_id, event_kind, stable_source_identity], sort_keys=True, separators=(",", ":")
    )
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


def _current_projection(
    conn: sqlite3.Connection, observations: Sequence[IREventObservation]
) -> tuple[IREventObservation | None, bool]:
    latest: dict[str, IREventObservation] = {}
    for item in observations:
        row = conn.execute(
            "SELECT surface_key FROM issuer_authority_surface_revisions WHERE surface_revision_id=?",
            (item.authority_surface_revision_id,),
        ).fetchone()
        if row is None:
            raise ValueError("missing event authority")
        key = str(row[0])
        previous = latest.get(key)
        if previous is None or item.observed_at >= previous.observed_at:
            latest[key] = item
    ranks = {
        "publisher_event_authority": 0,
        "issuer_ir_announcement": 1,
        "issuer_regulatory_announcement": 2,
    }
    best = min(ranks[item.source_tier] for item in latest.values())
    winners = [item for item in latest.values() if ranks[item.source_tier] == best]
    semantics = {
        (item.event_date, item.starts_at, item.status == "cancelled", item.title)
        for item in winners
    }
    if len(semantics) > 1:
        return None, True
    return max(winners, key=lambda item: item.observed_at), False


def _project(
    conn: sqlite3.Connection, event_id: str, current: IREventObservation | None, stamp: str
) -> None:
    if current is None or current.status == "cancelled":
        conn.execute("DELETE FROM signals WHERE ir_event_id=?", (event_id,))
        return
    issuer = conn.execute(
        "SELECT legal_name FROM issuer_profile_revisions WHERE issuer_id=? ORDER BY revision DESC LIMIT 1",
        (current.issuer_id,),
    ).fetchone()
    conn.execute(
        """INSERT INTO signals(ticker,signal_type,title,url,firm,event_date,published_at,weight,
           cadence,source_feed,created_at,ir_event_id,ir_event_revision_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ir_event_id) WHERE ir_event_id IS NOT NULL DO UPDATE SET
           ticker=excluded.ticker,title=excluded.title,url=excluded.url,firm=excluded.firm,
           event_date=excluded.event_date,published_at=excluded.published_at,
           ir_event_revision_id=excluded.ir_event_revision_id""",
        (
            current.ticker,
            SIGNAL_INVESTOR_DAY,
            current.title,
            current.source_url,
            str(issuer[0]) if issuer else current.issuer_id,
            current.event_date.isoformat(),
            (current.source_published_at or current.observed_at).isoformat(),
            DEFAULT_WEIGHTS[SIGNAL_INVESTOR_DAY],
            CADENCE_SCHEDULED,
            "ir_events",
            stamp,
            current.event_id,
            current.revision_id,
        ),
    )


def record_ir_events_batch(
    conn: sqlite3.Connection,
    observations: Sequence[IREventObservation],
    *,
    attempts: Sequence[IRSourceAttempt] = (),
    mode: Literal["dry_run", "apply"] = "apply",
    now: datetime | None = None,
    calendar_date: date | None = None,
    run_id: str | None = None,
    tickers: Sequence[str] = (),
) -> IREventRunResult:
    """Validate immutable publisher evidence, append revisions, reconcile atomically.

    The caller owns the database writer lock; this interface owns one transaction
    and rejects a connection with unrelated pending writes. Dry runs never write.
    """
    # Discovery consumes these public models; the delayed import breaks that cycle.
    from signals.ir_event_discovery import POLICY_SHA256, discover_ir_events

    observed_now = now or datetime.now(UTC)
    if observed_now.tzinfo is None:
        raise ValueError("batch clock must be timezone aware")
    cal_date = calendar_date or calendar_today(observed_now)
    roster = tuple(
        sorted(
            set(
                tickers
                or [item.ticker for item in observations]
                or [item.ticker for item in attempts]
            )
        )
    )
    roster_sha = hashlib.sha256(json.dumps(roster, separators=(",", ":")).encode()).hexdigest()
    rid = run_id or f"ir_events_{cal_date}_{roster_sha[:12]}_{POLICY_SHA256[:12]}"
    if conn.in_transaction:
        raise ValueError("IR batch requires a clean transaction boundary")
    # Arbitrary constructed observations cannot bypass capture, issuer, clock, or
    # parser admission. Re-read immutable evidence once for the entire batch.
    if observations or attempts or tickers:
        discovered = discover_ir_events(conn, roster, now=observed_now)
        admitted = {item.revision_id: item for item in discovered.events}
        if (attempts or tickers) and (
            tuple(attempts) != discovered.attempts
            or {item.revision_id for item in observations}
            != {item.revision_id for item in discovered.events}
        ):
            raise ValueError("batch does not reconcile to complete captured-source discovery")
        for item in observations:
            expected = admitted.get(item.revision_id)
            if expected is None or expected.model_dump(
                exclude={"supersedes_revision_id"}
            ) != item.model_dump(exclude={"supersedes_revision_id"}):
                raise ValueError("event observation differs from verified publisher capture")
    dispositions: list[IREventDisposition] = []
    histories: dict[str, list[IREventObservation]] = {}
    counts = {
        "inserted": 0,
        "replayed": 0,
        "superseded": 0,
        "cancelled": 0,
        "rejected": 0,
        "conflict": 0,
    }
    if mode == "apply":
        conn.execute("SAVEPOINT ir_event_batch")
    try:
        for item in observations:
            reason = ""
            disposition: Disposition
            if item.event_date < cal_date:
                disposition, reason = "rejected", "past_date"
            elif item.event_date > cal_date + timedelta(days=548):
                disposition, reason = "rejected", "date_beyond_548d_ceiling"
            else:
                if item.event_id not in histories:
                    histories[item.event_id] = [
                        IREventObservation.model_validate_json(str(row[0]))
                        for row in conn.execute(
                            "SELECT observation_json FROM ir_event_revisions WHERE event_id=? ORDER BY revision",
                            (item.event_id,),
                        )
                    ]
                history = histories[item.event_id]
                replay = next(
                    (prior for prior in history if prior.revision_id == item.revision_id), None
                )
                if replay is not None:
                    if replay.model_dump(exclude={"supersedes_revision_id"}) != item.model_dump(
                        exclude={"supersedes_revision_id"}
                    ):
                        raise ValueError("immutable event revision conflict")
                    _, conflict = _current_projection(conn, history)
                    disposition = "conflict" if conflict else "replayed"
                    reason = (
                        "same_tier_authority_disagreement" if conflict else "exact_revision_replay"
                    )
                else:
                    previous = history[-1].revision_id if history else None
                    if item.supersedes_revision_id not in (None, previous):
                        raise ValueError("event predecessor mismatch")
                    retained = item.model_copy(update={"supersedes_revision_id": previous})
                    if mode == "apply":
                        conn.execute(
                            """INSERT INTO ir_event_revisions(revision_id,event_id,revision,supersedes_revision_id,
                               issuer_id,ticker,event_kind,status,title,event_date,source_tier,source_observation_id,
                               authority_surface_revision_id,raw_sha256,observed_at,observation_json)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                item.revision_id,
                                item.event_id,
                                len(history) + 1,
                                previous,
                                item.issuer_id,
                                item.ticker,
                                item.event_kind,
                                item.status,
                                item.title,
                                item.event_date.isoformat(),
                                item.source_tier,
                                item.source_observation_id,
                                item.authority_surface_revision_id,
                                item.raw_sha256,
                                item.observed_at.isoformat(),
                                retained.model_dump_json(),
                            ),
                        )
                    history.append(retained)
                    current, conflict = _current_projection(conn, history)
                    disposition = (
                        "conflict"
                        if conflict
                        else "cancelled"
                        if current and current.status == "cancelled"
                        else "superseded"
                        if previous
                        else "inserted"
                    )
                    reason = "same_tier_authority_disagreement" if conflict else "verified_revision"
                    if mode == "apply":
                        _project(conn, item.event_id, current, observed_now.isoformat())
            counts[disposition] += 1
            dispositions.append(
                IREventDisposition(
                    event_id=item.event_id,
                    revision_id=item.revision_id,
                    disposition=disposition,
                    reason_code=reason,
                )
            )
        # An omitted event is not cancellation or resolution. Keep unresolved
        # retained conflicts visible even when a later feed is empty.
        if roster:
            placeholders = ",".join("?" for _ in roster)
            for row in conn.execute(
                f"SELECT DISTINCT event_id FROM ir_event_revisions WHERE ticker IN ({placeholders}) AND event_date>=?",
                (*roster, cal_date.isoformat()),
            ):
                event_id = str(row[0])
                if event_id not in histories:
                    histories[event_id] = [
                        IREventObservation.model_validate_json(str(entry[0]))
                        for entry in conn.execute(
                            "SELECT observation_json FROM ir_event_revisions WHERE event_id=? ORDER BY revision",
                            (event_id,),
                        )
                    ]
            retained_conflicts = sum(
                _current_projection(conn, history)[1] for history in histories.values()
            )
            counts["conflict"] = max(counts["conflict"], retained_conflicts)
        successful = {attempt.ticker for attempt in attempts if attempt.status == "ok"}
        failures = any(attempt.status != "ok" for attempt in attempts) or (
            bool(tickers) and not set(roster).issubset(successful)
        )
        status: RunStatus = (
            "partial"
            if failures and successful
            else "error"
            if failures
            else "complete"
            if observations
            else "empty"
        )
        if counts["conflict"] or any(
            d.reason_code == "date_beyond_548d_ceiling" for d in dispositions
        ):
            status = "partial"
        freshness: Freshness = (
            "unavailable" if status == "error" else "stale" if status == "partial" else "fresh"
        )
        result = IREventRunResult(
            run_id=rid,
            tickers=roster,
            mode=mode,
            status=status,
            freshness=freshness,
            as_of=observed_now,
            calendar_date=cal_date,
            roster_sha256=roster_sha,
            policy_sha256=POLICY_SHA256,
            attempts=tuple(attempts),
            events=tuple(observations),
            dispositions=tuple(dispositions),
            inserted=counts["inserted"],
            replayed=counts["replayed"],
            superseded=counts["superseded"],
            cancelled=counts["cancelled"],
            rejected=counts["rejected"],
            conflicts=counts["conflict"],
        )
        if mode == "apply":
            conn.execute(
                "INSERT INTO ir_event_runs(attempt_id,run_id,as_of,status,receipt_json) VALUES(?,?,?,?,?)",
                (
                    result.attempt_id,
                    result.run_id,
                    result.as_of.isoformat(),
                    result.status,
                    result.model_dump_json(),
                ),
            )
            conn.execute("RELEASE SAVEPOINT ir_event_batch")
        return result
    except Exception:
        if mode == "apply":
            conn.execute("ROLLBACK TO SAVEPOINT ir_event_batch")
            conn.execute("RELEASE SAVEPOINT ir_event_batch")
        raise


def ir_calendar_coverage(
    conn: sqlite3.Connection, *, now: datetime
) -> tuple[Freshness, str, str | None]:
    """Bounded coverage read; a targeted run cannot refresh the whole roster."""
    roster = {
        str(row[0]).upper()
        for row in conn.execute(
            "SELECT DISTINCT ticker FROM tracked_companies WHERE archived_at IS NULL"
        )
    }
    if not roster:
        return "unavailable", "active_roster_unavailable", None
    checked: set[str] = set()
    evidence_clocks: list[datetime] = []
    for row in conn.execute(
        "SELECT receipt_json FROM ir_event_runs ORDER BY rowid DESC LIMIT 1000"
    ):
        result = IREventRunResult.model_validate_json(str(row[0]))
        if result.mode != "apply":
            continue
        for ticker in set(result.tickers) & roster - checked:
            checked.add(ticker)
            attempts = [item for item in result.attempts if item.ticker == ticker]
            if not attempts or any(
                item.status != "ok" or item.observed_at is None for item in attempts
            ):
                return "unavailable", "source_refresh_incomplete", result.as_of.isoformat()
            if result.conflicts or result.status not in ("complete", "empty"):
                return "unavailable", "event_admission_incomplete", result.as_of.isoformat()
            clocks = [item.observed_at for item in attempts if item.observed_at is not None]
            evidence_clocks.extend(clocks)
            if any(
                now - clock < timedelta(0) or now - clock > timedelta(hours=36) for clock in clocks
            ):
                return "stale", "source_capture_stale", min(clocks).isoformat()
        if checked == roster:
            return "fresh", "verified_captured_feeds_complete", min(evidence_clocks).isoformat()
    return "unavailable", "source_refresh_never_completed", None
