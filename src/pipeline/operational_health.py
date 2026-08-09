"""Cheap, read-only health probes for backup, WAL, and LLM-eval freshness."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from runtime.backup_paths import backup_dir
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

BACKUP_MAX_AGE = timedelta(hours=48)
EVAL_MAX_AGE = timedelta(days=8)
DEFAULT_WAL_WARN_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class Freshness:
    path: Path | None
    observed_at: datetime | None
    max_age: timedelta

    def is_stale(self, *, now: datetime) -> bool:
        return self.observed_at is None or now - self.observed_at > self.max_age


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def latest_backup(*, directory: Path | None = None) -> Freshness:
    """Newest encrypted primary snapshot, excluding GC-archive sidecars."""
    base = directory or backup_dir()
    snapshots = tuple(base.glob("portfolio.db.*.gz.enc")) if base.is_dir() else ()
    newest = max(snapshots, key=lambda item: item.stat().st_mtime, default=None)
    return Freshness(
        path=newest,
        observed_at=_mtime(newest) if newest is not None else None,
        max_age=BACKUP_MAX_AGE,
    )


def latest_archive_backup(*, directory: Path | None = None) -> Freshness:
    """Newest encrypted snapshot of the reversible-GC archive sidecar."""
    base = directory or backup_dir()
    snapshots = tuple(base.glob("portfolio_gc_archive.db.*.gz.enc")) if base.is_dir() else ()
    newest = max(snapshots, key=lambda item: item.stat().st_mtime, default=None)
    return Freshness(
        path=newest,
        observed_at=_mtime(newest) if newest is not None else None,
        max_age=BACKUP_MAX_AGE,
    )


def wal_size(db_path: Path) -> tuple[int, int]:
    """Return ``(bytes, warning threshold)`` without opening or checkpointing SQLite."""
    configured = os.environ.get("ES_WAL_WARN_BYTES")
    threshold = int(configured) if configured is not None else DEFAULT_WAL_WARN_BYTES
    if threshold < 1:
        raise ValueError("ES_WAL_WARN_BYTES must be at least 1")
    wal = Path(f"{db_path}-wal")
    try:
        return wal.stat().st_size, threshold
    except FileNotFoundError:
        return 0, threshold


def latest_eval(db_path: Path) -> Freshness:
    """Return the newest completed/versioned eval cohort timestamp."""
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        row = conn.execute(
            "SELECT MAX(COALESCE(finished_at, started_at)) FROM eval_runs"
        ).fetchone()
    finally:
        conn.close()
    raw = row[0] if row else None
    observed: datetime | None = None
    if raw:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        observed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return Freshness(path=None, observed_at=observed, max_age=EVAL_MAX_AGE)


def archive_backup_covers_local_sidecar(
    repo_root: Path,
    *,
    directory: Path | None = None,
) -> bool:
    """Whether Drive has captured the current reversible-GC archive generation."""
    local = repo_root / "data" / "archive" / "portfolio_gc_archive.db"
    if not local.exists():
        return True
    remote = latest_archive_backup(directory=directory)
    return remote.observed_at is not None and remote.observed_at >= _mtime(local)


__all__ = [
    "BACKUP_MAX_AGE",
    "DEFAULT_WAL_WARN_BYTES",
    "EVAL_MAX_AGE",
    "Freshness",
    "archive_backup_covers_local_sidecar",
    "latest_archive_backup",
    "latest_backup",
    "latest_eval",
    "wal_size",
]
