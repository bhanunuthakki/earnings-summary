"""Immutable generation identities shared by builders, readers and publication."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from dcf.input_evidence import canonical_digest

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReportModelBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_id: int = Field(gt=0, strict=True)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,31}$")
    family: str = Field(min_length=1, max_length=100)
    input_sha256: Sha256
    workbook_sha256: Sha256
    engine_version: str = Field(min_length=1, max_length=100)
    provenance_sha256: Sha256
    assumption_snapshot_sha256: Sha256


class ReportDependency(BaseModel):
    """One consumed input, with an explicit publication fence owner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "observation", "document", "transcript", "definition", "analysis", "recipe", "contract"
    ]
    identity: str = Field(min_length=1, max_length=1024)
    sha256: Sha256
    owner: Literal["immutable", "database", "dcf", "analysis", "unavailable"]
    path: str | None = None

    @model_validator(mode="after")
    def safe_path(self) -> ReportDependency:
        if self.path is not None:
            path = PurePosixPath(self.path.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or ":" in self.path:
                raise ValueError("report dependency paths must be relative")
        return self


class ReportGenerationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["report_generation.v1"] = "report_generation.v1"
    model: ReportModelBinding
    cutoff_at: AwareDatetime
    source_manifest_sha256: Sha256
    evidence_snapshot_id: str | None = None
    evidence_member_sha256: Sha256 | None = None
    dependencies: tuple[ReportDependency, ...] = ()
    sections: tuple[str, ...] = ("full_brief",)
    recipe_versions: tuple[tuple[str, str], ...] = Field(min_length=1)
    expected_current_artifact_id: str | None = None

    @model_validator(mode="after")
    def exact_population(self) -> ReportGenerationContext:
        identities = tuple((item.kind, item.identity) for item in self.dependencies)
        if len(set(identities)) != len(identities):
            raise ValueError("report dependency identities must be unique")
        if not self.sections or len(set(self.sections)) != len(self.sections):
            raise ValueError("report section selection must be nonempty and unique")
        if (self.evidence_snapshot_id is None) != (self.evidence_member_sha256 is None):
            raise ValueError("report evidence snapshot identity and hash must be paired")
        recipes = tuple(name for name, _version in self.recipe_versions)
        if len(set(recipes)) != len(recipes) or any(
            not name or not version for name, version in self.recipe_versions
        ):
            raise ValueError("report recipes require unique names and explicit versions")
        return self

    @property
    def sha256(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

    @property
    def model_sha256(self) -> str:
        return canonical_digest(self.model.model_dump(mode="json"))

    @property
    def evidence_sha256(self) -> str:
        return canonical_digest(
            {
                "snapshot": self.evidence_snapshot_id,
                "members": self.evidence_member_sha256,
                "sources": self.source_manifest_sha256,
                "dependencies": [item.model_dump(mode="json") for item in self.dependencies],
            }
        )


class ReportCandidateReview(BaseModel):
    """Exact content proof. This receipt does not grant publication eligibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["report_candidate_review.v1"] = "report_candidate_review.v1"
    artifact_id: str
    body_sha256: Sha256
    workspace_sha256: Sha256
    context_sha256: Sha256
    model_sha256: Sha256
    evidence_sha256: Sha256
    content_receipt_path: str
    content_receipt_sha256: Sha256
    verification_contract: str = Field(min_length=1)
    evaluated_at: AwareDatetime
    ready: bool
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def no_ready_with_blockers(self) -> ReportCandidateReview:
        if self.ready and self.reason_codes:
            raise ValueError("a blocked candidate cannot be ready")
        return self
