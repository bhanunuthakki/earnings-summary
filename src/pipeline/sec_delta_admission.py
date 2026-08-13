"""Fail-closed admission for one planned SEC native-inventory task.

Admission verifies authorization parameters only.  It never performs network
work, opens a writer connection, or changes the plan or database snapshot.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pipeline import source_policy as source_policy_contract
from pipeline.sec_delta_planner import SecDeltaPlanReceipt, SecDeltaTask, TickerPlan
from pipeline.sec_xbrl import CIK_MAP
from pipeline.source_policy import (
    POLICY_VERSION,
    ArtifactKind,
    CollectionSource,
    StoredIdentityStatus,
    authorize_collection_target_in_connection,
    issuer_policy,
)
from provenance.data_backbone_rehearsal import (
    DatabaseStorageIdentity,
    RehearsalError,
    database_storage_identity,
    require_sidecar_free_database,
)
from schema_compat import expected_head
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

ADMISSION_VERSION = "sec-delta-native-inventory-admission.v1"


class SecDeltaAdmissionError(ValueError):
    """The planned task cannot be authorized against current exact state."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _current_source_policy_sha256() -> str:
    return _sha256_file(Path(source_policy_contract.__file__).resolve(strict=True))


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SecDeltaAdmissionRequest(_FrozenModel):
    """Exact plan, commitment, task, and closed snapshot to authorize."""

    plan_path: Path
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_path: Path
    task_id: str = Field(min_length=1, max_length=128)

    @field_validator("plan_path", "database_path")
    @classmethod
    def _require_existing_file(cls, value: Path) -> Path:
        resolved = value.resolve()
        if not resolved.is_file():
            raise ValueError("admission inputs must be existing files")
        return resolved


class SecDeltaNativeInventoryAuthorization(_FrozenModel):
    """Self-sealed immutable parameters for a later native inventory CLI."""

    schema_version: Literal["sec_delta_native_inventory_authorization.v1"]
    admission_version: Literal["sec-delta-native-inventory-admission.v1"]
    network_policy: Literal["FORBIDDEN"]
    plan_path: str = Field(min_length=1)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(min_length=1, max_length=32)
    cik: str = Field(pattern=r"^[0-9]{10}$")
    coverage_role: Literal["portfolio", "evaluation"]
    authorization: Literal["AUTOMATIC", "OWNER_REQUEST"]
    authorization_attestation: Literal["NOT_APPLICABLE", "CALLER_ATTESTED"]
    owner_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    inventory_key: str = Field(min_length=1, max_length=256)
    current_inventory_revision: int = Field(ge=0)
    next_inventory_revision: int = Field(gt=0)
    source_policy_version: str = Field(min_length=1, max_length=64)
    source_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issuer_policy_version: str = Field(min_length=1, max_length=64)
    issuer_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_path: str = Field(min_length=1)
    database_storage_identity: DatabaseStorageIdentity
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_total_changes: Literal[0]
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def computed_authorization_sha256(self) -> str:
        return _sha256_bytes(
            _canonical_json(self.model_dump(mode="json", exclude={"authorization_sha256"})).encode(
                "utf-8"
            )
        )

    @model_validator(mode="after")
    def _verify_authorization(self) -> Self:
        if self.next_inventory_revision != self.current_inventory_revision + 1:
            raise ValueError("next_inventory_revision must immediately follow current revision")
        if self.authorization == "AUTOMATIC":
            if (
                self.coverage_role != "portfolio"
                or self.authorization_attestation != "NOT_APPLICABLE"
                or self.owner_request_id is not None
            ):
                raise ValueError("automatic admission must be unrequested portfolio work")
        elif (
            self.coverage_role != "evaluation"
            or self.authorization_attestation != "CALLER_ATTESTED"
            or self.owner_request_id is None
        ):
            raise ValueError("evaluation admission requires a bound owner request")
        if len(self.database_storage_identity.entries) != 1:
            raise ValueError("authorization requires sidecar-free database storage")
        if self.database_sha256 != self.database_storage_identity.entries[0].content_sha256:
            raise ValueError("database_sha256 must match exact main storage")
        if self.authorization_sha256 != self.computed_authorization_sha256():
            raise ValueError("authorization_sha256 does not seal the authorization")
        return self


def _load_plan(request: SecDeltaAdmissionRequest) -> tuple[SecDeltaPlanReceipt, str]:
    try:
        raw_plan = request.plan_path.read_bytes()
    except OSError as exc:
        raise SecDeltaAdmissionError("unable to read the SEC delta plan") from exc
    raw_sha256 = _sha256_bytes(raw_plan)
    if raw_sha256 != request.expected_plan_sha256:
        raise SecDeltaAdmissionError("raw plan SHA-256 does not match the expected commitment")
    try:
        plan = SecDeltaPlanReceipt.model_validate_json(raw_plan)
    except (ValidationError, ValueError) as exc:
        raise SecDeltaAdmissionError("sealed SEC delta plan validation failed") from exc
    return plan, raw_sha256


def _select_task(plan: SecDeltaPlanReceipt, task_id: str) -> tuple[TickerPlan, SecDeltaTask]:
    matches = tuple(
        (ticker_plan, task)
        for ticker_plan in plan.ticker_plans
        for task in ticker_plan.tasks
        if task.task_id == task_id
    )
    if len(matches) != 1:
        raise SecDeltaAdmissionError("task_id must select exactly one planned task")
    ticker_plan, task = matches[0]
    if task.kind != "NATIVE_INVENTORY_PACKAGES" or task.status != "READY":
        raise SecDeltaAdmissionError("selected task must be READY NATIVE_INVENTORY_PACKAGES")
    if task.ticker != ticker_plan.ticker or task.cik != ticker_plan.cik:
        raise SecDeltaAdmissionError("selected task identity conflicts with its ticker plan")
    return ticker_plan, task


def _verify_policy_dependencies(
    plan: SecDeltaPlanReceipt,
    ticker_plan: TickerPlan,
    task: SecDeltaTask,
) -> tuple[str, str]:
    source_policy_sha256 = _current_source_policy_sha256()
    if (
        plan.source_policy_version != POLICY_VERSION
        or plan.source_policy_sha256 != source_policy_sha256
        or task.source_policy_version != POLICY_VERSION
        or task.source_policy_sha256 != source_policy_sha256
    ):
        raise SecDeltaAdmissionError("planned source policy is not the exact current policy")
    if task.cik is None:
        raise SecDeltaAdmissionError("native inventory admission requires an exact CIK")
    if CIK_MAP.get(task.ticker) != task.cik:
        raise SecDeltaAdmissionError("planned CIK does not match the curated ticker-to-CIK mapping")
    try:
        policy = issuer_policy(task.ticker)
    except ValueError as exc:
        raise SecDeltaAdmissionError("exact reviewed issuer policy is unavailable") from exc
    expected_issuer_id = f"sec-cik-{task.cik}"
    if (
        policy.issuer_id != expected_issuer_id
        or task.issuer_policy_version != policy.policy_version
        or task.issuer_policy_sha256 != policy.policy_sha256
    ):
        raise SecDeltaAdmissionError("planned issuer policy is not the exact reviewed policy")
    if ticker_plan.inventory.inventory_key != f"{policy.issuer_id}:sec-submissions":
        raise SecDeltaAdmissionError("planned inventory key conflicts with reviewed issuer policy")
    return policy.policy_version, policy.policy_sha256


def _verify_plan_authorization_shape(
    plan: SecDeltaPlanReceipt,
    ticker_plan: TickerPlan,
    task: SecDeltaTask,
) -> bool:
    roster = tuple(item for item in plan.roster if item.ticker == task.ticker)
    if len(roster) != 1:
        raise SecDeltaAdmissionError("plan must bind exactly one roster identity for the task")
    roster_entry = roster[0]
    if (
        roster_entry.archived
        or roster_entry.list_type != ticker_plan.list_type
        or roster_entry.owner_request_id != ticker_plan.owner_request_id
    ):
        raise SecDeltaAdmissionError("planned roster identity conflicts with the selected task")
    if ticker_plan.list_type == "portfolio":
        valid = (
            ticker_plan.authorization == "AUTOMATIC_PORTFOLIO"
            and ticker_plan.authorization_attestation == "NOT_APPLICABLE"
            and ticker_plan.owner_request_id is None
            and task.authorization == "AUTOMATIC"
            and task.authorization_attestation == "NOT_APPLICABLE"
            and task.owner_request_id is None
            and roster_entry.selection == "AUTOMATIC_PORTFOLIO"
        )
        requested = False
    else:
        valid = (
            ticker_plan.authorization == "OWNER_REQUEST"
            and ticker_plan.authorization_attestation == "CALLER_ATTESTED"
            and ticker_plan.owner_request_id is not None
            and task.authorization == "OWNER_REQUEST"
            and task.authorization_attestation == "CALLER_ATTESTED"
            and task.owner_request_id == ticker_plan.owner_request_id
            and roster_entry.selection == "OWNER_REQUESTED_EVALUATION"
        )
        requested = True
    if not valid:
        raise SecDeltaAdmissionError("planned owner authorization shape is invalid")
    return requested


def _require_closed_snapshot(path: Path) -> DatabaseStorageIdentity:
    try:
        require_sidecar_free_database(path)
        identity = database_storage_identity(path)
    except (OSError, RehearsalError, ValueError) as exc:
        raise SecDeltaAdmissionError(
            "SEC delta admission requires a closed sidecar-free immutable snapshot"
        ) from exc
    if len(identity.entries) != 1 or identity.entries[0].link_count != 1:
        raise SecDeltaAdmissionError(
            "SEC delta admission requires one unaliased immutable database file"
        )
    return identity


def _read_current_authorization_state(
    database_path: Path,
    *,
    planned_database_path: str,
    planned_storage_identity: DatabaseStorageIdentity,
    ticker_plan: TickerPlan,
    task: SecDeltaTask,
    requested: bool,
) -> tuple[DatabaseStorageIdentity, int]:
    if database_path != Path(planned_database_path).resolve():
        raise SecDeltaAdmissionError(
            "current snapshot does not match the plan database path and storage identity"
        )
    storage_before = _require_closed_snapshot(database_path)
    if storage_before != planned_storage_identity:
        raise SecDeltaAdmissionError(
            "current snapshot does not match the plan database path and storage identity"
        )
    conn: sqlite3.Connection | None = None
    read_error: SecDeltaAdmissionError | None = None
    read_cause: Exception | None = None
    current_revision = -1
    try:
        conn = connect_sqlite(
            database_path,
            role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
        )
        initial_total_changes = conn.total_changes
        revisions = tuple(
            str(row[0]) for row in conn.execute("SELECT version_num FROM alembic_version")
        )
        if revisions != (expected_head(),):
            raise SecDeltaAdmissionError("admission snapshot must use the exact current schema")
        stored = authorize_collection_target_in_connection(
            conn,
            task.ticker,
            requested=requested,
            source=CollectionSource.SEC,
            artifact_kind=ArtifactKind.FILING_PACKAGE,
        )
        if (
            stored.status is not StoredIdentityStatus.AUTHORIZED
            or not stored.allowed
            or stored.target is None
            or stored.target.ticker != task.ticker
            or stored.target.coverage_role.value != ticker_plan.list_type
            or stored.target.requested is not requested
        ):
            raise SecDeltaAdmissionError("current active roster authorization does not admit task")
        revision_row = conn.execute(
            "SELECT MAX(revision) FROM source_inventory_snapshots "
            "WHERE source_kind='sec_submissions' AND (UPPER(ticker)=? OR issuer_id=?)",
            (task.ticker, f"sec-cik-{task.cik}"),
        ).fetchone()
        current_revision = (
            0 if revision_row is None or revision_row[0] is None else int(revision_row[0])
        )
        if conn.total_changes != initial_total_changes:
            raise SecDeltaAdmissionError("SEC delta admission mutated its read-only connection")
    except SecDeltaAdmissionError as exc:
        read_error = exc
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        read_error = SecDeltaAdmissionError("unable to verify current SEC admission state")
        read_cause = exc
    finally:
        if conn is not None:
            conn.close()
    storage_after = _require_closed_snapshot(database_path)
    if storage_after != storage_before:
        raise SecDeltaAdmissionError("immutable database storage changed during admission")
    if read_error is not None:
        raise read_error from read_cause
    if current_revision != ticker_plan.inventory.next_inventory_revision - 1:
        raise SecDeltaAdmissionError(
            "current inventory revision does not immediately precede the planned revision"
        )
    return storage_before, current_revision


def admit_native_inventory_task(
    request: SecDeltaAdmissionRequest,
) -> SecDeltaNativeInventoryAuthorization:
    """Authorize exactly one native inventory task without executing it."""

    plan, raw_plan_sha256 = _load_plan(request)
    ticker_plan, task = _select_task(plan, request.task_id)
    issuer_policy_version, issuer_policy_sha256 = _verify_policy_dependencies(
        plan,
        ticker_plan,
        task,
    )
    requested = _verify_plan_authorization_shape(plan, ticker_plan, task)
    storage_identity, current_revision = _read_current_authorization_state(
        request.database_path,
        planned_database_path=plan.database_path,
        planned_storage_identity=plan.database_storage_identity,
        ticker_plan=ticker_plan,
        task=task,
        requested=requested,
    )
    if task.cik is None:
        raise SecDeltaAdmissionError("native inventory admission requires an exact CIK")
    draft = SecDeltaNativeInventoryAuthorization.model_construct(
        schema_version="sec_delta_native_inventory_authorization.v1",
        admission_version=ADMISSION_VERSION,
        network_policy="FORBIDDEN",
        plan_path=str(request.plan_path),
        plan_sha256=raw_plan_sha256,
        plan_receipt_sha256=plan.receipt_sha256,
        task_id=task.task_id,
        ticker=task.ticker,
        cik=task.cik,
        coverage_role=ticker_plan.list_type,
        authorization=task.authorization,
        authorization_attestation=task.authorization_attestation,
        owner_request_id=task.owner_request_id,
        inventory_key=ticker_plan.inventory.inventory_key,
        current_inventory_revision=current_revision,
        next_inventory_revision=ticker_plan.inventory.next_inventory_revision,
        source_policy_version=POLICY_VERSION,
        source_policy_sha256=plan.source_policy_sha256,
        issuer_policy_version=issuer_policy_version,
        issuer_policy_sha256=issuer_policy_sha256,
        database_path=str(request.database_path),
        database_storage_identity=storage_identity,
        database_sha256=storage_identity.entries[0].content_sha256,
        database_total_changes=0,
        authorization_sha256="0" * 64,
    )
    payload = draft.model_dump(mode="json")
    payload["authorization_sha256"] = draft.computed_authorization_sha256()
    return SecDeltaNativeInventoryAuthorization.model_validate_json(_canonical_json(payload))


__all__ = [
    "ADMISSION_VERSION",
    "SecDeltaAdmissionError",
    "SecDeltaAdmissionRequest",
    "SecDeltaNativeInventoryAuthorization",
    "admit_native_inventory_task",
]
