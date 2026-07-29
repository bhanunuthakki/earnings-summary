"""Closed evaluation artifacts are the only evidence-vector promotion input."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from search.embedding_eval import (
    CandidateMetrics,
    EmbeddingRecommendationArtifact,
    EvalThresholds,
)
from search.embedding_promotion import (
    current_promotion,
    persist_promotion,
    promoted_vector_backend,
    promotion_from_evaluation,
)
from search.embedding_runtime_artifact import (
    EmbeddingRuntimeArtifact,
    RuntimeArtifactFile,
    RuntimeComponentVersion,
)

STAMP = datetime(2026, 7, 27, 8, 0, 0)
SHA = "a" * 64


def _runtime_artifact(
    *, model: str = "BAAI/bge-small-en-v1.5", file_sha256: str = "b" * 64
) -> EmbeddingRuntimeArtifact:
    return EmbeddingRuntimeArtifact(
        provider="fastembed",
        model=model,
        dimensions=384,
        execution_provider="CPUExecutionProvider",
        execution_settings=(),
        component_versions=(RuntimeComponentVersion(component="fastembed", version="1.2.3"),),
        files=(
            RuntimeArtifactFile(
                logical_name="model/model.onnx",
                role="model",
                size_bytes=3,
                sha256=file_sha256,
            ),
        ),
    )


def _artifact(*, recommended: str | None = "BAAI/bge-small-en-v1.5"):
    return EmbeddingRecommendationArtifact(
        golden_sha256=SHA,
        k=10,
        thresholds=EvalThresholds(minimum_cases=30),
        results=(
            CandidateMetrics(
                model="BAAI/bge-small-en-v1.5",
                case_count=30,
                recall_at_k=0.9,
                mrr=0.8,
                ndcg=0.82,
                mean_latency_ms=30,
                coverage=1,
                runtime_artifact_sha256=_runtime_artifact().sha256(),
            ),
            CandidateMetrics(
                model="BAAI/bge-base-en-v1.5",
                case_count=30,
                recall_at_k=0.8,
                mrr=0.7,
                ndcg=0.75,
                mean_latency_ms=60,
                coverage=1,
                runtime_artifact_sha256=_runtime_artifact(model="BAAI/bge-base-en-v1.5").sha256(),
            ),
        ),
        recommended_model=recommended,
        reason="clear eligible winner; routing remains unchanged pending owner approval",
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE search_embedding_model_promotions (
            promotion_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE NOT NULL,
            purpose TEXT NOT NULL,
            revision INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            golden_sha256 TEXT NOT NULL,
            evaluation_artifact_sha256 TEXT NOT NULL,
            evaluation_metrics_json TEXT NOT NULL,
            runtime_artifact_json TEXT,
            runtime_artifact_sha256 TEXT,
            approved_by TEXT NOT NULL,
            approved_at DATETIME NOT NULL,
            supersedes_promotion_id TEXT
        );
        CREATE VIEW v_search_embedding_model_promotion_current AS
        SELECT promotion.* FROM search_embedding_model_promotions AS promotion
        WHERE NOT EXISTS (
            SELECT 1 FROM search_embedding_model_promotions AS newer
            WHERE newer.purpose = promotion.purpose
            AND newer.revision > promotion.revision
        );
        CREATE TABLE search_index_runs (
            index_run_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL,
            index_kind TEXT NOT NULL, config_sha256 TEXT NOT NULL,
            outcome TEXT NOT NULL, completed_at TEXT NOT NULL,
            index_key TEXT NOT NULL, revision INTEGER NOT NULL
        );
        CREATE TABLE search_chunks (
            chunk_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL
        );
        CREATE TABLE search_embedding_artifacts (
            index_run_id TEXT NOT NULL, chunk_id TEXT NOT NULL, purpose TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, dimensions INTEGER NOT NULL,
            vector_sha256 TEXT, storage_uri TEXT, input_sha256 TEXT NOT NULL,
            request_config_sha256 TEXT NOT NULL, outcome TEXT NOT NULL
            , runtime_artifact_sha256 TEXT
        );
        CREATE TABLE search_index_memberships (
            index_run_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
            membership_status TEXT NOT NULL
        );
        CREATE TABLE search_projection_seals (
            projection_seal_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
            index_run_id TEXT UNIQUE NOT NULL, manifest_id TEXT NOT NULL,
            index_kind TEXT NOT NULL, chunk_count INTEGER NOT NULL,
            chunk_set_sha256 TEXT NOT NULL, projection_records_sha256 TEXT NOT NULL,
            artifact_set_sha256 TEXT, provider TEXT, model TEXT, dimensions INTEGER,
            runtime_artifact_sha256 TEXT,
            config_sha256 TEXT NOT NULL, storage_uri TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE VIEW v_search_index_successful AS
        SELECT run.* FROM search_index_runs AS run
        JOIN search_projection_seals AS seal ON seal.index_run_id = run.index_run_id
        WHERE run.outcome = 'succeeded'
        AND NOT EXISTS (
            SELECT 1 FROM search_index_runs AS newer
            WHERE newer.index_key = run.index_key AND newer.revision > run.revision
        );
        """
    )
    return conn


def test_owner_approved_eligible_recommendation_persists_and_replays() -> None:
    promotion = promotion_from_evaluation(
        _artifact(),
        evaluation_artifact_sha256=SHA,
        revision=1,
        provider="fastembed",
        dimensions=384,
        approved_by="owner",
        approved_at=STAMP,
        runtime_artifact=_runtime_artifact(),
    )
    conn = _conn()
    try:
        assert persist_promotion(conn, promotion).created
        assert not persist_promotion(conn, promotion).created
        assert current_promotion(conn) == promotion
    finally:
        conn.close()


def test_no_recommendation_or_under_threshold_candidate_cannot_be_promoted() -> None:
    with pytest.raises(ValueError, match="does not recommend"):
        promotion_from_evaluation(
            _artifact(recommended=None),
            evaluation_artifact_sha256=SHA,
            revision=1,
            provider="fastembed",
            dimensions=384,
            approved_by="owner",
            approved_at=STAMP,
            runtime_artifact=_runtime_artifact(),
        )


def test_promotion_rejects_runtime_bytes_not_used_by_winning_evaluation() -> None:
    with pytest.raises(ValueError, match="runtime digest differs"):
        promotion_from_evaluation(
            _artifact(),
            evaluation_artifact_sha256=SHA,
            revision=1,
            provider="fastembed",
            dimensions=384,
            approved_by="owner",
            approved_at=STAMP,
            runtime_artifact=_runtime_artifact(file_sha256="c" * 64),
        )


def test_successful_run_without_final_projection_seal_is_not_promotable(
    tmp_path: Path,
) -> None:
    conn = _conn()
    try:
        promotion = promotion_from_evaluation(
            _artifact(),
            evaluation_artifact_sha256=SHA,
            revision=1,
            provider="fastembed",
            dimensions=384,
            approved_by="owner",
            approved_at=STAMP,
            runtime_artifact=_runtime_artifact(),
        )
        persist_promotion(conn, promotion)
        conn.execute(
            "INSERT INTO search_index_runs VALUES "
            "('run-1','manifest-1','vector',?,'succeeded',?,'vector-key',1)",
            (SHA, STAMP),
        )
        conn.execute(
            "INSERT INTO search_chunks VALUES ('chunk-1','manifest-1',?)",
            (SHA,),
        )
        conn.execute(
            "INSERT INTO search_embedding_artifacts VALUES "
            "('run-1','chunk-1','passage','fastembed',?,384,?,'uri',?,?,"
            "'succeeded',?)",
            (promotion.model, SHA, SHA, SHA, promotion.runtime_artifact_sha256),
        )
        conn.execute("INSERT INTO search_index_memberships VALUES ('run-1','chunk-1','included')")
        conn.commit()
        assert promoted_vector_backend(conn, manifest_id="manifest-1", index_root=tmp_path) is None
    finally:
        conn.close()
    artifact = _artifact().model_copy(
        update={
            "results": (
                _artifact().results[0].model_copy(update={"case_count": 2}),
                _artifact().results[1],
            )
        }
    )
    with pytest.raises(ValueError, match="minimum case"):
        promotion_from_evaluation(
            artifact,
            evaluation_artifact_sha256=SHA,
            revision=1,
            provider="fastembed",
            dimensions=384,
            approved_by="owner",
            approved_at=STAMP,
            runtime_artifact=_runtime_artifact(),
        )
