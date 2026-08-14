from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ObservationState = Literal["current", "missing", "stale", "invalid"]
SchedulerTaskState = Literal["Ready", "Running", "Disabled", "Unknown", "Missing"]
ServiceState = Literal["Running", "Stopped", "Paused", "Unknown", "Missing"]
RegistryMatch = Literal["expected", "missing", "unexpected"]
JobHealthStatus = Literal[
    "ok",
    "degraded_corpus",
    "partial",
    "failed",
    "skipped_locked",
    "blocked_schema_drift",
]
JobHealthSeverity = Literal["info", "warning", "error"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScheduleDefinition(FrozenModel):
    trigger: str
    start_boundary: str | None
    repetition_interval: str | None
    days_interval: int | None
    weeks_interval: int | None
    days_of_week: tuple[str, ...]
    days_of_month: tuple[int, ...]
    months: tuple[str, ...]


class ScheduledTaskDefinition(FrozenModel):
    task_name: str
    xml: str
    wrapper: str
    schedule: ScheduleDefinition
    service_owned: bool


class JobStepDefinition(FrozenModel):
    wrapper: str
    ordinal: int
    job: str
    raw_lane: str
    effective_lane: tuple[str, ...]
    command: tuple[str, ...]
    receipt_ttl_seconds: int


class ServiceDefinition(FrozenModel):
    role: str
    name: str
    purpose: str


class LLMModelPinDefinition(FrozenModel):
    purpose: str
    model: str


class EvalModeDefinition(FrozenModel):
    mode: str
    purposes: tuple[str, ...]


class QueueStateDefinition(FrozenModel):
    queue: str
    states: tuple[str, ...]


class IssuerPolicyDefinition(FrozenModel):
    issuer_id: str
    ticker_aliases: tuple[str, ...]
    policy_sha256: str
    adapter: str
    canonical_json: str


class SourcePolicyDefinition(FrozenModel):
    policy_version: str
    roles: tuple[str, ...]
    display_roles: tuple[str, ...]
    sources: tuple[str, ...]
    collection_modes: tuple[str, ...]
    issuers: tuple[IssuerPolicyDefinition, ...]


class OperationsRegistry(FrozenModel):
    registry_version: Literal["1"] = "1"
    scheduled_tasks: tuple[ScheduledTaskDefinition, ...]
    job_steps: tuple[JobStepDefinition, ...]
    services: tuple[ServiceDefinition, ...]
    llm_model_pins: tuple[LLMModelPinDefinition, ...]
    llm_purposes: tuple[str, ...]
    eval_modes: tuple[EvalModeDefinition, ...]
    source_policy: SourcePolicyDefinition
    queue_states: tuple[QueueStateDefinition, ...]
    expected_alembic_head: str


class ObservationEnvelope(FrozenModel):
    state: ObservationState
    observed_at: datetime
    evidence_source: str
    evidence_recorded_at: datetime | None = None
    detail: str | None = None

    @field_validator("observed_at", "evidence_recorded_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("observation timestamps must be timezone-aware")
        return value


class SchedulerTaskRow(FrozenModel):
    task_name: str
    state: SchedulerTaskState
    registry_match: RegistryMatch


class SchedulerObservation(ObservationEnvelope):
    values: tuple[SchedulerTaskRow, ...] = ()


class ServiceRow(FrozenModel):
    name: str
    state: ServiceState
    registry_match: RegistryMatch


class ServiceObservation(ObservationEnvelope):
    values: tuple[ServiceRow, ...] = ()


class JobHealthRow(FrozenModel):
    schema_version: Literal["1", "2"]
    job: str
    write_sets: tuple[str, ...]
    started_at: datetime
    ended_at: datetime
    status: JobHealthStatus
    exit_code: int
    severity: JobHealthSeverity
    detail: str | None = None
    operation_id: str | None = None
    trigger_kind: Literal["manual", "scheduled", "service"] | None = None
    journal_state: Literal["complete", "unavailable"] | None = None
    journal_detail_code: (
        Literal["request_unavailable", "start_unavailable", "terminal_unavailable", "schema_drift"]
        | None
    ) = None
    journal_reason: str | None = Field(default=None, min_length=1, max_length=240)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("job health timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _versioned_correlation(self) -> JobHealthRow:
        correlation = (
            self.operation_id,
            self.trigger_kind,
            self.journal_state,
            self.journal_detail_code,
            self.journal_reason,
        )
        if self.schema_version == "1":
            if any(value is not None for value in correlation):
                raise ValueError("v1 job health receipts cannot contain v2 correlation fields")
            return self
        if any(value is None for value in correlation[:3]):
            raise ValueError("v2 job health receipts require complete operation correlation")
        if self.operation_id is None or not (
            self.operation_id.startswith("operation:")
            and len(self.operation_id) == 74
            and all(character in "0123456789abcdef" for character in self.operation_id[10:])
        ):
            raise ValueError("v2 operation_id is not canonical")
        detail = (self.journal_detail_code, self.journal_reason)
        if self.journal_state == "complete" and any(value is not None for value in detail):
            raise ValueError("complete operation journals cannot contain an unavailable reason")
        if self.journal_state == "unavailable" and any(value is None for value in detail):
            raise ValueError("unavailable operation journals require a closed reason")
        return self


class JobReceiptObservation(ObservationEnvelope):
    job: str
    receipt: JobHealthRow | None = None


class DatabaseIdentityRow(FrozenModel):
    schema_name: str
    file_path: str | None


class DatabaseIdentityObservation(ObservationEnvelope):
    values: tuple[DatabaseIdentityRow, ...] = ()


class SchemaRevisionRow(FrozenModel):
    expected_head: str
    actual_heads: tuple[str, ...]
    matches: bool


class SchemaRevisionObservation(ObservationEnvelope):
    value: SchemaRevisionRow | None = None


class IngestionRunRow(FrozenModel):
    run_id: str
    directive: str
    status: str
    started_at: datetime
    ended_at: datetime | None

    @field_validator("started_at", "ended_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("ingestion timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _ordered(self) -> IngestionRunRow:
        if self.ended_at is not None and self.started_at > self.ended_at:
            raise ValueError("ingestion timestamps are out of order")
        return self


class DatabaseRunsObservation(ObservationEnvelope):
    values: tuple[IngestionRunRow, ...] = ()


class SourceCallRow(FrozenModel):
    source_name: str
    kind: str
    status: str
    called_at: datetime

    @field_validator("called_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("source call timestamp must be timezone-aware")
        return value


class SourceCallsObservation(ObservationEnvelope):
    values: tuple[SourceCallRow, ...] = ()


class LLMCallRow(FrozenModel):
    purpose: str | None
    model: str
    called_at: datetime
    elapsed_ms: int
    purpose_known: bool
    model_matches_pin: bool | None

    @field_validator("called_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("LLM call timestamp must be timezone-aware")
        return value


class LLMCallsObservation(ObservationEnvelope):
    values: tuple[LLMCallRow, ...] = ()


class FMPBacklogRow(FrozenModel):
    state: str
    count: int
    state_registered: bool


class FMPBacklogObservation(ObservationEnvelope):
    values: tuple[FMPBacklogRow, ...] = ()


class FMPCircuitRow(FrozenModel):
    provider: str
    state: str
    consecutive_failures: int
    consecutive_rate_limits: int
    updated_at: datetime
    state_registered: bool

    @field_validator("updated_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("circuit timestamp must be timezone-aware")
        return value


class FMPCircuitObservation(ObservationEnvelope):
    values: tuple[FMPCircuitRow, ...] = ()


class OperationsSnapshot(FrozenModel):
    snapshot_version: Literal["1"] = "1"
    observed_at: datetime
    registry_version: str
    database_identity: DatabaseIdentityObservation
    schema_revision: SchemaRevisionObservation
    scheduler: SchedulerObservation
    services: ServiceObservation
    job_receipts: tuple[JobReceiptObservation, ...]
    database_runs: DatabaseRunsObservation
    source_calls: SourceCallsObservation
    llm_calls: LLMCallsObservation
    fmp_backlog: FMPBacklogObservation
    fmp_circuit: FMPCircuitObservation

    @field_validator("observed_at")
    @classmethod
    def _observed_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("snapshot observed_at must be timezone-aware")
        return value
