"""Append-only judgments about whether captured bytes require semantic extraction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SemanticStatus = Literal["required", "not_required", "review_required", "quarantined"]
DecisionKind = Literal["deterministic", "human", "model_assisted"]
_Mode = Literal["dry_run", "apply"]


class SemanticDisposition(BaseModel):
    """One immutable, revisioned semantic-content decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    document_version_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    semantic_status: SemanticStatus
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    reason_details: tuple[tuple[str, str], ...] = Field(min_length=1)
    decision_kind: DecisionKind
    reviewer_identity: str | None = Field(default=None, min_length=1, max_length=255)
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    policy_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_assessment_id: str | None = Field(default=None, min_length=1, max_length=128)
    material_dissent: bool = False

    @model_validator(mode="after")
    def _validate_decision(self) -> Self:
        keys = [key for key, _ in self.reason_details]
        if any(not key or not value for key, value in self.reason_details):
            raise ValueError("semantic disposition details require non-empty keys and values")
        if len(keys) != len(set(keys)):
            raise ValueError("semantic disposition detail keys must be unique")
        if self.decision_kind == "human" and self.reviewer_identity is None:
            raise ValueError("human semantic disposition requires reviewer_identity")
        if self.decision_kind != "human" and self.reviewer_identity is not None:
            raise ValueError("only human semantic disposition may name a reviewer")
        if self.semantic_status == "not_required" and self.decision_kind != "human":
            raise ValueError("not_required semantic disposition requires a human decision")
        if self.knowledge_at < self.effective_at or self.recorded_at < self.knowledge_at:
            raise ValueError("semantic disposition clocks are out of order")
        if (self.revision == 1) != (self.supersedes_assessment_id is None):
            raise ValueError("semantic disposition supersession does not match revision")
        return self


class SemanticReviewInitializationRequest(BaseModel):
    """Bounded initialization of an explicit review queue for unsupported documents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    inventory_keys: tuple[str, ...] = Field(min_length=1)
    recorded_at: datetime
    batch_size: int = Field(default=500, ge=1, le=5_000)
    apply: bool = False

    @model_validator(mode="after")
    def _validate_inventory_keys(self) -> Self:
        if any(not key.strip() for key in self.inventory_keys):
            raise ValueError("inventory keys must not be blank")
        if len(self.inventory_keys) != len(set(self.inventory_keys)):
            raise ValueError("inventory keys must be unique")
        return self


class SemanticReviewInitializationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: _Mode
    documents_considered: int = Field(ge=0)
    assessments_planned: int = Field(ge=0)
    assessments_created: int = Field(ge=0)
    assessments_replayed: int = Field(ge=0)
    media_type_counts: dict[str, int]
    has_more: bool


class SemanticDispositionStore:
    """Conflict-detecting append boundary for semantic decisions."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist(self, record: SemanticDisposition) -> bool:
        document = self._conn.execute(
            "SELECT 1 FROM evidence_document_versions WHERE document_version_id = ?",
            (record.document_version_id,),
        ).fetchone()
        if document is None:
            raise ValueError("semantic disposition requires an existing document version")
        columns = (
            "assessment_id",
            "idempotency_key",
            "document_version_id",
            "revision",
            "semantic_status",
            "reason_code",
            "reason_details_json",
            "decision_kind",
            "reviewer_identity",
            "policy_name",
            "policy_version",
            "policy_config_sha256",
            "effective_at",
            "knowledge_at",
            "recorded_at",
            "supersedes_assessment_id",
            "material_dissent",
        )
        values = (
            record.assessment_id,
            record.idempotency_key,
            record.document_version_id,
            record.revision,
            record.semantic_status,
            record.reason_code,
            _details_json(record.reason_details),
            record.decision_kind,
            record.reviewer_identity,
            record.policy_name,
            record.policy_version,
            record.policy_config_sha256,
            record.effective_at,
            record.knowledge_at,
            record.recorded_at,
            record.supersedes_assessment_id,
            record.material_dissent,
        )
        placeholders = ", ".join("?" for _ in columns)
        cursor = self._conn.execute(
            f"INSERT INTO document_semantic_disposition_revisions "
            f"({', '.join(columns)}) VALUES ({placeholders}) "
            "ON CONFLICT DO NOTHING",
            values,
        )
        if cursor.rowcount == 1:
            return True
        existing = self._conn.execute(
            f"SELECT {', '.join(columns)} "
            "FROM document_semantic_disposition_revisions "
            "WHERE assessment_id = ? OR idempotency_key = ?",
            (record.assessment_id, record.idempotency_key),
        ).fetchone()
        if existing is None or not _same(existing, values):
            raise ValueError("semantic disposition identity conflicts with existing state")
        return False


def initialize_semantic_review_queue(
    conn: sqlite3.Connection,
    request: SemanticReviewInitializationRequest,
) -> SemanticReviewInitializationResult:
    """Plan or append review-required decisions for unextracted captured bytes."""

    placeholders = ", ".join("?" for _ in request.inventory_keys)
    rows = conn.execute(
        "SELECT coverage.document_version_id, blob.media_type, "
        "expected.expected_document_key "
        "FROM v_source_coverage_current AS coverage "
        "JOIN v_expected_documents_current AS expected "
        "ON expected.expected_document_id = coverage.expected_document_id "
        "JOIN v_source_inventory_current AS inventory "
        "ON inventory.snapshot_id = expected.snapshot_id "
        "JOIN evidence_document_versions AS document "
        "ON document.document_version_id = coverage.document_version_id "
        "JOIN evidence_content_blobs AS blob ON blob.sha256 = document.blob_sha256 "
        f"WHERE inventory.inventory_key IN ({placeholders}) "
        "AND NOT EXISTS (SELECT 1 FROM v_document_semantic_dispositions_current AS current "
        "WHERE current.document_version_id = coverage.document_version_id) "
        "AND EXISTS (SELECT 1 FROM evidence_extraction_runs AS failed "
        "WHERE failed.document_version_id = coverage.document_version_id "
        "AND failed.extractor_name = 'fulltext-evidence-backfill' "
        "AND failed.outcome = 'failed') "
        "AND NOT EXISTS (SELECT 1 FROM evidence_extraction_runs AS succeeded "
        "JOIN v_evidence_current AS node "
        "ON node.extraction_run_id = succeeded.extraction_run_id "
        "WHERE succeeded.document_version_id = coverage.document_version_id "
        "AND succeeded.outcome = 'succeeded' "
        "AND node.node_kind <> 'document' AND length(trim(node.text)) > 0) "
        "ORDER BY coverage.document_version_id LIMIT ?",
        (*request.inventory_keys, request.batch_size + 1),
    ).fetchall()
    has_more = len(rows) > request.batch_size
    selected = rows[: request.batch_size]
    records = tuple(_review_required(row, request) for row in selected)
    created = 0
    replayed = 0
    if request.apply and records:
        if conn.in_transaction:
            raise RuntimeError("semantic review initialization requires an idle connection")
        conn.execute("BEGIN IMMEDIATE")
        try:
            store = SemanticDispositionStore(conn)
            for record in records:
                was_created = store.persist(record)
                created += int(was_created)
                replayed += int(not was_created)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    counts = Counter(_text(row[1], "media_type").lower() for row in selected)
    return SemanticReviewInitializationResult(
        mode="apply" if request.apply else "dry_run",
        documents_considered=len(rows),
        assessments_planned=len(records),
        assessments_created=created,
        assessments_replayed=replayed,
        media_type_counts=dict(sorted(counts.items())),
        has_more=has_more,
    )


def _review_required(
    row: sqlite3.Row | tuple[object, ...],
    request: SemanticReviewInitializationRequest,
) -> SemanticDisposition:
    document_version_id = _text(row[0], "document_version_id")
    media_type = _text(row[1], "media_type").lower()
    expected_key = _text(row[2], "expected_document_key")
    semantic = {
        "document_version_id": document_version_id,
        "semantic_status": "review_required",
        "media_type": media_type,
        "expected_document_key": expected_key,
        "recorded_at": request.recorded_at,
    }
    fingerprint = _sha_json(semantic)
    return SemanticDisposition(
        assessment_id=f"semantic-disposition:{_sha_text(fingerprint + chr(0) + '1')}",
        idempotency_key=f"semantic-disposition:{fingerprint}",
        document_version_id=document_version_id,
        revision=1,
        semantic_status="review_required",
        reason_code="unsupported_document_requires_semantic_review",
        reason_details=(
            ("expected_document_key", expected_key),
            ("media_type", media_type),
        ),
        decision_kind="deterministic",
        reviewer_identity=None,
        policy_name="captured-unsupported-semantic-review",
        policy_version="1",
        policy_config_sha256=_sha_json(
            {
                "policy": "captured-unsupported-semantic-review",
                "version": "1",
                "failed_extractor": "fulltext-evidence-backfill",
            }
        ),
        effective_at=request.recorded_at,
        knowledge_at=request.recorded_at,
        recorded_at=request.recorded_at,
        supersedes_assessment_id=None,
        material_dissent=False,
    )


def _details_json(value: tuple[tuple[str, str], ...]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _same(existing: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    for stored, supplied in zip(existing, expected, strict=True):
        if isinstance(supplied, datetime):
            if datetime.fromisoformat(str(stored)).replace(tzinfo=None) != supplied.replace(
                tzinfo=None
            ):
                return False
        elif isinstance(supplied, bool):
            if bool(stored) != supplied:
                return False
        elif stored != supplied:
            return False
    return True


def _sha_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} must be non-empty text")
    return value
