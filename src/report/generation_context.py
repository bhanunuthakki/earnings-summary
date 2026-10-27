"""Immutable generation identities shared by builders, readers and publication."""

from __future__ import annotations

from datetime import date
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from dcf.input_evidence import canonical_digest

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

# Same labels as ``report.artifacts.CoverageRole``; the shared owner cannot be
# reused here because ``artifacts`` imports this module (circular import).
ReportCoverageRole = Literal["portfolio", "evaluation", "unknown"]


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


class ReportCaptureUnavailable(BaseModel):
    """Typed capture refusal; makes no readiness or decision-grade claim.

    Capture (owned by a later tranche) returns this instead of a context when the
    explicitly selected verified research/verification context is missing
    (``selected_context_unavailable``) or when the explicitly requested boundary
    does not match the selected verification cutoff (``requested_cutoff_mismatch``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_code: Literal["selected_context_unavailable", "requested_cutoff_mismatch"]
    requested_cutoff_at: AwareDatetime | None = None
    evidence_snapshot_id: str | None = Field(default=None, min_length=1, max_length=256)


class ReportDependency(BaseModel):
    """One consumed input, with an explicit publication fence owner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "observation", "document", "transcript", "definition", "analysis", "recipe", "contract"
    ]
    identity: str = Field(min_length=1, max_length=1024)
    sha256: Sha256
    owner: Literal["immutable", "database", "dcf", "analysis", "source", "unavailable"]
    path: str | None = None

    @model_validator(mode="after")
    def safe_path(self) -> ReportDependency:
        if self.path is not None:
            path = PurePosixPath(self.path.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or ":" in self.path:
                raise ValueError("report dependency paths must be relative")
        return self


class DBSnapshotRef(BaseModel):
    """Identity of the DB read snapshot a report consumed; never a DB path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_sha256: Sha256
    consumed_rows_sha256: Sha256


class ReportDirectoryDependency(BaseModel):
    """One consumed directory selection, with the producer fence actually held."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=1024)
    selector: str = Field(min_length=1, max_length=256)
    selected_member: str | None = Field(default=None, min_length=1, max_length=1024)
    membership_sha256: Sha256
    producer: str = Field(min_length=1, max_length=256)
    fence: Literal["producer-lock", "exact-id", "unavailable"]

    @model_validator(mode="after")
    def safe_relative_path(self) -> ReportDirectoryDependency:
        path = PurePosixPath(self.relative_path.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or ":" in self.relative_path:
            raise ValueError("report directory dependency paths must be relative")
        return self


# Exact v1 serialization field set. ``ReportGenerationContext.sha256`` must keep
# digesting exactly these keys for ``report_generation.v1``; new keys default only
# under ``report_generation.v2`` and are excluded from the v1 digest.
_V1_CONTEXT_FIELDS = (
    "schema_version",
    "model",
    "cutoff_at",
    "source_manifest_sha256",
    "evidence_snapshot_id",
    "evidence_member_sha256",
    "dependencies",
    "sections",
    "recipe_versions",
    "expected_current_artifact_id",
)


class ReportGenerationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["report_generation.v1", "report_generation.v2"] = "report_generation.v1"
    model: ReportModelBinding
    cutoff_at: AwareDatetime
    source_manifest_sha256: Sha256
    evidence_snapshot_id: str | None = None
    evidence_member_sha256: Sha256 | None = None
    dependencies: tuple[ReportDependency, ...] = ()
    sections: tuple[str, ...] = ("full_brief",)
    recipe_versions: tuple[tuple[str, str], ...] = Field(min_length=1)
    expected_current_artifact_id: str | None = None
    # v2-only fields: a v1 context must leave all of them at their defaults.
    db_snapshot: DBSnapshotRef | None = None
    directory_dependencies: tuple[ReportDirectoryDependency, ...] = ()
    unfenced_consumed_dependencies: tuple[ReportDependency, ...] = ()
    # v2-only explicit semantic request inputs: never inferred from now, dbdir
    # or cwd. ``source_authority_sha256`` binds the explicit SourceReadContext
    # roots/limits and is derived only by actual capture (a later tranche);
    # these types do not claim that derivation.
    report_date: date | None = None
    coverage_role: ReportCoverageRole | None = None
    source_authority_sha256: Sha256 | None = None

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
        directory_paths = tuple(item.relative_path for item in self.directory_dependencies)
        if len(set(directory_paths)) != len(directory_paths):
            raise ValueError("report directory dependency paths must be unique")
        consumed = {(item.kind, item.identity): item for item in self.dependencies}
        for item in self.unfenced_consumed_dependencies:
            match = consumed.get((item.kind, item.identity))
            if match is None:
                raise ValueError("unfenced report dependencies must be consumed dependencies")
            if match != item:
                raise ValueError("unfenced report dependencies must match consumed digests exactly")
        unfenced = tuple((item.kind, item.identity) for item in self.unfenced_consumed_dependencies)
        if len(set(unfenced)) != len(unfenced):
            raise ValueError("unfenced report dependency identities must be unique")
        if self.schema_version == "report_generation.v1":
            if self.db_snapshot is not None or self.directory_dependencies:
                raise ValueError(
                    "report generation v1 does not accept db/directory snapshot fields"
                )
            if self.unfenced_consumed_dependencies:
                raise ValueError("report generation v1 does not accept unfenced dependency fields")
            if any(item.owner == "source" for item in self.dependencies):
                raise ValueError("report generation v1 does not accept source-owned dependencies")
            if (
                self.report_date is not None
                or self.coverage_role is not None
                or self.source_authority_sha256 is not None
            ):
                raise ValueError(
                    "report generation v1 does not accept report date/coverage/authority inputs"
                )
        else:
            # A v2 verification-ready context binds non-missing paired evidence,
            # the consumed DB snapshot identity and the complete explicit
            # semantic request inputs. An unknown cutoff can never appear:
            # ``cutoff_at`` is required and must be timezone-aware.
            if self.evidence_snapshot_id is None or self.evidence_member_sha256 is None:
                raise ValueError("report generation v2 requires non-missing evidence identity")
            if self.db_snapshot is None:
                raise ValueError("report generation v2 requires a db snapshot reference")
            if self.report_date is None:
                raise ValueError("report generation v2 requires an explicit report date")
            if self.coverage_role is None:
                raise ValueError("report generation v2 requires an explicit coverage role")
            if self.source_authority_sha256 is None:
                raise ValueError(
                    "report generation v2 requires an explicit source authority digest"
                )
        return self

    @property
    def sha256(self) -> str:
        payload = self.model_dump(mode="json")
        if self.schema_version == "report_generation.v1":
            payload = {key: payload[key] for key in _V1_CONTEXT_FIELDS}
        return canonical_digest(payload)

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

    @property
    def semantic_request_key(self) -> str:
        """Deterministic identity of the semantic request; diagnostics never enter.

        A v1 context cannot represent the complete request inputs (report date,
        coverage role, source authority), so this property fails closed for v1
        rather than minting a misleadingly complete key; the exact ``sha256``
        property remains the supported v1 fingerprint.
        Included: the explicit ``cutoff_at`` boundary, the explicit report
        date/coverage role/source authority digest, evidence snapshot/member
        identity, model binding, source manifest, every consumed dependency
        (capture order matters and is preserved), the unfenced-consumed
        selection flags (eligibility-relevant), the selected directory
        populations (sorted by relative path; directory iteration order is not
        semantic), recipes and sections (declared order matters), the expected
        current artifact, and the consumed DB snapshot identity.
        Excluded: any captured/generated/approval timestamps, absolute DB paths,
        staging paths and ``data_version`` counters; the context model carries no
        diagnostic fields, and capture-time diagnostics stay in a separate
        diagnostic receipt owned by the capture tranche. Two captures with
        different requested verification cutoffs never join even when every
        document hash matches.
        """
        if self.schema_version == "report_generation.v1":
            raise ValueError("report generation v1 cannot mint a complete semantic request key")
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "report_date": self.model_dump(mode="json")["report_date"],
                "coverage_role": self.coverage_role,
                "cutoff_at": self.model_dump(mode="json")["cutoff_at"],
                "source_authority_sha256": self.source_authority_sha256,
                "model": self.model.model_dump(mode="json"),
                "source_manifest_sha256": self.source_manifest_sha256,
                "evidence": {
                    "snapshot_id": self.evidence_snapshot_id,
                    "member_sha256": self.evidence_member_sha256,
                },
                "dependencies": [item.model_dump(mode="json") for item in self.dependencies],
                "unfenced_consumed_identities": sorted(
                    f"{item.kind}:{item.identity}" for item in self.unfenced_consumed_dependencies
                ),
                "directory_selections": [
                    {
                        "relative_path": item.relative_path,
                        "selector": item.selector,
                        "selected_member": item.selected_member,
                        "membership_sha256": item.membership_sha256,
                    }
                    for item in sorted(
                        self.directory_dependencies, key=lambda dep: dep.relative_path
                    )
                ],
                "recipe_versions": [list(item) for item in self.recipe_versions],
                "sections": list(self.sections),
                "expected_current_artifact_id": self.expected_current_artifact_id,
                "db_snapshot": (
                    self.db_snapshot.model_dump(mode="json")
                    if self.db_snapshot is not None
                    else None
                ),
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
    url: str | None = None

    @model_validator(mode="after")
    def no_ready_with_blockers(self) -> ReportCandidateReview:
        if self.ready and self.reason_codes:
            raise ValueError("a blocked candidate cannot be ready")
        return self
