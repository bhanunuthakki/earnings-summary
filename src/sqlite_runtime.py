"""One connection policy for every direct portfolio SQLite caller."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def connect_sqlite(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open SQLite with the repository's concurrency and integrity policy."""
    resolved = os.fspath(path)
    if resolved != ":memory:":
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        conn.close()
        raise
    return conn
