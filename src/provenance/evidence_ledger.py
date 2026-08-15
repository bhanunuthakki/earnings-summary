"""Typed, append-only persistence boundary for canonical evidence.

Vocabulary proposed here for the next provenance increment (the authoritative
``DEFINITIONS.md`` is deliberately unchanged): a *content blob* identifies
immutable bytes by hash; a *source observation* records one retrieval of those
bytes and its source clocks; a *document version* gives those bytes a stable
logical-document identity; an *extraction run* records the deterministic
software/configuration that read that version; and an *evidence node* is one
hierarchical, revisioned assertion emitted by that run.  Nodes are never
updated: ``v_evidence_current`` projects each node's highest revision.

This module is intentionally not wired into legacy document, transcript,
filing, or IR writers yet.  ``EvidenceLedger.persist`` is the sole write
boundary for a future additive dual-write rollout.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256_LENGTH = 64
EvidenceNodeKind: TypeAlias = Literal[
    "document",
    "section",
    "passage",
    "table",
    "table_row",
    "table_cell",
    "pdf_page",
    "transcript_turn",
    "claim",
]
OfficeObjectKind: TypeAlias = Literal[
    "pptx_chart_inventory",
    "pptx_chart",
    "pptx_chart_series",
    "pptx_table_inventory",
    "pptx_table",
    "pptx_table_row",
    "pptx_table_cell",
    "xlsx_named_table_inventory",
    "xlsx_named_table",
]
_RunOutcome: TypeAlias = Literal["succeeded", "failed"]


class _LedgerRecord(BaseModel):
    """Base contract: typed, closed records cross the persistence boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_sha256(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != _SHA256_LENGTH or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


def _validate_optional_sha256(value: str | None) -> str | None:
    return None if value is None else _validate_sha256(value)


class ContentBlob(_LedgerRecord):
    sha256: str
    byte_size: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)
    storage_uri: str = Field(min_length=1)
    recorded_at: datetime

    _sha256 = field_validator("sha256")(_validate_sha256)


class EvidenceLocator(_LedgerRecord):
    """Closed locator grammar for text, tables, Office files, PDFs, and transcripts."""

    source_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    filing_section_key_raw: str | None = Field(default=None, min_length=1, max_length=512)
    filing_ordinal: int | None = Field(default=None, ge=0)
    page_number: int | None = Field(default=None, gt=0)
    bbox: tuple[float, float, float, float] | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    table_name: str | None = Field(default=None, min_length=1, max_length=255)
    table_row_index: int | None = Field(default=None, ge=0)
    table_column_index: int | None = Field(default=None, ge=0)
    slide_number: int | None = Field(default=None, gt=0)
    shape_index: int | None = Field(default=None, ge=0)
    sheet_name: str | None = Field(default=None, min_length=1, max_length=255)
    cell_address: str | None = Field(default=None, pattern=r"^\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}$")
    cell_range: str | None = Field(
        default=None,
        pattern=(
            r"^\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}:"
            r"\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}$"
        ),
    )
    office_object_kind: OfficeObjectKind | None = None
    office_package_part: str | None = Field(default=None, min_length=1, max_length=1024)
    office_relationship_id: str | None = Field(default=None, min_length=1, max_length=255)
    office_object_ordinal: int | None = Field(default=None, gt=0)
    office_series_ordinal: int | None = Field(default=None, gt=0)
    office_part_sha256: str | None = None
    transcript_turn_sequence: int | None = Field(default=None, ge=0)
    transcript_speaker: str | None = Field(default=None, min_length=1, max_length=512)
    transcript_time_code_start: str | None = Field(default=None, min_length=1, max_length=64)
    transcript_time_code_end: str | None = Field(default=None, min_length=1, max_length=64)
    transcript_start_seconds: float | None = Field(default=None, ge=0)
    transcript_end_seconds: float | None = Field(default=None, ge=0)
    xbrl_package_member: str | None = Field(default=None, min_length=1, max_length=1024)
    xbrl_fact_id: str | None = Field(default=None, min_length=1, max_length=512)
    xbrl_element_path: str | None = Field(default=None, min_length=1, max_length=4096)
    xbrl_concept_namespace: str | None = Field(default=None, min_length=1, max_length=2048)
    xbrl_concept_name: str | None = Field(default=None, min_length=1, max_length=512)
    xbrl_context_id: str | None = Field(default=None, min_length=1, max_length=512)
    xbrl_unit_id: str | None = Field(default=None, min_length=1, max_length=512)
    xbrl_target: str | None = Field(default=None, min_length=1, max_length=512)
    xbrl_continuation_ids: tuple[str, ...] | None = None
    legacy_table: str | None = Field(default=None, min_length=1, max_length=128)
    legacy_row_id: int | None = Field(default=None, gt=0)

    _office_part_sha256 = field_validator("office_part_sha256")(_validate_optional_sha256)

    @field_validator("xbrl_continuation_ids")
    @classmethod
    def _unique_xbrl_continuations(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if any(not item.strip() for item in value) or len(value) != len(set(value)):
            raise ValueError("XBRL continuation IDs must be non-empty and unique")
        return value

    @field_validator("office_package_part")
    @classmethod
    def _validate_office_package_part(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            value.startswith("/")
            or "\\" in value
            or any(segment in {"", ".", ".."} for segment in value.split("/"))
        ):
            raise ValueError("office_package_part must be a normalized package-relative part")
        return value

    @model_validator(mode="after")
    def _validate_ranges(self) -> Self:
        xbrl_identity_fields = (
            self.xbrl_package_member,
            self.xbrl_fact_id,
            self.xbrl_element_path,
            self.xbrl_concept_namespace,
            self.xbrl_concept_name,
            self.xbrl_context_id,
        )
        if any(value is not None for value in xbrl_identity_fields):
            if self.source_ref is None or any(value is None for value in xbrl_identity_fields):
                raise ValueError(
                    "XBRL locators require source, package member, fact, element, "
                    "concept, and context identity"
                )
            if self.source_ref != self.xbrl_package_member:
                raise ValueError("XBRL package member must equal the admitted source")
        elif any(
            value is not None
            for value in (
                self.xbrl_unit_id,
                self.xbrl_target,
                self.xbrl_continuation_ids,
            )
        ):
            raise ValueError("XBRL locator details require the complete XBRL identity")
        if (self.legacy_table is None) != (self.legacy_row_id is None):
            raise ValueError("legacy_table and legacy_row_id must be supplied together")
        if self.shape_index is not None and self.slide_number is None:
            raise ValueError("shape_index requires slide_number")
        if (
            self.cell_address is not None or self.cell_range is not None
        ) and self.sheet_name is None:
            raise ValueError("cell_address and cell_range require sheet_name")
        if self.cell_address is not None and self.cell_range is not None:
            raise ValueError("cell_address and cell_range are mutually exclusive")
        if self.slide_number is not None and self.sheet_name is not None:
            raise ValueError("slide and worksheet locators cannot be combined")
        office_fields = (
            self.office_package_part,
            self.office_relationship_id,
            self.office_object_ordinal,
            self.office_series_ordinal,
            self.office_part_sha256,
        )
        if self.office_object_kind is None and any(value is not None for value in office_fields):
            raise ValueError("Office native identity fields require office_object_kind")
        if self.office_object_kind is not None:
            if self.office_package_part is None:
                raise ValueError("office_object_kind requires office_package_part")
            if self.source_ref is None:
                raise ValueError("office_object_kind requires source_ref")
            if self.office_object_kind.startswith("pptx_"):
                if self.slide_number is None or self.sheet_name is not None:
                    raise ValueError("PPTX native identity requires slide_number only")
                if not self.office_package_part.startswith("ppt/"):
                    raise ValueError("PPTX native package parts must be under ppt/")
            else:
                if self.slide_number is not None:
                    raise ValueError("XLSX native identity cannot include slide_number")
                if not self.office_package_part.startswith("xl/"):
                    raise ValueError("XLSX native package parts must be under xl/")
            if self.office_object_kind == "pptx_chart_series":
                if (
                    self.office_relationship_id is None
                    or self.office_object_ordinal is None
                    or self.office_series_ordinal is None
                    or self.office_part_sha256 is None
                ):
                    raise ValueError(
                        "pptx_chart_series requires relationship, object and series "
                        "ordinals, and part SHA"
                    )
            elif self.office_series_ordinal is not None:
                raise ValueError("office_series_ordinal is valid only for pptx_chart_series")
            if self.office_object_kind == "pptx_chart" and (
                self.office_relationship_id is None
                or self.office_object_ordinal is None
                or self.office_part_sha256 is None
            ):
                raise ValueError("pptx_chart requires relationship, object ordinal, and part SHA")
            if (
                self.office_object_kind
                in {
                    "pptx_table",
                    "pptx_table_row",
                    "pptx_table_cell",
                    "xlsx_named_table",
                }
                and self.office_object_ordinal is None
            ):
                raise ValueError(f"{self.office_object_kind} requires office_object_ordinal")
            if self.office_object_kind.endswith("_inventory") and any(
                value is not None
                for value in (
                    self.office_relationship_id,
                    self.office_object_ordinal,
                    self.office_series_ordinal,
                    self.office_part_sha256,
                )
            ):
                raise ValueError("Office inventory locators cannot identify one object")
            if self.office_object_kind == "pptx_table_row" and (
                self.table_row_index is None or self.table_column_index is not None
            ):
                raise ValueError("pptx_table_row requires only table_row_index")
            if self.office_object_kind == "pptx_table_cell" and (
                self.table_row_index is None or self.table_column_index is None
            ):
                raise ValueError("pptx_table_cell requires row and column indexes")
            if self.office_object_kind == "pptx_table" and (
                self.table_row_index is not None or self.table_column_index is not None
            ):
                raise ValueError("pptx_table cannot include row or column indexes")
            if self.office_object_kind == "xlsx_named_table" and (
                self.sheet_name is None
                or self.table_name is None
                or (self.cell_address is None and self.cell_range is None)
                or self.office_relationship_id is None
                or self.office_part_sha256 is None
            ):
                raise ValueError(
                    "xlsx_named_table requires sheet, table range, relationship, "
                    "object ordinal, and part SHA"
                )
        if self.bbox is not None:
            left, top, right, bottom = self.bbox
            if right < left or bottom < top:
                raise ValueError("bbox right/bottom must not precede left/top")
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end < self.char_start
        ):
            raise ValueError("char_end must be greater than or equal to char_start")
        if (
            self.transcript_start_seconds is not None
            and self.transcript_end_seconds is not None
            and self.transcript_end_seconds < self.transcript_start_seconds
        ):
            raise ValueError("transcript_end_seconds must be after transcript_start_seconds")
        return self

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True), sort_keys=True, separators=(",", ":")
        )

    @property
    def canonical_sha256(self) -> str:
        return sha256(self.canonical_json.encode("utf-8")).hexdigest()


class SourceObservation(_LedgerRecord):
    observation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    source_kind: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1)
    blob_sha256: str
    source_published_at: datetime | None
    filing_at: datetime | None
    accepted_at: datetime | None
    observed_at: datetime
    retrieved_at: datetime
    retrieval_config_sha256: str
    collector_code_version: str = Field(min_length=1, max_length=255)

    _blob_sha256 = field_validator("blob_sha256")(_validate_sha256)
    _config_sha256 = field_validator("retrieval_config_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _validate_clocks(self) -> Self:
        if self.retrieved_at < self.observed_at:
            raise ValueError("retrieved_at must not precede observed_at")
        return self


class DocumentVersion(_LedgerRecord):
    document_version_id: str = Field(min_length=1, max_length=128)
    document_key: str = Field(min_length=1, max_length=256)
    version_sequence: int = Field(gt=0)
    observation_id: str = Field(min_length=1, max_length=128)
    blob_sha256: str
    issuer_id: str = Field(min_length=1, max_length=128)
    ticker: str | None = Field(default=None, min_length=1, max_length=16)
    document_type: str = Field(min_length=1, max_length=64)
    form_type: str = Field(min_length=1, max_length=64)
    accession_number: str | None = Field(default=None, min_length=1, max_length=64)
    exhibit_id: str | None = Field(default=None, min_length=1, max_length=128)
    period_start: datetime | None = None
    period_end: datetime | None = None
    as_of_at: datetime | None = None
    language: str = Field(min_length=2, max_length=32)
    replaces_document_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    legacy_document_id: int | None = Field(default=None, gt=0)
    recorded_at: datetime

    _blob_sha256 = field_validator("blob_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _validate_period(self) -> Self:
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_end < self.period_start
        ):
            raise ValueError("period_end must not precede period_start")
        return self


class ExtractionRun(_LedgerRecord):
    extraction_run_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    document_version_id: str = Field(min_length=1, max_length=128)
    input_sha256: str
    extractor_name: str = Field(min_length=1, max_length=128)
    extractor_config_sha256: str
    extractor_code_version: str = Field(min_length=1, max_length=255)
    output_sha256: str
    started_at: datetime
    completed_at: datetime
    outcome: _RunOutcome

    _input_sha256 = field_validator("input_sha256")(_validate_sha256)
    _config_sha256 = field_validator("extractor_config_sha256")(_validate_sha256)
    _output_sha256 = field_validator("output_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _validate_clocks(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


class EvidenceNode(_LedgerRecord):
    node_id: str = Field(min_length=1, max_length=128)
    evidence_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    extraction_run_id: str = Field(min_length=1, max_length=128)
    parent_node_id: str | None = Field(default=None, max_length=128)
    supersedes_node_id: str | None = Field(default=None, max_length=128)
    node_kind: EvidenceNodeKind
    text: str = Field(min_length=1)
    locator: EvidenceLocator | None = None
    locator_sha256: str | None = None
    recorded_at: datetime

    _locator_sha256 = field_validator("locator_sha256")(_validate_optional_sha256)

    @model_validator(mode="after")
    def _bind_locator_hash(self) -> Self:
        if self.locator is None:
            if self.locator_sha256 is not None:
                raise ValueError("locator_sha256 requires a locator")
            return self
        expected = self.locator.canonical_sha256
        if self.locator_sha256 is None:
            object.__setattr__(self, "locator_sha256", expected)
            return self
        if self.locator_sha256 != expected:
            raise ValueError("locator_sha256 must match the canonical locator JSON")
        return self


LedgerRecord: TypeAlias = (
    ContentBlob | SourceObservation | DocumentVersion | ExtractionRun | EvidenceNode
)


@dataclass(frozen=True, slots=True)
class PersistResult:
    """Result of one idempotent ``persist`` request."""

    record_id: str
    created: bool


def _matches_stored_values(existing: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    """Compare a replay with SQLite's datetime serialization normalized."""
    if len(existing) != len(expected):
        return False
    for stored, supplied in zip(existing, expected, strict=True):
        if isinstance(supplied, datetime):
            try:
                stored_time = datetime.fromisoformat(str(stored)).replace(tzinfo=None)
            except ValueError:
                return False
            if stored_time != supplied.replace(tzinfo=None):
                return False
        elif stored != supplied:
            return False
    return True


class EvidenceLedger:
    """One typed persistence API over the five canonical evidence relations."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist(self, record: LedgerRecord) -> PersistResult:
        """Atomically append one record, or report an exact idempotent replay.

        Existing rows are immutable.  Reusing an identity/idempotency key with
        different values is therefore a loud contract violation, not an update.
        The caller owns transaction commit/rollback so a full evidence chain can
        be written atomically.
        """
        if isinstance(record, DocumentVersion):
            from provenance.issuer_registry import ensure_sec_cik_evidence_binding

            ensure_sec_cik_evidence_binding(
                self._conn,
                recorded_issuer_id=record.issuer_id,
                recorded_at=record.recorded_at,
            )
            self._validate_legacy_document(record)
        table, columns, values, identity_column, identity_value = self._statement(record)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"  # nosec B608 -- trusted internal SQL shape; values remain bound
        cursor = self._conn.execute(sql, values)
        if cursor.rowcount == 1:
            return PersistResult(record_id=identity_value, created=True)
        existing = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {identity_column} = ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
            (identity_value,),
        ).fetchone()
        if existing is None or not _matches_stored_values(tuple(existing), values):
            raise ValueError(
                f"immutable {table} identity {identity_value!r} conflicts with existing data"
            )
        return PersistResult(record_id=identity_value, created=False)

    def _validate_legacy_document(self, record: DocumentVersion) -> None:
        """Validate the optional legacy bridge only when its source table exists."""
        if record.legacy_document_id is None:
            return
        has_documents = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
        ).fetchone()
        if has_documents is None:
            return
        legacy = self._conn.execute(
            "SELECT 1 FROM documents WHERE id = ?", (record.legacy_document_id,)
        ).fetchone()
        if legacy is None:
            raise ValueError(f"legacy documents.id {record.legacy_document_id} does not exist")

    def _statement(
        self, record: LedgerRecord
    ) -> tuple[str, tuple[str, ...], tuple[object, ...], str, str]:
        if isinstance(record, ContentBlob):
            return (
                "evidence_content_blobs",
                ("sha256", "byte_size", "media_type", "storage_uri", "recorded_at"),
                (
                    record.sha256,
                    record.byte_size,
                    record.media_type,
                    record.storage_uri,
                    record.recorded_at,
                ),
                "sha256",
                record.sha256,
            )
        if isinstance(record, SourceObservation):
            return (
                "evidence_source_observations",
                (
                    "observation_id",
                    "idempotency_key",
                    "source_kind",
                    "source_url",
                    "blob_sha256",
                    "source_published_at",
                    "filing_at",
                    "accepted_at",
                    "observed_at",
                    "retrieved_at",
                    "retrieval_config_sha256",
                    "collector_code_version",
                ),
                (
                    record.observation_id,
                    record.idempotency_key,
                    record.source_kind,
                    record.source_url,
                    record.blob_sha256,
                    record.source_published_at,
                    record.filing_at,
                    record.accepted_at,
                    record.observed_at,
                    record.retrieved_at,
                    record.retrieval_config_sha256,
                    record.collector_code_version,
                ),
                "idempotency_key",
                record.idempotency_key,
            )
        if isinstance(record, DocumentVersion):
            return (
                "evidence_document_versions",
                (
                    "document_version_id",
                    "document_key",
                    "version_sequence",
                    "observation_id",
                    "blob_sha256",
                    "issuer_id",
                    "ticker",
                    "document_type",
                    "form_type",
                    "accession_number",
                    "exhibit_id",
                    "period_start",
                    "period_end",
                    "as_of_at",
                    "language",
                    "replaces_document_version_id",
                    "legacy_document_id",
                    "recorded_at",
                ),
                (
                    record.document_version_id,
                    record.document_key,
                    record.version_sequence,
                    record.observation_id,
                    record.blob_sha256,
                    record.issuer_id,
                    record.ticker,
                    record.document_type,
                    record.form_type,
                    record.accession_number,
                    record.exhibit_id,
                    record.period_start,
                    record.period_end,
                    record.as_of_at,
                    record.language,
                    record.replaces_document_version_id,
                    record.legacy_document_id,
                    record.recorded_at,
                ),
                "document_version_id",
                record.document_version_id,
            )
        if isinstance(record, ExtractionRun):
            return (
                "evidence_extraction_runs",
                (
                    "extraction_run_id",
                    "idempotency_key",
                    "document_version_id",
                    "input_sha256",
                    "extractor_name",
                    "extractor_config_sha256",
                    "extractor_code_version",
                    "started_at",
                    "completed_at",
                    "outcome",
                    "output_sha256",
                ),
                (
                    record.extraction_run_id,
                    record.idempotency_key,
                    record.document_version_id,
                    record.input_sha256,
                    record.extractor_name,
                    record.extractor_config_sha256,
                    record.extractor_code_version,
                    record.started_at,
                    record.completed_at,
                    record.outcome,
                    record.output_sha256,
                ),
                "idempotency_key",
                record.idempotency_key,
            )
        return (
            "evidence_nodes",
            (
                "node_id",
                "evidence_key",
                "revision",
                "extraction_run_id",
                "parent_node_id",
                "supersedes_node_id",
                "node_kind",
                "text",
                "locator_json",
                "locator_sha256",
                "recorded_at",
            ),
            (
                record.node_id,
                record.evidence_key,
                record.revision,
                record.extraction_run_id,
                record.parent_node_id,
                record.supersedes_node_id,
                record.node_kind,
                record.text,
                None if record.locator is None else record.locator.canonical_json,
                record.locator_sha256,
                record.recorded_at,
            ),
            "node_id",
            record.node_id,
        )
