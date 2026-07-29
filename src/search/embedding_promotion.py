"""Owner-approved routing boundary for evaluated evidence embedding models."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.search_index_lineage import (
    load_projection_seal,
    verify_ledger_projection_seal,
)
from search.embedding_eval import (
    CandidateMetrics,
    EmbeddingRecommendationArtifact,
)
from search.embedding_runtime_artifact import (
    EmbeddingRuntimeArtifact,
    parse_runtime_artifact,
)
from search.local_vector import (
    EmbeddingModelSpec,
    FastEmbedEncoder,
    LanceVectorBackend,
    LanceVectorIndex,
)

PURPOSE = "evidence_vector_retrieval"
_RUNTIME_ENABLED_ENV = "EVIDENCE_VECTOR_RUNTIME_ENABLED"
_INDEX_ROOT_ENV = "EVIDENCE_VECTOR_INDEX_ROOT"
_RUNTIME_ROOT_ENV = "EVIDENCE_VECTOR_RUNTIME_ROOT"


class EmbeddingPromotion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    promotion_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    purpose: str = Field(default=PURPOSE, min_length=1, max_length=64)
    revision: int = Field(gt=0)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    dimensions: int = Field(gt=0)
    golden_sha256: str
    evaluation_artifact_sha256: str
    evaluation_metrics_json: str = Field(min_length=2)
    runtime_artifact_json: str | None = None
    runtime_artifact_sha256: str | None = None
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at: datetime
    knowledge_at: datetime | None = None
    recorded_at: datetime | None = None
    supersedes_promotion_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("golden_sha256", "evaluation_artifact_sha256", "runtime_artifact_sha256")
    @classmethod
    def _sha(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("promotion hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _revision(self) -> Self:
        if (self.revision == 1) != (self.supersedes_promotion_id is None):
            raise ValueError("promotion revision requires the exact prior promotion")
        if (self.runtime_artifact_json is None) != (self.runtime_artifact_sha256 is None):
            raise ValueError("runtime artifact JSON and digest must be supplied together")
        if self.runtime_artifact_json is not None:
            artifact = parse_runtime_artifact(
                self.runtime_artifact_json, self.runtime_artifact_sha256 or ""
            )
            if (
                artifact.provider,
                artifact.model,
                artifact.dimensions,
            ) != (self.provider, self.model, self.dimensions):
                raise ValueError("promotion model identity differs from runtime artifact")
        if (self.knowledge_at is None) != (self.recorded_at is None):
            raise ValueError(
                "embedding promotion knowledge and recorded clocks must be supplied together"
            )
        knowledge_at = self.knowledge_at or self.approved_at
        recorded_at = self.recorded_at or self.approved_at
        if self.approved_at > knowledge_at or knowledge_at > recorded_at:
            raise ValueError(
                "embedding promotion clocks must satisfy approved_at <= knowledge_at <= recorded_at"
            )
        return self


@dataclass(frozen=True, slots=True)
class PersistResult:
    promotion_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class LocalVectorRuntimeConfig:
    """Explicit local roots required to activate a verified semantic runtime."""

    index_root: Path
    runtime_root: Path

    @classmethod
    def from_environment(cls) -> LocalVectorRuntimeConfig | None:
        """Return the opt-in production runtime, or ``None`` when disabled."""

        enabled = os.environ.get(_RUNTIME_ENABLED_ENV, "0")
        if enabled not in {"0", "1"}:
            raise ValueError(f"{_RUNTIME_ENABLED_ENV} must be 0 or 1")
        if enabled == "0":
            return None
        index_root = os.environ.get(_INDEX_ROOT_ENV)
        runtime_root = os.environ.get(_RUNTIME_ROOT_ENV)
        if not index_root or not runtime_root:
            raise ValueError(
                f"enabled local vector runtime requires {_INDEX_ROOT_ENV} and {_RUNTIME_ROOT_ENV}"
            )
        return cls(index_root=Path(index_root), runtime_root=Path(runtime_root))


def load_evaluation_artifact(path: Path) -> tuple[EmbeddingRecommendationArtifact, str]:
    raw = path.read_bytes()
    artifact = EmbeddingRecommendationArtifact.model_validate_json(raw)
    return artifact, hashlib.sha256(raw).hexdigest()


def promotion_from_evaluation(
    artifact: EmbeddingRecommendationArtifact,
    *,
    evaluation_artifact_sha256: str,
    revision: int,
    provider: str,
    dimensions: int,
    approved_by: str,
    approved_at: datetime,
    runtime_artifact: EmbeddingRuntimeArtifact,
    supersedes_promotion_id: str | None = None,
) -> EmbeddingPromotion:
    model = artifact.recommended_model
    if model is None:
        raise ValueError("evaluation artifact does not recommend an eligible model")
    candidate = next((item for item in artifact.results if item.model == model), None)
    if candidate is None:
        raise ValueError("recommended model has no candidate metrics")
    _require_eligible(candidate, artifact)
    if (
        runtime_artifact.provider,
        runtime_artifact.model,
        runtime_artifact.dimensions,
    ) != (provider, model, dimensions):
        raise ValueError("winning evaluation model differs from runtime artifact")
    if candidate.runtime_artifact_sha256 != runtime_artifact.sha256():
        raise ValueError("winning evaluation runtime digest differs from promotion descriptor")
    runtime_json = runtime_artifact.canonical_json()
    runtime_sha = runtime_artifact.sha256()
    metrics_json = json.dumps(
        {
            "candidate": candidate.model_dump(mode="json"),
            "thresholds": artifact.thresholds.model_dump(mode="json"),
            "reason": artifact.reason,
            "k": artifact.k,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    semantic = {
        "purpose": artifact.purpose,
        "revision": revision,
        "provider": provider,
        "model": model,
        "dimensions": dimensions,
        "golden_sha256": artifact.golden_sha256,
        "evaluation_artifact_sha256": evaluation_artifact_sha256,
        "evaluation_metrics_json": metrics_json,
        "runtime_artifact_json": runtime_json,
        "runtime_artifact_sha256": runtime_sha,
        "approved_by": approved_by,
        "approved_at": approved_at.isoformat(),
        "supersedes_promotion_id": supersedes_promotion_id,
    }
    seed = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return EmbeddingPromotion(
        promotion_id=f"embedding-promotion:{seed}",
        idempotency_key=f"embedding-promotion:{seed}",
        purpose=artifact.purpose,
        revision=revision,
        provider=provider,
        model=model,
        dimensions=dimensions,
        golden_sha256=artifact.golden_sha256,
        evaluation_artifact_sha256=evaluation_artifact_sha256,
        evaluation_metrics_json=metrics_json,
        runtime_artifact_json=runtime_json,
        runtime_artifact_sha256=runtime_sha,
        approved_by=approved_by,
        approved_at=approved_at,
        knowledge_at=approved_at,
        recorded_at=approved_at,
        supersedes_promotion_id=supersedes_promotion_id,
    )


def persist_promotion(conn: sqlite3.Connection, promotion: EmbeddingPromotion) -> PersistResult:
    if promotion.runtime_artifact_json is None or promotion.runtime_artifact_sha256 is None:
        raise ValueError("new embedding promotions require a runtime artifact")
    if promotion.revision > 1:
        parent = conn.execute(
            "SELECT purpose, revision FROM search_embedding_model_promotions "
            "WHERE promotion_id = ?",
            (promotion.supersedes_promotion_id,),
        ).fetchone()
        if parent is None or (str(parent[0]), int(parent[1])) != (
            promotion.purpose,
            promotion.revision - 1,
        ):
            raise ValueError("promotion does not supersede the prior same-purpose revision")
    columns = (
        "promotion_id",
        "idempotency_key",
        "purpose",
        "revision",
        "provider",
        "model",
        "dimensions",
        "golden_sha256",
        "evaluation_artifact_sha256",
        "evaluation_metrics_json",
        "runtime_artifact_json",
        "runtime_artifact_sha256",
        "approved_by",
        "approved_at",
        "supersedes_promotion_id",
    )
    values = (
        promotion.promotion_id,
        promotion.idempotency_key,
        promotion.purpose,
        promotion.revision,
        promotion.provider,
        promotion.model,
        promotion.dimensions,
        promotion.golden_sha256,
        promotion.evaluation_artifact_sha256,
        promotion.evaluation_metrics_json,
        promotion.runtime_artifact_json,
        promotion.runtime_artifact_sha256,
        promotion.approved_by,
        promotion.approved_at,
        promotion.supersedes_promotion_id,
    )
    if _has_bitemporal_clock_columns(conn):
        columns += ("knowledge_at", "recorded_at")
        values += (
            promotion.knowledge_at or promotion.approved_at,
            promotion.recorded_at or promotion.approved_at,
        )
    existing = conn.execute(
        f"SELECT {', '.join(columns)} FROM search_embedding_model_promotions "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE idempotency_key = ?",
        (promotion.idempotency_key,),
    ).fetchone()
    if existing is not None:
        if not _same(tuple(existing), values):
            raise ValueError("immutable embedding promotion conflicts with existing data")
        return PersistResult(promotion.promotion_id, False)
    conn.execute(
        f"INSERT INTO search_embedding_model_promotions ({', '.join(columns)}) "  # nosec B608 -- trusted internal SQL shape; values remain bound
        f"VALUES ({', '.join('?' for _ in columns)})",
        values,
    )
    return PersistResult(promotion.promotion_id, True)


def current_promotion(
    conn: sqlite3.Connection, purpose: str = PURPOSE
) -> EmbeddingPromotion | None:
    if _has_bitemporal_clock_columns(conn):
        row = conn.execute(
            "SELECT promotion_id,idempotency_key,purpose,revision,provider,model,"
            "dimensions,golden_sha256,evaluation_artifact_sha256,"
            "evaluation_metrics_json,runtime_artifact_json,"
            "runtime_artifact_sha256,approved_by,approved_at,"
            "supersedes_promotion_id,knowledge_at,recorded_at "
            "FROM v_search_embedding_model_promotion_current WHERE purpose = ?",
            (purpose,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT promotion_id,idempotency_key,purpose,revision,provider,model,"
            "dimensions,golden_sha256,evaluation_artifact_sha256,"
            "evaluation_metrics_json,runtime_artifact_json,"
            "runtime_artifact_sha256,approved_by,approved_at,"
            "supersedes_promotion_id "
            "FROM v_search_embedding_model_promotion_current WHERE purpose = ?",
            (purpose,),
        ).fetchone()
    if row is None:
        return None
    approved_at = datetime.fromisoformat(str(row[13]))
    return EmbeddingPromotion(
        promotion_id=str(row[0]),
        idempotency_key=str(row[1]),
        purpose=str(row[2]),
        revision=int(row[3]),
        provider=str(row[4]),
        model=str(row[5]),
        dimensions=int(row[6]),
        golden_sha256=str(row[7]),
        evaluation_artifact_sha256=str(row[8]),
        evaluation_metrics_json=str(row[9]),
        runtime_artifact_json=None if row[10] is None else str(row[10]),
        runtime_artifact_sha256=None if row[11] is None else str(row[11]),
        approved_by=str(row[12]),
        approved_at=approved_at,
        supersedes_promotion_id=None if row[14] is None else str(row[14]),
        knowledge_at=(
            datetime.fromisoformat(str(row[15]))
            if len(row) > 15 and row[15] is not None
            else approved_at
        ),
        recorded_at=(
            datetime.fromisoformat(str(row[16]))
            if len(row) > 16 and row[16] is not None
            else approved_at
        ),
    )


def _has_bitemporal_clock_columns(conn: sqlite3.Connection) -> bool:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(search_embedding_model_promotions)")
    }
    return {"knowledge_at", "recorded_at"} <= columns


def promoted_vector_backend(
    conn: sqlite3.Connection,
    *,
    manifest_id: str,
    index_root: Path,
    runtime_root: Path | None = None,
) -> LanceVectorBackend | None:
    """Resolve only a successful full-coverage run for the approved model."""

    promotion = current_promotion(conn)
    if (
        promotion is None
        or promotion.runtime_artifact_json is None
        or promotion.runtime_artifact_sha256 is None
        or runtime_root is None
    ):
        return None
    row = conn.execute(
        "SELECT run.index_run_id FROM v_search_index_successful AS run "
        "WHERE run.manifest_id = ? AND run.index_kind = 'vector' "
        "AND EXISTS (SELECT 1 FROM search_embedding_artifacts AS artifact "
        "WHERE artifact.index_run_id = run.index_run_id AND artifact.provider = ? "
        "AND artifact.model = ? AND artifact.dimensions = ? "
        "AND artifact.outcome = 'succeeded') "
        "AND NOT EXISTS (SELECT 1 FROM search_chunks AS chunk "
        "LEFT JOIN search_embedding_artifacts AS artifact "
        "ON artifact.index_run_id = run.index_run_id "
        "AND artifact.chunk_id = chunk.chunk_id AND artifact.provider = ? "
        "AND artifact.model = ? AND artifact.dimensions = ? "
        "AND artifact.outcome = 'succeeded' "
        "LEFT JOIN search_index_memberships AS membership "
        "ON membership.index_run_id = run.index_run_id "
        "AND membership.chunk_id = chunk.chunk_id "
        "WHERE chunk.manifest_id = run.manifest_id "
        "AND (artifact.chunk_id IS NULL OR membership.membership_status <> 'included')) "
        "ORDER BY run.completed_at DESC, run.index_run_id DESC LIMIT 1",
        (
            manifest_id,
            promotion.provider,
            promotion.model,
            promotion.dimensions,
            promotion.provider,
            promotion.model,
            promotion.dimensions,
        ),
    ).fetchone()
    if row is None:
        return None
    index_run_id = str(row[0])
    seal = load_projection_seal(conn, index_run_id=index_run_id)
    if seal is None:
        return None
    if (
        seal.index_kind != "vector"
        or seal.manifest_id != manifest_id
        or seal.provider != promotion.provider
        or seal.model != promotion.model
        or seal.dimensions != promotion.dimensions
        or seal.runtime_artifact_sha256 != promotion.runtime_artifact_sha256
    ):
        return None
    try:
        verify_ledger_projection_seal(conn, seal)
    except RuntimeError:
        return None
    spec = EmbeddingModelSpec(
        provider=promotion.provider,
        model=promotion.model,
        dimensions=promotion.dimensions,
    )
    runtime_artifact = parse_runtime_artifact(
        promotion.runtime_artifact_json, promotion.runtime_artifact_sha256
    )
    return LanceVectorBackend(
        LanceVectorIndex(index_root),
        index_run_id=index_run_id,
        manifest_id=manifest_id,
        encoder=FastEmbedEncoder.from_spec(
            spec, runtime_artifact=runtime_artifact, runtime_root=runtime_root
        ),
        dimensions=spec.dimensions,
        ledger_conn=conn,
        projection_seal=seal,
    )


def _require_eligible(
    candidate: CandidateMetrics, artifact: EmbeddingRecommendationArtifact
) -> None:
    threshold = artifact.thresholds
    if candidate.case_count < threshold.minimum_cases:
        raise ValueError("recommended model does not meet minimum case coverage")
    checks = (
        candidate.recall_at_k >= threshold.min_recall_at_k,
        candidate.mrr >= threshold.min_mrr,
        candidate.ndcg >= threshold.min_ndcg,
        candidate.mean_latency_ms <= threshold.max_mean_latency_ms,
    )
    if not all(checks):
        raise ValueError("recommended model does not meet recorded promotion thresholds")


def _same(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    def normalized(value: object) -> object:
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value

    return tuple(normalized(value) for value in left) == tuple(normalized(value) for value in right)
