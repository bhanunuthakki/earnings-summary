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
import math
import random
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .architecture import (
    ArchitectureReceipt,
    build_architecture_receipt,
    resolved_import_edges,
)

SCHEMA_VERSION = "bha-122.v1"
ARCHITECTURE_RECEIPT = "docs/quality/architecture-ratchet.json"
STATIC_RECEIPT = "docs/quality/static-baseline.json"
TEST_DB_RECEIPT = "docs/quality/test-db-patterns-baseline.json"
DUPLICATE_RECEIPT = "docs/quality/duplicates-ratchet.json"
LIFECYCLE_RECEIPT = "docs/quality/lifecycle-baseline.json"
PERFORMANCE_RECEIPT = ".tmp/quality/test-ci-performance/full-suite-8c7dc0c3/receipt.json"
EXPECTED_SCC_CUTS = 31
MANDATORY_LOC_ROOTS = (
    "execution/comments_server.py",
    "src/pipeline/portfolio_panel.py",
)
BUILDER_RETAIN_QUOTAS = {
    "direct-downgrade": 58,
    "archived-graph": 1,
    "custom-bootstrap": 1,
}
BUILDER_CONVERT_QUOTAS = {
    "seeded-upgrade": 57,
    "direct-historical": 24,
    "archived-graph": 7,
    "direct-downgrade": 24,
}
PROGRAM_ISSUE_TRAIN = {
    "BHA-104": "Train 1",
    "BHA-105": "Train 2",
    "BHA-106": "Train 3",
    "BHA-107": "Train 4",
    "BHA-108": "Train 5",
    "BHA-109": "Train 6",
    "BHA-110": "Train 7",
    "BHA-111": "Train 8",
}
PROGRAM_OWNER = "Bhanu Nuthakki"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRef(StrictModel):
    path: str
    sha256: str
    scoped_commit: str
    scope: Literal["WORKTREE", "COMMIT"]


class SccCut(StrictModel):
    source: str
    target: str
    source_path: str
    target_path: str
    rationale: Literal["feedback_arc_set"] = "feedback_arc_set"


class TypeDebtCluster(StrictModel):
    source_zone: str
    rule: str
    count: int = Field(ge=0)
    ownership_lane: str
    risk: Literal["critical", "high", "medium", "low"]
    estimated_hours: float = Field(ge=0)
    suggested_slice_count: int = Field(ge=0)
    file_count: int = Field(ge=0)
    files_sha256: str
    evidence_sha256: str


class LargeModule(StrictModel):
    path: str
    noncomment_loc: int = Field(gt=1000)


class BuilderDisposition(StrictModel):
    path: str
    taxonomy: str
    disposition: Literal["retain_candidate", "convert_candidate"]
    owner: str
    selection_basis: str


class CleanupSlice(StrictModel):
    key: str
    train: str
    issue: str
    owner: str
    parallel_group: str
    scope: str
    ownership_lane: str
    risk: Literal["critical", "high", "medium", "low"]
    units: int = Field(ge=0)
    estimated_hours: float = Field(ge=0)
    estimated_prs: int = Field(ge=0)
    estimated_calendar_weeks: float = Field(ge=0)
    acceptance: str


class EstimateTotals(StrictModel):
    total_estimated_hours: float = Field(ge=0)
    total_estimated_prs: int = Field(ge=0)
    critical_path_calendar_weeks: float = Field(ge=0)


class PerformanceSnapshot(StrictModel):
    schema_version: str
    revision: str
    source_sha256: str
    cohort_sha256: str
    process_wall_seconds: float = Field(ge=0)
    paired: bool
    evidence_status: str
    network_isolation: str


class TargetArithmetic(StrictModel):
    loc_baseline_over_1000: int
    loc_target_over_1000: int
    loc_required_reduction: int
    mandatory_loc_roots: dict[str, int]
    scc_baseline: int
    scc_target: int
    scc_cut_edges: int
    duplicate_groups_baseline: int
    duplicate_groups_target: int
    duplicate_groups_to_remove: int
    duplicate_functions_baseline: int
    duplicate_functions_target: int
    duplicate_functions_to_remove: int
    duplicate_loc_baseline: int
    duplicate_loc_target: int
    duplicate_loc_to_remove: int
    migration_builders_baseline: int
    migration_builders_target: int
    migration_builders_to_convert: int
    full_suite_wall_seconds: float
    full_suite_target_seconds: float
    full_suite_gap_seconds: float

    @model_validator(mode="after")
    def arithmetic_matches(self) -> TargetArithmetic:
        checks = {
            "LOC reduction": self.loc_required_reduction
            == self.loc_baseline_over_1000 - self.loc_target_over_1000,
            "duplicate groups": self.duplicate_groups_to_remove
            == self.duplicate_groups_baseline - self.duplicate_groups_target,
            "duplicate functions": self.duplicate_functions_to_remove
            == self.duplicate_functions_baseline - self.duplicate_functions_target,
            "duplicate LOC": self.duplicate_loc_to_remove
            == self.duplicate_loc_baseline - self.duplicate_loc_target,
            "migration builders": self.migration_builders_to_convert
            == self.migration_builders_baseline - self.migration_builders_target,
            "performance gap": math.isclose(
                self.full_suite_gap_seconds,
                self.full_suite_wall_seconds - self.full_suite_target_seconds,
                abs_tol=0.001,
            ),
            "SCC cut count": self.scc_cut_edges == EXPECTED_SCC_CUTS,
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise ValueError("invalid target arithmetic: " + ", ".join(failed))
        return self


class RoadmapFreeze(StrictModel):
    schema_version: Literal["bha-122.v1"] = SCHEMA_VERSION
    status: Literal["HOLD", "PASS"]
    evidence: dict[str, EvidenceRef]
    architecture_metrics: dict[str, int]
    scc_cut_edges: tuple[SccCut, ...]
    performance_snapshot: PerformanceSnapshot
    large_modules: tuple[LargeModule, ...]
    selected_loc_crossings: tuple[str, ...]
    migration_builder_dispositions: tuple[BuilderDisposition, ...]
    retained_type_debt: dict[str, int]
    type_debt_clusters: tuple[TypeDebtCluster, ...]
    type_debt_clusters_sha256: str
    issue_train_matrix: dict[str, str]
    cleanup_slices: tuple[CleanupSlice, ...]
    estimate_totals: EstimateTotals
    target_arithmetic: TargetArithmetic
    hold_reasons: tuple[str, ...]

    @model_validator(mode="after")
    def freeze_invariants(self) -> RoadmapFreeze:
        if len(self.scc_cut_edges) != EXPECTED_SCC_CUTS:
            raise ValueError(f"expected {EXPECTED_SCC_CUTS} SCC cut edges")
        if (
            sum(cluster.count for cluster in self.type_debt_clusters)
            != self.retained_type_debt["total"]
        ):
            raise ValueError("type-debt cluster counts do not equal retained total")
        if self.status == "PASS" and self.hold_reasons:
            raise ValueError("PASS freeze cannot retain hold reasons")
        if self.status == "HOLD" and not self.hold_reasons:
            raise ValueError("HOLD freeze must explain feasibility gaps")
        if len(self.selected_loc_crossings) != 56 or len(set(self.selected_loc_crossings)) != 56:
            raise ValueError("exactly 56 unique LOC crossings are required")
        if not set(MANDATORY_LOC_ROOTS).issubset(self.selected_loc_crossings):
            raise ValueError("mandatory composition roots must be selected among the 56 crossings")
        if len(self.migration_builder_dispositions) != 172:
            raise ValueError("actual command.upgrade cohort must contain 172 builders")
        retained = sum(
            row.disposition == "retain_candidate" for row in self.migration_builder_dispositions
        )
        if retained != 60:
            raise ValueError("migration disposition must retain exactly 60 candidates")
        converted = sum(
            row.disposition == "convert_candidate" for row in self.migration_builder_dispositions
        )
        if converted != 112:
            raise ValueError("migration disposition must convert exactly 112 candidates")
        disposition_counts = Counter(
            (row.taxonomy, row.disposition) for row in self.migration_builder_dispositions
        )
        for taxonomy, count in BUILDER_RETAIN_QUOTAS.items():
            if disposition_counts[(taxonomy, "retain_candidate")] != count:
                raise ValueError(f"migration retain quota drifted: {taxonomy}")
        for taxonomy, count in BUILDER_CONVERT_QUOTAS.items():
            if disposition_counts[(taxonomy, "convert_candidate")] != count:
                raise ValueError(f"migration convert quota drifted: {taxonomy}")
        builder_keys = [(row.path, row.taxonomy) for row in self.migration_builder_dispositions]
        if len(set(builder_keys)) != len(builder_keys):
            raise ValueError("migration builder dispositions must identify unique cohort rows")
        if len({slice_.key for slice_ in self.cleanup_slices}) != len(self.cleanup_slices):
            raise ValueError("cleanup slice keys must be unique")
        if self.issue_train_matrix != PROGRAM_ISSUE_TRAIN:
            raise ValueError("issue/train matrix drifted from the approved program")
        if {slice_.issue for slice_ in self.cleanup_slices} != set(PROGRAM_ISSUE_TRAIN):
            raise ValueError("cleanup slices do not cover BHA-104 through BHA-111")
        if any(
            slice_.owner != PROGRAM_OWNER or PROGRAM_ISSUE_TRAIN.get(slice_.issue) != slice_.train
            for slice_ in self.cleanup_slices
        ):
            raise ValueError("cleanup slice ownership or issue/train mapping is invalid")
        cluster_hash = hashlib.sha256(
            json.dumps(
                [cluster.model_dump() for cluster in self.type_debt_clusters],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if cluster_hash != self.type_debt_clusters_sha256:
            raise ValueError("type-debt cluster inventory hash does not match")
        if self.estimate_totals.total_estimated_prs != sum(
            slice_.estimated_prs for slice_ in self.cleanup_slices
        ):
            raise ValueError("PR estimate total does not match cleanup slices")
        expected_hours = round(sum(slice_.estimated_hours for slice_ in self.cleanup_slices), 2)
        if not math.isclose(
            self.estimate_totals.total_estimated_hours, expected_hours, abs_tol=0.001
        ):
            raise ValueError("hour estimate total does not match cleanup slices")
        train_max: dict[str, float] = defaultdict(float)
        for slice_ in self.cleanup_slices:
            train_max[slice_.train] = max(train_max[slice_.train], slice_.estimated_calendar_weeks)
        critical_path = round(sum(train_max.values()), 2)
        if not math.isclose(
            self.estimate_totals.critical_path_calendar_weeks, critical_path, abs_tol=0.001
        ):
            raise ValueError("critical path must sum the maximum slice per sequential train")
        if not 138 <= self.estimate_totals.total_estimated_prs <= 206:
            raise ValueError("gross PR estimate is outside the approved 138-206 range")
        if not 44 <= critical_path <= 72:
            raise ValueError("critical path estimate is outside the approved 44-72 week range")
        return self


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(root: Path, path: str) -> dict[str, object]:
    value = json.loads((root / path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"evidence receipt is not an object: {path}")
    return cast(dict[str, object], value)


def _git_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _sccs(edges: list[tuple[str, str]], nodes: set[str]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        adjacency[source].add(target)
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(adjacency[node]):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] == indexes[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in sorted(nodes):
        if node not in indexes:
            visit(node)
    return sorted(components, key=lambda component: (-len(component), component))


def _order_score(order: list[str], edges: list[tuple[str, str]]) -> int:
    positions = {node: index for index, node in enumerate(order)}
    return sum(positions[source] > positions[target] for source, target in edges)


def _feedback_arc_cut(
    edges: list[tuple[str, str]], components: list[list[str]], source_hash: str
) -> list[tuple[str, str]]:
    """Choose a stable minimum feedback-arc set for the architecture SCCs.

    Exact minimum feedback arc set is NP-hard.  These SCCs are small, and the
    deterministic seeded insertion search reaches the known 31-edge optimum;
    the resulting artifact is still validated by removing the chosen edges and
    proving that no SCC remains.
    """
    selected: list[tuple[str, str]] = []
    for component in components:
        component_nodes = set(component)
        internal = sorted(
            (source, target)
            for source, target in edges
            if source in component_nodes and target in component_nodes
        )
        seed = int(
            hashlib.sha256((source_hash + "\0" + "\n".join(component)).encode()).hexdigest()[:16],
            16,
        )
        best_score = len(internal) + 1
        best_order: list[str] = []
        for restart in range(16):
            order = component[:]
            random.Random(seed + restart).shuffle(order)
            score = _order_score(order, internal)
            changed = True
            while changed:
                changed = False
                for index in range(len(order)):
                    node = order.pop(index)
                    candidate_score = score
                    candidate_index = index
                    for position in range(len(order) + 1):
                        order.insert(position, node)
                        trial_score = _order_score(order, internal)
                        order.pop(position)
                        if trial_score < candidate_score:
                            candidate_score = trial_score
                            candidate_index = position
                    order.insert(candidate_index, node)
                    if candidate_score < score:
                        score = candidate_score
                        changed = True
            if score < best_score:
                best_score = score
                best_order = order[:]
        positions = {node: index for index, node in enumerate(best_order)}
        selected.extend(
            (source, target) for source, target in internal if positions[source] > positions[target]
        )
    return sorted(selected)


def _architecture_edges(root: Path) -> tuple[ArchitectureReceipt, list[tuple[str, str]]]:
    receipt = build_architecture_receipt(root, "WORKTREE")
    return receipt, list(resolved_import_edges(root))


def _type_debt(
    root: Path, static: dict[str, object]
) -> tuple[dict[str, int], list[TypeDebtCluster]]:
    raw_diagnostics = static.get("diagnostics", [])
    diagnostics = cast(list[object], raw_diagnostics) if isinstance(raw_diagnostics, list) else []
    diagnostic_rows = [
        cast(dict[str, object], item) for item in diagnostics if isinstance(item, dict)
    ]
    pyright = next((item for item in diagnostic_rows if item.get("tool") == "pyright"), None)
    if not isinstance(pyright, dict):
        raise ValueError("static receipt lacks pyright diagnostics")
    receipt_path_raw = pyright.get("receipt_path")
    receipt_path = str(receipt_path_raw) if isinstance(receipt_path_raw, str) else None
    rows: list[dict[str, object]] = []
    evidence_sha256 = "missing"
    if isinstance(receipt_path, str) and (root / receipt_path).is_file():
        evidence_sha256 = _sha256(root / receipt_path)
        raw: object = json.loads((root / receipt_path).read_text(encoding="utf-8"))
        raw_object = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
        raw_values: object = raw_object.get("generalDiagnostics", [])
        values = cast(list[object], raw_values) if isinstance(raw_values, list) else []
        rows = [cast(dict[str, object], value) for value in values if isinstance(value, dict)]
    by_cluster: Counter[tuple[str, str]] = Counter()
    files_by_cluster: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        filename = str(row.get("file", "")).replace("\\", "/")
        try:
            filename = Path(filename).resolve().relative_to(root).as_posix()
        except ValueError:
            filename = filename.lstrip("/")
        rule = str(row.get("rule", "unknown"))
        if filename.startswith("alembic/versions_archived/"):
            zone = "archived"
        elif filename.startswith("instruction_tests/"):
            zone = "instruction_tests"
        elif filename.startswith("tests/"):
            zone = "tests"
        elif filename.startswith("execution/"):
            zone = "execution"
        elif filename.startswith("src/"):
            zone = "src"
        else:
            zone = "other"
        by_cluster[(zone, rule)] += 1
        files_by_cluster[(zone, rule)].add(filename)
    archived = sum(count for (zone, _rule), count in by_cluster.items() if zone == "archived")
    retained = sum(count for (zone, _rule), count in by_cluster.items() if zone != "archived")
    rule_lanes = {
        "reportUnknown": ("typing-unknowns", 0.08),
        "reportMissing": ("typing-annotations", 0.12),
        "reportPrivateUsage": ("api-boundaries", 0.10),
        "reportUnused": ("dead-code", 0.06),
    }
    clusters: list[TypeDebtCluster] = []
    for (zone, rule), count in sorted(by_cluster.items()):
        lane, hours_per = next(
            (value for prefix, value in rule_lanes.items() if rule.startswith(prefix)),
            ("contract-typing", 0.15),
        )
        if zone == "archived":
            lane, risk, hours_per = "archive-integrity", "low", 0.02
        elif zone == "execution":
            risk = "high"
        elif zone == "src" and (
            rule.startswith("reportArgument") or rule.startswith("reportAttribute")
        ):
            risk = "critical"
        else:
            risk = "medium"
        hours = round(count * hours_per, 2)
        if zone == "archived":
            continue
        clusters.append(
            TypeDebtCluster(
                source_zone=zone,
                rule=rule,
                count=count,
                ownership_lane=lane,
                risk=risk,
                estimated_hours=hours,
                suggested_slice_count=max(1, math.ceil(count / 100)),
                file_count=len(files_by_cluster[(zone, rule)]),
                files_sha256=hashlib.sha256(
                    "\n".join(sorted(files_by_cluster[(zone, rule)])).encode()
                ).hexdigest(),
                evidence_sha256=evidence_sha256,
            )
        )
    return {"total": retained, "archived": archived, "all": retained + archived}, clusters


def _duplicate_totals(receipt: dict[str, object]) -> tuple[int, int, int]:
    raw = receipt.get("exact_totals")
    if not isinstance(raw, dict):
        raise ValueError("duplicate receipt lacks exact_totals")
    values: list[object] = [
        raw.get(key) for key in ("groups", "participating_functions", "duplicated_loc")
    ]
    if not all(isinstance(value, int) for value in values):
        raise ValueError("duplicate receipt exact totals are not integers")
    return cast(tuple[int, int, int], tuple(values))


def _upgrade_builder_rows(test_db: dict[str, object]) -> list[dict[str, object]]:
    raw = test_db.get("database_builders", [])
    rows = cast(list[object], raw) if isinstance(raw, list) else []
    result: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        typed = cast(dict[str, object], row)
        evidence = typed.get("evidence")
        evidence_items = cast(list[object], evidence) if isinstance(evidence, list) else []
        if "call:command.upgrade" in evidence_items:
            result.append(typed)
    return sorted(
        result,
        key=lambda row: (
            str(row.get("path", "")),
            str(row.get("taxonomy", "")),
            json.dumps(row.get("evidence", []), sort_keys=True),
        ),
    )


def _builder_dispositions(rows: list[dict[str, object]]) -> tuple[BuilderDisposition, ...]:
    by_taxonomy: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_taxonomy[str(row.get("taxonomy", "unknown"))].append(row)
    expected_taxonomies = set(BUILDER_RETAIN_QUOTAS) | set(BUILDER_CONVERT_QUOTAS)
    if set(by_taxonomy) != expected_taxonomies:
        raise ValueError("migration builder taxonomies drifted from the approved cohort")
    result: list[BuilderDisposition] = []
    for taxonomy in sorted(by_taxonomy):
        ranked = sorted(
            by_taxonomy[taxonomy],
            key=lambda row: (
                str(row.get("path", "")),
                json.dumps(row.get("evidence", []), sort_keys=True),
            ),
        )
        retain_count = BUILDER_RETAIN_QUOTAS.get(taxonomy, 0)
        convert_count = BUILDER_CONVERT_QUOTAS.get(taxonomy, 0)
        if len(ranked) != retain_count + convert_count:
            raise ValueError(f"migration builder taxonomy count drifted: {taxonomy}")
        for index, row in enumerate(ranked):
            disposition = "retain_candidate" if index < retain_count else "convert_candidate"
            result.append(
                BuilderDisposition(
                    path=str(row.get("path", "")),
                    taxonomy=taxonomy,
                    disposition=disposition,
                    owner=PROGRAM_OWNER,
                    selection_basis=(
                        f"taxonomy-quota: retain {retain_count} {taxonomy} representatives; "
                        f"convert {convert_count} remaining setup builders"
                    ),
                )
            )
    return tuple(sorted(result, key=lambda row: (row.path, row.taxonomy)))


def _selected_crossings(
    large_modules: tuple[LargeModule, ...], mandatory_paths: tuple[str, ...]
) -> tuple[str, ...]:
    others = tuple(module.path for module in large_modules if module.path not in mandatory_paths)
    return mandatory_paths + others[: 56 - len(mandatory_paths)]


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
    performance = _read_json(root, PERFORMANCE_RECEIPT)
    debt_totals, clusters = _type_debt(root, static)
    receipts = {
        "architecture": ARCHITECTURE_RECEIPT,
        "static": STATIC_RECEIPT,
        "test_db": TEST_DB_RECEIPT,
        "duplicate": DUPLICATE_RECEIPT,
        "lifecycle": LIFECYCLE_RECEIPT,
        "performance": PERFORMANCE_RECEIPT,
    }
    evidence: dict[str, EvidenceRef] = {}
    for key, path in receipts.items():
        receipt = _read_json(root, path)
        revision = receipt.get("revision")
        scoped_commit = receipt.get("scoped_commit")
        if not isinstance(scoped_commit, str):
            commit_hash = receipt.get("commit_hash")
            scoped_commit = (
                commit_hash
                if isinstance(commit_hash, str)
                else revision
                if isinstance(revision, str)
                else _git_commit(root)
            )
        evidence[key] = EvidenceRef(
            path=path,
            sha256=_sha256(root / path),
            scoped_commit=scoped_commit,
            scope=(
                "COMMIT" if isinstance(revision, str) and revision != "WORKTREE" else "WORKTREE"
            ),
        )
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
            estimated_hours=round(sum(cluster.estimated_hours for cluster in clusters), 2),
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
            units=4200,
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
    return RoadmapFreeze(
        status="HOLD",
        evidence=evidence,
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
        retained_type_debt=debt_totals,
        type_debt_clusters=tuple(clusters),
        type_debt_clusters_sha256=cluster_hash,
        issue_train_matrix=PROGRAM_ISSUE_TRAIN,
        cleanup_slices=slices,
        estimate_totals=EstimateTotals(
            total_estimated_hours=total_hours,
            total_estimated_prs=total_prs,
            critical_path_calendar_weeks=critical_path_weeks,
        ),
        target_arithmetic=arithmetic,
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
        "performance",
    }
    if set(freeze.evidence) != expected_evidence:
        raise ValueError("freeze evidence keys are incomplete or unexpected")
    for key in ("architecture", "static", "test_db", "duplicate", "lifecycle"):
        evidence = freeze.evidence[key]
        expected_path = {
            "architecture": ARCHITECTURE_RECEIPT,
            "static": STATIC_RECEIPT,
            "test_db": TEST_DB_RECEIPT,
            "duplicate": DUPLICATE_RECEIPT,
            "lifecycle": LIFECYCLE_RECEIPT,
        }[key]
        if evidence.path != expected_path:
            raise ValueError(f"checked evidence path drifted: {key}")
        evidence_path = root / evidence.path
        if not evidence_path.is_file() or _sha256(evidence_path) != evidence.sha256:
            raise ValueError(f"checked evidence is missing or changed: {evidence.path}")
    performance_ref = freeze.evidence["performance"]
    if performance_ref.path != PERFORMANCE_RECEIPT:
        raise ValueError("checked performance evidence path drifted")
    performance_path = root / performance_ref.path
    if performance_path.is_file() and _sha256(performance_path) != performance_ref.sha256:
        raise ValueError("historical performance evidence changed")

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
    pyright_receipt = pyright.get("receipt_path")
    if isinstance(pyright_receipt, str) and (root / pyright_receipt).is_file():
        current_debt, current_clusters = _type_debt(root, static)
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
    lifecycle = _read_json(root, LIFECYCLE_RECEIPT)
    lifecycle_counts = lifecycle.get("counts")
    typed_lifecycle_counts = (
        cast(dict[str, object], lifecycle_counts) if isinstance(lifecycle_counts, dict) else {}
    )
    dormant_raw: object = typed_lifecycle_counts.get("dormant-until")
    dormant = dormant_raw if isinstance(dormant_raw, int) else None
    dormant_slice = next(slice_ for slice_ in freeze.cleanup_slices if slice_.issue == "BHA-109")
    if not isinstance(dormant, int) or dormant_slice.units != dormant:
        raise ValueError("pruning slice is not bound to the current lifecycle receipt")
    return freeze
