"""Regression coverage for the shared direct-SQLite connection policy."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlite_runtime import connect_sqlite


def test_connection_enforces_integrity_and_concurrency_policy(tmp_path: Path) -> None:
    conn = connect_sqlite(tmp_path / "nested" / "portfolio.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()


def test_connection_rejects_dangling_foreign_key(tmp_path: Path) -> None:
    conn = connect_sqlite(tmp_path / "portfolio.db")
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
