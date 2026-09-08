"""Build the deterministic BHA-144 roadmap-freeze index."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from quality.architecture import COMPOSITION_ROOTS, ArchitectureReceipt
from quality.duplicates import DuplicateInventory
from quality.git_env import clean_local_git_env
from quality.lifecycle_models import LifecycleInventory
from quality.performance_models import PerformanceReceipt
from quality.reachability import ReachabilityGraph
from quality.roadmap_freeze_inputs import (
    EVIDENCE_KEYS,
    FreezeInputError,
    LoadedInput,
    LoadedJson,
    assert_unchanged,
    exact_subject,
    load_evidence,
    load_owner_snapshot,
    load_plan,
    reject_aliases,
)
from quality.roadmap_freeze_models import (
    CandidateRow,
    CensusRow,
    EvidenceIndexEntry,
    EvidenceKey,
    FreezeCoverage,
    FreezePlan,
    FreezeReceipt,
    OwnerSnapshot,
    OwnerSnapshotIndex,
    WorkIntent,
)
from quality.roadmap_reconciliation import ReconciliationReceipt
from quality.static_quality import StaticQualityInventory
from quality.test_db_models import TestDbAudit

GENERATOR_PATHS = (
    "src/quality/roadmap_freeze.py",
    "src/quality/roadmap_freeze_inputs.py",
    "src/quality/roadmap_freeze_models.py",
)


def _runtime_generator_hash() -> str:
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in GENERATOR_PATHS:
        digest.update(path.encode() + b"\0" + (root / path).read_bytes() + b"\0")
    return digest.hexdigest()


RUNTIME_GENERATOR_SHA256 = _runtime_generator_hash()
TARGET_LARGE_MODULES = 35
POPULATION_EVIDENCE: dict[str, frozenset[EvidenceKey]] = {
    "large_module": frozenset({"architecture"}),
    "scc_cut": frozenset({"architecture"}),
    "duplicate_authority": frozenset({"duplicates"}),
    "builder_invocation": frozenset({"test_db"}),
    "type_cluster": frozenset({"static"}),
    "lifecycle": frozenset({"lifecycle"}),
    "deletion": frozenset({"lifecycle", "reachability"}),
    "admission": frozenset({"reconciliation"}),
}
NON_WORK_DISPOSITIONS = frozenset({"retain", "review", "exception"})


def compute_scope_sha256(
    subject_commit: str,
    intent: WorkIntent,
    candidates: tuple[CandidateRow, ...],
    evidence_bindings: Mapping[EvidenceKey, tuple[str, str]],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"roadmap-freeze-scope/v1\0" + subject_commit.encode() + b"\0")
    intent_material = intent.model_dump(exclude={"scope_sha256"}, mode="json")
    candidate_material = [
        item.model_dump(exclude={"scope_sha256"}, mode="json")
        for item in sorted(candidates, key=lambda row: row.candidate_id)
    ]
    digest.update(json.dumps(intent_material, sort_keys=True, separators=(",", ":")).encode())
    digest.update(b"\0")
    digest.update(json.dumps(candidate_material, sort_keys=True, separators=(",", ":")).encode())
    digest.update(b"\0")
    for key in sorted(evidence_bindings):
        path, sha256 = evidence_bindings[key]
        digest.update(
            b"evidence\0" + key.encode() + b"\0" + path.encode() + b"\0" + sha256.encode() + b"\0"
        )
    return digest.hexdigest()


def _tracked_test_exists(repo_root: Path, subject: str, path: str) -> bool:
    if (
        not path.startswith("tests/")
        or "::" in path
        or Path(path).is_absolute()
        or ".." in Path(path).parts
    ):
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{subject}:{path}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        env=clean_local_git_env(),
    )
    return result.returncode == 0


def _git_blob(repo_root: Path, subject: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{subject}:{path}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        env=clean_local_git_env(),
    )
    if result.returncode != 0:
        raise FreezeInputError(f"generator is absent from subject: {path}")
    current = (repo_root / path).read_bytes()
    if current != result.stdout:
        raise FreezeInputError(f"generator differs from subject: {path}")
    return current


def _generator_hash(repo_root: Path, subject: str) -> str:
    digest = hashlib.sha256()
    for path in GENERATOR_PATHS:
        raw = _git_blob(repo_root, subject, path)
        digest.update(path.encode() + b"\0" + raw + b"\0")
    value = digest.hexdigest()
    if value != RUNTIME_GENERATOR_SHA256:
        raise FreezeInputError("runtime freeze generator differs from subject")
    return value


def _schema(value: object) -> str | None:
    raw = getattr(value, "schema_version", None)
    return raw if isinstance(raw, str) else None


def _source_status(value: object) -> str | None:
    if isinstance(
        value,
        (StaticQualityInventory, LifecycleInventory, PerformanceReceipt, ReconciliationReceipt),
    ):
        return value.status
    if isinstance(value, (TestDbAudit, ReachabilityGraph)):
        return value.collection_status
    return None


def _subject(value: object) -> str | None:
    if isinstance(value, (ArchitectureReceipt, StaticQualityInventory, TestDbAudit)):
        return value.scoped_commit
    if isinstance(value, DuplicateInventory):
        return value.commit_hash
    if isinstance(value, (ReachabilityGraph, ReconciliationReceipt)):
        return value.subject_commit
    if isinstance(value, (LifecycleInventory, PerformanceReceipt)):
        return value.revision
    return None


def _index(key: EvidenceKey, item: LoadedInput) -> EvidenceIndexEntry:
    return EvidenceIndexEntry(
        key=key,
        path=item.relative_path,
        sha256=item.sha256,
        byte_length=len(item.raw),
        schema_version=_schema(item.value),
        source_status=_source_status(item.value),
        subject_commit=_subject(item.value),
        oracle_status=item.oracle_status,
        reasons=item.oracle_reasons,
    )


def _census(
    repo_root: Path, loaded: Mapping[EvidenceKey, LoadedInput], owners: OwnerSnapshot | None
) -> tuple[CensusRow, ...]:
    rows: list[CensusRow] = []
    architecture_item = loaded.get("architecture")
    if architecture_item is not None:
        architecture = cast(ArchitectureReceipt, architecture_item.value)
        modules = architecture.metrics.modules
        for module in sorted(modules, key=lambda item: item.path):
            if module.lines.noncomment > 1000 or module.path in COMPOSITION_ROOTS:
                rows.append(
                    CensusRow(
                        candidate_id=f"large-module:{module.path}",
                        population="large_module",
                        source_identity=module.path,
                        baseline_noncomment_loc=module.lines.noncomment,
                        baseline_fan_out=module.internal_fan_out,
                    )
                )
    duplicate_item = loaded.get("duplicates")
    if duplicate_item is not None:
        inventory = cast(DuplicateInventory, duplicate_item.value)
        for group in sorted(
            (*inventory.exact_groups, *inventory.near_miss_groups), key=lambda item: item.group_id
        ):
            rows.append(
                CensusRow(
                    candidate_id=f"duplicate-authority:{group.group_id}",
                    population="duplicate_authority",
                    source_identity=group.group_id,
                )
            )
    db_item = loaded.get("test_db")
    if db_item is not None:
        audit = cast(TestDbAudit, db_item.value)
        for invocation in sorted(audit.builder_invocations, key=lambda item: item.invocation_id):
            rows.append(
                CensusRow(
                    candidate_id=f"builder-invocation:{invocation.invocation_id}",
                    population="builder_invocation",
                    source_identity=invocation.invocation_id,
                )
            )
    lifecycle_item = loaded.get("lifecycle")
    if lifecycle_item is not None and lifecycle_item.oracle_status == "VERIFIED":
        lifecycle = cast(LifecycleInventory, lifecycle_item.value)
        for entry in sorted(lifecycle.entries, key=lambda item: item.fingerprint):
            rows.append(
                CensusRow(
                    candidate_id=f"lifecycle:{entry.fingerprint}",
                    population="lifecycle",
                    source_identity=entry.fingerprint,
                )
            )
    if owners is not None:
        for route in sorted(owners.admission_routes, key=lambda item: (item.kind, item.key)):
            identity = f"{route.kind}:{route.key}"
            rows.append(
                CensusRow(
                    candidate_id=f"admission:{identity}",
                    population="admission",
                    source_identity=identity,
                )
            )
    ids = [row.candidate_id for row in rows]
    if len(ids) != len(set(ids)):
        raise FreezeInputError("native candidate census contains duplicate identities")
    return tuple(sorted(rows, key=lambda row: row.candidate_id))


def _duplicates(values: tuple[str, ...] | list[str], label: str) -> None:
    repeated = sorted(key for key, count in Counter(values).items() if count > 1)
    if repeated:
        raise FreezeInputError(f"duplicate {label}: {repeated[0]}")


def _plan_holds(
    repo_root: Path,
    subject: str,
    plan: FreezePlan,
    census: tuple[CensusRow, ...],
    owners: OwnerSnapshot | None,
    loaded: Mapping[EvidenceKey, LoadedInput],
) -> tuple[list[str], tuple[str, ...], int, int]:
    errors: list[str] = []
    holds: list[str] = []
    _duplicates([item.issue_id for item in plan.owners], "plan owner")
    _duplicates([item.resource_id for item in plan.resources], "resource")
    _duplicates([item.intent_id for item in plan.intents], "intent")
    _duplicates([item.candidate_id for item in plan.candidates], "candidate")
    intent_by_id = {item.intent_id: item for item in plan.intents}
    candidate_by_id = {item.candidate_id: item for item in plan.candidates}
    census_by_id = {item.candidate_id: item for item in census}
    owner_by_id = {item.issue_id: item for item in owners.owners} if owners else {}
    routes = (
        {item.population: set(item.owner_issues) for item in owners.population_routes}
        if owners
        else {}
    )
    resources = {item.resource_id: item for item in plan.resources}
    admission_owners = (
        {f"{item.kind}:{item.key}": item.issue_id for item in owners.admission_routes}
        if owners
        else {}
    )
    for declared_owner in plan.owners:
        trusted_owner = owner_by_id.get(declared_owner.issue_id)
        if trusted_owner is None:
            holds.append(f"plan owner is absent from snapshot: {declared_owner.issue_id}")
        elif declared_owner != trusted_owner:
            errors.append(f"plan owner differs from snapshot: {declared_owner.issue_id}")
    for item in plan.intents:
        if item.owner_issue not in owner_by_id or not owner_by_id[item.owner_issue].confirmed:
            holds.append(f"intent owner is not confirmed by snapshot: {item.intent_id}")
        elif owner_by_id[item.owner_issue].lane != item.lane:
            errors.append(f"intent lane differs from owner snapshot: {item.intent_id}")
        for dependency in item.depends_on:
            if dependency not in intent_by_id:
                errors.append(f"dangling intent dependency: {dependency}")
        for resource in item.resources:
            if resource not in resources:
                errors.append(f"unknown resource: {resource}")
        for candidate_id in item.candidate_ids:
            if candidate_id not in candidate_by_id:
                errors.append(f"intent references unknown candidate: {candidate_id}")
        if any(
            not _tracked_test_exists(repo_root, subject, path) for path in item.acceptance_tests
        ):
            errors.append(f"intent acceptance test is not a tracked subject path: {item.intent_id}")
        bindings: dict[EvidenceKey, tuple[str, str]] = {
            key: (loaded[key].relative_path, loaded[key].sha256)
            for key in item.evidence_refs
            if key in loaded
        }
        scoped_candidates = tuple(
            candidate_by_id[candidate_id]
            for candidate_id in item.candidate_ids
            if candidate_id in candidate_by_id
        )
        if len(bindings) == len(
            set(item.evidence_refs)
        ) and item.scope_sha256 != compute_scope_sha256(subject, item, scoped_candidates, bindings):
            errors.append(f"intent scope hash mismatch: {item.intent_id}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(intent_id: str) -> None:
        if intent_id in visiting:
            errors.append(f"intent dependency cycle at: {intent_id}")
            return
        if intent_id in visited:
            return
        visiting.add(intent_id)
        for dependency in intent_by_id[intent_id].depends_on:
            if dependency in intent_by_id:
                visit(dependency)
        visiting.remove(intent_id)
        visited.add(intent_id)

    for intent_id in sorted(intent_by_id):
        visit(intent_id)
    claims: defaultdict[str, list[str]] = defaultdict(list)
    for item in plan.intents:
        for candidate_id in item.candidate_ids:
            claims[candidate_id].append(item.intent_id)
    for candidate in plan.candidates:
        source = census_by_id.get(candidate.candidate_id)
        if source is None:
            errors.append(f"candidate is absent from native census: {candidate.candidate_id}")
            continue
        if (
            (candidate.population, candidate.source_identity)
            != (source.population, source.source_identity)
            or candidate.baseline_noncomment_loc != source.baseline_noncomment_loc
            or (candidate.baseline_fan_out != source.baseline_fan_out)
        ):
            errors.append(f"candidate differs from native census: {candidate.candidate_id}")
        intent = intent_by_id.get(candidate.covered_by)
        if intent is None or candidate.candidate_id not in intent.candidate_ids:
            errors.append(f"dangling covered_by: {candidate.candidate_id}")
        elif (
            candidate.owner_issue != intent.owner_issue
            or candidate.lane != intent.lane
            or candidate.scope_sha256 != intent.scope_sha256
            or set(candidate.dependencies) != set(intent.depends_on)
            or set(candidate.resources) != set(intent.resources)
        ):
            errors.append(f"candidate and intent contract differ: {candidate.candidate_id}")
        allowed_claims = 2 if candidate.source_identity in COMPOSITION_ROOTS else 1
        if (
            len(claims[candidate.candidate_id]) < 1
            or len(claims[candidate.candidate_id]) > allowed_claims
        ):
            errors.append(f"candidate intent overlap: {candidate.candidate_id}")
        if candidate.owner_issue not in routes.get(candidate.population, set()):
            errors.append(f"owner is not routed for population: {candidate.candidate_id}")
        if (
            candidate.population == "admission"
            and admission_owners.get(candidate.source_identity) != candidate.owner_issue
        ):
            errors.append(f"admission owner differs from exact route: {candidate.candidate_id}")
        expected_evidence = POPULATION_EVIDENCE[candidate.population]
        if frozenset(candidate.evidence_refs) != expected_evidence:
            errors.append(f"candidate evidence does not match population: {candidate.candidate_id}")
        if not intent or not expected_evidence.issubset(intent.evidence_refs):
            errors.append(f"intent lacks candidate evidence: {candidate.candidate_id}")
        if intent and candidate.disposition not in NON_WORK_DISPOSITIONS:
            expected_action = {
                "large_module": candidate.disposition,
                "duplicate_authority": "deduplicate",
                "builder_invocation": candidate.disposition,
                "type_cluster": "type_remediation",
                "lifecycle": "lifecycle_review",
                "deletion": "retire",
                "admission": "admission_delivery",
                "scc_cut": "cycle_cut",
            }[candidate.population]
            if intent.action != expected_action:
                errors.append(f"candidate action contradicts disposition: {candidate.candidate_id}")
    missing = sorted(set(census_by_id) - set(candidate_by_id))
    if missing:
        holds.append(f"candidate census is unplanned ({len(missing)})")
    groups: defaultdict[tuple[str, str], int] = defaultdict(int)
    for intent in plan.intents:
        if intent.parallel_group:
            for resource in set(intent.resources):
                groups[(intent.parallel_group, resource)] += 1
    for (group, resource), count in sorted(groups.items()):
        if resource in resources and count > resources[resource].capacity:
            holds.append(f"resource collision: {group}/{resource}")
    large = [item for item in plan.candidates if item.population == "large_module"]
    crossings = tuple(
        sorted(
            item.source_identity
            for item in large
            if item.baseline_noncomment_loc is not None
            and item.baseline_noncomment_loc > 1000
            and item.disposition in {"split", "deduplicate", "retire"}
            and item.expected_post_loc is not None
            and item.expected_post_loc <= 1000
        )
    )
    required = max(
        0,
        sum(
            row.population == "large_module"
            and row.baseline_noncomment_loc is not None
            and row.baseline_noncomment_loc > 1000
            for row in census
        )
        - TARGET_LARGE_MODULES,
    )
    if len(crossings) < required:
        holds.append("planned large-module crossings are incomplete")
    for root, cap in COMPOSITION_ROOTS.items():
        row = next((item for item in large if item.source_identity == root), None)
        if row is None:
            holds.append(f"protected root work is unplanned: {root}")
        elif row.expected_post_loc is None or row.expected_post_loc > cap:
            holds.append(f"protected root work is incomplete: {root}")
        elif (
            row.protected_root_covered_by is None
            or row.protected_root_covered_by == row.covered_by
            or row.protected_root_covered_by not in intent_by_id
            or row.candidate_id not in intent_by_id[row.protected_root_covered_by].candidate_ids
        ):
            errors.append(f"protected root extra intent is invalid: {root}")
        else:
            extra = intent_by_id[row.protected_root_covered_by]
            if (
                extra.owner_issue != row.owner_issue
                or extra.lane != row.lane
                or not ({row.covered_by, *row.dependencies}).issubset(extra.depends_on)
                or set(extra.resources) != set(row.resources)
                or not set(row.evidence_refs).issubset(extra.evidence_refs)
                or extra.action != "root_reduction"
            ):
                errors.append(f"protected root extra intent contract differs: {root}")
    if errors:
        raise FreezeInputError(errors[0])
    actionable_ids = {
        item.covered_by for item in plan.candidates if item.disposition not in NON_WORK_DISPOSITIONS
    } | {
        item.protected_root_covered_by
        for item in plan.candidates
        if item.protected_root_covered_by is not None
    }
    return holds, crossings, required, len(actionable_ids)


def _derive(
    repo_root: Path,
    subject: str,
    loaded: Mapping[EvidenceKey, LoadedInput],
    owners: OwnerSnapshot | None,
    plan: FreezePlan | None,
) -> tuple[tuple[CensusRow, ...], FreezeCoverage, tuple[str, ...]]:
    census = _census(repo_root, loaded, owners)
    holds = [f"missing evidence: {key}" for key in EVIDENCE_KEYS if key not in loaded]
    for key in EVIDENCE_KEYS:
        item = loaded.get(key)
        if item and item.oracle_status == "HOLD":
            holds.extend(f"{key}: {reason}" for reason in item.oracle_reasons)
    holds.append(
        "owner snapshot is missing"
        if owners is None
        else "typed owner-approval binding is unavailable"
    )
    crossings: tuple[str, ...] = ()
    large_count = sum(
        row.population == "large_module"
        and row.baseline_noncomment_loc is not None
        and row.baseline_noncomment_loc > 1000
        for row in census
    )
    required = max(0, large_count - TARGET_LARGE_MODULES)
    intents = 0
    if plan is None:
        holds.append("exact-subject reviewed plan is missing")
    else:
        plan_holds, crossings, required, intents = _plan_holds(
            repo_root, subject, plan, census, owners, loaded
        )
        holds.extend(plan_holds)
        referenced = {
            key for item in (*plan.intents, *plan.candidates) for key in item.evidence_refs
        }
        holds.extend(
            f"planned evidence is missing: {key}" for key in sorted(referenced - set(loaded))
        )
        if plan.performance.paired_current_evidence:
            holds.append("plan cannot self-attest paired performance evidence")
        holds.append("typed lane-capacity producer is unavailable")
        if plan.capacity.calendar_weeks is not None:
            holds.append("calendar declaration is unsupported without typed capacity evidence")
    holds.extend(
        (
            "typed SCC cut-set producer is unavailable",
            "typed type-cluster producer is unavailable",
            "typed deletion candidate producer is unavailable",
        )
    )
    coverage = FreezeCoverage(
        baseline_modules_over_1000=large_count,
        target_modules_over_1000=TARGET_LARGE_MODULES,
        required_net_reduction=required,
        planned_crossing_paths=crossings,
        planned_net_reduction=len(crossings),
        observed_delivered_net_reduction=None,
        unplanned_net_reduction=max(0, required - len(crossings)),
        protected_root_intents=tuple(
            sorted(
                {
                    item.protected_root_covered_by
                    for item in (plan.candidates if plan else ())
                    if item.protected_root_covered_by is not None
                }
            )
        ),
        distinct_pr_intents=intents,
    )
    return census, coverage, tuple(dict.fromkeys(holds))


def build_freeze(
    repo_root: Path,
    input_paths: Mapping[str, Path],
    plan_path: Path | None = None,
    owner_snapshot_path: Path | None = None,
) -> FreezeReceipt:
    root = repo_root.resolve(strict=True)
    subject, tree = exact_subject(root)
    generator = _generator_hash(root, subject)
    unknown = sorted(set(input_paths) - set(EVIDENCE_KEYS))
    if unknown:
        raise FreezeInputError(f"unknown evidence key: {unknown[0]}")
    loaded: dict[EvidenceKey, LoadedInput] = {}
    for key in EVIDENCE_KEYS:
        path = input_paths.get(key)
        if path is not None:
            loaded[key] = load_evidence(root, key, path, subject)
    snapshot_path = owner_snapshot_path or Path("config/quality_roadmap_owners.json")
    if not snapshot_path.is_absolute():
        snapshot_path = root / snapshot_path
    snapshot_item: LoadedInput | None = None
    if snapshot_path.exists() or snapshot_path.is_symlink():
        snapshot_item = load_owner_snapshot(root, snapshot_path, subject)
    reject_aliases([*loaded.values(), *([snapshot_item] if snapshot_item else [])])
    snapshot = cast(OwnerSnapshot, snapshot_item.value) if snapshot_item else None
    plan: FreezePlan | None = None
    plan_item: LoadedJson | None = None
    plan_hash: str | None = None
    if plan_path is not None:
        plan_item = load_plan(root, plan_path)
        reject_aliases([*loaded.values(), *([snapshot_item] if snapshot_item else []), plan_item])
        try:
            plan = FreezePlan.model_validate_json(plan_item.raw)
        except ValidationError as exc:
            raise FreezeInputError("invalid FreezePlan") from exc
        if plan.subject_commit != subject:
            raise FreezeInputError("plan subject mismatch")
        plan_hash = plan_item.sha256
    census, coverage, reasons = _derive(root, subject, loaded, snapshot, plan)
    for item in loaded.values():
        assert_unchanged(item)
    if snapshot_item:
        assert_unchanged(snapshot_item)
    if plan_item:
        assert_unchanged(plan_item)
    final_subject, final_tree = exact_subject(root)
    if (final_subject, final_tree) != (subject, tree) or _generator_hash(
        root, subject
    ) != generator:
        raise FreezeInputError("subject or generator changed during freeze")
    snapshot_index = (
        OwnerSnapshotIndex(
            path=snapshot_item.relative_path,
            sha256=snapshot_item.sha256,
            source_document_id=snapshot.source_document_id,
            source_document_path=snapshot.source_document_path,
            source_sha256=snapshot.source_sha256,
            owners=snapshot.owners,
            population_routes=snapshot.population_routes,
            admission_routes=snapshot.admission_routes,
            oracle_status="HOLD",
            oracle_reasons=("typed owner-approval binding unavailable",),
        )
        if snapshot_item and snapshot
        else None
    )
    return FreezeReceipt(
        subject_commit=subject,
        subject_tree=tree,
        generator_sha256=generator,
        evidence=tuple(_index(key, loaded[key]) for key in EVIDENCE_KEYS if key in loaded),
        plan_path=plan_item.relative_path if plan_item else None,
        plan_sha256=plan_hash,
        plan_raw_json=plan_item.raw.decode("utf-8") if plan_item else None,
        plan=plan,
        owner_snapshot_sha256=snapshot_item.sha256 if snapshot_item else None,
        owner_snapshot=snapshot_index,
        candidate_census=census,
        coverage=coverage,
        artifact_status="HOLD" if reasons else "PASS",
        program_status="HOLD",
        hold_reasons=reasons,
    )


def verify_index_contents(
    repo_root: Path,
    receipt: FreezeReceipt,
    loaded: Mapping[EvidenceKey, LoadedInput],
    owners: OwnerSnapshot | None,
) -> None:
    """Replay derived index facts from already byte-verified native inputs."""
    census, expected_coverage, reasons = _derive(
        repo_root, receipt.subject_commit, loaded, owners, receipt.plan
    )
    if census != receipt.candidate_census:
        raise FreezeInputError("freeze candidate census differs from native inputs")
    expected_entries = tuple(_index(key, loaded[key]) for key in EVIDENCE_KEYS if key in loaded)
    if expected_entries != receipt.evidence:
        raise FreezeInputError("freeze evidence index differs from loaded inputs")
    if receipt.coverage != expected_coverage or receipt.hold_reasons != reasons:
        raise FreezeInputError("freeze derived coverage or HOLD reasons differ from replay")
    if receipt.artifact_status != ("HOLD" if reasons else "PASS"):
        raise FreezeInputError("freeze artifact status differs from replay")
    if (
        owners is not None
        and receipt.owner_snapshot is not None
        and (
            receipt.owner_snapshot.owners != owners.owners
            or receipt.owner_snapshot.population_routes != owners.population_routes
            or receipt.owner_snapshot.admission_routes != owners.admission_routes
        )
    ):
        raise FreezeInputError("freeze owner routes differ from verified snapshot")


__all__ = ["build_freeze", "compute_scope_sha256", "verify_index_contents"]
