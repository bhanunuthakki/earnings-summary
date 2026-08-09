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
from scope_identity import derive_retrieval_scope_id


def sqlite_version_is_wal_reset_safe(version: tuple[int, int, int]) -> bool:
    """Whether ``version`` contains SQLite's 2026 WAL-reset race fix."""
    return version == (3, 50, 7) or version >= (3, 51, 3)


_WRITER_SQLITE_VERSION_ERROR = (
    None
    if sqlite_version_is_wal_reset_safe(sqlite3.sqlite_version_info)
    else (
        "SQLite writer runtime is vulnerable to the WAL-reset corruption race: "
        f"loaded={sqlite3.sqlite_version}; require 3.50.7 or >=3.51.3. "
        "Launch through execution/sqlite_bootstrap.py."
    )
)


def require_safe_sqlite_writer_runtime() -> None:
    """Fail before any WAL writer can touch a database under an unsafe build."""
    if _WRITER_SQLITE_VERSION_ERROR is not None:
        raise RuntimeError(_WRITER_SQLITE_VERSION_ERROR)


class SQLiteConnectionRole(StrEnum):
    """Capabilities requested from a SQLite connection."""

    READ_ONLY = "read_only"
    QUIESCED_IMMUTABLE_READ_ONLY = "quiesced_immutable_read_only"
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
    parent directory. The quiesced immutable role additionally suppresses
    SQLite lock and WAL sidecars, and is only safe after the caller has proved
    the file cannot change for the connection lifetime. Writer connections may
    preflight Alembic compatibility and set the WAL/durability policy. Snapshot
    destinations are new, caller-owned local files used by backup or isolated
    synthetic tooling and intentionally retain the default journal mode.
    """
    if role is SQLiteConnectionRole.WRITER:
        require_safe_sqlite_writer_runtime()

    require_schema = (
        role is SQLiteConnectionRole.WRITER if schema_preflight is None else schema_preflight
    )

    resolved = os.fspath(path)
    if role in (
        SQLiteConnectionRole.READ_ONLY,
        SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
    ):
        conn = sqlite3.connect(
            _read_only_uri(
                resolved,
                immutable=(role is SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY),
            ),
            uri=True,
            timeout=30.0,
        )
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
        _register_scope_identity_function(conn)
        _apply_connection_policy(conn)
        if require_schema:
            # The compatibility probe is read-only despite its historical
            # name. Request/report readers opt in so schema drift is visible
            # instead of being misreported as an empty result set.
            require_current_for_write(conn)
        if role is SQLiteConnectionRole.WRITER:
            _apply_writer_policy(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _register_scope_identity_function(conn: sqlite3.Connection) -> None:
    def derive(source_scope_key: object, issuer_id: object) -> str:
        if not isinstance(source_scope_key, str) or not isinstance(issuer_id, str):
            raise ValueError("scope identity components must be text")
        return derive_retrieval_scope_id(
            source_scope_key=source_scope_key,
            issuer_id=issuer_id,
        )

    conn.create_function(
        "derive_retrieval_scope_id",
        2,
        derive,
        deterministic=True,
    )


def _read_only_uri(path: str, *, immutable: bool = False) -> str:
    """Build SQLite's read-only URI without touching the target path."""
    if path == ":memory:":
        raise ValueError("read-only connections require an on-disk database")
    immutable_query = "&immutable=1" if immutable else ""
    return f"{Path(path).resolve().as_uri()}?mode=ro{immutable_query}"


def _apply_connection_policy(conn: sqlite3.Connection) -> None:
    """Apply the PRAGMAs that are local to every connection."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")


def _apply_writer_policy(conn: sqlite3.Connection) -> None:
    """Apply database-wide settings permitted only to a writer."""
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")


__all__ = [
    "SQLiteConnectionRole",
    "connect_sqlite",
    "require_safe_sqlite_writer_runtime",
    "sqlite_version_is_wal_reset_safe",
]
