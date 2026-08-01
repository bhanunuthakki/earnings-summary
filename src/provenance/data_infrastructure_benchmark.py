"""Deterministic, provider-free scalability benchmark for data contracts.

The benchmark owns a caller-selected, newly created SQLite database.  It uses
only synthetic facts and a deliberately small schema that preserves the
contract properties under test: chained stream replay, checkpoint/delta
projection commitments, bounded reads, and full-versus-bucket verification.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self, cast

import pydantic
from alembic.config import Config
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from alembic import command
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionPolicy,
    ResolutionSnapshotScope,
)
from provenance.filing_xbrl_extraction_ledger import (
    FilingXbrlExtractionLedger,
)
from provenance.filing_xbrl_fact_adapter import (
    FilingXbrlExtractionIdentity,
    FilingXbrlNormalizedOutput,
    FilingXbrlSubjectIdentity,
    NormalizedFilingXbrlFact,
)
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalMetric,
    CanonicalMetricCell,
    CanonicalMetricDefinitionRevision,
    MappingRevision,
    MetricOntology,
    OntologySnapshot,
    SourceObservationTaxonomyAssertion,
    SourceTaxonomyComponent,
)
from provenance.source_fact_repository import (
    SourceFactPublication,
    SourceFactRepository,
)
from provenance.source_fact_stream import (
    PublicationCursor,
    bind_resolution_snapshot_watermark,
    read_publication_page,
)
from search.canonical_fact_projection import (
    ProjectionConfig,
    ProjectionGenerationRequest,
    build_canonical_projection_generation,
    search_canonical_facts,
    verify_canonical_projection_generation,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

MAX_FACT_COUNT = 1_000_000
MAX_PRODUCTION_FACT_COUNT = 100_000
MAX_FETCH_SIZE = 1_000
DIGEST_BUCKET_COUNT = 4_096
INITIAL_SHA256 = "0" * 64
CHECKPOINT_GENERATION = "checkpoint-v1"
DELTA_GENERATION = "delta-v1"
PRODUCTION_CHECKPOINT_GENERATION = "production-checkpoint-v1"
PRODUCTION_DELTA_GENERATION = "production-delta-v1"
PRODUCTION_STAMP = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BenchmarkConfig(_FrozenModel):
    fact_count: int = Field(ge=1, le=MAX_FACT_COUNT)
    delta_count: int = Field(ge=1, le=MAX_FACT_COUNT)
    chunk_size: int = Field(ge=1, le=MAX_FETCH_SIZE)
    page_size: int = Field(ge=1, le=MAX_FETCH_SIZE)
    read_samples: int = Field(ge=1, le=MAX_FETCH_SIZE)
    digest_bucket_count: int = DIGEST_BUCKET_COUNT

    @model_validator(mode="after")
    def _valid_shape(self) -> Self:
        if self.delta_count > self.fact_count:
            raise ValueError("delta_count cannot exceed fact_count")
        if self.digest_bucket_count != DIGEST_BUCKET_COUNT:
            raise ValueError("benchmark uses exactly 4096 digest buckets")
        return self


class ProductionBenchmarkConfig(_FrozenModel):
    """Measured scale for the real 0246/0247 public implementations."""

    fact_count: int = Field(ge=2, le=MAX_PRODUCTION_FACT_COUNT)
    publication_chunk_size: int = Field(ge=1, le=MAX_FETCH_SIZE)
    page_size: int = Field(ge=1, le=MAX_FETCH_SIZE)
    read_samples: int = Field(ge=1, le=MAX_FETCH_SIZE)


class BenchmarkBudgets(_FrozenModel):
    max_total_seconds: float = Field(gt=0)
    max_peak_python_memory_bytes: int = Field(gt=0)
    max_database_bytes: int = Field(gt=0)
    min_stream_rows_per_second: float = Field(gt=0)
    min_projection_rows_per_second: float = Field(gt=0)
    max_point_p95_milliseconds: float = Field(gt=0)
    max_page_p95_milliseconds: float = Field(gt=0)
    max_full_audit_seconds: float = Field(gt=0)
    max_bucket_audit_seconds: float = Field(gt=0)


class PhaseMeasurement(_FrozenModel):
    wall_seconds: float = Field(ge=0)
    row_count: int = Field(ge=0)
    rows_per_second: float = Field(ge=0)


class AuditMeasurement(_FrozenModel):
    wall_seconds: float = Field(ge=0)
    verified_rows: int = Field(ge=0)
    maximum_rows_fetched: int = Field(ge=0)


class TimingMeasurements(_FrozenModel):
    total_wall_seconds: float = Field(ge=0)
    stream_append: PhaseMeasurement
    checkpoint_projection: PhaseMeasurement
    delta_projection: PhaseMeasurement
    full_audit: AuditMeasurement
    bucket_audit: AuditMeasurement
    point_read_p50_milliseconds: float = Field(ge=0)
    point_read_p95_milliseconds: float = Field(ge=0)
    page_read_p50_milliseconds: float = Field(ge=0)
    page_read_p95_milliseconds: float = Field(ge=0)
    peak_python_memory_bytes: int = Field(ge=0)
    sqlite_file_bytes: int = Field(ge=0)


class BoundedReadResult(_FrozenModel):
    point_query_count: int = Field(ge=0)
    page_query_count: int = Field(ge=0)
    maximum_rows_fetched_per_query: int = Field(ge=0, le=MAX_FETCH_SIZE)
    configured_page_limit: int = Field(ge=1, le=MAX_FETCH_SIZE)


class CorrectnessPreflight(_FrozenModel):
    exact_stream_replay: bool
    exact_checkpoint_replay: bool
    exact_delta_replay: bool
    tamper_detected: bool
    row_count_invariants: bool


class DeterministicCommitments(_FrozenModel):
    stream_terminal_sha256: str = Field(min_length=64, max_length=64)
    checkpoint_entry_set_sha256: str = Field(min_length=64, max_length=64)
    delta_entry_set_sha256: str = Field(min_length=64, max_length=64)
    audited_digest_bucket: int = Field(ge=0, lt=DIGEST_BUCKET_COUNT)
    audited_bucket_sha256: str = Field(min_length=64, max_length=64)


class BenchmarkRowCounts(_FrozenModel):
    source_stream_events: int = Field(ge=0)
    checkpoint_entries: int = Field(ge=0)
    delta_entries: int = Field(ge=0)
    checkpoint_batches: int = Field(ge=0)
    delta_batches: int = Field(ge=0)


class EnvironmentVersions(_FrozenModel):
    python: str
    sqlite: str
    pydantic: str
    platform: str


class BudgetResult(_FrozenModel):
    budget_name: str
    operator: Literal["<=", ">="]
    actual: float
    threshold: float
    passed: bool


class BenchmarkReport(_FrozenModel):
    report_version: Literal["data_infrastructure_benchmark.v1"]
    config: BenchmarkConfig
    budgets: BenchmarkBudgets
    config_sha256: str = Field(min_length=64, max_length=64)
    deterministic_commitments: DeterministicCommitments
    correctness: CorrectnessPreflight
    row_counts: BenchmarkRowCounts
    bounded_reads: BoundedReadResult
    measurements: TimingMeasurements
    environment: EnvironmentVersions
    budget_results: tuple[BudgetResult, ...]
    overall_pass: bool
    report_sha256: str = Field(min_length=64, max_length=64)


class ProductionCorrectnessPreflight(_FrozenModel):
    publication_exact_replay: bool
    stream_page_replay: bool
    strict_checkpoint_audit: bool
    strict_delta_audit: bool
    bounded_search_reads: bool
    row_count_invariants: bool


class ProductionMeasurements(_FrozenModel):
    total_wall_seconds: float = Field(ge=0)
    migration_wall_seconds: float = Field(ge=0)
    source_fact_publication: PhaseMeasurement
    stream_append: PhaseMeasurement
    ontology_resolution: PhaseMeasurement
    checkpoint_projection: PhaseMeasurement
    delta_projection: PhaseMeasurement
    stream_replay: AuditMeasurement
    full_audit: AuditMeasurement
    bounded_bucket_reads: AuditMeasurement
    point_read_p50_milliseconds: float = Field(ge=0)
    point_read_p95_milliseconds: float = Field(ge=0)
    page_read_p50_milliseconds: float = Field(ge=0)
    page_read_p95_milliseconds: float = Field(ge=0)
    peak_python_memory_bytes: int = Field(ge=0)
    sqlite_file_bytes: int = Field(ge=0)


class ProductionDeterministicCommitments(_FrozenModel):
    stream_terminal_sha256: str = Field(min_length=64, max_length=64)
    checkpoint_projection_sha256: str = Field(min_length=64, max_length=64)
    delta_projection_sha256: str = Field(min_length=64, max_length=64)


class ProductionContractBenchmarkReport(_FrozenModel):
    report_version: Literal["data_infrastructure_production_contract.v1"]
    benchmark_mode: Literal["production_contract"]
    scale_interpretation: Literal["measured_only_no_extrapolation_proof"]
    production_fact_cap: int = MAX_PRODUCTION_FACT_COUNT
    config: ProductionBenchmarkConfig
    budgets: BenchmarkBudgets
    config_sha256: str = Field(min_length=64, max_length=64)
    migration_revision: str
    production_apis: tuple[str, ...]
    build_time_materialization: tuple[str, ...]
    production_limitations: tuple[str, ...]
    deterministic_commitments: ProductionDeterministicCommitments
    correctness: ProductionCorrectnessPreflight
    row_counts: BenchmarkRowCounts
    bounded_reads: BoundedReadResult
    measurements: ProductionMeasurements
    environment: EnvironmentVersions
    budget_results: tuple[BudgetResult, ...]
    overall_pass: bool
    report_sha256: str = Field(min_length=64, max_length=64)


class StreamVerification(_FrozenModel):
    row_count: int = Field(ge=0)
    terminal_sha256: str = Field(min_length=64, max_length=64)
    maximum_rows_fetched: int = Field(ge=0, le=MAX_FETCH_SIZE)


class ProjectionVerification(_FrozenModel):
    generation_id: str
    row_count: int = Field(ge=0)
    entry_set_sha256: str = Field(min_length=64, max_length=64)
    maximum_rows_fetched: int = Field(ge=0, le=MAX_FETCH_SIZE)


class BenchmarkIntegrityError(RuntimeError):
    """A synthetic benchmark commitment failed exact verification."""


class RefusedBenchmarkPathError(RuntimeError):
    """A path could target live or pre-existing state."""


class _FetchTrackingCursor:
    def __init__(
        self,
        cursor: sqlite3.Cursor,
        connection: _FetchTrackingConnection,
    ) -> None:
        self._cursor = cursor
        self._connection = connection

    @property
    def description(self) -> tuple[tuple[object, ...], ...] | None:
        return cast(
            tuple[tuple[object, ...], ...] | None,
            self._cursor.description,
        )

    def fetchone(self) -> tuple[object, ...] | None:
        return cast(tuple[object, ...] | None, self._cursor.fetchone())

    def fetchmany(self, size: int = 1) -> list[tuple[object, ...]]:
        rows = cast(list[tuple[object, ...]], self._cursor.fetchmany(size))
        self._connection.requested_fetch_sizes.append(size)
        self._connection.returned_fetch_sizes.append(len(rows))
        return rows

    def fetchall(self) -> list[tuple[object, ...]]:
        rows = cast(list[tuple[object, ...]], self._cursor.fetchall())
        self._connection.returned_fetch_sizes.append(len(rows))
        return rows

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        return cast(Iterator[tuple[object, ...]], iter(self._cursor))


class _FetchTrackingConnection:
    """Observe public verifier fetch bounds without changing its behavior."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.requested_fetch_sizes: list[int] = []
        self.returned_fetch_sizes: list[int] = []

    @property
    def row_factory(self) -> object:
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value: object) -> None:
        self._connection.row_factory = cast(
            Callable[[sqlite3.Cursor, tuple[object, ...]], object] | None,
            value,
        )

    def create_function(
        self,
        name: str,
        narg: int,
        func: Callable[..., bytes | float | int | str | None] | None,
        *,
        deterministic: bool = False,
    ) -> None:
        self._connection.create_function(
            name,
            narg,
            func,
            deterministic=deterministic,
        )

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> _FetchTrackingCursor:
        return _FetchTrackingCursor(
            self._connection.execute(sql, parameters),
            self,
        )

    @property
    def maximum_rows_returned(self) -> int:
        return max(self.returned_fetch_sizes, default=0)


def _chain(previous_sha256: str, item_sha256: str) -> str:
    return digest_text(
        canonical_json(
            {
                "item_sha256": item_sha256,
                "previous_sha256": previous_sha256,
            }
        )
    )


def _fact_id(ordinal: int) -> str:
    return f"fact-{ordinal:012d}"


def _fact_value(ordinal: int, *, delta: bool = False) -> str:
    suffix = ordinal * 17 + 3
    return f"{suffix}.delta" if delta else str(suffix)


def _digest_bucket(coordinate: str) -> int:
    return int(digest_text(coordinate)[:3], 16)


def _chunks(total: int, size: int) -> Iterable[range]:
    for start in range(0, total, size):
        yield range(start, min(start + size, total))


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE source_stream_events (
            event_sequence INTEGER PRIMARY KEY,
            fact_id TEXT NOT NULL UNIQUE,
            previous_event_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            event_sha256 TEXT NOT NULL
        );
        CREATE TABLE projection_generations (
            generation_id TEXT PRIMARY KEY,
            generation_kind TEXT NOT NULL,
            parent_generation_id TEXT,
            entry_count INTEGER NOT NULL,
            chunk_size INTEGER NOT NULL,
            entry_set_sha256 TEXT NOT NULL
        );
        CREATE TABLE projection_entries (
            generation_id TEXT NOT NULL,
            entry_ordinal INTEGER NOT NULL,
            change_kind TEXT NOT NULL,
            digest_bucket INTEGER NOT NULL,
            coordinate TEXT NOT NULL,
            value_text TEXT NOT NULL,
            entry_json TEXT NOT NULL,
            entry_sha256 TEXT NOT NULL,
            PRIMARY KEY (generation_id, entry_ordinal),
            UNIQUE (generation_id, coordinate)
        );
        CREATE INDEX ix_projection_entry_coordinate
            ON projection_entries (generation_id, coordinate);
        CREATE INDEX ix_projection_entry_bucket
            ON projection_entries
               (generation_id, digest_bucket, entry_ordinal);
        CREATE TABLE projection_batches (
            generation_id TEXT NOT NULL,
            batch_ordinal INTEGER NOT NULL,
            first_entry_ordinal INTEGER NOT NULL,
            last_entry_ordinal INTEGER NOT NULL,
            entry_count INTEGER NOT NULL,
            batch_sha256 TEXT NOT NULL,
            PRIMARY KEY (generation_id, batch_ordinal)
        );
        CREATE TABLE projection_buckets (
            generation_id TEXT NOT NULL,
            digest_bucket INTEGER NOT NULL,
            entry_count INTEGER NOT NULL,
            bucket_sha256 TEXT NOT NULL,
            PRIMARY KEY (generation_id, digest_bucket)
        );
        """
    )


def _append_stream(conn: sqlite3.Connection, config: BenchmarkConfig) -> PhaseMeasurement:
    started = time.perf_counter()
    previous_sha256 = INITIAL_SHA256
    for ordinal_range in _chunks(config.fact_count, config.chunk_size):
        rows: list[tuple[object, ...]] = []
        for ordinal in ordinal_range:
            event_sequence = ordinal + 1
            fact_id = _fact_id(ordinal)
            payload_json = canonical_json({"fact_id": fact_id, "value": _fact_value(ordinal)})
            event_sha256 = digest_text(
                canonical_json(
                    {
                        "event_sequence": event_sequence,
                        "fact_id": fact_id,
                        "payload_sha256": digest_text(payload_json),
                        "previous_event_sha256": previous_sha256,
                    }
                )
            )
            rows.append(
                (
                    event_sequence,
                    fact_id,
                    previous_sha256,
                    payload_json,
                    event_sha256,
                )
            )
            previous_sha256 = event_sha256
        conn.executemany(
            "INSERT INTO source_stream_events VALUES (?,?,?,?,?)",
            rows,
        )
    conn.commit()
    elapsed = time.perf_counter() - started
    return _phase(elapsed, config.fact_count)


def _entry_payload(
    *,
    generation_id: str,
    entry_ordinal: int,
    coordinate: str,
    value_text: str,
    digest_bucket: int,
) -> str:
    return canonical_json(
        {
            "change_kind": "upsert",
            "coordinate": coordinate,
            "digest_bucket": digest_bucket,
            "entry_ordinal": entry_ordinal,
            "generation_id": generation_id,
            "value_text": value_text,
        }
    )


def _build_projection(
    conn: sqlite3.Connection,
    config: BenchmarkConfig,
    *,
    generation_id: str,
    generation_kind: Literal["checkpoint", "delta"],
    parent_generation_id: str | None,
    entry_count: int,
) -> PhaseMeasurement:
    started = time.perf_counter()
    entry_set_sha256 = INITIAL_SHA256
    bucket_sha256 = [INITIAL_SHA256] * DIGEST_BUCKET_COUNT
    bucket_counts = [0] * DIGEST_BUCKET_COUNT
    for batch_ordinal, ordinal_range in enumerate(_chunks(entry_count, config.chunk_size)):
        rows: list[tuple[object, ...]] = []
        batch_hashes: list[str] = []
        for ordinal in ordinal_range:
            coordinate = _fact_id(ordinal)
            value_text = _fact_value(
                ordinal,
                delta=generation_kind == "delta",
            )
            bucket = _digest_bucket(coordinate)
            entry_json = _entry_payload(
                generation_id=generation_id,
                entry_ordinal=ordinal,
                coordinate=coordinate,
                value_text=value_text,
                digest_bucket=bucket,
            )
            entry_sha256 = digest_text(entry_json)
            rows.append(
                (
                    generation_id,
                    ordinal,
                    "upsert",
                    bucket,
                    coordinate,
                    value_text,
                    entry_json,
                    entry_sha256,
                )
            )
            batch_hashes.append(entry_sha256)
            entry_set_sha256 = _chain(entry_set_sha256, entry_sha256)
            bucket_sha256[bucket] = _chain(
                bucket_sha256[bucket],
                entry_sha256,
            )
            bucket_counts[bucket] += 1
        conn.executemany(
            "INSERT INTO projection_entries VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        first = ordinal_range.start
        last = ordinal_range.stop - 1
        conn.execute(
            "INSERT INTO projection_batches VALUES (?,?,?,?,?,?)",
            (
                generation_id,
                batch_ordinal,
                first,
                last,
                len(rows),
                digest_text(canonical_json(batch_hashes)),
            ),
        )
    conn.executemany(
        "INSERT INTO projection_buckets VALUES (?,?,?,?)",
        (
            (
                generation_id,
                bucket,
                bucket_counts[bucket],
                bucket_sha256[bucket],
            )
            for bucket in range(DIGEST_BUCKET_COUNT)
        ),
    )
    conn.execute(
        "INSERT INTO projection_generations VALUES (?,?,?,?,?,?)",
        (
            generation_id,
            generation_kind,
            parent_generation_id,
            entry_count,
            config.chunk_size,
            entry_set_sha256,
        ),
    )
    conn.commit()
    elapsed = time.perf_counter() - started
    return _phase(elapsed, entry_count)


def _phase(elapsed: float, row_count: int) -> PhaseMeasurement:
    return PhaseMeasurement(
        wall_seconds=elapsed,
        row_count=row_count,
        rows_per_second=(row_count / elapsed if elapsed > 0 else 0.0),
    )


def verify_stream_chain(
    conn: sqlite3.Connection,
    *,
    fetch_size: int,
) -> StreamVerification:
    if fetch_size < 1 or fetch_size > MAX_FETCH_SIZE:
        raise ValueError(f"fetch_size must be between 1 and {MAX_FETCH_SIZE}")
    cursor = conn.execute(
        "SELECT event_sequence,fact_id,previous_event_sha256,payload_json,"
        "event_sha256 FROM source_stream_events ORDER BY event_sequence"
    )
    expected_sequence = 1
    previous_sha256 = INITIAL_SHA256
    maximum_rows_fetched = 0
    while True:
        rows = cursor.fetchmany(fetch_size)
        maximum_rows_fetched = max(maximum_rows_fetched, len(rows))
        if not rows:
            break
        for row in rows:
            sequence = int(row[0])
            fact_id = str(row[1])
            stored_previous = str(row[2])
            payload_json = str(row[3])
            stored_sha256 = str(row[4])
            expected_payload = canonical_json(
                {
                    "fact_id": _fact_id(sequence - 1),
                    "value": _fact_value(sequence - 1),
                }
            )
            if payload_json != expected_payload:
                raise BenchmarkIntegrityError("stream_event_payload_tampered")
            expected_sha256 = digest_text(
                canonical_json(
                    {
                        "event_sequence": sequence,
                        "fact_id": fact_id,
                        "payload_sha256": digest_text(payload_json),
                        "previous_event_sha256": previous_sha256,
                    }
                )
            )
            if (
                sequence != expected_sequence
                or fact_id != _fact_id(sequence - 1)
                or stored_previous != previous_sha256
                or stored_sha256 != expected_sha256
            ):
                raise BenchmarkIntegrityError("stream_chain_tampered")
            previous_sha256 = stored_sha256
            expected_sequence += 1
    return StreamVerification(
        row_count=expected_sequence - 1,
        terminal_sha256=previous_sha256,
        maximum_rows_fetched=maximum_rows_fetched,
    )


def verify_projection_generation(
    conn: sqlite3.Connection,
    generation_id: str,
) -> ProjectionVerification:
    header = conn.execute(
        "SELECT generation_kind,entry_count,chunk_size,entry_set_sha256 "
        "FROM projection_generations WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    if header is None:
        raise BenchmarkIntegrityError("projection_generation_missing")
    generation_kind = str(header[0])
    if generation_kind not in {"checkpoint", "delta"}:
        raise BenchmarkIntegrityError("projection_generation_kind_tampered")
    expected_count = int(header[1])
    fetch_size = int(header[2])
    if fetch_size < 1 or fetch_size > MAX_FETCH_SIZE:
        raise BenchmarkIntegrityError("projection_fetch_bound_tampered")
    cursor = conn.execute(
        "SELECT entry_ordinal,change_kind,digest_bucket,coordinate,value_text,"
        "entry_json,entry_sha256 FROM projection_entries "
        "WHERE generation_id=? ORDER BY entry_ordinal",
        (generation_id,),
    )
    entry_set_sha256 = INITIAL_SHA256
    bucket_sha256 = [INITIAL_SHA256] * DIGEST_BUCKET_COUNT
    bucket_counts = [0] * DIGEST_BUCKET_COUNT
    expected_ordinal = 0
    maximum_rows_fetched = 0
    batch_ordinal = 0
    while True:
        rows = cursor.fetchmany(fetch_size)
        maximum_rows_fetched = max(maximum_rows_fetched, len(rows))
        if not rows:
            break
        batch_hashes: list[str] = []
        first_ordinal = expected_ordinal
        for row in rows:
            ordinal = int(row[0])
            change_kind = str(row[1])
            bucket = int(row[2])
            coordinate = str(row[3])
            value_text = str(row[4])
            entry_json = str(row[5])
            entry_sha256 = str(row[6])
            expected_json = _entry_payload(
                generation_id=generation_id,
                entry_ordinal=ordinal,
                coordinate=coordinate,
                value_text=value_text,
                digest_bucket=bucket,
            )
            if (
                ordinal != expected_ordinal
                or change_kind != "upsert"
                or coordinate != _fact_id(ordinal)
                or value_text != _fact_value(ordinal, delta=generation_kind == "delta")
                or bucket != _digest_bucket(coordinate)
                or entry_json != expected_json
                or entry_sha256 != digest_text(expected_json)
            ):
                raise BenchmarkIntegrityError("projection_entry_commitment_tampered")
            entry_set_sha256 = _chain(entry_set_sha256, entry_sha256)
            bucket_sha256[bucket] = _chain(
                bucket_sha256[bucket],
                entry_sha256,
            )
            bucket_counts[bucket] += 1
            batch_hashes.append(entry_sha256)
            expected_ordinal += 1
        batch = conn.execute(
            "SELECT first_entry_ordinal,last_entry_ordinal,entry_count,"
            "batch_sha256 FROM projection_batches "
            "WHERE generation_id=? AND batch_ordinal=?",
            (generation_id, batch_ordinal),
        ).fetchone()
        if batch is None or (
            int(batch[0]),
            int(batch[1]),
            int(batch[2]),
            str(batch[3]),
        ) != (
            first_ordinal,
            expected_ordinal - 1,
            len(rows),
            digest_text(canonical_json(batch_hashes)),
        ):
            raise BenchmarkIntegrityError("projection_batch_commitment_tampered")
        batch_ordinal += 1
    stored_batch_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM projection_batches WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0]
    )
    if stored_batch_count != batch_ordinal:
        raise BenchmarkIntegrityError("projection_batch_count_tampered")
    buckets = conn.execute(
        "SELECT digest_bucket,entry_count,bucket_sha256 "
        "FROM projection_buckets WHERE generation_id=? ORDER BY digest_bucket",
        (generation_id,),
    ).fetchall()
    if len(buckets) != DIGEST_BUCKET_COUNT:
        raise BenchmarkIntegrityError("projection_bucket_set_incomplete")
    for expected_bucket, row in enumerate(buckets):
        if (
            int(row[0]) != expected_bucket
            or int(row[1]) != bucket_counts[expected_bucket]
            or str(row[2]) != bucket_sha256[expected_bucket]
        ):
            raise BenchmarkIntegrityError("projection_bucket_commitment_tampered")
    if expected_ordinal != expected_count or entry_set_sha256 != str(header[3]):
        raise BenchmarkIntegrityError("projection_generation_seal_tampered")
    return ProjectionVerification(
        generation_id=generation_id,
        row_count=expected_ordinal,
        entry_set_sha256=entry_set_sha256,
        maximum_rows_fetched=maximum_rows_fetched,
    )


def verify_projection_bucket(
    conn: sqlite3.Connection,
    generation_id: str,
    digest_bucket: int,
    *,
    fetch_size: int,
) -> ProjectionVerification:
    if fetch_size < 1 or fetch_size > MAX_FETCH_SIZE:
        raise ValueError(f"fetch_size must be between 1 and {MAX_FETCH_SIZE}")
    if digest_bucket < 0 or digest_bucket >= DIGEST_BUCKET_COUNT:
        raise ValueError("digest_bucket is outside the 4096-bucket vector")
    stored = conn.execute(
        "SELECT entry_count,bucket_sha256 FROM projection_buckets "
        "WHERE generation_id=? AND digest_bucket=?",
        (generation_id, digest_bucket),
    ).fetchone()
    if stored is None:
        raise BenchmarkIntegrityError("projection_bucket_missing")
    cursor = conn.execute(
        "SELECT entry_ordinal,coordinate,value_text,entry_json,entry_sha256 "
        "FROM projection_entries WHERE generation_id=? AND digest_bucket=? "
        "ORDER BY entry_ordinal",
        (generation_id, digest_bucket),
    )
    bucket_sha256 = INITIAL_SHA256
    count = 0
    maximum_rows_fetched = 0
    while True:
        rows = cursor.fetchmany(fetch_size)
        maximum_rows_fetched = max(maximum_rows_fetched, len(rows))
        if not rows:
            break
        for row in rows:
            ordinal = int(row[0])
            coordinate = str(row[1])
            value_text = str(row[2])
            entry_json = str(row[3])
            entry_sha256 = str(row[4])
            expected_json = _entry_payload(
                generation_id=generation_id,
                entry_ordinal=ordinal,
                coordinate=coordinate,
                value_text=value_text,
                digest_bucket=digest_bucket,
            )
            if (
                coordinate != _fact_id(ordinal)
                or _digest_bucket(coordinate) != digest_bucket
                or entry_json != expected_json
                or entry_sha256 != digest_text(expected_json)
            ):
                raise BenchmarkIntegrityError("projection_bucket_entry_tampered")
            bucket_sha256 = _chain(bucket_sha256, entry_sha256)
            count += 1
    if count != int(stored[0]) or bucket_sha256 != str(stored[1]):
        raise BenchmarkIntegrityError("projection_bucket_commitment_tampered")
    return ProjectionVerification(
        generation_id=generation_id,
        row_count=count,
        entry_set_sha256=bucket_sha256,
        maximum_rows_fetched=maximum_rows_fetched,
    )


def _measure_reads(
    conn: sqlite3.Connection,
    config: BenchmarkConfig,
) -> tuple[BoundedReadResult, tuple[float, float, float, float]]:
    point_latencies: list[float] = []
    page_latencies: list[float] = []
    maximum_rows = 0
    stride = max(1, config.fact_count // config.read_samples)
    for sample in range(config.read_samples):
        ordinal = min(sample * stride, config.fact_count - 1)
        coordinate = _fact_id(ordinal)
        started = time.perf_counter_ns()
        point = conn.execute(
            """
            SELECT value_text FROM projection_entries
            WHERE generation_id=? AND coordinate=?
            UNION ALL
            SELECT checkpoint.value_text FROM projection_entries checkpoint
            WHERE checkpoint.generation_id=? AND checkpoint.coordinate=?
              AND NOT EXISTS (
                SELECT 1 FROM projection_entries delta
                WHERE delta.generation_id=? AND delta.coordinate=?
              )
            LIMIT 1
            """,
            (
                DELTA_GENERATION,
                coordinate,
                CHECKPOINT_GENERATION,
                coordinate,
                DELTA_GENERATION,
                coordinate,
            ),
        ).fetchall()
        point_latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        maximum_rows = max(maximum_rows, len(point))
        if len(point) != 1:
            raise BenchmarkIntegrityError("bounded_point_read_incomplete")
        expected_value = _fact_value(
            ordinal,
            delta=ordinal < config.delta_count,
        )
        if str(point[0][0]) != expected_value:
            raise BenchmarkIntegrityError("bounded_point_read_wrong_value")

        after_ordinal = min(
            sample * stride,
            max(0, config.fact_count - config.page_size - 1),
        )
        after_coordinate = "" if after_ordinal == 0 else _fact_id(after_ordinal - 1)
        started = time.perf_counter_ns()
        page = conn.execute(
            """
            SELECT coordinate,value_text FROM (
                SELECT coordinate,value_text FROM (
                    SELECT coordinate,value_text FROM projection_entries
                    WHERE generation_id=? AND coordinate>?
                    ORDER BY coordinate LIMIT ?
                )
                UNION ALL
                SELECT coordinate,value_text FROM (
                    SELECT checkpoint.coordinate,checkpoint.value_text
                    FROM projection_entries checkpoint
                    WHERE checkpoint.generation_id=?
                      AND checkpoint.coordinate>?
                      AND NOT EXISTS (
                          SELECT 1 FROM projection_entries delta
                          WHERE delta.generation_id=?
                            AND delta.coordinate=checkpoint.coordinate
                      )
                    ORDER BY checkpoint.coordinate LIMIT ?
                )
            )
            ORDER BY coordinate LIMIT ?
            """,
            (
                DELTA_GENERATION,
                after_coordinate,
                config.page_size,
                CHECKPOINT_GENERATION,
                after_coordinate,
                DELTA_GENERATION,
                config.page_size,
                config.page_size,
            ),
        ).fetchall()
        page_latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        maximum_rows = max(maximum_rows, len(page))
        coordinates = tuple(str(row[0]) for row in page)
        if len(page) > config.page_size or coordinates != tuple(sorted(coordinates)):
            raise BenchmarkIntegrityError("bounded_page_read_contract_failed")
    return (
        BoundedReadResult(
            point_query_count=config.read_samples,
            page_query_count=config.read_samples,
            maximum_rows_fetched_per_query=maximum_rows,
            configured_page_limit=config.page_size,
        ),
        (
            _percentile(point_latencies, 0.50),
            _percentile(point_latencies, 0.95),
            _percentile(page_latencies, 0.50),
            _percentile(page_latencies, 0.95),
        ),
    )


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def _row_counts(conn: sqlite3.Connection) -> BenchmarkRowCounts:
    def count(table: str, generation_id: str | None = None) -> int:
        if generation_id is None:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # nosec B608 -- trusted internal SQL shape; values remain bound
        else:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE generation_id=?",  # nosec B608 -- trusted internal SQL shape; values remain bound
                (generation_id,),
            ).fetchone()
        if row is None:
            raise BenchmarkIntegrityError("benchmark_count_query_failed")
        return int(row[0])

    return BenchmarkRowCounts(
        source_stream_events=count("source_stream_events"),
        checkpoint_entries=count("projection_entries", CHECKPOINT_GENERATION),
        delta_entries=count("projection_entries", DELTA_GENERATION),
        checkpoint_batches=count("projection_batches", CHECKPOINT_GENERATION),
        delta_batches=count("projection_batches", DELTA_GENERATION),
    )


def _production_row_counts(
    conn: sqlite3.Connection,
) -> BenchmarkRowCounts:
    def count(table: str, generation_id: str | None = None) -> int:
        if generation_id is None:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # nosec B608 -- trusted internal SQL shape; values remain bound
        else:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE generation_id=?",  # nosec B608 -- trusted internal SQL shape; values remain bound
                (generation_id,),
            ).fetchone()
        if row is None:
            raise BenchmarkIntegrityError("production_count_query_failed")
        return int(row[0])

    return BenchmarkRowCounts(
        source_stream_events=count("source_fact_publication_stream"),
        checkpoint_entries=count(
            "canonical_fact_projection_entries",
            PRODUCTION_CHECKPOINT_GENERATION,
        ),
        delta_entries=count(
            "canonical_fact_projection_entries",
            PRODUCTION_DELTA_GENERATION,
        ),
        checkpoint_batches=count(
            "canonical_fact_projection_batches",
            PRODUCTION_CHECKPOINT_GENERATION,
        ),
        delta_batches=count(
            "canonical_fact_projection_batches",
            PRODUCTION_DELTA_GENERATION,
        ),
    )


def _tamper_is_detected(conn: sqlite3.Connection) -> bool:
    conn.execute("SAVEPOINT benchmark_tamper_probe")
    try:
        conn.execute(
            "UPDATE projection_entries SET entry_sha256=? "
            "WHERE generation_id=? AND entry_ordinal=0",
            (INITIAL_SHA256, CHECKPOINT_GENERATION),
        )
        try:
            _verify_projection_entry(conn, CHECKPOINT_GENERATION, 0)
        except BenchmarkIntegrityError:
            return True
        return False
    finally:
        conn.execute("ROLLBACK TO SAVEPOINT benchmark_tamper_probe")
        conn.execute("RELEASE SAVEPOINT benchmark_tamper_probe")


def _verify_projection_entry(
    conn: sqlite3.Connection,
    generation_id: str,
    entry_ordinal: int,
) -> None:
    row = conn.execute(
        "SELECT generation.generation_kind,entry.change_kind,"
        "entry.digest_bucket,entry.coordinate,entry.value_text,"
        "entry.entry_json,entry.entry_sha256 "
        "FROM projection_entries entry "
        "JOIN projection_generations generation "
        "ON generation.generation_id=entry.generation_id "
        "WHERE entry.generation_id=? AND entry.entry_ordinal=?",
        (generation_id, entry_ordinal),
    ).fetchone()
    if row is None:
        raise BenchmarkIntegrityError("projection_entry_missing")
    generation_kind = str(row[0])
    coordinate = str(row[3])
    value_text = str(row[4])
    bucket = int(row[2])
    expected_json = _entry_payload(
        generation_id=generation_id,
        entry_ordinal=entry_ordinal,
        coordinate=coordinate,
        value_text=value_text,
        digest_bucket=bucket,
    )
    if (
        generation_kind not in {"checkpoint", "delta"}
        or str(row[1]) != "upsert"
        or coordinate != _fact_id(entry_ordinal)
        or value_text != _fact_value(entry_ordinal, delta=generation_kind == "delta")
        or bucket != _digest_bucket(coordinate)
        or str(row[5]) != expected_json
        or str(row[6]) != digest_text(expected_json)
    ):
        raise BenchmarkIntegrityError("projection_entry_commitment_tampered")


def _budget_results(
    budgets: BenchmarkBudgets,
    measurements: TimingMeasurements,
) -> tuple[BudgetResult, ...]:
    checks: tuple[tuple[str, Literal["<=", ">="], float, float], ...] = (
        (
            "max_total_seconds",
            "<=",
            measurements.total_wall_seconds,
            budgets.max_total_seconds,
        ),
        (
            "max_peak_python_memory_bytes",
            "<=",
            float(measurements.peak_python_memory_bytes),
            float(budgets.max_peak_python_memory_bytes),
        ),
        (
            "max_database_bytes",
            "<=",
            float(measurements.sqlite_file_bytes),
            float(budgets.max_database_bytes),
        ),
        (
            "min_stream_rows_per_second",
            ">=",
            measurements.stream_append.rows_per_second,
            budgets.min_stream_rows_per_second,
        ),
        (
            "min_projection_rows_per_second",
            ">=",
            measurements.checkpoint_projection.rows_per_second,
            budgets.min_projection_rows_per_second,
        ),
        (
            "max_point_p95_milliseconds",
            "<=",
            measurements.point_read_p95_milliseconds,
            budgets.max_point_p95_milliseconds,
        ),
        (
            "max_page_p95_milliseconds",
            "<=",
            measurements.page_read_p95_milliseconds,
            budgets.max_page_p95_milliseconds,
        ),
        (
            "max_full_audit_seconds",
            "<=",
            measurements.full_audit.wall_seconds,
            budgets.max_full_audit_seconds,
        ),
        (
            "max_bucket_audit_seconds",
            "<=",
            measurements.bucket_audit.wall_seconds,
            budgets.max_bucket_audit_seconds,
        ),
    )
    return tuple(
        BudgetResult(
            budget_name=name,
            operator=operator,
            actual=actual,
            threshold=threshold,
            passed=(actual <= threshold if operator == "<=" else actual >= threshold),
        )
        for name, operator, actual, threshold in checks
    )


def _production_budget_results(
    budgets: BenchmarkBudgets,
    measurements: ProductionMeasurements,
) -> tuple[BudgetResult, ...]:
    checks: tuple[tuple[str, Literal["<=", ">="], float, float], ...] = (
        (
            "max_total_seconds",
            "<=",
            measurements.total_wall_seconds,
            budgets.max_total_seconds,
        ),
        (
            "max_peak_python_memory_bytes",
            "<=",
            float(measurements.peak_python_memory_bytes),
            float(budgets.max_peak_python_memory_bytes),
        ),
        (
            "max_database_bytes",
            "<=",
            float(measurements.sqlite_file_bytes),
            float(budgets.max_database_bytes),
        ),
        (
            "min_stream_rows_per_second",
            ">=",
            measurements.stream_append.rows_per_second,
            budgets.min_stream_rows_per_second,
        ),
        (
            "min_projection_rows_per_second",
            ">=",
            measurements.checkpoint_projection.rows_per_second,
            budgets.min_projection_rows_per_second,
        ),
        (
            "max_point_p95_milliseconds",
            "<=",
            measurements.point_read_p95_milliseconds,
            budgets.max_point_p95_milliseconds,
        ),
        (
            "max_page_p95_milliseconds",
            "<=",
            measurements.page_read_p95_milliseconds,
            budgets.max_page_p95_milliseconds,
        ),
        (
            "max_full_audit_seconds",
            "<=",
            measurements.full_audit.wall_seconds,
            budgets.max_full_audit_seconds,
        ),
        (
            "max_bucket_audit_seconds",
            "<=",
            measurements.bounded_bucket_reads.wall_seconds,
            budgets.max_bucket_audit_seconds,
        ),
    )
    return tuple(
        BudgetResult(
            budget_name=name,
            operator=operator,
            actual=actual,
            threshold=threshold,
            passed=(actual <= threshold if operator == "<=" else actual >= threshold),
        )
        for name, operator, actual, threshold in checks
    )


def _migrate_production_database(database: Path) -> tuple[str, float]:
    started = time.perf_counter()
    conn = connect_sqlite(
        database,
        role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
    )
    try:
        conn.executescript(
            """
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY,
                source_doc_id INTEGER NOT NULL
            );
            CREATE TABLE kpi_facts (
                id INTEGER PRIMARY KEY,
                source_doc_id INTEGER NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
    root = Path(__file__).resolve().parents[2]
    base_revision = "0213_decision_draft_provider_id"
    # Build the in-process config explicitly so Alembic does not install its
    # console logger and mix non-JSON migration logs into the CLI's stderr.
    alembic_config = Config()
    alembic_config.set_main_option(
        "script_location",
        str(root / "alembic"),
    )
    alembic_config.set_main_option(
        "sqlalchemy.url",
        f"sqlite:///{database}",
    )
    command.stamp(alembic_config, base_revision)
    command.upgrade(alembic_config, "head")
    conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        if row is None:
            raise BenchmarkIntegrityError("production_migration_revision_missing")
        revision = str(row[0])
    finally:
        conn.close()
    return revision, time.perf_counter() - started


def _seed_production_identity(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        (
            "production-issuer",
            "production-issuer-key",
            "operating_company",
            PRODUCTION_STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "production-reporting-entity",
            "production-reporting-key",
            "production-issuer",
            "legal_registrant",
            "Production Benchmark Issuer",
            PRODUCTION_STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "production-subject-binding",
            "production-subject-binding-key",
            "production-issuer",
            1,
            "production-issuer",
            "production-reporting-entity",
            None,
            "selected",
            "deterministic",
            "exact_subject",
            "{}",
            0,
            PRODUCTION_STAMP,
            PRODUCTION_STAMP,
            PRODUCTION_STAMP,
            None,
        ),
    )
    conn.commit()


def _production_fact(
    global_ordinal: int,
    local_ordinal: int,
) -> NormalizedFilingXbrlFact:
    period_end = PRODUCTION_STAMP - timedelta(days=global_ordinal + 30)
    period_start = period_end - timedelta(days=365)
    locator: dict[str, JsonValue] = {
        "global_ordinal": global_ordinal,
        "path": f"/production/xbrl/{global_ordinal}",
    }
    locator_json = canonical_json(locator)
    numeric_value = Decimal(global_ordinal * 17 + 3)
    return NormalizedFilingXbrlFact(
        ordinal=local_ordinal,
        evidence_node_id=f"production-node-{global_ordinal:012d}",
        concept_namespace="https://fasb.org/us-gaap/2026",
        concept_name="Revenue",
        taxonomy_name="US GAAP",
        source_taxonomy_version="2026",
        accounting_basis="us_gaap",
        consolidation_scope="consolidated",
        period_kind="duration",
        period_start=period_start,
        period_end=period_end,
        fiscal_year=period_end.year,
        fiscal_period="FY",
        unit_key="iso4217:USD",
        currency="USD",
        value_kind="numeric",
        numeric_value=numeric_value,
        raw_lexical_value=str(numeric_value),
        source_context_id=f"production-context-{global_ordinal:012d}",
        source_unit_id=f"production-unit-{global_ordinal:012d}",
        decimals="-3",
        source_locator=locator,
        source_locator_sha256=digest_text(locator_json),
        source_entry_sha256=digest_text(f"production-entry-{global_ordinal}"),
        effective_at=period_end,
        knowledge_at=PRODUCTION_STAMP,
        recorded_at=PRODUCTION_STAMP,
    )


def _production_output(
    chunk_index: int,
    ordinal_range: range,
) -> FilingXbrlNormalizedOutput:
    entries = tuple(
        _production_fact(global_ordinal, local_ordinal)
        for local_ordinal, global_ordinal in enumerate(ordinal_range)
    )
    extraction = FilingXbrlExtractionIdentity(
        document_version_id=f"production-document-{chunk_index:08d}",
        extraction_run_id=f"production-extraction-{chunk_index:08d}",
        extractor_name="production-contract-benchmark",
        extractor_code_version="v1",
        extractor_config_sha256=digest_text("production-contract-benchmark-config-v1"),
        extraction_input_sha256=digest_text(f"production-filing-input-{chunk_index}"),
        extraction_output_sha256=INITIAL_SHA256,
        expected_evidence_node_count=len(entries),
        knowledge_at=PRODUCTION_STAMP,
        recorded_at=PRODUCTION_STAMP,
    )
    return FilingXbrlNormalizedOutput.with_computed_digest(
        extraction=extraction,
        subject=FilingXbrlSubjectIdentity(
            reporting_entity_id="production-reporting-entity",
            selected_subject_binding_revision_id=("production-subject-binding"),
        ),
        entries=entries,
    )


def _seed_production_extraction_evidence(
    conn: sqlite3.Connection,
    output: FilingXbrlNormalizedOutput,
    chunk_index: int,
) -> None:
    extraction = output.extraction
    period_starts = tuple(entry.period_start for entry in output.entries)
    if any(value is None for value in period_starts):
        raise BenchmarkIntegrityError("production_duration_period_start_missing")
    earliest_period_start = min(value for value in period_starts if value is not None)
    source_id = f"production-source-{chunk_index:08d}"
    blob_sha256 = extraction.extraction_input_sha256
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (
            blob_sha256,
            len(output.entries),
            "application/xhtml+xml",
            f"file:///production-benchmark/{chunk_index:08d}.xhtml",
            PRODUCTION_STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,"
        "source_published_at,filing_at,accepted_at,observed_at,retrieved_at,"
        "retrieval_config_sha256,collector_code_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            f"{source_id}-key",
            "sec_filing",
            (f"https://www.sec.gov/Archives/production-benchmark/{chunk_index:08d}.xhtml"),
            blob_sha256,
            PRODUCTION_STAMP,
            PRODUCTION_STAMP,
            PRODUCTION_STAMP,
            PRODUCTION_STAMP,
            PRODUCTION_STAMP,
            digest_text("production-retrieval-config"),
            "production-benchmark-v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id,document_key,version_sequence,observation_id,"
        "blob_sha256,issuer_id,ticker,document_type,form_type,"
        "accession_number,exhibit_id,period_start,period_end,as_of_at,"
        "language,replaces_document_version_id,legacy_document_id,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            extraction.document_version_id,
            f"production-document-key-{chunk_index:08d}",
            1,
            source_id,
            blob_sha256,
            "production-issuer",
            None,
            "regulatory_filing",
            "10-K",
            f"0000000001-26-{chunk_index:06d}",
            None,
            earliest_period_start,
            max(entry.period_end for entry in output.entries),
            max(entry.period_end for entry in output.entries),
            "en",
            None,
            None,
            PRODUCTION_STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            extraction.extraction_run_id,
            f"{extraction.extraction_run_id}-key",
            extraction.document_version_id,
            extraction.extraction_input_sha256,
            extraction.extractor_name,
            extraction.extractor_config_sha256,
            extraction.extractor_code_version,
            extraction.extraction_output_sha256,
            extraction.knowledge_at,
            extraction.recorded_at,
            "succeeded",
        ),
    )
    conn.executemany(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            (
                entry.evidence_node_id,
                f"{entry.evidence_node_id}-key",
                1,
                extraction.extraction_run_id,
                None,
                None,
                "table_cell",
                entry.raw_lexical_value,
                canonical_json(entry.source_locator),
                entry.source_locator_sha256,
                entry.recorded_at,
            )
            for entry in output.entries
        ),
    )


def _publish_production_facts(
    conn: sqlite3.Connection,
    config: ProductionBenchmarkConfig,
) -> tuple[PhaseMeasurement, int]:
    started = time.perf_counter()
    publication_count = 0
    ledger = FilingXbrlExtractionLedger(conn)
    for chunk_index, ordinal_range in enumerate(
        _chunks(config.fact_count, config.publication_chunk_size)
    ):
        output = _production_output(chunk_index, ordinal_range)
        _seed_production_extraction_evidence(conn, output, chunk_index)
        receipt = ledger.publish(output)
        if receipt.published_count != len(ordinal_range):
            raise BenchmarkIntegrityError("production_fact_publication_incomplete")
        conn.commit()
        publication_count += 1
    elapsed = time.perf_counter() - started
    return _phase(elapsed, config.fact_count), publication_count


def _append_production_empty_publications(
    conn: sqlite3.Connection,
    *,
    first_ordinal: int,
    target_event_count: int,
    commit_size: int,
) -> tuple[str, bool]:
    repository = SourceFactRepository(conn)
    last_publication: SourceFactPublication | None = None
    for ordinal in range(first_ordinal, target_event_count):
        publication = SourceFactPublication(
            publication_id=f"production-empty-{ordinal:012d}",
            idempotency_key=f"production-empty:{ordinal:012d}",
            created_at=PRODUCTION_STAMP,
            recorded_at=PRODUCTION_STAMP,
        )
        repository.publish(publication)
        last_publication = publication
        if (ordinal - first_ordinal + 1) % commit_size == 0:
            conn.commit()
    conn.commit()
    if last_publication is None:
        raise BenchmarkIntegrityError("production_exact_replay_publication_missing")
    replay = repository.publish(last_publication)
    conn.commit()
    return last_publication.publication_id, replay.exact_replay


def _production_component() -> SourceTaxonomyComponent:
    definition_qualifier_sha256 = digest_text(
        canonical_json(
            {
                "accounting_basis": "us_gaap",
                "concept_name": "Revenue",
                "concept_namespace": "https://fasb.org/us-gaap/2026",
                "consolidation_scope": "consolidated",
                "period_kind": "duration",
                "reporting_entity_id": "production-reporting-entity",
                "schema_version": "source-definition-identity/v1",
                "taxonomy_name": "US GAAP",
                "taxonomy_version": "2026",
                "unit_family": "currency",
                "value_kind": "numeric",
            }
        )
    )
    return SourceTaxonomyComponent(
        component_id="production-component:Revenue",
        idempotency_key="production-component:Revenue",
        component_kind="concept",
        taxonomy_namespace="https://fasb.org/us-gaap/2026",
        local_name="Revenue",
        taxonomy_name="US GAAP",
        taxonomy_version="2026",
        is_extension=False,
        data_type="monetaryItemType",
        period_type="duration",
        balance="credit",
        is_abstract=False,
        standard_label="Revenue",
        definition_text="Production benchmark revenue.",
        references=(),
        definition_qualifier_sha256=definition_qualifier_sha256,
        reporting_entity_id="production-reporting-entity",
        evidence_locator={"source": "production_contract_benchmark"},
        effective_at=PRODUCTION_STAMP,
        knowledge_at=PRODUCTION_STAMP,
        recorded_at=PRODUCTION_STAMP,
    )


def _production_mapping(
    component: SourceTaxonomyComponent,
) -> MappingRevision:
    return MappingRevision(
        mapping_revision_id="production-mapping:Revenue",
        idempotency_key="production-mapping:Revenue",
        source_component_id=component.component_id,
        metric_id="production-revenue",
        revision=1,
        disposition="equivalent",
        policy_name="production-benchmark",
        policy_version="v1",
        policy_config_sha256=digest_text("production-mapping-policy"),
        method_name="deterministic",
        method_version="v1",
        constraints={},
        evidence={"mode": "production_contract"},
        reviewer_identity="production-benchmark@example.test",
        effective_at=PRODUCTION_STAMP,
        knowledge_at=PRODUCTION_STAMP,
        recorded_at=PRODUCTION_STAMP,
    )


def _persist_production_taxonomy_assertion(
    conn: sqlite3.Connection,
    ontology: MetricOntology,
    *,
    fact_cell_id: str,
    observation_id: str,
) -> None:
    proof = conn.execute(
        "SELECT anchor.extraction_run_id,cell.taxonomy_name,"
        "anchor.source_taxonomy_version,cell_seal.semantic_key_sha256,"
        "anchor.anchor_payload_sha256,payload.observation_payload_sha256,"
        "run.output_sha256,anchor.raw_entry_sha256,"
        "completeness.observation_set_sha256 "
        "FROM fact_reported_observation_anchors_v2 anchor "
        "JOIN fact_cells_v2 cell ON cell.fact_cell_id=? "
        "JOIN fact_cell_identity_seals_v2 cell_seal "
        "ON cell_seal.fact_cell_id=cell.fact_cell_id "
        "JOIN fact_observation_payload_commitments_v2 payload "
        "ON payload.observation_id=anchor.observation_id "
        "JOIN evidence_extraction_runs run "
        "ON run.extraction_run_id=anchor.extraction_run_id "
        "JOIN fact_extraction_run_completeness_seals_v2 completeness "
        "ON completeness.extraction_run_id=anchor.extraction_run_id "
        "WHERE anchor.observation_id=?",
        (fact_cell_id, observation_id),
    ).fetchone()
    if proof is None:
        raise BenchmarkIntegrityError("production_taxonomy_proof_missing")
    ontology.persist_observation_taxonomy_assertion(
        SourceObservationTaxonomyAssertion(
            observation_id=observation_id,
            idempotency_key=f"production-taxonomy:{observation_id}",
            extraction_run_id=str(proof[0]),
            taxonomy_name=str(proof[1]),
            taxonomy_version=str(proof[2]),
            fact_cell_semantic_key_sha256=str(proof[3]),
            anchor_payload_sha256=str(proof[4]),
            observation_payload_sha256=str(proof[5]),
            extraction_output_sha256=str(proof[6]),
            raw_entry_sha256=str(proof[7]),
            observation_set_sha256=str(proof[8]),
            knowledge_at=PRODUCTION_STAMP,
            recorded_at=PRODUCTION_STAMP,
        )
    )


def _seed_production_ontology_and_resolutions(
    conn: sqlite3.Connection,
    config: ProductionBenchmarkConfig,
) -> PhaseMeasurement:
    started = time.perf_counter()
    ontology = MetricOntology(conn)
    ontology.persist_metric(
        CanonicalMetric(
            metric_id="production-revenue",
            idempotency_key="production-metric:revenue",
            canonical_name="Revenue",
            effective_at=PRODUCTION_STAMP,
            knowledge_at=PRODUCTION_STAMP,
            recorded_at=PRODUCTION_STAMP,
        )
    )
    ontology.persist_metric_definition(
        CanonicalMetricDefinitionRevision(
            metric_definition_revision_id="production-metric:revenue:v1",
            idempotency_key="production-metric:revenue:v1",
            metric_id="production-revenue",
            revision=1,
            lifecycle="active",
            definition_text="Revenue recognized from customer contracts.",
            aliases=("sales", "top line"),
            value_kind="numeric",
            period_kind="duration",
            unit_family="currency",
            accounting_basis="us_gaap",
            scope_constraints={},
            effective_at=PRODUCTION_STAMP,
            knowledge_at=PRODUCTION_STAMP,
            recorded_at=PRODUCTION_STAMP,
        )
    )
    component = _production_component()
    mapping = _production_mapping(component)
    ontology.persist_source_component(component)
    ontology.persist_mapping(mapping)
    source_cursor = conn.execute(
        "SELECT cell.fact_cell_id,cell.period_start,cell.period_end,"
        "observation.observation_id "
        "FROM fact_cells_v2 cell "
        "JOIN fact_observations_v2 observation "
        "ON observation.fact_cell_id=cell.fact_cell_id "
        "JOIN filing_xbrl_extraction_dispositions disposition "
        "ON disposition.observation_id=observation.observation_id "
        "WHERE disposition.disposition='published' "
        "ORDER BY cell.period_end DESC,cell.fact_cell_id"
    )
    ordinal = 0
    while True:
        rows = source_cursor.fetchmany(config.publication_chunk_size)
        if not rows:
            break
        for row in rows:
            fact_cell_id = str(row[0])
            observation_id = str(row[3])
            canonical_cell_id = f"production-canonical:{ordinal:012d}"
            _persist_production_taxonomy_assertion(
                conn,
                ontology,
                fact_cell_id=fact_cell_id,
                observation_id=observation_id,
            )
            ontology.persist_canonical_metric_cell(
                CanonicalMetricCell(
                    canonical_metric_cell_id=canonical_cell_id,
                    idempotency_key=canonical_cell_id,
                    metric_id="production-revenue",
                    reporting_entity_id="production-reporting-entity",
                    period_kind="duration",
                    period_start=datetime.fromisoformat(str(row[1])),
                    period_end=datetime.fromisoformat(str(row[2])),
                    unit_family="currency",
                    accounting_basis="us_gaap",
                    consolidation_scope="consolidated",
                    effective_at=PRODUCTION_STAMP,
                    knowledge_at=PRODUCTION_STAMP,
                    recorded_at=PRODUCTION_STAMP,
                )
            )
            ontology.persist_binding(
                BindingRevision(
                    binding_revision_id=(f"production-binding:{ordinal:012d}"),
                    idempotency_key=(f"production-binding:{ordinal:012d}"),
                    fact_cell_id=fact_cell_id,
                    source_observation_id=observation_id,
                    revision=1,
                    canonical_metric_cell_id=canonical_cell_id,
                    mapping_revision_id=mapping.mapping_revision_id,
                    source_component_id=component.component_id,
                    effective_at=PRODUCTION_STAMP,
                    knowledge_at=PRODUCTION_STAMP,
                    recorded_at=PRODUCTION_STAMP,
                )
            )
            ordinal += 1
        conn.commit()
    if ordinal != config.fact_count:
        raise BenchmarkIntegrityError("production_source_fact_count_invariant_failed")
    resolver = CanonicalFactResolutionEngine(conn)
    resolution_cursor = conn.execute(
        "SELECT canonical_metric_cell_id FROM canonical_metric_cells "
        "WHERE metric_id='production-revenue' "
        "ORDER BY canonical_metric_cell_id"
    )
    resolved = 0
    while True:
        rows = resolution_cursor.fetchmany(config.publication_chunk_size)
        if not rows:
            break
        for row in rows:
            receipt = resolver.resolve(
                str(row[0]),
                PRODUCTION_STAMP,
                ResolutionPolicy(
                    name="production-benchmark",
                    version="v1",
                    config={},
                ),
                recorded_at=PRODUCTION_STAMP,
            )
            if receipt.status != "resolved":
                raise BenchmarkIntegrityError("production_canonical_resolution_failed")
            resolved += 1
        conn.commit()
    ontology.seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="production-ontology-checkpoint",
            idempotency_key="production-ontology-checkpoint",
            cutoff_at=PRODUCTION_STAMP,
            recorded_at=PRODUCTION_STAMP,
        )
    )
    resolver.seal_snapshot(
        "production-resolution-checkpoint",
        PRODUCTION_STAMP,
        PRODUCTION_STAMP,
        ResolutionSnapshotScope(
            issuer_id="production-issuer",
            reporting_entity_ids=("production-reporting-entity",),
        ),
    )
    conn.commit()
    if resolved != config.fact_count:
        raise BenchmarkIntegrityError("production_resolution_count_invariant_failed")
    return _phase(time.perf_counter() - started, resolved)


def _replay_production_stream(
    conn: sqlite3.Connection,
    *,
    event_count: int,
    page_size: int,
) -> tuple[str, AuditMeasurement, bool]:
    started = time.perf_counter()
    cursor = PublicationCursor.initial()
    replayed = 0
    maximum_rows = 0
    while True:
        page = read_publication_page(
            conn,
            after=cursor,
            through_sequence=event_count,
            limit=page_size,
        )
        replayed += len(page.events)
        maximum_rows = max(maximum_rows, len(page.events))
        cursor = page.next_cursor
        if not page.has_more:
            break
    elapsed = time.perf_counter() - started
    return (
        cursor.event_sha256,
        AuditMeasurement(
            wall_seconds=elapsed,
            verified_rows=replayed,
            maximum_rows_fetched=maximum_rows,
        ),
        replayed == event_count and cursor.publication_sequence == event_count,
    )


def _measure_production_search_reads(
    conn: sqlite3.Connection,
    config: ProductionBenchmarkConfig,
) -> tuple[BoundedReadResult, tuple[float, float, float, float], float]:
    point_latencies: list[float] = []
    page_latencies: list[float] = []
    maximum_rows = 0
    started_all = time.perf_counter()
    for _ in range(config.read_samples):
        started = time.perf_counter_ns()
        point = search_canonical_facts(
            conn,
            generation_id=PRODUCTION_DELTA_GENERATION,
            query_text="Revenue",
            limit=1,
        )
        point_latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        maximum_rows = max(maximum_rows, len(point))
        started = time.perf_counter_ns()
        page = search_canonical_facts(
            conn,
            generation_id=PRODUCTION_DELTA_GENERATION,
            query_text="Revenue",
            limit=config.page_size,
        )
        page_latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        maximum_rows = max(maximum_rows, len(page))
    elapsed = time.perf_counter() - started_all
    if maximum_rows > config.page_size:
        raise BenchmarkIntegrityError("production_bounded_search_limit_failed")
    return (
        BoundedReadResult(
            point_query_count=config.read_samples,
            page_query_count=config.read_samples,
            maximum_rows_fetched_per_query=maximum_rows,
            configured_page_limit=config.page_size,
        ),
        (
            _percentile(point_latencies, 0.50),
            _percentile(point_latencies, 0.95),
            _percentile(page_latencies, 0.50),
            _percentile(page_latencies, 0.95),
        ),
        elapsed,
    )


def _validate_database_path(database_path: Path) -> Path:
    resolved = database_path.resolve()
    live = Path(__file__).resolve().parents[2] / "data" / "portfolio.db"
    if resolved == live.resolve():
        raise RefusedBenchmarkPathError("benchmark refuses the live portfolio database path")
    if resolved.exists():
        raise RefusedBenchmarkPathError("benchmark database must be a new, isolated path")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def run_benchmark(
    *,
    config: BenchmarkConfig,
    budgets: BenchmarkBudgets,
    database_path: Path,
) -> BenchmarkReport:
    """Build, verify, and measure one isolated synthetic benchmark database."""

    database = _validate_database_path(database_path)
    total_started = time.perf_counter()
    tracemalloc.start()
    conn = connect_sqlite(
        database,
        role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
    )
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        _create_schema(conn)

        stream_measurement = _append_stream(conn, config)
        checkpoint_measurement = _build_projection(
            conn,
            config,
            generation_id=CHECKPOINT_GENERATION,
            generation_kind="checkpoint",
            parent_generation_id=None,
            entry_count=config.fact_count,
        )
        delta_measurement = _build_projection(
            conn,
            config,
            generation_id=DELTA_GENERATION,
            generation_kind="delta",
            parent_generation_id=CHECKPOINT_GENERATION,
            entry_count=config.delta_count,
        )

        stream_first = verify_stream_chain(conn, fetch_size=config.chunk_size)
        full_started = time.perf_counter()
        checkpoint_first = verify_projection_generation(
            conn,
            CHECKPOINT_GENERATION,
        )
        full_seconds = time.perf_counter() - full_started
        delta_first = verify_projection_generation(conn, DELTA_GENERATION)
        row_counts = _row_counts(conn)
        row_invariants = (
            row_counts.source_stream_events == config.fact_count
            and row_counts.checkpoint_entries == config.fact_count
            and row_counts.delta_entries == config.delta_count
            and row_counts.checkpoint_batches == math.ceil(config.fact_count / config.chunk_size)
            and row_counts.delta_batches == math.ceil(config.delta_count / config.chunk_size)
        )
        correctness = CorrectnessPreflight(
            exact_stream_replay=(
                stream_first.row_count == config.fact_count
                and stream_first.terminal_sha256 != INITIAL_SHA256
            ),
            exact_checkpoint_replay=(checkpoint_first.row_count == config.fact_count),
            exact_delta_replay=(delta_first.row_count == config.delta_count),
            tamper_detected=_tamper_is_detected(conn),
            row_count_invariants=row_invariants,
        )
        if not all(correctness.model_dump().values()):
            raise BenchmarkIntegrityError("correctness_preflight_failed")

        bounded_reads, latency = _measure_reads(conn, config)

        audited_bucket = _digest_bucket(_fact_id(0))
        bucket_started = time.perf_counter()
        bucket = verify_projection_bucket(
            conn,
            CHECKPOINT_GENERATION,
            audited_bucket,
            fetch_size=config.chunk_size,
        )
        bucket_seconds = time.perf_counter() - bucket_started
        bucket_row = conn.execute(
            "SELECT bucket_sha256 FROM projection_buckets "
            "WHERE generation_id=? AND digest_bucket=?",
            (CHECKPOINT_GENERATION, audited_bucket),
        ).fetchone()
        if bucket_row is None:
            raise BenchmarkIntegrityError("projection_bucket_missing")
        conn.commit()
        _, peak_memory = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        conn.close()

    total_seconds = time.perf_counter() - total_started
    measurements = TimingMeasurements(
        total_wall_seconds=total_seconds,
        stream_append=stream_measurement,
        checkpoint_projection=checkpoint_measurement,
        delta_projection=delta_measurement,
        full_audit=AuditMeasurement(
            wall_seconds=full_seconds,
            verified_rows=checkpoint_first.row_count,
            maximum_rows_fetched=checkpoint_first.maximum_rows_fetched,
        ),
        bucket_audit=AuditMeasurement(
            wall_seconds=bucket_seconds,
            verified_rows=bucket.row_count,
            maximum_rows_fetched=bucket.maximum_rows_fetched,
        ),
        point_read_p50_milliseconds=latency[0],
        point_read_p95_milliseconds=latency[1],
        page_read_p50_milliseconds=latency[2],
        page_read_p95_milliseconds=latency[3],
        peak_python_memory_bytes=peak_memory,
        sqlite_file_bytes=database.stat().st_size,
    )
    config_json = canonical_json(config.model_dump(mode="json"))
    budget_results = _budget_results(budgets, measurements)
    payload: dict[str, object] = {
        "report_version": "data_infrastructure_benchmark.v1",
        "config": config.model_dump(mode="json"),
        "budgets": budgets.model_dump(mode="json"),
        "config_sha256": digest_text(config_json),
        "deterministic_commitments": DeterministicCommitments(
            stream_terminal_sha256=stream_first.terminal_sha256,
            checkpoint_entry_set_sha256=checkpoint_first.entry_set_sha256,
            delta_entry_set_sha256=delta_first.entry_set_sha256,
            audited_digest_bucket=audited_bucket,
            audited_bucket_sha256=str(bucket_row[0]),
        ).model_dump(mode="json"),
        "correctness": correctness.model_dump(mode="json"),
        "row_counts": row_counts.model_dump(mode="json"),
        "bounded_reads": bounded_reads.model_dump(mode="json"),
        "measurements": measurements.model_dump(mode="json"),
        "environment": EnvironmentVersions(
            python=platform.python_version(),
            sqlite=sqlite3.sqlite_version,
            pydantic=pydantic.__version__,
            platform=platform.platform(),
        ).model_dump(mode="json"),
        "budget_results": [result.model_dump(mode="json") for result in budget_results],
        "overall_pass": all(result.passed for result in budget_results),
    }
    payload["report_sha256"] = digest_text(canonical_json(payload))
    return BenchmarkReport.model_validate(payload)


def run_production_contract_benchmark(
    *,
    config: ProductionBenchmarkConfig,
    budgets: BenchmarkBudgets,
    database_path: Path,
) -> ProductionContractBenchmarkReport:
    """Measure the real 0246/0247 APIs on a new migrated SQLite database."""

    database = _validate_database_path(database_path)
    total_started = time.perf_counter()
    tracemalloc.start()
    try:
        migration_revision, migration_seconds = _migrate_production_database(database)
        conn = connect_sqlite(
            database,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=NORMAL")
            _seed_production_identity(conn)
            stream_started = time.perf_counter()
            source_measurement, fact_publication_count = _publish_production_facts(conn, config)
            _, exact_publication_replay = _append_production_empty_publications(
                conn,
                first_ordinal=fact_publication_count,
                target_event_count=config.fact_count,
                commit_size=config.publication_chunk_size,
            )
            stream_measurement = _phase(
                time.perf_counter() - stream_started,
                config.fact_count,
            )
            ontology_measurement = _seed_production_ontology_and_resolutions(conn, config)
            bind_resolution_snapshot_watermark(
                conn,
                resolution_snapshot_id="production-resolution-checkpoint",
                cutoff_at=PRODUCTION_STAMP,
                recorded_at=PRODUCTION_STAMP,
            )
            checkpoint_request = ProjectionGenerationRequest(
                generation_id=PRODUCTION_CHECKPOINT_GENERATION,
                idempotency_key=PRODUCTION_CHECKPOINT_GENERATION,
                generation_kind="checkpoint",
                resolution_snapshot_id=("production-resolution-checkpoint"),
                ontology_snapshot_id="production-ontology-checkpoint",
                cutoff_at=PRODUCTION_STAMP,
                recorded_at=PRODUCTION_STAMP,
                config=ProjectionConfig(max_batch_facts=config.publication_chunk_size),
            )
            checkpoint_started = time.perf_counter()
            checkpoint = build_canonical_projection_generation(
                conn,
                checkpoint_request,
            )
            checkpoint_measurement = _phase(
                time.perf_counter() - checkpoint_started,
                checkpoint.change_count,
            )
            later = PRODUCTION_STAMP + timedelta(hours=1)
            ontology = MetricOntology(conn)
            ontology.seal_snapshot(
                OntologySnapshot(
                    ontology_snapshot_id="production-ontology-delta",
                    idempotency_key="production-ontology-delta",
                    cutoff_at=later,
                    recorded_at=later,
                )
            )
            resolver = CanonicalFactResolutionEngine(conn)
            resolver.seal_snapshot(
                "production-resolution-delta",
                later,
                later,
                ResolutionSnapshotScope(
                    issuer_id="production-issuer",
                    reporting_entity_ids=("production-reporting-entity",),
                ),
            )
            bind_resolution_snapshot_watermark(
                conn,
                resolution_snapshot_id="production-resolution-delta",
                cutoff_at=later,
                recorded_at=later,
            )
            delta_request = ProjectionGenerationRequest(
                generation_id=PRODUCTION_DELTA_GENERATION,
                idempotency_key=PRODUCTION_DELTA_GENERATION,
                generation_kind="delta",
                parent_generation_id=PRODUCTION_CHECKPOINT_GENERATION,
                resolution_snapshot_id="production-resolution-delta",
                ontology_snapshot_id="production-ontology-delta",
                cutoff_at=later,
                recorded_at=later,
                config=ProjectionConfig(max_batch_facts=config.publication_chunk_size),
            )
            delta_started = time.perf_counter()
            delta = build_canonical_projection_generation(
                conn,
                delta_request,
            )
            delta_measurement = _phase(
                time.perf_counter() - delta_started,
                delta.change_count,
            )
            (
                stream_terminal_sha256,
                stream_replay,
                exact_stream_replay,
            ) = _replay_production_stream(
                conn,
                event_count=config.fact_count,
                page_size=config.page_size,
            )
            full_audit_started = time.perf_counter()
            tracked_checkpoint = _FetchTrackingConnection(conn)
            strict_checkpoint = verify_canonical_projection_generation(
                cast(sqlite3.Connection, tracked_checkpoint),
                PRODUCTION_CHECKPOINT_GENERATION,
                resolution_snapshot_id="production-resolution-checkpoint",
                ontology_snapshot_id="production-ontology-checkpoint",
                cutoff_at=PRODUCTION_STAMP,
            )
            full_audit_seconds = time.perf_counter() - full_audit_started
            tracked_delta = _FetchTrackingConnection(conn)
            strict_delta = verify_canonical_projection_generation(
                cast(sqlite3.Connection, tracked_delta),
                PRODUCTION_DELTA_GENERATION,
                resolution_snapshot_id="production-resolution-delta",
                ontology_snapshot_id="production-ontology-delta",
                cutoff_at=later,
            )
            bounded_reads, latency, bounded_read_seconds = _measure_production_search_reads(
                conn, config
            )
            row_counts = _production_row_counts(conn)
            row_invariants = (
                row_counts.source_stream_events == config.fact_count
                and row_counts.checkpoint_entries == config.fact_count
                and row_counts.delta_entries == 0
            )
            correctness = ProductionCorrectnessPreflight(
                publication_exact_replay=exact_publication_replay,
                stream_page_replay=exact_stream_replay,
                strict_checkpoint_audit=(
                    strict_checkpoint.effective_entry_count == config.fact_count
                ),
                strict_delta_audit=(
                    strict_delta.effective_entry_count == config.fact_count
                    and strict_delta.change_count == 0
                ),
                bounded_search_reads=(
                    bounded_reads.maximum_rows_fetched_per_query <= config.page_size
                ),
                row_count_invariants=row_invariants,
            )
            if not all(correctness.model_dump().values()):
                raise BenchmarkIntegrityError("production_correctness_preflight_failed")
            conn.commit()
            _, peak_memory = tracemalloc.get_traced_memory()
        finally:
            conn.close()
    finally:
        tracemalloc.stop()
    total_seconds = time.perf_counter() - total_started
    measurements = ProductionMeasurements(
        total_wall_seconds=total_seconds,
        migration_wall_seconds=migration_seconds,
        source_fact_publication=source_measurement,
        stream_append=stream_measurement,
        ontology_resolution=ontology_measurement,
        checkpoint_projection=checkpoint_measurement,
        delta_projection=delta_measurement,
        stream_replay=stream_replay,
        full_audit=AuditMeasurement(
            wall_seconds=full_audit_seconds,
            verified_rows=strict_checkpoint.effective_entry_count,
            maximum_rows_fetched=max(
                tracked_checkpoint.maximum_rows_returned,
                tracked_delta.maximum_rows_returned,
            ),
        ),
        bounded_bucket_reads=AuditMeasurement(
            wall_seconds=bounded_read_seconds,
            verified_rows=(bounded_reads.point_query_count + bounded_reads.page_query_count),
            maximum_rows_fetched=(bounded_reads.maximum_rows_fetched_per_query),
        ),
        point_read_p50_milliseconds=latency[0],
        point_read_p95_milliseconds=latency[1],
        page_read_p50_milliseconds=latency[2],
        page_read_p95_milliseconds=latency[3],
        peak_python_memory_bytes=peak_memory,
        sqlite_file_bytes=database.stat().st_size,
    )
    budget_results = _production_budget_results(budgets, measurements)
    config_json = canonical_json(config.model_dump(mode="json"))
    payload: dict[str, object] = {
        "report_version": ("data_infrastructure_production_contract.v1"),
        "benchmark_mode": "production_contract",
        "scale_interpretation": ("measured_only_no_extrapolation_proof"),
        "production_fact_cap": MAX_PRODUCTION_FACT_COUNT,
        "config": config.model_dump(mode="json"),
        "budgets": budgets.model_dump(mode="json"),
        "config_sha256": digest_text(config_json),
        "migration_revision": migration_revision,
        "production_apis": (
            "provenance.source_fact_stream.read_publication_page",
            "search.canonical_fact_projection.build_canonical_projection_generation",
            "search.canonical_fact_projection.search_canonical_facts",
            "search.canonical_fact_projection.verify_canonical_projection_generation",
        ),
        "build_time_materialization": (
            "_write_entries_and_batches buffers at most 1000 facts or "
            "16 MiB for one configured projection batch",
            "_write_buckets_and_seal retains only the changed-bucket id set, "
            "bounded by the fixed 4096 digest buckets",
            "_checkpoint_bucket_commitments and "
            "_effective_bucket_commitment materialize one canonical bucket "
            "payload at a time, capped at 250000 entries and 16 MiB",
            "_seal_payloads materializes the fixed 4096-bucket logical "
            "commitment vector and a batch vector capped at 250000 batches "
            "and 64 MiB",
        ),
        "production_limitations": (
            "production mode is capped at 100000 facts",
            "delta smoke measures a sealed zero-change generation",
            "larger-scale extrapolation is not proof",
        ),
        "deterministic_commitments": (
            ProductionDeterministicCommitments(
                stream_terminal_sha256=stream_terminal_sha256,
                checkpoint_projection_sha256=(strict_checkpoint.projection_seal_sha256),
                delta_projection_sha256=(strict_delta.projection_seal_sha256),
            ).model_dump(mode="json")
        ),
        "correctness": correctness.model_dump(mode="json"),
        "row_counts": row_counts.model_dump(mode="json"),
        "bounded_reads": bounded_reads.model_dump(mode="json"),
        "measurements": measurements.model_dump(mode="json"),
        "environment": environment_versions().model_dump(mode="json"),
        "budget_results": [result.model_dump(mode="json") for result in budget_results],
        "overall_pass": all(result.passed for result in budget_results),
    }
    payload["report_sha256"] = digest_text(canonical_json(payload))
    return ProductionContractBenchmarkReport.model_validate(payload)


def write_report_atomic(
    report: BenchmarkReport | ProductionContractBenchmarkReport,
    output_path: Path,
) -> None:
    """Atomically replace one canonical JSON report in its target directory."""

    output = output_path.resolve()
    live = Path(__file__).resolve().parents[2] / "data" / "portfolio.db"
    if output == live.resolve():
        raise RefusedBenchmarkPathError(
            "benchmark report cannot replace the live portfolio database"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(report.model_dump(mode="json")) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def verify_report_sha256(
    report: BenchmarkReport | ProductionContractBenchmarkReport,
) -> bool:
    payload = report.model_dump(mode="json")
    stored = str(payload.pop("report_sha256"))
    return stored == digest_text(canonical_json(payload))


def environment_versions() -> EnvironmentVersions:
    """Expose versions for CLI diagnostics without opening any database."""

    return EnvironmentVersions(
        python=sys.version.split()[0],
        sqlite=sqlite3.sqlite_version,
        pydantic=pydantic.__version__,
        platform=platform.platform(),
    )
