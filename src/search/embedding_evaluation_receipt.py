"""Append-only receipts for exact embedding-candidate evaluations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from search.embedding_eval import PURPOSE, EmbeddingRecommendationArtifact
from search.embedding_runtime_registration import register_embedding_governance_functions


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("evaluation receipt hashes must be lowercase SHA-256")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class EmbeddingEvaluationReceipt(BaseModel):
    """Full content-addressed evaluation evidence retained in the SQL ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_receipt_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    purpose: str = Field(default=PURPOSE, min_length=1, max_length=64)
    golden_sha256: str
    evaluation_artifact_json: str = Field(min_length=2)
    evaluation_artifact_sha256: str
    candidate_set_json: str = Field(min_length=2)
    candidate_set_sha256: str
    evaluated_at: datetime

    _golden_sha = field_validator(
        "golden_sha256",
        "evaluation_artifact_sha256",
        "candidate_set_sha256",
    )(_sha256)

    @model_validator(mode="after")
    def _artifact_contract(self) -> EmbeddingEvaluationReceipt:
        artifact = EmbeddingRecommendationArtifact.model_validate_json(
            self.evaluation_artifact_json
        )
        canonical_artifact = artifact.canonical_json()
        if canonical_artifact != self.evaluation_artifact_json:
            raise ValueError("evaluation artifact JSON is not canonical")
        if hashlib.sha256(canonical_artifact.encode()).hexdigest() != (
            self.evaluation_artifact_sha256
        ):
            raise ValueError("evaluation artifact digest differs")
        if artifact.purpose != self.purpose or artifact.golden_sha256 != self.golden_sha256:
            raise ValueError("evaluation receipt coordinate differs from artifact")
        canonical_candidates = _canonical_json(
            [item.model_dump(mode="json") for item in artifact.candidate_coordinates]
        )
        if canonical_candidates != self.candidate_set_json:
            raise ValueError("evaluation candidate set JSON differs from artifact")
        if hashlib.sha256(canonical_candidates.encode()).hexdigest() != (self.candidate_set_sha256):
            raise ValueError("evaluation candidate set digest differs")
        expected = evaluation_receipt_identity(self.evaluation_artifact_sha256)
        if self.evaluation_receipt_id != expected or self.idempotency_key != expected:
            raise ValueError("evaluation receipt identity is not content-derived")
        return self


def evaluation_receipt_identity(evaluation_artifact_sha256: str) -> str:
    return f"embedding-evaluation:{_sha256(evaluation_artifact_sha256)}"


def receipt_from_evaluation(
    artifact: EmbeddingRecommendationArtifact,
    *,
    evaluated_at: datetime,
) -> EmbeddingEvaluationReceipt:
    artifact_json = artifact.canonical_json()
    artifact_sha = hashlib.sha256(artifact_json.encode()).hexdigest()
    candidates_json = _canonical_json(
        [item.model_dump(mode="json") for item in artifact.candidate_coordinates]
    )
    identity = evaluation_receipt_identity(artifact_sha)
    return EmbeddingEvaluationReceipt(
        evaluation_receipt_id=identity,
        idempotency_key=identity,
        purpose=artifact.purpose,
        golden_sha256=artifact.golden_sha256,
        evaluation_artifact_json=artifact_json,
        evaluation_artifact_sha256=artifact_sha,
        candidate_set_json=candidates_json,
        candidate_set_sha256=hashlib.sha256(candidates_json.encode()).hexdigest(),
        evaluated_at=evaluated_at,
    )


def persist_evaluation_receipt(
    conn: sqlite3.Connection,
    receipt: EmbeddingEvaluationReceipt,
) -> bool:
    register_embedding_governance_functions(conn)
    columns = tuple(EmbeddingEvaluationReceipt.model_fields)
    values = tuple(getattr(receipt, column) for column in columns)
    existing = conn.execute(
        f"SELECT {', '.join(columns)} FROM search_embedding_evaluation_receipts "  # nosec B608 -- fixed typed columns; values remain bound
        "WHERE evaluation_receipt_id=? OR idempotency_key=?",
        (receipt.evaluation_receipt_id, receipt.idempotency_key),
    ).fetchall()
    if existing:
        if len(existing) != 1 or not _same_sql(tuple(existing[0]), values):
            raise ValueError("immutable embedding evaluation receipt replay conflict")
        return False
    conn.execute(
        f"INSERT INTO search_embedding_evaluation_receipts ({', '.join(columns)}) "  # nosec B608 -- fixed typed columns; values remain bound
        f"VALUES ({', '.join('?' for _ in columns)})",
        values,
    )
    return True


def _same_sql(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    def normalized(value: object) -> object:
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value

    return tuple(normalized(value) for value in left) == tuple(normalized(value) for value in right)


__all__ = [
    "EmbeddingEvaluationReceipt",
    "evaluation_receipt_identity",
    "persist_evaluation_receipt",
    "receipt_from_evaluation",
]
