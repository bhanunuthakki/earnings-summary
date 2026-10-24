"""Sealed read-only binding from an approved IR request to registered bytes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self, TypeAlias, cast, overload
from urllib.parse import unquote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from pipeline.source_policy import fiscal_quarter_end
from provenance.evidence_ledger import EvidenceLedger, EvidenceLocator, EvidenceNode, ExtractionRun
from provenance.verifier_identity import verifier_source_artifact_sha256
from runtime.deterministic import canonical_json as _canonical_json
from runtime.deterministic import sha256_text as _sha256_text

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
_CURRENT_RESULT_WORDING = re.compile(
    r"\b(?:reports?|reported|announces?|announced)\b.{0,100}"
    r"\b(?:quarter|q[1-4])\b.{0,70}\b(?:results|earnings)\b"
    r"|\b(?:quarter|q[1-4])\b.{0,70}\b(?:results|earnings)\b.{0,100}"
    r"\b(?:reports?|reported|announces?|announced)\b",
    re.IGNORECASE,
)
_FORWARD_OR_COMPARATIVE = re.compile(
    r"\b(?:guidance|outlook|forecast|expects?|anticipated?|prior|previous|"
    r"comparable|compared|versus|year.ago)\b",
    re.IGNORECASE,
)
_REQUIRED_DOCUMENT_COLUMNS = frozenset(
    {
        "id",
        "ticker",
        "source_type",
        "doc_type",
        "period_end",
        "file_path",
        "sha256",
        "fetched_at",
        "fetch_status",
        "raw_bytes_size",
        "source_url",
    }
)


class IssuerDocumentInventoryError(RuntimeError):
    """A stable failure code; callers must not recover by guessing provenance."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedIssuerDocument(_ClosedModel):
    source_url: str = Field(min_length=1, max_length=4096)
    document_type: str = Field(min_length=1, max_length=64)

    @field_validator("source_url")
    @classmethod
    def _safe_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("source_url must be an absolute credential-free HTTP(S) URL")
        return value


class FiscalPeriodWitness(_ClosedModel):
    """Existing source-backed profile and reported document identity, never a new calendar store."""

    issuer_id: str = Field(min_length=1)
    profile_revision_id: str = Field(min_length=1)
    fiscal_year_end: str = Field(pattern=r"^\d{2}-\d{2}$")
    document_version_id: str = Field(min_length=1)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_cutoff: datetime
    observed_through: datetime

    @model_validator(mode="after")
    def _clocks(self) -> Self:
        if any(c.tzinfo is None for c in (self.knowledge_cutoff, self.observed_through)):
            raise ValueError("fiscal witness clocks require timezones")
        if self.observed_through < self.knowledge_cutoff:
            raise ValueError("fiscal witness observation cutoff precedes knowledge cutoff")
        fiscal_quarter_end(fiscal_year=2000, fiscal_quarter=4, fiscal_year_end=self.fiscal_year_end)
        return self


class CurrentReportedPeriodReview(_ClosedModel):
    """An analyst's exact first-page source judgment, bound to retained bytes."""

    schema_version: Literal["current-reported-period-review@1"] = "current-reported-period-review@1"
    document_version_id: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issuer_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    profile_revision_id: str = Field(min_length=1, max_length=128)
    subject_binding_revision_id: str = Field(min_length=1, max_length=128)
    fiscal_year: int = Field(ge=1900, le=2200)
    fiscal_quarter: int = Field(ge=1, le=4)
    period_end: date
    page_number: Literal[1] = 1
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    exact_quote: str = Field(min_length=20, max_length=800)
    period_role: Literal["current_reported_actual"] = "current_reported_actual"
    reviewer_identity: str = Field(min_length=3, max_length=128)
    review_method: Literal["agent_analyst_source_review"] = "agent_analyst_source_review"
    review_evidence: str = Field(min_length=20, max_length=1000)
    knowledge_cutoff: datetime
    observed_through: datetime
    reviewed_at: datetime

    @model_validator(mode="after")
    def _review_contract(self) -> Self:
        if (
            any(
                value.tzinfo is None
                for value in (self.knowledge_cutoff, self.observed_through, self.reviewed_at)
            )
            or not self.reviewed_at <= self.knowledge_cutoff <= self.observed_through
        ):
            raise ValueError("reported-period review clocks are inconsistent")
        if self.char_end - self.char_start != len(self.exact_quote):
            raise ValueError("reported-period quote coordinates differ")
        wording = " ".join(self.exact_quote.split())
        if (
            _CURRENT_RESULT_WORDING.search(wording) is None
            or _FORWARD_OR_COMPARATIVE.search(wording) is not None
            or str(self.fiscal_year) not in wording
            or re.search(
                rf"\b(?:q{self.fiscal_quarter}|"
                rf"{('first', 'second', 'third', 'fourth')[self.fiscal_quarter - 1]})\b",
                wording,
                re.IGNORECASE,
            )
            is None
        ):
            raise ValueError("quote does not state this current reported actual quarter")
        return self


def current_reported_period_review_identity(
    review: CurrentReportedPeriodReview,
) -> tuple[str, str]:
    """Stable node and content identity used by inventory and coverage proofs."""
    return (
        "ir-current-period-review:" + _sha256_text(review.document_version_id),
        _sha256_text(_canonical_json(review.model_dump(mode="json"))),
    )


class _FiscalRequest(_ClosedModel):
    fiscal_period_witness: FiscalPeriodWitness | None = None

    @model_serializer(mode="wrap")
    def _legacy_payload(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        result = cast("dict[str, object]", handler(self))
        if self.fiscal_period_witness is None:
            result.pop("fiscal_period_witness", None)
        return result


class IssuerDocumentInventoryRequest(_FiscalRequest):
    """Closed expected URL/doc-type population from the source-discovery outcome."""

    schema_version: Literal["issuer_document_inventory_request.v1"] = (
        "issuer_document_inventory_request.v1"
    )
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    fiscal_year: int = Field(ge=1900, le=2200)
    fiscal_quarter: int = Field(ge=1, le=4)
    period_end: date
    expected_documents: tuple[ExpectedIssuerDocument, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _closed_set(self) -> Self:
        if self.ticker != self.ticker.strip().upper() or not _TICKER.fullmatch(self.ticker):
            raise ValueError("ticker must be canonical uppercase")
        expected_end = (
            _quarter_end(self.fiscal_year, self.fiscal_quarter)
            if self.fiscal_period_witness is None
            else fiscal_quarter_end(
                fiscal_year=self.fiscal_year,
                fiscal_quarter=self.fiscal_quarter,
                fiscal_year_end=self.fiscal_period_witness.fiscal_year_end,
            )
        )
        if self.period_end != expected_end:
            raise ValueError("period_end must match the requested fiscal quarter")
        keys = [(item.source_url, item.document_type) for item in self.expected_documents]
        if keys != sorted(keys):
            raise ValueError("expected_documents must be sorted by URL and document type")
        if len({item.source_url for item in self.expected_documents}) != len(
            self.expected_documents
        ):
            raise ValueError("expected document URLs must be unique")
        if len({item.document_type for item in self.expected_documents}) != len(
            self.expected_documents
        ):
            raise ValueError("expected document types must be unique")
        return self

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))

    @property
    def request_sha256(self) -> str:
        return _sha256_text(self.canonical_json)


class DatabaseBinding(_ClosedModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)
    alembic_revision: str = Field(min_length=1, max_length=255)
    documents_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_members: tuple[DatabaseStorageMember, DatabaseStorageMember]
    storage_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _sealed_storage_bundle(self) -> Self:
        members = self.storage_members
        if tuple(member.name for member in members) != ("main", "wal"):
            raise ValueError("storage members must be ordered main then WAL")
        main = members[0]
        if not main.present or main.sha256 != self.sha256 or main.byte_size != self.byte_size:
            raise ValueError("database legacy identity must match the main storage member")
        bundle_sha256 = _sha256_text(
            _canonical_json([member.model_dump(mode="json") for member in members])
        )
        if self.storage_bundle_sha256 != bundle_sha256:
            raise ValueError("storage_bundle_sha256 must bind canonical ordered storage members")
        return self


class DatabaseStorageMember(_ClosedModel):
    """One exact SQLite main/WAL storage member, without a physical path."""

    name: Literal["main", "wal"]
    present: bool
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    byte_size: int | None = Field(default=None, ge=0)
    file_token_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _presence_is_complete(self) -> Self:
        values = (self.sha256, self.byte_size, self.file_token_sha256)
        if self.present and not all(value is not None for value in values):
            raise ValueError("storage member presence must match its complete identity")
        if not self.present and any(value is not None for value in values):
            raise ValueError("absent storage member cannot carry an identity")
        return self


@dataclass(frozen=True, slots=True)
class _DatabaseBundle:
    members: tuple[DatabaseStorageMember, DatabaseStorageMember]

    @property
    def main(self) -> DatabaseStorageMember:
        return self.members[0]

    @property
    def bundle_sha256(self) -> str:
        return _sha256_text(
            _canonical_json([member.model_dump(mode="json") for member in self.members])
        )


class IssuerDocumentInventoryRecord(_ClosedModel):
    document_id: int = Field(gt=0)
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    period_end: date
    source_type: Literal["ir_doc"] = "ir_doc"
    document_type: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1, max_length=4096)
    local_path: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    fetched_at: datetime

    @model_validator(mode="after")
    def _fetched_at_is_utc(self) -> Self:
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() != UTC.utcoffset(None):
            raise ValueError("fetched_at must be timezone-aware UTC")
        return self


class IssuerDocumentInventoryReceipt(_ClosedModel):
    schema_version: Literal["issuer_document_inventory_receipt.v1"] = (
        "issuer_document_inventory_receipt.v1"
    )
    request: IssuerDocumentInventoryRequest
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database: DatabaseBinding
    verifier_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: tuple[IssuerDocumentInventoryRecord, ...] = Field(min_length=1)
    document_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _sealed_binding(self) -> Self:
        if self.request_sha256 != self.request.request_sha256:
            raise ValueError("request_sha256 must bind the canonical request")
        expected = {
            (item.source_url, item.document_type) for item in self.request.expected_documents
        }
        actual = {(item.source_url, item.document_type) for item in self.records}
        if actual != expected or len(self.records) != len(expected):
            raise ValueError("records must exactly cover the requested URL/doc-type set")
        if any(
            record.ticker != self.request.ticker or record.period_end != self.request.period_end
            for record in self.records
        ):
            raise ValueError("record identity must match the request")
        if (
            tuple(sorted(self.records, key=lambda item: (item.source_url, item.document_type)))
            != self.records
        ):
            raise ValueError("records must be canonically sorted")
        if self.document_set_sha256 != _sha256_text(
            _canonical_json([r.model_dump(mode="json") for r in self.records])
        ):
            raise ValueError("document_set_sha256 must bind exact canonical records")
        if self.receipt_sha256 != _sha256_text(self._unsigned_json()):
            raise ValueError("receipt_sha256 must bind the complete unsigned receipt")
        return self

    def _unsigned_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json", exclude={"receipt_sha256"}))

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))


def _build_v1_inventory(
    conn: sqlite3.Connection,
    *,
    database_path: Path,
    repo_root: Path,
    request: IssuerDocumentInventoryRequest,
    transaction_open: bool = False,
) -> IssuerDocumentInventoryReceipt:
    """Bind exactly one valid registry row and local byte stream to every URL."""
    db_path = database_path.resolve(strict=True)
    root = repo_root.resolve(strict=True)
    before = _database_bundle(db_path)
    try:
        if transaction_open:
            if not conn.in_transaction:
                raise IssuerDocumentInventoryError("snapshot_not_open")
        else:
            conn.execute("BEGIN")
        # Establish exactly one SQLite reader snapshot before any registry
        # query.  In WAL mode this pins the reader to one committed WAL frame.
        conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
        validate_fiscal_period_witness(conn, request=request)
        binding = _database_binding(conn, before)
        records = tuple(
            _load_expected_document(conn, root=root, request=request, expected=expected)
            for expected in request.expected_documents
        )
        ordered = tuple(sorted(records, key=lambda item: (item.source_url, item.document_type)))
    except sqlite3.Error as exc:
        raise IssuerDocumentInventoryError("schema_drift") from exc
    finally:
        if not transaction_open and conn.in_transaction:
            conn.rollback()
    after = _database_bundle(db_path)
    if before != after:
        raise IssuerDocumentInventoryError("database_changed")
    record_payload = [record.model_dump(mode="json") for record in ordered]
    document_set_sha256 = _sha256_text(_canonical_json(record_payload))
    verifier_code_sha256 = _verifier_code_sha256()
    unsigned: dict[str, object] = {
        "schema_version": "issuer_document_inventory_receipt.v1",
        "request": request.model_dump(mode="json"),
        "request_sha256": request.request_sha256,
        "database": binding.model_dump(mode="json"),
        "verifier_code_sha256": verifier_code_sha256,
        "records": record_payload,
        "document_set_sha256": document_set_sha256,
    }
    return IssuerDocumentInventoryReceipt(
        request=request,
        request_sha256=request.request_sha256,
        database=binding,
        verifier_code_sha256=verifier_code_sha256,
        records=ordered,
        document_set_sha256=document_set_sha256,
        receipt_sha256=_sha256_text(_canonical_json(unsigned)),
    )


def _load_expected_document(
    conn: sqlite3.Connection,
    *,
    root: Path,
    request: IssuerDocumentInventoryRequest,
    expected: ExpectedIssuerDocument,
) -> IssuerDocumentInventoryRecord:
    try:
        rows = conn.execute(
            "SELECT id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url FROM documents WHERE source_url = ?",
            (expected.source_url,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise IssuerDocumentInventoryError("schema_drift") from exc
    if not rows:
        raise IssuerDocumentInventoryError("missing_document")
    if len(rows) != 1:
        raise IssuerDocumentInventoryError("duplicate_document_url")
    return _registered_record(
        rows[0],
        root=root,
        ticker=request.ticker,
        period_end=request.period_end,
        document_type=expected.document_type,
        source_url=expected.source_url,
    )


def _registered_record(
    row: sqlite3.Row,
    *,
    root: Path,
    ticker: str,
    period_end: date,
    document_type: str,
    source_url: str,
) -> IssuerDocumentInventoryRecord:
    if str(row["ticker"]) != ticker:
        raise IssuerDocumentInventoryError("noncanonical_ticker")
    if str(row["source_type"]) != "ir_doc":
        raise IssuerDocumentInventoryError("wrong_source_type")
    if str(row["doc_type"]) != document_type:
        raise IssuerDocumentInventoryError("wrong_document_type")
    if _document_period(row["period_end"]) != period_end:
        raise IssuerDocumentInventoryError("ambiguous_period")
    if str(row["fetch_status"]) != "ok":
        raise IssuerDocumentInventoryError("invalid_fetch_status")
    fetched_at = _parse_utc(row["fetched_at"])
    if fetched_at is None:
        raise IssuerDocumentInventoryError("invalid_fetched_at")
    try:
        document_id, byte_size = int(row["id"]), int(row["raw_bytes_size"])
    except (TypeError, ValueError) as exc:
        raise IssuerDocumentInventoryError("schema_drift") from exc
    if document_id <= 0 or byte_size <= 0:
        raise IssuerDocumentInventoryError("invalid_byte_size")
    raw_sha = str(row["sha256"])
    if not _SHA256.fullmatch(raw_sha):
        raise IssuerDocumentInventoryError("invalid_document_hash")
    path = _safe_relative_file(row["file_path"], root)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise IssuerDocumentInventoryError("missing_local_file") from exc
    if len(payload) != byte_size:
        raise IssuerDocumentInventoryError("byte_size_mismatch")
    if hashlib.sha256(payload).hexdigest() != raw_sha:
        raise IssuerDocumentInventoryError("document_hash_mismatch")
    return IssuerDocumentInventoryRecord(
        document_id=document_id,
        ticker=ticker,
        period_end=period_end,
        document_type=document_type,
        source_url=source_url,
        local_path=path.relative_to(root).as_posix(),
        sha256=raw_sha,
        byte_size=byte_size,
        fetched_at=fetched_at,
    )


class IssuerDocumentAliasBinding(_ClosedModel):
    """Reviewed exact native lineage, separate from the immutable legacy URL."""

    document_id: int = Field(gt=0)
    document_version_id: str = Field(min_length=1, max_length=128)
    blob_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_observation_id: str = Field(min_length=1, max_length=128)
    document_link_id: str = Field(min_length=1, max_length=128)
    recorded_issuer_id: str = Field(min_length=1, max_length=128)
    issuer_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    subject_binding_revision_id: str = Field(min_length=1, max_length=128)
    registered_source_url: str = Field(min_length=1, max_length=4096)
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExpectedIssuerDocumentAlias(ExpectedIssuerDocument):
    alias: IssuerDocumentAliasBinding


class IssuerDocumentAliasInventoryRequest(_FiscalRequest):
    schema_version: Literal["issuer_document_inventory_request.v2_alias"] = (
        "issuer_document_inventory_request.v2_alias"
    )
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    fiscal_year: int = Field(ge=1900, le=2200)
    fiscal_quarter: int = Field(ge=1, le=4)
    period_end: date
    expected_documents: tuple[ExpectedIssuerDocumentAlias, ...] = Field(
        min_length=1, max_length=500
    )
    knowledge_cutoff: datetime
    observed_through: datetime

    @model_validator(mode="after")
    def _closed_alias_set(self) -> Self:
        # The new route retains v1's period, URL and closed-set rules.
        IssuerDocumentInventoryRequest(
            ticker=self.ticker,
            fiscal_year=self.fiscal_year,
            fiscal_quarter=self.fiscal_quarter,
            period_end=self.period_end,
            fiscal_period_witness=self.fiscal_period_witness,
            expected_documents=tuple(
                ExpectedIssuerDocument(source_url=item.source_url, document_type=item.document_type)
                for item in self.expected_documents
            ),
        )
        if self.fiscal_period_witness is not None and (
            self.fiscal_period_witness.knowledge_cutoff != self.knowledge_cutoff
            or self.fiscal_period_witness.observed_through != self.observed_through
        ):
            raise ValueError("alias and fiscal witness clocks must match")
        if any(clock.tzinfo is None for clock in (self.knowledge_cutoff, self.observed_through)):
            raise ValueError("alias cutoffs require explicit timezones")
        if self.observed_through < self.knowledge_cutoff:
            raise ValueError("observed_through must not precede knowledge_cutoff")
        ids = [item.alias.document_id for item in self.expected_documents]
        versions = [item.alias.document_version_id for item in self.expected_documents]
        if len(set(ids)) != len(ids) or len(set(versions)) != len(versions):
            raise ValueError("aliases require unique document IDs and versions")
        return self

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))

    @property
    def request_sha256(self) -> str:
        return _sha256_text(self.canonical_json)


class IssuerDocumentAliasInventoryRecord(IssuerDocumentInventoryRecord):
    registered_source_url: str = Field(min_length=1, max_length=4096)
    alias: IssuerDocumentAliasBinding
    lineage: dict[str, JsonValue]

    @model_validator(mode="after")
    def _exact_alias_record(self) -> Self:
        if (self.document_id, self.sha256, self.registered_source_url) != (
            self.alias.document_id,
            self.alias.blob_sha256,
            self.alias.registered_source_url,
        ):
            raise ValueError("record must preserve exact registered identity")
        if _sha256_text(_canonical_json(self.lineage)) != self.alias.lineage_sha256:
            raise ValueError("alias must bind exact persisted lineage")
        return self


class IssuerDocumentAliasInventoryReceipt(_ClosedModel):
    schema_version: Literal["issuer_document_inventory_receipt.v2_alias"] = (
        "issuer_document_inventory_receipt.v2_alias"
    )
    request: IssuerDocumentAliasInventoryRequest
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database: DatabaseBinding
    verifier_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: tuple[IssuerDocumentAliasInventoryRecord, ...] = Field(min_length=1)
    document_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _sealed_alias_binding(self) -> Self:
        if self.request_sha256 != self.request.request_sha256:
            raise ValueError("request_sha256 must bind the canonical request")
        expected = [
            (item.source_url, item.document_type, item.alias)
            for item in self.request.expected_documents
        ]
        actual = [(item.source_url, item.document_type, item.alias) for item in self.records]
        if actual != expected:
            raise ValueError("records must exactly bind ordered requested aliases")
        if any(
            r.ticker != self.request.ticker or r.period_end != self.request.period_end
            for r in self.records
        ):
            raise ValueError("record identity must match the request")
        if self.document_set_sha256 != _sha256_text(
            _canonical_json([r.model_dump(mode="json") for r in self.records])
        ):
            raise ValueError("document_set_sha256 must bind exact canonical records")
        if self.receipt_sha256 != _sha256_text(
            _canonical_json(self.model_dump(mode="json", exclude={"receipt_sha256"}))
        ):
            raise ValueError("receipt_sha256 must bind the complete unsigned receipt")
        return self

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))


IssuerDocumentInventoryRequestAny: TypeAlias = (
    IssuerDocumentInventoryRequest | IssuerDocumentAliasInventoryRequest
)
IssuerDocumentInventoryReceiptAny: TypeAlias = (
    IssuerDocumentInventoryReceipt | IssuerDocumentAliasInventoryReceipt
)


def load_issuer_document_inventory_request(raw: str | bytes) -> IssuerDocumentInventoryRequestAny:
    """Explicit version dispatcher; v1 remains strict and has no alias fallback."""
    envelope = json.loads(raw)
    if not isinstance(envelope, dict):
        raise ValueError("inventory request must be an object")
    envelope = cast(dict[str, object], envelope)
    if envelope.get("schema_version") == "issuer_document_inventory_request.v2_alias":
        return IssuerDocumentAliasInventoryRequest.model_validate_json(raw)
    return IssuerDocumentInventoryRequest.model_validate_json(raw)


def load_issuer_document_inventory_receipt(raw: str | bytes) -> IssuerDocumentInventoryReceiptAny:
    envelope = json.loads(raw)
    if not isinstance(envelope, dict):
        raise ValueError("inventory receipt must be an object")
    envelope = cast(dict[str, object], envelope)
    if envelope.get("schema_version") == "issuer_document_inventory_receipt.v2_alias":
        return IssuerDocumentAliasInventoryReceipt.model_validate_json(raw)
    return IssuerDocumentInventoryReceipt.model_validate_json(raw)


def _alias_row(
    conn: sqlite3.Connection, table: str, identity: str, value: str
) -> dict[str, JsonValue]:
    # Tables/columns are internal constants; reviewed values remain bound.
    cursor = conn.execute(f"SELECT * FROM {table} WHERE {identity}=?", (value,))  # nosec B608
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise IssuerDocumentInventoryError("alias_lineage_missing_or_ambiguous")
    keys = [str(column[0]) for column in cursor.description or ()]
    return dict(zip(keys, tuple(rows[0]), strict=True))


def _alias_clock(value: JsonValue, limit: datetime) -> None:
    if not isinstance(value, str):
        raise IssuerDocumentInventoryError("alias_clock_invalid")
    try:
        clock = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IssuerDocumentInventoryError("alias_clock_invalid") from exc
    if (clock.replace(tzinfo=UTC) if clock.tzinfo is None else clock) > limit:
        raise IssuerDocumentInventoryError("alias_lineage_after_cutoff")


def _alias_visible(value: JsonValue, limit: datetime) -> bool:
    try:
        _alias_clock(value, limit)
    except IssuerDocumentInventoryError as exc:
        if exc.reason_code == "alias_lineage_after_cutoff":
            return False
        raise
    return True


def inspect_issuer_document_alias(
    conn: sqlite3.Connection,
    *,
    expected: ExpectedIssuerDocumentAlias,
    request: IssuerDocumentAliasInventoryRequest,
) -> dict[str, JsonValue]:
    """Read actual lineage for review; this does not seal or grant an alias."""
    alias = expected.alias
    version = _alias_row(
        conn, "evidence_document_versions", "document_version_id", alias.document_version_id
    )
    observation = _alias_row(
        conn, "evidence_source_observations", "observation_id", alias.source_observation_id
    )
    original = _alias_row(
        conn, "evidence_source_observations", "observation_id", str(version["observation_id"])
    )
    link = _alias_row(
        conn, "evidence_document_observation_links", "link_id", alias.document_link_id
    )
    blob = _alias_row(conn, "evidence_content_blobs", "sha256", alias.blob_sha256)
    subject = _alias_row(
        conn,
        "recorded_subject_binding_revisions",
        "binding_revision_id",
        alias.subject_binding_revision_id,
    )
    entity = _alias_row(
        conn, "reporting_entities", "reporting_entity_id", alias.reporting_entity_id
    )
    issuer = _alias_row(conn, "issuer_entities", "issuer_id", alias.issuer_id)
    knowledge, observed = request.knowledge_cutoff, request.observed_through
    for item in (observation, original):
        _alias_clock(item["observed_at"], knowledge)
        _alias_clock(item["retrieved_at"], observed)
    for item, key, cutoff in (
        (version, "recorded_at", observed),
        (link, "linked_at", observed),
        (blob, "recorded_at", observed),
        (subject, "knowledge_at", knowledge),
        (subject, "effective_at", knowledge),
        (subject, "recorded_at", observed),
        (issuer, "created_at", knowledge),
        (entity, "created_at", knowledge),
    ):
        _alias_clock(item[key], cutoff)
    if (
        version["legacy_document_id"],
        version["blob_sha256"],
        version["issuer_id"],
        version["ticker"],
        version["document_type"],
    ) != (
        alias.document_id,
        alias.blob_sha256,
        alias.recorded_issuer_id,
        request.ticker,
        expected.document_type,
    ) or _document_period(version["period_end"]) != request.period_end:
        raise IssuerDocumentInventoryError("alias_document_scope_mismatch")
    if (observation["source_url"], observation["blob_sha256"]) != (
        expected.source_url,
        alias.blob_sha256,
    ) or (original["source_url"], original["blob_sha256"]) != (
        alias.registered_source_url,
        alias.blob_sha256,
    ):
        raise IssuerDocumentInventoryError("alias_source_mismatch")
    if (link["document_version_id"], link["observation_id"]) != (
        alias.document_version_id,
        alias.source_observation_id,
    ) or link["link_kind"] not in {"retrieval", "mirror", "primary"}:
        raise IssuerDocumentInventoryError("alias_link_mismatch")
    if (
        (
            subject["recorded_issuer_id"],
            subject["issuer_id"],
            subject["reporting_entity_id"],
            subject["outcome"],
            subject["material_dissent"],
            subject["security_id"],
        )
        != (
            alias.recorded_issuer_id,
            alias.issuer_id,
            alias.reporting_entity_id,
            "selected",
            0,
            None,
        )
        or entity["issuer_id"] != alias.issuer_id
        or entity["reporting_entity_kind"] != "legal_registrant"
    ):
        raise IssuerDocumentInventoryError("alias_subject_mismatch")
    current_subject = conn.execute(
        "SELECT binding_revision_id,knowledge_at,recorded_at FROM recorded_subject_binding_revisions WHERE recorded_issuer_id=? ORDER BY revision DESC",
        (alias.recorded_issuer_id,),
    ).fetchall()
    visible_subject = [
        row
        for row in current_subject
        if _alias_visible(row[1], knowledge) and _alias_visible(row[2], observed)
    ]
    if not visible_subject or str(visible_subject[0][0]) != alias.subject_binding_revision_id:
        raise IssuerDocumentInventoryError("alias_subject_not_current")
    current_versions = conn.execute(
        "SELECT document_version_id,recorded_at FROM evidence_document_versions WHERE document_key=? ORDER BY version_sequence DESC",
        (version["document_key"],),
    ).fetchall()
    visible_versions = [row for row in current_versions if _alias_visible(row[1], observed)]
    if not visible_versions or str(visible_versions[0][0]) != alias.document_version_id:
        raise IssuerDocumentInventoryError("alias_document_not_current")
    matches = conn.execute(
        "SELECT DISTINCT version.document_version_id,version.recorded_at,link.linked_at,observation.observed_at,observation.retrieved_at FROM evidence_document_versions version JOIN evidence_document_observation_links link ON link.document_version_id=version.document_version_id JOIN evidence_source_observations observation ON observation.observation_id=link.observation_id WHERE observation.source_url=? AND version.legacy_document_id IS NOT NULL",
        (expected.source_url,),
    ).fetchall()
    if {
        str(row[0])
        for row in matches
        if all(
            _alias_visible(value, limit)
            for value, limit in zip(row[1:], (observed, observed, knowledge, observed), strict=True)
        )
    } != {alias.document_version_id}:
        raise IssuerDocumentInventoryError("alias_source_ambiguous")
    return {
        "document_version": version,
        "source_observation": observation,
        "original_observation": original,
        "document_link": link,
        "content_blob": blob,
        "subject_binding": subject,
        "reporting_entity": entity,
        "issuer": issuer,
    }


def _build_alias_inventory(
    conn: sqlite3.Connection,
    *,
    database_path: Path,
    repo_root: Path,
    request: IssuerDocumentAliasInventoryRequest,
    transaction_open: bool,
) -> IssuerDocumentAliasInventoryReceipt:
    request = IssuerDocumentAliasInventoryRequest.model_validate(request.model_dump())
    db_path, root = database_path.resolve(strict=True), repo_root.resolve(strict=True)
    before = _database_bundle(db_path)
    try:
        if transaction_open:
            if not conn.in_transaction:
                raise IssuerDocumentInventoryError("snapshot_not_open")
        else:
            conn.execute("BEGIN")
        conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
        validate_fiscal_period_witness(conn, request=request)
        binding = _database_binding(conn, before)
        records: list[IssuerDocumentAliasInventoryRecord] = []
        for expected in request.expected_documents:
            proof = inspect_issuer_document_alias(conn, expected=expected, request=request)
            if _sha256_text(_canonical_json(proof)) != expected.alias.lineage_sha256:
                raise IssuerDocumentInventoryError("alias_lineage_commitment_mismatch")
            row = conn.execute(
                "SELECT id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url FROM documents WHERE id=?",
                (expected.alias.document_id,),
            ).fetchone()
            if row is None or str(row["source_url"]) != expected.alias.registered_source_url:
                raise IssuerDocumentInventoryError("alias_registered_identity_mismatch")
            record = _registered_record(
                row,
                root=root,
                ticker=request.ticker,
                period_end=request.period_end,
                document_type=expected.document_type,
                source_url=expected.alias.registered_source_url,
            )
            if (
                record.document_id != expected.alias.document_id
                or record.sha256 != expected.alias.blob_sha256
            ):
                raise IssuerDocumentInventoryError("alias_registered_identity_mismatch")
            blob = proof["content_blob"]
            if not isinstance(blob, dict) or blob.get("byte_size") != record.byte_size:
                raise IssuerDocumentInventoryError("alias_blob_size_mismatch")
            records.append(
                IssuerDocumentAliasInventoryRecord(
                    **record.model_dump(exclude={"source_url"}),
                    source_url=expected.source_url,
                    registered_source_url=record.source_url,
                    alias=expected.alias,
                    lineage=proof,
                )
            )
    except sqlite3.Error as exc:
        raise IssuerDocumentInventoryError("schema_drift") from exc
    finally:
        if not transaction_open and conn.in_transaction:
            conn.rollback()
    if before != _database_bundle(db_path):
        raise IssuerDocumentInventoryError("database_changed")
    unsigned = {
        "schema_version": "issuer_document_inventory_receipt.v2_alias",
        "request": request.model_dump(mode="json"),
        "request_sha256": request.request_sha256,
        "database": binding.model_dump(mode="json"),
        "verifier_code_sha256": _verifier_code_sha256(),
        "records": [r.model_dump(mode="json") for r in records],
        "document_set_sha256": _sha256_text(
            _canonical_json([r.model_dump(mode="json") for r in records])
        ),
    }
    return IssuerDocumentAliasInventoryReceipt.model_validate(
        {**unsigned, "receipt_sha256": _sha256_text(_canonical_json(unsigned))}
    )


@overload
def build_issuer_document_inventory(
    conn: sqlite3.Connection,
    *,
    database_path: Path,
    repo_root: Path,
    request: IssuerDocumentInventoryRequest,
    transaction_open: bool = False,
) -> IssuerDocumentInventoryReceipt: ...
@overload
def build_issuer_document_inventory(
    conn: sqlite3.Connection,
    *,
    database_path: Path,
    repo_root: Path,
    request: IssuerDocumentAliasInventoryRequest,
    transaction_open: bool = False,
) -> IssuerDocumentAliasInventoryReceipt: ...
def build_issuer_document_inventory(
    conn: sqlite3.Connection,
    *,
    database_path: Path,
    repo_root: Path,
    request: IssuerDocumentInventoryRequestAny,
    transaction_open: bool = False,
) -> IssuerDocumentInventoryReceiptAny:
    if isinstance(request, IssuerDocumentAliasInventoryRequest):
        return _build_alias_inventory(
            conn,
            database_path=database_path,
            repo_root=repo_root,
            request=request,
            transaction_open=transaction_open,
        )
    return _build_v1_inventory(
        conn,
        database_path=database_path,
        repo_root=repo_root,
        request=request,
        transaction_open=transaction_open,
    )


def validate_fiscal_period_witness(
    conn: sqlite3.Connection, *, request: IssuerDocumentInventoryRequestAny
) -> None:
    witness = request.fiscal_period_witness
    if witness is None:
        return
    if request.period_end != fiscal_quarter_end(
        fiscal_year=request.fiscal_year,
        fiscal_quarter=request.fiscal_quarter,
        fiscal_year_end=witness.fiscal_year_end,
    ):
        raise IssuerDocumentInventoryError("fiscal_witness_period_mismatch")
    profile = _alias_row(
        conn, "issuer_profile_revisions", "profile_revision_id", witness.profile_revision_id
    )
    version = _alias_row(
        conn, "evidence_document_versions", "document_version_id", witness.document_version_id
    )
    details = json.loads(str(profile["reason_details_json"]))
    details = dict(details)
    observation_id = details.get("source_observation_id")
    if not isinstance(observation_id, str):
        raise IssuerDocumentInventoryError("fiscal_profile_source_missing")
    source = _alias_row(conn, "evidence_source_observations", "observation_id", observation_id)
    cik_assertions = conn.execute(
        "SELECT DISTINCT normalized_value FROM issuer_identifier_assertions "
        "WHERE issuer_id=? AND identifier_type='sec_cik' AND source_observation_id=?",
        (witness.issuer_id, observation_id),
    ).fetchall()
    if len(cik_assertions) != 1 or source["source_url"] != (
        f"https://data.sec.gov/submissions/CIK{cik_assertions[0][0]}.json"
    ):
        raise IssuerDocumentInventoryError("fiscal_profile_authority_mismatch")
    document_source = _alias_row(
        conn, "evidence_source_observations", "observation_id", str(version["observation_id"])
    )
    subject = conn.execute(
        "SELECT issuer_id,outcome,material_dissent,knowledge_at,recorded_at,effective_at,"
        "binding_revision_id "
        "FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id=? ORDER BY revision DESC LIMIT 1",
        (f"legacy-ticker:{request.ticker}",),
    ).fetchone()
    if subject is None or tuple(subject[:3]) != (witness.issuer_id, "selected", 0):
        raise IssuerDocumentInventoryError("fiscal_subject_mismatch")
    _alias_clock(subject[3], witness.knowledge_cutoff)
    _alias_clock(subject[4], witness.observed_through)
    _alias_clock(subject[5], witness.knowledge_cutoff)
    reviewed_period = verified_current_reported_period_review(
        conn,
        document_version_id=witness.document_version_id,
        source_sha256=witness.document_sha256,
        period_end=request.period_end,
    )
    if (
        profile["issuer_id"] != witness.issuer_id
        or profile["status"] != "active"
        or profile["fiscal_year_end"] != witness.fiscal_year_end
        or details.get("field_locator") != "/fiscalYearEnd"
        or details.get("source_sha256") != source["blob_sha256"]
        or source["source_kind"] != "sec_submissions"
        or document_source["blob_sha256"] != witness.document_sha256
        or version["issuer_id"] not in (witness.issuer_id, f"legacy-ticker:{request.ticker}")
        or version["ticker"] != request.ticker
        or version["blob_sha256"] != witness.document_sha256
        or (
            datetime.fromisoformat(str(version["period_end"])).date() != request.period_end
            if version["period_end"] is not None
            else reviewed_period is None
        )
    ):
        raise IssuerDocumentInventoryError("fiscal_witness_identity_mismatch")
    if reviewed_period is not None and (
        reviewed_period.issuer_id != witness.issuer_id
        or reviewed_period.ticker != request.ticker
        or reviewed_period.profile_revision_id != witness.profile_revision_id
        or reviewed_period.subject_binding_revision_id != subject[6]
        or reviewed_period.knowledge_cutoff > witness.knowledge_cutoff
        or reviewed_period.observed_through > witness.observed_through
        or reviewed_period.reviewed_at > witness.knowledge_cutoff
    ):
        raise IssuerDocumentInventoryError("fiscal_review_binding_mismatch")
    for row, field, cutoff in (
        (profile, "knowledge_at", witness.knowledge_cutoff),
        (profile, "effective_at", witness.knowledge_cutoff),
        (profile, "recorded_at", witness.observed_through),
        (source, "observed_at", witness.knowledge_cutoff),
        (source, "retrieved_at", witness.observed_through),
        (version, "recorded_at", witness.observed_through),
        (document_source, "observed_at", witness.knowledge_cutoff),
        (document_source, "retrieved_at", witness.observed_through),
    ):
        _alias_clock(row[field], cutoff)
    newer = conn.execute(
        "SELECT knowledge_at,recorded_at FROM issuer_profile_revisions "
        "WHERE issuer_id=? AND revision>?",
        (witness.issuer_id, profile["revision"]),
    ).fetchall()
    if any(
        _alias_visible(row[0], witness.knowledge_cutoff)
        and _alias_visible(row[1], witness.observed_through)
        for row in newer
    ):
        raise IssuerDocumentInventoryError("fiscal_profile_superseded")


def _review_pdf_bytes(
    conn: sqlite3.Connection, document_version_id: str
) -> tuple[dict[str, object], bytes]:
    version = _alias_row(
        conn, "evidence_document_versions", "document_version_id", document_version_id
    )
    source = _alias_row(
        conn, "evidence_source_observations", "observation_id", str(version["observation_id"])
    )
    blob = _alias_row(conn, "evidence_content_blobs", "sha256", str(version["blob_sha256"]))
    locations = conn.execute(
        "SELECT availability_state,verified_sha256,verified_byte_size FROM "
        "evidence_blob_location_observations WHERE blob_sha256=? AND storage_uri=? "
        "ORDER BY location_sequence DESC LIMIT 1",
        (version["blob_sha256"], blob["storage_uri"]),
    ).fetchall()
    uri = urlsplit(str(blob["storage_uri"]))
    if (
        len(locations) != 1
        or tuple(locations[0]) != ("present", version["blob_sha256"], blob["byte_size"])
        or uri.scheme != "file"
        or uri.netloc not in {"", "localhost"}
        or source["blob_sha256"] != version["blob_sha256"]
        or source["source_kind"] != "ir_document"
        or blob["media_type"] != "application/pdf"
    ):
        raise IssuerDocumentInventoryError("reported_period_source_identity_mismatch")
    try:
        raw = Path(unquote(uri.path)).read_bytes()
    except OSError as exc:
        raise IssuerDocumentInventoryError("reported_period_source_unavailable") from exc
    if (
        not raw.startswith(b"%PDF-")
        or len(raw) != int(blob["byte_size"])
        or hashlib.sha256(raw).hexdigest() != version["blob_sha256"]
    ):
        raise IssuerDocumentInventoryError("reported_period_source_hash_mismatch")
    return version, raw


def verified_current_reported_period_review(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    source_sha256: str,
    period_end: date,
) -> CurrentReportedPeriodReview | None:
    """Replay the sole reviewed quarter against immutable native PDF text."""
    rows = conn.execute(
        "SELECT node.text,node.locator_json,node.locator_sha256,run.input_sha256,"
        "run.output_sha256,run.outcome,node.node_id,node.evidence_key,node.revision,"
        "node.node_kind,node.recorded_at,run.extraction_run_id,run.extractor_config_sha256,"
        "run.extractor_code_version,run.completed_at FROM evidence_nodes node "
        "JOIN evidence_extraction_runs run ON run.extraction_run_id=node.extraction_run_id "
        "WHERE run.document_version_id=? AND run.extractor_name='ir-current-reported-period'",
        (document_version_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    (
        text,
        locator_json,
        locator_sha,
        input_sha,
        output_sha,
        outcome,
        node_id,
        evidence_key,
        revision,
        node_kind,
        node_recorded_at,
        run_id,
        config_sha,
        code_version,
        completed_at,
    ) = rows[0]
    try:
        review = CurrentReportedPeriodReview.model_validate_json(str(text))
        locator = EvidenceLocator.model_validate_json(str(locator_json))
        version, raw = _review_pdf_bytes(conn, document_version_id)
        from provenance.fulltext_backfill import project_pdf_page_texts

        page = project_pdf_page_texts(raw)[0]
        node_recorded = datetime.fromisoformat(str(node_recorded_at))
        run_completed = datetime.fromisoformat(str(completed_at))
    except Exception:  # Native PDF parsing is optional; an unverified source stays pending.
        return None
    if (
        review.document_version_id != document_version_id
        or review.source_sha256 != source_sha256
        or review.period_end != period_end
        or version["blob_sha256"] != source_sha256
        or input_sha != source_sha256
        or output_sha != _sha256_text(str(text))
        or outcome != "succeeded"
        or node_id != current_reported_period_review_identity(review)[0]
        or evidence_key != node_id
        or revision != 1
        or node_kind != "claim"
        or node_recorded != review.reviewed_at
        or run_id != "ir-current-period-run:" + _sha256_text(document_version_id)
        or config_sha != _sha256_text("ir-current-reported-period@1")
        or code_version != "ir-current-reported-period@1"
        or run_completed != review.reviewed_at
        or locator.canonical_sha256 != locator_sha
        or locator.source_ref != document_version_id
        or locator.page_number != review.page_number
        or locator.char_start != review.char_start
        or locator.char_end != review.char_end
        or page[review.char_start : review.char_end] != review.exact_quote
    ):
        return None
    return review


def reviewed_current_period_for_url(
    conn: sqlite3.Connection,
    *,
    issuer_id: str,
    ticker: str,
    source_url: str,
    observed_through: datetime,
) -> CurrentReportedPeriodReview | None:
    """Resolve one exact publisher URL; conflicting versions remain unqualified."""
    rows = conn.execute(
        "SELECT DISTINCT version.document_version_id,version.blob_sha256,version.recorded_at "
        "FROM evidence_document_versions version "
        "JOIN evidence_source_observations source ON source.observation_id=version.observation_id "
        "WHERE version.issuer_id=? AND version.ticker=? AND source.source_url=? "
        "AND version.document_type IN ('ir_document','earnings_release') "
        "AND version.recorded_at<=? AND source.retrieved_at<=? "
        "ORDER BY version.recorded_at DESC",
        (issuer_id, ticker, source_url, observed_through, observed_through),
    ).fetchall()
    if not rows or sum(row[2] == rows[0][2] for row in rows) != 1:
        return None
    valid: list[CurrentReportedPeriodReview] = []
    for version_id, digest, _ in rows[:1]:
        node = conn.execute(
            "SELECT node.text FROM evidence_nodes node "
            "JOIN evidence_extraction_runs run ON run.extraction_run_id=node.extraction_run_id "
            "WHERE run.document_version_id=? AND run.extractor_name='ir-current-reported-period'",
            (version_id,),
        ).fetchall()
        if len(node) != 1:
            continue
        try:
            claim = CurrentReportedPeriodReview.model_validate_json(str(node[0][0]))
        except ValueError:
            continue
        if (
            claim.issuer_id == issuer_id
            and claim.ticker == ticker
            and claim.reviewed_at <= observed_through
            and claim.knowledge_cutoff <= observed_through
            and claim.observed_through <= observed_through
            and verified_current_reported_period_review(
                conn,
                document_version_id=str(version_id),
                source_sha256=str(digest),
                period_end=claim.period_end,
            )
            == claim
        ):
            try:
                profile = _alias_row(
                    conn,
                    "issuer_profile_revisions",
                    "profile_revision_id",
                    claim.profile_revision_id,
                )
                validate_fiscal_period_witness(
                    conn,
                    request=IssuerDocumentInventoryRequest(
                        ticker=ticker,
                        fiscal_year=claim.fiscal_year,
                        fiscal_quarter=claim.fiscal_quarter,
                        period_end=claim.period_end,
                        expected_documents=(
                            ExpectedIssuerDocument(
                                source_url=source_url, document_type="earnings_release"
                            ),
                        ),
                        fiscal_period_witness=FiscalPeriodWitness(
                            issuer_id=issuer_id,
                            profile_revision_id=claim.profile_revision_id,
                            fiscal_year_end=str(profile["fiscal_year_end"]),
                            document_version_id=str(version_id),
                            document_sha256=str(digest),
                            knowledge_cutoff=observed_through,
                            observed_through=observed_through,
                        ),
                    ),
                )
            except (IssuerDocumentInventoryError, ValueError, sqlite3.Error):
                continue
            valid.append(claim)
    return valid[0] if len(valid) == 1 else None


def persist_current_reported_period_review(
    conn: sqlite3.Connection, review: CurrentReportedPeriodReview
) -> str:
    """Append an exact analyst period judgment; changed replays fail atomically."""
    if conn.in_transaction:
        raise IssuerDocumentInventoryError("reported_period_writer_busy")
    review = CurrentReportedPeriodReview.model_validate(review.model_dump(mode="python"))
    version, raw = _review_pdf_bytes(conn, review.document_version_id)
    if (
        version["issuer_id"] != review.issuer_id
        or version["ticker"] != review.ticker
        or version["blob_sha256"] != review.source_sha256
        or version["document_type"] not in {"ir_document", "earnings_release"}
        or (
            version["period_end"] is not None
            and datetime.fromisoformat(str(version["period_end"])).date() != review.period_end
        )
    ):
        raise IssuerDocumentInventoryError("reported_period_document_identity_mismatch")
    from provenance.fulltext_backfill import project_pdf_page_texts

    try:
        page = project_pdf_page_texts(raw)[0]
    except Exception as exc:  # Native parser failures must not create a period witness.
        raise IssuerDocumentInventoryError("reported_period_native_text_unavailable") from exc
    if page[review.char_start : review.char_end] != review.exact_quote:
        raise IssuerDocumentInventoryError("reported_period_quote_mismatch")
    subject = _alias_row(
        conn,
        "recorded_subject_binding_revisions",
        "binding_revision_id",
        review.subject_binding_revision_id,
    )
    if (
        subject["issuer_id"] != review.issuer_id
        or subject["recorded_issuer_id"] != f"legacy-ticker:{review.ticker}"
        or subject["outcome"] != "selected"
        or subject["material_dissent"] != 0
    ):
        raise IssuerDocumentInventoryError("reported_period_subject_mismatch")
    latest_subject = conn.execute(
        "SELECT binding_revision_id FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id=? ORDER BY revision DESC LIMIT 1",
        (f"legacy-ticker:{review.ticker}",),
    ).fetchone()
    if latest_subject is None or latest_subject[0] != review.subject_binding_revision_id:
        raise IssuerDocumentInventoryError("reported_period_subject_superseded")
    profile = _alias_row(
        conn, "issuer_profile_revisions", "profile_revision_id", review.profile_revision_id
    )
    if (
        profile["issuer_id"] != review.issuer_id
        or profile["status"] != "active"
        or fiscal_quarter_end(
            fiscal_year=review.fiscal_year,
            fiscal_quarter=review.fiscal_quarter,
            fiscal_year_end=str(profile["fiscal_year_end"]),
        )
        != review.period_end
    ):
        raise IssuerDocumentInventoryError("reported_period_profile_mismatch")
    run_id = "ir-current-period-run:" + _sha256_text(review.document_version_id)
    node_id = "ir-current-period-review:" + _sha256_text(review.document_version_id)
    payload = _canonical_json(review.model_dump(mode="json"))
    config = _sha256_text("ir-current-reported-period@1")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ExtractionRun(
                extraction_run_id=run_id,
                idempotency_key=run_id,
                document_version_id=review.document_version_id,
                input_sha256=review.source_sha256,
                extractor_name="ir-current-reported-period",
                extractor_config_sha256=config,
                extractor_code_version="ir-current-reported-period@1",
                output_sha256=_sha256_text(payload),
                started_at=review.reviewed_at,
                completed_at=review.reviewed_at,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id=node_id,
                evidence_key=node_id,
                revision=1,
                extraction_run_id=run_id,
                node_kind="claim",
                text=payload,
                locator=EvidenceLocator(
                    source_ref=review.document_version_id,
                    page_number=review.page_number,
                    char_start=review.char_start,
                    char_end=review.char_end,
                ),
                recorded_at=review.reviewed_at,
            )
        )
        if (
            verified_current_reported_period_review(
                conn,
                document_version_id=review.document_version_id,
                source_sha256=review.source_sha256,
                period_end=review.period_end,
            )
            != review
        ):
            raise IssuerDocumentInventoryError("reported_period_review_replay_mismatch")
        witness = FiscalPeriodWitness(
            issuer_id=review.issuer_id,
            profile_revision_id=review.profile_revision_id,
            fiscal_year_end=str(profile["fiscal_year_end"]),
            document_version_id=review.document_version_id,
            document_sha256=review.source_sha256,
            knowledge_cutoff=review.knowledge_cutoff,
            observed_through=review.observed_through,
        )
        source = _alias_row(
            conn, "evidence_source_observations", "observation_id", str(version["observation_id"])
        )
        _alias_clock(source["retrieved_at"], review.reviewed_at)
        request = IssuerDocumentInventoryRequest(
            ticker=review.ticker,
            fiscal_year=review.fiscal_year,
            fiscal_quarter=review.fiscal_quarter,
            period_end=review.period_end,
            expected_documents=(
                ExpectedIssuerDocument(
                    source_url=str(source["source_url"]), document_type="earnings_release"
                ),
            ),
            fiscal_period_witness=witness,
        )
        validate_fiscal_period_witness(conn, request=request)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return node_id


def _database_binding(conn: sqlite3.Connection, bundle: _DatabaseBundle) -> DatabaseBinding:
    try:
        columns = conn.execute("PRAGMA table_info(documents)").fetchall()
        if not _REQUIRED_DOCUMENT_COLUMNS.issubset({str(row["name"]) for row in columns}):
            raise IssuerDocumentInventoryError("schema_drift")
        revisions = conn.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
    except IssuerDocumentInventoryError:
        raise
    except sqlite3.Error as exc:
        raise IssuerDocumentInventoryError("schema_drift") from exc
    if len(revisions) != 1 or not str(revisions[0][0]).strip():
        raise IssuerDocumentInventoryError("schema_drift")
    main_sha256 = bundle.main.sha256
    main_byte_size = bundle.main.byte_size
    if main_sha256 is None or main_byte_size is None:
        raise IssuerDocumentInventoryError("database_unreadable")
    shape = [
        {
            "cid": int(row["cid"]),
            "name": str(row["name"]),
            "type": str(row["type"]),
            "notnull": int(row["notnull"]),
            "default": None if row["dflt_value"] is None else str(row["dflt_value"]),
            "pk": int(row["pk"]),
        }
        for row in columns
    ]
    return DatabaseBinding(
        sha256=main_sha256,
        byte_size=main_byte_size,
        alembic_revision=str(revisions[0][0]),
        documents_schema_sha256=_sha256_text(_canonical_json(shape)),
        storage_members=bundle.members,
        storage_bundle_sha256=bundle.bundle_sha256,
    )


def _safe_relative_file(value: object, root: Path) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise IssuerDocumentInventoryError("unsafe_local_path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise IssuerDocumentInventoryError("unsafe_local_path")
    candidate = root.joinpath(*relative.parts)
    component = root
    for part in relative.parts:
        component /= part
        _reject_link_or_reparse_component(component)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise IssuerDocumentInventoryError("missing_local_file") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise IssuerDocumentInventoryError("unsafe_local_path")
    return resolved


def _reject_link_or_reparse_component(path: Path) -> None:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise IssuerDocumentInventoryError("missing_local_file") from exc
    reparse = getattr(file_stat, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    if path.is_symlink() or reparse:
        raise IssuerDocumentInventoryError("unsafe_local_path")


def _database_bundle(path: Path) -> _DatabaseBundle:
    return _DatabaseBundle(
        members=(
            _storage_member(path, name="main", required=True),
            _storage_member(Path(f"{path}-wal"), name="wal", required=False),
        )
    )


def _storage_member(
    path: Path,
    *,
    name: Literal["main", "wal"],
    required: bool,
) -> DatabaseStorageMember:
    try:
        before = path.stat()
    except FileNotFoundError:
        if not required:
            return DatabaseStorageMember(name=name, present=False)
        raise IssuerDocumentInventoryError("database_unreadable") from None
    except OSError as exc:
        raise IssuerDocumentInventoryError("database_unreadable") from exc
    try:
        body = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise IssuerDocumentInventoryError("database_unreadable") from exc
    before_token = _storage_token(before)
    after_token = _storage_token(after)
    if before_token != after_token:
        raise IssuerDocumentInventoryError("database_changed")
    return DatabaseStorageMember(
        name=name,
        present=True,
        sha256=hashlib.sha256(body).hexdigest(),
        byte_size=len(body),
        file_token_sha256=_sha256_text(_canonical_json(before_token)),
    )


def _storage_token(stat_result: os.stat_result) -> dict[str, int]:
    return {
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "mtime_ns": int(stat_result.st_mtime_ns),
        "size": int(stat_result.st_size),
    }


def _document_period(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None


def _parse_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return None if parsed.tzinfo is None else parsed.astimezone(UTC)


def _quarter_end(year: int, quarter: int) -> date:
    if not 1900 <= year <= 2200 or not 1 <= quarter <= 4:
        raise ValueError("invalid year or quarter")
    month = quarter * 3
    return date(
        year, month, (date(year + int(month == 12), month % 12 + 1, 1) - date.resolution).day
    )


def _verifier_code_sha256() -> str:
    project_root = Path(__file__).resolve().parents[2]
    return verifier_source_artifact_sha256(
        {
            "execution/capture_issuer_document_inventory.py": project_root
            / "execution"
            / "capture_issuer_document_inventory.py",
            "src/pipeline/issuer_document_inventory.py": Path(__file__),
        }
    )


__all__ = [
    "ExpectedIssuerDocument",
    "ExpectedIssuerDocumentAlias",
    "IssuerDocumentAliasBinding",
    "IssuerDocumentAliasInventoryReceipt",
    "IssuerDocumentAliasInventoryRecord",
    "IssuerDocumentAliasInventoryRequest",
    "IssuerDocumentInventoryError",
    "IssuerDocumentInventoryReceipt",
    "IssuerDocumentInventoryReceiptAny",
    "IssuerDocumentInventoryRecord",
    "IssuerDocumentInventoryRequest",
    "IssuerDocumentInventoryRequestAny",
    "build_issuer_document_inventory",
    "inspect_issuer_document_alias",
    "load_issuer_document_inventory_receipt",
    "load_issuer_document_inventory_request",
]
