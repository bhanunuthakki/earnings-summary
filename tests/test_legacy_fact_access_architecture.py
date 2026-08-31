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
from dataclasses import dataclass
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

# Pre-v2 mutation debt. New general production code must use SourceFactRepository.
_DIRECT_MUTATION_DEBT = frozenset(
    {
        "execution/db_gc.py",
        "src/ir_pipeline/ingest.py",
        "src/pipeline/kpi_persistence.py",
        "src/pipeline/restatement_detector.py",
    }
)


@dataclass(frozen=True)
class _GovernedRepairMutationSurface:
    function_name: str
    mutation_count: int
    retirement_criterion: str


# One exact, append-only correction seam remains at the legacy boundary until
# canonical source facts own KPI persistence. It is not a general extension
# point: the test below freezes its function, write count, and retirement gate.
_GOVERNED_REPAIR_MUTATION_SURFACES = {
    "src/pipeline/kpi_source_review.py": _GovernedRepairMutationSurface(
        function_name="insert_source_reviewed_kpi_supersession",
        mutation_count=1,
        retirement_criterion=(
            "Retire after source-reviewed KPI corrections write directly to the canonical "
            "source-fact plane and the legacy kpi_facts projection is trigger-owned."
        ),
    )
}
_DYNAMIC_MUTATION_DEBT = frozenset({"src/pipeline/confidence.py"})
_RESTATEMENT_HELPER_DEBT = frozenset(
    {
        "src/compute/_common.py",
        "src/compute/fmp_derived_kpis.py",
        "src/compute/metrics_engine/io.py",
        "src/pipeline/kpi_persistence.py",
        "src/pipeline/restatement_detector.py",
        "src/pipeline/sec_fpi_ingest.py",
        "src/pipeline/sec_xbrl.py",
    }
)
AUDITED_LEGACY_FACT_READS = {
    "execution/backfill_fiscal_period_stamps.py": 1,
    "execution/backfill_ir_deck_locators.py": 2,
    # The source-reviewed repair executor deliberately checks the old row,
    # supersession head, exact inserted successor, replay head, crash-recovery
    # postcondition, and quarantined-old invariant at the legacy boundary. These are repair guards,
    # not an analytical/product reader, and remain frozen here until cutover.
    "execution/apply_kpi_semantic_refresh.py": 7,
    "execution/daily_fetch_and_brief.py": 2,
    # The eighth literal is an EXPLAIN QUERY PLAN safety probe that proves the
    # supersedes self-FK lookup uses the migration-owned index before deletion;
    # it does not fetch domain rows.
    # The ninth read is the surviving-fact NOT EXISTS guard on the
    # attempts-grid cascade (2026-08-03 adversarial review, #7): a period
    # keeping ANY fact keeps its grid row.
    "execution/db_gc.py": 9,
    "execution/fix_kpi_series.py": 1,
    "execution/fmp_backpop.py": 1,
    "execution/grade_predictions.py": 1,
    "execution/mark_kpi_cadence.py": 2,
    "execution/onboard_pending_tickers.py": 2,
    "execution/pressure_test_thesis.py": 1,
    "execution/prune_misscaled_capture_facts.py": 1,
    "execution/retype_misfiled_sec_ir_docs.py": 2,
    "src/allocation/eligibility.py": 1,
    "src/ask/grounding.py": 3,
    "src/bear_case_grader.py": 1,
    "src/cockpit_fundamentals.py": 2,
    "src/competitive/holdings_sync.py": 1,
    "src/compute/fmp_derived_kpis.py": 1,
    # Shared fact-aware unit resolution moved this existing read out of the
    # seeding CLI so Ask approval and seeding use one deterministic authority.
    "src/compute/kpi_definition_units.py": 1,
    "src/compute/kpi_extract_summaries.py": 3,
    "src/compute/metrics_engine/io.py": 8,
    "src/compute/say_do.py": 1,
    "src/compute/segment_q4_derive.py": 1,
    "src/compute/segment_quarterly_10q.py": 1,
    "src/compute/segments.py": 1,
    "src/compute/soft_rule_evaluator.py": 1,
    "src/credibility/observations.py": 3,
    "src/dcf/fact_drivers.py": 1,
    "src/decision_conditions.py": 4,
    "src/pipeline/confidence.py": 2,
    "src/pipeline/key_metrics.py": 1,
    "src/pipeline/kpi_persistence.py": 4,
    # Exact predecessor/head reads guard the one governed append-only repair
    # write. They are not product analytics and retire with that write seam.
    "src/pipeline/kpi_source_review.py": 2,
    "src/pipeline/peeks.py": 3,
    "src/pipeline/reader_tier_audit.py": 1,
    "src/pipeline/restatement_detector.py": 8,
    "src/pipeline/issuer_document_coverage.py": 3,
    "src/pipeline/restatements_panel.py": 4,
    "src/pipeline/validation_engine.py": 5,
    # Canonical cutover resolution must inspect both exact legacy candidates
    # and their available-at-cutoff supersession links before it can publish
    # one immutable selected observation. These are resolver-boundary reads,
    # not product-consumer fallbacks.
    "src/provenance/financial_fact_resolution.py": 4,
    "src/provenance/integrity_audit.py": 1,
    "src/provenance/legacy_canonical_parity.py": 2,
    "src/report/metrics_view.py": 1,
    "src/report/sections/financials.py": 2,
    "src/synthesis/lenses/_shared.py": 1,
    # Admitted semantic persistence validates the exact legacy projection row's
    # stored unit before appending context. This is a write-boundary guard, not
    # an analytical reader, and retires with the legacy KPI projection.
    "src/pipeline/kpi_semantics.py": 1,
    "src/timeseries/loaders.py": 5,
    "src/triggers/kpi_inflection.py": 1,
    "src/user_state/kpi_catalog.py": 1,
    "src/viewspec/engine.py": 1,
}


@dataclass(frozen=True)
class _TransitionalReadExemption:
    function_name: str
    read_count: int
    retirement_criterion: str
    sql_constant_name: str | None = None


# This is migration repair, not a product read path: it enumerates only the
# exact legacy rows owned by one already-governed FMP document so missing
# immutable observations can be captured atomically. Keep it outside the
# frozen reader-debt count, but make both its scope and deletion gate executable.
_TRANSITIONAL_READ_EXEMPTIONS = {
    "src/pipeline/kpi_semantic_review.py": (
        _TransitionalReadExemption(
            function_name="build_quarantined_kpi_correction_review",
            read_count=1,
            retirement_criterion=(
                "Retire after observation-less legacy KPI predecessors are either "
                "source-reviewed into the canonical source-fact plane or dispositioned "
                "without a raw kpi_facts repair seam."
            ),
        ),
    ),
    "src/pipeline/issuer_fact_manifest.py": (
        _TransitionalReadExemption(
            function_name="_assert_kpi_replays_compatible",
            read_count=1,
            retirement_criterion=(
                "Retire after issuer fact manifest KPI capture publishes exact value, unit, "
                "currency, locator, and excerpt provenance to the canonical source-fact plane "
                "and receipt validation reads that projection without kpi_facts fallback."
            ),
            sql_constant_name="_ISSUER_MANIFEST_KPI_REPLAY_SQL",
        ),
    ),
    "src/provenance/financial_fact_resolution.py": (
        _TransitionalReadExemption(
            function_name="rehydrate_document_fact_observations",
            read_count=1,
            retirement_criterion=(
                "Retire after the governed FMP corpus backfill proves zero legacy "
                "financial_facts rows without fact_observation_revisions."
            ),
            sql_constant_name="DOCUMENT_FACT_REHYDRATION_SQL",
        ),
    ),
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


def _read_count(node: ast.AST) -> int:
    return sum(
        len(_DIRECT_READ.findall(child.value))
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and re.search(r"\bSELECT\b", child.value, re.IGNORECASE)
    )


def _named_assignment_value(node: ast.stmt, name: str) -> ast.AST | None:
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == name
    ):
        return node.value
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == name for target in node.targets
    ):
        return node.value
    return None


def _legacy_read_count(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relative = path.relative_to(ROOT).as_posix()
    count = _read_count(tree)
    for exemption in _TRANSITIONAL_READ_EXEMPTIONS.get(relative, ()):
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == exemption.function_name
        ]
        assert len(functions) == 1, f"transitional reader moved or disappeared: {relative}"
        if exemption.sql_constant_name is None:
            actual = _read_count(functions[0])
        else:
            constant_name = exemption.sql_constant_name
            loads = sum(
                1
                for node in ast.walk(functions[0])
                if isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id == constant_name
            )
            assert loads == 1, (
                f"transitional reader must load {constant_name} exactly once: "
                f"{relative}:{exemption.function_name}"
            )
            assert _read_count(functions[0]) == 0, (
                f"transitional reader duplicated its named SQL constant: "
                f"{relative}:{exemption.function_name}"
            )
            constants: list[ast.AST] = []
            for node in tree.body:
                constant_value = _named_assignment_value(node, constant_name)
                if constant_value is not None:
                    constants.append(constant_value)
            assert len(constants) == 1, (
                f"transitional reader SQL constant moved or duplicated: {relative}:{constant_name}"
            )
            actual = _read_count(constants[0])
        assert actual == exemption.read_count, (
            f"transitional reader scope changed: {relative}:{exemption.function_name}"
        )
        count -= actual
    return count


def test_no_new_direct_legacy_fact_mutation_surface() -> None:
    governed = frozenset(_GOVERNED_REPAIR_MUTATION_SURFACES)
    assert _matching_files(_DIRECT_MUTATION) <= _DIRECT_MUTATION_DEBT | governed
    assert _dynamic_legacy_mutation_files() <= _DYNAMIC_MUTATION_DEBT


def test_governed_repair_mutation_surface_is_narrow_and_retirable() -> None:
    assert _GOVERNED_REPAIR_MUTATION_SURFACES
    for relative, surface in _GOVERNED_REPAIR_MUTATION_SURFACES.items():
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == surface.function_name
        ]
        assert len(functions) == 1, f"governed repair writer moved: {relative}"
        mutation_count = sum(
            len(_DIRECT_MUTATION.findall(child.value))
            for child in ast.walk(functions[0])
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        )
        assert mutation_count == surface.mutation_count
        assert surface.retirement_criterion.startswith("Retire after ")


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


def test_legacy_fact_reader_inventory_names_live_files() -> None:
    missing = [path for path in AUDITED_LEGACY_FACT_READS if not (ROOT / path).is_file()]
    assert missing == []


def test_transitional_legacy_reader_exemptions_are_narrow_and_retirable() -> None:
    assert _TRANSITIONAL_READ_EXEMPTIONS
    for relative, exemptions in _TRANSITIONAL_READ_EXEMPTIONS.items():
        assert (ROOT / relative).is_file()
        assert len(exemptions) == 1
        exemption = exemptions[0]
        assert exemption.read_count == 1
        assert exemption.retirement_criterion.startswith("Retire after ")
        # _legacy_read_count also proves the exact named top-level function
        # still owns precisely the approved read count.
        _legacy_read_count(ROOT / relative)
