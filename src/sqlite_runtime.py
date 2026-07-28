"""One auditable connection policy for direct portfolio SQLite access.

The role is explicit because readers must not alter database-wide durability
    settings, while writers need the WAL policy used by concurrent pipelines.
"""

from __future__ import annotations

import os
import sqlite3
from enum import StrEnum
from pathlib import Path

from schema_compat import require_current_for_write


class SQLiteConnectionRole(StrEnum):
    """Capabilities requested from a SQLite connection."""

    READ_ONLY = "read_only"
    WRITER = "writer"
    SNAPSHOT_DESTINATION = "snapshot_destination"


def connect_sqlite(
    path: str | os.PathLike[str],
    *,
    role: SQLiteConnectionRole,
    schema_preflight: bool | None = None,
) -> sqlite3.Connection:
    """Open a SQLite connection under the repository's role-specific policy.

    Read-only connections use SQLite's ``mode=ro`` URI and never create a
    parent directory. Writer connections may preflight Alembic compatibility
    and set the WAL/durability policy. Snapshot destinations are new,
    caller-owned local files used by backup or isolated synthetic tooling and
    intentionally retain the default journal mode.
    """
    require_schema = (
        role is SQLiteConnectionRole.WRITER if schema_preflight is None else schema_preflight
    )
    if require_schema and role is not SQLiteConnectionRole.WRITER:
        raise ValueError("schema_preflight is available only to writer connections")

    resolved = os.fspath(path)
    if role is SQLiteConnectionRole.READ_ONLY:
        conn = sqlite3.connect(_read_only_uri(resolved), uri=True, timeout=30.0)
    else:
        if (
            role is SQLiteConnectionRole.WRITER
            and require_schema
            and resolved != ":memory:"
            and not Path(resolved).exists()
        ):
            raise FileNotFoundError("schema-preflighted writer requires an existing database")
        if resolved != ":memory:":
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(resolved, timeout=30.0)

    try:
        _apply_connection_policy(conn)
        if role is SQLiteConnectionRole.WRITER:
            if require_schema:
                require_current_for_write(conn)
            _apply_writer_policy(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _read_only_uri(path: str) -> str:
    """Build SQLite's read-only URI without touching the target path."""
    if path == ":memory:":
        raise ValueError("read-only connections require an on-disk database")
    return f"{Path(path).resolve().as_uri()}?mode=ro"


def _apply_connection_policy(conn: sqlite3.Connection) -> None:
    """Apply the PRAGMAs that are local to every connection."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")


def _apply_writer_policy(conn: sqlite3.Connection) -> None:
    """Apply database-wide settings permitted only to a writer."""
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
