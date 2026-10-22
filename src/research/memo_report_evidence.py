"""Closed, versioned evidence-population proof for one integrated MELI report.

The existing memo owner verifies every block and its source/model support first.
This contract binds the analyst's analytical coverage mapping to those blocks.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from provenance.report_evidence_contract import (
    MELI_REPORT_EVIDENCE_RECIPE as MELI_REPORT_EVIDENCE_RECIPE,
)
from provenance.report_evidence_contract import (
    MELI_REPORT_EVIDENCE_REQUIREMENTS,
    ReportEvidenceRequirementName,
)
from provenance.report_evidence_contract import (
    MemoReportSourcePopulation as MemoReportSourcePopulation,
)
from provenance.report_evidence_contract import (
    MemoReportSourceRequirement as MemoReportSourceRequirement,
)
from research.memo_claim_support import MemoClaimSupportError
from runtime.deterministic import canonical_json

if TYPE_CHECKING:
    from provenance.analysis_scope import AnalysisScopeRequest
    from research.memo_model_evidence import MemoModelCommitmentReference


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@runtime_checkable
class _ScopedModelClaim(Protocol):
    @property
    def supporting_evidence(self) -> Sequence[object]: ...

    @property
    def model_values(self) -> Sequence[object]: ...


class _ReviewedClaim(Protocol):
    @property
    def block_id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def evidence_node_ids(self) -> Sequence[str]: ...

    @property
    def financial_references(self) -> Sequence[object]: ...

    @property
    def calculation(self) -> object | None: ...

    @property
    def values(self) -> Sequence[object]: ...


class _ReportReview(Protocol):
    @property
    def claims(self) -> Sequence[_ReviewedClaim]: ...

    @property
    def report_evidence(self) -> MemoReportEvidencePopulation | None: ...


@runtime_checkable
class _ModelReportReview(Protocol):
    @property
    def schema_version(self) -> str: ...

    @property
    def model(self) -> MemoModelCommitmentReference | None: ...


class MemoReportEvidenceRequirement(_Closed):
    requirement: ReportEvidenceRequirementName
    claim_block_ids: tuple[str, ...] = Field(min_length=1)
    rationale: str = Field(min_length=20)

    @model_validator(mode="after")
    def unique_claims(self) -> MemoReportEvidenceRequirement:
        if self.claim_block_ids != tuple(sorted(set(self.claim_block_ids))) or any(
            len(key) != 64 or any(c not in "0123456789abcdef" for c in key)
            for key in self.claim_block_ids
        ):
            raise ValueError("memo_report_evidence_claim_membership_invalid")
        if not self.rationale.strip():
            raise ValueError("memo_report_evidence_review_missing")
        return self


class MemoReportEvidencePopulation(_Closed):
    """Reviewed analytical coverage; labels alone do not prove semantic adequacy."""

    recipe: Literal["meli_integrated_report.v1"]
    requirements: tuple[MemoReportEvidenceRequirement, ...] = Field(min_length=1)
    population_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_population_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def exact_population(self) -> MemoReportEvidencePopulation:
        if (
            tuple(item.requirement for item in self.requirements)
            != MELI_REPORT_EVIDENCE_REQUIREMENTS
        ):
            raise ValueError("memo_report_evidence_population_mismatch")
        payload = self.model_dump(mode="json", exclude={"population_sha256"})
        if self.population_sha256 != hashlib.sha256(canonical_json(payload).encode()).hexdigest():
            raise ValueError("memo_report_evidence_commitment_mismatch")
        return self


def verify_memo_report_evidence(
    review: _ReportReview,
    *,
    scope_request: AnalysisScopeRequest | None,
    ticker: str,
    verified_claim_ids: frozenset[str],
    verified_model_claim_ids: frozenset[str] = frozenset(),
) -> MemoReportEvidencePopulation | None:
    """Require the fixed report population after unchanged whole-output checks.

    The review maps analytical requirements to grounded blocks. Its explicit
    rationales remain analyst judgments, not automated semantic completeness.
    """
    from research.memo_model_evidence import MeliMemoModelValue, MemoModelCommitmentReference

    if scope_request is None or scope_request.readiness_profile is None:
        if review.report_evidence is not None:
            raise MemoClaimSupportError("memo_report_evidence_profile_required")
        return None
    if ticker != "MELI":
        raise MemoClaimSupportError("memo_report_evidence_recipe_unsupported")
    if review.report_evidence is None:
        raise MemoClaimSupportError("memo_report_evidence_population_missing")
    try:
        proof = MemoReportEvidencePopulation.model_validate(review.report_evidence.model_dump())
    except ValueError as exc:
        raise MemoClaimSupportError("memo_report_evidence_population_invalid") from exc
    claims = {claim.block_id: claim for claim in review.claims}
    if len(claims) != len(review.claims):
        raise MemoClaimSupportError("memo_report_evidence_claim_membership_invalid")
    grounded_by_requirement: dict[str, tuple[str, ...]] = {}
    selected_by_requirement: dict[str, list[_ReviewedClaim]] = {}
    for item in proof.requirements:
        selected: list[_ReviewedClaim] = []
        for block_id in item.claim_block_ids:
            claim = claims.get(block_id)
            if claim is None or block_id not in verified_claim_ids or claim.kind == "presentation":
                raise MemoClaimSupportError("memo_report_evidence_claim_unverified")
            selected.append(claim)
        grounded = tuple(
            claim.block_id
            for claim in selected
            if claim.kind in {"reported_fact", "management_claim", "calculation"}
            and (
                claim.evidence_node_ids
                or claim.financial_references
                or claim.calculation
                or claim.values
            )
        )
        if not grounded:
            raise MemoClaimSupportError("memo_report_evidence_grounded_support_missing")
        grounded_by_requirement[item.requirement] = grounded
        selected_by_requirement[item.requirement] = selected
        if item.requirement == "assumptions_capital_needs" and not any(
            claim.kind == "analyst_inference"
            or (isinstance(claim, _ScopedModelClaim) and claim.model_values)
            for claim in selected
        ):
            raise MemoClaimSupportError("memo_report_evidence_analyst_or_model_support_missing")

    # A full bipartite matching allows shared declared sets without letting one
    # fact stand in for ten topics. At most ten requirements can be visited.
    assigned: dict[str, str] = {}

    def assign(requirement: str, visited: set[str]) -> bool:
        for block_id in grounded_by_requirement[requirement]:
            if block_id in visited:
                continue
            visited.add(block_id)
            previous = assigned.get(block_id)
            if previous is None or assign(previous, visited):
                assigned[block_id] = requirement
                return True
        return False

    if not all(assign(item.requirement, set()) for item in proof.requirements):
        raise MemoClaimSupportError("memo_report_evidence_distinct_grounding_missing")
    if (
        not isinstance(review, _ModelReportReview)
        or review.schema_version != "memo_context_review.v2"
        or review.model is None
    ):
        raise MemoClaimSupportError("memo_report_evidence_model_support_missing")
    try:
        commitment: MemoModelCommitmentReference = TypeAdapter[MemoModelCommitmentReference](
            MemoModelCommitmentReference
        ).validate_python(review.model.model_dump())
    except ValueError as exc:
        raise MemoClaimSupportError("memo_report_evidence_model_support_missing") from exc
    if str(commitment.ticker) != ticker:
        raise MemoClaimSupportError("memo_report_evidence_model_support_missing")

    def has_result(claim: _ReviewedClaim, requirement: str) -> bool:
        if not isinstance(claim, _ScopedModelClaim):
            return False
        for value in claim.model_values:
            if not isinstance(value, MeliMemoModelValue):
                continue
            if requirement == "downside_scenarios" and (
                value.source in {"scenario", "scenario_row"} and value.scenario == "bear"
            ):
                return True
            if requirement == "valuation_sensitivity" and (
                value.source == "sensitivity"
                and value.key == "vps"
                and (value.wacc_delta != "0" or value.credit_ke_delta != "0")
            ):
                return True
        return False

    for requirement in ("downside_scenarios", "valuation_sensitivity"):
        if not any(
            has_result(claim, requirement) and claim.block_id in verified_model_claim_ids
            for claim in selected_by_requirement[requirement]
        ):
            raise MemoClaimSupportError("memo_report_evidence_model_support_missing")
    return proof


class DecisionBriefReadiness(_Closed):
    schema_version: Literal["decision_brief_readiness.v1"] = "decision_brief_readiness.v1"
    artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,191}$")
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,31}$")
    body_sha256: str | None
    evaluated_at: AwareDatetime
    status: Literal["verified", "degraded"] = "degraded"
    decision_grade: bool = False
    reason_codes: tuple[str, ...]
    research_snapshot_id: str | None = None
    snapshot_member_sha256: str | None = None
    financial_reference_count: int = 0
    reconstructed_document_count: int = 0
    valuation_run_id: int | None = None
    context_review_sha256: str | None = None
    readiness_profile: Literal["complete_report_evidence/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    readiness_scope: Literal["bounded_report", "historical_bounded"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    report_evidence_recipe: Literal["meli_integrated_report.v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    report_evidence_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    report_evidence_requirement_count: int | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    scope_limitation: str | None = Field(default=None, exclude_if=lambda value: value is None)
    joint_mvp_status: Literal["not_verified"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    report_evidence_population: MemoReportEvidencePopulation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def bounded_grade_invariant(self) -> DecisionBriefReadiness:
        if self.decision_grade and (
            self.readiness_profile is not None or self.readiness_scope is not None
        ):
            raise ValueError("bounded_brief_cannot_be_decision_grade")
        return self

    @property
    def report_evidence_qualified(self) -> bool:
        """Check receipt consistency; native assessment owns evidence verification."""
        if self.report_evidence_population is None:
            return False
        try:
            proof = MemoReportEvidencePopulation.model_validate(
                self.report_evidence_population.model_dump()
            )
        except ValueError:
            return False
        return (
            proof.population_sha256 == self.report_evidence_sha256
            and proof.recipe == self.report_evidence_recipe
            and len(proof.requirements) == self.report_evidence_requirement_count
            and self.status == "verified"
            and not self.reason_codes
            and not self.decision_grade
            and self.ticker == "MELI"
            and self.readiness_profile == "complete_report_evidence/v1"
            and self.readiness_scope == "bounded_report"
            and self.report_evidence_recipe == MELI_REPORT_EVIDENCE_RECIPE
            and self.report_evidence_requirement_count == len(MELI_REPORT_EVIDENCE_REQUIREMENTS)
            and all(
                value is not None
                and len(value) == 64
                and all(c in "0123456789abcdef" for c in value)
                for value in (
                    self.report_evidence_sha256,
                    self.context_review_sha256,
                    self.snapshot_member_sha256,
                )
            )
            and bool(self.research_snapshot_id)
            and bool(self.scope_limitation)
        )

    # Approval-bound portfolio fit and allocation are separate authorities.
    owner_thesis_required: Literal[False] = False
