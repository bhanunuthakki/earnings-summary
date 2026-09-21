"""Discover scheduled IR events from verified, immutable publisher captures.

Acquisition remains owned by the approved IR capture pipeline. This adapter never
fetches a URL, follows a redirect, or treats an ordinary HTML page as an empty feed.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from urllib.request import url2pathname
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ir_pipeline.authority import IRAuthorityEvidence, PublisherSurfaceEvidence
from log_redact import redact
from pipeline.source_policy import canonical_https_url
from provenance.immutable_artifact import read_stable_artifact
from provenance.issuer_registry import IssuerRegistry, UnresolvedIssuerIdentityError
from signals.ir_events import (
    EventKind,
    EventStatus,
    IREventObservation,
    IRSourceAttempt,
    generate_event_id,
    generate_revision_id,
)

POLICY_SHA256 = hashlib.sha256(b"ir-events-captured-feed:v1:explicit-category:36h:548d").hexdigest()
PACIFIC = ZoneInfo("America/Los_Angeles")


class FeedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_kind: EventKind
    status: EventStatus = "scheduled"
    title: str = Field(min_length=1)
    event_date: date
    starts_at: AwareDatetime | None = None
    source_timezone: str | None = None
    source_event_id: str = Field(min_length=1)
    source_url: str
    source_locator: str


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tickers: tuple[str, ...]
    events: tuple[IREventObservation, ...]
    attempts: tuple[IRSourceAttempt, ...]


class _JsonEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["Event", "BusinessEvent"] = Field(alias="@type")
    identifier: str
    name: str
    start_date: str = Field(alias="startDate")
    url: str
    category: str
    event_status: Literal[
        "https://schema.org/EventScheduled",
        "https://schema.org/EventRescheduled",
        "https://schema.org/EventCancelled",
    ] = Field(default="https://schema.org/EventScheduled", alias="eventStatus")


class _JsonFeed(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["ItemList"] = Field(alias="@type")
    count: int = Field(alias="numberOfItems", ge=0)
    items: tuple[_JsonEvent, ...] = Field(alias="itemListElement")


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self.current = []

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


def _kind(category: str) -> EventKind | None:
    # A structured publisher category, never a keyword on an unrelated page.
    kinds: dict[str, EventKind] = {
        "investor day": "investor_day",
        "analyst day": "analyst_day",
        "financial analyst day": "analyst_day",
        "capital markets day": "capital_markets_day",
        "investor strategy day": "strategy_day",
    }
    normalized = category.strip().casefold()
    if normalized in kinds:
        return kinds[normalized]
    if normalized in {
        "earnings",
        "earnings call",
        "earnings webcast",
        "conference appearance",
        "shareholder meeting",
        "product event",
        "regulatory milestone",
        "clinical milestone",
        "podcast",
    }:
        return None
    raise ValueError("unmapped publisher event category")


def _clock(value: str, zone: str | None = None) -> tuple[date, datetime | None, str | None]:
    if re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").date(), None, None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value), None, None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        if not zone:
            raise ValueError("timezone-less event timestamp")
        tz = ZoneInfo(zone)
        first = parsed.replace(tzinfo=tz, fold=0)
        second = parsed.replace(tzinfo=tz, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("ambiguous or nonexistent local event timestamp")
        parsed = first
    instant = parsed.astimezone(UTC)
    return instant.astimezone(PACIFIC).date(), instant, zone or str(parsed.tzinfo)


def _ical(raw: str) -> tuple[FeedEvent, ...]:
    lines = re.sub(r"\r?\n[ \t]", "", raw).splitlines()
    lines = [line.strip() for line in lines if line.strip()]
    if not lines or lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        raise ValueError("calendar is incomplete")
    events: list[FeedEvent] = []
    properties: dict[str, tuple[str, dict[str, str]]] | None = None
    seen: set[str] = set()
    for line in lines[1:-1]:
        if line == "BEGIN:VEVENT":
            if properties is not None:
                raise ValueError("nested event")
            properties = {}
        elif line == "END:VEVENT":
            if properties is None:
                raise ValueError("unmatched event end")
            category = properties.get("CATEGORIES", ("", {}))[0]
            kind = _kind(category)
            if kind is not None:
                if any(key in properties for key in ("RRULE", "RDATE", "RECURRENCE-ID")):
                    raise ValueError("recurring events require an explicit expansion adapter")
                try:
                    uid = properties["UID"][0]
                    if not uid or uid in seen:
                        raise ValueError("missing or duplicate event identity")
                    seen.add(uid)
                    value, params = properties["DTSTART"]
                    event_date, instant, zone = _clock(value, params.get("TZID"))
                    status_raw = properties.get("STATUS", ("CONFIRMED", {}))[0]
                    if status_raw not in ("CONFIRMED", "CANCELLED"):
                        raise ValueError("unconfirmed event status")
                    events.append(
                        FeedEvent(
                            event_kind=kind,
                            status="cancelled" if status_raw == "CANCELLED" else "scheduled",
                            title=properties["SUMMARY"][0],
                            event_date=event_date,
                            starts_at=instant,
                            source_timezone=zone,
                            source_event_id=uid,
                            source_url=properties["URL"][0],
                            source_locator=f"VEVENT/UID:{uid}",
                        )
                    )
                except KeyError as exc:
                    raise ValueError("event missing required publisher field") from exc
            elif not category:
                # A nonempty, unclassified publisher event is not evidence of zero eligible events.
                raise ValueError("event category unavailable")
            properties = None
        elif properties is not None:
            if ":" not in line or line.startswith(("BEGIN:", "END:")):
                raise ValueError("unsupported nested or malformed event")
            key, value = line.split(":", 1)
            name, *parameters = key.split(";")
            if name in properties:
                raise ValueError("duplicate event property")
            params: dict[str, str] = {}
            for parameter in parameters:
                if "=" not in parameter:
                    raise ValueError("malformed event parameter")
                pkey, pvalue = parameter.split("=", 1)
                params[pkey] = pvalue.strip('"')
            properties[name] = (value.replace(r"\,", ",").replace(r"\n", "\n"), params)
        elif line.startswith(("BEGIN:", "END:")):
            raise ValueError("unsupported calendar component")
    if properties is not None:
        raise ValueError("unterminated event")
    return tuple(events)


def parse_event_feed(raw: bytes, media_type: str) -> tuple[FeedEvent, ...]:
    if len(raw) > 5_000_000:
        raise ValueError("event feed exceeds bounded parser limit")
    text = raw.decode("utf-8-sig")
    if media_type.split(";")[0] == "text/calendar":
        return _ical(text)
    if media_type.split(";")[0] not in ("text/html", "application/ld+json", "application/json"):
        raise ValueError("unsupported event feed media type")
    if media_type.split(";")[0] == "text/html":
        parser = _Scripts()
        parser.feed(text)
        if parser.current is not None or len(parser.scripts) != 1:
            raise ValueError("one complete structured event feed required")
        text = parser.scripts[0]
    feed = _JsonFeed.model_validate_json(text)
    if feed.count != len(feed.items):
        raise ValueError("publisher event count does not reconcile")
    events: list[FeedEvent] = []
    seen: set[str] = set()
    for index, item in enumerate(feed.items):
        if item.identifier in seen:
            raise ValueError("duplicate event identity")
        seen.add(item.identifier)
        kind = _kind(item.category)
        if kind is None:
            continue
        event_date, instant, zone = _clock(item.start_date)
        statuses: dict[str, EventStatus] = {
            "https://schema.org/EventScheduled": "scheduled",
            "https://schema.org/EventRescheduled": "rescheduled",
            "https://schema.org/EventCancelled": "cancelled",
        }
        status = statuses[item.event_status]
        events.append(
            FeedEvent(
                event_kind=kind,
                status=status,
                title=item.name,
                event_date=event_date,
                starts_at=instant,
                source_timezone=zone,
                source_event_id=item.identifier,
                source_url=item.url,
                source_locator=f"/itemListElement/{index}",
            )
        )
    return tuple(events)


def discover_ir_events(
    conn: sqlite3.Connection, tickers: tuple[str, ...], *, now: datetime
) -> DiscoveryResult:
    if now.tzinfo is None:
        raise ValueError("discovery clock must be timezone aware")
    registry = IssuerRegistry(conn)
    observations: list[IREventObservation] = []
    attempts: list[IRSourceAttempt] = []
    for ticker in sorted(set(tickers)):
        try:
            issuer = registry.canonicalize_recorded_issuer(
                f"legacy-ticker:{ticker}", knowledge_at=now
            )
            if issuer.material_dissent:
                raise ValueError("canonical issuer binding has material dissent")
            surfaces = registry.source_authority(issuer.issuer_id, "ir_events", knowledge_at=now)
        except (UnresolvedIssuerIdentityError, ValueError):
            attempts.append(
                IRSourceAttempt(
                    ticker=ticker,
                    source_tier="publisher_event_authority",
                    source_url="",
                    status="unsupported",
                    reason_code="canonical_issuer_unavailable",
                )
            )
            continue
        if not surfaces:
            attempts.append(
                IRSourceAttempt(
                    ticker=ticker,
                    source_tier="publisher_event_authority",
                    source_url="",
                    status="unsupported",
                    reason_code="verified_issuer_event_surface_unavailable",
                )
            )
            continue
        for surface in surfaces:
            try:
                if surface.authority_level != "publisher":
                    raise ValueError("event authority is not issuer or regulator")
                authority_url = canonical_https_url(surface.source_url)
                if authority_url is None or redact(surface.source_url) != surface.source_url:
                    raise ValueError("invalid authority URL")
                row = conn.execute(
                    "SELECT o.source_url,o.blob_sha256,o.observed_at,o.source_published_at,b.storage_uri,b.media_type,b.byte_size "
                    "FROM evidence_source_observations o JOIN evidence_content_blobs b ON b.sha256=o.blob_sha256 "
                    "WHERE o.observation_id=?",
                    (surface.source_observation_id,),
                ).fetchone()
                if row is None or str(row[0]) != surface.source_url:
                    raise ValueError("authority observation URL mismatch")
                observed = datetime.fromisoformat(str(row[2]))
                if observed.tzinfo is None:
                    observed = observed.replace(
                        tzinfo=UTC
                    )  # ledger's canonical naive UTC serialization
                age = now - observed
                if age < timedelta(0) or age > timedelta(hours=36):
                    attempts.append(
                        IRSourceAttempt(
                            ticker=ticker,
                            source_tier="publisher_event_authority",
                            source_url=redact(surface.source_url),
                            status="stale",
                            reason_code="capture_outside_36h_window",
                            source_observation_id=surface.source_observation_id,
                        )
                    )
                    continue
                uri = urlsplit(str(row[4]))
                if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
                    raise ValueError("capture is not a local immutable artifact")
                if int(row[6]) > 5_000_000:
                    raise ValueError("event feed exceeds parser limit")
                capture_path = Path(url2pathname(uri.path))
                if capture_path.stat().st_size > 5_000_000:
                    raise ValueError("event capture exceeds parser limit")
                snapshot, raw = read_stable_artifact(capture_path)
                if snapshot.file_sha256 != str(row[1]) or snapshot.size_bytes != int(row[6]):
                    raise ValueError("capture hash or size mismatch")
                parsed = parse_event_feed(raw, str(row[5]))
                source_events: list[IREventObservation] = []
                for event in parsed:
                    event_url = canonical_https_url(event.source_url)
                    if (
                        event_url is None
                        or event_url[0] != authority_url[0]
                        or redact(event.source_url) != event.source_url
                    ):
                        raise ValueError("event URL is outside verified publisher authority")
                    event_id = generate_event_id(
                        issuer.issuer_id, event.event_kind, event.source_event_id
                    )
                    payload = event.model_dump(mode="json")
                    source_events.append(
                        IREventObservation(
                            event_id=event_id,
                            revision_id=generate_revision_id(
                                event_id, payload, surface.source_observation_id
                            ),
                            issuer_id=issuer.issuer_id,
                            ticker=ticker,
                            event_kind=event.event_kind,
                            status=event.status,
                            title=event.title,
                            event_date=event.event_date,
                            starts_at=event.starts_at,
                            source_timezone=event.source_timezone,
                            source_event_id=event.source_event_id,
                            source_tier="publisher_event_authority",
                            source_url=event.source_url,
                            source_locator=event.source_locator,
                            source_observation_id=surface.source_observation_id,
                            raw_sha256=str(row[1]),
                            authority_surface_revision_id=surface.surface_revision_id,
                            observed_at=observed,
                            source_published_at=_ledger_clock(str(row[3])) if row[3] else None,
                        )
                    )
                observations.extend(source_events)
                attempts.append(
                    IRSourceAttempt(
                        ticker=ticker,
                        source_tier="publisher_event_authority",
                        source_url=redact(surface.source_url),
                        status="ok",
                        reason_code="structured_feed_exhausted",
                        authority_evidence=IRAuthorityEvidence(
                            authority_basis="publisher_document_feed",
                            asserted_at=observed,
                            surfaces=(
                                PublisherSurfaceEvidence(
                                    surface_key=surface.surface_key,
                                    surface_kind="event_feed",
                                    source_url=redact(surface.source_url),
                                    source_observation_id=surface.source_observation_id,
                                    raw_sha256=str(row[1]),
                                    traversal_kind="single_response",
                                    outcome="exhausted",
                                    terminal_condition="single_response_declared_complete",
                                    observed_document_urls=tuple(
                                        sorted({event.source_url for event in parsed})
                                    ),
                                ),
                            ),
                        ),
                        record_count=len(source_events),
                        source_observation_id=surface.source_observation_id,
                        observed_at=observed,
                    )
                )
            except (ValueError, OSError):
                attempts.append(
                    IRSourceAttempt(
                        ticker=ticker,
                        source_tier="publisher_event_authority",
                        source_url=redact(surface.source_url),
                        status="contract_error",
                        reason_code="source_contract_failed",
                        source_observation_id=surface.source_observation_id,
                    )
                )
    return DiscoveryResult(
        tickers=tuple(sorted(set(tickers))), events=tuple(observations), attempts=tuple(attempts)
    )


def _ledger_clock(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
