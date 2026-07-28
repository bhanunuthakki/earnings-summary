"""Typed append-only bridge from legacy source rows to canonical evidence.

The legacy ``documents`` table gives many writers a stable foreign-key target.
Canonical evidence, however, versions immutable retrieved bytes.  This module
lets a legacy source row advance to newly observed evidence without mutating
either its prior binding or the fact observations already captured from it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_LENGTH = 64


def _sha256(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


def _optional_sha256(value: str | None) -> str | None:
    return None if value is None else _sha256(value)


def _timeline(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _ClosedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LegacyDocumentScopeLocator(_ClosedRecord):
    """Closed identity of the subset represented by one legacy source row."""

    source_ref: str = Field(min_length=1, max_length=2048)
    accession_number: str | None = Field(default=None, min_length=1, max_length=64)

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode()).hexdigest()


class LegacyDocumentEvidenceBindingRevision(_ClosedRecord):
    """One immutable selection of canonical evidence for a legacy document."""

    binding_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    legacy_document_id: int = Field(gt=0)
    revision: int = Field(gt=0)
    document_version_id: str = Field(min_length=1, max_length=128)
    evidence_node_id: str = Field(min_length=1, max_length=128)
    scope_locator: LegacyDocumentScopeLocator
    scope_locator_sha256: str | None = None
    scope_content_sha256: str
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_binding_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )

    _scope_locator_sha = field_validator("scope_locator_sha256")(_optional_sha256)
    _scope_content_sha = field_validator("scope_content_sha256")(_sha256)

    @model_validator(mode="after")
    def _validate_revision(self) -> Self:
        expected_locator_sha = self.scope_locator.canonical_sha256
        if self.scope_locator_sha256 is None:
            object.__setattr__(self, "scope_locator_sha256", expected_locator_sha)
        elif self.scope_locator_sha256 != expected_locator_sha:
            raise ValueError("scope_locator_sha256 must match canonical scope locator JSON")
        if (self.revision == 1) != (self.supersedes_binding_revision_id is None):
            raise ValueError("legacy evidence binding revision chain is incomplete")
        effective = _timeline(self.effective_at)
        knowledge = _timeline(self.knowledge_at)
        recorded = _timeline(self.recorded_at)
        if knowledge < effective or recorded < knowledge:
            raise ValueError("legacy evidence binding clocks are inconsistent")
        return self


@dataclass(frozen=True, slots=True)
class PersistResult:
    record_id: str
    created: bool


def _matches(existing: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    if len(existing) != len(expected):
        return False
    for stored, supplied in zip(existing, expected, strict=True):
        if isinstance(supplied, datetime):
            try:
                stored_time = datetime.fromisoformat(str(stored))
            except ValueError:
                return False
            if _timeline(stored_time) != _timeline(supplied):
                return False
        elif stored != supplied:
            return False
    return True


class LegacyDocumentEvidenceBindingLedger:
    """Exact-replay-only persistence boundary for legacy evidence bindings."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist(self, record: LegacyDocumentEvidenceBindingRevision) -> PersistResult:
        validated = LegacyDocumentEvidenceBindingRevision.model_validate(record.model_dump())
        self._validate_legacy_document(validated.legacy_document_id)
        columns = (
            "binding_revision_id",
            "idempotency_key",
            "legacy_document_id",
            "revision",
            "document_version_id",
            "evidence_node_id",
            "scope_locator_json",
            "scope_locator_sha256",
            "scope_content_sha256",
            "effective_at",
            "knowledge_at",
            "recorded_at",
            "supersedes_binding_revision_id",
        )
        values: tuple[object, ...] = (
            validated.binding_revision_id,
            validated.idempotency_key,
            validated.legacy_document_id,
            validated.revision,
            validated.document_version_id,
            validated.evidence_node_id,
            validated.scope_locator.canonical_json,
            validated.scope_locator_sha256,
            validated.scope_content_sha256,
            validated.effective_at,
            validated.knowledge_at,
            validated.recorded_at,
            validated.supersedes_binding_revision_id,
        )
        placeholders = ",".join("?" for _ in columns)
        cursor = self._conn.execute(
            "INSERT INTO legacy_document_evidence_binding_revisions "
            f"({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            values,
        )
        if cursor.rowcount == 1:
            return PersistResult(validated.binding_revision_id, True)
        existing = self._conn.execute(
            "SELECT " + ",".join(columns) + " "
            "FROM legacy_document_evidence_binding_revisions "
            "WHERE idempotency_key = ?",
            (validated.idempotency_key,),
        ).fetchone()
        if existing is None or not _matches(tuple(existing), values):
            raise ValueError(
                "legacy evidence binding idempotency key conflicts with immutable data"
            )
        return PersistResult(validated.binding_revision_id, False)

    def _validate_legacy_document(self, document_id: int) -> None:
        has_documents = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
        ).fetchone()
        if has_documents is None:
            return
        exists = self._conn.execute(
            "SELECT 1 FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if exists is None:
            raise ValueError(f"legacy documents.id {document_id} does not exist")
