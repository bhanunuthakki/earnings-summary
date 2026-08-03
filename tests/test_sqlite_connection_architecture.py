"""Architecture ratchet for migration to ``sqlite_runtime.connect_sqlite``."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (PROJECT_ROOT / "src", PROJECT_ROOT / "execution")
INVESTOR_GRADE_CORE_ROOTS = (
    PROJECT_ROOT / "src" / "provenance",
    PROJECT_ROOT / "src" / "search",
)
INVESTOR_GRADE_EXECUTION_FILES = (
    "audit_data_cutover_readiness.py",
    "audit_evidence_integrity.py",
    "backfill_evidence_ledger.py",
    "backfill_financial_fact_resolutions.py",
    "backfill_fulltext_evidence.py",
    "backfill_image_ocr_evidence.py",
    "backfill_ocr_evidence.py",
    "backfill_pdf_table_evidence.py",
    "backfill_sec_companyfacts_evidence.py",
    "build_evidence_vector_index.py",
    "build_grounded_search_corpus.py",
    "capture_expected_sec_documents.py",
    "capture_ir_authority_surfaces.py",
    "capture_observed_ir_documents.py",
    "evaluate_embedding_models.py",
    "initialize_semantic_review.py",
    "match_legacy_companyfacts_evidence.py",
    "promote_embedding_model.py",
    "prepare_data_cutover.py",
    "reconcile_source_coverage.py",
    "refresh_source_coverage_from_evidence.py",
    "resolve_foreign_identity_blockers.py",
    "sync_ir_source_inventory.py",
    "sync_sec_filing_inventory.py",
    "verify_ir_home_authorities.py",
)
CORE_SCHEDULED_DATA_PATHS = (
    PROJECT_ROOT / "execution" / "daily_fetch_and_brief.py",
    PROJECT_ROOT / "execution" / "run_morning_pipeline.py",
    PROJECT_ROOT / "execution" / "refresh_dcf.py",
    PROJECT_ROOT / "src" / "timeseries" / "loaders.py",
)

# The runtime implementation owns persistent application connections. The
# repair CLI retains one explicit arbitrary-URI seam so operators can inspect
# or repair an isolated SQLite copy addressed by a full URI; its normal path
# still uses connect_sqlite. schema_compat's drift probe cannot route through
# connect_sqlite at all: sqlite_runtime imports schema_compat, so the guard is
# BELOW the runtime, not a caller of it. It is also deliberately cheaper — a
# 5s busy_timeout rather than the writer policy's 30s, because the probe runs
# before EVERY scheduled job and a stalled preflight would delay the whole
# cron fleet. gc_restore.py's --drill mode opens a THROWAWAY temp SQLite DB
# (a TemporaryDirectory scratch file, never the portfolio DB) as its own main
# schema and ATTACHes the live DB + archive read-only; routing that through
# connect_sqlite would wrongly impose the writer WAL/busy_timeout policy and a
# schema preflight the empty scratch DB cannot satisfy. Broad directory-level
# debt allowlists are prohibited.
INTENTIONAL_DIRECT_SQLITE_CONNECT_CALLS = {
    "execution/fix_kpi_series.py": 1,
    "execution/gc_restore.py": 1,
    "src/schema_compat.py": 1,
    "src/sqlite_runtime.py": 2,
}
SQLITE_RUNTIME_CALLS = 2


def _direct_sqlite_connect_calls(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sqlite3"
        for node in ast.walk(tree)
    )


def _unguarded_connect_sqlite_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "connect_sqlite"
        and not any(keyword.arg == "role" for keyword in node.keywords)
    ]


def test_only_the_central_runtime_owns_raw_sqlite_connections() -> None:
    """Production callers declare a role through sqlite_runtime."""
    actual = Counter(
        path.relative_to(PROJECT_ROOT).as_posix()
        for root in PRODUCTION_ROOTS
        for path in root.rglob("*.py")
        for _ in range(_direct_sqlite_connect_calls(path))
    )
    assert dict(actual) == INTENTIONAL_DIRECT_SQLITE_CONNECT_CALLS


def test_pipeline_queries_uses_the_central_connection_runtime() -> None:
    assert _direct_sqlite_connect_calls(PROJECT_ROOT / "src" / "pipeline" / "queries.py") == 0


def test_core_scheduled_data_paths_use_the_central_connection_runtime() -> None:
    assert {
        path.relative_to(PROJECT_ROOT).as_posix(): _direct_sqlite_connect_calls(path)
        for path in CORE_SCHEDULED_DATA_PATHS
    } == {
        "execution/daily_fetch_and_brief.py": 0,
        "execution/run_morning_pipeline.py": 0,
        "execution/refresh_dcf.py": 0,
        "src/timeseries/loaders.py": 0,
    }


def test_investor_grade_data_core_has_no_raw_sqlite_connections() -> None:
    core_calls = sum(
        _direct_sqlite_connect_calls(path)
        for root in INVESTOR_GRADE_CORE_ROOTS
        for path in root.rglob("*.py")
    )
    execution_calls = sum(
        _direct_sqlite_connect_calls(PROJECT_ROOT / "execution" / filename)
        for filename in INVESTOR_GRADE_EXECUTION_FILES
    )
    assert core_calls == 0
    assert execution_calls == 0


def test_central_connection_calls_declare_an_explicit_capability_role() -> None:
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): lines
        for root in PRODUCTION_ROOTS
        for path in root.rglob("*.py")
        if (lines := _unguarded_connect_sqlite_calls(path))
    }
    assert offenders == {}


def test_sqlite_runtime_is_the_explicit_policy_implementation() -> None:
    assert (
        _direct_sqlite_connect_calls(PROJECT_ROOT / "src" / "sqlite_runtime.py")
        == SQLITE_RUNTIME_CALLS
    )
