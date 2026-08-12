"""Immutable snapshot capture of SEC CompanyFacts responses.

One CompanyFacts response contains many filing accessions and changes whenever
the SEC adds or corrects any of them.  The raw response is stored once by
content hash. A logical document version and one legacy snapshot document
represent that aggregate response. Filing accessions remain locators inside
the snapshot; they are never manufactured as native filing documents.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.evidence_links import (
    BlobLocationObservation,
    DocumentObservationLink,
    EvidenceLinkLedger,
)
from provenance.legacy_document_evidence import (
    LegacyDocumentEvidenceBindingLedger,
    LegacyDocumentEvidenceBindingRevision,
    LegacyDocumentScopeLocator,
)

_COLLECTOR_VERSION = "sec-companyfacts-capture@1"
_DOCUMENT_EXTRACTOR = "sec-companyfacts-document-anchor"
_DOCUMENT_CONFIG_SHA = hashlib.sha256(b"sec-companyfacts-document-anchor@1").hexdigest()
_ACCESSION_PATTERN = r"^\d{10}-\d{2}-\d{6}$"
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_FORM_TYPES = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "8-K",
        "8-K/A",
        "6-K",
        "6-K/A",
    }
)


class CompanyFactsContractError(ValueError):
    """The response is not the complete CompanyFacts contract requested."""

    def __init__(self, message: str, *, raw_response_path: Path | None = None) -> None:
        super().__init__(message)
        self.raw_response_path = raw_response_path


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompanyFactEntry(_ClosedModel):
    end: str = Field(pattern=_DATE_PATTERN)
    val: int | float
    accn: str = Field(pattern=_ACCESSION_PATTERN)
    fy: int | None
    fp: str | None
    form: str = Field(min_length=1, max_length=32)
    filed: str = Field(pattern=_DATE_PATTERN)
    start: str | None = Field(default=None, pattern=_DATE_PATTERN)
    frame: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("val")
    @classmethod
    def _numeric_not_boolean(cls, value: int | float) -> int | float:
        if isinstance(value, bool):
            raise ValueError("CompanyFacts values must be numeric, not boolean")
        return value


class CompanyFactConcept(_ClosedModel):
    label: str | None
    description: str | None
    units: dict[str, tuple[CompanyFactEntry, ...]]


class CompanyFactsPayload(_ClosedModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    cik: int = Field(gt=0, le=9_999_999_999)
    entity_name: str = Field(min_length=1, alias="entityName")
    facts: dict[str, dict[str, CompanyFactConcept]]


class SecCompanyFactsCaptureRequest(_ClosedModel):
    ticker: str = Field(min_length=1, max_length=32)
    normalized_cik: str = Field(pattern=r"^\d{10}$")
    issuer_id: str = Field(min_length=1, max_length=128)
    source_url: str = Field(min_length=1)
    raw_body: bytes = Field(min_length=1)
    payload: CompanyFactsPayload
    snapshot_document_id: int = Field(gt=0)
    blob_root: Path
    observed_at: datetime
    retrieved_at: datetime

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def _validate_capture(self) -> Self:
        if str(self.payload.cik).zfill(10) != self.normalized_cik:
            raise ValueError("CompanyFacts payload CIK conflicts with requested CIK")
        if _timeline(self.retrieved_at) < _timeline(self.observed_at):
            raise ValueError("retrieved_at must not precede observed_at")
        return self


class SecCompanyFactsCaptureResult(_ClosedModel):
    blob_sha256: str
    document_version_id: str
    document_version_created: bool
    bindings_created: int = Field(ge=0)
    bindings_unchanged: int = Field(ge=0)
    records_created: int = Field(ge=0)
    records_replayed: int = Field(ge=0)


def _timeline(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_companyfacts_body(raw_body: bytes, *, expected_cik: str) -> CompanyFactsPayload:
    """Validate the complete JSON response and its requested SEC registrant."""

    try:
        payload = CompanyFactsPayload.model_validate_json(raw_body)
    except ValueError as exc:
        raise CompanyFactsContractError("SEC CompanyFacts body violates the closed schema") from exc
    if str(payload.cik).zfill(10) != expected_cik:
        raise CompanyFactsContractError("SEC CompanyFacts body CIK conflicts with request")
    return payload


def capture_sec_companyfacts(
    conn: sqlite3.Connection,
    request: SecCompanyFactsCaptureRequest,
) -> SecCompanyFactsCaptureResult:
    """Persist one complete evidence chain without committing the caller transaction."""

    reparsed = parse_companyfacts_body(
        request.raw_body,
        expected_cik=request.normalized_cik,
    )
    if reparsed != request.payload:
        raise ValueError("raw CompanyFacts bytes conflict with validated payload")
    digest = hashlib.sha256(request.raw_body).hexdigest()
    _validate_snapshot_document(conn, request, digest)
    storage_path = request.blob_root.resolve() / digest[:2] / f"{digest}.json"
    _write_verified_blob(storage_path, request.raw_body, digest)
    storage_uri = storage_path.as_uri()
    ledger = EvidenceLedger(conn)
    link_ledger = EvidenceLinkLedger(conn)
    binding_ledger = LegacyDocumentEvidenceBindingLedger(conn)
    created = 0
    replayed = 0

    blob = _content_blob(conn, request, digest=digest, storage_uri=storage_uri)
    created, replayed = _account(ledger.persist(blob).created, created, replayed)
    observation = _source_observation(request, digest)
    created, replayed = _account(ledger.persist(observation).created, created, replayed)
    document, document_created = _document_version(conn, request, observation, digest)
    created, replayed = _account(ledger.persist(document).created, created, replayed)
    location_created = _ensure_location(
        conn,
        link_ledger,
        digest=digest,
        storage_uri=storage_uri,
        byte_size=len(request.raw_body),
        verified_at=request.retrieved_at,
    )
    created, replayed = _account(location_created, created, replayed)
    link = _document_observation_link(
        request,
        observation,
        document,
        link_kind=(
            "primary" if document.observation_id == observation.observation_id else "retrieval"
        ),
    )
    created, replayed = _account(link_ledger.persist_link(link).created, created, replayed)

    document_anchor_id, document_anchor_created = _ensure_document_anchor(
        conn,
        ledger,
        request=request,
        document=document,
        digest=digest,
    )
    created += document_anchor_created
    replayed += 2 - document_anchor_created

    binding_identity = _stable_digest(
        str(request.snapshot_document_id),
        document.document_version_id,
        digest,
    )
    binding = binding_ledger.persist(
        LegacyDocumentEvidenceBindingRevision(
            binding_revision_id=f"sec-companyfacts-binding:{binding_identity}",
            idempotency_key=f"sec-companyfacts-binding:{binding_identity}",
            legacy_document_id=request.snapshot_document_id,
            revision=1,
            document_version_id=document.document_version_id,
            evidence_node_id=document_anchor_id,
            scope_locator=LegacyDocumentScopeLocator(source_ref=request.source_url),
            scope_content_sha256=digest,
            effective_at=request.retrieved_at,
            knowledge_at=request.retrieved_at,
            recorded_at=request.retrieved_at,
            supersedes_binding_revision_id=None,
        )
    )
    created, replayed = _account(binding.created, created, replayed)

    return SecCompanyFactsCaptureResult(
        blob_sha256=digest,
        document_version_id=document.document_version_id,
        document_version_created=document_created,
        bindings_created=int(binding.created),
        bindings_unchanged=int(not binding.created),
        records_created=created,
        records_replayed=replayed,
    )


def _content_blob(
    conn: sqlite3.Connection,
    request: SecCompanyFactsCaptureRequest,
    *,
    digest: str,
    storage_uri: str,
) -> ContentBlob:
    existing = conn.execute(
        "SELECT byte_size, media_type, storage_uri, recorded_at "
        "FROM evidence_content_blobs WHERE sha256 = ?",
        (digest,),
    ).fetchone()
    if existing is None:
        return ContentBlob(
            sha256=digest,
            byte_size=len(request.raw_body),
            media_type="application/json",
            storage_uri=storage_uri,
            recorded_at=request.retrieved_at,
        )
    return ContentBlob(
        sha256=digest,
        byte_size=int(existing[0]),
        media_type=str(existing[1]),
        storage_uri=str(existing[2]),
        recorded_at=_datetime(existing[3]),
    )


def _source_observation(
    request: SecCompanyFactsCaptureRequest,
    digest: str,
) -> SourceObservation:
    identity = _stable_digest(
        request.normalized_cik,
        digest,
        _timeline(request.observed_at).isoformat(),
        _timeline(request.retrieved_at).isoformat(),
    )
    config_sha = _stable_digest(request.source_url, request.normalized_cik)
    return SourceObservation(
        observation_id=f"sec-companyfacts-observation:{identity}",
        idempotency_key=f"sec-companyfacts-observation:{identity}",
        source_kind="sec_companyfacts",
        source_url=request.source_url,
        blob_sha256=digest,
        source_published_at=None,
        filing_at=None,
        accepted_at=None,
        observed_at=request.observed_at,
        retrieved_at=request.retrieved_at,
        retrieval_config_sha256=config_sha,
        collector_code_version=_COLLECTOR_VERSION,
    )


def _document_version(
    conn: sqlite3.Connection,
    request: SecCompanyFactsCaptureRequest,
    observation: SourceObservation,
    digest: str,
) -> tuple[DocumentVersion, bool]:
    document_key = f"{request.issuer_id}:sec-companyfacts"
    exact = conn.execute(
        "SELECT document_version_id, version_sequence, observation_id, "
        "replaces_document_version_id, recorded_at "
        "FROM evidence_document_versions WHERE document_key = ? AND blob_sha256 = ?",
        (document_key, digest),
    ).fetchone()
    if exact is not None:
        return (
            DocumentVersion(
                document_version_id=str(exact[0]),
                document_key=document_key,
                version_sequence=int(exact[1]),
                observation_id=str(exact[2]),
                blob_sha256=digest,
                issuer_id=request.issuer_id,
                ticker=request.ticker,
                document_type="companyfacts_snapshot",
                form_type="SEC-COMPANYFACTS",
                accession_number=None,
                exhibit_id=None,
                period_start=None,
                period_end=None,
                as_of_at=_datetime(exact[4]),
                language="en",
                replaces_document_version_id=(None if exact[3] is None else str(exact[3])),
                legacy_document_id=None,
                recorded_at=_datetime(exact[4]),
            ),
            False,
        )
    current = conn.execute(
        "SELECT document_version_id, version_sequence "
        "FROM evidence_document_versions WHERE document_key = ? "
        "ORDER BY version_sequence DESC LIMIT 1",
        (document_key,),
    ).fetchone()
    sequence = 1 if current is None else int(current[1]) + 1
    replaces = None if current is None else str(current[0])
    identity = _stable_digest(document_key, digest)
    return (
        DocumentVersion(
            document_version_id=f"sec-companyfacts-document:{identity}",
            document_key=document_key,
            version_sequence=sequence,
            observation_id=observation.observation_id,
            blob_sha256=digest,
            issuer_id=request.issuer_id,
            ticker=request.ticker,
            document_type="companyfacts_snapshot",
            form_type="SEC-COMPANYFACTS",
            accession_number=None,
            exhibit_id=None,
            period_start=None,
            period_end=None,
            as_of_at=request.retrieved_at,
            language="en",
            replaces_document_version_id=replaces,
            legacy_document_id=None,
            recorded_at=request.retrieved_at,
        ),
        True,
    )


def _document_observation_link(
    request: SecCompanyFactsCaptureRequest,
    observation: SourceObservation,
    document: DocumentVersion,
    *,
    link_kind: Literal["primary", "retrieval"],
) -> DocumentObservationLink:
    identity = _stable_digest(
        document.document_version_id,
        observation.observation_id,
        link_kind,
    )
    return DocumentObservationLink(
        link_id=f"sec-companyfacts-link:{identity}",
        document_version_id=document.document_version_id,
        observation_id=observation.observation_id,
        link_kind=link_kind,
        linked_at=request.retrieved_at,
    )


def _ensure_document_anchor(
    conn: sqlite3.Connection,
    ledger: EvidenceLedger,
    *,
    request: SecCompanyFactsCaptureRequest,
    document: DocumentVersion,
    digest: str,
) -> tuple[str, int]:
    existing = conn.execute(
        "SELECT node.node_id FROM evidence_nodes AS node "
        "JOIN evidence_extraction_runs AS run USING (extraction_run_id) "
        "WHERE run.document_version_id = ? AND node.node_kind = 'document' LIMIT 1",
        (document.document_version_id,),
    ).fetchone()
    if existing is not None:
        return str(existing[0]), 0
    identity = _stable_digest(document.document_version_id, _DOCUMENT_EXTRACTOR)
    node_text = (
        f"SEC CompanyFacts snapshot for {request.ticker} "
        f"(CIK {request.normalized_cik}, SHA-256 {digest})."
    )
    output_sha = hashlib.sha256(node_text.encode()).hexdigest()
    run = ExtractionRun(
        extraction_run_id=f"sec-companyfacts-run:{identity}",
        idempotency_key=f"sec-companyfacts-run:{identity}",
        document_version_id=document.document_version_id,
        input_sha256=digest,
        extractor_name=_DOCUMENT_EXTRACTOR,
        extractor_config_sha256=_DOCUMENT_CONFIG_SHA,
        extractor_code_version=_COLLECTOR_VERSION,
        output_sha256=output_sha,
        started_at=request.retrieved_at,
        completed_at=request.retrieved_at,
        outcome="succeeded",
    )
    ledger.persist(run)
    node = EvidenceNode(
        node_id=f"sec-companyfacts-node:{identity}",
        evidence_key=f"{document.document_version_id}:document",
        revision=1,
        extraction_run_id=run.extraction_run_id,
        parent_node_id=None,
        supersedes_node_id=None,
        node_kind="document",
        text=node_text,
        locator=EvidenceLocator(source_ref=request.source_url),
        recorded_at=request.retrieved_at,
    )
    ledger.persist(node)
    return node.node_id, 2


def _validate_snapshot_document(
    conn: sqlite3.Connection,
    request: SecCompanyFactsCaptureRequest,
    digest: str,
) -> None:
    row = conn.execute(
        "SELECT ticker, source_type, doc_type, file_path, sha256, accession_number "
        "FROM documents WHERE id = ?",
        (request.snapshot_document_id,),
    ).fetchone()
    if row is None:
        raise ValueError("CompanyFacts snapshot document does not exist")
    expected = (
        request.ticker,
        "sec_xbrl",
        "sec_companyfacts_snapshot",
        str((request.blob_root.resolve() / digest[:2] / f"{digest}.json").resolve()),
        digest,
        None,
    )
    if tuple(row) != expected:
        raise ValueError("CompanyFacts snapshot document conflicts with exact response bytes")


def _accession_scopes(payload: CompanyFactsPayload) -> dict[str, bytes]:
    rows: dict[str, list[dict[str, object]]] = {}
    for namespace in sorted(payload.facts):
        concepts = payload.facts[namespace]
        for concept_name in sorted(concepts):
            concept = concepts[concept_name]
            for unit_code in sorted(concept.units):
                entries = concept.units[unit_code]
                for entry in entries:
                    if entry.form not in _FORM_TYPES:
                        continue
                    rows.setdefault(entry.accn, []).append(
                        {
                            "namespace": namespace,
                            "concept": concept_name,
                            "unit": unit_code,
                            "entry": entry.model_dump(mode="json", exclude_none=False),
                        }
                    )
    return {
        accession: json.dumps(
            sorted(
                entries,
                key=lambda item: json.dumps(
                    item,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        for accession, entries in rows.items()
    }


def supported_companyfacts_accessions(
    payload: CompanyFactsPayload,
) -> frozenset[str]:
    """Return the accession identities supported by immutable capture."""

    return frozenset(_accession_scopes(payload))


def _ensure_location(
    conn: sqlite3.Connection,
    ledger: EvidenceLinkLedger,
    *,
    digest: str,
    storage_uri: str,
    byte_size: int,
    verified_at: datetime,
) -> bool:
    current = conn.execute(
        "SELECT location_observation_id, availability_state, location_sequence "
        "FROM v_evidence_blob_locations_current "
        "WHERE blob_sha256 = ? AND storage_uri = ?",
        (digest, storage_uri),
    ).fetchone()
    if current is not None and str(current[1]) == "present":
        return False
    sequence = 1 if current is None else int(current[2]) + 1
    supersedes = None if current is None else str(current[0])
    identity = _stable_digest(digest, storage_uri, str(sequence), "present")
    return ledger.persist_location(
        BlobLocationObservation(
            location_observation_id=f"sec-companyfacts-location:{identity}",
            idempotency_key=f"sec-companyfacts-location:{identity}",
            blob_sha256=digest,
            storage_uri=storage_uri,
            location_kind="local",
            availability_state="present",
            location_sequence=sequence,
            verified_at=verified_at,
            verified_byte_size=byte_size,
            verified_sha256=digest,
            supersedes_location_observation_id=supersedes,
            recorded_at=verified_at,
        )
    ).created


def _write_verified_blob(path: Path, body: bytes, digest: str) -> None:
    if path.exists():
        existing = path.read_bytes()
        if hashlib.sha256(existing).hexdigest() != digest:
            raise ValueError("existing CompanyFacts blob path contains different bytes")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{digest}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary = Path(temporary_name)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            raise ValueError("CompanyFacts blob verification failed before publication")
        os.replace(temporary, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _stable_digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return _timeline(parsed)


def _account(created_now: bool, created: int, replayed: int) -> tuple[int, int]:
    return (created + 1, replayed) if created_now else (created, replayed + 1)
