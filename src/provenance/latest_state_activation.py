"""Fail-closed admission and inventory for latest-state activation candidates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ask.sealed_retrieval import (
    PRODUCTION_SCOPE_REGISTRY_ID,
    PRODUCTION_SCOPE_SCHEMA_VERSION,
    PRODUCTION_SUPPORTED_COHORT,
)
from provenance.latest_governed_state import (
    LatestGovernedRefreshRequest,
    LatestGovernedStateError,
    refresh_latest_governed_state,
)
from provenance.scope_identity import RetrievalScope
from scope_identity import derive_retrieval_scope_id
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_SCHEMA_VERSION = "latest-governed-activation-candidate/v2"
_HEX = frozenset("0123456789abcdef")

# This fixed inventory spans the upstream governed inputs, cutover coordinates,
# Ask promotion, and all nine 0261 projection/audit tables. Missing planes are
# recorded explicitly rather than collapsed into a zero-row count.
GOVERNED_PLANE_NAMES = (
    "issuer_entities",
    "reporting_entities",
    "issuer_reporting_scope_revisions",
    "evidence_document_versions",
    "source_obligation_revisions",
    "source_inventory_snapshots",
    "source_inventory_snapshot_seals",
    "expected_documents",
    "expected_document_obligation_bindings",
    "fact_cells_v2",
    "fact_observations_v2",
    "source_observation_taxonomy_assertions",
    "fact_cell_canonical_binding_revisions",
    "source_fact_publications",
    "source_fact_publication_members",
    "source_fact_publication_seals",
    "source_fact_publication_stream",
    "filing_xbrl_extraction_dispositions",
    "filing_xbrl_extraction_disposition_seals",
    "ontology_snapshot_headers",
    "ontology_snapshot_members",
    "ontology_snapshot_seals",
    "canonical_fact_resolution_snapshot_seals",
    "canonical_fact_resolution_snapshot_scope_headers",
    "canonical_fact_resolution_snapshot_scope_members",
    "canonical_fact_resolution_snapshot_scope_seals",
    "canonical_fact_resolution_snapshot_watermarks",
    "canonical_fact_resolution_snapshot_members",
    "canonical_fact_projection_generations",
    "canonical_fact_projection_entries",
    "canonical_fact_projection_batches",
    "canonical_fact_projection_buckets",
    "canonical_fact_projection_seals",
    "canonical_fact_projection_scope_bindings",
    "document_processing_evidence_headers",
    "document_processing_evidence_members",
    "document_processing_evidence_seals",
    "document_processing_snapshot_headers",
    "document_processing_snapshot_members",
    "document_processing_snapshot_seals",
    "document_processing_disposition_headers",
    "document_processing_disposition_members",
    "document_processing_disposition_seals",
    "research_snapshot_headers",
    "research_snapshot_members",
    "research_snapshot_seals",
    "research_snapshot_universe_commitments",
    "search_corpus_manifests",
    "search_corpus_document_memberships",
    "search_lexical_chunks",
    "search_embedding_artifacts",
    "search_embedding_model_promotions",
    "search_projection_seals",
    "search_index_runs",
    "heterogeneous_retrieval_trace_headers",
    "heterogeneous_retrieval_trace_candidates",
    "heterogeneous_retrieval_trace_results",
    "heterogeneous_retrieval_trace_seals",
    "ask_retrieval_scope_promotions",
    "population_run_headers",
    "population_plane_receipts",
    "population_cutover_receipts",
    "latest_governed_refresh_runs",
    "latest_governed_refresh_stage",
    "latest_governed_refresh_receipts",
    "latest_governed_refresh_changes",
    "latest_governed_scope_heads",
    "latest_governed_fact_entries",
    "latest_governed_document_entries",
    "latest_governed_narrative_entries",
    "latest_governed_narrative_fts",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GovernedPlaneCount(_FrozenModel):
    plane_name: str
    row_count: int | None = Field(default=None, ge=0)


class CandidateFileIdentity(_FrozenModel):
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    modified_time_ns: int = Field(ge=0)
    changed_time_ns: int = Field(ge=0)


class CandidateArtifactSnapshot(_FrozenModel):
    identity: CandidateFileIdentity
    file_sha256: str

    @field_validator("file_sha256")
    @classmethod
    def _valid_file_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in _HEX for character in value):
            raise ValueError("artifact SHA-256 is malformed")
        return value


class CandidateSeal(_FrozenModel):
    canonical_bindings: int = Field(ge=0)
    database: str
    foreign_key_violations: int = Field(ge=0)
    quick_check: str
    revision: tuple[str, ...]
    sha256: str
    size_bytes: int = Field(ge=0)
    source_taxonomy_components: int = Field(ge=0)

    @field_validator("sha256")
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in _HEX for character in value):
            raise ValueError("seal SHA-256 is malformed")
        return value


class GovernedCandidateAudit(_FrozenModel):
    schema_version: Literal["latest-governed-activation-candidate/v2"] = _SCHEMA_VERSION
    database_path: str
    database_size_bytes: int = Field(ge=0)
    database_sha256: str
    database_identity_before: CandidateFileIdentity
    database_identity_after: CandidateFileIdentity
    seal_path: str
    seal_sha256: str
    seal_identity_before: CandidateFileIdentity
    seal_identity_after: CandidateFileIdentity
    alembic_revision: str
    quick_check: str
    integrity_check: str
    foreign_key_violation_count: int = Field(ge=0)
    schema_fingerprint_sha256: str
    report_sha256: str


class GovernedCandidateCoverageAudit(_FrozenModel):
    schema_version: Literal["latest-governed-candidate-coverage/v2"] = (
        "latest-governed-candidate-coverage/v2"
    )
    database_path: str
    database_sha256: str
    database_identity_before: CandidateFileIdentity
    database_identity_after: CandidateFileIdentity
    candidate_audit_receipt: str
    candidate_audit_file_sha256: str
    candidate_audit_report_sha256: str
    candidate_audit_identity_before: CandidateFileIdentity
    candidate_audit_identity_after: CandidateFileIdentity
    planes: tuple[GovernedPlaneCount, ...]
    report_sha256: str


class LatestStateActivationError(RuntimeError):
    """A candidate failed a non-negotiable activation admission gate."""


ScopeEligibilityStatus = Literal["eligible", "blocked", "intentionally_excluded"]


class ScopeEligibility(_FrozenModel):
    scope_revision_id: str
    scope_id: str
    source_scope_key: str
    issuer_id: str
    inclusion_state: str
    status: ScopeEligibilityStatus
    reason_codes: tuple[str, ...]
    reporting_entity_id: str | None = None
    ticker: str | None = None
    promotion_id: str | None = None
    population_receipt_set_sha256: str | None = None
    fact_projection_seal_sha256: str | None = None
    source_inventory_commitment_sha256: str | None = None
    narrative_bundle_commitment_sha256: str | None = None
    terminal_commitment: str | None = None
    blocker_detail: str | None = None
    blocker_detail_sha256: str | None = None


class LatestStateEligibilityManifest(_FrozenModel):
    schema_version: str = "latest-governed-scope-eligibility/v2"
    operation_recorded_at: datetime
    population_run_id: str | None
    population_receipt_set_sha256: str | None
    scope_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    source_scope_revision_ids: tuple[str, ...]
    scopes: tuple[ScopeEligibility, ...]
    manifest_sha256: str

    @field_validator("operation_recorded_at")
    @classmethod
    def _aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operation_recorded_at must include a timezone")
        return value


class BoundLatestStateEligibilityManifest(_FrozenModel):
    schema_version: Literal["latest-governed-bound-scope-eligibility/v3"] = (
        "latest-governed-bound-scope-eligibility/v3"
    )
    database_path: str
    database_sha256: str
    alembic_revision: str
    database_identity_before: CandidateFileIdentity
    database_identity_after: CandidateFileIdentity
    candidate_audit_receipt: str
    candidate_audit_file_sha256: str
    candidate_audit_report_sha256: str
    candidate_audit_identity_before: CandidateFileIdentity
    candidate_audit_identity_after: CandidateFileIdentity
    candidate_coverage_receipt: str
    candidate_coverage_file_sha256: str
    candidate_coverage_report_sha256: str
    candidate_coverage_identity_before: CandidateFileIdentity
    candidate_coverage_identity_after: CandidateFileIdentity
    production_scope_registry: str
    production_scope_registry_file_sha256: str
    production_scope_registry_sha256: str
    production_scope_registry_identity_before: CandidateFileIdentity
    production_scope_registry_identity_after: CandidateFileIdentity
    expected_scope_count: int = Field(ge=1)
    expected_scope_ids: tuple[str, ...]
    expected_scope_revision_ids: tuple[str, ...]
    eligibility: LatestStateEligibilityManifest
    report_sha256: str


def verify_bound_eligibility_manifest(
    report: BoundLatestStateEligibilityManifest,
) -> bool:
    payload = report.model_dump(mode="json")
    stored = str(payload.pop("report_sha256"))
    return stored == _digest(payload)


def bind_scope_eligibility_manifest(
    *,
    database_path: Path,
    audit_path: Path,
    coverage_path: Path,
    scope_registry_path: Path,
    scope_registry_sha256: str,
    audit_snapshot: CandidateArtifactSnapshot,
    coverage_snapshot: CandidateArtifactSnapshot,
    registry_snapshot: CandidateArtifactSnapshot,
    expected_scope_revision_ids: tuple[str, ...],
    identity_before: CandidateFileIdentity,
    identity_after: CandidateFileIdentity,
    eligibility: LatestStateEligibilityManifest,
    expected_revision: str,
) -> BoundLatestStateEligibilityManifest:
    """Bind scope classification to exact candidate and frozen registry evidence."""

    database = database_path.resolve()
    structural_path = audit_path.resolve()
    exhaustive_path = coverage_path.resolve()
    registry_path = scope_registry_path.resolve()
    try:
        observed_audit, structural_bytes = read_candidate_artifact(structural_path)
        observed_coverage, coverage_bytes = read_candidate_artifact(exhaustive_path)
        observed_registry, registry_bytes = read_candidate_artifact(registry_path)
        structural = GovernedCandidateAudit.model_validate_json(structural_bytes)
        coverage = GovernedCandidateCoverageAudit.model_validate_json(coverage_bytes)
        decoded_registry: object = json.loads(registry_bytes)
    except (OSError, ValueError) as exc:
        raise LatestStateActivationError("candidate admission receipt is malformed") from exc
    if (
        observed_audit != audit_snapshot
        or observed_coverage != coverage_snapshot
        or observed_registry != registry_snapshot
    ):
        raise LatestStateActivationError("eligibility input artifact changed during census")
    if not verify_candidate_audit_receipt(structural):
        raise LatestStateActivationError("candidate audit receipt commitment is invalid")
    if not verify_candidate_coverage_receipt(coverage):
        raise LatestStateActivationError("candidate coverage receipt commitment is invalid")
    if not isinstance(decoded_registry, dict):
        raise LatestStateActivationError("production scope registry is malformed")
    registry_payload = cast(dict[str, object], decoded_registry)
    registry_core: dict[str, object] = {
        key: value for key, value in registry_payload.items() if key != "registry_sha256"
    }
    if (
        registry_payload.get("registry_sha256") != scope_registry_sha256
        or _digest(registry_core) != scope_registry_sha256
    ):
        raise LatestStateActivationError("production scope registry commitment is invalid")
    raw_registry_scopes = registry_payload.get("scopes")
    if not isinstance(raw_registry_scopes, list) or not raw_registry_scopes:
        raise LatestStateActivationError("production scope registry is empty")
    registry_scope_payloads = cast(list[object], raw_registry_scopes)
    try:
        registry_scopes = tuple(
            RetrievalScope.model_validate(item) for item in registry_scope_payloads
        )
    except ValueError as exc:
        raise LatestStateActivationError("production scope registry scopes are malformed") from exc
    if (
        registry_payload.get("registry_id") != PRODUCTION_SCOPE_REGISTRY_ID
        or registry_payload.get("schema_version") != PRODUCTION_SCOPE_SCHEMA_VERSION
        or registry_payload.get("supported_cohort") != list(PRODUCTION_SUPPORTED_COHORT)
        or registry_scopes != tuple(sorted(registry_scopes, key=lambda item: item.scope_id))
        or len({scope.scope_id for scope in registry_scopes}) != len(registry_scopes)
        or registry_payload.get("scope_set_sha256")
        != _digest([scope.model_dump(mode="json") for scope in registry_scopes])
        or registry_payload.get("source_scope_revision_ids")
        != sorted(scope.source_scope_revision_id for scope in registry_scopes)
    ):
        raise LatestStateActivationError("production scope registry contract is invalid")
    if (
        Path(structural.database_path).resolve() != database
        or structural.alembic_revision != expected_revision
        or structural.database_identity_after != identity_before
        or identity_after != identity_before
    ):
        raise LatestStateActivationError("candidate identity or revision differs from admission")
    if (
        Path(coverage.database_path).resolve() != database
        or Path(coverage.candidate_audit_receipt).resolve() != structural_path
        or coverage.candidate_audit_report_sha256 != structural.report_sha256
        or coverage.candidate_audit_file_sha256 != audit_snapshot.file_sha256
        or coverage.database_identity_after != identity_before
        or coverage.database_sha256 != structural.database_sha256
    ):
        raise LatestStateActivationError("candidate coverage differs from structural admission")
    if not expected_scope_revision_ids:
        raise LatestStateActivationError("production scope registry is empty")
    eligible_core_revision_ids = tuple(
        sorted(
            item.scope_revision_id for item in eligibility.scopes if item.inclusion_state == "core"
        )
    )
    if eligible_core_revision_ids != tuple(sorted(expected_scope_revision_ids)):
        raise LatestStateActivationError("eligibility core scopes differ from production registry")
    expected_scope_evidence = tuple(
        sorted(
            (
                scope.scope_id,
                scope.source_scope_key,
                scope.source_scope_revision_id,
                scope.issuer_id,
                scope.reporting_entity_id,
                scope.ticker,
            )
            for scope in registry_scopes
        )
    )
    actual_scope_evidence = tuple(
        sorted(
            (
                item.scope_id,
                item.source_scope_key,
                item.scope_revision_id,
                item.issuer_id,
                item.reporting_entity_id or "",
                item.ticker or "",
            )
            for item in eligibility.scopes
            if item.inclusion_state == "core"
        )
    )
    if actual_scope_evidence != expected_scope_evidence:
        raise LatestStateActivationError(
            "eligibility scope identities differ from production registry"
        )
    _before_bound_artifact_recheck()
    audit_after = candidate_artifact_snapshot(structural_path)
    coverage_after = candidate_artifact_snapshot(exhaustive_path)
    registry_after = candidate_artifact_snapshot(registry_path)
    if (
        audit_after != audit_snapshot
        or coverage_after != coverage_snapshot
        or registry_after != registry_snapshot
    ):
        raise LatestStateActivationError("eligibility input artifact changed during binding")
    core = {
        "alembic_revision": structural.alembic_revision,
        "candidate_audit_file_sha256": audit_snapshot.file_sha256,
        "candidate_audit_identity_after": audit_after.identity.model_dump(mode="json"),
        "candidate_audit_identity_before": audit_snapshot.identity.model_dump(mode="json"),
        "candidate_audit_receipt": str(structural_path),
        "candidate_audit_report_sha256": structural.report_sha256,
        "candidate_coverage_file_sha256": coverage_snapshot.file_sha256,
        "candidate_coverage_identity_after": coverage_after.identity.model_dump(mode="json"),
        "candidate_coverage_identity_before": coverage_snapshot.identity.model_dump(mode="json"),
        "candidate_coverage_receipt": str(exhaustive_path),
        "candidate_coverage_report_sha256": coverage.report_sha256,
        "database_identity_after": identity_after.model_dump(mode="json"),
        "database_identity_before": identity_before.model_dump(mode="json"),
        "database_path": str(database),
        "database_sha256": structural.database_sha256,
        "eligibility": eligibility.model_dump(mode="json"),
        "expected_scope_count": len(expected_scope_revision_ids),
        "expected_scope_ids": sorted(scope.scope_id for scope in registry_scopes),
        "expected_scope_revision_ids": sorted(expected_scope_revision_ids),
        "production_scope_registry": str(registry_path),
        "production_scope_registry_file_sha256": registry_snapshot.file_sha256,
        "production_scope_registry_identity_after": registry_after.identity.model_dump(mode="json"),
        "production_scope_registry_identity_before": registry_snapshot.identity.model_dump(
            mode="json"
        ),
        "production_scope_registry_sha256": scope_registry_sha256,
        "schema_version": "latest-governed-bound-scope-eligibility/v3",
    }
    return BoundLatestStateEligibilityManifest.model_validate(
        core | {"report_sha256": _digest(core)}
    )


def build_scope_eligibility_manifest(
    conn: sqlite3.Connection,
    *,
    operation_recorded_at: datetime,
) -> LatestStateEligibilityManifest:
    """Classify every current scope and dry-run the governed materializer."""

    if operation_recorded_at.tzinfo is None or operation_recorded_at.utcoffset() is None:
        raise LatestStateActivationError("operation_recorded_at must include a timezone")
    population_rows = conn.execute(
        "SELECT population_run_id,receipt_set_sha256 FROM v_population_cutover_current"
    ).fetchall()
    population = (
        (str(population_rows[0][0]), str(population_rows[0][1]))
        if len(population_rows) == 1
        else (None, None)
    )
    scope_rows = conn.execute(
        "SELECT scope_revision_id,scope_key,issuer_id,inclusion_state "
        "FROM v_issuer_reporting_scope_current "
        "ORDER BY scope_key,issuer_id,scope_revision_id"
    ).fetchall()
    if not any(str(row[3]) == "core" for row in scope_rows):
        raise LatestStateActivationError("production core scope cohort is empty")
    core_composites = tuple(
        (str(row[1]), str(row[2])) for row in scope_rows if str(row[3]) == "core"
    )
    core_revision_ids = tuple(str(row[0]) for row in scope_rows if str(row[3]) == "core")
    ambiguous_composites = {value for value in core_composites if core_composites.count(value) > 1}
    ambiguous_revision_ids = {
        value for value in core_revision_ids if core_revision_ids.count(value) > 1
    }
    scopes = tuple(
        _classify_scope(
            conn,
            row=tuple(row),
            population=population,
            operation_recorded_at=operation_recorded_at,
            registry_ambiguous=(
                str(row[3]) == "core"
                and (
                    (str(row[1]), str(row[2])) in ambiguous_composites
                    or str(row[0]) in ambiguous_revision_ids
                )
            ),
        )
        for row in scope_rows
    )
    core = {
        "blocked_count": sum(item.status == "blocked" for item in scopes),
        "eligible_count": sum(item.status == "eligible" for item in scopes),
        "excluded_count": sum(item.status == "intentionally_excluded" for item in scopes),
        "manifest_sha256": "",
        "operation_recorded_at": operation_recorded_at,
        "population_receipt_set_sha256": population[1],
        "population_run_id": population[0],
        "schema_version": "latest-governed-scope-eligibility/v2",
        "scope_count": len(scopes),
        "scopes": scopes,
        "source_scope_revision_ids": tuple(item.scope_revision_id for item in scopes),
    }
    draft = LatestStateEligibilityManifest.model_validate(core)
    digest_payload = draft.model_dump(mode="json")
    digest_payload.pop("manifest_sha256")
    return draft.model_copy(update={"manifest_sha256": _digest(digest_payload)})


def _classify_scope(
    conn: sqlite3.Connection,
    *,
    row: tuple[object, ...],
    population: tuple[str | None, str | None],
    operation_recorded_at: datetime,
    registry_ambiguous: bool,
) -> ScopeEligibility:
    scope_revision_id, scope_key, issuer_id, inclusion_state = map(str, row)
    scope_id = derive_retrieval_scope_id(
        source_scope_key=scope_key,
        issuer_id=issuer_id,
    )
    base = {
        "scope_revision_id": scope_revision_id,
        "scope_id": scope_id,
        "source_scope_key": scope_key,
        "issuer_id": issuer_id,
        "inclusion_state": inclusion_state,
    }
    if inclusion_state != "core":
        return ScopeEligibility(
            **base,
            status="intentionally_excluded",
            reason_codes=("scope_not_core",),
        )
    if registry_ambiguous:
        return ScopeEligibility(
            **base,
            status="blocked",
            reason_codes=("scope_registry_ambiguous",),
        )
    issuer_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM issuer_entities "
            "WHERE issuer_id=? AND entity_kind='operating_company'",
            (issuer_id,),
        ).fetchone()[0]
    )
    reporting_rows = conn.execute(
        "SELECT reporting_entity_id FROM reporting_entities "
        "WHERE issuer_id=? AND reporting_entity_kind='legal_registrant' "
        "ORDER BY reporting_entity_id",
        (issuer_id,),
    ).fetchall()
    listing_rows = conn.execute(
        "SELECT normalized_ticker FROM v_security_listings_canonical "
        "WHERE issuer_id=? AND status='listed' ORDER BY normalized_ticker",
        (issuer_id,),
    ).fetchall()
    if issuer_count != 1 or len(reporting_rows) != 1 or len(listing_rows) != 1:
        return ScopeEligibility(
            **base,
            status="blocked",
            reason_codes=(
                "scope_identity_incomplete"
                if issuer_count == 0 or not reporting_rows or not listing_rows
                else "scope_identity_ambiguous",
            ),
        )
    reporting_entity_id = str(reporting_rows[0][0])
    ticker = str(listing_rows[0][0]).strip().upper()
    if not reporting_entity_id or not ticker:
        return ScopeEligibility(
            **base,
            status="blocked",
            reason_codes=("scope_identity_incomplete",),
        )
    if population[0] is None or population[1] is None:
        return ScopeEligibility(
            **base,
            reporting_entity_id=reporting_entity_id,
            ticker=ticker,
            status="blocked",
            reason_codes=("population_cutover_missing_or_ambiguous",),
        )
    promotion_rows = conn.execute(
        "SELECT promotion_id,status,population_receipt_set_sha256,"
        "fact_projection_seal_sha256,source_inventory_set_json,"
        "source_inventory_set_sha256,narrative_bundles_json,"
        "narrative_bundles_sha256,issuer_id,reporting_entity_id,"
        "source_scope_key,source_scope_revision_id "
        "FROM v_ask_retrieval_scope_current WHERE scope_key=?",
        (scope_id,),
    ).fetchall()
    if not promotion_rows:
        return ScopeEligibility(
            **base,
            reporting_entity_id=reporting_entity_id,
            ticker=ticker,
            status="blocked",
            reason_codes=("promotion_missing",),
        )
    if len(promotion_rows) != 1 or str(promotion_rows[0][1]) != "promoted":
        return ScopeEligibility(
            **base,
            reporting_entity_id=reporting_entity_id,
            ticker=ticker,
            status="blocked",
            reason_codes=("promotion_not_promoted",),
        )
    promotion = promotion_rows[0]
    if (
        str(promotion[2]) != population[1]
        or str(promotion[8]) != issuer_id
        or str(promotion[9]) != reporting_entity_id
        or str(promotion[10]) != scope_key
        or str(promotion[11]) != scope_revision_id
    ):
        return ScopeEligibility(
            **base,
            reporting_entity_id=reporting_entity_id,
            ticker=ticker,
            promotion_id=str(promotion[0]),
            status="blocked",
            reason_codes=("promotion_identity_mismatch",),
        )
    if _text_digest(str(promotion[4])) != str(promotion[5]) or _text_digest(
        str(promotion[6])
    ) != str(promotion[7]):
        return ScopeEligibility(
            **base,
            reporting_entity_id=reporting_entity_id,
            ticker=ticker,
            promotion_id=str(promotion[0]),
            status="blocked",
            reason_codes=("promotion_evidence_commitment_mismatch",),
        )
    evidence = {
        **base,
        "reporting_entity_id": reporting_entity_id,
        "ticker": ticker,
        "promotion_id": str(promotion[0]),
        "population_receipt_set_sha256": str(promotion[2]),
        "fact_projection_seal_sha256": str(promotion[3]),
        "source_inventory_commitment_sha256": str(promotion[5]),
        "narrative_bundle_commitment_sha256": str(promotion[7]),
    }
    try:
        result = refresh_latest_governed_state(
            conn,
            LatestGovernedRefreshRequest(
                scope_id=scope_id,
                operation_recorded_at=operation_recorded_at,
                apply=False,
            ),
        )
    except (LatestGovernedStateError, LatestStateActivationError) as exc:
        detail = str(exc)
        return ScopeEligibility(
            **evidence,
            status="blocked",
            reason_codes=("materializer_validation_failed",),
            blocker_detail=detail,
            blocker_detail_sha256=_digest(detail),
        )
    if result.mode != "dry_run" or result.outcome not in {"no_op", "changed"}:
        detail = f"mode={result.mode};outcome={result.outcome}"
        return ScopeEligibility(
            **evidence,
            status="blocked",
            reason_codes=("materializer_result_invalid",),
            blocker_detail=detail,
            blocker_detail_sha256=_digest(detail),
        )
    return ScopeEligibility(
        **evidence,
        status="eligible",
        reason_codes=("eligible",),
        terminal_commitment=result.terminal_commitment,
    )


def verify_candidate_audit_receipt(report: GovernedCandidateAudit) -> bool:
    payload = report.model_dump(mode="json")
    stored = str(payload.pop("report_sha256"))
    return stored == _digest(payload)


def verify_candidate_coverage_receipt(report: GovernedCandidateCoverageAudit) -> bool:
    payload = report.model_dump(mode="json")
    stored = str(payload.pop("report_sha256"))
    return (
        stored == _digest(payload)
        and tuple(item.plane_name for item in report.planes) == GOVERNED_PLANE_NAMES
    )


def audit_candidate_coverage(
    database_path: Path,
    *,
    candidate_audit_receipt: Path,
) -> GovernedCandidateCoverageAudit:
    """Bind an exhaustive governed-plane census to a structural audit receipt."""

    database = database_path.resolve()
    audit_path = candidate_audit_receipt.resolve()
    try:
        audit_snapshot_before, audit_bytes = read_candidate_artifact(audit_path)
        audit = GovernedCandidateAudit.model_validate_json(audit_bytes)
    except (OSError, ValueError) as exc:
        raise LatestStateActivationError("candidate audit receipt is malformed") from exc
    if not verify_candidate_audit_receipt(audit):
        raise LatestStateActivationError("candidate audit receipt commitment is invalid")
    if Path(audit.database_path).resolve() != database:
        raise LatestStateActivationError("candidate audit receipt names a different database")
    require_checkpointed_sidecars(database)
    identity_before = _file_identity(database)
    if identity_before != audit.database_identity_after:
        raise LatestStateActivationError("candidate identity differs from structural audit")
    conn = connect_sqlite(
        database,
        role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
        schema_preflight=False,
    )
    try:
        existing = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        }
        planes = tuple(
            GovernedPlaneCount(
                plane_name=name,
                row_count=(_count_fixed_plane(conn, name) if name in existing else None),
            )
            for name in GOVERNED_PLANE_NAMES
        )
    except sqlite3.Error as exc:
        raise LatestStateActivationError("candidate coverage census failed") from exc
    finally:
        conn.close()
    require_checkpointed_sidecars(database)
    identity_after = _file_identity(database)
    if identity_after != identity_before:
        raise LatestStateActivationError("candidate identity changed during coverage census")
    _before_coverage_artifact_recheck()
    audit_snapshot_after = candidate_artifact_snapshot(audit_path)
    if audit_snapshot_after != audit_snapshot_before:
        raise LatestStateActivationError("candidate audit receipt changed during coverage census")
    core = {
        "candidate_audit_file_sha256": audit_snapshot_before.file_sha256,
        "candidate_audit_identity_after": audit_snapshot_after.identity.model_dump(mode="json"),
        "candidate_audit_identity_before": audit_snapshot_before.identity.model_dump(mode="json"),
        "candidate_audit_receipt": str(audit_path),
        "candidate_audit_report_sha256": audit.report_sha256,
        "database_identity_after": identity_after.model_dump(mode="json"),
        "database_identity_before": identity_before.model_dump(mode="json"),
        "database_path": str(database),
        "database_sha256": audit.database_sha256,
        "planes": [item.model_dump(mode="json") for item in planes],
        "schema_version": "latest-governed-candidate-coverage/v2",
    }
    return GovernedCandidateCoverageAudit.model_validate(core | {"report_sha256": _digest(core)})


def audit_governed_candidate(
    database_path: Path,
    *,
    seal_path: Path,
    expected_revision: str,
) -> GovernedCandidateAudit:
    """Inventory one quiesced candidate without creating SQLite sidecars."""

    database = database_path.resolve()
    if not database.is_file():
        raise LatestStateActivationError("candidate database is missing")
    require_checkpointed_sidecars(database)
    identity_before = _file_identity(database)
    seal_file = seal_path.resolve()
    if not seal_file.is_file():
        raise LatestStateActivationError("candidate seal is missing")
    seal_identity_before = _file_identity(seal_file)
    seal = _load_seal(seal_file)
    seal_sha256 = _sha256(seal_file)
    seal_identity_after_load = _file_identity(seal_file)
    if seal_identity_after_load != seal_identity_before:
        raise LatestStateActivationError("candidate seal changed during admission")
    if Path(seal.database).resolve() != database:
        raise LatestStateActivationError("candidate seal names a different database")
    if seal.size_bytes != identity_before.size_bytes:
        raise LatestStateActivationError("candidate size differs from its seal")
    if len(seal.revision) != 1:
        raise LatestStateActivationError("candidate seal must name exactly one revision")
    if seal.quick_check != "ok" or seal.foreign_key_violations:
        raise LatestStateActivationError("candidate seal records a failed database check")
    actual_sha256 = _sha256(database)
    if actual_sha256 != seal.sha256:
        raise LatestStateActivationError("candidate database SHA-256 differs from its seal")

    conn = connect_sqlite(
        database,
        role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
        schema_preflight=False,
    )
    try:
        revision_rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        if len(revision_rows) != 1:
            raise LatestStateActivationError("candidate must have exactly one Alembic revision")
        revision = str(revision_rows[0][0])
        if revision != seal.revision[0]:
            raise LatestStateActivationError("candidate revision differs from its seal")
        if revision != expected_revision:
            raise LatestStateActivationError("candidate Alembic revision differs from expectation")
        quick_rows = conn.execute("PRAGMA quick_check").fetchall()
        # Avoid depending on sqlite3.Row equality configuration.
        quick_values = tuple(str(row[0]) for row in quick_rows)
        quick_check = "ok" if quick_values == ("ok",) else ";".join(quick_values)
        if quick_check != "ok":
            raise LatestStateActivationError("candidate quick_check failed")
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        integrity_values = tuple(str(row[0]) for row in integrity_rows)
        integrity_check = "ok" if integrity_values == ("ok",) else ";".join(integrity_values)
        if integrity_check != "ok":
            raise LatestStateActivationError("candidate integrity_check failed")
        foreign_key_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        if foreign_key_violations:
            raise LatestStateActivationError("candidate has foreign-key violations")
        schema_rows = conn.execute(
            "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"
        ).fetchall()
        schema_fingerprint_sha256 = _digest(
            [tuple(str(value) for value in row) for row in schema_rows]
        )
    except sqlite3.Error as exc:
        raise LatestStateActivationError("candidate SQLite verification failed") from exc
    finally:
        conn.close()

    require_checkpointed_sidecars(database)
    identity_after = _file_identity(database)
    if identity_after != identity_before:
        raise LatestStateActivationError("candidate file identity changed during audit")
    seal_identity_after = _file_identity(seal_file)
    if seal_identity_after != seal_identity_before or _sha256(seal_file) != seal_sha256:
        raise LatestStateActivationError("candidate seal changed during audit")

    core = {
        "alembic_revision": revision,
        "database_identity_after": identity_after.model_dump(mode="json"),
        "database_identity_before": identity_before.model_dump(mode="json"),
        "database_path": str(database),
        "database_sha256": actual_sha256,
        "database_size_bytes": identity_after.size_bytes,
        "foreign_key_violation_count": foreign_key_violations,
        "integrity_check": integrity_check,
        "quick_check": quick_check,
        "schema_version": _SCHEMA_VERSION,
        "schema_fingerprint_sha256": schema_fingerprint_sha256,
        "seal_identity_after": seal_identity_after.model_dump(mode="json"),
        "seal_identity_before": seal_identity_before.model_dump(mode="json"),
        "seal_path": str(seal_file),
        "seal_sha256": seal_sha256,
    }
    return GovernedCandidateAudit.model_validate(core | {"report_sha256": _digest(core)})


def build_governed_candidate_seal(
    database_path: Path,
    *,
    expected_revision: str,
) -> CandidateSeal:
    """Create the companion seal for one quiesced, checkpointed governed DB."""

    database = database_path.expanduser().resolve()
    if not database.is_file():
        raise LatestStateActivationError("candidate database is missing")
    require_checkpointed_sidecars(database)
    identity_before = _file_identity(database)
    database_sha256 = _sha256(database)
    conn = connect_sqlite(
        database,
        role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
        schema_preflight=False,
    )
    try:
        revisions = tuple(
            str(row[0])
            for row in conn.execute("SELECT version_num FROM alembic_version").fetchall()
        )
        if revisions != (expected_revision,):
            raise LatestStateActivationError("candidate revision differs from expectation")
        quick_values = tuple(str(row[0]) for row in conn.execute("PRAGMA quick_check"))
        integrity_values = tuple(str(row[0]) for row in conn.execute("PRAGMA integrity_check"))
        foreign_key_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        if quick_values != ("ok",) or integrity_values != ("ok",) or foreign_key_violations:
            raise LatestStateActivationError("candidate failed SQLite sealing checks")
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {
            "source_taxonomy_components",
            "fact_cell_canonical_binding_revisions",
        }
        if not required.issubset(tables):
            raise LatestStateActivationError("candidate lacks governed ontology tables")
        source_taxonomy_components = _count_fixed_table(
            conn,
            "source_taxonomy_components",
            required,
        )
        canonical_bindings = _count_fixed_table(
            conn,
            "fact_cell_canonical_binding_revisions",
            required,
        )
    except sqlite3.Error as exc:
        raise LatestStateActivationError("candidate sealing census failed") from exc
    finally:
        conn.close()
    require_checkpointed_sidecars(database)
    identity_after = _file_identity(database)
    if identity_after != identity_before or _sha256(database) != database_sha256:
        raise LatestStateActivationError("candidate changed while its seal was built")
    return CandidateSeal(
        canonical_bindings=canonical_bindings,
        database=str(database),
        foreign_key_violations=0,
        quick_check="ok",
        revision=revisions,
        sha256=database_sha256,
        size_bytes=identity_after.size_bytes,
        source_taxonomy_components=source_taxonomy_components,
    )


def _count_fixed_table(
    conn: sqlite3.Connection,
    table_name: str,
    allowed: set[str],
) -> int:
    if table_name not in allowed:
        raise LatestStateActivationError("candidate sealing table is not allowlisted")
    return int(
        conn.execute(
            f'SELECT COUNT(*) FROM "{table_name}"'  # nosec B608 -- fixed allowlist
        ).fetchone()[0]
    )


def _count_fixed_plane(conn: sqlite3.Connection, plane_name: str) -> int:
    if plane_name not in GOVERNED_PLANE_NAMES:
        raise LatestStateActivationError("unregistered governed plane")
    return int(
        conn.execute(
            f'SELECT COUNT(*) FROM "{plane_name}"'  # nosec B608 -- fixed allowlist
        ).fetchone()[0]
    )


def require_checkpointed_sidecars(database: Path) -> None:
    wal = Path(f"{database}-wal")
    journal = Path(f"{database}-journal")
    if wal.exists() and wal.stat().st_size:
        raise LatestStateActivationError("candidate has a non-empty WAL sidecar")
    if journal.exists() and journal.stat().st_size:
        raise LatestStateActivationError("candidate has a non-empty rollback journal")


def candidate_file_identity(path: Path) -> CandidateFileIdentity:
    """Capture the stable main-file identity used by admission receipts."""

    return _file_identity(path.resolve())


def read_candidate_artifact(path: Path) -> tuple[CandidateArtifactSnapshot, bytes]:
    """Read one small evidence artifact from a stable file identity."""

    artifact = path.resolve()
    if not artifact.is_file():
        raise LatestStateActivationError("candidate evidence artifact is missing")
    identity_before = _file_identity(artifact)
    try:
        payload = artifact.read_bytes()
    except OSError as exc:
        raise LatestStateActivationError("candidate evidence artifact could not be read") from exc
    identity_after = _file_identity(artifact)
    if identity_after != identity_before:
        raise LatestStateActivationError("candidate evidence artifact changed while reading")
    return (
        CandidateArtifactSnapshot(
            identity=identity_after,
            file_sha256=hashlib.sha256(payload).hexdigest(),
        ),
        payload,
    )


def candidate_artifact_snapshot(path: Path) -> CandidateArtifactSnapshot:
    """Capture identity and content hash for one small evidence artifact."""

    snapshot, _ = read_candidate_artifact(path)
    return snapshot


def _before_coverage_artifact_recheck() -> None:
    """Test seam before coverage publishes its structural-input binding."""


def _before_bound_artifact_recheck() -> None:
    """Test seam before eligibility publishes its three-input binding."""


def _load_seal(path: Path) -> CandidateSeal:
    if not path.is_file():
        raise LatestStateActivationError("candidate seal is missing")
    try:
        return CandidateSeal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LatestStateActivationError("candidate seal is malformed") from exc


def _file_identity(path: Path) -> CandidateFileIdentity:
    stat = path.stat()
    return CandidateFileIdentity(
        device=int(stat.st_dev),
        inode=int(stat.st_ino),
        size_bytes=stat.st_size,
        modified_time_ns=stat.st_mtime_ns,
        changed_time_ns=stat.st_ctime_ns,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
