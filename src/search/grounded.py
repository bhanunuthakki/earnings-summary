"""Typed persistence and retrieval seams for evidence-grounded search.

This module intentionally neither selects an embedding model nor sends an LLM
request.  A future governed provider owns vector creation; this layer records
the resulting artifact metadata and fuses caller-injected vector candidates
with SQLite FTS5 lexical candidates.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.issuer_registry import evidence_document_relation

_SHA_LEN = 64
MembershipStatus = Literal["included", "missing", "quarantined"]
IndexMembershipStatus = Literal["included", "missing", "quarantined", "failed"]
Outcome = Literal["succeeded", "failed"]
IndexKind = Literal["lexical", "vector"]


def _sha(value: str) -> str:
    value = value.lower()
    if len(value) != _SHA_LEN or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


def _optional_sha(value: str | None) -> str | None:
    return None if value is None else _sha(value)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CorpusManifest(_Record):
    manifest_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    corpus_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    selection_config_sha256: str
    selector_code_version: str = Field(min_length=1, max_length=255)
    knowledge_cutoff: datetime | None = None
    supersedes_manifest_id: str | None = Field(default=None, min_length=1, max_length=128)
    recorded_at: datetime

    _config = field_validator("selection_config_sha256")(_sha)


class CorpusDocumentMembership(_Record):
    membership_id: str = Field(min_length=1, max_length=128)
    manifest_id: str = Field(min_length=1, max_length=128)
    expected_document_key: str = Field(min_length=1, max_length=256)
    document_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    membership_status: MembershipStatus
    reason: str = Field(min_length=1)
    recorded_at: datetime

    @model_validator(mode="after")
    def _validate_document_contract(self) -> CorpusDocumentMembership:
        if self.membership_status == "included" and self.document_version_id is None:
            raise ValueError("included corpus membership requires a document version")
        return self


class CorpusManifestSeal(_Record):
    manifest_id: str = Field(min_length=1, max_length=128)
    expected_document_count: int = Field(ge=0)
    membership_digest_sha256: str
    completion_status: Literal["complete", "incomplete"]
    sealed_at: datetime

    _digest = field_validator("membership_digest_sha256")(_sha)


class SearchChunk(_Record):
    chunk_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    manifest_id: str = Field(min_length=1, max_length=128)
    evidence_node_id: str = Field(min_length=1, max_length=128)
    chunk_key: str = Field(min_length=1, max_length=256)
    chunk_revision: int = Field(gt=0)
    text: str = Field(min_length=1)
    content_sha256: str | None = None
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    chunker_config_sha256: str
    chunker_code_version: str = Field(min_length=1, max_length=255)
    available_at: datetime
    recorded_at: datetime

    _content = field_validator("content_sha256")(_sha)
    _config = field_validator("chunker_config_sha256")(_sha)

    @model_validator(mode="after")
    def _validate_text(self) -> SearchChunk:
        if self.char_end < self.char_start:
            raise ValueError("char_end must be greater than or equal to char_start")
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("chunk range must equal the supplied text length")
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.content_sha256 is None:
            object.__setattr__(self, "content_sha256", digest)
        elif self.content_sha256 != digest:
            raise ValueError("content_sha256 must match chunk text")
        return self


class EmbeddingArtifact(_Record):
    embedding_artifact_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    index_run_id: str = Field(min_length=1, max_length=128)
    chunk_id: str = Field(min_length=1, max_length=128)
    purpose: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    dimensions: int = Field(gt=0)
    vector_sha256: str | None = None
    storage_uri: str | None = None
    input_sha256: str
    request_config_sha256: str
    runtime_artifact_sha256: str | None = None
    outcome: Outcome
    failure_reason: str | None = None
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    started_at: datetime
    completed_at: datetime

    _vector = field_validator("vector_sha256")(_optional_sha)
    _input = field_validator("input_sha256")(_sha)
    _config = field_validator("request_config_sha256")(_sha)
    _runtime = field_validator("runtime_artifact_sha256")(_optional_sha)

    @model_validator(mode="after")
    def _validate_outcome_artifact(self) -> EmbeddingArtifact:
        if self.outcome == "succeeded" and (self.vector_sha256 is None or self.storage_uri is None):
            raise ValueError("a successful embedding requires vector hash and storage URI")
        if self.outcome == "failed" and (
            self.vector_sha256 is not None or self.storage_uri is not None
        ):
            raise ValueError("a failed embedding must not claim a stored vector")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


class IndexRun(_Record):
    index_run_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    index_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    manifest_id: str = Field(min_length=1, max_length=128)
    index_kind: IndexKind
    config_sha256: str
    code_version: str = Field(min_length=1, max_length=255)
    outcome: Outcome
    failure_reason: str | None = None
    started_at: datetime
    completed_at: datetime

    _config = field_validator("config_sha256")(_sha)

    @model_validator(mode="after")
    def _validate_clocks(self) -> IndexRun:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


class IndexMembership(_Record):
    index_run_id: str = Field(min_length=1, max_length=128)
    chunk_id: str = Field(min_length=1, max_length=128)
    membership_status: IndexMembershipStatus
    failure_reason: str | None = None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class PersistResult:
    record_id: str
    created: bool


class SearchCapabilityError(RuntimeError):
    """Raised loudly when required lexical-search support is unavailable."""


class GroundedSearchStore:
    """The sole typed append boundary for the search metadata ledger."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist(
        self,
        record: CorpusManifest
        | CorpusDocumentMembership
        | CorpusManifestSeal
        | SearchChunk
        | EmbeddingArtifact
        | IndexRun
        | IndexMembership,
    ) -> PersistResult:
        if isinstance(record, CorpusManifestSeal):
            self._validate_seal(record)
        table, columns, values, identity_column, identity_value = self._statement(record)
        result = self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) ON CONFLICT DO NOTHING",
            values,
        )
        if result.rowcount == 1:
            return PersistResult(identity_value, True)
        existing = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {identity_column} = ?",
            (identity_value,),
        ).fetchone()
        if existing is None or not _same(tuple(existing), values):
            raise ValueError(
                f"immutable {table} identity {identity_value!r} conflicts with existing data"
            )
        return PersistResult(identity_value, False)

    def _validate_seal(self, record: CorpusManifestSeal) -> None:
        rows = self._conn.execute(
            "SELECT membership_id, expected_document_key, document_version_id, membership_status, reason "
            "FROM search_corpus_document_memberships WHERE manifest_id = ? "
            "ORDER BY expected_document_key, membership_id",
            (record.manifest_id,),
        ).fetchall()
        if len(rows) != record.expected_document_count:
            raise ValueError("corpus seal expected_document_count does not match membership")
        digest = _membership_digest(rows)
        if digest != record.membership_digest_sha256:
            raise ValueError("corpus seal membership digest does not match membership")
        incomplete = any(str(row[3]) != "included" for row in rows)
        if (record.completion_status == "complete") == incomplete:
            raise ValueError("corpus seal completion status does not match membership")

    def require_fts5(self) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_lexical_chunks'"
        ).fetchone()
        if row is None:
            raise SearchCapabilityError("SQLite FTS5 lexical index is required but unavailable")

    def _statement(
        self,
        record: CorpusManifest
        | CorpusDocumentMembership
        | CorpusManifestSeal
        | SearchChunk
        | EmbeddingArtifact
        | IndexRun
        | IndexMembership,
    ) -> tuple[str, tuple[str, ...], tuple[object, ...], str, str]:
        if isinstance(record, CorpusManifest):
            return (
                "search_corpus_manifests",
                (
                    "manifest_id",
                    "idempotency_key",
                    "corpus_key",
                    "revision",
                    "selection_config_sha256",
                    "selector_code_version",
                    "knowledge_cutoff",
                    "supersedes_manifest_id",
                    "recorded_at",
                ),
                (
                    record.manifest_id,
                    record.idempotency_key,
                    record.corpus_key,
                    record.revision,
                    record.selection_config_sha256,
                    record.selector_code_version,
                    record.knowledge_cutoff,
                    record.supersedes_manifest_id,
                    record.recorded_at,
                ),
                "manifest_id",
                record.manifest_id,
            )
        if isinstance(record, CorpusDocumentMembership):
            return (
                "search_corpus_document_memberships",
                (
                    "membership_id",
                    "manifest_id",
                    "expected_document_key",
                    "document_version_id",
                    "membership_status",
                    "reason",
                    "recorded_at",
                ),
                (
                    record.membership_id,
                    record.manifest_id,
                    record.expected_document_key,
                    record.document_version_id,
                    record.membership_status,
                    record.reason,
                    record.recorded_at,
                ),
                "membership_id",
                record.membership_id,
            )
        if isinstance(record, CorpusManifestSeal):
            return (
                "search_corpus_manifest_seals",
                (
                    "manifest_id",
                    "expected_document_count",
                    "membership_digest_sha256",
                    "completion_status",
                    "sealed_at",
                ),
                (
                    record.manifest_id,
                    record.expected_document_count,
                    record.membership_digest_sha256,
                    record.completion_status,
                    record.sealed_at,
                ),
                "manifest_id",
                record.manifest_id,
            )
        if isinstance(record, SearchChunk):
            return (
                "search_chunks",
                (
                    "chunk_id",
                    "idempotency_key",
                    "manifest_id",
                    "evidence_node_id",
                    "chunk_key",
                    "chunk_revision",
                    "text",
                    "content_sha256",
                    "char_start",
                    "char_end",
                    "chunker_config_sha256",
                    "chunker_code_version",
                    "available_at",
                    "recorded_at",
                ),
                (
                    record.chunk_id,
                    record.idempotency_key,
                    record.manifest_id,
                    record.evidence_node_id,
                    record.chunk_key,
                    record.chunk_revision,
                    record.text,
                    record.content_sha256,
                    record.char_start,
                    record.char_end,
                    record.chunker_config_sha256,
                    record.chunker_code_version,
                    record.available_at,
                    record.recorded_at,
                ),
                "chunk_id",
                record.chunk_id,
            )
        if isinstance(record, EmbeddingArtifact):
            columns = (
                "embedding_artifact_id",
                "idempotency_key",
                "index_run_id",
                "chunk_id",
                "purpose",
                "provider",
                "model",
                "dimensions",
                "vector_sha256",
                "storage_uri",
                "input_sha256",
                "request_config_sha256",
                "runtime_artifact_sha256",
                "outcome",
                "failure_reason",
                "cost_usd",
                "latency_ms",
                "started_at",
                "completed_at",
            )
            values = (
                record.embedding_artifact_id,
                record.idempotency_key,
                record.index_run_id,
                record.chunk_id,
                record.purpose,
                record.provider,
                record.model,
                record.dimensions,
                record.vector_sha256,
                record.storage_uri,
                record.input_sha256,
                record.request_config_sha256,
                record.runtime_artifact_sha256,
                record.outcome,
                record.failure_reason,
                record.cost_usd,
                record.latency_ms,
                record.started_at,
                record.completed_at,
            )
            has_runtime_column = any(
                str(row[1]) == "runtime_artifact_sha256"
                for row in self._conn.execute("PRAGMA table_info(search_embedding_artifacts)")
            )
            if not has_runtime_column:
                columns = (*columns[:12], *columns[13:])
                values = (*values[:12], *values[13:])
            return (
                "search_embedding_artifacts",
                columns,
                values,
                "embedding_artifact_id",
                record.embedding_artifact_id,
            )
        if isinstance(record, IndexRun):
            return (
                "search_index_runs",
                (
                    "index_run_id",
                    "idempotency_key",
                    "index_key",
                    "revision",
                    "manifest_id",
                    "index_kind",
                    "config_sha256",
                    "code_version",
                    "outcome",
                    "failure_reason",
                    "started_at",
                    "completed_at",
                ),
                (
                    record.index_run_id,
                    record.idempotency_key,
                    record.index_key,
                    record.revision,
                    record.manifest_id,
                    record.index_kind,
                    record.config_sha256,
                    record.code_version,
                    record.outcome,
                    record.failure_reason,
                    record.started_at,
                    record.completed_at,
                ),
                "index_run_id",
                record.index_run_id,
            )
        return (
            "search_index_memberships",
            ("index_run_id", "chunk_id", "membership_status", "failure_reason", "recorded_at"),
            (
                record.index_run_id,
                record.chunk_id,
                record.membership_status,
                record.failure_reason,
                record.recorded_at,
            ),
            "index_run_id || ':' || chunk_id",
            f"{record.index_run_id}:{record.chunk_id}",
        )


def _same(existing: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    if len(existing) != len(expected):
        return False
    for stored, supplied in zip(existing, expected, strict=True):
        if isinstance(supplied, datetime):
            if datetime.fromisoformat(str(stored)).replace(tzinfo=None) != supplied.replace(
                tzinfo=None
            ):
                return False
        elif stored != supplied:
            return False
    return True


def membership_digest(
    memberships: Sequence[CorpusDocumentMembership],
) -> str:
    """Return the canonical seal digest for a complete manifest membership set."""
    rows = [
        (
            membership.membership_id,
            membership.expected_document_key,
            membership.document_version_id,
            membership.membership_status,
            membership.reason,
        )
        for membership in memberships
    ]
    return _membership_digest(sorted(rows, key=lambda row: (str(row[1]), str(row[0]))))


def _membership_digest(rows: Sequence[Sequence[object]]) -> str:
    payload = [list(row) for row in rows]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


class SearchFilter(_Record):
    issuer_id: str | None = None
    ticker: str | None = None
    form_types: tuple[str, ...] = ()
    period_start: datetime | None = None
    period_end: datetime | None = None
    node_kinds: tuple[str, ...] = ()
    knowledge_cutoff: datetime | None = None


@dataclass(frozen=True, slots=True)
class VectorCandidate:
    chunk_id: str
    score: float
    index_run_id: str | None = None


class VectorBackend(Protocol):
    def search(
        self, query: str, filters: SearchFilter, limit: int
    ) -> Sequence[VectorCandidate]: ...


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    chunk_id: str
    score: float
    text: str
    node_id: str
    node_kind: str
    locator: dict[str, object] | None
    document_version_id: str
    issuer_id: str
    ticker: str | None
    form_type: str
    period_start: datetime | None
    period_end: datetime | None
    source_url: str
    source_published_at: datetime | None
    filing_at: datetime | None
    accepted_at: datetime | None
    observed_at: datetime
    retrieved_at: datetime
    recorded_issuer_id: str | None = None


class HybridRetriever:
    def __init__(
        self, conn: sqlite3.Connection, vector_backend: VectorBackend | None = None
    ) -> None:
        self._conn = conn
        self._vectors = vector_backend
        self._verified_lexical_manifests: dict[str, tuple[int, int]] = {}

    def search(
        self,
        query: str,
        manifest_id: str,
        filters: SearchFilter | None = None,
        limit: int = 10,
        rrf_k: int = 60,
    ) -> list[EvidenceBundle]:
        if limit <= 0:
            return []
        filters = SearchFilter() if filters is None else filters
        self._require_manifest_ready(manifest_id)
        lexical = self._lexical(query, manifest_id, filters, limit)
        vectors = (
            list(self._vectors.search(query, filters, limit)) if self._vectors is not None else []
        )
        vectors = [
            candidate
            for candidate in vectors
            if candidate.index_run_id is not None
            and self._has_successful_index_membership(
                candidate.chunk_id, manifest_id, "vector", candidate.index_run_id
            )
            and self._bundle(candidate.chunk_id, manifest_id, 0.0, filters) is not None
        ]
        ranks: dict[str, float] = {}
        for candidates in (lexical, vectors):
            for rank, candidate in enumerate(candidates, start=1):
                ranks[candidate.chunk_id] = ranks.get(candidate.chunk_id, 0.0) + 1.0 / (
                    rrf_k + rank
                )
        ids = [
            chunk_id
            for chunk_id, _ in sorted(ranks.items(), key=lambda entry: (-entry[1], entry[0]))[
                :limit
            ]
        ]
        bundles: list[EvidenceBundle] = []
        for chunk_id in ids:
            bundle = self._bundle(chunk_id, manifest_id, ranks[chunk_id], filters)
            if bundle is not None:
                bundles.append(bundle)
        return bundles

    def _lexical(
        self, query: str, manifest_id: str, filters: SearchFilter, limit: int
    ) -> list[VectorCandidate]:
        self._require_fts5()
        fts_query = _fts_query(query)
        if not fts_query:
            return []
        where, params = _filter_sql(filters)
        document_relation = evidence_document_relation(self._conn)
        rows = self._conn.execute(
            "SELECT lex.chunk_id FROM search_lexical_chunks AS lex "
            "JOIN search_chunks AS chunk ON chunk.chunk_id = lex.chunk_id "
            "JOIN evidence_nodes AS node ON node.node_id = chunk.evidence_node_id "
            "JOIN evidence_extraction_runs AS run ON run.extraction_run_id = node.extraction_run_id "
            f"JOIN {document_relation} AS doc "
            "ON doc.document_version_id = run.document_version_id "
            "JOIN evidence_source_observations AS source ON source.observation_id = doc.observation_id "
            "WHERE search_lexical_chunks MATCH ? AND chunk.manifest_id = ? "
            + where
            + " ORDER BY bm25(search_lexical_chunks), lex.chunk_id LIMIT ?",
            (fts_query, manifest_id, *params, limit),
        ).fetchall()
        return [VectorCandidate(chunk_id=str(row[0]), score=0.0) for row in rows]

    def _bundle(
        self, chunk_id: str, manifest_id: str, score: float, filters: SearchFilter
    ) -> EvidenceBundle | None:
        where, params = _filter_sql(filters)
        document_relation = evidence_document_relation(self._conn)
        recorded_issuer_sql = (
            "doc.recorded_issuer_id"
            if document_relation == "v_evidence_document_versions_canonical"
            else "doc.issuer_id"
        )
        row = self._conn.execute(
            "SELECT chunk.chunk_id, chunk.text, node.node_id, node.node_kind, node.locator_json, "
            f"doc.document_version_id, doc.issuer_id, {recorded_issuer_sql}, "
            "doc.ticker, doc.form_type, doc.period_start, doc.period_end, "
            "source.source_url, source.source_published_at, source.filing_at, source.accepted_at, source.observed_at, source.retrieved_at "
            "FROM search_chunks AS chunk JOIN evidence_nodes AS node ON node.node_id = chunk.evidence_node_id "
            "JOIN evidence_extraction_runs AS run ON run.extraction_run_id = node.extraction_run_id "
            f"JOIN {document_relation} AS doc "
            "ON doc.document_version_id = run.document_version_id "
            "JOIN evidence_source_observations AS source ON source.observation_id = doc.observation_id "
            "WHERE chunk.chunk_id = ? AND chunk.manifest_id = ?" + where,
            (chunk_id, manifest_id, *params),
        ).fetchone()
        if row is None:
            return None
        return EvidenceBundle(
            chunk_id=str(row[0]),
            score=score,
            text=str(row[1]),
            node_id=str(row[2]),
            node_kind=str(row[3]),
            locator=None if row[4] is None else json.loads(str(row[4])),
            document_version_id=str(row[5]),
            issuer_id=str(row[6]),
            recorded_issuer_id=str(row[7]),
            ticker=None if row[8] is None else str(row[8]),
            form_type=str(row[9]),
            period_start=_dt(row[10]),
            period_end=_dt(row[11]),
            source_url=str(row[12]),
            source_published_at=_dt(row[13]),
            filing_at=_dt(row[14]),
            accepted_at=_dt(row[15]),
            observed_at=_dt(row[16]) or datetime.min,
            retrieved_at=_dt(row[17]) or datetime.min,
        )

    def _require_fts5(self) -> None:
        if (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_lexical_chunks'"
            ).fetchone()
            is None
        ):
            raise SearchCapabilityError("SQLite FTS5 lexical index is required but unavailable")

    def _require_manifest_ready(self, manifest_id: str) -> None:
        seal = self._conn.execute(
            "SELECT expected_document_count, membership_digest_sha256, completion_status "
            "FROM search_corpus_manifest_seals WHERE manifest_id = ?",
            (manifest_id,),
        ).fetchone()
        if seal is None:
            raise ValueError("grounded search requires a sealed corpus manifest")
        if str(seal[2]) != "complete":
            raise ValueError("grounded search requires a complete corpus manifest")
        rows = self._conn.execute(
            "SELECT membership_id, expected_document_key, document_version_id, membership_status, reason "
            "FROM search_corpus_document_memberships WHERE manifest_id = ? "
            "ORDER BY expected_document_key, membership_id",
            (manifest_id,),
        ).fetchall()
        if len(rows) != int(seal[0]) or _membership_digest(rows) != str(seal[1]):
            raise ValueError("corpus manifest seal no longer matches membership")
        for membership in rows:
            reason = str(membership[4])
            prefix = "semantic:not_required:"
            if not reason.startswith(prefix):
                continue
            assessment_id = reason.removeprefix(prefix)
            current = self._conn.execute(
                "SELECT 1 FROM v_document_semantic_dispositions_current "
                "WHERE assessment_id = ? AND document_version_id = ? "
                "AND semantic_status = 'not_required' AND decision_kind = 'human' "
                "AND reviewer_identity IS NOT NULL",
                (assessment_id, membership[2]),
            ).fetchone()
            if current is None:
                raise ValueError(
                    "corpus semantic exclusion is no longer the current human decision"
                )
        lexical = self._conn.execute(
            "SELECT index_run_id, config_sha256 FROM v_search_index_successful "
            "WHERE manifest_id = ? AND index_kind = 'lexical'",
            (manifest_id,),
        ).fetchone()
        if lexical is None:
            raise ValueError("grounded search requires a successful current lexical index")
        change_token = (
            int(self._conn.execute("PRAGMA data_version").fetchone()[0]),
            self._conn.total_changes,
        )
        if self._verified_lexical_manifests.get(manifest_id) != change_token:
            # Re-hashing the live projection queries the FTS virtual table.
            # Preserve the public fail-closed capability contract when that
            # table is unavailable instead of leaking sqlite.OperationalError.
            self._require_fts5()
            # Imported lazily to keep the core record module independent from
            # the corpus builder at import time.
            from search.corpus_builder import lexical_index_config_sha256

            expected_config = lexical_index_config_sha256(
                self._conn,
                manifest_id=manifest_id,
            )
            if str(lexical[1]) != expected_config:
                raise ValueError(
                    "lexical index run does not commit to the exact sealed-manifest chunk set"
                )
            self._verified_lexical_manifests[manifest_id] = change_token

    def _has_successful_index_membership(
        self,
        chunk_id: str,
        manifest_id: str,
        index_kind: IndexKind,
        index_run_id: str | None = None,
    ) -> bool:
        run_filter = "" if index_run_id is None else " AND indexed.index_run_id = ?"
        params: tuple[object, ...] = (chunk_id, manifest_id, index_kind)
        if index_run_id is not None:
            params += (index_run_id,)
        return (
            self._conn.execute(
                "SELECT 1 FROM search_index_memberships AS membership "
                "JOIN v_search_index_successful AS indexed ON indexed.index_run_id = membership.index_run_id "
                "WHERE membership.chunk_id = ? AND membership.membership_status = 'included' "
                "AND indexed.manifest_id = ? AND indexed.index_kind = ?" + run_filter,
                params,
            ).fetchone()
            is not None
        )


def _filter_sql(filters: SearchFilter) -> tuple[str, list[object]]:
    parts: list[str] = []
    params: list[object] = []
    for column, value in (("doc.issuer_id", filters.issuer_id), ("doc.ticker", filters.ticker)):
        if value is not None:
            parts.append(f" AND {column} = ?")
            params.append(value)
    for column, values in (
        ("doc.form_type", filters.form_types),
        ("node.node_kind", filters.node_kinds),
    ):
        if values:
            parts.append(f" AND {column} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
    if filters.period_start is not None:
        parts.append(" AND doc.period_end >= ?")
        params.append(filters.period_start)
    if filters.period_end is not None:
        parts.append(" AND doc.period_start <= ?")
        params.append(filters.period_end)
    if filters.knowledge_cutoff is not None:
        parts.append(
            " AND source.observed_at <= ? AND source.retrieved_at <= ? AND chunk.available_at <= ?"
        )
        params.extend(
            (filters.knowledge_cutoff, filters.knowledge_cutoff, filters.knowledge_cutoff)
        )
    return "".join(parts), params


def _fts_query(query: str) -> str:
    """Compile untrusted natural language into inert FTS5 token operands."""

    tokens = list(dict.fromkeys(re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", query)))
    return " OR ".join(f'"{token}"' for token in tokens)


def _dt(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))
