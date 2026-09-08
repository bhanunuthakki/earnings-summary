from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from quality.architecture import build_architecture_receipt
from quality.roadmap_freeze import GENERATOR_PATHS, build_freeze, compute_scope_sha256
from quality.roadmap_freeze_inputs import FreezeInputError
from quality.roadmap_freeze_models import FreezePlan, FreezeReceipt
from quality.scoring import HARD_GATES, SCORE_BLOCKS


def _run(root: Path, *args: str) -> None:
    subprocess.run(args, cwd=root, check=True, capture_output=True)


def _fixture_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    source_root = Path(__file__).resolve().parents[1]
    for relative in GENERATOR_PATHS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / relative, target)
    for index in range(36):
        target = root / "src" / f"large_{index:02d}.py"
        target.write_text(
            "".join(f"VALUE_{line} = {line}\n" for line in range(1001)), encoding="utf-8"
        )
    roadmap = root / "docs/quality/quality-9plus-roadmap.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_text("# approved roadmap\n", encoding="utf-8")
    contract_test = root / "tests/test_contract.py"
    contract_test.parent.mkdir()
    contract_test.write_text("def test_contract():\n    assert True\n", encoding="utf-8")
    owner = root / "config/quality_roadmap_owners.json"
    owner.parent.mkdir(parents=True)
    populations = [
        "large_module",
        "scc_cut",
        "duplicate_authority",
        "builder_invocation",
        "type_cluster",
        "lifecycle",
        "deletion",
        "admission",
    ]
    admissions = [
        {
            "kind": "block",
            "key": key,
            "issue_id": "BHA-108",
            "delivery_issues": ["BHA-108"],
        }
        for key, _label, _points in SCORE_BLOCKS
    ] + [
        {
            "kind": "hard_gate",
            "key": key,
            "issue_id": "BHA-108",
            "delivery_issues": ["BHA-108"],
        }
        for key in HARD_GATES
    ]
    owner.write_text(
        json.dumps(
            {
                "schema_version": "quality-roadmap-owners/v1",
                "source_document_id": "approved-roadmap",
                "source_document_path": "docs/quality/quality-9plus-roadmap.md",
                "source_sha256": hashlib.sha256(roadmap.read_bytes()).hexdigest(),
                "owners": [{"issue_id": "BHA-108", "lane": "structure-request", "confirmed": True}],
                "population_routes": [
                    {"population": population, "owner_issues": ["BHA-108"]}
                    for population in populations
                ],
                "admission_routes": admissions,
            }
        ),
        encoding="utf-8",
    )
    _run(root, "git", "init", "-q")
    _run(root, "git", "config", "user.email", "test@example.invalid")
    _run(root, "git", "config", "user.name", "Test")
    _run(root, "git", "add", ".")
    _run(root, "git", "commit", "-qm", "fixture")
    staged = root / ".tmp/architecture.json"
    staged.parent.mkdir()
    receipt = build_architecture_receipt(root, "WORKTREE")
    staged.write_text(receipt.model_dump_json(indent=2), encoding="utf-8")
    return root, staged


def test_build_without_plan_exposes_complete_current_census_and_holds(tmp_path: Path) -> None:
    root, architecture = _fixture_repo(tmp_path)
    receipt = build_freeze(root, {"architecture": architecture})

    assert receipt.coverage.baseline_modules_over_1000 == 36
    assert receipt.coverage.required_net_reduction == 1
    assert receipt.coverage.observed_delivered_net_reduction is None
    assert len([row for row in receipt.candidate_census if row.population == "large_module"]) == 36
    assert len([row for row in receipt.candidate_census if row.population == "admission"]) == 28
    assert receipt.artifact_status == "HOLD"
    assert "exact-subject reviewed plan is missing" in receipt.hold_reasons


def _plan_for(
    receipt: FreezeReceipt, architecture: Path, *, dependency: str | None = None
) -> dict[str, object]:
    subject = receipt.subject_commit
    census = receipt.candidate_census
    candidates = []
    crossed = False
    groups: dict[str, list[str]] = {"architecture": [], "reconciliation": []}
    for row in census:
        evidence_key = "architecture" if row.population == "large_module" else "reconciliation"
        groups[evidence_key].append(row.candidate_id)
    for row in census:
        crossing = row.population == "large_module" and not crossed
        crossed = crossed or crossing
        evidence_key = "architecture" if row.population == "large_module" else "reconciliation"
        intent_id = f"intent-{evidence_key}"
        scope_hash = "a" * 64 if evidence_key == "architecture" else "b" * 64
        candidates.append(
            {
                **row.model_dump(),
                "partition": "reviewed",
                "disposition": "split" if crossing else "retain",
                "owner_issue": "BHA-108",
                "lane": "structure-request",
                "dependencies": [dependency]
                if dependency and evidence_key == "architecture"
                else [],
                "resources": ["repo"],
                "scope_sha256": scope_hash,
                "evidence_refs": [evidence_key],
                "covered_by": intent_id,
                "expected_post_loc": 1000 if crossing else None,
                "protected_root_covered_by": None,
            }
        )
    result: dict[str, object] = {
        "schema_version": "roadmap-freeze-plan/v1",
        "subject_commit": subject,
        "owners": [{"issue_id": "BHA-108", "lane": "structure-request", "confirmed": True}],
        "resources": [{"resource_id": "repo", "capacity": 1}],
        "intents": [
            {
                "intent_id": f"intent-{key}",
                "owner_issue": "BHA-108",
                "lane": "structure-request",
                "action": "split" if key == "architecture" else "admission_delivery",
                "intended_outcome": "deliver the enumerated candidate scope",
                "depends_on": [dependency] if dependency and key == "architecture" else [],
                "resources": ["repo"],
                "parallel_group": None,
                "scope_sha256": "a" * 64 if key == "architecture" else "b" * 64,
                "evidence_refs": [key],
                "acceptance_tests": ["tests/test_contract.py"],
                "candidate_ids": candidate_ids,
            }
            for key, candidate_ids in groups.items()
        ],
        "candidates": candidates,
        "performance": {
            "target_seconds": 510,
            "owner_issue": "BHA-104",
            "evidence_ref": None,
            "paired_current_evidence": False,
        },
        "capacity": {"measured_lane_capacity": False, "calendar_weeks": None},
    }
    plan = FreezePlan.model_validate_json(json.dumps(result))
    architecture_intent = plan.intents[0]
    architecture_candidates = tuple(
        item for item in plan.candidates if item.population == "large_module"
    )
    scope = compute_scope_sha256(
        subject,
        architecture_intent,
        architecture_candidates,
        {
            "architecture": (
                ".tmp/architecture.json",
                hashlib.sha256(architecture.read_bytes()).hexdigest(),
            )
        },
    )
    return plan.model_copy(
        update={
            "intents": (
                architecture_intent.model_copy(update={"scope_sha256": scope}),
                *plan.intents[1:],
            ),
            "candidates": tuple(
                item.model_copy(update={"scope_sha256": scope})
                if item.population == "large_module"
                else item
                for item in plan.candidates
            ),
        }
    ).model_dump(mode="json")


def test_plan_counts_distinct_intent_separately_from_crossings(tmp_path: Path) -> None:
    root, architecture = _fixture_repo(tmp_path)
    census_receipt = build_freeze(root, {"architecture": architecture})
    plan = root / ".tmp/plan.json"
    plan.write_text(json.dumps(_plan_for(census_receipt, architecture)), encoding="utf-8")

    receipt = build_freeze(root, {"architecture": architecture}, plan)

    assert receipt.coverage.planned_net_reduction == 1
    assert receipt.coverage.distinct_pr_intents == 2
    assert receipt.coverage.observed_delivered_net_reduction is None
    assert receipt.program_status == "HOLD"
    assert receipt.plan_raw_json == plan.read_text(encoding="utf-8")
    forged = receipt.model_dump(mode="json")
    forged["plan_raw_json"] = forged["plan_raw_json"] + " "
    with pytest.raises(ValueError, match="raw plan hash mismatch"):
        FreezeReceipt.model_validate_json(json.dumps(forged))


def test_plan_dependency_cycle_is_rejected(tmp_path: Path) -> None:
    root, architecture = _fixture_repo(tmp_path)
    census_receipt = build_freeze(root, {"architecture": architecture})
    plan = root / ".tmp/plan.json"
    plan.write_text(
        json.dumps(_plan_for(census_receipt, architecture, dependency="intent-architecture")),
        encoding="utf-8",
    )

    with pytest.raises(FreezeInputError, match="dependency cycle"):
        build_freeze(root, {"architecture": architecture}, plan)


def test_self_consistent_forged_scope_is_rejected(tmp_path: Path) -> None:
    root, architecture = _fixture_repo(tmp_path)
    census_receipt = build_freeze(root, {"architecture": architecture})
    value = FreezePlan.model_validate_json(json.dumps(_plan_for(census_receipt, architecture)))
    forged = value.model_copy(
        update={
            "intents": (
                value.intents[0].model_copy(update={"scope_sha256": "f" * 64}),
                *value.intents[1:],
            ),
            "candidates": tuple(
                item.model_copy(update={"scope_sha256": "f" * 64})
                if item.population == "large_module"
                else item
                for item in value.candidates
            ),
        }
    )
    plan = root / ".tmp/plan.json"
    plan.write_text(forged.model_dump_json(), encoding="utf-8")

    with pytest.raises(FreezeInputError, match="scope hash mismatch"):
        build_freeze(root, {"architecture": architecture}, plan)


def test_untracked_file_makes_subject_inexact(tmp_path: Path) -> None:
    root, architecture = _fixture_repo(tmp_path)
    (root / "unexpected.txt").write_text("untracked", encoding="utf-8")

    with pytest.raises(FreezeInputError, match="worktree must be clean"):
        build_freeze(root, {"architecture": architecture})


def test_forged_architecture_membership_is_rejected(tmp_path: Path) -> None:
    root, architecture = _fixture_repo(tmp_path)
    value = json.loads(architecture.read_text(encoding="utf-8"))
    value["metrics"]["modules_over_1000_loc"] = 35
    architecture.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(FreezeInputError, match="native exact-subject oracle"):
        build_freeze(root, {"architecture": architecture})


def test_symlinked_staged_evidence_is_rejected(tmp_path: Path) -> None:
    root, architecture = _fixture_repo(tmp_path)
    alias = root / ".tmp/alias.json"
    alias.symlink_to(architecture.name)

    with pytest.raises(FreezeInputError, match="symlink"):
        build_freeze(root, {"architecture": alias})
