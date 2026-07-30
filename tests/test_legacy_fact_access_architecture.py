"""Freeze legacy fact mutation seams while the v2 plane takes ownership.

The allowlists are migration debt, not approved extension points.  Removing an
entry is always allowed; adding one requires an explicit architecture decision.
Migrations are excluded because their historical data-shaping SQL is separately
versioned and reviewed.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (ROOT / "src", ROOT / "execution")

_DIRECT_MUTATION = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+(financial_facts|kpi_facts)\b",
    re.IGNORECASE,
)
_DYNAMIC_MUTATION = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+\{(?:table|fact_table)\}",
    re.IGNORECASE,
)
_LEGACY_RESTATEMENT_HELPERS = re.compile(
    r"\b(?:insert_with_restatement_detection|"
    r"insert_kpi_with_restatement_detection)\b"
)
_DIRECT_READ = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:financial_facts|kpi_facts)\b",
    re.IGNORECASE,
)

# Pre-v2 mutation debt.  New production code must use SourceFactRepository.
_DIRECT_MUTATION_DEBT = frozenset(
    {
        "execution/backfill_fiscal_period_stamps.py",
        "execution/backfill_ir_deck_locators.py",
        "execution/fix_kpi_series.py",
        "execution/mark_kpi_cadence.py",
        "execution/process_report_comments.py",
        "src/ir_pipeline/ingest.py",
        "src/pipeline/kpi_persistence.py",
        "src/pipeline/restatement_detector.py",
    }
)
_DYNAMIC_MUTATION_DEBT = frozenset({"src/pipeline/confidence.py"})
_RESTATEMENT_HELPER_DEBT = frozenset(
    {
        "src/compute/_common.py",
        "src/compute/fmp_derived_kpis.py",
        "src/compute/metrics_engine/io.py",
        "src/pipeline/kpi_persistence.py",
        "src/pipeline/restatement_detector.py",
        "src/pipeline/sec_xbrl.py",
    }
)
AUDITED_LEGACY_FACT_READS = {
    "execution/backfill_fiscal_period_stamps.py": 1,
    "execution/backfill_ir_deck_locators.py": 2,
    "execution/daily_fetch_and_brief.py": 2,
    "execution/extract_kpis_from_ir.py": 1,
    "execution/fix_kpi_series.py": 2,
    "execution/fmp_backpop.py": 1,
    "execution/grade_predictions.py": 1,
    "execution/mark_kpi_cadence.py": 2,
    "execution/onboard_pending_tickers.py": 2,
    "execution/pressure_test_thesis.py": 1,
    "execution/prune_misscaled_capture_facts.py": 1,
    "execution/retype_misfiled_sec_ir_docs.py": 2,
    "execution/seed_kpi_definitions.py": 1,
    "src/allocation/eligibility.py": 1,
    "src/ask/grounding.py": 5,
    "src/bear_case_grader.py": 1,
    "src/cockpit_fundamentals.py": 2,
    "src/competitive/holdings_sync.py": 1,
    "src/compute/fmp_derived_kpis.py": 3,
    "src/compute/holding_scorecard.py": 2,
    "src/compute/kpi_extract_summaries.py": 3,
    "src/compute/kpi_resolver.py": 3,
    "src/compute/metrics_engine/io.py": 5,
    "src/compute/say_do.py": 1,
    "src/compute/segment_q4_derive.py": 1,
    "src/compute/segment_quarterly_10q.py": 1,
    "src/compute/segments.py": 1,
    "src/compute/soft_rule_evaluator.py": 2,
    "src/compute/thesis_evaluator.py": 2,
    "src/credibility/observations.py": 3,
    "src/dcf/fact_drivers.py": 1,
    "src/decision_conditions.py": 4,
    "src/pipeline/confidence.py": 2,
    "src/pipeline/key_metrics.py": 1,
    "src/pipeline/kpi_persistence.py": 3,
    "src/pipeline/peeks.py": 3,
    "src/pipeline/quarterly_refresh.py": 1,
    "src/pipeline/reader_tier_audit.py": 1,
    "src/pipeline/research_cockpit.py": 1,
    "src/pipeline/restatement_detector.py": 3,
    "src/pipeline/restatements_panel.py": 4,
    "src/pipeline/validation_engine.py": 4,
    "src/provenance/financial_fact_resolution.py": 2,
    "src/provenance/integrity_audit.py": 1,
    "src/provenance/legacy_canonical_parity.py": 2,
    # The cutover receipt must count the sealed legacy source universe and
    # compare both legacy planes against v2 before ownership can transfer.
    "src/provenance/population_cutover.py": 2,
    "src/report/metrics_view.py": 1,
    "src/report/sections/financials.py": 7,
    "src/report/sections/thesis.py": 1,
    "src/synthesis/lenses/_shared.py": 1,
    "src/timeseries/loaders.py": 10,
    "src/triggers/kpi_inflection.py": 1,
    "src/user_state/kpi_catalog.py": 1,
    "src/viewspec/engine.py": 2,
}


def _matching_files(pattern: re.Pattern[str]) -> set[str]:
    return {
        path.relative_to(ROOT).as_posix()
        for production_root in PRODUCTION_ROOTS
        for path in production_root.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    }


def _dynamic_legacy_mutation_files() -> set[str]:
    result: set[str] = set()
    for production_root in PRODUCTION_ROOTS:
        for path in production_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if _DYNAMIC_MUTATION.search(text) and "financial_facts" in text and "kpi_facts" in text:
                result.add(path.relative_to(ROOT).as_posix())
    return result


def _legacy_read_count(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(
        len(_DIRECT_READ.findall(node.value))
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.search(r"\bSELECT\b", node.value, re.IGNORECASE)
    )


def test_no_new_direct_legacy_fact_mutation_surface() -> None:
    assert _matching_files(_DIRECT_MUTATION) <= _DIRECT_MUTATION_DEBT
    assert _dynamic_legacy_mutation_files() <= _DYNAMIC_MUTATION_DEBT


def test_no_new_legacy_restatement_helper_consumers() -> None:
    assert _matching_files(_LEGACY_RESTATEMENT_HELPERS) <= _RESTATEMENT_HELPER_DEBT


def test_no_new_legacy_fact_readers_before_canonical_cutover() -> None:
    """Freeze every legacy reader so the cutover debt can only shrink."""

    actual = Counter(
        path.relative_to(ROOT).as_posix()
        for production_root in PRODUCTION_ROOTS
        for path in production_root.rglob("*.py")
        for _ in range(_legacy_read_count(path))
    )
    assert dict(actual) == AUDITED_LEGACY_FACT_READS
