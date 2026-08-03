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
        "run_track_comp_metrics.bat",
    ):
        text = (CRON / wrapper).read_text(encoding="utf-8")
        assert "run_python.bat" in text
        assert '"portfolio-db"' in text


def test_daily_wrappers_return_the_child_exit_code_through_endlocal() -> None:
    for wrapper in (
        "run_daily_fetch_and_brief.bat",
        "run_morning_pipeline.bat",
        "run_refresh_cache.bat",
    ):
        lines = (CRON / wrapper).read_text(encoding="utf-8").splitlines()
        call_index = next(
            index for index, line in enumerate(lines) if line.strip().lower().startswith("call ")
        )
        assert lines[call_index + 1].strip() == 'set "RC=%ERRORLEVEL%"'
        assert lines[-1].strip() == "endlocal & exit /b %RC%"


def test_daily_wrappers_stay_on_the_invoking_checkout() -> None:
    for wrapper in (
        "run_daily_fetch_and_brief.bat",
        "run_morning_pipeline.bat",
        "run_refresh_cache.bat",
        "run_track_comp_metrics.bat",
    ):
        text = (CRON / wrapper).read_text(encoding="utf-8")
        assert 'set "PROJECT_ROOT=%~dp0.."' in text
        assert 'for %%I in ("%PROJECT_ROOT%") do set "PROJECT_ROOT=%%~fI"' in text
        assert r"\scratch\earnings-summary" not in text


def test_comp_metrics_wrapper_preserves_the_first_failed_step() -> None:
    lines = (CRON / "run_track_comp_metrics.bat").read_text(encoding="utf-8").splitlines()
    call_indexes = [
        index for index, line in enumerate(lines) if line.strip().lower().startswith("call ")
    ]
    assert len(call_indexes) == 2
    assert lines[call_indexes[0] + 1].strip() == 'set "RC=%ERRORLEVEL%"'
    assert lines[call_indexes[1] + 1].strip() == 'set "STEP_RC=%ERRORLEVEL%"'
    assert lines[call_indexes[1] + 2].strip() == ('if "%RC%"=="0" set "RC=%STEP_RC%"')
    assert lines[-1].strip() == "endlocal & exit /b %RC%"


def test_shared_runtime_forwards_original_arguments_without_shift() -> None:
    """SHIFT does not alter ``%*``; the runtime must slice wrapper args itself."""
    text = (CRON / "run_python.bat").read_text(encoding="utf-8").lower()
    assert "shift" not in text
    assert "--scheduler-wrapper" in text
    assert "-- %*" in text
    assert "set llm_capture_dir=" not in text


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


def test_every_wrapper_propagates_its_exit_code() -> None:
    """A wrapper that ends on `endlocal` ALWAYS returns 0, so Task Scheduler
    records "Last Result: 0" for a job that failed outright.

    That is how three days of failed nightly backups looked healthy in every
    place an operator would check, and it applied to 36 of the 45 wrappers.
    A silent job is worse than a missing one: it reports success forever.
    """
    offenders = [
        p.name
        for p in sorted(CRON.glob("run_*.bat"))
        if "exit /b" not in p.read_bytes().decode("utf-8")
    ]
    assert not offenders, f"wrappers that swallow their exit code: {offenders}"


def test_weekly_db_gc_never_schedules_destructive_facts_depth_apply() -> None:
    text = (CRON / "run_db_gc.bat").read_text(encoding="utf-8").lower()
    invocation = next(line for line in text.splitlines() if "execution\\db_gc.py" in line)
    assert "--apply" in invocation
    assert "--policies validation-issues,telemetry,maintenance" in invocation
    assert "--include-portfolio" not in invocation


def test_no_wrapper_carries_a_stray_carriage_return() -> None:
    r"""A bare CR (not part of CRLF) inside a batch file is the signature of a
    Python non-raw string escaping the path separator during generation.

    `cron/run_db_gc.bat` shipped `%PROJECT_ROOT%\cron` + 0x0D + `un_python.bat`
    — i.e. "cron\run_python.bat" with \r eaten — so the weekly db_gc job
    registered by #1127 pointed at a file that does not exist and could never
    have run. Paired with the swallowed exit code above, it would have reported
    success forever.
    """
    offenders = []
    for path in sorted(CRON.glob("*.bat")):
        raw = path.read_bytes()
        if raw.count(b"\x0d") != raw.count(b"\x0d\x0a"):
            offenders.append(path.name)
    assert not offenders, f"batch files with a bare CR: {offenders}"


def test_every_wrapper_call_target_exists() -> None:
    """The direct consequence check for the defect above: every `call` target a
    wrapper names must actually be present on disk.

    Batch files address paths with ``\\``, which is a separator only on Windows —
    CI runs on Linux, where a raw backslash string is one long filename and every
    target would look missing. Normalize to ``/`` and resolve relative to the repo
    so the check means the same thing on both platforms. A stray CR inside the
    path survives normalization and still fails the check, which is the point.
    """
    missing: list[str] = []
    for path in sorted(CRON.glob("run_*.bat")):
        text = path.read_bytes().decode("utf-8")
        for match in re.finditer(r'call\s+"([^"]+)"', text):
            raw = match.group(1)
            if "%PROJECT_ROOT%" not in raw:
                continue
            relative = raw.split("%PROJECT_ROOT%", 1)[1].replace("\\", "/").lstrip("/")
            if not (PROJECT_ROOT / relative).exists():
                missing.append(f"{path.name} -> {raw!r}")
    assert not missing, f"wrappers calling a nonexistent target: {missing}"
