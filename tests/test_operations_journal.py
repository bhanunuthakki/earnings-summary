from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from operations.journal import (
    OperationConflictError,
    OperationCursor,
    OperationRequestInput,
    accept_operation_request,
    finish_operation,
    list_operations,
    make_command_sha256,
    make_operation_id,
    mark_operation_started,
)


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE operation_requests (
            operation_id TEXT PRIMARY KEY,
            idempotency_key_sha256 TEXT NOT NULL UNIQUE,
            request_sha256 TEXT NOT NULL,
            actor TEXT NOT NULL,
            job_name TEXT NOT NULL,
            trigger_kind TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            command_sha256 TEXT NOT NULL,
            write_sets_json TEXT NOT NULL,
            requested_at TEXT NOT NULL
        );
        CREATE TABLE operation_events (
            event_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL REFERENCES operation_requests(operation_id),
            event_kind TEXT NOT NULL,
            event_sha256 TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            status TEXT,
            exit_code INTEGER,
            severity TEXT,
            detail_code TEXT,
            detail_reason TEXT,
            UNIQUE(operation_id, event_kind)
        );
        """
    )


def _request(
    key: str,
    now: datetime,
    *,
    trace_id: str = "2" * 32,
) -> OperationRequestInput:
    return OperationRequestInput(
        idempotency_key=key,
        actor="task_scheduler",
        job_name="refresh_cache",
        trigger_kind="scheduled",
        trace_id=trace_id,
        stage="refresh_cache",
        scope={"job": "refresh_cache", "attempt": 1},
        command_sha256=make_command_sha256(["python", "refresh_cache.py"]),
        write_sets=("fmp-refresh",),
        requested_at=now,
    )


def test_accept_start_finish_are_separate_idempotent_and_payload_free() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _schema(conn)
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    key = "scheduled:refresh-cache:one"

    first = accept_operation_request(conn, _request(key, now))
    assert conn.execute("SELECT COUNT(*) FROM operation_events").fetchone()[0] == 0
    replay = accept_operation_request(conn, _request(key, now + timedelta(minutes=1)))
    assert first == replay
    assert first.operation_id == make_operation_id(key)
    assert conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0] == 1
    assert key not in str(tuple(conn.execute("SELECT * FROM operation_requests").fetchone()))

    started = mark_operation_started(conn, operation_id=first.operation_id, occurred_at=now)
    assert (
        mark_operation_started(
            conn, operation_id=first.operation_id, occurred_at=now + timedelta(minutes=1)
        )
        == started
    )
    terminal = finish_operation(
        conn,
        operation_id=first.operation_id,
        status="ok",
        exit_code=0,
        severity="info",
        occurred_at=now + timedelta(minutes=2),
    )
    assert terminal.detail_code == "job_ok"
    assert conn.execute("SELECT COUNT(*) FROM operation_events").fetchone()[0] == 2


def test_request_trace_and_terminal_conflicts_are_loud_without_mutation() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    now = datetime(2026, 8, 13, tzinfo=UTC)
    operation = accept_operation_request(conn, _request("same-key", now))
    with pytest.raises(OperationConflictError, match="canonical request hash"):
        accept_operation_request(conn, _request("same-key", now, trace_id="4" * 32))
    assert conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM operation_events").fetchone()[0] == 0

    finish_operation(
        conn,
        operation_id=operation.operation_id,
        status="failed",
        exit_code=1,
        severity="error",
        occurred_at=now,
    )
    with pytest.raises(OperationConflictError, match="terminal event"):
        finish_operation(
            conn,
            operation_id=operation.operation_id,
            status="ok",
            exit_code=0,
            severity="info",
            occurred_at=now + timedelta(hours=1),
        )


def test_terminal_detail_is_closed_bounded_and_status_is_closed() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    now = datetime(2026, 8, 13, tzinfo=UTC)
    operation = accept_operation_request(conn, _request("detail-key", now))
    terminal = finish_operation(
        conn,
        operation_id=operation.operation_id,
        status="failed",
        exit_code=1,
        severity="error",
        occurred_at=now,
        detail_reason="worker failed",
    )
    assert terminal.detail_reason == "terminal_detail_withheld"
    assert len(terminal.detail_reason) <= 240
    with pytest.raises(ValueError):
        finish_operation(
            conn,
            operation_id=operation.operation_id,
            status="cancelled",
            exit_code=2,
            severity="warning",
            occurred_at=now,
        )
    with pytest.raises(ValueError, match="integer"):
        finish_operation(
            conn,
            operation_id=operation.operation_id,
            status="failed",
            exit_code=cast(int, 1.5),
            severity="error",
            occurred_at=now,
        )


def test_reader_rejects_noncanonical_persisted_terminal_detail() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    now = datetime(2026, 8, 13, tzinfo=UTC)
    operation = accept_operation_request(conn, _request("poisoned-detail", now))
    finish_operation(
        conn,
        operation_id=operation.operation_id,
        status="failed",
        exit_code=1,
        severity="error",
        occurred_at=now,
        detail_reason="worker failed",
    )
    conn.execute(
        "UPDATE operation_events SET detail_reason='legacy free text' "
        "WHERE operation_id=? AND event_kind='terminal'",
        (operation.operation_id,),
    )
    conn.commit()

    with pytest.raises(ValueError, match="detail_reason"):
        finish_operation(
            conn,
            operation_id=operation.operation_id,
            status="failed",
            exit_code=1,
            severity="error",
            occurred_at=now,
            detail_reason="worker failed",
        )


def test_keyset_reads_are_bounded_stable_and_reject_poisoned_timestamps() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    start = datetime(2026, 8, 13, tzinfo=UTC)
    for ordinal in range(4):
        accept_operation_request(
            conn, _request(f"key-{ordinal}", start + timedelta(minutes=ordinal))
        )

    first = list_operations(conn, limit=2)
    second = list_operations(
        conn,
        limit=2,
        before=OperationCursor(first[-1].requested_at, first[-1].operation_id),
    )
    assert len(first) == len(second) == 2
    assert {row.operation_id for row in first}.isdisjoint(row.operation_id for row in second)

    conn.execute(
        "UPDATE operation_requests SET requested_at='not-a-time' WHERE operation_id=?",
        (second[-1].operation_id,),
    )
    with pytest.raises(ValueError):
        list_operations(conn, limit=10)


@pytest.mark.parametrize("limit", [0, 501])
def test_keyset_reads_reject_unbounded_limits(limit: int) -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    with pytest.raises(ValueError, match="limit"):
        list_operations(conn, limit=limit)
