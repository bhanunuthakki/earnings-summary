"""Contract tests for the checked-in weekly-cleanup Task Scheduler artifacts."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

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
    assert _task_text("task:Settings/task:StartWhenAvailable") == "false"
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

    cleanup = 'call "%project_root%\\cron\\run_python.bat" "weekly-cleanup" "portfolio-db" execution\\run_weekly_cleanup.py --apply'
    expiry = 'call "%project_root%\\cron\\run_python.bat" "weekly-cleanup-expire-research" "portfolio-db" execution\\expire_stale_research.py --apply'
    assert cleanup in normalized
    assert expiry in normalized
    assert normalized.index(cleanup) < normalized.index(expiry)
    assert "if not errorlevel 1 goto expire_research" in normalized
    assert "exit /b %exit_code%" in normalized
    assert ".tmp\\cron_logs" in normalized
    assert "cannot hold the portfolio-db lock across the two run_python calls" in normalized
    assert "del " not in normalized
    assert "rmdir " not in normalized
    assert "remove-item" not in normalized
