"""Monotonic replay stream for verified sealed source-fact publications.

The stream is an outbox owned by the same SQLite transaction as publication.
Transport may replay events, while deterministic identities and compare-on-
conflict make consumer effects exactly-once.  Knowledge and recorded clocks
remain bitemporal facts; ``publication_sequence`` is only consumer order.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.source_fact_publication import (
    canonical_json,
    canonical_time,
    digest_text,
    verify_source_fact_publication,
)

SOURCE_FACT_STREAM_ID = "source-fact-publication-stream-v1"
PUBLICATION_EVENT_VERSION = "source_fact_publication_event.v1"
RESOLUTION_WATERMARK_VERSION = "canonical_resolution_snapshot_watermark.v1"
INITIAL_EVENT_SHA256 = "0" * 64
MAX_PUBLICATION_PAGE_SIZE = 1_000

SequenceBasis = Literal["legacy_backfill", "transactional_publish"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicationCursor(_FrozenModel):
    stream_id: str = SOURCE_FACT_STREAM_ID
    publication_sequence: int = Field(ge=0)
    event_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def _initial_cursor_shape(self) -> Self:
        if self.stream_id != SOURCE_FACT_STREAM_ID:
            raise ValueError("publication cursor belongs to an unknown stream")
        if (self.publication_sequence == 0) != (self.event_sha256 == INITIAL_EVENT_SHA256):
            raise ValueError("initial publication cursor shape is invalid")
        return self

    @classmethod
    def initial(cls) -> PublicationCursor:
        return cls(
            publication_sequence=0,
            event_sha256=INITIAL_EVENT_SHA256,
        )


class PublicationEvent(_FrozenModel):
    publication_sequence: int = Field(gt=0)
    stream_id: str = SOURCE_FACT_STREAM_ID
    publication_id: str = Field(min_length=1, max_length=128)
    publication_seal_id: str = Field(min_length=1, max_length=128)
    publication_payload_sha256: str = Field(min_length=64, max_length=64)
    member_set_sha256: str = Field(min_length=64, max_length=64)
    sequence_basis: SequenceBasis
    sealed_at: datetime
    assigned_at: datetime
    event_version: str = PUBLICATION_EVENT_VERSION
    canonical_event_json: str
    event_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def _exact_event(self) -> Self:
        if self.stream_id != SOURCE_FACT_STREAM_ID:
            raise ValueError("publication event belongs to an unknown stream")
        if self.event_version != PUBLICATION_EVENT_VERSION:
            raise ValueError("publication event version is unsupported")
        if _utc(self.assigned_at) < _utc(self.sealed_at):
            raise ValueError("publication event cannot precede its seal")
        payload = publication_event_payload(
            stream_id=self.stream_id,
            publication_sequence=self.publication_sequence,
            publication_id=self.publication_id,
            publication_seal_id=self.publication_seal_id,
            publication_payload_sha256=self.publication_payload_sha256,
            member_set_sha256=self.member_set_sha256,
            sequence_basis=self.sequence_basis,
            sealed_at=self.sealed_at,
            assigned_at=self.assigned_at,
        )
        if self.canonical_event_json != payload:
            raise ValueError("publication event JSON is not canonical")
        if self.event_sha256 != digest_text(payload):
            raise ValueError("publication event hash is not exact")
        return self

    @property
    def cursor(self) -> PublicationCursor:
        return PublicationCursor(
            publication_sequence=self.publication_sequence,
            event_sha256=self.event_sha256,
        )


class PublicationPage(_FrozenModel):
    after: PublicationCursor
    through_sequence: int = Field(ge=0)
    events: tuple[PublicationEvent, ...]
    next_cursor: PublicationCursor
    has_more: bool

    @model_validator(mode="after")
    def _ordered_page(self) -> Self:
        sequences = tuple(event.publication_sequence for event in self.events)
        if sequences != tuple(sorted(sequences)) or len(sequences) != len(set(sequences)):
            raise ValueError("publication page must be strictly ordered")
        if sequences and sequences[0] <= self.after.publication_sequence:
            raise ValueError("publication page does not follow its cursor")
        if sequences and sequences[-1] > self.through_sequence:
            raise ValueError("publication page exceeds its high-watermark")
        expected = self.events[-1].cursor if self.events else self.after
        if self.next_cursor != expected:
            raise ValueError("publication page next cursor is not exact")
        return self


class StreamBackfillReceipt(_FrozenModel):
    publication_count: int = Field(ge=0)
    first_cursor: PublicationCursor
    last_cursor: PublicationCursor
    ordered_event_set_sha256: str = Field(min_length=64, max_length=64)
    created: bool


class ResolutionSnapshotWatermark(_FrozenModel):
    resolution_snapshot_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    stream_id: str = SOURCE_FACT_STREAM_ID
    publication_high_watermark: int = Field(ge=0)
    high_watermark_event_sha256: str = Field(min_length=64, max_length=64)
    cutoff_at: datetime
    recorded_at: datetime
    watermark_version: str = RESOLUTION_WATERMARK_VERSION
    canonical_watermark_json: str
    watermark_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def _exact_watermark(self) -> Self:
        if self.stream_id != SOURCE_FACT_STREAM_ID:
            raise ValueError("resolution watermark belongs to an unknown stream")
        if self.watermark_version != RESOLUTION_WATERMARK_VERSION:
            raise ValueError("resolution watermark version is unsupported")
        if (self.publication_high_watermark == 0) != (
            self.high_watermark_event_sha256 == INITIAL_EVENT_SHA256
        ):
            raise ValueError("resolution watermark cursor shape is invalid")
        if _utc(self.recorded_at) < _utc(self.cutoff_at):
            raise ValueError("resolution watermark cannot precede its cutoff")
        payload = resolution_snapshot_watermark_payload(
            resolution_snapshot_id=self.resolution_snapshot_id,
            stream_id=self.stream_id,
            publication_high_watermark=self.publication_high_watermark,
            high_watermark_event_sha256=self.high_watermark_event_sha256,
            cutoff_at=self.cutoff_at,
            recorded_at=self.recorded_at,
        )
        if self.canonical_watermark_json != payload:
            raise ValueError("resolution watermark JSON is not canonical")
        if self.watermark_sha256 != digest_text(payload):
            raise ValueError("resolution watermark hash is not exact")
        return self

    @property
    def cursor(self) -> PublicationCursor:
        return PublicationCursor(
            publication_sequence=self.publication_high_watermark,
            event_sha256=self.high_watermark_event_sha256,
        )


class PublicationStreamVerificationError(RuntimeError):
    """The stream, event, cursor, or watermark cannot be admitted."""

    def __init__(
        self,
        reason_code: str,
        *,
        publication_sequence: int | None = None,
        publication_id: str | None = None,
        resolution_snapshot_id: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.publication_sequence = publication_sequence
        self.publication_id = publication_id
        self.resolution_snapshot_id = resolution_snapshot_id
        super().__init__(reason_code)


class PublicationStreamUnavailableError(RuntimeError):
    """The repository schema predates the required publication stream."""


def publication_event_payload(
    *,
    stream_id: object,
    publication_sequence: object,
    publication_id: object,
    publication_seal_id: object,
    publication_payload_sha256: object,
    member_set_sha256: object,
    sequence_basis: object,
    sealed_at: object,
    assigned_at: object,
) -> str:
    return canonical_json(
        {
            "assigned_at": canonical_time(assigned_at),
            "event_version": PUBLICATION_EVENT_VERSION,
            "member_set_sha256": str(member_set_sha256),
            "publication_id": str(publication_id),
            "publication_payload_sha256": str(publication_payload_sha256),
            "publication_seal_id": str(publication_seal_id),
            "publication_sequence": int(str(publication_sequence)),
            "sealed_at": canonical_time(sealed_at),
            "sequence_basis": str(sequence_basis),
            "stream_id": str(stream_id),
        }
    )


def resolution_snapshot_watermark_payload(
    *,
    resolution_snapshot_id: object,
    stream_id: object,
    publication_high_watermark: object,
    high_watermark_event_sha256: object,
    cutoff_at: object,
    recorded_at: object,
) -> str:
    return canonical_json(
        {
            "cutoff_at": canonical_time(cutoff_at),
            "high_watermark_event_sha256": str(high_watermark_event_sha256),
            "publication_high_watermark": int(str(publication_high_watermark)),
            "recorded_at": canonical_time(recorded_at),
            "resolution_snapshot_id": str(resolution_snapshot_id),
            "stream_id": str(stream_id),
            "watermark_version": RESOLUTION_WATERMARK_VERSION,
        }
    )


def register_source_fact_stream_functions(conn: sqlite3.Connection) -> None:
    """Register deterministic functions required by the 0246 SQL gates."""

    conn.create_function(
        "fact_sha256",
        1,
        _sql_fact_sha256,
        deterministic=True,
    )
    conn.create_function(
        "source_fact_publication_event_v1",
        9,
        _sql_publication_event_v1,
        deterministic=True,
    )
    conn.create_function(
        "canonical_resolution_snapshot_watermark_v1",
        6,
        _sql_resolution_snapshot_watermark_v1,
        deterministic=True,
    )


def require_source_fact_stream_schema(conn: sqlite3.Connection) -> None:
    """Fail closed before a publisher can create an unstreamed publication."""

    required = {
        "source_fact_publication_stream",
        "source_fact_publication_stream_clock",
    }
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            tuple(sorted(required)),
        )
    }
    if present != required:
        missing = ",".join(sorted(required - present))
        raise PublicationStreamUnavailableError(
            f"SourceFactRepository requires migration 0246; missing tables: {missing}"
        )


def append_verified_publication_event(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
    sequence_basis: SequenceBasis,
    assigned_at: datetime,
) -> PublicationEvent:
    """Append or exactly replay one verified publication event."""

    register_source_fact_stream_functions(conn)
    with _writer_scope(conn):
        existing = _event_row_by_publication(conn, publication_id)
        if existing is not None:
            return _verify_event_row(conn, existing)

        header = _publication_header(conn, publication_id)
        if header is None:
            raise PublicationStreamVerificationError(
                "publication_missing",
                publication_id=publication_id,
            )
        sealed_at = _datetime(header["sealed_at"])
        verified = verify_source_fact_publication(
            conn,
            publication_id=publication_id,
            cutoff=sealed_at,
        )
        bounded_assigned_at = max(_utc(assigned_at), _utc(sealed_at))

        with _savepoint(conn, "append_source_fact_publication_event"):
            replay = _event_row_by_publication(conn, publication_id)
            if replay is not None:
                return _verify_event_row(conn, replay)
            publication_sequence = _allocate_sequence(conn)
            payload = publication_event_payload(
                stream_id=SOURCE_FACT_STREAM_ID,
                publication_sequence=publication_sequence,
                publication_id=publication_id,
                publication_seal_id=verified.publication_seal_id,
                publication_payload_sha256=(verified.publication_payload_sha256),
                member_set_sha256=verified.member_set_sha256,
                sequence_basis=sequence_basis,
                sealed_at=sealed_at,
                assigned_at=bounded_assigned_at,
            )
            conn.execute(
                "INSERT INTO source_fact_publication_stream "
                "(publication_sequence,stream_id,publication_id,"
                "publication_seal_id,publication_payload_sha256,"
                "member_set_sha256,sequence_basis,sealed_at,assigned_at,"
                "event_version,canonical_event_json,event_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    publication_sequence,
                    SOURCE_FACT_STREAM_ID,
                    publication_id,
                    verified.publication_seal_id,
                    verified.publication_payload_sha256,
                    verified.member_set_sha256,
                    sequence_basis,
                    header["sealed_at"],
                    canonical_time(bounded_assigned_at),
                    PUBLICATION_EVENT_VERSION,
                    payload,
                    digest_text(payload),
                ),
            )
            return verify_publication_event(
                conn,
                publication_sequence=publication_sequence,
            )


def verify_publication_event(
    conn: sqlite3.Connection,
    *,
    publication_sequence: int,
) -> PublicationEvent:
    register_source_fact_stream_functions(conn)
    row = _event_row_by_sequence(conn, publication_sequence)
    if row is None:
        raise PublicationStreamVerificationError(
            "publication_event_missing",
            publication_sequence=publication_sequence,
        )
    return _verify_event_row(conn, row)


def publication_event_for_publication(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
) -> PublicationEvent:
    """Load one exact event without allocating a sequence on replay."""

    register_source_fact_stream_functions(conn)
    row = _event_row_by_publication(conn, publication_id)
    if row is None:
        raise PublicationStreamVerificationError(
            "publication_event_missing_requires_explicit_backfill",
            publication_id=publication_id,
        )
    return _verify_event_row(conn, row)


def read_publication_page(
    conn: sqlite3.Connection,
    *,
    after: PublicationCursor,
    through_sequence: int,
    limit: int,
) -> PublicationPage:
    """Read and verify a bounded, keyset-paginated publication page."""

    register_source_fact_stream_functions(conn)
    if limit <= 0 or limit > MAX_PUBLICATION_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PUBLICATION_PAGE_SIZE}")
    if through_sequence < after.publication_sequence:
        raise ValueError("through_sequence cannot precede the cursor")
    _verify_cursor(conn, after)
    rows = _fetchall(
        conn,
        "SELECT publication_sequence,stream_id,publication_id,"
        "publication_seal_id,publication_payload_sha256,"
        "member_set_sha256,sequence_basis,sealed_at,assigned_at,"
        "event_version,canonical_event_json,event_sha256 "
        "FROM source_fact_publication_stream "
        "WHERE stream_id=? AND publication_sequence>? "
        "AND publication_sequence<=? ORDER BY publication_sequence LIMIT ?",
        (
            SOURCE_FACT_STREAM_ID,
            after.publication_sequence,
            through_sequence,
            limit,
        ),
    )
    events = tuple(_verify_event_row(conn, row) for row in rows)
    next_cursor = events[-1].cursor if events else after
    has_more = (
        conn.execute(
            "SELECT 1 FROM source_fact_publication_stream "
            "WHERE stream_id=? AND publication_sequence>? "
            "AND publication_sequence<=? LIMIT 1",
            (
                SOURCE_FACT_STREAM_ID,
                next_cursor.publication_sequence,
                through_sequence,
            ),
        ).fetchone()
        is not None
    )
    return PublicationPage(
        after=after,
        through_sequence=through_sequence,
        events=events,
        next_cursor=next_cursor,
        has_more=has_more,
    )


def backfill_legacy_publication_stream(
    conn: sqlite3.Connection,
) -> StreamBackfillReceipt:
    """Explicitly backfill every sealed 0241 publication in stable order."""

    register_source_fact_stream_functions(conn)
    publications = _fetchall(
        conn,
        "SELECT publication.publication_id,seal.sealed_at "
        "FROM source_fact_publications AS publication "
        "JOIN source_fact_publication_seals AS seal "
        "ON seal.publication_id=publication.publication_id "
        "ORDER BY publication.recorded_at,publication.publication_id",
        (),
    )
    existing = _fetchall(
        conn,
        "SELECT publication_sequence,stream_id,publication_id,"
        "publication_seal_id,publication_payload_sha256,"
        "member_set_sha256,sequence_basis,sealed_at,assigned_at,"
        "event_version,canonical_event_json,event_sha256 "
        "FROM source_fact_publication_stream "
        "ORDER BY publication_sequence",
        (),
    )
    if existing:
        events = tuple(_verify_event_row(conn, row) for row in existing)
        if (
            tuple(event.publication_id for event in events)
            != tuple(str(row["publication_id"]) for row in publications)
            or any(event.sequence_basis != "legacy_backfill" for event in events)
            or tuple(event.publication_sequence for event in events)
            != tuple(range(1, len(events) + 1))
        ):
            raise PublicationStreamVerificationError(
                "legacy_backfill_conflicts_with_existing_stream"
            )
        return _backfill_receipt(events, created=False)

    clock = conn.execute(
        "SELECT next_sequence FROM source_fact_publication_stream_clock WHERE singleton_key=1"
    ).fetchone()
    if clock is None or int(clock[0]) != 1:
        raise PublicationStreamVerificationError("legacy_backfill_requires_empty_stream_clock")
    events_list: list[PublicationEvent] = []
    with _savepoint(conn, "backfill_source_fact_publication_stream"):
        for publication in publications:
            sealed_at = _datetime(publication["sealed_at"])
            events_list.append(
                append_verified_publication_event(
                    conn,
                    publication_id=str(publication["publication_id"]),
                    sequence_basis="legacy_backfill",
                    assigned_at=sealed_at,
                )
            )
    return _backfill_receipt(
        tuple(events_list),
        created=bool(events_list),
    )


def publication_cursor_through(
    conn: sqlite3.Connection,
    *,
    cutoff_at: datetime,
) -> PublicationCursor:
    """Return the verified maximal publication cursor through a cutoff."""

    register_source_fact_stream_functions(conn)
    row = conn.execute(
        "SELECT publication_sequence FROM source_fact_publication_stream "
        "WHERE stream_id=? AND julianday(sealed_at)<=julianday(?) "
        "ORDER BY publication_sequence DESC LIMIT 1",
        (SOURCE_FACT_STREAM_ID, canonical_time(_utc(cutoff_at))),
    ).fetchone()
    if row is None:
        return PublicationCursor.initial()
    return verify_publication_event(
        conn,
        publication_sequence=int(row[0]),
    ).cursor


def bind_resolution_snapshot_watermark(
    conn: sqlite3.Connection,
    *,
    resolution_snapshot_id: str,
    cutoff_at: datetime,
    recorded_at: datetime,
) -> ResolutionSnapshotWatermark:
    """Bind one sealed 0244 snapshot to its maximal stream cursor."""

    register_source_fact_stream_functions(conn)
    cutoff = _utc(cutoff_at)
    recorded = _utc(recorded_at)
    snapshot = _fetchone(
        conn,
        "SELECT cutoff_at,recorded_at "
        "FROM canonical_fact_resolution_snapshot_seals "
        "WHERE resolution_snapshot_id=?",
        (resolution_snapshot_id,),
    )
    if snapshot is None:
        raise PublicationStreamVerificationError(
            "resolution_snapshot_missing",
            resolution_snapshot_id=resolution_snapshot_id,
        )
    if _datetime(snapshot["cutoff_at"]) != cutoff:
        raise PublicationStreamVerificationError(
            "resolution_snapshot_cutoff_mismatch",
            resolution_snapshot_id=resolution_snapshot_id,
        )
    cursor = publication_cursor_through(conn, cutoff_at=cutoff)
    bounded_recorded = max(
        recorded,
        cutoff,
        _datetime(snapshot["recorded_at"]),
    )
    idempotency_key = f"resolution-snapshot-watermark:{resolution_snapshot_id}"
    payload = resolution_snapshot_watermark_payload(
        resolution_snapshot_id=resolution_snapshot_id,
        stream_id=SOURCE_FACT_STREAM_ID,
        publication_high_watermark=cursor.publication_sequence,
        high_watermark_event_sha256=cursor.event_sha256,
        cutoff_at=snapshot["cutoff_at"],
        recorded_at=bounded_recorded,
    )
    values: tuple[object, ...] = (
        resolution_snapshot_id,
        idempotency_key,
        SOURCE_FACT_STREAM_ID,
        cursor.publication_sequence,
        cursor.event_sha256,
        snapshot["cutoff_at"],
        canonical_time(bounded_recorded),
        RESOLUTION_WATERMARK_VERSION,
        payload,
        digest_text(payload),
    )
    columns = (
        "resolution_snapshot_id",
        "idempotency_key",
        "stream_id",
        "publication_high_watermark",
        "high_watermark_event_sha256",
        "cutoff_at",
        "recorded_at",
        "watermark_version",
        "canonical_watermark_json",
        "watermark_sha256",
    )
    with _savepoint(conn, "bind_resolution_snapshot_watermark"):
        existing = _fetchone(
            conn,
            "SELECT " + ",".join(columns) + " "
            "FROM canonical_fact_resolution_snapshot_watermarks "
            "WHERE resolution_snapshot_id=? OR idempotency_key=?",
            (resolution_snapshot_id, idempotency_key),
        )
        if existing is None:
            conn.execute(
                "INSERT INTO canonical_fact_resolution_snapshot_watermarks "
                f"({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                values,
            )
        elif not _values_equal(
            tuple(existing[column] for column in columns),
            values,
        ):
            raise PublicationStreamVerificationError(
                "resolution_snapshot_watermark_conflict",
                resolution_snapshot_id=resolution_snapshot_id,
            )
        return verify_resolution_snapshot_watermark(
            conn,
            resolution_snapshot_id=resolution_snapshot_id,
            cutoff_at=cutoff,
        )


def verify_resolution_snapshot_watermark(
    conn: sqlite3.Connection,
    *,
    resolution_snapshot_id: str,
    cutoff_at: datetime,
) -> ResolutionSnapshotWatermark:
    register_source_fact_stream_functions(conn)
    row = _fetchone(
        conn,
        "SELECT resolution_snapshot_id,idempotency_key,stream_id,"
        "publication_high_watermark,high_watermark_event_sha256,"
        "cutoff_at,recorded_at,watermark_version,"
        "canonical_watermark_json,watermark_sha256 "
        "FROM canonical_fact_resolution_snapshot_watermarks "
        "WHERE resolution_snapshot_id=?",
        (resolution_snapshot_id,),
    )
    if row is None:
        raise PublicationStreamVerificationError(
            "resolution_snapshot_watermark_missing",
            resolution_snapshot_id=resolution_snapshot_id,
        )
    try:
        watermark = ResolutionSnapshotWatermark.model_validate(row)
    except ValueError as exc:
        raise PublicationStreamVerificationError(
            "resolution_snapshot_watermark_tampered",
            resolution_snapshot_id=resolution_snapshot_id,
        ) from exc
    if _utc(watermark.cutoff_at) != _utc(cutoff_at):
        raise PublicationStreamVerificationError(
            "resolution_snapshot_cutoff_mismatch",
            resolution_snapshot_id=resolution_snapshot_id,
        )
    expected_cursor = publication_cursor_through(
        conn,
        cutoff_at=watermark.cutoff_at,
    )
    if expected_cursor != watermark.cursor:
        raise PublicationStreamVerificationError(
            "resolution_snapshot_watermark_event_mismatch",
            resolution_snapshot_id=resolution_snapshot_id,
        )
    snapshot = _fetchone(
        conn,
        "SELECT cutoff_at,recorded_at "
        "FROM canonical_fact_resolution_snapshot_seals "
        "WHERE resolution_snapshot_id=?",
        (resolution_snapshot_id,),
    )
    if (
        snapshot is None
        or _datetime(snapshot["cutoff_at"]) != _utc(watermark.cutoff_at)
        or _datetime(snapshot["recorded_at"]) > _utc(watermark.recorded_at)
    ):
        raise PublicationStreamVerificationError(
            "resolution_snapshot_watermark_snapshot_mismatch",
            resolution_snapshot_id=resolution_snapshot_id,
        )
    later = conn.execute(
        "SELECT 1 FROM source_fact_publication_stream "
        "WHERE stream_id=? AND publication_sequence>? "
        "AND julianday(sealed_at)<=julianday(?) "
        "LIMIT 1",
        (
            watermark.stream_id,
            watermark.publication_high_watermark,
            snapshot["cutoff_at"],
        ),
    ).fetchone()
    if later is not None:
        raise PublicationStreamVerificationError(
            "resolution_snapshot_watermark_incomplete",
            resolution_snapshot_id=resolution_snapshot_id,
        )
    return watermark


def _verify_cursor(
    conn: sqlite3.Connection,
    cursor: PublicationCursor,
) -> None:
    if cursor.publication_sequence == 0:
        return
    event = verify_publication_event(
        conn,
        publication_sequence=cursor.publication_sequence,
    )
    if event.cursor != cursor:
        raise PublicationStreamVerificationError(
            "publication_cursor_hash_mismatch",
            publication_sequence=cursor.publication_sequence,
        )


def _verify_event_row(
    conn: sqlite3.Connection,
    row: dict[str, object],
) -> PublicationEvent:
    try:
        event = PublicationEvent.model_validate(row)
    except ValueError as exc:
        raise PublicationStreamVerificationError(
            "publication_event_tampered",
            publication_sequence=_optional_int(row.get("publication_sequence")),
            publication_id=_optional_text(row.get("publication_id")),
        ) from exc
    verified = verify_source_fact_publication(
        conn,
        publication_id=event.publication_id,
        cutoff=event.sealed_at,
    )
    if (
        verified.publication_seal_id != event.publication_seal_id
        or verified.publication_payload_sha256 != event.publication_payload_sha256
        or verified.member_set_sha256 != event.member_set_sha256
        or _utc(verified.sealed_at) != _utc(event.sealed_at)
    ):
        raise PublicationStreamVerificationError(
            "publication_event_graph_mismatch",
            publication_sequence=event.publication_sequence,
            publication_id=event.publication_id,
        )
    clock = conn.execute(
        "SELECT next_sequence FROM source_fact_publication_stream_clock WHERE singleton_key=1"
    ).fetchone()
    if clock is None or event.publication_sequence >= int(clock[0]):
        raise PublicationStreamVerificationError(
            "publication_stream_clock_tampered",
            publication_sequence=event.publication_sequence,
            publication_id=event.publication_id,
        )
    return event


def _backfill_receipt(
    events: tuple[PublicationEvent, ...],
    *,
    created: bool,
) -> StreamBackfillReceipt:
    digest_payload = [
        {
            "event_sha256": event.event_sha256,
            "publication_id": event.publication_id,
            "publication_sequence": event.publication_sequence,
        }
        for event in events
    ]
    initial = PublicationCursor.initial()
    return StreamBackfillReceipt(
        publication_count=len(events),
        first_cursor=events[0].cursor if events else initial,
        last_cursor=events[-1].cursor if events else initial,
        ordered_event_set_sha256=digest_text(canonical_json(digest_payload)),
        created=created,
    )


def _allocate_sequence(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "UPDATE source_fact_publication_stream_clock "
        "SET next_sequence=next_sequence+1 WHERE singleton_key=1 "
        "RETURNING next_sequence-1"
    ).fetchone()
    if row is None:
        raise PublicationStreamVerificationError("publication_stream_clock_missing")
    return int(row[0])


def _publication_header(
    conn: sqlite3.Connection,
    publication_id: str,
) -> dict[str, object] | None:
    return _fetchone(
        conn,
        "SELECT publication.publication_id,"
        "publication.publication_payload_sha256,"
        "publication.member_set_sha256,seal.publication_seal_id,"
        "seal.sealed_at "
        "FROM source_fact_publications AS publication "
        "JOIN source_fact_publication_seals AS seal "
        "ON seal.publication_id=publication.publication_id "
        "WHERE publication.publication_id=?",
        (publication_id,),
    )


def _event_row_by_publication(
    conn: sqlite3.Connection,
    publication_id: str,
) -> dict[str, object] | None:
    return _fetchone(
        conn,
        "SELECT publication_sequence,stream_id,publication_id,"
        "publication_seal_id,publication_payload_sha256,"
        "member_set_sha256,sequence_basis,sealed_at,assigned_at,"
        "event_version,canonical_event_json,event_sha256 "
        "FROM source_fact_publication_stream WHERE publication_id=?",
        (publication_id,),
    )


def _event_row_by_sequence(
    conn: sqlite3.Connection,
    publication_sequence: int,
) -> dict[str, object] | None:
    return _fetchone(
        conn,
        "SELECT publication_sequence,stream_id,publication_id,"
        "publication_seal_id,publication_payload_sha256,"
        "member_set_sha256,sequence_basis,sealed_at,assigned_at,"
        "event_version,canonical_event_json,event_sha256 "
        "FROM source_fact_publication_stream WHERE publication_sequence=?",
        (publication_sequence,),
    )


def _sql_publication_event_v1(*values: object) -> str:
    return publication_event_payload(
        stream_id=values[0],
        publication_sequence=values[1],
        publication_id=values[2],
        publication_seal_id=values[3],
        publication_payload_sha256=values[4],
        member_set_sha256=values[5],
        sequence_basis=values[6],
        sealed_at=values[7],
        assigned_at=values[8],
    )


def _sql_fact_sha256(value: object) -> str:
    return digest_text(str(value))


def _sql_resolution_snapshot_watermark_v1(*values: object) -> str:
    return resolution_snapshot_watermark_payload(
        resolution_snapshot_id=values[0],
        stream_id=values[1],
        publication_high_watermark=values[2],
        high_watermark_event_sha256=values[3],
        cutoff_at=values[4],
        recorded_at=values[5],
    )


@contextmanager
def _savepoint(
    conn: sqlite3.Connection,
    name: str,
) -> Generator[None, None, None]:
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")


@contextmanager
def _writer_scope(
    conn: sqlite3.Connection,
) -> Generator[None, None, None]:
    """Own and commit standalone writes; preserve an enclosing transaction."""

    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        if started and conn.in_transaction:
            conn.rollback()
        raise
    if started:
        conn.commit()


def _fetchone(
    conn: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...],
) -> dict[str, object] | None:
    cursor = conn.execute(statement, parameters)
    row = cursor.fetchone()
    if row is None:
        return None
    columns = tuple(description[0] for description in cursor.description or ())
    return dict(zip(columns, tuple(row), strict=True))


def _fetchall(
    conn: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...],
) -> list[dict[str, object]]:
    cursor = conn.execute(statement, parameters)
    columns = tuple(description[0] for description in cursor.description or ())
    return [dict(zip(columns, tuple(row), strict=True)) for row in cursor.fetchall()]


def _values_equal(
    stored: tuple[object, ...],
    expected: tuple[object, ...],
) -> bool:
    if len(stored) != len(expected):
        return False
    for left, right in zip(stored, expected, strict=True):
        if isinstance(right, datetime):
            if _datetime(left) != _utc(right):
                return False
        elif left != right:
            return False
    return True


def _datetime(value: object) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid stream clock: {value!r}") from exc
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_int(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _optional_text(value: object | None) -> str | None:
    return None if value is None else str(value)
