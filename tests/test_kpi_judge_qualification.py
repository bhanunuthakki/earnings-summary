"""Model attribution and purpose coverage cannot be supplied by a model alias."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from operations.kpi_repair_receipts import (
    KpiRepairJudgeReceipt,
    canonical_sha256,
    judge_qualification_is_current,
    seal_judgment,
)
from tests.fixtures.kpi_judge_setup import qualification_fixture

NOW = datetime(2026, 10, 3, tzinfo=UTC)


def _receipt(*, current: bool = True) -> KpiRepairJudgeReceipt:
    fields: dict[str, object] = dict(
        manifest_sha256="a" * 64,
        dry_run_receipt_sha256="b" * 64,
        review_bundle_sha256="c" * 64,
        executor_code_sha256="d" * 64,
        purpose="kpi_source_repair",
        rubric_version="source-repair-v1",
        evidence_tier="J3",
        judge_model="synthetic-judge" if current else "gpt-5.6-sol",
        judge_run_id="synthetic-run",
        prompt_sha256="e" * 64,
        response_sha256="f" * 64,
        verdict="PASS",
        findings=(),
        observed_at=NOW,
        issuance_identity_sha256="1" * 64,
    )
    if current:
        fields["qualification"] = qualification_fixture("kpi_source_repair", NOW)
    return seal_judgment(**fields)


def test_current_model_attribution_requires_matching_purpose_qualification() -> None:
    receipt = _receipt()
    assert receipt.schema_version == "kpi_repair_judge.v3"
    assert judge_qualification_is_current(receipt, now=NOW)
    assert not judge_qualification_is_current(receipt, now=NOW + timedelta(days=366))
    for change in ({"judge_model": "different-model"}, {"qualification": None}):
        payload = {**receipt.model_dump(mode="json", exclude={"content_sha256"}), **change}
        with pytest.raises(ValidationError, match="qualification"):
            KpiRepairJudgeReceipt.model_validate(
                {**payload, "content_sha256": canonical_sha256(payload)}
            )


def test_historical_receipt_keeps_its_hash_and_cannot_grant_new_apply() -> None:
    receipt = _receipt(current=False)
    assert "qualification" not in receipt.model_dump(mode="json")
    assert KpiRepairJudgeReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    assert not judge_qualification_is_current(receipt, now=NOW)


def test_partial_case_coverage_cannot_qualify_a_model() -> None:
    receipt = qualification_fixture("kpi_source_repair", NOW)
    payload = receipt.model_dump(mode="json", exclude={"content_sha256"})
    payload["passed_cases"] = 7
    with pytest.raises(ValidationError, match="complete passing"):
        type(receipt).model_validate({**payload, "content_sha256": canonical_sha256(payload)})
