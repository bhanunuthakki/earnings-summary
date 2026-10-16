"""What the backup job may and may not be blocked by.

The snapshot is a READER: SQLite's online-backup API is safe while other writers
work, and ``_integrity_ok`` is what proves a snapshot good. Claiming the
database's exclusive ``portfolio-db`` write set therefore bought no safety while
costing every run that overlapped any writer -- ``JobLock`` is fail-fast with
zero wait. On 2026-08-03 the 02:45 run gave up 12 ms in with "write set busy:
portfolio-db" while an hourly onboard job (01:17 -> 03:20) held it; four
consecutive scheduled backups were lost the same way.

These tests pin the distinction: backups serialize against OTHER BACKUPS (they
share a destination directory and its retention prune) and against nothing else.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from runtime.job_runtime import JobAlreadyRunningError, JobLock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_WRAPPER = PROJECT_ROOT / "cron" / "run_backup_db.bat"


def test_a_db_writer_does_not_block_a_backup(tmp_path: Path) -> None:
    """The regression that lost four nights of backups."""
    # The second acquisition is the assertion: it must not raise.
    with (
        JobLock(tmp_path, "some-pipeline", ["portfolio-db"]),
        JobLock(tmp_path, "backup_db", ["db-backup"]),
    ):
        pass


def test_a_backup_does_not_block_a_db_writer(tmp_path: Path) -> None:
    """Symmetric: a 3-minute snapshot must not stall the pipeline either."""
    with (
        JobLock(tmp_path, "backup_db", ["db-backup"]),
        JobLock(tmp_path, "some-pipeline", ["portfolio-db"]),
    ):
        pass


def test_two_backups_still_exclude_each_other(tmp_path: Path) -> None:
    """The exclusion that IS real: concurrent runs race on the destination
    directory and its retention prune."""
    # Entered in order: the first lock is held, then the second must raise.
    with (
        JobLock(tmp_path, "backup_db", ["db-backup"]),
        pytest.raises(JobAlreadyRunningError, match="db-backup"),
        JobLock(tmp_path, "backup_db_direct", ["db-backup"]),
    ):
        pass


def test_scheduler_wrapper_declares_the_backup_write_set() -> None:
    """The .bat is the only place the scheduled run's write set is declared --
    the task manifest carries schedule and wrapper, not write sets. If this
    reverts to portfolio-db the job silently returns to losing races, and the
    only symptom is a 0-byte log plus exit 75."""
    text = BACKUP_WRAPPER.read_text(encoding="utf-8", errors="replace")
    invocation = next(
        line for line in text.splitlines() if "run_python.bat" in line and "backup_db.py" in line
    )
    assert '"db-backup"' in invocation, invocation
    assert '"portfolio-db"' not in invocation, invocation


def test_scheduler_backup_requires_structured_receipt_before_upload() -> None:
    text = BACKUP_WRAPPER.read_text(encoding="utf-8").lower()
    assert text.index("cron\\backup_db.py") < text.index("backup-retention-v1")
    assert "convertfrom-json" in text
    assert "test-path -literalpath $r.backup_dir" in text
    assert "^|" not in text
    assert "find_existing_receipt" not in text
    assert "execution\\backup_file_gc.py" not in text
    assert text.rstrip().endswith("endlocal & exit /b %rc%")


def test_scheduler_uploads_every_family_before_retirement() -> None:
    text = BACKUP_WRAPPER.read_text(encoding="utf-8").lower()
    calls = [line for line in text.splitlines() if "execution\\upload_drive_backups.py" in line]
    assert len(calls) == 4
    assert all("--defer-retention" in line for line in calls[:2])
    assert all("--finalize-only" in line for line in calls[2:])
    assert '--backup-set "portfolio-db"' in calls[0]
    assert '--backup-set "portfolio-gc-archive"' in calls[1]
    assert 'if not defined es_db_backup_retain set "es_db_backup_retain=1"' in text
    assert 'if not defined es_archive_backup_retain set "es_archive_backup_retain=1"' in text
    assert 'if not "%rc%"=="0" goto done' in text


def test_unchanged_and_idempotent_runs_share_deterministic_receipt() -> None:
    text = BACKUP_WRAPPER.read_text(encoding="utf-8").lower()
    assert "skipped_unchanged" in text and "already_done" in text
    assert "get-childitem" not in text
    assert "ok backup ->" not in text
    assert "backup-retention-v1" in text
