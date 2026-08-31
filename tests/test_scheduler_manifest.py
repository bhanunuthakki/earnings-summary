"""Canonical scheduled-task manifest coverage and artifact generation."""

from __future__ import annotations

import shutil
from pathlib import Path, PureWindowsPath

from scheduler_manifest import (
    TaskManifest,
    TaskSpec,
    extract_xml_metadata,
    generated_inventory_markdown,
    generated_registration_script,
    load_manifest,
    rendered_xml_bytes,
    validate_source_tree,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CRON_DIR = PROJECT_ROOT / "cron"
MANIFEST_PATH = CRON_DIR / "task_manifest.json"
RUNBOOK_PATH = CRON_DIR / "SETUP_WINDOWS_SCHEDULER.md"
EXPECTED_DISABLED_TASKS = {
    r"\earnings-summary\backfill_transcripts",
    r"\earnings-summary\discover_ir_documents",
    r"\earnings-summary\discover_ir_failing",
    r"\earnings-summary\onboard_pending",
    r"\earnings-summary\refresh_cache",
    r"\earnings-summary\refresh_ir_kpis",
    r"\earnings-summary\scan_ir_transcripts",
}


def test_manifest_has_exact_xml_and_wrapper_coverage() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    assert len(manifest.tasks) == 46
    collector = next(
        task
        for task in manifest.tasks
        if task.task_name == r"\earnings-summary\collect_operations_runtime_observations"
    )
    assert collector.xml == "collect_operations_runtime_observations.task.xml"
    assert collector.wrapper == "run_collect_operations_runtime_observations.bat"
    assert collector.schedule.repetition_interval == "PT10M"
    collector_xml = (CRON_DIR / collector.xml).read_text(encoding="utf-8")
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in collector_xml
    assert "<ExecutionTimeLimit>PT5M</ExecutionTimeLimit>" in collector_xml
    assert "<RestartOnFailure>" not in collector_xml
    # schtasks.exe accepts this CalendarTrigger only when the repetition block
    # precedes its start boundary (Windows' schema parser is order-sensitive).
    assert collector_xml.index("<Repetition>") < collector_xml.index("<StartBoundary>")
    assert all(task.task_name != r"\earnings-summary\session_distill" for task in manifest.tasks)
    assert all(task.task_name != r"\earnings-summary\monthly_p3_refresh" for task in manifest.tasks)
    semantic_review = next(
        task
        for task in manifest.tasks
        if task.task_name == r"\earnings-summary\prepare_kpi_semantic_review"
    )
    assert semantic_review.schedule.repetition_interval == "PT10M"
    assert semantic_review.xml == "prepare_kpi_semantic_review.task.xml"
    assert semantic_review.wrapper == "run_prepare_kpi_semantic_review.bat"
    semantic_xml = (CRON_DIR / semantic_review.xml).read_text(encoding="utf-8")
    assert "<ExecutionTimeLimit>PT5M</ExecutionTimeLimit>" in semantic_xml
    assert semantic_xml.index("<Repetition>") < semantic_xml.index("<StartBoundary>")
    assert r"runtime\earnings-summary\cron\run_prepare_kpi_semantic_review.bat" in semantic_xml
    semantic_wrapper = (CRON_DIR / semantic_review.wrapper).read_text(encoding="utf-8")
    assert "if not defined EARNINGS_SUMMARY_DB_PATH" in semantic_wrapper
    assert 'for %%I in ("%EARNINGS_SUMMARY_DB_PATH%") do set "DB_DIR=%%~dpI"' in semantic_wrapper
    assert 'for %%I in ("%DB_DIR%..") do set "PRODUCT_STATE_ROOT=%%~fI"' in semantic_wrapper
    assert 'set "ES_JOB_RUNTIME_REPO_ROOT=%PRODUCT_STATE_ROOT%"' in semantic_wrapper
    assert 'set "ES_JOB_RUNTIME_CODE_ROOT=%PROJECT_ROOT%"' in semantic_wrapper
    assert r'set "LOG_DIR=%PRODUCT_STATE_ROOT%\.tmp\cron_logs"' in semantic_wrapper
    assert '--code-root "%PROJECT_ROOT%"' in semantic_wrapper
    assert "--user-id" not in semantic_wrapper
    assert '--code-root "%PROJECT_ROOT%" --publish' in semantic_wrapper
    assert "http" not in semantic_wrapper.casefold()
    prepare_source = (PROJECT_ROOT / "execution" / "prepare_kpi_semantic_review.py").read_text(
        encoding="utf-8"
    )
    assert 'parser.add_argument("--user-id", default=DEFAULT_USER_ID)' in prepare_source
    assert 'default="default"' not in prepare_source
    shared_wrapper = (CRON_DIR / "run_python.bat").read_text(encoding="utf-8")
    assert '--repo-root "%ES_JOB_RUNTIME_REPO_ROOT%"' in shared_wrapper
    assert '--code-root "%ES_JOB_RUNTIME_CODE_ROOT%"' in shared_wrapper
    assert "ES_JOB_RUNTIME_CODE_ROOT is required with ES_JOB_RUNTIME_REPO_ROOT" in shared_wrapper
    assert "ES_JOB_RUNTIME_REPO_ROOT is required with ES_JOB_RUNTIME_CODE_ROOT" in shared_wrapper
    assert validate_source_tree(manifest, cron_dir=CRON_DIR) == []
    assert {task.xml for task in manifest.tasks} == {
        path.name for path in CRON_DIR.glob("*.task.xml")
    }


def test_portfolio_tracker_runtime_tasks_keep_api_ownership_and_refresh_evidence_separate() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    api = next(
        task
        for task in manifest.tasks
        if task.task_name == r"\earnings-summary\portfolio_tracker_api"
    )
    refresh = next(
        task
        for task in manifest.tasks
        if task.task_name == r"\earnings-summary\refresh_portfolio_tracker"
    )

    assert api.schedule.trigger == "BootTrigger"
    assert api.schedule.start_boundary is None
    assert refresh.schedule.trigger == "CalendarTrigger"
    assert refresh.schedule.days_interval == 1

    api_xml = (CRON_DIR / api.xml).read_text(encoding="utf-8")
    assert "<BootTrigger>" in api_xml
    assert "<UserId>S-1-5-18</UserId>" in api_xml
    assert (
        "<SecurityDescriptor>D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;IU)</SecurityDescriptor>"
        in api_xml
    )
    for forbidden_query_ace in (
        "(A;;FA;;;SY)",
        "(A;;FA;;;BA)",
        "(A;;GRGX;;;BU)",
        "(A;;GRGX;;;AU)",
        "(A;;GR;;;IU)",
        "(A;;FR;;;IU)",
    ):
        assert forbidden_query_ace not in api_xml
    assert api_xml.count("<SecurityDescriptor>") == 1
    rendered_api_xml = rendered_xml_bytes(api, cron_dir=CRON_DIR, project_root=PROJECT_ROOT).decode(
        "utf-16"
    )
    assert "(A;;GRGX;;;IU)" in rendered_api_xml
    for forbidden_query_ace in (
        "(A;;FA;;;SY)",
        "(A;;FA;;;BA)",
        "(A;;GRGX;;;BU)",
        "(A;;GRGX;;;AU)",
        "(A;;GR;;;IU)",
        "(A;;FR;;;IU)",
    ):
        assert forbidden_query_ace not in rendered_api_xml
    # UserId identifies LOCAL SYSTEM. Explicit ServiceAccount is rejected by
    # schtasks.exe on the target Windows host.
    assert "<LogonType>" not in api_xml
    assert "<RunLevel>HighestAvailable</RunLevel>" in api_xml
    refresh_xml = (CRON_DIR / refresh.xml).read_text(encoding="utf-8")
    assert "<LogonType>S4U</LogonType>" in refresh_xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in refresh_xml

    api_wrapper = (CRON_DIR / api.wrapper).read_text(encoding="utf-8")
    assert "if not defined portfolio_tracker_root" in api_wrapper.casefold()
    assert "if not defined portfolio_tracker_api_url" in api_wrapper.casefold()
    assert "earnings_summary_db_path" in api_wrapper.casefold()
    assert "execution\\serve_portfolio_tracker.py" in api_wrapper
    refresh_wrapper = (CRON_DIR / refresh.wrapper).read_text(encoding="utf-8")
    assert "if not defined portfolio_tracker_api_url" in refresh_wrapper.casefold()
    assert "earnings_summary_db_path" in refresh_wrapper.casefold()
    assert "execution\\refresh_portfolio_tracker.py" in refresh_wrapper
    assert '--code-root "%project_root%"' in refresh_wrapper.casefold()
    assert '--repo-root "%project_root%"' not in refresh_wrapper.casefold()
    assert "serve_portfolio_tracker.py" not in refresh_wrapper
    assert {task.wrapper for task in manifest.tasks} == {
        path.name for path in CRON_DIR.glob("run_*.bat") if path.name != "run_python.bat"
    }


def test_operations_runtime_collector_keeps_scheduler_access_query_only() -> None:
    collector = (
        PROJECT_ROOT / "execution" / "collect_operations_runtime_observations.py"
    ).read_text(encoding="utf-8")

    assert '["schtasks.exe", "/Query", "/FO", "CSV"]' in collector
    for forbidden_operation in (
        '"/Run"',
        '"/End"',
        '"/Change"',
        '"/Delete"',
        "SetSecurityDescriptor",
    ):
        assert forbidden_operation not in collector


def test_source_xml_preserves_exact_expected_disabled_lanes() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    disabled = {
        task.task_name
        for task in manifest.tasks
        if not extract_xml_metadata(CRON_DIR / task.xml).enabled
    }

    assert disabled == EXPECTED_DISABLED_TASKS


def test_generated_registration_and_inventory_are_deterministic_and_current() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    first_script = generated_registration_script(manifest)
    first_doc = generated_inventory_markdown(manifest)
    assert first_script == generated_registration_script(load_manifest(MANIFEST_PATH))
    assert first_doc == generated_inventory_markdown(load_manifest(MANIFEST_PATH))
    assert (CRON_DIR / "register_tasks.generated.ps1").read_text(encoding="utf-8") == first_script
    assert (CRON_DIR / "TASKS.generated.md").read_text(encoding="utf-8") == first_doc
    assert "/Create /TN '\\earnings-summary\\capture_poller'" not in first_script
    registered_tasks = [
        task
        for task in manifest.tasks
        if task.task_name.casefold() != r"\earnings-summary\capture_poller".casefold()
    ]
    assert first_script.count("schtasks.exe /Create") == len(registered_tasks)
    assert first_script.count("Failed to register scheduled task") == len(registered_tasks)
    tracker_create = (
        "& schtasks.exe /Create /TN '\\earnings-summary\\portfolio_tracker_api' "
        "/XML (Join-Path $renderDir 'portfolio_tracker_api.task.xml') /F"
    )
    tracker_acl = (
        "& $taskSecurityScript -TaskPath '\\earnings-summary\\portfolio_tracker_api' "
        "-RenderedXmlPath (Join-Path $renderDir 'portfolio_tracker_api.task.xml')"
    )
    assert tracker_create in first_script
    assert tracker_acl in first_script
    assert first_script.index(tracker_create) < first_script.index(tracker_acl)
    assert first_script.count("& $taskSecurityScript -TaskPath") == len(registered_tasks)
    assert "| Windows service |" in first_doc


def test_task_security_descriptor_helper_is_fail_closed_and_rollback_safe() -> None:
    helper = (CRON_DIR / "apply_task_security_descriptor.ps1").read_text(encoding="utf-8")

    assert "'/t:Task/t:RegistrationInfo/t:SecurityDescriptor'" in helper
    assert "'/t:Task/t:RegistrationInfo/t:URI'" in helper
    assert "Rendered task XML URI does not exactly match TaskPath" in helper
    assert "$TaskDontAddPrincipalAce = 0x10" in helper
    assert "$registeredTask.GetSecurityDescriptor(" in helper
    assert "$previousDescriptor" in helper
    assert "$registeredTask.SetSecurityDescriptor(" in helper
    assert "Rollback readback did not match" in helper
    assert "DiscretionaryAclProtected" in helper
    assert "AccessAllowed" in helper
    assert "S-1-5-18" in helper
    assert "S-1-5-32-544" in helper
    assert "S-1-5-4" in helper
    assert "[uint32]268435456" in helper
    assert "[uint32]2684354560" in helper
    assert "[uint32]2032127" in helper
    assert "[uint32]1179817" in helper
    assert "unexpected trustee" in helper
    assert "unexpected access mask" in helper
    assert "nonzero ACE flags" in helper
    assert "Assert-DeclaredQueryOnlyTaskDacl" in helper
    assert "Assert-ActualQueryOnlyTaskDacl" in helper
    assert "Get-DaclSemanticSignature" in helper
    assert '"protected=$protected;daclNull=$daclNull;"' in helper
    assert '"aceCount=$($aceSignatures.Count);aces=' in helper
    assert "$VerifyOnly" in helper
    assert "GetSddlForm" not in helper


def test_operator_runbook_uses_generated_registration_and_safe_recovery_contract() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    normalized = runbook.casefold()
    manifest = load_manifest(MANIFEST_PATH)
    generated_registration = (CRON_DIR / "register_tasks.generated.ps1").read_text(encoding="utf-8")
    scheduler_tasks = [
        task
        for task in manifest.tasks
        if task.task_name.casefold() != r"\earnings-summary\capture_poller".casefold()
    ]

    assert f"{len(manifest.tasks)} operational declarations" in runbook
    assert f"{len(scheduler_tasks)} Task Scheduler registrations" in runbook
    assert r"C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary" in runbook
    assert r"C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary" in runbook
    assert "register_tasks.generated.ps1" in runbook
    assert "-RepoRoot $EarningsSummaryCodeRoot" in runbook
    assert "schtasks /create /tn" not in normalized
    assert r"scratch\earnings-summary\cron" not in normalized
    for tracker_task in (
        r"\earnings-summary\portfolio_tracker_api",
        r"\earnings-summary\refresh_portfolio_tracker",
    ):
        assert tracker_task in runbook
        assert tracker_task in generated_registration

    assert "execution/verify_cron_registration.py" in runbook
    assert "schtasks.exe /Query" in runbook
    assert "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;IU)" in runbook
    assert "`GR` alone can expose the" in runbook
    assert "do not broaden it to Builtin or Authenticated" in runbook
    assert "must remain query-only" in runbook
    assert "SetSecurityDescriptor" in runbook
    assert "GetSecurityDescriptor(4)" in runbook
    assert "-VerifyOnly" in runbook
    assert "0x1f01ff" in runbook
    assert "0x1200a9" in runbook
    assert "actual task DACL" in runbook
    assert "S-1-5-18" in runbook

    # Keep the operator path contract aligned with backup_db.py and
    # restore_db.py: mounted Drive roots D:..Z: win over the stale C: mirror.
    assert "first existing mounted `<drive>:\\My Drive` from `D:` through `Z:`" in runbook
    assert "`G:\\My Drive`" in runbook
    assert "`C:\\Users\\Bhanu\\My Drive`" in runbook
    assert "%USERPROFILE%\\My Drive" not in runbook
    assert "an exact schema-version match against the live DB" in runbook
    assert "a soft schema-version match" not in runbook

    assert "cron/restore_db.py --list" in runbook
    assert "cron/restore_db.py --latest" in runbook
    assert "--to $EarningsSummaryRecoveryDbPath" in runbook
    assert "execution/create_sqlite_snapshot.py" in runbook
    assert "$EarningsSummaryExportDbPath" in runbook
    assert ".manifest.json" in runbook
    assert "rollback" in normalized


def test_rendered_actions_point_to_invoking_checkout(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    task = manifest.tasks[0]
    render_dir = tmp_path / "rendered"
    render_dir.mkdir()
    rendered = render_dir / task.xml
    rendered.write_bytes(rendered_xml_bytes(task, cron_dir=CRON_DIR, project_root=PROJECT_ROOT))
    metadata = extract_xml_metadata(rendered)
    assert Path(metadata.command) == PROJECT_ROOT.resolve() / "cron" / task.wrapper
    assert PureWindowsPath(metadata.command).name == task.wrapper


def test_validation_reports_orphan_xml_and_wrapper(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    source = manifest.tasks[0]
    shutil.copy2(CRON_DIR / source.xml, tmp_path / source.xml)
    shutil.copy2(CRON_DIR / source.wrapper, tmp_path / source.wrapper)
    (tmp_path / "orphan.task.xml").write_text("<Task/>", encoding="utf-8")
    (tmp_path / "run_orphan.bat").write_text("@echo off\n", encoding="utf-8")
    one_task = TaskManifest(
        version=manifest.version,
        namespace=manifest.namespace,
        tasks=(
            TaskSpec(
                task_name=source.task_name,
                xml=source.xml,
                wrapper=source.wrapper,
                schedule=source.schedule,
            ),
        ),
    )
    errors = validate_source_tree(one_task, cron_dir=tmp_path)
    assert "orphan XML not in manifest: orphan.task.xml" in errors
    assert "orphan wrapper not in manifest: run_orphan.bat" in errors


def test_validation_rejects_wrapper_that_escapes_its_checkout(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    source = manifest.tasks[0]
    shutil.copy2(CRON_DIR / source.xml, tmp_path / source.xml)
    (tmp_path / source.wrapper).write_text(
        "set PROJECT_ROOT=%USERPROFILE%\\.gemini\\antigravity\\scratch\\earnings-summary\n",
        encoding="utf-8",
    )
    one_task = TaskManifest(
        version=manifest.version,
        namespace=manifest.namespace,
        tasks=(source,),
    )

    errors = validate_source_tree(one_task, cron_dir=tmp_path)

    assert f"{source.wrapper}: wrapper does not resolve from its own checkout" in errors
    assert f"{source.wrapper}: wrapper hardcodes a mutable checkout" in errors


def test_validation_rejects_wrapper_without_standard_exit_tail(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    source = manifest.tasks[0]
    shutil.copy2(CRON_DIR / source.xml, tmp_path / source.xml)
    (tmp_path / source.wrapper).write_text(
        '@echo off\nset "PROJECT_ROOT=%~dp0.."\necho done\n',
        encoding="utf-8",
    )
    one_task = TaskManifest(
        version=manifest.version,
        namespace=manifest.namespace,
        tasks=(source,),
    )
    errors = validate_source_tree(one_task, cron_dir=tmp_path)
    assert any(
        "wrapper does not end with standard 'endlocal & exit /b' exit tail" in err for err in errors
    )
