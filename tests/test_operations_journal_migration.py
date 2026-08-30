from __future__ import annotations

import sqlite3
import sys
import textwrap
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from operations.journal import OperationRequestInput, accept_operation_request, finish_operation


def _config(db_path: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def _seed_legacy_correlations(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO pipeline_runs(pipeline_key,directive,ticker_scope,first_started_at) "
        "VALUES ('pipeline_legacy','legacy','[]','2026-08-13T00:00:00')"
    )
    conn.execute(
        "INSERT INTO pipeline_attempts(attempt_id,pipeline_key,started_at,status) "
        "VALUES ('attempt-legacy','pipeline_legacy','2026-08-13T00:00:00','ok')"
    )
    conn.execute("INSERT INTO source_calls(source_name,kind,status) VALUES ('sec','facts','ok')")
    conn.commit()
    conn.close()


def _noop_before_upgrade(_db_path: Path) -> None:
    return None


_REQUEST_INSERT = (
    "INSERT INTO operation_requests "
    "(operation_id,idempotency_key_sha256,request_sha256,actor,job_name,trigger_kind,"
    "trace_id,stage,scope_json,command_sha256,write_sets_json,requested_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _valid_request() -> list[object]:
    return [
        "operation:" + "a" * 64,
        "a" * 64,
        "b" * 64,
        "task_scheduler",
        "unit-job",
        "scheduled",
        "c" * 32,
        "unit-job",
        '{"job":"unit-job"}',
        "d" * 64,
        '["unit-lane"]',
        "2026-08-13T12:00:00.000000+00:00",
    ]


def test_0011_adds_append_only_journal_correlations_and_trace_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(
        tmp_path / "journal.db",
        upgrade_from="0010_add_rehearsal_io_indexes",
        before_upgrade=_seed_legacy_correlations,
    )
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    request_columns = {row[1] for row in conn.execute("PRAGMA table_info(operation_requests)")}
    event_columns = {row[1] for row in conn.execute("PRAGMA table_info(operation_events)")}
    assert request_columns == {
        "operation_id",
        "idempotency_key_sha256",
        "request_sha256",
        "actor",
        "job_name",
        "trigger_kind",
        "trace_id",
        "stage",
        "scope_json",
        "command_sha256",
        "write_sets_json",
        "requested_at",
    }
    assert event_columns == {
        "event_id",
        "operation_id",
        "event_kind",
        "event_sha256",
        "occurred_at",
        "status",
        "exit_code",
        "severity",
        "detail_code",
        "detail_reason",
    }
    forbidden = {"argv", "env", "stdout", "stderr", "prompt", "response", "url", "payload"}
    assert request_columns.isdisjoint(forbidden)
    assert event_columns.isdisjoint(forbidden)
    assert "operation_id" in {
        row[1] for row in conn.execute("PRAGMA table_info(pipeline_attempts)")
    }
    assert "operation_id" in {row[1] for row in conn.execute("PRAGMA table_info(source_calls)")}
    assert conn.execute(
        "SELECT operation_id FROM pipeline_attempts WHERE attempt_id='attempt-legacy'"
    ).fetchone() == (None,)
    assert conn.execute("SELECT operation_id FROM source_calls").fetchone() == (None,)

    indexes = {
        row[1]
        for row in conn.execute(
            "SELECT tbl_name,name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'"
        )
    }
    assert {
        "ix_operation_requests_requested_at_operation_id",
        "ix_pipeline_attempts_operation_id",
        "ix_source_calls_operation_id",
        "ix_operation_events_operation_id",
        "ix_llm_calls_trace_id_called_at",
    } <= indexes

    from runtime import job_runtime

    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db_path))

    def no_drift(
        _repo_root: Path,
        _job_name: str,
        *,
        code_root: Path | None = None,
    ) -> None:
        del code_root

    def child_ok(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        scheduler_owner: tuple[int, str | None] | None,
    ) -> int:
        del cwd, env, scheduler_owner
        return 0

    monkeypatch.setattr(job_runtime, "_schema_preflight", no_drift)
    monkeypatch.setattr(job_runtime, "_run_managed_child", child_ok)
    assert (
        job_runtime.run_job(
            repo_root=tmp_path,
            job_name="migration-rehearsal",
            write_sets=["rehearsal-lane"],
            command=["python", "-c", "pass"],
            idempotency_key="migration-rehearsal-one",
            actor="task_scheduler",
            trace_id="e" * 32,
            scope={"job": "migration-rehearsal"},
            trigger_kind="scheduled",
        )
        == 0
    )
    assert conn.execute(
        "SELECT actor,trigger_kind FROM operation_requests WHERE job_name='migration-rehearsal'"
    ).fetchone() == ("task_scheduler", "scheduled")
    assert conn.execute(
        "SELECT event_kind,status,exit_code,severity,detail_code FROM operation_events "
        "ORDER BY event_kind"
    ).fetchall() == [
        ("started", None, None, None, None),
        ("terminal", "ok", 0, "info", "job_ok"),
    ]

    values = _valid_request()
    conn.execute(_REQUEST_INSERT, values)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE operation_requests SET job_name='other' WHERE operation_id=?", (values[0],)
        )
    conn.close()


def test_migrated_db_correlates_pipeline_source_and_llm_without_time_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "correlation.db")
    root = Path(__file__).resolve().parents[1]
    child = tmp_path / "emit_correlations.py"
    child.write_text(
        textwrap.dedent(
            f"""
            import sqlite3
            import sys
            from datetime import UTC, datetime
            from pathlib import Path

            sys.path.insert(0, {str(root / "src")!r})
            import db
            from llm.ledger import record_llm_call
            from pipeline.run_accounting import start_run
            from sources.registry import CallStatus, log_call, set_db_path

            db_path = Path({str(db_path)!r})
            db.set_db_path(db_path)
            set_db_path(db_path)
            conn = sqlite3.connect(db_path)
            run_id = start_run(conn, "unit-correlation", ["WIX"])
            conn.close()
            log_call(source_name="sec", kind="facts", ticker="WIX", status=CallStatus.OK)
            record_llm_call(
                started_at=datetime.now(UTC),
                elapsed_ms=1,
                model="unit-model",
                prompt_sha="f" * 64,
                prompt_chars=1,
                purpose="unit_correlation",
                ticker="WIX",
                scope="unit",
                run_id=run_id,
            )
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db_path))
    from runtime import job_runtime

    assert (
        job_runtime.run_job(
            repo_root=root,
            job_name="correlation-integration",
            write_sets=["correlation-lane"],
            command=[
                sys.executable,
                str(root / "execution" / "sqlite_bootstrap.py"),
                str(child),
            ],
            idempotency_key="correlation-integration-one",
            actor="task_scheduler",
            trace_id="9" * 32,
            scope={"job": "correlation-integration"},
            trigger_kind="scheduled",
        )
        == 0
    )
    conn = sqlite3.connect(db_path)
    operation_id, trace_id = conn.execute(
        "SELECT operation_id,trace_id FROM operation_requests "
        "WHERE job_name='correlation-integration'"
    ).fetchone()
    assert conn.execute(
        "SELECT operation_id FROM pipeline_attempts WHERE operation_id IS NOT NULL"
    ).fetchone() == (operation_id,)
    assert conn.execute(
        "SELECT operation_id FROM source_calls WHERE operation_id IS NOT NULL"
    ).fetchone() == (operation_id,)
    assert conn.execute("SELECT trace_id FROM llm_calls WHERE trace_id IS NOT NULL").fetchone() == (
        trace_id,
    )
    plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM llm_calls WHERE trace_id=? ORDER BY called_at DESC",
            (trace_id,),
        )
    )
    assert "ix_llm_calls_trace_id_called_at" in plan
    conn.close()


@pytest.mark.parametrize(
    ("position", "unsafe_value"),
    [
        (3, "https://credential.example"),
        (4, "prompt-export"),
        (6, "argv"),
        (7, "env-stage"),
        (8, '{"url":"https://credential.example"}'),
        (8, '{"safe":"api_key=supersecret"}'),
        (10, '["https://credential.example"]'),
        (11, "not-a-timestamp"),
        (4, "j" * 129),
    ],
)
def test_0011_rejects_unsafe_direct_operation_request_inserts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    position: int,
    unsafe_value: object,
) -> None:
    db_path = migrated_db(tmp_path / f"unsafe-{position}-{len(str(unsafe_value))}.db")
    conn = sqlite3.connect(db_path)
    values = _valid_request()
    values[position] = unsafe_value
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(_REQUEST_INSERT, values)
    assert conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone() == (0,)
    conn.close()


@pytest.mark.parametrize(
    "terminal",
    [
        ("cancelled", 1, "warning", "job_cancelled", None, "2026-08-13T12:01:00+00:00"),
        ("failed", 1.5, "error", "job_failed", None, "2026-08-13T12:01:00+00:00"),
        ("failed", 1, "error", "job_failed", "api_key=supersecret", "2026-08-13T12:01:00+00:00"),
        ("failed", 1, "error", "job_failed", "worker failed", "2026-08-13T12:01:00+00:00"),
        (
            "failed",
            1,
            "error",
            "job_failed",
            "https://user:" + "password@example.test",
            "2026-08-13T12:01:00+00:00",
        ),
        ("failed", 1, "error", "job_failed", None, "not-a-timestamp"),
    ],
)
def test_0011_rejects_noncanonical_terminal_events(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    terminal: tuple[object, ...],
) -> None:
    db_path = migrated_db(tmp_path / f"unsafe-event-{terminal[0]}-{terminal[1]}.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    values = _valid_request()
    conn.execute(_REQUEST_INSERT, values)
    status, exit_code, severity, detail_code, detail_reason, occurred_at = terminal
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operation_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "operation-event:" + "e" * 64,
                values[0],
                "terminal",
                "f" * 64,
                occurred_at,
                status,
                exit_code,
                severity,
                detail_code,
                detail_reason,
            ),
        )
    conn.close()


def test_0011_api_terminal_falls_back_when_redaction_still_violates_privacy_check(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(
        tmp_path / "journal-redaction.db",
        upgrade_from="0010_add_rehearsal_io_indexes",
        before_upgrade=_noop_before_upgrade,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    request = accept_operation_request(
        conn,
        OperationRequestInput(
            idempotency_key="migration-redaction-safe",
            actor="task_scheduler",
            job_name="unit-job",
            trigger_kind="scheduled",
            trace_id="c" * 32,
            stage="unit-job",
            scope={"job": "unit-job"},
            command_sha256="d" * 64,
            write_sets=("unit-lane",),
            requested_at=now,
        ),
    )
    sentinel = "JOURNAL-RAW-CREDENTIAL-7319"

    terminal = finish_operation(
        conn,
        operation_id=request.operation_id,
        status="failed",
        exit_code=1,
        severity="error",
        occurred_at=now,
        detail_reason=f"https://example.test/failure?api_key={sentinel}",
    )

    assert terminal.detail_reason
    assert terminal.detail_reason == "terminal_detail_withheld"
    assert len(terminal.detail_reason) <= 240
    assert sentinel not in terminal.detail_reason
    assert "api_key=" not in terminal.detail_reason.casefold()
    assert "://" not in terminal.detail_reason
    persisted = conn.execute(
        "SELECT detail_reason FROM operation_events WHERE operation_id=? AND event_kind='terminal'",
        (request.operation_id,),
    ).fetchone()[0]
    assert persisted == terminal.detail_reason

    second = accept_operation_request(
        conn,
        OperationRequestInput(
            idempotency_key="migration-redaction-direct-sql",
            actor="task_scheduler",
            job_name="unit-job",
            trigger_kind="scheduled",
            trace_id="e" * 32,
            stage="unit-job",
            scope={"job": "unit-job"},
            command_sha256="f" * 64,
            write_sets=("unit-lane",),
            requested_at=now,
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operation_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "operation-event:" + "a" * 64,
                second.operation_id,
                "terminal",
                "b" * 64,
                now.isoformat(timespec="microseconds"),
                "failed",
                1,
                "error",
                "job_failed",
                f"api_key={sentinel}",
            ),
        )
    conn.close()


def test_operations_journal_is_excluded_from_gc_retention() -> None:
    from execution.db_gc import TELEMETRY_TABLES

    retained = {table for table, _timestamp_column in TELEMETRY_TABLES}
    assert "operation_requests" not in retained
    assert "operation_events" not in retained


def test_0011_downgrade_removes_only_journal_additions(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "journal-down.db")
    config = _config(db_path)
    command.downgrade(config, "0010_add_rehearsal_io_indexes")
    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "operation_requests" not in tables
    assert "operation_events" not in tables
    assert "operation_id" not in {
        row[1] for row in conn.execute("PRAGMA table_info(pipeline_attempts)")
    }
    assert "operation_id" not in {row[1] for row in conn.execute("PRAGMA table_info(source_calls)")}
    assert "ix_llm_calls_trace_id_called_at" not in {
        row[1] for row in conn.execute("PRAGMA index_list(llm_calls)")
    }
    conn.close()
