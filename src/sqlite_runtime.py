"""One auditable connection policy for direct portfolio SQLite access.

The role is explicit because readers must not alter database-wide durability
    settings, while writers need the WAL policy used by concurrent pipelines.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
import unicodedata
from enum import StrEnum
from pathlib import Path

from schema_compat import require_current_for_write
from scope_identity import derive_retrieval_scope_id

SQLITE_BUSY_TIMEOUT_MS = 30_000
_WAL_TRANSITION_RETRY_INTERVAL_S = 0.01
_FORBIDDEN_MAC_CHECKOUT_DB = (
    Path(__file__).resolve().parents[1] / "data" / "portfolio.db"
).resolve()


def _normalized_component(value: str) -> str:
    """Compare one filesystem component using macOS's Unicode/case rules."""
    return unicodedata.normalize("NFC", value).casefold()


def _is_forbidden_mac_checkout_database(path: str | os.PathLike[str]) -> bool:
    candidate = Path(path).expanduser().resolve(strict=False)
    if candidate == _FORBIDDEN_MAC_CHECKOUT_DB:
        return True

    candidate_parent = candidate.parent
    forbidden_parent = _FORBIDDEN_MAC_CHECKOUT_DB.parent
    if not candidate_parent.exists() or not forbidden_parent.exists():
        return False
    try:
        same_parent = candidate_parent.samefile(forbidden_parent)
    except OSError:
        return False
    return same_parent and _normalized_component(candidate.name) == _normalized_component(
        _FORBIDDEN_MAC_CHECKOUT_DB.name
    )


def reject_forbidden_mac_checkout_database(path: str | os.PathLike[str]) -> None:
    """Prevent every connection path from treating the Mac checkout as authority."""
    if sys.platform != "darwin" or os.fspath(path) == ":memory:":
        return
    if _is_forbidden_mac_checkout_database(path):
        raise RuntimeError(
            "Mac checkout database is prohibited; use an explicit disposable test database "
            "or approved provenance-bearing export of the canonical Windows database"
        )


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
    reject_forbidden_mac_checkout_database(path)
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
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
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
        conn = sqlite3.connect(resolved, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)

    try:
        _register_scope_identity_function(conn)
        _register_transcript_receipt_function(conn, database_path=resolved)
        _apply_connection_policy(conn)
        if require_schema:
            # The compatibility probe is read-only despite its historical
            # name. Request/report readers opt in so schema drift is visible
            # instead of being misreported as an empty result set.
            require_current_for_write(conn)
        if role is SQLiteConnectionRole.WRITER:
            _apply_writer_policy(conn, require_wal=resolved != ":memory:")
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


def _register_transcript_receipt_function(
    conn: sqlite3.Connection,
    *,
    database_path: str,
) -> None:
    from pipeline.transcript_acquisition import register_transcript_receipt_sqlite_functions

    register_transcript_receipt_sqlite_functions(conn, database_path=database_path)


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
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")


def _pragma_journal_mode(conn: sqlite3.Connection, *, wal: bool = False) -> str:
    statement = "PRAGMA journal_mode = WAL" if wal else "PRAGMA journal_mode"
    row = conn.execute(statement).fetchone()
    if row is None:
        raise sqlite3.OperationalError("SQLite did not report a journal mode")
    return str(row[0]).casefold()


def _raise_wal_transition_timeout(
    *, busy_error: sqlite3.OperationalError | None, observed_mode: str
) -> None:
    if busy_error is not None:
        raise busy_error
    raise sqlite3.OperationalError(
        "SQLite did not enter WAL journal mode within the configured busy timeout "
        f"(last mode={observed_mode})"
    )


def _apply_remaining_busy_timeout(
    conn: sqlite3.Connection,
    *,
    deadline: float,
    busy_error: sqlite3.OperationalError | None,
    observed_mode: str,
) -> None:
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        _raise_wal_transition_timeout(
            busy_error=busy_error,
            observed_mode=observed_mode,
        )
    conn.execute(f"PRAGMA busy_timeout = {remaining_ms}")


def _establish_wal_within_busy_timeout(conn: sqlite3.Connection) -> None:
    timeout_row = conn.execute("PRAGMA busy_timeout").fetchone()
    if timeout_row is None:
        raise sqlite3.OperationalError("SQLite did not report its busy timeout")
    configured_timeout_ms = int(timeout_row[0])
    deadline = time.monotonic() + configured_timeout_ms / 1000
    last_busy_error: sqlite3.OperationalError | None = None
    observed_mode = "unknown"

    try:
        while True:
            _apply_remaining_busy_timeout(
                conn,
                deadline=deadline,
                busy_error=last_busy_error,
                observed_mode=observed_mode,
            )
            try:
                observed_mode = _pragma_journal_mode(conn)
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorname", "") != "SQLITE_BUSY":
                    raise
                last_busy_error = error
            else:
                if observed_mode == "wal":
                    return
                last_busy_error = None
                _apply_remaining_busy_timeout(
                    conn,
                    deadline=deadline,
                    busy_error=last_busy_error,
                    observed_mode=observed_mode,
                )
                try:
                    observed_mode = _pragma_journal_mode(conn, wal=True)
                except sqlite3.OperationalError as error:
                    if getattr(error, "sqlite_errorname", "") != "SQLITE_BUSY":
                        raise
                    last_busy_error = error
                else:
                    if observed_mode == "wal":
                        return
                    last_busy_error = None

            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                _raise_wal_transition_timeout(
                    busy_error=last_busy_error,
                    observed_mode=observed_mode,
                )
            time.sleep(min(_WAL_TRANSITION_RETRY_INTERVAL_S, remaining_s))
    finally:
        conn.execute(f"PRAGMA busy_timeout = {configured_timeout_ms}")


def _apply_writer_policy(conn: sqlite3.Connection, *, require_wal: bool = True) -> None:
    """Apply database-wide settings permitted only to a writer."""
    if require_wal:
        _establish_wal_within_busy_timeout(conn)
    conn.execute("PRAGMA synchronous = NORMAL")


__all__ = [
    "SQLITE_BUSY_TIMEOUT_MS",
    "SQLiteConnectionRole",
    "connect_sqlite",
    "require_safe_sqlite_writer_runtime",
    "sqlite_version_is_wal_reset_safe",
]
