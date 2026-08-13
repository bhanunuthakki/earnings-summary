"""Immutable owner approval and exact-document selection for approved IR catalogs.

This module stops at governance. It records catalog-bound candidates and owner
decisions, then verifies a typed capture proof before a separate ingestion
adapter may admit bytes. It performs no network access and does not mutate
issuer policy, IR configuration, documents, or runtime scheduling.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models.documents import DocType
from pipeline.approved_ir_catalog import (
    ApprovedIrCatalog,
    CatalogDisposition,
    IrCatalogEntry,
    IrSourceObservation,
)
from pipeline.source_policy import (
    IssuerAcquisitionPolicy,
    ir_url_is_authorized,
    issuer_policy,
)


class IrApprovalError(ValueError):
    """Base error for the IR owner-approval store."""


class IrApprovalConflictError(IrApprovalError):
    """An idempotency identity or compare-and-swap revision conflicted."""


class IrAuthorizationError(IrApprovalError):
    """A candidate, selection, or admission proof failed closed."""


class DecisionAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    SELECT_EXACT = "select_exact"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _validate_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return value


def _validate_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        raise ValueError("timestamps must use the repository naive-UTC convention")
    return value


def _validate_optional_sha256(value: str | None) -> str | None:
    return None if value is None else _validate_sha256(value)


class EvidenceReference(_FrozenModel):
    evidence_id: str = Field(min_length=1, max_length=256)
    locator: str = Field(min_length=1, max_length=2048)
    content_sha256: str | None = None

    _content_hash = field_validator("content_sha256")(_validate_optional_sha256)


class IrCandidateRequest(_FrozenModel):
    request_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(min_length=1, max_length=16)
    catalog: ApprovedIrCatalog
    candidate_url: str = Field(min_length=1, max_length=4096)
    recorded_by: str = Field(min_length=1, max_length=256)
    recorded_at: datetime
    reason: str = Field(min_length=1, max_length=4096)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1, max_length=100)

    _recorded_at = field_validator("recorded_at")(_validate_naive_utc)

    @field_validator("ticker")
    @classmethod
    def _uppercase_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not ticker:
            raise ValueError("ticker cannot be blank")
        return ticker


class IrDecisionRequest(_FrozenModel):
    request_id: str = Field(min_length=1, max_length=128)
    candidate_id: str
    action: DecisionAction
    expected_revision: int = Field(ge=0)
    owner_actor: str = Field(min_length=1, max_length=256)
    decided_at: datetime
    reason: str = Field(min_length=1, max_length=4096)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1, max_length=100)
    selected_url: str | None = Field(default=None, min_length=1, max_length=4096)
    selected_doc_type: DocType | None = None
    selected_content_sha256: str | None = None

    _candidate_hash = field_validator("candidate_id")(_validate_sha256)
    _decided_at = field_validator("decided_at")(_validate_naive_utc)

    @model_validator(mode="after")
    def _selection_fields_match_action(self) -> Self:
        has_selection = any(
            value is not None
            for value in (
                self.selected_url,
                self.selected_doc_type,
                self.selected_content_sha256,
            )
        )
        if self.action is DecisionAction.SELECT_EXACT:
            if (
                self.selected_url is None
                or self.selected_doc_type is None
                or self.selected_content_sha256 is None
            ):
                raise ValueError("select_exact requires URL, document type, and content hash")
        elif has_selection:
            raise ValueError("only select_exact may carry document-selection fields")
        return self

    _selected_content_hash = field_validator("selected_content_sha256")(_validate_optional_sha256)


class IrCandidate(_FrozenModel):
    candidate_id: str
    request_id: str
    request_sha256: str
    issuer_id: str
    ticker: str
    catalog_sha256: str
    issuer_policy_sha256: str
    authority_url: str
    quarter_end: date
    title: str
    candidate_url: str
    disposition: CatalogDisposition
    doc_type: DocType
    observation_key: str
    observation_raw_sha256: str
    evidence_locator: str
    recorded_by: str
    recorded_at: datetime
    reason: str
    evidence: tuple[EvidenceReference, ...]
    evidence_sha256: str

    _hashes = field_validator(
        "candidate_id",
        "request_sha256",
        "catalog_sha256",
        "issuer_policy_sha256",
        "observation_raw_sha256",
        "evidence_sha256",
    )(_validate_sha256)
    _recorded_at = field_validator("recorded_at")(_validate_naive_utc)


class IrDecision(_FrozenModel):
    decision_id: str
    request_id: str
    request_sha256: str
    candidate_id: str
    action: DecisionAction
    expected_revision: int
    revision: int
    supersedes_decision_id: str | None
    owner_actor: str
    decided_at: datetime
    reason: str
    evidence: tuple[EvidenceReference, ...]
    evidence_sha256: str
    selected_url: str | None
    selected_doc_type: DocType | None
    selected_content_sha256: str | None

    _required_hashes = field_validator(
        "decision_id", "request_sha256", "candidate_id", "evidence_sha256"
    )(_validate_sha256)
    _optional_hash = field_validator("supersedes_decision_id")(_validate_optional_sha256)
    _selected_content_hash = field_validator("selected_content_sha256")(_validate_optional_sha256)
    _decided_at = field_validator("decided_at")(_validate_naive_utc)


class CandidateWriteResult(_FrozenModel):
    outcome: Literal["created", "exact_replay"]
    candidate: IrCandidate


class DecisionWriteResult(_FrozenModel):
    outcome: Literal["appended", "exact_replay"]
    decision: IrDecision


class IrAdmissionProof(_FrozenModel):
    """Capture-time proof required before another adapter may admit document bytes."""

    candidate_id: str
    selection_decision_id: str
    issuer_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(min_length=1, max_length=16)
    quarter_end: date
    doc_type: DocType
    canonical_url: str = Field(min_length=1, max_length=4096)
    final_url: str = Field(min_length=1, max_length=4096)
    issuer_policy_sha256: str
    catalog_sha256: str
    observation_key: str = Field(min_length=1, max_length=256)
    observation_raw_sha256: str
    captured_content_sha256: str
    captured_at: datetime

    _hashes = field_validator(
        "candidate_id",
        "selection_decision_id",
        "issuer_policy_sha256",
        "catalog_sha256",
        "observation_raw_sha256",
        "captured_content_sha256",
    )(_validate_sha256)
    _captured_at = field_validator("captured_at")(_validate_naive_utc)

    @field_validator("ticker")
    @classmethod
    def _uppercase_ticker(cls, value: str) -> str:
        ticker = value.strip().upper()
        if not ticker:
            raise ValueError("ticker cannot be blank")
        return ticker


class VerifiedIrAdmission(_FrozenModel):
    candidate_id: str
    selection_decision_id: str
    captured_content_sha256: str
    captured_at: datetime

    _hashes = field_validator("candidate_id", "selection_decision_id", "captured_content_sha256")(
        _validate_sha256
    )
    _captured_at = field_validator("captured_at")(_validate_naive_utc)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _model_sha256(model: BaseModel) -> str:
    return _sha256_text(_canonical_json(model.model_dump(mode="json")))


def _evidence_payload(
    evidence: tuple[EvidenceReference, ...],
) -> tuple[str, str]:
    payload = _canonical_json([item.model_dump(mode="json") for item in evidence])
    return payload, _sha256_text(payload)


@contextmanager
def _savepoint(connection: sqlite3.Connection, name: str) -> Generator[None, None, None]:
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
        connection.execute(f"RELEASE SAVEPOINT {name}")
        raise
    connection.execute(f"RELEASE SAVEPOINT {name}")


_CANDIDATE_COLUMNS = (
    "candidate_id",
    "request_id",
    "request_sha256",
    "issuer_id",
    "ticker",
    "catalog_sha256",
    "issuer_policy_sha256",
    "authority_url",
    "quarter_end",
    "title",
    "candidate_url",
    "disposition",
    "doc_type",
    "observation_key",
    "observation_raw_sha256",
    "evidence_locator",
    "recorded_by",
    "recorded_at",
    "reason",
    "evidence_json",
    "evidence_sha256",
)

_DECISION_COLUMNS = (
    "decision_id",
    "request_id",
    "request_sha256",
    "candidate_id",
    "action",
    "expected_revision",
    "revision",
    "supersedes_decision_id",
    "owner_actor",
    "decided_at",
    "reason",
    "evidence_json",
    "evidence_sha256",
    "selected_url",
    "selected_doc_type",
    "selected_content_sha256",
)


def _mapping(columns: Sequence[str], row: sqlite3.Row | tuple[object, ...]) -> dict[str, object]:
    return dict(zip(columns, tuple(row), strict=True))


def _candidate_from_row(row: sqlite3.Row | tuple[object, ...]) -> IrCandidate:
    values = _mapping(_CANDIDATE_COLUMNS, row)
    return IrCandidate(
        candidate_id=str(values["candidate_id"]),
        request_id=str(values["request_id"]),
        request_sha256=str(values["request_sha256"]),
        issuer_id=str(values["issuer_id"]),
        ticker=str(values["ticker"]),
        catalog_sha256=str(values["catalog_sha256"]),
        issuer_policy_sha256=str(values["issuer_policy_sha256"]),
        authority_url=str(values["authority_url"]),
        quarter_end=date.fromisoformat(str(values["quarter_end"])),
        title=str(values["title"]),
        candidate_url=str(values["candidate_url"]),
        disposition=CatalogDisposition(str(values["disposition"])),
        doc_type=DocType(str(values["doc_type"])),
        observation_key=str(values["observation_key"]),
        observation_raw_sha256=str(values["observation_raw_sha256"]),
        evidence_locator=str(values["evidence_locator"]),
        recorded_by=str(values["recorded_by"]),
        recorded_at=datetime.fromisoformat(str(values["recorded_at"])),
        reason=str(values["reason"]),
        evidence=tuple(
            EvidenceReference.model_validate(item)
            for item in json.loads(str(values["evidence_json"]))
        ),
        evidence_sha256=str(values["evidence_sha256"]),
    )


def _decision_from_row(row: sqlite3.Row | tuple[object, ...]) -> IrDecision:
    values = _mapping(_DECISION_COLUMNS, row)
    supersedes = values["supersedes_decision_id"]
    selected_url = values["selected_url"]
    selected_doc_type = values["selected_doc_type"]
    selected_content_sha256 = values["selected_content_sha256"]
    return IrDecision(
        decision_id=str(values["decision_id"]),
        request_id=str(values["request_id"]),
        request_sha256=str(values["request_sha256"]),
        candidate_id=str(values["candidate_id"]),
        action=DecisionAction(str(values["action"])),
        expected_revision=int(str(values["expected_revision"])),
        revision=int(str(values["revision"])),
        supersedes_decision_id=None if supersedes is None else str(supersedes),
        owner_actor=str(values["owner_actor"]),
        decided_at=datetime.fromisoformat(str(values["decided_at"])),
        reason=str(values["reason"]),
        evidence=tuple(
            EvidenceReference.model_validate(item)
            for item in json.loads(str(values["evidence_json"]))
        ),
        evidence_sha256=str(values["evidence_sha256"]),
        selected_url=None if selected_url is None else str(selected_url),
        selected_doc_type=None if selected_doc_type is None else DocType(str(selected_doc_type)),
        selected_content_sha256=(
            None if selected_content_sha256 is None else str(selected_content_sha256)
        ),
    )


def _select_candidate(connection: sqlite3.Connection, candidate_id: str) -> IrCandidate | None:
    row = connection.execute(
        f"SELECT {','.join(_CANDIDATE_COLUMNS)} FROM ir_approval_candidates WHERE candidate_id=?",  # nosec B608 -- fixed internal column tuple; all values remain bound
        (candidate_id,),
    ).fetchone()
    return None if row is None else _candidate_from_row(row)


def get_candidate(connection: sqlite3.Connection, candidate_id: str) -> IrCandidate | None:
    """Return one immutable candidate by its validated content identity."""

    _validate_sha256(candidate_id)
    return _select_candidate(connection, candidate_id)


def get_candidate_by_request_id(
    connection: sqlite3.Connection, request_id: str
) -> IrCandidate | None:
    """Return one immutable candidate by its stable caller replay identity."""

    normalized = request_id.strip()
    if not normalized:
        raise ValueError("request_id cannot be blank")
    row = connection.execute(
        f"SELECT {','.join(_CANDIDATE_COLUMNS)} FROM ir_approval_candidates "  # nosec B608 -- fixed internal column tuple; all values remain bound
        "WHERE request_id=?",
        (normalized,),
    ).fetchone()
    return None if row is None else _candidate_from_row(row)


def authorize_current_candidate(candidate: IrCandidate) -> None:
    """Fail closed unless an immutable candidate still matches current issuer policy."""

    _current_policy(candidate)


def _current_policy(candidate: IrCandidate) -> IssuerAcquisitionPolicy:
    try:
        policy = issuer_policy(candidate.issuer_id)
    except ValueError as exc:
        raise IrAuthorizationError("candidate issuer policy is unavailable") from exc
    if candidate.ticker.casefold() not in {alias.casefold() for alias in policy.ticker_aliases}:
        raise IrAuthorizationError("candidate ticker no longer matches issuer policy")
    if candidate.issuer_policy_sha256 != policy.policy_sha256:
        raise IrAuthorizationError("candidate issuer policy hash is stale")
    if candidate.authority_url != policy.ir.authority_url:
        raise IrAuthorizationError("candidate authority URL no longer matches issuer policy")
    if candidate.doc_type not in policy.ir.admitted_doc_types:
        raise IrAuthorizationError("candidate document type is not currently authorized")
    if not ir_url_is_authorized(policy.ir, candidate.candidate_url):
        raise IrAuthorizationError("candidate URL is not currently authorized")
    return policy


def _catalog_entry_and_observation(
    request: IrCandidateRequest,
) -> tuple[IrCatalogEntry, IrSourceObservation, DocType]:
    catalog = request.catalog
    try:
        policy = issuer_policy(catalog.issuer_id)
    except ValueError as exc:
        raise IrAuthorizationError("catalog issuer policy is unavailable") from exc
    if request.ticker.casefold() not in {alias.casefold() for alias in policy.ticker_aliases}:
        raise IrAuthorizationError("ticker does not match the catalog issuer policy")
    if catalog.issuer_policy_sha256 != policy.policy_sha256:
        raise IrAuthorizationError("catalog policy hash does not match current issuer policy")
    if catalog.authority_url != policy.ir.authority_url:
        raise IrAuthorizationError("catalog authority URL does not match issuer policy")
    matches = tuple(entry for entry in catalog.entries if entry.url == request.candidate_url)
    if len(matches) != 1:
        raise IrAuthorizationError("candidate URL is not one exact catalog entry")
    entry = matches[0]
    if entry.disposition not in {
        CatalogDisposition.IR_DOCUMENT,
        CatalogDisposition.TRANSCRIPT_CANDIDATE,
    }:
        raise IrAuthorizationError("catalog entry is not an admissible IR document candidate")
    if entry.doc_type is None or entry.doc_type not in policy.ir.admitted_doc_types:
        raise IrAuthorizationError("catalog document type is not authorized")
    doc_type = entry.doc_type
    if not ir_url_is_authorized(policy.ir, entry.url):
        raise IrAuthorizationError("catalog candidate URL is outside the approved endpoint policy")
    observations = tuple(
        observation
        for observation in catalog.observations
        if observation.observation_key == entry.observation_key
    )
    if len(observations) != 1 or observations[0].quarter_end != entry.quarter_end:
        raise IrAuthorizationError("catalog entry is not bound to one exact source observation")
    return entry, observations[0], doc_type


def persist_candidate(
    connection: sqlite3.Connection, request: IrCandidateRequest
) -> CandidateWriteResult:
    """Append one catalog-bound candidate; accept only an exact request replay."""

    request = IrCandidateRequest.model_validate(request.model_dump())
    entry, observation, doc_type = _catalog_entry_and_observation(request)
    request_sha256 = _model_sha256(request)
    candidate_id = _sha256_text(
        _canonical_json(
            {
                "catalog_sha256": request.catalog.catalog_sha256,
                "candidate_url": entry.url,
                "issuer_id": request.catalog.issuer_id,
                "observation_raw_sha256": observation.raw_sha256,
                "quarter_end": entry.quarter_end.isoformat(),
            }
        )
    )
    evidence_json, evidence_sha256 = _evidence_payload(request.evidence)
    candidate = IrCandidate(
        candidate_id=candidate_id,
        request_id=request.request_id,
        request_sha256=request_sha256,
        issuer_id=request.catalog.issuer_id,
        ticker=request.ticker,
        catalog_sha256=request.catalog.catalog_sha256,
        issuer_policy_sha256=request.catalog.issuer_policy_sha256,
        authority_url=request.catalog.authority_url,
        quarter_end=entry.quarter_end,
        title=entry.title,
        candidate_url=entry.url,
        disposition=entry.disposition,
        doc_type=doc_type,
        observation_key=entry.observation_key,
        observation_raw_sha256=observation.raw_sha256,
        evidence_locator=entry.evidence_locator,
        recorded_by=request.recorded_by,
        recorded_at=request.recorded_at,
        reason=request.reason,
        evidence=request.evidence,
        evidence_sha256=evidence_sha256,
    )
    values = (
        candidate.candidate_id,
        candidate.request_id,
        candidate.request_sha256,
        candidate.issuer_id,
        candidate.ticker,
        candidate.catalog_sha256,
        candidate.issuer_policy_sha256,
        candidate.authority_url,
        candidate.quarter_end.isoformat(),
        candidate.title,
        candidate.candidate_url,
        candidate.disposition.value,
        candidate.doc_type.value,
        candidate.observation_key,
        candidate.observation_raw_sha256,
        candidate.evidence_locator,
        candidate.recorded_by,
        candidate.recorded_at.isoformat(),
        candidate.reason,
        evidence_json,
        candidate.evidence_sha256,
    )
    with _savepoint(connection, "persist_ir_approval_candidate"):
        existing = connection.execute(
            f"SELECT {','.join(_CANDIDATE_COLUMNS)} FROM ir_approval_candidates "  # nosec B608 -- fixed internal column tuple; all values remain bound
            "WHERE candidate_id=? OR request_id=?",
            (candidate.candidate_id, candidate.request_id),
        ).fetchall()
        if existing:
            if len(existing) != 1:
                raise IrApprovalConflictError("candidate identity is split across rows")
            stored = _candidate_from_row(existing[0])
            if stored.request_sha256 != request_sha256 or stored.candidate_id != candidate_id:
                raise IrApprovalConflictError("immutable candidate replay conflict")
            return CandidateWriteResult(outcome="exact_replay", candidate=stored)
        try:
            connection.execute(
                f"INSERT INTO ir_approval_candidates ({','.join(_CANDIDATE_COLUMNS)}) "  # nosec B608 -- fixed internal column tuple; all values remain bound
                f"VALUES ({','.join('?' for _ in _CANDIDATE_COLUMNS)})",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise IrApprovalConflictError("immutable candidate insert conflict") from exc
    return CandidateWriteResult(outcome="created", candidate=candidate)


def get_current_decision(connection: sqlite3.Connection, candidate_id: str) -> IrDecision | None:
    _validate_sha256(candidate_id)
    row = connection.execute(
        f"SELECT {','.join(_DECISION_COLUMNS)} FROM ir_approval_decisions "  # nosec B608 -- fixed internal column tuple; all values remain bound
        "WHERE candidate_id=? ORDER BY revision DESC LIMIT 1",
        (candidate_id,),
    ).fetchone()
    return None if row is None else _decision_from_row(row)


def append_decision(
    connection: sqlite3.Connection, request: IrDecisionRequest
) -> DecisionWriteResult:
    """Append one owner action with request replay and revision CAS protection."""

    request = IrDecisionRequest.model_validate(request.model_dump())
    request_sha256 = _model_sha256(request)
    decision_id = _sha256_text(
        _canonical_json({"request_id": request.request_id, "request_sha256": request_sha256})
    )
    evidence_json, evidence_sha256 = _evidence_payload(request.evidence)
    with _savepoint(connection, "append_ir_approval_decision"):
        existing = connection.execute(
            f"SELECT {','.join(_DECISION_COLUMNS)} FROM ir_approval_decisions "  # nosec B608 -- fixed internal column tuple; all values remain bound
            "WHERE decision_id=? OR request_id=?",
            (decision_id, request.request_id),
        ).fetchall()
        if existing:
            if len(existing) != 1:
                raise IrApprovalConflictError("decision identity is split across rows")
            stored = _decision_from_row(existing[0])
            if stored.request_sha256 != request_sha256 or stored.decision_id != decision_id:
                raise IrApprovalConflictError("immutable decision replay conflict")
            return DecisionWriteResult(outcome="exact_replay", decision=stored)

        candidate = _select_candidate(connection, request.candidate_id)
        if candidate is None:
            raise IrAuthorizationError("decision candidate does not exist")
        policy = _current_policy(candidate)
        current = get_current_decision(connection, request.candidate_id)
        current_revision = 0 if current is None else current.revision
        if request.expected_revision != current_revision:
            raise IrApprovalConflictError(
                f"candidate revision conflict: expected {request.expected_revision}, current {current_revision}"
            )
        if request.action is DecisionAction.SELECT_EXACT:
            if current is None or current.action is not DecisionAction.APPROVE:
                raise IrAuthorizationError(
                    "exact selection requires the current revision to approve"
                )
            if request.selected_url is None or not ir_url_is_authorized(
                policy.ir, request.selected_url
            ):
                raise IrAuthorizationError(
                    "owner selection is outside the current approved endpoint policy"
                )
            if request.selected_doc_type not in policy.ir.admitted_doc_types:
                raise IrAuthorizationError(
                    "owner selection document type is not currently authorized"
                )
        revision = current_revision + 1
        supersedes = None if current is None else current.decision_id
        decision = IrDecision(
            decision_id=decision_id,
            request_id=request.request_id,
            request_sha256=request_sha256,
            candidate_id=request.candidate_id,
            action=request.action,
            expected_revision=request.expected_revision,
            revision=revision,
            supersedes_decision_id=supersedes,
            owner_actor=request.owner_actor,
            decided_at=request.decided_at,
            reason=request.reason,
            evidence=request.evidence,
            evidence_sha256=evidence_sha256,
            selected_url=request.selected_url,
            selected_doc_type=request.selected_doc_type,
            selected_content_sha256=request.selected_content_sha256,
        )
        values = (
            decision.decision_id,
            decision.request_id,
            decision.request_sha256,
            decision.candidate_id,
            decision.action.value,
            decision.expected_revision,
            decision.revision,
            decision.supersedes_decision_id,
            decision.owner_actor,
            decision.decided_at.isoformat(),
            decision.reason,
            evidence_json,
            decision.evidence_sha256,
            decision.selected_url,
            None if decision.selected_doc_type is None else decision.selected_doc_type.value,
            decision.selected_content_sha256,
        )
        try:
            connection.execute(
                f"INSERT INTO ir_approval_decisions ({','.join(_DECISION_COLUMNS)}) "  # nosec B608 -- fixed internal column tuple; all values remain bound
                f"VALUES ({','.join('?' for _ in _DECISION_COLUMNS)})",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise IrApprovalConflictError("decision revision CAS insert conflict") from exc
    return DecisionWriteResult(outcome="appended", decision=decision)


def verify_admission(
    connection: sqlite3.Connection, proof: IrAdmissionProof
) -> VerifiedIrAdmission:
    """Revalidate a capture proof against the current exact selection; write nothing."""

    proof = IrAdmissionProof.model_validate(proof.model_dump())
    candidate = _select_candidate(connection, proof.candidate_id)
    if candidate is None:
        raise IrAuthorizationError("admission candidate does not exist")
    current = get_current_decision(connection, proof.candidate_id)
    if (
        current is None
        or current.action is not DecisionAction.SELECT_EXACT
        or current.decision_id != proof.selection_decision_id
    ):
        raise IrAuthorizationError("admission requires the current exact selection")
    policy = _current_policy(candidate)
    if current.selected_url is None or not ir_url_is_authorized(policy.ir, current.selected_url):
        raise IrAuthorizationError("exact selection URL is not currently authorized")
    if current.selected_doc_type not in policy.ir.admitted_doc_types:
        raise IrAuthorizationError("exact selection document type is not currently authorized")
    if proof.issuer_id != candidate.issuer_id or proof.ticker != candidate.ticker:
        raise IrAuthorizationError("admission issuer identity does not match candidate")
    if proof.quarter_end != candidate.quarter_end:
        raise IrAuthorizationError("admission reporting period does not match candidate")
    if proof.doc_type is not current.selected_doc_type:
        raise IrAuthorizationError("admission document type does not match exact selection")
    if proof.canonical_url != current.selected_url:
        raise IrAuthorizationError("admission canonical URL does not match exact selection")
    if proof.final_url != proof.canonical_url:
        raise IrAuthorizationError("admission redirect escaped the exact selected URL")
    if proof.captured_content_sha256 != current.selected_content_sha256:
        raise IrAuthorizationError("captured bytes do not match the owner-selected content hash")
    if proof.issuer_policy_sha256 != candidate.issuer_policy_sha256:
        raise IrAuthorizationError("admission policy hash does not match candidate")
    if proof.catalog_sha256 != candidate.catalog_sha256:
        raise IrAuthorizationError("admission catalog hash does not match candidate")
    if proof.observation_key != candidate.observation_key:
        raise IrAuthorizationError("admission observation key does not match candidate")
    if proof.observation_raw_sha256 != candidate.observation_raw_sha256:
        raise IrAuthorizationError("admission observation hash does not match candidate")
    return VerifiedIrAdmission(
        candidate_id=candidate.candidate_id,
        selection_decision_id=current.decision_id,
        captured_content_sha256=proof.captured_content_sha256,
        captured_at=proof.captured_at,
    )


__all__ = [
    "CandidateWriteResult",
    "DecisionAction",
    "DecisionWriteResult",
    "EvidenceReference",
    "IrAdmissionProof",
    "IrApprovalConflictError",
    "IrApprovalError",
    "IrAuthorizationError",
    "IrCandidate",
    "IrCandidateRequest",
    "IrDecision",
    "IrDecisionRequest",
    "VerifiedIrAdmission",
    "append_decision",
    "authorize_current_candidate",
    "get_candidate",
    "get_candidate_by_request_id",
    "get_current_decision",
    "persist_candidate",
    "verify_admission",
]
