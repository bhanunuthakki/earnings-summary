"""Closed evaluation and approval receipts are the only promotion input."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from search.embedding_eval import (
    CandidateEvaluationCoordinate,
    CandidateMetrics,
    EmbeddingRecommendationArtifact,
    EvalThresholds,
)
from search.embedding_promotion import (
    EmbeddingApprovalReceipt,
    current_promotion,
    persist_promotion,
    promotion_from_evaluation,
)
from search.embedding_runtime_artifact import (
    EmbeddingRuntimeArtifact,
    RuntimeArtifactFile,
    RuntimeComponentVersion,
)
from search.embedding_runtime_registration import (
    persist_runtime_registration,
    registration_from_artifact,
)

STAMP = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
SHA = "a" * 64
MODELS = ("BAAI/bge-base-en-v1.5", "BAAI/bge-small-en-v1.5")


def _runtime_artifact(model: str) -> EmbeddingRuntimeArtifact:
    dimensions = 768 if "base" in model else 384
    return EmbeddingRuntimeArtifact(
        provider="fastembed",
        model=model,
        dimensions=dimensions,
        execution_provider="CPUExecutionProvider",
        execution_settings=(),
        component_versions=(RuntimeComponentVersion(component="fastembed", version="1.2.3"),),
        files=(
            RuntimeArtifactFile(
                logical_name="model/model.onnx",
                role="model",
                size_bytes=3,
                sha256=("b" if "base" in model else "c") * 64,
            ),
        ),
    )


def _artifact(*, recommended: str | None = MODELS[1]) -> EmbeddingRecommendationArtifact:
    runtimes = {model: _runtime_artifact(model) for model in MODELS}
    return EmbeddingRecommendationArtifact(
        golden_sha256=SHA,
        k=10,
        thresholds=EvalThresholds(minimum_cases=30),
        results=tuple(
            CandidateMetrics(
                model=model,
                case_count=30,
                recall_at_k=0.9 if model == MODELS[1] else 0.8,
                mrr=0.8 if model == MODELS[1] else 0.7,
                ndcg=0.82 if model == MODELS[1] else 0.75,
                mean_latency_ms=30,
                coverage=1,
                runtime_artifact_sha256=runtimes[model].sha256(),
            )
            for model in MODELS
        ),
        candidate_coordinates=tuple(
            CandidateEvaluationCoordinate(
                model=model,
                index_run_id=f"run-{index}",
                manifest_id="manifest-1",
                projection_seal_id=f"seal-{index}",
                projection_records_sha256=f"{index + 3:x}" * 64,
                artifact_set_sha256=f"{index + 5:x}" * 64,
                config_sha256=f"{index + 7:x}" * 64,
                chunk_count=30,
                chunk_set_sha256="d" * 64,
                runtime_registration_id=registration_from_artifact(
                    runtimes[model], registered_at=STAMP
                ).runtime_registration_id,
                runtime_artifact_sha256=runtimes[model].sha256(),
                sealed_at=STAMP,
            )
            for index, model in enumerate(MODELS)
        ),
        evaluated_at=STAMP,
        recommended_model=recommended,
        reason="clear eligible winner; routing remains unchanged pending owner approval",
    )


def _approval(artifact: EmbeddingRecommendationArtifact) -> EmbeddingApprovalReceipt:
    import hashlib

    digest = hashlib.sha256(artifact.canonical_json().encode()).hexdigest()
    return EmbeddingApprovalReceipt(
        decision="approved",
        evaluation_artifact_sha256=digest,
        approved_model=MODELS[1],
        approved_by="owner",
        approved_at=STAMP,
        rationale="Closed golden evaluation is eligible and materially better.",
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE search_embedding_runtime_registrations (
          runtime_registration_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,purpose TEXT,
          provider TEXT,model TEXT,dimensions INTEGER,runtime_artifact_json TEXT,
          runtime_artifact_sha256 TEXT,registered_at DATETIME);
        CREATE TABLE search_embedding_evaluation_receipts (
          evaluation_receipt_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,purpose TEXT,
          golden_sha256 TEXT,evaluation_artifact_json TEXT,evaluation_artifact_sha256 TEXT,
          candidate_set_json TEXT,candidate_set_sha256 TEXT,evaluated_at DATETIME);
        CREATE TABLE search_embedding_model_promotions (
          promotion_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,purpose TEXT,revision INTEGER,
          provider TEXT,model TEXT,dimensions INTEGER,golden_sha256 TEXT,
          evaluation_artifact_sha256 TEXT,evaluation_metrics_json TEXT,
          evaluation_receipt_id TEXT,evaluation_artifact_json TEXT,runtime_artifact_json TEXT,
          runtime_artifact_sha256 TEXT,runtime_registration_id TEXT,
          approval_receipt_json TEXT,approval_receipt_sha256 TEXT,approved_by TEXT,
          approved_at DATETIME,supersedes_promotion_id TEXT,knowledge_at DATETIME,
          recorded_at DATETIME);
        CREATE VIEW v_search_embedding_model_promotion_current AS
          SELECT promotion.* FROM search_embedding_model_promotions promotion
          WHERE NOT EXISTS (SELECT 1 FROM search_embedding_model_promotions newer
          WHERE newer.purpose=promotion.purpose AND newer.revision>promotion.revision);
        """
    )
    return conn


def test_governed_promotion_persists_idempotently_and_activates() -> None:
    artifact = _artifact()
    registration = registration_from_artifact(_runtime_artifact(MODELS[1]), registered_at=STAMP)
    promotion = promotion_from_evaluation(
        artifact,
        revision=1,
        runtime_registration=registration,
        approval=_approval(artifact),
    )
    conn = _conn()
    try:
        assert persist_runtime_registration(conn, registration)
        assert persist_promotion(conn, promotion).created
        assert not persist_promotion(conn, promotion).created
        assert current_promotion(conn) == promotion
    finally:
        conn.close()


def test_registered_candidate_is_inert_until_approval() -> None:
    conn = _conn()
    try:
        registration = registration_from_artifact(_runtime_artifact(MODELS[1]), registered_at=STAMP)
        assert persist_runtime_registration(conn, registration)
        assert current_promotion(conn) is None
    finally:
        conn.close()


def test_no_recommendation_or_wrong_approval_cannot_be_promoted() -> None:
    artifact = _artifact(recommended=None)
    registration = registration_from_artifact(_runtime_artifact(MODELS[1]), registered_at=STAMP)
    with pytest.raises(ValueError, match="does not recommend"):
        promotion_from_evaluation(
            artifact,
            revision=1,
            runtime_registration=registration,
            approval=_approval(_artifact()),
        )

    eligible = _artifact()
    wrong = _approval(eligible).model_copy(update={"approved_model": MODELS[0]})
    with pytest.raises(ValueError, match="owner approval"):
        promotion_from_evaluation(
            eligible,
            revision=1,
            runtime_registration=registration,
            approval=wrong,
        )


def test_legacy_latest_promotion_is_preserved_but_inactive() -> None:
    conn = _conn()
    try:
        runtime = _runtime_artifact(MODELS[1])
        conn.execute(
            "INSERT INTO search_embedding_model_promotions "
            "(promotion_id,idempotency_key,purpose,revision,provider,model,dimensions,"
            "golden_sha256,evaluation_artifact_sha256,evaluation_metrics_json,"
            "runtime_artifact_json,runtime_artifact_sha256,approved_by,approved_at,"
            "knowledge_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy",
                "legacy",
                "evidence_vector_retrieval",
                1,
                "fastembed",
                MODELS[1],
                384,
                SHA,
                SHA,
                "{}",
                runtime.canonical_json(),
                runtime.sha256(),
                "owner",
                STAMP,
                STAMP,
                STAMP,
            ),
        )
        assert current_promotion(conn) is None
    finally:
        conn.close()
