"""Typed index models for the BHA-144 roadmap freeze."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Hex64 = str
EvidenceKey = Literal[
    "architecture",
    "duplicates",
    "static",
    "test_db",
    "lifecycle",
    "reachability",
    "performance",
    "reconciliation",
]
Population = Literal[
    "large_module",
    "scc_cut",
    "duplicate_authority",
    "builder_invocation",
    "type_cluster",
    "lifecycle",
    "deletion",
    "admission",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OwnerIssue(StrictModel):
    issue_id: str = Field(pattern=r"^BHA-[0-9]+$")
    lane: str = Field(min_length=1, max_length=80)
    confirmed: bool


class Resource(StrictModel):
    resource_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    capacity: int = Field(ge=1, le=8)


class WorkIntent(StrictModel):
    intent_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    owner_issue: str = Field(pattern=r"^BHA-[0-9]+$")
    lane: str = Field(min_length=1, max_length=80)
    action: Literal[
        "split",
        "deduplicate",
        "retire",
        "root_reduction",
        "cycle_cut",
        "type_remediation",
        "lifecycle_review",
        "admission_delivery",
    ]
    intended_outcome: str = Field(min_length=1, max_length=300)
    depends_on: tuple[str, ...]
    resources: tuple[str, ...]
    parallel_group: str | None = Field(default=None, max_length=80)
    scope_sha256: Hex64 = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: tuple[EvidenceKey, ...]
    acceptance_tests: tuple[str, ...] = Field(min_length=1)
    candidate_ids: tuple[str, ...] = Field(min_length=1)


class CandidateRow(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z0-9][a-z0-9_.:/-]*$")
    population: Population
    source_identity: str = Field(min_length=1, max_length=300)
    baseline_noncomment_loc: int | None = Field(default=None, ge=0)
    baseline_fan_out: int | None = Field(default=None, ge=0)
    partition: str = Field(min_length=1, max_length=80)
    disposition: Literal["split", "deduplicate", "retire", "retain", "exception", "review"]
    owner_issue: str = Field(pattern=r"^BHA-[0-9]+$")
    lane: str = Field(min_length=1, max_length=80)
    dependencies: tuple[str, ...]
    resources: tuple[str, ...]
    scope_sha256: Hex64 = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: tuple[EvidenceKey, ...]
    covered_by: str = Field(min_length=1, max_length=120)
    expected_post_loc: int | None = Field(default=None, ge=0)
    protected_root_covered_by: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _large_module_fields(self) -> CandidateRow:
        if self.population == "large_module" and (
            self.baseline_noncomment_loc is None or self.baseline_fan_out is None
        ):
            raise ValueError("large-module candidate requires exact baseline metrics")
        if self.population != "large_module" and (
            self.baseline_noncomment_loc is not None or self.baseline_fan_out is not None
        ):
            raise ValueError("only large-module candidates carry architecture metrics")
        return self


class CensusRow(StrictModel):
    candidate_id: str
    population: Population
    source_identity: str
    baseline_noncomment_loc: int | None = Field(default=None, ge=0)
    baseline_fan_out: int | None = Field(default=None, ge=0)


class PerformancePlan(StrictModel):
    target_seconds: Literal[510]
    owner_issue: Literal["BHA-104"]
    evidence_ref: EvidenceKey | None
    paired_current_evidence: bool


class CapacityPlan(StrictModel):
    measured_lane_capacity: bool
    calendar_weeks: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _calendar_requires_measurement(self) -> CapacityPlan:
        if self.calendar_weeks is not None and not self.measured_lane_capacity:
            raise ValueError("calendar requires measured lane capacity")
        return self


class FreezePlan(StrictModel):
    schema_version: Literal["roadmap-freeze-plan/v1"]
    subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    owners: tuple[OwnerIssue, ...]
    resources: tuple[Resource, ...]
    intents: tuple[WorkIntent, ...]
    candidates: tuple[CandidateRow, ...]
    performance: PerformancePlan
    capacity: CapacityPlan


class PopulationRoute(StrictModel):
    population: Population
    owner_issues: tuple[str, ...] = Field(min_length=1)


class AdmissionRoute(StrictModel):
    kind: Literal["block", "hard_gate"]
    key: str = Field(min_length=1, max_length=120)
    issue_id: str = Field(pattern=r"^BHA-[0-9]+$")
    delivery_issues: tuple[str, ...]


class OwnerSnapshot(StrictModel):
    schema_version: Literal["quality-roadmap-owners/v1"]
    source_document_id: str = Field(min_length=1, max_length=120)
    source_document_path: Literal["docs/quality/quality-9plus-roadmap.md"]
    source_sha256: Hex64 = Field(pattern=r"^[0-9a-f]{64}$")
    owners: tuple[OwnerIssue, ...] = Field(min_length=1)
    population_routes: tuple[PopulationRoute, ...] = Field(min_length=1)
    admission_routes: tuple[AdmissionRoute, ...] = Field(min_length=1)


class OwnerSnapshotIndex(StrictModel):
    path: str
    sha256: Hex64 = Field(pattern=r"^[0-9a-f]{64}$")
    source_document_id: str
    source_document_path: str
    source_sha256: Hex64 = Field(pattern=r"^[0-9a-f]{64}$")
    owners: tuple[OwnerIssue, ...]
    population_routes: tuple[PopulationRoute, ...]
    admission_routes: tuple[AdmissionRoute, ...]
    oracle_status: Literal["HOLD"]
    oracle_reasons: tuple[str, ...] = Field(min_length=1)


class EvidenceIndexEntry(StrictModel):
    key: EvidenceKey
    path: str
    sha256: Hex64 = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)
    schema_version: str | None
    source_status: str | None
    subject_commit: str | None
    oracle_status: Literal["VERIFIED", "HOLD"]
    reasons: tuple[str, ...]


class FreezeCoverage(StrictModel):
    baseline_modules_over_1000: int = Field(ge=0)
    target_modules_over_1000: Literal[35]
    required_net_reduction: int = Field(ge=0)
    planned_crossing_paths: tuple[str, ...]
    planned_net_reduction: int = Field(ge=0)
    observed_delivered_net_reduction: int | None = Field(default=None, ge=0)
    unplanned_net_reduction: int = Field(ge=0)
    protected_root_intents: tuple[str, ...]
    distinct_pr_intents: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent_arithmetic(self) -> FreezeCoverage:
        required = max(0, self.baseline_modules_over_1000 - self.target_modules_over_1000)
        if self.required_net_reduction != required:
            raise ValueError("required reduction does not match baseline and target")
        if len(self.planned_crossing_paths) != len(set(self.planned_crossing_paths)):
            raise ValueError("planned crossing paths must be unique")
        if self.planned_net_reduction != len(self.planned_crossing_paths):
            raise ValueError("planned reduction does not match crossing paths")
        if self.unplanned_net_reduction != max(0, required - self.planned_net_reduction):
            raise ValueError("unplanned reduction is inconsistent")
        if self.observed_delivered_net_reduction is not None:
            raise ValueError("this freeze schema has no before/after achievement producer")
        return self


class FreezeReceipt(StrictModel):
    schema_version: Literal["roadmap-freeze-index/v1"] = "roadmap-freeze-index/v1"
    subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    subject_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    generator_sha256: Hex64 = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: tuple[EvidenceIndexEntry, ...]
    plan_path: str | None = None
    plan_sha256: Hex64 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    plan_raw_json: str | None = None
    plan: FreezePlan | None = None
    owner_snapshot_sha256: Hex64 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    owner_snapshot: OwnerSnapshotIndex | None = None
    candidate_census: tuple[CensusRow, ...]
    coverage: FreezeCoverage
    artifact_status: Literal["PASS", "HOLD"]
    program_status: Literal["HOLD"]
    hold_reasons: tuple[str, ...]

    @model_validator(mode="after")
    def _consistent_status(self) -> FreezeReceipt:
        keys = [item.key for item in self.evidence]
        paths = [item.path for item in self.evidence]
        if len(keys) != len(set(keys)) or len(paths) != len(set(paths)):
            raise ValueError("evidence index keys and paths must be unique")
        if (self.owner_snapshot is None) != (self.owner_snapshot_sha256 is None):
            raise ValueError("owner snapshot and hash must appear together")
        if self.owner_snapshot and self.owner_snapshot.sha256 != self.owner_snapshot_sha256:
            raise ValueError("owner snapshot hash mismatch")
        plan_parts = (self.plan_path, self.plan_sha256, self.plan_raw_json, self.plan)
        if 0 < sum(item is not None for item in plan_parts) < len(plan_parts):
            raise ValueError("plan, path, and hash must appear together")
        if self.plan is not None and self.plan_raw_json is not None:

            def reject_constant(value: str) -> float:
                raise ValueError(f"non-finite JSON number: {value}")

            def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(f"duplicate JSON key: {key}")
                    result[key] = value
                return result

            try:
                json.loads(
                    self.plan_raw_json,
                    object_pairs_hook=reject_duplicates,
                    parse_constant=reject_constant,
                )
                parsed = FreezePlan.model_validate_json(self.plan_raw_json)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ValueError("plan_raw_json is not strict JSON") from exc
            if parsed != self.plan:
                raise ValueError("raw and parsed plans differ")
            if hashlib.sha256(self.plan_raw_json.encode("utf-8")).hexdigest() != self.plan_sha256:
                raise ValueError("raw plan hash mismatch")
        if self.artifact_status == "PASS" and self.hold_reasons:
            raise ValueError("PASS freeze cannot carry HOLD reasons")
        if self.artifact_status == "PASS" and (
            set(keys)
            != {
                "architecture",
                "duplicates",
                "static",
                "test_db",
                "lifecycle",
                "reachability",
                "performance",
                "reconciliation",
            }
            or self.plan is None
            or self.owner_snapshot is None
            or any(item.oracle_status != "VERIFIED" for item in self.evidence)
            or self.coverage.unplanned_net_reduction != 0
        ):
            raise ValueError("PASS freeze is missing complete verified inputs")
        if self.artifact_status == "HOLD" and not self.hold_reasons:
            raise ValueError("HOLD freeze requires reasons")
        candidate_ids = [item.candidate_id for item in self.candidate_census]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate census identities must be unique")
        measured_large = sum(
            item.population == "large_module"
            and item.baseline_noncomment_loc is not None
            and item.baseline_noncomment_loc > 1000
            for item in self.candidate_census
        )
        if measured_large != self.coverage.baseline_modules_over_1000:
            raise ValueError("coverage baseline differs from candidate census")
        if self.plan is not None and self.plan.subject_commit != self.subject_commit:
            raise ValueError("plan subject differs from freeze subject")
        if self.plan is not None:
            actionable = {
                item.covered_by
                for item in self.plan.candidates
                if item.disposition in {"split", "deduplicate", "retire"}
                or item.population == "admission"
            } | {
                item.protected_root_covered_by
                for item in self.plan.candidates
                if item.protected_root_covered_by is not None
            }
            if self.coverage.distinct_pr_intents != len(actionable):
                raise ValueError("distinct intent count differs from plan")
            by_id = {item.candidate_id: item for item in self.candidate_census}
            planned = tuple(
                sorted(
                    item.source_identity
                    for item in self.plan.candidates
                    if item.population == "large_module"
                    and by_id.get(item.candidate_id) is not None
                    and (by_id[item.candidate_id].baseline_noncomment_loc or 0) > 1000
                    and item.disposition in {"split", "deduplicate", "retire"}
                    and item.expected_post_loc is not None
                    and item.expected_post_loc <= 1000
                )
            )
            if planned != self.coverage.planned_crossing_paths:
                raise ValueError("planned crossing paths differ from plan")
            protected = tuple(
                sorted(
                    {
                        item.protected_root_covered_by
                        for item in self.plan.candidates
                        if item.protected_root_covered_by is not None
                    }
                )
            )
            if protected != self.coverage.protected_root_intents:
                raise ValueError("protected-root intents differ from plan")
        if any(item.subject_commit not in (None, self.subject_commit) for item in self.evidence):
            raise ValueError("evidence subject differs from freeze subject")
        return self


__all__ = [
    "AdmissionRoute",
    "CandidateRow",
    "CapacityPlan",
    "CensusRow",
    "EvidenceIndexEntry",
    "EvidenceKey",
    "FreezeCoverage",
    "FreezePlan",
    "FreezeReceipt",
    "OwnerIssue",
    "OwnerSnapshot",
    "OwnerSnapshotIndex",
    "PerformancePlan",
    "Population",
    "PopulationRoute",
    "Resource",
    "WorkIntent",
]
