"""Closed NU historical earnings evidence; no bank model or alternate fact store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from provenance.immutable_artifact import read_stable_artifact
from provenance.metric_ontology import MetricOntology
from provenance.research_snapshot import ResearchSnapshotRequest
from report.artifacts import ReportArtifactRef, validate_report_artifact_path
from research.memo_report_evidence import DecisionBriefReadiness
from sources.report_financials import FinancialEvidenceReference, FinancialTableCell

if TYPE_CHECKING:
    from research.decision_brief import MemoCalculation, ReviewedMemoClaimV2


class NuEarningsEvidenceError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NuInferenceAnchor(_Closed):
    claim_block_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    anchor_block_ids: tuple[str, ...] = Field(min_length=1)


class NuFinancialComparison(_Closed):
    policy: Literal["nu-ifrs-quarter-comparison/v1"]
    claim_block_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    concept: Literal["revenue", "gross_profit", "net_income"]
    prior_period_end: Literal["2026-03-31", "2025-06-30"]
    rationale: str = Field(min_length=20)


class NuPreBriefCommitment(_Closed):
    artifact_manifest_path: str = Field(min_length=1)
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_review_path: str = Field(min_length=1)
    context_review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    readiness_path: str = Field(min_length=1)
    readiness_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    public_cutoff_at: AwareDatetime
    research_snapshot_id: str = Field(min_length=1)
    snapshot_member_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NuHistoricalEarningsContext(_Closed):
    recipe: Literal["nu-q2-2026-earnings/v1"]
    phase: Literal["pre", "post"]
    fiscal_period: Literal["Q2 2026"]
    valuation_mode: Literal["qualitative_only"]
    public_cutoff_at: AwareDatetime
    inference_anchors: tuple[NuInferenceAnchor, ...] = ()
    comparisons: tuple[NuFinancialComparison, ...] = ()
    pre_brief: NuPreBriefCommitment | None = None

    @model_validator(mode="after")
    def phase_population(self) -> NuHistoricalEarningsContext:
        ids = tuple(item.claim_block_id for item in self.inference_anchors)
        comparison_ids = tuple(item.claim_block_id for item in self.comparisons)
        if ids != tuple(sorted(set(ids))) or comparison_ids != tuple(sorted(set(comparison_ids))):
            raise ValueError("nu_earnings_claim_population_invalid")
        if any(
            item.anchor_block_ids != tuple(sorted(set(item.anchor_block_ids)))
            or item.claim_block_id in item.anchor_block_ids
            for item in self.inference_anchors
        ):
            raise ValueError("nu_earnings_inference_anchors_invalid")
        if self.phase == "pre" and (self.pre_brief is not None or self.comparisons):
            raise ValueError("nu_pre_cannot_claim_post_results")
        if self.phase == "pre" and not self.inference_anchors:
            raise ValueError("nu_pre_requires_source_anchored_expectations")
        if self.phase == "post" and (self.pre_brief is None or len(self.comparisons) < 3):
            raise ValueError("nu_post_requires_sealed_pre_and_comparisons")
        return self


def verify_nu_historical_context(
    context: NuHistoricalEarningsContext,
    artifact: ReportArtifactRef,
    snapshot: ResearchSnapshotRequest,
) -> None:
    analysis = snapshot.research_universe.analysis_scope
    historical = None if analysis is None else analysis.request.historical_reconstruction
    if (
        artifact.ticker != "NU"
        or artifact.fiscal_period_label != context.fiscal_period
        or analysis is None
        or historical is None
        or (
            analysis.request.readiness_profile is not None
            and (
                analysis.request.readiness_profile != "complete_report_evidence/v1"
                or analysis.schema_version != "analysis-evidence-scope/v6"
            )
        )
        or historical.public_witness_policy != "historical-public-witness/v1"
        or historical.public_cutoff_at != context.public_cutoff_at
        or not analysis.public_witnesses
        or not set(snapshot.research_universe.document_version_ids).issubset(
            {item.document_version_id for item in analysis.public_witnesses}
        )
    ):
        raise NuEarningsEvidenceError("nu_earnings_historical_source_context_required")
    if context.phase == "pre":
        if context.public_cutoff_at > datetime(2026, 8, 12, 23, 59, 59, tzinfo=UTC):
            raise NuEarningsEvidenceError("nu_pre_public_cutoff_after_approved_window")
        if historical.public_window is not None:
            raise NuEarningsEvidenceError("nu_pre_cannot_use_post_public_window")
    elif historical.public_window != "nu-q2-2026-post/v1":
        raise NuEarningsEvidenceError("nu_post_fixed_public_window_required")


def verify_nu_inference_anchors(
    context: NuHistoricalEarningsContext,
    claims: tuple[ReviewedMemoClaimV2, ...],
    verified_claim_ids: frozenset[str],
) -> None:
    by_id = {item.block_id: item for item in claims}
    inferred = {item.block_id for item in claims if item.kind == "analyst_inference"}
    if inferred != {item.claim_block_id for item in context.inference_anchors}:
        raise NuEarningsEvidenceError("nu_earnings_inference_anchor_population_mismatch")
    for item in context.inference_anchors:
        if item.claim_block_id not in verified_claim_ids:
            raise NuEarningsEvidenceError("nu_earnings_inference_not_verified")
        for key in item.anchor_block_ids:
            claim = by_id.get(key)
            if (
                claim is None
                or key not in verified_claim_ids
                or claim.kind not in {"reported_fact", "management_claim", "calculation"}
                or not (
                    claim.excerpts
                    or claim.evidence_node_ids
                    or claim.financial_references
                    or claim.values
                    or claim.calculation
                    or claim.observation_sum
                )
            ):
                raise NuEarningsEvidenceError("nu_earnings_inference_anchor_not_source_backed")


def verify_nu_comparison_operands(
    conn: sqlite3.Connection,
    comparison: NuFinancialComparison,
    calculation: MemoCalculation,
    cells: tuple[FinancialTableCell, ...],
) -> None:
    """Allow source-wording revisions only through admitted IFRS role/context."""
    if (
        comparison.policy != "nu-ifrs-quarter-comparison/v1"
        or calculation.operation != "growth"
        or len(cells) != 2
    ):
        raise NuEarningsEvidenceError("nu_earnings_comparison_requires_growth")
    if any(
        ref.ticker != "NU" or ref.concept != comparison.concept for ref in calculation.operands
    ) or any(
        not cell.available
        or cell.concept != comparison.concept
        or cell.cadence != "quarterly"
        or cell.source_scope_label != "consolidated"
        or cell.provenance is None
        for cell in cells
    ):
        raise NuEarningsEvidenceError("nu_earnings_comparison_role_unverified")
    bundles = [cell.provenance for cell in cells if cell.provenance is not None]
    identities = {
        (
            value.cell.reporting_entity_id,
            value.cell.scope_security_id,
            value.cell.currency,
            value.cell.unit_key,
            value.cell.accounting_basis,
            value.cell.consolidation_scope,
            tuple(item.model_dump_json() for item in value.cell.dimensions),
        )
        for value in bundles
    }
    if len(identities) != 1 or any(
        item.cell.accounting_basis != "ifrs"
        or item.cell.currency != "USD"
        or item.cell.unit_key != "USD"
        or item.cell.dimensions
        for item in bundles
    ):
        raise NuEarningsEvidenceError("nu_earnings_comparison_economic_scope_mismatch")
    expected = (
        (date(2026, 4, 1), date(2026, 6, 30), 2026, "Q2"),
        (date(2026, 1, 1), date(2026, 3, 31), 2026, "Q1")
        if comparison.prior_period_end == "2026-03-31"
        else (date(2025, 4, 1), date(2025, 6, 30), 2025, "Q2"),
    )
    for bundle, coordinate, cell, reference in zip(
        bundles, expected, cells, calculation.operands, strict=True
    ):
        source = bundle.cell
        if (
            source.period_start is None
            or (
                source.period_start.date(),
                source.period_end.date(),
                source.fiscal_year,
                source.fiscal_period,
            )
            != coordinate
        ):
            raise NuEarningsEvidenceError("nu_earnings_comparison_calendar_mismatch")
        definition = MetricOntology(conn).metric_definition_as_known(
            cell.metric_id, reference.as_of
        )
        reviews = (
            None
            if definition is None
            else definition.scope_constraints.get("financial_statement_reviews")
        )
        if (
            definition is None
            or definition.metric_definition_revision_id != reference.metric_definition_revision_id
            or definition.scope_constraints.get("financial_statement_concept") != comparison.concept
            or not isinstance(reviews, dict)
            or reference.observation_id not in reviews
        ):
            raise NuEarningsEvidenceError("nu_earnings_comparison_admitted_role_required")


def financial_reference_identity(reference: FinancialEvidenceReference) -> tuple[str, ...]:
    # Resolution revisions belong to each K/O context. The immutable reported
    # cell and Observation Version must remain identical across PRE and POST.
    return (
        reference.ticker,
        reference.concept,
        reference.canonical_metric_cell_id,
        reference.observation_id,
    )


def verify_nu_definition_successor(
    conn: sqlite3.Connection,
    previous: FinancialEvidenceReference,
    current: FinancialEvidenceReference,
) -> None:
    if financial_reference_identity(previous) != financial_reference_identity(current):
        raise NuEarningsEvidenceError("nu_post_baseline_financial_identity_changed")
    if previous.canonical_resolution_revision_id != current.canonical_resolution_revision_id:
        # Both immutable report contexts keep their own exact sealed resolution.
        # A refresh can change that revision, but cannot change its source fact.
        from sources.report_financials import FinancialTableCell, read_financial_evidence

        for reference in (previous, current):
            cell = read_financial_evidence(conn, reference)
            if (
                not isinstance(cell, FinancialTableCell)
                or cell.provenance is None
                or cell.provenance.evidence is None
                or cell.canonical_metric_cell_id != previous.canonical_metric_cell_id
                or cell.provenance.observation.observation_id != previous.observation_id
            ):
                raise NuEarningsEvidenceError("nu_post_baseline_resolution_refresh_unverified")
    if previous.metric_definition_revision_id == current.metric_definition_revision_id:
        return
    # Both native report-table references are separately replayed by the caller.
    rows = conn.execute(
        "WITH RECURSIVE lineage(id,parent,depth) AS ("
        "SELECT metric_definition_revision_id,supersedes_metric_definition_revision_id,0 "
        "FROM canonical_metric_definition_revisions WHERE metric_definition_revision_id=? "
        "UNION ALL SELECT d.metric_definition_revision_id,d.supersedes_metric_definition_revision_id,l.depth+1 "
        "FROM canonical_metric_definition_revisions d JOIN lineage l ON d.metric_definition_revision_id=l.parent "
        "WHERE l.depth<32) SELECT id FROM lineage",
        (current.metric_definition_revision_id,),
    ).fetchall()
    if previous.metric_definition_revision_id not in {str(row[0]) for row in rows}:
        raise NuEarningsEvidenceError("nu_post_baseline_definition_not_successor")
    original_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        pair = [
            conn.execute(
                "SELECT metric_id,lifecycle,definition_text,value_kind,period_kind,unit_family,accounting_basis,scope_constraints_json "
                "FROM canonical_metric_definition_revisions WHERE metric_definition_revision_id=?",
                (key,),
            ).fetchone()
            for key in (
                previous.metric_definition_revision_id,
                current.metric_definition_revision_id,
            )
        ]
    finally:
        conn.row_factory = original_factory
    if any(row is None for row in pair):
        raise NuEarningsEvidenceError("nu_post_baseline_definition_unavailable")
    before, after = (dict(row) for row in pair if row is not None)
    old_scope, new_scope = (
        json.loads(str(before.pop("scope_constraints_json"))),
        json.loads(str(after.pop("scope_constraints_json"))),
    )
    old_reviews, new_reviews = (
        old_scope.pop("financial_statement_reviews", {}),
        new_scope.pop("financial_statement_reviews", {}),
    )
    if not isinstance(old_reviews, dict) or not isinstance(new_reviews, dict):
        raise NuEarningsEvidenceError("nu_post_baseline_definition_semantics_changed")
    old_reviews = cast(dict[str, object], old_reviews)
    new_reviews = cast(dict[str, object], new_reviews)
    if (
        before != after
        or old_scope != new_scope
        or any(new_reviews.get(key) != value for key, value in old_reviews.items())
    ):
        raise NuEarningsEvidenceError("nu_post_baseline_definition_semantics_changed")


def load_nu_pre_brief(
    repo_root: Path, commitment: NuPreBriefCommitment
) -> tuple[ReportArtifactRef, bytes, DecisionBriefReadiness]:
    payloads: list[bytes] = []
    for relative, expected in (
        (commitment.artifact_manifest_path, commitment.artifact_manifest_sha256),
        (commitment.context_review_path, commitment.context_review_sha256),
        (commitment.readiness_path, commitment.readiness_sha256),
    ):
        target = repo_root / relative
        validate_report_artifact_path(repo_root, target)
        _snapshot, raw = read_stable_artifact(
            target, max_bytes=8 * 1024 * 1024, allowed_root=repo_root
        )
        if hashlib.sha256(raw).hexdigest() != expected:
            raise NuEarningsEvidenceError("nu_post_pre_artifact_changed")
        payloads.append(raw)
    artifact = ReportArtifactRef.model_validate_json(payloads[0])
    if artifact.ticker != "NU" or artifact.manifest_path != commitment.artifact_manifest_path:
        raise NuEarningsEvidenceError("nu_post_pre_artifact_identity_mismatch")
    directory = Path(artifact.manifest_path).parent
    if any(
        not Path(value).is_relative_to(directory)
        for value in (
            commitment.context_review_path,
            commitment.readiness_path,
        )
    ):
        raise NuEarningsEvidenceError("nu_post_pre_proof_outside_exact_artifact")
    readiness = DecisionBriefReadiness.model_validate_json(payloads[2])
    if (
        readiness.status != "verified"
        or readiness.reason_codes
        or readiness.decision_grade
        or readiness.readiness_scope != "historical_bounded"
        or readiness.valuation_run_id is not None
        or readiness.artifact_id != artifact.artifact_id
        or readiness.body_sha256 != artifact.body_sha256
        or readiness.research_snapshot_id != commitment.research_snapshot_id
        or readiness.snapshot_member_sha256 != commitment.snapshot_member_sha256
    ):
        raise NuEarningsEvidenceError("nu_post_pre_readiness_not_verified")
    return artifact, payloads[1], readiness


def verify_nu_post_baselines(
    conn: sqlite3.Connection,
    context: NuHistoricalEarningsContext,
    claims: tuple[ReviewedMemoClaimV2, ...],
    verified_claim_ids: frozenset[str],
    pre_claims: tuple[ReviewedMemoClaimV2, ...],
) -> None:
    prior_references = {
        financial_reference_identity(reference): reference
        for claim in pre_claims
        for reference in (
            *claim.financial_references,
            *(value.reference for value in claim.values),
            *(claim.calculation.operands if claim.calculation else ()),
        )
        if claim.kind in {"reported_fact", "management_claim", "calculation"}
    }
    by_id = {item.block_id: item for item in claims}
    coordinates: set[tuple[str, str]] = set()
    for comparison in context.comparisons:
        claim = by_id.get(comparison.claim_block_id)
        if (
            claim is None
            or claim.block_id not in verified_claim_ids
            or claim.kind != "calculation"
            or claim.calculation is None
            or len(claim.calculation.operands) != 2
        ):
            raise NuEarningsEvidenceError("nu_post_comparison_not_verified")
        previous = claim.calculation.operands[1]
        baseline = prior_references.get(financial_reference_identity(previous))
        if baseline is None:
            raise NuEarningsEvidenceError("nu_post_comparison_not_in_sealed_pre")
        verify_nu_definition_successor(conn, baseline, previous)
        coordinate = (comparison.concept, comparison.prior_period_end)
        if coordinate in coordinates:
            raise NuEarningsEvidenceError("nu_post_duplicate_financial_comparison")
        coordinates.add(coordinate)
    if {concept for concept, _period in coordinates} != {"revenue", "gross_profit", "net_income"}:
        raise NuEarningsEvidenceError("nu_post_material_comparison_population_incomplete")
