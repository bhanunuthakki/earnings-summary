"""Capture and canonically admit one owner-approved exact IR document."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ir_pipeline.authority import PublisherEndpointRule
from ir_pipeline.evidence_capture import (
    ExactIRFetchRequest,
    ExactIRFetchResult,
    IRDocumentCaptureError,
    IRDocumentCaptureHardStopError,
    RobotsCheck,
    SessionLike,
    fetch_exact_ir_bytes,
)
from pipeline.ir_approval_store import (
    DecisionAction,
    EvidenceReference,
    IrAdmissionProof,
    IrApprovalConflictError,
    IrAuthorizationError,
    IrCandidate,
    IrDecision,
    IrDecisionRequest,
    append_decision,
    authorize_current_candidate,
    get_candidate,
    get_current_decision,
    verify_admission,
)
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
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_COLLECTOR = "approved-exact-ir-capture@1"


class ExactIrCaptureError(RuntimeError):
    """An approved exact capture failed closed."""


class ExactIrCaptureHardStopError(ExactIrCaptureError):
    """Authentication is required and browser-auth capture is forbidden."""


class ExactIrCaptureActionInput(BaseModel):
    """Narrow browser boundary: identity is derived from the stored candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    reason: str = Field(min_length=1, max_length=4096)

    @field_validator("candidate_id")
    @classmethod
    def _candidate_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("candidate_id must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason is required")
        return normalized


class ExactIrCaptureReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["admitted", "exact_replay"]
    candidate_id: str
    selection_decision_id: str
    document_version_id: str
    final_url: str
    content_sha256: str
    byte_size: int = Field(ge=0)
    media_type: str
    network_fetched: bool


def capture_and_admit_exact_ir_document(
    db_path: Path,
    action_input: ExactIrCaptureActionInput,
    *,
    owner_actor: str,
    checkpoint_root: Path,
    blob_root: Path,
    task_id: str,
    user_agent: str,
    session: SessionLike,
    robots_allows: RobotsCheck | None = None,
    now: Callable[[], datetime] | None = None,
) -> ExactIrCaptureReceipt:
    """Fetch outside the lock, then CAS-select and admit evidence atomically."""

    action = ExactIrCaptureActionInput.model_validate(action_input.model_dump())
    actor = owner_actor.strip()
    if not actor:
        raise ValueError("owner_actor cannot be blank")
    candidate = _approved_candidate_preflight(db_path, action.candidate_id)
    parsed_candidate_url = urlsplit(candidate.candidate_url)
    if parsed_candidate_url.hostname is None:
        raise ExactIrCaptureError("approved candidate URL has no host")
    rules = (
        PublisherEndpointRule(
            host=parsed_candidate_url.hostname,
            path_prefix=parsed_candidate_url.path,
        ),
    )
    try:
        fetched = fetch_exact_ir_bytes(
            ExactIRFetchRequest(
                candidate_id=candidate.candidate_id,
                authority_url=candidate.authority_url,
                exact_url=candidate.candidate_url,
                publisher_file_rules=rules,
                checkpoint_root=checkpoint_root,
                blob_root=blob_root,
                task_id=task_id,
                user_agent=user_agent,
            ),
            session=session,
            robots_allows=robots_allows,
        )
    except IRDocumentCaptureHardStopError as exc:
        raise ExactIrCaptureHardStopError(str(exc)) from None
    except IRDocumentCaptureError as exc:
        raise ExactIrCaptureError(str(exc)) from None
    clock = now or (lambda: datetime.now(UTC).replace(tzinfo=None))
    decided_at = _naive_utc(clock())
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_candidate = get_candidate(conn, action.candidate_id)
        if current_candidate is None or current_candidate != candidate:
            raise IrApprovalConflictError("candidate changed after exact-byte capture")
        authorize_current_candidate(current_candidate)
        current = get_current_decision(conn, action.candidate_id)
        if current is None:
            raise IrAuthorizationError("exact capture requires current APPROVE")
        selection, replay = _select_exact(
            conn,
            candidate=current_candidate,
            current=current,
            fetched=fetched,
            actor=actor,
            reason=action.reason,
            decided_at=decided_at,
        )
        proof = IrAdmissionProof(
            candidate_id=current_candidate.candidate_id,
            selection_decision_id=selection.decision_id,
            issuer_id=current_candidate.issuer_id,
            ticker=current_candidate.ticker,
            quarter_end=current_candidate.quarter_end,
            doc_type=current_candidate.doc_type,
            canonical_url=current_candidate.candidate_url,
            final_url=fetched.final_url,
            issuer_policy_sha256=current_candidate.issuer_policy_sha256,
            catalog_sha256=current_candidate.catalog_sha256,
            observation_key=current_candidate.observation_key,
            observation_raw_sha256=current_candidate.observation_raw_sha256,
            captured_content_sha256=fetched.content_sha256,
            captured_at=_naive_utc(fetched.retrieved_at),
        )
        verify_admission(conn, proof)
        document_version_id = _persist_evidence(conn, current_candidate, fetched)
        conn.commit()
        return ExactIrCaptureReceipt(
            outcome="exact_replay" if replay else "admitted",
            candidate_id=current_candidate.candidate_id,
            selection_decision_id=selection.decision_id,
            document_version_id=document_version_id,
            final_url=fetched.final_url,
            content_sha256=fetched.content_sha256,
            byte_size=fetched.byte_size,
            media_type=fetched.media_type,
            network_fetched=fetched.network_fetched,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _approved_candidate_preflight(db_path: Path, candidate_id: str) -> IrCandidate:
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=True)
    try:
        candidate = get_candidate(conn, candidate_id)
        if candidate is None:
            raise ExactIrCaptureError("IR approval candidate does not exist")
        authorize_current_candidate(candidate)
        current = get_current_decision(conn, candidate_id)
        if current is None or current.action not in {
            DecisionAction.APPROVE,
            DecisionAction.SELECT_EXACT,
        }:
            raise ExactIrCaptureError("exact capture requires current APPROVE")
        return candidate
    except IrAuthorizationError as exc:
        raise ExactIrCaptureError(str(exc)) from None
    finally:
        conn.close()


def _select_exact(
    conn: sqlite3.Connection,
    *,
    candidate: IrCandidate,
    current: IrDecision,
    fetched: ExactIRFetchResult,
    actor: str,
    reason: str,
    decided_at: datetime,
) -> tuple[IrDecision, bool]:
    if current.action is DecisionAction.SELECT_EXACT:
        if (
            current.selected_url != candidate.candidate_url
            or current.selected_doc_type is not candidate.doc_type
            or current.selected_content_sha256 != fetched.content_sha256
        ):
            raise IrApprovalConflictError("current exact selection conflicts with captured bytes")
        return current, True
    if current.action is not DecisionAction.APPROVE:
        raise IrAuthorizationError("exact capture requires current APPROVE")
    evidence = (
        *candidate.evidence,
        EvidenceReference(
            evidence_id=f"capture:{fetched.content_sha256}",
            locator=fetched.storage_uri,
            content_sha256=fetched.content_sha256,
        ),
    )
    request_hash = _digest(
        candidate.candidate_id,
        str(current.revision),
        actor,
        reason,
        fetched.content_sha256,
    )
    result = append_decision(
        conn,
        IrDecisionRequest(
            request_id=f"exact-capture:{request_hash}",
            candidate_id=candidate.candidate_id,
            action=DecisionAction.SELECT_EXACT,
            expected_revision=current.revision,
            owner_actor=actor,
            decided_at=decided_at,
            reason=reason,
            evidence=evidence,
            selected_url=candidate.candidate_url,
            selected_doc_type=candidate.doc_type,
            selected_content_sha256=fetched.content_sha256,
        ),
    )
    return result.decision, result.outcome == "exact_replay"


def _persist_evidence(
    conn: sqlite3.Connection,
    candidate: IrCandidate,
    fetched: ExactIRFetchResult,
) -> str:
    ledger = EvidenceLedger(conn)
    recorded_at = _naive_utc(fetched.retrieved_at)
    existing_blob = conn.execute(
        "SELECT byte_size,media_type,storage_uri,recorded_at FROM evidence_content_blobs WHERE sha256=?",
        (fetched.content_sha256,),
    ).fetchone()
    if existing_blob is not None and (
        int(existing_blob[0]) != fetched.byte_size or str(existing_blob[1]) != fetched.media_type
    ):
        raise ExactIrCaptureError("canonical IR blob metadata conflicts with captured bytes")
    blob = ContentBlob(
        sha256=fetched.content_sha256,
        byte_size=fetched.byte_size if existing_blob is None else int(existing_blob[0]),
        media_type=fetched.media_type if existing_blob is None else str(existing_blob[1]),
        storage_uri=fetched.storage_uri if existing_blob is None else str(existing_blob[2]),
        recorded_at=recorded_at
        if existing_blob is None
        else datetime.fromisoformat(str(existing_blob[3])),
    )
    ledger.persist(blob)
    identity = _digest(candidate.candidate_id, fetched.content_sha256)
    observation_id = f"ir-approved:{identity}"
    existing_observation = conn.execute(
        "SELECT source_kind,source_url,blob_sha256,retrieval_config_sha256,collector_code_version "
        "FROM evidence_source_observations WHERE observation_id=?",
        (observation_id,),
    ).fetchone()
    observation = SourceObservation(
        observation_id=observation_id,
        idempotency_key=observation_id,
        source_kind="ir_document",
        source_url=fetched.final_url,
        blob_sha256=fetched.content_sha256,
        source_published_at=None,
        filing_at=None,
        accepted_at=None,
        observed_at=_naive_utc(fetched.observed_at),
        retrieved_at=recorded_at,
        retrieval_config_sha256=candidate.issuer_policy_sha256,
        collector_code_version=_COLLECTOR,
    )
    if existing_observation is None:
        ledger.persist(observation)
    elif tuple(existing_observation) != (
        "ir_document",
        fetched.final_url,
        fetched.content_sha256,
        candidate.issuer_policy_sha256,
        _COLLECTOR,
    ):
        raise ExactIrCaptureError("canonical IR observation identity conflicts")
    document_key = (
        f"ir:{candidate.issuer_id}:{candidate.quarter_end.isoformat()}:{candidate.doc_type.value}"
    )
    row = conn.execute(
        "SELECT document_version_id,blob_sha256 FROM evidence_document_versions "
        "WHERE document_key=? ORDER BY version_sequence DESC LIMIT 1",
        (document_key,),
    ).fetchone()
    if row is not None and str(row[1]) == fetched.content_sha256:
        document_id = str(row[0])
    else:
        sequence = (
            1
            if row is None
            else int(
                conn.execute(
                    "SELECT MAX(version_sequence) FROM evidence_document_versions WHERE document_key=?",
                    (document_key,),
                ).fetchone()[0]
            )
            + 1
        )
        document_id = f"ir-approved-doc:{identity}"
        ledger.persist(
            DocumentVersion(
                document_version_id=document_id,
                document_key=document_key,
                version_sequence=sequence,
                observation_id=observation.observation_id,
                blob_sha256=fetched.content_sha256,
                issuer_id=candidate.issuer_id,
                ticker=candidate.ticker,
                document_type=candidate.doc_type.value,
                form_type="IR",
                as_of_at=datetime.combine(candidate.quarter_end, datetime.min.time()),
                language="und",
                replaces_document_version_id=None if row is None else str(row[0]),
                recorded_at=recorded_at,
            )
        )
    links = EvidenceLinkLedger(conn)
    location_identity = _digest(identity, fetched.storage_uri)
    location_id = f"ir-approved-loc:{location_identity}"
    existing_location = conn.execute(
        "SELECT blob_sha256,storage_uri,availability_state,verified_byte_size,verified_sha256 "
        "FROM evidence_blob_location_observations WHERE location_observation_id=?",
        (location_id,),
    ).fetchone()
    location = BlobLocationObservation(
        location_observation_id=location_id,
        idempotency_key=location_id,
        blob_sha256=fetched.content_sha256,
        storage_uri=fetched.storage_uri,
        location_kind="local",
        availability_state="present",
        location_sequence=1,
        verified_at=recorded_at,
        verified_byte_size=fetched.byte_size,
        verified_sha256=fetched.content_sha256,
        recorded_at=recorded_at,
    )
    if existing_location is None:
        links.persist_location(location)
    elif tuple(existing_location) != (
        fetched.content_sha256,
        fetched.storage_uri,
        "present",
        fetched.byte_size,
        fetched.content_sha256,
    ):
        raise ExactIrCaptureError("canonical IR blob location identity conflicts")
    link_id = f"ir-approved-link:{identity}"
    existing_link = conn.execute(
        "SELECT document_version_id,observation_id,link_kind "
        "FROM evidence_document_observation_links WHERE link_id=?",
        (link_id,),
    ).fetchone()
    link = DocumentObservationLink(
        link_id=link_id,
        document_version_id=document_id,
        observation_id=observation.observation_id,
        link_kind="primary",
        linked_at=recorded_at,
    )
    if existing_link is None:
        links.persist_link(link)
    elif tuple(existing_link) != (document_id, observation.observation_id, "primary"):
        raise ExactIrCaptureError("canonical IR document link identity conflicts")
    return document_id


def _naive_utc(value: datetime) -> datetime:
    return value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


__all__ = [
    "ExactIrCaptureActionInput",
    "ExactIrCaptureError",
    "ExactIrCaptureHardStopError",
    "ExactIrCaptureReceipt",
    "capture_and_admit_exact_ir_document",
]
