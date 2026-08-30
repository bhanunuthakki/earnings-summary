"""Seal an attributable Sol judgment for one KPI disposition dry run."""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from operations.kpi_repair_receipts import (  # noqa: E402
    KpiDispositionAttemptReceipt,
    canonical_sha256,
    repair_executor_code_sha256,
    seal_disposition_judgment,
)


class SolDispositionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: Literal["kpi_semantic_disposition"]
    rubric_version: str = Field(min_length=1, max_length=80)
    evidence_tier: Literal["J2", "J3"]
    verdict: Literal["PASS", "BLOCK", "HOLD", "ABSTAIN"]
    findings: tuple[str, ...] = ()
    issued_at: datetime

    @field_validator("issued_at")
    @classmethod
    def _issued_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Sol decision issued_at must be timezone-aware")
        return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run-receipt", type=Path, required=True)
    parser.add_argument("--judge-run-id", required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--response-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    dry_run = KpiDispositionAttemptReceipt.model_validate_json(
        args.dry_run_receipt.read_text(encoding="utf-8")
    )
    if dry_run.mode != "dry_run" or dry_run.state != "passed":
        raise ValueError("Sol judgment requires a passed deterministic dry-run receipt")
    prompt = args.prompt_file.read_text(encoding="utf-8")
    response = args.response_file.read_text(encoding="utf-8")
    decision = SolDispositionDecision.model_validate_json(response)
    current_code_sha = repair_executor_code_sha256(PROJECT_ROOT)
    if dry_run.executor_code_sha256 != current_code_sha:
        raise ValueError("disposition code changed after deterministic dry run")
    issuance_identity = canonical_sha256(
        {
            "judge_model": "gpt-5.6-sol",
            "judge_run_id": args.judge_run_id,
            "decision": decision.model_dump(mode="json"),
        }
    )
    receipt = seal_disposition_judgment(
        manifest_sha256=dry_run.manifest_sha256,
        dry_run_receipt_sha256=dry_run.content_sha256,
        review_bundle_sha256=dry_run.review_bundle_sha256,
        executor_code_sha256=current_code_sha,
        purpose=decision.purpose,
        rubric_version=decision.rubric_version,
        evidence_tier=decision.evidence_tier,
        judge_model="gpt-5.6-sol",
        judge_run_id=args.judge_run_id,
        prompt_sha256=_sha256_text(prompt),
        response_sha256=_sha256_text(response),
        verdict=decision.verdict,
        findings=decision.findings,
        observed_at=decision.issued_at,
        issuance_identity_sha256=issuance_identity,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = receipt.model_dump_json(indent=2) + "\n"
    if args.output.exists() and args.output.read_text(encoding="utf-8") != encoded:
        raise ValueError("refusing to overwrite a different judge receipt")
    args.output.write_text(encoded, encoding="utf-8")
    print(receipt.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
