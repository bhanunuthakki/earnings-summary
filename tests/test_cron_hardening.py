"""Regression checks for scheduler hardening that do not require Windows."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CRON = PROJECT_ROOT / "cron"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _start_boundary(filename: str) -> str:
    root = ET.parse(CRON / filename).getroot()
    value = root.findtext(".//t:StartBoundary", namespaces=NS)
    assert value is not None
    return value


def test_first_of_month_03_00_jobs_are_staggered() -> None:
    """The monthly jobs previously collided at 03:00 on every first day."""
    assert _start_boundary("monthly_p3_refresh.task.xml").endswith("03:20:00")
    assert _start_boundary("refresh_scenario_priors.task.xml").endswith("03:40:00")


def test_project_backup_excludes_all_standard_credential_paths() -> None:
    backup = (CRON / "backup_project.ps1").read_text(encoding="utf-8")
    for forbidden in (".env", "credentials.json", "token.json", "*.pem", "*.key"):
        assert forbidden in backup


def test_critical_wrappers_use_explicit_runtime_and_write_lock() -> None:
    for wrapper in (
        "run_backup_db.bat",
        "run_daily_fetch_and_brief.bat",
        "run_morning_pipeline.bat",
        "run_refresh_cache.bat",
        "run_monthly_p3_refresh.bat",
        "run_refresh_scenario_priors.bat",
    ):
        text = (CRON / wrapper).read_text(encoding="utf-8")
        assert "run_python.bat" in text
        assert '"portfolio-db"' in text


def test_shared_runtime_forwards_original_arguments_without_shift() -> None:
    """SHIFT does not alter ``%*``; the runtime must slice wrapper args itself."""
    text = (CRON / "run_python.bat").read_text(encoding="utf-8").lower()
    assert "shift" not in text
    assert "--scheduler-wrapper" in text
    assert "-- %*" in text


def test_scheduled_wrappers_do_not_invoke_path_dependent_python() -> None:
    command = re.compile(r"^(?:call\s+)?python(?:\.exe)?(?:\s|$)", re.IGNORECASE)
    violations: list[str] = []
    for wrapper in sorted(CRON.glob("run_*.bat")):
        if wrapper.name == "run_python.bat":
            continue
        lines = wrapper.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.lower().startswith(("rem ", "::")):
                continue
            if command.search(stripped):
                violations.append(f"{wrapper.name}:{line_number}: {stripped}")
    assert violations == []
