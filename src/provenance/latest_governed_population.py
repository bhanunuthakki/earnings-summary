"""Deterministic production-cohort execution for the 0261 current-state projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ask.sealed_retrieval import RetrievalScope
from provenance.latest_governed_state import (
    CurrentHead,
    LatestGovernedCohortAudit,
    LatestGovernedRefreshRequest,
    LatestGovernedRefreshResult,
    LatestGovernedScopeAudit,
    LatestGovernedStateError,
    RefreshReceipt,
    audit_latest_governed_cohort,
    audit_latest_governed_scope,
    current_latest_governed_projection_commitment,
    refresh_latest_governed_state,
)
from provenance.latest_state_activation import (
    BoundLatestStateEligibilityManifest,
    verify_bound_eligibility_manifest,
)

_HEX = frozenset("0123456789abcdef")
_OPERATION_LEDGER_TABLE = "latest_governed_population_operation_ledger_v2"


def _canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LatestGovernedPopulationScopeAdmission(_FrozenModel):
    scope_id: str = Field(min_length=1, max_length=256)
    source_scope_key: str
    source_scope_revision_id: str
    issuer_id: str
    reporting_entity_id: str
    ticker: str
    promotion_id: str
    terminal_commitment: str

    @field_validator("terminal_commitment")
    @classmethod
    def _sha(cls, value: str) -> str:
        if len(value) != 64 or any(character not in _HEX for character in value):
            raise ValueError("scope admission commitment must be lowercase SHA-256")
        return value


class LatestGovernedPopulationAdmission(_FrozenModel):
    schema_version: Literal["latest-governed-population-admission/v1"] = (
        "latest-governed-population-admission/v1"
    )
    eligibility_report_sha256: str
    production_scope_registry_sha256: str
    population_run_id: str
    population_receipt_set_sha256: str
    scopes: tuple[LatestGovernedPopulationScopeAdmission, ...]

    @model_validator(mode="after")
    def _shape(self) -> Self:
        scope_ids = tuple(item.scope_id for item in self.scopes)
        if not scope_ids or scope_ids != tuple(sorted(set(scope_ids))):
            raise ValueError("population admission scopes must be nonempty, unique, and sorted")
        for value in (
            self.eligibility_report_sha256,
            self.production_scope_registry_sha256,
            self.population_receipt_set_sha256,
        ):
            if len(value) != 64 or any(character not in _HEX for character in value):
                raise ValueError("population admission commitments must be lowercase SHA-256")
        return self

    @property
    def commitment_sha256(self) -> str:
        return _digest(self)


class LatestGovernedPopulationRequest(_FrozenModel):
    operation_recorded_at: datetime
    admission_sha256: str
    apply: bool = False
    after_scope_id: str | None = Field(default=None, max_length=256)
    max_scopes: int = Field(default=25, ge=1, le=1_000)
    max_batch_rows: int = Field(default=1_000, ge=1, le=10_000)
    document_checkpoint: bool = False

    @field_validator("operation_recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operation_recorded_at must include a timezone")
        return value

    @field_validator("admission_sha256")
    @classmethod
    def _admission_sha(cls, value: str) -> str:
        if len(value) != 64 or any(character not in _HEX for character in value):
            raise ValueError("admission_sha256 must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _single_scope_apply(self) -> Self:
        if self.apply and self.max_scopes != 1:
            raise ValueError("latest governed apply must checkpoint after exactly one scope")
        return self


class LatestGovernedPopulationScopeResult(_FrozenModel):
    scope_id: str
    admitted_terminal_commitment: str
    dry_run: LatestGovernedRefreshResult
    refresh_receipt: RefreshReceipt | None
    head_receipt_id: str | None
    head_state_sha256: str | None
    stage_count: int = Field(ge=0)
    scope_audit: LatestGovernedScopeAudit | None


class LatestGovernedPopulationResult(_FrozenModel):
    schema_version: Literal["latest-governed-population-result/v2"] = (
        "latest-governed-population-result/v2"
    )
    mode: Literal["dry_run", "apply"]
    outcome: Literal["planned", "checkpoint", "complete"]
    processed_scope_ids: tuple[str, ...]
    remaining_scope_ids: tuple[str, ...]
    last_scope_id: str | None
    scope_results: tuple[LatestGovernedPopulationScopeResult, ...]
    heads_before: dict[str, tuple[str, str] | None]
    heads_after: dict[str, tuple[str, str] | None]
    heads_before_sha256: str
    heads_after_sha256: str
    prefix_projection_commitments: dict[str, str]
    fts_rowid_commitment_sha256: str
    cohort_audit: LatestGovernedCohortAudit | None
    result_sha256: str

    @model_validator(mode="after")
    def _commitment(self) -> Self:
        payload = self.model_dump(mode="json")
        stored = str(payload.pop("result_sha256"))
        if stored != _digest(payload):
            raise ValueError("latest governed population result commitment mismatch")
        if self.processed_scope_ids != tuple(item.scope_id for item in self.scope_results):
            raise ValueError("population scope results differ from processed scopes")
        if self.last_scope_id != (
            None if not self.processed_scope_ids else self.processed_scope_ids[-1]
        ):
            raise ValueError("population result last-scope cursor is invalid")
        if set(self.processed_scope_ids) & set(self.remaining_scope_ids):
            raise ValueError("population processed and remaining scopes overlap")
        if (self.outcome == "complete") != (self.cohort_audit is not None):
            raise ValueError("only a complete population result may carry cohort readiness")
        if self.heads_before_sha256 != _digest(
            self.heads_before
        ) or self.heads_after_sha256 != _digest(self.heads_after):
            raise ValueError("population head-set commitment mismatch")
        if len(self.fts_rowid_commitment_sha256) != 64 or any(
            character not in _HEX for character in self.fts_rowid_commitment_sha256
        ):
            raise ValueError("population FTS rowid commitment must be lowercase SHA-256")
        if self.mode == "apply" and len(self.processed_scope_ids) != 1:
            raise ValueError("apply results must contain exactly one checkpointed scope")
        if self.mode == "apply" and any(item.scope_audit is None for item in self.scope_results):
            raise ValueError("apply results require an exhaustive new-scope audit")
        if self.mode == "dry_run" and (
            self.prefix_projection_commitments
            or any(item.scope_audit is not None for item in self.scope_results)
        ):
            raise ValueError("dry-run results cannot carry applied projection evidence")
        if self.outcome == "checkpoint" and not self.remaining_scope_ids:
            raise ValueError("checkpoint result must retain a nonempty suffix")
        return self


class LatestGovernedPopulationPersistence(_FrozenModel):
    database_path: str
    database_instance_id: str = Field(
        min_length=50,
        max_length=50,
        pattern=r"^database-instance:[0-9a-f]{32}$",
    )
    alembic_revision: str = Field(min_length=1, max_length=128)
    eligibility_artifact_sha256: str
    registry_artifact_sha256: str
    prior_checkpoint_receipt_sha256: str | None = None

    @field_validator(
        "eligibility_artifact_sha256",
        "registry_artifact_sha256",
        "prior_checkpoint_receipt_sha256",
    )
    @classmethod
    def _sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in _HEX for character in value)
        ):
            raise ValueError("population persistence commitments must be lowercase SHA-256")
        return value


class LatestGovernedPopulationReceipt(_FrozenModel):
    schema_version: Literal["latest-governed-population-receipt/v2"] = (
        "latest-governed-population-receipt/v2"
    )
    operation_id: str = Field(
        min_length=101,
        max_length=101,
        pattern=r"^latest-governed-population-operation:[0-9a-f]{64}$",
    )
    database_path: str
    database_instance_id: str = Field(
        min_length=50,
        max_length=50,
        pattern=r"^database-instance:[0-9a-f]{32}$",
    )
    alembic_revision: str = Field(min_length=1, max_length=128)
    eligibility_artifact_sha256: str
    registry_artifact_sha256: str
    admission_sha256: str
    request_sha256: str
    admission: LatestGovernedPopulationAdmission
    request: LatestGovernedPopulationRequest
    result: LatestGovernedPopulationResult
    prior_checkpoint_receipt_sha256: str | None
    receipt_sha256: str

    @field_validator(
        "eligibility_artifact_sha256",
        "registry_artifact_sha256",
        "admission_sha256",
        "request_sha256",
        "prior_checkpoint_receipt_sha256",
        "receipt_sha256",
    )
    @classmethod
    def _receipt_sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in _HEX for character in value)
        ):
            raise ValueError("receipt commitments must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _commitment(self) -> Self:
        payload = self.model_dump(mode="json")
        stored = str(payload.pop("receipt_sha256"))
        if stored != _digest(payload):
            raise ValueError("latest governed population receipt commitment mismatch")
        if self.admission_sha256 != self.admission.commitment_sha256:
            raise ValueError("latest governed receipt admission commitment mismatch")
        if self.request.admission_sha256 != self.admission_sha256:
            raise ValueError("latest governed request is not bound to its admission")
        if self.request_sha256 != _digest(self.request):
            raise ValueError("latest governed receipt request commitment mismatch")
        if self.operation_id != latest_governed_population_operation_id(
            persistence=LatestGovernedPopulationPersistence(
                database_path=self.database_path,
                database_instance_id=self.database_instance_id,
                alembic_revision=self.alembic_revision,
                eligibility_artifact_sha256=self.eligibility_artifact_sha256,
                registry_artifact_sha256=self.registry_artifact_sha256,
                prior_checkpoint_receipt_sha256=self.prior_checkpoint_receipt_sha256,
            ),
            admission_sha256=self.admission_sha256,
            request=self.request,
        ):
            raise ValueError("latest governed population operation identity mismatch")
        scope_ids = tuple(item.scope_id for item in self.admission.scopes)
        start = 0
        if self.request.after_scope_id is not None:
            if self.request.after_scope_id not in scope_ids:
                raise ValueError("population receipt resume cursor is outside admission")
            start = scope_ids.index(self.request.after_scope_id) + 1
        selected = scope_ids[start : start + self.request.max_scopes]
        remaining = scope_ids[start + len(selected) :]
        if (
            self.result.processed_scope_ids != selected
            or self.result.remaining_scope_ids != remaining
            or tuple(self.result.heads_before) != scope_ids
            or tuple(self.result.heads_after) != scope_ids
        ):
            raise ValueError("population receipt cohort partition mismatch")
        admitted = {item.scope_id: item for item in self.admission.scopes}
        for result in self.result.scope_results:
            scope = admitted[result.scope_id]
            if (
                result.admitted_terminal_commitment != scope.terminal_commitment
                or result.dry_run.terminal_commitment != scope.terminal_commitment
            ):
                raise ValueError("population receipt scope admission mismatch")
            if self.request.apply and (
                result.refresh_receipt is None
                or result.refresh_receipt.scope_id != result.scope_id
                or result.refresh_receipt.current_state_sha256 != result.head_state_sha256
                or result.head_state_sha256 != scope.terminal_commitment
                or result.stage_count != 0
                or result.scope_audit is None
                or result.scope_audit.scope_id != result.scope_id
                or result.scope_audit.terminal_commitment != scope.terminal_commitment
            ):
                raise ValueError("population receipt finalized scope evidence mismatch")
            if not self.request.apply and result.refresh_receipt is not None:
                raise ValueError("dry-run population receipt cannot contain apply evidence")
        if self.request.apply != (self.result.mode == "apply") or (
            self.request.apply != (self.result.outcome in {"checkpoint", "complete"})
        ):
            raise ValueError("population receipt mode and outcome mismatch")
        if self.result.cohort_audit is not None and (
            self.result.cohort_audit.scope_ids != scope_ids
        ):
            raise ValueError("population readiness cohort differs from admission")
        expected_prefix = scope_ids[: start + len(selected)] if self.request.apply else ()
        if tuple(self.result.prefix_projection_commitments) != expected_prefix:
            raise ValueError("population prefix projection evidence is incomplete")
        for scope_result in self.result.scope_results:
            audit = scope_result.scope_audit
            if (
                audit is not None
                and self.result.prefix_projection_commitments.get(scope_result.scope_id)
                != audit.current_projection_commitment_sha256
            ):
                raise ValueError("population new-scope projection evidence differs")
        if self.result.cohort_audit is not None:
            audited = {item.scope_id: item for item in self.result.cohort_audit.scopes}
            for scope_id, scope in admitted.items():
                audit = audited.get(scope_id)
                if (
                    audit is None
                    or audit.terminal_commitment != scope.terminal_commitment
                    or self.result.prefix_projection_commitments.get(scope_id)
                    != audit.current_projection_commitment_sha256
                ):
                    raise ValueError("population cohort audit differs from admission")
        return self


def build_latest_governed_population_receipt(
    *,
    database_path: str,
    database_instance_id: str,
    alembic_revision: str,
    eligibility_artifact_sha256: str,
    registry_artifact_sha256: str,
    admission: LatestGovernedPopulationAdmission,
    request: LatestGovernedPopulationRequest,
    result: LatestGovernedPopulationResult,
    prior_checkpoint_receipt_sha256: str | None,
) -> LatestGovernedPopulationReceipt:
    """Bind a bounded cohort attempt to its exact database and immutable inputs."""

    admission_sha256 = admission.commitment_sha256
    request_sha256 = _digest(request)
    persistence = LatestGovernedPopulationPersistence(
        database_path=database_path,
        database_instance_id=database_instance_id,
        alembic_revision=alembic_revision,
        eligibility_artifact_sha256=eligibility_artifact_sha256,
        registry_artifact_sha256=registry_artifact_sha256,
        prior_checkpoint_receipt_sha256=prior_checkpoint_receipt_sha256,
    )
    operation_id = latest_governed_population_operation_id(
        persistence=persistence,
        admission_sha256=admission_sha256,
        request=request,
    )
    core = {
        "schema_version": "latest-governed-population-receipt/v2",
        "operation_id": operation_id,
        "database_path": database_path,
        "database_instance_id": database_instance_id,
        "alembic_revision": alembic_revision,
        "eligibility_artifact_sha256": eligibility_artifact_sha256,
        "registry_artifact_sha256": registry_artifact_sha256,
        "admission_sha256": admission_sha256,
        "request_sha256": request_sha256,
        "admission": admission.model_dump(mode="json"),
        "request": request.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
        "prior_checkpoint_receipt_sha256": prior_checkpoint_receipt_sha256,
    }
    return LatestGovernedPopulationReceipt.model_validate(core | {"receipt_sha256": _digest(core)})


def latest_governed_population_operation_id(
    *,
    persistence: LatestGovernedPopulationPersistence,
    admission_sha256: str,
    request: LatestGovernedPopulationRequest,
) -> str:
    return "latest-governed-population-operation:" + _digest(
        {
            "admission_sha256": admission_sha256,
            "persistence": persistence.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
        }
    )


def verify_latest_governed_population_receipt(
    receipt: LatestGovernedPopulationReceipt,
) -> bool:
    """Return whether a population receipt and all nested commitments validate."""

    try:
        LatestGovernedPopulationReceipt.model_validate(receipt.model_dump(mode="json"))
    except ValueError:
        return False
    return True


def persist_latest_governed_population_receipt(
    conn: sqlite3.Connection,
    receipt: LatestGovernedPopulationReceipt,
) -> bool:
    """Insert one immutable receipt inside the materializer final transaction."""

    payload = receipt.model_dump_json()
    values = (
        receipt.operation_id,
        receipt.operation_id,
        receipt.database_instance_id,
        receipt.eligibility_artifact_sha256,
        receipt.registry_artifact_sha256,
        receipt.admission_sha256,
        receipt.request_sha256,
        receipt.result.result_sha256,
        receipt.receipt_sha256,
        payload,
    )
    existing = conn.execute(
        "SELECT operation_id,idempotency_key,database_instance_id,"
        "eligibility_artifact_sha256,registry_artifact_sha256,admission_sha256,"
        "request_sha256,result_sha256,receipt_sha256,receipt_json "
        f"FROM {_OPERATION_LEDGER_TABLE} WHERE operation_id=? "  # nosec B608 -- fixed table
        "OR idempotency_key=? OR receipt_sha256=?",
        (receipt.operation_id, receipt.operation_id, receipt.receipt_sha256),
    ).fetchall()
    if existing:
        if len(existing) != 1 or tuple(existing[0]) != values:
            raise LatestGovernedStateError("latest governed population ledger conflict")
        return False
    conn.execute(
        f"INSERT INTO {_OPERATION_LEDGER_TABLE} ("  # nosec B608 -- fixed table
        "operation_id,idempotency_key,database_instance_id,"
        "eligibility_artifact_sha256,registry_artifact_sha256,admission_sha256,"
        "request_sha256,result_sha256,receipt_sha256,receipt_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    row = conn.execute(
        "SELECT operation_id,idempotency_key,database_instance_id,"
        "eligibility_artifact_sha256,registry_artifact_sha256,admission_sha256,"
        "request_sha256,result_sha256,receipt_sha256,receipt_json "
        f"FROM {_OPERATION_LEDGER_TABLE} WHERE operation_id=?",  # nosec B608 -- fixed table
        (receipt.operation_id,),
    ).fetchone()
    if row is None or tuple(row) != values:
        raise LatestGovernedStateError("latest governed population ledger conflict")
    return True


def load_latest_governed_population_receipt(
    conn: sqlite3.Connection,
    operation_id: str,
) -> LatestGovernedPopulationReceipt | None:
    row = conn.execute(
        f"SELECT receipt_json FROM {_OPERATION_LEDGER_TABLE} "  # nosec B608 -- fixed table
        "WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = LatestGovernedPopulationReceipt.model_validate_json(str(row[0]))
    if not verify_latest_governed_population_receipt(receipt):
        raise LatestGovernedStateError("stored latest governed population receipt is invalid")
    return receipt


def admit_latest_governed_population(
    manifest: BoundLatestStateEligibilityManifest,
    scopes: tuple[RetrievalScope, ...],
) -> LatestGovernedPopulationAdmission:
    """Reduce the bound eligibility and registry contracts to exact apply coordinates."""

    if not verify_bound_eligibility_manifest(manifest):
        raise ValueError("latest governed eligibility admission is invalid")
    if manifest.eligibility.blocked_count or manifest.eligibility.eligible_count == 0:
        raise ValueError("latest governed production cohort is not wholly eligible")
    ordered = tuple(sorted(scopes, key=lambda item: item.scope_id))
    if scopes != ordered or tuple(item.scope_id for item in scopes) != manifest.expected_scope_ids:
        raise ValueError("production registry scopes differ from eligibility admission")
    eligible = {
        item.scope_id: item
        for item in manifest.eligibility.scopes
        if item.status == "eligible" and item.inclusion_state == "core"
    }
    admissions: list[LatestGovernedPopulationScopeAdmission] = []
    for scope in scopes:
        item = eligible.get(scope.scope_id)
        if item is None or item.terminal_commitment is None or item.promotion_id is None:
            raise ValueError("production registry scope lacks exact eligible evidence")
        if (
            item.source_scope_key,
            item.scope_revision_id,
            item.issuer_id,
            item.reporting_entity_id,
            item.ticker,
        ) != (
            scope.source_scope_key,
            scope.source_scope_revision_id,
            scope.issuer_id,
            scope.reporting_entity_id,
            scope.ticker,
        ):
            raise ValueError("production registry identity differs from eligibility evidence")
        admissions.append(
            LatestGovernedPopulationScopeAdmission(
                scope_id=scope.scope_id,
                source_scope_key=scope.source_scope_key,
                source_scope_revision_id=scope.source_scope_revision_id,
                issuer_id=scope.issuer_id,
                reporting_entity_id=scope.reporting_entity_id,
                ticker=scope.ticker,
                promotion_id=item.promotion_id,
                terminal_commitment=item.terminal_commitment,
            )
        )
    population_run_id = manifest.eligibility.population_run_id
    population_sha = manifest.eligibility.population_receipt_set_sha256
    if population_run_id is None or population_sha is None:
        raise ValueError("latest governed admission lacks a population cutover")
    return LatestGovernedPopulationAdmission(
        eligibility_report_sha256=manifest.report_sha256,
        production_scope_registry_sha256=manifest.production_scope_registry_sha256,
        population_run_id=population_run_id,
        population_receipt_set_sha256=population_sha,
        scopes=tuple(admissions),
    )


def populate_latest_governed_cohort(
    conn: sqlite3.Connection,
    admission: LatestGovernedPopulationAdmission,
    request: LatestGovernedPopulationRequest,
    *,
    persistence: LatestGovernedPopulationPersistence | None = None,
    prior_checkpoint: LatestGovernedPopulationReceipt | None = None,
    input_stability_check: Callable[[], None] | None = None,
) -> LatestGovernedPopulationResult:
    """Plan or apply a bounded deterministic slice of the admitted production cohort."""

    if request.admission_sha256 != admission.commitment_sha256:
        raise LatestGovernedStateError("latest governed population admission changed")
    scope_ids = tuple(item.scope_id for item in admission.scopes)
    start = 0
    if request.after_scope_id is not None:
        if request.after_scope_id not in scope_ids:
            raise LatestGovernedStateError("population resume cursor is outside the cohort")
        start = scope_ids.index(request.after_scope_id) + 1
    selected = admission.scopes[start : start + request.max_scopes]
    remaining = scope_ids[start + len(selected) :]
    heads_before = _head_snapshot(conn, scope_ids)
    if request.apply and persistence is None:
        raise LatestGovernedStateError("latest governed apply requires atomic persistence")
    if request.apply and input_stability_check is None:
        raise LatestGovernedStateError("latest governed apply requires an input stability gate")
    if request.apply and len(selected) != 1:
        raise LatestGovernedStateError("latest governed apply must select exactly one scope")
    expected_prefix_ids = scope_ids[:start]
    if request.apply and bool(expected_prefix_ids) != (prior_checkpoint is not None):
        raise LatestGovernedStateError(
            "latest governed resume requires its exact prior checkpoint evidence"
        )
    if request.apply:
        assert persistence is not None
        if (persistence.prior_checkpoint_receipt_sha256 is not None) != (
            prior_checkpoint is not None
        ):
            raise LatestGovernedStateError(
                "latest governed persistence differs from its prior checkpoint"
            )
    if prior_checkpoint is not None and (
        load_latest_governed_population_receipt(conn, prior_checkpoint.operation_id)
        != prior_checkpoint
        or prior_checkpoint.admission != admission
        or prior_checkpoint.result.outcome != "checkpoint"
        or prior_checkpoint.result.last_scope_id != request.after_scope_id
        or tuple(prior_checkpoint.result.prefix_projection_commitments) != expected_prefix_ids
        or prior_checkpoint.result.heads_after != heads_before
        or request.operation_recorded_at < prior_checkpoint.request.operation_recorded_at
    ):
        raise LatestGovernedStateError(
            "latest governed prior checkpoint differs from this exact resume"
        )
    stable_data_version = _data_version(conn)
    current_scope_ids = tuple(
        scope_id for scope_id, head in heads_before.items() if head is not None
    )
    _require_projection_scope_isolation(
        conn,
        admitted_scope_ids=scope_ids,
        expected_current_scope_ids=current_scope_ids,
    )
    if prior_checkpoint is not None:
        if _fts_rowid_commitment(conn) != prior_checkpoint.result.fts_rowid_commitment_sha256:
            raise LatestGovernedStateError(
                "latest governed FTS rowids differ from checkpoint evidence"
            )
        _verify_projection_prefix(
            conn,
            prior_checkpoint.result.prefix_projection_commitments,
        )
        if _data_version(conn) != stable_data_version:
            raise LatestGovernedStateError(
                "latest governed database changed during prefix verification"
            )
    if request.apply:
        assert persistence is not None
        operation_id = latest_governed_population_operation_id(
            persistence=persistence,
            admission_sha256=admission.commitment_sha256,
            request=request,
        )
        stored = load_latest_governed_population_receipt(conn, operation_id)
        if stored is not None:
            stored_persistence = LatestGovernedPopulationPersistence(
                database_path=stored.database_path,
                database_instance_id=stored.database_instance_id,
                alembic_revision=stored.alembic_revision,
                eligibility_artifact_sha256=stored.eligibility_artifact_sha256,
                registry_artifact_sha256=stored.registry_artifact_sha256,
                prior_checkpoint_receipt_sha256=stored.prior_checkpoint_receipt_sha256,
            )
            if (
                stored.admission != admission
                or stored.request != request
                or stored_persistence != persistence
            ):
                raise LatestGovernedStateError("stored population operation identity conflict")
            if _head_snapshot(conn, scope_ids) != stored.result.heads_after:
                raise LatestGovernedStateError(
                    "stored population receipt differs from current database heads"
                )
            stable_data_version = _data_version(conn)
            stored_scope_ids = tuple(
                scope_id for scope_id, head in stored.result.heads_after.items() if head is not None
            )
            _require_projection_scope_isolation(
                conn,
                admitted_scope_ids=scope_ids,
                expected_current_scope_ids=stored_scope_ids,
            )
            if _fts_rowid_commitment(conn) != stored.result.fts_rowid_commitment_sha256:
                raise LatestGovernedStateError(
                    "stored population FTS rowids differ from current database"
                )
            _verify_projection_prefix(conn, stored.result.prefix_projection_commitments)
            if stored.result.outcome == "complete":
                audit = audit_latest_governed_cohort(
                    conn,
                    scope_ids,
                    operation_recorded_at=request.operation_recorded_at,
                )
                _require_admitted_audit_terminals(admission, audit)
                if audit != stored.result.cohort_audit:
                    raise LatestGovernedStateError(
                        "stored complete population readiness no longer verifies"
                    )
            if _data_version(conn) != stable_data_version:
                raise LatestGovernedStateError(
                    "latest governed database changed during stored replay verification"
                )
            try:
                conn.execute("BEGIN IMMEDIATE")
                if _data_version(conn) != stable_data_version:
                    raise LatestGovernedStateError(
                        "latest governed database changed before stored replay lock"
                    )
                if _head_snapshot(conn, scope_ids) != stored.result.heads_after:
                    raise LatestGovernedStateError(
                        "stored population receipt differs from current database heads"
                    )
                _require_projection_scope_isolation(
                    conn,
                    admitted_scope_ids=scope_ids,
                    expected_current_scope_ids=stored_scope_ids,
                )
                if _fts_rowid_commitment(conn) != stored.result.fts_rowid_commitment_sha256:
                    raise LatestGovernedStateError(
                        "stored population FTS rowids changed before replay lock"
                    )
                _verify_projection_prefix(
                    conn,
                    stored.result.prefix_projection_commitments,
                )
                assert input_stability_check is not None
                input_stability_check()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return stored.result
    dry_results: list[
        tuple[LatestGovernedPopulationScopeAdmission, LatestGovernedRefreshResult]
    ] = []
    for item in selected:
        dry_run = refresh_latest_governed_state(
            conn,
            LatestGovernedRefreshRequest(
                scope_id=item.scope_id,
                operation_recorded_at=request.operation_recorded_at,
                max_batch_rows=request.max_batch_rows,
                document_checkpoint=request.document_checkpoint,
                apply=False,
            ),
        )
        if (
            dry_run.mode != "dry_run"
            or dry_run.outcome not in {"no_op", "changed"}
            or dry_run.terminal_commitment != item.terminal_commitment
        ):
            raise LatestGovernedStateError(
                "latest governed dry-run differs from its eligibility admission"
            )
        dry_results.append((item, dry_run))
    if not request.apply:
        planned_results: list[LatestGovernedPopulationScopeResult] = []
        for item, dry_run in dry_results:
            prior_head = heads_before[item.scope_id]
            planned_results.append(
                LatestGovernedPopulationScopeResult(
                    scope_id=item.scope_id,
                    admitted_terminal_commitment=item.terminal_commitment,
                    dry_run=dry_run,
                    refresh_receipt=None,
                    head_receipt_id=None if prior_head is None else prior_head[0],
                    head_state_sha256=None if prior_head is None else prior_head[1],
                    stage_count=0,
                    scope_audit=None,
                )
            )
        results = tuple(planned_results)
        return _population_result(
            mode="dry_run",
            outcome="planned",
            processed_scope_ids=tuple(item.scope_id for item in selected),
            remaining_scope_ids=remaining,
            scope_results=results,
            heads_before=heads_before,
            heads_after=heads_before,
            prefix_projection_commitments={},
            fts_rowid_commitment_sha256=_fts_rowid_commitment(conn),
            cohort_audit=None,
        )

    item, dry_run = dry_results[0]
    captured: list[LatestGovernedPopulationResult] = []

    def finalize(
        transaction: sqlite3.Connection,
        _refresh_request: LatestGovernedRefreshRequest,
        _frontier: object,
        refresh_receipt: RefreshReceipt,
        head: CurrentHead,
    ) -> None:
        assert input_stability_check is not None
        input_stability_check()
        if _data_version(transaction) != stable_data_version:
            raise LatestGovernedStateError(
                "latest governed database changed before checkpoint finalization"
            )
        if head.state_commitment_sha256 != item.terminal_commitment:
            raise LatestGovernedStateError(
                "latest governed finalized head differs from admitted terminal"
            )
        heads_after = _head_snapshot(transaction, scope_ids)
        expected_non_target_heads = (
            heads_before if prior_checkpoint is None else prior_checkpoint.result.heads_after
        )
        for scope_id in scope_ids:
            if (
                scope_id != item.scope_id
                and heads_after[scope_id] != expected_non_target_heads[scope_id]
            ):
                raise LatestGovernedStateError(
                    "latest governed population changed a non-target scope"
                )
        if prior_checkpoint is not None:
            _verify_projection_prefix(
                transaction,
                prior_checkpoint.result.prefix_projection_commitments,
            )
        finalized_scope_ids = tuple(
            scope_id for scope_id, value in heads_after.items() if value is not None
        )
        _require_projection_scope_isolation(
            transaction,
            admitted_scope_ids=scope_ids,
            expected_current_scope_ids=finalized_scope_ids,
        )
        stage_count = int(
            transaction.execute(
                "SELECT COUNT(*) FROM latest_governed_refresh_stage stage "
                "JOIN latest_governed_refresh_runs run "
                "ON run.refresh_run_id=stage.refresh_run_id WHERE run.scope_key=?",
                (item.scope_id,),
            ).fetchone()[0]
        )
        if stage_count:
            raise LatestGovernedStateError("latest governed finalized scope retained stage rows")
        cohort_audit = None
        if remaining:
            scope_audit = audit_latest_governed_scope(
                transaction,
                item.scope_id,
                operation_recorded_at=request.operation_recorded_at,
            )
            if scope_audit.terminal_commitment != item.terminal_commitment:
                raise LatestGovernedStateError(
                    "latest governed new-scope audit differs from admission"
                )
            prefix_projection_commitments = (
                {}
                if prior_checkpoint is None
                else dict(prior_checkpoint.result.prefix_projection_commitments)
            )
            prefix_projection_commitments[item.scope_id] = (
                scope_audit.current_projection_commitment_sha256
            )
        else:
            cohort_audit = audit_latest_governed_cohort(
                transaction,
                scope_ids,
                operation_recorded_at=request.operation_recorded_at,
            )
            _require_admitted_audit_terminals(admission, cohort_audit)
            scope_audit = next(
                audit for audit in cohort_audit.scopes if audit.scope_id == item.scope_id
            )
            prefix_projection_commitments = {
                audit.scope_id: audit.current_projection_commitment_sha256
                for audit in cohort_audit.scopes
            }
        input_stability_check()
        result = _population_result(
            mode="apply",
            outcome="checkpoint" if remaining else "complete",
            processed_scope_ids=(item.scope_id,),
            remaining_scope_ids=remaining,
            scope_results=(
                LatestGovernedPopulationScopeResult(
                    scope_id=item.scope_id,
                    admitted_terminal_commitment=item.terminal_commitment,
                    dry_run=dry_run,
                    refresh_receipt=refresh_receipt,
                    head_receipt_id=head.receipt_id,
                    head_state_sha256=head.state_commitment_sha256,
                    stage_count=0,
                    scope_audit=scope_audit,
                ),
            ),
            heads_before=heads_before,
            heads_after=heads_after,
            prefix_projection_commitments=prefix_projection_commitments,
            fts_rowid_commitment_sha256=_fts_rowid_commitment(transaction),
            cohort_audit=cohort_audit,
        )
        assert persistence is not None
        receipt = build_latest_governed_population_receipt(
            database_path=persistence.database_path,
            database_instance_id=persistence.database_instance_id,
            alembic_revision=persistence.alembic_revision,
            eligibility_artifact_sha256=persistence.eligibility_artifact_sha256,
            registry_artifact_sha256=persistence.registry_artifact_sha256,
            admission=admission,
            request=request,
            result=result,
            prior_checkpoint_receipt_sha256=persistence.prior_checkpoint_receipt_sha256,
        )
        persist_latest_governed_population_receipt(transaction, receipt)
        captured.append(result)

    refresh_latest_governed_state(
        conn,
        LatestGovernedRefreshRequest(
            scope_id=item.scope_id,
            operation_recorded_at=request.operation_recorded_at,
            max_batch_rows=request.max_batch_rows,
            document_checkpoint=request.document_checkpoint,
            expected_terminal_commitment=item.terminal_commitment,
            apply=True,
        ),
        finalization_hook=finalize,
    )
    if len(captured) != 1:
        raise LatestGovernedStateError("atomic population receipt was not finalized")
    return captured[0]


def _population_result(
    *,
    mode: Literal["dry_run", "apply"],
    outcome: Literal["planned", "checkpoint", "complete"],
    processed_scope_ids: tuple[str, ...],
    remaining_scope_ids: tuple[str, ...],
    scope_results: tuple[LatestGovernedPopulationScopeResult, ...],
    heads_before: dict[str, tuple[str, str] | None],
    heads_after: dict[str, tuple[str, str] | None],
    prefix_projection_commitments: dict[str, str],
    fts_rowid_commitment_sha256: str,
    cohort_audit: LatestGovernedCohortAudit | None,
) -> LatestGovernedPopulationResult:
    core = {
        "schema_version": "latest-governed-population-result/v2",
        "mode": mode,
        "outcome": outcome,
        "processed_scope_ids": processed_scope_ids,
        "remaining_scope_ids": remaining_scope_ids,
        "last_scope_id": None if not processed_scope_ids else processed_scope_ids[-1],
        "scope_results": [item.model_dump(mode="json") for item in scope_results],
        "heads_before": heads_before,
        "heads_after": heads_after,
        "heads_before_sha256": _digest(heads_before),
        "heads_after_sha256": _digest(heads_after),
        "prefix_projection_commitments": prefix_projection_commitments,
        "fts_rowid_commitment_sha256": fts_rowid_commitment_sha256,
        "cohort_audit": None if cohort_audit is None else cohort_audit.model_dump(mode="json"),
    }
    return LatestGovernedPopulationResult.model_validate(core | {"result_sha256": _digest(core)})


def _data_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA data_version").fetchone()[0])


def _verify_projection_prefix(
    conn: sqlite3.Connection,
    expected: dict[str, str],
) -> None:
    for scope_id, commitment in expected.items():
        if current_latest_governed_projection_commitment(conn, scope_id) != commitment:
            raise LatestGovernedStateError(
                "latest governed prefix projection differs from checkpoint evidence"
            )


def _fts_rowid_commitment(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT id FROM latest_governed_narrative_fts_docsize ORDER BY id"
    ).fetchall()
    return _digest({"rowids": [int(row[0]) for row in rows], "version": "fts-rowids/v1"})


def _require_projection_scope_isolation(
    conn: sqlite3.Connection,
    *,
    admitted_scope_ids: tuple[str, ...],
    expected_current_scope_ids: tuple[str, ...],
) -> None:
    admitted = set(admitted_scope_ids)
    expected_current = set(expected_current_scope_ids)
    fts_delta_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT docsize.id FROM latest_governed_narrative_fts_docsize docsize "
            "WHERE NOT EXISTS (SELECT 1 FROM latest_governed_narrative_entries entry "
            "WHERE entry.rowid=docsize.id) UNION ALL "
            "SELECT entry.rowid FROM latest_governed_narrative_entries entry "
            "WHERE NOT EXISTS (SELECT 1 FROM latest_governed_narrative_fts_docsize docsize "
            "WHERE docsize.id=entry.rowid))"
        ).fetchone()[0]
    )
    if fts_delta_count:
        raise LatestGovernedStateError("latest governed FTS rowids differ from narrative rows")
    for table in (
        "latest_governed_scope_heads",
        "latest_governed_fact_entries",
        "latest_governed_document_entries",
        "latest_governed_narrative_entries",
    ):
        current_scope_ids = {
            str(row[0])
            for row in conn.execute(
                f"SELECT DISTINCT scope_key FROM {table}"  # nosec B608 -- closed table tuple
            ).fetchall()
        }
        if current_scope_ids != expected_current:
            raise LatestGovernedStateError(
                "latest governed current scopes differ from checkpoint heads"
            )
    plane_scope_ids = {
        str(row[0])
        for row in conn.execute(
            "SELECT scope_key FROM latest_governed_refresh_runs UNION "
            "SELECT scope_key FROM latest_governed_refresh_receipts UNION "
            "SELECT scope_key FROM latest_governed_scope_heads UNION "
            "SELECT scope_key FROM latest_governed_fact_entries UNION "
            "SELECT scope_key FROM latest_governed_document_entries UNION "
            "SELECT scope_key FROM latest_governed_narrative_entries"
        ).fetchall()
    }
    if not plane_scope_ids.issubset(admitted):
        raise LatestGovernedStateError(
            "latest governed planes contain a scope outside the admission"
        )


def _require_admitted_audit_terminals(
    admission: LatestGovernedPopulationAdmission,
    audit: LatestGovernedCohortAudit,
) -> None:
    admitted = {item.scope_id: item.terminal_commitment for item in admission.scopes}
    actual = {item.scope_id: item.terminal_commitment for item in audit.scopes}
    if actual != admitted:
        raise LatestGovernedStateError(
            "latest governed cohort audit terminals differ from admission"
        )


def _head_snapshot(
    conn: sqlite3.Connection,
    scope_ids: tuple[str, ...],
) -> dict[str, tuple[str, str] | None]:
    snapshot: dict[str, tuple[str, str] | None] = {}
    for scope_id in scope_ids:
        rows = conn.execute(
            "SELECT refresh_receipt_id,state_sha256 FROM latest_governed_scope_heads "
            "WHERE scope_key=?",
            (scope_id,),
        ).fetchall()
        if len(rows) > 1:
            raise LatestGovernedStateError("latest governed scope head is ambiguous")
        snapshot[scope_id] = None if not rows else (str(rows[0][0]), str(rows[0][1]))
    return snapshot
