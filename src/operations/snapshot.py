from __future__ import annotations

import sqlite3
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, TypeVar, cast

from pydantic import BaseModel, TypeAdapter, ValidationError

from operations.models import (
    RUNTIME_PAIR_RECEIPT_FILENAME,
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
    RuntimeProbeAttempt,
    RuntimeReceiptPair,
    SchedulerExpectation,
    SchedulerObservation,
    SchedulerReceipt,
    SchedulerRuntimeReceipt,
    SchedulerTaskRow,
    SchedulerTaskState,
    SchemaRevisionObservation,
    SchemaRevisionRow,
    ServiceObservation,
    ServiceReceipt,
    ServiceRow,
    ServiceRuntimeReceipt,
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


def _read_runtime_receipt(
    path: Path, kind: Literal["scheduler", "service"]
) -> tuple[RuntimeProbeAttempt | None, SchedulerReceipt | ServiceReceipt | None]:
    """Accept historical v1 evidence and the v2 probe-attempt envelope."""

    try:
        with path.open("rb") as handle:
            payload = handle.read(_MAX_RECEIPT_BYTES + 1)
    except OSError as exc:
        raise _ReceiptError(type(exc).__name__) from exc
    if len(payload) > _MAX_RECEIPT_BYTES:
        raise _ReceiptError("receipt exceeds 64 KiB")
    if path.name == RUNTIME_PAIR_RECEIPT_FILENAME:
        try:
            pair = RuntimeReceiptPair.model_validate_json(payload)
        except ValidationError as exc:
            raise _ReceiptError("receipt schema validation failed") from exc
        if kind == "scheduler":
            return pair.scheduler.probe_attempt, pair.scheduler.last_successful
        return pair.services.probe_attempt, pair.services.last_successful
    adapter: (
        TypeAdapter[SchedulerReceipt | SchedulerRuntimeReceipt]
        | TypeAdapter[ServiceReceipt | ServiceRuntimeReceipt]
    )
    if kind == "scheduler":
        adapter = TypeAdapter(SchedulerReceipt | SchedulerRuntimeReceipt)
    else:
        adapter = TypeAdapter(ServiceReceipt | ServiceRuntimeReceipt)
    try:
        parsed = adapter.validate_json(payload)
    except ValidationError as exc:
        raise _ReceiptError("receipt schema validation failed") from exc
    if isinstance(parsed, (SchedulerRuntimeReceipt, ServiceRuntimeReceipt)):
        return parsed.probe_attempt, parsed.last_successful
    return None, parsed


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
    scheduler_expectations: dict[str, SchedulerExpectation] | None = None,
    receipt_override: SchedulerRuntimeReceipt | ServiceRuntimeReceipt | None = None,
    receipt_error: str | None = None,
) -> SchedulerObservation | ServiceObservation:
    source = f"{kind}:cached_receipt"

    def missing_values() -> tuple[SchedulerTaskRow, ...] | tuple[ServiceRow, ...]:
        if kind == "scheduler":
            return tuple(
                SchedulerTaskRow(
                    task_name=name,
                    state="Missing",
                    registry_match="missing",
                    scheduler_expectation=(scheduler_expectations or {}).get(name.casefold()),
                )
                for name in expected
            )
        return tuple(
            ServiceRow(name=name, state="Missing", registry_match="missing") for name in expected
        )

    if receipt_error is not None:
        observation_type = SchedulerObservation if kind == "scheduler" else ServiceObservation
        return observation_type(
            state="invalid",
            observed_at=observed_at,
            evidence_source=str(receipt_path),
            detail=receipt_error,
        )
    if receipt_override is not None:
        if kind == "scheduler":
            runtime_receipt = cast(SchedulerRuntimeReceipt, receipt_override)
            probe_attempt = runtime_receipt.probe_attempt
            receipt: SchedulerReceipt | ServiceReceipt | None = runtime_receipt.last_successful
        else:
            runtime_receipt = cast(ServiceRuntimeReceipt, receipt_override)
            probe_attempt = runtime_receipt.probe_attempt
            receipt = runtime_receipt.last_successful
    elif receipt_path is None:
        if kind == "scheduler":
            return SchedulerObservation(
                state="missing",
                observed_at=observed_at,
                evidence_source=source,
                values=cast(tuple[SchedulerTaskRow, ...], missing_values()),
                detail="no typed scheduler receipt supplied",
            )
        return ServiceObservation(
            state="missing",
            observed_at=observed_at,
            evidence_source=source,
            values=cast(tuple[ServiceRow, ...], missing_values()),
            detail="no typed service receipt supplied",
        )
    else:
        try:
            receipt_stat = receipt_path.lstat()
        except FileNotFoundError:
            if kind == "scheduler":
                return SchedulerObservation(
                    state="missing",
                    observed_at=observed_at,
                    evidence_source=str(receipt_path),
                    values=cast(tuple[SchedulerTaskRow, ...], missing_values()),
                    detail="typed scheduler receipt is absent",
                )
            return ServiceObservation(
                state="missing",
                observed_at=observed_at,
                evidence_source=str(receipt_path),
                values=cast(tuple[ServiceRow, ...], missing_values()),
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
            probe_attempt, receipt = _read_runtime_receipt(receipt_path, kind)
        except _ReceiptError as exc:
            observation_type = SchedulerObservation if kind == "scheduler" else ServiceObservation
            return observation_type(
                state="invalid",
                observed_at=observed_at,
                evidence_source=str(receipt_path),
                detail=str(exc),
            )

    try:
        if receipt is None:
            supplied: list[tuple[str, SchedulerTaskState | ServiceState]] = []
            recorded_at: datetime | None = None
        elif kind == "scheduler":
            scheduler_receipt = cast(SchedulerReceipt, receipt)
            supplied = [(row.task_name, row.state) for row in scheduler_receipt.tasks]
            recorded_at = scheduler_receipt.observed_at
        else:
            service_receipt = cast(ServiceReceipt, receipt)
            supplied = [(row.name, row.state) for row in service_receipt.services]
            recorded_at = service_receipt.observed_at
        names = [name.casefold() for name, _state in supplied]
        if len(names) != len(set(names)):
            raise _ReceiptError(f"duplicate {kind} identities")
        if recorded_at is not None and recorded_at > observed_at:
            raise _ReceiptError(f"future {kind} receipt clock")
        if probe_attempt is not None and probe_attempt.attempted_at > observed_at:
            raise _ReceiptError(f"future {kind} probe clock")
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
    age_stale = recorded_at is not None and observed_at - recorded_at > _RUNTIME_RECEIPT_TTL
    state = "stale" if age_stale else "current"
    detail_parts: list[str] = []
    if gaps or extras:
        detail_parts.append(f"missing={len(gaps)} unexpected={len(extras)}")
    if age_stale:
        detail_parts.append("cached runtime receipt is older than 15 minutes")
    detail = "; ".join(detail_parts) or None
    if age_stale and recorded_at is not None:
        detail = f"{detail}; retained successful evidence at {recorded_at.isoformat()}"
    if kind == "scheduler":
        values: list[SchedulerTaskRow] = []
        policy_violations: list[str] = []
        for key, name in expected_by_key.items():
            task_state = cast(
                SchedulerTaskState,
                supplied_by_key[key][1] if key in supplied_by_key else "Missing",
            )
            expectation = (scheduler_expectations or {}).get(key)
            expectation_match = (
                task_state in {"Ready", "Running"}
                if expectation == "required_enabled"
                else task_state == "Disabled"
                if expectation == "required_disabled"
                else task_state == "Missing"
                if expectation == "absent_service_owned"
                else None
            )
            attention_detail = None
            if expectation_match is False:
                attention_detail = f"Scheduler state {task_state} violates {expectation}"
                policy_violations.append(name)
            values.append(
                SchedulerTaskRow(
                    task_name=name,
                    state=task_state,
                    registry_match="expected" if key in supplied_by_key else "missing",
                    scheduler_expectation=expectation,
                    expectation_match=expectation_match,
                    attention_detail=attention_detail,
                )
            )
        values.extend(
            SchedulerTaskRow(
                task_name=supplied_by_key[key][0],
                state=cast(SchedulerTaskState, supplied_by_key[key][1]),
                registry_match="unexpected",
            )
            for key in sorted(extras)
        )
        if policy_violations:
            policy_detail = f"scheduler expectation violations={len(policy_violations)}"
            detail = f"{detail}; {policy_detail}" if detail else policy_detail
        if probe_attempt is not None and probe_attempt.availability == "unavailable":
            state = "unavailable"
            retained = (
                f"; retained successful evidence at {recorded_at.isoformat()}"
                if recorded_at is not None
                else ""
            )
            detail = f"{probe_attempt.detail}{retained}"
        return SchedulerObservation(
            state=state,
            observed_at=observed_at,
            evidence_source=str(receipt_path),
            evidence_recorded_at=(
                probe_attempt.attempted_at if probe_attempt is not None else recorded_at
            ),
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
    if probe_attempt is not None and probe_attempt.availability == "unavailable":
        state = "unavailable"
        retained = (
            f"; retained successful evidence at {recorded_at.isoformat()}"
            if recorded_at is not None
            else ""
        )
        detail = f"{probe_attempt.detail}{retained}"
    return ServiceObservation(
        state=state,
        observed_at=observed_at,
        evidence_source=str(receipt_path),
        evidence_recorded_at=(
            probe_attempt.attempted_at if probe_attempt is not None else recorded_at
        ),
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
            if record.schema_version == "2" and record.journal_state == "unavailable":
                state = "invalid"
                detail = (
                    f"operation journal unavailable: {record.journal_detail_code}: "
                    f"{record.journal_reason}"
                )
            elif observed_at - record.ended_at > timedelta(seconds=ttl_seconds):
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
    pair_base = scheduler_receipt_path or service_receipt_path
    pair_path = (
        pair_base.with_name(RUNTIME_PAIR_RECEIPT_FILENAME) if pair_base is not None else None
    )
    pair_available = pair_path is not None and pair_path.exists()
    pair_receipt: RuntimeReceiptPair | None = None
    pair_error: str | None = None
    if pair_available and pair_path is not None:
        try:
            pair_receipt = _read_receipt(pair_path, RuntimeReceiptPair)
        except _ReceiptError as exc:
            pair_error = str(exc)
    scheduler_source = pair_path if pair_available else scheduler_receipt_path
    service_source = pair_path if pair_available else service_receipt_path
    scheduler = _runtime_state(
        receipt_path=scheduler_source,
        observed_at=observed_at,
        expected=tuple(task.task_name for task in registry.scheduled_tasks),
        kind="scheduler",
        scheduler_expectations={
            task.task_name.casefold(): task.scheduler_expectation
            for task in registry.scheduled_tasks
        },
        receipt_override=pair_receipt.scheduler if pair_receipt is not None else None,
        receipt_error=pair_error,
    )
    services = _runtime_state(
        receipt_path=service_source,
        observed_at=observed_at,
        expected=tuple(service.name for service in registry.services),
        kind="service",
        receipt_override=pair_receipt.services if pair_receipt is not None else None,
        receipt_error=pair_error,
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
