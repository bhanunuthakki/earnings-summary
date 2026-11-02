"""Retained schemas and seal verification for saved SEC delta plans.

New plan creation is retired. These models preserve the existing saved-plan
format and current admission checks at the original import path.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.data_backbone_rehearsal import DatabaseStorageIdentity
from runtime.deterministic import content_sha256_ascii as _digest
from schema_compat import expected_head

PLANNER_VERSION = "sec-delta-planner.v2"
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
AuthorizationKind = Literal["AUTOMATIC_FULL", "OWNER_REQUEST"]
AuthorizationAttestation = Literal["NOT_APPLICABLE", "CALLER_ATTESTED"]
ReceiptStatus = Literal["READY", "BLOCKED"]
RosterSelection = Literal[
    "AUTOMATIC_FULL",
    "OWNER_REQUESTED_EVALUATION",
    "EXCLUDED_LIST_TYPE",
    "EXCLUDED_IDENTITY",
    "EXCLUDED_INSTRUMENT",
    "EXCLUDED_ARCHIVED",
    "EXCLUDED_NO_SEC_FILER",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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
    list_type: Literal["portfolio", "evaluation", "watchlist"]
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
    schema_version: Literal["sec_delta_plan_receipt.v2"]
    planner_version: Literal["sec-delta-planner.v2"]
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
