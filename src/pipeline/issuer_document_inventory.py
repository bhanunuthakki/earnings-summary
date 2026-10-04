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
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from provenance.verifier_identity import verifier_source_artifact_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
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


class IssuerDocumentInventoryRequest(_ClosedModel):
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
        if self.period_end != _quarter_end(self.fiscal_year, self.fiscal_quarter):
            raise ValueError("period_end must be the requested calendar-quarter end")
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


class IssuerDocumentAliasInventoryRequest(_ClosedModel):
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
            expected_documents=tuple(
                ExpectedIssuerDocument(source_url=item.source_url, document_type=item.document_type)
                for item in self.expected_documents
            ),
        )
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


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
