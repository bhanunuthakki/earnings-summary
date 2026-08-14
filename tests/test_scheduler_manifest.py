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
    assert len(manifest.tasks) == 43
    assert all(task.task_name != r"\earnings-summary\session_distill" for task in manifest.tasks)
    assert validate_source_tree(manifest, cron_dir=CRON_DIR) == []
    assert {task.xml for task in manifest.tasks} == {
        path.name for path in CRON_DIR.glob("*.task.xml")
    }
    assert {task.wrapper for task in manifest.tasks} == {
        path.name for path in CRON_DIR.glob("run_*.bat") if path.name != "run_python.bat"
    }


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
    assert "| Windows service |" in first_doc


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
