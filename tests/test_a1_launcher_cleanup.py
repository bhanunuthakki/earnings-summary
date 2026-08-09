"""A1 contracts for root launchers and the protected morning schedule."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CRON_DIR = PROJECT_ROOT / "cron"
TASK_NS = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
FINITE_ROOT_LAUNCHERS = {
    "build_report.bat",
    "process_comments.bat",
    "refresh_fmp.bat",
    "refresh_news.bat",
    "refresh_transcripts.bat",
}


def _scheduled_time(filename: str) -> time:
    root = ET.parse(CRON_DIR / filename).getroot()
    boundary = root.findtext(".//task:StartBoundary", namespaces=TASK_NS)
    assert boundary is not None
    return time.fromisoformat(boundary.split("T", 1)[1])


def test_legacy_full_refresh_wrapper_is_deleted() -> None:
    assert not (PROJECT_ROOT / "full_refresh.bat").exists()


def test_finite_root_launchers_use_managed_runtime_and_explicit_write_set() -> None:
    actual = {
        path.name for path in PROJECT_ROOT.glob("*.bat") if path.name != "start_comments_server.bat"
    }
    assert actual == FINITE_ROOT_LAUNCHERS
    for launcher in sorted(actual):
        invocations = [
            line
            for line in (PROJECT_ROOT / launcher).read_text(encoding="utf-8").splitlines()
            if "run_python.bat" in line and not line.lstrip().lower().startswith("rem ")
        ]
        assert invocations, launcher
        assert all(
            re.search(r'run_python\.bat"\s+"[^\"]+"\s+"portfolio-db"', invocation)
            for invocation in invocations
        ), launcher


def test_long_lived_server_bootstraps_sqlite_without_holding_writer_lock() -> None:
    launcher = (PROJECT_ROOT / "start_comments_server.bat").read_text(encoding="utf-8")
    assert "sqlite_bootstrap.py" in launcher
    assert "run_python.bat" not in launcher


def test_transcript_jobs_do_not_run_inside_protected_morning_window() -> None:
    protected_start = time(3, 0)
    protected_end = time(5, 0)
    for filename in ("backfill_transcripts.task.xml", "scan_ir_transcripts.task.xml"):
        scheduled = _scheduled_time(filename)
        assert not protected_start <= scheduled < protected_end, (filename, scheduled)


def test_transcript_schedule_documentation_matches_checked_in_triggers() -> None:
    expectations = {
        "backfill_transcripts.task.xml": ("run_backfill_transcripts.bat", "02:00"),
        "scan_ir_transcripts.task.xml": ("run_scan_ir_transcripts.bat", "02:15"),
    }
    for filename, (wrapper_name, label) in expectations.items():
        root = ET.parse(CRON_DIR / filename).getroot()
        description = root.findtext("task:RegistrationInfo/task:Description", namespaces=TASK_NS)
        wrapper = (CRON_DIR / wrapper_name).read_text(encoding="utf-8")
        assert description is not None and label in description
        assert label in wrapper
