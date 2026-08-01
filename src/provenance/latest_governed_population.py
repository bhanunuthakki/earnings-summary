"""Deterministic production-cohort execution for the 0261 current-state projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ask.sealed_retrieval import RetrievalScope
from provenance.latest_governed_state import (
    LatestGovernedCohortAudit,
    LatestGovernedRefreshRequest,
    LatestGovernedRefreshResult,
    LatestGovernedStateError,
    audit_latest_governed_cohort,
    refresh_latest_governed_state,
)
from provenance.latest_state_activation import (
    BoundLatestStateEligibilityManifest,
    verify_bound_eligibility_manifest,
)

_HEX = frozenset("0123456789abcdef")


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


class LatestGovernedPopulationScopeResult(_FrozenModel):
    scope_id: str
    admitted_terminal_commitment: str
    dry_run: LatestGovernedRefreshResult
    apply_result: LatestGovernedRefreshResult | None
    head_receipt_id: str | None
    head_state_sha256: str | None
    stage_count: int = Field(ge=0)


class LatestGovernedPopulationResult(_FrozenModel):
    schema_version: Literal["latest-governed-population-result/v1"] = (
        "latest-governed-population-result/v1"
    )
    mode: Literal["dry_run", "apply"]
    outcome: Literal["planned", "checkpoint", "complete"]
    processed_scope_ids: tuple[str, ...]
    remaining_scope_ids: tuple[str, ...]
    last_scope_id: str | None
    scope_results: tuple[LatestGovernedPopulationScopeResult, ...]
    heads_before_sha256: str
    heads_after_sha256: str
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
        return self


class LatestGovernedPopulationReceipt(_FrozenModel):
    schema_version: Literal["latest-governed-population-receipt/v1"] = (
        "latest-governed-population-receipt/v1"
    )
    database_path: str
    database_instance_id: str
    alembic_revision: str
    eligibility_artifact_sha256: str
    registry_artifact_sha256: str
    admission: LatestGovernedPopulationAdmission
    request: LatestGovernedPopulationRequest
    result: LatestGovernedPopulationResult
    prior_checkpoint_receipt_sha256: str | None
    receipt_sha256: str

    @field_validator(
        "eligibility_artifact_sha256",
        "registry_artifact_sha256",
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

    core = {
        "schema_version": "latest-governed-population-receipt/v1",
        "database_path": database_path,
        "database_instance_id": database_instance_id,
        "alembic_revision": alembic_revision,
        "eligibility_artifact_sha256": eligibility_artifact_sha256,
        "registry_artifact_sha256": registry_artifact_sha256,
        "admission": admission.model_dump(mode="json"),
        "request": request.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
        "prior_checkpoint_receipt_sha256": prior_checkpoint_receipt_sha256,
    }
    return LatestGovernedPopulationReceipt.model_validate(core | {"receipt_sha256": _digest(core)})


def verify_latest_governed_population_receipt(
    receipt: LatestGovernedPopulationReceipt,
) -> bool:
    """Return whether a population receipt and all nested commitments validate."""

    try:
        LatestGovernedPopulationReceipt.model_validate(receipt.model_dump(mode="json"))
    except ValueError:
        return False
    return True


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
    results: list[LatestGovernedPopulationScopeResult] = []
    processed: set[str] = set()
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
        apply_result: LatestGovernedRefreshResult | None = None
        if request.apply:
            apply_result = refresh_latest_governed_state(
                conn,
                LatestGovernedRefreshRequest(
                    scope_id=item.scope_id,
                    operation_recorded_at=request.operation_recorded_at,
                    max_batch_rows=request.max_batch_rows,
                    document_checkpoint=request.document_checkpoint,
                    apply=True,
                ),
            )
            if apply_result.outcome not in {"no_op", "changed"}:
                raise LatestGovernedStateError("latest governed scope did not finalize")
        head = conn.execute(
            "SELECT refresh_receipt_id,state_sha256 FROM latest_governed_scope_heads "
            "WHERE scope_key=?",
            (item.scope_id,),
        ).fetchone()
        stage_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM latest_governed_refresh_stage stage "
                "JOIN latest_governed_refresh_runs run "
                "ON run.refresh_run_id=stage.refresh_run_id WHERE run.scope_key=?",
                (item.scope_id,),
            ).fetchone()[0]
        )
        if request.apply and (
            head is None or str(head[1]) != item.terminal_commitment or stage_count != 0
        ):
            raise LatestGovernedStateError(
                "latest governed finalized head differs from the admitted terminal"
            )
        processed.add(item.scope_id)
        current_heads = _head_snapshot(conn, scope_ids)
        for scope_id in scope_ids:
            if scope_id not in processed and current_heads[scope_id] != heads_before[scope_id]:
                raise LatestGovernedStateError(
                    "latest governed population changed a non-target scope"
                )
        results.append(
            LatestGovernedPopulationScopeResult(
                scope_id=item.scope_id,
                admitted_terminal_commitment=item.terminal_commitment,
                dry_run=dry_run,
                apply_result=apply_result,
                head_receipt_id=None if head is None else str(head[0]),
                head_state_sha256=None if head is None else str(head[1]),
                stage_count=stage_count,
            )
        )
    heads_after = _head_snapshot(conn, scope_ids)
    cohort_audit = (
        audit_latest_governed_cohort(
            conn,
            scope_ids,
            operation_recorded_at=request.operation_recorded_at,
        )
        if request.apply and not remaining
        else None
    )
    outcome: Literal["planned", "checkpoint", "complete"] = (
        "planned" if not request.apply else ("checkpoint" if remaining else "complete")
    )
    core = {
        "schema_version": "latest-governed-population-result/v1",
        "mode": "apply" if request.apply else "dry_run",
        "outcome": outcome,
        "processed_scope_ids": tuple(item.scope_id for item in selected),
        "remaining_scope_ids": remaining,
        "last_scope_id": None if not selected else selected[-1].scope_id,
        "scope_results": [item.model_dump(mode="json") for item in results],
        "heads_before_sha256": _digest(heads_before),
        "heads_after_sha256": _digest(heads_after),
        "cohort_audit": None if cohort_audit is None else cohort_audit.model_dump(mode="json"),
    }
    return LatestGovernedPopulationResult.model_validate(core | {"result_sha256": _digest(core)})


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
