"""Build and validate the checked BHA-122 quality-roadmap freeze.

The freeze is deliberately an evidence index, not another quality policy.  It
binds the current receipts to deterministic cleanup arithmetic and records the
remaining feasibility hold.  The SCC cut set is derived from the same AST
import resolver used by the architecture receipt; no hand-maintained edge
list can silently drift.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import cast

from .roadmap_freeze_contract import (
    ARCHITECTURE_RECEIPT,
    BHA115_CLOSURE_CONDITION,
    BHA115_OWNER_AMENDMENT_PATH,
    BUILDER_CONVERT_QUOTAS,
    BUILDER_RETAIN_QUOTAS,
    DUPLICATE_RECEIPT,
    EXPECTED_SCC_CUTS,
    FROZEN_ESTIMATE_MATRIX,
    FROZEN_ESTIMATE_TOTALS,
    FROZEN_PERFORMANCE,
    FROZEN_PERFORMANCE_RECEIPT_SHA256,
    FROZEN_TYPE_DEBT_AUTHORITY_SHA256,
    FROZEN_TYPE_DEBT_TOTALS,
    FUNCTION_LIFECYCLE_RECEIPT,
    LIFECYCLE_RECEIPT,
    LOC_TARGET_CAPS,
    MANDATORY_LOC_ROOTS,
    PERFORMANCE_RECEIPT,
    PROGRAM_ISSUE_TRAIN,
    PROGRAM_OWNER,
    RECONCILIATION_RECEIPT,
    SCHEMA_VERSION,
    STATIC_RECEIPT,
    TEST_DB_RECEIPT,
    TYPE_DEBT_AUTHORITY_PATH,
    TYPE_DEBT_AUTHORITY_SHA256,
    BHA115Closure,
    BudgetMapping,
    BuilderDisposition,
    BuilderException,
    CleanupSlice,
    EstimateTotals,
    EvidenceRef,
    FunctionLifecycleSnapshot,
    LargeModule,
    LocCrossing,
    PerformanceSnapshot,
    ReachabilityEvidence,
    ReconciliationSnapshot,
    RoadmapFreeze,
    SccCut,
    StrictModel,
    TargetArithmetic,
    TrainPlan,
    TypeDebtCluster,
)
from .roadmap_freeze_contract import (
    train_plan as _train_plan,
)
from .roadmap_freeze_inventory import (
    architecture_edges as _architecture_edges,
)
from .roadmap_freeze_inventory import (
    budget_mappings as _budget_mappings,
)
from .roadmap_freeze_inventory import (
    builder_dispositions as _builder_dispositions,
)
from .roadmap_freeze_inventory import (
    builder_semantic_score as _builder_semantic_score,
)
from .roadmap_freeze_inventory import (
    duplicate_totals as _duplicate_totals,
)
from .roadmap_freeze_inventory import (
    feedback_arc_cut as _feedback_arc_cut,
)
from .roadmap_freeze_inventory import (
    function_lifecycle as _function_lifecycle,
)
from .roadmap_freeze_inventory import (
    order_score as _order_score,
)
from .roadmap_freeze_inventory import (
    reachability as _reachability,
)
from .roadmap_freeze_inventory import (
    read_json as _read_json,
)
from .roadmap_freeze_inventory import (
    reconciliation as _reconciliation,
)
from .roadmap_freeze_inventory import (
    sccs as _sccs,
)
from .roadmap_freeze_inventory import (
    selected_crossings as _selected_crossings,
)
from .roadmap_freeze_inventory import (
    sha256 as _sha256,
)
from .roadmap_freeze_inventory import (
    static_quality as _static_quality,
)
from .roadmap_freeze_inventory import (
    suppression_retirement as _suppression_retirement,
)
from .roadmap_freeze_inventory import (
    tracked_type_debt_authority as _tracked_type_debt_authority,
)
from .roadmap_freeze_inventory import (
    type_debt as _type_debt,
)
from .roadmap_freeze_inventory import (
    upgrade_builder_rows as _upgrade_builder_rows,
)
from .roadmap_freeze_inventory import (
    validate_performance_snapshot as _validate_performance_snapshot,
)
from .static_quality import scanner_input_hashes as _scanner_input_hashes

__all__ = (
    "ARCHITECTURE_RECEIPT",
    "BHA115_CLOSURE_CONDITION",
    "BHA115_OWNER_AMENDMENT_PATH",
    "BUILDER_CONVERT_QUOTAS",
    "BUILDER_RETAIN_QUOTAS",
    "DUPLICATE_RECEIPT",
    "EXPECTED_SCC_CUTS",
    "FROZEN_ESTIMATE_MATRIX",
    "FROZEN_ESTIMATE_TOTALS",
    "FROZEN_PERFORMANCE",
    "FROZEN_PERFORMANCE_RECEIPT_SHA256",
    "FROZEN_TYPE_DEBT_AUTHORITY_SHA256",
    "FROZEN_TYPE_DEBT_TOTALS",
    "FUNCTION_LIFECYCLE_RECEIPT",
    "LIFECYCLE_RECEIPT",
    "LOC_TARGET_CAPS",
    "MANDATORY_LOC_ROOTS",
    "PERFORMANCE_RECEIPT",
    "PROGRAM_ISSUE_TRAIN",
    "PROGRAM_OWNER",
    "RECONCILIATION_RECEIPT",
    "SCHEMA_VERSION",
    "STATIC_RECEIPT",
    "TEST_DB_RECEIPT",
    "TYPE_DEBT_AUTHORITY_PATH",
    "TYPE_DEBT_AUTHORITY_SHA256",
    "BHA115Closure",
    "BudgetMapping",
    "BuilderDisposition",
    "BuilderException",
    "CleanupSlice",
    "EstimateTotals",
    "EvidenceRef",
    "FunctionLifecycleSnapshot",
    "LargeModule",
    "LocCrossing",
    "PerformanceSnapshot",
    "ReachabilityEvidence",
    "ReconciliationSnapshot",
    "RoadmapFreeze",
    "SccCut",
    "StrictModel",
    "TargetArithmetic",
    "TrainPlan",
    "TypeDebtCluster",
)

# Keep historical private helper attributes available to downstream tests and
# tooling. Reading the aliases here makes that compatibility surface explicit
# to strict static analysis without widening the module's public `__all__`.
_LEGACY_PRIVATE_EXPORTS = (_builder_semantic_score, _order_score)

_WORKTREE_EVIDENCE_PATHS = frozenset(
    {
        ARCHITECTURE_RECEIPT,
        STATIC_RECEIPT,
        TEST_DB_RECEIPT,
        DUPLICATE_RECEIPT,
        LIFECYCLE_RECEIPT,
        FUNCTION_LIFECYCLE_RECEIPT,
        RECONCILIATION_RECEIPT,
        TYPE_DEBT_AUTHORITY_PATH,
    }
)
_HISTORICAL_COMMIT_EVIDENCE_PATHS = frozenset({PERFORMANCE_RECEIPT})


def evidence_ref(root: Path, path: str, receipt: dict[str, object]) -> EvidenceRef:
    """Describe receipt provenance without promoting worktree evidence to COMMIT."""
    revision = receipt.get("revision")
    scoped_revision = receipt.get("scoped_revision")
    scoped_commit = receipt.get("scoped_commit")
    commit_hash = receipt.get("commit_hash")
    source_commit = next(
        (
            value
            for value in (scoped_commit, commit_hash, revision)
            if isinstance(value, str) and value != "WORKTREE"
        ),
        "WORKTREE",
    )
    worktree_dirty = receipt.get("worktree_dirty") is True
    explicit_worktree = scoped_revision == "WORKTREE"
    if explicit_worktree or path in _WORKTREE_EVIDENCE_PATHS:
        commit_scoped = False
    elif path in _HISTORICAL_COMMIT_EVIDENCE_PATHS:
        commit_scoped = source_commit != "WORKTREE"
    else:
        commit_scoped = source_commit != "WORKTREE" and not worktree_dirty
    return EvidenceRef(
        path=path,
        sha256=_sha256(root / path),
        scoped_commit=source_commit,
        scope="COMMIT" if commit_scoped else "WORKTREE",
    )


def build_freeze(root: Path) -> RoadmapFreeze:
    root = root.resolve()
    architecture, edges = _architecture_edges(root)
    architecture_json = _read_json(root, ARCHITECTURE_RECEIPT)
    if architecture_json.get("source_sha256") != architecture.source_sha256:
        raise ValueError("architecture receipt source hash is stale; regenerate it first")
    components = [
        list(component) for component in architecture.metrics.strongly_connected_components
    ]
    cuts = _feedback_arc_cut(edges, components, architecture.source_sha256)
    module_paths = {module.module: module.path for module in architecture.metrics.modules}
    static = _read_json(root, STATIC_RECEIPT)
    test_db = _read_json(root, TEST_DB_RECEIPT)
    duplicate = _read_json(root, DUPLICATE_RECEIPT)
    lifecycle = _read_json(root, LIFECYCLE_RECEIPT)
    function_lifecycle = _function_lifecycle(root)
    performance = _read_json(root, PERFORMANCE_RECEIPT)
    debt_totals, clusters = _type_debt(root, static)
    suppression = _suppression_retirement(static)
    static_total, _static_components = _static_quality(static)
    reconciliation = _reconciliation(root)
    reachability = _reachability(root, lifecycle)
    receipts = {
        "architecture": ARCHITECTURE_RECEIPT,
        "static": STATIC_RECEIPT,
        "test_db": TEST_DB_RECEIPT,
        "duplicate": DUPLICATE_RECEIPT,
        "lifecycle": LIFECYCLE_RECEIPT,
        "function_lifecycle": FUNCTION_LIFECYCLE_RECEIPT,
        "reconciliation": RECONCILIATION_RECEIPT,
        "performance": PERFORMANCE_RECEIPT,
        "type_debt_authority": TYPE_DEBT_AUTHORITY_PATH,
    }
    evidence: dict[str, EvidenceRef] = {}
    for key, path in receipts.items():
        receipt = _read_json(root, path)
        evidence[key] = evidence_ref(root, path, receipt)
    metrics = architecture.metrics
    upgrade_builders = _upgrade_builder_rows(test_db)
    builders = len(upgrade_builders)
    wall_raw = performance.get("process_wall_seconds")
    if not isinstance(wall_raw, (int, float)):
        raise ValueError("performance receipt lacks numeric process_wall_seconds")
    wall = float(wall_raw)
    duplicate_groups, duplicate_functions, duplicate_loc = _duplicate_totals(duplicate)
    lifecycle_counts = lifecycle.get("counts")
    if not isinstance(lifecycle_counts, dict) or not isinstance(
        lifecycle_counts.get("dormant-until"), int
    ):
        raise ValueError("lifecycle receipt lacks dormant count")
    dormant_count = cast(int, lifecycle_counts["dormant-until"])
    perf_source = performance.get("source_sha256")
    perf_cohort = performance.get("cohort_sha256")
    perf_revision = performance.get("revision")
    perf_schema = performance.get("schema_version")
    perf_status = performance.get("evidence_status")
    perf_network = performance.get("network_isolation")
    if not all(
        isinstance(value, str)
        for value in (
            perf_source,
            perf_cohort,
            perf_revision,
            perf_schema,
            perf_status,
            perf_network,
        )
    ):
        raise ValueError("performance receipt lacks frozen provenance fields")
    perf_schema = cast(str, perf_schema)
    perf_revision = cast(str, perf_revision)
    perf_source = cast(str, perf_source)
    perf_cohort = cast(str, perf_cohort)
    perf_status = cast(str, perf_status)
    perf_network = cast(str, perf_network)
    paired = performance.get("paired")
    if not isinstance(paired, bool):
        raise ValueError("performance receipt lacks boolean paired status")
    arithmetic = TargetArithmetic(
        loc_baseline_over_1000=metrics.modules_over_1000_loc,
        loc_target_over_1000=35,
        loc_required_reduction=max(0, metrics.modules_over_1000_loc - 35),
        mandatory_loc_roots=metrics.composition_root_loc,
        scc_baseline=metrics.scc_count,
        scc_target=3,
        scc_cut_edges=len(cuts),
        duplicate_groups_baseline=duplicate_groups,
        duplicate_groups_target=1,
        duplicate_groups_to_remove=duplicate_groups - 1,
        duplicate_functions_baseline=duplicate_functions,
        duplicate_functions_target=3,
        duplicate_functions_to_remove=duplicate_functions - 3,
        duplicate_loc_baseline=duplicate_loc,
        duplicate_loc_target=67,
        duplicate_loc_to_remove=duplicate_loc - 67,
        migration_builders_baseline=builders,
        migration_builders_target=60,
        migration_builders_to_convert=max(0, builders - 60),
        static_quality_baseline=static_total,
        full_suite_wall_seconds=wall,
        full_suite_target_seconds=510.0,
        full_suite_gap_seconds=round(wall - 510.0, 3),
    )
    slices = (
        CleanupSlice(
            key="migration-test-ci",
            train="Train 1",
            issue="BHA-104",
            owner=PROGRAM_OWNER,
            parallel_group="test-performance",
            scope="112 actual command.upgrade test builders plus full-suite CI pairing",
            ownership_lane="test-harness",
            risk="high",
            units=arithmetic.migration_builders_to_convert,
            estimated_hours=round(arithmetic.migration_builders_to_convert * 0.5, 2),
            estimated_prs=22,
            estimated_calendar_weeks=7.0,
            acceptance="actual upgrade-builder cohort is <=60 and paired CI evidence meets the benchmark contract",
        ),
        CleanupSlice(
            key="active-type-debt",
            train="Train 2",
            issue="BHA-105",
            owner=PROGRAM_OWNER,
            parallel_group="typed-contracts",
            scope="non-archived strict-Pyright clusters",
            ownership_lane="typed-contracts",
            risk="high",
            units=debt_totals["total"],
            # The evidence cohort can be corrected independently of the
            # owner-approved roadmap estimate.  Keep the estimate frozen until
            # the program is explicitly amended and rejudged.
            estimated_hours=FROZEN_ESTIMATE_MATRIX["active-type-debt"][0],
            estimated_prs=20,
            estimated_calendar_weeks=8.0,
            acceptance="active diagnostics decline without adding ignores or weakening strictness",
        ),
        CleanupSlice(
            key="integrity-audit",
            train="Train 3",
            issue="BHA-106",
            owner=PROGRAM_OWNER,
            parallel_group="integrity-boundaries",
            scope="integrity-audit responsibilities and typed evidence paths",
            ownership_lane="integrity-audit",
            risk="critical",
            units=1,
            estimated_hours=120.0,
            estimated_prs=9,
            estimated_calendar_weeks=5.0,
            acceptance="integrity audit is split into typed, independently testable responsibilities",
        ),
        CleanupSlice(
            key="duplicate-authorities",
            train="Train 4",
            issue="BHA-107",
            owner=PROGRAM_OWNER,
            parallel_group="maintainability",
            scope="exact clone groups and duplicate authority consolidation",
            ownership_lane="shared-primitives",
            risk="medium",
            units=duplicate_groups,
            estimated_hours=80.0,
            estimated_prs=11,
            estimated_calendar_weeks=6.0,
            acceptance="duplicate receipt is <=1 group / <=3 functions / <=67 LOC",
        ),
        CleanupSlice(
            key="architecture-boundaries",
            train="Train 5",
            issue="BHA-108",
            owner=PROGRAM_OWNER,
            parallel_group="architecture-boundaries",
            scope="request pooling, composition roots, module shape, and 31-edge SCC cut",
            ownership_lane="architecture-boundaries",
            risk="critical",
            units=len(cuts),
            estimated_hours=180.0,
            estimated_prs=68,
            estimated_calendar_weeks=16.0,
            acceptance="request paths use one connection, composition roots meet caps, and SCC target is met",
        ),
        CleanupSlice(
            key="lifecycle-pruning",
            train="Train 6",
            issue="BHA-109",
            owner=PROGRAM_OWNER,
            parallel_group="cleanup",
            scope="dormant/dead operational candidates after reachability review",
            ownership_lane="lifecycle-pruning",
            risk="high",
            units=dormant_count,
            estimated_hours=120.0,
            estimated_prs=12,
            estimated_calendar_weeks=6.0,
            acceptance="only dispositioned, unreachable code is removed and lifecycle receipt remains complete",
            candidate_count=function_lifecycle.candidate_count,
        ),
        CleanupSlice(
            key="final-static-zero",
            train="Train 7",
            issue="BHA-110",
            owner=PROGRAM_OWNER,
            parallel_group="static-quality",
            scope="active Ruff, format, Pyright, and suppression retirement",
            ownership_lane="static-quality",
            risk="high",
            units=static_total,
            estimated_hours=80.0,
            estimated_prs=12,
            estimated_calendar_weeks=6.0,
            acceptance="active static diagnostics and unauthorized suppressions reach zero",
        ),
        CleanupSlice(
            key="quality-closure",
            train="Train 8",
            issue="BHA-111",
            owner=PROGRAM_OWNER,
            parallel_group="closure",
            scope="reconstruction, reconciliation, owner acceptance, and final score receipt",
            ownership_lane="quality-closure",
            risk="medium",
            units=1,
            estimated_hours=60.0,
            estimated_prs=2,
            estimated_calendar_weeks=3.0,
            acceptance="all hard gates and reconstruction checks pass with owner-accepted receipts",
        ),
    )
    total_hours = round(sum(slice_.estimated_hours for slice_ in slices), 2)
    total_prs = sum(slice_.estimated_prs for slice_ in slices)
    train_max: dict[str, float] = defaultdict(float)
    for slice_ in slices:
        train_max[slice_.train] = max(train_max[slice_.train], slice_.estimated_calendar_weeks)
    critical_path_weeks = round(sum(train_max.values()), 2)
    cluster_hash = hashlib.sha256(
        json.dumps(
            [cluster.model_dump() for cluster in clusters], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    large_modules = tuple(
        LargeModule(path=module.path, noncomment_loc=module.lines.noncomment)
        for module in sorted(
            (module for module in metrics.modules if module.lines.noncomment > 1000),
            key=lambda module: (module.lines.noncomment, module.path),
        )
    )
    mandatory_paths = tuple(sorted(metrics.composition_root_loc))
    selected_crossings = _selected_crossings(large_modules, mandatory_paths)
    builder_dispositions = _builder_dispositions(upgrade_builders)
    builder_exceptions = tuple(
        BuilderException(
            path=row.path,
            taxonomy=row.taxonomy,
            selection_rank=row.selection_rank,
            evidence=row.evidence,
            rationale=row.rationale,
        )
        for row in builder_dispositions
        if row.disposition == "retain_candidate"
    )
    budget_mappings = _budget_mappings(
        cuts,
        selected_crossings,
        clusters,
        builder_dispositions,
        suppression,
        function_lifecycle,
        (static_total, _static_components),
    )
    return RoadmapFreeze(
        status="HOLD",
        artifact_acceptance_status="PASS",
        program_feasibility_status="HOLD",
        evidence=evidence,
        function_lifecycle=function_lifecycle,
        reconciliation=reconciliation,
        reachability=reachability,
        architecture_metrics={
            "executable_modules": metrics.executable_modules,
            "total_noncomment_loc": metrics.total_noncomment_loc,
            "modules_over_1000_loc": metrics.modules_over_1000_loc,
            "modules_over_2000_loc": metrics.modules_over_2000_loc,
            "modules_at_least_3000_loc": metrics.modules_at_least_3000_loc,
            "max_internal_fan_out": metrics.max_internal_fan_out,
            "scc_count": metrics.scc_count,
            "scc_module_count": metrics.scc_module_count,
            "largest_scc": metrics.largest_scc,
        },
        scc_cut_edges=tuple(
            SccCut(
                source=source,
                target=target,
                source_path=module_paths[source],
                target_path=module_paths[target],
            )
            for source, target in cuts
        ),
        performance_snapshot=PerformanceSnapshot(
            schema_version=perf_schema,
            revision=perf_revision,
            source_sha256=perf_source,
            cohort_sha256=perf_cohort,
            process_wall_seconds=wall,
            paired=paired,
            evidence_status=perf_status,
            network_isolation=perf_network,
        ),
        large_modules=large_modules,
        selected_loc_crossings=selected_crossings,
        migration_builder_dispositions=builder_dispositions,
        builder_exception_inventory=builder_exceptions,
        retained_type_debt=debt_totals,
        type_debt_clusters=tuple(clusters),
        type_debt_clusters_sha256=cluster_hash,
        suppression_retirement=suppression,
        issue_train_matrix=PROGRAM_ISSUE_TRAIN,
        train_plan=_train_plan(),
        cleanup_slices=slices,
        budget_mappings=budget_mappings,
        estimate_totals=EstimateTotals(
            total_estimated_hours=total_hours,
            total_estimated_prs=total_prs,
            critical_path_calendar_weeks=critical_path_weeks,
        ),
        target_arithmetic=arithmetic,
        bha115_closure=BHA115Closure(
            status="OPEN",
            condition=BHA115_CLOSURE_CONDITION,
            owner_amendment_path=BHA115_OWNER_AMENDMENT_PATH,
            rejudge_required=True,
        ),
        hold_reasons=(
            "full-suite evidence is a single unpaired run; >=7 paired cold/warm runs and network isolation are not yet proven",
            "type-debt burn-down and composition-root split feasibility lack ownership-confirmed execution receipts",
        ),
    )


def validate_freeze(root: Path, path: Path) -> RoadmapFreeze:
    freeze = RoadmapFreeze.model_validate_json(path.read_text(encoding="utf-8"))
    expected_evidence = {
        "architecture",
        "static",
        "test_db",
        "duplicate",
        "lifecycle",
        "function_lifecycle",
        "reconciliation",
        "performance",
        "type_debt_authority",
    }
    if set(freeze.evidence) != expected_evidence:
        raise ValueError("freeze evidence keys are incomplete or unexpected")
    for key in (
        "architecture",
        "static",
        "test_db",
        "duplicate",
        "lifecycle",
        "function_lifecycle",
        "reconciliation",
        "type_debt_authority",
    ):
        evidence = freeze.evidence[key]
        expected_path = {
            "architecture": ARCHITECTURE_RECEIPT,
            "static": STATIC_RECEIPT,
            "test_db": TEST_DB_RECEIPT,
            "duplicate": DUPLICATE_RECEIPT,
            "lifecycle": LIFECYCLE_RECEIPT,
            "function_lifecycle": FUNCTION_LIFECYCLE_RECEIPT,
            "reconciliation": RECONCILIATION_RECEIPT,
            "type_debt_authority": TYPE_DEBT_AUTHORITY_PATH,
        }[key]
        if evidence.path != expected_path:
            raise ValueError(f"checked evidence path drifted: {key}")
        evidence_path = root / evidence.path
        if not evidence_path.is_file() or _sha256(evidence_path) != evidence.sha256:
            raise ValueError(f"checked evidence is missing or changed: {evidence.path}")
        receipt = _read_json(root, evidence.path)
        if evidence != evidence_ref(root, evidence.path, receipt):
            raise ValueError(f"checked evidence provenance drifted: {evidence.path}")
    _validate_performance_snapshot(root, freeze)

    lifecycle = _read_json(root, LIFECYCLE_RECEIPT)
    expected_function_lifecycle = _function_lifecycle(root)
    if freeze.function_lifecycle != expected_function_lifecycle:
        raise ValueError("function lifecycle evidence drifted from the current source tree")
    expected_reconciliation = _reconciliation(root)
    if freeze.reconciliation != expected_reconciliation:
        raise ValueError("reconciliation evidence drifted from the current receipt")
    expected_reachability = _reachability(root, lifecycle)
    if freeze.reachability != expected_reachability:
        raise ValueError("reachability evidence drifted from the current graph or lifecycle")

    architecture, edges = _architecture_edges(root)
    cut = {(edge.source, edge.target) for edge in freeze.scc_cut_edges}
    derived_cut = {
        (source, target)
        for source, target in _feedback_arc_cut(
            edges,
            [list(component) for component in architecture.metrics.strongly_connected_components],
            architecture.source_sha256,
        )
    }
    if cut != derived_cut:
        raise ValueError("checked SCC cut list does not match deterministic derivation")
    remaining = [edge for edge in edges if edge not in cut]
    if _sccs(remaining, {source for source, _ in edges} | {target for _, target in edges}):
        raise ValueError("SCC cut list does not make architecture graph acyclic")
    if architecture.metrics.scc_count != freeze.architecture_metrics["scc_count"]:
        raise ValueError("architecture metrics drifted from freeze")
    expected_metrics = {
        "executable_modules": architecture.metrics.executable_modules,
        "total_noncomment_loc": architecture.metrics.total_noncomment_loc,
        "modules_over_1000_loc": architecture.metrics.modules_over_1000_loc,
        "modules_over_2000_loc": architecture.metrics.modules_over_2000_loc,
        "modules_at_least_3000_loc": architecture.metrics.modules_at_least_3000_loc,
        "max_internal_fan_out": architecture.metrics.max_internal_fan_out,
        "scc_count": architecture.metrics.scc_count,
        "scc_module_count": architecture.metrics.scc_module_count,
        "largest_scc": architecture.metrics.largest_scc,
    }
    if freeze.architecture_metrics != expected_metrics:
        raise ValueError("architecture metrics drifted from current receipt")
    module_paths = {module.module: module.path for module in architecture.metrics.modules}
    for edge in freeze.scc_cut_edges:
        if (
            module_paths.get(edge.source) != edge.source_path
            or module_paths.get(edge.target) != edge.target_path
        ):
            raise ValueError("SCC cut path attribution drifted from architecture receipt")

    static = _read_json(root, STATIC_RECEIPT)
    source_hash, config_hash = _scanner_input_hashes(root)
    if static.get("source_hash") != source_hash:
        raise ValueError("static receipt source hash is stale; regenerate it first")
    if static.get("config_hash") != config_hash:
        raise ValueError("static receipt config hash is stale; regenerate it first")
    expected_static_total, expected_static_components = _static_quality(static)
    if freeze.target_arithmetic.static_quality_baseline != expected_static_total:
        raise ValueError("static-quality baseline arithmetic drifted from static receipt")
    expected_suppression = _suppression_retirement(static)
    if freeze.suppression_retirement != expected_suppression:
        raise ValueError("suppression-retirement baseline drifted from static evidence")
    diagnostics = static.get("diagnostics", [])
    pyright_rows = (
        [
            cast(dict[str, object], item)
            for item in cast(list[object], diagnostics)
            if isinstance(item, dict)
        ]
        if isinstance(diagnostics, list)
        else []
    )
    pyright = next((row for row in pyright_rows if row.get("tool") == "pyright"), None)
    count_raw = pyright.get("count") if pyright is not None else None
    if pyright is None or not isinstance(count_raw, int):
        raise ValueError("static receipt lacks pyright count")
    by_directory = pyright.get("diagnostics_by_directory", {})
    archived = (
        sum(
            value
            for key, value in cast(dict[str, object], by_directory).items()
            if key.startswith("alembic/versions_archived") and isinstance(value, int)
        )
        if isinstance(by_directory, dict)
        else 0
    )
    if freeze.retained_type_debt["total"] != count_raw - archived:
        raise ValueError("retained type-debt total drifted from static receipt")
    # Cluster membership is derived from raw diagnostic rows when present, or
    # from the separate tracked authority when the ignored raw receipt is
    # absent.  The candidate's self-hash is never used as the authority.
    current_debt, current_clusters = _type_debt(root, static)
    authority_debt, authority_clusters = _tracked_type_debt_authority(root)
    if current_debt != authority_debt or tuple(current_clusters) != tuple(authority_clusters):
        raise ValueError("static diagnostic evidence drifted from tracked type-debt authority")
    if (
        current_debt != freeze.retained_type_debt
        or tuple(current_clusters) != freeze.type_debt_clusters
    ):
        raise ValueError("type-debt cluster inventory drifted from static evidence")

    test_db = _read_json(root, TEST_DB_RECEIPT)
    raw_builders = test_db.get("database_builders", [])
    builder_rows = cast(list[object], raw_builders) if isinstance(raw_builders, list) else []
    upgrade_builders = sum(
        1
        for row in builder_rows
        if isinstance(row, dict)
        and isinstance(row.get("evidence"), list)
        and "call:command.upgrade" in cast(list[object], row["evidence"])
    )
    if freeze.target_arithmetic.migration_builders_baseline != upgrade_builders:
        raise ValueError("migration-builder baseline is not the actual upgrade cohort")
    duplicate = _read_json(root, DUPLICATE_RECEIPT)
    duplicate_totals = _duplicate_totals(duplicate)
    arithmetic = freeze.target_arithmetic
    if (
        arithmetic.duplicate_groups_baseline,
        arithmetic.duplicate_functions_baseline,
        arithmetic.duplicate_loc_baseline,
    ) != duplicate_totals:
        raise ValueError("duplicate baseline arithmetic drifted from duplicate receipt")
    if arithmetic.loc_baseline_over_1000 != architecture.metrics.modules_over_1000_loc:
        raise ValueError("LOC baseline arithmetic drifted from architecture receipt")
    expected_large = tuple(
        LargeModule(path=module.path, noncomment_loc=module.lines.noncomment)
        for module in sorted(
            (module for module in architecture.metrics.modules if module.lines.noncomment > 1000),
            key=lambda module: (module.lines.noncomment, module.path),
        )
    )
    if freeze.large_modules != expected_large:
        raise ValueError("large-module inventory drifted from architecture receipt")
    mandatory_paths = tuple(sorted(architecture.metrics.composition_root_loc))
    expected_crossings = _selected_crossings(expected_large, mandatory_paths)
    if freeze.selected_loc_crossings != expected_crossings:
        raise ValueError("selected LOC crossings drifted from deterministic inventory")
    expected_builders = _upgrade_builder_rows(test_db)
    expected_dispositions = _builder_dispositions(expected_builders)
    if freeze.migration_builder_dispositions != expected_dispositions:
        raise ValueError("migration builder cohort, taxonomy, disposition, or owner drifted")
    expected_mappings = _budget_mappings(
        [(edge.source, edge.target) for edge in freeze.scc_cut_edges],
        expected_crossings,
        current_clusters,
        expected_dispositions,
        expected_suppression,
        expected_function_lifecycle,
        (expected_static_total, expected_static_components),
    )
    if freeze.budget_mappings != expected_mappings:
        raise ValueError("budget mappings do not match the measured roadmap items")
    lifecycle_counts = lifecycle.get("counts")
    typed_lifecycle_counts = (
        cast(dict[str, object], lifecycle_counts) if isinstance(lifecycle_counts, dict) else {}
    )
    dormant_raw: object = typed_lifecycle_counts.get("dormant-until")
    dormant = dormant_raw if isinstance(dormant_raw, int) else None
    dormant_slice = next(slice_ for slice_ in freeze.cleanup_slices if slice_.issue == "BHA-109")
    if not isinstance(dormant, int) or dormant_slice.units != dormant:
        raise ValueError("pruning slice is not bound to the current lifecycle receipt")
    if dormant_slice.candidate_count != expected_function_lifecycle.candidate_count:
        raise ValueError("pruning slice candidate count is not bound to function lifecycle receipt")
    return freeze
