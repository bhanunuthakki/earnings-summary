"""Evidence-first persistence for one audited investor-relations crawl.

The generic crawler returns both raw page-link observations and interpreted
document candidates.  This module first content-addresses and persists the raw
page artifacts plus the complete candidate envelope, then derives the 0219
coverage import.  A crawl failure therefore cannot disappear behind a plausible
short candidate list.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ir_pipeline.authority import (
    IRAuthorityEvidence,
    PublisherSurfaceEvidence,
    authority_is_complete,
)
from ir_pipeline.discover.generic import DocumentDiscoveryInventory
from provenance.evidence_ledger import (
    ContentBlob,
    EvidenceLedger,
    SourceObservation,
)
from provenance.evidence_links import (
    BlobLocationObservation,
    EvidenceLinkLedger,
)
from provenance.source_coverage_reconcile import (
    ExpectedDocumentImport,
    ExplicitAbsence,
    InventoryComponentImport,
    SourceCoverageImport,
    reconcile_source_coverage,
)

_Mode = Literal["dry_run", "apply"]
_PageOutcome = Literal["succeeded", "robots_denied", "failed"]
_SCHEMA_VERSION = "ir-discovery-inventory@2"
_DEFAULT_DOCUMENT_TYPE = "ir_document"


def _timeline(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_sha256(value: str) -> str:
    normalized = value.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IRAnchor(_ClosedModel):
    href: str = Field(min_length=1)
    text: str


class IRCrawlPage(_ClosedModel):
    page_url: str = Field(min_length=1)
    outcome: _PageOutcome
    anchors: tuple[IRAnchor, ...]
    failure_reason: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if self.outcome == "succeeded" and self.failure_reason is not None:
            raise ValueError("successful crawl page cannot have a failure reason")
        if self.outcome != "succeeded" and self.failure_reason is None:
            raise ValueError("failed crawl page requires a failure reason")
        return self


class IRDocumentCandidate(_ClosedModel):
    url: str = Field(min_length=1)
    link_text: str
    filename_hint: str
    document_type_guess: str | None = Field(default=None, min_length=1, max_length=64)
    year_guess: int | None = Field(default=None, ge=1900, le=2200)
    quarter_guess: int | None = Field(default=None, ge=1, le=4)
    source_page: str = Field(min_length=1)

    @model_validator(mode="after")
    def _bind_period_hint(self) -> Self:
        if self.quarter_guess is not None and self.year_guess is None:
            raise ValueError("quarter_guess requires year_guess")
        return self


class IRDiscoverySnapshot(_ClosedModel):
    pages: tuple[IRCrawlPage, ...]
    candidates: tuple[IRDocumentCandidate, ...]
    crawl_complete: bool
    crawl_stop_reason: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_membership(self) -> Self:
        page_urls = [page.page_url for page in self.pages]
        candidate_urls = [candidate.url for candidate in self.candidates]
        if len(page_urls) != len(set(page_urls)):
            raise ValueError("crawl page URLs must be unique")
        if len(candidate_urls) != len(set(candidate_urls)):
            raise ValueError("candidate URLs must be unique")
        if self.crawl_complete and any(page.outcome != "succeeded" for page in self.pages):
            raise ValueError("complete crawl cannot contain failed pages")
        return self


class IRSourceInventoryRequest(_ClosedModel):
    issuer_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(min_length=1, max_length=32)
    ir_url: str = Field(min_length=1)
    revision: int = Field(gt=0)
    discovery: IRDiscoverySnapshot
    authority: IRAuthorityEvidence | None = None
    retrieval_config_sha256: str
    collector_code_version: str = Field(min_length=1, max_length=255)
    started_at: datetime
    completed_at: datetime
    recorded_at: datetime
    reconciled_at: datetime
    apply: bool = False

    _config_sha256 = field_validator("retrieval_config_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _validate_clocks(self) -> Self:
        if _timeline(self.completed_at) < _timeline(self.started_at):
            raise ValueError("completed_at must not precede started_at")
        if _timeline(self.recorded_at) < _timeline(self.completed_at):
            raise ValueError("recorded_at must not precede completed_at")
        if _timeline(self.reconciled_at) < _timeline(self.recorded_at):
            raise ValueError("reconciled_at must not precede recorded_at")
        return self


class IRSourceInventoryResult(_ClosedModel):
    mode: _Mode
    snapshot_id: str
    complete: bool
    candidate_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    records_created: int = Field(ge=0)
    records_replayed: int = Field(ge=0)
    assessment_statuses: tuple[str, ...]


class _PageArtifact(_ClosedModel):
    schema_version: Literal["ir-crawl-page@1"] = "ir-crawl-page@1"
    page: IRCrawlPage


class _CandidateInventoryArtifact(_ClosedModel):
    schema_version: Literal["ir-candidate-inventory@2"] = "ir-candidate-inventory@2"
    page_artifact_sha256: tuple[str, ...]
    candidates: tuple[IRDocumentCandidate, ...]
    crawl_complete: bool
    crawl_stop_reason: str
    authority: IRAuthorityEvidence | None = None


@dataclass(frozen=True, slots=True)
class _Artifact:
    artifact_key: str
    source_url: str
    body: bytes
    sha256: str
    observation_id: str
    storage_path: Path


def source_inventory_request(
    *,
    issuer_id: str,
    ticker: str,
    ir_url: str,
    revision: int,
    inventory: DocumentDiscoveryInventory,
    authority: IRAuthorityEvidence | None = None,
    retrieval_config_sha256: str,
    collector_code_version: str,
    started_at: datetime,
    completed_at: datetime,
    recorded_at: datetime,
    reconciled_at: datetime,
    apply: bool = False,
) -> IRSourceInventoryRequest:
    """Validate crawler output into the closed durable-import boundary."""

    pages = tuple(
        IRCrawlPage(
            page_url=page.page_url,
            outcome=_page_outcome(page.outcome),
            anchors=tuple(IRAnchor(href=href, text=text) for href, text in page.anchors),
            failure_reason=page.failure_reason,
        )
        for page in inventory.pages
    )
    candidates = tuple(
        IRDocumentCandidate(
            url=candidate.url,
            link_text=candidate.link_text,
            filename_hint=candidate.filename_hint,
            document_type_guess=candidate.doc_type_guess,
            year_guess=candidate.year_guess,
            quarter_guess=candidate.quarter_guess,
            source_page=candidate.source_page,
        )
        for candidate in inventory.candidates
    )
    return IRSourceInventoryRequest(
        issuer_id=issuer_id,
        ticker=ticker.strip().upper(),
        ir_url=ir_url,
        revision=revision,
        discovery=IRDiscoverySnapshot(
            pages=pages,
            candidates=candidates,
            crawl_complete=inventory.crawl_complete,
            crawl_stop_reason=inventory.crawl_stop_reason,
        ),
        authority=authority,
        retrieval_config_sha256=retrieval_config_sha256,
        collector_code_version=collector_code_version,
        started_at=started_at,
        completed_at=completed_at,
        recorded_at=recorded_at,
        reconciled_at=reconciled_at,
        apply=apply,
    )


def _page_outcome(value: str) -> _PageOutcome:
    if value not in {"succeeded", "robots_denied", "failed"}:
        raise ValueError(f"unsupported crawl page outcome: {value!r}")
    return cast(_PageOutcome, value)


def sync_ir_source_inventory(
    conn: sqlite3.Connection,
    request: IRSourceInventoryRequest,
    *,
    blob_root: Path,
) -> IRSourceInventoryResult:
    """Persist discovery evidence first, then append its sealed coverage view."""

    request = IRSourceInventoryRequest.model_validate(request.model_dump())
    artifacts = _artifacts(request, blob_root)
    authority_complete = _authority_complete(request)
    if request.authority is not None:
        _validate_authority_observations(conn, request.authority)
    evidence_created = 0
    evidence_replayed = 0
    if request.apply:
        evidence_created, evidence_replayed = _persist_artifacts(
            conn, artifacts, recorded_at=request.recorded_at, request=request
        )
    primary_observation_id = artifacts[-1].observation_id
    coverage_request = _coverage_request(
        request,
        primary_observation_id=primary_observation_id,
        page_artifacts=artifacts[:-1],
    )
    coverage = reconcile_source_coverage(conn, coverage_request)
    return IRSourceInventoryResult(
        mode=coverage.mode,
        snapshot_id=coverage.snapshot_id,
        complete=authority_complete,
        candidate_count=len(request.discovery.candidates),
        page_count=len(request.discovery.pages),
        artifact_count=len(artifacts),
        records_created=evidence_created + coverage.records_created,
        records_replayed=evidence_replayed + coverage.records_replayed,
        assessment_statuses=coverage.assessment_statuses,
    )


def _canonical_bytes(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _artifacts(request: IRSourceInventoryRequest, blob_root: Path) -> tuple[_Artifact, ...]:
    artifacts: list[_Artifact] = []
    page_digests: list[str] = []
    for ordinal, page in enumerate(request.discovery.pages):
        body = _canonical_bytes(_PageArtifact(page=page))
        artifact = _artifact(
            artifact_key=f"page:{ordinal}",
            source_url=page.page_url,
            body=body,
            blob_root=blob_root,
            request=request,
        )
        artifacts.append(artifact)
        page_digests.append(artifact.sha256)
    inventory_body = _canonical_bytes(
        _CandidateInventoryArtifact(
            page_artifact_sha256=tuple(page_digests),
            candidates=tuple(sorted(request.discovery.candidates, key=lambda item: item.url)),
            crawl_complete=request.discovery.crawl_complete,
            crawl_stop_reason=request.discovery.crawl_stop_reason,
            authority=request.authority,
        )
    )
    artifacts.append(
        _artifact(
            artifact_key="candidate-inventory",
            source_url=request.ir_url,
            body=inventory_body,
            blob_root=blob_root,
            request=request,
        )
    )
    return tuple(artifacts)


def _artifact(
    *,
    artifact_key: str,
    source_url: str,
    body: bytes,
    blob_root: Path,
    request: IRSourceInventoryRequest,
) -> _Artifact:
    digest = hashlib.sha256(body).hexdigest()
    observation_seed = _sha_json(
        {
            "schema_version": _SCHEMA_VERSION,
            "artifact_key": artifact_key,
            "source_url": source_url,
            "blob_sha256": digest,
            "retrieval_config_sha256": request.retrieval_config_sha256,
            "started_at": request.started_at.isoformat(),
            "recorded_at": request.recorded_at.isoformat(),
        }
    )
    return _Artifact(
        artifact_key=artifact_key,
        source_url=source_url,
        body=body,
        sha256=digest,
        observation_id=f"source-observation:{observation_seed}",
        storage_path=blob_root / digest[:2] / digest,
    )


def _persist_artifacts(
    conn: sqlite3.Connection,
    artifacts: tuple[_Artifact, ...],
    *,
    recorded_at: datetime,
    request: IRSourceInventoryRequest,
) -> tuple[int, int]:
    for artifact in artifacts:
        artifact.storage_path.parent.mkdir(parents=True, exist_ok=True)
        if artifact.storage_path.exists():
            if hashlib.sha256(artifact.storage_path.read_bytes()).hexdigest() != artifact.sha256:
                raise RuntimeError("existing IR evidence blob fails hash verification")
        else:
            artifact.storage_path.write_bytes(artifact.body)

    ledger = EvidenceLedger(conn)
    links = EvidenceLinkLedger(conn)
    created = 0
    replayed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for artifact in artifacts:
            existing_blob = conn.execute(
                "SELECT byte_size FROM evidence_content_blobs WHERE sha256 = ?",
                (artifact.sha256,),
            ).fetchone()
            if existing_blob is None:
                persisted = ledger.persist(
                    ContentBlob(
                        sha256=artifact.sha256,
                        byte_size=len(artifact.body),
                        media_type="application/json",
                        storage_uri=artifact.storage_path.resolve().as_uri(),
                        recorded_at=recorded_at,
                    )
                )
                created += int(persisted.created)
                replayed += int(not persisted.created)
            elif int(existing_blob[0]) != len(artifact.body):
                raise ValueError("existing IR evidence blob metadata conflicts")
            else:
                replayed += 1

            persisted = ledger.persist(
                SourceObservation(
                    observation_id=artifact.observation_id,
                    idempotency_key=artifact.observation_id,
                    source_kind="ir_crawl",
                    source_url=artifact.source_url,
                    blob_sha256=artifact.sha256,
                    source_published_at=None,
                    filing_at=None,
                    accepted_at=None,
                    observed_at=request.completed_at,
                    retrieved_at=request.completed_at,
                    retrieval_config_sha256=request.retrieval_config_sha256,
                    collector_code_version=request.collector_code_version,
                )
            )
            created += int(persisted.created)
            replayed += int(not persisted.created)

            storage_uri = artifact.storage_path.resolve().as_uri()
            location_seed = _sha_text(artifact.sha256 + "\0" + storage_uri)
            location_id = f"blob-location:{location_seed}"
            existing_location = conn.execute(
                "SELECT 1 FROM evidence_blob_location_observations "
                "WHERE location_observation_id = ?",
                (location_id,),
            ).fetchone()
            if existing_location is None:
                persisted_location = links.persist_location(
                    BlobLocationObservation(
                        location_observation_id=location_id,
                        idempotency_key=location_id,
                        blob_sha256=artifact.sha256,
                        storage_uri=storage_uri,
                        location_kind="local",
                        availability_state="present",
                        location_sequence=1,
                        verified_at=recorded_at,
                        verified_byte_size=len(artifact.body),
                        verified_sha256=artifact.sha256,
                        recorded_at=recorded_at,
                    )
                )
                created += int(persisted_location.created)
                replayed += int(not persisted_location.created)
            else:
                replayed += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return created, replayed


def _coverage_request(
    request: IRSourceInventoryRequest,
    *,
    primary_observation_id: str,
    page_artifacts: tuple[_Artifact, ...],
) -> SourceCoverageImport:
    authority_complete = _authority_complete(request)
    components: list[InventoryComponentImport] = [
        InventoryComponentImport(
            component_key="candidate-inventory",
            component_kind="primary",
            source_url=request.ir_url,
            source_observation_id=primary_observation_id,
            outcome="succeeded",
            required=True,
            ordinal=0,
        )
    ]
    ordinal = 1
    for page, artifact in zip(request.discovery.pages, page_artifacts, strict=True):
        if page.outcome == "succeeded":
            components.append(
                InventoryComponentImport(
                    component_key=f"crawl-page:{ordinal - 1}",
                    component_kind="crawl_page",
                    source_url=page.page_url,
                    source_observation_id=artifact.observation_id,
                    outcome="succeeded",
                    required=True,
                    ordinal=ordinal,
                )
            )
            ordinal += 1
            continue
        components.append(
            InventoryComponentImport(
                component_key=f"page-artifact:{ordinal - 1}",
                component_kind="crawl_page",
                source_url=page.page_url,
                source_observation_id=artifact.observation_id,
                outcome="succeeded",
                required=False,
                ordinal=ordinal,
            )
        )
        ordinal += 1
        components.append(
            InventoryComponentImport(
                component_key=f"page-access:{ordinal - 2}",
                component_kind="crawl_page",
                source_url=page.page_url,
                outcome="failed",
                required=True,
                failure_reason=_reason_code(page.failure_reason or page.outcome),
                ordinal=ordinal,
            )
        )
        ordinal += 1
    if not request.discovery.crawl_complete:
        components.append(
            InventoryComponentImport(
                component_key="crawl-completeness",
                component_kind="other",
                source_url=request.ir_url + "#crawl-completeness",
                outcome="failed",
                required=True,
                failure_reason=_reason_code(request.discovery.crawl_stop_reason),
                ordinal=ordinal,
            )
        )
        ordinal += 1
    ordinal = _authority_components(
        request,
        components=components,
        ordinal=ordinal,
        authority_complete=authority_complete,
    )

    expected = tuple(
        _expected_candidate(
            request,
            candidate,
            authoritative=authority_complete,
        )
        for candidate in sorted(request.discovery.candidates, key=lambda item: item.url)
    )
    return SourceCoverageImport(
        inventory_key=f"{request.issuer_id}:ir-crawl",
        revision=request.revision,
        issuer_id=request.issuer_id,
        ticker=request.ticker,
        source_kind="ir_crawl",
        source_url=request.ir_url,
        source_observation_id=primary_observation_id,
        outcome="succeeded" if authority_complete else "partial",
        authoritative=authority_complete,
        retrieval_config_sha256=request.retrieval_config_sha256,
        collector_code_version=request.collector_code_version,
        started_at=request.started_at,
        completed_at=request.completed_at,
        recorded_at=request.recorded_at,
        reconciled_at=request.reconciled_at,
        components=tuple(components),
        expected_documents=expected,
        apply=request.apply,
    )


def _expected_candidate(
    request: IRSourceInventoryRequest,
    candidate: IRDocumentCandidate,
    *,
    authoritative: bool,
) -> ExpectedDocumentImport:
    url_sha = _sha_text(candidate.url)
    return ExpectedDocumentImport(
        expected_document_key=f"{request.issuer_id}:ir:{url_sha}",
        source_kind="ir_document",
        document_type=candidate.document_type_guess or _DEFAULT_DOCUMENT_TYPE,
        source_url=candidate.url,
        expected_at=request.completed_at,
        expectation_basis="authoritative" if authoritative else "publisher_candidate",
        absence=ExplicitAbsence(
            coverage_status="available",
            reason_code="ir_publisher_candidate",
            reason_details=(
                ("candidate_url_sha256", url_sha),
                ("source_page_sha256", _sha_text(candidate.source_page)),
            ),
        ),
    )


def _reason_code(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return (normalized or "unknown_failure")[:128]


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _authority_complete(request: IRSourceInventoryRequest) -> bool:
    if request.authority is None or not request.discovery.crawl_complete:
        return False
    return authority_is_complete(
        request.authority,
        discovered_urls=tuple(candidate.url for candidate in request.discovery.candidates),
    )


def _validate_authority_observations(
    conn: sqlite3.Connection,
    authority: IRAuthorityEvidence,
) -> None:
    for surface in authority.surfaces:
        row = conn.execute(
            "SELECT source_url, blob_sha256 FROM evidence_source_observations "
            "WHERE observation_id = ?",
            (surface.source_observation_id,),
        ).fetchone()
        if row is None or str(row[0]) != surface.source_url or str(row[1]) != surface.raw_sha256:
            raise ValueError(
                f"authority observation {surface.source_observation_id!r} "
                "does not match its declared source URL and raw digest"
            )


def _authority_components(
    request: IRSourceInventoryRequest,
    *,
    components: list[InventoryComponentImport],
    ordinal: int,
    authority_complete: bool,
) -> int:
    if request.authority is None:
        components.append(
            InventoryComponentImport(
                component_key="publisher-authority",
                component_kind="other",
                source_url=request.ir_url + "#publisher-authority",
                outcome="failed",
                required=True,
                failure_reason="publisher_authority_evidence_required",
                ordinal=ordinal,
            )
        )
        return ordinal + 1
    for surface in request.authority.surfaces:
        components.append(
            InventoryComponentImport(
                component_key=f"authority:{surface.surface_key}",
                component_kind=_authority_component_kind(surface),
                source_url=surface.source_url,
                source_observation_id=surface.source_observation_id,
                outcome="succeeded",
                required=surface.required and surface.outcome == "exhausted",
                ordinal=ordinal,
            )
        )
        ordinal += 1
        if surface.required and surface.outcome != "exhausted":
            components.append(
                InventoryComponentImport(
                    component_key=f"authority-exhaustion:{surface.surface_key}",
                    component_kind=_authority_component_kind(surface),
                    source_url=surface.source_url + "#authority-exhaustion",
                    outcome="failed",
                    required=True,
                    failure_reason=f"publisher_surface_{surface.outcome}",
                    ordinal=ordinal,
                )
            )
            ordinal += 1
    if not authority_complete and all(
        not surface.required or surface.outcome == "exhausted"
        for surface in request.authority.surfaces
    ):
        components.append(
            InventoryComponentImport(
                component_key="publisher-authority-url-membership",
                component_kind="other",
                source_url=request.ir_url + "#publisher-authority-url-membership",
                outcome="failed",
                required=True,
                failure_reason="publisher_authority_url_set_mismatch",
                ordinal=ordinal,
            )
        )
        ordinal += 1
    return ordinal


def _authority_component_kind(
    surface: PublisherSurfaceEvidence,
) -> Literal["primary", "historical_page", "event_feed", "other"]:
    if surface.surface_kind == "primary_landing":
        return "primary"
    if surface.surface_kind in {"archive", "pagination", "load_more"}:
        return "historical_page"
    if surface.surface_kind in {"publisher_api", "event_feed"}:
        return "event_feed"
    return "other"
