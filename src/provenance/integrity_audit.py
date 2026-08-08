"""Read-only, schema-adaptive integrity checks for investor-grade evidence.

The auditor is deliberately separate from repair and ingestion paths: it
reports deterministic, bounded findings but never writes to the database or
its evidence storage.  It can therefore run before a migration, backfill, or
index build without risking the chain it is meant to inspect.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

from ask.grounded_retrieval import GroundedAskItem, ask_item_bundle_sha256
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.document_processing_evidence import (
    verify_document_processing_evidence,
)
from provenance.filing_xbrl_extraction_ledger import (
    FilingXbrlExtractionDispositionRecord,
    FilingXbrlExtractionDispositionSeal,
)
from provenance.metric_ontology import MetricOntology
from provenance.population_completeness import PopulationTemporalScope
from provenance.research_snapshot import (
    verify_processing_snapshot,
    verify_research_snapshot,
)
from provenance.search_index_lineage import (
    load_projection_seal,
    verify_ledger_projection_seal,
)
from provenance.source_fact_publication import (
    canonical_json as publication_canonical_json,
)
from provenance.source_fact_publication import (
    digest_text as publication_digest_text,
)
from provenance.source_fact_publication import verify_source_fact_publication
from provenance.source_fact_stream import (
    publication_event_for_publication,
    verify_resolution_snapshot_watermark,
)
from provenance.source_inventory_seal import InventoryComponent, component_digest
from search.canonical_fact_projection import verify_canonical_projection_generation
from search.embedding_promotion import EmbeddingPromotion
from search.heterogeneous_retrieval import verify_heterogeneous_retrieval_trace


class Severity(StrEnum):
    ADVISORY = "advisory"
    WARNING = "warning"
    BLOCKER = "blocker"


class RemediationClass(StrEnum):
    REPAIRABLE = "repairable"
    BACKFILL = "backfill"
    REINGEST = "reingest"
    MANUAL = "manual"
    HARD_STOP = "hard-stop"


class AuditOptions(BaseModel):
    """Bounded audit configuration; byte verification is explicitly opt-in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_limit: int = Field(default=20, ge=1, le=500)
    deep_sqlite_checks: bool = True
    verify_bytes: bool = False
    repo_root: Path | None = None
    content_roots: tuple[Path, ...] = ()
    max_verify_bytes: int = Field(default=100 * 1024 * 1024, ge=0)

    @model_validator(mode="after")
    def _require_root_for_byte_verification(self) -> AuditOptions:
        if self.verify_bytes and self.repo_root is None and not self.content_roots:
            raise ValueError(
                "repo_root or at least one content_root is required when verify_bytes is enabled"
            )
        return self


class IntegrityFinding(BaseModel):
    """One stable, bounded finding suitable for CI and analyst review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[A-Z0-9_]+$")
    severity: Severity
    remediation: RemediationClass
    count: int = Field(ge=0)
    query_context: str
    samples: tuple[str, ...]


class IntegrityAuditSummary(BaseModel):
    """Closed stdout contract for the command-line entrypoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "evidence-integrity-audit/v1"
    generated_at: datetime
    findings: tuple[IntegrityFinding, ...]
    has_blockers: bool
    tables_present: tuple[str, ...]


class CutoverAuditOptions(BaseModel):
    """One immutable dual-clock scope and bounded materialization budget."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    knowledge_cutoff: datetime
    observed_through: datetime
    sample_limit: int = Field(default=20, ge=1, le=500)
    fetch_size: int = Field(default=250, ge=1, le=1_000)

    @model_validator(mode="before")
    @classmethod
    def _legacy_single_clock(cls, value: object) -> object:
        if isinstance(value, dict) and "cutoff_at" in value:
            migrated = dict(cast(dict[str, object], value))
            cutoff = migrated.pop("cutoff_at")
            migrated.setdefault("knowledge_cutoff", cutoff)
            migrated.setdefault("observed_through", cutoff)
            return migrated
        return cast(object, value)

    @model_validator(mode="after")
    def _require_timezone(self) -> CutoverAuditOptions:
        PopulationTemporalScope(
            knowledge_cutoff=self.knowledge_cutoff,
            observed_through=self.observed_through,
        )
        return self

    @property
    def temporal_scope(self) -> PopulationTemporalScope:
        return PopulationTemporalScope(
            knowledge_cutoff=self.knowledge_cutoff,
            observed_through=self.observed_through,
        )

    @property
    def cutoff_at(self) -> datetime:
        """Compatibility name for verifier APIs whose cutoff is the K clock."""

        return self.knowledge_cutoff


class CutoverGateCoverage(BaseModel):
    """Exact row coverage for one cutoff-pinned verifier gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: str = Field(pattern=r"^[a-z0-9_]+$")
    eligible_count: int = Field(ge=0)
    verified_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _reconcile(self) -> CutoverGateCoverage:
        if self.verified_count + self.failed_count != self.eligible_count:
            raise ValueError("cutover gate coverage counts do not reconcile")
        return self


class CutoverGateCandidateCommitment(BaseModel):
    """Exact ordered candidate-set commitment for one governed gate query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: str = Field(pattern=r"^[a-z0-9_]+$")
    selection_policy_id: str = Field(min_length=1, max_length=256)
    row_count: int = Field(ge=0)
    rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CutoverReadinessSummary(BaseModel):
    """Closed output contract for the operational cutover readiness command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "data-cutover-readiness-audit/v1"
    knowledge_cutoff: datetime
    observed_through: datetime
    generated_at: datetime
    coverage: tuple[CutoverGateCoverage, ...]
    candidate_commitments: tuple[CutoverGateCandidateCommitment, ...]
    findings: tuple[IntegrityFinding, ...]
    has_blockers: bool
    tables_present: tuple[str, ...]

    @property
    def temporal_scope(self) -> PopulationTemporalScope:
        return PopulationTemporalScope(
            knowledge_cutoff=self.knowledge_cutoff,
            observed_through=self.observed_through,
        )

    @property
    def cutoff_at(self) -> datetime:
        return self.knowledge_cutoff


class CutoverGateQuerySpec(BaseModel):
    """Closed, versioned selector for one gate's dual-clock candidate universe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: str = Field(pattern=r"^[a-z0-9_]+$")
    selection_policy_id: str = Field(min_length=1, max_length=256)
    query: str
    key_column: str = Field(pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def _explicit_scope(self) -> CutoverGateQuerySpec:
        normalized = " ".join(self.query.strip().split()).upper()
        if not normalized.startswith("SELECT "):
            raise ValueError("cutover gate query must be an explicit SELECT")
        if " ORDER BY " not in f" {normalized} ":
            raise ValueError("cutover gate query must declare deterministic ordering")
        if ":KNOWLEDGE_CUTOFF" not in normalized or ":OBSERVED_THROUGH" not in normalized:
            raise ValueError("cutover gate query must explicitly bind both temporal clocks")
        return self


_EVIDENCE_TABLES = (
    "evidence_content_blobs",
    "evidence_source_observations",
    "evidence_document_versions",
    "evidence_extraction_runs",
    "evidence_nodes",
)
_RESOLUTION_TABLES = (
    "reported_observations",
    "observation_resolution_revisions",
    "observation_resolution_candidates",
)
_REPLICA_TABLES = (
    "evidence_blob_location_observations",
    "evidence_document_observation_links",
)
_FACT_SELECTION_TABLES = ("fact_selection_decisions",)
_SOURCE_COVERAGE_TABLES = (
    "source_inventory_snapshots",
    "expected_documents",
    "source_coverage_assessments",
)
_SOURCE_INVENTORY_SEAL_TABLES = (
    "source_inventory_components",
    "source_inventory_snapshot_seals",
    "search_manifest_source_inventories",
)
_ASK_TRACE_TABLES = (
    "ask_retrieval_traces",
    "ask_retrieval_trace_items",
    "ask_answer_groundings",
)
_EMBEDDING_PROMOTION_TABLES = ("search_embedding_model_promotions",)
_EXPECTATION_LIFECYCLE_TABLES = ("expected_document_lifecycle_revisions",)
_FACT_RESOLUTION_CUTOVER_TABLES = (
    "fact_observation_revisions",
    "fact_resolution_outcomes",
)
_FACT_MATCH_PROOF_TABLES = (
    "legacy_fact_evidence_match_revisions",
    "fact_observation_match_proofs",
)
_FACT_CURRENT_CANDIDATE_SET_INCOMPLETE_QUERY = (
    "WITH current_links AS MATERIALIZED ("
    "SELECT link.logical_key, link.observation_id "
    "FROM fact_observation_revisions AS link "
    "WHERE NOT EXISTS (SELECT 1 FROM fact_observation_revisions AS newer "
    "WHERE newer.fact_table = link.fact_table "
    "AND newer.fact_row_id = link.fact_row_id "
    "AND newer.fact_revision > link.fact_revision)"
    "), current_members AS MATERIALIZED ("
    "SELECT resolution.logical_key, member.observation_id "
    "FROM observation_resolution_revisions AS resolution "
    "JOIN fact_resolution_outcomes AS outcome "
    "ON outcome.resolution_id = resolution.resolution_id "
    "JOIN observation_resolution_candidates AS member "
    "ON member.resolution_id = resolution.resolution_id "
    "WHERE NOT EXISTS (SELECT 1 FROM observation_resolution_revisions AS newer "
    "WHERE newer.logical_key = resolution.logical_key "
    "AND newer.revision > resolution.revision)"
    "), missing_members AS ("
    "SELECT logical_key, observation_id FROM current_links "
    "EXCEPT SELECT logical_key, observation_id FROM current_members"
    "), extra_members AS ("
    "SELECT logical_key, observation_id FROM current_members "
    "EXCEPT SELECT logical_key, observation_id FROM current_links"
    ") SELECT logical_key FROM missing_members "
    "UNION SELECT logical_key FROM extra_members ORDER BY logical_key"
)
FACT_CURRENT_CANDIDATE_SET_INCOMPLETE_QUERY = _FACT_CURRENT_CANDIDATE_SET_INCOMPLETE_QUERY
_OCR_TABLES = (
    "ocr_document_assessments",
    "ocr_preflight_pages",
    "ocr_extraction_governance",
    "ocr_page_results",
)
_SEARCH_TABLES = (
    "search_corpus_manifests",
    "search_corpus_document_memberships",
    "search_corpus_manifest_seals",
    "search_chunks",
    "search_embedding_artifacts",
    "search_index_runs",
    "search_index_memberships",
)
_ISSUER_REGISTRY_TABLES = (
    "issuer_entities",
    "issuer_profile_revisions",
    "issuer_identifier_assertions",
    "issuer_identifier_resolution_outcomes",
    "securities",
    "security_listing_assertions",
    "security_listing_resolution_outcomes",
    "issuer_authority_surface_revisions",
    "issuer_reporting_scope_revisions",
    "legacy_issuer_binding_revisions",
)
_REPORTING_IDENTITY_TABLES = (
    "reporting_entities",
    "reporting_entity_identifier_assertions",
    "reporting_entity_identifier_resolution_outcomes",
    "security_identifier_assertions",
    "security_identifier_resolution_outcomes",
    "security_reporting_entity_revisions",
    "source_obligation_revisions",
)
_SUBJECT_BINDING_TABLES = ("recorded_subject_binding_revisions",)
_SEMANTIC_DISPOSITION_TABLES = ("document_semantic_disposition_revisions",)
_FACT_PLANE_V2_TABLES = (
    "fact_cells_v2",
    "fact_observations_v2",
    "fact_observation_relations_v2",
    "fact_resolution_candidates_v2",
    "fact_resolution_revisions_v2",
    "fact_derivation_input_edges_v2",
    "fact_derivation_seals_v2",
)
_FACT_SEARCH_V2_TABLES = (
    "search_fact_projection_runs",
    "search_fact_projection_memberships",
    "search_fact_projection_rows",
    "search_fact_projection_seals",
    "ask_retrieval_trace_hits",
)
_FACT_PLANE_V2_HARDENING_TABLES = (
    "fact_dimensions_normalized_v2",
    "fact_cell_identity_seals_v2",
    "fact_reported_observation_anchors_v2",
    "fact_observation_payload_commitments_v2",
    "fact_derivation_basis_commitments_v2",
    "fact_extraction_run_completeness_seals_v2",
)
_FACT_PLANE_V2_VIEWS = (
    "v_fact_resolutions_current_v2",
    "v_fact_cells_resolved_current_v2",
    "v_fact_observations_as_reported_v2",
)
_FACT_SEARCH_V2_VIEWS = (
    "v_search_fact_projection_current_sealed",
    "v_search_fact_hits_current",
)
_FACT_PLANE_V2_HARDENING_VIEWS = (
    "v_fact_cells_hardened_v2",
    "v_fact_reported_anchors_selected_v2",
    "v_fact_observations_committed_v2",
    "v_fact_extraction_runs_complete_v2",
)
_FACT_PLANE_V2_INDEXES = (
    "ix_fact_cells_v2_entity_period",
    "ix_fact_cells_v2_security_period",
    "ix_fact_observations_v2_as_known",
    "ix_fact_observations_v2_document",
    "ix_fact_observation_relations_v2_subject",
    "ix_fact_observation_relations_v2_object",
    "ix_fact_resolution_candidates_v2_set",
    "ix_fact_resolution_revisions_v2_current",
    "ix_fact_resolution_revisions_v2_as_known",
    "ix_fact_derivation_edges_v2_output",
    "ix_fact_derivation_edges_v2_input",
)
_FACT_SEARCH_V2_INDEXES = (
    "ix_search_fact_projection_runs_manifest",
    "ix_search_fact_projection_memberships_disposition",
    "ix_search_fact_projection_rows_entity_concept",
    "ix_search_fact_projection_rows_security_period",
    "ix_ask_retrieval_trace_hits_source",
)
_FACT_PLANE_V2_GUARD_TRIGGERS = (
    "trg_fact_cells_v2_security_scope",
    "trg_fact_cells_v2_security_relationship",
    "trg_fact_observations_v2_revision_parent",
    "trg_fact_observations_v2_match_scope",
    "trg_fact_observation_relations_v2_scope",
    "trg_fact_resolution_candidates_v2_scope",
    "trg_fact_resolution_candidates_v2_finalized",
    "trg_fact_resolution_revisions_v2_first",
    "trg_fact_resolution_revisions_v2_parent",
    "trg_fact_resolution_revisions_v2_candidates",
    "trg_fact_resolution_revisions_v2_selected",
    "trg_fact_resolution_revisions_v2_derived_seals",
    "trg_fact_derivation_edges_v2_output",
    "trg_fact_derivation_edges_v2_resolution",
    "trg_fact_derivation_edges_v2_sealed",
    "trg_fact_derivation_seals_v2_validate",
)
_FACT_SEARCH_V2_GUARD_TRIGGERS = (
    "trg_search_fact_projection_runs_manifest",
    "trg_search_fact_projection_runs_first",
    "trg_search_fact_projection_runs_parent",
    "trg_search_fact_projection_memberships_unsealed",
    "trg_search_fact_projection_memberships_scope",
    "trg_search_fact_projection_memberships_included",
    "trg_search_fact_projection_memberships_unresolved",
    "trg_search_fact_projection_memberships_resolution_scope",
    "trg_search_fact_projection_rows_unsealed",
    "trg_search_fact_projection_rows_membership",
    "trg_search_fact_projection_rows_exact",
    "trg_search_fact_projection_seals_contract",
    "trg_search_fact_projection_seals_counts",
    "trg_search_fact_projection_seals_coverage",
    "trg_ask_retrieval_trace_hits_rank_legacy",
    "trg_ask_retrieval_trace_items_rank_v2",
    "trg_ask_retrieval_trace_hits_document",
    "trg_ask_retrieval_trace_hits_fact",
)
_FACT_PLANE_V2_HARDENING_GUARD_TRIGGERS = (
    "trg_fact_observations_v2_evidence_chain_hardened",
    "trg_fact_dimensions_normalized_v2_typed_digest",
    "trg_fact_cell_identity_seals_v2_complete",
    "trg_fact_reported_anchors_v2_exact",
    "trg_fact_observation_payload_commitments_v2_exact",
    "trg_fact_resolution_candidates_v2_payload_commitment",
    "trg_fact_derivation_basis_v2_exact",
    "trg_fact_extraction_seals_v2_complete",
    "trg_evidence_nodes_fact_extraction_sealed_v2",
    "trg_fact_reported_anchors_extraction_sealed_v2",
    "trg_search_fact_membership_hardened_v2",
    "trg_search_fact_rows_hardened_v2",
)
_CUTOVER_GATE_TABLES: dict[str, tuple[str, ...]] = {
    "source_fact_publications": (
        "source_fact_publications",
        "source_fact_publication_members",
        "source_fact_publication_seals",
    ),
    "source_fact_publication_stream": (
        "source_fact_publications",
        "source_fact_publication_stream",
    ),
    "filing_xbrl_dispositions": (
        "filing_xbrl_extraction_dispositions",
        "filing_xbrl_extraction_disposition_seals",
        "source_fact_publications",
        "source_fact_publication_members",
        "source_fact_publication_seals",
    ),
    "ontology_snapshots": (
        "ontology_snapshot_headers",
        "ontology_snapshot_members",
        "ontology_snapshot_seals",
        "fact_cell_canonical_binding_revisions",
    ),
    "canonical_resolution_snapshots": (
        "canonical_fact_resolution_snapshot_seals",
        "canonical_fact_resolution_snapshot_members",
        "canonical_fact_resolution_snapshot_scope_headers",
        "canonical_fact_resolution_snapshot_scope_members",
        "canonical_fact_resolution_snapshot_scope_seals",
        "canonical_fact_resolution_snapshot_watermarks",
    ),
    "canonical_projection_generations": (
        "canonical_fact_projection_generations",
        "canonical_fact_projection_entries",
        "canonical_fact_projection_batches",
        "canonical_fact_projection_buckets",
        "canonical_fact_projection_seals",
        "canonical_fact_projection_scope_bindings",
    ),
    "document_processing_evidence": (
        "document_processing_evidence_headers",
        "document_processing_evidence_members",
        "document_processing_evidence_seals",
    ),
    "document_processing_snapshots": (
        "document_processing_snapshot_headers",
        "document_processing_snapshot_members",
        "document_processing_snapshot_seals",
        "document_processing_disposition_headers",
        "document_processing_disposition_members",
        "document_processing_disposition_seals",
    ),
    "research_snapshots": (
        "research_snapshot_headers",
        "research_snapshot_members",
        "research_snapshot_seals",
    ),
    "heterogeneous_retrieval_traces": (
        "heterogeneous_retrieval_trace_headers",
        "heterogeneous_retrieval_trace_candidates",
        "heterogeneous_retrieval_trace_results",
        "heterogeneous_retrieval_trace_seals",
    ),
    "embedding_runtime_promotions": ("search_embedding_model_promotions",),
    "embedding_runtime_artifacts": (
        "search_embedding_model_promotions",
        "search_embedding_artifacts",
    ),
    "embedding_runtime_projection_seals": (
        "search_embedding_model_promotions",
        "search_embedding_artifacts",
        "search_projection_seals",
        "search_index_runs",
    ),
}
_JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_JSON_OBJECT_LIST_ADAPTER: TypeAdapter[list[dict[str, JsonValue]]] = TypeAdapter(
    list[dict[str, JsonValue]]
)


def exit_code(*, has_blockers: bool, strict: bool) -> int:
    """Return the stable CLI status without treating advisory drift as failure."""
    return 2 if strict and has_blockers else 0


def audit_connection(conn: sqlite3.Connection, options: AuditOptions) -> IntegrityAuditSummary:
    """Inspect a SQLite connection without issuing any mutating statement."""
    tables = _table_names(conn)
    findings: list[IntegrityFinding] = []
    if options.deep_sqlite_checks:
        _audit_sqlite_pragmas(conn, findings, options)
    _audit_document_parents(conn, tables, findings, options)
    _audit_lifecycle(conn, tables, findings, options)
    _audit_evidence_ledger(conn, tables, findings, options)
    _audit_semantic_dispositions(conn, tables, findings, options)
    _audit_ocr_governance(conn, tables, findings, options)
    _audit_evidence_replica_links(conn, tables, findings, options)
    _audit_observation_resolution(conn, tables, findings, options)
    _audit_fact_resolution_cutover(conn, tables, findings, options)
    _audit_fact_match_proofs(conn, tables, findings, options)
    _audit_fact_plane_v2(conn, tables, findings, options)
    _audit_filing_xbrl_processor_closure(conn, tables, findings, options)
    _audit_fact_selection(conn, tables, findings, options)
    _audit_issuer_registry(conn, tables, findings, options)
    _audit_reporting_identity(conn, tables, findings, options)
    _audit_evidence_subject_bindings(conn, tables, findings, options)
    _audit_source_coverage(conn, tables, findings, options)
    _audit_source_inventory_seals(conn, tables, findings, options)
    _audit_expectation_lifecycle(conn, tables, findings, options)
    _audit_search_corpus(conn, tables, findings, options)
    _audit_embedding_promotion(conn, tables, findings, options)
    _audit_ask_traces(conn, tables, findings, options)
    if options.verify_bytes:
        _audit_blob_bytes(conn, tables, findings, options)
    findings.sort(key=lambda item: (item.severity.value, item.code))
    return IntegrityAuditSummary(
        generated_at=datetime.now(UTC),
        findings=tuple(findings),
        has_blockers=any(finding.severity is Severity.BLOCKER for finding in findings),
        tables_present=tuple(sorted(tables)),
    )


def _audit_filing_xbrl_processor_closure(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    required = {
        "filing_xbrl_processor_artifacts",
        "filing_xbrl_extraction_input_members",
        "filing_xbrl_extraction_input_seals",
        "filing_xbrl_raw_fact_commitments",
        "filing_xbrl_footnote_commitments",
    }
    present = required & tables
    if not present:
        return
    missing = sorted(required - present)
    _add(
        findings,
        code="FILING_XBRL_PROCESSOR_SCHEMA_PARTIAL",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=len(missing),
        query_context="0254 filing-XBRL processor closure tables",
        samples=tuple(missing[: options.sample_limit]),
    )
    if missing:
        return
    _query_finding(
        conn,
        findings,
        options,
        code="FILING_XBRL_PROCESSOR_COORDINATES_UNQUALIFIED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT processor_artifact_id FROM filing_xbrl_processor_artifacts "
            "WHERE arelle_version<>'2.39.8' OR edgar_version<>'26.1' "
            "OR xule_version<>'30052' "
            "OR bridge_protocol_version<>'filing-xbrl-bridge.v1'"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FILING_XBRL_INPUT_SEAL_INCOMPLETE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT seal.extraction_run_id FROM filing_xbrl_extraction_input_seals seal "
            "LEFT JOIN filing_xbrl_extraction_input_members member "
            "ON member.extraction_run_id=seal.extraction_run_id "
            "GROUP BY seal.extraction_run_id,seal.member_count "
            "HAVING COUNT(member.input_member_id)<>seal.member_count "
            "OR MIN(member.member_ordinal)<>0 "
            "OR MAX(member.member_ordinal)<>seal.member_count-1 "
            "OR json_array_length(seal.canonical_member_set_json)<>seal.member_count "
            "OR json_array_length(seal.canonical_network_artifact_set_json)"
            "<>seal.network_artifact_count "
            "OR json_array_length(seal.canonical_footnote_set_json)<>seal.footnote_count"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FILING_XBRL_RESULT_SEAL_INCOMPLETE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT seal.extraction_run_id FROM filing_xbrl_extraction_input_seals seal "
            "LEFT JOIN filing_xbrl_raw_fact_commitments raw "
            "ON raw.extraction_run_id=seal.extraction_run_id "
            "LEFT JOIN filing_xbrl_footnote_commitments footnote "
            "ON footnote.extraction_run_id=raw.extraction_run_id "
            "AND footnote.input_ordinal=raw.input_ordinal "
            "GROUP BY seal.extraction_run_id,seal.raw_fact_count,seal.footnote_count,"
            "seal.zero_fact_disposition "
            "HAVING COUNT(DISTINCT raw.raw_fact_commitment_id)<>seal.raw_fact_count "
            "OR COUNT(footnote.footnote_commitment_id)<>seal.footnote_count "
            "OR (seal.raw_fact_count=0 "
            "AND seal.zero_fact_disposition<>'verified_no_inline_xbrl') "
            "OR (seal.raw_fact_count>0 AND seal.zero_fact_disposition IS NOT NULL)"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FILING_XBRL_RAW_FACT_ORPHANED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT raw.raw_fact_commitment_id "
            "FROM filing_xbrl_raw_fact_commitments raw "
            "LEFT JOIN evidence_nodes node ON node.node_id=raw.evidence_node_id "
            "LEFT JOIN filing_xbrl_extraction_input_members member "
            "ON member.extraction_run_id=raw.extraction_run_id "
            "AND member.member_ordinal=raw.package_member_ordinal "
            "AND member.blob_sha256=raw.package_member_blob_sha256 "
            "LEFT JOIN filing_xbrl_extraction_input_seals seal "
            "ON seal.extraction_run_id=raw.extraction_run_id "
            "WHERE node.node_id IS NULL OR node.extraction_run_id<>raw.extraction_run_id "
            "OR node.locator_sha256<>raw.source_locator_sha256 "
            "OR member.input_member_id IS NULL "
            "OR seal.accession_number<>raw.accession_number "
            "OR seal.expected_cik<>raw.observed_cik"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FILING_XBRL_FOOTNOTE_ORPHANED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT footnote.footnote_commitment_id "
            "FROM filing_xbrl_footnote_commitments footnote "
            "LEFT JOIN filing_xbrl_raw_fact_commitments raw "
            "ON raw.extraction_run_id=footnote.extraction_run_id "
            "AND raw.input_ordinal=footnote.input_ordinal "
            "WHERE raw.raw_fact_commitment_id IS NULL"
        ),
    )
    digest_mismatches: list[str] = []

    def canonical_json(value: object) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    for artifact in conn.execute(
        "SELECT processor_artifact_id,bundle_name,arelle_version,edgar_version,"
        "xule_version,bridge_protocol_version,artifact_sha256,"
        "sandbox_launcher_sha256,bundle_python_sha256,"
        "canonical_manifest_json,manifest_sha256 "
        "FROM filing_xbrl_processor_artifacts ORDER BY processor_artifact_id"
    ):
        artifact_id = str(artifact[0])
        try:
            manifest_json = str(artifact[9])
            manifest = _JSON_OBJECT_ADAPTER.validate_json(manifest_json)
            execution = _JSON_OBJECT_ADAPTER.validate_python(manifest["execution"])
            coordinates = _JSON_OBJECT_ADAPTER.validate_python(manifest["coordinates"])
            qualification = _JSON_OBJECT_ADAPTER.validate_python(manifest["qualification"])
            runtime_members = _JSON_OBJECT_LIST_ADAPTER.validate_python(
                execution["runtime_members"]
            )
            exact = (
                canonical_json(manifest) == manifest_json
                and hashlib.sha256(manifest_json.encode()).hexdigest() == str(artifact[10])
                and str(manifest["bundle_name"]) == str(artifact[1])
                and str(coordinates["arelle"]) == str(artifact[2])
                and str(coordinates["edgar"]) == str(artifact[3])
                and str(coordinates["xule"]) == str(artifact[4])
                and str(manifest["bridge_protocol_version"]) == str(artifact[5])
                and str(execution["sandbox_launcher_sha256"]) == str(artifact[7])
                and str(execution["bundle_python_sha256"]) == str(artifact[8])
                and str(execution["runtime_artifact_sha256"]) == str(artifact[6])
                and hashlib.sha256(canonical_json(runtime_members).encode()).hexdigest()
                == str(artifact[6])
                and qualification
                == {
                    "profile": "sec-inline-xbrl-investor-grade.v1",
                    "require_exact_coordinates": True,
                    "require_footnote_commitments": True,
                    "require_network_artifact_commitments": True,
                    "require_os_network_denial": True,
                    "require_runtime_artifact_sha256": True,
                    "require_sec_filing_identity": True,
                    "require_source_locator_commitments": True,
                    "require_zero_fact_host_verification": True,
                }
                and execution["internet_connectivity"] == "os_denied"
                and execution["sandbox_contract_version"] == "earnings-xbrl-os-sandbox.v1"
                and execution["isolated_python"] is True
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            exact = False
        if not exact:
            digest_mismatches.append(artifact_id)
    for row in conn.execute(
        "SELECT extraction_run_id,canonical_member_set_json,member_set_sha256,"
        "canonical_network_artifact_set_json,network_artifact_set_sha256,"
        "canonical_footnote_set_json,footnote_set_sha256,"
        "canonical_execution_evidence_json,execution_evidence_sha256,"
        "raw_fact_set_sha256,accession_number,expected_cik,processor_artifact_id,"
        "issuer_id "
        "FROM filing_xbrl_extraction_input_seals ORDER BY extraction_run_id"
    ):
        run_id = str(row[0])
        committed_sets = (
            (str(row[1]), str(row[2])),
            (str(row[3]), str(row[4])),
            (str(row[5]), str(row[6])),
            (str(row[7]), str(row[8])),
        )
        if any(
            hashlib.sha256(canonical.encode()).hexdigest() != expected
            for canonical, expected in committed_sets
        ):
            digest_mismatches.append(run_id)
            continue
        member_payload: list[dict[str, object]] = []
        member_mismatch = False
        for member in conn.execute(
            "SELECT member_ordinal,member_role,document_version_id,source_url,"
            "blob_sha256,byte_size,media_type,canonical_member_json,member_sha256 "
            "FROM filing_xbrl_extraction_input_members "
            "WHERE extraction_run_id=? ORDER BY member_ordinal",
            (run_id,),
        ):
            payload: dict[str, object] = {
                "blob_sha256": str(member[4]),
                "byte_size": int(member[5]),
                "document_version_id": (None if member[2] is None else str(member[2])),
                "media_type": str(member[6]),
                "member_ordinal": int(member[0]),
                "member_role": str(member[1]),
                "source_url": str(member[3]),
            }
            canonical_member = canonical_json(payload)
            if canonical_member != str(member[7]) or hashlib.sha256(
                canonical_member.encode()
            ).hexdigest() != str(member[8]):
                member_mismatch = True
            member_payload.append(payload)
        canonical_members = canonical_json(member_payload)
        try:
            sealed_network = _JSON_OBJECT_LIST_ADAPTER.validate_json(str(row[3]))
        except ValueError:
            sealed_network = None
        expected_network = sorted(
            (
                str(member["source_url"]),
                str(member["blob_sha256"]),
            )
            for member in member_payload
            if member["member_role"] in {"issuer_taxonomy", "standard_taxonomy", "network_artifact"}
        )
        network_shape_exact = sealed_network is not None and all(
            set(item) == {"source_url", "blob_sha256"} for item in sealed_network
        )
        actual_network: list[tuple[str, str]] | None = None
        if network_shape_exact and sealed_network is not None:
            actual_network = sorted(
                (str(item["source_url"]), str(item["blob_sha256"])) for item in sealed_network
            )
        raw_payload: list[dict[str, object]] = []
        source_entry_mismatch = False
        for raw in conn.execute(
            "SELECT input_ordinal,raw_fact_sha256,source_entry_sha256,"
            "source_locator_sha256,accession_number,observed_cik,"
            "package_member_ordinal,package_member_blob_sha256,"
            "canonical_raw_fact_json "
            "FROM filing_xbrl_raw_fact_commitments "
            "WHERE extraction_run_id=? ORDER BY input_ordinal",
            (run_id,),
        ):
            raw_payload.append(
                {
                    "input_ordinal": int(raw[0]),
                    "raw_fact_sha256": str(raw[1]),
                    "source_entry_sha256": str(raw[2]),
                    "source_locator_sha256": str(raw[3]),
                }
            )
            source_entry = {
                "accession_number": str(raw[4]),
                "observed_cik": str(raw[5]),
                "package_member_blob_sha256": str(raw[7]),
                "package_member_ordinal": int(raw[6]),
                "raw_fact_sha256": str(raw[1]),
                "source_locator_sha256": str(raw[3]),
            }
            canonical_entry = json.dumps(
                source_entry,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if hashlib.sha256(canonical_entry.encode()).hexdigest() != str(raw[2]):
                source_entry_mismatch = True
            if hashlib.sha256(str(raw[8]).encode()).hexdigest() != str(raw[1]):
                source_entry_mismatch = True
        canonical_raw = json.dumps(
            raw_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        footnote_payload: list[dict[str, object]] = []
        footnote_mismatch = False
        for footnote in conn.execute(
            "SELECT input_ordinal,footnote_ordinal,canonical_footnote_json,"
            "footnote_sha256 FROM filing_xbrl_footnote_commitments "
            "WHERE extraction_run_id=? ORDER BY input_ordinal,footnote_ordinal",
            (run_id,),
        ):
            try:
                footnote_body = json.loads(str(footnote[2]))
            except json.JSONDecodeError:
                footnote_mismatch = True
                continue
            canonical_footnote = canonical_json(footnote_body)
            if canonical_footnote != str(footnote[2]) or hashlib.sha256(
                canonical_footnote.encode()
            ).hexdigest() != str(footnote[3]):
                footnote_mismatch = True
            footnote_payload.append(
                {
                    "canonical_footnote": footnote_body,
                    "footnote_ordinal": int(footnote[1]),
                    "footnote_sha256": str(footnote[3]),
                    "input_ordinal": int(footnote[0]),
                }
            )
        canonical_footnotes = canonical_json(footnote_payload)
        try:
            execution_evidence = _JSON_OBJECT_ADAPTER.validate_json(str(row[7]))
        except ValueError:
            execution_evidence = None
        artifact_runtime = conn.execute(
            "SELECT artifact_sha256 FROM filing_xbrl_processor_artifacts "
            "WHERE processor_artifact_id=?",
            (str(row[12]),),
        ).fetchone()
        execution_evidence_exact = isinstance(execution_evidence, dict) and execution_evidence == {
            "accession_number": str(row[10]),
            "expected_cik": str(row[11]),
            "internet_connectivity": "os_denied",
            "network_requests_observed": 0,
            "package_member_set_sha256": str(row[2]),
            "runtime_artifact_sha256": (
                None if artifact_runtime is None else str(artifact_runtime[0])
            ),
            "sandbox_contract_version": "earnings-xbrl-os-sandbox.v1",
        }
        if (
            member_mismatch
            or canonical_members != str(row[1])
            or actual_network != expected_network
            or source_entry_mismatch
            or hashlib.sha256(canonical_raw.encode()).hexdigest() != str(row[9])
            or footnote_mismatch
            or canonical_footnotes != str(row[5])
            or not execution_evidence_exact
        ):
            digest_mismatches.append(run_id)
    _add(
        findings,
        code="FILING_XBRL_RESULT_COMMITMENT_DIGEST_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        count=len(digest_mismatches),
        query_context="recomputed filing-XBRL input/network/raw-fact/footnote sets",
        samples=tuple(digest_mismatches[: options.sample_limit]),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FILING_XBRL_RAW_FACT_DISPOSITION_GAP",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT raw.extraction_run_id,raw.input_ordinal "
            "FROM filing_xbrl_raw_fact_commitments raw "
            "LEFT JOIN filing_xbrl_extraction_dispositions disposition "
            "ON disposition.extraction_run_id=raw.extraction_run_id "
            "AND disposition.input_ordinal=raw.input_ordinal "
            "WHERE disposition.disposition_id IS NULL "
            "OR (raw.normalization_outcome='rejected' "
            "AND disposition.disposition<>'quarantined')"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FILING_XBRL_EVIDENCE_OR_CLOCK_BINDING_GAP",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT member.input_member_id "
            "FROM filing_xbrl_extraction_input_members member "
            "JOIN filing_xbrl_extraction_input_seals seal "
            "ON seal.extraction_run_id=member.extraction_run_id "
            "LEFT JOIN evidence_content_blobs blob ON blob.sha256=member.blob_sha256 "
            "LEFT JOIN evidence_document_versions document "
            "ON document.document_version_id=member.document_version_id "
            "LEFT JOIN evidence_extraction_runs run "
            "ON run.extraction_run_id=member.extraction_run_id "
            "WHERE blob.sha256 IS NULL OR blob.byte_size<>member.byte_size "
            "OR blob.media_type<>member.media_type "
            "OR julianday(blob.recorded_at)>julianday(seal.recorded_at) "
            "OR julianday(member.recorded_at)<>julianday(seal.recorded_at) "
            "OR run.extractor_name<>'filing-native-xbrl' OR run.outcome<>'succeeded' "
            "OR julianday(run.started_at)<>julianday(seal.recorded_at) "
            "OR julianday(run.completed_at)<>julianday(seal.recorded_at) "
            "OR (member.document_version_id IS NOT NULL "
            "AND (document.document_version_id IS NULL "
            "OR document.blob_sha256<>member.blob_sha256 "
            "OR (member.member_role='primary_document' "
            "AND document.issuer_id<>seal.issuer_id) "
            "OR julianday(document.recorded_at)>julianday(seal.recorded_at))) "
            "OR (member.document_version_id IS NULL AND NOT EXISTS (SELECT 1 "
            "FROM evidence_source_observations observation "
            "WHERE observation.source_url=member.source_url "
            "AND observation.blob_sha256=member.blob_sha256 "
            "AND julianday(observation.retrieved_at)<=julianday(seal.recorded_at)))"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FILING_XBRL_CIK_ISSUER_BINDING_GAP",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT seal.extraction_run_id "
            "FROM filing_xbrl_extraction_input_seals seal "
            "WHERE NOT EXISTS (SELECT 1 "
            "FROM issuer_identifier_resolution_outcomes resolution "
            "JOIN issuer_identifier_assertions assertion "
            "ON assertion.assertion_id=resolution.selected_assertion_id "
            "WHERE resolution.resolution_key='sec_cik:'||seal.expected_cik "
            "AND resolution.outcome='selected' "
            "AND assertion.issuer_id=seal.issuer_id "
            "AND resolution.knowledge_at<=seal.recorded_at "
            "AND assertion.knowledge_at<=seal.recorded_at "
            "AND NOT EXISTS (SELECT 1 "
            "FROM issuer_identifier_resolution_outcomes newer "
            "WHERE newer.resolution_key=resolution.resolution_key "
            "AND newer.knowledge_at<=seal.recorded_at "
            "AND newer.revision>resolution.revision))"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FILING_XBRL_PUBLICATION_CLOCK_OR_CLOSURE_GAP",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT seal.extraction_run_id "
            "FROM filing_xbrl_extraction_input_seals seal "
            "LEFT JOIN filing_xbrl_extraction_disposition_seals disposition_seal "
            "ON disposition_seal.extraction_run_id=seal.extraction_run_id "
            "WHERE disposition_seal.extraction_run_id IS NULL "
            "OR disposition_seal.entry_count<>seal.raw_fact_count "
            "OR julianday(disposition_seal.recorded_at)<>julianday(seal.recorded_at) "
            "OR julianday(disposition_seal.knowledge_at)<>julianday(seal.recorded_at) "
            "OR EXISTS (SELECT 1 FROM evidence_nodes node "
            "WHERE node.extraction_run_id=seal.extraction_run_id "
            "AND julianday(node.recorded_at)<>julianday(seal.recorded_at)) "
            "OR EXISTS (SELECT 1 FROM filing_xbrl_raw_fact_commitments raw "
            "WHERE raw.extraction_run_id=seal.extraction_run_id "
            "AND julianday(raw.recorded_at)<>julianday(seal.recorded_at)) "
            "OR EXISTS (SELECT 1 FROM filing_xbrl_footnote_commitments footnote "
            "WHERE footnote.extraction_run_id=seal.extraction_run_id "
            "AND julianday(footnote.recorded_at)<>julianday(seal.recorded_at)) "
            "OR EXISTS (SELECT 1 FROM filing_xbrl_extraction_dispositions disposition "
            "WHERE disposition.extraction_run_id=seal.extraction_run_id "
            "AND (julianday(disposition.recorded_at)<>julianday(seal.recorded_at) "
            "OR julianday(disposition.knowledge_at)<>julianday(seal.recorded_at)))"
        ),
    )


def audit_cutover_readiness(
    conn: sqlite3.Connection,
    options: CutoverAuditOptions,
) -> CutoverReadinessSummary:
    """Strictly verify every cutover artifact admitted at one explicit cutoff.

    Candidate enumeration is keyset-stable and fetched in bounded pages.  The
    public ledger verifiers remain the authority for commitment semantics; this
    layer owns only cutoff selection, exact coverage accounting, and bounded
    failure reporting.
    """

    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    tables = _table_names(conn)
    findings: list[IntegrityFinding] = []
    coverage: list[CutoverGateCoverage] = []
    candidate_commitments: list[CutoverGateCandidateCommitment] = []
    knowledge_cutoff = _canonical_datetime(options.knowledge_cutoff)
    observed_through = _canonical_datetime(options.observed_through)
    params = {
        "knowledge_cutoff": knowledge_cutoff,
        "observed_through": observed_through,
    }

    try:
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="source_fact_publications",
                selection_policy_id="source-fact-publications.K-created.O-recorded.v1",
                key_column="publication_id",
                query=(
                    "SELECT publication_id FROM source_fact_publications "
                    "WHERE julianday(created_at)<=julianday(:knowledge_cutoff) "
                    "AND julianday(recorded_at)<=julianday(:observed_through) "
                    "ORDER BY publication_id"
                ),
            ),
            params=params,
            verifier=lambda row: verify_source_fact_publication(
                conn,
                publication_id=str(row["publication_id"]),
                cutoff=options.cutoff_at,
                observed_through=options.observed_through,
            ),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="source_fact_publication_stream",
                selection_policy_id="source-fact-publication-stream.K-created.O-recorded.v1",
                key_column="publication_id",
                query=(
                    "SELECT publication_id FROM source_fact_publications "
                    "WHERE julianday(created_at)<=julianday(:knowledge_cutoff) "
                    "AND julianday(recorded_at)<=julianday(:observed_through) "
                    "ORDER BY publication_id"
                ),
            ),
            params=params,
            verifier=lambda row: publication_event_for_publication(
                conn,
                publication_id=str(row["publication_id"]),
            ),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="filing_xbrl_dispositions",
                selection_policy_id="filing-xbrl-dispositions.K-knowledge.O-recorded.v1",
                key_column="extraction_run_id",
                query=(
                    "SELECT DISTINCT extraction_run_id "
                    "FROM filing_xbrl_extraction_dispositions "
                    "WHERE julianday(knowledge_at)<=julianday(:knowledge_cutoff) "
                    "AND julianday(recorded_at)<=julianday(:observed_through) "
                    "ORDER BY extraction_run_id"
                ),
            ),
            params=params,
            verifier=lambda row: _verify_filing_disposition_cutover(
                conn,
                extraction_run_id=str(row["extraction_run_id"]),
                cutoff_at=options.cutoff_at,
                observed_through=options.observed_through,
            ),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="ontology_snapshots",
                selection_policy_id="ontology-snapshots.K-cutoff.O-recorded.v1",
                key_column="ontology_snapshot_id",
                query=(
                    "SELECT ontology_snapshot_id FROM ontology_snapshot_headers "
                    "WHERE julianday(cutoff_at)=julianday(:knowledge_cutoff) "
                    "AND julianday(recorded_at)<=julianday(:observed_through) "
                    "ORDER BY ontology_snapshot_id"
                ),
            ),
            params=params,
            verifier=lambda row: MetricOntology(conn).verify_snapshot(
                str(row["ontology_snapshot_id"])
            ),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="canonical_resolution_snapshots",
                selection_policy_id="canonical-resolution.K-cutoff.O-recorded.v1",
                key_column="resolution_snapshot_id",
                query=(
                    "SELECT resolution_snapshot_id "
                    "FROM canonical_fact_resolution_snapshot_seals "
                    "WHERE julianday(cutoff_at)=julianday(:knowledge_cutoff) "
                    "AND julianday(recorded_at)<=julianday(:observed_through) "
                    "ORDER BY resolution_snapshot_id"
                ),
            ),
            params=params,
            verifier=lambda row: _verify_resolution_cutover(
                conn,
                resolution_snapshot_id=str(row["resolution_snapshot_id"]),
                cutoff_at=options.cutoff_at,
                observed_through=options.observed_through,
            ),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="canonical_projection_generations",
                selection_policy_id="canonical-projection.K-cutoff.O-recorded.v1",
                key_column="generation_id",
                query=(
                    "SELECT generation_id,resolution_snapshot_id,ontology_snapshot_id "
                    "FROM canonical_fact_projection_generations "
                    "WHERE julianday(cutoff_at)=julianday(:knowledge_cutoff) "
                    "AND julianday(recorded_at)<=julianday(:observed_through) "
                    "ORDER BY generation_id"
                ),
            ),
            params=params,
            verifier=lambda row: verify_canonical_projection_generation(
                conn,
                str(row["generation_id"]),
                resolution_snapshot_id=str(row["resolution_snapshot_id"]),
                ontology_snapshot_id=str(row["ontology_snapshot_id"]),
                cutoff_at=options.cutoff_at,
            ),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="document_processing_evidence",
                selection_policy_id="document-processing-evidence.K-cutoff.O-recorded.v1",
                key_column="evidence_seal_id",
                query=(
                    "SELECT evidence_seal_id,document_version_id,processing_lane "
                    "FROM document_processing_evidence_headers "
                    "WHERE julianday(cutoff_at)=julianday(:knowledge_cutoff) "
                    "AND julianday(recorded_at)<=julianday(:observed_through) "
                    "ORDER BY evidence_seal_id"
                ),
            ),
            params=params,
            verifier=lambda row: verify_document_processing_evidence(
                conn,
                str(row["evidence_seal_id"]),
                document_version_id=str(row["document_version_id"]),
                processing_lane=str(row["processing_lane"]),
                cutoff_at=options.cutoff_at,
            ),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="document_processing_snapshots",
                selection_policy_id="document-processing-snapshots.K-cutoff.O-recorded.v1",
                key_column="processing_snapshot_id",
                query=(
                    "SELECT processing_snapshot_id "
                    "FROM document_processing_snapshot_headers "
                    "WHERE julianday(cutoff_at)=julianday(:knowledge_cutoff) "
                    "AND julianday(recorded_at)<=julianday(:observed_through) "
                    "ORDER BY processing_snapshot_id"
                ),
            ),
            params=params,
            verifier=lambda row: verify_processing_snapshot(
                conn, str(row["processing_snapshot_id"])
            ),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="research_snapshots",
                selection_policy_id="research-snapshots.K-cutoff.O-recorded.v1",
                key_column="research_snapshot_id",
                query=(
                    "SELECT research_snapshot_id FROM research_snapshot_headers "
                    "WHERE julianday(cutoff_at)=julianday(:knowledge_cutoff) "
                    "AND julianday(recorded_at)<=julianday(:observed_through) "
                    "ORDER BY research_snapshot_id"
                ),
            ),
            params=params,
            verifier=lambda row: verify_research_snapshot(conn, str(row["research_snapshot_id"])),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="heterogeneous_retrieval_traces",
                selection_policy_id="retrieval-traces.K-cutoff.O-recorded.v1",
                key_column="trace_id",
                query=(
                    "SELECT trace_id FROM heterogeneous_retrieval_trace_headers "
                    "WHERE julianday(cutoff_at)=julianday(:knowledge_cutoff) "
                    "AND julianday(recorded_at)<=julianday(:observed_through) "
                    "ORDER BY trace_id"
                ),
            ),
            params=params,
            verifier=lambda row: verify_heterogeneous_retrieval_trace(conn, str(row["trace_id"])),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="embedding_runtime_promotions",
                selection_policy_id="embedding-promotions.operational.O-approved.v1",
                key_column="promotion_id",
                query=(
                    "SELECT promotion_id,idempotency_key,purpose,revision,provider,model,"
                    "dimensions,golden_sha256,evaluation_artifact_sha256,"
                    "evaluation_metrics_json,runtime_artifact_json,"
                    "runtime_artifact_sha256,approved_by,approved_at,"
                    "supersedes_promotion_id FROM search_embedding_model_promotions "
                    "WHERE julianday(approved_at)<=julianday(:observed_through) "
                    "AND julianday(:knowledge_cutoff)<=julianday(:observed_through) "
                    "ORDER BY promotion_id"
                ),
            ),
            params=params,
            verifier=lambda row: EmbeddingPromotion.model_validate(dict(row)),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="embedding_runtime_artifacts",
                selection_policy_id="embedding-artifacts.K-manifest.O-completed.v1",
                key_column="embedding_artifact_id",
                query=(
                    "SELECT artifact.embedding_artifact_id,artifact.index_run_id,"
                    "artifact.provider,artifact.model,artifact.dimensions,"
                    "artifact.runtime_artifact_sha256 "
                    "FROM search_embedding_artifacts artifact "
                    "JOIN search_index_runs run ON run.index_run_id=artifact.index_run_id "
                    "JOIN search_corpus_manifests manifest "
                    "ON manifest.manifest_id=run.manifest_id "
                    "WHERE artifact.outcome='succeeded' "
                    "AND julianday(manifest.knowledge_cutoff)<=julianday(:knowledge_cutoff) "
                    "AND julianday(artifact.completed_at)<=julianday(:observed_through) "
                    "ORDER BY artifact.embedding_artifact_id"
                ),
            ),
            params=params,
            verifier=lambda row: _verify_embedding_artifact_binding(
                conn, row, cutoff_at=options.cutoff_at
            ),
        )
        _run_cutover_gate(
            conn,
            tables,
            findings,
            coverage,
            options,
            candidate_commitments=candidate_commitments,
            spec=CutoverGateQuerySpec(
                gate="embedding_runtime_projection_seals",
                selection_policy_id="embedding-projections.K-manifest.O-sealed.v1",
                key_column="projection_seal_id",
                query=(
                    "SELECT seal.projection_seal_id,seal.index_run_id "
                    "FROM search_projection_seals seal "
                    "JOIN search_index_runs run ON run.index_run_id=seal.index_run_id "
                    "JOIN search_corpus_manifests manifest "
                    "ON manifest.manifest_id=run.manifest_id "
                    "WHERE seal.index_kind='vector' "
                    "AND julianday(manifest.knowledge_cutoff)<=julianday(:knowledge_cutoff) "
                    "AND julianday(seal.sealed_at)<=julianday(:observed_through) "
                    "ORDER BY seal.projection_seal_id"
                ),
            ),
            params=params,
            verifier=lambda row: _verify_embedding_projection_binding(
                conn,
                index_run_id=str(row["index_run_id"]),
                cutoff_at=options.cutoff_at,
            ),
        )
    finally:
        conn.row_factory = original_row_factory

    findings.sort(key=lambda item: (item.severity.value, item.code))
    return CutoverReadinessSummary(
        knowledge_cutoff=options.knowledge_cutoff,
        observed_through=options.observed_through,
        generated_at=datetime.now(UTC),
        coverage=tuple(coverage),
        candidate_commitments=tuple(candidate_commitments),
        findings=tuple(findings),
        has_blockers=any(item.severity is Severity.BLOCKER for item in findings),
        tables_present=tuple(sorted(tables)),
    )


def _run_cutover_gate(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    coverage: list[CutoverGateCoverage],
    options: CutoverAuditOptions,
    *,
    candidate_commitments: list[CutoverGateCandidateCommitment],
    spec: CutoverGateQuerySpec,
    params: Mapping[str, object],
    verifier: Callable[[sqlite3.Row], object],
) -> None:
    gate = spec.gate
    required = set(_CUTOVER_GATE_TABLES[gate])
    missing = tuple(sorted(required - tables))
    if missing:
        findings.append(
            IntegrityFinding(
                code=f"CUTOVER_{gate.upper()}_SCHEMA_MISSING",
                severity=Severity.BLOCKER,
                remediation=RemediationClass.HARD_STOP,
                count=len(missing),
                query_context="sqlite_master required-table inventory",
                samples=missing[: options.sample_limit],
            )
        )
        coverage.append(
            CutoverGateCoverage(
                gate=gate,
                eligible_count=0,
                verified_count=0,
                failed_count=0,
            )
        )
        return

    scoped_query = spec.query.strip().removesuffix(";")
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM ({scoped_query}) AS cutover_candidates",  # nosec B608 -- trusted internal SQL shape; values remain bound
        params,
    ).fetchone()
    eligible = 0 if count_row is None else int(count_row[0])
    verified = 0
    failed = 0
    samples: list[str] = []
    candidate_digest = hashlib.sha256()
    candidate_digest.update(
        _canonical_commitment_json(
            {
                "gate": gate,
                "selection_policy_id": spec.selection_policy_id,
            }
        ).encode()
    )
    candidate_count = 0
    after_key: str | None = None
    page_query = (
        "SELECT * FROM ("  # nosec B608 -- validated internal selector and key identifier; temporal values remain bound
        f"{scoped_query}) AS cutover_candidates "
        f"WHERE (:_cutover_after_key IS NULL OR {spec.key_column}>:_cutover_after_key) "
        f"ORDER BY {spec.key_column} LIMIT :_cutover_page_size"
    )
    while True:
        page_params = {
            **params,
            "_cutover_after_key": after_key,
            "_cutover_page_size": options.fetch_size,
        }
        rows = conn.execute(page_query, page_params).fetchall()
        if not rows:
            break
        for row in rows:
            candidate_digest.update(b"\n")
            candidate_digest.update(_canonical_commitment_json(dict(row)).encode())
            candidate_count += 1
            key = str(row[spec.key_column])
            if after_key is not None and key <= after_key:
                raise RuntimeError("cutover gate keyset is not strictly increasing")
            try:
                verifier(row)
            except (
                KeyError,
                TypeError,
                ValueError,
                RuntimeError,
                sqlite3.DatabaseError,
            ) as exc:
                failed += 1
                if len(samples) < options.sample_limit:
                    samples.append(_cutover_failure_sample(key, exc))
            else:
                verified += 1
            after_key = key
    if candidate_count != eligible:
        raise RuntimeError("cutover gate candidate count changed during verification")
    candidate_commitments.append(
        CutoverGateCandidateCommitment(
            gate=gate,
            selection_policy_id=spec.selection_policy_id,
            row_count=candidate_count,
            rows_sha256=candidate_digest.hexdigest(),
        )
    )
    coverage.append(
        CutoverGateCoverage(
            gate=gate,
            eligible_count=eligible,
            verified_count=verified,
            failed_count=failed,
        )
    )
    if eligible == 0:
        findings.append(
            IntegrityFinding(
                code=f"CUTOVER_{gate.upper()}_COVERAGE_EMPTY",
                severity=Severity.BLOCKER,
                remediation=RemediationClass.BACKFILL,
                count=1,
                query_context=spec.selection_policy_id,
                samples=(
                    _canonical_datetime(options.knowledge_cutoff)
                    + "|"
                    + _canonical_datetime(options.observed_through),
                ),
            )
        )
    if failed:
        findings.append(
            IntegrityFinding(
                code=f"CUTOVER_{gate.upper()}_VERIFICATION_FAILED",
                severity=Severity.BLOCKER,
                remediation=RemediationClass.HARD_STOP,
                count=failed,
                query_context=spec.selection_policy_id,
                samples=tuple(samples),
            )
        )


def _cutover_failure_sample(key: str, exc: BaseException) -> str:
    detail = " ".join(str(exc).replace("|", "/").split())
    return f"{key}|{type(exc).__name__}|{detail}"[:500]


def _verify_resolution_cutover(
    conn: sqlite3.Connection,
    *,
    resolution_snapshot_id: str,
    cutoff_at: datetime,
    observed_through: datetime,
) -> None:
    CanonicalFactResolutionEngine(conn).verify_snapshot(
        resolution_snapshot_id,
        cutoff_at,
        observed_through=observed_through,
    )
    verify_resolution_snapshot_watermark(
        conn,
        resolution_snapshot_id=resolution_snapshot_id,
        cutoff_at=cutoff_at,
        observed_through=observed_through,
    )


def _verify_filing_disposition_cutover(
    conn: sqlite3.Connection,
    *,
    extraction_run_id: str,
    cutoff_at: datetime,
    observed_through: datetime,
) -> None:
    seal_row = conn.execute(
        "SELECT * FROM filing_xbrl_extraction_disposition_seals WHERE extraction_run_id=?",
        (extraction_run_id,),
    ).fetchone()
    if seal_row is None:
        raise ValueError("filing-XBRL extraction has no disposition seal")
    seal = FilingXbrlExtractionDispositionSeal.model_validate(dict(seal_row))
    rows = conn.execute(
        "SELECT * FROM filing_xbrl_extraction_dispositions "
        "WHERE extraction_run_id=? ORDER BY input_ordinal",
        (extraction_run_id,),
    ).fetchall()
    records = tuple(FilingXbrlExtractionDispositionRecord.model_validate(dict(row)) for row in rows)
    disposition_payloads: list[dict[str, JsonValue]] = []
    for record in records:
        normalized = _JSON_OBJECT_ADAPTER.validate_json(record.canonical_normalized_entry_json)
        disposition = _JSON_OBJECT_ADAPTER.validate_json(record.canonical_disposition_json)
        if (
            record.canonical_normalized_entry_json != publication_canonical_json(normalized)
            or record.normalized_entry_sha256
            != publication_digest_text(record.canonical_normalized_entry_json)
            or record.canonical_disposition_json != publication_canonical_json(disposition)
            or record.disposition_sha256
            != publication_digest_text(record.canonical_disposition_json)
        ):
            raise ValueError("filing-XBRL disposition member commitment mismatch")
        disposition_payloads.append(disposition)
    disposition_set_json = publication_canonical_json(disposition_payloads)
    disposition_counts = {
        disposition: sum(record.disposition == disposition for record in records)
        for disposition in ("published", "duplicate", "quarantined")
    }
    if (
        seal.entry_count != len(records)
        or seal.published_count != disposition_counts["published"]
        or seal.duplicate_count != disposition_counts["duplicate"]
        or seal.quarantined_count != disposition_counts["quarantined"]
        or seal.canonical_disposition_set_json != disposition_set_json
        or seal.disposition_set_sha256 != publication_digest_text(disposition_set_json)
    ):
        raise ValueError("filing-XBRL disposition final seal mismatch")
    verify_source_fact_publication(
        conn,
        publication_id=seal.publication_id,
        cutoff=cutoff_at,
        observed_through=observed_through,
    )


def _promotion_for_runtime_coordinate(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    dimensions: int,
    runtime_artifact_sha256: str,
    cutoff_at: datetime,
) -> EmbeddingPromotion:
    row = conn.execute(
        "SELECT promotion_id,idempotency_key,purpose,revision,provider,model,"
        "dimensions,golden_sha256,evaluation_artifact_sha256,"
        "evaluation_metrics_json,runtime_artifact_json,runtime_artifact_sha256,"
        "approved_by,approved_at,supersedes_promotion_id "
        "FROM search_embedding_model_promotions "
        "WHERE provider=? AND model=? AND dimensions=? "
        "AND runtime_artifact_sha256=? "
        "AND julianday(approved_at)<=julianday(?) "
        "ORDER BY approved_at DESC,promotion_id DESC LIMIT 1",
        (
            provider,
            model,
            dimensions,
            runtime_artifact_sha256,
            _canonical_datetime(cutoff_at),
        ),
    ).fetchone()
    if row is None:
        raise ValueError("runtime artifact is not bound to a cutoff-eligible promotion")
    return EmbeddingPromotion.model_validate(dict(row))


def _verify_embedding_artifact_binding(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    cutoff_at: datetime,
) -> None:
    runtime_sha = row["runtime_artifact_sha256"]
    if runtime_sha is None:
        raise ValueError("successful embedding artifact lacks runtime binding")
    _promotion_for_runtime_coordinate(
        conn,
        provider=str(row["provider"]),
        model=str(row["model"]),
        dimensions=int(row["dimensions"]),
        runtime_artifact_sha256=str(runtime_sha),
        cutoff_at=cutoff_at,
    )


def _verify_embedding_projection_binding(
    conn: sqlite3.Connection,
    *,
    index_run_id: str,
    cutoff_at: datetime,
) -> None:
    seal = load_projection_seal(conn, index_run_id=index_run_id)
    if (
        seal is None
        or seal.index_kind != "vector"
        or seal.provider is None
        or seal.model is None
        or seal.dimensions is None
        or seal.runtime_artifact_sha256 is None
    ):
        raise ValueError("vector projection seal lacks an exact runtime coordinate")
    verify_ledger_projection_seal(conn, seal)
    _promotion_for_runtime_coordinate(
        conn,
        provider=seal.provider,
        model=seal.model,
        dimensions=seal.dimensions,
        runtime_artifact_sha256=seal.runtime_artifact_sha256,
        cutoff_at=cutoff_at,
    )


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add(
    findings: list[IntegrityFinding],
    *,
    code: str,
    severity: Severity,
    remediation: RemediationClass,
    count: int,
    query_context: str,
    samples: tuple[str, ...],
) -> None:
    if count:
        findings.append(
            IntegrityFinding(
                code=code,
                severity=severity,
                remediation=remediation,
                count=count,
                query_context=query_context,
                samples=samples,
            )
        )


def _query_finding(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
    *,
    code: str,
    severity: Severity,
    remediation: RemediationClass,
    query: str,
) -> None:
    scoped_query = query.strip().removesuffix(";")
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM ({scoped_query}) AS audit_violations"  # nosec B608 -- trusted internal SQL shape; values remain bound
    ).fetchone()
    count = 0 if count_row is None else int(count_row[0])
    rows = (
        []
        if count == 0
        else conn.execute(
            f"SELECT * FROM ({scoped_query}) AS audit_violations LIMIT ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
            (options.sample_limit,),
        ).fetchmany(options.sample_limit)
    )
    _add(
        findings,
        code=code,
        severity=severity,
        remediation=remediation,
        count=count,
        query_context=query,
        samples=tuple(_render_row(row) for row in rows[: options.sample_limit]),
    )


append_query_finding = _query_finding


def _render_row(row: sqlite3.Row | tuple[object, ...]) -> str:
    return "|".join("NULL" if value is None else str(value) for value in row)


def _audit_sqlite_pragmas(
    conn: sqlite3.Connection, findings: list[IntegrityFinding], options: AuditOptions
) -> None:
    foreign_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    _add(
        findings,
        code="SQLITE_FOREIGN_KEY_FAILURE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=len(foreign_rows),
        query_context="PRAGMA foreign_key_check",
        samples=tuple(_render_row(row) for row in foreign_rows[: options.sample_limit]),
    )
    integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
    bad_rows = [row for row in integrity_rows if str(row[0]).lower() != "ok"]
    _add(
        findings,
        code="SQLITE_INTEGRITY_FAILURE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=len(bad_rows),
        query_context="PRAGMA integrity_check",
        samples=tuple(_render_row(row) for row in bad_rows[: options.sample_limit]),
    )


def _audit_document_parents(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    if "documents" not in tables or "parent_document_id" not in _columns(conn, "documents"):
        return
    _query_finding(
        conn,
        findings,
        options,
        code="DOCUMENT_PARENT_DANGLING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT child.id, child.parent_document_id FROM documents AS child "
            "LEFT JOIN documents AS parent ON parent.id = child.parent_document_id "
            "WHERE child.parent_document_id IS NOT NULL AND parent.id IS NULL ORDER BY child.id"
        ),
    )


def _audit_lifecycle(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    _audit_lifecycle_table(
        conn,
        tables,
        findings,
        options,
        "transcripts",
        ("ticker", "fiscal_period_type", "period_end"),
    )
    _audit_lifecycle_table(
        conn,
        tables,
        findings,
        options,
        "filing_sections",
        ("source", "source_ref", "section_key_raw", "ordinal"),
    )


def _audit_lifecycle_table(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
    table: str,
    key_columns: tuple[str, ...],
) -> None:
    if table not in tables:
        return
    columns = _columns(conn, table)
    if not {"id", "is_active", "superseded_by_id"} <= columns or not set(key_columns) <= columns:
        _add(
            findings,
            code=f"{table.upper()}_LIFECYCLE_SCHEMA_ABSENT",
            severity=Severity.ADVISORY,
            remediation=RemediationClass.BACKFILL,
            count=1,
            query_context=f"PRAGMA table_info({table})",
            samples=("missing lifecycle columns",),
        )
        return
    key_sql = ", ".join(key_columns)
    _query_finding(
        conn,
        findings,
        options,
        code=f"{table.upper()}_ACTIVE_DUPLICATE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            f"SELECT {key_sql}, COUNT(*) FROM {table} WHERE is_active = 1 "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"GROUP BY {key_sql} HAVING COUNT(*) > 1 ORDER BY {key_sql}"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code=f"{table.upper()}_SUPERSESSION_BROKEN",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            f"SELECT child.id, child.superseded_by_id FROM {table} AS child "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"LEFT JOIN {table} AS successor ON successor.id = child.superseded_by_id "
            "WHERE child.superseded_by_id IS NOT NULL "
            "AND (successor.id IS NULL OR child.id = child.superseded_by_id) ORDER BY child.id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code=f"{table.upper()}_ACTIVE_SUPERSEDED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            f"SELECT id, superseded_by_id FROM {table} WHERE is_active = 1 "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "AND superseded_by_id IS NOT NULL ORDER BY id"
        ),
    )


def _audit_evidence_ledger(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_EVIDENCE_TABLES) & tables
    missing = set(_EVIDENCE_TABLES) - tables
    if not present:
        _add(
            findings,
            code="EVIDENCE_LEDGER_SCHEMA_ABSENT",
            severity=Severity.ADVISORY,
            remediation=RemediationClass.BACKFILL,
            count=1,
            query_context="sqlite_master evidence-ledger table inventory",
            samples=("no evidence-ledger tables present",),
        )
        return
    if missing:
        _add(
            findings,
            code="EVIDENCE_LEDGER_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(missing),
            query_context="sqlite_master evidence-ledger table inventory",
            samples=tuple(sorted(missing)[: options.sample_limit]),
        )
        return
    _audit_append_only_triggers(conn, findings, options, _EVIDENCE_TABLES, "EVIDENCE")
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_DOCUMENT_BLOB_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT document_version_id FROM evidence_document_versions AS document "
            "LEFT JOIN evidence_source_observations AS observation ON observation.observation_id = document.observation_id "
            "WHERE observation.observation_id IS NULL OR observation.blob_sha256 <> document.blob_sha256 "
            "ORDER BY document_version_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_DOCUMENT_REVISION_CHAIN_BROKEN",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT document.document_version_id FROM evidence_document_versions AS document "
            "LEFT JOIN evidence_document_versions AS prior "
            "ON prior.document_version_id = document.replaces_document_version_id "
            "AND prior.document_key = document.document_key "
            "AND prior.version_sequence = document.version_sequence - 1 "
            "WHERE (document.version_sequence = 1 AND document.replaces_document_version_id IS NOT NULL) "
            "OR (document.version_sequence > 1 AND prior.document_version_id IS NULL) "
            "ORDER BY document.document_version_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_EXTRACTION_INPUT_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT extraction_run_id FROM evidence_extraction_runs AS run "
            "LEFT JOIN evidence_document_versions AS document ON document.document_version_id = run.document_version_id "
            "WHERE document.document_version_id IS NULL OR document.blob_sha256 <> run.input_sha256 "
            "ORDER BY extraction_run_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_NODE_REVISION_CHAIN_BROKEN",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT node.node_id FROM evidence_nodes AS node LEFT JOIN evidence_nodes AS prior "
            "ON prior.node_id = node.supersedes_node_id AND prior.evidence_key = node.evidence_key "
            "AND prior.revision = node.revision - 1 WHERE (node.revision = 1 AND node.supersedes_node_id IS NOT NULL) "
            "OR (node.revision > 1 AND prior.node_id IS NULL) ORDER BY node.node_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_NODE_PARENT_RUN_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT node.node_id FROM evidence_nodes AS node LEFT JOIN evidence_nodes AS parent "
            "ON parent.node_id = node.parent_node_id AND parent.extraction_run_id = node.extraction_run_id "
            "WHERE node.parent_node_id IS NOT NULL AND parent.node_id IS NULL ORDER BY node.node_id"
        ),
    )
    _audit_node_locator_hashes(conn, findings, options)


def _audit_node_locator_hashes(
    conn: sqlite3.Connection, findings: list[IntegrityFinding], options: AuditOptions
) -> None:
    rows = conn.execute(
        "SELECT node_id, locator_json, locator_sha256 FROM evidence_nodes "
        "WHERE locator_json IS NOT NULL OR locator_sha256 IS NOT NULL ORDER BY node_id"
    ).fetchall()
    invalid = [
        str(node_id)
        for node_id, locator_json, locator_sha256 in rows
        if locator_json is None
        or locator_sha256 is None
        or hashlib.sha256(str(locator_json).encode("utf-8")).hexdigest() != str(locator_sha256)
    ]
    _add(
        findings,
        code="EVIDENCE_NODE_LOCATOR_HASH_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        count=len(invalid),
        query_context="evidence_nodes.locator_json SHA-256 equals locator_sha256",
        samples=tuple(invalid[: options.sample_limit]),
    )


def _audit_semantic_dispositions(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    evidence_tables = {
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
    }
    if not evidence_tables.issubset(tables):
        return
    if "document_semantic_disposition_revisions" not in tables:
        _query_finding(
            conn,
            findings,
            options,
            code="SEMANTIC_DISPOSITION_SCHEMA_ABSENT",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.BACKFILL,
            query=_failed_substantive_extraction_query(require_missing_disposition=False),
        )
        return

    _audit_append_only_triggers(
        conn,
        findings,
        options,
        _SEMANTIC_DISPOSITION_TABLES,
        "SEMANTIC_DISPOSITION",
    )
    current_view_present = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'view' "
            "AND name = 'v_document_semantic_dispositions_current'"
        ).fetchone()
        is not None
    )
    _add(
        findings,
        code="SEMANTIC_DISPOSITION_CURRENT_VIEW_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=int(not current_view_present),
        query_context="sqlite_master semantic-disposition current-view inventory",
        samples=(() if current_view_present else ("v_document_semantic_dispositions_current",)),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SEMANTIC_DISPOSITION_REVISION_CHAIN_BROKEN",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT disposition.assessment_id "
            "FROM document_semantic_disposition_revisions AS disposition "
            "LEFT JOIN document_semantic_disposition_revisions AS prior "
            "ON prior.assessment_id = disposition.supersedes_assessment_id "
            "AND prior.document_version_id = disposition.document_version_id "
            "AND prior.revision = disposition.revision - 1 "
            "WHERE (disposition.revision = 1 "
            "AND disposition.supersedes_assessment_id IS NOT NULL) "
            "OR (disposition.revision > 1 AND prior.assessment_id IS NULL) "
            "ORDER BY disposition.assessment_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SEMANTIC_DISPOSITION_INVALID_RECORD",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT disposition.assessment_id "
            "FROM document_semantic_disposition_revisions AS disposition "
            "WHERE disposition.revision <= 0 "
            "OR disposition.semantic_status NOT IN "
            "('required', 'not_required', 'review_required', 'quarantined') "
            "OR disposition.decision_kind NOT IN ('deterministic', 'human', 'model_assisted') "
            "OR (disposition.decision_kind = 'human' "
            "AND (disposition.reviewer_identity IS NULL "
            "OR length(trim(disposition.reviewer_identity)) = 0)) "
            "OR (disposition.decision_kind <> 'human' "
            "AND disposition.reviewer_identity IS NOT NULL) "
            "OR length(trim(disposition.reason_code)) = 0 "
            "OR length(trim(disposition.policy_name)) = 0 "
            "OR length(trim(disposition.policy_version)) = 0 "
            "OR length(disposition.policy_config_sha256) <> 64 "
            "OR disposition.policy_config_sha256 GLOB '*[^0-9a-f]*' "
            "OR disposition.knowledge_at < disposition.effective_at "
            "OR disposition.recorded_at < disposition.knowledge_at "
            "OR disposition.material_dissent NOT IN (0, 1) "
            "OR CASE "
            "WHEN json_valid(disposition.reason_details_json) "
            "AND json_type(disposition.reason_details_json) = 'object' "
            "THEN NOT EXISTS (SELECT 1 FROM json_each(disposition.reason_details_json)) "
            "OR EXISTS (SELECT 1 FROM json_each(disposition.reason_details_json) AS detail "
            "WHERE length(trim(detail.key)) = 0 OR detail.type <> 'text' "
            "OR length(trim(CAST(detail.value AS TEXT))) = 0) "
            "ELSE 1 END "
            "ORDER BY disposition.assessment_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SEMANTIC_NOT_REQUIRED_UNAUTHORIZED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT assessment_id "
            "FROM document_semantic_disposition_revisions "
            "WHERE semantic_status = 'not_required' "
            "AND (decision_kind <> 'human' OR reviewer_identity IS NULL "
            "OR length(trim(reviewer_identity)) = 0) "
            "ORDER BY assessment_id"
        ),
    )
    if current_view_present:
        _query_finding(
            conn,
            findings,
            options,
            code="SEMANTIC_DISPOSITION_CURRENT_VIEW_MISMATCH",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            query=(
                "WITH expected AS MATERIALIZED ("
                "SELECT disposition.assessment_id "
                "FROM document_semantic_disposition_revisions AS disposition "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM document_semantic_disposition_revisions AS newer "
                "WHERE newer.document_version_id = disposition.document_version_id "
                "AND newer.revision > disposition.revision"
                ")), missing AS ("
                "SELECT assessment_id FROM expected EXCEPT "
                "SELECT assessment_id FROM v_document_semantic_dispositions_current"
                "), extra AS ("
                "SELECT assessment_id FROM v_document_semantic_dispositions_current EXCEPT "
                "SELECT assessment_id FROM expected"
                ") SELECT assessment_id FROM missing "
                "UNION SELECT assessment_id FROM extra ORDER BY assessment_id"
            ),
        )
    _query_finding(
        conn,
        findings,
        options,
        code="SEMANTIC_DISPOSITION_MISSING_AFTER_FAILED_EXTRACTION",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=_failed_substantive_extraction_query(require_missing_disposition=True),
    )
    if set(_SEARCH_TABLES).issubset(tables):
        _query_finding(
            conn,
            findings,
            options,
            code="SEMANTIC_DISPOSITION_CORPUS_CONTRADICTION",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.REINGEST,
            query=(
                "WITH current_disposition AS MATERIALIZED ("
                "SELECT disposition.* "
                "FROM document_semantic_disposition_revisions AS disposition "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM document_semantic_disposition_revisions AS newer "
                "WHERE newer.document_version_id = disposition.document_version_id "
                "AND newer.revision > disposition.revision"
                ")) "
                "SELECT membership.manifest_id, membership.document_version_id, "
                "membership.membership_status, disposition.semantic_status, membership.reason "
                "FROM search_corpus_document_memberships AS membership "
                "JOIN v_search_corpus_current AS manifest "
                "ON manifest.manifest_id = membership.manifest_id "
                "LEFT JOIN current_disposition AS disposition "
                "ON disposition.document_version_id = membership.document_version_id "
                "WHERE (membership.membership_status = 'included' "
                "AND disposition.semantic_status IN ('review_required', 'quarantined')) "
                "OR (membership.membership_status = 'included' "
                "AND disposition.semantic_status = 'not_required' "
                "AND (disposition.decision_kind <> 'human' "
                "OR disposition.reviewer_identity IS NULL "
                "OR length(trim(disposition.reviewer_identity)) = 0 "
                "OR membership.reason <> "
                "'semantic:not_required:' || disposition.assessment_id)) "
                "OR (membership.reason LIKE 'semantic:not_required:%' "
                "AND (membership.membership_status <> 'included' "
                "OR disposition.assessment_id IS NULL "
                "OR disposition.semantic_status <> 'not_required' "
                "OR disposition.decision_kind <> 'human' "
                "OR disposition.reviewer_identity IS NULL "
                "OR length(trim(disposition.reviewer_identity)) = 0 "
                "OR membership.reason <> "
                "'semantic:not_required:' || disposition.assessment_id)) "
                "ORDER BY membership.manifest_id, membership.document_version_id"
            ),
        )


def _failed_substantive_extraction_query(*, require_missing_disposition: bool) -> str:
    disposition_predicate = (
        "AND NOT EXISTS ("
        "SELECT 1 FROM document_semantic_disposition_revisions AS disposition "
        "WHERE disposition.document_version_id = failed.document_version_id "
        "AND NOT EXISTS ("
        "SELECT 1 FROM document_semantic_disposition_revisions AS newer "
        "WHERE newer.document_version_id = disposition.document_version_id "
        "AND newer.revision > disposition.revision"
        ")) "
        if require_missing_disposition
        else ""
    )
    return (
        "WITH failed_documents AS MATERIALIZED ("  # nosec B608 -- trusted internal SQL shape; values remain bound
        "SELECT DISTINCT document_version_id "
        "FROM evidence_extraction_runs "
        "WHERE extractor_name = 'fulltext-evidence-backfill' AND outcome = 'failed'"
        "), substantive_documents AS MATERIALIZED ("
        "SELECT DISTINCT run.document_version_id "
        "FROM evidence_extraction_runs AS run "
        "JOIN evidence_nodes AS node ON node.extraction_run_id = run.extraction_run_id "
        "WHERE run.outcome = 'succeeded' AND node.node_kind <> 'document' "
        "AND length(trim(node.text)) > 0 "
        "AND NOT EXISTS ("
        "SELECT 1 FROM evidence_nodes AS newer "
        "WHERE newer.evidence_key = node.evidence_key AND newer.revision > node.revision"
        ")) "
        "SELECT failed.document_version_id FROM failed_documents AS failed "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM substantive_documents AS substantive "
        "WHERE substantive.document_version_id = failed.document_version_id"
        ") "
        f"{disposition_predicate}"
        "ORDER BY failed.document_version_id"
    )


def _audit_append_only_triggers(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
    tables: tuple[str, ...],
    prefix: str,
) -> None:
    missing: list[str] = []
    for table in tables:
        names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
            )
        }
        for suffix in ("", "_delete"):
            expected = f"trg_{table}_append_only{suffix}"
            if expected not in names:
                missing.append(expected)
    _add(
        findings,
        code=f"{prefix}_APPEND_ONLY_TRIGGER_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=len(missing),
        query_context="sqlite_master trigger inventory",
        samples=tuple(missing[: options.sample_limit]),
    )


def _audit_issuer_registry(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_ISSUER_REGISTRY_TABLES) & tables
    if not present:
        _add(
            findings,
            code="IDENTITY_REGISTRY_SCHEMA_ABSENT",
            severity=Severity.ADVISORY,
            remediation=RemediationClass.BACKFILL,
            count=1,
            query_context="sqlite_master issuer-registry table inventory",
            samples=("no canonical issuer-registry tables present",),
        )
        return
    missing = set(_ISSUER_REGISTRY_TABLES) - tables
    if missing:
        _add(
            findings,
            code="IDENTITY_REGISTRY_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(missing),
            query_context="sqlite_master issuer-registry table inventory",
            samples=tuple(sorted(missing)[: options.sample_limit]),
        )
        return
    _audit_append_only_triggers(
        conn,
        findings,
        options,
        _ISSUER_REGISTRY_TABLES,
        "IDENTITY_REGISTRY",
    )
    evidence_count = (
        int(conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0])
        if "evidence_document_versions" in tables
        else 0
    )
    inventory_count = (
        int(conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0])
        if "source_inventory_snapshots" in tables
        else 0
    )
    entity_count = int(conn.execute("SELECT COUNT(*) FROM issuer_entities").fetchone()[0])
    if (evidence_count or inventory_count) and not entity_count:
        _add(
            findings,
            code="IDENTITY_REGISTRY_UNINITIALIZED",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.BACKFILL,
            count=1,
            query_context=(
                "issuer_entities empty while evidence_document_versions or "
                "source_inventory_snapshots is non-empty"
            ),
            samples=(f"evidence={evidence_count}|inventories={inventory_count}",),
        )
    if "evidence_document_versions" in tables:
        _query_finding(
            conn,
            findings,
            options,
            code="EVIDENCE_ISSUER_BINDING_MISSING",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.MANUAL,
            query=(
                "SELECT document.issuer_id, COUNT(*) "
                "FROM evidence_document_versions AS document "
                "LEFT JOIN issuer_entities AS canonical "
                "ON canonical.issuer_id = document.issuer_id "
                "LEFT JOIN v_legacy_issuer_bindings_current AS binding "
                "ON binding.recorded_issuer_id = document.issuer_id "
                "AND binding.outcome = 'selected' "
                "WHERE canonical.issuer_id IS NULL "
                "AND binding.canonical_issuer_id IS NULL "
                "GROUP BY document.issuer_id ORDER BY document.issuer_id"
            ),
        )
    if "source_inventory_snapshots" in tables:
        _query_finding(
            conn,
            findings,
            options,
            code="SOURCE_INVENTORY_ISSUER_NOT_CANONICAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.MANUAL,
            query=(
                "SELECT inventory.snapshot_id, inventory.issuer_id "
                "FROM source_inventory_snapshots AS inventory "
                "LEFT JOIN issuer_entities AS canonical "
                "ON canonical.issuer_id = inventory.issuer_id "
                "WHERE canonical.issuer_id IS NULL ORDER BY inventory.snapshot_id"
            ),
        )
        _query_finding(
            conn,
            findings,
            options,
            code="SOURCE_INVENTORY_AUTHORITY_SURFACE_MISSING",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.MANUAL,
            query=(
                "SELECT inventory.snapshot_id, inventory.issuer_id, inventory.source_kind "
                "FROM v_source_inventory_current AS inventory "
                "WHERE inventory.authoritative = 1 AND NOT EXISTS ("
                "SELECT 1 FROM v_issuer_authority_surfaces_current AS surface "
                "WHERE surface.issuer_id = inventory.issuer_id "
                "AND surface.status = 'verified' "
                "AND ((inventory.source_kind = 'sec_submissions' "
                "AND surface.surface_kind = 'sec_submissions' "
                "AND surface.source_url = inventory.source_url) "
                "OR (inventory.source_kind = 'ir_crawl' "
                "AND surface.surface_kind = 'ir_home' "
                "AND surface.source_url = inventory.source_url) "
                "OR (inventory.source_kind = 'earnings_events' "
                "AND surface.surface_kind IN ('earnings_feed', 'ir_events') "
                "AND surface.source_url = inventory.source_url))) "
                "ORDER BY inventory.snapshot_id"
            ),
        )
    if "expected_documents" in tables:
        _query_finding(
            conn,
            findings,
            options,
            code="EXPECTED_DOCUMENT_ISSUER_NOT_CANONICAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.MANUAL,
            query=(
                "SELECT expected.expected_document_id, expected.issuer_id "
                "FROM expected_documents AS expected "
                "LEFT JOIN issuer_entities AS canonical "
                "ON canonical.issuer_id = expected.issuer_id "
                "WHERE canonical.issuer_id IS NULL ORDER BY expected.expected_document_id"
            ),
        )
    _query_finding(
        conn,
        findings,
        options,
        code="ISSUER_IDENTIFIER_UNRESOLVED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT resolution_key FROM v_issuer_identifier_resolutions_current "
            "WHERE outcome = 'unresolved' ORDER BY resolution_key"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="ISSUER_IDENTIFIER_MATERIAL_DISSENT",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT resolution_key FROM v_issuer_identifier_resolutions_current "
            "WHERE outcome = 'selected' AND material_dissent = 1 ORDER BY resolution_key"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SECURITY_LISTING_UNRESOLVED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT resolution_key FROM v_security_listing_resolutions_current "
            "WHERE outcome = 'unresolved' ORDER BY resolution_key"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SECURITY_LISTING_MATERIAL_DISSENT",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT resolution_key FROM v_security_listing_resolutions_current "
            "WHERE outcome = 'selected' AND material_dissent = 1 ORDER BY resolution_key"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="LEGACY_ISSUER_BINDING_MATERIAL_DISSENT",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT recorded_issuer_id FROM v_legacy_issuer_bindings_current "
            "WHERE outcome = 'selected' AND material_dissent = 1 "
            "ORDER BY recorded_issuer_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SCOPE_SEC_IDENTITY_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT scope.scope_key, scope.issuer_id "
            "FROM v_issuer_reporting_scope_current AS scope "
            "WHERE scope.inclusion_state IN ('core', 'monitored') "
            "AND scope.require_sec = 1 AND NOT EXISTS ("
            "SELECT 1 FROM v_issuer_identifiers_canonical AS identifier "
            "WHERE identifier.issuer_id = scope.issuer_id "
            "AND identifier.identifier_type = 'sec_cik') "
            "ORDER BY scope.scope_key, scope.issuer_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SCOPE_IR_AUTHORITY_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT scope.scope_key, scope.issuer_id "
            "FROM v_issuer_reporting_scope_current AS scope "
            "WHERE scope.inclusion_state IN ('core', 'monitored') "
            "AND scope.require_ir = 1 AND NOT EXISTS ("
            "SELECT 1 FROM v_issuer_authority_surfaces_current AS surface "
            "WHERE surface.issuer_id = scope.issuer_id "
            "AND surface.status = 'verified' "
            "AND surface.surface_kind IN ('ir_home', 'ir_archive', 'ir_events', "
            "'ir_presentations', 'ir_financials', 'ir_sec_filings')) "
            "ORDER BY scope.scope_key, scope.issuer_id"
        ),
    )


def _audit_reporting_identity(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_REPORTING_IDENTITY_TABLES) & tables
    if not present:
        return
    missing = set(_REPORTING_IDENTITY_TABLES) - tables
    if missing:
        _add(
            findings,
            code="REPORTING_IDENTITY_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(missing),
            query_context="sqlite_master reporting-identity table inventory",
            samples=tuple(sorted(missing)[: options.sample_limit]),
        )
        return
    _audit_append_only_triggers(
        conn,
        findings,
        options,
        _REPORTING_IDENTITY_TABLES,
        "REPORTING_IDENTITY",
    )
    _query_finding(
        conn,
        findings,
        options,
        code="REPORTING_ENTITY_REGISTRY_UNINITIALIZED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT scope.scope_key, scope.issuer_id "
            "FROM v_issuer_reporting_scope_current AS scope "
            "WHERE scope.inclusion_state IN ('core', 'monitored') "
            "AND NOT EXISTS (SELECT 1 FROM reporting_entities AS entity "
            "WHERE entity.issuer_id = scope.issuer_id) "
            "ORDER BY scope.scope_key, scope.issuer_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="REPORTING_ENTITY_IDENTIFIER_UNRESOLVED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT resolution_key "
            "FROM v_reporting_entity_identifier_resolutions_current "
            "WHERE outcome = 'unresolved' ORDER BY resolution_key"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="REPORTING_ENTITY_IDENTIFIER_MATERIAL_DISSENT",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT resolution_key "
            "FROM v_reporting_entity_identifier_resolutions_current "
            "WHERE outcome = 'selected' AND material_dissent = 1 "
            "ORDER BY resolution_key"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SECURITY_IDENTIFIER_UNRESOLVED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT resolution_key FROM v_security_identifier_resolutions_current "
            "WHERE outcome = 'unresolved' ORDER BY resolution_key"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SECURITY_IDENTIFIER_MATERIAL_DISSENT",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT resolution_key FROM v_security_identifier_resolutions_current "
            "WHERE outcome = 'selected' AND material_dissent = 1 "
            "ORDER BY resolution_key"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SCOPE_SOURCE_OBLIGATION_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT scope.scope_key, scope.issuer_id "
            "FROM v_issuer_reporting_scope_current AS scope "
            "WHERE scope.inclusion_state IN ('core', 'monitored') "
            "AND NOT EXISTS (SELECT 1 FROM v_source_obligations_current AS obligation "
            "WHERE obligation.issuer_id = scope.issuer_id "
            "AND obligation.obligation_state = 'required') "
            "ORDER BY scope.scope_key, scope.issuer_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SCOPE_REGULATOR_OBLIGATION_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT scope.scope_key, scope.issuer_id "
            "FROM v_issuer_reporting_scope_current AS scope "
            "WHERE scope.inclusion_state IN ('core', 'monitored') "
            "AND scope.require_sec = 1 AND NOT EXISTS ("
            "SELECT 1 FROM v_source_obligations_current AS obligation "
            "WHERE obligation.issuer_id = scope.issuer_id "
            "AND obligation.obligation_state = 'required' "
            "AND obligation.authority_kind IN ('sec_edgar', 'sedar_plus', 'edinet')) "
            "ORDER BY scope.scope_key, scope.issuer_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SCOPE_IR_OBLIGATION_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT scope.scope_key, scope.issuer_id "
            "FROM v_issuer_reporting_scope_current AS scope "
            "WHERE scope.inclusion_state IN ('core', 'monitored') "
            "AND scope.require_ir = 1 AND NOT EXISTS ("
            "SELECT 1 FROM v_source_obligations_current AS obligation "
            "WHERE obligation.issuer_id = scope.issuer_id "
            "AND obligation.obligation_state = 'required' "
            "AND obligation.authority_kind = 'issuer_publisher' "
            "AND obligation.document_family IN "
            "('issuer_financial_statements', 'issuer_presentations')) "
            "ORDER BY scope.scope_key, scope.issuer_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SCOPE_EARNINGS_OBLIGATION_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT scope.scope_key, scope.issuer_id "
            "FROM v_issuer_reporting_scope_current AS scope "
            "WHERE scope.inclusion_state IN ('core', 'monitored') "
            "AND scope.require_earnings = 1 AND NOT EXISTS ("
            "SELECT 1 FROM v_source_obligations_current AS obligation "
            "WHERE obligation.issuer_id = scope.issuer_id "
            "AND obligation.obligation_state = 'required' "
            "AND obligation.document_family = 'issuer_earnings_materials') "
            "ORDER BY scope.scope_key, scope.issuer_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SOURCE_OBLIGATION_ISSUER_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT obligation.obligation_revision_id "
            "FROM source_obligation_revisions AS obligation "
            "JOIN reporting_entities AS entity "
            "ON entity.reporting_entity_id = obligation.reporting_entity_id "
            "WHERE entity.issuer_id <> obligation.issuer_id "
            "ORDER BY obligation.obligation_revision_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SECURITY_REPORTING_ENTITY_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT security.security_id "
            "FROM securities AS security "
            "WHERE EXISTS (SELECT 1 FROM v_issuer_reporting_scope_current AS scope "
            "WHERE scope.issuer_id = security.issuer_id "
            "AND scope.inclusion_state IN ('core', 'monitored')) "
            "AND NOT EXISTS (SELECT 1 FROM v_security_reporting_entities_current AS relation "
            "WHERE relation.security_id = security.security_id) "
            "ORDER BY security.security_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SECURITY_REPORTING_ENTITY_ISSUER_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT relation.relationship_revision_id "
            "FROM security_reporting_entity_revisions AS relation "
            "JOIN securities AS security ON security.security_id = relation.security_id "
            "JOIN reporting_entities AS entity "
            "ON entity.reporting_entity_id = relation.reporting_entity_id "
            "WHERE security.issuer_id <> entity.issuer_id "
            "ORDER BY relation.relationship_revision_id"
        ),
    )


def _audit_evidence_subject_bindings(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    if "recorded_subject_binding_revisions" not in tables:
        return
    _audit_append_only_triggers(
        conn,
        findings,
        options,
        _SUBJECT_BINDING_TABLES,
        "EVIDENCE_SUBJECT_BINDING",
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_SUBJECT_BINDING_UNRESOLVED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT binding.recorded_issuer_id "
            "FROM v_recorded_subject_bindings_current AS binding "
            "WHERE binding.outcome = 'unresolved' AND EXISTS ("
            "SELECT 1 FROM evidence_document_versions AS document "
            "WHERE document.issuer_id = binding.recorded_issuer_id) "
            "ORDER BY binding.recorded_issuer_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_SUBJECT_BINDING_MATERIAL_DISSENT",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT recorded_issuer_id "
            "FROM v_recorded_subject_bindings_current "
            "WHERE outcome = 'selected' AND material_dissent = 1 "
            "ORDER BY recorded_issuer_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_SUBJECT_BINDING_AMBIGUOUS",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT binding.recorded_issuer_id "
            "FROM v_legacy_issuer_bindings_current AS binding "
            "WHERE binding.outcome = 'selected' "
            "AND EXISTS (SELECT 1 FROM evidence_document_versions AS document "
            "WHERE document.issuer_id = binding.recorded_issuer_id) "
            "AND 1 < (SELECT COUNT(*) FROM reporting_entities AS entity "
            "WHERE entity.issuer_id = binding.canonical_issuer_id) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM v_recorded_subject_bindings_current AS subject "
            "WHERE subject.recorded_issuer_id = binding.recorded_issuer_id "
            "AND subject.outcome = 'selected') "
            "ORDER BY binding.recorded_issuer_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_SUBJECT_REPORTING_ISSUER_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT binding.binding_revision_id "
            "FROM recorded_subject_binding_revisions AS binding "
            "LEFT JOIN reporting_entities AS entity "
            "ON entity.reporting_entity_id = binding.reporting_entity_id "
            "WHERE binding.reporting_entity_id IS NOT NULL "
            "AND (entity.reporting_entity_id IS NULL "
            "OR entity.issuer_id <> binding.issuer_id) "
            "ORDER BY binding.binding_revision_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_SUBJECT_SECURITY_ISSUER_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT binding.binding_revision_id "
            "FROM recorded_subject_binding_revisions AS binding "
            "LEFT JOIN securities AS security "
            "ON security.security_id = binding.security_id "
            "WHERE binding.security_id IS NOT NULL "
            "AND (security.security_id IS NULL "
            "OR security.issuer_id <> binding.issuer_id) "
            "ORDER BY binding.binding_revision_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_SUBJECT_RELATIONSHIP_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT binding.binding_revision_id "
            "FROM recorded_subject_binding_revisions AS binding "
            "WHERE binding.reporting_entity_id IS NOT NULL "
            "AND binding.security_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM v_security_reporting_entities_current AS relation "
            "WHERE relation.security_id = binding.security_id "
            "AND relation.reporting_entity_id = binding.reporting_entity_id) "
            "ORDER BY binding.binding_revision_id"
        ),
    )


def _audit_blob_bytes(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    if "evidence_content_blobs" not in tables:
        return
    columns = _columns(conn, "evidence_content_blobs")
    if not {"sha256", "byte_size", "storage_uri"} <= columns:
        return
    roots = (() if options.repo_root is None else (options.repo_root,)) + options.content_roots
    assert roots
    budget = options.max_verify_bytes
    consumed = 0
    absent: list[str] = []
    mismatched: list[str] = []
    unsafe: list[str] = []
    exhausted: list[str] = []
    rows = conn.execute(
        "SELECT sha256, byte_size, storage_uri FROM evidence_content_blobs ORDER BY sha256"
    ).fetchall()
    for sha256, byte_size, storage_uri in rows:
        size = int(byte_size)
        label = str(sha256)
        if consumed + size > budget:
            exhausted.append(label)
            continue
        path = _local_storage_path(str(storage_uri), roots)
        if path is None:
            unsafe.append(label)
            continue
        if not path.is_file():
            absent.append(label)
            continue
        consumed += size
        actual_size = path.stat().st_size
        actual_sha256 = _sha256_file(path)
        if actual_size != size or actual_sha256 != label:
            mismatched.append(label)
    _add(
        findings,
        code="BLOB_STORAGE_URI_UNSAFE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        count=len(unsafe),
        query_context="file storage URI constrained to explicit content roots",
        samples=tuple(unsafe[: options.sample_limit]),
    )
    _add(
        findings,
        code="BLOB_BYTES_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        count=len(absent),
        query_context="evidence_content_blobs.storage_uri existence",
        samples=tuple(absent[: options.sample_limit]),
    )
    _add(
        findings,
        code="BLOB_BYTES_HASH_OR_SIZE_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        count=len(mismatched),
        query_context="evidence_content_blobs SHA-256 and byte_size",
        samples=tuple(mismatched[: options.sample_limit]),
    )
    _add(
        findings,
        code="BLOB_BYTE_BUDGET_EXHAUSTED",
        severity=Severity.ADVISORY,
        remediation=RemediationClass.BACKFILL,
        count=len(exhausted),
        query_context=f"max_verify_bytes={budget}",
        samples=tuple(exhausted[: options.sample_limit]),
    )


def _local_storage_path(storage_uri: str, content_roots: tuple[Path, ...]) -> Path | None:
    parsed = urlparse(storage_uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    decoded_path = unquote(parsed.path)
    # ``Path.as_uri`` serializes a Windows drive as ``/C:/...``.  Stripping
    # that URI-only leading slash is necessary before pathlib resolves it.
    if len(decoded_path) >= 3 and decoded_path[0] == "/" and decoded_path[2] == ":":
        decoded_path = decoded_path[1:]
    path = Path(decoded_path).resolve()
    for content_root in content_roots:
        try:
            path.relative_to(content_root.resolve())
        except ValueError:
            continue
        return path
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _audit_ocr_governance(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_OCR_TABLES) & tables
    if not present:
        return
    missing = set(_OCR_TABLES) - tables
    if missing:
        _add(
            findings,
            code="OCR_GOVERNANCE_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(missing),
            query_context="sqlite_master OCR-governance table inventory",
            samples=tuple(sorted(missing)[: options.sample_limit]),
        )
        return
    _audit_append_only_triggers(conn, findings, options, _OCR_TABLES, "OCR_GOVERNANCE")
    _query_finding(
        conn,
        findings,
        options,
        code="OCR_ASSESSMENT_INPUT_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT assessment.assessment_id "
            "FROM ocr_document_assessments AS assessment "
            "JOIN evidence_document_versions AS document "
            "ON document.document_version_id = assessment.document_version_id "
            "WHERE document.blob_sha256 <> assessment.input_sha256 "
            "ORDER BY assessment.assessment_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="OCR_PREFLIGHT_PAGE_COUNT_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT assessment.assessment_id "
            "FROM ocr_document_assessments AS assessment "
            "LEFT JOIN ocr_preflight_pages AS page "
            "ON page.assessment_id = assessment.assessment_id "
            "GROUP BY assessment.assessment_id, assessment.page_count "
            "HAVING COUNT(page.page_number) <> assessment.page_count "
            "ORDER BY assessment.assessment_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="OCR_PREFLIGHT_OUTCOME_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT assessment.assessment_id "
            "FROM ocr_document_assessments AS assessment "
            "WHERE (assessment.outcome = 'ocr_required' AND NOT EXISTS "
            "(SELECT 1 FROM ocr_preflight_pages AS page "
            "WHERE page.assessment_id = assessment.assessment_id "
            "AND page.requires_ocr = 1)) "
            "OR (assessment.outcome = 'native_sufficient' AND EXISTS "
            "(SELECT 1 FROM ocr_preflight_pages AS page "
            "WHERE page.assessment_id = assessment.assessment_id "
            "AND page.requires_ocr = 1)) ORDER BY assessment.assessment_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="OCR_RUN_GOVERNANCE_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT run.extraction_run_id FROM evidence_extraction_runs AS run "
            "LEFT JOIN ocr_extraction_governance AS governance "
            "ON governance.extraction_run_id = run.extraction_run_id "
            "WHERE run.extractor_name = 'governed-pdf-ocr' "
            "AND governance.extraction_run_id IS NULL ORDER BY run.extraction_run_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="OCR_GOVERNANCE_RUN_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT governance.extraction_run_id "
            "FROM ocr_extraction_governance AS governance "
            "JOIN evidence_extraction_runs AS run "
            "ON run.extraction_run_id = governance.extraction_run_id "
            "JOIN ocr_document_assessments AS assessment "
            "ON assessment.assessment_id = governance.assessment_id "
            "WHERE assessment.outcome <> 'ocr_required' "
            "OR assessment.document_version_id <> run.document_version_id "
            "OR assessment.input_sha256 <> run.input_sha256 "
            "OR run.extractor_name <> 'governed-pdf-ocr' "
            "OR run.extractor_config_sha256 <> governance.extractor_config_sha256 "
            "ORDER BY governance.extraction_run_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="OCR_PAGE_PREFLIGHT_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT result.extraction_run_id || ':' || result.page_number "
            "FROM ocr_page_results AS result "
            "JOIN ocr_extraction_governance AS governance "
            "ON governance.extraction_run_id = result.extraction_run_id "
            "LEFT JOIN ocr_preflight_pages AS page "
            "ON page.assessment_id = governance.assessment_id "
            "AND page.page_number = result.page_number AND page.requires_ocr = 1 "
            "WHERE page.assessment_id IS NULL "
            "ORDER BY result.extraction_run_id, result.page_number"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="OCR_PAGE_NODE_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT result.extraction_run_id || ':' || result.page_number "
            "FROM ocr_page_results AS result "
            "LEFT JOIN evidence_nodes AS node ON node.node_id = result.node_id "
            "WHERE result.outcome = 'accepted' AND "
            "(node.node_id IS NULL OR node.extraction_run_id <> result.extraction_run_id "
            "OR node.node_kind <> 'pdf_page') "
            "ORDER BY result.extraction_run_id, result.page_number"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="OCR_SUCCEEDED_RUN_WITHOUT_ACCEPTED_PAGE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT run.extraction_run_id FROM evidence_extraction_runs AS run "
            "WHERE run.extractor_name = 'governed-pdf-ocr' AND run.outcome = 'succeeded' "
            "AND NOT EXISTS (SELECT 1 FROM ocr_page_results AS result "
            "WHERE result.extraction_run_id = run.extraction_run_id "
            "AND result.outcome = 'accepted') ORDER BY run.extraction_run_id"
        ),
    )
    _audit_ocr_locator_hashes(conn, findings, options)


def _audit_ocr_locator_hashes(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    rows = conn.execute(
        "SELECT extraction_run_id, page_number, locator_json, locator_sha256 "
        "FROM ocr_page_results ORDER BY extraction_run_id, page_number"
    ).fetchall()
    invalid = [
        f"{run_id}:{page_number}"
        for run_id, page_number, locator_json, locator_sha256 in rows
        if hashlib.sha256(str(locator_json).encode("utf-8")).hexdigest() != str(locator_sha256)
    ]
    _add(
        findings,
        code="OCR_PAGE_LOCATOR_HASH_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        count=len(invalid),
        query_context="ocr_page_results.locator_json SHA-256 equals locator_sha256",
        samples=tuple(invalid[: options.sample_limit]),
    )


def _audit_evidence_replica_links(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_REPLICA_TABLES) & tables
    if not present:
        return
    missing = set(_REPLICA_TABLES) - tables
    if missing:
        _add(
            findings,
            code="EVIDENCE_REPLICA_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(missing),
            query_context="sqlite_master evidence-replica table inventory",
            samples=tuple(sorted(missing)[: options.sample_limit]),
        )
        return
    _audit_append_only_triggers(conn, findings, options, _REPLICA_TABLES, "EVIDENCE_REPLICA")
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_BLOB_NO_PRESENT_LOCATION",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT blob.sha256 FROM evidence_content_blobs AS blob "
            "LEFT JOIN v_evidence_blob_locations_current AS location "
            "ON location.blob_sha256 = blob.sha256 AND location.availability_state = 'present' "
            "WHERE location.location_observation_id IS NULL ORDER BY blob.sha256"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_LOCATION_VERIFICATION_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT location.location_observation_id "
            "FROM evidence_blob_location_observations AS location "
            "JOIN evidence_content_blobs AS blob ON blob.sha256 = location.blob_sha256 "
            "WHERE (location.verified_sha256 IS NOT NULL "
            "AND location.verified_sha256 <> location.blob_sha256) "
            "OR (location.verified_byte_size IS NOT NULL "
            "AND location.verified_byte_size <> blob.byte_size) "
            "ORDER BY location.location_observation_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_LOCATION_REVISION_CHAIN_BROKEN",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT location.location_observation_id "
            "FROM evidence_blob_location_observations AS location "
            "LEFT JOIN evidence_blob_location_observations AS prior "
            "ON prior.location_observation_id = location.supersedes_location_observation_id "
            "AND prior.blob_sha256 = location.blob_sha256 "
            "AND prior.storage_uri = location.storage_uri "
            "AND prior.location_sequence = location.location_sequence - 1 "
            "WHERE (location.location_sequence = 1 "
            "AND location.supersedes_location_observation_id IS NOT NULL) "
            "OR (location.location_sequence > 1 AND prior.location_observation_id IS NULL) "
            "ORDER BY location.location_observation_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_DOCUMENT_PRIMARY_LINK_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT document.document_version_id "
            "FROM evidence_document_versions AS document "
            "LEFT JOIN evidence_document_observation_links AS link "
            "ON link.document_version_id = document.document_version_id "
            "AND link.observation_id = document.observation_id AND link.link_kind = 'primary' "
            "WHERE link.link_id IS NULL ORDER BY document.document_version_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EVIDENCE_DOCUMENT_LINK_BLOB_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT link.link_id FROM evidence_document_observation_links AS link "
            "JOIN evidence_document_versions AS document "
            "ON document.document_version_id = link.document_version_id "
            "JOIN evidence_source_observations AS observation "
            "ON observation.observation_id = link.observation_id "
            "WHERE document.blob_sha256 <> observation.blob_sha256 "
            "OR (link.link_kind = 'primary' AND document.observation_id <> link.observation_id) "
            "ORDER BY link.link_id"
        ),
    )


def _audit_fact_selection(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    if "fact_selection_decisions" not in tables:
        return
    _audit_append_only_triggers(conn, findings, options, _FACT_SELECTION_TABLES, "FACT_SELECTION")
    if "kpi_facts" in tables:
        _query_finding(
            conn,
            findings,
            options,
            code="FACT_SELECTION_TARGET_MISSING",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.MANUAL,
            query=(
                "SELECT decision.decision_id FROM fact_selection_decisions AS decision "
                "LEFT JOIN kpi_facts AS fact ON fact.id = decision.target_row_id "
                "WHERE decision.target_table = 'kpi_facts' AND fact.id IS NULL "
                "ORDER BY decision.decision_id"
            ),
        )
    _query_finding(
        conn,
        findings,
        options,
        code="FACT_SELECTION_REVISION_CHAIN_BROKEN",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT decision.decision_id FROM fact_selection_decisions AS decision "
            "LEFT JOIN fact_selection_decisions AS prior "
            "ON prior.decision_id = decision.supersedes_decision_id "
            "AND prior.target_table = decision.target_table "
            "AND prior.target_row_id = decision.target_row_id "
            "AND prior.revision = decision.revision - 1 "
            "WHERE (decision.revision = 1 AND decision.supersedes_decision_id IS NOT NULL) "
            "OR (decision.revision > 1 AND prior.decision_id IS NULL) "
            "ORDER BY decision.decision_id"
        ),
    )


def _audit_source_coverage(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_SOURCE_COVERAGE_TABLES) & tables
    if not present:
        return
    missing = set(_SOURCE_COVERAGE_TABLES) - tables
    if missing:
        _add(
            findings,
            code="SOURCE_COVERAGE_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(missing),
            query_context="sqlite_master source-coverage table inventory",
            samples=tuple(sorted(missing)[: options.sample_limit]),
        )
        return
    _audit_append_only_triggers(conn, findings, options, _SOURCE_COVERAGE_TABLES, "SOURCE_COVERAGE")
    _query_finding(
        conn,
        findings,
        options,
        code="SOURCE_COVERAGE_UNINITIALIZED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT document.document_version_id "
            "FROM evidence_document_versions AS document "
            "WHERE NOT EXISTS (SELECT 1 FROM source_inventory_snapshots) "
            "ORDER BY document.document_version_id LIMIT 1"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SOURCE_INVENTORY_INCOMPLETE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT snapshot_id, inventory_key, outcome FROM v_source_inventory_current "
            "WHERE outcome <> 'succeeded' ORDER BY inventory_key"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SOURCE_INVENTORY_REVISION_CHAIN_BROKEN",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT snapshot.snapshot_id FROM source_inventory_snapshots AS snapshot "
            "LEFT JOIN source_inventory_snapshots AS prior "
            "ON prior.snapshot_id = snapshot.supersedes_snapshot_id "
            "AND prior.inventory_key = snapshot.inventory_key "
            "AND prior.revision = snapshot.revision - 1 "
            "WHERE (snapshot.revision = 1 AND snapshot.supersedes_snapshot_id IS NOT NULL) "
            "OR (snapshot.revision > 1 AND prior.snapshot_id IS NULL) "
            "ORDER BY snapshot.snapshot_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EXPECTED_DOCUMENT_UNASSESSED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT expected.expected_document_id FROM v_expected_documents_current AS expected "
            "LEFT JOIN v_source_coverage_current AS assessment "
            "ON assessment.expected_document_id = expected.expected_document_id "
            "WHERE assessment.assessment_id IS NULL ORDER BY expected.expected_document_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SOURCE_COVERAGE_NOT_INDEXED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT expected.expected_document_id, assessment.coverage_status "
            "FROM v_expected_documents_current AS expected "
            "JOIN v_source_coverage_current AS assessment "
            "ON assessment.expected_document_id = expected.expected_document_id "
            "WHERE assessment.coverage_status <> 'indexed' "
            "ORDER BY expected.expected_document_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SOURCE_COVERAGE_REVISION_CHAIN_BROKEN",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT assessment.assessment_id "
            "FROM source_coverage_assessments AS assessment "
            "LEFT JOIN source_coverage_assessments AS prior "
            "ON prior.assessment_id = assessment.supersedes_assessment_id "
            "AND prior.expected_document_id = assessment.expected_document_id "
            "AND prior.revision = assessment.revision - 1 "
            "WHERE (assessment.revision = 1 "
            "AND assessment.supersedes_assessment_id IS NOT NULL) "
            "OR (assessment.revision > 1 AND prior.assessment_id IS NULL) "
            "ORDER BY assessment.assessment_id"
        ),
    )


def _audit_source_inventory_seals(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_SOURCE_INVENTORY_SEAL_TABLES) & tables
    if not present:
        return
    missing = set(_SOURCE_INVENTORY_SEAL_TABLES) - tables
    if missing:
        _add(
            findings,
            code="SOURCE_INVENTORY_SEAL_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(missing),
            query_context="sqlite_master source inventory seal tables",
            samples=tuple(sorted(missing)[: options.sample_limit]),
        )
        return
    _audit_append_only_triggers(
        conn,
        findings,
        options,
        _SOURCE_INVENTORY_SEAL_TABLES,
        "SOURCE_INVENTORY_SEAL",
    )
    _query_finding(
        conn,
        findings,
        options,
        code="CURRENT_SOURCE_INVENTORY_UNSEALED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT current.snapshot_id FROM v_source_inventory_current AS current "
            "LEFT JOIN source_inventory_snapshot_seals AS seal "
            "ON seal.snapshot_id = current.snapshot_id "
            "WHERE seal.snapshot_id IS NULL ORDER BY current.snapshot_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SOURCE_INVENTORY_SEAL_COUNT_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT seal.snapshot_id FROM source_inventory_snapshot_seals AS seal "
            "LEFT JOIN source_inventory_components AS component "
            "ON component.snapshot_id = seal.snapshot_id "
            "GROUP BY seal.snapshot_id, seal.expected_component_count "
            "HAVING COUNT(component.component_id) <> seal.expected_component_count "
            "ORDER BY seal.snapshot_id"
        ),
    )
    digest_mismatches: list[str] = []
    digest_mismatch_count = 0
    for seal_row in conn.execute(
        "SELECT snapshot_id, component_digest_sha256 "
        "FROM source_inventory_snapshot_seals ORDER BY snapshot_id"
    ):
        snapshot_id = str(seal_row[0])
        try:
            components = tuple(
                InventoryComponent.model_validate(
                    {
                        "component_id": row[0],
                        "idempotency_key": row[1],
                        "snapshot_id": row[2],
                        "component_key": row[3],
                        "component_kind": row[4],
                        "source_url": row[5],
                        "source_observation_id": row[6],
                        "outcome": row[7],
                        "required": bool(row[8]),
                        "failure_reason": row[9],
                        "ordinal": row[10],
                        "recorded_at": row[11],
                    }
                )
                for row in conn.execute(
                    "SELECT component_id,idempotency_key,snapshot_id,component_key,"
                    "component_kind,source_url,source_observation_id,outcome,required,"
                    "failure_reason,ordinal,recorded_at "
                    "FROM source_inventory_components WHERE snapshot_id = ? "
                    "ORDER BY ordinal,component_key",
                    (snapshot_id,),
                )
            )
            matches = component_digest(components) == str(seal_row[1])
        except ValueError:
            matches = False
        if not matches:
            digest_mismatch_count += 1
            if len(digest_mismatches) < options.sample_limit:
                digest_mismatches.append(snapshot_id)
    if digest_mismatch_count:
        _add(
            findings,
            code="SOURCE_INVENTORY_SEAL_DIGEST_MISMATCH",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.REINGEST,
            count=digest_mismatch_count,
            query_context="recomputed source inventory component digest",
            samples=tuple(digest_mismatches),
        )
    _query_finding(
        conn,
        findings,
        options,
        code="SOURCE_INVENTORY_SEAL_STATUS_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT seal.snapshot_id FROM source_inventory_snapshot_seals AS seal "
            "WHERE (seal.completion_status = 'complete') <> "
            "(NOT EXISTS (SELECT 1 FROM source_inventory_components AS component "
            "WHERE component.snapshot_id = seal.snapshot_id AND component.required = 1 "
            "AND component.outcome <> 'succeeded')) ORDER BY seal.snapshot_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SEARCH_MANIFEST_SOURCE_INVENTORY_UNLINKED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT manifest.manifest_id FROM search_corpus_manifests AS manifest "
            "LEFT JOIN search_manifest_source_inventories AS link "
            "ON link.manifest_id = manifest.manifest_id "
            "WHERE link.manifest_id IS NULL ORDER BY manifest.manifest_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SEARCH_MANIFEST_SOURCE_INVENTORY_STALE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT link.manifest_id || ':' || link.snapshot_id "
            "FROM search_manifest_source_inventories AS link "
            "LEFT JOIN v_source_inventory_sealed_complete AS current "
            "ON current.snapshot_id = link.snapshot_id "
            "WHERE current.snapshot_id IS NULL ORDER BY link.manifest_id, link.snapshot_id"
        ),
    )


def _audit_expectation_lifecycle(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    if "expected_document_lifecycle_revisions" not in tables:
        return
    _audit_append_only_triggers(
        conn,
        findings,
        options,
        _EXPECTATION_LIFECYCLE_TABLES,
        "EXPECTED_DOCUMENT_LIFECYCLE",
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EXPECTED_DOCUMENT_LIFECYCLE_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT expected.expected_document_id FROM expected_documents AS expected "
            "JOIN source_inventory_snapshots AS inventory "
            "ON inventory.snapshot_id = expected.snapshot_id "
            "LEFT JOIN expected_document_lifecycle_revisions AS lifecycle "
            "ON lifecycle.source_inventory_snapshot_id = inventory.snapshot_id "
            "AND lifecycle.expected_document_key = expected.expected_document_key "
            "AND lifecycle.expected_document_id = expected.expected_document_id "
            "AND lifecycle.status = 'expected' "
            "WHERE lifecycle.lifecycle_id IS NULL ORDER BY expected.expected_document_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="EXPECTED_DOCUMENT_LIFECYCLE_CHAIN_BROKEN",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT lifecycle.lifecycle_id "
            "FROM expected_document_lifecycle_revisions AS lifecycle "
            "LEFT JOIN expected_document_lifecycle_revisions AS prior "
            "ON prior.lifecycle_id = lifecycle.supersedes_lifecycle_id "
            "AND prior.inventory_key = lifecycle.inventory_key "
            "AND prior.expected_document_key = lifecycle.expected_document_key "
            "AND prior.revision = lifecycle.revision - 1 "
            "WHERE (lifecycle.revision = 1 "
            "AND lifecycle.supersedes_lifecycle_id IS NOT NULL) "
            "OR (lifecycle.revision > 1 AND prior.lifecycle_id IS NULL) "
            "ORDER BY lifecycle.lifecycle_id"
        ),
    )


def _audit_ask_traces(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_ASK_TRACE_TABLES) & tables
    if not present:
        return
    missing = set(_ASK_TRACE_TABLES) - tables
    if missing:
        _add(
            findings,
            code="ASK_TRACE_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(missing),
            query_context="sqlite_master Ask trace tables",
            samples=tuple(sorted(missing)[: options.sample_limit]),
        )
        return
    _audit_append_only_triggers(conn, findings, options, _ASK_TRACE_TABLES, "ASK_TRACE")
    _query_finding(
        conn,
        findings,
        options,
        code="ASK_READY_TRACE_WITHOUT_MANIFEST",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT trace_id FROM ask_retrieval_traces "
            "WHERE outcome = 'ready' AND manifest_ids_json = '[]' ORDER BY trace_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="ASK_TRACE_ITEM_MANIFEST_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT item.trace_id || ':' || item.rank "
            "FROM ask_retrieval_trace_items AS item "
            "JOIN search_chunks AS chunk ON chunk.chunk_id = item.chunk_id "
            "WHERE chunk.manifest_id <> item.manifest_id ORDER BY item.trace_id, item.rank"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="ASK_TRACE_ITEM_OUTSIDE_MANIFEST_SET",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT item.trace_id || ':' || item.rank "
            "FROM ask_retrieval_trace_items AS item "
            "JOIN ask_retrieval_traces AS trace ON trace.trace_id = item.trace_id "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM json_each("
            "CASE WHEN json_valid(trace.manifest_ids_json) "
            "THEN trace.manifest_ids_json ELSE '[]' END"
            ") AS manifest WHERE manifest.value = item.manifest_id"
            ") ORDER BY item.trace_id, item.rank"
        ),
    )
    bundle_mismatches: list[str] = []
    bundle_mismatch_count = 0
    rows = conn.execute(
        "SELECT item.trace_id,item.rank,item.manifest_id,item.chunk_id,item.score,"
        "item.bundle_sha256,chunk.text,node.node_id,node.node_kind,node.locator_json,"
        "doc.document_version_id,doc.issuer_id,doc.ticker,doc.form_type,"
        "source.source_url,source.filing_at,source.observed_at,source.retrieved_at "
        "FROM ask_retrieval_trace_items AS item "
        "JOIN search_chunks AS chunk ON chunk.chunk_id = item.chunk_id "
        "JOIN evidence_nodes AS node ON node.node_id = chunk.evidence_node_id "
        "JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = node.extraction_run_id "
        "JOIN evidence_document_versions AS doc "
        "ON doc.document_version_id = run.document_version_id "
        "JOIN evidence_source_observations AS source "
        "ON source.observation_id = doc.observation_id "
        "ORDER BY item.trace_id,item.rank"
    )
    for row in rows:
        identity = f"{row[0]}:{row[1]}"
        try:
            item = GroundedAskItem.model_validate(
                {
                    "rank": row[1],
                    "manifest_id": row[2],
                    "chunk_id": row[3],
                    "score": row[4],
                    "text": row[6],
                    "node_id": row[7],
                    "node_kind": row[8],
                    "locator": None if row[9] is None else json.loads(str(row[9])),
                    "document_version_id": row[10],
                    "issuer_id": row[11],
                    "ticker": row[12],
                    "form_type": row[13],
                    "source_url": row[14],
                    "filing_at": row[15],
                    "observed_at": row[16],
                    "retrieved_at": row[17],
                }
            )
            matches = ask_item_bundle_sha256(item) == str(row[5])
        except (ValueError, json.JSONDecodeError):
            matches = False
        if not matches:
            bundle_mismatch_count += 1
            if len(bundle_mismatches) < options.sample_limit:
                bundle_mismatches.append(identity)
    if bundle_mismatch_count:
        _add(
            findings,
            code="ASK_TRACE_ITEM_BUNDLE_DIGEST_MISMATCH",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.REINGEST,
            count=bundle_mismatch_count,
            query_context="recomputed Ask retrieval evidence bundle digest",
            samples=tuple(bundle_mismatches),
        )


def _audit_embedding_promotion(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    if "search_embedding_model_promotions" not in tables:
        return
    _audit_append_only_triggers(
        conn,
        findings,
        options,
        _EMBEDDING_PROMOTION_TABLES,
        "EMBEDDING_PROMOTION",
    )
    count = int(
        conn.execute(
            "SELECT COUNT(*) FROM v_search_embedding_model_promotion_current "
            "WHERE purpose = 'evidence_vector_retrieval'"
        ).fetchone()[0]
    )
    if count == 0:
        _add(
            findings,
            code="EMBEDDING_MODEL_NOT_PROMOTED",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.MANUAL,
            count=1,
            query_context="current evidence-vector embedding promotion",
            samples=("evidence_vector_retrieval",),
        )
        return
    if "search_projection_seals" not in tables:
        _add(
            findings,
            code="SEARCH_PROJECTION_SEAL_SCHEMA_ABSENT",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=1,
            query_context="promoted vector retrieval requires sealed physical projections",
            samples=("search_projection_seals",),
        )
        return
    _query_finding(
        conn,
        findings,
        options,
        code="COMPLETE_CORPUS_VECTOR_INDEX_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT manifest.manifest_id FROM search_corpus_manifests AS manifest "
            "JOIN search_corpus_manifest_seals AS seal "
            "ON seal.manifest_id = manifest.manifest_id "
            "JOIN v_search_embedding_model_promotion_current AS promotion "
            "ON promotion.purpose = 'evidence_vector_retrieval' "
            "WHERE seal.completion_status = 'complete' AND NOT EXISTS "
            "(SELECT 1 FROM v_search_index_successful AS run "
            "JOIN search_projection_seals AS projection "
            "ON projection.index_run_id = run.index_run_id "
            "WHERE run.manifest_id = manifest.manifest_id "
            "AND run.index_kind = 'vector' "
            "AND projection.provider = promotion.provider "
            "AND projection.model = promotion.model "
            "AND projection.dimensions = promotion.dimensions) "
            "ORDER BY manifest.manifest_id"
        ),
    )


def _audit_observation_resolution(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_RESOLUTION_TABLES) & tables
    if not present:
        return
    if set(_RESOLUTION_TABLES) - tables:
        _add(
            findings,
            code="OBSERVATION_RESOLUTION_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(set(_RESOLUTION_TABLES) - tables),
            query_context="sqlite_master observation-resolution table inventory",
            samples=tuple(sorted(set(_RESOLUTION_TABLES) - tables)[: options.sample_limit]),
        )
        return
    _audit_append_only_triggers(
        conn, findings, options, _RESOLUTION_TABLES, "OBSERVATION_RESOLUTION"
    )
    _query_finding(
        conn,
        findings,
        options,
        code="REPORTED_OBSERVATION_EVIDENCE_ANCHOR_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT observation_id FROM reported_observations AS observation LEFT JOIN evidence_nodes AS node "
            "ON node.node_id = observation.evidence_node_id WHERE node.node_id IS NULL ORDER BY observation_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="REPORTED_OBSERVATION_VALUE_OR_CLOCK_INVALID",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT observation_id FROM reported_observations WHERE "
            "(numeric_value IS NULL AND text_value IS NULL) OR (numeric_value IS NOT NULL AND text_value IS NOT NULL) "
            "OR (numeric_value IS NOT NULL AND unit IS NULL) "
            "OR (numeric_value IS NOT NULL AND unit = 'currency' AND currency IS NULL) "
            "OR recorded_at < available_at ORDER BY observation_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="RESOLUTION_SELECTED_MEMBERSHIP_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT revision.resolution_id FROM observation_resolution_revisions AS revision "
            "LEFT JOIN observation_resolution_candidates AS candidate ON candidate.resolution_id = revision.resolution_id "
            "AND candidate.observation_id = revision.selected_observation_id WHERE candidate.observation_id IS NULL ORDER BY revision.resolution_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="RESOLUTION_REVISION_CHAIN_BROKEN",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT revision.resolution_id FROM observation_resolution_revisions AS revision "
            "LEFT JOIN observation_resolution_revisions AS prior ON prior.resolution_id = revision.supersedes_resolution_id "
            "AND prior.logical_key = revision.logical_key AND prior.revision = revision.revision - 1 "
            "WHERE (revision.revision = 1 AND revision.supersedes_resolution_id IS NOT NULL) "
            "OR (revision.revision > 1 AND prior.resolution_id IS NULL) ORDER BY revision.resolution_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="RESOLUTION_CANDIDATE_PARENT_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        query=(
            "SELECT candidate.resolution_id, candidate.observation_id FROM observation_resolution_candidates AS candidate "
            "LEFT JOIN observation_resolution_revisions AS revision ON revision.resolution_id = candidate.resolution_id "
            "WHERE revision.resolution_id IS NULL ORDER BY candidate.resolution_id, candidate.observation_id"
        ),
    )
    expected_finality = "trg_observation_resolution_candidates_finalized"
    has_finality = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?", (expected_finality,)
    ).fetchone()
    if has_finality is None:
        _add(
            findings,
            code="RESOLUTION_CANDIDATE_FINALITY_TRIGGER_MISSING",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=1,
            query_context="sqlite_master trigger inventory",
            samples=(expected_finality,),
        )


def _audit_fact_resolution_cutover(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_FACT_RESOLUTION_CUTOVER_TABLES) & tables
    if not present:
        return
    missing = set(_FACT_RESOLUTION_CUTOVER_TABLES) - tables
    if missing:
        _add(
            findings,
            code="FACT_RESOLUTION_CUTOVER_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(missing),
            query_context="sqlite_master fact-resolution cutover table inventory",
            samples=tuple(sorted(missing)[: options.sample_limit]),
        )
        return
    _audit_append_only_triggers(
        conn,
        findings,
        options,
        _FACT_RESOLUTION_CUTOVER_TABLES,
        "FACT_RESOLUTION_CUTOVER",
    )
    for fact_table in ("financial_facts", "kpi_facts"):
        if fact_table not in tables:
            continue
        _query_finding(
            conn,
            findings,
            options,
            code=f"{fact_table.upper()}_OBSERVATION_MISSING",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.BACKFILL,
            query=(
                f"SELECT '{fact_table}:' || fact.id FROM {fact_table} AS fact "  # nosec B608 -- trusted internal SQL shape; values remain bound
                "WHERE NOT EXISTS (SELECT 1 FROM fact_observation_revisions AS link "
                f"WHERE link.fact_table = '{fact_table}' AND link.fact_row_id = fact.id) "
                "ORDER BY fact.id"
            ),
        )
        required_triggers = tuple(
            f"trg_{fact_table}_observation_{suffix}" for suffix in ("insert", "update", "delete")
        )
        absent = tuple(
            trigger
            for trigger in required_triggers
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (trigger,),
            ).fetchone()
            is None
        )
        _add(
            findings,
            code=f"{fact_table.upper()}_CAPTURE_TRIGGER_MISSING",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(absent),
            query_context="sqlite_master fact observation capture trigger inventory",
            samples=absent[: options.sample_limit],
        )
    binding_match = (
        "AND NOT EXISTS ("
        "SELECT 1 FROM legacy_document_evidence_binding_revisions AS binding "
        "WHERE binding.legacy_document_id = link.source_document_id "
        "AND binding.document_version_id = version.document_version_id "
        "AND binding.evidence_node_id = observation.evidence_node_id)"
        if "legacy_document_evidence_binding_revisions" in tables
        else ""
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FACT_OBSERVATION_EVIDENCE_DOCUMENT_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT link.fact_table || ':' || link.fact_row_id || ':r' || link.fact_revision "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "FROM fact_observation_revisions AS link "
            "JOIN reported_observations AS observation USING (observation_id) "
            "JOIN evidence_nodes AS node ON node.node_id = observation.evidence_node_id "
            "JOIN evidence_extraction_runs AS run "
            "ON run.extraction_run_id = node.extraction_run_id "
            "JOIN evidence_document_versions AS version "
            "ON version.document_version_id = run.document_version_id "
            "WHERE (version.legacy_document_id IS NULL "
            "OR version.legacy_document_id <> link.source_document_id) "
            f"{binding_match} "
            "ORDER BY link.fact_table, link.fact_row_id, link.fact_revision"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FACT_CURRENT_CANDIDATE_SET_INCOMPLETE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=_FACT_CURRENT_CANDIDATE_SET_INCOMPLETE_QUERY,
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FACT_RESOLUTION_UNRESOLVED_MATERIAL",
        severity=Severity.WARNING,
        remediation=RemediationClass.MANUAL,
        query=(
            "SELECT resolution.logical_key FROM v_observation_resolution_current AS resolution "
            "JOIN fact_resolution_outcomes AS outcome USING (resolution_id) "
            "WHERE outcome.resolution_status = 'unresolved_material' "
            "ORDER BY resolution.logical_key"
        ),
    )
    digest_mismatch_count, digest_mismatches = fact_resolution_digest_mismatches(
        conn,
        sample_limit=options.sample_limit,
    )
    _add(
        findings,
        code="FACT_RESOLUTION_CANDIDATE_DIGEST_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        count=digest_mismatch_count,
        query_context="recomputed complete fact resolution candidate-set digest",
        samples=digest_mismatches,
    )


def fact_resolution_digest_mismatches(
    conn: sqlite3.Connection,
    *,
    sample_limit: int,
) -> tuple[int, tuple[str, ...]]:
    """Recompute every candidate-set digest with one ordered streaming query."""

    rows = conn.execute(
        "SELECT outcome.resolution_id, outcome.candidate_set_sha256, "
        "candidate.observation_id "
        "FROM fact_resolution_outcomes AS outcome "
        "LEFT JOIN observation_resolution_candidates AS candidate "
        "ON candidate.resolution_id = outcome.resolution_id "
        "ORDER BY outcome.resolution_id, candidate.observation_id"
    )
    current_id: str | None = None
    current_expected = ""
    current_digest = hashlib.sha256()
    has_candidate = False
    mismatch_count = 0
    samples: list[str] = []

    def finalize() -> None:
        nonlocal mismatch_count
        if current_id is None or current_digest.hexdigest() == current_expected:
            return
        mismatch_count += 1
        if len(samples) < sample_limit:
            samples.append(current_id)

    while batch := rows.fetchmany(8192):
        for row in batch:
            resolution_id = str(row[0])
            if resolution_id != current_id:
                finalize()
                current_id = resolution_id
                current_expected = str(row[1])
                current_digest = hashlib.sha256()
                has_candidate = False
            if row[2] is None:
                continue
            if has_candidate:
                current_digest.update(b"\0")
            current_digest.update(str(row[2]).encode())
            has_candidate = True
    finalize()
    return mismatch_count, tuple(samples)


def _audit_fact_match_proofs(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    present = set(_FACT_MATCH_PROOF_TABLES) & tables
    if not present:
        return
    missing = set(_FACT_MATCH_PROOF_TABLES) - tables
    if missing:
        _add(
            findings,
            code="FACT_MATCH_PROOF_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(missing),
            query_context="sqlite_master fact-match proof table inventory",
            samples=tuple(sorted(missing)[: options.sample_limit]),
        )
        return
    required_views = (
        "v_legacy_fact_evidence_matches_current",
        "v_legacy_fact_evidence_matches_accepted_current",
        "v_fact_observation_match_proofs_current_valid",
    )
    absent_views = tuple(
        view
        for view in required_views
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = ?",
            (view,),
        ).fetchone()
        is None
    )
    _add(
        findings,
        code="FACT_MATCH_PROOF_VIEW_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=len(absent_views),
        query_context="sqlite_master fact-match proof view inventory",
        samples=absent_views[: options.sample_limit],
    )
    if absent_views:
        return
    _audit_append_only_triggers(
        conn,
        findings,
        options,
        _FACT_MATCH_PROOF_TABLES,
        "FACT_MATCH_PROOF",
    )
    required_match_triggers = (
        "trg_legacy_fact_evidence_match_revisions_binding_current",
        "trg_legacy_fact_evidence_match_revisions_knowledge_clock",
        "trg_legacy_fact_evidence_match_revisions_accepted_candidate",
        "trg_legacy_fact_evidence_match_revisions_financial_facts_scope",
        "trg_legacy_fact_evidence_match_revisions_kpi_facts_scope",
        "trg_legacy_fact_evidence_match_revisions_financial_facts_accepted_update",
        "trg_legacy_fact_evidence_match_revisions_financial_facts_accepted_delete",
        "trg_legacy_fact_evidence_match_revisions_kpi_facts_accepted_update",
        "trg_legacy_fact_evidence_match_revisions_kpi_facts_accepted_delete",
        "trg_fact_observation_match_proofs_validate",
    )
    absent_triggers = tuple(
        trigger
        for trigger in required_match_triggers
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger,),
        ).fetchone()
        is None
    )
    _add(
        findings,
        code="FACT_MATCH_PROOF_GUARD_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=len(absent_triggers),
        query_context="sqlite_master fact-match proof guard inventory",
        samples=absent_triggers[: options.sample_limit],
    )
    capture_gates_missing: list[str] = []
    for fact_table in ("financial_facts", "kpi_facts"):
        for suffix in ("insert", "update"):
            trigger = f"trg_{fact_table}_observation_{suffix}"
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (trigger,),
            ).fetchone()
            sql = "" if row is None else str(row[0])
            if " WHEN " not in sql or "sec_companyfacts" not in sql:
                capture_gates_missing.append(trigger)
    _add(
        findings,
        code="COMPANYFACTS_CAPTURE_GATE_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=len(capture_gates_missing),
        query_context="CompanyFacts match-gated fact capture trigger inventory",
        samples=tuple(capture_gates_missing[: options.sample_limit]),
    )
    digest_count, digest_samples, canonical_count, canonical_samples = fact_match_json_mismatches(
        conn, sample_limit=options.sample_limit
    )
    _add(
        findings,
        code="FACT_MATCH_JSON_DIGEST_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        count=digest_count,
        query_context="recomputed fact-match JSON digests",
        samples=digest_samples,
    )
    _add(
        findings,
        code="FACT_MATCH_JSON_NONCANONICAL",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        count=canonical_count,
        query_context="canonical fact-match JSON serialization",
        samples=canonical_samples,
    )
    _query_finding(
        conn,
        findings,
        options,
        code="COMPANYFACTS_CURRENT_OBSERVATION_MATCH_PROOF_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "WITH current_links AS MATERIALIZED ("
            "SELECT link.* FROM fact_observation_revisions AS link "
            "WHERE NOT EXISTS (SELECT 1 FROM fact_observation_revisions AS newer "
            "WHERE newer.fact_table = link.fact_table "
            "AND newer.fact_row_id = link.fact_row_id "
            "AND newer.fact_revision > link.fact_revision)) "
            "SELECT link.fact_table || ':' || link.fact_row_id || ':r' || "
            "link.fact_revision FROM current_links AS link "
            "JOIN v_legacy_document_evidence_bindings_current AS binding "
            "ON binding.legacy_document_id = link.source_document_id "
            "JOIN evidence_document_versions AS document "
            "ON document.document_version_id = binding.document_version_id "
            "JOIN evidence_source_observations AS source "
            "ON source.observation_id = document.observation_id "
            "WHERE source.source_kind = 'sec_companyfacts' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM v_fact_observation_match_proofs_current_valid AS proof "
            "WHERE proof.observation_id = link.observation_id) "
            "ORDER BY link.fact_table, link.fact_row_id, link.fact_revision"
        ),
    )


def fact_match_json_mismatches(
    conn: sqlite3.Connection,
    *,
    sample_limit: int,
) -> tuple[int, tuple[str, ...], int, tuple[str, ...]]:
    """Recompute every JSON-backed 0235 digest and canonical representation."""

    rows = conn.execute(
        "SELECT match_revision_id, fact_payload_json, "
        "fact_payload_fingerprint_sha256, original_locator_json, "
        "original_locator_sha256, relocated_locator_json, "
        "relocated_locator_sha256, candidate_manifest_json, "
        "candidate_manifest_sha256 "
        "FROM legacy_fact_evidence_match_revisions "
        "ORDER BY match_revision_id"
    )
    digest_count = 0
    canonical_count = 0
    digest_samples: list[str] = []
    canonical_samples: list[str] = []
    while batch := rows.fetchmany(8192):
        for row in batch:
            revision_id = str(row[0])
            fields = (
                ("fact_payload", row[1], row[2]),
                ("original_locator", row[3], row[4]),
                ("relocated_locator", row[5], row[6]),
                ("candidate_manifest", row[7], row[8]),
            )
            row_digest_bad = False
            row_canonical_bad = False
            for _, raw_value, expected_value in fields:
                if raw_value is None and expected_value is None:
                    continue
                if raw_value is None or expected_value is None:
                    row_digest_bad = True
                    continue
                raw = str(raw_value)
                if hashlib.sha256(raw.encode()).hexdigest() != str(expected_value):
                    row_digest_bad = True
                try:
                    decoded = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    row_canonical_bad = True
                    continue
                canonical = json.dumps(
                    decoded,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                if canonical != raw:
                    row_canonical_bad = True
            if row_digest_bad:
                digest_count += 1
                if len(digest_samples) < sample_limit:
                    digest_samples.append(revision_id)
            if row_canonical_bad:
                canonical_count += 1
                if len(canonical_samples) < sample_limit:
                    canonical_samples.append(revision_id)
    return (
        digest_count,
        tuple(digest_samples),
        canonical_count,
        tuple(canonical_samples),
    )


def _audit_search_corpus(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    if not (set(_SEARCH_TABLES) & tables):
        return
    if set(_SEARCH_TABLES) - tables:
        _add(
            findings,
            code="SEARCH_CORPUS_SCHEMA_PARTIAL",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(set(_SEARCH_TABLES) - tables),
            query_context="sqlite_master grounded-search table inventory",
            samples=tuple(sorted(set(_SEARCH_TABLES) - tables)[: options.sample_limit]),
        )
        return
    _query_finding(
        conn,
        findings,
        options,
        code="SEARCH_CORPUS_NOT_BUILT",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT document.document_version_id "
            "FROM evidence_document_versions AS document "
            "WHERE NOT EXISTS (SELECT 1 FROM search_corpus_manifests) "
            "ORDER BY document.document_version_id LIMIT 1"
        ),
    )
    _audit_append_only_triggers(conn, findings, options, _SEARCH_TABLES, "SEARCH_CORPUS")
    if "search_projection_seals" in tables:
        _audit_append_only_triggers(
            conn,
            findings,
            options,
            ("search_projection_seals",),
            "SEARCH_PROJECTION",
        )
        _audit_search_projection_seals(conn, findings, options)
    _query_finding(
        conn,
        findings,
        options,
        code="SEARCH_MANIFEST_UNSEALED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        query=(
            "SELECT manifest.manifest_id FROM search_corpus_manifests AS manifest "
            "LEFT JOIN search_corpus_manifest_seals AS seal ON seal.manifest_id = manifest.manifest_id "
            "WHERE seal.manifest_id IS NULL ORDER BY manifest.manifest_id"
        ),
    )
    _audit_search_manifest_seals(conn, findings, options)
    valid_nonsemantic_exemption = (
        "AND NOT EXISTS ("
        "SELECT 1 FROM document_semantic_disposition_revisions AS disposition "
        "WHERE disposition.document_version_id = membership.document_version_id "
        "AND disposition.semantic_status = 'not_required' "
        "AND disposition.decision_kind = 'human' "
        "AND disposition.reviewer_identity IS NOT NULL "
        "AND length(trim(disposition.reviewer_identity)) > 0 "
        "AND membership.reason = 'semantic:not_required:' || disposition.assessment_id "
        "AND NOT EXISTS ("
        "SELECT 1 FROM document_semantic_disposition_revisions AS newer "
        "WHERE newer.document_version_id = disposition.document_version_id "
        "AND newer.revision > disposition.revision"
        ")) "
        if "document_semantic_disposition_revisions" in tables
        else ""
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SEARCH_CORPUS_COVERAGE_GAP",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT membership.manifest_id, membership.document_version_id, membership.membership_status "
            "FROM search_corpus_document_memberships AS membership "
            "JOIN v_search_corpus_current AS current ON current.manifest_id = membership.manifest_id "
            "WHERE membership.membership_status <> 'included' "
            "ORDER BY membership.manifest_id, membership.document_version_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SEARCH_INCLUDED_DOCUMENT_UNCHUNKED",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.BACKFILL,
        query=(
            "SELECT membership.manifest_id, membership.document_version_id FROM search_corpus_document_memberships AS membership "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "JOIN v_search_corpus_current AS current ON current.manifest_id = membership.manifest_id "
            "LEFT JOIN evidence_extraction_runs AS run ON run.document_version_id = membership.document_version_id "
            "LEFT JOIN evidence_nodes AS node ON node.extraction_run_id = run.extraction_run_id "
            "LEFT JOIN search_chunks AS chunk ON chunk.manifest_id = membership.manifest_id AND chunk.evidence_node_id = node.node_id "
            "WHERE membership.membership_status = 'included' "
            f"{valid_nonsemantic_exemption}"
            "GROUP BY membership.manifest_id, membership.document_version_id "
            "HAVING COUNT(chunk.chunk_id) = 0 ORDER BY membership.manifest_id, membership.document_version_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SEARCH_INDEX_MEMBERSHIP_GAP",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT run.index_run_id, chunk.chunk_id, membership.membership_status "
            "FROM search_index_runs AS run "
            "JOIN search_chunks AS chunk ON chunk.manifest_id = run.manifest_id "
            "LEFT JOIN search_index_memberships AS membership "
            "ON membership.index_run_id = run.index_run_id AND membership.chunk_id = chunk.chunk_id "
            "WHERE run.outcome = 'succeeded' AND run.index_kind = 'vector' "
            "AND (membership.chunk_id IS NULL OR membership.membership_status <> 'included') "
            "ORDER BY run.index_run_id, chunk.chunk_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="SEARCH_VECTOR_ARTIFACT_GAP",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT run.index_run_id, membership.chunk_id "
            "FROM search_index_runs AS run "
            "JOIN search_index_memberships AS membership "
            "ON membership.index_run_id = run.index_run_id "
            "LEFT JOIN search_embedding_artifacts AS artifact "
            "ON artifact.index_run_id = run.index_run_id "
            "AND artifact.chunk_id = membership.chunk_id AND artifact.outcome = 'succeeded' "
            "WHERE run.index_kind = 'vector' AND run.outcome = 'succeeded' "
            "AND membership.membership_status = 'included' "
            "AND artifact.embedding_artifact_id IS NULL "
            "ORDER BY run.index_run_id, membership.chunk_id"
        ),
    )


def _audit_search_projection_seals(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    _query_finding(
        conn,
        findings,
        options,
        code="SEARCH_PROJECTION_SEAL_MISSING",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.REINGEST,
        query=(
            "SELECT run.index_run_id FROM search_index_runs AS run "
            "JOIN search_corpus_manifest_seals AS corpus "
            "ON corpus.manifest_id = run.manifest_id "
            "LEFT JOIN search_projection_seals AS projection "
            "ON projection.index_run_id = run.index_run_id "
            "WHERE run.outcome = 'succeeded' "
            "AND corpus.completion_status = 'complete' "
            "AND projection.index_run_id IS NULL ORDER BY run.index_run_id"
        ),
    )
    mismatches: list[str] = []
    mismatch_count = 0
    cursor = conn.execute("SELECT index_run_id FROM search_projection_seals ORDER BY index_run_id")
    while True:
        rows = cursor.fetchmany(256)
        if not rows:
            break
        for row in rows:
            index_run_id = str(row[0])
            try:
                seal = load_projection_seal(conn, index_run_id=index_run_id)
                if seal is None:
                    raise RuntimeError("projection seal disappeared during audit")
                verify_ledger_projection_seal(conn, seal)
            except (RuntimeError, ValueError):
                mismatch_count += 1
                if len(mismatches) < options.sample_limit:
                    mismatches.append(index_run_id)
    if mismatch_count:
        _add(
            findings,
            code="SEARCH_PROJECTION_SEAL_DIGEST_MISMATCH",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.REINGEST,
            count=mismatch_count,
            query_context="recomputed SQL search projection commitments",
            samples=tuple(mismatches),
        )


def _audit_search_manifest_seals(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    mismatched: list[str] = []
    seals = conn.execute(
        "SELECT manifest_id, expected_document_count, membership_digest_sha256, completion_status "
        "FROM search_corpus_manifest_seals ORDER BY manifest_id"
    ).fetchall()
    for manifest_id, expected_count, expected_digest, completion_status in seals:
        memberships = conn.execute(
            "SELECT membership_id, expected_document_key, document_version_id, membership_status, reason "
            "FROM search_corpus_document_memberships WHERE manifest_id = ? "
            "ORDER BY expected_document_key, membership_id",
            (manifest_id,),
        ).fetchall()
        payload = [list(row) for row in memberships]
        actual_digest = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        actual_status = (
            "complete" if all(str(row[3]) == "included" for row in memberships) else "incomplete"
        )
        if (
            int(expected_count) != len(memberships)
            or str(expected_digest) != actual_digest
            or str(completion_status) != actual_status
        ):
            mismatched.append(str(manifest_id))
    _add(
        findings,
        code="SEARCH_MANIFEST_SEAL_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=len(mismatched),
        query_context=(
            "search_corpus_manifest_seals count, canonical membership digest, and completion status"
        ),
        samples=tuple(mismatched[: options.sample_limit]),
    )


def _audit_fact_plane_v2(
    conn: sqlite3.Connection,
    tables: set[str],
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    """Audit the additive v2 fact plane, its hardening, and search projection."""
    if not (set(_FACT_PLANE_V2_TABLES) & tables):
        return
    if not _require_schema_group(
        findings,
        options,
        present=tables,
        required=_FACT_PLANE_V2_TABLES,
        code="FACT_PLANE_V2_SCHEMA_PARTIAL",
        context="evidence-first fact-plane table inventory",
    ):
        return
    _audit_fact_v2_object_inventory(conn, findings, options)
    _audit_fact_v2_base(conn, findings, options)

    projection_present = set(_FACT_SEARCH_V2_TABLES) & tables
    if not projection_present:
        _add(
            findings,
            code="FACT_SEARCH_V2_SCHEMA_ABSENT",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(_FACT_SEARCH_V2_TABLES),
            query_context="structured fact-search table inventory",
            samples=tuple(_FACT_SEARCH_V2_TABLES[: options.sample_limit]),
        )
    elif _require_schema_group(
        findings,
        options,
        present=tables,
        required=_FACT_SEARCH_V2_TABLES,
        code="FACT_SEARCH_V2_SCHEMA_PARTIAL",
        context="structured fact-search table inventory",
    ):
        _audit_fact_search_v2(conn, findings, options)

    hardening_present = set(_FACT_PLANE_V2_HARDENING_TABLES) & tables
    if not hardening_present:
        _add(
            findings,
            code="FACT_PLANE_V2_HARDENING_ABSENT",
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            count=len(_FACT_PLANE_V2_HARDENING_TABLES),
            query_context="0240 fact-plane hardening table inventory",
            samples=tuple(_FACT_PLANE_V2_HARDENING_TABLES[: options.sample_limit]),
        )
        return
    if not _require_schema_group(
        findings,
        options,
        present=tables,
        required=_FACT_PLANE_V2_HARDENING_TABLES,
        code="FACT_PLANE_V2_HARDENING_SCHEMA_PARTIAL",
        context="0240 fact-plane hardening table inventory",
    ):
        return
    _audit_fact_v2_hardening_inventory(conn, findings, options)
    _audit_fact_v2_hardening(conn, findings, options)
    if set(_FACT_SEARCH_V2_TABLES).issubset(tables):
        _audit_fact_search_v2_hardened_inclusion(conn, findings, options)


def _require_schema_group(
    findings: list[IntegrityFinding],
    options: AuditOptions,
    *,
    present: set[str],
    required: tuple[str, ...],
    code: str,
    context: str,
) -> bool:
    missing = tuple(sorted(set(required) - present))
    _add(
        findings,
        code=code,
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=len(missing),
        query_context=context,
        samples=missing[: options.sample_limit],
    )
    return not missing


def _audit_named_objects(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
    *,
    object_type: str,
    expected: tuple[str, ...],
    code: str,
    context: str,
) -> None:
    names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ?",
            (object_type,),
        )
    }
    missing = tuple(sorted(set(expected) - names))
    _add(
        findings,
        code=code,
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=len(missing),
        query_context=context,
        samples=missing[: options.sample_limit],
    )


def _append_only_names(tables: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        name
        for table in tables
        for name in (
            f"trg_{table}_append_only",
            f"trg_{table}_append_only_delete",
        )
    )


def _audit_fact_v2_object_inventory(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    hardening_installed = "fact_cell_identity_seals_v2" in _table_names(conn)
    version_specific_guards = (
        () if hardening_installed else ("trg_fact_observations_v2_reported_anchor",)
    )
    _audit_named_objects(
        conn,
        findings,
        options,
        object_type="view",
        expected=_FACT_PLANE_V2_VIEWS,
        code="FACT_PLANE_V2_VIEW_MISSING",
        context="sqlite_master evidence-first fact-plane view inventory",
    )
    _audit_named_objects(
        conn,
        findings,
        options,
        object_type="index",
        expected=_FACT_PLANE_V2_INDEXES,
        code="FACT_PLANE_V2_INDEX_MISSING",
        context="sqlite_master evidence-first fact-plane index inventory",
    )
    _audit_named_objects(
        conn,
        findings,
        options,
        object_type="trigger",
        expected=(
            *_append_only_names(_FACT_PLANE_V2_TABLES),
            *_FACT_PLANE_V2_GUARD_TRIGGERS,
            *version_specific_guards,
        ),
        code="FACT_PLANE_V2_TRIGGER_MISSING",
        context="sqlite_master evidence-first fact-plane trigger inventory",
    )


def _audit_fact_v2_hardening_inventory(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    _audit_named_objects(
        conn,
        findings,
        options,
        object_type="view",
        expected=_FACT_PLANE_V2_HARDENING_VIEWS,
        code="FACT_PLANE_V2_HARDENING_VIEW_MISSING",
        context="sqlite_master hardened fact-plane view inventory",
    )
    _audit_named_objects(
        conn,
        findings,
        options,
        object_type="trigger",
        expected=(
            *_append_only_names(_FACT_PLANE_V2_HARDENING_TABLES),
            *_FACT_PLANE_V2_HARDENING_GUARD_TRIGGERS,
        ),
        code="FACT_PLANE_V2_HARDENING_TRIGGER_MISSING",
        context="sqlite_master hardened fact-plane trigger inventory",
    )


def _audit_fact_search_v2_inventory(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    _audit_named_objects(
        conn,
        findings,
        options,
        object_type="view",
        expected=_FACT_SEARCH_V2_VIEWS,
        code="FACT_SEARCH_V2_VIEW_MISSING",
        context="sqlite_master structured fact-search view inventory",
    )
    _audit_named_objects(
        conn,
        findings,
        options,
        object_type="index",
        expected=_FACT_SEARCH_V2_INDEXES,
        code="FACT_SEARCH_V2_INDEX_MISSING",
        context="sqlite_master structured fact-search index inventory",
    )
    _audit_named_objects(
        conn,
        findings,
        options,
        object_type="trigger",
        expected=(
            *_append_only_names(_FACT_SEARCH_V2_TABLES),
            *_FACT_SEARCH_V2_GUARD_TRIGGERS,
        ),
        code="FACT_SEARCH_V2_TRIGGER_MISSING",
        context="sqlite_master structured fact-search trigger inventory",
    )


def _audit_fact_v2_base(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    _audit_json_digest_column(
        conn,
        findings,
        options,
        query=(
            "SELECT fact_cell_id,canonical_dimensions_json,"
            "canonical_dimensions_sha256 FROM fact_cells_v2 "
            "ORDER BY fact_cell_id"
        ),
        code="FACT_PLANE_V2_DIMENSION_DIGEST_MISMATCH",
        context="fact_cells_v2 canonical dimension commitments",
    )
    _audit_json_digest_column(
        conn,
        findings,
        options,
        query=(
            "SELECT observation_id,source_locator_json,"
            "source_locator_sha256 FROM fact_observations_v2 "
            "WHERE observation_kind = 'reported' ORDER BY observation_id"
        ),
        code="FACT_PLANE_V2_LOCATOR_DIGEST_MISMATCH",
        context="reported fact observation locator commitments",
    )
    _audit_fact_v2_candidate_sets(conn, findings, options)
    _audit_fact_v2_derivation_inputs(conn, findings, options)
    _audit_fact_v2_relation_cycles(conn, findings, options)

    checks = (
        (
            "FACT_PLANE_V2_RESOLUTION_CHAIN_BROKEN",
            RemediationClass.HARD_STOP,
            "SELECT resolution.resolution_revision_id "
            "FROM fact_resolution_revisions_v2 AS resolution "
            "LEFT JOIN fact_resolution_revisions_v2 AS prior "
            "ON prior.resolution_revision_id = "
            "resolution.supersedes_resolution_revision_id "
            "WHERE (resolution.revision = 1 "
            "AND resolution.supersedes_resolution_revision_id IS NOT NULL) "
            "OR (resolution.revision > 1 AND (prior.resolution_revision_id IS NULL "
            "OR prior.fact_cell_id <> resolution.fact_cell_id "
            "OR prior.revision <> resolution.revision - 1 "
            "OR prior.knowledge_at > resolution.knowledge_at "
            "OR prior.recorded_at > resolution.recorded_at)) "
            "ORDER BY resolution.resolution_revision_id",
        ),
        (
            "FACT_PLANE_V2_RESOLUTION_CANDIDATES_INCOMPLETE",
            RemediationClass.HARD_STOP,
            "SELECT resolution.resolution_revision_id "
            "FROM fact_resolution_revisions_v2 AS resolution "
            "LEFT JOIN fact_resolution_candidates_v2 AS candidate "
            "ON candidate.candidate_set_id = resolution.candidate_set_id "
            "GROUP BY resolution.resolution_revision_id "
            "HAVING COUNT(candidate.candidate_id) <> resolution.candidate_count "
            "OR MIN(candidate.fact_cell_id) <> resolution.fact_cell_id "
            "OR MAX(candidate.fact_cell_id) <> resolution.fact_cell_id "
            "ORDER BY resolution.resolution_revision_id",
        ),
        (
            "FACT_PLANE_V2_RESOLUTION_SELECTION_INVALID",
            RemediationClass.HARD_STOP,
            "SELECT resolution.resolution_revision_id "
            "FROM fact_resolution_revisions_v2 AS resolution "
            "LEFT JOIN fact_resolution_candidates_v2 AS selected "
            "ON selected.candidate_set_id = resolution.candidate_set_id "
            "AND selected.observation_id = resolution.selected_observation_id "
            "AND selected.eligibility = 'eligible' "
            "WHERE (resolution.status = 'resolved' "
            "AND selected.candidate_id IS NULL) "
            "OR (resolution.status <> 'resolved' "
            "AND resolution.selected_observation_id IS NOT NULL) "
            "ORDER BY resolution.resolution_revision_id",
        ),
        (
            "FACT_PLANE_V2_NO_LOOKAHEAD",
            RemediationClass.HARD_STOP,
            "SELECT resolution.resolution_revision_id,candidate.observation_id "
            "FROM fact_resolution_revisions_v2 AS resolution "
            "JOIN fact_resolution_candidates_v2 AS candidate "
            "ON candidate.candidate_set_id = resolution.candidate_set_id "
            "JOIN fact_observations_v2 AS observation "
            "ON observation.observation_id = candidate.observation_id "
            "WHERE observation.knowledge_at > resolution.knowledge_at "
            "OR observation.recorded_at > resolution.recorded_at "
            "OR candidate.recorded_at > resolution.recorded_at "
            "ORDER BY resolution.resolution_revision_id,candidate.observation_id",
        ),
        (
            "FACT_PLANE_V2_RELATION_INCONSISTENT",
            RemediationClass.HARD_STOP,
            "SELECT relation.relation_id "
            "FROM fact_observation_relations_v2 AS relation "
            "LEFT JOIN fact_observations_v2 AS subject "
            "ON subject.observation_id = relation.subject_observation_id "
            "LEFT JOIN fact_observations_v2 AS object "
            "ON object.observation_id = relation.object_observation_id "
            "WHERE subject.observation_id IS NULL OR object.observation_id IS NULL "
            "OR subject.fact_cell_id <> object.fact_cell_id "
            "OR relation.knowledge_at < subject.knowledge_at "
            "OR relation.knowledge_at < object.knowledge_at "
            "OR relation.recorded_at < subject.recorded_at "
            "OR relation.recorded_at < object.recorded_at "
            "ORDER BY relation.relation_id",
        ),
        (
            "FACT_PLANE_V2_STAGED_CANDIDATES_UNSEALED",
            RemediationClass.HARD_STOP,
            "SELECT candidate.candidate_set_id "
            "FROM fact_resolution_candidates_v2 AS candidate "
            "LEFT JOIN fact_resolution_revisions_v2 AS resolution "
            "ON resolution.candidate_set_id = candidate.candidate_set_id "
            "WHERE resolution.resolution_revision_id IS NULL "
            "GROUP BY candidate.candidate_set_id ORDER BY candidate.candidate_set_id",
        ),
        (
            "FACT_PLANE_V2_DERIVATION_EDGES_UNSEALED",
            RemediationClass.HARD_STOP,
            "SELECT edge.output_observation_id "
            "FROM fact_derivation_input_edges_v2 AS edge "
            "LEFT JOIN fact_derivation_seals_v2 AS seal "
            "ON seal.output_observation_id = edge.output_observation_id "
            "WHERE seal.derivation_seal_id IS NULL "
            "GROUP BY edge.output_observation_id ORDER BY edge.output_observation_id",
        ),
    )
    for code, remediation, query in checks:
        _query_finding(
            conn,
            findings,
            options,
            code=code,
            severity=Severity.BLOCKER,
            remediation=remediation,
            query=query,
        )


def _canonical_commitment_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _commitment_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sequence_digest(values: Iterable[object]) -> tuple[int, str]:
    digest = hashlib.sha256()
    digest.update(b"[")
    count = 0
    for value in values:
        if count:
            digest.update(b",")
        digest.update(_canonical_commitment_json(value).encode("utf-8"))
        count += 1
    digest.update(b"]")
    return count, digest.hexdigest()


def _canonical_datetime(value: object) -> str:
    return datetime.fromisoformat(str(value)).isoformat()


def _audit_utc_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _audit_json_digest_column(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
    *,
    query: str,
    code: str,
    context: str,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(query)
    while batch := cursor.fetchmany(512):
        for identity, raw_json, stored_digest in batch:
            mismatch = raw_json is None or stored_digest is None
            if not mismatch:
                try:
                    parsed = json.loads(str(raw_json))
                    canonical = _canonical_commitment_json(parsed)
                    mismatch = canonical != str(raw_json) or _commitment_sha256(
                        str(raw_json)
                    ) != str(stored_digest)
                except (TypeError, ValueError, json.JSONDecodeError):
                    mismatch = True
            if mismatch:
                count += 1
                if len(samples) < options.sample_limit:
                    samples.append(str(identity))
    _add(
        findings,
        code=code,
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context=context,
        samples=tuple(samples),
    )


def _audit_fact_v2_candidate_sets(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT resolution.resolution_revision_id,"
        "resolution.candidate_set_id,resolution.candidate_count,"
        "resolution.candidate_set_digest_sha256,candidate.observation_id,"
        "candidate.candidate_ordinal,candidate.eligibility,"
        "candidate.candidate_payload_sha256 "
        "FROM fact_resolution_revisions_v2 AS resolution "
        "LEFT JOIN fact_resolution_candidates_v2 AS candidate "
        "ON candidate.candidate_set_id = resolution.candidate_set_id "
        "ORDER BY resolution.resolution_revision_id,candidate.candidate_ordinal"
    )
    current_id: str | None = None
    expected_count = 0
    expected_digest = ""
    payload: list[dict[str, object]] = []

    def finish() -> None:
        nonlocal count
        if current_id is None:
            return
        actual = _commitment_sha256(_canonical_commitment_json(payload))
        if len(payload) != expected_count or actual != expected_digest:
            count += 1
            if len(samples) < options.sample_limit:
                samples.append(current_id)

    while batch := cursor.fetchmany(512):
        for row in batch:
            resolution_id = str(row[0])
            if current_id != resolution_id:
                finish()
                current_id = resolution_id
                expected_count = int(row[2])
                expected_digest = str(row[3])
                payload = []
            if row[4] is not None:
                payload.append(
                    {
                        "candidate_ordinal": int(row[5]),
                        "candidate_payload_sha256": str(row[7]),
                        "eligibility": str(row[6]),
                        "observation_id": str(row[4]),
                    }
                )
    finish()
    _add(
        findings,
        code="FACT_PLANE_V2_CANDIDATE_SET_DIGEST_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context="recomputed ordered v2 resolution candidate sets",
        samples=tuple(samples),
    )


def _audit_fact_v2_derivation_inputs(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT seal.derivation_seal_id,seal.output_observation_id,"
        "seal.input_count,seal.canonical_input_digest_sha256,"
        "edge.input_observation_id,edge.input_resolution_revision_id,"
        "edge.input_role,edge.input_ordinal "
        "FROM fact_derivation_seals_v2 AS seal "
        "LEFT JOIN fact_derivation_input_edges_v2 AS edge "
        "ON edge.output_observation_id = seal.output_observation_id "
        "ORDER BY seal.derivation_seal_id,edge.input_ordinal"
    )
    current_id: str | None = None
    output_id = ""
    expected_count = 0
    expected_digest = ""
    payload: list[dict[str, object]] = []

    def finish() -> None:
        nonlocal count
        if current_id is None:
            return
        actual = _commitment_sha256(_canonical_commitment_json(payload))
        if len(payload) != expected_count or actual != expected_digest:
            count += 1
            if len(samples) < options.sample_limit:
                samples.append(current_id)

    while batch := cursor.fetchmany(512):
        for row in batch:
            seal_id = str(row[0])
            if current_id != seal_id:
                finish()
                current_id = seal_id
                output_id = str(row[1])
                expected_count = int(row[2])
                expected_digest = str(row[3])
                payload = []
            if row[4] is not None:
                payload.append(
                    {
                        "input_observation_id": str(row[4]),
                        "input_ordinal": int(row[7]),
                        "input_resolution_revision_id": (None if row[5] is None else str(row[5])),
                        "input_role": str(row[6]),
                        "output_observation_id": output_id,
                    }
                )
    finish()
    _add(
        findings,
        code="FACT_PLANE_V2_DERIVATION_INPUT_DIGEST_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context="recomputed ordered v2 derivation inputs",
        samples=tuple(samples),
    )

    _query_finding(
        conn,
        findings,
        options,
        code="FACT_PLANE_V2_DERIVATION_NO_LOOKAHEAD",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        query=(
            "SELECT seal.derivation_seal_id,edge.input_observation_id "
            "FROM fact_derivation_seals_v2 AS seal "
            "JOIN fact_observations_v2 AS output "
            "ON output.observation_id = seal.output_observation_id "
            "JOIN fact_derivation_input_edges_v2 AS edge "
            "ON edge.output_observation_id = seal.output_observation_id "
            "JOIN fact_observations_v2 AS input "
            "ON input.observation_id = edge.input_observation_id "
            "LEFT JOIN fact_resolution_revisions_v2 AS resolution "
            "ON resolution.resolution_revision_id = "
            "edge.input_resolution_revision_id "
            "WHERE input.knowledge_at > seal.knowledge_at "
            "OR input.recorded_at > seal.recorded_at "
            "OR input.effective_at > output.effective_at "
            "OR edge.recorded_at > seal.recorded_at "
            "OR (edge.input_resolution_revision_id IS NOT NULL AND "
            "(resolution.resolution_revision_id IS NULL "
            "OR resolution.selected_observation_id <> input.observation_id "
            "OR resolution.knowledge_at > seal.knowledge_at "
            "OR resolution.recorded_at > seal.recorded_at)) "
            "ORDER BY seal.derivation_seal_id,edge.input_observation_id"
        ),
    )


def _audit_fact_v2_relation_cycles(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    cursor = conn.execute(
        "SELECT subject.fact_cell_id,relation.subject_observation_id,"
        "relation.object_observation_id "
        "FROM fact_observation_relations_v2 AS relation "
        "JOIN fact_observations_v2 AS subject "
        "ON subject.observation_id = relation.subject_observation_id "
        "WHERE relation.relation_kind <> 'conflicts_with' "
        "ORDER BY subject.fact_cell_id,relation.subject_observation_id"
    )
    current_cell: str | None = None
    graph: dict[str, set[str]] = {}
    cycle_count = 0
    samples: list[str] = []

    def find_cycles() -> None:
        nonlocal cycle_count
        visiting: set[str] = set()
        visited: set[str] = set()
        cycle_nodes: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                cycle_nodes.add(node)
                return
            if node in visited:
                return
            visiting.add(node)
            for target in graph.get(node, ()):
                visit(target)
            visiting.remove(node)
            visited.add(node)

        for node in tuple(graph):
            visit(node)
        cycle_count += len(cycle_nodes)
        for node in sorted(cycle_nodes):
            if len(samples) < options.sample_limit:
                samples.append(f"{current_cell}|{node}")

    while batch := cursor.fetchmany(512):
        for cell_id, subject_id, object_id in batch:
            cell = str(cell_id)
            if current_cell is not None and cell != current_cell:
                find_cycles()
                graph = {}
            current_cell = cell
            graph.setdefault(str(subject_id), set()).add(str(object_id))
    if current_cell is not None:
        find_cycles()
    _add(
        findings,
        code="FACT_PLANE_V2_RELATION_CYCLE",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.MANUAL,
        count=cycle_count,
        query_context="streamed per-cell directed observation relation graph",
        samples=tuple(samples),
    )


def _audit_fact_v2_hardening(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    for query, code, context in (
        (
            "SELECT fact_cell_id,semantic_identity_json,semantic_key_sha256 "
            "FROM fact_cell_identity_seals_v2 ORDER BY fact_cell_id",
            "FACT_PLANE_V2_SEMANTIC_IDENTITY_DIGEST_MISMATCH",
            "hardened fact-cell semantic identity commitments",
        ),
        (
            "SELECT fact_cell_id,dimension_set_json,dimension_set_sha256 "
            "FROM fact_cell_identity_seals_v2 ORDER BY fact_cell_id",
            "FACT_PLANE_V2_DIMENSION_SEAL_DIGEST_MISMATCH",
            "hardened fact-cell dimension-set commitments",
        ),
        (
            "SELECT observation_id,anchor_payload_json,anchor_payload_sha256 "
            "FROM fact_reported_observation_anchors_v2 ORDER BY observation_id",
            "FACT_PLANE_V2_ANCHOR_DIGEST_MISMATCH",
            "reported observation anchor commitments",
        ),
        (
            "SELECT observation_id,canonical_payload_json,"
            "observation_payload_sha256 "
            "FROM fact_observation_payload_commitments_v2 "
            "ORDER BY observation_id",
            "FACT_PLANE_V2_OBSERVATION_PAYLOAD_DIGEST_MISMATCH",
            "canonical fact observation payload commitments",
        ),
        (
            "SELECT derivation_seal_id,canonical_basis_json,"
            "canonical_basis_sha256 "
            "FROM fact_derivation_basis_commitments_v2 "
            "ORDER BY derivation_seal_id",
            "FACT_PLANE_V2_DERIVATION_BASIS_DIGEST_MISMATCH",
            "derived fact as-reported or as-known basis commitments",
        ),
        (
            "SELECT extraction_seal_id,node_set_json,node_set_sha256 "
            "FROM fact_extraction_run_completeness_seals_v2 "
            "ORDER BY extraction_seal_id",
            "FACT_PLANE_V2_EXTRACTION_NODE_SET_DIGEST_MISMATCH",
            "fact extraction node-set commitments",
        ),
        (
            "SELECT extraction_seal_id,observation_set_json,"
            "observation_set_sha256 "
            "FROM fact_extraction_run_completeness_seals_v2 "
            "ORDER BY extraction_seal_id",
            "FACT_PLANE_V2_EXTRACTION_OBSERVATION_SET_DIGEST_MISMATCH",
            "fact extraction observation-set commitments",
        ),
    ):
        _audit_json_digest_column(
            conn,
            findings,
            options,
            query=query,
            code=code,
            context=context,
        )
    _audit_fact_v2_cell_identity_material(conn, findings, options)
    _audit_fact_v2_reported_anchors(conn, findings, options)
    _audit_fact_v2_observation_payloads(conn, findings, options)
    _audit_fact_v2_derivation_bases(conn, findings, options)
    _audit_fact_v2_extraction_seals(conn, findings, options)

    for code, query in (
        (
            "FACT_PLANE_V2_CANDIDATE_PAYLOAD_UNCOMMITTED",
            "SELECT candidate.candidate_id "
            "FROM fact_resolution_candidates_v2 AS candidate "
            "LEFT JOIN fact_observation_payload_commitments_v2 AS payload "
            "ON payload.observation_id = candidate.observation_id "
            "WHERE payload.observation_id IS NULL "
            "OR payload.observation_payload_sha256 "
            "<> candidate.candidate_payload_sha256 "
            "ORDER BY candidate.candidate_id",
        ),
        (
            "FACT_PLANE_V2_CELL_IDENTITY_UNSEALED",
            "SELECT cell.fact_cell_id FROM fact_cells_v2 AS cell "
            "LEFT JOIN fact_cell_identity_seals_v2 AS seal "
            "ON seal.fact_cell_id = cell.fact_cell_id "
            "WHERE seal.fact_cell_id IS NULL ORDER BY cell.fact_cell_id",
        ),
        (
            "FACT_PLANE_V2_OBSERVATION_UNCOMMITTED",
            "SELECT observation.observation_id "
            "FROM fact_observations_v2 AS observation "
            "LEFT JOIN fact_observation_payload_commitments_v2 AS payload "
            "ON payload.observation_id = observation.observation_id "
            "WHERE payload.observation_id IS NULL "
            "ORDER BY observation.observation_id",
        ),
        (
            "FACT_PLANE_V2_REPORTED_ANCHOR_MISSING",
            "SELECT observation.observation_id "
            "FROM fact_observations_v2 AS observation "
            "LEFT JOIN fact_reported_observation_anchors_v2 AS anchor "
            "ON anchor.observation_id = observation.observation_id "
            "WHERE observation.observation_kind = 'reported' "
            "AND anchor.observation_id IS NULL ORDER BY observation.observation_id",
        ),
        (
            "FACT_PLANE_V2_DERIVATION_BASIS_MISSING",
            "SELECT seal.derivation_seal_id "
            "FROM fact_derivation_seals_v2 AS seal "
            "LEFT JOIN fact_derivation_basis_commitments_v2 AS basis "
            "ON basis.derivation_seal_id = seal.derivation_seal_id "
            "WHERE basis.derivation_seal_id IS NULL "
            "ORDER BY seal.derivation_seal_id",
        ),
        (
            "FACT_PLANE_V2_REPORTED_EXTRACTION_UNSEALED",
            "SELECT anchor.observation_id "
            "FROM fact_reported_observation_anchors_v2 AS anchor "
            "LEFT JOIN fact_extraction_run_completeness_seals_v2 AS seal "
            "ON seal.extraction_run_id = anchor.extraction_run_id "
            "WHERE seal.extraction_seal_id IS NULL ORDER BY anchor.observation_id",
        ),
    ):
        _query_finding(
            conn,
            findings,
            options,
            code=code,
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            query=query,
        )
    _audit_typed_dimension_digests(conn, findings, options)


def _audit_typed_dimension_digests(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT dimension_id,typed_member_value_json,"
        "typed_member_value_sha256 FROM fact_dimensions_normalized_v2 "
        "WHERE member_kind = 'typed' ORDER BY dimension_id"
    )
    while batch := cursor.fetchmany(512):
        for dimension_id, raw_json, expected in batch:
            if (
                raw_json is None
                or expected is None
                or _commitment_sha256(str(raw_json)) != str(expected)
            ):
                count += 1
                if len(samples) < options.sample_limit:
                    samples.append(str(dimension_id))
    _add(
        findings,
        code="FACT_PLANE_V2_TYPED_DIMENSION_DIGEST_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context="normalized typed-member exact JSON bytes",
        samples=tuple(samples),
    )


def _audit_fact_v2_cell_identity_material(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT cell.fact_cell_id,cell.reporting_entity_id,"
        "cell.scope_security_id,cell.concept_namespace,cell.concept_name,"
        "cell.taxonomy_name,cell.accounting_basis,cell.consolidation_scope,"
        "cell.period_kind,cell.period_start,cell.period_end,cell.unit_key,"
        "cell.currency,cell.semantic_key_sha256,"
        "cell.canonical_dimensions_sha256,seal.semantic_key_version,"
        "seal.semantic_identity_json,seal.dimension_count,"
        "seal.dimension_set_json,seal.dimension_set_sha256 "
        "FROM fact_cells_v2 AS cell "
        "JOIN fact_cell_identity_seals_v2 AS seal "
        "ON seal.fact_cell_id = cell.fact_cell_id ORDER BY cell.fact_cell_id"
    )
    while batch := cursor.fetchmany(512):
        for row in batch:
            cell_id = str(row[0])
            mismatch = False
            try:
                dimensions = json.loads(str(row[18]))
                semantic = {
                    "accounting_basis": row[6],
                    "consolidation_scope": row[7],
                    "currency": row[12],
                    "dimensions": dimensions,
                    "period_end": _canonical_datetime(row[10]),
                    "period_kind": row[8],
                    "period_start": (None if row[9] is None else _canonical_datetime(row[9])),
                    "concept_name": row[4],
                    "concept_namespace": row[3],
                    "reporting_entity_id": row[1],
                    "scope_security_id": row[2],
                    "semantic_key_version": "fact_cell_semantic_key.v3",
                    "taxonomy_name": row[5],
                    "unit_key": row[11],
                }
                semantic_json = _canonical_commitment_json(semantic)
                dimension_rows = conn.execute(
                    "SELECT dimension_ordinal,axis_namespace,axis_name,"
                    "member_kind,explicit_member_namespace,"
                    "explicit_member_name,typed_member_value_json "
                    "FROM fact_dimensions_normalized_v2 "
                    "WHERE fact_cell_id = ? ORDER BY dimension_ordinal",
                    (cell_id,),
                ).fetchall()
                normalized = [
                    {
                        "axis_name": str(item[2]),
                        "axis_namespace": str(item[1]),
                        "explicit_member_name": (None if item[5] is None else str(item[5])),
                        "explicit_member_namespace": (None if item[4] is None else str(item[4])),
                        "member_kind": str(item[3]),
                        "typed_member_value": (
                            None if item[6] is None else json.loads(str(item[6]))
                        ),
                    }
                    for item in dimension_rows
                ]
                mismatch = (
                    tuple(int(item[0]) for item in dimension_rows)
                    != tuple(range(len(dimension_rows)))
                    or len(dimension_rows) != int(row[17])
                    or _canonical_commitment_json(normalized) != str(row[18])
                    or semantic_json != str(row[16])
                    or str(row[15]) != "fact_cell_semantic_key.v3"
                    or _commitment_sha256(semantic_json) != str(row[13])
                    or _commitment_sha256(str(row[18])) != str(row[14])
                    or str(row[19]) != str(row[14])
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                mismatch = True
            if mismatch:
                count += 1
                if len(samples) < options.sample_limit:
                    samples.append(cell_id)
    _add(
        findings,
        code="FACT_PLANE_V2_CELL_IDENTITY_MATERIAL_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context="reconstructed semantic v3 identity and normalized dimensions",
        samples=tuple(samples),
    )


def _audit_fact_v2_reported_anchors(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT anchor.observation_id,anchor.subject_binding_revision_id,"
        "anchor.extraction_run_id,anchor.source_taxonomy_version,"
        "anchor.extractor_name,anchor.extractor_code_version,"
        "anchor.extractor_config_sha256,anchor.extraction_input_sha256,"
        "anchor.extraction_output_sha256,anchor.raw_entry_sha256,"
        "anchor.anchor_payload_json,anchor.anchor_payload_sha256,"
        "observation.document_version_id,observation.evidence_node_id,"
        "observation.source_locator_sha256,observation.source_entry_sha256,"
        "observation.fact_cell_id,cell.reporting_entity_id,"
        "cell.scope_security_id,document.issuer_id,node.extraction_run_id,"
        "run.document_version_id,run.extractor_name,"
        "run.extractor_code_version,run.extractor_config_sha256,"
        "run.input_sha256,run.output_sha256,run.outcome,"
        "binding.recorded_issuer_id,binding.issuer_id,"
        "binding.reporting_entity_id,binding.security_id,binding.outcome "
        "FROM fact_reported_observation_anchors_v2 AS anchor "
        "LEFT JOIN fact_observations_v2 AS observation "
        "ON observation.observation_id = anchor.observation_id "
        "LEFT JOIN fact_cells_v2 AS cell "
        "ON cell.fact_cell_id = observation.fact_cell_id "
        "LEFT JOIN evidence_document_versions AS document "
        "ON document.document_version_id = observation.document_version_id "
        "LEFT JOIN evidence_nodes AS node "
        "ON node.node_id = observation.evidence_node_id "
        "LEFT JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = anchor.extraction_run_id "
        "LEFT JOIN recorded_subject_binding_revisions AS binding "
        "ON binding.binding_revision_id = anchor.subject_binding_revision_id "
        "ORDER BY anchor.observation_id"
    )
    while batch := cursor.fetchmany(512):
        for row in batch:
            observation_id = str(row[0])
            payload = {
                "document_version_id": row[12],
                "evidence_node_id": row[13],
                "extraction_input_sha256": row[7],
                "extraction_output_sha256": row[8],
                "extraction_run_id": row[2],
                "extractor_code_version": row[5],
                "extractor_config_sha256": row[6],
                "extractor_name": row[4],
                "raw_entry_sha256": row[9],
                "source_locator_sha256": row[14],
                "source_taxonomy_version": row[3],
                "subject_binding_revision_id": row[1],
            }
            payload_json = _canonical_commitment_json(payload)
            mismatch = (
                payload_json != str(row[10])
                or _commitment_sha256(payload_json) != str(row[11])
                or row[15] != row[9]
                or row[20] != row[2]
                or row[21] != row[12]
                or row[22] != row[4]
                or row[23] != row[5]
                or row[24] != row[6]
                or row[25] != row[7]
                or row[26] != row[8]
                or row[27] != "succeeded"
                or row[28] != row[19]
                or row[29] != row[19]
                or row[30] != row[17]
                or row[32] != "selected"
                or (row[18] is not None and row[31] != row[18])
            )
            if mismatch:
                count += 1
                if len(samples) < options.sample_limit:
                    samples.append(observation_id)
    _add(
        findings,
        code="FACT_PLANE_V2_REPORTED_ANCHOR_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context=(
            "reconstructed exact subject-binding, evidence-node, extraction-run, "
            "and reported-entry anchor"
        ),
        samples=tuple(samples),
    )


def _audit_fact_v2_observation_payloads(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT payload.observation_id,payload.payload_version,"
        "payload.canonical_payload_json,payload.observation_payload_sha256,"
        "payload.committed_at,observation.fact_cell_id,"
        "observation.observation_kind,observation.value_kind,"
        "observation.numeric_value,observation.text_value,observation.is_nil,"
        "observation.raw_lexical_value,observation.method_name,"
        "observation.method_version,observation.method_config_sha256,"
        "observation.revision_kind,observation.supersedes_observation_id,"
        "observation.effective_at,observation.knowledge_at,"
        "observation.recorded_at,observation.document_version_id,"
        "observation.evidence_node_id,observation.source_context_id,"
        "observation.source_unit_id,observation.source_entry_sha256,"
        "observation.source_locator_sha256,observation.decimals,"
        "observation.precision,observation.formula_id,"
        "observation.formula_version,cell.semantic_key_sha256,"
        "anchor.anchor_payload_sha256 "
        "FROM fact_observation_payload_commitments_v2 AS payload "
        "LEFT JOIN fact_observations_v2 AS observation "
        "ON observation.observation_id = payload.observation_id "
        "LEFT JOIN fact_cell_identity_seals_v2 AS cell "
        "ON cell.fact_cell_id = observation.fact_cell_id "
        "LEFT JOIN fact_reported_observation_anchors_v2 AS anchor "
        "ON anchor.observation_id = observation.observation_id "
        "ORDER BY payload.observation_id"
    )
    while batch := cursor.fetchmany(512):
        for row in batch:
            observation_id = str(row[0])
            try:
                reported = row[6] == "reported"
                provenance = (
                    {
                        "anchor_payload_sha256": row[31],
                        "document_version_id": row[20],
                        "evidence_node_id": row[21],
                        "source_context_id": row[22],
                        "source_entry_sha256": row[24],
                        "source_locator_sha256": row[25],
                        "source_unit_id": row[23],
                    }
                    if reported
                    else {
                        "formula_id": row[28],
                        "formula_version": row[29],
                    }
                )
                canonical = _canonical_commitment_json(
                    {
                        "decimals": row[26] if reported else None,
                        "effective_at": _canonical_datetime(row[17]),
                        "fact_cell_semantic_key_sha256": row[30],
                        "is_nil": bool(row[10]),
                        "knowledge_at": _canonical_datetime(row[18]),
                        "method_config_sha256": row[14],
                        "method_name": row[12],
                        "method_version": row[13],
                        "numeric_value": row[8],
                        "observation_kind": row[6],
                        "payload_version": "fact_observation_payload.v1",
                        "precision": row[27] if reported else None,
                        "provenance": provenance,
                        "raw_lexical_value": row[11],
                        "recorded_at": _canonical_datetime(row[19]),
                        "revision_kind": row[15],
                        "supersedes_observation_id": row[16],
                        "text_value": row[9],
                        "value_kind": row[7],
                    }
                )
                mismatch = (
                    row[5] is None
                    or row[30] is None
                    or (reported and row[31] is None)
                    or (not reported and row[31] is not None)
                    or row[1] != "fact_observation_payload.v1"
                    or canonical != str(row[2])
                    or _commitment_sha256(canonical) != str(row[3])
                    or _canonical_datetime(row[4]) != _canonical_datetime(row[19])
                )
            except (TypeError, ValueError):
                mismatch = True
            if mismatch:
                count += 1
                if len(samples) < options.sample_limit:
                    samples.append(observation_id)
    _add(
        findings,
        code="FACT_PLANE_V2_OBSERVATION_PAYLOAD_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context="reconstructed fact_observation_payload.v1 commitments",
        samples=tuple(samples),
    )


def _audit_fact_v2_derivation_bases(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT basis.derivation_seal_id,basis.input_basis,basis.formula_id,"
        "basis.formula_version,basis.formula_definition_sha256,"
        "basis.execution_config_sha256,basis.knowledge_cutoff,"
        "basis.canonical_basis_json,basis.canonical_basis_sha256,"
        "basis.recorded_at,seal.canonical_input_digest_sha256,"
        "seal.formula_config_sha256,seal.knowledge_at,seal.recorded_at,"
        "observation.formula_id,observation.formula_version "
        "FROM fact_derivation_basis_commitments_v2 AS basis "
        "LEFT JOIN fact_derivation_seals_v2 AS seal "
        "ON seal.derivation_seal_id = basis.derivation_seal_id "
        "LEFT JOIN fact_observations_v2 AS observation "
        "ON observation.observation_id = seal.output_observation_id "
        "ORDER BY basis.derivation_seal_id"
    )
    while batch := cursor.fetchmany(512):
        for row in batch:
            seal_id = str(row[0])
            try:
                canonical = _canonical_commitment_json(
                    {
                        "canonical_input_digest_sha256": row[10],
                        "execution_config_sha256": row[5],
                        "formula_definition_sha256": row[4],
                        "formula_id": row[2],
                        "formula_version": row[3],
                        "input_basis": row[1],
                        "knowledge_cutoff": _canonical_datetime(row[6]),
                    }
                )
                mismatch = (
                    row[10] is None
                    or row[2] != row[14]
                    or row[3] != row[15]
                    or row[5] != row[11]
                    or _canonical_datetime(row[6]) != _canonical_datetime(row[12])
                    or _audit_utc_datetime(row[9]) < _audit_utc_datetime(row[13])
                    or canonical != str(row[7])
                    or _commitment_sha256(canonical) != str(row[8])
                )
            except (TypeError, ValueError):
                mismatch = True
            if mismatch:
                count += 1
                if len(samples) < options.sample_limit:
                    samples.append(seal_id)
    _add(
        findings,
        code="FACT_PLANE_V2_DERIVATION_BASIS_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context="reconstructed derivation basis and no-look-ahead clocks",
        samples=tuple(samples),
    )


def _audit_fact_v2_extraction_seals(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT seal.extraction_seal_id,seal.extraction_run_id,"
        "seal.expected_node_count,seal.observed_node_count,"
        "seal.reported_fact_count,seal.node_set_json,seal.node_set_sha256,"
        "seal.observation_set_json,seal.observation_set_sha256,"
        "seal.extractor_config_sha256,seal.extraction_output_sha256,"
        "seal.knowledge_at,seal.recorded_at,run.extractor_config_sha256,"
        "run.output_sha256,run.outcome "
        "FROM fact_extraction_run_completeness_seals_v2 AS seal "
        "LEFT JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = seal.extraction_run_id "
        "ORDER BY seal.extraction_seal_id"
    )
    while batch := cursor.fetchmany(128):
        for row in batch:
            seal_id = str(row[0])
            run_id = str(row[1])
            node_count, node_digest = _canonical_sequence_digest(
                str(item[0])
                for item in conn.execute(
                    "SELECT node_id FROM evidence_nodes "
                    "WHERE extraction_run_id = ? ORDER BY node_id",
                    (run_id,),
                )
            )
            observation_count, observation_digest = _canonical_sequence_digest(
                str(item[0])
                for item in conn.execute(
                    "SELECT observation_id "
                    "FROM fact_reported_observation_anchors_v2 "
                    "WHERE extraction_run_id = ? ORDER BY observation_id",
                    (run_id,),
                )
            )
            mismatch = (
                row[15] != "succeeded"
                or row[9] != row[13]
                or row[10] != row[14]
                or int(row[2]) != node_count
                or int(row[3]) != node_count
                or int(row[4]) != observation_count
                or node_digest != str(row[6])
                or observation_digest != str(row[8])
                or _audit_utc_datetime(row[12]) < _audit_utc_datetime(row[11])
            )
            if mismatch:
                count += 1
                if len(samples) < options.sample_limit:
                    samples.append(seal_id)
    _add(
        findings,
        code="FACT_PLANE_V2_EXTRACTION_SEAL_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context=(
            "recomputed extraction node and reported-observation sets, "
            "counts, software commitments, and clocks"
        ),
        samples=tuple(samples),
    )


def _audit_fact_search_v2(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    _audit_fact_search_v2_inventory(conn, findings, options)
    _audit_fact_search_v2_row_bundles(conn, findings, options)
    _audit_fact_search_v2_membership_bundles(conn, findings, options)
    _audit_fact_search_v2_seals(conn, findings, options)

    for code, query in (
        (
            "FACT_SEARCH_V2_RUN_CORPUS_MISMATCH",
            "SELECT run.projection_run_id "
            "FROM search_fact_projection_runs AS run "
            "LEFT JOIN search_corpus_manifests AS manifest "
            "ON manifest.manifest_id = run.manifest_id "
            "LEFT JOIN search_corpus_manifest_seals AS seal "
            "ON seal.manifest_id = run.manifest_id "
            "WHERE manifest.manifest_id IS NULL "
            "OR seal.completion_status <> 'complete' "
            "OR manifest.knowledge_cutoff IS NULL "
            "OR manifest.knowledge_cutoff <> run.knowledge_cutoff "
            "ORDER BY run.projection_run_id",
        ),
        (
            "FACT_SEARCH_V2_MEMBERSHIP_COVERAGE_MISMATCH",
            "SELECT run.projection_run_id,cell.fact_cell_id "
            "FROM search_fact_projection_runs AS run "
            "JOIN fact_cells_v2 AS cell "
            "ON cell.knowledge_at <= run.knowledge_cutoff "
            "AND cell.recorded_at <= run.recorded_at "
            "LEFT JOIN search_fact_projection_memberships AS membership "
            "ON membership.projection_run_id = run.projection_run_id "
            "AND membership.fact_cell_id = cell.fact_cell_id "
            "WHERE membership.membership_id IS NULL "
            "ORDER BY run.projection_run_id,cell.fact_cell_id",
        ),
        (
            "FACT_SEARCH_V2_ROW_DISPOSITION_MISMATCH",
            "SELECT membership.membership_id "
            "FROM search_fact_projection_memberships AS membership "
            "LEFT JOIN search_fact_projection_rows AS row "
            "ON row.projection_run_id = membership.projection_run_id "
            "AND row.fact_cell_id = membership.fact_cell_id "
            "WHERE (membership.disposition = 'included' "
            "AND row.fact_hit_id IS NULL) "
            "OR (membership.disposition <> 'included' "
            "AND row.fact_hit_id IS NOT NULL) "
            "ORDER BY membership.membership_id",
        ),
        (
            "FACT_SEARCH_V2_ROW_SOURCE_MISMATCH",
            "SELECT row.fact_hit_id "
            "FROM search_fact_projection_rows AS row "
            "LEFT JOIN fact_cells_v2 AS cell "
            "ON cell.fact_cell_id = row.fact_cell_id "
            "LEFT JOIN fact_resolution_revisions_v2 AS resolution "
            "ON resolution.resolution_revision_id = row.resolution_revision_id "
            "LEFT JOIN fact_observations_v2 AS observation "
            "ON observation.observation_id = row.observation_id "
            "WHERE cell.fact_cell_id IS NULL "
            "OR resolution.resolution_revision_id IS NULL "
            "OR observation.observation_id IS NULL "
            "OR resolution.fact_cell_id <> row.fact_cell_id "
            "OR resolution.status <> 'resolved' "
            "OR resolution.selected_observation_id <> row.observation_id "
            "OR observation.fact_cell_id <> row.fact_cell_id "
            "OR row.reporting_entity_id <> cell.reporting_entity_id "
            "OR row.scope_security_id IS NOT cell.scope_security_id "
            "OR row.concept_namespace <> cell.concept_namespace "
            "OR row.concept_name <> cell.concept_name "
            "OR row.period_start IS NOT cell.period_start "
            "OR row.period_end <> cell.period_end "
            "OR row.canonical_dimensions_sha256 "
            "<> cell.canonical_dimensions_sha256 "
            "OR row.unit_key <> cell.unit_key OR row.currency IS NOT cell.currency "
            "OR row.value_kind <> observation.value_kind "
            "OR row.numeric_value IS NOT observation.numeric_value "
            "OR row.text_value IS NOT observation.text_value "
            "OR row.is_nil <> observation.is_nil "
            "OR row.candidate_set_id <> resolution.candidate_set_id "
            "OR row.candidate_count <> resolution.candidate_count "
            "OR row.candidate_set_digest_sha256 "
            "<> resolution.candidate_set_digest_sha256 "
            "OR row.cell_knowledge_at <> cell.knowledge_at "
            "OR row.observation_knowledge_at <> observation.knowledge_at "
            "OR row.resolution_knowledge_at <> resolution.knowledge_at "
            "ORDER BY row.fact_hit_id",
        ),
        (
            "FACT_SEARCH_V2_NO_LOOKAHEAD",
            "SELECT row.fact_hit_id "
            "FROM search_fact_projection_rows AS row "
            "JOIN search_fact_projection_runs AS run "
            "ON run.projection_run_id = row.projection_run_id "
            "JOIN fact_observations_v2 AS observation "
            "ON observation.observation_id = row.observation_id "
            "JOIN fact_resolution_revisions_v2 AS resolution "
            "ON resolution.resolution_revision_id = row.resolution_revision_id "
            "WHERE observation.knowledge_at > run.knowledge_cutoff "
            "OR observation.recorded_at > run.knowledge_cutoff "
            "OR resolution.knowledge_at > run.knowledge_cutoff "
            "OR resolution.recorded_at > run.knowledge_cutoff "
            "OR row.recorded_at > run.recorded_at ORDER BY row.fact_hit_id",
        ),
        (
            "FACT_SEARCH_V2_UNSEALED_RUN",
            "SELECT run.projection_run_id "
            "FROM search_fact_projection_runs AS run "
            "LEFT JOIN search_fact_projection_seals AS seal "
            "ON seal.projection_run_id = run.projection_run_id "
            "WHERE seal.projection_run_id IS NULL ORDER BY run.projection_run_id",
        ),
    ):
        _query_finding(
            conn,
            findings,
            options,
            code=code,
            severity=Severity.BLOCKER,
            remediation=RemediationClass.HARD_STOP,
            query=query,
        )


def _audit_fact_search_v2_row_bundles(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT fact_hit_id,row_bundle_json,row_bundle_sha256 "
        "FROM search_fact_projection_rows ORDER BY fact_hit_id"
    )
    while batch := cursor.fetchmany(512):
        for fact_hit_id, raw_json, expected in batch:
            mismatch = False
            try:
                parsed = _JSON_OBJECT_ADAPTER.validate_json(str(raw_json))
                embedded = parsed.pop("row_sha256", None)
                canonical = _canonical_commitment_json(parsed)
                mismatch = embedded != expected or _commitment_sha256(canonical) != str(expected)
            except (TypeError, ValueError, json.JSONDecodeError):
                mismatch = True
            if mismatch:
                count += 1
                if len(samples) < options.sample_limit:
                    samples.append(str(fact_hit_id))
    _add(
        findings,
        code="FACT_SEARCH_V2_ROW_BUNDLE_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context="recomputed structured FactHit bundle excluding self hash",
        samples=tuple(samples),
    )


def _audit_fact_search_v2_membership_bundles(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT membership_id,projection_run_id,fact_cell_id,disposition,"
        "resolution_revision_id,reason_code,reason_details_json,"
        "membership_bundle_sha256,recorded_at "
        "FROM search_fact_projection_memberships ORDER BY membership_id"
    )
    while batch := cursor.fetchmany(512):
        for row in batch:
            membership_id = str(row[0])
            try:
                canonical = _canonical_commitment_json(
                    {
                        "disposition": row[3],
                        "fact_cell_id": row[2],
                        "membership_id": row[0],
                        "projection_run_id": row[1],
                        "reason_code": row[5],
                        "reason_details": json.loads(str(row[6])),
                        "recorded_at": _canonical_datetime(row[8]),
                        "resolution_revision_id": row[4],
                    }
                )
                mismatch = _commitment_sha256(canonical) != str(row[7])
            except (TypeError, ValueError, json.JSONDecodeError):
                mismatch = True
            if mismatch:
                count += 1
                if len(samples) < options.sample_limit:
                    samples.append(membership_id)
    _add(
        findings,
        code="FACT_SEARCH_V2_MEMBERSHIP_BUNDLE_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context="recomputed structured fact-search membership bundles",
        samples=tuple(samples),
    )


def _audit_fact_search_v2_seals(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    count = 0
    samples: list[str] = []
    cursor = conn.execute(
        "SELECT seal.projection_seal_id,seal.projection_run_id,"
        "seal.manifest_id,seal.eligible_fact_cell_count,"
        "seal.membership_count,seal.included_count,"
        "seal.unresolved_material_count,seal.missing_provenance_count,"
        "seal.quarantined_count,seal.row_count,"
        "seal.membership_set_sha256,seal.row_set_sha256,"
        "seal.config_sha256,seal.sealed_at,run.manifest_id,"
        "run.config_sha256,run.recorded_at "
        "FROM search_fact_projection_seals AS seal "
        "LEFT JOIN search_fact_projection_runs AS run "
        "ON run.projection_run_id = seal.projection_run_id "
        "ORDER BY seal.projection_seal_id"
    )
    while batch := cursor.fetchmany(128):
        for row in batch:
            seal_id = str(row[0])
            run_id = str(row[1])
            disposition_counts = {
                "included": 0,
                "unresolved_material": 0,
                "missing_provenance": 0,
                "quarantined": 0,
            }
            membership_digest_state = hashlib.sha256()
            membership_digest_state.update(b"[")
            membership_count = 0
            membership_rows = conn.execute(
                "SELECT membership_id,membership_bundle_sha256,disposition "
                "FROM search_fact_projection_memberships "
                "WHERE projection_run_id = ? ORDER BY membership_id",
                (run_id,),
            )
            for item in membership_rows:
                disposition = str(item[2])
                if disposition in disposition_counts:
                    disposition_counts[disposition] += 1
                if membership_count:
                    membership_digest_state.update(b",")
                membership_digest_state.update(
                    _canonical_commitment_json(
                        {
                            "membership_bundle_sha256": item[1],
                            "membership_id": item[0],
                        }
                    ).encode("utf-8")
                )
                membership_count += 1
            membership_digest_state.update(b"]")
            membership_digest = membership_digest_state.hexdigest()
            row_count, row_digest = _canonical_sequence_digest(
                {
                    "fact_hit_id": item[0],
                    "row_bundle_sha256": item[1],
                }
                for item in conn.execute(
                    "SELECT fact_hit_id,row_bundle_sha256 "
                    "FROM search_fact_projection_rows "
                    "WHERE projection_run_id = ? ORDER BY fact_hit_id",
                    (run_id,),
                )
            )
            eligible_row = conn.execute(
                "SELECT COUNT(*) FROM fact_cells_v2 AS cell "
                "JOIN search_fact_projection_runs AS run "
                "ON run.projection_run_id = ? "
                "WHERE cell.knowledge_at <= run.knowledge_cutoff "
                "AND cell.recorded_at <= run.recorded_at",
                (run_id,),
            ).fetchone()
            eligible = 0 if eligible_row is None else int(eligible_row[0])
            mismatch = (
                row[14] is None
                or row[2] != row[14]
                or row[12] != row[15]
                or _audit_utc_datetime(row[13]) < _audit_utc_datetime(row[16])
                or int(row[3]) != eligible
                or int(row[4]) != membership_count
                or int(row[5]) != disposition_counts["included"]
                or int(row[6]) != disposition_counts["unresolved_material"]
                or int(row[7]) != disposition_counts["missing_provenance"]
                or int(row[8]) != disposition_counts["quarantined"]
                or int(row[9]) != row_count
                or row_count != disposition_counts["included"]
                or str(row[10]) != membership_digest
                or str(row[11]) != row_digest
            )
            if mismatch:
                count += 1
                if len(samples) < options.sample_limit:
                    samples.append(seal_id)
    _add(
        findings,
        code="FACT_SEARCH_V2_PROJECTION_SEAL_MISMATCH",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        count=count,
        query_context=(
            "recomputed fact-search eligible cells, dispositions, row coverage, "
            "and ordered set commitments"
        ),
        samples=tuple(samples),
    )


def _audit_fact_search_v2_hardened_inclusion(
    conn: sqlite3.Connection,
    findings: list[IntegrityFinding],
    options: AuditOptions,
) -> None:
    _query_finding(
        conn,
        findings,
        options,
        code="FACT_SEARCH_V2_INCLUDED_WITHOUT_COMMITMENT",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        query=(
            "SELECT membership.membership_id "
            "FROM search_fact_projection_memberships AS membership "
            "JOIN fact_resolution_revisions_v2 AS resolution "
            "ON resolution.resolution_revision_id = "
            "membership.resolution_revision_id "
            "LEFT JOIN fact_observation_payload_commitments_v2 AS payload "
            "ON payload.observation_id = resolution.selected_observation_id "
            "LEFT JOIN fact_observations_v2 AS observation "
            "ON observation.observation_id = resolution.selected_observation_id "
            "LEFT JOIN fact_reported_observation_anchors_v2 AS anchor "
            "ON anchor.observation_id = resolution.selected_observation_id "
            "LEFT JOIN fact_extraction_run_completeness_seals_v2 AS extraction "
            "ON extraction.extraction_run_id = anchor.extraction_run_id "
            "WHERE membership.disposition = 'included' "
            "AND (payload.observation_id IS NULL "
            "OR observation.observation_id IS NULL "
            "OR (observation.observation_kind = 'reported' "
            "AND (anchor.observation_id IS NULL "
            "OR extraction.extraction_seal_id IS NULL))) "
            "ORDER BY membership.membership_id"
        ),
    )
    _query_finding(
        conn,
        findings,
        options,
        code="FACT_SEARCH_V2_ROW_WITHOUT_COMMITMENT",
        severity=Severity.BLOCKER,
        remediation=RemediationClass.HARD_STOP,
        query=(
            "SELECT row.fact_hit_id "
            "FROM search_fact_projection_rows AS row "
            "LEFT JOIN fact_observation_payload_commitments_v2 AS payload "
            "ON payload.observation_id = row.observation_id "
            "LEFT JOIN fact_observations_v2 AS observation "
            "ON observation.observation_id = row.observation_id "
            "LEFT JOIN fact_reported_observation_anchors_v2 AS anchor "
            "ON anchor.observation_id = row.observation_id "
            "LEFT JOIN fact_extraction_run_completeness_seals_v2 AS extraction "
            "ON extraction.extraction_run_id = anchor.extraction_run_id "
            "WHERE payload.observation_id IS NULL "
            "OR observation.observation_id IS NULL "
            "OR (observation.observation_kind = 'reported' "
            "AND (anchor.observation_id IS NULL "
            "OR extraction.extraction_seal_id IS NULL)) "
            "ORDER BY row.fact_hit_id"
        ),
    )
