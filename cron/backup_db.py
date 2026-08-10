"""Consistent, sync-safe backup of data/portfolio.db.

Once the scratch tree is removed from Google Drive's folder backup, the live DB is no
longer backed up by Drive. This produces a CONSISTENT snapshot (SQLite online
backup API — safe even while the pipeline writes) and gzips it into a synced
backup folder. The gzip is encrypted with AES-256-GCM before it reaches the
synced directory, so Drive receives only a static authenticated ciphertext.

Run through the shared lock wrapper (cron/run_backup_db.bat schedules it daily):
    cron\run_python.bat backup_db portfolio-db cron\backup_db.py

Backup dir is `ES_DB_BACKUP_DIR` (default: a Google Drive folder so the snapshot
is cloud-backed). Keeps the most recent ES_DB_BACKUP_RETAIN snapshots.

Restore is the tested counterpart `cron/restore_db.py` (integrity-checked
gunzip + move), which also documents the RPO/RTO targets (sre-3).
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from models.runs import StageStatus  # noqa: E402
from pipeline.run_accounting import (  # noqa: E402
    PipelineRunSuppressedError,
    end_run,
    start_run,
    suppression_payload,
)
from runtime.backup_crypto import encrypt_file, load_or_create_key  # noqa: E402
from runtime.job_runtime import JobLock, inherited_lock_is_valid, portfolio_db_path  # noqa: E402
from runtime.secrets import load_project_env  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

DEFAULT_SRC_DB = (PROJECT_ROOT / "data" / "portfolio.db").resolve()
SRC_DB = DEFAULT_SRC_DB
MIRROR_DRIVE_ROOT = Path(r"C:\Users\Bhanu\My Drive")


def _google_drive_root() -> Path:
    """Locate the Google Drive root in either sync mode.

    In Stream mode Drive mounts a virtual drive (usually G:), and the old
    mirror folder at C:\\Users\\Bhanu\\My Drive lingers on disk as a stale,
    UNSYNCED leftover until manually deleted. A mounted "<letter>:\\My Drive"
    can only be the Drive mount, so any non-C: hit wins over the mirror path —
    checking C: first would keep writing backups into the dead folder while
    reporting OK. restore_db.py duplicates this deliberately (backup and
    restore must never resolve to different answers — keep them identical).
    """
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{letter}:/My Drive")
        if candidate.is_dir():
            return candidate
    return MIRROR_DRIVE_ROOT


DEFAULT_DEST = _google_drive_root() / "earnings-summary-db-backups"
DEFAULT_RETAIN = 14
# The db_gc archive sidecar (execution/db_gc.py) holds the ONLY copy of pruned
# rows and underpins the "reversible" contract, yet it lived unbacked-up until
# 2026-08-03. It is append-only, so a snapshot only needs to be newer than the
# last prune, not daily — keep fewer, and skip re-encrypting an unchanged file.
ARCHIVE_PREFIX = "portfolio_gc_archive.db"
DEFAULT_ARCHIVE_RETAIN = 6


def _consistent_snapshot(src_db: Path, tmp_path: Path) -> None:
    """SQLite online backup -> tmp_path (consistent even under concurrent writes)."""
    src = sqlite3.connect(str(src_db))
    dst = sqlite3.connect(str(tmp_path))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


def _integrity_ok(db_path: Path) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    finally:
        conn.close()


def _start_accounting(dest_dir: Path, retain: int) -> tuple[sqlite3.Connection, str]:
    """Claim the daily backup invocation before snapshot/encryption begins."""
    conn = connect_sqlite(
        SRC_DB,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        run_id = start_run(
            conn,
            directive="backup_db",
            ticker_scope=[],
            invocation_inputs={
                "backup_dir": str(dest_dir.resolve()),
                "retain": retain,
                "run_date": datetime.now().date().isoformat(),
                "source_db": str(SRC_DB.resolve()),
            },
            deduplicate_completed=True,
        )
    except Exception:
        conn.close()
        raise
    return conn, run_id


def _finish_accounting(
    accounting: tuple[sqlite3.Connection, str] | None,
    *,
    success: bool,
    error_msg: str | None = None,
) -> None:
    if accounting is None:
        return
    conn, run_id = accounting
    try:
        try:
            end_run(
                conn,
                run_id,
                StageStatus.OK if success else StageStatus.FAILED,
                error_msg,
            )
        except Exception as exc:
            print(
                f"WARN: backup accounting completion unavailable: {redact(exc)}",
                file=sys.stderr,
            )
    finally:
        conn.close()


def _run_backup() -> int:
    if not SRC_DB.exists():
        print(f"ERROR: source DB not found: {SRC_DB}", file=sys.stderr)
        return 1
    dest_dir = Path(os.environ.get("ES_DB_BACKUP_DIR", DEFAULT_DEST))
    retain = int(os.environ.get("ES_DB_BACKUP_RETAIN", str(DEFAULT_RETAIN)))
    if retain < 1:
        raise ValueError("ES_DB_BACKUP_RETAIN must be at least 1")
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        accounting = _start_accounting(dest_dir, retain)
    except PipelineRunSuppressedError as exc:
        print(json.dumps(suppression_payload(exc)))
        return 0
    except Exception as exc:
        # The online snapshot is deliberately reader-safe and may overlap a
        # writer. Accounting is observability, not a backup prerequisite.
        print(
            f"WARN: backup accounting unavailable; snapshot proceeding: {redact(exc)}",
            file=sys.stderr,
        )
        accounting = None

    # Refuse up front rather than half-write onto a full volume. Staging holds the raw
    # snapshot AND its gzip before either is released, so the true cost is ~2x the DB
    # (plus the compressed artifact landing in dest_dir). A backup that dies mid-write
    # on a full disk is indistinguishable from one that never ran.
    db_bytes = SRC_DB.stat().st_size
    need_staging = 2 * db_bytes + 256 * 1024 * 1024
    free_staging = shutil.disk_usage(tempfile.gettempdir()).free
    if free_staging < need_staging:
        msg = (
            f"insufficient free space for backup staging: need "
            f"{need_staging / 1e9:.2f} GB, have {free_staging / 1e9:.2f} GB "
            f"on {tempfile.gettempdir()}"
        )
        _finish_accounting(accounting, success=False, error_msg=msg)
        print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    final_path = dest_dir / f"portfolio.db.{stamp}.gz.enc"

    try:
        with tempfile.TemporaryDirectory(prefix="portfolio_backup.") as staging:
            tmp_path = Path(staging) / "portfolio.db"
            tmp_gz = Path(staging) / "portfolio.db.gz"
            _consistent_snapshot(SRC_DB, tmp_path)
            if not _integrity_ok(tmp_path):
                raise RuntimeError("consistent snapshot failed SQLite integrity_check")
            with open(tmp_path, "rb") as raw, gzip.open(tmp_gz, "wb") as gz:
                shutil.copyfileobj(raw, gz)
            encrypt_file(tmp_gz, final_path, key=load_or_create_key())
        # Retention spans EVERY snapshot format, not just the one this build writes.
        # Globbing only "*.gz.enc" made the pruner blind to its own history: when the
        # running checkout wrote plaintext ".gz" (2026-07-27 onward) the 15 encrypted
        # snapshots became unprunable and sat at ~3.7 GB with nothing able to reclaim
        # them. A format switch must never orphan the previous format's files.
        snapshots = sorted(dest_dir.glob("portfolio.db.*.gz*"))
        for stale in snapshots[:-retain]:
            stale.unlink()

        size_mb = final_path.stat().st_size / 1e6
        kept = min(len(snapshots), retain)
        print(f"OK backup -> {final_path}  ({size_mb:.1f} MB encrypted)  retained={kept}")
    except Exception as exc:
        _finish_accounting(accounting, success=False, error_msg=str(exc))
        print(f"ERROR: backup failed: {exc}", file=sys.stderr)
        return 1
    _backup_archive_sidecar(dest_dir)
    _finish_accounting(accounting, success=True)
    return 0


def _backup_archive_sidecar(dest_dir: Path) -> None:
    """Best-effort encrypted snapshot of the db_gc archive sidecar.

    Deliberately non-fatal: a missing archive (GC never ran) or a failed
    archive snapshot must NOT fail the portfolio.db backup that already
    succeeded — the archive is a recovery aid, not the primary asset. Skips
    re-encrypting an archive already captured at its current size+mtime
    (append-only, so those two fields moving is a reliable change signal).
    """
    archive = SRC_DB.parent / "archive" / ARCHIVE_PREFIX
    if not archive.exists():
        print("(no db_gc archive to back up — GC has not pruned yet)")
        return
    retain = int(os.environ.get("ES_ARCHIVE_BACKUP_RETAIN", str(DEFAULT_ARCHIVE_RETAIN)))
    existing = sorted(dest_dir.glob(f"{ARCHIVE_PREFIX}.*.gz.enc"))
    stat = archive.stat()
    tag = f"{stat.st_size}_{int(stat.st_mtime)}"
    if existing and existing[-1].name.endswith(f".{tag}.gz.enc"):
        print(f"(archive unchanged since last backup: {existing[-1].name})")
        return
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    final_path = dest_dir / f"{ARCHIVE_PREFIX}.{stamp}.{tag}.gz.enc"
    try:
        with tempfile.TemporaryDirectory(prefix="archive_backup.") as staging:
            tmp_path = Path(staging) / ARCHIVE_PREFIX
            tmp_gz = Path(staging) / f"{ARCHIVE_PREFIX}.gz"
            _consistent_snapshot(archive, tmp_path)
            if not _integrity_ok(tmp_path):
                raise RuntimeError("archive snapshot failed SQLite integrity_check")
            with open(tmp_path, "rb") as raw, gzip.open(tmp_gz, "wb") as gz:
                shutil.copyfileobj(raw, gz)
            encrypt_file(tmp_gz, final_path, key=load_or_create_key())
        for stale in sorted(dest_dir.glob(f"{ARCHIVE_PREFIX}.*.gz.enc"))[:-retain]:
            stale.unlink()
        size_mb = final_path.stat().st_size / 1e6
        print(f"OK archive backup -> {final_path.name}  ({size_mb:.1f} MB encrypted)")
    except Exception as exc:  # never fail the primary backup on the archive leg
        print(f"WARN: archive backup skipped: {exc}", file=sys.stderr)


def main() -> int:
    global SRC_DB
    if SRC_DB == DEFAULT_SRC_DB:
        load_project_env(PROJECT_ROOT)
        SRC_DB = portfolio_db_path(PROJECT_ROOT)
    # "db-backup", NOT "portfolio-db". This job READS the database through
    # SQLite's online-backup API, which is explicitly safe while other writers
    # work (and _integrity_ok below is what actually proves the snapshot good).
    # Claiming the database's exclusive write set bought no safety and cost
    # every scheduled run that overlapped any writer: JobLock is fail-fast with
    # zero wait, so on 2026-08-03 the 02:45 run gave up 12 ms in with
    # "write set busy: portfolio-db" while an hourly onboard job -- started at
    # 01:17 and still running at 03:20 -- held it. Four consecutive scheduled
    # backups were lost that way. The lock this job does need is against ANOTHER
    # backup: two concurrent runs would race on dest_dir and its retention prune.
    lock = (
        nullcontext()
        if inherited_lock_is_valid(PROJECT_ROOT, "db-backup")
        else JobLock(PROJECT_ROOT, "backup_db_direct", ["db-backup"])
    )
    try:
        with lock:
            return _run_backup()
    except Exception as exc:
        print(f"ERROR: backup lock failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
