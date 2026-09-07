"""Regression coverage for the shared direct-SQLite connection policy."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import sqlite_runtime
from schema_compat import SchemaRevisionMismatch
from scope_identity import derive_retrieval_scope_id
from sqlite_runtime import (
    SQLiteConnectionRole,
    connect_sqlite,
    sqlite_version_is_wal_reset_safe,
)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ((3, 50, 6), False),
        ((3, 50, 7), True),
        ((3, 51, 2), False),
        ((3, 51, 3), True),
        ((3, 53, 4), True),
        ((4, 0, 0), True),
    ],
)
def test_wal_reset_version_boundary(version: tuple[int, int, int], expected: bool) -> None:
    assert sqlite_version_is_wal_reset_safe(version) is expected


def test_writer_refuses_unsafe_sqlite_before_touching_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "must-not-exist.db"
    monkeypatch.setattr(
        sqlite_runtime,
        "_WRITER_SQLITE_VERSION_ERROR",
        "unsafe SQLite test sentinel",
    )

    with pytest.raises(RuntimeError, match="unsafe SQLite test sentinel"):
        connect_sqlite(
            path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=False,
        )

    assert not path.exists()


def test_reader_does_not_require_writer_capable_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "read-only.db"
    raw = sqlite3.connect(path)
    raw.close()
    monkeypatch.setattr(
        sqlite_runtime,
        "_WRITER_SQLITE_VERSION_ERROR",
        "unsafe SQLite test sentinel",
    )

    reader = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
    reader.close()


def test_connection_enforces_integrity_and_concurrency_policy(tmp_path: Path) -> None:
    conn = connect_sqlite(
        tmp_path / "nested" / "portfolio.db",
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_writer_connections_share_initial_wal_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "initial-delete-mode.db"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    transition_barrier = threading.Barrier(2)
    busy_observed = threading.Event()
    real_connect: Callable[..., sqlite3.Connection] = sqlite3.connect

    class CoordinatedTransitionConnection(sqlite3.Connection):
        synchronized = False

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            if args:
                raise AssertionError("coordinated connection received parameterized SQL")
            is_wal_transition = sql.strip().casefold().replace(" ", "") == "pragmajournal_mode=wal"
            if not is_wal_transition or self.synchronized:
                return super().execute(sql)
            self.synchronized = True
            transition_barrier.wait(timeout=5)
            # SQLite may bypass a busy handler to avoid a lock cycle. Remove
            # the wait from this first attempt so the real DELETE-to-WAL lock
            # conflict deterministically exercises immediate-BUSY handling.
            super().execute("PRAGMA busy_timeout=0")
            try:
                cursor = super().execute(sql)
            except sqlite3.OperationalError as error:
                if error.sqlite_errorname == "SQLITE_BUSY":
                    busy_observed.set()
                raise
            assert busy_observed.wait(timeout=5)
            return cursor

    def synchronized_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = CoordinatedTransitionConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite_runtime.sqlite3, "connect", synchronized_connect)
    modes: list[str] = []
    failures: list[BaseException] = []

    def connect_writer() -> None:
        try:
            conn = connect_sqlite(
                path,
                role=SQLiteConnectionRole.WRITER,
                schema_preflight=False,
            )
        except BaseException as error:  # pragma: no cover - failure is asserted below
            failures.append(error)
            return
        try:
            modes.append(str(conn.execute("PRAGMA journal_mode").fetchone()[0]))
        finally:
            conn.close()

    first = threading.Thread(target=connect_writer)
    second = threading.Thread(target=connect_writer)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not failures
    assert modes == ["wal", "wal"]


def test_established_wal_writer_does_not_repeat_mode_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "established-wal.db"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        assert raw.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

    real_connect: Callable[..., sqlite3.Connection] = sqlite3.connect
    transition_attempts: list[str] = []

    def guarded_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)

        def authorize(
            action_code: int,
            pragma_name: str | None,
            pragma_value: str | None,
            database_name: str | None,
            trigger_name: str | None,
        ) -> int:
            del database_name, trigger_name
            if (
                action_code == sqlite3.SQLITE_PRAGMA
                and pragma_name == "journal_mode"
                and pragma_value == "WAL"
            ):
                transition_attempts.append(pragma_value)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(sqlite_runtime.sqlite3, "connect", guarded_connect)
    conn = connect_sqlite(
        path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    finally:
        conn.close()
    assert transition_attempts == []


def test_wal_transition_rechecks_when_sqlite_returns_old_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old-mode-result.db"

    class OldModeOnceConnection(sqlite3.Connection):
        wal_attempts = 0

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            if args:
                raise AssertionError("old-mode connection received parameterized SQL")
            is_wal_transition = sql.strip().casefold().replace(" ", "") == "pragmajournal_mode=wal"
            if is_wal_transition:
                self.wal_attempts += 1
                if self.wal_attempts == 1:
                    return super().execute("PRAGMA journal_mode")
            return super().execute(sql)

    real_connect: Callable[..., sqlite3.Connection] = sqlite3.connect

    def old_mode_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = OldModeOnceConnection
        return real_connect(*args, **kwargs)

    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    monkeypatch.setattr(sqlite_runtime.sqlite3, "connect", old_mode_connect)
    conn = connect_sqlite(
        path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        assert isinstance(conn, OldModeOnceConnection)
        assert conn.wal_attempts == 2
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("clock_after_read", "expected_set_timeout_ms"),
    [(29.0, 1_000), (30.0, None)],
)
def test_wal_transition_refreshes_budget_before_mode_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock_after_read: float,
    expected_set_timeout_ms: int | None,
) -> None:
    path = tmp_path / "slow-journal-read.db"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")

    clock = [0.0]
    set_timeouts: list[int] = []
    real_connect: Callable[..., sqlite3.Connection] = sqlite3.connect

    class DelayedReadConnection(sqlite3.Connection):
        delayed_read = False

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            if args:
                raise AssertionError("delayed-read connection received parameterized SQL")
            normalized = sql.strip().casefold().replace(" ", "")
            if normalized == "pragmajournal_mode" and not self.delayed_read:
                self.delayed_read = True
                cursor = super().execute(sql)
                clock[0] = clock_after_read
                return cursor
            if normalized == "pragmajournal_mode=wal":
                set_timeouts.append(int(super().execute("PRAGMA busy_timeout").fetchone()[0]))
            return super().execute(sql)

    def delayed_read_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = DelayedReadConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite_runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sqlite_runtime.sqlite3, "connect", delayed_read_connect)

    if expected_set_timeout_ms is None:
        with pytest.raises(sqlite3.OperationalError, match="within the configured busy timeout"):
            connect_sqlite(
                path,
                role=SQLiteConnectionRole.WRITER,
                schema_preflight=False,
            )
        assert set_timeouts == []
        return

    conn = connect_sqlite(
        path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        assert set_timeouts == [expected_set_timeout_ms]
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    finally:
        conn.close()


def test_wal_transition_uses_one_busy_budget_and_restores_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "locked-delete-mode.db"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")

    blocker = sqlite3.connect(path)
    real_connect: Callable[..., sqlite3.Connection] = sqlite3.connect
    timeout_at_close: list[int] = []

    class TimeoutTrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            timeout_at_close.append(int(self.execute("PRAGMA busy_timeout").fetchone()[0]))
            super().close()

    def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = TimeoutTrackingConnection
        return real_connect(*args, **kwargs)

    try:
        blocker.execute("BEGIN")
        blocker.execute("SELECT * FROM sample").fetchall()
        monkeypatch.setattr(sqlite_runtime, "SQLITE_BUSY_TIMEOUT_MS", 25)
        monkeypatch.setattr(sqlite_runtime.sqlite3, "connect", tracking_connect)
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked") as raised:
            connect_sqlite(
                path,
                role=SQLiteConnectionRole.WRITER,
                schema_preflight=False,
            )
        elapsed = time.monotonic() - started

        assert raised.value.sqlite_errorname == "SQLITE_BUSY"
        assert elapsed < 0.5
        assert timeout_at_close == [25]
        assert blocker.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        blocker.close()


def test_wal_transition_does_not_retry_non_busy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "denied-delete-mode.db"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")

    real_connect: Callable[..., sqlite3.Connection] = sqlite3.connect
    transition_attempts: list[str] = []
    timeout_at_close: list[int] = []

    class TimeoutTrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            timeout_at_close.append(int(self.execute("PRAGMA busy_timeout").fetchone()[0]))
            super().close()

    def denied_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = TimeoutTrackingConnection
        conn = real_connect(*args, **kwargs)

        def authorize(
            action_code: int,
            pragma_name: str | None,
            pragma_value: str | None,
            database_name: str | None,
            trigger_name: str | None,
        ) -> int:
            del database_name, trigger_name
            if (
                action_code == sqlite3.SQLITE_PRAGMA
                and pragma_name == "journal_mode"
                and pragma_value == "WAL"
            ):
                transition_attempts.append(pragma_value)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(sqlite_runtime.sqlite3, "connect", denied_connect)
    started = time.monotonic()
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connect_sqlite(
            path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=False,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert transition_attempts == ["WAL"]
    assert timeout_at_close == [30_000]


def test_writer_preserves_supported_in_memory_journal_mode() -> None:
    conn = connect_sqlite(
        ":memory:",
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    finally:
        conn.close()


def test_scope_identity_sql_function_rejects_forged_canonical_id(tmp_path: Path) -> None:
    conn = connect_sqlite(
        tmp_path / "scope-identity.db",
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        conn.executescript(
            """
            CREATE TABLE promotions (
              scope_id TEXT, source_scope_key TEXT, issuer_id TEXT
            );
            CREATE TRIGGER promotion_scope_exact BEFORE INSERT ON promotions
            WHEN NEW.scope_id<>derive_retrieval_scope_id(
              NEW.source_scope_key,NEW.issuer_id
            ) BEGIN
              SELECT RAISE(ABORT, 'scope ID mismatch');
            END;
            """
        )
        exact = derive_retrieval_scope_id(
            source_scope_key="investor-research",
            issuer_id="issuer-1",
        )
        conn.execute(
            "INSERT INTO promotions VALUES (?,?,?)",
            (exact, "investor-research", "issuer-1"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="scope ID mismatch"):
            conn.execute(
                "INSERT INTO promotions VALUES (?,?,?)",
                ("ask-scope:v1:" + "0" * 64, "investor-research", "issuer-1"),
            )
    finally:
        conn.close()


def test_connection_rejects_dangling_foreign_key(tmp_path: Path) -> None:
    conn = connect_sqlite(
        tmp_path / "portfolio.db",
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        conn.executescript(
            """
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parent(id)
            );
            """
        )
        try:
            conn.execute("INSERT INTO child (parent_id) VALUES (99)")
        except sqlite3.IntegrityError as exc:
            assert "FOREIGN KEY constraint failed" in str(exc)
        else:
            raise AssertionError("foreign-key violation was accepted")
    finally:
        conn.close()


def test_read_only_role_never_creates_or_mutates_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        connect_sqlite(missing, role=SQLiteConnectionRole.READ_ONLY)
    assert not missing.exists()

    writer = connect_sqlite(
        tmp_path / "portfolio.db",
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        writer.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        writer.commit()
    finally:
        writer.close()

    reader = connect_sqlite(
        tmp_path / "portfolio.db",
        role=SQLiteConnectionRole.READ_ONLY,
    )
    try:
        assert reader.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO sample VALUES (1)")
    finally:
        reader.close()


def test_quiesced_immutable_reader_does_not_create_wal_sidecars(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.db"
    writer = sqlite3.connect(path)
    try:
        writer.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        writer.execute("INSERT INTO sample VALUES (1)")
        writer.commit()
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        writer.close()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{path}{suffix}").exists()

    reader = connect_sqlite(
        path,
        role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
    )
    try:
        assert reader.execute("SELECT id FROM sample").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO sample VALUES (2)")
    finally:
        reader.close()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{path}{suffix}").exists()


def test_snapshot_destination_retains_default_journal_policy(
    tmp_path: Path,
) -> None:
    destination = connect_sqlite(
        tmp_path / "snapshot.db",
        role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
    )
    try:
        assert destination.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert destination.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        destination.close()


def test_writer_preflights_versioned_database_by_default(tmp_path: Path) -> None:
    path = tmp_path / "stale.db"
    raw = sqlite3.connect(path)
    try:
        raw.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        raw.execute("INSERT INTO alembic_version VALUES ('stale_revision')")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(SchemaRevisionMismatch):
        connect_sqlite(path, role=SQLiteConnectionRole.WRITER)


def test_schema_preflighted_writer_refuses_to_create_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError, match="existing database"):
        connect_sqlite(path, role=SQLiteConnectionRole.WRITER)
    assert not path.exists()


def test_read_only_connection_can_preflight_schema(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    raw = sqlite3.connect(path)
    try:
        raw.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        raw.execute("INSERT INTO alembic_version VALUES ('stale_revision')")
        raw.commit()
    finally:
        raw.close()
    with pytest.raises(SchemaRevisionMismatch):
        connect_sqlite(
            path,
            role=SQLiteConnectionRole.READ_ONLY,
            schema_preflight=True,
        )
