"""Consistent, sync-safe backup of data/portfolio.db.

Once the scratch tree is removed from Google Drive's folder backup, the live DB is no
longer backed up by Drive. This produces a CONSISTENT snapshot (SQLite online
backup API — safe even while the pipeline writes) and gzips it into a synced
backup folder. The gzip is encrypted with AES-256-GCM before it reaches the
synced directory, so Drive receives only a static authenticated ciphertext.

The existing daily cron/run_backup_db.bat chain creates snapshots, uploads
all required families, then retires only verified older copies. The default
retention is one completed copy per source/family. Pending, failed, unclassified,
and pinned copies remain intact. ES_DB_BACKUP_DIR sets the destination.

After the integrity gate, unchanged content can reuse a hash-bound local
snapshot receipt. The wrapper still verifies/retries its remote upload. Local
production and remote completion are separate receipt fields. RPO is 24h of
changes — see cron/restore_db.py.

Restore is the tested counterpart `cron/restore_db.py` (integrity-checked
gunzip + move), which also documents the RPO/RTO targets (sre-3).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from db_paths import require_db_path
from log_redact import redact
from models.runs import StageStatus
from pipeline.run_accounting import (
    PipelineRunSuppressedError,
    end_run,
    start_run,
    suppression_payload,
)
from runtime.backup_crypto import (
    decrypt_file,
    encrypt_file,
    load_key,
    load_or_create_key,
)
from runtime.backup_retention import (
    BackupReceipt,
    record_snapshot,
    verified_files,
)
from runtime.job_runtime import JobLock, inherited_lock_is_valid
from runtime.secrets import load_project_env
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC_DB = (PROJECT_ROOT / "data" / "portfolio.db").resolve()
SRC_DB = DEFAULT_SRC_DB
MIRROR_DRIVE_ROOT = Path(os.environ.get("ES_MIRROR_DRIVE_ROOT") or Path.home() / "My Drive")


def _google_drive_root() -> Path:
    """Locate the Google Drive root in either sync mode.

    In Stream mode Drive mounts a virtual drive (usually G:), and the old
    mirror folder under the user profile lingers on disk as a stale,
    UNSYNCED leftover until manually deleted. A mounted "<letter>:\\My Drive"
    can only be the Drive mount, so any non-C: hit wins over the mirror path —
    checking C: first would keep writing backups into the dead folder while
    reporting OK. restore_db.py duplicates this deliberately (backup and
    restore must never resolve to different answers — keep them identical).
    """
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{letter}:/My Drive")
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return MIRROR_DRIVE_ROOT


DEFAULT_DEST = MIRROR_DRIVE_ROOT / "earnings-summary-db-backups"


def configured_backup_dir() -> Path:
    configured = os.environ.get("ES_DB_BACKUP_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _google_drive_root() / "earnings-summary-db-backups"


DEFAULT_RETAIN = 1
# The db_gc archive sidecar (execution/db_gc.py) holds the ONLY copy of pruned
# rows and underpins the "reversible" contract, yet it lived unbacked-up until
# 2026-08-03. It is append-only, so a snapshot only needs to be newer than the
# last prune, not daily — keep fewer, and skip re-encrypting an unchanged file.
ARCHIVE_PREFIX = "portfolio_gc_archive.db"
DEFAULT_ARCHIVE_RETAIN = 1


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skipped_unchanged_payload(receipt: BackupReceipt) -> dict[str, str]:
    """Local production can skip; the wrapper must still verify/retry upload."""
    return {
        "status": "skipped_unchanged",
        "last_verified_snapshot": receipt.snapshot_name,
        "snapshot_sha256": receipt.snapshot_sha256,
        "verified_at_utc": receipt.verified_at_utc,
    }


def _verify_encrypted_snapshot(snapshot: Path, staging: Path, expected_sha256: str) -> None:
    """Authenticate the envelope and stream the restored bytes into the source hash."""
    restored_gz = staging / "verified_roundtrip.gz"
    decrypt_file(snapshot, restored_gz, key=load_key())
    digest = hashlib.sha256()
    with gzip.open(restored_gz, "rb") as restored:
        for chunk in iter(lambda: restored.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError("encrypted backup restore hash differs from the verified snapshot")
    restored_gz.unlink()


def consistent_snapshot(src_db: Path, tmp_path: Path) -> None:
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


def source_schema_revision(conn: sqlite3.Connection) -> str:
    """Read the single current Alembic revision used in backup identity."""
    try:
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("source Alembic revision is unreadable") from exc
    if len(rows) != 1:
        raise RuntimeError("source Alembic revision is unreadable: expected exactly one revision")
    revision: object = rows[0][0]
    if not isinstance(revision, str) or not revision.strip():
        raise RuntimeError("source Alembic revision is unreadable: value is empty")
    return revision.strip()


def backup_invocation_inputs(
    dest_dir: Path,
    retain: int,
    schema_revision: str,
    run_date: str,
) -> dict[str, str | int]:
    """Build the complete logical identity for one schema/day backup."""
    return {
        "backup_dir": str(dest_dir.resolve()),
        "retain": retain,
        "run_date": run_date,
        "source_db": str(SRC_DB.resolve()),
        "source_schema_revision": schema_revision,
    }


def start_accounting(dest_dir: Path, retain: int) -> tuple[sqlite3.Connection, str]:
    """Claim the daily backup invocation before snapshot/encryption begins."""
    conn = connect_sqlite(
        SRC_DB,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        schema_revision = source_schema_revision(conn)
        run_date = datetime.now().date().isoformat()
        run_id = start_run(
            conn,
            directive="backup_db",
            ticker_scope=[],
            invocation_inputs=backup_invocation_inputs(
                dest_dir,
                retain,
                schema_revision,
                run_date,
            ),
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
    skipped_unchanged: bool = False,
) -> None:
    if accounting is None:
        return
    conn, run_id = accounting
    try:
        try:
            if skipped_unchanged:
                status = StageStatus.SKIPPED
            elif success:
                status = StageStatus.OK
            else:
                status = StageStatus.FAILED
            end_run(conn, run_id, status, error_msg)
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
    dest_dir = configured_backup_dir()
    retain = int(os.environ.get("ES_DB_BACKUP_RETAIN", str(DEFAULT_RETAIN)))
    if retain < 1:
        raise ValueError("ES_DB_BACKUP_RETAIN must be at least 1")
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        accounting = start_accounting(dest_dir, retain)
    except PipelineRunSuppressedError as exc:
        print(json.dumps(suppression_payload(exc)))
        return 75 if exc.status is StageStatus.IN_PROGRESS else 0
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
            consistent_snapshot(SRC_DB, tmp_path)
            if not _integrity_ok(tmp_path):
                raise RuntimeError("consistent snapshot failed SQLite integrity_check")
            snapshot_sha256 = _file_sha256(tmp_path)
            existing = verified_files(dest_dir, family="portfolio-db", source=SRC_DB)
            local_receipt = existing[-1][1] if existing else None
            if (
                local_receipt is not None
                and local_receipt.snapshot_sha256 == snapshot_sha256
                and local_receipt.source == str(SRC_DB.resolve())
            ):
                print(
                    "OK backup skipped (skipped_unchanged) — snapshot matches "
                    f"last locally verified snapshot {local_receipt.snapshot_name}"
                )
                print(json.dumps(_skipped_unchanged_payload(local_receipt)))
                _backup_archive_sidecar(dest_dir)
                _finish_accounting(
                    accounting,
                    success=True,
                    skipped_unchanged=True,
                    error_msg="skipped_unchanged: snapshot sha256 matches last locally verified snapshot",
                )
                return 0
            with open(tmp_path, "rb") as raw, gzip.open(tmp_gz, "wb") as gz:
                shutil.copyfileobj(raw, gz)
            encrypt_file(tmp_gz, final_path, key=load_or_create_key())
            _verify_encrypted_snapshot(final_path, Path(staging), snapshot_sha256)
        record_snapshot(
            final_path, family="portfolio-db", source=SRC_DB, snapshot_sha256=snapshot_sha256
        )
        size_mb = final_path.stat().st_size / 1e6
        print(f"OK backup -> {final_path}  ({size_mb:.1f} MB encrypted)  retained=pending-upload")
        _backup_archive_sidecar(dest_dir)
    except Exception as exc:
        _finish_accounting(accounting, success=False, error_msg=redact(exc))
        print(f"ERROR: backup failed: {redact(exc)}", file=sys.stderr)
        return 1
    _finish_accounting(accounting, success=True)
    return 0


def _backup_archive_sidecar(dest_dir: Path) -> None:
    """Capture the recovery archive; failures stop all downstream retirement.

    Missing means GC has not created an archive. A present archive must verify.
    Content identity, rather than size/mtime, controls duplicate suppression.
    """
    archive = SRC_DB.parent / "archive" / ARCHIVE_PREFIX
    if not archive.exists():
        print("(no db_gc archive to back up — GC has not pruned yet)")
        return
    if int(os.environ.get("ES_ARCHIVE_BACKUP_RETAIN", str(DEFAULT_ARCHIVE_RETAIN))) < 1:
        raise ValueError("ES_ARCHIVE_BACKUP_RETAIN must be at least 1")
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    final_path = dest_dir / f"{ARCHIVE_PREFIX}.{stamp}.gz.enc"
    with tempfile.TemporaryDirectory(prefix="archive_backup.") as staging:
        tmp_path = Path(staging) / ARCHIVE_PREFIX
        tmp_gz = Path(staging) / f"{ARCHIVE_PREFIX}.gz"
        consistent_snapshot(archive, tmp_path)
        if not _integrity_ok(tmp_path):
            raise RuntimeError("archive snapshot failed SQLite integrity_check")
        snapshot_sha256 = _file_sha256(tmp_path)
        existing = verified_files(dest_dir, family="portfolio-gc-archive", source=archive)
        if existing and existing[-1][1].snapshot_sha256 == snapshot_sha256:
            print(f"(archive unchanged since last verified snapshot: {existing[-1][0].name})")
            return
        with open(tmp_path, "rb") as raw, gzip.open(tmp_gz, "wb") as gz:
            shutil.copyfileobj(raw, gz)
        encrypt_file(tmp_gz, final_path, key=load_or_create_key())
        _verify_encrypted_snapshot(final_path, Path(staging), snapshot_sha256)
    record_snapshot(
        final_path, family="portfolio-gc-archive", source=archive, snapshot_sha256=snapshot_sha256
    )
    size_mb = final_path.stat().st_size / 1e6
    print(f"OK archive backup -> {final_path.name}  ({size_mb:.1f} MB encrypted)")


def main() -> int:
    global SRC_DB
    if SRC_DB == DEFAULT_SRC_DB:
        try:
            load_project_env(PROJECT_ROOT)
            configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
            if not configured:
                raise RuntimeError(
                    "EARNINGS_SUMMARY_DB_PATH is required; checkout fallback is prohibited"
                )
            SRC_DB = require_db_path(Path(configured))
        except Exception as exc:
            print(f"ERROR: backup source unavailable: {redact(exc)}", file=sys.stderr)
            return 1
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
            result = _run_backup()
            if result == 0:
                dest_dir = configured_backup_dir()
                files = verified_files(dest_dir, family="portfolio-db", source=SRC_DB)
                if not files or files[-1][1].source != str(SRC_DB.resolve()):
                    raise RuntimeError("no verified backup receipt for the configured source")
                print(
                    json.dumps(
                        {
                            "policy": "backup-retention-v1",
                            "status": "ready",
                            "backup_dir": str(dest_dir.resolve()),
                            "snapshot_name": files[-1][0].name,
                            "ciphertext_sha256": files[-1][1].ciphertext_sha256,
                        }
                    )
                )
            return result
    except Exception as exc:
        print(f"ERROR: backup lock failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
