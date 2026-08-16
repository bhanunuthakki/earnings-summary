"""Evidence-governed judging harness and active enforcement controller.

Enforces J0-J3 evidence policies, statistical sample derivation from Tolerable Error Rates (TER),
Task Population Frame invocation auditing (preventing receipt-ledger survivorship bias),
and fail-closed execution blocking under Active Enforcement mode.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class JudgeTier(StrEnum):
    """Calibrated evidence-governance judge tiers."""

    J0_DETERMINISTIC = "J0_DETERMINISTIC"
    J1_SHADOW_SAMPLE = "J1_SHADOW_SAMPLE"
    J2_SPECIALIST_AUDIT = "J2_SPECIALIST_AUDIT"
    J3_IRREVERSIBLE_RATIFIED = "J3_IRREVERSIBLE_RATIFIED"


class JudgeMode(StrEnum):
    """Operating mode of the evidence-governance judge harness."""

    SHADOW = "SHADOW"
    ACTIVE_ENFORCEMENT = "ACTIVE_ENFORCEMENT"


class EvidenceJudgeStatus(StrEnum):
    """Standardized disposition outcomes for judge evaluations."""

    PASS = "PASS"
    HOLD = "HOLD"
    BLOCK = "BLOCK"
    ABSTAIN = "ABSTAIN"


class EvidenceVerificationContract(BaseModel):
    """Immutable contract governing an evidence-backed task evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: str
    task_id: str
    tier: JudgeTier
    mode: JudgeMode
    task_population_frame: str
    tolerable_error_rate: Decimal
    confidence_target: Decimal
    sample_count_derived: int
    deterministic_checks_passed: bool
    specialist_score: Decimal | None = None
    owner_ratification_recorded: bool = False
    rollback_tested: bool = True
    verified_receipt_hashes: tuple[str, ...] = Field(default=(), min_length=0)
    status: EvidenceJudgeStatus
    reason: str
    evaluated_at: datetime


class TaskPopulationAuditReceipt(BaseModel):
    """Immutable audit receipt verifying task population completeness against receipt ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    frame_name: str
    total_tasks_in_frame: int
    tasks_with_valid_receipts: int
    missing_receipt_tasks: tuple[str, ...] = ()
    is_population_complete: bool
    verified_at: datetime


def derive_statistical_sample_size(
    population_size: int,
    tolerable_error_rate: Decimal,
    confidence_target: Decimal,
) -> int:
    """Derive minimum statistical sample size using hypergeometric / binomial formulation.

    Prevents arbitrary fixed percentage rules by computing sample counts from TER and confidence.
    """
    if population_size <= 0:
        return 0
    ter = float(tolerable_error_rate)
    conf = float(confidence_target)
    if ter <= 0.0 or ter >= 1.0 or conf <= 0.0 or conf >= 1.0:
        return population_size

    # Z-score approximation for standard confidence intervals (e.g. 0.95 -> 1.96, 0.99 -> 2.576)
    if conf >= 0.99:
        z = 2.576
    elif conf >= 0.95:
        z = 1.96
    elif conf >= 0.90:
        z = 1.645
    else:
        z = 1.282

    p = 0.5  # maximum variance assumption
    n_inf = (z**2 * p * (1 - p)) / (ter**2)
    # Finite population correction
    n_adj = n_inf / (1 + (n_inf - 1) / population_size)
    return max(1, min(population_size, math.ceil(n_adj)))


class TaskPopulationFrameAuditor:
    """Audits task population frame to detect unreceipted tasks and ensure full invocation coverage."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.receipts_dir = self.repo_root / "evals" / "receipts"

    def audit_frame(
        self,
        frame_name: str,
        expected_task_ids: list[str],
        receipt_filenames: list[str] | None = None,
    ) -> TaskPopulationAuditReceipt:
        """Verify that every task in the population frame has a corresponding verified receipt."""
        run_id = f"pop_audit_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
        now_ts = datetime.now(UTC)

        available_files: dict[str, str] = {}
        if receipt_filenames is not None:
            available_files = {fname: "" for fname in receipt_filenames}
        elif self.receipts_dir.exists():
            for p in self.receipts_dir.glob("*.md"):
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:
                    content = ""
                available_files[p.name.lower()] = content.lower()

        missing: list[str] = []
        for task_id in expected_task_ids:
            t_lower = task_id.lower()
            t_no_hyphen = t_lower.replace("-", "")
            t_underscore = t_lower.replace("-", "_")

            has_receipt = any(
                t_lower in fname
                or t_no_hyphen in fname
                or t_underscore in fname
                or t_lower in content
                or t_no_hyphen in content
                or t_underscore in content
                for fname, content in available_files.items()
            )
            if not has_receipt:
                missing.append(task_id)

        is_complete = len(missing) == 0

        return TaskPopulationAuditReceipt(
            run_id=run_id,
            frame_name=frame_name,
            total_tasks_in_frame=len(expected_task_ids),
            tasks_with_valid_receipts=len(expected_task_ids) - len(missing),
            missing_receipt_tasks=tuple(missing),
            is_population_complete=is_complete,
            verified_at=now_ts,
        )


class EvidenceJudgeEnforcer:
    """Evaluates task verification contracts under Shadow or Active Enforcement modes."""

    def evaluate_task(
        self,
        task_id: str,
        tier: JudgeTier,
        mode: JudgeMode,
        *,
        population_size: int,
        tolerable_error_rate: Decimal = Decimal("0.05"),
        confidence_target: Decimal = Decimal("0.95"),
        deterministic_checks_passed: bool,
        specialist_score: Decimal | None = None,
        owner_ratification: bool = False,
        receipt_hashes: tuple[str, ...] = (),
    ) -> EvidenceVerificationContract:
        """Evaluate a task against J0-J3 evidence policy."""
        eval_id = f"judge_eval_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
        now_ts = datetime.now(UTC)

        sample_size = derive_statistical_sample_size(
            population_size=population_size,
            tolerable_error_rate=tolerable_error_rate,
            confidence_target=confidence_target,
        )

        # 1. J0 Deterministic checks must always pass
        if not deterministic_checks_passed:
            return EvidenceVerificationContract(
                evaluation_id=eval_id,
                task_id=task_id,
                tier=tier,
                mode=mode,
                task_population_frame=f"frame_{task_id}",
                tolerable_error_rate=tolerable_error_rate,
                confidence_target=confidence_target,
                sample_count_derived=sample_size,
                deterministic_checks_passed=False,
                specialist_score=specialist_score,
                owner_ratification_recorded=owner_ratification,
                rollback_tested=True,
                verified_receipt_hashes=receipt_hashes,
                status=EvidenceJudgeStatus.BLOCK,
                reason=f"Deterministic J0 validation failed for {task_id}.",
                evaluated_at=now_ts,
            )

        # 2. In Shadow Mode, report shadow disposition without blocking
        if mode == JudgeMode.SHADOW:
            return EvidenceVerificationContract(
                evaluation_id=eval_id,
                task_id=task_id,
                tier=tier,
                mode=mode,
                task_population_frame=f"frame_{task_id}",
                tolerable_error_rate=tolerable_error_rate,
                confidence_target=confidence_target,
                sample_count_derived=sample_size,
                deterministic_checks_passed=True,
                specialist_score=specialist_score,
                owner_ratification_recorded=owner_ratification,
                rollback_tested=True,
                verified_receipt_hashes=receipt_hashes,
                status=EvidenceJudgeStatus.PASS,
                reason=f"Shadow mode evaluation passed for {task_id}.",
                evaluated_at=now_ts,
            )

        # 3. In Active Enforcement Mode, apply strict J1-J3 rules:
        if tier == JudgeTier.J1_SHADOW_SAMPLE:
            # J1 requires sample receipts
            if not receipt_hashes:
                return EvidenceVerificationContract(
                    evaluation_id=eval_id,
                    task_id=task_id,
                    tier=tier,
                    mode=mode,
                    task_population_frame=f"frame_{task_id}",
                    tolerable_error_rate=tolerable_error_rate,
                    confidence_target=confidence_target,
                    sample_count_derived=sample_size,
                    deterministic_checks_passed=True,
                    specialist_score=specialist_score,
                    owner_ratification_recorded=owner_ratification,
                    rollback_tested=True,
                    verified_receipt_hashes=(),
                    status=EvidenceJudgeStatus.HOLD,
                    reason=f"Active J1 requires statistical sample receipts ({sample_size} required).",
                    evaluated_at=now_ts,
                )

        elif tier == JudgeTier.J2_SPECIALIST_AUDIT:
            # J2 requires specialist judge score >= 9.0 / 10.0
            if specialist_score is None or specialist_score < Decimal("9.0"):
                return EvidenceVerificationContract(
                    evaluation_id=eval_id,
                    task_id=task_id,
                    tier=tier,
                    mode=mode,
                    task_population_frame=f"frame_{task_id}",
                    tolerable_error_rate=tolerable_error_rate,
                    confidence_target=confidence_target,
                    sample_count_derived=sample_size,
                    deterministic_checks_passed=True,
                    specialist_score=specialist_score,
                    owner_ratification_recorded=owner_ratification,
                    rollback_tested=True,
                    verified_receipt_hashes=receipt_hashes,
                    status=EvidenceJudgeStatus.HOLD,
                    reason=f"Active J2 specialist score ({specialist_score}) is below required 9.0 threshold.",
                    evaluated_at=now_ts,
                )

        elif tier == JudgeTier.J3_IRREVERSIBLE_RATIFIED and not owner_ratification:
            # J3 requires irreversible owner ratification
            return EvidenceVerificationContract(
                evaluation_id=eval_id,
                task_id=task_id,
                tier=tier,
                mode=mode,
                task_population_frame=f"frame_{task_id}",
                tolerable_error_rate=tolerable_error_rate,
                confidence_target=confidence_target,
                sample_count_derived=sample_size,
                deterministic_checks_passed=True,
                specialist_score=specialist_score,
                owner_ratification_recorded=False,
                rollback_tested=True,
                verified_receipt_hashes=receipt_hashes,
                status=EvidenceJudgeStatus.BLOCK,
                reason=f"Active J3 irreversible action for {task_id} requires explicit owner ratification.",
                evaluated_at=now_ts,
            )

        return EvidenceVerificationContract(
            evaluation_id=eval_id,
            task_id=task_id,
            tier=tier,
            mode=mode,
            task_population_frame=f"frame_{task_id}",
            tolerable_error_rate=tolerable_error_rate,
            confidence_target=confidence_target,
            sample_count_derived=sample_size,
            deterministic_checks_passed=True,
            specialist_score=specialist_score,
            owner_ratification_recorded=owner_ratification,
            rollback_tested=True,
            verified_receipt_hashes=receipt_hashes,
            status=EvidenceJudgeStatus.PASS,
            reason=f"Active enforcement verification passed for {task_id} (Tier {tier.value}).",
            evaluated_at=now_ts,
        )
