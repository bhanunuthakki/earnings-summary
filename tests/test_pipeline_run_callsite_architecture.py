"""Architecture ratchets for pipeline invocation identity and suppression."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (PROJECT_ROOT / "cron", PROJECT_ROOT / "execution", PROJECT_ROOT / "src")

# These jobs intentionally key only on their resolved ticker scope because the
# upstream database/network state is mutable and completed attempts must not be
# treated as reusable results.
SCOPE_ONLY_START_RUN_CALLS = {
    "execution/derive_kpis_from_fmp.py",
    "execution/fetch_sec_xbrl.py",
    "execution/run_weekly_validation.py",
}

SCHEDULED_SUPPRESSION_BOUNDARIES = {
    "cron/backup_db.py",
    "execution/check_comp_set_drift.py",
    "execution/fetch_sec_xbrl.py",
    "execution/quarterly_refresh.py",
    "execution/restore_drill.py",
    "execution/run_morning_pipeline.py",
    "execution/run_validation_engine.py",
    "execution/run_weekly_validation.py",
    "execution/track_comp_metrics.py",
}

DEEP_SUPPRESSION_BOUNDARIES = {
    "execution/extract_competitive_mentions.py",
    "execution/extract_document_tables.py",
    "execution/extract_kpis_from_summaries.py",
    "execution/ingest_competitive_category_share.py",
    "execution/refresh_ir_kpis.py",
}


def _production_python_files() -> list[Path]:
    return sorted(path for root in PRODUCTION_ROOTS for path in root.rglob("*.py"))


def _start_run_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    start_names = {"start_run"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "pipeline.run_accounting":
            continue
        for alias in node.names:
            if alias.name == "start_run":
                start_names.add(alias.asname or alias.name)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id in start_names) or (
            isinstance(func, ast.Attribute) and func.attr == "start_run"
        ):
            calls.append(node)
    return calls


def test_every_material_start_run_call_declares_invocation_inputs() -> None:
    missing: set[str] = set()
    for path in _production_python_files():
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for call in _start_run_calls(path):
            if not any(keyword.arg == "invocation_inputs" for keyword in call.keywords):
                missing.add(relative)

    assert missing == SCOPE_ONLY_START_RUN_CALLS


def test_every_pipeline_run_callsite_has_terminal_accounting_in_its_module() -> None:
    missing: set[str] = set()
    for path in _production_python_files():
        if not _start_run_calls(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        end_names = {"end_run"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "pipeline.run_accounting":
                continue
            for alias in node.names:
                if alias.name == "end_run":
                    end_names.add(alias.asname or alias.name)
        if not any(isinstance(node, ast.Name) and node.id in end_names for node in ast.walk(tree)):
            missing.add(path.relative_to(PROJECT_ROOT).as_posix())

    assert missing == set()


def test_scheduler_and_deep_cli_boundaries_handle_suppression_explicitly() -> None:
    for relative in sorted(SCHEDULED_SUPPRESSION_BOUNDARIES | DEEP_SUPPRESSION_BOUNDARIES):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        assert "PipelineRunSuppressedError" in source, relative
        assert "suppression_payload" in source, relative
