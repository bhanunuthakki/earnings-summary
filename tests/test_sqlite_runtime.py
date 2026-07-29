"""Regression coverage for the shared direct-SQLite connection policy."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schema_compat import SchemaRevisionMismatch
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


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


def test_schema_preflight_is_writer_only(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    sqlite3.connect(path).close()
    with pytest.raises(ValueError, match="writer"):
        connect_sqlite(
            path,
            role=SQLiteConnectionRole.READ_ONLY,
            schema_preflight=True,
        )
