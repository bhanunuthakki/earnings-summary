"""Shared SQLite helpers for the user_state CRUD modules.

These modules (registry, sizing, ledger) are *primary* write paths for the
Personal CIO substrate. Unlike the telemetry stores (calibration.py,
llm_artifact_store.py) that fall back to no-op on a missing DB, this layer
fails loudly: a missing DB or missing table means the substrate is not
installed and the caller MUST hear about it rather than silently dropping
the user's KPI/sizing/ledger writes.

`db_path` defaulting follows the predictions_store / artifact_store pattern:
caller can pass an explicit path; otherwise we import `db.DB_PATH`. The
import is wrapped because pyright strict and pytest collection both touch
this module from contexts where `db.py` may not be on sys.path (e.g.
linting). When `db.DB_PATH` cannot be resolved AND no override is given,
we raise — the absence of any path is itself a bug.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from clock import now_naive_utc, to_naive_utc
from db_paths import resolve_db_path
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def _resolved_path(db_path: Path | str | None) -> Path:
    """Resolve and validate a configured Personal CIO SQLite path."""
    path = resolve_db_path(db_path)
    if path is None:
        raise RuntimeError(
            "DB path not configured: pass db_path explicitly or ensure db.DB_PATH "
            "is importable from src/."
        )
    if not path.exists():
        raise FileNotFoundError(f"SQLite DB does not exist: {path}")
    return path


def open_conn(db_path: Path | str | None, *, schema_preflight: bool = True) -> sqlite3.Connection:
    """Open a connection to the Personal CIO SQLite DB.

    Raises ``FileNotFoundError`` when the resolved path doesn't exist and
    ``RuntimeError`` when no path is configured. The caller owns closing
    the connection (use try/finally).
    """
    path = _resolved_path(db_path)
    conn = connect_sqlite(
        path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=schema_preflight,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def open_read_conn(db_path: Path | str | None) -> sqlite3.Connection:
    """Open a read-only connection without demanding current writer schema."""
    path = _resolved_path(db_path)
    conn = connect_sqlite(
        path,
        role=SQLiteConnectionRole.READ_ONLY,
        schema_preflight=True,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso() -> str:
    """Current UTC time as a naive-UTC ISO 8601 string — the canonical format
    for every TEXT timestamp column in the Personal CIO schema."""
    return now_naive_utc().isoformat()


def parse_dt(raw: object) -> datetime:
    """Parse an ISO 8601 TEXT timestamp coming back from SQLite into a naive-UTC
    datetime. Raises if the value can't be parsed — there's no graceful fallback
    because every row's created_at / updated_at column is NOT NULL by schema.

    Rows written before the naive-UTC migration carry an aware ``+00:00`` offset;
    ``to_naive_utc`` strips it so old (aware) and new (naive) stamps compare
    without ``TypeError``."""
    dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    return to_naive_utc(dt)
