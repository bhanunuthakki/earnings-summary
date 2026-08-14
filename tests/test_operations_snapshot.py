from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from operations.registry import build_operations_registry
from operations.snapshot import (
    _database_identity,  # pyright: ignore[reportPrivateUsage]
    _metadata_tables,  # pyright: ignore[reportPrivateUsage]
    collect_operations_snapshot,
)
from runtime.job_runtime import health_receipt_directory

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _runtime_receipt(
    path: Path, registry: object, recorded: datetime, *, service: bool = False
) -> None:
    if service:
        services = getattr(registry, "services")
        payload = {
            "schema_version": "1",
            "observed_at": recorded.isoformat(),
            "services": [{"name": row.name, "state": "Stopped"} for row in services],
        }
    else:
        tasks = getattr(registry, "scheduled_tasks")
        payload = {
            "schema_version": "1",
            "observed_at": recorded.isoformat(),
            "tasks": [{"task_name": row.task_name, "state": "Ready"} for row in tasks],
        }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _job_receipt(path: Path, *, job: str, lane: tuple[str, ...], ended: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "job": job,
                "write_sets": list(lane),
                "started_at": (ended - timedelta(minutes=1)).isoformat(),
                "ended_at": ended.isoformat(),
                "status": "ok",
                "exit_code": 0,
                "severity": "info",
                "detail": None,
            }
        ),
        encoding="utf-8",
    )


def test_job_health_model_accepts_historical_v1_and_correlated_v2() -> None:
    from operations.models import JobHealthRow

    base = {
        "job": "unit-job",
        "write_sets": ["unit-lane"],
        "started_at": "2026-08-13T00:00:00+00:00",
        "ended_at": "2026-08-13T00:01:00+00:00",
        "status": "ok",
        "exit_code": 0,
        "severity": "info",
        "detail": None,
    }
    assert JobHealthRow.model_validate({"schema_version": "1", **base}).operation_id is None
    v2 = JobHealthRow.model_validate(
        {
            "schema_version": "2",
            **base,
            "operation_id": "operation:" + "a" * 64,
            "trigger_kind": "scheduled",
            "journal_state": "complete",
        }
    )
    assert v2.trigger_kind == "scheduled"
    unavailable = JobHealthRow.model_validate(
        {
            "schema_version": "2",
            **base,
            "operation_id": "operation:" + "b" * 64,
            "trigger_kind": "service",
            "journal_state": "unavailable",
            "journal_detail_code": "terminal_unavailable",
            "journal_reason": "OperationalError: database is locked",
        }
    )
    assert unavailable.journal_detail_code == "terminal_unavailable"
    with pytest.raises(ValidationError):
        JobHealthRow.model_validate(
            {
                "schema_version": "2",
                **base,
                "operation_id": "operation:" + "c" * 64,
                "trigger_kind": "scheduled",
                "journal_state": "unavailable",
            }
        )


def test_snapshot_is_caller_connection_only_truthful_and_creates_no_files(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    observed_at = datetime(2026, 8, 13, 12, tzinfo=UTC)
    snapshot = collect_operations_snapshot(
        build_operations_registry(PROJECT_ROOT),
        repo_root=tmp_path,
        conn=conn,
        observed_at=observed_at,
    )
    assert snapshot.scheduler.state == "missing"
    assert snapshot.services.state == "missing"
    assert snapshot.job_receipts
    assert all(item.state == "missing" for item in snapshot.job_receipts)
    assert snapshot.schema_revision.state == "missing"
    assert not tuple(tmp_path.iterdir())
    conn.execute("SELECT 1")


def test_snapshot_reads_bounded_rows_and_exact_runtime_receipts(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ingestion_runs (run_id TEXT, directive TEXT, status TEXT, "
        "started_at TEXT, ended_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO ingestion_runs VALUES (?,?,?,?,?)",
        (
            (
                f"r{index}",
                "morning_pipeline",
                "ok",
                "2026-08-13T10:00:00+00:00",
                "2026-08-13T10:01:00+00:00",
            )
            for index in range(105)
        ),
    )
    registry = build_operations_registry(PROJECT_ROOT)
    recorded = datetime(2026, 8, 13, 11, 55, tzinfo=UTC)
    scheduler = tmp_path / "scheduler.json"
    services = tmp_path / "services.json"
    _runtime_receipt(scheduler, registry, recorded)
    _runtime_receipt(services, registry, recorded, service=True)
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=conn,
        observed_at=recorded + timedelta(minutes=10),
        scheduler_receipt_path=scheduler,
        service_receipt_path=services,
    )
    assert snapshot.scheduler.state == "current"
    assert snapshot.services.state == "current"
    assert len(snapshot.database_runs.values) == 100
    assert conn.in_transaction
    with pytest.raises(ValidationError):
        setattr(snapshot.database_runs.values[0], "status", "changed")
    nested = snapshot.database_runs.values[0].model_dump()
    nested["unexpected"] = True
    with pytest.raises(ValidationError):
        type(snapshot.database_runs.values[0]).model_validate(nested)


def test_runtime_receipts_distinguish_partial_duplicate_future_stale_and_invalid(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    registry = build_operations_registry(PROJECT_ROOT)
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    path = tmp_path / "scheduler.json"
    path.write_text("not json", encoding="utf-8")
    invalid = collect_operations_snapshot(
        registry, repo_root=tmp_path, conn=conn, observed_at=now, scheduler_receipt_path=path
    )
    assert invalid.scheduler.state == "invalid"

    _runtime_receipt(path, registry, now - timedelta(days=1))
    stale = collect_operations_snapshot(
        registry, repo_root=tmp_path, conn=conn, observed_at=now, scheduler_receipt_path=path
    )
    assert stale.scheduler.state == "stale"

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tasks"] = payload["tasks"][:1]
    path.write_text(json.dumps(payload), encoding="utf-8")
    partial = collect_operations_snapshot(
        registry, repo_root=tmp_path, conn=conn, observed_at=now, scheduler_receipt_path=path
    )
    assert partial.scheduler.state == "invalid"
    assert any(row.registry_match == "missing" for row in partial.scheduler.values)

    payload["tasks"] = [payload["tasks"][0], payload["tasks"][0]]
    path.write_text(json.dumps(payload), encoding="utf-8")
    duplicate = collect_operations_snapshot(
        registry, repo_root=tmp_path, conn=conn, observed_at=now, scheduler_receipt_path=path
    )
    assert duplicate.scheduler.state == "invalid"

    _runtime_receipt(path, registry, now + timedelta(minutes=1))
    future = collect_operations_snapshot(
        registry, repo_root=tmp_path, conn=conn, observed_at=now, scheduler_receipt_path=path
    )
    assert future.scheduler.state == "invalid"


def test_runtime_receipt_existing_directories_are_invalid(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    scheduler = tmp_path / "scheduler"
    services = tmp_path / "services"
    scheduler.mkdir()
    services.mkdir()
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=now,
        scheduler_receipt_path=scheduler,
        service_receipt_path=services,
    )
    assert snapshot.scheduler.state == "invalid"
    assert snapshot.services.state == "invalid"
    assert snapshot.scheduler.evidence_source == str(scheduler)
    assert snapshot.services.evidence_source == str(services)


def test_schema_queue_and_llm_drift_are_explicit(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE alembic_version(version_num TEXT);"
        "INSERT INTO alembic_version VALUES ('wrong_head');"
        "CREATE TABLE fmp_work_backlog(state TEXT);"
        "INSERT INTO fmp_work_backlog VALUES ('ALIEN');"
        "CREATE TABLE llm_calls(purpose TEXT,model TEXT,called_at TEXT,elapsed_ms INTEGER);"
        "INSERT INTO llm_calls VALUES ('alien_purpose','alien_model',"
        "'2026-08-13T11:00:00+00:00',10);"
    )
    snapshot = collect_operations_snapshot(
        build_operations_registry(PROJECT_ROOT),
        repo_root=tmp_path,
        conn=conn,
        observed_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
    )
    assert snapshot.schema_revision.state == "invalid"
    assert snapshot.fmp_backlog.state == "invalid"
    assert snapshot.llm_calls.state == "invalid"


@pytest.mark.parametrize(("state", "expected"), [("CLOSED", "current"), ("ALIEN", "invalid")])
def test_provider_circuit_state_is_observed_and_registry_bound(
    tmp_path: Path, state: str, expected: str
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE provider_circuit_state ("
        "provider TEXT, state TEXT, consecutive_failures INTEGER, "
        "consecutive_rate_limits INTEGER, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO provider_circuit_state VALUES (?,?,?,?,?)",
        ("fmp", state, 0, 0, "2026-08-13T11:00:00+00:00"),
    )
    snapshot = collect_operations_snapshot(
        build_operations_registry(PROJECT_ROOT),
        repo_root=tmp_path,
        conn=conn,
        observed_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
    )
    assert snapshot.fmp_circuit.state == expected
    assert len(snapshot.fmp_circuit.values) == 1


def test_job_receipts_read_only_canonical_latest_and_ignore_legacy_history(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    registry = build_operations_registry(PROJECT_ROOT)
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    job = "morning_pipeline"
    lane = next(step.effective_lane for step in registry.job_steps if step.job == job)
    directory = health_receipt_directory(tmp_path, job)
    directory.mkdir(parents=True)

    for index in range(281):
        (directory / f"legacy-{index:03d}.json").write_text("not json", encoding="utf-8")
    _job_receipt(directory / "latest.json", job=job, lane=lane, ended=now - timedelta(minutes=1))
    snapshot = collect_operations_snapshot(registry, repo_root=tmp_path, conn=conn, observed_at=now)
    morning = next(item for item in snapshot.job_receipts if item.job == job)
    assert morning.state == "current"
    assert morning.receipt is not None
    assert morning.receipt.ended_at == now - timedelta(minutes=1)
    assert morning.evidence_source == str(directory / "latest.json")
    assert any(item.state == "missing" for item in snapshot.job_receipts)


def test_job_receipt_future_clock_is_invalid(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    registry = build_operations_registry(PROJECT_ROOT)
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    step = registry.job_steps[0]
    directory = health_receipt_directory(tmp_path, step.job)
    directory.mkdir(parents=True)
    _job_receipt(
        directory / "latest.json",
        job=step.job,
        lane=step.effective_lane,
        ended=now + timedelta(minutes=1),
    )
    snapshot = collect_operations_snapshot(registry, repo_root=tmp_path, conn=conn, observed_at=now)
    receipt = next(item for item in snapshot.job_receipts if item.job == step.job)
    assert receipt.state == "invalid"


def test_current_v2_journal_failure_is_attention_first_invalid(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    registry = build_operations_registry(PROJECT_ROOT)
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    step = registry.job_steps[0]
    latest = health_receipt_directory(tmp_path, step.job) / "latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(
        json.dumps(
            {
                "schema_version": "2",
                "job": step.job,
                "write_sets": list(step.effective_lane),
                "started_at": (now - timedelta(minutes=2)).isoformat(),
                "ended_at": (now - timedelta(minutes=1)).isoformat(),
                "status": "ok",
                "exit_code": 0,
                "severity": "info",
                "detail": None,
                "operation_id": "operation:" + "d" * 64,
                "trigger_kind": "scheduled",
                "journal_state": "unavailable",
                "journal_detail_code": "terminal_unavailable",
                "journal_reason": "OperationalError: database is locked",
            }
        ),
        encoding="utf-8",
    )
    snapshot = collect_operations_snapshot(registry, repo_root=tmp_path, conn=conn, observed_at=now)
    receipt = next(item for item in snapshot.job_receipts if item.job == step.job)
    assert receipt.state == "invalid"
    assert "terminal_unavailable" in (receipt.detail or "")


@pytest.mark.parametrize(
    ("case", "expected_detail"),
    [
        ("wrong_type", "not a regular file"),
        ("oversize", "exceeds 64 KiB"),
        ("malformed", "schema validation failed"),
    ],
)
def test_job_receipt_invalid_canonical_file_classes_are_explicit(
    tmp_path: Path, case: str, expected_detail: str
) -> None:
    conn = sqlite3.connect(":memory:")
    registry = build_operations_registry(PROJECT_ROOT)
    step = registry.job_steps[0]
    latest = health_receipt_directory(tmp_path, step.job) / "latest.json"
    latest.parent.mkdir(parents=True)
    if case == "wrong_type":
        latest.mkdir()
    elif case == "oversize":
        latest.write_bytes(b"x" * (64 * 1024 + 1))
    else:
        latest.write_text("not json", encoding="utf-8")

    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=conn,
        observed_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
    )
    receipt = next(item for item in snapshot.job_receipts if item.job == step.job)
    assert receipt.state == "invalid"
    assert receipt.evidence_source == str(latest)
    assert expected_detail in (receipt.detail or "")


def test_job_receipt_nonexistent_canonical_file_is_missing_with_exact_path(
    tmp_path: Path,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    step = registry.job_steps[0]
    latest = health_receipt_directory(tmp_path, step.job) / "latest.json"
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
    )
    receipt = next(item for item in snapshot.job_receipts if item.job == step.job)
    assert receipt.state == "missing"
    assert receipt.evidence_source == str(latest)


def test_job_receipt_unreadable_canonical_file_is_invalid_with_exact_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    step = registry.job_steps[0]
    latest = health_receipt_directory(tmp_path, step.job) / "latest.json"
    _job_receipt(
        latest,
        job=step.job,
        lane=step.effective_lane,
        ended=datetime(2026, 8, 13, 11, 59, tzinfo=UTC),
    )
    monkeypatch.setattr(Path, "open", Mock(side_effect=PermissionError("denied")))
    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
    )
    receipt = next(item for item in snapshot.job_receipts if item.job == step.job)
    assert receipt.state == "invalid"
    assert receipt.evidence_source == str(latest)
    assert receipt.detail == "PermissionError"


def test_runtime_receipt_extras_are_sorted(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    scheduler = tmp_path / "scheduler.json"
    service = tmp_path / "service.json"
    _runtime_receipt(scheduler, registry, now)
    _runtime_receipt(service, registry, now, service=True)
    scheduler_payload = json.loads(scheduler.read_text(encoding="utf-8"))
    scheduler_payload["tasks"].extend(
        [
            {"task_name": r"\z-extra", "state": "Ready"},
            {"task_name": r"\a-extra", "state": "Ready"},
        ]
    )
    scheduler.write_text(json.dumps(scheduler_payload), encoding="utf-8")
    service_payload = json.loads(service.read_text(encoding="utf-8"))
    service_payload["services"].extend(
        [
            {"name": "z-extra", "state": "Running"},
            {"name": "a-extra", "state": "Running"},
        ]
    )
    service.write_text(json.dumps(service_payload), encoding="utf-8")

    snapshot = collect_operations_snapshot(
        registry,
        repo_root=tmp_path,
        conn=sqlite3.connect(":memory:"),
        observed_at=now,
        scheduler_receipt_path=scheduler,
        service_receipt_path=service,
    )
    assert [
        row.task_name for row in snapshot.scheduler.values if row.registry_match == "unexpected"
    ] == [
        r"\a-extra",
        r"\z-extra",
    ]
    assert [row.name for row in snapshot.services.values if row.registry_match == "unexpected"] == [
        "a-extra",
        "z-extra",
    ]


class _MetadataDeniedConnection:
    class _DatabaseList:
        @staticmethod
        def fetchmany(_size: int) -> list[tuple[int, str, str]]:
            return [(0, "main", "")]

    def execute(self, sql: str, parameters: object = ()) -> object:
        del parameters
        if "sqlite_master" in sql:
            raise sqlite3.OperationalError("denied")
        if sql == "PRAGMA database_list":
            return self._DatabaseList()
        raise AssertionError(f"unexpected read after metadata denial: {sql}")


def test_metadata_denial_is_invalid_not_exception(tmp_path: Path) -> None:
    snapshot = collect_operations_snapshot(
        build_operations_registry(PROJECT_ROOT),
        repo_root=tmp_path,
        conn=_MetadataDeniedConnection(),  # type: ignore[arg-type]
        observed_at=datetime(2026, 8, 13, 12, tzinfo=UTC),
    )
    assert snapshot.schema_revision.state == "invalid"
    assert snapshot.database_runs.state == "invalid"


class _RowsCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.fetch_size: int | None = None

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        self.fetch_size = size
        return self.rows[:size]

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class _DatabaseListConnection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.cursor = _RowsCursor(rows)

    def execute(self, sql: str, parameters: object = ()) -> _RowsCursor:
        del parameters
        assert sql == "PRAGMA database_list"
        return self.cursor


@pytest.mark.parametrize(
    "rows",
    [
        [(0, "aux", "aux.db")],
        [(0, "main", "a.db"), (1, "main", "b.db")],
    ],
)
def test_database_identity_requires_exactly_one_main(
    rows: list[tuple[object, ...]],
) -> None:
    conn = _DatabaseListConnection(rows)
    observation = _database_identity(
        cast(sqlite3.Connection, conn), datetime(2026, 8, 13, 12, tzinfo=UTC)
    )
    assert observation.state == "invalid"
    assert observation.detail == "database identity requires exactly one main schema"
    assert conn.cursor.fetch_size == 101


def test_metadata_inventory_is_query_bounded() -> None:
    calls: list[tuple[str, object]] = []

    class _MetadataConnection:
        def execute(self, sql: str, parameters: object = ()) -> _RowsCursor:
            calls.append((sql, parameters))
            return _RowsCursor([("alembic_version",)])

    tables, error = _metadata_tables(cast(sqlite3.Connection, _MetadataConnection()))
    assert tables == {"alembic_version"}
    assert error is None
    sql, parameters = calls[0]
    assert " LIMIT ?" in sql
    assert isinstance(parameters, tuple)
    assert parameters[-1] == 100
