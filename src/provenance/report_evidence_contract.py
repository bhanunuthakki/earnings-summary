"""Pure shared vocabulary and source plan for the existing integrated report.

The memo owner retains claim, model and analytical-coverage verification. These
immutable types let the provenance scope bind the same finite requirements
without importing report-generation code.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.deterministic import canonical_json


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


MELI_REPORT_EVIDENCE_RECIPE = "meli_integrated_report.v1"
ReportEvidenceRequirementName = Literal[
    "operating",
    "business",
    "commerce",
    "fintech",
    "credit",
    "geography",
    "financial_baseline",
    "assumptions_capital_needs",
    "downside_scenarios",
    "valuation_sensitivity",
]
MELI_REPORT_EVIDENCE_REQUIREMENTS = (
    "assumptions_capital_needs",
    "business",
    "commerce",
    "credit",
    "downside_scenarios",
    "financial_baseline",
    "fintech",
    "geography",
    "operating",
    "valuation_sensitivity",
)


class MemoReportSourceRequirement(_Closed):
    """Independently selected sources for one existing report requirement."""

    requirement: ReportEvidenceRequirementName
    evidence_node_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    rationale: str = Field(min_length=20)

    @model_validator(mode="after")
    def source_population(self) -> MemoReportSourceRequirement:
        if not (self.evidence_node_ids or self.observation_ids):
            raise ValueError("report_source_requirement_support_missing")
        for values in (self.evidence_node_ids, self.observation_ids):
            if values != tuple(sorted(set(values))) or any(not value.strip() for value in values):
                raise ValueError("report_source_requirement_membership_invalid")
        return self


class MemoReportSourcePopulation(_Closed):
    """A pre-generation source plan for the existing ten report requirements."""

    recipe: Literal["meli_integrated_report.v1"]
    requirements: tuple[MemoReportSourceRequirement, ...] = Field(min_length=10, max_length=10)
    population_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_population(self) -> MemoReportSourcePopulation:
        if (
            tuple(item.requirement for item in self.requirements)
            != MELI_REPORT_EVIDENCE_REQUIREMENTS
        ):
            raise ValueError("report_source_population_mismatch")
        payload = self.model_dump(mode="json", exclude={"population_sha256"})
        if self.population_sha256 != hashlib.sha256(canonical_json(payload).encode()).hexdigest():
            raise ValueError("report_source_population_commitment_mismatch")
        return self
