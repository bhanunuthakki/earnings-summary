"""Opt-in local vector indexing with immutable evidence provenance.

The SQL ledger remains the source of truth.  LanceDB only stores a projection
for one immutable index run, and this module never installs packages, downloads
models, or selects a default model.  Callers must explicitly opt in.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
import os
import platform
import sqlite3
import struct
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.issuer_registry import evidence_document_relation
from provenance.search_index_lineage import (
    SearchProjectionSeal,
    load_projection_seal,
    manifest_chunk_commitment,
    persist_projection_seal,
    vector_artifact_commitment,
    verify_ledger_projection_seal,
)
from search.embedding_runtime_artifact import (
    EmbeddingRuntimeArtifact,
    RuntimeArtifactSource,
    verify_runtime_artifact,
)
from search.grounded import (
    EmbeddingArtifact,
    GroundedSearchStore,
    IndexMembership,
    IndexRun,
    SearchFilter,
    VectorCandidate,
)


class LocalVectorCapabilityError(RuntimeError):
    """Raised when the explicitly requested local-vector capability is absent."""


def _lower_sha256(value: str, *, field_name: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def require_fastembed(importer: Callable[[str], object] = importlib.import_module) -> object:
    """Load FastEmbed only when an explicit vector action needs it."""
    try:
        return importer("fastembed")
    except ImportError as exc:
        raise LocalVectorCapabilityError(
            "fastembed is required for local semantic search; install the search extra"
        ) from exc


def require_lancedb(importer: Callable[[str], object] = importlib.import_module) -> object:
    """Load LanceDB only when an explicit vector action needs it."""
    try:
        return importer("lancedb")
    except ImportError as exc:
        raise LocalVectorCapabilityError(
            "lancedb is required for local semantic search; install the search extra"
        ) from exc


class EmbeddingModelSpec(BaseModel):
    """A fully specified local embedding model; routing is deliberately external."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    dimensions: int = Field(gt=0)


class VectorBuildRequest(BaseModel):
    """One immutable index projection request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index_run_id: str = Field(min_length=1, max_length=128)
    index_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    manifest_id: str = Field(min_length=1, max_length=128)
    code_version: str = Field(min_length=1, max_length=255)
    request_config_sha256: str = Field(min_length=64, max_length=64)
    model: EmbeddingModelSpec
    runtime_artifact: EmbeddingRuntimeArtifact
    purpose: str = Field(default="passage", min_length=1, max_length=64)
    batch_size: int = Field(default=128, gt=0, le=1000)
    started_at: datetime

    @field_validator("request_config_sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        return _lower_sha256(value, field_name="request_config_sha256")

    @model_validator(mode="after")
    def _runtime_coordinate(self) -> VectorBuildRequest:
        if (
            self.runtime_artifact.provider,
            self.runtime_artifact.model,
            self.runtime_artifact.dimensions,
        ) != (self.model.provider, self.model.model, self.model.dimensions):
            raise ValueError("vector request model differs from runtime artifact")
        return self


class VectorDocument(BaseModel):
    """A chunk and the exact metadata needed for prefiltered retrieval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    input_sha256: str = Field(min_length=64, max_length=64)
    manifest_id: str = Field(min_length=1, max_length=128)
    issuer_id: str = Field(min_length=1, max_length=128)
    recorded_issuer_id: str | None = Field(default=None, min_length=1, max_length=128)
    ticker: str | None = Field(default=None, max_length=32)
    form_type: str = Field(min_length=1, max_length=64)
    period_start: str | None = None
    period_end: str | None = None
    node_kind: str = Field(min_length=1, max_length=64)
    available_at: datetime
    observed_at: datetime
    retrieved_at: datetime

    @field_validator("input_sha256")
    @classmethod
    def _input_sha(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("input_sha256 must be a lowercase SHA-256 digest")
        return value


class VectorBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index_run_id: str
    outcome: str
    created: bool
    chunk_count: int = Field(ge=0)
    failure_reason: str | None = None


class VectorBatchReceipt(BaseModel):
    """Hash commitment to one deterministic, externally staged vector batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_number: int = Field(ge=1)
    first_chunk_id: str = Field(min_length=1, max_length=128)
    last_chunk_id: str = Field(min_length=1, max_length=128)
    chunk_count: int = Field(ge=1)
    records_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("records_sha256")
    @classmethod
    def _records_sha(cls, value: str) -> str:
        return _lower_sha256(value, field_name="records_sha256")


class VectorBuildCheckpoint(BaseModel):
    """Durable exact-resume state; it never stores model input text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_identity_sha256: str = Field(min_length=64, max_length=64)
    total_chunks: int = Field(ge=0)
    started_at: datetime
    receipts: tuple[VectorBatchReceipt, ...] = ()
    published: bool = False
    storage_uri: str | None = None
    published_at: datetime | None = None

    @field_validator("request_identity_sha256")
    @classmethod
    def _identity_sha(cls, value: str) -> str:
        return _lower_sha256(value, field_name="request_identity_sha256")

    @model_validator(mode="after")
    def _publication_contract(self) -> VectorBuildCheckpoint:
        has_publication = self.storage_uri is not None and self.published_at is not None
        if self.published != has_publication:
            raise ValueError("published checkpoint requires URI and publication clock")
        if not self.published and (self.storage_uri is not None or self.published_at is not None):
            raise ValueError("unpublished checkpoint cannot claim publication metadata")
        completed = sum(receipt.chunk_count for receipt in self.receipts)
        if completed > self.total_chunks:
            raise ValueError("checkpoint receipts exceed total chunk count")
        if [receipt.batch_number for receipt in self.receipts] != list(
            range(1, len(self.receipts) + 1)
        ):
            raise ValueError("checkpoint batch numbers must be contiguous")
        for previous, current in zip(self.receipts, self.receipts[1:], strict=False):
            if previous.last_chunk_id >= current.first_chunk_id:
                raise ValueError("checkpoint chunk ranges must be strictly ordered")
        if self.published and completed != self.total_chunks:
            raise ValueError("published checkpoint must cover every chunk")
        return self


class VectorBuildCheckpointStore:
    """Atomic JSON checkpoint storage scoped to one already-safe run path."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load_or_create(
        self,
        *,
        request_identity_sha256: str,
        total_chunks: int,
        started_at: datetime,
    ) -> VectorBuildCheckpoint:
        if not self.path.exists():
            state = VectorBuildCheckpoint(
                request_identity_sha256=request_identity_sha256,
                total_chunks=total_chunks,
                started_at=started_at,
            )
            self.save(state)
            return state
        state = VectorBuildCheckpoint.model_validate_json(self.path.read_text(encoding="utf-8"))
        if (
            state.request_identity_sha256 != request_identity_sha256
            or state.total_chunks != total_chunks
        ):
            raise ValueError("vector checkpoint conflicts with immutable build request")
        return state

    def save(self, state: VectorBuildCheckpoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(state.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)


class PassageQueryEncoder(Protocol):
    """Separate passage/query methods prevent accidental symmetric BGE encoding."""

    def encode_passages(self, texts: list[str]) -> list[list[float]]: ...

    def encode_queries(self, texts: list[str]) -> list[list[float]]: ...


class StagedVectorIndex(Protocol):
    """Bounded external projection with an atomic staging/publication boundary."""

    def stage_batch(
        self,
        index_run_id: str,
        records: list[dict[str, object]],
    ) -> None: ...

    def verify_batch(
        self,
        index_run_id: str,
        receipt: VectorBatchReceipt,
    ) -> bool: ...

    def read_batch(
        self,
        index_run_id: str,
        receipt: VectorBatchReceipt,
    ) -> list[dict[str, object]]: ...

    def count_records(self, index_run_id: str) -> int: ...

    def read_projection(
        self, index_run_id: str, *, expected_count: int
    ) -> list[dict[str, object]]: ...

    def publish(self, index_run_id: str) -> str: ...


class _LanceSearch(Protocol):
    def where(self, predicate: str, *, prefilter: bool) -> _LanceSearch: ...

    def limit(self, limit: int) -> _LanceSearch: ...

    def to_list(self) -> list[dict[str, object]]: ...


class _LancePlainQuery(Protocol):
    def where(self, predicate: str) -> _LancePlainQuery: ...

    def limit(self, limit: int) -> _LancePlainQuery: ...

    def to_list(self) -> list[dict[str, object]]: ...


class _LanceTable(Protocol):
    def search(self, vector: list[float]) -> _LanceSearch: ...

    def query(self) -> _LancePlainQuery: ...

    def add(self, data: list[dict[str, object]]) -> None: ...

    def count_rows(self, where_filter: str | None = None) -> int: ...


class _LanceDatabase(Protocol):
    def table_names(self) -> Sequence[str]: ...

    def open_table(self, name: str) -> _LanceTable: ...

    def create_table(self, name: str, *, data: list[dict[str, object]]) -> _LanceTable: ...


class _LanceModule(Protocol):
    def connect(self, uri: str) -> _LanceDatabase: ...


def canonical_float32_vector(values: Sequence[float], *, dimensions: int) -> bytes:
    """Canonicalize a vector before hashing or persisting it externally."""
    if len(values) != dimensions:
        raise ValueError(f"vector dimensions {len(values)} do not match expected {dimensions}")
    canonical: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("vector values must be finite numeric scalars")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("vector values must be finite")
        canonical.append(numeric)
    return struct.pack(f"<{dimensions}f", *canonical)


def vector_sha256(values: Sequence[float], *, dimensions: int) -> str:
    return hashlib.sha256(canonical_float32_vector(values, dimensions=dimensions)).hexdigest()


def _vector_list(values: Sequence[float], dimensions: int) -> list[float]:
    """Validate float32 representation and return exactly what LanceDB receives."""
    packed = canonical_float32_vector(values, dimensions=dimensions)
    return list(struct.unpack(f"<{dimensions}f", packed))


_VECTOR_DIGEST_FIELDS_LEGACY = (
    "chunk_id",
    "vector_sha256",
    "dimensions",
    "input_sha256",
    "manifest_id",
    "issuer_id",
    "recorded_issuer_id",
    "ticker",
    "form_type",
    "period_start",
    "period_end",
    "node_kind",
    "available_at",
    "observed_at",
    "retrieved_at",
    "latency_ms",
    "embedding_started_at",
    "embedding_completed_at",
)
_VECTOR_DIGEST_FIELDS_BOUND = (
    *_VECTOR_DIGEST_FIELDS_LEGACY,
    "runtime_artifact_sha256",
)


def _validate_vector_records(records: Sequence[dict[str, object]]) -> None:
    seen: set[str] = set()
    for record in records:
        chunk_id = record.get("chunk_id")
        vector = record.get("vector")
        declared_hash = record.get("vector_sha256")
        dimensions = record.get("dimensions")
        if (
            not isinstance(chunk_id, str)
            or chunk_id in seen
            or isinstance(dimensions, bool)
            or not isinstance(dimensions, int)
            or not isinstance(vector, Sequence)
            or isinstance(vector, str | bytes)
            or not isinstance(declared_hash, str)
        ):
            raise LocalVectorCapabilityError("stored vector row has an invalid immutable shape")
        seen.add(chunk_id)
        try:
            actual_hash = vector_sha256(cast(Sequence[float], vector), dimensions=dimensions)
        except (TypeError, ValueError) as exc:
            raise LocalVectorCapabilityError("stored vector row is not canonical") from exc
        if actual_hash != declared_hash:
            raise LocalVectorCapabilityError("stored vector row hash does not match vector values")
        runtime_sha = record.get("runtime_artifact_sha256")
        if runtime_sha is not None and (
            not isinstance(runtime_sha, str)
            or len(runtime_sha) != 64
            or any(char not in "0123456789abcdef" for char in runtime_sha)
        ):
            raise LocalVectorCapabilityError(
                "stored vector row has an invalid runtime artifact hash"
            )
        input_sha256 = record.get("input_sha256")
        if (
            not isinstance(input_sha256, str)
            or len(input_sha256) != 64
            or any(char not in "0123456789abcdef" for char in input_sha256)
        ):
            raise LocalVectorCapabilityError("stored vector row has an invalid input hash")


def vector_records_digest(records: Sequence[dict[str, object]]) -> str:
    _validate_vector_records(records)
    runtime_bound = [record.get("runtime_artifact_sha256") is not None for record in records]
    if any(runtime_bound) and not all(runtime_bound):
        raise LocalVectorCapabilityError(
            "stored projection mixes runtime-bound and legacy vector rows"
        )
    fields = (
        _VECTOR_DIGEST_FIELDS_BOUND
        if runtime_bound and all(runtime_bound)
        else _VECTOR_DIGEST_FIELDS_LEGACY
    )
    canonical = [
        [record.get(field) for field in fields]
        for record in sorted(records, key=lambda item: str(item["chunk_id"]))
    ]
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class FastEmbedEncoder:
    """Thin FastEmbed adapter that refuses to blur query and passage encodings."""

    def __init__(self, model: object) -> None:
        self._model = model

    @classmethod
    def from_spec(
        cls,
        spec: EmbeddingModelSpec,
        *,
        runtime_artifact: EmbeddingRuntimeArtifact,
        runtime_root: Path,
        sources: Sequence[RuntimeArtifactSource] | None = None,
        importer: Callable[[str], object] = importlib.import_module,
        version_lookup: Callable[[str], str] = metadata.version,
    ) -> FastEmbedEncoder:
        if (
            runtime_artifact.provider,
            runtime_artifact.model,
            runtime_artifact.dimensions,
        ) != (spec.provider, spec.model, spec.dimensions):
            raise LocalVectorCapabilityError(
                "embedding model coordinate differs from its runtime artifact"
            )
        runtime_sources = (
            tuple(
                RuntimeArtifactSource(
                    logical_name=item.logical_name,
                    role=item.role,
                    relative_path=Path(item.logical_name),
                )
                for item in runtime_artifact.files
            )
            if sources is None
            else tuple(sources)
        )
        try:
            verify_runtime_artifact(runtime_artifact, runtime_root, runtime_sources)
            _verify_component_versions(runtime_artifact, version_lookup=version_lookup)
        except ValueError:
            raise LocalVectorCapabilityError(
                "local embedding runtime does not match its sealed artifact"
            ) from None
        module = require_fastembed(importer)
        text_embedding = getattr(module, "TextEmbedding", None)
        if text_embedding is None:
            raise LocalVectorCapabilityError("installed fastembed does not expose TextEmbedding")
        try:
            parameters = inspect.signature(text_embedding).parameters
        except (TypeError, ValueError):
            raise LocalVectorCapabilityError(
                "installed fastembed constructor cannot prove offline loading"
            ) from None
        required = {"model_name", "cache_dir", "local_files_only", "providers"}
        if not required.issubset(parameters):
            raise LocalVectorCapabilityError(
                "installed fastembed API cannot prove offline local-only loading"
            )
        settings = {item.name: item.value for item in runtime_artifact.execution_settings}
        if any(name not in parameters for name in settings):
            raise LocalVectorCapabilityError(
                "sealed execution settings are unsupported by installed fastembed"
            )
        try:
            instance = text_embedding(
                model_name=spec.model,
                cache_dir=runtime_root,
                local_files_only=True,
                providers=[runtime_artifact.execution_provider],
                **settings,
            )
            verify_runtime_artifact(runtime_artifact, runtime_root, runtime_sources)
            _verify_component_versions(runtime_artifact, version_lookup=version_lookup)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise LocalVectorCapabilityError(
                "local embedding runtime initialization failed closed"
            ) from None
        return cls(instance)

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        method = getattr(self._model, "passage_embed", None)
        if method is None:
            raise LocalVectorCapabilityError(
                "installed fastembed lacks passage_embed; refusing ambiguous passage encoding"
            )
        return _embedding_rows(method(texts))

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        method = getattr(self._model, "query_embed", None)
        if method is None:
            raise LocalVectorCapabilityError(
                "installed fastembed lacks query_embed; refusing ambiguous query encoding"
            )
        return _embedding_rows(method(texts))


def _embedding_rows(raw: object) -> list[list[float]]:
    try:
        rows = list(cast(Iterable[object], raw))
    except TypeError as exc:
        raise LocalVectorCapabilityError(
            "FastEmbed returned a non-iterable embedding response"
        ) from exc
    result: list[list[float]] = []
    for row in rows:
        try:
            values = list(cast(Iterable[object], row))
        except TypeError as exc:
            raise LocalVectorCapabilityError(
                "FastEmbed returned a non-vector embedding row"
            ) from exc
        numeric_values: list[float] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise LocalVectorCapabilityError("FastEmbed returned a non-numeric embedding value")
            numeric_values.append(float(value))
        result.append(numeric_values)
    return result


def _verify_component_versions(
    artifact: EmbeddingRuntimeArtifact,
    *,
    version_lookup: Callable[[str], str],
) -> None:
    for item in artifact.component_versions:
        try:
            actual = (
                platform.python_version()
                if item.component == "python"
                else version_lookup(item.component)
            )
        except metadata.PackageNotFoundError:
            raise ValueError("required embedding component version is unavailable") from None
        if actual != item.version:
            raise ValueError("installed embedding component version differs from artifact")


def _safe_run_path(root: Path, index_run_id: str) -> Path:
    digest = hashlib.sha256(index_run_id.encode("utf-8")).hexdigest()
    return root / f"run-{digest}"


def _safe_staging_path(root: Path, index_run_id: str) -> Path:
    return _safe_run_path(root, index_run_id).with_name(
        _safe_run_path(root, index_run_id).name + ".staging"
    )


class LanceVectorIndex:
    """LanceDB storage isolated to one immutable index-run directory and table."""

    _TABLE = "evidence_chunks"

    def __init__(self, root: Path) -> None:
        self._root = root
        self._tables: dict[str, object] = {}

    def stage_batch(
        self,
        index_run_id: str,
        records: list[dict[str, object]],
    ) -> None:
        if not records:
            raise ValueError("vector stage batches must not be empty")
        if _safe_run_path(self._root, index_run_id).exists():
            raise ValueError("published vector index cannot receive staged rows")
        module = cast(_LanceModule, require_lancedb())
        path = _safe_staging_path(self._root, index_run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        database = module.connect(str(path))
        names = set(database.table_names())
        if self._TABLE not in names:
            table = database.create_table(self._TABLE, data=records)
        else:
            table = database.open_table(self._TABLE)
            first, last = str(records[0]["chunk_id"]), str(records[-1]["chunk_id"])
            existing = self._bounded_rows(table, first, last, len(records))
            if existing:
                expected = vector_records_digest(records)
                if len(existing) != len(records) or vector_records_digest(existing) != expected:
                    raise ValueError("staged vector batch conflicts with existing rows")
            else:
                table.add(records)
        self._tables[index_run_id] = table

    def verify_batch(
        self,
        index_run_id: str,
        receipt: VectorBatchReceipt,
    ) -> bool:
        try:
            rows = self.read_batch(index_run_id, receipt)
        except (LocalVectorCapabilityError, ValueError):
            return False
        return (
            len(rows) == receipt.chunk_count
            and vector_records_digest(rows) == receipt.records_sha256
        )

    def read_batch(
        self,
        index_run_id: str,
        receipt: VectorBatchReceipt,
    ) -> list[dict[str, object]]:
        table = cast(_LanceTable, self._open_any_table(index_run_id))
        rows = self._bounded_rows(
            table,
            receipt.first_chunk_id,
            receipt.last_chunk_id,
            receipt.chunk_count,
        )
        _validate_vector_records(rows)
        return rows

    def count_records(self, index_run_id: str) -> int:
        return int(cast(_LanceTable, self._open_any_table(index_run_id)).count_rows())

    def read_projection(self, index_run_id: str, *, expected_count: int) -> list[dict[str, object]]:
        table = cast(_LanceTable, self._open_any_table(index_run_id))
        rows = table.query().limit(expected_count + 1).to_list()
        _validate_vector_records(rows)
        if len(rows) != expected_count:
            raise LocalVectorCapabilityError(
                "vector projection row count does not match sealed corpus"
            )
        return rows

    def publish(self, index_run_id: str) -> str:
        final = _safe_run_path(self._root, index_run_id)
        staging = _safe_staging_path(self._root, index_run_id)
        if final.exists():
            if staging.exists():
                raise ValueError("both staged and published vector indexes exist")
            return f"lance://{final.resolve().as_posix()}#{self._TABLE}"
        if not staging.exists():
            raise ValueError("complete staged vector index is absent")
        self._tables.pop(index_run_id, None)
        os.replace(staging, final)
        return f"lance://{final.resolve().as_posix()}#{self._TABLE}"

    def published_storage_uri(self, index_run_id: str) -> str:
        final = _safe_run_path(self._root, index_run_id)
        if not final.exists():
            raise LocalVectorCapabilityError("published vector projection is absent")
        return f"lance://{final.resolve().as_posix()}#{self._TABLE}"

    def _open_any_table(self, index_run_id: str) -> object:
        table = self._tables.get(index_run_id)
        if table is not None:
            return table
        final = _safe_run_path(self._root, index_run_id)
        staging = _safe_staging_path(self._root, index_run_id)
        path = final if final.exists() else staging
        if not path.exists():
            raise LocalVectorCapabilityError("vector index run is absent")
        module = cast(_LanceModule, require_lancedb())
        database = module.connect(str(path))
        table = database.open_table(self._TABLE)
        self._tables[index_run_id] = table
        return table

    @staticmethod
    def _bounded_rows(
        table: _LanceTable,
        first_chunk_id: str,
        last_chunk_id: str,
        expected_count: int,
    ) -> list[dict[str, object]]:
        predicate = (
            _predicate("chunk_id", first_chunk_id, operator=">=")
            + " AND "
            + _predicate("chunk_id", last_chunk_id, operator="<=")
        )
        return table.query().where(predicate).limit(expected_count + 1).to_list()

    def open_table(self, index_run_id: str) -> object:
        table = self._tables.get(index_run_id)
        if table is not None:
            return table
        module = cast(_LanceModule, require_lancedb())
        database = module.connect(str(_safe_run_path(self._root, index_run_id)))
        table = database.open_table(self._TABLE)
        self._tables[index_run_id] = table
        return table


class LanceVectorBackend:
    """Query one immutable Lance table with manifest and evidence metadata prefilters."""

    def __init__(
        self,
        index: LanceVectorIndex,
        *,
        index_run_id: str,
        manifest_id: str,
        encoder: PassageQueryEncoder,
        dimensions: int,
        ledger_conn: sqlite3.Connection | None = None,
        projection_seal: SearchProjectionSeal | None = None,
    ) -> None:
        self._index = index
        self._index_run_id = index_run_id
        self._manifest_id = manifest_id
        self._encoder = encoder
        self._dimensions = dimensions
        self._ledger_conn = ledger_conn
        self._projection_seal = projection_seal

    def search(self, query: str, filters: SearchFilter, limit: int) -> list[VectorCandidate]:
        if limit <= 0:
            return []
        self._verify_projection()
        vectors = self._encoder.encode_queries([query])
        if len(vectors) != 1:
            raise LocalVectorCapabilityError("query encoder must return exactly one vector")
        query_vector = _vector_list(vectors[0], self._dimensions)
        search = cast(_LanceTable, self._index.open_table(self._index_run_id)).search(query_vector)
        search = search.where(_lance_filter(self._manifest_id, filters), prefilter=True).limit(
            limit
        )
        raw = search.to_list()
        candidates: list[VectorCandidate] = []
        for row in raw:
            if not isinstance(row.get("chunk_id"), str):
                raise LocalVectorCapabilityError("LanceDB search result lacks immutable chunk_id")
            if self._ledger_conn is not None:
                self._verify_runtime_row(row)
            distance = row.get("_distance", 0.0)
            if isinstance(distance, bool) or not isinstance(distance, int | float):
                raise LocalVectorCapabilityError("LanceDB search result has non-numeric distance")
            candidates.append(
                VectorCandidate(
                    str(row["chunk_id"]), 1.0 / (1.0 + float(distance)), self._index_run_id
                )
            )
        return candidates

    def _verify_projection(self) -> None:
        seal = self._projection_seal
        if seal is None:
            return
        ledger = self._ledger_conn
        if ledger is None:
            raise LocalVectorCapabilityError("sealed vector runtime requires its projection ledger")
        if (
            seal.index_run_id != self._index_run_id
            or seal.manifest_id != self._manifest_id
            or seal.index_kind != "vector"
            or seal.dimensions != self._dimensions
        ):
            raise LocalVectorCapabilityError(
                "vector runtime identity does not match projection seal"
            )
        try:
            verify_ledger_projection_seal(ledger, seal)
            if self._index.published_storage_uri(self._index_run_id) != seal.storage_uri:
                raise LocalVectorCapabilityError(
                    "vector projection storage URI no longer matches seal"
                )
            rows = self._index.read_projection(self._index_run_id, expected_count=seal.chunk_count)
            if vector_records_digest(rows) != seal.projection_records_sha256:
                raise LocalVectorCapabilityError(
                    "external vector projection no longer matches seal"
                )
        except (RuntimeError, ValueError) as exc:
            raise LocalVectorCapabilityError(
                "sealed vector projection failed runtime verification"
            ) from exc

    def _verify_runtime_row(self, row: dict[str, object]) -> None:
        _validate_vector_records([row])
        chunk_id = str(row["chunk_id"])
        ledger = self._ledger_conn
        assert ledger is not None
        expected = ledger.execute(
            "SELECT artifact.vector_sha256, artifact.input_sha256, chunk.content_sha256 "
            "FROM search_embedding_artifacts AS artifact "
            "JOIN search_chunks AS chunk ON chunk.chunk_id = artifact.chunk_id "
            "JOIN search_index_memberships AS membership "
            "ON membership.index_run_id = artifact.index_run_id "
            "AND membership.chunk_id = artifact.chunk_id "
            "WHERE artifact.index_run_id = ? AND artifact.chunk_id = ? "
            "AND artifact.outcome = 'succeeded' "
            "AND membership.membership_status = 'included'",
            (self._index_run_id, chunk_id),
        ).fetchone()
        if expected is None or (
            str(row["vector_sha256"]),
            str(row["input_sha256"]),
            str(row["input_sha256"]),
        ) != tuple(str(value) for value in expected):
            raise LocalVectorCapabilityError(
                f"vector result failed ledger hash verification: {chunk_id}"
            )


def _lance_filter(manifest_id: str, filters: SearchFilter) -> str:
    terms = [_predicate("manifest_id", manifest_id)]
    for column, value in (("issuer_id", filters.issuer_id), ("ticker", filters.ticker)):
        if value is not None:
            terms.append(_predicate(column, value))
    for column, values in (("form_type", filters.form_types), ("node_kind", filters.node_kinds)):
        if values:
            terms.append("(" + " OR ".join(_predicate(column, value) for value in values) + ")")
    if filters.period_start is not None:
        terms.append(_predicate("period_end", filters.period_start.isoformat(), operator=">="))
    if filters.period_end is not None:
        terms.append(_predicate("period_start", filters.period_end.isoformat(), operator="<="))
    if filters.knowledge_cutoff is not None:
        cutoff = filters.knowledge_cutoff.isoformat()
        for column in ("available_at", "observed_at", "retrieved_at"):
            terms.append(_predicate(column, cutoff, operator="<="))
    return " AND ".join(terms)


def _predicate(column: str, value: str, *, operator: str = "=") -> str:
    escaped = value.replace("'", "''")
    return f"{column} {operator} '{escaped}'"


DocumentBatchFactory = Callable[[str | None], Iterable[Sequence[VectorDocument]]]


class ResumableVectorIndexBuilder:
    """Embed bounded batches, verify exact checkpoints, then publish once."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        encoder: PassageQueryEncoder,
        index: StagedVectorIndex,
        checkpoint_store: VectorBuildCheckpointStore,
    ) -> None:
        self._conn = conn
        self._encoder = encoder
        self._index = index
        self._checkpoints = checkpoint_store
        self._store = GroundedSearchStore(conn)

    def build(
        self,
        request: VectorBuildRequest,
        *,
        total_documents: int,
        document_batches: DocumentBatchFactory,
        on_batch_complete: Callable[[int, int], None] | None = None,
    ) -> VectorBuildResult:
        if total_documents <= 0:
            raise ValueError("vector indexing requires at least one sealed corpus chunk")
        identity = _vector_request_identity(request)
        state = self._checkpoints.load_or_create(
            request_identity_sha256=identity,
            total_chunks=total_documents,
            started_at=request.started_at,
        )
        self._verify_receipts(request.index_run_id, state.receipts)
        existing = self._existing_run(request)
        if existing is not None:
            if not state.published:
                raise ValueError("successful vector ledger row lacks a published checkpoint")
            if existing[0] != "succeeded":
                return VectorBuildResult(
                    index_run_id=request.index_run_id,
                    outcome=existing[0],
                    created=False,
                    chunk_count=total_documents,
                    failure_reason=existing[1],
                )
            storage_uri = state.storage_uri
            assert storage_uri is not None
            self._persist_published_ledger(request, state, storage_uri)
            self._verify_published_ledger(request, state)
            return VectorBuildResult(
                index_run_id=request.index_run_id,
                outcome=existing[0],
                created=False,
                chunk_count=total_documents,
                failure_reason=existing[1],
            )
        if state.published:
            storage_uri = state.storage_uri
            assert storage_uri is not None
        else:
            after = state.receipts[-1].last_chunk_id if state.receipts else None
            completed = sum(receipt.chunk_count for receipt in state.receipts)
            for documents in document_batches(after):
                batch = list(documents)
                self._validate_document_batch(request, batch, after)
                records = self._embed_batch(request, batch)
                self._index.stage_batch(request.index_run_id, records)
                receipt = VectorBatchReceipt(
                    batch_number=len(state.receipts) + 1,
                    first_chunk_id=batch[0].chunk_id,
                    last_chunk_id=batch[-1].chunk_id,
                    chunk_count=len(batch),
                    records_sha256=vector_records_digest(records),
                )
                if not self._index.verify_batch(request.index_run_id, receipt):
                    raise LocalVectorCapabilityError(
                        "staged vector batch failed immutable row-hash verification"
                    )
                state = state.model_copy(update={"receipts": (*state.receipts, receipt)})
                self._checkpoints.save(state)
                completed += len(batch)
                after = batch[-1].chunk_id
                if on_batch_complete is not None:
                    on_batch_complete(completed, total_documents)
            if completed != total_documents:
                raise ValueError(
                    "deterministic vector source count changed during build: "
                    f"expected {total_documents}, staged {completed}"
                )
            if self._index.count_records(request.index_run_id) != total_documents:
                raise LocalVectorCapabilityError(
                    "staged vector index row count does not match sealed corpus"
                )
            self._verify_receipts(request.index_run_id, state.receipts)
            storage_uri = self._index.publish(request.index_run_id)
            state = state.model_copy(
                update={
                    "published": True,
                    "storage_uri": storage_uri,
                    "published_at": _now(),
                }
            )
            self._checkpoints.save(state)
            # The atomic rename must not weaken validation.  Re-open the
            # published path and verify each bounded receipt again.
            self._verify_receipts(request.index_run_id, state.receipts)
        self._persist_published_ledger(request, state, storage_uri)
        return VectorBuildResult(
            index_run_id=request.index_run_id,
            outcome="succeeded",
            created=True,
            chunk_count=total_documents,
        )

    def _existing_run(
        self,
        request: VectorBuildRequest,
    ) -> tuple[str, str | None] | None:
        row = self._conn.execute(
            "SELECT index_key, revision, manifest_id, index_kind, config_sha256, code_version, "
            "outcome, failure_reason FROM search_index_runs WHERE index_run_id = ?",
            (request.index_run_id,),
        ).fetchone()
        if row is None:
            return None
        expected = (
            request.index_key,
            request.revision,
            request.manifest_id,
            "vector",
            request.request_config_sha256,
            request.code_version,
        )
        if tuple(row[:6]) != expected:
            raise ValueError(f"immutable index run {request.index_run_id!r} conflicts with request")
        return str(row[6]), None if row[7] is None else str(row[7])

    def _validate_document_batch(
        self,
        request: VectorBuildRequest,
        documents: Sequence[VectorDocument],
        after: str | None,
    ) -> None:
        if not documents or len(documents) > request.batch_size:
            raise ValueError("document source emitted an empty or oversized vector batch")
        ids = [document.chunk_id for document in documents]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("vector document batches must have unique ordered chunk IDs")
        if after is not None and ids[0] <= after:
            raise ValueError("vector document source did not resume after checkpoint")
        for document in documents:
            if document.manifest_id != request.manifest_id:
                raise ValueError("every vector document must belong to the requested manifest")
            if hashlib.sha256(document.text.encode("utf-8")).hexdigest() != document.input_sha256:
                raise ValueError(
                    f"vector input hash does not match chunk text: {document.chunk_id}"
                )

    def _embed_batch(
        self,
        request: VectorBuildRequest,
        documents: Sequence[VectorDocument],
    ) -> list[dict[str, object]]:
        started_ns = time.perf_counter_ns()
        vectors = self._encoder.encode_passages([document.text for document in documents])
        latency_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
        if len(vectors) != len(documents):
            raise LocalVectorCapabilityError(
                "passage encoder returned a different number of vectors"
            )
        completed_at = _now().isoformat()
        return [
            _vector_record(
                request,
                document,
                raw_vector,
                latency_ms=latency_ms,
                completed_at=completed_at,
            )
            for document, raw_vector in zip(documents, vectors, strict=True)
        ]

    def _verify_receipts(
        self,
        index_run_id: str,
        receipts: Sequence[VectorBatchReceipt],
    ) -> None:
        for receipt in receipts:
            try:
                verified = self._index.verify_batch(index_run_id, receipt)
            except (LocalVectorCapabilityError, ValueError) as exc:
                raise LocalVectorCapabilityError(
                    f"vector checkpoint batch {receipt.batch_number} is corrupt"
                ) from exc
            if not verified:
                raise LocalVectorCapabilityError(
                    f"vector checkpoint batch {receipt.batch_number} no longer matches storage"
                )

    def _persist_published_ledger(
        self,
        request: VectorBuildRequest,
        state: VectorBuildCheckpoint,
        storage_uri: str,
    ) -> None:
        completed_at = state.published_at
        if completed_at is None:
            raise ValueError("vector ledger publication requires a durable publication clock")
        rows = self._index.read_projection(request.index_run_id, expected_count=state.total_chunks)
        projection_digest = vector_records_digest(rows)
        chunk_count, chunk_digest = manifest_chunk_commitment(
            self._conn, manifest_id=request.manifest_id
        )
        if chunk_count != state.total_chunks:
            raise LocalVectorCapabilityError(
                "sealed manifest chunk count changed before vector publication"
            )
        expected_chunks = {
            str(row[0]): str(row[1])
            for row in self._conn.execute(
                "SELECT chunk_id, content_sha256 FROM search_chunks WHERE manifest_id = ?",
                (request.manifest_id,),
            )
        }
        projected_chunks = {str(row["chunk_id"]): str(row["input_sha256"]) for row in rows}
        if projected_chunks != expected_chunks:
            raise LocalVectorCapabilityError(
                "external vector projection does not exactly match manifest chunks"
            )
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._store.persist(
                IndexRun(
                    index_run_id=request.index_run_id,
                    idempotency_key=request.index_run_id,
                    index_key=request.index_key,
                    revision=request.revision,
                    manifest_id=request.manifest_id,
                    index_kind="vector",
                    config_sha256=request.request_config_sha256,
                    code_version=request.code_version,
                    outcome="succeeded",
                    started_at=state.started_at,
                    completed_at=completed_at,
                )
            )
            for row in rows:
                chunk_id = str(row["chunk_id"])
                artifact_id = _embedding_artifact_id(request.index_run_id, chunk_id)
                latency_ms = row["latency_ms"]
                if isinstance(latency_ms, bool) or not isinstance(latency_ms, int):
                    raise LocalVectorCapabilityError(
                        f"stored vector latency is invalid: {chunk_id}"
                    )
                self._store.persist(
                    EmbeddingArtifact(
                        embedding_artifact_id=artifact_id,
                        idempotency_key=artifact_id,
                        index_run_id=request.index_run_id,
                        chunk_id=chunk_id,
                        purpose=request.purpose,
                        provider=request.model.provider,
                        model=request.model.model,
                        dimensions=request.model.dimensions,
                        vector_sha256=str(row["vector_sha256"]),
                        storage_uri=f"{storage_uri}/{chunk_id}",
                        input_sha256=str(row["input_sha256"]),
                        request_config_sha256=request.request_config_sha256,
                        runtime_artifact_sha256=request.runtime_artifact.sha256(),
                        outcome="succeeded",
                        latency_ms=latency_ms,
                        started_at=datetime.fromisoformat(str(row["embedding_started_at"])),
                        completed_at=datetime.fromisoformat(str(row["embedding_completed_at"])),
                    )
                )
                self._store.persist(
                    IndexMembership(
                        index_run_id=request.index_run_id,
                        chunk_id=chunk_id,
                        membership_status="included",
                        recorded_at=completed_at,
                    )
                )
            artifact_count, artifact_digest = vector_artifact_commitment(
                self._conn, index_run_id=request.index_run_id
            )
            if artifact_count != state.total_chunks:
                raise LocalVectorCapabilityError(
                    "vector artifacts do not exactly cover sealed manifest"
                )
            seal_id = _projection_seal_id(
                request.index_run_id,
                chunk_digest,
                projection_digest,
                artifact_digest,
                request.runtime_artifact.sha256(),
            )
            seal = SearchProjectionSeal(
                projection_seal_id=seal_id,
                idempotency_key=seal_id,
                index_run_id=request.index_run_id,
                manifest_id=request.manifest_id,
                index_kind="vector",
                chunk_count=state.total_chunks,
                chunk_set_sha256=chunk_digest,
                projection_records_sha256=projection_digest,
                artifact_set_sha256=artifact_digest,
                provider=request.model.provider,
                model=request.model.model,
                dimensions=request.model.dimensions,
                runtime_artifact_sha256=request.runtime_artifact.sha256(),
                config_sha256=request.request_config_sha256,
                storage_uri=storage_uri,
                sealed_at=completed_at,
            )
            persist_projection_seal(self._conn, seal)
            verify_ledger_projection_seal(self._conn, seal)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        self._verify_published_ledger(request, state)

    def _verify_published_ledger(
        self,
        request: VectorBuildRequest,
        state: VectorBuildCheckpoint,
    ) -> None:
        self._verify_receipts(request.index_run_id, state.receipts)
        if self._index.count_records(request.index_run_id) != state.total_chunks:
            raise LocalVectorCapabilityError("published vector index row count changed")
        rows = self._conn.execute(
            "SELECT artifact.chunk_id, artifact.vector_sha256, artifact.input_sha256, "
            "chunk.content_sha256, artifact.runtime_artifact_sha256 "
            "FROM search_embedding_artifacts AS artifact "
            "JOIN search_chunks AS chunk ON chunk.chunk_id = artifact.chunk_id "
            "JOIN search_index_memberships AS membership "
            "ON membership.index_run_id = artifact.index_run_id "
            "AND membership.chunk_id = artifact.chunk_id "
            "WHERE artifact.index_run_id = ? AND artifact.outcome = 'succeeded' "
            "AND membership.membership_status = 'included' ORDER BY artifact.chunk_id",
            (request.index_run_id,),
        )
        verified = 0
        for row in rows:
            if str(row[2]) != str(row[3]):
                raise LocalVectorCapabilityError(
                    f"vector ledger input hash drifted for chunk {row[0]}"
                )
            if str(row[4]) != request.runtime_artifact.sha256():
                raise LocalVectorCapabilityError(
                    f"vector ledger runtime artifact drifted for chunk {row[0]}"
                )
            verified += 1
        if verified != state.total_chunks:
            raise LocalVectorCapabilityError(
                "published vector ledger does not cover every sealed corpus chunk"
            )
        seal = load_projection_seal(self._conn, index_run_id=request.index_run_id)
        if seal is None:
            raise LocalVectorCapabilityError("published vector index lacks projection seal")
        verify_ledger_projection_seal(self._conn, seal)
        external_rows = self._index.read_projection(
            request.index_run_id, expected_count=state.total_chunks
        )
        if vector_records_digest(external_rows) != seal.projection_records_sha256:
            raise LocalVectorCapabilityError(
                "published vector projection no longer matches its seal"
            )


def _vector_request_identity(request: VectorBuildRequest) -> str:
    return request_config_sha256(
        {
            "index_run_id": request.index_run_id,
            "index_key": request.index_key,
            "revision": request.revision,
            "manifest_id": request.manifest_id,
            "code_version": request.code_version,
            "request_config_sha256": request.request_config_sha256,
            "model": request.model.model_dump(mode="json"),
            "runtime_artifact_sha256": request.runtime_artifact.sha256(),
            "purpose": request.purpose,
            "batch_size": request.batch_size,
        }
    )


def _embedding_artifact_id(index_run_id: str, chunk_id: str) -> str:
    return "embed-" + hashlib.sha256(f"{index_run_id}:{chunk_id}".encode()).hexdigest()[:48]


def _projection_seal_id(
    index_run_id: str,
    chunk_set_sha256: str,
    projection_records_sha256: str,
    artifact_set_sha256: str,
    runtime_artifact_sha256: str,
) -> str:
    seed = ":".join(
        (
            index_run_id,
            chunk_set_sha256,
            projection_records_sha256,
            artifact_set_sha256,
            runtime_artifact_sha256,
        )
    )
    return "search-projection-seal:" + hashlib.sha256(seed.encode()).hexdigest()


def _vector_record(
    request: VectorBuildRequest,
    document: VectorDocument,
    raw_vector: Sequence[float],
    *,
    latency_ms: int,
    completed_at: str,
) -> dict[str, object]:
    vector = _vector_list(raw_vector, request.model.dimensions)
    return {
        "chunk_id": document.chunk_id,
        "vector": vector,
        "vector_sha256": vector_sha256(vector, dimensions=request.model.dimensions),
        "dimensions": request.model.dimensions,
        "runtime_artifact_sha256": request.runtime_artifact.sha256(),
        "input_sha256": document.input_sha256,
        "manifest_id": document.manifest_id,
        "issuer_id": document.issuer_id,
        "recorded_issuer_id": document.recorded_issuer_id or document.issuer_id,
        # Empty strings keep Lance's inferred append schema stable even when
        # the first bounded batch has no optional value.  Runtime filters use
        # exact non-empty values, so the sentinel cannot create false matches.
        "ticker": document.ticker or "",
        "form_type": document.form_type,
        "period_start": document.period_start or "",
        "period_end": document.period_end or "",
        "node_kind": document.node_kind,
        "available_at": document.available_at.isoformat(),
        "observed_at": document.observed_at.isoformat(),
        "retrieved_at": document.retrieved_at.isoformat(),
        "latency_ms": latency_ms,
        "embedding_started_at": request.started_at.isoformat(),
        "embedding_completed_at": completed_at,
    }


def _documents_for_manifest_sql(conn: sqlite3.Connection) -> str:
    relation = evidence_document_relation(conn)
    recorded_issuer_sql = (
        "doc.recorded_issuer_id"
        if relation == "v_evidence_document_versions_canonical"
        else "doc.issuer_id"
    )
    return (
        "SELECT chunk.chunk_id, chunk.text, chunk.content_sha256, chunk.manifest_id, "  # nosec B608 -- trusted internal SQL shape; values remain bound
        f"doc.issuer_id, {recorded_issuer_sql}, doc.ticker, doc.form_type, "
        "doc.period_start, doc.period_end, node.node_kind, "
        "chunk.available_at, source.observed_at, source.retrieved_at "
        "FROM search_chunks AS chunk "
        "JOIN evidence_nodes AS node ON node.node_id = chunk.evidence_node_id "
        "JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = node.extraction_run_id "
        f"JOIN {relation} AS doc ON doc.document_version_id = run.document_version_id "
        "JOIN evidence_source_observations AS source "
        "ON source.observation_id = doc.observation_id "
    )


def count_documents_for_manifest(conn: sqlite3.Connection, manifest_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM search_chunks WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def document_batches_for_manifest(
    conn: sqlite3.Connection,
    manifest_id: str,
    *,
    batch_size: int,
    after_chunk_id: str | None = None,
) -> Iterator[list[VectorDocument]]:
    """Keyset-page a sealed manifest without accumulating its chunk text."""

    if batch_size <= 0:
        raise ValueError("vector document batch size must be positive")
    after = "" if after_chunk_id is None else after_chunk_id
    while True:
        rows = conn.execute(
            _documents_for_manifest_sql(conn)
            + "WHERE chunk.manifest_id = ? AND chunk.chunk_id > ? "
            "ORDER BY chunk.chunk_id LIMIT ?",
            (manifest_id, after, batch_size),
        ).fetchall()
        if not rows:
            return
        batch = [_vector_document(row) for row in rows]
        yield batch
        after = batch[-1].chunk_id


def _vector_document(row: Sequence[object]) -> VectorDocument:
    document = VectorDocument(
        chunk_id=str(row[0]),
        text=str(row[1]),
        input_sha256=str(row[2]),
        manifest_id=str(row[3]),
        issuer_id=str(row[4]),
        recorded_issuer_id=str(row[5]),
        ticker=None if row[6] is None else str(row[6]),
        form_type=str(row[7]),
        period_start=None if row[8] is None else str(row[8]),
        period_end=None if row[9] is None else str(row[9]),
        node_kind=str(row[10]),
        available_at=datetime.fromisoformat(str(row[11])),
        observed_at=datetime.fromisoformat(str(row[12])),
        retrieved_at=datetime.fromisoformat(str(row[13])),
    )
    if hashlib.sha256(document.text.encode("utf-8")).hexdigest() != document.input_sha256:
        raise ValueError(f"search chunk content hash mismatch: {document.chunk_id}")
    return document


def request_config_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
