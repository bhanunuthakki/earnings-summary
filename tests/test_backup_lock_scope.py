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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402

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


def test_scheduler_backup_requires_encrypted_receipt_before_file_gc_apply() -> None:
    """File retention belongs to the existing daily backup chain.

    A zero exit alone is insufficient because the backup's idempotency guard can
    suppress an invocation. The wrapper must prove that this invocation emitted
    an encrypted snapshot receipt, prove the referenced file exists, and only
    then run destructive file retention. Its exit code must remain the final
    scheduled-task result.
    """
    text = BACKUP_WRAPPER.read_text(encoding="utf-8", errors="replace")
    lowered = text.lower()

    backup_index = lowered.index("cron\\backup_db.py")
    receipt_index = lowered.index("ok backup ->")
    existence_index = lowered.index("test-path -literalpath")
    gc_index = lowered.index("execution\\backup_file_gc.py")
    assert backup_index < receipt_index < existence_index < gc_index
    assert ".gz.enc" in lowered[receipt_index:gc_index]
    assert "^|" not in text, "quoted PowerShell pipelines must not receive CMD caret escapes"

    gc_invocation = next(
        line
        for line in text.splitlines()
        if "run_python.bat" in line and "backup_file_gc.py" in line
    )
    assert '"backup-file-gc"' in gc_invocation
    assert "--apply" in gc_invocation
    assert text.rstrip().endswith("endlocal & exit /b %RC%")


def test_scheduler_backup_publishes_encrypted_artifacts_headlessly() -> None:
    text = BACKUP_WRAPPER.read_text(encoding="utf-8", errors="replace")

    assert "execution\\upload_drive_backups.py" in text
    assert '--pattern "portfolio.db.*.gz.enc"' in text
    assert '--backup-set "portfolio-db" --retain 14 --latest-only' in text
    assert '--backup-set "portfolio-gc-archive" --retain 6 --allow-empty --latest-only' in text
    assert text.index("execution\\upload_drive_backups.py") < text.index(
        "execution\\backup_file_gc.py"
    )


def test_scheduler_backup_treats_completed_idempotent_retry_as_successful_noop() -> None:
    """A completed same-day invocation is healthy, but must not trigger GC.

    ``backup_db.py`` emits a stable ``already_done`` JSON receipt and exits
    zero when run accounting deduplicates a completed backup.  Task Scheduler
    retries must preserve that success without pretending a new snapshot was
    created or authorizing destructive file retention.
    """
    text = BACKUP_WRAPPER.read_text(encoding="utf-8", errors="replace").lower()

    backup_index = text.index("cron\\backup_db.py")
    already_done_index = text.index("already_done")
    receipt_index = text.index("ok backup ->")
    gc_index = text.index("execution\\backup_file_gc.py")
    assert backup_index < already_done_index < receipt_index < gc_index
    assert "if not errorlevel 1 goto done" in text[already_done_index:receipt_index]
