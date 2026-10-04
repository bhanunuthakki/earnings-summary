"""Synthetic qualification evidence; never production model qualification."""

from datetime import datetime, timedelta

from operations.kpi_repair_receipts import KpiJudgePurposeQualification, canonical_sha256


def qualification_fixture(purpose: str, observed_at: datetime) -> KpiJudgePurposeQualification:
    payload = {
        "schema_version": "kpi_judge_purpose_qualification.v1",
        "purpose": purpose,
        "capability_role": "frontier-synthesizer",
        "model_id": "synthetic-judge",
        "dataset_version": "synthetic-cases-v1",
        "dataset_sha256": "a" * 64,
        "evaluator_code_sha256": "b" * 64,
        "run_evidence_sha256": "c" * 64,
        "attempted_cases": 8,
        "passed_cases": 8,
        "required_cases": 8,
        "result": "passed",
        "evaluated_at": observed_at - timedelta(days=1),
        "expires_at": observed_at + timedelta(days=365),
    }
    return KpiJudgePurposeQualification.model_validate(
        {**payload, "content_sha256": canonical_sha256(payload)}
    )
