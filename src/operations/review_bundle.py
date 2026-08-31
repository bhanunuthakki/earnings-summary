"""Sanitized, read-only Operations projection for cross-machine review."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import to_jsonable_python

from operations.kpi_repair_receipts import KpiRepairAttemptReceipt
from operations.models import (
    ObservationEnvelope,
    ObservationState,
    OperationsRegistry,
    OperationsSnapshot,
    SchedulerExpectation,
)
from pipeline.kpi_report_reference_dispositions import (
    ReportKpiReferenceSourceStatus,
    ReportKpiReferenceStatus,
)
from pipeline.kpi_semantic_scope import ScopedKpiDefinition

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
REVIEW_CODE_IDENTITY_DEPENDENCIES = (
    "cron/prepare_kpi_semantic_review.task.xml",
    "cron/run_prepare_kpi_semantic_review.bat",
    "cron/task_manifest.json",
    "execution/apply_kpi_semantic_dispositions.py",
    "execution/apply_kpi_semantic_refresh.py",
    "execution/backup_restore_readiness_receipt.py",
    "execution/build_kpi_semantic_refresh_manifest.py",
    "execution/collect_operations_runtime_observations.py",
    "execution/comments_server.py",
    "execution/fetch_windows_kpi_semantic_review.py",
    "execution/fetch_windows_review_bundle.py",
    "execution/prepare_kpi_semantic_dispositions.py",
    "execution/prepare_kpi_semantic_review.py",
    "execution/record_kpi_disposition_judgment.py",
    "execution/record_kpi_repair_judgment.py",
    "src/compute/kpi_resolver.py",
    "src/operations/kpi_repair_receipts.py",
    "src/operations/kpi_semantic_review_export.py",
    "src/operations/models.py",
    "src/operations/registry.py",
    "src/operations/review_bundle.py",
    "src/operations/snapshot.py",
    "src/pipeline/kpi_report_reference_dispositions.py",
    "src/pipeline/kpi_report_reference_resolver.py",
    "src/pipeline/kpi_semantic_dispositions.py",
    "src/pipeline/kpi_semantic_review.py",
    "src/pipeline/kpi_semantic_scope.py",
    "src/pipeline/kpi_source_review.py",
    "src/provenance/evidence_ledger.py",
    "src/provenance/financial_fact_resolution.py",
    "src/provenance/fulltext_extractor_identity.py",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewIdentity(_FrozenModel):
    serving_origin_sha256: str = Field(pattern=_SHA256_PATTERN)
    code_instance_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_instance_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    scheduler_definition_sha256: str = Field(pattern=_SHA256_PATTERN)


class ReviewObservation(_FrozenModel):
    state: ObservationState
    observed_at: datetime
    evidence_recorded_at: datetime | None = None

    @field_validator("observed_at", "evidence_recorded_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("review observation timestamps must be timezone-aware")
        return value


class ReviewSchema(_FrozenModel):
    observation: ReviewObservation
    expected_head: str | None = None
    actual_heads: tuple[str, ...] = ()
    matches: bool | None = None


class ReviewSchedulerTask(_FrozenModel):
    task_name: str = Field(min_length=1, max_length=240)
    state: Literal["Ready", "Running", "Disabled", "Unknown", "Missing"]
    registry_match: Literal["expected", "missing", "unexpected"]
    scheduler_expectation: SchedulerExpectation | None = None
    expectation_match: bool | None = None
    registered_action_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    registered_checkout_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    registered_wrapper_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    wrapper_match: bool | None = None
    last_attempted_at: datetime | None = None
    last_successful_at: datetime | None = None
    next_expected_at: datetime | None = None
    last_result: int | None = None
    attempt_state: Literal["never_attempted", "running", "succeeded", "failed", "unknown"] = (
        "unknown"
    )

    @field_validator("last_attempted_at", "last_successful_at", "next_expected_at")
    @classmethod
    def _history_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Scheduler history timestamps must be timezone-aware")
        return value


class ReviewScheduler(_FrozenModel):
    observation: ReviewObservation
    tasks: tuple[ReviewSchedulerTask, ...]


class ReviewServiceRow(_FrozenModel):
    name: str = Field(min_length=1, max_length=160)
    state: Literal["Running", "Stopped", "Paused", "Unknown", "Missing"]
    registry_match: Literal["expected", "missing", "unexpected"]


class ReviewServices(_FrozenModel):
    observation: ReviewObservation
    services: tuple[ReviewServiceRow, ...]


class ReviewJob(_FrozenModel):
    job: str = Field(min_length=1, max_length=160)
    observation: ReviewObservation
    status: str | None = Field(default=None, max_length=64)
    severity: str | None = Field(default=None, max_length=32)
    ended_at: datetime | None = None

    @field_validator("ended_at")
    @classmethod
    def _ended_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("job ended_at must be timezone-aware")
        return value


class ReviewKpiCensus(_FrozenModel):
    definitions: int = Field(ge=0)
    facts: int = Field(ge=0)
    admitted: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    legacy_unknown: int = Field(ge=0)
    missing: int = Field(ge=0)
    unresolved_report_metrics: int = Field(ge=0)
    undisposed_report_references: int = Field(ge=0)
    disposed_unresolved_report_references: int = Field(ge=0)
    invalid_or_missing_report_configurations: int = Field(ge=0)
    disposition_gate_blocked: bool
    decision_grade_admission_blocked: bool
    current_actual: int = Field(ge=0)
    comparator: int = Field(ge=0)
    guidance_target: int = Field(ge=0)
    management_explanation: int = Field(ge=0)
    analyst_question: int = Field(ge=0)


class ReviewKpiRepair(_FrozenModel):
    state: ObservationState
    observed_at: datetime
    evidence_recorded_at: datetime | None = None
    mode: Literal["dry_run", "apply"] | None = None
    attempt_state: Literal["passed", "applied", "replayed", "blocked", "failed"] | None = None
    manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    validated_entries: int | None = Field(default=None, ge=0)
    inserted_fact_rows: int | None = Field(default=None, ge=0)
    inserted_context_rows: int | None = Field(default=None, ge=0)
    blocker_codes: tuple[str, ...] = ()

    @field_validator("observed_at", "evidence_recorded_at")
    @classmethod
    def _timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("KPI repair review timestamps must be timezone-aware")
        return value


class OperationsReviewBundle(_FrozenModel):
    schema_version: Literal["operations_review_bundle.v4"] = "operations_review_bundle.v4"
    observed_at: datetime
    identity: ReviewIdentity
    database: ReviewObservation
    schema_revision: ReviewSchema
    scheduler: ReviewScheduler
    services: ReviewServices
    jobs: tuple[ReviewJob, ...]
    kpi_census: ReviewKpiCensus
    kpi_repair: ReviewKpiRepair
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("observed_at")
    @classmethod
    def _observed_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("review bundle observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _hash_matches_payload(self) -> OperationsReviewBundle:
        if self.content_sha256 != _payload_sha256(
            self.model_dump(mode="json", exclude={"content_sha256"})
        ):
            raise ValueError("review bundle content_sha256 does not match its payload")
        return self


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def review_code_identity(repo_root: Path) -> str:
    """Digest the bounded code/config surface that produces this projection."""
    digest = hashlib.sha256()
    for relative in REVIEW_CODE_IDENTITY_DEPENDENCIES:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            payload = (repo_root / relative).read_bytes()
        except OSError:
            digest.update(b"MISSING")
        else:
            digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def database_lineage_identity(conn: sqlite3.Connection) -> str:
    """Return the immutable lineage identity, never the configured path."""
    rows = conn.execute(
        "SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1"
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("database lineage identity is missing or ambiguous")
    value = str(rows[0][0])
    suffix = value.removeprefix("database-instance:")
    if (
        len(value) != 50
        or len(suffix) != 32
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        raise ValueError("database lineage identity is invalid")
    return value


def _payload_sha256(payload: object) -> str:
    canonical = json.dumps(
        to_jsonable_python(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return _sha256_text(canonical)


def _observation(value: ObservationEnvelope) -> ReviewObservation:
    return ReviewObservation(
        state=value.state,
        observed_at=value.observed_at,
        evidence_recorded_at=value.evidence_recorded_at,
    )


def _kpi_census(rows: tuple[ScopedKpiDefinition, ...]) -> ReviewKpiCensus:
    missing = sum(row.missing_context_count for row in rows)
    quarantined = sum(row.quarantined_context_count for row in rows)
    legacy_unknown = sum(row.legacy_unknown_context_count for row in rows)
    undisposed = sum(
        row.kpi_definition_id is None
        and row.report_reference_source_status is ReportKpiReferenceSourceStatus.VALID
        and row.report_reference_status is None
        for row in rows
    )
    disposed_unresolved = sum(
        row.report_reference_status is ReportKpiReferenceStatus.UNRESOLVED for row in rows
    )
    invalid_sources = sum(
        row.report_reference_source_status is not None
        and row.report_reference_source_status.value != "valid"
        for row in rows
    )
    return ReviewKpiCensus(
        definitions=len(rows),
        facts=sum(row.fact_count for row in rows),
        admitted=sum(row.admitted_context_count for row in rows),
        quarantined=quarantined,
        legacy_unknown=legacy_unknown,
        missing=missing,
        unresolved_report_metrics=undisposed + disposed_unresolved,
        undisposed_report_references=undisposed,
        disposed_unresolved_report_references=disposed_unresolved,
        invalid_or_missing_report_configurations=invalid_sources,
        disposition_gate_blocked=bool(
            not rows or missing or legacy_unknown or undisposed or invalid_sources
        ),
        decision_grade_admission_blocked=bool(
            not rows
            or missing
            or quarantined
            or legacy_unknown
            or undisposed
            or disposed_unresolved
            or invalid_sources
        ),
        current_actual=sum(row.current_actual_count for row in rows),
        comparator=sum(row.comparator_count for row in rows),
        guidance_target=sum(row.guidance_target_count for row in rows),
        management_explanation=sum(row.management_explanation_count for row in rows),
        analyst_question=sum(row.analyst_question_count for row in rows),
    )


def load_kpi_repair_review(
    *,
    repo_root: Path,
    observed_at: datetime,
    max_age: timedelta = timedelta(days=7),
) -> ReviewKpiRepair:
    """Read one bounded pointer receipt; never scan the attempt directory."""
    path = repo_root / "data" / "operations" / "kpi_repairs" / "latest.json"
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    try:
        if path.stat().st_size > 64_000:
            raise ValueError("latest KPI repair receipt exceeds its bounded size")
        receipt = KpiRepairAttemptReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ReviewKpiRepair(state="missing", observed_at=observed_at)
    except (OSError, ValueError):
        return ReviewKpiRepair(state="invalid", observed_at=observed_at)
    state: ObservationState = "stale" if observed_at - receipt.completed_at > max_age else "current"
    return ReviewKpiRepair(
        state=state,
        observed_at=observed_at,
        evidence_recorded_at=receipt.completed_at,
        mode=receipt.mode,
        attempt_state=receipt.state,
        manifest_sha256=receipt.manifest_sha256,
        receipt_sha256=receipt.content_sha256,
        validated_entries=receipt.validated_entries,
        inserted_fact_rows=receipt.inserted_fact_rows,
        inserted_context_rows=receipt.inserted_context_rows,
        blocker_codes=receipt.blocker_codes,
    )


def build_operations_review_bundle(
    *,
    snapshot: OperationsSnapshot,
    registry: OperationsRegistry,
    semantic_rows: tuple[ScopedKpiDefinition, ...],
    serving_origin: str,
    code_instance: str,
    database_instance: str | None,
    kpi_repair: ReviewKpiRepair | None = None,
) -> OperationsReviewBundle:
    """Build a closed projection; raw registry/snapshot models never cross the boundary."""
    registry_json = registry.model_dump_json(exclude_none=False)
    scheduler_definition = json.dumps(
        [
            {
                "task_name": task.task_name,
                "wrapper_sha256": _sha256_text(task.wrapper),
                "expectation": task.scheduler_expectation,
            }
            for task in registry.scheduled_tasks
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    schema_value = snapshot.schema_revision.value
    payload: dict[str, object] = {
        "schema_version": "operations_review_bundle.v4",
        "observed_at": snapshot.observed_at,
        "identity": ReviewIdentity(
            serving_origin_sha256=_sha256_text(serving_origin.rstrip("/")),
            code_instance_sha256=_sha256_text(code_instance),
            database_instance_sha256=(
                None if database_instance is None else _sha256_text(database_instance)
            ),
            registry_sha256=_sha256_text(registry_json),
            scheduler_definition_sha256=_sha256_text(scheduler_definition),
        ),
        "database": _observation(snapshot.database_identity),
        "schema_revision": ReviewSchema(
            observation=_observation(snapshot.schema_revision),
            expected_head=None if schema_value is None else schema_value.expected_head,
            actual_heads=() if schema_value is None else schema_value.actual_heads,
            matches=None if schema_value is None else schema_value.matches,
        ),
        "scheduler": ReviewScheduler(
            observation=_observation(snapshot.scheduler),
            tasks=tuple(
                ReviewSchedulerTask(
                    task_name=row.task_name,
                    state=row.state,
                    registry_match=row.registry_match,
                    scheduler_expectation=row.scheduler_expectation,
                    expectation_match=row.expectation_match,
                    registered_action_sha256=row.registered_action_sha256,
                    registered_checkout_sha256=row.registered_checkout_sha256,
                    registered_wrapper_sha256=row.registered_wrapper_sha256,
                    wrapper_match=row.wrapper_match,
                    last_attempted_at=row.last_attempted_at,
                    last_successful_at=row.last_successful_at,
                    next_expected_at=row.next_expected_at,
                    last_result=row.last_result,
                    attempt_state=row.attempt_state,
                )
                for row in snapshot.scheduler.values
            ),
        ),
        "services": ReviewServices(
            observation=_observation(snapshot.services),
            services=tuple(
                ReviewServiceRow(
                    name=row.name,
                    state=row.state,
                    registry_match=row.registry_match,
                )
                for row in snapshot.services.values
            ),
        ),
        "jobs": tuple(
            ReviewJob(
                job=row.job,
                observation=_observation(row),
                status=None if row.receipt is None else row.receipt.status,
                severity=None if row.receipt is None else row.receipt.severity,
                ended_at=None if row.receipt is None else row.receipt.ended_at,
            )
            for row in snapshot.job_receipts
        ),
        "kpi_census": _kpi_census(semantic_rows),
        "kpi_repair": kpi_repair
        or ReviewKpiRepair(state="missing", observed_at=snapshot.observed_at.astimezone(UTC)),
    }
    return OperationsReviewBundle.model_validate(
        {**payload, "content_sha256": _payload_sha256(payload)}
    )


__all__ = [
    "REVIEW_CODE_IDENTITY_DEPENDENCIES",
    "OperationsReviewBundle",
    "ReviewKpiRepair",
    "build_operations_review_bundle",
    "database_lineage_identity",
    "load_kpi_repair_review",
    "review_code_identity",
]
