"""Bounded, locally recomputable semantic retrieval over a sealed vector projection.

The external vector store is only a projection.  This module admits results by
re-reading every canonical float32 vector in a bounded sealed projection,
recomputing normalized cosine scores locally, and proving the complete ordered
top-k set.  No ANN response or opaque service receipt is trusted.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.search_index_lineage import (
    SearchProjectionSeal,
    load_projection_seal,
    verify_ledger_projection_seal,
)
from search.canonical_fact_projection import canonical_decimal
from search.embedding_promotion import (
    EmbeddingPromotion,
    current_promotion,
)
from search.embedding_runtime_artifact import parse_runtime_artifact
from search.local_vector import (
    EmbeddingModelSpec,
    FastEmbedEncoder,
    LanceVectorIndex,
    PassageQueryEncoder,
    canonical_float32_vector,
    vector_records_digest,
    vector_sha256,
)

EXACT_SEMANTIC_ALGORITHM_VERSION = "canonical-float32-normalized-cosine.v1"
MAX_EXACT_VECTOR_ROWS = 100_000


class ExactSemanticError(RuntimeError):
    """A sealed semantic coordinate could not be reproduced exactly."""


class ExactVectorProjection(Protocol):
    def read_projection(
        self, index_run_id: str, *, expected_count: int
    ) -> list[dict[str, object]]: ...

    def published_storage_uri(self, index_run_id: str) -> str: ...


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExactSemanticCandidate(_Frozen):
    chunk_id: str = Field(min_length=1, max_length=128)
    score: str

    @model_validator(mode="after")
    def _score_range(self) -> Self:
        score = float(self.score)
        if not math.isfinite(score) or score < 0 or score > 1:
            raise ValueError("exact semantic score must be finite and in [0,1]")
        if self.score != canonical_decimal(self.score):
            raise ValueError("exact semantic score must use canonical decimal form")
        return self


class ExactSemanticBackendReceipt(_Frozen):
    algorithm: str = EXACT_SEMANTIC_ALGORITHM_VERSION
    query_sha256: str
    query_vector_sha256: str
    vector_index_run_id: str = Field(min_length=1, max_length=128)
    projection_seal_id: str = Field(min_length=1, max_length=128)
    projection_seal_sha256: str
    projection_records_sha256: str
    artifact_set_sha256: str
    embedding_promotion_id: str = Field(min_length=1, max_length=128)
    embedding_promotion_sha256: str
    promotion_eval_sha256: str
    promotion_golden_sha256: str
    runtime_artifact_sha256: str
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    dimensions: int = Field(gt=0)
    config_sha256: str
    storage_uri: str = Field(min_length=1)
    exact_row_cap: int = Field(gt=0, le=MAX_EXACT_VECTOR_ROWS)
    rows_scanned: int = Field(ge=1)
    requested_limit: int = Field(gt=0)

    @field_validator(
        "query_sha256",
        "query_vector_sha256",
        "projection_seal_sha256",
        "projection_records_sha256",
        "artifact_set_sha256",
        "embedding_promotion_sha256",
        "promotion_eval_sha256",
        "promotion_golden_sha256",
        "runtime_artifact_sha256",
        "config_sha256",
    )
    @classmethod
    def _sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("exact semantic hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _bounded_scan(self) -> Self:
        if self.rows_scanned > self.exact_row_cap:
            raise ValueError("exact semantic scan exceeds its declared row cap")
        return self


class ExactSemanticEvidence(_Frozen):
    candidates: tuple[ExactSemanticCandidate, ...]
    backend: ExactSemanticBackendReceipt


class ExactSemanticRuntime:
    """One exact runtime bound to one current promotion and vector projection."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        index: ExactVectorProjection,
        encoder: PassageQueryEncoder,
        seal: SearchProjectionSeal,
        promotion: EmbeddingPromotion,
        exact_row_cap: int,
    ) -> None:
        if exact_row_cap <= 0 or exact_row_cap > MAX_EXACT_VECTOR_ROWS:
            raise ValueError(f"exact_row_cap must be in [1,{MAX_EXACT_VECTOR_ROWS}]")
        self._conn = conn
        self._index = index
        self._encoder = encoder
        self._seal = seal
        self._promotion = promotion
        self._exact_row_cap = exact_row_cap
        self._verify_coordinates()

    @property
    def vector_index_run_id(self) -> str:
        return self._seal.index_run_id

    @property
    def embedding_promotion_id(self) -> str:
        return self._promotion.promotion_id

    @property
    def manifest_id(self) -> str:
        return self._seal.manifest_id

    @classmethod
    def from_local_ledger(
        cls,
        conn: sqlite3.Connection,
        *,
        vector_index_run_id: str,
        embedding_promotion_id: str,
        runtime_root: Path | None = None,
        exact_row_cap: int = MAX_EXACT_VECTOR_ROWS,
    ) -> ExactSemanticRuntime:
        """Resolve the sealed index path and approved model from the local ledger."""

        seal, promotion = _load_coordinates(
            conn,
            vector_index_run_id=vector_index_run_id,
            embedding_promotion_id=embedding_promotion_id,
        )
        if seal.chunk_count > exact_row_cap:
            raise ExactSemanticError("sealed vector projection exceeds exact row cap")
        index_root = _index_root_from_storage_uri(seal.storage_uri)
        spec = EmbeddingModelSpec(
            provider=promotion.provider,
            model=promotion.model,
            dimensions=promotion.dimensions,
        )
        if (
            runtime_root is None
            or promotion.runtime_artifact_json is None
            or promotion.runtime_artifact_sha256 is None
        ):
            raise ExactSemanticError(
                "semantic runtime requires an explicit local runtime artifact root"
            )
        runtime_artifact = parse_runtime_artifact(
            promotion.runtime_artifact_json, promotion.runtime_artifact_sha256
        )
        return cls(
            conn,
            index=LanceVectorIndex(index_root),
            encoder=FastEmbedEncoder.from_spec(
                spec,
                runtime_artifact=runtime_artifact,
                runtime_root=runtime_root,
            ),
            seal=seal,
            promotion=promotion,
            exact_row_cap=exact_row_cap,
        )

    @classmethod
    def from_verified_components_for_test(
        cls,
        conn: sqlite3.Connection,
        *,
        vector_index_run_id: str,
        embedding_promotion_id: str,
        index: ExactVectorProjection,
        encoder: PassageQueryEncoder,
        exact_row_cap: int = MAX_EXACT_VECTOR_ROWS,
    ) -> ExactSemanticRuntime:
        """Test seam; ledger coordinates remain fully verified and cannot be replaced."""

        seal, promotion = _load_coordinates(
            conn,
            vector_index_run_id=vector_index_run_id,
            embedding_promotion_id=embedding_promotion_id,
        )
        return cls(
            conn,
            index=index,
            encoder=encoder,
            seal=seal,
            promotion=promotion,
            exact_row_cap=exact_row_cap,
        )

    def search(self, query_text: str, *, limit: int) -> ExactSemanticEvidence:
        if not query_text:
            raise ExactSemanticError("exact semantic query must not be empty")
        if limit <= 0:
            raise ExactSemanticError("exact semantic limit must be positive")
        self._verify_coordinates()
        if self._seal.chunk_count > self._exact_row_cap:
            raise ExactSemanticError("sealed vector projection exceeds exact row cap")
        rows = self._read_and_verify_projection()
        query_vector = self._encode_query(query_text)
        candidates = tuple(
            sorted(
                (
                    ExactSemanticCandidate(
                        chunk_id=_chunk_id(row),
                        score=_normalized_cosine(
                            query_vector,
                            _canonical_vector(
                                row["vector"],
                                dimensions=self._promotion.dimensions,
                            ),
                        ),
                    )
                    for row in rows
                ),
                key=lambda item: (-Decimal(item.score), item.chunk_id),
            )[:limit]
        )
        return ExactSemanticEvidence(
            candidates=candidates,
            backend=ExactSemanticBackendReceipt(
                query_sha256=_digest_text(query_text),
                query_vector_sha256=vector_sha256(
                    query_vector, dimensions=self._promotion.dimensions
                ),
                vector_index_run_id=self._seal.index_run_id,
                projection_seal_id=self._seal.projection_seal_id,
                projection_seal_sha256=_model_digest(self._seal),
                projection_records_sha256=self._seal.projection_records_sha256,
                artifact_set_sha256=cast(str, self._seal.artifact_set_sha256),
                embedding_promotion_id=self._promotion.promotion_id,
                embedding_promotion_sha256=_model_digest(self._promotion),
                promotion_eval_sha256=(self._promotion.evaluation_artifact_sha256),
                promotion_golden_sha256=self._promotion.golden_sha256,
                runtime_artifact_sha256=cast(str, self._promotion.runtime_artifact_sha256),
                provider=self._promotion.provider,
                model=self._promotion.model,
                dimensions=self._promotion.dimensions,
                config_sha256=self._seal.config_sha256,
                storage_uri=self._seal.storage_uri,
                exact_row_cap=self._exact_row_cap,
                rows_scanned=len(rows),
                requested_limit=limit,
            ),
        )

    def verify(
        self,
        evidence: ExactSemanticEvidence,
        *,
        query_text: str,
        limit: int,
    ) -> None:
        """Recompute the full bounded scan and exact ordered top-k commitment."""

        expected = self.search(query_text, limit=limit)
        if evidence != expected:
            raise ExactSemanticError(
                "semantic receipt does not match recomputed sealed exact top-k"
            )

    def _verify_coordinates(self) -> None:
        current = current_promotion(self._conn)
        if current is None or current != self._promotion:
            raise ExactSemanticError(
                "semantic runtime is not bound to the current ledger promotion"
            )
        stored_seal = load_projection_seal(self._conn, index_run_id=self._seal.index_run_id)
        if stored_seal is None or stored_seal != self._seal:
            raise ExactSemanticError("semantic runtime projection seal drifted")
        if (
            self._seal.index_kind != "vector"
            or self._seal.provider != self._promotion.provider
            or self._seal.model != self._promotion.model
            or self._seal.dimensions != self._promotion.dimensions
            or self._seal.artifact_set_sha256 is None
            or self._promotion.runtime_artifact_sha256 is None
            or self._promotion.runtime_artifact_json is None
            or self._seal.runtime_artifact_sha256 != self._promotion.runtime_artifact_sha256
        ):
            raise ExactSemanticError("semantic promotion and vector projection identities differ")
        try:
            verify_ledger_projection_seal(self._conn, self._seal)
        except (RuntimeError, ValueError) as exc:
            raise ExactSemanticError("semantic vector ledger commitments no longer verify") from exc
        if self._index.published_storage_uri(self._seal.index_run_id) != (self._seal.storage_uri):
            raise ExactSemanticError("semantic vector storage URI differs from seal")

    def _read_and_verify_projection(self) -> list[dict[str, object]]:
        try:
            rows = self._index.read_projection(
                self._seal.index_run_id,
                expected_count=self._seal.chunk_count,
            )
            records_sha256 = vector_records_digest(rows)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ExactSemanticError(
                "semantic vector projection cannot be read canonically"
            ) from exc
        if (
            len(rows) != self._seal.chunk_count
            or records_sha256 != self._seal.projection_records_sha256
        ):
            raise ExactSemanticError("semantic vector projection differs from immutable seal")
        ledger_rows = self._conn.execute(
            "SELECT artifact.chunk_id,artifact.vector_sha256,"
            "artifact.input_sha256,artifact.provider,artifact.model,"
            "artifact.dimensions "
            ",artifact.runtime_artifact_sha256 "
            "FROM search_embedding_artifacts AS artifact "
            "JOIN search_index_memberships AS membership "
            "ON membership.index_run_id=artifact.index_run_id "
            "AND membership.chunk_id=artifact.chunk_id "
            "WHERE artifact.index_run_id=? AND artifact.outcome='succeeded' "
            "AND membership.membership_status='included' "
            "ORDER BY artifact.chunk_id",
            (self._seal.index_run_id,),
        ).fetchall()
        external = sorted(rows, key=lambda row: _chunk_id(row))
        if len(ledger_rows) != len(external):
            raise ExactSemanticError("semantic vector ledger coverage is incomplete")
        for row, ledger in zip(external, ledger_rows, strict=True):
            dimensions = row.get("dimensions")
            if isinstance(dimensions, bool) or not isinstance(dimensions, int):
                raise ExactSemanticError("semantic vector row has invalid dimensions")
            identity = (
                _chunk_id(row),
                str(row["vector_sha256"]),
                str(row["input_sha256"]),
                self._promotion.provider,
                self._promotion.model,
                dimensions,
                str(row.get("runtime_artifact_sha256")),
            )
            expected = (
                str(ledger[0]),
                str(ledger[1]),
                str(ledger[2]),
                str(ledger[3]),
                str(ledger[4]),
                int(ledger[5]),
                str(ledger[6]),
            )
            if identity != expected or dimensions != self._promotion.dimensions:
                raise ExactSemanticError(
                    "semantic external vector row differs from ledger artifact"
                )
        return rows

    def _encode_query(self, query_text: str) -> list[float]:
        try:
            encoded = self._encoder.encode_queries([query_text])
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ExactSemanticError("semantic query encoding failed") from exc
        if len(encoded) != 1:
            raise ExactSemanticError("semantic query encoder must return exactly one vector")
        return _canonical_vector(encoded[0], dimensions=self._promotion.dimensions)


def evidence_from_json(
    *,
    candidates: Sequence[dict[str, object]],
    backend_receipt_json: str,
) -> ExactSemanticEvidence:
    try:
        backend_raw = json.loads(backend_receipt_json)
    except json.JSONDecodeError as exc:
        raise ExactSemanticError("semantic backend receipt is not JSON") from exc
    if not isinstance(backend_raw, dict):
        raise ExactSemanticError("semantic backend receipt must be an object")
    return ExactSemanticEvidence(
        candidates=tuple(
            ExactSemanticCandidate.model_validate(candidate) for candidate in candidates
        ),
        backend=ExactSemanticBackendReceipt.model_validate(backend_raw),
    )


def backend_receipt_json(evidence: ExactSemanticEvidence) -> str:
    return _canonical_json(evidence.backend.model_dump(mode="json"))


def _load_coordinates(
    conn: sqlite3.Connection,
    *,
    vector_index_run_id: str,
    embedding_promotion_id: str,
) -> tuple[SearchProjectionSeal, EmbeddingPromotion]:
    promotion = current_promotion(conn)
    if promotion is None or promotion.promotion_id != embedding_promotion_id:
        raise ExactSemanticError(
            "requested embedding promotion is not the current approved promotion"
        )
    seal = load_projection_seal(conn, index_run_id=vector_index_run_id)
    if seal is None:
        raise ExactSemanticError("requested vector projection seal is absent")
    return seal, promotion


def _index_root_from_storage_uri(storage_uri: str) -> Path:
    prefix = "lance://"
    suffix = "#evidence_chunks"
    if not storage_uri.startswith(prefix) or not storage_uri.endswith(suffix):
        raise ExactSemanticError("sealed vector storage URI is not a local Lance projection")
    raw = storage_uri[len(prefix) : -len(suffix)]
    path = Path(raw)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise ExactSemanticError("sealed vector storage URI is not an absolute safe path")
    return path.parent


def _canonical_vector(values: object, *, dimensions: int) -> list[float]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise ExactSemanticError("semantic vector is not a numeric sequence")
    try:
        packed = canonical_float32_vector(cast(Sequence[float], values), dimensions=dimensions)
    except (TypeError, ValueError) as exc:
        raise ExactSemanticError("semantic vector is not canonicalizable") from exc
    return list(struct.unpack(f"<{dimensions}f", packed))


def _normalized_cosine(query: Sequence[float], document: Sequence[float]) -> str:
    dot = math.fsum(left * right for left, right in zip(query, document, strict=True))
    query_norm = math.sqrt(math.fsum(value * value for value in query))
    document_norm = math.sqrt(math.fsum(value * value for value in document))
    if query_norm == 0 or document_norm == 0:
        raise ExactSemanticError("exact cosine is undefined for a zero vector")
    cosine = dot / (query_norm * document_norm)
    normalized = (max(-1.0, min(1.0, cosine)) + 1.0) / 2.0
    return canonical_decimal(format(normalized, ".17g"))


def _chunk_id(row: dict[str, object]) -> str:
    value = row.get("chunk_id")
    if not isinstance(value, str) or not value:
        raise ExactSemanticError("semantic vector row lacks an immutable chunk ID")
    return value


def _model_digest(model: BaseModel) -> str:
    return _digest_text(_canonical_json(model.model_dump(mode="json")))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
