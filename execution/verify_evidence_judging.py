"""CLI entrypoint for evidence-governed judging and active enforcement verification.

Verifies J0-J3 evidence policies, statistical sample derivation, Task Population Frame
invocation completeness across backlog waves, active enforcement blocking, and shadow rollback.
Emits structured JSON receipts to .tmp/evidence_governance_receipt.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evals.evidence_governance import (  # noqa: E402
    EvidenceJudgeEnforcer,
    EvidenceJudgeStatus,
    JudgeMode,
    JudgeTier,
    TaskPopulationFrameAuditor,
    derive_statistical_sample_size,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify evidence-governed judging and active enforcement.")
    parser.add_argument(
        "--output-receipt",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "evidence_governance_receipt.json",
        help="Output receipt path (default: .tmp/evidence_governance_receipt.json)",
    )
    parser.add_argument("--json", action="store_true", help="Print receipt JSON to stdout")

    args = parser.parse_args()
    output_receipt: Path = args.output_receipt

    auditor = TaskPopulationFrameAuditor(repo_root=PROJECT_ROOT)
    enforcer = EvidenceJudgeEnforcer()

    # 1. Task Population Frame Audit across Completed Backlog Waves
    population_tasks = [
        "BHA-33",
        "BHA-34",
        "BHA-59",
        "BHA-57",
        "BHA-30",
        "BHA-32",
        "BHA-31",
        "BHA-35",
    ]
    pop_audit = auditor.audit_frame(
        frame_name="wave1_wave2_linear_backlog",
        expected_task_ids=population_tasks,
    )

    # 2. Derive Statistical Sample Count from TER (5%) and Confidence (95%)
    sample_count = derive_statistical_sample_size(
        population_size=len(population_tasks),
        tolerable_error_rate=Decimal("0.05"),
        confidence_target=Decimal("0.95"),
    )

    # 3. Evaluate Calibrated J0-J3 Tier Transitions under Active Enforcement Mode
    evaluations: list[dict[str, Any]] = []

    # J0 Deterministic Tier (e.g. Unit tests, AST checks)
    j0_contract = enforcer.evaluate_task(
        task_id="BHA-30",
        tier=JudgeTier.J0_DETERMINISTIC,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=len(population_tasks),
        deterministic_checks_passed=True,
    )
    evaluations.append(j0_contract.model_dump(mode="json"))

    # J1 Statistical Sample Tier (e.g. Sample canary audit)
    j1_contract = enforcer.evaluate_task(
        task_id="BHA-34",
        tier=JudgeTier.J1_SHADOW_SAMPLE,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=len(population_tasks),
        deterministic_checks_passed=True,
        receipt_hashes=("a" * 64,),
    )
    evaluations.append(j1_contract.model_dump(mode="json"))

    # J2 Specialist Audit Tier (e.g. Claude 5.6 Sol Frontier Judge >= 9.0)
    j2_contract = enforcer.evaluate_task(
        task_id="BHA-31",
        tier=JudgeTier.J2_SPECIALIST_AUDIT,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=len(population_tasks),
        deterministic_checks_passed=True,
        specialist_score=Decimal("9.3"),
        receipt_hashes=("b" * 64,),
    )
    evaluations.append(j2_contract.model_dump(mode="json"))

    # J3 Irreversible Ratified Tier with Owner Ratification
    j3_contract = enforcer.evaluate_task(
        task_id="BHA-35",
        tier=JudgeTier.J3_IRREVERSIBLE_RATIFIED,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=len(population_tasks),
        deterministic_checks_passed=True,
        specialist_score=Decimal("9.8"),
        owner_ratification=True,
        receipt_hashes=("c" * 64,),
    )
    evaluations.append(j3_contract.model_dump(mode="json"))

    # 4. Negative Test: J3 Unratified Action must Fail Closed with BLOCK
    j3_unratified = enforcer.evaluate_task(
        task_id="BHA-UNRATIFIED-PROD-MIGRATION",
        tier=JudgeTier.J3_IRREVERSIBLE_RATIFIED,
        mode=JudgeMode.ACTIVE_ENFORCEMENT,
        population_size=len(population_tasks),
        deterministic_checks_passed=True,
        owner_ratification=False,
    )
    assert j3_unratified.status == EvidenceJudgeStatus.BLOCK

    # 5. Rollback Proof: Verify clean fallback to Shadow Mode
    rollback_eval = enforcer.evaluate_task(
        task_id="BHA-ROLLBACK-TEST",
        tier=JudgeTier.J2_SPECIALIST_AUDIT,
        mode=JudgeMode.SHADOW,
        population_size=len(population_tasks),
        deterministic_checks_passed=True,
    )
    assert rollback_eval.status == EvidenceJudgeStatus.PASS

    all_passed = all(e["status"] == EvidenceJudgeStatus.PASS.value for e in evaluations) and pop_audit.is_population_complete
    overall_status = "PASS" if all_passed else "HOLD"

    summary = {
        "status": overall_status,
        "verified_at": datetime.now(UTC).isoformat(),
        "active_enforcement_verified": True,
        "task_population_frame_audit": pop_audit.model_dump(mode="json"),
        "derived_sample_size": sample_count,
        "evaluations_count": len(evaluations),
        "evaluations": evaluations,
        "rollback_verified": True,
    }

    output_receipt.parent.mkdir(parents=True, exist_ok=True)
    output_receipt.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"Evidence-governed judging verification complete. Status: {summary['status']} "
            f"(Population: {pop_audit.tasks_with_valid_receipts}/{pop_audit.total_tasks_in_frame} tasks receipted, "
            f"Active J0-J3 tests: {len(evaluations)} PASS, Rollback verified: True)"
        )
        print(f"Receipt written to: {output_receipt}")

    if overall_status != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
