"""Receipt and data models for test database-builder audit."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Taxonomy = Literal[
    "direct-downgrade",
    "archived-graph",
    "seeded-upgrade",
    "direct-historical",
    "custom-bootstrap",
    "performance-volume",
    "hand-DDL-unit-schema",
    "cached-current-head",
    "unclassified",
]
Evidence = Literal[
    "call:downgrade",
    "call:upgrade",
    "call:stamp",
    "call:create_all",
    "call:executescript",
    "call:migrated_db",
    "sql:create table",
    "sql:alter table",
    "sql:create index",
    "sql:create trigger",
    "text:archived",
    "text:seed",
    "text:historical",
    "text:bootstrap",
    "text:volume",
    "text:cached-head",
]
FindingEvidence = Literal[
    "temporary-fixture", "checkout-default", "read-error", "invalid-utf8", "syntax-error"
]
CollectionNote = Literal[
    "",
    "git-unavailable",
    "git-nonzero",
    "invalid-head",
    "invalid-git-utf8",
    "invalid-git-framing",
    "invalid-path",
    "duplicate-path",
    "empty-scope",
    "missing-path",
    "closure-untracked",
    "closure-unreadable",
    "dirty-tree",
    "invalid-porcelain",
    "scanner-closure-mismatch",
]
_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)

Disposition = Literal["RETAIN", "CONVERT", "HOLD"]
BuilderIdentity = Literal[
    "alembic.command.upgrade",
    "alembic.command.stamp",
    "alembic.command.downgrade",
    "migrated_db",
    "connection.executescript",
    "metadata.create_all",
]


class SourceLocator(BaseModel):
    model_config = _STRICT
    start_line: int = Field(ge=1)
    start_col: int = Field(ge=0)
    end_line: int = Field(ge=1)
    end_col: int = Field(ge=0)


class BuilderInvocation(BaseModel):
    model_config = _STRICT
    path: str
    locator: SourceLocator
    source_sha256: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    invocation_id: str = Field(min_length=16, pattern="^[0-9a-f]+$")
    observed_call: str = Field(min_length=1)
    canonical_identity: BuilderIdentity | None
    factory: str | None
    evidence: Evidence
    taxonomy: Taxonomy
    disposition: Disposition


class InvocationConversion(BaseModel):
    model_config = _STRICT
    invocation_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    locator: SourceLocator
    source_sha256: str = Field(min_length=1)
    parity_receipt: str = Field(default="")
    owner_issue: str = Field(default="")
    reason: str = Field(default="")
    expires_at: datetime


class ParityReceipt(BaseModel):
    model_config = _STRICT
    schema_version: Literal["test-db-parity/v1"]
    status: Literal["PASS"]
    invocation_id: str = Field(min_length=16, pattern="^[0-9a-f]+$")
    path: str = Field(min_length=1)
    locator: SourceLocator
    source_sha256: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")


class PatternFinding(BaseModel):
    model_config = _STRICT
    path: str
    line: int
    kind: Literal["forbidden_checkout_default", "explicit_fixture", "parse_error"]
    evidence: FindingEvidence


class BuilderClassification(BaseModel):
    model_config = _STRICT
    path: str
    taxonomy: Taxonomy
    evidence: tuple[Evidence, ...]


class TestDbAudit(BaseModel):
    model_config = _STRICT
    schema_version: Literal["test-db-patterns/v1"] = "test-db-patterns/v1"
    scoped_commit: str
    scanner_sha256: str
    source_sha256: str
    collection_status: Literal["COMPLETE", "HOLD"]
    collection_note: CollectionNote = ""
    raw_audit_status: Literal["PASS", "HOLD"]
    admission_status: Literal["HOLD"] = "HOLD"
    admission_reason: Literal["disposition_and_ratchet_deferred"] = (
        "disposition_and_ratchet_deferred"
    )
    tracked_test_files: tuple[str, ...] = Field(default_factory=tuple)
    database_builders: tuple[BuilderClassification, ...] = Field(default_factory=tuple)
    counts_by_taxonomy: dict[str, int] = Field(default_factory=dict)
    findings: tuple[PatternFinding, ...] = Field(default_factory=tuple)
    violations: tuple[str, ...] = Field(default_factory=tuple)
    builder_invocations: tuple[BuilderInvocation, ...] = Field(default_factory=tuple)
