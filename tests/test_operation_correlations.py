from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from operations.context import activate
from pipeline.run_accounting import start_run
from sources.registry import (
    CallStatus,
    PendingSourceCall,
    log_call,
    log_calls_batch,
    set_db_path,
)


def test_pipeline_attempt_inherits_operation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE pipeline_runs (
            pipeline_key TEXT PRIMARY KEY, directive TEXT, ticker_scope TEXT, first_started_at TEXT
        );
        CREATE TABLE pipeline_attempts (
            attempt_id TEXT PRIMARY KEY, pipeline_key TEXT, started_at TEXT, ended_at TEXT,
            status TEXT, error_summary TEXT, operation_id TEXT
        );
        CREATE TABLE operation_requests (operation_id TEXT PRIMARY KEY);
        CREATE TABLE ingestion_runs (
            run_id TEXT PRIMARY KEY, attempt_id TEXT, pipeline_key TEXT, started_at TEXT,
            ended_at TEXT, directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT
        );
        """
    )
    def accept_schema(_conn: sqlite3.Connection) -> None:
        return None

    monkeypatch.setattr("pipeline.run_accounting.require_current_for_write", accept_schema)
    operation_id = "operation:" + "a" * 64
    conn.execute("INSERT INTO operation_requests VALUES (?)", (operation_id,))
    with activate(operation_id=operation_id, trace_id="b" * 32, stage="unit-job"):
        run_id = start_run(conn, "unit", ["WIX"])
    assert (
        conn.execute(
            "SELECT operation_id FROM pipeline_attempts WHERE attempt_id=?", (run_id,)
        ).fetchone()[0]
        == operation_id
    )


def test_source_call_inherits_operation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "source.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE source_calls (id INTEGER PRIMARY KEY,source_name TEXT,kind TEXT,ticker TEXT,"
        "called_at TEXT,latency_ms INTEGER,status TEXT,http_code INTEGER,record_count INTEGER,"
        "notes TEXT,operation_id TEXT)"
    )
    conn.execute("CREATE TABLE operation_requests (operation_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    def connect_test_db(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(db_path)

    monkeypatch.setattr("sources.registry.connect_sqlite", connect_test_db)
    set_db_path(db_path)
    operation_id = "operation:" + "c" * 64
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO operation_requests VALUES (?)", (operation_id,))
    conn.commit()
    conn.close()
    try:
        with activate(operation_id=operation_id, trace_id="d" * 32, stage="unit-source"):
            log_call(source_name="sec", kind="facts", ticker="WIX", status=CallStatus.OK)
            log_calls_batch(
                [
                    PendingSourceCall(
                        source_name="fmp",
                        kind="profile",
                        ticker="WIX",
                        status=CallStatus.OK,
                    )
                ]
            )
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT operation_id FROM source_calls ORDER BY id").fetchall() == [
            (operation_id,),
            (operation_id,),
        ]
        conn.close()
    finally:
        set_db_path(tmp_path / "reset.db")
