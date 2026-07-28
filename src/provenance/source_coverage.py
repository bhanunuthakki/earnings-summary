"""Immutable source inventories and expected-document coverage assessments.

Captured bytes cannot establish completeness.  This ledger keeps the upstream
inventory that says what should exist separate from evidence capture,
extraction, and search indexing, so every completeness claim is time-travelable
and can retain missing, failed, quarantined, or unknown states.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.issuer_registry import evidence_document_relation

InventorySourceKind: TypeAlias = Literal["sec_submissions", "ir_crawl", "earnings_events"]
InventoryOutcome: TypeAlias = Literal["succeeded", "partial", "failed"]
ExpectedSourceKind: TypeAlias = Literal["sec_filing", "ir_document", "earnings_call"]
ExpectationBasis: TypeAlias = Literal["authoritative", "publisher_candidate", "policy_inferred"]
CoverageStatus: TypeAlias = Literal[
    "available",
    "not_published",
    "not_discovered",
    "fetch_failed",
    "quarantined",
    "captured",
    "extracted",
    "indexed",
    "unsupported",
    "authority_unavailable",
]
DecisionKind: TypeAlias = Literal["deterministic", "manual", "imported"]
_SHA_LENGTH = 64


def _sha256(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != _SHA_LENGTH or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("must be a lowercase SHA-256 digest")
    return normalized


def _timeline(value: datetime) -> datetime:
    """Compare legacy naive SQLite clocks and offset-aware clocks on one UTC timeline."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _CoverageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceInventorySnapshot(_CoverageRecord):
    snapshot_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    inventory_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    issuer_id: str = Field(min_length=1, max_length=128)
    ticker: str | None = Field(default=None, max_length=32)
    source_kind: InventorySourceKind
    source_url: str = Field(min_length=1)
    source_observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    outcome: InventoryOutcome
    authoritative: bool
    retrieval_config_sha256: str
    collector_code_version: str = Field(min_length=1, max_length=255)
    started_at: datetime
    completed_at: datetime
    recorded_at: datetime
    supersedes_snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)

    _config_hash = field_validator("retrieval_config_sha256")(_sha256)

    @model_validator(mode="after")
    def _validate_inventory(self) -> Self:
        if self.revision == 1 and self.supersedes_snapshot_id is not None:
            raise ValueError("first source inventory cannot supersede")
        if self.outcome in {"succeeded", "partial"} and self.source_observation_id is None:
            raise ValueError("successful or partial inventory requires source observation")
        if self.outcome == "failed" and self.source_observation_id is not None:
            raise ValueError("failed inventory cannot claim source observation bytes")
        if _timeline(self.completed_at) < _timeline(self.started_at) or _timeline(
            self.recorded_at
        ) < _timeline(self.completed_at):
            raise ValueError("source inventory clocks are out of order")
        return self


class ExpectedDocument(_CoverageRecord):
    expected_document_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=128)
    expected_document_key: str = Field(min_length=1, max_length=256)
    issuer_id: str = Field(min_length=1, max_length=128)
    ticker: str | None = Field(default=None, max_length=32)
    source_kind: ExpectedSourceKind
    document_type: str = Field(min_length=1, max_length=64)
    form_type: str | None = Field(default=None, max_length=64)
    accession_number: str | None = Field(default=None, max_length=64)
    source_url: str | None = None
    primary_document: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    filing_at: datetime | None = None
    expected_at: datetime | None = None
    expectation_basis: ExpectationBasis
    recorded_at: datetime

    @model_validator(mode="after")
    def _validate_period(self) -> Self:
        if (
            self.period_start is not None
            and self.period_end is not None
            and _timeline(self.period_end) < _timeline(self.period_start)
        ):
            raise ValueError("period_end must not precede period_start")
        return self


class CoverageAssessment(_CoverageRecord):
    assessment_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    expected_document_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    coverage_status: CoverageStatus
    document_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    extraction_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    manifest_id: str | None = Field(default=None, min_length=1, max_length=128)
    index_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    reason_details: tuple[tuple[str, str], ...] = Field(min_length=1)
    decision_kind: DecisionKind
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_config_sha256: str
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_assessment_id: str | None = Field(default=None, min_length=1, max_length=128)
    material_dissent: bool

    _policy_hash = field_validator("policy_config_sha256")(_sha256)

    @field_validator("reason_details")
    @classmethod
    def _canonical_details(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        keys = [key for key, _ in value]
        if any(not key or not detail for key, detail in value):
            raise ValueError("reason details require non-empty keys and values")
        if len(keys) != len(set(keys)):
            raise ValueError("reason detail keys must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _validate_lineage(self) -> Self:
        if self.revision == 1 and self.supersedes_assessment_id is not None:
            raise ValueError("first coverage assessment cannot supersede")
        if (
            self.coverage_status in {"captured", "extracted", "indexed"}
            and self.document_version_id is None
        ):
            raise ValueError("captured coverage requires document_version_id")
        if self.coverage_status in {"extracted", "indexed"} and self.extraction_run_id is None:
            raise ValueError("extracted coverage requires extraction_run_id")
        if self.coverage_status == "indexed" and (
            self.manifest_id is None or self.index_run_id is None
        ):
            raise ValueError("indexed coverage requires manifest_id and index_run_id")
        if _timeline(self.knowledge_at) < _timeline(self.effective_at) or _timeline(
            self.recorded_at
        ) < _timeline(self.knowledge_at):
            raise ValueError("coverage assessment clocks are out of order")
        return self

    @property
    def reason_details_json(self) -> str:
        return json.dumps(dict(self.reason_details), sort_keys=True, separators=(",", ":"))


CoverageRecord: TypeAlias = SourceInventorySnapshot | ExpectedDocument | CoverageAssessment


@dataclass(frozen=True, slots=True)
class PersistResult:
    record_id: str
    created: bool


class SourceCoverageLedger:
    """Sole typed append boundary for expected-source coverage records."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist(self, record: CoverageRecord) -> PersistResult:
        self._validate_references(record)
        table, columns, values, identity_column, identity_value, record_id = self._statement(record)
        cursor = self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) ON CONFLICT DO NOTHING",
            values,
        )
        if cursor.rowcount == 1:
            return PersistResult(record_id, True)
        existing = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {identity_column} = ?",
            (identity_value,),
        ).fetchone()
        if existing is None or not _same(tuple(existing), values):
            raise ValueError(
                f"immutable {table} identity {identity_value!r} conflicts with existing data"
            )
        return PersistResult(record_id, False)

    def _validate_references(self, record: CoverageRecord) -> None:
        if isinstance(record, SourceInventorySnapshot):
            if record.revision > 1:
                parent = self._conn.execute(
                    "SELECT inventory_key, revision FROM source_inventory_snapshots "
                    "WHERE snapshot_id = ?",
                    (record.supersedes_snapshot_id,),
                ).fetchone()
                if parent is None or (str(parent[0]), int(parent[1])) != (
                    record.inventory_key,
                    record.revision - 1,
                ):
                    raise ValueError("source inventory must supersede the prior same-key revision")
            return
        if isinstance(record, ExpectedDocument):
            snapshot = self._conn.execute(
                "SELECT issuer_id, ticker, outcome, authoritative "
                "FROM source_inventory_snapshots WHERE snapshot_id = ?",
                (record.snapshot_id,),
            ).fetchone()
            if snapshot is None or str(snapshot[2]) not in {"succeeded", "partial"}:
                raise ValueError("expected document requires successful or partial inventory")
            if str(snapshot[0]) != record.issuer_id:
                raise ValueError("expected document issuer must match inventory issuer")
            if (
                record.ticker is not None
                and snapshot[1] is not None
                and str(snapshot[1]) != record.ticker
            ):
                raise ValueError("expected document ticker must match inventory ticker")
            if record.expectation_basis == "authoritative" and not bool(snapshot[3]):
                raise ValueError("authoritative expectation requires authoritative inventory")
            return
        expected = self._conn.execute(
            "SELECT issuer_id FROM expected_documents WHERE expected_document_id = ?",
            (record.expected_document_id,),
        ).fetchone()
        if expected is None:
            raise ValueError("expected document does not exist")
        if record.revision > 1:
            parent = self._conn.execute(
                "SELECT expected_document_id, revision FROM source_coverage_assessments "
                "WHERE assessment_id = ?",
                (record.supersedes_assessment_id,),
            ).fetchone()
            if parent is None or (str(parent[0]), int(parent[1])) != (
                record.expected_document_id,
                record.revision - 1,
            ):
                raise ValueError("coverage assessment must supersede prior same-document revision")
        if record.document_version_id is not None:
            relation = evidence_document_relation(self._conn)
            document = self._conn.execute(
                f"SELECT issuer_id FROM {relation} WHERE document_version_id = ?",
                (record.document_version_id,),
            ).fetchone()
            if document is None:
                raise ValueError("document version does not exist")
            if str(document[0]) != str(expected[0]):
                raise ValueError("document version issuer does not match expected document")
        if record.extraction_run_id is not None:
            extraction = self._conn.execute(
                "SELECT document_version_id FROM evidence_extraction_runs "
                "WHERE extraction_run_id = ?",
                (record.extraction_run_id,),
            ).fetchone()
            if extraction is None or str(extraction[0]) != record.document_version_id:
                raise ValueError("extraction run must belong to the assessed document")
        if record.index_run_id is not None:
            indexed = self._conn.execute(
                "SELECT manifest_id, outcome FROM search_index_runs WHERE index_run_id = ?",
                (record.index_run_id,),
            ).fetchone()
            if (
                indexed is None
                or str(indexed[0]) != record.manifest_id
                or str(indexed[1]) != "succeeded"
            ):
                raise ValueError(
                    "indexed coverage requires a successful run for the assessed manifest"
                )

    @staticmethod
    def _statement(
        record: CoverageRecord,
    ) -> tuple[str, tuple[str, ...], tuple[object, ...], str, str, str]:
        if isinstance(record, SourceInventorySnapshot):
            columns = (
                "snapshot_id",
                "idempotency_key",
                "inventory_key",
                "revision",
                "issuer_id",
                "ticker",
                "source_kind",
                "source_url",
                "source_observation_id",
                "outcome",
                "authoritative",
                "retrieval_config_sha256",
                "collector_code_version",
                "started_at",
                "completed_at",
                "recorded_at",
                "supersedes_snapshot_id",
            )
            values: tuple[object, ...] = (
                record.snapshot_id,
                record.idempotency_key,
                record.inventory_key,
                record.revision,
                record.issuer_id,
                record.ticker,
                record.source_kind,
                record.source_url,
                record.source_observation_id,
                record.outcome,
                record.authoritative,
                record.retrieval_config_sha256,
                record.collector_code_version,
                record.started_at,
                record.completed_at,
                record.recorded_at,
                record.supersedes_snapshot_id,
            )
            return (
                "source_inventory_snapshots",
                columns,
                values,
                "idempotency_key",
                record.idempotency_key,
                record.snapshot_id,
            )
        if isinstance(record, ExpectedDocument):
            columns = (
                "expected_document_id",
                "idempotency_key",
                "snapshot_id",
                "expected_document_key",
                "issuer_id",
                "ticker",
                "source_kind",
                "document_type",
                "form_type",
                "accession_number",
                "source_url",
                "primary_document",
                "period_start",
                "period_end",
                "filing_at",
                "expected_at",
                "expectation_basis",
                "recorded_at",
            )
            values = (
                record.expected_document_id,
                record.idempotency_key,
                record.snapshot_id,
                record.expected_document_key,
                record.issuer_id,
                record.ticker,
                record.source_kind,
                record.document_type,
                record.form_type,
                record.accession_number,
                record.source_url,
                record.primary_document,
                record.period_start,
                record.period_end,
                record.filing_at,
                record.expected_at,
                record.expectation_basis,
                record.recorded_at,
            )
            return (
                "expected_documents",
                columns,
                values,
                "idempotency_key",
                record.idempotency_key,
                record.expected_document_id,
            )
        columns = (
            "assessment_id",
            "idempotency_key",
            "expected_document_id",
            "revision",
            "coverage_status",
            "document_version_id",
            "extraction_run_id",
            "manifest_id",
            "index_run_id",
            "reason_code",
            "reason_details_json",
            "decision_kind",
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
            record.expected_document_id,
            record.revision,
            record.coverage_status,
            record.document_version_id,
            record.extraction_run_id,
            record.manifest_id,
            record.index_run_id,
            record.reason_code,
            record.reason_details_json,
            record.decision_kind,
            record.policy_name,
            record.policy_version,
            record.policy_config_sha256,
            record.effective_at,
            record.knowledge_at,
            record.recorded_at,
            record.supersedes_assessment_id,
            record.material_dissent,
        )
        return (
            "source_coverage_assessments",
            columns,
            values,
            "idempotency_key",
            record.idempotency_key,
            record.assessment_id,
        )


def _same(existing: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    if len(existing) != len(expected):
        return False
    for stored, supplied in zip(existing, expected, strict=True):
        if isinstance(supplied, datetime):
            try:
                parsed = datetime.fromisoformat(str(stored)).replace(tzinfo=None)
            except ValueError:
                return False
            if parsed != supplied.replace(tzinfo=None):
                return False
        elif isinstance(supplied, bool):
            if bool(stored) is not supplied:
                return False
        elif stored != supplied:
            return False
    return True
