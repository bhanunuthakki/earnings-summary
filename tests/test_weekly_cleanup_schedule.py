"""Contract tests for the checked-in weekly-cleanup Task Scheduler artifacts."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from execution import run_weekly_cleanup as cleanup
from src.operations import temp_run_retention

ROOT = Path(__file__).resolve().parent.parent
CRON_DIR = ROOT / "cron"
TASK_NS = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _task_text(name: str) -> str:
    return ET.parse(CRON_DIR / "weekly_cleanup.task.xml").findtext(name, namespaces=TASK_NS) or ""


def test_weekly_cleanup_task_contract() -> None:
    """The checked-in task is a local, bounded Sunday cleanup job."""
    tree = ET.parse(CRON_DIR / "weekly_cleanup.task.xml")
    root = tree.getroot()
    assert root.attrib["version"] == "1.4"

    assert _task_text("task:RegistrationInfo/task:URI") == "\\earnings-summary\\weekly_cleanup"
    assert _task_text("task:Triggers/task:CalendarTrigger/task:StartBoundary").endswith("T13:00:00")
    assert (
        _task_text("task:Triggers/task:CalendarTrigger/task:ScheduleByWeek/task:WeeksInterval")
        == "1"
    )
    assert (
        _task_text(
            "task:Triggers/task:CalendarTrigger/task:ScheduleByWeek/task:DaysOfWeek/task:Sunday"
        )
        == ""
    )
    assert _task_text("task:Principals/task:Principal/task:LogonType") == "InteractiveToken"
    assert _task_text("task:Principals/task:Principal/task:RunLevel") == "LeastPrivilege"
    assert _task_text("task:Settings/task:MultipleInstancesPolicy") == "IgnoreNew"
    assert _task_text("task:Settings/task:StartWhenAvailable") == "true"
    assert _task_text("task:Settings/task:RunOnlyIfNetworkAvailable") == "false"
    assert _task_text("task:Settings/task:ExecutionTimeLimit") == "PT15M"
    assert _task_text("task:Settings/task:RestartOnFailure/task:Interval") == "PT30M"
    assert _task_text("task:Settings/task:RestartOnFailure/task:Count") == "1"
    assert _task_text("task:Actions/task:Exec/task:Command").endswith(
        "\\cron\\run_weekly_cleanup.bat"
    )


def test_weekly_cleanup_wrapper_runs_ordered_locked_stages_without_raw_deletes() -> None:
    """The cleanup must finish before state expiry, with nonzero failures preserved."""
    wrapper = (CRON_DIR / "run_weekly_cleanup.bat").read_text(encoding="utf-8")
    normalized = wrapper.lower()

    cleanup = 'call "%project_root%\\cron\\run_python.bat" "weekly-cleanup" "filesystem-maintenance" execution\\run_weekly_cleanup.py --apply'
    expiry = 'call "%project_root%\\cron\\run_python.bat" "weekly-cleanup-expire-research" "portfolio-db" execution\\expire_stale_research.py --apply'
    assert cleanup in normalized
    assert expiry in normalized
    assert normalized.index(cleanup) < normalized.index(expiry)
    assert "if not errorlevel 1 goto expire_research" in normalized
    assert "exit /b %exit_code%" in normalized
    assert ".tmp\\cron_logs" in normalized
    assert "filesystem cleanup uses its own single-flight lane" in normalized
    assert "state-expiry step is" in normalized and "bounded db-only work" in normalized
    assert "del " not in normalized
    assert "rmdir " not in normalized
    assert "remove-item" not in normalized


def test_cleanup_retry_recovers_transient_failure_without_repeating_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A later attempt can finish a partial sweep while preserving failure evidence."""
    state, runtime = tmp_path / "state", tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()

    def isolated_roots(_repo: Path, _code: Path | None) -> list[tuple[Path, bool]]:
        return [(state / ".tmp", False), (runtime / ".tmp", False)]

    monkeypatch.setattr(temp_run_retention, "_search_roots", isolated_roots)
    now = datetime(2026, 10, 8, tzinfo=UTC)
    old = (now - timedelta(days=90)).timestamp()
    transient = runtime / ".tmp" / "completed-output" / "output.txt"
    disposable = state / ".tmp" / "completed-output" / "output.txt"
    failure_log = runtime / ".tmp" / "cron_logs" / "weekly_cleanup_20260801T200000Z.log"
    newer_log = failure_log.with_name("weekly_cleanup_20260802T200000Z.log")
    active = runtime / ".tmp" / "active-run" / "output.txt"
    for file in (transient, disposable, failure_log, newer_log, active):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("output", encoding="utf-8")
        os.utime(file, (old, old))
    failure_log.write_text('recorded failure: {"exit_code": 1}', encoding="utf-8")
    os.utime(failure_log, (old, old))
    (active.parent / "state.json").write_text('{"status": "active"}', encoding="utf-8")
    original_unlink = Path.unlink
    delete_calls: list[Path] = []
    attempted_failure = False

    def transient_unlink(file: Path, missing_ok: bool = False) -> None:
        nonlocal attempted_failure
        delete_calls.append(file)
        if file == transient and not attempted_failure:
            attempted_failure = True
            raise PermissionError("synthetic transient sharing violation")
        original_unlink(file, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", transient_unlink)
    args = [
        "--repo-root",
        str(state),
        "--code-root",
        str(runtime),
        "--now",
        now.isoformat(),
        "--apply",
    ]
    assert cleanup.main(args) == 1
    first = json.loads(capsys.readouterr().out)
    assert first["deleted"] == 1
    assert first["policies"]["runtime_tmp_unclassified_30d"]["skipped_error"] == 1
    assert transient.exists() and not disposable.exists()

    assert cleanup.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["deleted"] == 1
    assert not transient.exists()
    assert cleanup.main(args) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == 0
    assert delete_calls.count(disposable) == 1
    assert delete_calls.count(transient) == 2
    assert failure_log.exists() and newer_log.exists() and active.exists()
