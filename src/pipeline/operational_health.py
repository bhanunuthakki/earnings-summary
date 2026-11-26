"""Cheap, read-only health probes for backup, WAL, DB size, and LLM-eval freshness."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from runtime.backup_paths import backup_dir
from runtime.backup_retention import BackupReceipt
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

BACKUP_MAX_AGE = timedelta(hours=48)
EVAL_MAX_AGE = timedelta(days=8)
DEFAULT_WAL_WARN_BYTES = 256 * 1024 * 1024
DEFAULT_DB_SIZE_WARN_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_DB_SIZE_CRIT_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_DB_GROWTH_WARN_BYTES = 500 * 1024 * 1024
DEFAULT_DB_GROWTH_WARN_PCT = 20.0
DB_GROWTH_LOOKBACK = timedelta(hours=48)
_MIN_PCT_BASELINE_CAP_BYTES = 64 * 1024 * 1024
_MAX_RECEIPT_SCAN = 32
_MAX_RECEIPT_BYTES = 16 * 1024
_FUTURE_SKEW = timedelta(minutes=5)

DatabaseSizeSeverity = Literal["ok", "warn", "critical"]
DatabaseSizeThreshold = Literal["none", "warn", "critical"]


@dataclass(frozen=True)
class Freshness:
    path: Path | None
    observed_at: datetime | None
    max_age: timedelta

    def is_stale(self, *, now: datetime) -> bool:
        return self.observed_at is None or now - self.observed_at > self.max_age


@dataclass(frozen=True)
class DatabaseSizeHealth:
    """Read-only database size and growth observation against backup receipts."""

    size_bytes: int
    warn_bytes: int
    crit_bytes: int
    baseline_bytes: int | None
    baseline_observed_at: datetime | None
    growth_bytes: int | None
    growth_pct: float | None
    growth_warn_bytes: int
    growth_warn_pct: float
    size_exceeded: DatabaseSizeThreshold
    growth_exceeded: bool
    severity: DatabaseSizeSeverity


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def _positive_int_env(name: str, default: int) -> int:
    configured = os.environ.get(name)
    value = int(configured) if configured is not None else default
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _positive_float_env(name: str, default: float) -> float:
    configured = os.environ.get(name)
    value = float(configured) if configured is not None else default
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")
    return value


def _normalized_path_key(raw: str) -> str:
    return os.path.normcase(os.path.normpath(raw))


def _parse_receipt_timestamp(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _read_bounded_backup_receipt(path: Path) -> BackupReceipt | None:
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > _MAX_RECEIPT_BYTES
        ):
            return None
        with path.open("rb") as handle:
            payload = handle.read(_MAX_RECEIPT_BYTES + 1)
        if len(payload) > _MAX_RECEIPT_BYTES:
            return None
        return BackupReceipt.model_validate_json(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValidationError):
        return None


def _baseline_from_receipts(
    db_path: Path,
    *,
    directory: Path | None,
    observed_now: datetime,
) -> tuple[int, datetime] | None:
    """Pick the comparison baseline size from bounded portfolio-db backup receipts.

    Prefers the earliest verified receipt within ``DB_GROWTH_LOOKBACK`` (48h) so an
    overnight size jump stays visible even after the 02:45 daily backup completes.
    If no receipt falls inside that window (for example because oversized backups
    started timing out), falls back to the newest historical receipt that recorded
    ``source_size_bytes``.
    """
    base = directory or backup_dir()
    if not base.is_dir():
        return None
    target_key = _normalized_path_key(str(db_path.resolve()))
    receipt_paths = sorted(
        base.glob("portfolio.db.*.gz.enc.receipt.json"),
        reverse=True,
    )[:_MAX_RECEIPT_SCAN]
    observations: list[tuple[datetime, int]] = []
    for receipt_file in receipt_paths:
        receipt = _read_bounded_backup_receipt(receipt_file)
        if (
            receipt is None
            or receipt.family != "portfolio-db"
            or receipt.source_size_bytes is None
            or _normalized_path_key(receipt.source) != target_key
        ):
            continue
        verified_at = _parse_receipt_timestamp(receipt.verified_at_utc)
        if verified_at is None or verified_at > observed_now + _FUTURE_SKEW:
            continue
        observations.append((verified_at, receipt.source_size_bytes))
    if not observations:
        return None
    observations.sort(key=lambda item: item[0])
    recent = [item for item in observations if observed_now - item[0] <= DB_GROWTH_LOOKBACK]
    selected_at, selected_bytes = recent[0] if recent else observations[-1]
    return selected_bytes, selected_at


def db_size_health(
    db_path: Path,
    *,
    directory: Path | None = None,
    now: datetime | None = None,
) -> DatabaseSizeHealth:
    """Inspect current database size and recent growth without opening SQLite."""
    warn_bytes = _positive_int_env("ES_DB_SIZE_WARN_BYTES", DEFAULT_DB_SIZE_WARN_BYTES)
    crit_bytes = _positive_int_env("ES_DB_SIZE_CRIT_BYTES", DEFAULT_DB_SIZE_CRIT_BYTES)
    if crit_bytes < warn_bytes:
        raise ValueError("ES_DB_SIZE_CRIT_BYTES must be at least ES_DB_SIZE_WARN_BYTES")
    growth_warn_bytes = _positive_int_env("ES_DB_GROWTH_WARN_BYTES", DEFAULT_DB_GROWTH_WARN_BYTES)
    growth_warn_pct = _positive_float_env("ES_DB_GROWTH_WARN_PCT", DEFAULT_DB_GROWTH_WARN_PCT)

    observed_now = now or datetime.now(UTC)
    current_bytes = db_path.stat().st_size

    if current_bytes >= crit_bytes:
        size_exceeded: DatabaseSizeThreshold = "critical"
    elif current_bytes >= warn_bytes:
        size_exceeded = "warn"
    else:
        size_exceeded = "none"

    baseline = _baseline_from_receipts(db_path, directory=directory, observed_now=observed_now)
    baseline_bytes: int | None = None
    baseline_observed_at: datetime | None = None
    growth_bytes: int | None = None
    growth_pct: float | None = None
    growth_exceeded = False

    if baseline is not None:
        baseline_bytes, baseline_observed_at = baseline
        growth_bytes = current_bytes - baseline_bytes
        growth_pct = (growth_bytes / baseline_bytes) * 100.0 if baseline_bytes > 0 else None
        min_pct_baseline = min(growth_warn_bytes, _MIN_PCT_BASELINE_CAP_BYTES)
        growth_exceeded = growth_bytes >= growth_warn_bytes or (
            growth_bytes > 0
            and baseline_bytes >= min_pct_baseline
            and growth_pct is not None
            and growth_pct >= growth_warn_pct
        )

    if size_exceeded == "critical":
        severity: DatabaseSizeSeverity = "critical"
    elif size_exceeded == "warn" or growth_exceeded:
        severity = "warn"
    else:
        severity = "ok"

    return DatabaseSizeHealth(
        size_bytes=current_bytes,
        warn_bytes=warn_bytes,
        crit_bytes=crit_bytes,
        baseline_bytes=baseline_bytes,
        baseline_observed_at=baseline_observed_at,
        growth_bytes=growth_bytes,
        growth_pct=growth_pct,
        growth_warn_bytes=growth_warn_bytes,
        growth_warn_pct=growth_warn_pct,
        size_exceeded=size_exceeded,
        growth_exceeded=growth_exceeded,
        severity=severity,
    )


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
    "DB_GROWTH_LOOKBACK",
    "DEFAULT_DB_GROWTH_WARN_BYTES",
    "DEFAULT_DB_GROWTH_WARN_PCT",
    "DEFAULT_DB_SIZE_CRIT_BYTES",
    "DEFAULT_DB_SIZE_WARN_BYTES",
    "DEFAULT_WAL_WARN_BYTES",
    "EVAL_MAX_AGE",
    "DatabaseSizeHealth",
    "DatabaseSizeSeverity",
    "DatabaseSizeThreshold",
    "Freshness",
    "archive_backup_covers_local_sidecar",
    "db_size_health",
    "latest_archive_backup",
    "latest_backup",
    "latest_eval",
    "wal_size",
]
