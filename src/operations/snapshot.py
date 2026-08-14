from __future__ import annotations

import sqlite3
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from operations.models import (
    DatabaseIdentityObservation,
    DatabaseIdentityRow,
    DatabaseRunsObservation,
    FMPBacklogObservation,
    FMPBacklogRow,
    FMPCircuitObservation,
    FMPCircuitRow,
    IngestionRunRow,
    JobHealthRow,
    JobReceiptObservation,
    LLMCallRow,
    LLMCallsObservation,
    OperationsRegistry,
    OperationsSnapshot,
    SchedulerObservation,
    SchedulerTaskRow,
    SchedulerTaskState,
    SchemaRevisionObservation,
    SchemaRevisionRow,
    ServiceObservation,
    ServiceRow,
    ServiceState,
    SourceCallRow,
    SourceCallsObservation,
)
from runtime.job_runtime import health_receipt_directory

_MAX_ROWS = 100
_MAX_RECEIPT_BYTES = 64 * 1024
_RUNTIME_RECEIPT_TTL = timedelta(minutes=15)
_OBSERVED_TABLES = (
    "alembic_version",
    "provider_circuit_state",
    "fmp_work_backlog",
    "ingestion_runs",
    "llm_calls",
    "source_calls",
)
_T = TypeVar("_T", bound=BaseModel)


class _ReceiptError(ValueError):
    pass


class _SchedulerTaskReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_name: str
    state: Literal["Ready", "Running", "Disabled", "Unknown"]


class _SchedulerReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1"]
    observed_at: datetime
    tasks: tuple[_SchedulerTaskReceipt, ...]

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("scheduler receipt observed_at must be aware")
        return value


class _ServiceReceiptRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    state: Literal["Running", "Stopped", "Paused", "Unknown"]


class _ServiceReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1"]
    observed_at: datetime
    services: tuple[_ServiceReceiptRow, ...]

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("service receipt observed_at must be aware")
        return value


def _read_receipt(path: Path, model: type[_T]) -> _T:
    """Read one immutable cached receipt through a shared byte-bounded boundary."""

    try:
        with path.open("rb") as handle:
            payload = handle.read(_MAX_RECEIPT_BYTES + 1)
    except OSError as exc:
        raise _ReceiptError(type(exc).__name__) from exc
    if len(payload) > _MAX_RECEIPT_BYTES:
        raise _ReceiptError("receipt exceeds 64 KiB")
    try:
        return model.model_validate_json(payload)
    except ValidationError as exc:
        raise _ReceiptError("receipt schema validation failed") from exc


def _metadata_tables(conn: sqlite3.Connection) -> tuple[set[str] | None, str | None]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN (?,?,?,?,?,?) ORDER BY name LIMIT ?",
            (*_OBSERVED_TABLES, _MAX_ROWS),
        ).fetchall()
    except sqlite3.Error as exc:
        return None, f"metadata read failed: {type(exc).__name__}"
    return {str(row[0]) for row in rows}, None


def _database_identity(
    conn: sqlite3.Connection, observed_at: datetime
) -> DatabaseIdentityObservation:
    try:
        rows = conn.execute("PRAGMA database_list").fetchmany(_MAX_ROWS + 1)
        if len(rows) > _MAX_ROWS:
            return DatabaseIdentityObservation(
                state="invalid",
                observed_at=observed_at,
                evidence_source="sqlite:database_list",
                detail="database identity exceeds bounded inventory",
            )
        values = tuple(
            DatabaseIdentityRow(schema_name=str(row[1]), file_path=str(row[2]) or None)
            for row in rows
        )
    except (sqlite3.Error, ValidationError) as exc:
        return DatabaseIdentityObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source="sqlite:database_list",
            detail=f"database identity read failed: {type(exc).__name__}",
        )
    main_count = sum(row.schema_name.casefold() == "main" for row in values)
    return DatabaseIdentityObservation(
        state="current" if main_count == 1 else "invalid",
        observed_at=observed_at,
        evidence_source="sqlite:database_list",
        values=values,
        detail=None if main_count == 1 else "database identity requires exactly one main schema",
    )


def _schema_revision(
    conn: sqlite3.Connection,
    tables: set[str] | None,
    registry: OperationsRegistry,
    observed_at: datetime,
    metadata_error: str | None,
) -> SchemaRevisionObservation:
    if tables is None:
        return SchemaRevisionObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source="sqlite:alembic_version",
            detail=metadata_error,
        )
    if "alembic_version" not in tables:
        return SchemaRevisionObservation(
            state="missing",
            observed_at=observed_at,
            evidence_source="sqlite:alembic_version",
            detail="alembic_version table is absent",
        )
    try:
        actual = tuple(
            sorted(
                str(row[0])
                for row in conn.execute(
                    "SELECT version_num FROM alembic_version LIMIT ?", (_MAX_ROWS,)
                ).fetchall()
            )
        )
        value = SchemaRevisionRow(
            expected_head=registry.expected_alembic_head,
            actual_heads=actual,
            matches=actual == (registry.expected_alembic_head,),
        )
    except (sqlite3.Error, ValidationError) as exc:
        return SchemaRevisionObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source="sqlite:alembic_version",
            detail=f"schema revision read failed: {type(exc).__name__}",
        )
    return SchemaRevisionObservation(
        state="current" if value.matches else "invalid",
        observed_at=observed_at,
        evidence_source="sqlite:alembic_version",
        value=value,
        detail=None if value.matches else "database revision does not match registry head",
    )


def _runtime_state(
    *,
    receipt_path: Path | None,
    observed_at: datetime,
    expected: tuple[str, ...],
    kind: Literal["scheduler", "service"],
) -> SchedulerObservation | ServiceObservation:
    source = f"{kind}:cached_receipt"
    if receipt_path is None:
        if kind == "scheduler":
            return SchedulerObservation(
                state="missing",
                observed_at=observed_at,
                evidence_source=source,
                values=tuple(
                    SchedulerTaskRow(task_name=name, state="Missing", registry_match="missing")
                    for name in expected
                ),
                detail="no typed scheduler receipt supplied",
            )
        return ServiceObservation(
            state="missing",
            observed_at=observed_at,
            evidence_source=source,
            values=tuple(
                ServiceRow(name=name, state="Missing", registry_match="missing")
                for name in expected
            ),
            detail="no typed service receipt supplied",
        )
    try:
        receipt_stat = receipt_path.lstat()
    except FileNotFoundError:
        if kind == "scheduler":
            return SchedulerObservation(
                state="missing",
                observed_at=observed_at,
                evidence_source=str(receipt_path),
                values=tuple(
                    SchedulerTaskRow(task_name=name, state="Missing", registry_match="missing")
                    for name in expected
                ),
                detail="typed scheduler receipt is absent",
            )
        return ServiceObservation(
            state="missing",
            observed_at=observed_at,
            evidence_source=str(receipt_path),
            values=tuple(
                ServiceRow(name=name, state="Missing", registry_match="missing")
                for name in expected
            ),
            detail="typed service receipt is absent",
        )
    except OSError as exc:
        observation_type = SchedulerObservation if kind == "scheduler" else ServiceObservation
        return observation_type(
            state="invalid",
            observed_at=observed_at,
            evidence_source=str(receipt_path),
            detail=f"receipt metadata read failed: {type(exc).__name__}",
        )
    if not stat.S_ISREG(receipt_stat.st_mode):
        observation_type = SchedulerObservation if kind == "scheduler" else ServiceObservation
        return observation_type(
            state="invalid",
            observed_at=observed_at,
            evidence_source=str(receipt_path),
            detail="receipt path is not a regular file",
        )
    try:
        if kind == "scheduler":
            receipt = _read_receipt(receipt_path, _SchedulerReceipt)
            recorded_at = receipt.observed_at
            supplied = [(row.task_name, row.state) for row in receipt.tasks]
        else:
            service_receipt = _read_receipt(receipt_path, _ServiceReceipt)
            recorded_at = service_receipt.observed_at
            supplied = [(row.name, row.state) for row in service_receipt.services]
        names = [name.casefold() for name, _state in supplied]
        if len(names) != len(set(names)):
            raise _ReceiptError(f"duplicate {kind} identities")
        if recorded_at > observed_at:
            raise _ReceiptError(f"future {kind} receipt clock")
    except _ReceiptError as exc:
        observation_type = SchedulerObservation if kind == "scheduler" else ServiceObservation
        return observation_type(
            state="invalid",
            observed_at=observed_at,
            evidence_source=str(receipt_path),
            detail=str(exc),
        )

    expected_by_key = {name.casefold(): name for name in expected}
    supplied_by_key = {name.casefold(): (name, state) for name, state in supplied}
    gaps = set(expected_by_key) - set(supplied_by_key)
    extras = set(supplied_by_key) - set(expected_by_key)
    age_stale = observed_at - recorded_at > _RUNTIME_RECEIPT_TTL
    state = "invalid" if gaps or extras else "stale" if age_stale else "current"
    detail = (
        f"missing={len(gaps)} unexpected={len(extras)}"
        if gaps or extras
        else "cached runtime receipt is older than 15 minutes"
        if age_stale
        else None
    )
    if kind == "scheduler":
        values = [
            SchedulerTaskRow(
                task_name=expected_by_key[key],
                state=cast(
                    SchedulerTaskState,
                    supplied_by_key[key][1] if key in supplied_by_key else "Missing",
                ),
                registry_match="expected" if key in supplied_by_key else "missing",
            )
            for key in expected_by_key
        ]
        values.extend(
            SchedulerTaskRow(
                task_name=supplied_by_key[key][0],
                state=cast(SchedulerTaskState, supplied_by_key[key][1]),
                registry_match="unexpected",
            )
            for key in sorted(extras)
        )
        return SchedulerObservation(
            state=state,
            observed_at=observed_at,
            evidence_source=str(receipt_path),
            evidence_recorded_at=recorded_at,
            values=tuple(values),
            detail=detail,
        )
    service_values = [
        ServiceRow(
            name=expected_by_key[key],
            state=cast(
                ServiceState,
                supplied_by_key[key][1] if key in supplied_by_key else "Missing",
            ),
            registry_match="expected" if key in supplied_by_key else "missing",
        )
        for key in expected_by_key
    ]
    service_values.extend(
        ServiceRow(
            name=supplied_by_key[key][0],
            state=cast(ServiceState, supplied_by_key[key][1]),
            registry_match="unexpected",
        )
        for key in sorted(extras)
    )
    return ServiceObservation(
        state=state,
        observed_at=observed_at,
        evidence_source=str(receipt_path),
        evidence_recorded_at=recorded_at,
        values=tuple(service_values),
        detail=detail,
    )


def _job_receipts(
    registry: OperationsRegistry, repo_root: Path, observed_at: datetime
) -> tuple[JobReceiptObservation, ...]:
    job_contracts: dict[str, tuple[set[str], int]] = {}
    for step in registry.job_steps:
        lanes, ttl = job_contracts.setdefault(step.job, (set(), step.receipt_ttl_seconds))
        lanes.update(step.effective_lane)
        job_contracts[step.job] = (lanes, max(ttl, step.receipt_ttl_seconds))
    observations: list[JobReceiptObservation] = []
    for job, (expected_lanes, ttl_seconds) in sorted(job_contracts.items()):
        latest = health_receipt_directory(repo_root, job) / "latest.json"
        source = str(latest)
        try:
            metadata = latest.lstat()
        except FileNotFoundError:
            observations.append(
                JobReceiptObservation(
                    state="missing",
                    observed_at=observed_at,
                    evidence_source=source,
                    job=job,
                    detail="canonical job health receipt does not exist",
                )
            )
            continue
        except OSError as exc:
            observations.append(
                JobReceiptObservation(
                    state="invalid",
                    observed_at=observed_at,
                    evidence_source=source,
                    job=job,
                    detail=f"canonical job health receipt inspection failed: {type(exc).__name__}",
                )
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            observations.append(
                JobReceiptObservation(
                    state="invalid",
                    observed_at=observed_at,
                    evidence_source=source,
                    job=job,
                    detail="canonical job health receipt is not a regular file",
                )
            )
            continue
        try:
            record = _read_receipt(latest, JobHealthRow)
            if record.job != job:
                raise _ReceiptError("foreign job identity")
            if record.started_at > record.ended_at or record.ended_at > observed_at:
                raise _ReceiptError("job receipt clocks are out of order")
            if set(record.write_sets) != expected_lanes:
                raise _ReceiptError("job receipt lane identity mismatch")
        except _ReceiptError as exc:
            state = "invalid"
            detail = str(exc)
            record = None
        else:
            if observed_at - record.ended_at > timedelta(seconds=ttl_seconds):
                state = "stale"
                detail = f"latest receipt exceeds owned TTL of {ttl_seconds} seconds"
            else:
                state = "current"
                detail = None
        observations.append(
            JobReceiptObservation(
                state=state,
                observed_at=observed_at,
                evidence_source=source,
                evidence_recorded_at=record.ended_at if record else None,
                job=job,
                receipt=record,
                detail=detail,
            )
        )
    return tuple(observations)


def _missing_database_observation(
    observation_type: type[_T], table: str, observed_at: datetime, detail: str
) -> _T:
    return observation_type(
        state="missing",
        observed_at=observed_at,
        evidence_source=f"sqlite:{table}",
        detail=detail,
    )


def _database_observations(
    conn: sqlite3.Connection,
    tables: set[str] | None,
    registry: OperationsRegistry,
    observed_at: datetime,
    metadata_error: str | None,
) -> tuple[
    DatabaseRunsObservation,
    SourceCallsObservation,
    LLMCallsObservation,
    FMPBacklogObservation,
    FMPCircuitObservation,
]:
    runs = _runs_observation(conn, tables, observed_at, metadata_error)
    sources = _source_calls_observation(conn, tables, observed_at, metadata_error)
    llm = _llm_observation(conn, tables, registry, observed_at, metadata_error)
    backlog = _backlog_observation(conn, tables, registry, observed_at, metadata_error)
    circuit = _circuit_observation(conn, tables, registry, observed_at, metadata_error)
    return runs, sources, llm, backlog, circuit


def _runs_observation(
    conn: sqlite3.Connection,
    tables: set[str] | None,
    observed_at: datetime,
    metadata_error: str | None,
) -> DatabaseRunsObservation:
    table = "ingestion_runs"
    if tables is None:
        return DatabaseRunsObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source=f"sqlite:{table}",
            detail=metadata_error,
        )
    if table not in tables:
        return _missing_database_observation(
            DatabaseRunsObservation, table, observed_at, f"{table} table is absent"
        )
    try:
        rows = conn.execute(
            "SELECT run_id,directive,status,started_at,ended_at FROM ingestion_runs "
            "ORDER BY rowid DESC LIMIT ?",
            (_MAX_ROWS,),
        ).fetchall()
        values = tuple(
            IngestionRunRow(
                run_id=str(row[0]),
                directive=str(row[1]),
                status=str(row[2]),
                started_at=row[3],
                ended_at=row[4],
            )
            for row in rows
        )
    except (sqlite3.Error, ValidationError) as exc:
        return DatabaseRunsObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source=f"sqlite:{table}",
            detail=f"bounded read failed: {type(exc).__name__}",
        )
    future = sum(
        row.started_at > observed_at or (row.ended_at is not None and row.ended_at > observed_at)
        for row in values
    )
    return DatabaseRunsObservation(
        state="invalid" if future else "current",
        observed_at=observed_at,
        evidence_source=f"sqlite:{table}",
        values=values,
        detail=f"{future} future-dated rows" if future else None if values else "no rows recorded",
    )


def _source_calls_observation(
    conn: sqlite3.Connection,
    tables: set[str] | None,
    observed_at: datetime,
    metadata_error: str | None,
) -> SourceCallsObservation:
    table = "source_calls"
    if tables is None:
        return SourceCallsObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source=f"sqlite:{table}",
            detail=metadata_error,
        )
    if table not in tables:
        return _missing_database_observation(
            SourceCallsObservation, table, observed_at, f"{table} table is absent"
        )
    try:
        rows = conn.execute(
            "SELECT source_name,kind,status,called_at FROM source_calls "
            "ORDER BY rowid DESC LIMIT ?",
            (_MAX_ROWS,),
        ).fetchall()
        values = tuple(
            SourceCallRow(
                source_name=str(row[0]),
                kind=str(row[1]),
                status=str(row[2]),
                called_at=row[3],
            )
            for row in rows
        )
    except (sqlite3.Error, ValidationError) as exc:
        return SourceCallsObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source=f"sqlite:{table}",
            detail=f"bounded read failed: {type(exc).__name__}",
        )
    future = sum(row.called_at > observed_at for row in values)
    return SourceCallsObservation(
        state="invalid" if future else "current",
        observed_at=observed_at,
        evidence_source=f"sqlite:{table}",
        values=values,
        detail=f"{future} future-dated rows" if future else None if values else "no rows recorded",
    )


def _llm_observation(
    conn: sqlite3.Connection,
    tables: set[str] | None,
    registry: OperationsRegistry,
    observed_at: datetime,
    metadata_error: str | None,
) -> LLMCallsObservation:
    if tables is None:
        return LLMCallsObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source="sqlite:llm_calls",
            detail=metadata_error,
        )
    if "llm_calls" not in tables:
        return _missing_database_observation(
            LLMCallsObservation, "llm_calls", observed_at, "llm_calls table is absent"
        )
    pins = {pin.purpose: pin.model for pin in registry.llm_model_pins}
    known = set(registry.llm_purposes)
    try:
        rows = conn.execute(
            "SELECT purpose,model,called_at,elapsed_ms FROM llm_calls ORDER BY rowid DESC LIMIT ?",
            (_MAX_ROWS,),
        ).fetchall()
        values = tuple(
            LLMCallRow(
                purpose=str(row[0]) if row[0] is not None else None,
                model=str(row[1]),
                called_at=row[2],
                elapsed_ms=int(row[3]),
                purpose_known=row[0] in known,
                model_matches_pin=(str(row[1]) == pins[str(row[0])]) if row[0] in pins else None,
            )
            for row in rows
        )
    except (sqlite3.Error, ValidationError, TypeError, ValueError) as exc:
        return LLMCallsObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source="sqlite:llm_calls",
            detail=f"bounded read failed: {type(exc).__name__}",
        )
    drift = sum(not row.purpose_known or row.model_matches_pin is False for row in values)
    future = sum(row.called_at > observed_at for row in values)
    return LLMCallsObservation(
        state="invalid" if drift or future else "current",
        observed_at=observed_at,
        evidence_source="sqlite:llm_calls",
        values=values,
        detail=(
            f"{drift} unknown purpose or model-pin drift rows; {future} future-dated rows"
            if drift or future
            else None
        ),
    )


def _queue_states(registry: OperationsRegistry, queue: str) -> set[str]:
    return set(next(item.states for item in registry.queue_states if item.queue == queue))


def _backlog_observation(
    conn: sqlite3.Connection,
    tables: set[str] | None,
    registry: OperationsRegistry,
    observed_at: datetime,
    metadata_error: str | None,
) -> FMPBacklogObservation:
    table = "fmp_work_backlog"
    if tables is None:
        return FMPBacklogObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source=f"sqlite:{table}",
            detail=metadata_error,
        )
    if table not in tables:
        return _missing_database_observation(
            FMPBacklogObservation, table, observed_at, f"{table} table is absent"
        )
    registered = _queue_states(registry, "fmp_work")
    try:
        rows = conn.execute(
            "SELECT state,COUNT(*) FROM fmp_work_backlog GROUP BY state LIMIT ?", (_MAX_ROWS,)
        ).fetchall()
        values = tuple(
            FMPBacklogRow(
                state=str(row[0]), count=int(row[1]), state_registered=row[0] in registered
            )
            for row in rows
        )
    except (sqlite3.Error, ValidationError, TypeError, ValueError) as exc:
        return FMPBacklogObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source=f"sqlite:{table}",
            detail=f"bounded read failed: {type(exc).__name__}",
        )
    drift = sum(not row.state_registered for row in values)
    return FMPBacklogObservation(
        state="invalid" if drift else "current",
        observed_at=observed_at,
        evidence_source=f"sqlite:{table}",
        values=values,
        detail=f"{drift} unregistered queue states" if drift else None,
    )


def _circuit_observation(
    conn: sqlite3.Connection,
    tables: set[str] | None,
    registry: OperationsRegistry,
    observed_at: datetime,
    metadata_error: str | None,
) -> FMPCircuitObservation:
    table = "provider_circuit_state"
    if tables is None:
        return FMPCircuitObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source=f"sqlite:{table}",
            detail=metadata_error,
        )
    if table not in tables:
        return _missing_database_observation(
            FMPCircuitObservation, table, observed_at, f"{table} table is absent"
        )
    registered = _queue_states(registry, "fmp_circuit")
    try:
        rows = conn.execute(
            "SELECT provider,state,consecutive_failures,consecutive_rate_limits,updated_at "
            "FROM provider_circuit_state ORDER BY provider LIMIT ?",
            (_MAX_ROWS,),
        ).fetchall()
        values = tuple(
            FMPCircuitRow(
                provider=str(row[0]),
                state=str(row[1]),
                consecutive_failures=int(row[2]),
                consecutive_rate_limits=int(row[3]),
                updated_at=row[4],
                state_registered=row[1] in registered,
            )
            for row in rows
        )
    except (sqlite3.Error, ValidationError, TypeError, ValueError) as exc:
        return FMPCircuitObservation(
            state="invalid",
            observed_at=observed_at,
            evidence_source=f"sqlite:{table}",
            detail=f"bounded read failed: {type(exc).__name__}",
        )
    drift = sum(not row.state_registered for row in values)
    future = sum(row.updated_at > observed_at for row in values)
    return FMPCircuitObservation(
        state="invalid" if drift or future else "current",
        observed_at=observed_at,
        evidence_source=f"sqlite:{table}",
        values=values,
        detail=(
            f"{drift} unregistered circuit states; {future} future-dated rows"
            if drift or future
            else None
        ),
    )


def collect_operations_snapshot(
    registry: OperationsRegistry,
    *,
    repo_root: Path,
    conn: sqlite3.Connection,
    observed_at: datetime,
    scheduler_receipt_path: Path | None = None,
    service_receipt_path: Path | None = None,
) -> OperationsSnapshot:
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    tables, metadata_error = _metadata_tables(conn)
    runs, sources, llm, backlog, circuit = _database_observations(
        conn, tables, registry, observed_at, metadata_error
    )
    scheduler = _runtime_state(
        receipt_path=scheduler_receipt_path,
        observed_at=observed_at,
        expected=tuple(task.task_name for task in registry.scheduled_tasks),
        kind="scheduler",
    )
    services = _runtime_state(
        receipt_path=service_receipt_path,
        observed_at=observed_at,
        expected=tuple(service.name for service in registry.services),
        kind="service",
    )
    return OperationsSnapshot(
        observed_at=observed_at,
        registry_version=registry.registry_version,
        database_identity=_database_identity(conn, observed_at),
        schema_revision=_schema_revision(conn, tables, registry, observed_at, metadata_error),
        scheduler=SchedulerObservation.model_validate(scheduler.model_dump()),
        services=ServiceObservation.model_validate(services.model_dump()),
        job_receipts=_job_receipts(registry, repo_root, observed_at),
        database_runs=runs,
        source_calls=sources,
        llm_calls=llm,
        fmp_backlog=backlog,
        fmp_circuit=circuit,
    )
