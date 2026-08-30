"""Typed, content-addressed receipts for source-reviewed KPI repairs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import to_jsonable_python

_SHA256 = r"^[0-9a-f]{64}$"


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        to_jsonable_python(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def repair_executor_code_sha256(repo_root: Path) -> str:
    """Digest the whole approved Python code surface shared by dry run and apply.

    The repair crosses legacy triggers, evidence resolution, backup validation,
    locking, models, and receipt verification. Hashing every versioned Python
    source under those three runtime roots is deliberately conservative and
    prevents a transitive behavior change from reusing an earlier Sol approval.
    """
    paths = tuple(
        sorted(
            path.relative_to(repo_root).as_posix()
            for root in ("src", "execution", "alembic/versions")
            for path in (repo_root / root).rglob("*.py")
        )
    )
    digest = hashlib.sha256()
    for relative in paths:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        path = repo_root / relative
        try:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            digest.update(b"MISSING")
    return digest.hexdigest()


class _Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KpiRepairAttemptReceipt(_Receipt):
    schema_version: Literal["kpi_repair_attempt.v2"] = "kpi_repair_attempt.v2"
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    logical_idempotency_key_sha256: str = Field(pattern=_SHA256)
    manifest_sha256: str = Field(pattern=_SHA256)
    review_bundle_sha256: str = Field(pattern=_SHA256)
    backup_restore_evidence_id: str = Field(pattern=_SHA256)
    executor_code_sha256: str = Field(pattern=_SHA256)
    mode: Literal["dry_run", "apply"]
    state: Literal["passed", "applied", "replayed", "blocked", "failed"]
    started_at: datetime
    completed_at: datetime
    validated_entries: int = Field(ge=0)
    inserted_fact_rows: int = Field(ge=0)
    inserted_context_rows: int = Field(ge=0)
    blocker_codes: tuple[str, ...] = ()
    result_fact_head_ids: tuple[int, ...] = ()
    content_sha256: str = Field(pattern=_SHA256)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("receipt timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _hash_matches(self) -> KpiRepairAttemptReceipt:
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != canonical_sha256(payload):
            raise ValueError("KPI repair attempt receipt hash mismatch")
        return self


class KpiRepairJudgeReceipt(_Receipt):
    schema_version: Literal["kpi_repair_judge.v2"] = "kpi_repair_judge.v2"
    manifest_sha256: str = Field(pattern=_SHA256)
    dry_run_receipt_sha256: str = Field(pattern=_SHA256)
    review_bundle_sha256: str = Field(pattern=_SHA256)
    executor_code_sha256: str = Field(pattern=_SHA256)
    purpose: Literal["kpi_source_repair"] = "kpi_source_repair"
    rubric_version: str = Field(min_length=1, max_length=80)
    evidence_tier: Literal["J2", "J3"]
    judge_model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    judge_run_id: str = Field(min_length=1, max_length=160)
    prompt_sha256: str = Field(pattern=_SHA256)
    response_sha256: str = Field(pattern=_SHA256)
    verdict: Literal["PASS", "BLOCK", "HOLD", "ABSTAIN"]
    findings: tuple[str, ...] = ()
    observed_at: datetime
    issuance_identity_sha256: str = Field(pattern=_SHA256)
    content_sha256: str = Field(pattern=_SHA256)

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("judge timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _hash_matches(self) -> KpiRepairJudgeReceipt:
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != canonical_sha256(payload):
            raise ValueError("KPI repair judge receipt hash mismatch")
        return self


class KpiDispositionAttemptReceipt(_Receipt):
    schema_version: Literal["kpi_disposition_attempt.v1"] = "kpi_disposition_attempt.v1"
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    logical_idempotency_key_sha256: str = Field(pattern=_SHA256)
    manifest_sha256: str = Field(pattern=_SHA256)
    review_bundle_sha256: str = Field(pattern=_SHA256)
    backup_restore_evidence_id: str = Field(pattern=_SHA256)
    executor_code_sha256: str = Field(pattern=_SHA256)
    mode: Literal["dry_run", "apply"]
    state: Literal["passed", "applied", "replayed", "blocked", "failed"]
    started_at: datetime
    completed_at: datetime
    validated_fact_dispositions: int = Field(ge=0)
    validated_reference_dispositions: int = Field(ge=0)
    inserted_context_rows: int = Field(ge=0)
    replayed_context_rows: int = Field(ge=0)
    inserted_reference_rows: int = Field(ge=0)
    replayed_reference_rows: int = Field(ge=0)
    blocker_codes: tuple[str, ...] = ()
    content_sha256: str = Field(pattern=_SHA256)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("disposition receipt timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _hash_matches(self) -> KpiDispositionAttemptReceipt:
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != canonical_sha256(payload):
            raise ValueError("KPI disposition attempt receipt hash mismatch")
        return self


class KpiDispositionJudgeReceipt(_Receipt):
    schema_version: Literal["kpi_disposition_judge.v1"] = "kpi_disposition_judge.v1"
    manifest_sha256: str = Field(pattern=_SHA256)
    dry_run_receipt_sha256: str = Field(pattern=_SHA256)
    review_bundle_sha256: str = Field(pattern=_SHA256)
    executor_code_sha256: str = Field(pattern=_SHA256)
    purpose: Literal["kpi_semantic_disposition"] = "kpi_semantic_disposition"
    rubric_version: str = Field(min_length=1, max_length=80)
    evidence_tier: Literal["J2", "J3"]
    judge_model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    judge_run_id: str = Field(min_length=1, max_length=160)
    prompt_sha256: str = Field(pattern=_SHA256)
    response_sha256: str = Field(pattern=_SHA256)
    verdict: Literal["PASS", "BLOCK", "HOLD", "ABSTAIN"]
    findings: tuple[str, ...] = ()
    observed_at: datetime
    issuance_identity_sha256: str = Field(pattern=_SHA256)
    content_sha256: str = Field(pattern=_SHA256)

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("judge timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _hash_matches(self) -> KpiDispositionJudgeReceipt:
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != canonical_sha256(payload):
            raise ValueError("KPI disposition judge receipt hash mismatch")
        return self


def seal_attempt(**values: object) -> KpiRepairAttemptReceipt:
    payload = {"schema_version": "kpi_repair_attempt.v2", **values}
    return KpiRepairAttemptReceipt.model_validate(
        {**payload, "content_sha256": canonical_sha256(payload)}
    )


def seal_judgment(**values: object) -> KpiRepairJudgeReceipt:
    payload = {"schema_version": "kpi_repair_judge.v2", **values}
    return KpiRepairJudgeReceipt.model_validate(
        {**payload, "content_sha256": canonical_sha256(payload)}
    )


def seal_disposition_attempt(**values: object) -> KpiDispositionAttemptReceipt:
    payload = {"schema_version": "kpi_disposition_attempt.v1", **values}
    return KpiDispositionAttemptReceipt.model_validate(
        {**payload, "content_sha256": canonical_sha256(payload)}
    )


def seal_disposition_judgment(**values: object) -> KpiDispositionJudgeReceipt:
    payload = {"schema_version": "kpi_disposition_judge.v1", **values}
    return KpiDispositionJudgeReceipt.model_validate(
        {**payload, "content_sha256": canonical_sha256(payload)}
    )
