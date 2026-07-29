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
DEFAULT_DEST = r"C:\Users\Bhanu\My Drive\earnings-summary-db-backups"
DEFAULT_RETAIN = 14


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
    accounting: tuple[sqlite3.Connection, str],
    *,
    success: bool,
    error_msg: str | None = None,
) -> None:
    conn, run_id = accounting
    try:
        end_run(
            conn,
            run_id,
            StageStatus.OK if success else StageStatus.FAILED,
            error_msg,
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
        print(f"ERROR: backup accounting unavailable: {exc}", file=sys.stderr)
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
        snapshots = sorted(dest_dir.glob("portfolio.db.*.gz.enc"))
        for stale in snapshots[:-retain]:
            stale.unlink()

        size_mb = final_path.stat().st_size / 1e6
        kept = min(len(snapshots), retain)
        print(f"OK backup -> {final_path}  ({size_mb:.1f} MB encrypted)  retained={kept}")
    except Exception as exc:
        _finish_accounting(accounting, success=False, error_msg=str(exc))
        print(f"ERROR: backup failed: {exc}", file=sys.stderr)
        return 1
    _finish_accounting(accounting, success=True)
    return 0


def main() -> int:
    global SRC_DB
    if SRC_DB == DEFAULT_SRC_DB:
        load_project_env(PROJECT_ROOT)
        SRC_DB = portfolio_db_path(PROJECT_ROOT)
    lock = (
        nullcontext()
        if inherited_lock_is_valid(PROJECT_ROOT, "portfolio-db")
        else JobLock(PROJECT_ROOT, "backup_db_direct", ["portfolio-db"])
    )
    try:
        with lock:
            return _run_backup()
    except Exception as exc:
        print(f"ERROR: backup lock failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
