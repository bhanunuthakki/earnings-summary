"""DB-neutral contracts for an exact, resumable PostgreSQL shadow read model.

This module deliberately knows nothing about SQLite, PostgreSQL, pgvector, or a
transport.  It defines the immutable wire batches, source coordinates,
compare-and-swap checkpoint transition, and parity decisions that an adapter
must honor.  Canonical JSON is the hash authority; database-native row
serialization is never admitted as evidence of parity.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SHADOW_CONTRACT_VERSION = "postgres-shadow-read.v1"
SOURCE_BINDING_VERSION = "postgres-shadow-source-binding.v1"
RETRIEVAL_TRACE_VERSION = "postgres-shadow-retrieval-trace.v1"
DELIVERY_RECORD_VERSION = "postgres-shadow-delivery-record.v1"
BATCH_VERSION = "postgres-shadow-batch.v1"
CHECKPOINT_VERSION = "postgres-shadow-checkpoint.v1"
INITIAL_EVENT_SHA256 = "0" * 64
INITIAL_BATCH_SHA256 = "0" * 64
MAX_BATCH_ROWS = 1_000
MAX_BATCH_BYTES = 16 * 1024 * 1024

StreamKind = Literal["canonical_projection", "retrieval_trace"]
RetrievalKind = Literal["fact", "lexical", "ann"]
ParityMode = Literal["fact_exact", "lexical_exact", "ann_eval_gated"]
ImportStatus = Literal["applied", "buffered", "duplicate"]


def canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_time(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_sha(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


def _canonical_payload(value: str) -> str:
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("payload must be valid JSON") from exc
    if canonical_json(parsed) != value:
        raise ValueError("payload JSON must be canonical")
    return value


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ShadowSeal(_Frozen):
    """One exact immutable upstream seal."""

    seal_id: str = Field(min_length=1, max_length=128)
    seal_sha256: str

    _sha = field_validator("seal_sha256")(_validate_sha)


class ShadowSourceBinding(_Frozen):
    """The complete source coordinate shared by every shadow artifact."""

    stream_id: str = Field(min_length=1, max_length=128)
    publication_sequence: int = Field(ge=0)
    publication_event_sha256: str
    ontology: ShadowSeal
    resolution: ShadowSeal
    research: ShadowSeal
    projection: ShadowSeal
    cutoff_at: datetime
    binding_version: str = SOURCE_BINDING_VERSION
    canonical_binding_json: str
    binding_sha256: str

    _event_sha = field_validator("publication_event_sha256")(_validate_sha)
    _binding_sha = field_validator("binding_sha256")(_validate_sha)

    @model_validator(mode="after")
    def _exact_binding(self) -> Self:
        if self.binding_version != SOURCE_BINDING_VERSION:
            raise ValueError("source binding version is unsupported")
        if (self.publication_sequence == 0) != (
            self.publication_event_sha256 == INITIAL_EVENT_SHA256
        ):
            raise ValueError("initial publication cursor shape is invalid")
        payload = _source_binding_payload(
            stream_id=self.stream_id,
            publication_sequence=self.publication_sequence,
            publication_event_sha256=self.publication_event_sha256,
            ontology=self.ontology,
            resolution=self.resolution,
            research=self.research,
            projection=self.projection,
            cutoff_at=self.cutoff_at,
        )
        if self.canonical_binding_json != payload:
            raise ValueError("source binding JSON is not canonical")
        if self.binding_sha256 != digest_text(payload):
            raise ValueError("source binding hash is not exact")
        return self


def _source_binding_payload(
    *,
    stream_id: str,
    publication_sequence: int,
    publication_event_sha256: str,
    ontology: ShadowSeal,
    resolution: ShadowSeal,
    research: ShadowSeal,
    projection: ShadowSeal,
    cutoff_at: datetime,
) -> str:
    return canonical_json(
        {
            "binding_version": SOURCE_BINDING_VERSION,
            "cutoff_at": canonical_time(cutoff_at),
            "ontology": ontology.model_dump(mode="json"),
            "projection": projection.model_dump(mode="json"),
            "publication_event_sha256": publication_event_sha256,
            "publication_sequence": publication_sequence,
            "research": research.model_dump(mode="json"),
            "resolution": resolution.model_dump(mode="json"),
            "stream_id": stream_id,
        }
    )


def build_source_binding(
    *,
    stream_id: str,
    publication_sequence: int,
    publication_event_sha256: str,
    ontology: ShadowSeal,
    resolution: ShadowSeal,
    research: ShadowSeal,
    projection: ShadowSeal,
    cutoff_at: datetime,
) -> ShadowSourceBinding:
    payload = _source_binding_payload(
        stream_id=stream_id,
        publication_sequence=publication_sequence,
        publication_event_sha256=publication_event_sha256,
        ontology=ontology,
        resolution=resolution,
        research=research,
        projection=projection,
        cutoff_at=cutoff_at,
    )
    return ShadowSourceBinding(
        stream_id=stream_id,
        publication_sequence=publication_sequence,
        publication_event_sha256=publication_event_sha256,
        ontology=ontology,
        resolution=resolution,
        research=research,
        projection=projection,
        cutoff_at=cutoff_at,
        canonical_binding_json=payload,
        binding_sha256=digest_text(payload),
    )


class ProjectionRecord(_Frozen):
    """One canonical projection row serialized independently of a DB driver."""

    record_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=512)
    canonical_payload_json: str
    payload_sha256: str

    _payload_json = field_validator("canonical_payload_json")(_canonical_payload)
    _payload_sha = field_validator("payload_sha256")(_validate_sha)

    @model_validator(mode="after")
    def _exact_payload(self) -> Self:
        if self.payload_sha256 != digest_text(self.canonical_payload_json):
            raise ValueError("projection record payload hash is not exact")
        return self

    @classmethod
    def from_payload(
        cls,
        *,
        record_id: str,
        idempotency_key: str,
        canonical_payload_json: str,
    ) -> ProjectionRecord:
        return cls(
            record_id=record_id,
            idempotency_key=idempotency_key,
            canonical_payload_json=canonical_payload_json,
            payload_sha256=digest_text(canonical_payload_json),
        )


class RetrievalHit(_Frozen):
    rank: int = Field(gt=0)
    hit_id: str = Field(min_length=1, max_length=256)
    payload_sha256: str
    score: str = Field(min_length=1, max_length=128)

    _payload_sha = field_validator("payload_sha256")(_validate_sha)


class RetrievalTrace(_Frozen):
    """One exact fact/lexical trace or one ANN observation for eval."""

    trace_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=512)
    retrieval_kind: RetrievalKind
    query_sha256: str
    retrieval_config_sha256: str
    source_binding: ShadowSourceBinding
    source_binding_sha256: str
    cutoff_at: datetime
    hits: tuple[RetrievalHit, ...]
    trace_version: str = RETRIEVAL_TRACE_VERSION
    canonical_trace_json: str
    trace_sha256: str

    _sha = field_validator(
        "query_sha256",
        "retrieval_config_sha256",
        "source_binding_sha256",
        "trace_sha256",
    )(_validate_sha)

    @model_validator(mode="after")
    def _exact_trace(self) -> Self:
        if self.trace_version != RETRIEVAL_TRACE_VERSION:
            raise ValueError("retrieval trace version is unsupported")
        if self.source_binding_sha256 != self.source_binding.binding_sha256 or _utc(
            self.cutoff_at
        ) != _utc(self.source_binding.cutoff_at):
            raise ValueError("retrieval trace source binding is not exact")
        ranks = tuple(hit.rank for hit in self.hits)
        if ranks != tuple(range(1, len(self.hits) + 1)):
            raise ValueError("retrieval hits must have contiguous ranks")
        hit_ids = tuple(hit.hit_id for hit in self.hits)
        if len(hit_ids) != len(set(hit_ids)):
            raise ValueError("retrieval hit identities must be unique")
        payload = _retrieval_trace_payload(
            trace_id=self.trace_id,
            idempotency_key=self.idempotency_key,
            retrieval_kind=self.retrieval_kind,
            query_sha256=self.query_sha256,
            retrieval_config_sha256=self.retrieval_config_sha256,
            source_binding=self.source_binding,
            source_binding_sha256=self.source_binding_sha256,
            cutoff_at=self.cutoff_at,
            hits=self.hits,
        )
        if self.canonical_trace_json != payload:
            raise ValueError("retrieval trace JSON is not canonical")
        if self.trace_sha256 != digest_text(payload):
            raise ValueError("retrieval trace hash is not exact")
        return self


def _retrieval_trace_payload(
    *,
    trace_id: str,
    idempotency_key: str,
    retrieval_kind: RetrievalKind,
    query_sha256: str,
    retrieval_config_sha256: str,
    source_binding: ShadowSourceBinding,
    source_binding_sha256: str,
    cutoff_at: datetime,
    hits: tuple[RetrievalHit, ...],
) -> str:
    return canonical_json(
        {
            "cutoff_at": canonical_time(cutoff_at),
            "hits": [hit.model_dump(mode="json") for hit in hits],
            "idempotency_key": idempotency_key,
            "query_sha256": query_sha256,
            "retrieval_config_sha256": retrieval_config_sha256,
            "retrieval_kind": retrieval_kind,
            "source_binding": source_binding.model_dump(mode="json"),
            "source_binding_sha256": source_binding_sha256,
            "trace_id": trace_id,
            "trace_version": RETRIEVAL_TRACE_VERSION,
        }
    )


def build_retrieval_trace(
    *,
    trace_id: str,
    idempotency_key: str,
    retrieval_kind: RetrievalKind,
    query_sha256: str,
    retrieval_config_sha256: str,
    source_binding: ShadowSourceBinding,
    hits: tuple[RetrievalHit, ...],
) -> RetrievalTrace:
    payload = _retrieval_trace_payload(
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        retrieval_kind=retrieval_kind,
        query_sha256=query_sha256,
        retrieval_config_sha256=retrieval_config_sha256,
        source_binding=source_binding,
        source_binding_sha256=source_binding.binding_sha256,
        cutoff_at=source_binding.cutoff_at,
        hits=hits,
    )
    return RetrievalTrace(
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        retrieval_kind=retrieval_kind,
        query_sha256=query_sha256,
        retrieval_config_sha256=retrieval_config_sha256,
        source_binding=source_binding,
        source_binding_sha256=source_binding.binding_sha256,
        cutoff_at=source_binding.cutoff_at,
        hits=hits,
        canonical_trace_json=payload,
        trace_sha256=digest_text(payload),
    )


class BatchLimits(_Frozen):
    max_rows: int = Field(default=MAX_BATCH_ROWS, ge=1, le=MAX_BATCH_ROWS)
    max_bytes: int = Field(default=MAX_BATCH_BYTES, ge=1_024, le=MAX_BATCH_BYTES)


DEFAULT_BATCH_LIMITS = BatchLimits()


class ShadowDeliveryRecord(_Frozen):
    delivery_sequence: int = Field(gt=0)
    record_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=512)
    canonical_payload_json: str
    payload_sha256: str
    record_version: str = DELIVERY_RECORD_VERSION
    canonical_record_json: str
    record_sha256: str

    _payload_json = field_validator("canonical_payload_json")(_canonical_payload)
    _sha = field_validator("payload_sha256", "record_sha256")(_validate_sha)

    @model_validator(mode="after")
    def _exact_record(self) -> Self:
        if self.record_version != DELIVERY_RECORD_VERSION:
            raise ValueError("delivery record version is unsupported")
        if self.payload_sha256 != digest_text(self.canonical_payload_json):
            raise ValueError("delivery payload hash is not exact")
        payload = _delivery_record_payload(
            delivery_sequence=self.delivery_sequence,
            record_id=self.record_id,
            idempotency_key=self.idempotency_key,
            canonical_payload_json=self.canonical_payload_json,
            payload_sha256=self.payload_sha256,
        )
        if self.canonical_record_json != payload:
            raise ValueError("delivery record JSON is not canonical")
        if self.record_sha256 != digest_text(payload):
            raise ValueError("delivery record hash is not exact")
        return self


def _delivery_record_payload(
    *,
    delivery_sequence: int,
    record_id: str,
    idempotency_key: str,
    canonical_payload_json: str,
    payload_sha256: str,
) -> str:
    return canonical_json(
        {
            "canonical_payload_json": canonical_payload_json,
            "delivery_sequence": delivery_sequence,
            "idempotency_key": idempotency_key,
            "payload_sha256": payload_sha256,
            "record_id": record_id,
            "record_version": DELIVERY_RECORD_VERSION,
        }
    )


def _delivery_record(
    sequence: int,
    record_id: str,
    idempotency_key: str,
    canonical_payload_json: str,
    payload_sha256: str,
) -> ShadowDeliveryRecord:
    payload = _delivery_record_payload(
        delivery_sequence=sequence,
        record_id=record_id,
        idempotency_key=idempotency_key,
        canonical_payload_json=canonical_payload_json,
        payload_sha256=payload_sha256,
    )
    return ShadowDeliveryRecord(
        delivery_sequence=sequence,
        record_id=record_id,
        idempotency_key=idempotency_key,
        canonical_payload_json=canonical_payload_json,
        payload_sha256=payload_sha256,
        canonical_record_json=payload,
        record_sha256=digest_text(payload),
    )


class ShadowBatch(_Frozen):
    contract_version: str = SHADOW_CONTRACT_VERSION
    batch_version: str = BATCH_VERSION
    stream_kind: StreamKind
    source_binding: ShadowSourceBinding
    source_binding_sha256: str
    batch_ordinal: int = Field(gt=0)
    batch_count: int = Field(gt=0)
    start_sequence: int = Field(ge=0)
    end_sequence: int = Field(ge=0)
    terminal_sequence: int = Field(ge=0)
    previous_batch_sha256: str
    records: tuple[ShadowDeliveryRecord, ...]
    row_count: int = Field(ge=0, le=MAX_BATCH_ROWS)
    payload_bytes: int = Field(ge=0, le=MAX_BATCH_BYTES)
    limits: BatchLimits
    batch_id: str = Field(min_length=1, max_length=128)
    canonical_batch_json: str
    batch_sha256: str

    _sha = field_validator(
        "source_binding_sha256",
        "previous_batch_sha256",
        "batch_sha256",
    )(_validate_sha)

    @model_validator(mode="after")
    def _exact_batch(self) -> Self:
        if self.contract_version != SHADOW_CONTRACT_VERSION:
            raise ValueError("shadow contract version is unsupported")
        if self.batch_version != BATCH_VERSION:
            raise ValueError("shadow batch version is unsupported")
        if self.source_binding_sha256 != self.source_binding.binding_sha256:
            raise ValueError("batch source binding hash is not exact")
        if self.batch_ordinal > self.batch_count:
            raise ValueError("batch ordinal exceeds batch count")
        sequences = tuple(record.delivery_sequence for record in self.records)
        if self.records:
            expected = tuple(range(self.start_sequence, self.end_sequence + 1))
            if sequences != expected:
                raise ValueError("batch records must be a contiguous range")
        elif not (
            self.batch_ordinal == 1
            and self.batch_count == 1
            and self.start_sequence == 0
            and self.end_sequence == 0
            and self.terminal_sequence == 0
        ):
            raise ValueError("only a terminal empty export may have no records")
        if self.end_sequence > self.terminal_sequence:
            raise ValueError("batch exceeds terminal sequence")
        if self.row_count != len(self.records):
            raise ValueError("batch row count is not exact")
        payload_bytes = sum(
            len(record.canonical_record_json.encode("utf-8")) for record in self.records
        )
        if self.payload_bytes != payload_bytes:
            raise ValueError("batch byte count is not exact")
        if self.row_count > self.limits.max_rows:
            raise ValueError("batch row cap exceeded")
        if self.payload_bytes > self.limits.max_bytes:
            raise ValueError("batch byte cap exceeded")
        identity = _batch_identity_payload(
            stream_kind=self.stream_kind,
            source_binding_sha256=self.source_binding_sha256,
            batch_ordinal=self.batch_ordinal,
            batch_count=self.batch_count,
            start_sequence=self.start_sequence,
            end_sequence=self.end_sequence,
            terminal_sequence=self.terminal_sequence,
            previous_batch_sha256=self.previous_batch_sha256,
            records=self.records,
            row_count=self.row_count,
            payload_bytes=self.payload_bytes,
            limits=self.limits,
        )
        expected_id = f"psb_{digest_text(identity)[:40]}"
        if self.batch_id != expected_id:
            raise ValueError("batch id is not deterministic")
        payload = _batch_payload(self, identity)
        if self.canonical_batch_json != payload:
            raise ValueError("batch JSON is not canonical")
        if self.batch_sha256 != digest_text(payload):
            raise ValueError("batch hash is not exact")
        return self


def _batch_identity_payload(
    *,
    stream_kind: StreamKind,
    source_binding_sha256: str,
    batch_ordinal: int,
    batch_count: int,
    start_sequence: int,
    end_sequence: int,
    terminal_sequence: int,
    previous_batch_sha256: str,
    records: tuple[ShadowDeliveryRecord, ...],
    row_count: int,
    payload_bytes: int,
    limits: BatchLimits,
) -> str:
    return canonical_json(
        {
            "batch_count": batch_count,
            "batch_ordinal": batch_ordinal,
            "batch_version": BATCH_VERSION,
            "contract_version": SHADOW_CONTRACT_VERSION,
            "end_sequence": end_sequence,
            "limits": limits.model_dump(mode="json"),
            "payload_bytes": payload_bytes,
            "previous_batch_sha256": previous_batch_sha256,
            "record_sha256s": [record.record_sha256 for record in records],
            "row_count": row_count,
            "source_binding_sha256": source_binding_sha256,
            "start_sequence": start_sequence,
            "stream_kind": stream_kind,
            "terminal_sequence": terminal_sequence,
        }
    )


def _batch_payload(batch: ShadowBatch, identity: str) -> str:
    return canonical_json(
        {
            "batch_id": batch.batch_id,
            "batch_identity_json": identity,
            "records": [record.model_dump(mode="json") for record in batch.records],
            "source_binding": batch.source_binding.model_dump(mode="json"),
        }
    )


def _make_batch(
    *,
    source_binding: ShadowSourceBinding,
    stream_kind: StreamKind,
    batch_ordinal: int,
    batch_count: int,
    terminal_sequence: int,
    previous_batch_sha256: str,
    records: tuple[ShadowDeliveryRecord, ...],
    limits: BatchLimits,
) -> ShadowBatch:
    start = records[0].delivery_sequence if records else 0
    end = records[-1].delivery_sequence if records else 0
    payload_bytes = sum(len(record.canonical_record_json.encode("utf-8")) for record in records)
    identity = _batch_identity_payload(
        stream_kind=stream_kind,
        source_binding_sha256=source_binding.binding_sha256,
        batch_ordinal=batch_ordinal,
        batch_count=batch_count,
        start_sequence=start,
        end_sequence=end,
        terminal_sequence=terminal_sequence,
        previous_batch_sha256=previous_batch_sha256,
        records=records,
        row_count=len(records),
        payload_bytes=payload_bytes,
        limits=limits,
    )
    batch_id = f"psb_{digest_text(identity)[:40]}"
    incomplete = ShadowBatch.model_construct(
        stream_kind=stream_kind,
        source_binding=source_binding,
        source_binding_sha256=source_binding.binding_sha256,
        batch_ordinal=batch_ordinal,
        batch_count=batch_count,
        start_sequence=start,
        end_sequence=end,
        terminal_sequence=terminal_sequence,
        previous_batch_sha256=previous_batch_sha256,
        records=records,
        row_count=len(records),
        payload_bytes=payload_bytes,
        limits=limits,
        batch_id=batch_id,
        canonical_batch_json="",
        batch_sha256=INITIAL_BATCH_SHA256,
    )
    payload = _batch_payload(incomplete, identity)
    return ShadowBatch(
        stream_kind=stream_kind,
        source_binding=source_binding,
        source_binding_sha256=source_binding.binding_sha256,
        batch_ordinal=batch_ordinal,
        batch_count=batch_count,
        start_sequence=start,
        end_sequence=end,
        terminal_sequence=terminal_sequence,
        previous_batch_sha256=previous_batch_sha256,
        records=records,
        row_count=len(records),
        payload_bytes=payload_bytes,
        limits=limits,
        batch_id=batch_id,
        canonical_batch_json=payload,
        batch_sha256=digest_text(payload),
    )


def _export_batches(
    source_binding: ShadowSourceBinding,
    stream_kind: StreamKind,
    records: tuple[tuple[str, str, str, str], ...],
    limits: BatchLimits,
) -> tuple[ShadowBatch, ...]:
    idempotency_keys = tuple(item[1] for item in records)
    if len(idempotency_keys) != len(set(idempotency_keys)):
        raise ValueError("export idempotency keys must be unique")
    delivery = tuple(
        _delivery_record(
            sequence,
            record_id,
            idempotency_key,
            canonical_payload_json,
            payload_sha256,
        )
        for sequence, (
            record_id,
            idempotency_key,
            canonical_payload_json,
            payload_sha256,
        ) in enumerate(records, start=1)
    )
    groups: list[tuple[ShadowDeliveryRecord, ...]] = []
    pending: list[ShadowDeliveryRecord] = []
    pending_bytes = 0
    for record in delivery:
        size = len(record.canonical_record_json.encode("utf-8"))
        if size > limits.max_bytes:
            raise ValueError("record_exceeds_batch_byte_cap")
        if pending and (len(pending) >= limits.max_rows or pending_bytes + size > limits.max_bytes):
            groups.append(tuple(pending))
            pending = []
            pending_bytes = 0
        pending.append(record)
        pending_bytes += size
    if pending:
        groups.append(tuple(pending))
    if not groups:
        groups.append(())
    batches: list[ShadowBatch] = []
    previous_sha = INITIAL_BATCH_SHA256
    for ordinal, group in enumerate(groups, start=1):
        batch = _make_batch(
            source_binding=source_binding,
            stream_kind=stream_kind,
            batch_ordinal=ordinal,
            batch_count=len(groups),
            terminal_sequence=len(delivery),
            previous_batch_sha256=previous_sha,
            records=group,
            limits=limits,
        )
        batches.append(batch)
        previous_sha = batch.batch_sha256
    return tuple(batches)


def export_projection_batches(
    source_binding: ShadowSourceBinding,
    records: tuple[ProjectionRecord, ...],
    *,
    limits: BatchLimits = DEFAULT_BATCH_LIMITS,
) -> tuple[ShadowBatch, ...]:
    return _export_batches(
        source_binding,
        "canonical_projection",
        tuple(
            (
                record.record_id,
                record.idempotency_key,
                record.canonical_payload_json,
                record.payload_sha256,
            )
            for record in records
        ),
        limits,
    )


def export_retrieval_trace_batches(
    source_binding: ShadowSourceBinding,
    traces: tuple[RetrievalTrace, ...],
    *,
    limits: BatchLimits = DEFAULT_BATCH_LIMITS,
) -> tuple[ShadowBatch, ...]:
    for trace in traces:
        if (
            trace.source_binding != source_binding
            or trace.source_binding_sha256 != source_binding.binding_sha256
            or _utc(trace.cutoff_at) != _utc(source_binding.cutoff_at)
        ):
            raise ValueError("retrieval trace source binding is not exact")
    return _export_batches(
        source_binding,
        "retrieval_trace",
        tuple(
            (
                trace.trace_id,
                trace.idempotency_key,
                trace.canonical_trace_json,
                trace.trace_sha256,
            )
            for trace in traces
        ),
        limits,
    )


class AppliedRecord(_Frozen):
    delivery_sequence: int = Field(gt=0)
    record_id: str
    idempotency_key: str
    payload_sha256: str
    record_sha256: str

    _sha = field_validator("payload_sha256", "record_sha256")(_validate_sha)


class AppliedBatch(_Frozen):
    batch_ordinal: int = Field(gt=0)
    batch_id: str
    batch_sha256: str
    end_sequence: int = Field(ge=0)

    _sha = field_validator("batch_sha256")(_validate_sha)


class ImportCheckpoint(_Frozen):
    checkpoint_version: str = CHECKPOINT_VERSION
    stream_kind: StreamKind
    source_binding: ShadowSourceBinding
    source_binding_sha256: str
    expected_batch_count: int | None = Field(default=None, gt=0)
    terminal_sequence: int | None = Field(default=None, ge=0)
    applied_through_sequence: int = Field(ge=0)
    last_batch_sha256: str
    applied_batches: tuple[AppliedBatch, ...]
    applied_records: tuple[AppliedRecord, ...]
    pending_batches: tuple[ShadowBatch, ...]
    canonical_checkpoint_json: str
    checkpoint_sha256: str

    _sha = field_validator("source_binding_sha256", "last_batch_sha256", "checkpoint_sha256")(
        _validate_sha
    )

    @model_validator(mode="after")
    def _exact_checkpoint(self) -> Self:
        if self.checkpoint_version != CHECKPOINT_VERSION:
            raise ValueError("checkpoint version is unsupported")
        if self.source_binding_sha256 != self.source_binding.binding_sha256:
            raise ValueError("checkpoint source binding hash is not exact")
        if tuple(item.batch_ordinal for item in self.applied_batches) != tuple(
            range(1, len(self.applied_batches) + 1)
        ):
            raise ValueError("applied batch receipts must be contiguous")
        if tuple(item.delivery_sequence for item in self.applied_records) != tuple(
            range(1, self.applied_through_sequence + 1)
        ):
            raise ValueError("applied record receipts must be contiguous")
        pending_ordinals = tuple(item.batch_ordinal for item in self.pending_batches)
        if pending_ordinals != tuple(sorted(pending_ordinals)):
            raise ValueError("pending batches must be ordered")
        if len(pending_ordinals) != len(set(pending_ordinals)):
            raise ValueError("pending batch ordinals must be unique")
        if any(item.stream_kind != self.stream_kind for item in self.pending_batches):
            raise ValueError("pending batch stream does not match checkpoint")
        payload = _checkpoint_payload(
            stream_kind=self.stream_kind,
            source_binding_sha256=self.source_binding_sha256,
            expected_batch_count=self.expected_batch_count,
            terminal_sequence=self.terminal_sequence,
            applied_through_sequence=self.applied_through_sequence,
            last_batch_sha256=self.last_batch_sha256,
            applied_batches=self.applied_batches,
            applied_records=self.applied_records,
            pending_batches=self.pending_batches,
        )
        if self.canonical_checkpoint_json != payload:
            raise ValueError("checkpoint JSON is not canonical")
        if self.checkpoint_sha256 != digest_text(payload):
            raise ValueError("checkpoint hash is not exact")
        return self

    @classmethod
    def initial(
        cls,
        *,
        source_binding: ShadowSourceBinding,
        stream_kind: StreamKind,
    ) -> ImportCheckpoint:
        return _make_checkpoint(
            source_binding=source_binding,
            stream_kind=stream_kind,
            expected_batch_count=None,
            terminal_sequence=None,
            applied_through_sequence=0,
            last_batch_sha256=INITIAL_BATCH_SHA256,
            applied_batches=(),
            applied_records=(),
            pending_batches=(),
        )


def _checkpoint_payload(
    *,
    stream_kind: StreamKind,
    source_binding_sha256: str,
    expected_batch_count: int | None,
    terminal_sequence: int | None,
    applied_through_sequence: int,
    last_batch_sha256: str,
    applied_batches: tuple[AppliedBatch, ...],
    applied_records: tuple[AppliedRecord, ...],
    pending_batches: tuple[ShadowBatch, ...],
) -> str:
    return canonical_json(
        {
            "applied_batches": [item.model_dump(mode="json") for item in applied_batches],
            "applied_records": [item.model_dump(mode="json") for item in applied_records],
            "applied_through_sequence": applied_through_sequence,
            "checkpoint_version": CHECKPOINT_VERSION,
            "expected_batch_count": expected_batch_count,
            "last_batch_sha256": last_batch_sha256,
            "pending_batches": [item.model_dump(mode="json") for item in pending_batches],
            "source_binding_sha256": source_binding_sha256,
            "stream_kind": stream_kind,
            "terminal_sequence": terminal_sequence,
        }
    )


def _make_checkpoint(
    *,
    source_binding: ShadowSourceBinding,
    stream_kind: StreamKind,
    expected_batch_count: int | None,
    terminal_sequence: int | None,
    applied_through_sequence: int,
    last_batch_sha256: str,
    applied_batches: tuple[AppliedBatch, ...],
    applied_records: tuple[AppliedRecord, ...],
    pending_batches: tuple[ShadowBatch, ...],
) -> ImportCheckpoint:
    payload = _checkpoint_payload(
        stream_kind=stream_kind,
        source_binding_sha256=source_binding.binding_sha256,
        expected_batch_count=expected_batch_count,
        terminal_sequence=terminal_sequence,
        applied_through_sequence=applied_through_sequence,
        last_batch_sha256=last_batch_sha256,
        applied_batches=applied_batches,
        applied_records=applied_records,
        pending_batches=pending_batches,
    )
    return ImportCheckpoint(
        stream_kind=stream_kind,
        source_binding=source_binding,
        source_binding_sha256=source_binding.binding_sha256,
        expected_batch_count=expected_batch_count,
        terminal_sequence=terminal_sequence,
        applied_through_sequence=applied_through_sequence,
        last_batch_sha256=last_batch_sha256,
        applied_batches=applied_batches,
        applied_records=applied_records,
        pending_batches=pending_batches,
        canonical_checkpoint_json=payload,
        checkpoint_sha256=digest_text(payload),
    )


class Divergence(_Frozen):
    reason_code: str
    expected: str | None = None
    actual: str | None = None
    batch_ordinal: int | None = Field(default=None, gt=0)
    delivery_sequence: int | None = Field(default=None, gt=0)


class DivergenceReport(_Frozen):
    admitted: bool
    context: str
    source_binding_sha256: str
    divergences: tuple[Divergence, ...]

    _sha = field_validator("source_binding_sha256")(_validate_sha)

    @model_validator(mode="after")
    def _closed(self) -> Self:
        if self.admitted == bool(self.divergences):
            raise ValueError("admission must be the inverse of divergence presence")
        return self


class ShadowContractError(RuntimeError):
    """A structured fail-closed contract rejection."""

    def __init__(self, report: DivergenceReport) -> None:
        self.report = report
        super().__init__(report.divergences[0].reason_code)


def _report(
    source_binding_sha256: str,
    context: str,
    *divergences: Divergence,
) -> DivergenceReport:
    return DivergenceReport(
        admitted=not divergences,
        context=context,
        source_binding_sha256=source_binding_sha256,
        divergences=tuple(divergences),
    )


def _reject(
    source_binding_sha256: str,
    context: str,
    reason_code: str,
    *,
    expected: str | None = None,
    actual: str | None = None,
    batch_ordinal: int | None = None,
    delivery_sequence: int | None = None,
) -> None:
    raise ShadowContractError(
        _report(
            source_binding_sha256,
            context,
            Divergence(
                reason_code=reason_code,
                expected=expected,
                actual=actual,
                batch_ordinal=batch_ordinal,
                delivery_sequence=delivery_sequence,
            ),
        )
    )


class ImportResult(_Frozen):
    status: ImportStatus
    checkpoint: ImportCheckpoint
    newly_applied_record_count: int = Field(ge=0)


def import_shadow_batch(
    checkpoint: ImportCheckpoint,
    batch: ShadowBatch,
    *,
    expected_checkpoint_sha256: str,
) -> ImportResult:
    """Admit one at-least-once delivery with an exact checkpoint CAS token."""

    try:
        checkpoint = ImportCheckpoint.model_validate(checkpoint.model_dump(mode="python"))
    except ValidationError:
        _reject(
            checkpoint.source_binding_sha256,
            "shadow_import",
            "checkpoint_tampered",
        )
    try:
        batch = ShadowBatch.model_validate(batch.model_dump(mode="python"))
    except ValidationError:
        _reject(
            checkpoint.source_binding_sha256,
            "shadow_import",
            "batch_tampered",
        )
    if expected_checkpoint_sha256 != checkpoint.checkpoint_sha256:
        _reject(
            checkpoint.source_binding_sha256,
            "shadow_import",
            "checkpoint_cas_mismatch",
            expected=checkpoint.checkpoint_sha256,
            actual=expected_checkpoint_sha256,
        )
    if (
        batch.source_binding_sha256 != checkpoint.source_binding_sha256
        or batch.source_binding != checkpoint.source_binding
    ):
        _reject(
            checkpoint.source_binding_sha256,
            "shadow_import",
            "source_binding_mismatch",
            expected=checkpoint.source_binding_sha256,
            actual=batch.source_binding_sha256,
            batch_ordinal=batch.batch_ordinal,
        )
    if batch.stream_kind != checkpoint.stream_kind:
        _reject(
            checkpoint.source_binding_sha256,
            "shadow_import",
            "stream_kind_mismatch",
            expected=checkpoint.stream_kind,
            actual=batch.stream_kind,
            batch_ordinal=batch.batch_ordinal,
        )
    expected_count = checkpoint.expected_batch_count or batch.batch_count
    terminal_sequence = (
        checkpoint.terminal_sequence
        if checkpoint.terminal_sequence is not None
        else batch.terminal_sequence
    )
    if batch.batch_count != expected_count or batch.terminal_sequence != terminal_sequence:
        _reject(
            checkpoint.source_binding_sha256,
            "shadow_import",
            "export_extent_mismatch",
            batch_ordinal=batch.batch_ordinal,
        )
    known_batches = {item.batch_ordinal: item.batch_sha256 for item in checkpoint.applied_batches}
    known_batches.update(
        {item.batch_ordinal: item.batch_sha256 for item in checkpoint.pending_batches}
    )
    known_sha = known_batches.get(batch.batch_ordinal)
    if known_sha is not None:
        if known_sha != batch.batch_sha256:
            _reject(
                checkpoint.source_binding_sha256,
                "shadow_import",
                "batch_ordinal_conflict",
                expected=known_sha,
                actual=batch.batch_sha256,
                batch_ordinal=batch.batch_ordinal,
            )
        return ImportResult(
            status="duplicate",
            checkpoint=checkpoint,
            newly_applied_record_count=0,
        )
    pending = tuple(
        sorted(
            (*checkpoint.pending_batches, batch),
            key=lambda item: item.batch_ordinal,
        )
    )
    applied_batches = list(checkpoint.applied_batches)
    applied_records = list(checkpoint.applied_records)
    applied_through = checkpoint.applied_through_sequence
    last_batch_sha = checkpoint.last_batch_sha256
    newly_applied = 0
    while pending and pending[0].batch_ordinal == len(applied_batches) + 1:
        current, pending = pending[0], pending[1:]
        if current.previous_batch_sha256 != last_batch_sha:
            _reject(
                checkpoint.source_binding_sha256,
                "shadow_import",
                "batch_chain_mismatch",
                expected=last_batch_sha,
                actual=current.previous_batch_sha256,
                batch_ordinal=current.batch_ordinal,
            )
        if current.records and current.start_sequence != applied_through + 1:
            _reject(
                checkpoint.source_binding_sha256,
                "shadow_import",
                "delivery_sequence_gap",
                expected=str(applied_through + 1),
                actual=str(current.start_sequence),
                batch_ordinal=current.batch_ordinal,
            )
        existing_keys = {item.idempotency_key: item.payload_sha256 for item in applied_records}
        for record in current.records:
            existing_sha = existing_keys.get(record.idempotency_key)
            if existing_sha is not None:
                _reject(
                    checkpoint.source_binding_sha256,
                    "shadow_import",
                    "record_idempotency_conflict",
                    expected=existing_sha,
                    actual=record.payload_sha256,
                    batch_ordinal=current.batch_ordinal,
                    delivery_sequence=record.delivery_sequence,
                )
            applied_records.append(
                AppliedRecord(
                    delivery_sequence=record.delivery_sequence,
                    record_id=record.record_id,
                    idempotency_key=record.idempotency_key,
                    payload_sha256=record.payload_sha256,
                    record_sha256=record.record_sha256,
                )
            )
            existing_keys[record.idempotency_key] = record.payload_sha256
            newly_applied += 1
        applied_through = current.end_sequence
        last_batch_sha = current.batch_sha256
        applied_batches.append(
            AppliedBatch(
                batch_ordinal=current.batch_ordinal,
                batch_id=current.batch_id,
                batch_sha256=current.batch_sha256,
                end_sequence=current.end_sequence,
            )
        )
    updated = _make_checkpoint(
        source_binding=checkpoint.source_binding,
        stream_kind=checkpoint.stream_kind,
        expected_batch_count=expected_count,
        terminal_sequence=terminal_sequence,
        applied_through_sequence=applied_through,
        last_batch_sha256=last_batch_sha,
        applied_batches=tuple(applied_batches),
        applied_records=tuple(applied_records),
        pending_batches=pending,
    )
    return ImportResult(
        status="applied"
        if newly_applied or len(applied_batches) > len(checkpoint.applied_batches)
        else "buffered",
        checkpoint=updated,
        newly_applied_record_count=newly_applied,
    )


def finish_shadow_import(checkpoint: ImportCheckpoint) -> DivergenceReport:
    """Return a fail-closed completeness decision for a resumable import."""

    if checkpoint.expected_batch_count is None or checkpoint.terminal_sequence is None:
        return _report(
            checkpoint.source_binding_sha256,
            "shadow_import_completion",
            Divergence(reason_code="export_extent_unknown"),
        )
    if (
        len(checkpoint.applied_batches) != checkpoint.expected_batch_count
        or checkpoint.pending_batches
        or checkpoint.applied_through_sequence != checkpoint.terminal_sequence
    ):
        return _report(
            checkpoint.source_binding_sha256,
            "shadow_import_completion",
            Divergence(
                reason_code="missing_batch_range",
                expected=(
                    f"batches=1..{checkpoint.expected_batch_count};"
                    f"sequence={checkpoint.terminal_sequence}"
                ),
                actual=(
                    f"batches=1..{len(checkpoint.applied_batches)};"
                    f"sequence={checkpoint.applied_through_sequence}"
                ),
            ),
        )
    return _report(
        checkpoint.source_binding_sha256,
        "shadow_import_completion",
    )


class AnnParityGate(_Frozen):
    """A dated exact-baseline evaluation receipt for non-reproducible ANN."""

    eval_run_id: str = Field(min_length=1, max_length=128)
    eval_config_sha256: str
    authoritative_trace_sha256: str
    shadow_trace_sha256: str
    source_binding_sha256: str
    cutoff_at: datetime
    recall_at_k_ppm: int = Field(ge=0, le=1_000_000)
    minimum_recall_at_k_ppm: int = Field(ge=0, le=1_000_000)
    evaluated_query_count: int = Field(gt=0)

    _sha = field_validator(
        "eval_config_sha256",
        "authoritative_trace_sha256",
        "shadow_trace_sha256",
        "source_binding_sha256",
    )(_validate_sha)

    @property
    def passed(self) -> bool:
        return self.recall_at_k_ppm >= self.minimum_recall_at_k_ppm


class ParityReport(_Frozen):
    admitted: bool
    parity_mode: ParityMode
    source_binding_sha256: str
    divergences: tuple[Divergence, ...]
    ann_eval_run_id: str | None = None

    _sha = field_validator("source_binding_sha256")(_validate_sha)

    @model_validator(mode="after")
    def _closed(self) -> Self:
        if self.admitted == bool(self.divergences):
            raise ValueError("parity admission must be the inverse of divergence presence")
        return self


def compare_projection_parity(
    *,
    authoritative_binding: ShadowSourceBinding,
    shadow_binding: ShadowSourceBinding,
    authoritative_records: tuple[ProjectionRecord, ...],
    shadow_records: tuple[ProjectionRecord, ...],
) -> ParityReport:
    divergences: list[Divergence] = []
    if authoritative_binding != shadow_binding:
        divergences.append(
            Divergence(
                reason_code="source_binding_mismatch",
                expected=authoritative_binding.binding_sha256,
                actual=shadow_binding.binding_sha256,
            )
        )
    authoritative = tuple(
        (item.record_id, item.idempotency_key, item.payload_sha256)
        for item in authoritative_records
    )
    shadow = tuple(
        (item.record_id, item.idempotency_key, item.payload_sha256) for item in shadow_records
    )
    if authoritative != shadow:
        divergences.append(
            Divergence(
                reason_code="exact_projection_mismatch",
                expected=digest_text(canonical_json(authoritative)),
                actual=digest_text(canonical_json(shadow)),
            )
        )
    return ParityReport(
        admitted=not divergences,
        parity_mode="fact_exact",
        source_binding_sha256=authoritative_binding.binding_sha256,
        divergences=tuple(divergences),
    )


def _trace_content(trace: RetrievalTrace) -> tuple[tuple[int, str, str, str], ...]:
    return tuple((hit.rank, hit.hit_id, hit.payload_sha256, hit.score) for hit in trace.hits)


def compare_retrieval_parity(
    authoritative: RetrievalTrace,
    shadow: RetrievalTrace,
    *,
    ann_gate: AnnParityGate | None = None,
) -> ParityReport:
    if authoritative.retrieval_kind == "ann":
        mode: ParityMode = "ann_eval_gated"
    else:
        mode = cast(ParityMode, f"{authoritative.retrieval_kind}_exact")
    divergences: list[Divergence] = []
    shared_coordinates = (
        authoritative.retrieval_kind == shadow.retrieval_kind
        and authoritative.query_sha256 == shadow.query_sha256
        and authoritative.retrieval_config_sha256 == shadow.retrieval_config_sha256
        and authoritative.source_binding_sha256 == shadow.source_binding_sha256
        and authoritative.source_binding == shadow.source_binding
        and _utc(authoritative.cutoff_at) == _utc(shadow.cutoff_at)
    )
    if not shared_coordinates:
        divergences.append(Divergence(reason_code="retrieval_coordinate_mismatch"))
    if authoritative.retrieval_kind != "ann":
        if _trace_content(authoritative) != _trace_content(shadow):
            divergences.append(
                Divergence(
                    reason_code="exact_retrieval_mismatch",
                    expected=digest_text(canonical_json(_trace_content(authoritative))),
                    actual=digest_text(canonical_json(_trace_content(shadow))),
                )
            )
    elif ann_gate is None:
        divergences.append(Divergence(reason_code="ann_eval_gate_required"))
    elif (
        ann_gate.authoritative_trace_sha256 != authoritative.trace_sha256
        or ann_gate.shadow_trace_sha256 != shadow.trace_sha256
        or ann_gate.source_binding_sha256 != authoritative.source_binding_sha256
        or _utc(ann_gate.cutoff_at) != _utc(authoritative.cutoff_at)
    ):
        divergences.append(Divergence(reason_code="ann_eval_gate_coordinate_mismatch"))
    elif not ann_gate.passed:
        divergences.append(
            Divergence(
                reason_code="ann_recall_below_gate",
                expected=str(ann_gate.minimum_recall_at_k_ppm),
                actual=str(ann_gate.recall_at_k_ppm),
            )
        )
    return ParityReport(
        admitted=not divergences,
        parity_mode=mode,
        source_binding_sha256=authoritative.source_binding_sha256,
        divergences=tuple(divergences),
        ann_eval_run_id=None if ann_gate is None else ann_gate.eval_run_id,
    )
