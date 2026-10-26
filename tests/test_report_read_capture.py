"""Bounded report SQL capture tests: one execution, frozen commitments, replay.

The recorder is exercised only through its owner-controlled session and the
caller's ordinary parse paths (execute / fetchone / fetchmany / fetchall /
iteration). Every refusal below must be the typed ``ReadCaptureUnavailable``
carrying its reason code; a partial or degraded receipt is never accepted.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from report import read_capture
from report.read_capture import (
    CapturedParameters,
    CapturedValue,
    CaptureLimits,
    ReadCaptureSession,
    ReadCaptureUnavailable,
    RecordingManagedConnection,
    replay_read_capture,
)
from sqlite_runtime import (
    SQLITE_BUSY_TIMEOUT_MS,
    SQLiteConnectionRole,
    connect_sqlite,
)

_BIG_NOTE = "n" * 6000

#: Caller-owned explicit replay budgets (bounded deadline; never assumed).
REPLAY_LIMITS = CaptureLimits(execution_deadline_s=5.0)


def _build_capture_db(path: Path) -> None:
    """Create one disposable report-shaped database for capture tests."""
    conn = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
    try:
        conn.executescript(
            """
            CREATE TABLE metrics (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                price REAL NOT NULL,
                note TEXT,
                payload BLOB
            );
            CREATE TABLE facts (
                metric_id INTEGER NOT NULL REFERENCES metrics(id),
                label TEXT NOT NULL
            );
            INSERT INTO metrics (id, ticker, price, note, payload) VALUES
                (1, 'AAA', 10.5, 'first', x'00ff'),
                (2, 'BBB', 20.25, 'second', NULL),
                (3, 'CCC', 30.0, 'small', NULL),
                (4, 'DDD', 40.0, 'small', NULL);
            INSERT INTO facts (metric_id, label) VALUES (1, 'alpha'), (2, 'beta');
            """
        )
        conn.execute("UPDATE metrics SET note = :big WHERE id IN (3, 4)", {"big": _BIG_NOTE})
        conn.commit()
    finally:
        conn.close()


def _open_session(
    path: Path,
    *,
    limits: CaptureLimits | None = None,
    allowed_functions: frozenset[str] | None = None,
) -> tuple[ReadCaptureSession, RecordingManagedConnection]:
    session = ReadCaptureSession(
        path,
        limits=limits,
        allowed_functions=allowed_functions,
    )
    conn = session.open()
    assert isinstance(conn, RecordingManagedConnection)
    return session, conn


def _refuses_to_seal(session: ReadCaptureSession) -> None:
    """A refused capture must never seal a partial receipt."""
    with pytest.raises(ReadCaptureUnavailable, match="capture_invalidated"):
        session.finish()


def test_buffered_fetch_paths_match_the_underlying_read(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()

    cursor = conn.execute("SELECT id, ticker, note FROM metrics WHERE id <= 2 ORDER BY id")
    first = cursor.fetchone()
    assert first is not None
    assert tuple(first) == (1, "AAA", "first")
    assert first["ticker"] == "AAA"
    assert first.keys() == ["id", "ticker", "note"]
    assert [tuple(row) for row in cursor.fetchmany(1)] == [(2, "BBB", "second")]
    assert cursor.fetchall() == []
    assert cursor.fetchone() is None

    iterate = conn.execute("SELECT ticker FROM metrics ORDER BY id")
    assert [tuple(row) for row in iterate] == [
        ("AAA",),
        ("BBB",),
        ("CCC",),
        ("DDD",),
    ]

    batched = conn.execute("SELECT metric_id FROM facts ORDER BY metric_id")
    # The default sqlite3 arraysize is 1: the buffered paths mirror the
    # underlying read exactly.
    assert batched.arraysize == 1
    assert [tuple(row) for row in batched.fetchmany()] == [(1,)]
    assert [tuple(row) for row in batched] == [(2,)]
    assert batched.fetchmany() == []

    receipt = session.finish()
    assert receipt.schema_version == "report_read_capture.v1"
    assert [statement.sql for statement in receipt.statements] == [
        "SELECT id, ticker, note FROM metrics WHERE id <= 2 ORDER BY id",
        "SELECT ticker FROM metrics ORDER BY id",
        "SELECT metric_id FROM facts ORDER BY metric_id",
    ]
    assert receipt.total_rows == 8


def test_statement_is_executed_exactly_once_per_commitment(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path, allowed_functions=frozenset({"tick_counter"}))
    calls: list[str] = []

    def tick_counter() -> str:
        calls.append("tick")
        return "tick"

    conn.create_function("tick_counter", 0, tick_counter, deterministic=True)
    session.begin_capture()

    cursor = conn.execute("SELECT tick_counter() AS t")
    assert [tuple(row) for row in cursor.fetchall()] == [("tick",)]
    assert cursor.fetchall() == []
    assert cursor.fetchone() is None
    assert calls == ["tick"]

    again = conn.execute("SELECT tick_counter() AS t")
    assert tuple(next(iter(again))) == ("tick",)
    assert calls == ["tick", "tick"]

    receipt = session.finish()
    assert len(receipt.statements) == 2
    assert all(statement.row_count == 1 for statement in receipt.statements)


def test_binding_normalization_roundtrips_through_replay(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()

    moment = datetime(2024, 1, 2, 3, 4, 5)
    conn.execute("SELECT ?, ?, ?, ?, ?", (None, 7, 2.5, "txt", b"\x00\xff"))
    conn.execute("SELECT :d, :day", {"d": moment, "day": date(2024, 1, 2)})
    conn.execute("SELECT price FROM metrics WHERE ticker = ?", ("AAA",))

    receipt = session.finish()

    first, second, third = receipt.statements
    assert first.parameters.style == "positional"
    assert first.parameters.keys is None
    assert [value.kind for value in first.parameters.values] == [
        "null",
        "int",
        "float",
        "text",
        "blob",
    ]
    assert first.parameters.values[1].value == 7
    assert first.parameters.values[3].value == "txt"
    assert first.parameters.values[4].value == b"\x00\xff".hex()

    assert second.parameters.style == "named"
    assert second.parameters.keys == ("d", "day")
    assert second.parameters.values[0] == CapturedValue(kind="text", value="2024-01-02 03:04:05")
    assert second.parameters.values[1] == CapturedValue(kind="text", value="2024-01-02")

    assert third.row_count == 1
    assert third.parameters.values[0].value == "AAA"

    replayed = replay_read_capture(path, receipt, limits=REPLAY_LIMITS)
    assert replayed.semantic_sha256 == receipt.semantic_sha256
    for original, fresh in zip(receipt.statements, replayed.statements, strict=True):
        assert fresh.result_sha256 == original.result_sha256


def test_empty_aggregate_cte_join_and_master_commitments(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()

    empty = conn.execute("SELECT id FROM metrics WHERE 1 = 0")
    assert empty.fetchall() == []

    counted = conn.execute("SELECT COUNT(*) AS n FROM facts")
    assert [tuple(row) for row in counted] == [(2,)]

    cte = conn.execute("WITH top AS (SELECT ticker FROM metrics WHERE id <= 1) SELECT * FROM top")
    assert [tuple(row) for row in cte] == [("AAA",)]

    joined = conn.execute(
        "SELECT m.ticker, f.label FROM metrics m JOIN facts f ON f.metric_id = m.id"
        " WHERE m.id = 1 ORDER BY m.id"
    )
    assert [tuple(row) for row in joined] == [("AAA", "alpha")]

    membership = conn.execute(
        "SELECT name FROM main.sqlite_master WHERE type = 'table' ORDER BY name"
    )
    assert [tuple(row) for row in membership] == [("facts",), ("metrics",)]

    introspection = conn.execute("PRAGMA table_info(metrics)")
    assert [tuple(row) for row in introspection][:1] == [(0, "id", "INTEGER", 0, None, 1)]
    versioned = conn.execute("PRAGMA user_version")
    assert [tuple(row) for row in versioned] == [(0,)]

    receipt = session.finish()
    assert [statement.row_count for statement in receipt.statements] == [0, 1, 1, 1, 2, 5, 1]
    introspection_statement = receipt.statements[5]
    assert introspection_statement.sql == "PRAGMA table_info(metrics)"
    assert list(introspection_statement.columns) == [
        "cid",
        "name",
        "type",
        "notnull",
        "dflt_value",
        "pk",
    ]

    replayed = replay_read_capture(path, receipt, limits=REPLAY_LIMITS)
    assert replayed.semantic_sha256 == receipt.semantic_sha256


def test_diagnostic_pragmas_stay_out_of_the_semantic_digest(tmp_path: Path) -> None:
    digests: list[str] = []
    for index in range(2):
        path = tmp_path / f"twin-{index}.db"
        _build_capture_db(path)
        session, conn = _open_session(path)
        session.begin_capture()
        conn.execute("PRAGMA database_list")
        conn.execute("PRAGMA data_version")
        conn.execute("SELECT COUNT(*) FROM facts")
        receipt = session.finish()

        assert receipt.diagnostics == ("PRAGMA database_list", "PRAGMA data_version")
        assert [statement.sql for statement in receipt.statements] == ["SELECT COUNT(*) FROM facts"]
        digests.append(receipt.semantic_sha256)

    assert digests[0] == digests[1]


@pytest.mark.parametrize(
    ("limits_kwargs", "sql", "parameters", "reason"),
    [
        (
            {"max_statements": 2},
            "SELECT COUNT(*) FROM facts",
            None,
            "statement_limit_exceeded",
        ),
        (
            {"max_rows_per_query": 1},
            "SELECT id FROM metrics ORDER BY id",
            None,
            "query_row_limit_exceeded",
        ),
        (
            {"max_total_rows": 3},
            "SELECT id FROM metrics ORDER BY id",
            None,
            "total_row_limit_exceeded",
        ),
        (
            {"max_sql_bytes": 100},
            "SELECT " + ("1 + " * 60) + "1",
            None,
            "sql_length_limit_exceeded",
        ),
        (
            {"max_parameters": 1},
            "SELECT ?, ?",
            (1, 2),
            "parameter_count_limit_exceeded",
        ),
        (
            {"max_parameter_bytes": 8},
            "SELECT ?",
            ("0123456789",),
            "parameter_bytes_limit_exceeded",
        ),
        (
            {"max_row_bytes_per_query": 10000},
            "SELECT note FROM metrics WHERE id >= 3",
            None,
            "query_byte_limit_exceeded",
        ),
        (
            {"max_total_row_bytes": 8},
            "SELECT id FROM metrics ORDER BY id",
            None,
            "total_byte_limit_exceeded",
        ),
        (
            {"max_instructions": 2000},
            "WITH RECURSIVE cnt(x) AS ("
            "SELECT 1 UNION ALL SELECT x + 1 FROM cnt WHERE x < 100000"
            ") SELECT COUNT(*) FROM cnt",
            None,
            "instruction_budget_exhausted",
        ),
    ],
)
def test_budgets_refuse_and_invalidate_before_sealing(
    tmp_path: Path,
    limits_kwargs: dict[str, Any],
    sql: str,
    parameters: object,
    reason: str,
) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path, limits=CaptureLimits(**limits_kwargs))
    session.begin_capture()

    with pytest.raises(ReadCaptureUnavailable, match=reason):
        for _ in range(5):
            if parameters is None:
                conn.execute(sql)
            else:
                conn.execute(sql, parameters)

    _refuses_to_seal(session)


def test_execution_deadline_is_monotonic_and_enforced(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path, limits=CaptureLimits(execution_deadline_s=0.001))
    session.begin_capture()

    time.sleep(0.05)
    with pytest.raises(ReadCaptureUnavailable, match="execution_deadline_exceeded"):
        conn.execute("SELECT COUNT(*) FROM facts")
    _refuses_to_seal(session)


def test_unsupported_binding_types_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)

    session, conn = _open_session(path)
    session.begin_capture()
    with pytest.raises(ReadCaptureUnavailable, match="unsupported_parameter_type"):
        conn.execute("SELECT ?", (object(),))
    _refuses_to_seal(session)

    session, conn = _open_session(path)
    session.begin_capture()
    with pytest.raises(ReadCaptureUnavailable, match="non-finite float"):
        conn.execute("SELECT ?", (float("nan"),))
    _refuses_to_seal(session)


def test_pragma_assignments_are_denied_and_invalidate(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)

    for pragma_stmt in (
        "PRAGMA foreign_keys = ON",
        "PRAGMA query_only = ON",
        "PRAGMA busy_timeout = 5000",
    ):
        session, conn = _open_session(path)
        session.begin_capture()
        with pytest.raises(ReadCaptureUnavailable, match="sandbox_denied_pragma_write"):
            conn.execute(pragma_stmt)
        _refuses_to_seal(session)


def test_transaction_boundaries_invalidate_the_recording(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)

    for sql in ("COMMIT", "ROLLBACK", "END"):
        session, conn = _open_session(path)
        session.begin_capture()
        with pytest.raises(ReadCaptureUnavailable, match="transaction_boundary_during_capture"):
            conn.execute(sql)
        _refuses_to_seal(session)

    session, conn = _open_session(path)
    session.begin_capture()
    with pytest.raises(ReadCaptureUnavailable, match="transaction_boundary_during_capture"):
        conn.commit()
    _refuses_to_seal(session)

    session, conn = _open_session(path)
    session.begin_capture()
    with pytest.raises(ReadCaptureUnavailable, match="transaction_boundary_during_capture"):
        conn.rollback()
    _refuses_to_seal(session)

    session, conn = _open_session(path)
    session.begin_capture()
    with pytest.raises(ReadCaptureUnavailable, match="transaction_boundary_during_capture"), conn:
        conn.execute("SELECT COUNT(*) FROM facts")
    _refuses_to_seal(session)


def test_pre_existing_user_callbacks_block_recorder_ownership(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)

    def permissive_authorizer(
        action: int,
        arg1: str | None,
        arg2: str | None,
        db_name: str | None,
        trigger: str | None,
    ) -> int:
        return sqlite3.SQLITE_OK

    session, conn = _open_session(path)
    conn.set_authorizer(permissive_authorizer)
    with pytest.raises(ReadCaptureUnavailable, match="capture_callback_conflict"):
        session.begin_capture()
    session.abort()

    session, conn = _open_session(path)
    conn.set_progress_handler(lambda: 0, 1000)
    with pytest.raises(ReadCaptureUnavailable, match="capture_callback_conflict"):
        session.begin_capture()
    session.abort()


def test_recorder_callbacks_are_never_replaced_mid_capture(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()

    with pytest.raises(ReadCaptureUnavailable, match="capture_callback_conflict"):
        conn.set_authorizer(None)
    with pytest.raises(ReadCaptureUnavailable, match="capture_callback_conflict"):
        conn.set_progress_handler(lambda: 0, 1000)
    with pytest.raises(ReadCaptureUnavailable, match="capture_callback_conflict"):
        conn.set_progress_handler(None)

    conn.execute("SELECT COUNT(*) FROM facts")
    receipt = session.finish()
    assert len(receipt.statements) == 1


def test_unrecordable_execution_paths_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()

    with pytest.raises(ReadCaptureUnavailable, match="unsupported_cursor_factory"):
        conn.cursor(sqlite3.Cursor)
    with pytest.raises(ReadCaptureUnavailable, match="unrecordable_execution_path"):
        conn.executemany("SELECT ?", [(1,), (2,)])
    _refuses_to_seal(session)

    # The executemany refusal invalidated the recording; executescript needs
    # its own recording to refuse.
    session, conn = _open_session(path)
    session.begin_capture()
    with pytest.raises(ReadCaptureUnavailable, match="unrecordable_execution_path"):
        conn.executescript("SELECT 1;")
    _refuses_to_seal(session)


def test_sql_sandbox_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)

    denials: list[tuple[str, str]] = [
        ("SELECT randomblob(16)", "sandbox_denied_function"),
        (
            "INSERT INTO facts (metric_id, label) VALUES (1, 'x')",
            "sandbox_denied_action",
        ),
        ("PRAGMA journal_mode = wal", "sandbox_denied_pragma_write"),
        ("PRAGMA user_version = 5", "sandbox_denied_pragma_write"),
        ("PRAGMA page_size", "sandbox_denied_pragma"),
        ("ATTACH DATABASE ':memory:' AS extra", "sandbox_denied_action"),
    ]
    for sql, reason in denials:
        session, conn = _open_session(path)
        session.begin_capture()
        with pytest.raises(ReadCaptureUnavailable, match=reason):
            conn.execute(sql)
        _refuses_to_seal(session)


def test_replay_refuses_schema_and_result_drift(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()
    conn.execute("SELECT price FROM metrics WHERE id = 1")
    conn.execute("SELECT label FROM facts ORDER BY label")
    receipt = session.finish()

    writer = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
    try:
        writer.execute("UPDATE metrics SET price = 99.5 WHERE id = 1")
        writer.commit()
    finally:
        writer.close()

    with pytest.raises(ReadCaptureUnavailable, match="replay_result_mismatch"):
        replay_read_capture(path, receipt, limits=REPLAY_LIMITS)

    writer = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
    try:
        writer.executescript("ALTER TABLE metrics ADD COLUMN extra TEXT")
        writer.commit()
    finally:
        writer.close()

    with pytest.raises(ReadCaptureUnavailable, match="replay_schema_mismatch"):
        replay_read_capture(path, receipt, limits=REPLAY_LIMITS)


def test_tampered_receipt_is_untrusted_and_refused(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()
    conn.execute("SELECT COUNT(*) FROM facts")
    receipt = session.finish()

    forged_result = receipt.statements[0].model_copy(update={"result_sha256": "0" * 64})
    tampered = receipt.model_copy(update={"statements": (forged_result,)})
    with pytest.raises(ReadCaptureUnavailable, match="replay_result_mismatch"):
        replay_read_capture(path, tampered, limits=REPLAY_LIMITS)

    unsupported = receipt.model_copy(update={"schema_version": "report_read_capture.v0"})
    with pytest.raises(ReadCaptureUnavailable, match="unsupported_receipt_version"):
        replay_read_capture(path, unsupported, limits=REPLAY_LIMITS)


def test_identical_databases_produce_identical_semantic_digests(tmp_path: Path) -> None:
    digests: list[str] = []
    for index in range(2):
        path = tmp_path / f"twin-{index}.db"
        _build_capture_db(path)
        session, conn = _open_session(path)
        session.begin_capture()
        conn.execute("SELECT ticker, price FROM metrics ORDER BY id")
        conn.execute("SELECT label FROM facts ORDER BY metric_id")
        receipt = session.finish()
        digests.append(receipt.semantic_sha256)
        assert receipt.total_rows == 6

        replayed = replay_read_capture(path, receipt, limits=REPLAY_LIMITS)
        assert replayed.semantic_sha256 == receipt.semantic_sha256

    assert digests[0] == digests[1]


def test_trace_observer_sees_each_statement_once(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    observed: list[str] = []
    conn.register_trace_observer(observed.append)
    session.begin_capture()

    watched = "SELECT COUNT(*) FROM facts"
    conn.execute(watched)
    session.finish()

    assert observed.count(watched) == 1
    assert "BEGIN" in observed


def test_capture_limits_validation_and_session_lifecycle(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)

    with pytest.raises(ValidationError):
        CaptureLimits(max_statements=0)
    with pytest.raises(ValidationError):
        CaptureLimits(execution_deadline_s=0)

    session = ReadCaptureSession(path)
    with pytest.raises(ReadCaptureUnavailable, match="session_not_open"):
        session.begin_capture()

    with session as active:
        conn = active.open()
        active.begin_capture()
        conn.execute("SELECT COUNT(*) FROM facts")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT COUNT(*) FROM facts")

    session = ReadCaptureSession(path)
    conn = session.open()
    session.begin_capture()
    conn.execute("SELECT COUNT(*) FROM facts")
    receipt = session.finish()
    assert len(receipt.statements) == 1
    with pytest.raises(ReadCaptureUnavailable, match="session_not_capturing"):
        session.finish()

    session = ReadCaptureSession(path)
    session.open()
    with pytest.raises(ReadCaptureUnavailable, match="session_already_open"):
        session.open()
    session.abort()


def test_engine_local_limits_are_enforced_before_the_first_row(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    limits = CaptureLimits(max_row_bytes_per_query=2048)
    session, conn = _open_session(path, limits=limits)
    session.begin_capture()

    # The engine itself refuses the oversized string (SQLITE_LIMIT_LENGTH is
    # clamped through the public setlimit seam) before any row is produced;
    # the SQLite error is translated to a typed budget exhaustion refusal.
    with pytest.raises(ReadCaptureUnavailable, match="budget_exhausted"):
        conn.execute("SELECT note FROM metrics WHERE id = 3")
    _refuses_to_seal(session)

    # Readback proof: the engine categories are actually clamped.
    clamped = CaptureLimits(max_columns=4, max_expression_depth=8, max_parameters=5)
    session, conn = _open_session(path, limits=clamped)
    session.begin_capture()
    assert conn.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) == 0
    assert conn.getlimit(sqlite3.SQLITE_LIMIT_COLUMN) <= 4
    assert conn.getlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH) <= 8
    assert conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) <= 5
    conn.execute("SELECT COUNT(*) FROM facts")
    receipt = session.finish()
    assert len(receipt.statements) == 1


def test_connection_policy_applies_to_recording_connections(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)

    assert conn.row_factory is sqlite3.Row
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()
    assert foreign_keys is not None
    assert int(foreign_keys[0]) == 1
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()
    assert busy_timeout is not None
    assert int(busy_timeout[0]) == SQLITE_BUSY_TIMEOUT_MS
    session.abort()


def test_autocommit_and_isolation_level_mutations_refuse_and_fail_to_seal(tmp_path: Path) -> None:
    path = tmp_path / "wal_capture.db"
    _build_capture_db(path)
    writer = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.commit()
    finally:
        writer.close()

    # 1. Test autocommit mutation during active capture
    session, conn = _open_session(path)
    session.begin_capture()
    first = conn.execute("SELECT COUNT(*) FROM facts").fetchone()
    assert first is not None
    assert int(first[0]) == 2

    writer = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
    try:
        writer.execute("INSERT INTO facts (metric_id, label) VALUES (1, 'gamma')")
        writer.commit()
    finally:
        writer.close()

    with pytest.raises(ReadCaptureUnavailable, match="transaction_boundary_violated"):
        conn.autocommit = True
    _refuses_to_seal(session)

    # 2. Test isolation_level mutation during active capture
    session2, conn2 = _open_session(path)
    session2.begin_capture()
    second = conn2.execute("SELECT COUNT(*) FROM facts").fetchone()
    assert second is not None
    assert int(second[0]) == 3

    writer = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
    try:
        writer.execute("INSERT INTO facts (metric_id, label) VALUES (2, 'delta')")
        writer.commit()
    finally:
        writer.close()

    with pytest.raises(ReadCaptureUnavailable, match="transaction_boundary_violated"):
        conn2.isolation_level = None
    _refuses_to_seal(session2)


@pytest.mark.parametrize("sql", ["SAVEPOINT sp", "RELEASE sp", "ROLLBACK TO sp"])
def test_raw_sqlite_transaction_statements_are_denied_by_authorizer(
    tmp_path: Path, sql: str
) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()
    with pytest.raises(
        ReadCaptureUnavailable,
        match=r"transaction_control_forbidden|transaction_boundary_during_capture",
    ):
        conn.execute(sql)
    _refuses_to_seal(session)


def test_repeated_cached_diagnostic_pragmas_stay_out_of_statements_across_twin_paths(
    tmp_path: Path,
) -> None:
    digests: list[str] = []
    receipts: list[Any] = []
    for idx in range(2):
        twin_path = tmp_path / f"twin_{idx}" / "capture.db"
        twin_path.parent.mkdir(parents=True, exist_ok=True)
        _build_capture_db(twin_path)
        session, conn = _open_session(twin_path)
        session.begin_capture()

        # First execution prepares statement (authorizer callback invoked)
        diag_sql = "  PRAGMA database_list -- trailing comment\n"
        rows1 = conn.execute(diag_sql).fetchall()
        assert len(rows1) >= 1
        # Semantic query
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone() is not None
        # Second execution hits prepared statement cache (authorizer callback skipped)
        rows2 = conn.execute(diag_sql).fetchall()
        assert len(rows2) >= 1

        receipt = session.finish()
        receipts.append(receipt)
        digests.append(receipt.semantic_sha256)

    # Exactly 1 statement (only the SELECT statement), NOT 2 or 3
    assert len(receipts[0].statements) == 1
    assert len(receipts[1].statements) == 1
    assert receipts[0].statements[0].sql == "SELECT COUNT(*) FROM facts"
    # Both database_list executions are captured into diagnostics
    assert len(receipts[0].diagnostics) == 2
    assert len(receipts[1].diagnostics) == 2
    # Semantic commitments ignore absolute database paths, producing identical digests
    assert digests[0] == digests[1]


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA busy_timeout = 1",
        "PRAGMA foreign_keys = OFF",
        "PRAGMA query_only = OFF",
    ],
)
def test_mutable_pragma_assignments_are_denied_during_capture(tmp_path: Path, sql: str) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()
    with pytest.raises(ReadCaptureUnavailable, match="sandbox_denied_pragma_write"):
        conn.execute(sql)
    _refuses_to_seal(session)


def test_single_row_prefetch_budget_stop(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    writer = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=False)
    try:
        writer.execute(
            "CREATE TABLE payload_test (id INTEGER PRIMARY KEY, note1 TEXT NOT NULL, note2 TEXT NOT NULL)"
        )
        writer.execute("INSERT INTO payload_test VALUES (1, ?, ?)", ("a" * 50, "a" * 50))
        writer.execute("INSERT INTO payload_test VALUES (2, ?, ?)", ("b" * 80, "b" * 80))
        writer.execute("INSERT INTO payload_test VALUES (3, ?, ?)", ("c" * 80, "c" * 80))
        writer.execute("INSERT INTO payload_test VALUES (4, ?, ?)", ("d" * 80, "d" * 80))
        writer.execute("INSERT INTO payload_test VALUES (5, ?, ?)", ("e" * 80, "e" * 80))
        writer.commit()
    finally:
        writer.close()

    calls: list[int] = []

    def track_call(row_id: int) -> int:
        calls.append(row_id)
        return row_id

    # max_row_bytes_per_query=200: row 1 (108 bytes) fits; row 2 (168 bytes; total 276 bytes) exceeds
    limits = CaptureLimits(max_row_bytes_per_query=200)
    session, conn = _open_session(path, limits=limits, allowed_functions=frozenset({"track_call"}))
    conn.create_function("track_call", 1, track_call, deterministic=True)
    session.begin_capture()

    with pytest.raises(ReadCaptureUnavailable, match="query_byte_limit_exceeded"):
        conn.execute("SELECT track_call(id), note1, note2 FROM payload_test ORDER BY id")

    # Prefetch fetched row 1 (fit budget), attempted row 2 (exceeded budget and halted).
    # Because Python sqlite3.Cursor.fetchone steps to the lookahead row upon returning row 2,
    # row 3 is stepped, but subsequent rows (4 and 5) are never fetched or stepped.
    assert 1 in calls
    assert 2 in calls
    assert 4 not in calls
    assert 5 not in calls
    _refuses_to_seal(session)


def test_replay_pre_decode_parameter_budget_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()
    conn.execute("SELECT price FROM metrics WHERE id = 1")
    receipt = session.finish()

    # Craft an oversized hex blob parameter in the manifest (4000 hex chars = 2000 bytes)
    oversized_hex = "00" * 2000
    oversized_param = CapturedParameters(
        style="positional",
        values=(CapturedValue(kind="blob", value=oversized_hex),),
    )
    tampered_stmt = receipt.statements[0].model_copy(
        update={"sql": "SELECT ? AS b", "parameters": oversized_param}
    )
    tampered_receipt = receipt.model_copy(update={"statements": (tampered_stmt,)})

    decode_calls: list[CapturedValue] = []
    original_decode = getattr(read_capture, "_normalized_python_value")

    def tracking_decode(value: CapturedValue) -> object:
        decode_calls.append(value)
        return original_decode(value)

    monkeypatch.setattr(read_capture, "_normalized_python_value", tracking_decode)

    # max_parameter_bytes=100: must be rejected before decode/bytes.fromhex
    with pytest.raises(ReadCaptureUnavailable, match="budget_exhausted"):
        replay_read_capture(path, tampered_receipt, limits=CaptureLimits(max_parameter_bytes=100))

    assert len(decode_calls) == 0


def test_cursor_compatibility_against_standard_sqlite_cursor(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()

    cursor = conn.cursor()
    cursor.execute("SELECT id, ticker FROM metrics ORDER BY id")

    # 1. Setting cursor.row_factory after execute and before fetchone
    cursor.row_factory = sqlite3.Row
    first = cursor.fetchone()
    assert isinstance(first, sqlite3.Row)
    assert first["ticker"] == "AAA"

    # 2. Switching back to None (tuple) before fetchall
    cursor.row_factory = None
    remaining = cursor.fetchall()
    assert len(remaining) == 3
    assert isinstance(remaining[0], tuple)
    assert not isinstance(remaining[0], sqlite3.Row)
    assert remaining[0] == (2, "BBB")

    # 3. arraysize default and custom values on fetchmany
    cursor2 = conn.cursor()
    cursor2.execute("SELECT id FROM metrics ORDER BY id")
    assert cursor2.arraysize == 1
    assert [r[0] for r in cursor2.fetchmany()] == [1]
    cursor2.arraysize = 2
    assert [r[0] for r in cursor2.fetchmany()] == [2, 3]
    assert [r[0] for r in cursor2.fetchmany()] == [4]

    # 4. Rejecting custom lambda row_factory
    def custom_row_factory(c: sqlite3.Cursor, r: tuple[Any, ...]) -> dict[str, Any]:
        return dict(zip([str(d[0]) for d in (c.description or ())], r))

    with pytest.raises(ReadCaptureUnavailable, match="unsupported_factory"):
        cursor2.row_factory = custom_row_factory

    with pytest.raises(ReadCaptureUnavailable, match="unsupported_factory"):
        conn.row_factory = custom_row_factory

    # 5. Rejecting unsupported text_factory
    def custom_text_factory(b: bytes) -> str:
        return b.decode("latin1")

    with pytest.raises(ReadCaptureUnavailable, match="unsupported_factory"):
        conn.text_factory = custom_text_factory

    with pytest.raises(ReadCaptureUnavailable, match="unsupported_factory"):
        conn.text_factory = bytes

    session.abort()


def test_replay_metadata_tampering_separate_checks(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path)
    session.begin_capture()
    conn.execute("SELECT id, ticker FROM metrics ORDER BY id")
    receipt = session.finish()
    stmt = receipt.statements[0]

    # 1. Tamper columns only
    tampered_cols = receipt.model_copy(
        update={"statements": (stmt.model_copy(update={"columns": ("id", "other")}),)}
    )
    with pytest.raises(ReadCaptureUnavailable, match="replay_result_mismatch"):
        replay_read_capture(path, tampered_cols, limits=REPLAY_LIMITS)

    # 2. Tamper column_types only
    tampered_types = receipt.model_copy(
        update={"statements": (stmt.model_copy(update={"column_types": ("INTEGER", "FLOAT")}),)}
    )
    with pytest.raises(ReadCaptureUnavailable, match="replay_result_mismatch"):
        replay_read_capture(path, tampered_types, limits=REPLAY_LIMITS)

    # 3. Tamper row_count only
    tampered_count = receipt.model_copy(
        update={"statements": (stmt.model_copy(update={"row_count": 999}),)}
    )
    with pytest.raises(ReadCaptureUnavailable, match="replay_result_mismatch"):
        replay_read_capture(path, tampered_count, limits=REPLAY_LIMITS)


def test_capture_pre_normalization_parameter_budget_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)
    session, conn = _open_session(path, limits=CaptureLimits(max_parameter_bytes=100))
    session.begin_capture()

    norm_calls: list[object] = []
    original_norm = getattr(read_capture, "_normalize_parameter")

    def tracking_norm(value: object) -> CapturedValue:
        norm_calls.append(value)
        return original_norm(value)

    monkeypatch.setattr(read_capture, "_normalize_parameter", tracking_norm)

    oversized_blob = b"x" * 2000
    with pytest.raises(ReadCaptureUnavailable, match="parameter_bytes_limit_exceeded"):
        conn.execute("SELECT ? AS b", (oversized_blob,))

    assert len(norm_calls) == 0
    session.abort()


def test_user_callbacks_registered_via_kwargs_raise_conflict(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)

    # 1. Authorizer registered via keyword argument
    session, conn = _open_session(path)

    def dummy_authorizer(*_args: object) -> int:
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer_callback=dummy_authorizer)
    with pytest.raises(ReadCaptureUnavailable, match="capture_callback_conflict"):
        session.begin_capture()
    session.abort()

    # 2. Progress handler registered via keyword arguments
    session, conn = _open_session(path)
    conn.set_progress_handler(progress_handler=lambda: 0, n=10)
    with pytest.raises(ReadCaptureUnavailable, match="capture_callback_conflict"):
        session.begin_capture()
    session.abort()


def test_rollback_observers_are_notified_on_finish_and_abort(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)

    # 1. Finish path notifies registered rollback observer
    session, conn = _open_session(path)
    finish_notifications = 0

    def on_finish_rollback() -> None:
        nonlocal finish_notifications
        finish_notifications += 1

    conn.register_rollback_observer(on_finish_rollback)
    session.begin_capture()
    conn.execute("SELECT COUNT(*) FROM facts")
    receipt = session.finish()
    assert len(receipt.statements) == 1
    assert finish_notifications == 1

    # 2. Abort path notifies registered rollback observer
    session2, conn2 = _open_session(path)
    abort_notifications = 0

    def on_abort_rollback() -> None:
        nonlocal abort_notifications
        abort_notifications += 1

    conn2.register_rollback_observer(on_abort_rollback)
    session2.begin_capture()
    conn2.execute("SELECT COUNT(*) FROM facts")
    session2.abort()
    assert abort_notifications == 1


def test_built_in_glob_and_like_allowed_under_capture(tmp_path: Path) -> None:
    path = tmp_path / "capture.db"
    _build_capture_db(path)

    session, conn = _open_session(path)
    session.begin_capture()
    cursor = conn.execute("SELECT label FROM facts WHERE label GLOB 'alph*' AND label LIKE '%pha'")
    rows = cursor.fetchall()
    assert len(rows) == 1
    receipt = session.finish()
    assert len(receipt.statements) == 1
    replay_receipt = replay_read_capture(path, receipt, limits=CaptureLimits())
    assert len(replay_receipt.statements) == 1
