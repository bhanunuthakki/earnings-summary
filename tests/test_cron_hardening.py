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


def test_scenario_priors_wrapper_stays_on_invoking_checkout() -> None:
    root = ET.parse(CRON / "refresh_scenario_priors.task.xml").getroot()
    command = root.findtext(".//t:Actions/t:Exec/t:Command", namespaces=NS)
    assert command is not None
    assert r"\runtime\earnings-summary\cron\run_refresh_scenario_priors.bat" in command
    assert r"\scratch\earnings-summary" not in command

    wrapper = (CRON / "run_refresh_scenario_priors.bat").read_text(encoding="utf-8")
    assert "%~dp0.." in wrapper
    assert r"\scratch\earnings-summary" not in wrapper


def test_production_capture_is_allowlisted_and_has_no_extra_llm_job() -> None:
    expected = {
        "run_backfill_transcripts.bat": {"saydo_commitment_extract"},
        "run_refresh_dirty_artifacts.bat": {
            "saydo_filter",
            "valuation_basis",
            "exec_comp_alignment",
            "company_description",
            "recent_developments",
        },
        "run_morning_pipeline.bat": {"material_news_classification"},
    }
    for filename, purposes in expected.items():
        text = (CRON / filename).read_text(encoding="utf-8")
        assert (
            'if not defined LLM_CAPTURE_DIR set "LLM_CAPTURE_DIR=%LOCALAPPDATA%'
            '\\earnings-summary\\llm_capture"' in text
        )
        assert (
            "if not defined EARNINGS_SUMMARY_CAPTURE_RETENTION_DAYS "
            "set EARNINGS_SUMMARY_CAPTURE_RETENTION_DAYS=90" in text
        )
        match = re.search(r"^set LLM_CAPTURE_PURPOSES=(.+)$", text, re.MULTILINE)
        assert match is not None
        assert set(match.group(1).strip().split(",")) == purposes
        assert "run_llm_evals.py" not in text


def test_project_backup_excludes_all_standard_credential_paths() -> None:
    backup = (CRON / "backup_project.ps1").read_text(encoding="utf-8")
    assert r"data\llm_capture" in backup
    assert r"data\secrets" in backup
    assert "Join-Path $repo 'data\\secrets'" in backup
    assert 'throw "robocopy ERROR' in backup
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
    assert "set llm_capture_dir=" not in text
    assert "where py" not in text
    assert "py -3.11" not in text
    assert r"venv\scripts\python.exe" in text


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
