"""Typed, content-addressed receipts for source-reviewed KPI repairs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
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


class KpiJudgePurposeQualification(_Receipt):
    """Attributable evaluation evidence for one model and repair purpose.

    The qualification artifact is supplied by the independent evaluation owner.
    Validation checks its sealed contract; it does not invent an evaluation or
    promote a provider candidate because the provider advertises capability.
    """

    schema_version: Literal["kpi_judge_purpose_qualification.v1"] = (
        "kpi_judge_purpose_qualification.v1"
    )
    purpose: Literal["kpi_source_repair", "kpi_semantic_disposition"]
    capability_role: Literal["frontier-synthesizer"] = "frontier-synthesizer"
    model_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(pattern=_SHA256)
    evaluator_code_sha256: str = Field(pattern=_SHA256)
    run_evidence_sha256: str = Field(pattern=_SHA256)
    attempted_cases: int = Field(gt=0)
    passed_cases: int = Field(gt=0)
    required_cases: int = Field(gt=0)
    result: Literal["passed"]
    evaluated_at: datetime
    expires_at: datetime
    content_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _qualification(self) -> KpiJudgePurposeQualification:
        if self.evaluated_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("qualification timestamps must be timezone-aware")
        if self.expires_at <= self.evaluated_at:
            raise ValueError("qualification expiry must follow evaluation")
        if not self.attempted_cases == self.passed_cases == self.required_cases:
            raise ValueError("qualification requires complete passing case coverage")
        if self.content_sha256 != canonical_sha256(
            self.model_dump(mode="json", exclude={"content_sha256"})
        ):
            raise ValueError("qualification receipt hash mismatch")
        return self


def _validate_judge_qualification(
    *,
    model: str,
    purpose: str,
    observed_at: datetime,
    qualification: KpiJudgePurposeQualification | None,
    legacy: bool,
) -> None:
    if legacy:
        if model != "gpt-5.6-sol" or qualification is not None:
            raise ValueError("historical judge receipt must retain its original model contract")
        return
    if qualification is None:
        raise ValueError("current judge receipt requires purpose qualification")
    if qualification.model_id != model or qualification.purpose != purpose:
        raise ValueError("judge qualification model or purpose mismatch")
    if not qualification.evaluated_at <= observed_at < qualification.expires_at:
        raise ValueError("judge purpose qualification is not current")


def judge_qualification_is_current(
    judge: KpiRepairJudgeReceipt | KpiDispositionJudgeReceipt, *, now: datetime
) -> bool:
    """Historical receipts reconstruct, but do not grant new apply authority."""
    qualification = judge.qualification
    return bool(
        now.tzinfo is not None
        and qualification is not None
        and qualification.model_id == judge.judge_model
        and qualification.purpose == judge.purpose
        and qualification.evaluated_at <= judge.observed_at <= now < qualification.expires_at
    )


class KpiRepairAttemptReceipt(_Receipt):
    schema_version: Literal["kpi_repair_attempt.v2", "kpi_repair_attempt.v3"] = (
        "kpi_repair_attempt.v2"
    )
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
    inserted_definition_rows: int = Field(default=0, ge=0)
    inserted_comparability_rows: int = Field(default=0, ge=0)
    blocker_codes: tuple[str, ...] = ()
    result_fact_head_ids: tuple[int, ...] = ()
    result_definition_revision_ids: tuple[str | None, ...] = ()
    result_definition_commitment_sha256s: tuple[str | None, ...] = ()
    content_sha256: str = Field(pattern=_SHA256)

    @field_validator("started_at", "completed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("receipt timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _hash_matches(self) -> KpiRepairAttemptReceipt:
        if self.schema_version == "kpi_repair_attempt.v2":
            if (
                self.inserted_definition_rows != 0
                or self.inserted_comparability_rows != 0
                or self.result_definition_revision_ids
                or self.result_definition_commitment_sha256s
            ):
                raise ValueError("v2 repair receipts cannot carry definition effects")
        elif len(self.result_definition_revision_ids) != len(self.result_fact_head_ids) or len(
            self.result_definition_commitment_sha256s
        ) != len(self.result_fact_head_ids):
            raise ValueError("v3 repair receipt definition results must align with fact heads")
        for revision_id, commitment in zip(
            self.result_definition_revision_ids,
            self.result_definition_commitment_sha256s,
            strict=True,
        ):
            if (revision_id is None) != (commitment is None):
                raise ValueError("definition result identity and commitment must both be present")
            if commitment is not None and (
                len(commitment) != 64
                or any(character not in "0123456789abcdef" for character in commitment)
            ):
                raise ValueError("definition result commitment must be a lowercase SHA-256")
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != canonical_sha256(payload):
            raise ValueError("KPI repair attempt receipt hash mismatch")
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned_contract(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        serialized = handler(self)
        if not isinstance(serialized, dict):
            raise TypeError("KPI repair receipt serializer must return an object")
        payload = cast("dict[str, object]", serialized).copy()
        if self.schema_version == "kpi_repair_attempt.v2":
            for field in (
                "inserted_definition_rows",
                "inserted_comparability_rows",
                "result_definition_revision_ids",
                "result_definition_commitment_sha256s",
            ):
                payload.pop(field, None)
        return payload


class KpiRepairJudgeReceipt(_Receipt):
    schema_version: Literal["kpi_repair_judge.v2", "kpi_repair_judge.v3"] = "kpi_repair_judge.v2"
    manifest_sha256: str = Field(pattern=_SHA256)
    dry_run_receipt_sha256: str = Field(pattern=_SHA256)
    review_bundle_sha256: str = Field(pattern=_SHA256)
    executor_code_sha256: str = Field(pattern=_SHA256)
    purpose: Literal["kpi_source_repair"] = "kpi_source_repair"
    rubric_version: str = Field(min_length=1, max_length=80)
    evidence_tier: Literal["J2", "J3"]
    judge_model: str = Field(default="gpt-5.6-sol", min_length=1, max_length=128)
    qualification: KpiJudgePurposeQualification | None = None
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
        _validate_judge_qualification(
            model=self.judge_model,
            purpose=self.purpose,
            observed_at=self.observed_at,
            qualification=self.qualification,
            legacy=self.schema_version == "kpi_repair_judge.v2",
        )
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != canonical_sha256(payload):
            raise ValueError("KPI repair judge receipt hash mismatch")
        return self

    @model_serializer(mode="wrap")
    def _historical_shape(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        payload = handler(self)
        if not isinstance(payload, dict):
            raise TypeError("judge serializer must return an object")
        result = cast("dict[str, object]", payload)
        if self.schema_version == "kpi_repair_judge.v2":
            result.pop("qualification", None)
        return result


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
    schema_version: Literal["kpi_disposition_judge.v1", "kpi_disposition_judge.v2"] = (
        "kpi_disposition_judge.v1"
    )
    manifest_sha256: str = Field(pattern=_SHA256)
    dry_run_receipt_sha256: str = Field(pattern=_SHA256)
    review_bundle_sha256: str = Field(pattern=_SHA256)
    executor_code_sha256: str = Field(pattern=_SHA256)
    purpose: Literal["kpi_semantic_disposition"] = "kpi_semantic_disposition"
    rubric_version: str = Field(min_length=1, max_length=80)
    evidence_tier: Literal["J2", "J3"]
    judge_model: str = Field(default="gpt-5.6-sol", min_length=1, max_length=128)
    qualification: KpiJudgePurposeQualification | None = None
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
        _validate_judge_qualification(
            model=self.judge_model,
            purpose=self.purpose,
            observed_at=self.observed_at,
            qualification=self.qualification,
            legacy=self.schema_version == "kpi_disposition_judge.v1",
        )
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != canonical_sha256(payload):
            raise ValueError("KPI disposition judge receipt hash mismatch")
        return self

    @model_serializer(mode="wrap")
    def _historical_shape(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        payload = handler(self)
        if not isinstance(payload, dict):
            raise TypeError("judge serializer must return an object")
        result = cast("dict[str, object]", payload)
        if self.schema_version == "kpi_disposition_judge.v1":
            result.pop("qualification", None)
        return result


def seal_attempt(**values: object) -> KpiRepairAttemptReceipt:
    v3_fields = {
        "inserted_definition_rows",
        "inserted_comparability_rows",
        "result_definition_revision_ids",
        "result_definition_commitment_sha256s",
    }
    schema_version = (
        "kpi_repair_attempt.v3"
        if any(field in values for field in v3_fields)
        else "kpi_repair_attempt.v2"
    )
    payload = {"schema_version": schema_version, **values}
    return KpiRepairAttemptReceipt.model_validate(
        {**payload, "content_sha256": canonical_sha256(payload)}
    )


def seal_judgment(**values: object) -> KpiRepairJudgeReceipt:
    payload = {
        "schema_version": "kpi_repair_judge.v3"
        if values.get("qualification") is not None
        else "kpi_repair_judge.v2",
        **values,
    }
    return KpiRepairJudgeReceipt.model_validate(
        {**payload, "content_sha256": canonical_sha256(payload)}
    )


def seal_disposition_attempt(**values: object) -> KpiDispositionAttemptReceipt:
    payload = {"schema_version": "kpi_disposition_attempt.v1", **values}
    return KpiDispositionAttemptReceipt.model_validate(
        {**payload, "content_sha256": canonical_sha256(payload)}
    )


def seal_disposition_judgment(**values: object) -> KpiDispositionJudgeReceipt:
    payload = {
        "schema_version": "kpi_disposition_judge.v2"
        if values.get("qualification") is not None
        else "kpi_disposition_judge.v1",
        **values,
    }
    return KpiDispositionJudgeReceipt.model_validate(
        {**payload, "content_sha256": canonical_sha256(payload)}
    )
