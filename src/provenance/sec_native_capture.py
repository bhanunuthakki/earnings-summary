"""Bounded evidence-native capture for authoritative SEC expected documents.

This lane deliberately does not download IR documents.  A sealed SEC inventory
is itself the authority for accession, filename, form, and archive URL.  IR
capture additionally needs publisher-specific robots and render authorization,
which cannot be inferred safely from an ``expected_documents`` row.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self
from urllib.parse import unquote, urlparse

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from log_redact import redact
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.evidence_links import (
    BlobLocationObservation,
    DocumentObservationLink,
    EvidenceLinkLedger,
)
from provenance.source_coverage import CoverageAssessment, SourceCoverageLedger

_COLLECTOR_VERSION = "sec-native-capture@1"
_POLICY_NAME = "sealed-sec-expected-document-capture"
_POLICY_VERSION = "1"
_SEC_ARCHIVE_HOSTS = frozenset({"sec.gov", "www.sec.gov"})
_SEC_ARCHIVE_PATH_PREFIX = "/Archives/edgar/data/"
_ACCESSION_PATTERN = r"^\d{10}-\d{2}-\d{6}$"
_CONFIG_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "archive_hosts": sorted(_SEC_ARCHIVE_HOSTS),
            "archive_path_prefix": _SEC_ARCHIVE_PATH_PREFIX,
            "collector": _COLLECTOR_VERSION,
            "identity_policy": "sealed-authoritative-exact",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()

FetchOutcome = Literal[
    "fetched",
    "transient_deferred",
    "contract_failure",
    "identity_rejected",
    "auth_hard_stop",
]


class SecNativeCaptureError(RuntimeError):
    """Base error for the evidence-native SEC capture boundary."""


class SecNativeCaptureHardStopError(SecNativeCaptureError):
    """Authentication or SEC fair-access rejection; retrying unchanged is unsafe."""


class _ResponseLike(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def iter_content(self, chunk_size: int) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class SessionLike(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
    ) -> _ResponseLike: ...


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SecNativeCaptureRequest(_ClosedModel):
    """Validated authority and operational controls for one bounded capture."""

    inventory_keys: tuple[str, ...] = Field(min_length=1)
    checkpoint_root: Path
    blob_root: Path
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    user_agent: str = Field(min_length=8, max_length=512)
    apply: bool = False
    batch_size: int = Field(default=25, ge=1, le=250)
    max_document_bytes: int = Field(default=100_000_000, ge=1, le=1_000_000_000)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    read_timeout_seconds: int = Field(default=60, ge=1, le=600)
    minimum_request_interval_seconds: float = Field(default=0.25, ge=0.0, le=10.0)

    @field_validator("inventory_keys")
    @classmethod
    def _unique_inventory_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("inventory keys must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("inventory keys must be unique")
        return normalized


class ExpectedSecDocument(_ClosedModel):
    expected_document_id: str
    snapshot_id: str
    inventory_key: str
    expected_document_key: str
    issuer_id: str
    ticker: str | None
    document_type: str
    form_type: str
    accession_number: str = Field(pattern=_ACCESSION_PATTERN)
    source_url: str
    primary_document: str
    period_start: datetime | None
    period_end: datetime | None
    filing_at: datetime | None
    current_coverage_status: str | None

    @model_validator(mode="after")
    def _validate_authoritative_identity(self) -> Self:
        parsed = urlparse(self.source_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _SEC_ARCHIVE_HOSTS
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith(_SEC_ARCHIVE_PATH_PREFIX)
        ):
            raise ValueError("source URL is not an uncredentialed SEC archive URL")
        path_parts = tuple(part for part in parsed.path.split("/") if part)
        accession_compact = self.accession_number.replace("-", "")
        if len(path_parts) < 6 or path_parts[-2] != accession_compact:
            raise ValueError("SEC archive URL does not match the declared accession")
        if unquote(path_parts[-1]) != self.primary_document:
            raise ValueError("SEC archive URL does not match the declared primary document")
        if "/" in self.primary_document or "\\" in self.primary_document:
            raise ValueError("primary document must be a single filename")
        return self


class CaptureCheckpointEntry(_ClosedModel):
    expected_document_id: str
    snapshot_id: str
    source_url: str
    outcome: FetchOutcome
    observed_at: datetime
    retrieved_at: datetime
    response_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    byte_size: int | None = Field(default=None, ge=0)
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    document_version_id: str | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        has_bytes = (
            self.response_sha256 is not None
            and self.byte_size is not None
            and self.media_type is not None
        )
        if (self.outcome == "fetched") != has_bytes:
            raise ValueError("only fetched checkpoint entries may identify response bytes")
        return self


class CaptureCheckpoint(_ClosedModel):
    task_id: str
    entries: tuple[CaptureCheckpointEntry, ...] = ()
    updated_at: datetime

    @model_validator(mode="after")
    def _unique_entries(self) -> Self:
        identities = [item.expected_document_id for item in self.entries]
        if len(identities) != len(set(identities)):
            raise ValueError("checkpoint expected-document identities must be unique")
        return self


class CaptureItemResult(_ClosedModel):
    expected_document_id: str
    expected_document_key: str
    outcome: FetchOutcome
    reason_code: str
    document_version_id: str | None = None
    records_created: int = Field(default=0, ge=0)
    records_replayed: int = Field(default=0, ge=0)


class SecNativeCaptureResult(_ClosedModel):
    task_id: str
    mode: Literal["dry_run", "apply"]
    inventory_keys: tuple[str, ...]
    considered: int = Field(ge=0)
    fetched: int = Field(ge=0)
    deferred: int = Field(ge=0)
    failed: int = Field(ge=0)
    records_created: int = Field(ge=0)
    records_replayed: int = Field(ge=0)
    has_more: bool
    sec_only_boundary: str
    items: tuple[CaptureItemResult, ...]


def load_expected_sec_documents(
    conn: sqlite3.Connection,
    *,
    inventory_keys: tuple[str, ...],
    limit: int,
) -> tuple[tuple[ExpectedSecDocument, ...], bool]:
    """Load only current, completely sealed, authoritative SEC expectations."""

    placeholders = ", ".join("?" for _ in inventory_keys)
    inventory_rows = conn.execute(
        "SELECT inventory_key, source_kind FROM v_source_inventory_sealed_complete "  # nosec B608 -- trusted internal SQL shape; values remain bound
        f"WHERE inventory_key IN ({placeholders}) ORDER BY inventory_key",
        inventory_keys,
    ).fetchall()
    found = {str(row[0]): str(row[1]) for row in inventory_rows}
    missing = sorted(set(inventory_keys) - set(found))
    if missing:
        raise SecNativeCaptureError(
            f"inventory keys are not current complete seals: {', '.join(missing)}"
        )
    wrong_kind = sorted(
        key for key, source_kind in found.items() if source_kind != "sec_submissions"
    )
    if wrong_kind:
        raise SecNativeCaptureError(
            "SEC-native capture refuses non-SEC inventories: " + ", ".join(wrong_kind)
        )

    rows = conn.execute(
        "SELECT expected.expected_document_id, expected.snapshot_id, inventory.inventory_key, "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "expected.expected_document_key, expected.issuer_id, expected.ticker, "
        "expected.document_type, expected.form_type, expected.accession_number, "
        "expected.source_url, expected.primary_document, expected.period_start, "
        "expected.period_end, expected.filing_at, coverage.coverage_status "
        "FROM v_expected_documents_current AS expected "
        "JOIN v_source_inventory_sealed_complete AS inventory "
        "ON inventory.snapshot_id = expected.snapshot_id "
        "LEFT JOIN v_source_coverage_current AS coverage "
        "ON coverage.expected_document_id = expected.expected_document_id "
        f"WHERE inventory.inventory_key IN ({placeholders}) "
        "AND expected.source_kind = 'sec_filing' "
        "AND expected.expectation_basis = 'authoritative' "
        "AND (coverage.coverage_status IS NULL OR coverage.coverage_status "
        "NOT IN ('captured', 'extracted', 'indexed')) "
        "ORDER BY inventory.inventory_key, expected.expected_document_key "
        "LIMIT ?",
        (*inventory_keys, limit + 1),
    ).fetchall()
    candidates: list[ExpectedSecDocument] = []
    for row in rows[:limit]:
        try:
            candidates.append(
                ExpectedSecDocument(
                    expected_document_id=str(row[0]),
                    snapshot_id=str(row[1]),
                    inventory_key=str(row[2]),
                    expected_document_key=str(row[3]),
                    issuer_id=str(row[4]),
                    ticker=None if row[5] is None else str(row[5]),
                    document_type=str(row[6]),
                    form_type=_required_identity(row[7], "form_type"),
                    accession_number=_required_identity(row[8], "accession_number"),
                    source_url=_required_identity(row[9], "source_url"),
                    primary_document=_required_identity(row[10], "primary_document"),
                    period_start=_optional_datetime(row[11]),
                    period_end=_optional_datetime(row[12]),
                    filing_at=_optional_datetime(row[13]),
                    current_coverage_status=None if row[14] is None else str(row[14]),
                )
            )
        except ValueError as exc:
            raise SecNativeCaptureError(
                f"sealed expected document {row[0]!s} has rejected identity: {redact(exc)}"
            ) from None
    return tuple(candidates), len(rows) > limit


def capture_expected_sec_documents(
    conn: sqlite3.Connection,
    request: SecNativeCaptureRequest,
    *,
    session: SessionLike,
) -> SecNativeCaptureResult:
    """Fetch and optionally persist one bounded, all-or-nothing SEC batch."""

    candidates, has_more = load_expected_sec_documents(
        conn,
        inventory_keys=request.inventory_keys,
        limit=request.batch_size,
    )
    run_root = request.checkpoint_root / request.task_id
    checkpoint_path = run_root / "state.json"
    checkpoint = _load_checkpoint(checkpoint_path, request.task_id)
    checkpoint_entries = {item.expected_document_id: item for item in checkpoint.entries}
    items: list[tuple[ExpectedSecDocument, CaptureCheckpointEntry]] = []
    network_requests_made = 0

    for candidate in candidates:
        prior = checkpoint_entries.get(candidate.expected_document_id)
        if prior is not None:
            _validate_checkpoint_identity(candidate, prior)
        if prior is None or prior.outcome in {
            "transient_deferred",
            "auth_hard_stop",
        }:
            if network_requests_made:
                time.sleep(request.minimum_request_interval_seconds)
            entry = _fetch_one(session, candidate, request, run_root)
            network_requests_made += 1
            checkpoint_entries[candidate.expected_document_id] = entry
            checkpoint = CaptureCheckpoint(
                task_id=request.task_id,
                entries=tuple(
                    sorted(checkpoint_entries.values(), key=lambda item: item.expected_document_id)
                ),
                updated_at=datetime.now(UTC),
            )
            _write_checkpoint(checkpoint_path, checkpoint)
        else:
            entry = prior
        items.append((candidate, entry))
        if entry.outcome == "auth_hard_stop":
            raise SecNativeCaptureHardStopError(
                "SEC authorization hard stop recorded; verify the declared User-Agent"
            )

    if request.apply and items:
        for _, entry in items:
            if entry.outcome == "fetched":
                _promote_raw_bytes(run_root, request.blob_root, entry)
        item_results = _persist_batch(conn, request, items)
        by_id = {item.expected_document_id: item for item in item_results}
        checkpoint_entries = {
            identity: (
                entry.model_copy(
                    update={"document_version_id": by_id[identity].document_version_id}
                )
                if identity in by_id
                else entry
            )
            for identity, entry in checkpoint_entries.items()
        }
        _write_checkpoint(
            checkpoint_path,
            CaptureCheckpoint(
                task_id=request.task_id,
                entries=tuple(
                    sorted(checkpoint_entries.values(), key=lambda item: item.expected_document_id)
                ),
                updated_at=datetime.now(UTC),
            ),
        )
    else:
        item_results = tuple(
            CaptureItemResult(
                expected_document_id=candidate.expected_document_id,
                expected_document_key=candidate.expected_document_key,
                outcome=entry.outcome,
                reason_code=entry.reason_code,
            )
            for candidate, entry in items
        )

    return SecNativeCaptureResult(
        task_id=request.task_id,
        mode="apply" if request.apply else "dry_run",
        inventory_keys=request.inventory_keys,
        considered=len(item_results),
        fetched=sum(item.outcome == "fetched" for item in item_results),
        deferred=sum(item.outcome == "transient_deferred" for item in item_results),
        failed=sum(
            item.outcome in {"contract_failure", "identity_rejected"} for item in item_results
        ),
        records_created=sum(item.records_created for item in item_results),
        records_replayed=sum(item.records_replayed for item in item_results),
        has_more=has_more,
        sec_only_boundary=(
            "IR documents are intentionally excluded because robots and render "
            "authorization require the IR-specific capture lane."
        ),
        items=item_results,
    )


def _fetch_one(
    session: SessionLike,
    candidate: ExpectedSecDocument,
    request: SecNativeCaptureRequest,
    run_root: Path,
) -> CaptureCheckpointEntry:
    started = datetime.now(UTC)
    try:
        response = session.get(
            candidate.source_url,
            headers={
                "User-Agent": request.user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=(request.connect_timeout_seconds, request.read_timeout_seconds),
            stream=True,
        )
    except requests.Timeout:
        return _failure_entry(candidate, "transient_deferred", "sec_fetch_timeout", started)
    except requests.RequestException:
        return _failure_entry(
            candidate, "transient_deferred", "sec_fetch_network_deferred", started
        )
    try:
        if response.status_code in {401, 403}:
            return _failure_entry(
                candidate, "auth_hard_stop", "sec_authorization_hard_stop", started
            )
        if response.status_code == 429 or response.status_code >= 500:
            return _failure_entry(
                candidate, "transient_deferred", "sec_fetch_transient_status", started
            )
        if response.status_code != 200:
            return _failure_entry(
                candidate, "contract_failure", "sec_fetch_contract_status", started
            )
        declared_length = _content_length(response.headers)
        if declared_length is not None and declared_length > request.max_document_bytes:
            return _failure_entry(candidate, "contract_failure", "sec_document_too_large", started)
        body = _bounded_body(response, request.max_document_bytes)
        if not body:
            return _failure_entry(candidate, "contract_failure", "sec_empty_response", started)
        digest = hashlib.sha256(body).hexdigest()
        _store_checkpoint_body(run_root, digest, body)
        retrieved = datetime.now(UTC)
        return CaptureCheckpointEntry(
            expected_document_id=candidate.expected_document_id,
            snapshot_id=candidate.snapshot_id,
            source_url=candidate.source_url,
            outcome="fetched",
            observed_at=started,
            retrieved_at=retrieved,
            response_sha256=digest,
            byte_size=len(body),
            media_type=_media_type(response.headers),
            reason_code="sec_authoritative_bytes_fetched",
        )
    except requests.Timeout:
        return _failure_entry(candidate, "transient_deferred", "sec_fetch_timeout", started)
    except requests.RequestException:
        return _failure_entry(
            candidate, "transient_deferred", "sec_fetch_network_deferred", started
        )
    except SecNativeCaptureError:
        return _failure_entry(candidate, "contract_failure", "sec_document_too_large", started)
    finally:
        response.close()


def _bounded_body(response: _ResponseLike, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > maximum:
            raise SecNativeCaptureError("SEC response exceeds configured byte budget")
        chunks.append(chunk)
    return b"".join(chunks)


def _failure_entry(
    candidate: ExpectedSecDocument,
    outcome: FetchOutcome,
    reason_code: str,
    observed_at: datetime,
) -> CaptureCheckpointEntry:
    return CaptureCheckpointEntry(
        expected_document_id=candidate.expected_document_id,
        snapshot_id=candidate.snapshot_id,
        source_url=candidate.source_url,
        outcome=outcome,
        observed_at=observed_at,
        retrieved_at=datetime.now(UTC),
        reason_code=reason_code,
    )


def _persist_batch(
    conn: sqlite3.Connection,
    request: SecNativeCaptureRequest,
    items: list[tuple[ExpectedSecDocument, CaptureCheckpointEntry]],
) -> tuple[CaptureItemResult, ...]:
    results: list[CaptureItemResult] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for candidate, entry in items:
            if entry.outcome == "fetched":
                results.append(_persist_fetched(conn, request, candidate, entry))
            else:
                results.append(_persist_failure(conn, request, candidate, entry))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return tuple(results)


def _persist_fetched(
    conn: sqlite3.Connection,
    request: SecNativeCaptureRequest,
    candidate: ExpectedSecDocument,
    entry: CaptureCheckpointEntry,
) -> CaptureItemResult:
    digest = _required_entry_digest(entry)
    storage_path = _blob_path(request.blob_root, digest)
    storage_uri = storage_path.resolve().as_uri()
    ledger = EvidenceLedger(conn)
    created = 0
    replayed = 0
    existing_blob = conn.execute(
        "SELECT byte_size, media_type, storage_uri, recorded_at "
        "FROM evidence_content_blobs WHERE sha256 = ?",
        (digest,),
    ).fetchone()
    if existing_blob is None:
        blob = ContentBlob(
            sha256=digest,
            byte_size=_required_entry_size(entry),
            media_type=_required_entry_media_type(entry),
            storage_uri=storage_uri,
            recorded_at=entry.retrieved_at,
        )
    else:
        blob = ContentBlob(
            sha256=digest,
            byte_size=int(existing_blob[0]),
            media_type=str(existing_blob[1]),
            storage_uri=str(existing_blob[2]),
            recorded_at=_required_datetime(existing_blob[3]),
        )
    created, replayed = _account(ledger.persist(blob).created, created, replayed)

    identity = _stable_digest(
        request.task_id,
        candidate.expected_document_id,
        digest,
    )
    observation = SourceObservation(
        observation_id=f"sec-capture-observation:{identity}",
        idempotency_key=f"sec-capture:{identity}",
        source_kind="sec_filing",
        source_url=candidate.source_url,
        blob_sha256=digest,
        source_published_at=None,
        filing_at=candidate.filing_at,
        accepted_at=None,
        observed_at=entry.observed_at,
        retrieved_at=entry.retrieved_at,
        retrieval_config_sha256=_CONFIG_SHA256,
        collector_code_version=_COLLECTOR_VERSION,
    )
    created, replayed = _account(ledger.persist(observation).created, created, replayed)

    document_version_id, is_new = _document_version(
        conn,
        ledger,
        candidate,
        observation,
        digest,
        entry.retrieved_at,
    )
    created, replayed = _account(is_new, created, replayed)
    link_ledger = EvidenceLinkLedger(conn)
    location_created = _ensure_location(
        conn,
        link_ledger,
        digest=digest,
        storage_uri=storage_uri,
        byte_size=_required_entry_size(entry),
        verified_at=entry.retrieved_at,
    )
    created, replayed = _account(location_created, created, replayed)
    link = DocumentObservationLink(
        link_id=f"sec-capture-link:{identity}",
        document_version_id=document_version_id,
        observation_id=observation.observation_id,
        link_kind="primary" if is_new else "retrieval",
        linked_at=entry.retrieved_at,
    )
    created, replayed = _account(link_ledger.persist_link(link).created, created, replayed)
    coverage_created = _persist_coverage(
        conn,
        request,
        candidate,
        entry,
        coverage_status="captured",
        document_version_id=document_version_id,
    )
    created, replayed = _account(coverage_created, created, replayed)
    return CaptureItemResult(
        expected_document_id=candidate.expected_document_id,
        expected_document_key=candidate.expected_document_key,
        outcome=entry.outcome,
        reason_code=entry.reason_code,
        document_version_id=document_version_id,
        records_created=created,
        records_replayed=replayed,
    )


def _persist_failure(
    conn: sqlite3.Connection,
    request: SecNativeCaptureRequest,
    candidate: ExpectedSecDocument,
    entry: CaptureCheckpointEntry,
) -> CaptureItemResult:
    created = _persist_coverage(
        conn,
        request,
        candidate,
        entry,
        coverage_status="fetch_failed" if entry.outcome == "transient_deferred" else "quarantined",
        document_version_id=None,
    )
    return CaptureItemResult(
        expected_document_id=candidate.expected_document_id,
        expected_document_key=candidate.expected_document_key,
        outcome=entry.outcome,
        reason_code=entry.reason_code,
        records_created=int(created),
        records_replayed=int(not created),
    )


def _document_version(
    conn: sqlite3.Connection,
    ledger: EvidenceLedger,
    candidate: ExpectedSecDocument,
    observation: SourceObservation,
    digest: str,
    recorded_at: datetime,
) -> tuple[str, bool]:
    exact = conn.execute(
        "SELECT document_version_id, issuer_id, ticker, document_type, form_type, "
        "accession_number, period_start, period_end "
        "FROM evidence_document_versions "
        "WHERE document_key = ? AND blob_sha256 = ?",
        (candidate.expected_document_key, digest),
    ).fetchone()
    if exact is not None:
        expected_metadata = (
            candidate.issuer_id,
            candidate.ticker,
            candidate.document_type,
            candidate.form_type,
            candidate.accession_number,
            candidate.period_start,
            candidate.period_end,
        )
        stored_metadata = (
            str(exact[1]),
            None if exact[2] is None else str(exact[2]),
            str(exact[3]),
            str(exact[4]),
            None if exact[5] is None else str(exact[5]),
            _optional_datetime(exact[6]),
            _optional_datetime(exact[7]),
        )
        if not _same_metadata(stored_metadata, expected_metadata):
            raise SecNativeCaptureError(
                "existing document bytes conflict with sealed expected-document metadata"
            )
        return str(exact[0]), False
    current = conn.execute(
        "SELECT document_version_id, version_sequence "
        "FROM evidence_document_versions WHERE document_key = ? "
        "ORDER BY version_sequence DESC LIMIT 1",
        (candidate.expected_document_key,),
    ).fetchone()
    sequence = 1 if current is None else int(current[1]) + 1
    replaces = None if current is None else str(current[0])
    document_identity = _stable_digest(candidate.expected_document_key, digest)
    document = DocumentVersion(
        document_version_id=f"sec-native-document:{document_identity}",
        document_key=candidate.expected_document_key,
        version_sequence=sequence,
        observation_id=observation.observation_id,
        blob_sha256=digest,
        issuer_id=candidate.issuer_id,
        ticker=candidate.ticker,
        document_type=candidate.document_type,
        form_type=candidate.form_type,
        accession_number=candidate.accession_number,
        exhibit_id=None,
        period_start=candidate.period_start,
        period_end=candidate.period_end,
        as_of_at=candidate.filing_at,
        language="und",
        replaces_document_version_id=replaces,
        legacy_document_id=None,
        recorded_at=recorded_at,
    )
    result = ledger.persist(document)
    return document.document_version_id, result.created


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
        "FROM v_evidence_blob_locations_current WHERE blob_sha256 = ? AND storage_uri = ?",
        (digest, storage_uri),
    ).fetchone()
    if current is not None and str(current[1]) == "present":
        return False
    sequence = 1 if current is None else int(current[2]) + 1
    supersedes = None if current is None else str(current[0])
    identity = _stable_digest(digest, storage_uri, str(sequence), "present")
    return ledger.persist_location(
        BlobLocationObservation(
            location_observation_id=f"sec-native-location:{identity}",
            idempotency_key=f"sec-native-location:{identity}",
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


def _persist_coverage(
    conn: sqlite3.Connection,
    request: SecNativeCaptureRequest,
    candidate: ExpectedSecDocument,
    entry: CaptureCheckpointEntry,
    *,
    coverage_status: Literal["captured", "fetch_failed", "quarantined"],
    document_version_id: str | None,
) -> bool:
    idempotency_key = "sec-native-coverage:" + _stable_digest(
        request.task_id,
        candidate.expected_document_id,
        entry.reason_code,
    )
    existing = conn.execute(
        "SELECT expected_document_id, coverage_status, document_version_id, reason_code "
        "FROM source_coverage_assessments WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        existing_values = (
            str(existing[0]),
            str(existing[1]),
            None if existing[2] is None else str(existing[2]),
            str(existing[3]),
        )
        expected_values = (
            candidate.expected_document_id,
            coverage_status,
            document_version_id,
            entry.reason_code,
        )
        if existing_values != expected_values:
            raise SecNativeCaptureError(
                "coverage idempotency identity conflicts with existing data"
            )
        return False
    current = conn.execute(
        "SELECT assessment_id, revision FROM v_source_coverage_current "
        "WHERE expected_document_id = ?",
        (candidate.expected_document_id,),
    ).fetchone()
    revision = 1 if current is None else int(current[1]) + 1
    supersedes = None if current is None else str(current[0])
    identity = _stable_digest(idempotency_key)
    return (
        SourceCoverageLedger(conn)
        .persist(
            CoverageAssessment(
                assessment_id=f"sec-native-coverage:{identity}",
                idempotency_key=idempotency_key,
                expected_document_id=candidate.expected_document_id,
                revision=revision,
                coverage_status=coverage_status,
                document_version_id=document_version_id,
                extraction_run_id=None,
                manifest_id=None,
                index_run_id=None,
                reason_code=entry.reason_code,
                reason_details=(
                    ("collector", _COLLECTOR_VERSION),
                    ("source_inventory_snapshot_id", candidate.snapshot_id),
                ),
                decision_kind="deterministic",
                policy_name=_POLICY_NAME,
                policy_version=_POLICY_VERSION,
                policy_config_sha256=_CONFIG_SHA256,
                effective_at=entry.observed_at,
                knowledge_at=entry.retrieved_at,
                recorded_at=entry.retrieved_at,
                supersedes_assessment_id=supersedes,
                material_dissent=False,
            )
        )
        .created
    )


def _promote_raw_bytes(
    run_root: Path,
    blob_root: Path,
    entry: CaptureCheckpointEntry,
) -> None:
    digest = _required_entry_digest(entry)
    body = _read_checkpoint_body(run_root, digest)
    if len(body) != _required_entry_size(entry):
        raise SecNativeCaptureError("checkpoint byte size does not match fetched metadata")
    _write_content_addressed(_blob_path(blob_root, digest), digest, body)


def _store_checkpoint_body(run_root: Path, digest: str, body: bytes) -> None:
    _write_content_addressed(run_root / "responses" / digest, digest, body)


def _read_checkpoint_body(run_root: Path, digest: str) -> bytes:
    path = run_root / "responses" / digest
    try:
        body = path.read_bytes()
    except OSError:
        raise SecNativeCaptureError("checkpoint response bytes are unavailable") from None
    if hashlib.sha256(body).hexdigest() != digest:
        raise SecNativeCaptureError("checkpoint response bytes fail hash verification")
    return body


def _write_content_addressed(path: Path, digest: str, body: bytes) -> None:
    if hashlib.sha256(body).hexdigest() != digest:
        raise SecNativeCaptureError("content-addressed write received mismatched bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SecNativeCaptureError("existing content-addressed file fails verification")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_checkpoint(path: Path, task_id: str) -> CaptureCheckpoint:
    if not path.exists():
        return CaptureCheckpoint(task_id=task_id, updated_at=datetime.now(UTC))
    try:
        checkpoint = CaptureCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SecNativeCaptureError(f"capture checkpoint is invalid: {redact(exc)}") from None
    if checkpoint.task_id != task_id:
        raise SecNativeCaptureError("capture checkpoint task identity does not match request")
    return checkpoint


def _write_checkpoint(path: Path, checkpoint: CaptureCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(checkpoint.model_dump_json(), encoding="utf-8")
    temporary.replace(path)


def _validate_checkpoint_identity(
    candidate: ExpectedSecDocument,
    checkpoint: CaptureCheckpointEntry,
) -> None:
    if (
        checkpoint.snapshot_id != candidate.snapshot_id
        or checkpoint.source_url != candidate.source_url
    ):
        raise SecNativeCaptureError(
            "checkpoint source identity no longer matches the current sealed inventory"
        )


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = _header(headers, "content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _media_type(headers: Mapping[str, str]) -> str:
    raw = _header(headers, "content-type")
    media_type = "application/octet-stream" if raw is None else raw.split(";", 1)[0].strip().lower()
    return media_type[:255] or "application/octet-stream"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return str(value)
    return None


def _required_identity(value: object, label: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{label} is required")
    return str(value).strip()


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _required_datetime(value)


def _required_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _required_entry_digest(entry: CaptureCheckpointEntry) -> str:
    if entry.response_sha256 is None:
        raise SecNativeCaptureError("fetched entry has no response digest")
    return entry.response_sha256


def _required_entry_size(entry: CaptureCheckpointEntry) -> int:
    if entry.byte_size is None:
        raise SecNativeCaptureError("fetched entry has no byte size")
    return entry.byte_size


def _required_entry_media_type(entry: CaptureCheckpointEntry) -> str:
    if entry.media_type is None:
        raise SecNativeCaptureError("fetched entry has no media type")
    return entry.media_type


def _blob_path(blob_root: Path, digest: str) -> Path:
    return blob_root / digest[:2] / digest


def _stable_digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _account(was_created: bool, created: int, replayed: int) -> tuple[int, int]:
    return (created + 1, replayed) if was_created else (created, replayed + 1)


def _same_metadata(
    stored: tuple[object, ...],
    expected: tuple[object, ...],
) -> bool:
    if len(stored) != len(expected):
        return False
    for left, right in zip(stored, expected, strict=True):
        if isinstance(left, datetime) and isinstance(right, datetime):
            left_utc = left.replace(tzinfo=UTC) if left.tzinfo is None else left.astimezone(UTC)
            right_utc = right.replace(tzinfo=UTC) if right.tzinfo is None else right.astimezone(UTC)
            if left_utc != right_utc:
                return False
        elif left != right:
            return False
    return True
