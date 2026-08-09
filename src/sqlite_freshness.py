"""Cheap file-generation tokens for SQLite databases, including WAL commits."""

from __future__ import annotations

from pathlib import Path

SQLiteFileToken = tuple[int, int, int, int]


def sqlite_file_token(db_path: Path) -> SQLiteFileToken | None:
    """Return a conservative main-file plus WAL freshness token.

    In WAL mode a committed write normally changes ``<db>-wal`` without
    changing the main database file. Keying caches on the main-file mtime
    alone can therefore preserve stale query results until a later checkpoint.
    Including nanosecond mtime and size for both files invalidates on ordinary
    appends, WAL resets, checkpoints, and sidecar deletion. ``None`` means the
    database itself is unavailable and callers must bypass their cache.
    """
    try:
        main = db_path.stat()
    except OSError:
        return None
    wal_path = Path(f"{db_path}-wal")
    try:
        wal = wal_path.stat()
    except OSError:
        return (main.st_mtime_ns, main.st_size, 0, 0)
    return (main.st_mtime_ns, main.st_size, wal.st_mtime_ns, wal.st_size)


__all__ = ["SQLiteFileToken", "sqlite_file_token"]
