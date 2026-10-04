"""Deterministic qualification of an independently run repair Judge seat.

This scores a fixed synthetic hazard set. It does not claim statistical
calibration or invoke a provider. Actual model/run evidence is caller supplied.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from operations.kpi_repair_receipts import KpiJudgePurposeQualification, canonical_sha256

Purpose = Literal["kpi_source_repair", "kpi_semantic_disposition"]
Verdict = Literal["PASS", "BLOCK", "HOLD", "ABSTAIN"]


class QualificationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str
    purpose: Purpose
    prompt: str = Field(min_length=1)
    expected_verdict: Verdict


class QualificationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["kpi_repair_judge_dataset.v1"]
    dataset_version: str
    cases: tuple[QualificationCase, ...]

    @model_validator(mode="after")
    def _coverage(self) -> QualificationDataset:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("qualification case identities must be unique")
        for purpose in ("kpi_source_repair", "kpi_semantic_disposition"):
            cases = [case for case in self.cases if case.purpose == purpose]
            if len(cases) < 12 or {case.expected_verdict for case in cases} != {
                "PASS",
                "BLOCK",
                "HOLD",
            }:
                raise ValueError("qualification requires complete repair hazard coverage")
        return self


class QualificationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str
    verdict: Verdict
    rationale: str = Field(min_length=1)


def qualify_repair_judge(
    dataset: QualificationDataset,
    answers: tuple[QualificationAnswer, ...],
    *,
    purpose: Purpose,
    model_id: str,
    run_evidence_sha256: str,
    evaluated_at: datetime,
) -> KpiJudgePurposeQualification:
    """Require complete exact expected verdicts, including abstention cases."""
    if evaluated_at.tzinfo is None:
        raise ValueError("qualification clock must be timezone-aware")
    expected_ids = {case.case_id for case in dataset.cases}
    if {answer.case_id for answer in answers} != expected_ids or len(answers) != len(expected_ids):
        raise ValueError("qualification answers must exactly cover the dataset")
    by_id = {answer.case_id: answer for answer in answers}
    cases = [case for case in dataset.cases if case.purpose == purpose]
    if any(by_id[case.case_id].verdict != case.expected_verdict for case in cases):
        raise ValueError("Judge failed the repair purpose qualification cases")
    payload = {
        "schema_version": "kpi_judge_purpose_qualification.v1",
        "purpose": purpose,
        "capability_role": "frontier-synthesizer",
        "model_id": model_id,
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": canonical_sha256(dataset.model_dump(mode="json")),
        "evaluator_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "run_evidence_sha256": run_evidence_sha256,
        "attempted_cases": len(cases),
        "passed_cases": len(cases),
        "required_cases": len(cases),
        "result": "passed",
        "evaluated_at": evaluated_at,
        "expires_at": evaluated_at + timedelta(days=30),
    }
    return KpiJudgePurposeQualification.model_validate(
        {**payload, "content_sha256": canonical_sha256(payload)}
    )
