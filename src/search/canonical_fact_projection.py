"""Bounded, sealed projection of selected canonical facts.

The projection is downstream of the exhaustive 0244 resolution snapshot.  It
never enumerates ``fact_cells_v2`` directly and never admits an unsealed source
publication.  Checkpoints contain the full selected state; deltas contain only
changed upserts and deletions relative to a sealed parent.  Retrieval resolves
the short delta chain in SQL and applies a hard candidate limit.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.metric_ontology import MetricOntology
from provenance.source_fact_publication import verify_source_fact_publication
from provenance.source_fact_stream import verify_resolution_snapshot_watermark
from provenance.verifier_identity import verifier_source_artifact_sha256

DIGEST_BUCKET_COUNT = 4096
DEFAULT_BATCH_FACTS = 1_000
DEFAULT_BATCH_BYTES = 16 * 1024 * 1024
DEFAULT_BATCH_MILLISECONDS = 1_000
MAX_DELTA_DEPTH = 32
MAX_BUCKET_ENTRY_COUNT = 250_000
MAX_BUCKET_SERIALIZED_BYTES = 16 * 1024 * 1024
MAX_BATCH_VECTOR_COUNT = 250_000
MAX_BATCH_VECTOR_SERIALIZED_BYTES = 64 * 1024 * 1024
_GENERATION_VERSION = "canonical_fact_projection_generation.v2"
_ENTRY_VERSION = "canonical_fact_projection_entry.v1"
_AUDIT_VERIFIER_NAME = "strict-canonical-fact-projection-auditor"
_AUDIT_VERIFIER_VERSION = "1"
_STRICT_AUDIT_FETCH_ROWS = 1_000
_monotonic = time.monotonic


def canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_AUDIT_VERIFIER_CODE_SHA256 = verifier_source_artifact_sha256(
    {
        "provenance/canonical_fact_resolution.py": (
            _SOURCE_ROOT / "provenance" / "canonical_fact_resolution.py"
        ),
        "provenance/metric_ontology.py": (_SOURCE_ROOT / "provenance" / "metric_ontology.py"),
        "provenance/source_fact_publication.py": (
            _SOURCE_ROOT / "provenance" / "source_fact_publication.py"
        ),
        "provenance/source_fact_stream.py": (_SOURCE_ROOT / "provenance" / "source_fact_stream.py"),
        "search/canonical_fact_projection.py": Path(__file__).resolve(),
    }
)


def canonical_time(value: object) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    aware = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return aware.isoformat(timespec="microseconds").replace("+00:00", "Z")


def db_time(value: object) -> str:
    return canonical_time(value).replace("T", " ").replace("Z", "")


def canonical_decimal(value: object) -> str:
    """Return a driver-neutral, exponent-free exact decimal representation."""

    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("numeric fact is not an exact decimal") from exc
    if not number.is_finite():
        raise ValueError("numeric fact must be finite")
    if number == 0:
        return "0"
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def digest_bucket(coordinate: str) -> int:
    return int(digest_text(coordinate)[:3], 16)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectionConfig(_Frozen):
    max_batch_facts: int = Field(default=DEFAULT_BATCH_FACTS, ge=1, le=1_000)
    max_batch_bytes: int = Field(default=DEFAULT_BATCH_BYTES, ge=1_024, le=16 * 1024 * 1024)
    max_batch_milliseconds: int = Field(default=DEFAULT_BATCH_MILLISECONDS, ge=1, le=1_000)
    digest_bucket_count: int = DIGEST_BUCKET_COUNT

    @field_validator("digest_bucket_count")
    @classmethod
    def _fixed_buckets(cls, value: int) -> int:
        if value != DIGEST_BUCKET_COUNT:
            raise ValueError("canonical projection uses exactly 4096 digest buckets")
        return value


class ProjectionGenerationRequest(_Frozen):
    generation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    generation_kind: Literal["checkpoint", "delta"]
    parent_generation_id: str | None = Field(default=None, max_length=128)
    resolution_snapshot_id: str = Field(min_length=1, max_length=128)
    ontology_snapshot_id: str = Field(min_length=1, max_length=128)
    cutoff_at: datetime
    recorded_at: datetime
    config: ProjectionConfig = ProjectionConfig()

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.generation_kind == "checkpoint") != (self.parent_generation_id is None):
            raise ValueError("only delta generations have a parent")
        if _utc(self.recorded_at) < _utc(self.cutoff_at):
            raise ValueError("projection recording cannot precede its cutoff")
        return self


class VerifiedCanonicalProjectionGeneration(_Frozen):
    generation_id: str
    projection_seal_id: str
    projection_seal_sha256: str
    generation_kind: Literal["checkpoint", "delta"]
    parent_generation_id: str | None
    resolution_snapshot_id: str
    resolution_snapshot_sha256: str
    resolution_scope_sha256: str
    resolution_snapshot_commitment_sha256: str
    resolution_watermark_sha256: str
    ontology_snapshot_id: str
    ontology_snapshot_sha256: str
    cutoff_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    change_count: int = Field(ge=0)
    upsert_count: int = Field(ge=0)
    tombstone_count: int = Field(ge=0)
    effective_entry_count: int = Field(ge=0)
    batch_count: int = Field(ge=0)
    bucket_count: int = DIGEST_BUCKET_COUNT
    generation_sha256: str
    ordered_entry_set_sha256: str


class CanonicalFactProjectionError(RuntimeError):
    def __init__(self, reason_code: str, *, generation_id: str | None = None) -> None:
        self.reason_code = reason_code
        self.generation_id = generation_id
        super().__init__(reason_code)


class _CanonicalArrayDigest:
    """Incrementally hash the exact compact JSON representation of an array."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(b"[")
        self._item_count = 0
        self._finished = False

    def add(self, value: object) -> None:
        if self._finished:
            raise RuntimeError("canonical array digest is already finished")
        if self._item_count:
            self._digest.update(b",")
        self._digest.update(canonical_json(value).encode("utf-8"))
        self._item_count += 1

    def finish(self) -> str:
        if not self._finished:
            self._digest.update(b"]")
            self._finished = True
        return self._digest.hexdigest()


class _BoundedCanonicalArray:
    """Build one persisted canonical array under explicit storage caps."""

    def __init__(
        self,
        *,
        generation_id: str,
        row_cap: int,
        byte_cap: int,
        row_reason: str,
        byte_reason: str,
    ) -> None:
        self._generation_id = generation_id
        self._row_cap = row_cap
        self._byte_cap = byte_cap
        self._row_reason = row_reason
        self._byte_reason = byte_reason
        self._buffer = StringIO()
        self._buffer.write("[")
        self._item_count = 0
        self._serialized_bytes = 1
        self._finished = False

    @property
    def item_count(self) -> int:
        return self._item_count

    def add(self, value: object) -> None:
        if self._finished:
            raise RuntimeError("bounded canonical array is already finished")
        if self._item_count >= self._row_cap:
            raise CanonicalFactProjectionError(self._row_reason, generation_id=self._generation_id)
        item = canonical_json(value)
        separator = "," if self._item_count else ""
        projected_bytes = (
            self._serialized_bytes + len(separator.encode("utf-8")) + len(item.encode("utf-8")) + 1
        )
        if projected_bytes > self._byte_cap:
            raise CanonicalFactProjectionError(self._byte_reason, generation_id=self._generation_id)
        self._buffer.write(separator)
        self._buffer.write(item)
        self._serialized_bytes = projected_bytes - 1
        self._item_count += 1

    def finish(self) -> str:
        if not self._finished:
            if self._serialized_bytes + 1 > self._byte_cap:
                raise CanonicalFactProjectionError(
                    self._byte_reason, generation_id=self._generation_id
                )
            self._buffer.write("]")
            self._serialized_bytes += 1
            self._finished = True
        return self._buffer.getvalue()


@dataclass(frozen=True)
class _StrictEntryAudit:
    change_count: int
    upsert_count: int
    tombstone_count: int
    batch_count: int
    ordered_entry_set_sha256: str
    batch_set_sha256: str
    changed_buckets: frozenset[int]


class CanonicalFactHit(_Frozen):
    generation_id: str
    canonical_metric_cell_id: str
    canonical_metric_name: str
    canonical_search_text: str
    canonical_value: str | None
    value_kind: Literal["numeric", "text", "nil"]
    period_start: str | None
    period_end: str
    currency: str | None
    unit_key: str
    entry_sha256: str
    lineage: dict[str, object]
    evidence_locator: dict[str, object]


def build_canonical_projection_generation(
    conn: sqlite3.Connection,
    request: ProjectionGenerationRequest,
) -> VerifiedCanonicalProjectionGeneration:
    """Build one exact generation atomically with bounded keyset batches."""

    references = _verify_generation_references(conn, request)
    delta_depth = _parent_depth(conn, request)
    config_json = canonical_json(request.config)
    generation_payload = {
        "config_sha256": digest_text(config_json),
        "cutoff_at": canonical_time(request.cutoff_at),
        "generation_kind": request.generation_kind,
        "generation_version": _GENERATION_VERSION,
        "ontology_snapshot_id": request.ontology_snapshot_id,
        "ontology_snapshot_sha256": references["ontology_snapshot_sha256"],
        "parent_generation_id": request.parent_generation_id,
        "resolution_snapshot_id": request.resolution_snapshot_id,
        "resolution_scope_sha256": references["resolution_scope_sha256"],
        "resolution_snapshot_commitment_sha256": references[
            "resolution_snapshot_commitment_sha256"
        ],
        "resolution_snapshot_sha256": references["resolution_snapshot_sha256"],
        "resolution_watermark_sha256": references["resolution_watermark_sha256"],
    }
    generation_json = canonical_json(generation_payload)
    header_values: tuple[object, ...] = (
        request.generation_id,
        request.idempotency_key,
        request.generation_kind,
        request.parent_generation_id,
        delta_depth,
        request.resolution_snapshot_id,
        references["resolution_snapshot_sha256"],
        references["resolution_watermark_sha256"],
        request.ontology_snapshot_id,
        references["ontology_snapshot_sha256"],
        db_time(request.cutoff_at),
        config_json,
        digest_text(config_json),
        DIGEST_BUCKET_COUNT,
        request.config.max_batch_facts,
        request.config.max_batch_bytes,
        request.config.max_batch_milliseconds,
        generation_json,
        digest_text(generation_json),
        db_time(request.recorded_at),
    )
    with _savepoint(conn, "build_canonical_fact_projection"):
        _ensure_projection_scope_binding(conn, request, references)
        existing = _row(
            conn,
            "SELECT * FROM canonical_fact_projection_generations "
            "WHERE generation_id=? OR idempotency_key=?",
            (request.generation_id, request.idempotency_key),
        )
        if existing is not None:
            if tuple(existing[key] for key in _GENERATION_COLUMNS) != header_values:
                raise CanonicalFactProjectionError(
                    "projection_generation_idempotency_conflict",
                    generation_id=request.generation_id,
                )
            verified = verify_canonical_projection_generation(
                conn,
                request.generation_id,
                resolution_snapshot_id=request.resolution_snapshot_id,
                ontology_snapshot_id=request.ontology_snapshot_id,
                cutoff_at=request.cutoff_at,
            )
            _record_projection_audit_receipt(conn, verified, audited_at=request.recorded_at)
            return verified
        conn.execute(
            "INSERT INTO canonical_fact_projection_generations "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"({','.join(_GENERATION_COLUMNS)}) VALUES "
            f"({','.join('?' for _ in _GENERATION_COLUMNS)})",
            header_values,
        )
        _write_entries_and_batches(conn, request)
        _write_buckets_and_seal(conn, request)
        verified = verify_canonical_projection_generation(
            conn,
            request.generation_id,
            resolution_snapshot_id=request.resolution_snapshot_id,
            ontology_snapshot_id=request.ontology_snapshot_id,
            cutoff_at=request.cutoff_at,
        )
        _record_projection_audit_receipt(conn, verified, audited_at=request.recorded_at)
        return verified


def verify_canonical_projection_generation(
    conn: sqlite3.Connection,
    generation_id: str,
    *,
    resolution_snapshot_id: str,
    ontology_snapshot_id: str,
    cutoff_at: datetime,
) -> VerifiedCanonicalProjectionGeneration:
    """Recompute and verify the public projection commitment.

    This is the strict audit verifier. It accepts identifiers, not a
    caller-supplied verifier, and re-verifies all upstream seals and every
    batch, bucket, source publication, and selected resolution link. Bounded
    live reads use ``admit_canonical_projection_for_read`` after this strict
    verifier has produced the immutable audit receipt.
    """

    header = _row(
        conn,
        "SELECT * FROM canonical_fact_projection_generations WHERE generation_id=?",
        (generation_id,),
    )
    seal_limits = _row(
        conn,
        "SELECT batch_count,"
        "length(CAST(ordered_batch_set_json AS BLOB)) AS batch_vector_bytes "
        "FROM canonical_fact_projection_seals WHERE generation_id=?",
        (generation_id,),
    )
    if seal_limits is not None and (
        _int(seal_limits["batch_count"]) > MAX_BATCH_VECTOR_COUNT
        or _int(seal_limits["batch_vector_bytes"]) > MAX_BATCH_VECTOR_SERIALIZED_BYTES
    ):
        raise CanonicalFactProjectionError(
            "projection_batch_vector_storage_cap_exceeded",
            generation_id=generation_id,
        )
    seal = _row(
        conn,
        "SELECT * FROM canonical_fact_projection_seals WHERE generation_id=?",
        (generation_id,),
    )
    if header is None or seal is None:
        raise CanonicalFactProjectionError(
            "projection_generation_not_fully_sealed", generation_id=generation_id
        )
    if (
        str(header["resolution_snapshot_id"]) != resolution_snapshot_id
        or str(header["ontology_snapshot_id"]) != ontology_snapshot_id
        or canonical_time(header["cutoff_at"]) != canonical_time(cutoff_at)
    ):
        raise CanonicalFactProjectionError(
            "projection_generation_coordinate_mismatch", generation_id=generation_id
        )
    request = ProjectionGenerationRequest(
        generation_id=generation_id,
        idempotency_key=str(header["idempotency_key"]),
        generation_kind=cast(Literal["checkpoint", "delta"], str(header["generation_kind"])),
        parent_generation_id=_optional_text(header["parent_generation_id"]),
        resolution_snapshot_id=resolution_snapshot_id,
        ontology_snapshot_id=ontology_snapshot_id,
        cutoff_at=_datetime(header["cutoff_at"]),
        recorded_at=_datetime(header["recorded_at"]),
        config=ProjectionConfig.model_validate(json.loads(str(header["config_json"]))),
    )
    references = _verify_generation_references(conn, request)
    config_json = canonical_json(request.config)
    payload = canonical_json(
        {
            "config_sha256": digest_text(config_json),
            "cutoff_at": canonical_time(request.cutoff_at),
            "generation_kind": request.generation_kind,
            "generation_version": _GENERATION_VERSION,
            "ontology_snapshot_id": ontology_snapshot_id,
            "ontology_snapshot_sha256": references["ontology_snapshot_sha256"],
            "parent_generation_id": request.parent_generation_id,
            "resolution_snapshot_id": resolution_snapshot_id,
            "resolution_scope_sha256": references["resolution_scope_sha256"],
            "resolution_snapshot_commitment_sha256": references[
                "resolution_snapshot_commitment_sha256"
            ],
            "resolution_snapshot_sha256": references["resolution_snapshot_sha256"],
            "resolution_watermark_sha256": references["resolution_watermark_sha256"],
        }
    )
    if (
        str(header["generation_json"]) != payload
        or str(header["generation_sha256"]) != digest_text(payload)
        or str(header["config_sha256"]) != digest_text(config_json)
        or _int(header["delta_depth"]) != _parent_depth(conn, request)
    ):
        raise CanonicalFactProjectionError(
            "projection_generation_header_tampered", generation_id=generation_id
        )
    entry_audit = _audit_entries_and_batches(conn, request)
    for publication in _iter_rows_batched(
        conn,
        "SELECT DISTINCT source_publication_id "
        "FROM canonical_fact_projection_entries "
        "WHERE generation_id=? AND change_kind='upsert' "
        "ORDER BY source_publication_id",
        (generation_id,),
    ):
        verify_source_fact_publication(
            conn,
            publication_id=str(publication["source_publication_id"]),
            cutoff=request.cutoff_at,
        )
    bucket_payload = _verify_buckets_streaming(conn, request, entry_audit.changed_buckets)
    ordered_entry_set_sha = entry_audit.ordered_entry_set_sha256
    batch_set_sha = entry_audit.batch_set_sha256
    bucket_json = canonical_json(bucket_payload)
    expected_seal_json = canonical_json(
        {
            "batch_set_sha256": batch_set_sha,
            "bucket_set_sha256": digest_text(bucket_json),
            "change_count": entry_audit.change_count,
            "effective_entry_count": sum(_int(item["entry_count"]) for item in bucket_payload),
            "tombstone_count": entry_audit.tombstone_count,
            "upsert_count": entry_audit.upsert_count,
            "generation_id": generation_id,
            "ordered_entry_set_sha256": ordered_entry_set_sha,
            "projection_seal_version": "canonical_fact_projection_seal.v1",
        }
    )
    if (
        _int(seal["change_count"]) != entry_audit.change_count
        or _int(seal["upsert_count"]) != entry_audit.upsert_count
        or _int(seal["tombstone_count"]) != entry_audit.tombstone_count
        or _int(seal["effective_entry_count"])
        != sum(_int(item["entry_count"]) for item in bucket_payload)
        or _int(seal["batch_count"]) != entry_audit.batch_count
        or _int(seal["stored_bucket_count"])
        != (
            DIGEST_BUCKET_COUNT
            if request.generation_kind == "checkpoint"
            else len(entry_audit.changed_buckets)
        )
        or _int(seal["logical_bucket_count"]) != DIGEST_BUCKET_COUNT
        or digest_text(str(seal["ordered_batch_set_json"])) != batch_set_sha
        or str(seal["batch_set_sha256"]) != batch_set_sha
        or str(seal["ordered_bucket_set_json"]) != bucket_json
        or str(seal["bucket_set_sha256"]) != digest_text(bucket_json)
        or str(seal["ordered_entry_set_sha256"]) != ordered_entry_set_sha
        or str(seal["projection_seal_json"]) != expected_seal_json
        or str(seal["projection_seal_sha256"]) != digest_text(expected_seal_json)
        or str(seal["projection_seal_id"]) != f"cfps_{digest_text(expected_seal_json)[:40]}"
    ):
        raise CanonicalFactProjectionError(
            "projection_final_seal_tampered", generation_id=generation_id
        )
    _verify_exact_generation_state(conn, request)
    return VerifiedCanonicalProjectionGeneration(
        generation_id=generation_id,
        projection_seal_id=str(seal["projection_seal_id"]),
        projection_seal_sha256=str(seal["projection_seal_sha256"]),
        generation_kind=request.generation_kind,
        parent_generation_id=request.parent_generation_id,
        resolution_snapshot_id=resolution_snapshot_id,
        resolution_snapshot_sha256=str(header["resolution_snapshot_sha256"]),
        resolution_scope_sha256=references["resolution_scope_sha256"],
        resolution_snapshot_commitment_sha256=references[
            "resolution_snapshot_commitment_sha256"
        ],
        resolution_watermark_sha256=str(header["resolution_watermark_sha256"]),
        ontology_snapshot_id=ontology_snapshot_id,
        ontology_snapshot_sha256=str(header["ontology_snapshot_sha256"]),
        cutoff_at=request.cutoff_at,
        knowledge_at=request.cutoff_at,
        recorded_at=request.recorded_at,
        change_count=entry_audit.change_count,
        upsert_count=_int(seal["upsert_count"]),
        tombstone_count=_int(seal["tombstone_count"]),
        effective_entry_count=_int(seal["effective_entry_count"]),
        batch_count=entry_audit.batch_count,
        generation_sha256=str(header["generation_sha256"]),
        ordered_entry_set_sha256=ordered_entry_set_sha,
    )


def search_canonical_facts(
    conn: sqlite3.Connection,
    *,
    generation_id: str,
    query_text: str,
    limit: int,
    reporting_entity_id: str | None = None,
) -> tuple[CanonicalFactHit, ...]:
    """Bounded SQL lexical candidate stage over a sealed generation chain."""

    if not query_text.strip():
        return ()
    if limit < 1 or limit > 1_000:
        raise ValueError("fact candidate limit must be between 1 and 1000")
    header = _row(
        conn,
        "SELECT resolution_snapshot_id,ontology_snapshot_id,cutoff_at "
        "FROM canonical_fact_projection_generations WHERE generation_id=?",
        (generation_id,),
    )
    if header is None:
        raise CanonicalFactProjectionError(
            "projection_generation_missing", generation_id=generation_id
        )
    admit_canonical_projection_for_read(conn, generation_id)
    plan = plan_canonical_fact_query(query_text)
    tokens = plan.metric_terms
    score_sql = " + ".join(
        "CASE WHEN instr(lower(entry.canonical_search_text),?)>0 THEN 1 ELSE 0 END" for _ in tokens
    )
    score_sql = score_sql or "1"
    params: list[object] = [generation_id, *tokens]
    entity_predicate = ""
    if reporting_entity_id is not None:
        entity_predicate = " AND entry.reporting_entity_id=?"
        params.append(reporting_entity_id)
    year_predicate = ""
    if plan.years:
        year_predicate = (
            " AND substr(entry.period_end,1,4) IN (" + ",".join("?" for _ in plan.years) + ")"
        )
        params.extend(str(year) for year in plan.years)
    params.append(limit)
    rows = _rows(
        conn,
        _CURRENT_STATE_CTE + f" SELECT entry.*,({score_sql}) AS query_score "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "FROM current_state entry "
        + f"WHERE entry.change_kind='upsert' AND ({score_sql})>0"
        + entity_predicate
        + year_predicate
        + " ORDER BY query_score DESC,entry.canonical_metric_name,"
        "entry.period_end DESC,"
        "entry.canonical_metric_cell_id LIMIT ?",
        tuple([generation_id, *tokens, *tokens, *params[1 + len(tokens) :]]),
    )
    hits: list[CanonicalFactHit] = []
    verified_buckets: set[int] = set()
    for row in rows:
        _verify_projected_hit_for_read(conn, generation_id, row, verified_buckets=verified_buckets)
        hits.append(
            CanonicalFactHit(
                generation_id=generation_id,
                canonical_metric_cell_id=str(row["canonical_metric_cell_id"]),
                canonical_metric_name=str(row["canonical_metric_name"]),
                canonical_search_text=str(row["canonical_search_text"]),
                canonical_value=_optional_text(row["canonical_value"]),
                value_kind=cast(Literal["numeric", "text", "nil"], str(row["value_kind"])),
                period_start=_optional_text(row["period_start"]),
                period_end=str(row["period_end"]),
                currency=_optional_text(row["currency"]),
                unit_key=str(row["unit_key"]),
                entry_sha256=str(row["entry_sha256"]),
                lineage={
                    "binding_commitment_sha256": row["binding_commitment_sha256"],
                    "binding_revision_id": row["binding_revision_id"],
                    "canonical_resolution_revision_id": row["canonical_resolution_revision_id"],
                    "metric_definition_commitment_sha256": row[
                        "metric_definition_commitment_sha256"
                    ],
                    "metric_definition_revision_id": row["metric_definition_revision_id"],
                    "ontology_snapshot_id": header["ontology_snapshot_id"],
                    "resolution_snapshot_id": header["resolution_snapshot_id"],
                    "selected_observation_id": row["selected_observation_id"],
                    "source_fact_cell_id": row["source_fact_cell_id"],
                    "source_publication_id": row["source_publication_id"],
                    "source_publication_member_id": row["source_publication_member_id"],
                    "source_publication_seal_id": row["source_publication_seal_id"],
                },
                evidence_locator=json.loads(str(row["evidence_locator_json"])),
            )
        )
    return tuple(hits)


class CanonicalFactQueryPlan(_Frozen):
    metric_terms: tuple[str, ...]
    years: tuple[int, ...]


def plan_canonical_fact_query(query_text: str) -> CanonicalFactQueryPlan:
    """Deterministically separate metric terms from periods and narrative intent."""

    import re

    years = tuple(sorted({int(match) for match in re.findall(r"\b(?:19|20)\d{2}\b", query_text)}))
    stopwords = {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "explain",
        "explanation",
        "for",
        "from",
        "growth",
        "in",
        "management",
        "managements",
        "of",
        "on",
        "s",
        "the",
        "to",
        "versus",
        "vs",
        "what",
        "why",
        "with",
    }
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", query_text.casefold())
    terms = tuple(sorted({word for word in words if word not in stopwords}))
    return CanonicalFactQueryPlan(metric_terms=terms, years=years)


def admit_canonical_projection_for_read(
    conn: sqlite3.Connection, generation_id: str
) -> VerifiedCanonicalProjectionGeneration:
    """Bounded read admission using the immutable strict-audit receipt."""

    header = _row(
        conn,
        "SELECT * FROM canonical_fact_projection_generations WHERE generation_id=?",
        (generation_id,),
    )
    seal = _row(
        conn,
        "SELECT * FROM canonical_fact_projection_seals WHERE generation_id=?",
        (generation_id,),
    )
    receipt = _row(
        conn,
        "SELECT * FROM canonical_fact_projection_audit_receipts WHERE generation_id=?",
        (generation_id,),
    )
    binding = _row(
        conn,
        "SELECT * FROM canonical_fact_projection_scope_bindings WHERE generation_id=?",
        (generation_id,),
    )
    if header is None or seal is None or receipt is None or binding is None:
        raise CanonicalFactProjectionError(
            "projection_strict_audit_receipt_missing", generation_id=generation_id
        )
    expected_payload = _projection_audit_payload(header, seal, binding)
    expected_json = canonical_json(expected_payload)
    if (
        str(receipt["projection_seal_sha256"]) != str(seal["projection_seal_sha256"])
        or str(receipt["verifier_name"]) != _AUDIT_VERIFIER_NAME
        or str(receipt["verifier_version"]) != _AUDIT_VERIFIER_VERSION
        or str(receipt["verifier_code_sha256"]) != _AUDIT_VERIFIER_CODE_SHA256
        or str(receipt["verifier_config_sha256"]) != digest_text(str(header["config_json"]))
        or str(receipt["audit_payload_json"]) != expected_json
        or str(receipt["audit_payload_sha256"]) != digest_text(expected_json)
    ):
        raise CanonicalFactProjectionError(
            "projection_strict_audit_receipt_tampered", generation_id=generation_id
        )
    return VerifiedCanonicalProjectionGeneration(
        generation_id=generation_id,
        projection_seal_id=str(seal["projection_seal_id"]),
        projection_seal_sha256=str(seal["projection_seal_sha256"]),
        generation_kind=cast(Literal["checkpoint", "delta"], str(header["generation_kind"])),
        parent_generation_id=_optional_text(header["parent_generation_id"]),
        resolution_snapshot_id=str(header["resolution_snapshot_id"]),
        resolution_snapshot_sha256=str(header["resolution_snapshot_sha256"]),
        resolution_scope_sha256=str(binding["resolution_scope_sha256"]),
        resolution_snapshot_commitment_sha256=str(
            binding["resolution_snapshot_commitment_sha256"]
        ),
        resolution_watermark_sha256=str(header["resolution_watermark_sha256"]),
        ontology_snapshot_id=str(header["ontology_snapshot_id"]),
        ontology_snapshot_sha256=str(header["ontology_snapshot_sha256"]),
        cutoff_at=_datetime(header["cutoff_at"]),
        knowledge_at=_datetime(header["cutoff_at"]),
        recorded_at=_datetime(header["recorded_at"]),
        change_count=_int(seal["change_count"]),
        upsert_count=_int(seal["upsert_count"]),
        tombstone_count=_int(seal["tombstone_count"]),
        effective_entry_count=_int(seal["effective_entry_count"]),
        batch_count=_int(seal["batch_count"]),
        generation_sha256=str(header["generation_sha256"]),
        ordered_entry_set_sha256=str(seal["ordered_entry_set_sha256"]),
    )


def _verify_projected_hit_for_read(
    conn: sqlite3.Connection,
    generation_id: str,
    entry: dict[str, object],
    *,
    verified_buckets: set[int],
) -> None:
    expected_entry_json = _entry_json_from_row(entry)
    if str(entry["entry_json"]) != expected_entry_json or str(entry["entry_sha256"]) != digest_text(
        expected_entry_json
    ):
        raise CanonicalFactProjectionError(
            "projection_read_entry_commitment_tampered",
            generation_id=generation_id,
        )
    bucket = _int(entry["digest_bucket"])
    if bucket not in verified_buckets:
        bucket_row = _row(
            conn,
            """
            WITH RECURSIVE lineage(generation_id,parent_generation_id,depth) AS (
              SELECT generation_id,parent_generation_id,0
              FROM canonical_fact_projection_generations WHERE generation_id=?
              UNION ALL
              SELECT parent.generation_id,parent.parent_generation_id,
                     lineage.depth+1
              FROM canonical_fact_projection_generations parent
              JOIN lineage ON parent.generation_id=lineage.parent_generation_id
              WHERE lineage.depth<32
            )
            SELECT bucket.canonical_entry_set_json,bucket.entry_set_sha256
            FROM lineage
            JOIN canonical_fact_projection_buckets bucket
              ON bucket.generation_id=lineage.generation_id
            WHERE bucket.digest_bucket=?
            ORDER BY lineage.depth LIMIT 1
            """,
            (generation_id, bucket),
        )
        if (
            bucket_row is None
            or str(bucket_row["entry_set_sha256"])
            != digest_text(str(bucket_row["canonical_entry_set_json"]))
            or str(entry["entry_sha256"])
            not in json.loads(str(bucket_row["canonical_entry_set_json"]))
        ):
            raise CanonicalFactProjectionError(
                "projection_read_bucket_commitment_tampered",
                generation_id=generation_id,
            )
        verified_buckets.add(bucket)
    publication_member = _row(
        conn,
        "SELECT member.publication_id,member.canonical_member_sha256,"
        "member.record_commitment_sha256,seal.publication_seal_id "
        "FROM source_fact_publication_members member "
        "JOIN source_fact_publication_seals seal "
        "ON seal.publication_id=member.publication_id "
        "WHERE member.publication_member_id=?",
        (entry["source_publication_member_id"],),
    )
    if (
        publication_member is None
        or str(publication_member["publication_id"]) != str(entry["source_publication_id"])
        or str(publication_member["publication_seal_id"])
        != str(entry["source_publication_seal_id"])
        or str(publication_member["canonical_member_sha256"])
        != str(entry["source_publication_member_sha256"])
        or str(publication_member["record_commitment_sha256"])
        != str(entry["source_record_commitment_sha256"])
    ):
        raise CanonicalFactProjectionError(
            "projection_read_publication_lineage_mismatch",
            generation_id=generation_id,
        )


def _record_projection_audit_receipt(
    conn: sqlite3.Connection,
    verified: VerifiedCanonicalProjectionGeneration,
    *,
    audited_at: datetime,
) -> None:
    header = _row(
        conn,
        "SELECT * FROM canonical_fact_projection_generations WHERE generation_id=?",
        (verified.generation_id,),
    )
    seal = _row(
        conn,
        "SELECT * FROM canonical_fact_projection_seals WHERE generation_id=?",
        (verified.generation_id,),
    )
    if header is None or seal is None:
        raise CanonicalFactProjectionError(
            "projection_audit_target_missing", generation_id=verified.generation_id
        )
    binding = _row(
        conn,
        "SELECT * FROM canonical_fact_projection_scope_bindings WHERE generation_id=?",
        (verified.generation_id,),
    )
    if binding is None:
        raise CanonicalFactProjectionError(
            "projection_scope_binding_missing", generation_id=verified.generation_id
        )
    payload_json = canonical_json(_projection_audit_payload(header, seal, binding))
    values = (
        verified.generation_id,
        verified.projection_seal_sha256,
        _AUDIT_VERIFIER_NAME,
        _AUDIT_VERIFIER_VERSION,
        _AUDIT_VERIFIER_CODE_SHA256,
        digest_text(str(header["config_json"])),
        payload_json,
        digest_text(payload_json),
        db_time(audited_at),
    )
    columns = (
        "generation_id",
        "projection_seal_sha256",
        "verifier_name",
        "verifier_version",
        "verifier_code_sha256",
        "verifier_config_sha256",
        "audit_payload_json",
        "audit_payload_sha256",
        "audited_at",
    )
    existing = _row(
        conn,
        "SELECT * FROM canonical_fact_projection_audit_receipts WHERE generation_id=?",
        (verified.generation_id,),
    )
    if existing is None:
        conn.execute(
            "INSERT INTO canonical_fact_projection_audit_receipts "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"({','.join(columns)}) VALUES "
            f"({','.join('?' for _ in columns)})",
            values,
        )
    elif tuple(existing[column] for column in columns) != values:
        raise CanonicalFactProjectionError(
            "projection_audit_receipt_conflict",
            generation_id=verified.generation_id,
        )


def _projection_audit_payload(
    header: dict[str, object],
    seal: dict[str, object],
    binding: dict[str, object],
) -> dict[str, object]:
    return {
        "audit_version": "canonical_fact_projection_full_audit.v2",
        "change_count": _int(seal["change_count"]),
        "effective_entry_count": _int(seal["effective_entry_count"]),
        "generation_id": header["generation_id"],
        "generation_sha256": header["generation_sha256"],
        "ontology_snapshot_sha256": header["ontology_snapshot_sha256"],
        "projection_seal_sha256": seal["projection_seal_sha256"],
        "resolution_snapshot_sha256": header["resolution_snapshot_sha256"],
        "resolution_scope_sha256": binding["resolution_scope_sha256"],
        "resolution_snapshot_commitment_sha256": binding[
            "resolution_snapshot_commitment_sha256"
        ],
        "resolution_watermark_sha256": header["resolution_watermark_sha256"],
        "tombstone_count": _int(seal["tombstone_count"]),
        "upsert_count": _int(seal["upsert_count"]),
    }


def _verify_generation_references(
    conn: sqlite3.Connection, request: ProjectionGenerationRequest
) -> dict[str, str]:
    resolution_receipt = CanonicalFactResolutionEngine(conn).verify_snapshot(
        request.resolution_snapshot_id, request.cutoff_at
    )
    watermark = verify_resolution_snapshot_watermark(
        conn,
        resolution_snapshot_id=request.resolution_snapshot_id,
        cutoff_at=request.cutoff_at,
    )
    MetricOntology(conn).verify_snapshot(request.ontology_snapshot_id)
    ontology = _row(
        conn,
        "SELECT header.cutoff_at,seal.member_set_sha256 "
        "FROM ontology_snapshot_headers header JOIN ontology_snapshot_seals seal "
        "ON seal.ontology_snapshot_id=header.ontology_snapshot_id "
        "WHERE header.ontology_snapshot_id=?",
        (request.ontology_snapshot_id,),
    )
    resolution = _row(
        conn,
        "SELECT member_set_sha256 FROM canonical_fact_resolution_snapshot_seals "
        "WHERE resolution_snapshot_id=?",
        (request.resolution_snapshot_id,),
    )
    if (
        ontology is None
        or resolution is None
        or canonical_time(ontology["cutoff_at"]) != canonical_time(request.cutoff_at)
    ):
        raise CanonicalFactProjectionError(
            "projection_upstream_snapshot_mismatch",
            generation_id=request.generation_id,
        )
    if request.parent_generation_id is not None:
        parent_binding = _row(
            conn,
            "SELECT resolution_scope_sha256 "
            "FROM canonical_fact_projection_scope_bindings WHERE generation_id=?",
            (request.parent_generation_id,),
        )
        if (
            parent_binding is None
            or str(parent_binding["resolution_scope_sha256"])
            != resolution_receipt.scope_sha256
        ):
            raise CanonicalFactProjectionError(
                "projection_parent_scope_mismatch",
                generation_id=request.generation_id,
            )
    return {
        "ontology_snapshot_sha256": str(ontology["member_set_sha256"]),
        "resolution_snapshot_sha256": str(resolution["member_set_sha256"]),
        "resolution_scope_sha256": resolution_receipt.scope_sha256,
        "resolution_snapshot_commitment_sha256": (
            resolution_receipt.snapshot_commitment_sha256
        ),
        "resolution_watermark_sha256": watermark.watermark_sha256,
    }


def _ensure_projection_scope_binding(
    conn: sqlite3.Connection,
    request: ProjectionGenerationRequest,
    references: dict[str, str],
) -> None:
    values = (
        request.generation_id,
        request.resolution_snapshot_id,
        references["resolution_scope_sha256"],
        references["resolution_snapshot_commitment_sha256"],
        db_time(request.recorded_at),
    )
    existing = _row(
        conn,
        "SELECT generation_id,resolution_snapshot_id,resolution_scope_sha256,"
        "resolution_snapshot_commitment_sha256,recorded_at "
        "FROM canonical_fact_projection_scope_bindings WHERE generation_id=?",
        (request.generation_id,),
    )
    if existing is None:
        conn.execute(
            "INSERT INTO canonical_fact_projection_scope_bindings VALUES (?,?,?,?,?)",
            values,
        )
        return
    columns = (
        "generation_id",
        "resolution_snapshot_id",
        "resolution_scope_sha256",
        "resolution_snapshot_commitment_sha256",
        "recorded_at",
    )
    if tuple(existing[column] for column in columns) != values:
        raise CanonicalFactProjectionError(
            "projection_scope_binding_conflict",
            generation_id=request.generation_id,
        )


def _parent_depth(conn: sqlite3.Connection, request: ProjectionGenerationRequest) -> int:
    if request.parent_generation_id is None:
        return 0
    parent = _row(
        conn,
        "SELECT generation.delta_depth,generation.cutoff_at "
        "FROM canonical_fact_projection_generations generation "
        "JOIN canonical_fact_projection_seals seal "
        "ON seal.generation_id=generation.generation_id "
        "WHERE generation.generation_id=?",
        (request.parent_generation_id,),
    )
    if parent is None or canonical_time(parent["cutoff_at"]) > canonical_time(request.cutoff_at):
        raise CanonicalFactProjectionError(
            "projection_parent_missing_or_after_cutoff",
            generation_id=request.generation_id,
        )
    depth = _int(parent["delta_depth"]) + 1
    if depth > MAX_DELTA_DEPTH:
        raise CanonicalFactProjectionError(
            "projection_delta_chain_requires_checkpoint",
            generation_id=request.generation_id,
        )
    return depth


def _write_entries_and_batches(
    conn: sqlite3.Connection, request: ProjectionGenerationRequest
) -> None:
    entry_ordinal = 0
    batch_ordinal = 0
    pending: list[tuple[dict[str, object], str, str]] = []
    pending_bytes = 0
    inspected_in_window = 0
    batch_started = _monotonic()

    def enforce_time_cap() -> None:
        if (_monotonic() - batch_started) * 1000 > request.config.max_batch_milliseconds:
            raise CanonicalFactProjectionError(
                "projection_batch_time_cap_exceeded",
                generation_id=request.generation_id,
            )

    def finish_scan_item() -> None:
        nonlocal inspected_in_window, batch_started
        inspected_in_window += 1
        if inspected_in_window >= request.config.max_batch_facts:
            enforce_time_cap()
            inspected_in_window = 0
            batch_started = _monotonic()

    def flush() -> None:
        nonlocal batch_ordinal, inspected_in_window
        nonlocal pending, pending_bytes, batch_started
        if not pending:
            return
        enforce_time_cap()
        conn.executemany(
            "INSERT INTO canonical_fact_projection_entries "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"({','.join(_ENTRY_COLUMNS)}) VALUES "
            f"({','.join('?' for _ in _ENTRY_COLUMNS)})",
            [tuple(item[0][column] for column in _ENTRY_COLUMNS) for item in pending],
        )
        hashes = [item[2] for item in pending]
        payload = canonical_json(hashes)
        conn.execute(
            "INSERT INTO canonical_fact_projection_batches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                request.generation_id,
                batch_ordinal,
                pending[0][0]["entry_ordinal"],
                pending[-1][0]["entry_ordinal"],
                pending[0][1],
                pending[-1][1],
                len(pending),
                pending_bytes,
                0,
                payload,
                digest_text(payload),
            ),
        )
        enforce_time_cap()
        batch_ordinal += 1
        pending = []
        pending_bytes = 0
        inspected_in_window = 0
        batch_started = _monotonic()

    def append(entry: dict[str, object], coordinate: str) -> None:
        nonlocal entry_ordinal, pending_bytes
        entry_json = str(entry["entry_json"])
        size = len(entry_json.encode("utf-8"))
        if size > request.config.max_batch_bytes:
            raise CanonicalFactProjectionError(
                "projection_entry_exceeds_batch_byte_cap",
                generation_id=request.generation_id,
            )
        if pending and (
            len(pending) >= request.config.max_batch_facts
            or pending_bytes + size > request.config.max_batch_bytes
        ):
            flush()
        pending.append((entry, coordinate, str(entry["entry_sha256"])))
        pending_bytes += size
        entry_ordinal += 1

    prior_rows = (
        _current_entries_batched(conn, request.parent_generation_id)
        if request.parent_generation_id is not None
        else iter(())
    )
    prior = next(prior_rows, None)
    for source in _selected_rows_keyset(conn, request):
        coordinate = str(source["canonical_metric_cell_id"])
        enforce_time_cap()
        while prior is not None and str(prior["canonical_metric_cell_id"]) < coordinate:
            prior = next(prior_rows, None)
        entry = _upsert_entry(request.generation_id, entry_ordinal, source)
        if (
            prior is None
            or str(prior["canonical_metric_cell_id"]) != coordinate
            or _semantic_entry_json(prior) != _semantic_entry_json(entry)
        ):
            append(entry, coordinate)
        else:
            prior = next(prior_rows, None)
        finish_scan_item()
    if request.parent_generation_id is not None:
        for coordinate in _deleted_coordinates_batched(
            conn, request.parent_generation_id, request.resolution_snapshot_id
        ):
            enforce_time_cap()
            entry = _delete_entry(request.generation_id, entry_ordinal, coordinate)
            append(entry, coordinate)
            finish_scan_item()
    flush()


def _write_buckets_and_seal(conn: sqlite3.Connection, request: ProjectionGenerationRequest) -> None:
    changed = {
        _int(row["digest_bucket"])
        for row in _iter_rows_batched(
            conn,
            "SELECT DISTINCT digest_bucket "
            "FROM canonical_fact_projection_entries WHERE generation_id=? "
            "ORDER BY digest_bucket",
            (request.generation_id,),
        )
    }
    if request.generation_kind == "checkpoint":
        commitments = _checkpoint_bucket_commitments(conn, request.generation_id)
        stored_bucket_count = DIGEST_BUCKET_COUNT
    else:
        commitments = (
            (
                bucket,
                *_effective_bucket_commitment(conn, request.generation_id, bucket),
            )
            for bucket in sorted(changed)
        )
        stored_bucket_count = len(changed)
    for bucket, count, payload, payload_sha in commitments:
        conn.execute(
            "INSERT INTO canonical_fact_projection_buckets VALUES (?,?,?,?,?)",
            (
                request.generation_id,
                bucket,
                count,
                payload,
                payload_sha,
            ),
        )
    ordered_entries = _CanonicalArrayDigest()
    change_count = 0
    upsert_count = 0
    tombstone_count = 0
    for row in _iter_rows_batched(
        conn,
        "SELECT change_kind,entry_sha256 "
        "FROM canonical_fact_projection_entries "
        "WHERE generation_id=? ORDER BY entry_ordinal",
        (request.generation_id,),
    ):
        ordered_entries.add(str(row["entry_sha256"]))
        change_count += 1
        if str(row["change_kind"]) == "upsert":
            upsert_count += 1
        else:
            tombstone_count += 1
    batch_json, batch_set_sha, batch_count, bucket_payload = _seal_payloads(
        conn, request.generation_id
    )
    bucket_json = canonical_json(bucket_payload)
    ordered_entry_sha = ordered_entries.finish()
    seal_payload = canonical_json(
        {
            "batch_set_sha256": batch_set_sha,
            "bucket_set_sha256": digest_text(bucket_json),
            "change_count": change_count,
            "effective_entry_count": sum(_int(item["entry_count"]) for item in bucket_payload),
            "generation_id": request.generation_id,
            "ordered_entry_set_sha256": ordered_entry_sha,
            "projection_seal_version": "canonical_fact_projection_seal.v1",
            "tombstone_count": tombstone_count,
            "upsert_count": upsert_count,
        }
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            request.generation_id,
            f"cfps_{digest_text(seal_payload)[:40]}",
            change_count,
            upsert_count,
            tombstone_count,
            sum(_int(item["entry_count"]) for item in bucket_payload),
            batch_count,
            stored_bucket_count,
            DIGEST_BUCKET_COUNT,
            batch_json,
            batch_set_sha,
            bucket_json,
            digest_text(bucket_json),
            ordered_entry_sha,
            seal_payload,
            digest_text(seal_payload),
            db_time(request.recorded_at),
        ),
    )


def _selected_rows_keyset(
    conn: sqlite3.Connection, request: ProjectionGenerationRequest
) -> Iterator[dict[str, object]]:
    last = ""
    while True:
        cursor = conn.execute(
            _SELECTED_FACT_SQL,
            (
                request.ontology_snapshot_id,
                request.resolution_snapshot_id,
                last,
                _STRICT_AUDIT_FETCH_ROWS,
            ),
        )
        names = tuple(item[0] for item in cursor.description or ())
        seen = False
        for raw in cursor:
            seen = True
            row = dict(zip(names, tuple(raw), strict=True))
            last = str(row["canonical_metric_cell_id"])
            yield row
        if not seen:
            return


def _upsert_entry(generation_id: str, ordinal: int, source: dict[str, object]) -> dict[str, object]:
    value_kind = str(source["value_kind"])
    value = (
        canonical_decimal(source["numeric_value"])
        if value_kind == "numeric"
        else (_optional_text(source["text_value"]) if value_kind == "text" else None)
    )
    dimensions_json = canonical_json(json.loads(str(source["dimension_set_json"])))
    locator_json = canonical_json(json.loads(str(source["source_locator_json"])))
    if digest_text(locator_json) != str(source["source_locator_sha256"]):
        raise ValueError("selected fact evidence locator commitment is not exact")
    period_start = (
        None if source["period_start"] is None else canonical_time(source["period_start"])
    )
    period_end = canonical_time(source["period_end"])
    search_text = " | ".join(
        part
        for part in (
            str(source["canonical_name"]),
            str(source["definition_text"]),
            " ".join(str(item) for item in json.loads(str(source["aliases_json"]))),
            str(source["reporting_entity_id"]),
            _optional_text(source["scope_security_id"]),
            period_start,
            period_end,
            dimensions_json,
        )
        if part is not None
    )
    payload: dict[str, object] = {
        "binding_commitment_sha256": source["binding_commitment_sha256"],
        "binding_revision_id": source["binding_revision_id"],
        "canonical_metric_cell_id": source["canonical_metric_cell_id"],
        "canonical_metric_name": source["canonical_name"],
        "canonical_resolution_revision_id": source["canonical_resolution_revision_id"],
        "canonical_search_text": search_text,
        "canonical_value": value,
        "change_kind": "upsert",
        "currency": source["currency"],
        "dimensions_json": dimensions_json,
        "entry_version": _ENTRY_VERSION,
        "evidence_document_version_id": source["document_version_id"],
        "evidence_locator_json": locator_json,
        "evidence_locator_sha256": source["source_locator_sha256"],
        "evidence_node_id": source["evidence_node_id"],
        "mapping_commitment_sha256": source["mapping_commitment_sha256"],
        "mapping_revision_id": source["mapping_revision_id"],
        "metric_definition_commitment_sha256": source["metric_definition_commitment_sha256"],
        "metric_definition_revision_id": source["metric_definition_revision_id"],
        "period_end": period_end,
        "period_kind": source["period_kind"],
        "period_start": period_start,
        "reporting_entity_id": source["reporting_entity_id"],
        "scope_security_id": source["scope_security_id"],
        "selected_observation_id": source["selected_observation_id"],
        "source_fact_cell_id": source["source_fact_cell_id"],
        "source_publication_id": source["source_publication_id"],
        "source_publication_member_id": source["source_publication_member_id"],
        "source_publication_member_sha256": source["source_publication_member_sha256"],
        "source_publication_seal_id": source["source_publication_seal_id"],
        "source_record_commitment_sha256": source["source_record_commitment_sha256"],
        "unit_key": source["unit_key"],
        "value_kind": value_kind,
    }
    entry_json = canonical_json(payload)
    return {
        "generation_id": generation_id,
        "entry_ordinal": ordinal,
        "change_kind": "upsert",
        "digest_bucket": digest_bucket(str(source["canonical_metric_cell_id"])),
        **{key: payload.get(key) for key in _ENTRY_PAYLOAD_COLUMNS},
        "entry_json": entry_json,
        "entry_sha256": digest_text(entry_json),
    }


def _delete_entry(generation_id: str, ordinal: int, coordinate: str) -> dict[str, object]:
    payload = {
        "canonical_metric_cell_id": coordinate,
        "change_kind": "delete",
        "entry_version": _ENTRY_VERSION,
    }
    entry_json = canonical_json(payload)
    return {
        "generation_id": generation_id,
        "entry_ordinal": ordinal,
        "change_kind": "delete",
        "digest_bucket": digest_bucket(coordinate),
        **{
            key: (coordinate if key == "canonical_metric_cell_id" else None)
            for key in _ENTRY_PAYLOAD_COLUMNS
        },
        "entry_json": entry_json,
        "entry_sha256": digest_text(entry_json),
    }


def _entry_json_from_row(row: dict[str, object]) -> str:
    if str(row["change_kind"]) == "delete":
        return canonical_json(
            {
                "canonical_metric_cell_id": row["canonical_metric_cell_id"],
                "change_kind": "delete",
                "entry_version": _ENTRY_VERSION,
            }
        )
    payload = {key: row[key] for key in _ENTRY_PAYLOAD_COLUMNS}
    payload["change_kind"] = "upsert"
    payload["entry_version"] = _ENTRY_VERSION
    return canonical_json(payload)


def _semantic_entry_json(row: dict[str, object]) -> str:
    payload = json.loads(_entry_json_from_row(row))
    payload.pop("change_kind", None)
    return canonical_json(payload)


def _current_entries_batched(
    conn: sqlite3.Connection, generation_id: str
) -> Iterator[dict[str, object]]:
    return _iter_rows_batched(
        conn,
        _CURRENT_STATE_CTE + " SELECT * FROM current_state WHERE change_kind='upsert' "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "ORDER BY canonical_metric_cell_id",
        (generation_id,),
    )


def _deleted_coordinates_batched(
    conn: sqlite3.Connection, parent_generation_id: str, resolution_snapshot_id: str
) -> Iterator[str]:
    for row in _iter_rows_batched(
        conn,
        _CURRENT_STATE_CTE + " SELECT current_state.canonical_metric_cell_id FROM current_state "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE current_state.change_kind='upsert' "
        "AND NOT EXISTS (SELECT 1 "
        "FROM canonical_fact_resolution_snapshot_members member "
        "JOIN canonical_fact_resolution_revisions resolution "
        "ON resolution.canonical_resolution_revision_id="
        "member.canonical_resolution_revision_id "
        "WHERE member.resolution_snapshot_id=? "
        "AND member.canonical_metric_cell_id="
        "current_state.canonical_metric_cell_id "
        "AND resolution.status='resolved') "
        "ORDER BY current_state.canonical_metric_cell_id",
        (parent_generation_id, resolution_snapshot_id),
    ):
        yield str(row["canonical_metric_cell_id"])


def _verify_upsert_against_snapshot(
    conn: sqlite3.Connection,
    request: ProjectionGenerationRequest,
    entry: dict[str, object],
) -> None:
    row = _row(
        conn,
        _SELECTED_FACT_BASE + " AND member.canonical_metric_cell_id=?",
        (
            request.ontology_snapshot_id,
            request.resolution_snapshot_id,
            entry["canonical_metric_cell_id"],
        ),
    )
    if row is None:
        raise CanonicalFactProjectionError(
            "projection_contains_unselected_or_ineligible_fact",
            generation_id=request.generation_id,
        )
    expected = _upsert_entry(request.generation_id, _int(entry["entry_ordinal"]), row)
    if _semantic_entry_json(expected) != _semantic_entry_json(entry):
        raise CanonicalFactProjectionError(
            "projection_fact_no_longer_matches_selected_resolution",
            generation_id=request.generation_id,
        )


def _verify_exact_generation_state(
    conn: sqlite3.Connection, request: ProjectionGenerationRequest
) -> None:
    projected = _iter_rows_batched(
        conn,
        _CURRENT_STATE_CTE + " SELECT canonical_metric_cell_id FROM current_state "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE change_kind='upsert' ORDER BY canonical_metric_cell_id",
        (request.generation_id,),
    )
    expected = _iter_rows_batched(
        conn,
        "SELECT member.canonical_metric_cell_id "
        "FROM canonical_fact_resolution_snapshot_members member "
        "JOIN canonical_fact_resolution_revisions resolution "
        "ON resolution.canonical_resolution_revision_id="
        "member.canonical_resolution_revision_id "
        "WHERE member.resolution_snapshot_id=? AND resolution.status='resolved' "
        "ORDER BY member.canonical_metric_cell_id",
        (request.resolution_snapshot_id,),
    )
    while True:
        projected_row = next(projected, None)
        expected_row = next(expected, None)
        if projected_row is None or expected_row is None:
            if projected_row is expected_row:
                return
            break
        if str(projected_row["canonical_metric_cell_id"]) != str(
            expected_row["canonical_metric_cell_id"]
        ):
            break
    raise CanonicalFactProjectionError(
        "projection_omits_or_adds_selected_canonical_facts",
        generation_id=request.generation_id,
    )


def _audit_entries_and_batches(
    conn: sqlite3.Connection,
    request: ProjectionGenerationRequest,
) -> _StrictEntryAudit:
    entries = _iter_rows_batched(
        conn,
        "SELECT * FROM canonical_fact_projection_entries "
        "WHERE generation_id=? ORDER BY entry_ordinal",
        (request.generation_id,),
    )
    batches = _iter_rows_batched(
        conn,
        "SELECT * FROM canonical_fact_projection_batches "
        "WHERE generation_id=? ORDER BY batch_ordinal",
        (request.generation_id,),
    )
    current_batch = next(batches, None)
    ordered_entries = _CanonicalArrayDigest()
    ordered_batches = _CanonicalArrayDigest()
    changed_buckets: set[int] = set()
    change_count = 0
    upsert_count = 0
    tombstone_count = 0
    batch_count = 0
    batch_member_count = 0
    batch_serialized_bytes = 0
    batch_first_coordinate: str | None = None
    batch_last_coordinate: str | None = None
    batch_members = _CanonicalArrayDigest()

    for entry in entries:
        ordinal = change_count
        if _int(entry["entry_ordinal"]) != ordinal:
            raise CanonicalFactProjectionError(
                "projection_entry_ordinal_gap", generation_id=request.generation_id
            )
        expected_json = _entry_json_from_row(entry)
        if (
            str(entry["entry_json"]) != expected_json
            or str(entry["entry_sha256"]) != digest_text(expected_json)
            or _int(entry["digest_bucket"]) != digest_bucket(str(entry["canonical_metric_cell_id"]))
        ):
            raise CanonicalFactProjectionError(
                "projection_entry_commitment_tampered",
                generation_id=request.generation_id,
            )
        if current_batch is None:
            raise CanonicalFactProjectionError(
                "projection_batches_omit_entries",
                generation_id=request.generation_id,
            )
        declared_count = _int(current_batch["entry_count"])
        if batch_member_count == 0 and (
            _int(current_batch["batch_ordinal"]) != batch_count
            or _int(current_batch["first_entry_ordinal"]) != ordinal
            or declared_count < 1
            or declared_count > request.config.max_batch_facts
        ):
            raise CanonicalFactProjectionError(
                "projection_batch_commitment_tampered",
                generation_id=request.generation_id,
            )

        coordinate = str(entry["canonical_metric_cell_id"])
        if batch_first_coordinate is None:
            batch_first_coordinate = coordinate
        batch_last_coordinate = coordinate
        batch_member_count += 1
        batch_serialized_bytes += len(expected_json.encode("utf-8"))
        entry_sha = str(entry["entry_sha256"])
        batch_members.add(entry_sha)
        ordered_entries.add(entry_sha)
        changed_buckets.add(_int(entry["digest_bucket"]))
        change_count += 1
        if str(entry["change_kind"]) == "upsert":
            upsert_count += 1
            _verify_upsert_against_snapshot(conn, request, entry)
        else:
            tombstone_count += 1

        if batch_member_count == declared_count:
            member_set_sha = batch_members.finish()
            batch_payload = {
                "batch_ordinal": batch_count,
                "entry_count": declared_count,
                "entry_set_sha256": member_set_sha,
            }
            if (
                _int(current_batch["last_entry_ordinal"]) != ordinal
                or str(current_batch["first_coordinate"]) != batch_first_coordinate
                or str(current_batch["last_coordinate"]) != batch_last_coordinate
                or _int(current_batch["serialized_bytes"]) != batch_serialized_bytes
                or batch_serialized_bytes > request.config.max_batch_bytes
                or _int(current_batch["elapsed_milliseconds"]) != 0
                or digest_text(str(current_batch["canonical_entry_set_json"])) != member_set_sha
                or str(current_batch["entry_set_sha256"]) != member_set_sha
            ):
                raise CanonicalFactProjectionError(
                    "projection_batch_commitment_tampered",
                    generation_id=request.generation_id,
                )
            ordered_batches.add(batch_payload)
            batch_count += 1
            current_batch = next(batches, None)
            batch_member_count = 0
            batch_serialized_bytes = 0
            batch_first_coordinate = None
            batch_last_coordinate = None
            batch_members = _CanonicalArrayDigest()

    if current_batch is not None or batch_member_count:
        raise CanonicalFactProjectionError(
            "projection_batch_commitment_tampered",
            generation_id=request.generation_id,
        )
    return _StrictEntryAudit(
        change_count=change_count,
        upsert_count=upsert_count,
        tombstone_count=tombstone_count,
        batch_count=batch_count,
        ordered_entry_set_sha256=ordered_entries.finish(),
        batch_set_sha256=ordered_batches.finish(),
        changed_buckets=frozenset(changed_buckets),
    )


def _verify_buckets_streaming(
    conn: sqlite3.Connection,
    request: ProjectionGenerationRequest,
    changed_buckets: frozenset[int],
) -> list[dict[str, object]]:
    buckets = _iter_rows_batched(
        conn,
        "SELECT generation_id,digest_bucket,entry_count,entry_set_sha256,"
        "length(CAST(canonical_entry_set_json AS BLOB)) AS payload_bytes "
        "FROM canonical_fact_projection_buckets "
        "WHERE generation_id=? ORDER BY digest_bucket",
        (request.generation_id,),
    )
    expected_buckets: Iterator[int] = iter(
        range(DIGEST_BUCKET_COUNT)
        if request.generation_kind == "checkpoint"
        else sorted(changed_buckets)
    )
    effective_commitments = _effective_bucket_commitment_digests(conn, request.generation_id)
    for expected_ordinal in expected_buckets:
        bucket = next(buckets, None)
        if bucket is None or _int(bucket["digest_bucket"]) != expected_ordinal:
            raise CanonicalFactProjectionError(
                "projection_bucket_set_incomplete",
                generation_id=request.generation_id,
            )
        if (
            _int(bucket["entry_count"]) > MAX_BUCKET_ENTRY_COUNT
            or _int(bucket["payload_bytes"]) > MAX_BUCKET_SERIALIZED_BYTES
        ):
            raise CanonicalFactProjectionError(
                "projection_bucket_storage_cap_exceeded",
                generation_id=request.generation_id,
            )
        count, payload_sha = effective_commitments[expected_ordinal]
        stored_payload = _row(
            conn,
            "SELECT canonical_entry_set_json "
            "FROM canonical_fact_projection_buckets "
            "WHERE generation_id=? AND digest_bucket=?",
            (request.generation_id, expected_ordinal),
        )
        if (
            stored_payload is None
            or _int(bucket["entry_count"]) != count
            or digest_text(str(stored_payload["canonical_entry_set_json"])) != payload_sha
            or str(bucket["entry_set_sha256"]) != payload_sha
        ):
            raise CanonicalFactProjectionError(
                "projection_bucket_commitment_tampered",
                generation_id=request.generation_id,
            )
    if next(buckets, None) is not None:
        raise CanonicalFactProjectionError(
            "projection_bucket_set_incomplete", generation_id=request.generation_id
        )
    return _logical_bucket_vector(conn, request.generation_id)


def _effective_bucket_commitment_digests(
    conn: sqlite3.Connection, generation_id: str
) -> list[tuple[int, str]]:
    digests = [_CanonicalArrayDigest() for _ in range(DIGEST_BUCKET_COUNT)]
    counts = [0] * DIGEST_BUCKET_COUNT
    for row in _iter_rows_batched(
        conn,
        _CURRENT_STATE_CTE + " SELECT digest_bucket,entry_sha256 FROM current_state "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE change_kind='upsert' "
        "ORDER BY digest_bucket,canonical_metric_cell_id",
        (generation_id,),
    ):
        bucket = _int(row["digest_bucket"])
        if bucket < 0 or bucket >= DIGEST_BUCKET_COUNT:
            raise CanonicalFactProjectionError(
                "projection_bucket_commitment_tampered",
                generation_id=generation_id,
            )
        digests[bucket].add(str(row["entry_sha256"]))
        counts[bucket] += 1
    return [(counts[bucket], digests[bucket].finish()) for bucket in range(DIGEST_BUCKET_COUNT)]


def _seal_payloads(
    conn: sqlite3.Connection, generation_id: str
) -> tuple[str, str, int, list[dict[str, object]]]:
    batch_vector = _BoundedCanonicalArray(
        generation_id=generation_id,
        row_cap=MAX_BATCH_VECTOR_COUNT,
        byte_cap=MAX_BATCH_VECTOR_SERIALIZED_BYTES,
        row_reason="projection_batch_vector_row_cap_exceeded",
        byte_reason="projection_batch_vector_byte_cap_exceeded",
    )
    for row in _iter_rows_batched(
        conn,
        "SELECT batch_ordinal,entry_count,entry_set_sha256 "
        "FROM canonical_fact_projection_batches WHERE generation_id=? "
        "ORDER BY batch_ordinal",
        (generation_id,),
    ):
        batch_vector.add(
            {
                "batch_ordinal": _int(row["batch_ordinal"]),
                "entry_count": _int(row["entry_count"]),
                "entry_set_sha256": str(row["entry_set_sha256"]),
            }
        )
    batch_json = batch_vector.finish()
    buckets = _logical_bucket_vector(conn, generation_id)
    return (
        batch_json,
        digest_text(batch_json),
        batch_vector.item_count,
        buckets,
    )


def _checkpoint_bucket_commitments(
    conn: sqlite3.Connection, generation_id: str
) -> Iterator[tuple[int, int, str, str]]:
    rows = _iter_rows_batched(
        conn,
        _CURRENT_STATE_CTE + " SELECT digest_bucket,entry_sha256 FROM current_state "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE change_kind='upsert' "
        "ORDER BY digest_bucket,canonical_metric_cell_id",
        (generation_id,),
    )
    current = next(rows, None)
    for bucket in range(DIGEST_BUCKET_COUNT):
        payload_builder = _bucket_payload_builder(generation_id)
        while current is not None and _int(current["digest_bucket"]) == bucket:
            payload_builder.add(str(current["entry_sha256"]))
            current = next(rows, None)
        if current is not None and _int(current["digest_bucket"]) < bucket:
            raise CanonicalFactProjectionError(
                "projection_bucket_commitment_tampered",
                generation_id=generation_id,
            )
        payload = payload_builder.finish()
        yield (
            bucket,
            payload_builder.item_count,
            payload,
            digest_text(payload),
        )
    if current is not None:
        raise CanonicalFactProjectionError(
            "projection_bucket_commitment_tampered",
            generation_id=generation_id,
        )


def _logical_bucket_vector(conn: sqlite3.Connection, generation_id: str) -> list[dict[str, object]]:
    rows = list(
        _iter_rows_batched(
            conn,
            """
            WITH RECURSIVE lineage(generation_id,parent_generation_id,depth) AS (
              SELECT generation_id,parent_generation_id,0
              FROM canonical_fact_projection_generations WHERE generation_id=?
              UNION ALL
              SELECT parent.generation_id,parent.parent_generation_id,lineage.depth+1
              FROM canonical_fact_projection_generations parent
              JOIN lineage ON parent.generation_id=lineage.parent_generation_id
              WHERE lineage.depth<32
            ),
            ranked AS (
              SELECT bucket.digest_bucket,bucket.entry_count,bucket.entry_set_sha256,
                     row_number() OVER (
                       PARTITION BY bucket.digest_bucket ORDER BY lineage.depth
                     ) AS bucket_rank
              FROM lineage
              JOIN canonical_fact_projection_buckets bucket
                ON bucket.generation_id=lineage.generation_id
            )
            SELECT digest_bucket,entry_count,entry_set_sha256
            FROM ranked WHERE bucket_rank=1 ORDER BY digest_bucket
            """,
            (generation_id,),
        )
    )
    if len(rows) != DIGEST_BUCKET_COUNT or [_int(row["digest_bucket"]) for row in rows] != list(
        range(DIGEST_BUCKET_COUNT)
    ):
        raise CanonicalFactProjectionError(
            "projection_effective_bucket_vector_incomplete",
            generation_id=generation_id,
        )
    return [
        {
            "digest_bucket": _int(row["digest_bucket"]),
            "entry_count": _int(row["entry_count"]),
            "entry_set_sha256": str(row["entry_set_sha256"]),
        }
        for row in rows
    ]


def _effective_bucket_commitment(
    conn: sqlite3.Connection, generation_id: str, bucket: int
) -> tuple[int, str, str]:
    payload_builder = _bucket_payload_builder(generation_id)
    for row in _iter_rows_batched(
        conn,
        _CURRENT_STATE_CTE + " SELECT entry_sha256 FROM current_state "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE change_kind='upsert' AND digest_bucket=? "
        "ORDER BY canonical_metric_cell_id",
        (generation_id, bucket),
    ):
        payload_builder.add(str(row["entry_sha256"]))
    payload = payload_builder.finish()
    return payload_builder.item_count, payload, digest_text(payload)


def _bucket_payload_builder(generation_id: str) -> _BoundedCanonicalArray:
    return _BoundedCanonicalArray(
        generation_id=generation_id,
        row_cap=MAX_BUCKET_ENTRY_COUNT,
        byte_cap=MAX_BUCKET_SERIALIZED_BYTES,
        row_reason="projection_bucket_row_cap_exceeded",
        byte_reason="projection_bucket_byte_cap_exceeded",
    )


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str) -> Generator[None, None, None]:
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")


def _rows(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> list[dict[str, object]]:
    cursor = conn.execute(sql, params)
    names = tuple(item[0] for item in cursor.description or ())
    return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]


def _iter_rows_batched(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
) -> Iterator[dict[str, object]]:
    """Yield rows while capping every Python-side database fetch."""

    cursor = conn.execute(sql, params)
    names = tuple(item[0] for item in cursor.description or ())
    while True:
        batch = cursor.fetchmany(_STRICT_AUDIT_FETCH_ROWS)
        if not batch:
            return
        for row in batch:
            yield dict(zip(names, tuple(row), strict=True))


def _row(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> dict[str, object] | None:
    cursor = conn.execute(sql, params)
    names = tuple(item[0] for item in cursor.description or ())
    value = cursor.fetchone()
    return None if value is None else dict(zip(names, tuple(value), strict=True))


def _datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_text(value: object | None) -> str | None:
    return None if value is None else str(value)


def _int(value: object) -> int:
    return int(str(value))


_GENERATION_COLUMNS = (
    "generation_id",
    "idempotency_key",
    "generation_kind",
    "parent_generation_id",
    "delta_depth",
    "resolution_snapshot_id",
    "resolution_snapshot_sha256",
    "resolution_watermark_sha256",
    "ontology_snapshot_id",
    "ontology_snapshot_sha256",
    "cutoff_at",
    "config_json",
    "config_sha256",
    "digest_bucket_count",
    "max_batch_facts",
    "max_batch_bytes",
    "max_batch_milliseconds",
    "generation_json",
    "generation_sha256",
    "recorded_at",
)

_ENTRY_PAYLOAD_COLUMNS = (
    "canonical_metric_cell_id",
    "canonical_resolution_revision_id",
    "selected_observation_id",
    "source_fact_cell_id",
    "source_publication_id",
    "source_publication_seal_id",
    "source_publication_member_id",
    "source_publication_member_sha256",
    "source_record_commitment_sha256",
    "binding_revision_id",
    "binding_commitment_sha256",
    "mapping_revision_id",
    "mapping_commitment_sha256",
    "metric_definition_revision_id",
    "metric_definition_commitment_sha256",
    "reporting_entity_id",
    "scope_security_id",
    "canonical_metric_name",
    "period_kind",
    "period_start",
    "period_end",
    "dimensions_json",
    "unit_key",
    "currency",
    "value_kind",
    "canonical_value",
    "evidence_document_version_id",
    "evidence_node_id",
    "evidence_locator_json",
    "evidence_locator_sha256",
    "canonical_search_text",
)

_ENTRY_COLUMNS = (
    "generation_id",
    "entry_ordinal",
    "change_kind",
    "digest_bucket",
    *_ENTRY_PAYLOAD_COLUMNS,
    "entry_json",
    "entry_sha256",
)

_SELECTED_FACT_BASE = """
SELECT
 member.canonical_metric_cell_id,
 member.canonical_resolution_revision_id,
 resolution.selected_observation_id,
 disposition.source_fact_cell_id,
 disposition.source_publication_id,
 disposition.source_publication_seal_id,
 disposition.source_publication_member_id,
 disposition.source_publication_member_sha256,
 disposition.source_record_commitment_sha256,
 disposition.binding_revision_id,
 disposition.binding_commitment_sha256,
 binding.mapping_revision_id,
 disposition.mapping_commitment_sha256,
 definition.metric_definition_revision_id,
 definition.commitment_sha256 AS metric_definition_commitment_sha256,
 definition.definition_text,
 definition.aliases_json,
 canonical_cell.reporting_entity_id,
 canonical_cell.scope_security_id,
 metric.canonical_name,
 canonical_cell.period_kind,
 canonical_cell.period_start,
 canonical_cell.period_end,
 canonical_seal.dimension_set_json,
 source_cell.unit_key,
 source_cell.currency,
 observation.value_kind,
 observation.numeric_value,
 observation.text_value,
 observation.document_version_id,
 observation.evidence_node_id,
 observation.source_locator_json,
 observation.source_locator_sha256
FROM canonical_fact_resolution_snapshot_members member
JOIN canonical_fact_resolution_revisions resolution
 ON resolution.canonical_resolution_revision_id=
    member.canonical_resolution_revision_id
 AND resolution.status='resolved'
JOIN canonical_fact_candidate_dispositions disposition
 ON disposition.candidate_universe_id=resolution.candidate_universe_id
 AND disposition.observation_id=resolution.selected_observation_id
 AND disposition.eligibility='eligible'
JOIN fact_observations_v2 observation
 ON observation.observation_id=resolution.selected_observation_id
JOIN evidence_nodes evidence_node
 ON evidence_node.node_id=observation.evidence_node_id
JOIN evidence_extraction_runs evidence_run
 ON evidence_run.extraction_run_id=evidence_node.extraction_run_id
 AND evidence_run.document_version_id=observation.document_version_id
JOIN fact_cells_v2 source_cell
 ON source_cell.fact_cell_id=disposition.source_fact_cell_id
JOIN source_fact_publication_seals publication_seal
 ON publication_seal.publication_seal_id=disposition.source_publication_seal_id
 AND publication_seal.publication_id=disposition.source_publication_id
JOIN source_fact_publication_members publication_member
 ON publication_member.publication_member_id=
    disposition.source_publication_member_id
 AND publication_member.publication_id=disposition.source_publication_id
 AND publication_member.record_id=resolution.selected_observation_id
 AND publication_member.canonical_member_sha256=
    disposition.source_publication_member_sha256
 AND publication_member.record_commitment_sha256=
    disposition.source_record_commitment_sha256
JOIN fact_cell_canonical_binding_revisions binding
 ON binding.binding_revision_id=disposition.binding_revision_id
 AND binding.binding_status='bound'
 AND binding.canonical_metric_cell_id=member.canonical_metric_cell_id
 AND binding.commitment_sha256=disposition.binding_commitment_sha256
JOIN metric_mapping_revisions mapping
 ON mapping.mapping_revision_id=binding.mapping_revision_id
 AND mapping.commitment_sha256=disposition.mapping_commitment_sha256
JOIN canonical_metric_cells canonical_cell
 ON canonical_cell.canonical_metric_cell_id=member.canonical_metric_cell_id
JOIN canonical_metric_cell_seals canonical_seal
 ON canonical_seal.canonical_metric_cell_id=canonical_cell.canonical_metric_cell_id
JOIN canonical_metrics metric ON metric.metric_id=canonical_cell.metric_id
JOIN canonical_metric_definition_revisions definition
 ON definition.metric_id=canonical_cell.metric_id
JOIN ontology_snapshot_members definition_member
 ON definition_member.ontology_snapshot_id=?
 AND definition_member.member_kind='metric_definition'
 AND definition_member.member_id=definition.metric_definition_revision_id
 AND definition_member.member_sha256=definition.commitment_sha256
WHERE member.resolution_snapshot_id=?
"""

_SELECTED_FACT_SQL = (
    _SELECTED_FACT_BASE + " AND member.canonical_metric_cell_id>? "
    "ORDER BY member.canonical_metric_cell_id LIMIT ?"
)

_CURRENT_STATE_CTE = """
WITH RECURSIVE lineage(generation_id,parent_generation_id,depth) AS (
 SELECT generation_id,parent_generation_id,0
 FROM canonical_fact_projection_generations WHERE generation_id=?
 UNION ALL
 SELECT parent.generation_id,parent.parent_generation_id,lineage.depth+1
 FROM canonical_fact_projection_generations parent
 JOIN lineage ON parent.generation_id=lineage.parent_generation_id
 WHERE lineage.depth<32
),
ranked AS (
 SELECT entry.*,lineage.depth,
 row_number() OVER (
   PARTITION BY entry.canonical_metric_cell_id
   ORDER BY lineage.depth ASC
 ) AS state_rank
 FROM lineage
 JOIN canonical_fact_projection_entries entry
 ON entry.generation_id=lineage.generation_id
),
current_state AS (
 SELECT * FROM ranked WHERE state_rank=1
)
"""
