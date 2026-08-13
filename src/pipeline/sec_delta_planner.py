"""Deterministic, read-only planning for tiered SEC delta refreshes.

This module never performs collection. It commits the exact tracked-company
roster, owner authorizations, source-policy dependencies, and current sealed
inventory state into a self-verifying receipt that downstream execution can
use without turning an unscoped scheduled sweep into implicit authorization.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pipeline import source_policy as source_policy_contract
from pipeline.sec_xbrl import CIK_MAP, NO_SEC_FILERS
from pipeline.source_policy import (
    POLICY_VERSION,
    ArtifactKind,
    CollectionSource,
    decision_for,
    issuer_policy,
    mode_for_role,
)
from provenance.data_backbone_rehearsal import (
    DatabaseStorageIdentity,
    RehearsalError,
    database_storage_identity,
    require_sidecar_free_database,
)
from schema_compat import expected_head
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PLANNER_VERSION = "sec-delta-planner.v1"
SUPPORTED_ALEMBIC_REVISION = expected_head()

TaskKind = Literal[
    "COMPANYFACTS_DELTA",
    "NATIVE_INVENTORY_PACKAGES",
    "NATIVE_FILING_SECTIONS",
]
TaskStatus = Literal[
    "READY",
    "BLOCKED_MISSING_CIK",
    "BLOCKED_ISSUER_POLICY",
    "BLOCKED_EVIDENCE_LINKAGE",
    "BLOCKED_SOURCE_POLICY",
]
DependencyState = Literal["SATISFIED", "MISSING", "NOT_APPLICABLE"]
AuthorizationKind = Literal["AUTOMATIC_PORTFOLIO", "OWNER_REQUEST"]
AuthorizationAttestation = Literal["NOT_APPLICABLE", "CALLER_ATTESTED"]
ReceiptStatus = Literal["READY", "BLOCKED"]
RosterSelection = Literal[
    "AUTOMATIC_PORTFOLIO",
    "OWNER_REQUESTED_EVALUATION",
    "EXCLUDED_EVALUATION_REQUEST_REQUIRED",
    "EXCLUDED_LIST_TYPE",
    "EXCLUDED_ARCHIVED",
    "EXCLUDED_NO_SEC_FILER",
]

_SCHEMA: dict[str, frozenset[str]] = {
    "tracked_companies": frozenset({"ticker", "list_type", "archived_at"}),
    "source_inventory_snapshots": frozenset(
        {
            "snapshot_id",
            "inventory_key",
            "revision",
            "issuer_id",
            "ticker",
            "source_kind",
            "recorded_at",
        }
    ),
    "source_inventory_snapshot_seals": frozenset({"snapshot_id", "completion_status", "sealed_at"}),
    "expected_documents": frozenset({"expected_document_id", "snapshot_id", "source_kind"}),
    "source_coverage_assessments": frozenset(
        {
            "expected_document_id",
            "revision",
            "coverage_status",
            "recorded_at",
        }
    ),
}
_CAPTURED_COVERAGE = ("captured", "extracted", "indexed")


class SnapshotSafetyError(ValueError):
    """The planner input is not a closed, immutable SQLite snapshot."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _database_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvaluationAuthorization(_FrozenModel):
    ticker: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9][A-Z0-9.-]*$")
    owner_request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("owner_request_id", mode="before")
    @classmethod
    def _normalize_owner_request(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @classmethod
    def parse(cls, value: str) -> EvaluationAuthorization:
        ticker, separator, owner_request_id = value.partition(":")
        if not separator:
            raise ValueError("evaluation request must be TICKER:OWNER_REQUEST_ID")
        return cls(ticker=ticker, owner_request_id=owner_request_id)


class SecDeltaPlannerRequest(_FrozenModel):
    database_path: Path
    as_of: date
    evaluation_requests: tuple[EvaluationAuthorization, ...] = ()

    @field_validator("database_path")
    @classmethod
    def _database_exists(cls, value: Path) -> Path:
        resolved = value.resolve()
        if not resolved.is_file():
            raise ValueError("database_path must be an existing file")
        return resolved

    @field_validator("evaluation_requests")
    @classmethod
    def _canonical_requests(
        cls, value: tuple[EvaluationAuthorization, ...]
    ) -> tuple[EvaluationAuthorization, ...]:
        return tuple(sorted(value, key=lambda item: (item.ticker, item.owner_request_id)))


class DependencyStatus(_FrozenModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    status: DependencyState
    detail: str = Field(min_length=1, max_length=256)


class NativeInventoryState(_FrozenModel):
    inventory_key: str = Field(min_length=1, max_length=256)
    current_sealed_snapshot_id: str | None = None
    current_sealed_revision: int | None = Field(default=None, gt=0)
    next_inventory_revision: int = Field(gt=0)
    sealed_native_document_count: int = Field(ge=0)
    outstanding_native_document_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent_counts(self) -> Self:
        if self.outstanding_native_document_count > self.sealed_native_document_count:
            raise ValueError("outstanding native documents cannot exceed sealed documents")
        if (self.current_sealed_snapshot_id is None) != (self.current_sealed_revision is None):
            raise ValueError("sealed snapshot id and revision must be present together")
        return self


class SecDeltaTask(_FrozenModel):
    task_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(min_length=1, max_length=32)
    cik: str | None = Field(default=None, pattern=r"^[0-9]{10}$")
    kind: TaskKind
    status: TaskStatus
    reason_code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    authorization: Literal["AUTOMATIC", "OWNER_REQUEST"]
    authorization_attestation: AuthorizationAttestation
    owner_request_id: str | None = None
    source_policy_version: str = Field(min_length=1, max_length=64)
    source_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issuer_policy_version: str | None = None
    issuer_policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dependencies: tuple[DependencyStatus, ...]


class TickerPlan(_FrozenModel):
    ticker: str = Field(min_length=1, max_length=32)
    list_type: Literal["portfolio", "evaluation"]
    authorization: AuthorizationKind
    authorization_attestation: AuthorizationAttestation
    owner_request_id: str | None = None
    cik: str | None = Field(default=None, pattern=r"^[0-9]{10}$")
    inventory: NativeInventoryState
    tasks: tuple[SecDeltaTask, ...] = Field(min_length=3, max_length=3)


class RosterEntry(_FrozenModel):
    ticker: str = Field(min_length=1, max_length=32)
    list_type: str = Field(min_length=1, max_length=32)
    archived: bool
    selection: RosterSelection
    owner_request_id: str | None = None


class AuthorizationRejection(_FrozenModel):
    ticker: str = Field(min_length=1, max_length=32)
    owner_request_id: str = Field(min_length=1, max_length=128)
    reason_code: Literal[
        "ticker_is_not_active_evaluation",
        "conflicting_owner_request_ids",
    ]


class SecDeltaPlanReceipt(_FrozenModel):
    schema_version: Literal["sec_delta_plan_receipt.v1"]
    planner_version: Literal["sec-delta-planner.v1"]
    as_of: date
    network_policy: Literal["FORBIDDEN"]
    status: ReceiptStatus
    alembic_revision: str = Field(min_length=1, max_length=128)
    database_path: str = Field(min_length=1)
    database_storage_identity: DatabaseStorageIdentity
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_total_changes: Literal[0]
    source_policy_version: str = Field(min_length=1, max_length=64)
    source_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    roster: tuple[RosterEntry, ...]
    roster_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ticker_plans: tuple[TickerPlan, ...]
    authorization_rejections: tuple[AuthorizationRejection, ...]
    missing_schema: tuple[str, ...]
    blocked_task_count: int = Field(ge=0)
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def computed_roster_sha256(self) -> str:
        return _digest([item.model_dump(mode="json") for item in self.roster])

    def computed_receipt_sha256(self) -> str:
        return _digest(self.model_dump(mode="json", exclude={"receipt_sha256"}))

    @model_validator(mode="after")
    def _verify_seals(self) -> Self:
        if self.roster_sha256 != self.computed_roster_sha256():
            raise ValueError("roster_sha256 does not seal the exact roster")
        if self.alembic_revision != SUPPORTED_ALEMBIC_REVISION:
            raise ValueError("plan does not use the current supported planner schema revision")
        if len(self.database_storage_identity.entries) != 1:
            raise ValueError("planner receipt requires a sidecar-free database identity")
        if self.database_sha256 != self.database_storage_identity.entries[0].content_sha256:
            raise ValueError("database_sha256 must match the sealed main storage entry")
        if self.receipt_sha256 != self.computed_receipt_sha256():
            raise ValueError("receipt_sha256 does not seal the receipt")
        expected_blocked = sum(
            task.status != "READY" for plan in self.ticker_plans for task in plan.tasks
        )
        if self.blocked_task_count != expected_blocked:
            raise ValueError("blocked_task_count does not match planned tasks")
        should_block = bool(
            self.missing_schema or self.authorization_rejections or self.blocked_task_count
        )
        if (self.status == "BLOCKED") != should_block:
            raise ValueError("receipt status does not match blocked work")
        return self


class SecDeltaPlanTerminalReceipt(_FrozenModel):
    """Compact self-sealed stdout receipt for one durable full plan artifact."""

    schema_version: Literal["sec_delta_plan_terminal_receipt.v1"]
    status: ReceiptStatus
    plan_path: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_size_bytes: int = Field(gt=0)
    plan_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_storage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ticker_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    blocked_task_count: int = Field(ge=0)
    blocked_finding_count: int = Field(ge=0)
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def computed_receipt_sha256(self) -> str:
        return _digest(self.model_dump(mode="json", exclude={"receipt_sha256"}))

    @model_validator(mode="after")
    def _verify_seal(self) -> Self:
        if self.receipt_sha256 != self.computed_receipt_sha256():
            raise ValueError("terminal receipt seal is invalid")
        if self.blocked_task_count > self.task_count:
            raise ValueError("blocked task count cannot exceed task count")
        if self.blocked_finding_count < self.blocked_task_count:
            raise ValueError("blocked finding count cannot be below blocked tasks")
        if (self.status == "BLOCKED") != (self.blocked_finding_count > 0):
            raise ValueError("terminal status does not match blocked findings")
        return self


def _schema_gaps(conn: sqlite3.Connection) -> tuple[str, ...]:
    gaps: list[str] = []
    existing_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for table, required_columns in _SCHEMA.items():
        if table not in existing_tables:
            gaps.append(table)
            continue
        columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        gaps.extend(f"{table}.{column}" for column in sorted(required_columns - columns))
    return tuple(sorted(gaps))


def _supported_alembic_revision(conn: sqlite3.Connection) -> str:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    revisions: tuple[str, ...] = ()
    if has_table is not None:
        revisions = tuple(
            str(row[0]) for row in conn.execute("SELECT version_num FROM alembic_version")
        )
    if revisions != (SUPPORTED_ALEMBIC_REVISION,):
        raise ValueError(
            "database must contain exactly one supported planner schema revision: "
            f"{SUPPORTED_ALEMBIC_REVISION}"
        )
    return SUPPORTED_ALEMBIC_REVISION


def _source_policy_sha256() -> str:
    return _database_sha256(Path(source_policy_contract.__file__).resolve(strict=True))


def _require_closed_snapshot(path: Path) -> DatabaseStorageIdentity:
    try:
        require_sidecar_free_database(path)
        identity = database_storage_identity(path)
    except (OSError, RehearsalError, ValueError) as exc:
        raise SnapshotSafetyError(
            "SEC delta planner requires a closed sidecar-free immutable snapshot"
        ) from exc
    if len(identity.entries) != 1:
        raise SnapshotSafetyError(
            "SEC delta planner requires a closed sidecar-free immutable snapshot"
        )
    if identity.entries[0].link_count != 1:
        raise SnapshotSafetyError(
            "immutable database main storage must have exactly one filesystem link"
        )
    return identity


def _read_roster(
    conn: sqlite3.Connection,
    authorizations: dict[str, str],
) -> tuple[tuple[RosterEntry, ...], dict[str, tuple[str, str | None]]]:
    rows = conn.execute(
        "SELECT ticker,list_type,archived_at FROM tracked_companies "
        "ORDER BY UPPER(ticker),list_type,COALESCE(archived_at,'')"
    ).fetchall()
    roster: list[RosterEntry] = []
    selected: dict[str, tuple[str, str | None]] = {}
    for raw_ticker, raw_list_type, archived_at in rows:
        ticker = str(raw_ticker).strip().upper()
        list_type = str(raw_list_type).strip().lower()
        owner_request_id = authorizations.get(ticker)
        if archived_at is not None:
            selection: RosterSelection = "EXCLUDED_ARCHIVED"
        elif ticker in NO_SEC_FILERS:
            selection = "EXCLUDED_NO_SEC_FILER"
        elif list_type == "portfolio":
            selection = "AUTOMATIC_PORTFOLIO"
            selected[ticker] = (list_type, None)
        elif list_type == "evaluation" and owner_request_id is not None:
            selection = "OWNER_REQUESTED_EVALUATION"
            selected[ticker] = (list_type, owner_request_id)
        elif list_type == "evaluation":
            selection = "EXCLUDED_EVALUATION_REQUEST_REQUIRED"
        else:
            selection = "EXCLUDED_LIST_TYPE"
        roster.append(
            RosterEntry(
                ticker=ticker,
                list_type=list_type,
                archived=archived_at is not None,
                selection=selection,
                owner_request_id=(
                    owner_request_id if selection == "OWNER_REQUESTED_EVALUATION" else None
                ),
            )
        )
    return tuple(roster), selected


def _authorize_evaluations(
    conn: sqlite3.Connection,
    requests: tuple[EvaluationAuthorization, ...],
) -> tuple[dict[str, str], tuple[AuthorizationRejection, ...]]:
    active_evaluations = {
        str(row[0]).strip().upper()
        for row in conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE list_type='evaluation' AND archived_at IS NULL"
        ).fetchall()
    }
    grouped: dict[str, set[str]] = {}
    for request in requests:
        grouped.setdefault(request.ticker, set()).add(request.owner_request_id)
    accepted: dict[str, str] = {}
    rejected: list[AuthorizationRejection] = []
    for ticker, owner_ids in sorted(grouped.items()):
        if ticker not in active_evaluations:
            rejected.extend(
                AuthorizationRejection(
                    ticker=ticker,
                    owner_request_id=owner_id,
                    reason_code="ticker_is_not_active_evaluation",
                )
                for owner_id in sorted(owner_ids)
            )
        elif len(owner_ids) != 1:
            rejected.extend(
                AuthorizationRejection(
                    ticker=ticker,
                    owner_request_id=owner_id,
                    reason_code="conflicting_owner_request_ids",
                )
                for owner_id in sorted(owner_ids)
            )
        else:
            accepted[ticker] = next(iter(owner_ids))
    return accepted, tuple(rejected)


def _cutoff(as_of: date) -> str:
    next_day = as_of + timedelta(days=1)
    return datetime.combine(next_day, time.min, tzinfo=UTC).isoformat()


def _inventory_state(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    cik: str | None,
    as_of: date,
) -> NativeInventoryState:
    issuer_id = f"sec-cik-{cik}" if cik is not None else f"sec-ticker-{ticker.lower()}"
    inventory_key = f"{issuer_id}:sec-submissions"
    cutoff = _cutoff(as_of)
    scope_parameters = (ticker, issuer_id, cutoff)
    max_revision_row = conn.execute(
        "SELECT MAX(revision) FROM source_inventory_snapshots "
        "WHERE source_kind='sec_submissions' AND (UPPER(ticker)=? OR issuer_id=?) "
        "AND datetime(recorded_at)<datetime(?)",
        scope_parameters,
    ).fetchone()
    max_revision = (
        0 if max_revision_row is None or max_revision_row[0] is None else int(max_revision_row[0])
    )
    sealed = conn.execute(
        "SELECT inventory.snapshot_id,inventory.revision "
        "FROM source_inventory_snapshots AS inventory "
        "JOIN source_inventory_snapshot_seals AS seal "
        "ON seal.snapshot_id=inventory.snapshot_id "
        "WHERE inventory.source_kind='sec_submissions' "
        "AND (UPPER(inventory.ticker)=? OR inventory.issuer_id=?) "
        "AND datetime(inventory.recorded_at)<datetime(?) "
        "AND datetime(seal.sealed_at)<datetime(?) "
        "AND seal.completion_status='complete' "
        "ORDER BY inventory.revision DESC,inventory.snapshot_id DESC LIMIT 1",
        (ticker, issuer_id, cutoff, cutoff),
    ).fetchone()
    if sealed is None:
        return NativeInventoryState(
            inventory_key=inventory_key,
            current_sealed_snapshot_id=None,
            current_sealed_revision=None,
            next_inventory_revision=max_revision + 1,
            sealed_native_document_count=0,
            outstanding_native_document_count=0,
        )
    snapshot_id = str(sealed[0])
    counts = conn.execute(
        "SELECT COUNT(*),COALESCE(SUM(CASE WHEN EXISTS ("
        "SELECT 1 FROM source_coverage_assessments AS assessment "
        "WHERE assessment.expected_document_id=expected.expected_document_id "
        "AND datetime(assessment.recorded_at)<datetime(?) "
        "AND assessment.coverage_status IN (?,?,?) "
        "AND NOT EXISTS (SELECT 1 FROM source_coverage_assessments AS newer "
        "WHERE newer.expected_document_id=assessment.expected_document_id "
        "AND datetime(newer.recorded_at)<datetime(?) "
        "AND newer.revision>assessment.revision)) THEN 0 ELSE 1 END),0) "
        "FROM expected_documents AS expected "
        "WHERE expected.snapshot_id=? AND expected.source_kind='sec_filing'",
        (cutoff, *_CAPTURED_COVERAGE, cutoff, snapshot_id),
    ).fetchone()
    sealed_count = 0 if counts is None else int(counts[0])
    outstanding_count = 0 if counts is None else int(counts[1])
    return NativeInventoryState(
        inventory_key=inventory_key,
        current_sealed_snapshot_id=snapshot_id,
        current_sealed_revision=int(sealed[1]),
        next_inventory_revision=max_revision + 1,
        sealed_native_document_count=sealed_count,
        outstanding_native_document_count=outstanding_count,
    )


def _dependency(name: str, status: DependencyState, detail: str) -> DependencyStatus:
    return DependencyStatus(name=name, status=status, detail=detail)


def _task(
    *,
    ticker: str,
    cik: str | None,
    kind: TaskKind,
    status: TaskStatus,
    reason_code: str,
    authorization: Literal["AUTOMATIC", "OWNER_REQUEST"],
    authorization_attestation: AuthorizationAttestation,
    owner_request_id: str | None,
    policy_version: str | None,
    policy_sha256: str | None,
    dependencies: tuple[DependencyStatus, ...],
    as_of: date,
    next_inventory_revision: int,
    source_policy_sha256: str,
) -> SecDeltaTask:
    identity = {
        "as_of": as_of.isoformat(),
        "authorization": authorization,
        "authorization_attestation": authorization_attestation,
        "cik": cik,
        "kind": kind,
        "next_inventory_revision": next_inventory_revision,
        "owner_request_id": owner_request_id,
        "source_policy_version": POLICY_VERSION,
        "source_policy_sha256": source_policy_sha256,
        "ticker": ticker,
    }
    task_id = f"sec-delta:{kind.lower()}:{ticker}:{_digest(identity)[:16]}"
    return SecDeltaTask(
        task_id=task_id,
        ticker=ticker,
        cik=cik,
        kind=kind,
        status=status,
        reason_code=reason_code,
        authorization=authorization,
        authorization_attestation=authorization_attestation,
        owner_request_id=owner_request_id,
        source_policy_version=POLICY_VERSION,
        source_policy_sha256=source_policy_sha256,
        issuer_policy_version=policy_version,
        issuer_policy_sha256=policy_sha256,
        dependencies=dependencies,
    )


def _ticker_plan(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    list_type: str,
    owner_request_id: str | None,
    as_of: date,
    source_policy_sha256: str,
) -> TickerPlan:
    cik = CIK_MAP.get(ticker)
    authorization: Literal["AUTOMATIC", "OWNER_REQUEST"] = (
        "OWNER_REQUEST" if owner_request_id is not None else "AUTOMATIC"
    )
    authorization_attestation: AuthorizationAttestation = (
        "CALLER_ATTESTED" if owner_request_id is not None else "NOT_APPLICABLE"
    )
    collection_mode = mode_for_role(list_type)
    requested = owner_request_id is not None
    companyfacts_decision = decision_for(
        list_type,
        CollectionSource.SEC,
        ArtifactKind.COMPANY_FACTS,
        requested=requested,
    )
    native_decision = decision_for(
        list_type,
        CollectionSource.SEC,
        ArtifactKind.FILING_PACKAGE,
        requested=requested,
    )
    sections_decision = decision_for(
        list_type,
        CollectionSource.SEC,
        ArtifactKind.FILING_SECTION,
        requested=requested,
    )
    inventory = _inventory_state(conn, ticker=ticker, cik=cik, as_of=as_of)
    policy_version: str | None = None
    policy_sha256: str | None = None
    reviewed_policy = False
    if cik is not None:
        try:
            policy = issuer_policy(ticker)
        except ValueError:
            pass
        else:
            reviewed_policy = policy.issuer_id == f"sec-cik-{cik}"
            if reviewed_policy:
                policy_version = policy.policy_version
                policy_sha256 = policy.policy_sha256

    common = (
        _dependency("ACTIVE_TRACKED_TICKER", "SATISFIED", f"active {list_type} ticker"),
        _dependency(
            "CIK_MAPPING",
            "SATISFIED" if cik is not None else "MISSING",
            cik if cik is not None else "no exact curated CIK mapping",
        ),
        _dependency("EXPLICIT_TICKER_SCOPE", "SATISFIED", ticker),
        _dependency(
            "OWNER_AUTHORIZATION",
            "SATISFIED" if owner_request_id is not None else "NOT_APPLICABLE",
            (
                f"caller_attested:{owner_request_id}"
                if owner_request_id is not None
                else "automatic portfolio policy"
            ),
        ),
        _dependency(
            "SOURCE_POLICY_AUTHORIZATION",
            "SATISFIED" if companyfacts_decision.allowed else "MISSING",
            f"{collection_mode.value}:{companyfacts_decision.reason.value}",
        ),
    )
    if cik is None:
        companyfacts_status: TaskStatus = "BLOCKED_MISSING_CIK"
        companyfacts_reason = "missing_exact_cik_mapping"
    elif not companyfacts_decision.allowed:
        companyfacts_status = "BLOCKED_SOURCE_POLICY"
        companyfacts_reason = "source_policy_authorization_required"
    else:
        companyfacts_status = "READY"
        companyfacts_reason = "explicit_ticker_companyfacts_ready"
    companyfacts = _task(
        ticker=ticker,
        cik=cik,
        kind="COMPANYFACTS_DELTA",
        status=companyfacts_status,
        reason_code=companyfacts_reason,
        authorization=authorization,
        authorization_attestation=authorization_attestation,
        owner_request_id=owner_request_id,
        policy_version=None,
        policy_sha256=None,
        dependencies=common,
        as_of=as_of,
        next_inventory_revision=inventory.next_inventory_revision,
        source_policy_sha256=source_policy_sha256,
    )
    native_dependencies = (
        *common,
        _dependency(
            "REVIEWED_ISSUER_POLICY",
            "SATISFIED" if reviewed_policy else "MISSING",
            policy_sha256 if policy_sha256 is not None else "no exact reviewed issuer policy",
        ),
        _dependency(
            "NATIVE_SOURCE_POLICY_AUTHORIZATION",
            "SATISFIED" if native_decision.allowed else "MISSING",
            f"{collection_mode.value}:{native_decision.reason.value}",
        ),
    )
    if cik is None:
        native_status: TaskStatus = "BLOCKED_MISSING_CIK"
        native_reason = "missing_exact_cik_mapping"
    elif not native_decision.allowed:
        native_status = "BLOCKED_SOURCE_POLICY"
        native_reason = "source_policy_authorization_required"
    elif not reviewed_policy:
        native_status = "BLOCKED_ISSUER_POLICY"
        native_reason = "exact_reviewed_issuer_policy_required"
    else:
        native_status = "READY"
        native_reason = "reviewed_policy_inventory_package_ready"
    native = _task(
        ticker=ticker,
        cik=cik,
        kind="NATIVE_INVENTORY_PACKAGES",
        status=native_status,
        reason_code=native_reason,
        authorization=authorization,
        authorization_attestation=authorization_attestation,
        owner_request_id=owner_request_id,
        policy_version=policy_version,
        policy_sha256=policy_sha256,
        dependencies=native_dependencies,
        as_of=as_of,
        next_inventory_revision=inventory.next_inventory_revision,
        source_policy_sha256=source_policy_sha256,
    )
    section_dependencies = (
        *native_dependencies,
        _dependency(
            "EVIDENCE_DOCUMENT_LINKAGE",
            "MISSING",
            "filing sections are not yet linked to immutable evidence document versions",
        ),
        _dependency(
            "SECTION_SOURCE_POLICY_AUTHORIZATION",
            "SATISFIED" if sections_decision.allowed else "MISSING",
            f"{collection_mode.value}:{sections_decision.reason.value}",
        ),
    )
    sections = _task(
        ticker=ticker,
        cik=cik,
        kind="NATIVE_FILING_SECTIONS",
        status="BLOCKED_EVIDENCE_LINKAGE",
        reason_code="filing_section_evidence_document_linkage_required",
        authorization=authorization,
        authorization_attestation=authorization_attestation,
        owner_request_id=owner_request_id,
        policy_version=policy_version,
        policy_sha256=policy_sha256,
        dependencies=section_dependencies,
        as_of=as_of,
        next_inventory_revision=inventory.next_inventory_revision,
        source_policy_sha256=source_policy_sha256,
    )
    return TickerPlan(
        ticker=ticker,
        list_type=cast(Literal["portfolio", "evaluation"], list_type),
        authorization=("OWNER_REQUEST" if owner_request_id is not None else "AUTOMATIC_PORTFOLIO"),
        authorization_attestation=authorization_attestation,
        owner_request_id=owner_request_id,
        cik=cik,
        inventory=inventory,
        tasks=(companyfacts, native, sections),
    )


def build_sec_delta_plan(request: SecDeltaPlannerRequest) -> SecDeltaPlanReceipt:
    """Build one deterministic plan without mutating SQLite or using the network."""

    source_before = _require_closed_snapshot(request.database_path)
    database_before = source_before.entries[0].content_sha256
    source_policy_sha256 = _source_policy_sha256()
    conn = connect_sqlite(
        request.database_path,
        role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
    )
    try:
        initial_total_changes = conn.total_changes
        alembic_revision = _supported_alembic_revision(conn)
        missing_schema = _schema_gaps(conn)
        request_sha256 = _digest(
            {
                "as_of": request.as_of.isoformat(),
                "alembic_revision": alembic_revision,
                "evaluation_requests": [
                    item.model_dump(mode="json") for item in request.evaluation_requests
                ],
                "source_policy_sha256": source_policy_sha256,
                "source_policy_version": POLICY_VERSION,
            }
        )
        if missing_schema:
            roster: tuple[RosterEntry, ...] = ()
            plans: tuple[TickerPlan, ...] = ()
            rejections: tuple[AuthorizationRejection, ...] = ()
        else:
            authorizations, rejections = _authorize_evaluations(conn, request.evaluation_requests)
            roster, selected = _read_roster(conn, authorizations)
            plans = tuple(
                _ticker_plan(
                    conn,
                    ticker=ticker,
                    list_type=list_type,
                    owner_request_id=owner_request_id,
                    as_of=request.as_of,
                    source_policy_sha256=source_policy_sha256,
                )
                for ticker, (list_type, owner_request_id) in sorted(selected.items())
            )
        total_changes = conn.total_changes - initial_total_changes
        if total_changes != 0:
            raise RuntimeError("SEC delta planner mutated its read-only database connection")
    finally:
        conn.close()
    source_after = _require_closed_snapshot(request.database_path)
    if source_after != source_before:
        raise SnapshotSafetyError("immutable snapshot storage identity changed during planning")
    blocked_task_count = sum(task.status != "READY" for plan in plans for task in plan.tasks)
    roster_sha256 = _digest([item.model_dump(mode="json") for item in roster])
    status: ReceiptStatus = (
        "BLOCKED" if missing_schema or rejections or blocked_task_count else "READY"
    )
    draft = SecDeltaPlanReceipt.model_construct(
        schema_version="sec_delta_plan_receipt.v1",
        planner_version=PLANNER_VERSION,
        as_of=request.as_of,
        network_policy="FORBIDDEN",
        status=status,
        alembic_revision=alembic_revision,
        database_path=str(request.database_path),
        database_storage_identity=source_before,
        database_sha256=database_before,
        database_total_changes=0,
        source_policy_version=POLICY_VERSION,
        source_policy_sha256=source_policy_sha256,
        request_sha256=request_sha256,
        roster=roster,
        roster_sha256=roster_sha256,
        ticker_plans=plans,
        authorization_rejections=rejections,
        missing_schema=missing_schema,
        blocked_task_count=blocked_task_count,
        receipt_sha256="0" * 64,
    )
    payload = draft.model_dump(mode="json")
    payload["receipt_sha256"] = draft.computed_receipt_sha256()
    return SecDeltaPlanReceipt.model_validate_json(_canonical_json(payload))


def write_sec_delta_plan(
    plan: SecDeltaPlanReceipt,
    output_path: Path,
) -> SecDeltaPlanTerminalReceipt:
    """Atomically persist the full sealed plan and return a compact sealed receipt."""

    output = output_path.resolve()
    if any(part.casefold() == ".tmp" for part in output.parts):
        raise ValueError("SEC delta plan output must be a governed durable path outside .tmp")
    database = Path(plan.database_path).resolve()
    storage_paths = (
        database,
        *(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm", "-journal")),
    )
    output_aliases_storage = output in storage_paths or (
        output.exists()
        and any(storage.exists() and os.path.samefile(output, storage) for storage in storage_paths)
    )
    if output_aliases_storage:
        raise ValueError("SEC delta plan output cannot alias SQLite storage")
    storage_before = _require_closed_snapshot(database)
    if storage_before != plan.database_storage_identity:
        raise SnapshotSafetyError("immutable database storage changed since plan construction")
    output.parent.mkdir(parents=True, exist_ok=True)
    plan_bytes = (_canonical_json(plan.model_dump(mode="json", exclude_none=False)) + "\n").encode(
        "utf-8"
    )
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".pending",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(plan_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    stored = output.read_bytes()
    if stored != plan_bytes:
        raise RuntimeError("durable SEC delta plan bytes do not match sealed plan")
    storage_after = _require_closed_snapshot(database)
    if storage_after != plan.database_storage_identity:
        raise SnapshotSafetyError("immutable database storage changed during plan artifact write")
    task_count = sum(len(item.tasks) for item in plan.ticker_plans)
    blocked_finding_count = (
        plan.blocked_task_count + len(plan.authorization_rejections) + len(plan.missing_schema)
    )
    draft = SecDeltaPlanTerminalReceipt.model_construct(
        schema_version="sec_delta_plan_terminal_receipt.v1",
        status=plan.status,
        plan_path=str(output),
        plan_sha256=plan_sha256,
        plan_size_bytes=len(plan_bytes),
        plan_receipt_sha256=plan.receipt_sha256,
        database_storage_sha256=plan.database_storage_identity.aggregate_sha256,
        source_policy_sha256=plan.source_policy_sha256,
        ticker_count=len(plan.ticker_plans),
        task_count=task_count,
        blocked_task_count=plan.blocked_task_count,
        blocked_finding_count=blocked_finding_count,
        receipt_sha256="0" * 64,
    )
    payload = draft.model_dump(mode="json")
    payload["receipt_sha256"] = draft.computed_receipt_sha256()
    return SecDeltaPlanTerminalReceipt.model_validate(payload)
