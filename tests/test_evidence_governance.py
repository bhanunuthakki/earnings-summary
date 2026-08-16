"""Hermetic unit tests for evidence-governed judging and active enforcement."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.evidence_governance import (
    EvidenceJudgeEnforcer,
    EvidenceJudgeStatus,
    EvidenceVerificationContract,
    JudgeMode,
    JudgeTier,
    TaskPopulationAuditReceipt,
    TaskPopulationFrameAuditor,
    derive_statistical_sample_size,
)


def test_evidence_models_frozen_immutability() -> None:
    """Assert evidence contracts and receipts reject mutation and extra fields."""
    contract = EvidenceVerificationContract(
        evaluation_id="eval_1",
        task_id="BHA-30",
        tier=JudgeTier.J0_DETERMINISTIC,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        task_population_frame="frame_1",
        tolerable_error_rate=Decimal("0.05"),
        confidence_target=Decimal("0.95"),
        sample_count_derived=5,
        deterministic_checks_passed=True,
        status=EvidenceJudgeStatus.PASS,
        reason="OK",
        evaluated_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        contract.status = EvidenceJudgeStatus.BLOCK  # type: ignore[misc]

    receipt = TaskPopulationAuditReceipt(
        run_id="run_1",
        frame_name="frame_1",
        total_tasks_in_frame=5,
        tasks_with_valid_receipts=5,
        missing_receipt_tasks=(),
        is_population_complete=True,
        verified_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        receipt.is_population_complete = False  # type: ignore[misc]


def test_statistical_sample_size_derivation() -> None:
    """Assert sample size derives dynamically from TER and confidence targets."""
    # 1. Zero population -> 0
    assert derive_statistical_sample_size(0, Decimal("0.05"), Decimal("0.95")) == 0

    # 2. Small population (8 tasks) with 5% TER, 95% confidence -> bounded
    n_small = derive_statistical_sample_size(8, Decimal("0.05"), Decimal("0.95"))
    assert 1 <= n_small <= 8

    # 3. Large population (1000 tasks)
    n_large = derive_statistical_sample_size(1000, Decimal("0.05"), Decimal("0.95"))
    assert 200 <= n_large <= 400


def test_task_population_frame_auditor_detection() -> None:
    """Assert auditor correctly identifies unreceipted tasks and passes on complete frames."""
    auditor = TaskPopulationFrameAuditor(repo_root=Path("."))

    # Simulated receipts
    mock_receipts = [
        "2026-08-15_sol_frontier_audit_bha32.md",
        "2026-08-15_sol_frontier_audit_bha31.md",
        "2026-08-15_quality_judge_audit_bha35.md",
    ]

    # Incomplete frame
    audit_incomplete = auditor.audit_frame(
        frame_name="test_frame",
        expected_task_ids=["BHA-31", "BHA-32", "BHA-99_UNRECEIPTED"],
        receipt_filenames=mock_receipts,
    )
    assert audit_incomplete.is_population_complete is False
    assert audit_incomplete.missing_receipt_tasks == ("BHA-99_UNRECEIPTED",)
    assert audit_incomplete.tasks_with_valid_receipts == 2

    # Complete frame
    audit_complete = auditor.audit_frame(
        frame_name="test_frame",
        expected_task_ids=["BHA-31", "BHA-32", "BHA-35"],
        receipt_filenames=mock_receipts,
    )
    assert audit_complete.is_population_complete is True
    assert len(audit_complete.missing_receipt_tasks) == 0


def test_active_enforcement_j0_to_j3_gating() -> None:
    """Assert enforcer blocks unratified or sub-threshold tasks under active enforcement."""
    enforcer = EvidenceJudgeEnforcer()

    # 1. J0 Deterministic failure -> BLOCK
    j0_fail = enforcer.evaluate_task(
        task_id="BHA-FAIL",
        tier=JudgeTier.J0_DETERMINISTIC,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=10,
        deterministic_checks_passed=False,
    )
    assert j0_fail.status == EvidenceJudgeStatus.BLOCK

    # 2. J1 Missing sample receipts -> HOLD
    j1_hold = enforcer.evaluate_task(
        task_id="BHA-J1",
        tier=JudgeTier.J1_SHADOW_SAMPLE,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=10,
        deterministic_checks_passed=True,
        receipt_hashes=(),
    )
    assert j1_hold.status == EvidenceJudgeStatus.HOLD

    # 3. J2 Specialist score < 9.0 -> HOLD
    j2_hold = enforcer.evaluate_task(
        task_id="BHA-J2",
        tier=JudgeTier.J2_SPECIALIST_AUDIT,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=10,
        deterministic_checks_passed=True,
        specialist_score=Decimal("8.5"),
        receipt_hashes=("a" * 64,),
    )
    assert j2_hold.status == EvidenceJudgeStatus.HOLD

    # 4. J2 Specialist score >= 9.0 -> PASS
    j2_pass = enforcer.evaluate_task(
        task_id="BHA-J2-GOOD",
        tier=JudgeTier.J2_SPECIALIST_AUDIT,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=10,
        deterministic_checks_passed=True,
        specialist_score=Decimal("9.5"),
        receipt_hashes=("a" * 64,),
    )
    assert j2_pass.status == EvidenceJudgeStatus.PASS

    # 5. J3 Irreversible unratified -> BLOCK
    j3_block = enforcer.evaluate_task(
        task_id="BHA-J3",
        tier=JudgeTier.J3_IRREVERSIBLE_RATIFIED,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=10,
        deterministic_checks_passed=True,
        owner_ratification=False,
    )
    assert j3_block.status == EvidenceJudgeStatus.BLOCK

    # 6. J3 Irreversible ratified -> PASS
    j3_pass = enforcer.evaluate_task(
        task_id="BHA-J3-RATIFIED",
        tier=JudgeTier.J3_IRREVERSIBLE_RATIFIED,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=10,
        deterministic_checks_passed=True,
        specialist_score=Decimal("9.8"),
        owner_ratification=True,
        receipt_hashes=("b" * 64,),
    )
    assert j3_pass.status == EvidenceJudgeStatus.PASS


def test_shadow_mode_and_rollback() -> None:
    """Assert shadow mode passes without active blocking and rollback functions reliably."""
    enforcer = EvidenceJudgeEnforcer()

    shadow_eval = enforcer.evaluate_task(
        task_id="BHA-SHADOW",
        tier=JudgeTier.J2_SPECIALIST_AUDIT,
        mode=JudgeMode.SHADOW,
        population_size=10,
        deterministic_checks_passed=True,
        specialist_score=Decimal("7.0"),  # Sub-9 score passes in shadow mode
    )
    assert shadow_eval.status == EvidenceJudgeStatus.PASS
    assert shadow_eval.rollback_tested is True
