"""Typed contracts and frozen constants for the BHA-122 roadmap freeze.

This module is dependency-free with respect to the freeze builder so inventory
and workflow modules can import the contract without circular dependencies.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
LOC_TARGET_CAPS = {
    "execution/comments_server.py": 600,
    "src/pipeline/portfolio_panel.py": 200,
}
TYPE_DEBT_AUTHORITY_PATH = "docs/quality/type-debt-membership-authority.json"
TYPE_DEBT_AUTHORITY_SHA256 = "3a3d09fe84406bcdf499d050b28de32d76575db88a4dfd722a13ec3ee32c73c9"
FROZEN_TYPE_DEBT_AUTHORITY_SHA256 = (
    "6785672f42a8883db1761c4b37ff6cc5b0a938e38d1fc4320ae237ecd57114ed"
)
FROZEN_TYPE_DEBT_TOTALS = {"total": 4200, "archived": 175, "all": 4375}

# Train 0 froze one specific performance run.  The receipt is intentionally
# under .tmp and may be absent on a fresh checkout, so its absence cannot turn
# the freeze into an editable bag of self-attested numbers.  These identities
# are the locally approved authority for this historical snapshot; when the
# receipt is present we additionally verify its bytes and fields.
FROZEN_PERFORMANCE_RECEIPT_SHA256 = (
    "72e2629b68cb70fb40d01321c425aa5db7dbd74cc28ae831b614b90590a3c7a8"
)
FROZEN_PERFORMANCE: dict[str, object] = {
    "schema_version": "test-ci-performance/v2",
    "revision": "8c7dc0c3560eddf20d9375c428f8dc795625dcb6",
    "source_sha256": "a4181b2effa3387fa1918e2e1f3d817e1dbca311f30f68f6035237240cb75675",
    "cohort_sha256": "bd2a31afb89053f0bd13bbd6514274beefb24e90425cac9f73669385ec348156",
    "process_wall_seconds": 952.1505841249891,
    "paired": False,
    "evidence_status": "hold",
    "network_isolation": "requested-not-proven",
}
FROZEN_ESTIMATE_MATRIX: dict[str, tuple[float, int, float]] = {
    "migration-test-ci": (56.0, 22, 7.0),
    "active-type-debt": (387.55, 20, 8.0),
    "integrity-audit": (120.0, 9, 5.0),
    "duplicate-authorities": (80.0, 11, 6.0),
    "architecture-boundaries": (180.0, 68, 16.0),
    "lifecycle-pruning": (120.0, 12, 6.0),
    "final-static-zero": (80.0, 12, 6.0),
    "quality-closure": (60.0, 2, 3.0),
}
FROZEN_ESTIMATE_TOTALS: tuple[float, int, float] = (1083.55, 156, 57.0)
BHA115_CLOSURE_CONDITION = (
    "BHA-115 closes only after integrity SQL/rows/time/RSS, Alembic invocation/time, "
    "fixed 20-route cold/warm connection/query evidence, DCF stage evidence, source "
    "cache/RSS evidence, CI setup/test timing, >=7 paired repeats with adaptive stability, "
    "approved production-shaped evidence, and Sol acceptance of the owner-amended rejudged receipt"
)
BHA115_OWNER_AMENDMENT_PATH = "owner amendment -> regenerate receipts -> Sol rejudge"


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


class LocCrossing(StrictModel):
    path: str
    baseline_loc: int = Field(gt=1000)
    target_cap: int = Field(gt=0)
    selection_basis: str


class BuilderDisposition(StrictModel):
    path: str
    taxonomy: str
    disposition: Literal["retain_candidate", "convert_candidate"]
    owner: str
    selection_basis: str
    selection_rank: int = Field(ge=1)
    evidence: tuple[str, ...]
    rationale: str
    exception_code: str | None = None


class BuilderException(StrictModel):
    path: str
    taxonomy: str
    selection_rank: int = Field(ge=1)
    evidence: tuple[str, ...]
    rationale: str


class TrainPlan(StrictModel):
    issue: str
    train: str
    depends_on: tuple[str, ...]
    resource_lane: str
    overlap_group: str
    max_parallel: int = Field(ge=1)


class BudgetMapping(StrictModel):
    item_kind: Literal["scc_cut", "loc_crossing", "type_cluster", "builder", "slice_anchor"]
    item_id: str
    slice_key: str
    work_package: str
    units: int = Field(gt=0)
    estimated_prs: int = Field(ge=0)
    evidence: tuple[str, ...]


class BHA115Closure(StrictModel):
    status: Literal["OPEN", "CLOSED"]
    condition: str
    owner_amendment_path: str
    rejudge_required: bool


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
    artifact_acceptance_status: Literal["PASS", "HOLD"]
    program_feasibility_status: Literal["PASS", "HOLD"]
    evidence: dict[str, EvidenceRef]
    architecture_metrics: dict[str, int]
    scc_cut_edges: tuple[SccCut, ...]
    performance_snapshot: PerformanceSnapshot
    large_modules: tuple[LargeModule, ...]
    selected_loc_crossings: tuple[LocCrossing, ...]
    migration_builder_dispositions: tuple[BuilderDisposition, ...]
    builder_exception_inventory: tuple[BuilderException, ...]
    retained_type_debt: dict[str, int]
    type_debt_clusters: tuple[TypeDebtCluster, ...]
    type_debt_clusters_sha256: str
    issue_train_matrix: dict[str, str]
    train_plan: tuple[TrainPlan, ...]
    cleanup_slices: tuple[CleanupSlice, ...]
    budget_mappings: tuple[BudgetMapping, ...]
    estimate_totals: EstimateTotals
    target_arithmetic: TargetArithmetic
    bha115_closure: BHA115Closure
    hold_reasons: tuple[str, ...]

    @model_validator(mode="after")
    def freeze_invariants(self) -> RoadmapFreeze:
        expected_performance = PerformanceSnapshot.model_validate(FROZEN_PERFORMANCE)
        if self.performance_snapshot != expected_performance:
            raise ValueError("performance snapshot drifted from the approved Train 0 freeze")
        if self.artifact_acceptance_status != "PASS":
            raise ValueError("freeze artifact acceptance must be PASS after schema validation")
        if self.program_feasibility_status != self.status:
            raise ValueError("legacy status must mirror program feasibility status")
        if self.bha115_closure.status != "OPEN" or not self.bha115_closure.rejudge_required:
            raise ValueError("BHA-115 closure must remain open pending owner amendment and rejudge")
        if self.bha115_closure.condition != BHA115_CLOSURE_CONDITION:
            raise ValueError("BHA-115 closure condition drifted")
        if self.bha115_closure.owner_amendment_path != BHA115_OWNER_AMENDMENT_PATH:
            raise ValueError("BHA-115 owner amendment path drifted")
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
        if (
            len(self.selected_loc_crossings) != 56
            or len({row.path for row in self.selected_loc_crossings}) != 56
        ):
            raise ValueError("exactly 56 unique LOC crossings are required")
        selected_paths = {row.path for row in self.selected_loc_crossings}
        if not set(MANDATORY_LOC_ROOTS).issubset(selected_paths):
            raise ValueError("mandatory composition roots must be selected among the 56 crossings")
        for path, cap in LOC_TARGET_CAPS.items():
            row = next((row for row in self.selected_loc_crossings if row.path == path), None)
            if row is None or row.target_cap != cap or row.baseline_loc <= cap:
                raise ValueError("mandatory LOC target cap drifted")
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
        if (
            tuple(
                BuilderException(
                    path=row.path,
                    taxonomy=row.taxonomy,
                    selection_rank=row.selection_rank,
                    evidence=row.evidence,
                    rationale=row.rationale,
                )
                for row in self.migration_builder_dispositions
                if row.disposition == "retain_candidate"
            )
            != self.builder_exception_inventory
        ):
            raise ValueError("builder exception inventory is not the exact retained set")
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
        expected_train_plan = _train_plan()
        if self.train_plan != expected_train_plan:
            raise ValueError("train dependencies or resource constraints drifted")
        slices_by_issue = {slice_.issue: slice_ for slice_ in self.cleanup_slices}
        if any(
            slices_by_issue.get(plan.issue) is None
            or slices_by_issue[plan.issue].ownership_lane != plan.resource_lane
            or slices_by_issue[plan.issue].parallel_group != plan.overlap_group
            or plan.max_parallel != 1
            for plan in self.train_plan
        ):
            raise ValueError("train resource or overlap constraints do not bind to slices")
        for cleanup_slice in self.cleanup_slices:
            expected = FROZEN_ESTIMATE_MATRIX.get(cleanup_slice.key)
            if (
                expected is None
                or (
                    cleanup_slice.estimated_hours,
                    cleanup_slice.estimated_prs,
                    cleanup_slice.estimated_calendar_weeks,
                )
                != expected
            ):
                raise ValueError("cleanup-slice estimate matrix drifted from the approved program")
        if (
            self.estimate_totals.total_estimated_hours,
            self.estimate_totals.total_estimated_prs,
            self.estimate_totals.critical_path_calendar_weeks,
        ) != FROZEN_ESTIMATE_TOTALS:
            raise ValueError("estimate totals drifted from the approved program")
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
        mapping_keys = [(row.item_kind, row.item_id) for row in self.budget_mappings]
        if len(mapping_keys) != len(set(mapping_keys)):
            raise ValueError("budget mappings must not overlap items")
        mapping_counts = Counter(row.item_kind for row in self.budget_mappings)
        if mapping_counts != {
            "scc_cut": 31,
            "loc_crossing": 56,
            "type_cluster": 61,
            "builder": 172,
            "slice_anchor": 5,
        }:
            raise ValueError("budget mappings do not cover the complete measured inventory")
        if (
            sum(row.estimated_prs for row in self.budget_mappings)
            != self.estimate_totals.total_estimated_prs
        ):
            raise ValueError("bottom-up budget mappings do not sum to PR estimate")
        mapped_prs: dict[str, int] = defaultdict(int)
        for row in self.budget_mappings:
            mapped_prs[row.slice_key] += row.estimated_prs
        for cleanup_slice in self.cleanup_slices:
            if mapped_prs[cleanup_slice.key] != cleanup_slice.estimated_prs:
                raise ValueError("budget mappings do not reconcile to cleanup slices")
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


def _train_plan() -> tuple[TrainPlan, ...]:
    """Return the frozen sequential train/resource dependency chain."""
    lanes = (
        "test-harness",
        "typed-contracts",
        "integrity-audit",
        "shared-primitives",
        "architecture-boundaries",
        "lifecycle-pruning",
        "static-quality",
        "quality-closure",
    )
    overlap_groups = (
        "test-performance",
        "typed-contracts",
        "integrity-boundaries",
        "maintainability",
        "architecture-boundaries",
        "cleanup",
        "static-quality",
        "closure",
    )
    rows: list[TrainPlan] = []
    issues = tuple(PROGRAM_ISSUE_TRAIN)
    for index, (issue, train) in enumerate(PROGRAM_ISSUE_TRAIN.items()):
        rows.append(
            TrainPlan(
                issue=issue,
                train=train,
                depends_on=issues[:index],
                resource_lane=lanes[index],
                overlap_group=overlap_groups[index],
                max_parallel=1,
            )
        )
    return tuple(rows)


# Public composition alias: the workflow module imports this without reaching
# across a private-module boundary, while its historical `_train_plan` seam is
# retained by `quality.roadmap_freeze`.
train_plan = _train_plan
