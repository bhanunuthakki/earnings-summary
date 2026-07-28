"""Evidence-native capture of publisher documents observed by an IR inventory."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import urllib.parse
import urllib.robotparser
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self, cast

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ir_pipeline.authority import IRAuthorityEvidence, PublisherEndpointRule
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

_COLLECTOR = "ir-evidence-capture@1"
_POLICY = "observed-ir-document-capture"
_POLICY_VERSION = "1"
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

CaptureOutcome = Literal[
    "fetched",
    "transient_deferred",
    "contract_failure",
    "identity_rejected",
    "robots_denied",
    "auth_hard_stop",
]
RobotsCheck = Callable[[str, str], bool]


class IRDocumentCaptureError(RuntimeError):
    """The IR capture contract could not be established safely."""


class IRDocumentCaptureHardStopError(IRDocumentCaptureError):
    """Authentication or authorization response requires operator intervention."""


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
        allow_redirects: bool,
    ) -> _ResponseLike: ...


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IRDocumentCaptureRequest(_ClosedModel):
    inventory_keys: tuple[str, ...] = Field(min_length=1)
    publisher_file_rules: tuple[PublisherEndpointRule, ...] = ()
    checkpoint_root: Path
    blob_root: Path
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    user_agent: str = Field(min_length=8, max_length=512)
    apply: bool = False
    batch_size: int = Field(default=25, ge=1, le=250)
    max_document_bytes: int = Field(default=100_000_000, ge=1, le=1_000_000_000)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    read_timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_redirects: int = Field(default=5, ge=0, le=10)

    @field_validator("inventory_keys")
    @classmethod
    def _inventory_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("inventory keys must be non-empty and unique")
        return normalized


class ObservedIRDocument(_ClosedModel):
    expected_document_id: str
    snapshot_id: str
    inventory_key: str
    inventory_source_url: str
    inventory_completion_status: Literal["complete", "incomplete"]
    expected_document_key: str
    issuer_id: str
    ticker: str | None
    document_type: str
    source_url: str
    expected_at: datetime | None


class IRFetchCheckpointEntry(_ClosedModel):
    expected_document_id: str
    snapshot_id: str
    requested_url: str
    final_url: str | None = None
    outcome: CaptureOutcome
    observed_at: datetime
    retrieved_at: datetime
    response_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    byte_size: int | None = Field(default=None, ge=0)
    media_type: str | None = Field(default=None, min_length=1, max_length=255)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    document_version_id: str | None = None

    @model_validator(mode="after")
    def _fetched_bytes(self) -> Self:
        identified = (
            self.response_sha256 is not None
            and self.byte_size is not None
            and self.media_type is not None
            and self.final_url is not None
        )
        if identified != (self.outcome == "fetched"):
            raise ValueError("only fetched IR entries may identify response bytes")
        return self


class IRFetchCheckpoint(_ClosedModel):
    task_id: str
    entries: tuple[IRFetchCheckpointEntry, ...] = ()
    updated_at: datetime


class IRDocumentCaptureItem(_ClosedModel):
    expected_document_id: str
    outcome: CaptureOutcome
    reason_code: str
    document_version_id: str | None = None
    records_created: int = Field(default=0, ge=0)
    records_replayed: int = Field(default=0, ge=0)


class IRDocumentCaptureResult(_ClosedModel):
    task_id: str
    mode: Literal["dry_run", "apply"]
    considered: int = Field(ge=0)
    fetched: int = Field(ge=0)
    deferred: int = Field(ge=0)
    failed: int = Field(ge=0)
    complete_inventory_count: int = Field(ge=0)
    incomplete_inventory_count: int = Field(ge=0)
    records_created: int = Field(ge=0)
    records_replayed: int = Field(ge=0)
    has_more: bool
    items: tuple[IRDocumentCaptureItem, ...]


class _RawCandidate(_ClosedModel):
    url: str
    link_text: str
    filename_hint: str
    document_type_guess: str | None = None
    year_guess: int | None = None
    quarter_guess: int | None = None
    source_page: str


class _RawCandidateInventory(_ClosedModel):
    schema_version: Literal["ir-candidate-inventory@2"]
    page_artifact_sha256: tuple[str, ...]
    candidates: tuple[_RawCandidate, ...]
    crawl_complete: bool
    crawl_stop_reason: str
    authority: IRAuthorityEvidence | None = None


def load_observed_ir_documents(
    conn: sqlite3.Connection,
    *,
    inventory_keys: tuple[str, ...],
    limit: int,
) -> tuple[tuple[ObservedIRDocument, ...], bool]:
    """Load current IR expectations only after exact raw-candidate verification."""

    placeholders = ", ".join("?" for _ in inventory_keys)
    inventories = conn.execute(
        "SELECT inventory.inventory_key, inventory.snapshot_id, inventory.source_url, "
        "seal.completion_status, inventory.source_kind "
        "FROM v_source_inventory_current AS inventory "
        "JOIN source_inventory_snapshot_seals AS seal "
        "ON seal.snapshot_id = inventory.snapshot_id "
        f"WHERE inventory.inventory_key IN ({placeholders}) "
        "ORDER BY inventory.inventory_key",
        inventory_keys,
    ).fetchall()
    by_key = {str(row[0]): row for row in inventories}
    missing = sorted(set(inventory_keys) - set(by_key))
    if missing:
        raise IRDocumentCaptureError(
            "IR inventory keys are not current sealed snapshots: " + ", ".join(missing)
        )
    wrong = sorted(key for key, row in by_key.items() if str(row[4]) != "ir_crawl")
    if wrong:
        raise IRDocumentCaptureError("IR capture refuses non-IR inventories: " + ", ".join(wrong))
    observed_by_snapshot = {
        str(row[1]): _raw_candidate_urls(conn, str(row[1])) for row in inventories
    }
    rows = conn.execute(
        "SELECT expected.expected_document_id, expected.snapshot_id, "
        "inventory.inventory_key, inventory.source_url, seal.completion_status, "
        "expected.expected_document_key, expected.issuer_id, expected.ticker, "
        "expected.document_type, expected.source_url, expected.expected_at "
        "FROM v_expected_documents_current AS expected "
        "JOIN v_source_inventory_current AS inventory "
        "ON inventory.snapshot_id = expected.snapshot_id "
        "JOIN source_inventory_snapshot_seals AS seal "
        "ON seal.snapshot_id = inventory.snapshot_id "
        "LEFT JOIN v_source_coverage_current AS coverage "
        "ON coverage.expected_document_id = expected.expected_document_id "
        f"WHERE inventory.inventory_key IN ({placeholders}) "
        "AND expected.source_kind = 'ir_document' "
        "AND expected.expectation_basis IN ('publisher_candidate', 'authoritative') "
        "AND (coverage.coverage_status IS NULL OR coverage.coverage_status "
        "NOT IN ('captured', 'extracted', 'indexed')) "
        "ORDER BY inventory.inventory_key, expected.expected_document_key LIMIT ?",
        (*inventory_keys, limit + 1),
    ).fetchall()
    documents: list[ObservedIRDocument] = []
    for row in rows[:limit]:
        source_url = _required(row[9], "IR expected source URL")
        snapshot_id = str(row[1])
        if source_url not in observed_by_snapshot[snapshot_id]:
            raise IRDocumentCaptureError(
                "expected IR document is absent from immutable raw discovery evidence"
            )
        documents.append(
            ObservedIRDocument(
                expected_document_id=str(row[0]),
                snapshot_id=snapshot_id,
                inventory_key=str(row[2]),
                inventory_source_url=str(row[3]),
                inventory_completion_status=_completion_status(row[4]),
                expected_document_key=str(row[5]),
                issuer_id=str(row[6]),
                ticker=None if row[7] is None else str(row[7]),
                document_type=str(row[8]),
                source_url=source_url,
                expected_at=_optional_datetime(row[10]),
            )
        )
    return tuple(documents), len(rows) > limit


def capture_observed_ir_documents(
    conn: sqlite3.Connection,
    request: IRDocumentCaptureRequest,
    *,
    session: SessionLike,
    robots_allows: RobotsCheck | None = None,
) -> IRDocumentCaptureResult:
    """Capture one bounded batch; raw bytes and checkpoints precede DB mutation."""

    request = IRDocumentCaptureRequest.model_validate(request.model_dump())
    candidates, has_more = load_observed_ir_documents(
        conn,
        inventory_keys=request.inventory_keys,
        limit=request.batch_size,
    )
    inventory_completion = _inventory_completion(
        conn,
        request.inventory_keys,
    )
    authorizer = robots_allows or _robots_allows
    run_root = request.checkpoint_root / request.task_id
    checkpoint_path = run_root / "state.json"
    checkpoint = _load_checkpoint(checkpoint_path, request.task_id)
    entries = {entry.expected_document_id: entry for entry in checkpoint.entries}
    fetched: list[tuple[ObservedIRDocument, IRFetchCheckpointEntry]] = []
    for candidate in candidates:
        _authorize_url(candidate, candidate.source_url, request)
        prior = entries.get(candidate.expected_document_id)
        if prior is not None:
            _validate_checkpoint(candidate, prior)
        if prior is None or prior.outcome == "transient_deferred":
            entry = _fetch_one(
                session,
                candidate,
                request,
                run_root,
                robots_allows=authorizer,
            )
            entries[candidate.expected_document_id] = entry
            _write_checkpoint(checkpoint_path, request.task_id, entries)
        else:
            entry = prior
        fetched.append((candidate, entry))
        if entry.outcome == "auth_hard_stop":
            raise IRDocumentCaptureHardStopError(
                "publisher authorization hard stop recorded; browser-auth scraping is forbidden"
            )
    if request.apply and fetched:
        for _, entry in fetched:
            if entry.outcome == "fetched":
                _promote_raw(run_root, request.blob_root, entry)
        items = _persist_batch(conn, request, fetched)
        by_id = {item.expected_document_id: item for item in items}
        entries = {
            identity: (
                entry.model_copy(
                    update={"document_version_id": by_id[identity].document_version_id}
                )
                if identity in by_id
                else entry
            )
            for identity, entry in entries.items()
        }
        _write_checkpoint(checkpoint_path, request.task_id, entries)
    else:
        items = tuple(
            IRDocumentCaptureItem(
                expected_document_id=candidate.expected_document_id,
                outcome=entry.outcome,
                reason_code=entry.reason_code,
            )
            for candidate, entry in fetched
        )
    return IRDocumentCaptureResult(
        task_id=request.task_id,
        mode="apply" if request.apply else "dry_run",
        considered=len(items),
        fetched=sum(item.outcome == "fetched" for item in items),
        deferred=sum(item.outcome == "transient_deferred" for item in items),
        failed=sum(
            item.outcome in {"contract_failure", "identity_rejected", "robots_denied"}
            for item in items
        ),
        complete_inventory_count=sum(
            value == "complete" for value in inventory_completion.values()
        ),
        incomplete_inventory_count=sum(
            value == "incomplete" for value in inventory_completion.values()
        ),
        records_created=sum(item.records_created for item in items),
        records_replayed=sum(item.records_replayed for item in items),
        has_more=has_more,
        items=items,
    )


def _fetch_one(
    session: SessionLike,
    candidate: ObservedIRDocument,
    request: IRDocumentCaptureRequest,
    run_root: Path,
    *,
    robots_allows: RobotsCheck,
) -> IRFetchCheckpointEntry:
    observed_at = datetime.now(UTC)
    current_url = candidate.source_url
    for redirect_count in range(request.max_redirects + 1):
        if not robots_allows(current_url, request.user_agent):
            return _failure(candidate, "robots_denied", "ir_robots_denied", observed_at)
        try:
            response = session.get(
                current_url,
                headers={
                    "User-Agent": request.user_agent,
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=(
                    request.connect_timeout_seconds,
                    request.read_timeout_seconds,
                ),
                stream=True,
                allow_redirects=False,
            )
        except requests.Timeout:
            return _failure(
                candidate,
                "transient_deferred",
                "ir_fetch_timeout",
                observed_at,
            )
        except requests.RequestException:
            return _failure(
                candidate,
                "transient_deferred",
                "ir_fetch_network_deferred",
                observed_at,
            )
        try:
            if response.status_code in {401, 403}:
                return _failure(
                    candidate,
                    "auth_hard_stop",
                    "ir_authorization_hard_stop",
                    observed_at,
                )
            if response.status_code in _REDIRECT_CODES:
                location = _header(response.headers, "location")
                if location is None:
                    return _failure(
                        candidate,
                        "contract_failure",
                        "ir_redirect_without_location",
                        observed_at,
                    )
                if redirect_count >= request.max_redirects:
                    return _failure(
                        candidate,
                        "contract_failure",
                        "ir_redirect_budget_exhausted",
                        observed_at,
                    )
                target = _without_fragment(urllib.parse.urljoin(current_url, location))
                try:
                    _authorize_url(candidate, target, request)
                except IRDocumentCaptureError:
                    return _failure(
                        candidate,
                        "identity_rejected",
                        "ir_redirect_not_authorized",
                        observed_at,
                    )
                current_url = target
                continue
            if response.status_code == 429 or response.status_code >= 500:
                return _failure(
                    candidate,
                    "transient_deferred",
                    "ir_fetch_transient_status",
                    observed_at,
                )
            if response.status_code != 200:
                return _failure(
                    candidate,
                    "contract_failure",
                    "ir_fetch_contract_status",
                    observed_at,
                )
            declared = _content_length(response.headers)
            if declared is not None and declared > request.max_document_bytes:
                return _failure(
                    candidate,
                    "contract_failure",
                    "ir_document_too_large",
                    observed_at,
                )
            try:
                digest, byte_size = _stream_checkpoint_body(
                    response,
                    run_root,
                    candidate.expected_document_id,
                    request.max_document_bytes,
                )
            except IRDocumentCaptureError:
                return _failure(
                    candidate,
                    "contract_failure",
                    "ir_document_too_large",
                    observed_at,
                )
            if byte_size == 0:
                return _failure(
                    candidate,
                    "contract_failure",
                    "ir_empty_response",
                    observed_at,
                )
            return IRFetchCheckpointEntry(
                expected_document_id=candidate.expected_document_id,
                snapshot_id=candidate.snapshot_id,
                requested_url=candidate.source_url,
                final_url=current_url,
                outcome="fetched",
                observed_at=observed_at,
                retrieved_at=datetime.now(UTC),
                response_sha256=digest,
                byte_size=byte_size,
                media_type=_media_type(response.headers),
                reason_code="ir_observed_publisher_bytes_fetched",
            )
        finally:
            response.close()
    raise RuntimeError("redirect loop exited without a capture outcome")


def _stream_checkpoint_body(
    response: _ResponseLike,
    run_root: Path,
    expected_document_id: str,
    maximum: int,
) -> tuple[str, int]:
    responses = run_root / "responses"
    responses.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{_stable_digest(expected_document_id)[:12]}-",
        dir=responses,
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > maximum:
                    raise IRDocumentCaptureError(
                        "publisher response exceeds configured byte budget"
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        target = responses / digest.hexdigest()
        if target.exists():
            _verify_file(target, digest.hexdigest(), total)
            temporary.unlink()
        else:
            temporary.replace(target)
        return digest.hexdigest(), total
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _persist_batch(
    conn: sqlite3.Connection,
    request: IRDocumentCaptureRequest,
    records: list[tuple[ObservedIRDocument, IRFetchCheckpointEntry]],
) -> tuple[IRDocumentCaptureItem, ...]:
    results: list[IRDocumentCaptureItem] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for candidate, entry in records:
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
    request: IRDocumentCaptureRequest,
    candidate: ObservedIRDocument,
    entry: IRFetchCheckpointEntry,
) -> IRDocumentCaptureItem:
    digest = _required_digest(entry)
    byte_size = _required_size(entry)
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
    blob = (
        ContentBlob(
            sha256=digest,
            byte_size=byte_size,
            media_type=_required_media_type(entry),
            storage_uri=storage_uri,
            recorded_at=entry.retrieved_at,
        )
        if existing_blob is None
        else ContentBlob(
            sha256=digest,
            byte_size=int(existing_blob[0]),
            media_type=str(existing_blob[1]),
            storage_uri=str(existing_blob[2]),
            recorded_at=_required_datetime(existing_blob[3]),
        )
    )
    created, replayed = _account(ledger.persist(blob).created, created, replayed)
    identity = _stable_digest(
        candidate.expected_document_id,
        digest,
        _config_sha(request),
        candidate.source_url,
    )
    observation = SourceObservation(
        observation_id=f"ir-capture-observation:{identity}",
        idempotency_key=f"ir-capture-observation:{identity}",
        source_kind="ir_document",
        source_url=candidate.source_url,
        blob_sha256=digest,
        source_published_at=candidate.expected_at,
        filing_at=None,
        accepted_at=None,
        observed_at=entry.observed_at,
        retrieved_at=entry.retrieved_at,
        retrieval_config_sha256=_config_sha(request),
        collector_code_version=_COLLECTOR,
    )
    created, replayed = _account(ledger.persist(observation).created, created, replayed)
    final_url = _required_final_url(entry)
    redirect_observation: SourceObservation | None = None
    if final_url != candidate.source_url:
        redirect_identity = _stable_digest(
            candidate.expected_document_id,
            digest,
            _config_sha(request),
            final_url,
        )
        redirect_observation = SourceObservation(
            observation_id=f"ir-capture-observation:{redirect_identity}",
            idempotency_key=f"ir-capture-observation:{redirect_identity}",
            source_kind="ir_document",
            source_url=final_url,
            blob_sha256=digest,
            source_published_at=candidate.expected_at,
            filing_at=None,
            accepted_at=None,
            observed_at=entry.observed_at,
            retrieved_at=entry.retrieved_at,
            retrieval_config_sha256=_config_sha(request),
            collector_code_version=_COLLECTOR,
        )
        created, replayed = _account(
            ledger.persist(redirect_observation).created,
            created,
            replayed,
        )
    document_version_id, document_created = _document_version(
        conn,
        ledger,
        candidate,
        observation,
        digest,
        entry.retrieved_at,
    )
    created, replayed = _account(document_created, created, replayed)
    links = EvidenceLinkLedger(conn)
    location_created = _ensure_location(
        conn,
        links,
        digest=digest,
        storage_uri=storage_uri,
        byte_size=byte_size,
        verified_at=entry.retrieved_at,
    )
    created, replayed = _account(location_created, created, replayed)
    link = DocumentObservationLink(
        link_id=f"ir-capture-link:{identity}",
        document_version_id=document_version_id,
        observation_id=observation.observation_id,
        link_kind="primary" if document_created else "retrieval",
        linked_at=entry.retrieved_at,
    )
    created, replayed = _account(links.persist_link(link).created, created, replayed)
    if redirect_observation is not None:
        redirect_link = DocumentObservationLink(
            link_id=f"ir-capture-link:{_stable_digest(identity, redirect_observation.observation_id)}",
            document_version_id=document_version_id,
            observation_id=redirect_observation.observation_id,
            link_kind="retrieval",
            linked_at=entry.retrieved_at,
        )
        created, replayed = _account(
            links.persist_link(redirect_link).created,
            created,
            replayed,
        )
    coverage_created = _persist_coverage(
        conn,
        request,
        candidate,
        entry,
        coverage_status="captured",
        document_version_id=document_version_id,
    )
    created, replayed = _account(coverage_created, created, replayed)
    return IRDocumentCaptureItem(
        expected_document_id=candidate.expected_document_id,
        outcome=entry.outcome,
        reason_code=entry.reason_code,
        document_version_id=document_version_id,
        records_created=created,
        records_replayed=replayed,
    )


def _persist_failure(
    conn: sqlite3.Connection,
    request: IRDocumentCaptureRequest,
    candidate: ObservedIRDocument,
    entry: IRFetchCheckpointEntry,
) -> IRDocumentCaptureItem:
    created = _persist_coverage(
        conn,
        request,
        candidate,
        entry,
        coverage_status=(
            "fetch_failed"
            if entry.outcome in {"transient_deferred", "robots_denied"}
            else "quarantined"
        ),
        document_version_id=None,
    )
    return IRDocumentCaptureItem(
        expected_document_id=candidate.expected_document_id,
        outcome=entry.outcome,
        reason_code=entry.reason_code,
        records_created=int(created),
        records_replayed=int(not created),
    )


def _document_version(
    conn: sqlite3.Connection,
    ledger: EvidenceLedger,
    candidate: ObservedIRDocument,
    observation: SourceObservation,
    digest: str,
    recorded_at: datetime,
) -> tuple[str, bool]:
    exact = conn.execute(
        "SELECT document_version_id, issuer_id, ticker, document_type, form_type "
        "FROM evidence_document_versions WHERE document_key = ? AND blob_sha256 = ?",
        (candidate.expected_document_key, digest),
    ).fetchone()
    expected_metadata = (
        candidate.issuer_id,
        candidate.ticker,
        candidate.document_type,
        "IR",
    )
    if exact is not None:
        stored = (
            str(exact[1]),
            None if exact[2] is None else str(exact[2]),
            str(exact[3]),
            str(exact[4]),
        )
        if stored != expected_metadata:
            raise IRDocumentCaptureError(
                "existing document bytes conflict with observed IR metadata"
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
    document = DocumentVersion(
        document_version_id=(
            "ir-native-document:" + _stable_digest(candidate.expected_document_key, digest)
        ),
        document_key=candidate.expected_document_key,
        version_sequence=sequence,
        observation_id=observation.observation_id,
        blob_sha256=digest,
        issuer_id=candidate.issuer_id,
        ticker=candidate.ticker,
        document_type=candidate.document_type,
        form_type="IR",
        accession_number=None,
        exhibit_id=None,
        period_start=None,
        period_end=None,
        as_of_at=candidate.expected_at,
        language="und",
        replaces_document_version_id=replaces,
        legacy_document_id=None,
        recorded_at=recorded_at,
    )
    return document.document_version_id, ledger.persist(document).created


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
            location_observation_id=f"ir-native-location:{identity}",
            idempotency_key=f"ir-native-location:{identity}",
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
    request: IRDocumentCaptureRequest,
    candidate: ObservedIRDocument,
    entry: IRFetchCheckpointEntry,
    *,
    coverage_status: Literal["captured", "fetch_failed", "quarantined"],
    document_version_id: str | None,
) -> bool:
    idempotency_key = "ir-native-coverage:" + _stable_digest(
        request.task_id,
        candidate.expected_document_id,
        entry.reason_code,
        document_version_id or "",
    )
    existing = conn.execute(
        "SELECT expected_document_id, coverage_status, document_version_id, reason_code "
        "FROM source_coverage_assessments WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        expected = (
            candidate.expected_document_id,
            coverage_status,
            document_version_id,
            entry.reason_code,
        )
        stored = (
            str(existing[0]),
            str(existing[1]),
            None if existing[2] is None else str(existing[2]),
            str(existing[3]),
        )
        if stored != expected:
            raise IRDocumentCaptureError(
                "IR coverage idempotency identity conflicts with existing data"
            )
        return False
    current = conn.execute(
        "SELECT assessment_id, revision FROM v_source_coverage_current "
        "WHERE expected_document_id = ?",
        (candidate.expected_document_id,),
    ).fetchone()
    revision = 1 if current is None else int(current[1]) + 1
    supersedes = None if current is None else str(current[0])
    config_sha = _config_sha(request)
    return (
        SourceCoverageLedger(conn)
        .persist(
            CoverageAssessment(
                assessment_id=f"ir-native-coverage:{_stable_digest(idempotency_key)}",
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
                    ("collector", _COLLECTOR),
                    ("source_inventory_snapshot_id", candidate.snapshot_id),
                    ("inventory_completion_status", candidate.inventory_completion_status),
                ),
                decision_kind="deterministic",
                policy_name=_POLICY,
                policy_version=_POLICY_VERSION,
                policy_config_sha256=config_sha,
                effective_at=entry.observed_at,
                knowledge_at=entry.retrieved_at,
                recorded_at=entry.retrieved_at,
                supersedes_assessment_id=supersedes,
                material_dissent=candidate.inventory_completion_status == "incomplete",
            )
        )
        .created
    )


def _raw_candidate_urls(conn: sqlite3.Connection, snapshot_id: str) -> frozenset[str]:
    row = conn.execute(
        "SELECT observation.blob_sha256, location.storage_uri "
        "FROM source_inventory_components AS component "
        "JOIN evidence_source_observations AS observation "
        "ON observation.observation_id = component.source_observation_id "
        "JOIN v_evidence_blob_locations_current AS location "
        "ON location.blob_sha256 = observation.blob_sha256 "
        "WHERE component.snapshot_id = ? "
        "AND component.component_key = 'candidate-inventory' "
        "AND component.outcome = 'succeeded' "
        "AND location.availability_state = 'present' "
        "ORDER BY location.verified_at DESC LIMIT 1",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise IRDocumentCaptureError(
            "IR snapshot has no readable immutable candidate-inventory evidence"
        )
    path = _file_uri_path(str(row[1]))
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != str(row[0]):
        raise IRDocumentCaptureError("IR candidate-inventory evidence fails digest verification")
    try:
        payload = _RawCandidateInventory.model_validate_json(body)
    except ValueError:
        raise IRDocumentCaptureError(
            "IR candidate-inventory evidence has an unsupported schema"
        ) from None
    return frozenset(candidate.url for candidate in payload.candidates)


def _inventory_completion(
    conn: sqlite3.Connection,
    inventory_keys: tuple[str, ...],
) -> dict[str, Literal["complete", "incomplete"]]:
    placeholders = ", ".join("?" for _ in inventory_keys)
    rows = conn.execute(
        "SELECT inventory.inventory_key, seal.completion_status "
        "FROM v_source_inventory_current AS inventory "
        "JOIN source_inventory_snapshot_seals AS seal "
        "ON seal.snapshot_id = inventory.snapshot_id "
        f"WHERE inventory.inventory_key IN ({placeholders})",
        inventory_keys,
    ).fetchall()
    statuses: dict[str, Literal["complete", "incomplete"]] = {}
    for row in rows:
        statuses[str(row[0])] = _completion_status(row[1])
    if set(statuses) != set(inventory_keys):
        raise IRDocumentCaptureError(
            "IR inventory completion state changed during capture planning"
        )
    return statuses


def _authorize_url(
    candidate: ObservedIRDocument,
    url: str,
    request: IRDocumentCaptureRequest,
) -> None:
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise IRDocumentCaptureError("IR document URL is not uncredentialed HTTPS")
    inventory_host = urllib.parse.urlparse(candidate.inventory_source_url).hostname
    same_publisher = inventory_host is not None and parsed.hostname.lower().rstrip(
        "."
    ) == inventory_host.lower().rstrip(".")
    if not same_publisher and not any(rule.allows(url) for rule in request.publisher_file_rules):
        raise IRDocumentCaptureError(
            "IR document host/path is not authorized by the publisher inventory"
        )


def _robots_allows(url: str, user_agent: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
    except OSError:
        return False
    return parser.can_fetch(user_agent, url)


def _promote_raw(
    run_root: Path,
    blob_root: Path,
    entry: IRFetchCheckpointEntry,
) -> None:
    digest = _required_digest(entry)
    source = run_root / "responses" / digest
    _verify_file(source, digest, _required_size(entry))
    target = _blob_path(blob_root, digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _verify_file(target, digest, _required_size(entry))
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ir-capture-", dir=target.parent)
    temporary = Path(temporary_name)
    copied_digest = hashlib.sha256()
    copied_size = 0
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            while chunk := reader.read(64 * 1024):
                writer.write(chunk)
                copied_digest.update(chunk)
                copied_size += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if copied_digest.hexdigest() != digest or copied_size != _required_size(entry):
            raise IRDocumentCaptureError("promoted IR bytes fail digest verification")
        temporary.replace(target)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _load_checkpoint(path: Path, task_id: str) -> IRFetchCheckpoint:
    if not path.exists():
        return IRFetchCheckpoint(task_id=task_id, updated_at=datetime.now(UTC))
    checkpoint = IRFetchCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    if checkpoint.task_id != task_id:
        raise IRDocumentCaptureError("IR capture checkpoint task identity mismatch")
    return checkpoint


def _write_checkpoint(
    path: Path,
    task_id: str,
    entries: Mapping[str, IRFetchCheckpointEntry],
) -> None:
    checkpoint = IRFetchCheckpoint(
        task_id=task_id,
        entries=tuple(sorted(entries.values(), key=lambda item: item.expected_document_id)),
        updated_at=datetime.now(UTC),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(checkpoint.model_dump_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _validate_checkpoint(
    candidate: ObservedIRDocument,
    entry: IRFetchCheckpointEntry,
) -> None:
    if entry.snapshot_id != candidate.snapshot_id or entry.requested_url != candidate.source_url:
        raise IRDocumentCaptureError(
            "IR checkpoint identity no longer matches immutable discovery evidence"
        )


def _failure(
    candidate: ObservedIRDocument,
    outcome: CaptureOutcome,
    reason_code: str,
    observed_at: datetime,
) -> IRFetchCheckpointEntry:
    return IRFetchCheckpointEntry(
        expected_document_id=candidate.expected_document_id,
        snapshot_id=candidate.snapshot_id,
        requested_url=candidate.source_url,
        outcome=outcome,
        observed_at=observed_at,
        retrieved_at=datetime.now(UTC),
        reason_code=reason_code,
    )


def _config_sha(request: IRDocumentCaptureRequest) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "collector": _COLLECTOR,
                "publisher_file_rules": [
                    rule.model_dump(mode="json") for rule in request.publisher_file_rules
                ],
                "max_document_bytes": request.max_document_bytes,
                "max_redirects": request.max_redirects,
                "robots": "fail_closed",
                "redirects": "manual_each_hop_authorized",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = _header(headers, "content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _media_type(headers: Mapping[str, str]) -> str:
    raw = _header(headers, "content-type")
    value = "application/octet-stream" if raw is None else raw.split(";", 1)[0].strip().lower()
    return value[:255] or "application/octet-stream"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return str(value)
    return None


def _without_fragment(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def _file_uri_path(storage_uri: str) -> Path:
    parsed = urllib.parse.urlparse(storage_uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise IRDocumentCaptureError(
            "IR candidate-inventory evidence has no supported local replica"
        )
    path = urllib.parse.unquote(parsed.path)
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


def _verify_file(path: Path, digest: str, byte_size: int) -> None:
    hasher = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                hasher.update(chunk)
                total += len(chunk)
    except OSError:
        raise IRDocumentCaptureError("IR response bytes are unavailable") from None
    if hasher.hexdigest() != digest or total != byte_size:
        raise IRDocumentCaptureError("IR response bytes fail digest verification")


def _required(value: object, label: str) -> str:
    if value is None or not str(value).strip():
        raise IRDocumentCaptureError(f"{label} is required")
    return str(value).strip()


def _completion_status(value: object) -> Literal["complete", "incomplete"]:
    normalized = str(value)
    if normalized not in {"complete", "incomplete"}:
        raise IRDocumentCaptureError("IR inventory has invalid completion status")
    return cast(Literal["complete", "incomplete"], normalized)


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _required_datetime(value)


def _required_datetime(value: object) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _required_digest(entry: IRFetchCheckpointEntry) -> str:
    if entry.response_sha256 is None:
        raise IRDocumentCaptureError("fetched IR entry has no digest")
    return entry.response_sha256


def _required_size(entry: IRFetchCheckpointEntry) -> int:
    if entry.byte_size is None:
        raise IRDocumentCaptureError("fetched IR entry has no byte size")
    return entry.byte_size


def _required_media_type(entry: IRFetchCheckpointEntry) -> str:
    if entry.media_type is None:
        raise IRDocumentCaptureError("fetched IR entry has no media type")
    return entry.media_type


def _required_final_url(entry: IRFetchCheckpointEntry) -> str:
    if entry.final_url is None:
        raise IRDocumentCaptureError("fetched IR entry has no final URL")
    return entry.final_url


def _blob_path(blob_root: Path, digest: str) -> Path:
    return blob_root / digest[:2] / digest


def _stable_digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _account(
    was_created: bool,
    created: int,
    replayed: int,
) -> tuple[int, int]:
    return (created + 1, replayed) if was_created else (created, replayed + 1)
