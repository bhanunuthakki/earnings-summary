"""Populate issuer-scoped Research Snapshots from exact sealed coordinates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal, Self, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from provenance.population_completeness import (
    PopulationArtifactSetCommitment,
    PopulationPlaneVerification,
    PopulationTemporalScope,
    canonical_json,
    digest_text,
    stream_population_artifact_set,
)
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    ResearchSnapshotRequest,
    ResearchUniverse,
    build_research_snapshot,
    verify_research_snapshot,
)
from search.embedding_promotion import PURPOSE

_REPORTING_FAMILIES = (
    "annual_securities_report",
    "continuous_disclosure",
    "investment_company_periodic",
    "issuer_earnings_materials",
    "issuer_financial_statements",
    "issuer_presentations",
    "operating_company_periodic",
)
_RESEARCH_SELECTION_POLICY = "research-snapshot-terminal-at-k-observed-through-o.v1"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchSnapshotPopulationRequest(_FrozenModel):
    cutoff_at: datetime
    operation_recorded_at: datetime = Field(
        validation_alias=AliasChoices("operation_recorded_at", "recorded_at")
    )
    issuer_ids: tuple[str, ...] = ()
    apply: bool = False
    input_commitment_sha256: str | None = None
    plan_commitment_sha256: str | None = None

    @field_validator("input_commitment_sha256", "plan_commitment_sha256")
    @classmethod
    def _commitment_sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("population commitment must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _commitment_contract(self) -> Self:
        if (self.input_commitment_sha256 is None) != (self.plan_commitment_sha256 is None):
            raise ValueError("population commitments must be supplied together")
        if self.apply and self.issuer_ids and self.input_commitment_sha256 is None:
            raise ValueError("a subset apply requires population commitments")
        return self


class IssuerResearchSnapshotStatus(_FrozenModel):
    issuer_id: str
    ready: bool
    research_snapshot_id: str | None = None
    research_snapshot_sha256: str | None = None
    document_count: int = Field(ge=0)
    reporting_entity_count: int = Field(ge=0)
    blockers: tuple[str, ...] = ()


class ResearchSnapshotPopulationResult(_FrozenModel):
    mode: Literal["dry_run", "apply"]
    issuer_count: int
    ready_issuer_count: int
    blocked_issuer_count: int
    created_snapshot_count: int
    statuses: tuple[IssuerResearchSnapshotStatus, ...]
    input_commitment_sha256: str
    plan_commitment_sha256: str
    output_commitment_sha256: str


class ResearchSnapshotPlanError(ValueError):
    """One stable, machine-readable Research Snapshot population blocker."""


def verify_research_snapshots(
    conn: sqlite3.Connection,
    scope: PopulationTemporalScope,
) -> PopulationPlaneVerification:
    """Verify persisted research snapshots at K as actually visible by O."""

    knowledge, observed = _utc(scope.knowledge_cutoff), _utc(scope.observed_through)
    expected = len(_issuer_ids(conn, knowledge, observed))
    header_set = stream_population_artifact_set(
        conn,
        table="research_snapshot_headers",
        query=(
            "SELECT header.research_snapshot_id AS artifact_id,"
            "header.request_sha256 AS payload_sha256,"
            "seal.member_set_sha256 AS seal_sha256,"
            "header.cutoff_at AS knowledge_at,"
            "seal.sealed_at AS recorded_at "
            "FROM research_snapshot_headers header "
            "JOIN research_snapshot_seals seal "
            "ON seal.research_snapshot_id=header.research_snapshot_id "
            "WHERE datetime(header.cutoff_at)=datetime(?) "
            "AND datetime(header.recorded_at)=datetime(?) "
            "AND datetime(seal.sealed_at)=datetime(?) "
            "ORDER BY header.research_snapshot_id"
        ),
        params=(_db_time(knowledge), _db_time(observed), _db_time(observed)),
        selection_policy_id=_RESEARCH_SELECTION_POLICY + ".request",
    )
    universe_set = stream_population_artifact_set(
        conn,
        table="research_snapshot_universe_commitments",
        query=(
            "SELECT universe.research_snapshot_id AS artifact_id,"
            "universe.universe_sha256 AS payload_sha256,"
            "seal.member_set_sha256 AS seal_sha256,"
            "universe.cutoff_at AS knowledge_at,"
            "seal.sealed_at AS recorded_at "
            "FROM research_snapshot_universe_commitments universe "
            "JOIN research_snapshot_headers header "
            "ON header.research_snapshot_id=universe.research_snapshot_id "
            "JOIN research_snapshot_seals seal "
            "ON seal.research_snapshot_id=universe.research_snapshot_id "
            "WHERE datetime(universe.cutoff_at)=datetime(?) "
            "AND datetime(universe.recorded_at)=datetime(?) "
            "AND datetime(header.recorded_at)=datetime(?) "
            "AND datetime(seal.sealed_at)=datetime(?) "
            "ORDER BY universe.issuer_id,universe.research_snapshot_id"
        ),
        params=(
            _db_time(knowledge),
            _db_time(observed),
            _db_time(observed),
            _db_time(observed),
        ),
        selection_policy_id=_RESEARCH_SELECTION_POLICY + ".universe",
    )
    if header_set.row_count != universe_set.row_count:
        raise ValueError("research snapshot artifact commitments disagree")
    duplicate = conn.execute(
        "SELECT 1 FROM research_snapshot_universe_commitments universe "
        "JOIN research_snapshot_headers header "
        "ON header.research_snapshot_id=universe.research_snapshot_id "
        "JOIN research_snapshot_seals seal "
        "ON seal.research_snapshot_id=universe.research_snapshot_id "
        "WHERE datetime(universe.cutoff_at)=datetime(?) "
        "AND datetime(universe.recorded_at)=datetime(?) "
        "AND datetime(header.recorded_at)=datetime(?) "
        "AND datetime(seal.sealed_at)=datetime(?) "
        "GROUP BY universe.issuer_id HAVING COUNT(*)<>1 LIMIT 1",
        (
            _db_time(knowledge),
            _db_time(observed),
            _db_time(observed),
            _db_time(observed),
        ),
    ).fetchone()
    if duplicate is not None:
        raise ValueError("research snapshot artifact scope is ambiguous at K,O")
    _verify_terminal_research_requests(
        conn,
        issuer_ids=_issuer_ids(conn, knowledge, observed),
        knowledge=knowledge,
        observed=observed,
    )
    artifacts = tuple(sorted((header_set, universe_set), key=lambda item: item.table))
    return _research_plane_verification(scope=scope, expected=expected, artifacts=artifacts)


def populate_research_snapshots(
    conn: sqlite3.Connection,
    request: ResearchSnapshotPopulationRequest,
) -> ResearchSnapshotPopulationResult:
    """Build every ready issuer snapshot and retain exact blocker codes."""

    cutoff = _utc(request.cutoff_at)
    recorded = _utc(request.operation_recorded_at)
    if recorded < cutoff:
        raise ValueError("research snapshot operation_recorded_at must not precede cutoff_at")
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        _require_schema(conn)
        discovered = _issuer_ids(conn, cutoff, recorded)
        if not discovered:
            raise ResearchSnapshotPlanError("research_snapshot_expected_universe_empty")
        requested = tuple(sorted(set(request.issuer_ids)))
        issuers = requested or discovered
        unknown = sorted(set(requested) - set(discovered))
        status_by_issuer: dict[str, IssuerResearchSnapshotStatus] = {
            issuer_id: IssuerResearchSnapshotStatus(
                issuer_id=issuer_id,
                ready=False,
                document_count=0,
                reporting_entity_count=0,
                blockers=("issuer_not_in_expected_research_universe",),
            )
            for issuer_id in unknown
        }
        plans: dict[str, ResearchSnapshotRequest] = {}
        commitment_entries: list[dict[str, object]] = []
        for issuer_id in issuers:
            if issuer_id in unknown:
                commitment_entries.append(
                    {
                        "blocker": "issuer_not_in_expected_research_universe",
                        "issuer_id": issuer_id,
                    }
                )
                continue
            try:
                plan = assemble_research_snapshot_request(
                    conn,
                    issuer_id,
                    cutoff,
                    observed_through=recorded,
                )
                plans[issuer_id] = plan
                commitment_entries.append(_request_input_manifest(conn, plan))
                status_by_issuer[issuer_id] = IssuerResearchSnapshotStatus(
                    issuer_id=issuer_id,
                    ready=True,
                    research_snapshot_id=plan.research_snapshot_id,
                    document_count=len(plan.research_universe.document_version_ids),
                    reporting_entity_count=len(plan.research_universe.reporting_entity_ids),
                )
            except (ResearchSnapshotPlanError, ValueError, RuntimeError) as exc:
                blocker = _blocker(exc)
                commitment_entries.append({"blocker": blocker, "issuer_id": issuer_id})
                status_by_issuer[issuer_id] = IssuerResearchSnapshotStatus(
                    issuer_id=issuer_id,
                    ready=False,
                    document_count=_issuer_document_count(
                        conn,
                        issuer_id,
                        cutoff,
                        recorded,
                    ),
                    reporting_entity_count=0,
                    blockers=(blocker,),
                )

        input_commitment = _population_input_commitment(
            cutoff,
            discovered,
            issuers,
            commitment_entries,
        )
        plan_commitment = _population_plan_commitment(
            request,
            input_commitment=input_commitment,
            selected_issuer_ids=issuers,
        )
        _verify_commitments(
            request,
            input_sha=input_commitment,
            plan_sha=plan_commitment,
        )
        created = 0
        if request.apply:
            for issuer_id in sorted(plans):
                plan = plans[issuer_id]
                try:
                    existing = conn.execute(
                        "SELECT 1 FROM research_snapshot_seals WHERE research_snapshot_id=?",
                        (plan.research_snapshot_id,),
                    ).fetchone()
                    with conn:
                        admission = (
                            verify_research_snapshot(conn, plan.research_snapshot_id)
                            if existing is not None
                            else build_research_snapshot(conn, plan)
                        )
                    created += int(existing is None)
                    status_by_issuer[issuer_id] = status_by_issuer[issuer_id].model_copy(
                        update={"research_snapshot_sha256": (admission.member_set_sha256)}
                    )
                except (ValueError, RuntimeError) as exc:
                    prior = status_by_issuer[issuer_id]
                    status_by_issuer[issuer_id] = prior.model_copy(
                        update={"ready": False, "blockers": (_blocker(exc),)}
                    )
        ordered = tuple(status_by_issuer[issuer_id] for issuer_id in sorted(status_by_issuer))
        return ResearchSnapshotPopulationResult(
            mode="apply" if request.apply else "dry_run",
            issuer_count=len(ordered),
            ready_issuer_count=sum(item.ready for item in ordered),
            blocked_issuer_count=sum(not item.ready for item in ordered),
            created_snapshot_count=created,
            statuses=ordered,
            input_commitment_sha256=input_commitment,
            plan_commitment_sha256=plan_commitment,
            output_commitment_sha256=_output_commitment(conn, cutoff),
        )
    finally:
        conn.row_factory = original_row_factory


def assemble_research_snapshot_request(
    conn: sqlite3.Connection,
    issuer_id: str,
    cutoff_at: datetime,
    *,
    observed_through: datetime | None = None,
) -> ResearchSnapshotRequest:
    """Assemble one exact issuer request or raise one stable blocker."""

    cutoff = _utc(cutoff_at)
    observed = cutoff if observed_through is None else _utc(observed_through)
    if observed < cutoff:
        raise ValueError("observed_through must not precede cutoff_at")
    processing_snapshot_id, documents = _processing_coordinate(conn, issuer_id, cutoff, observed)
    reporting_entities = _reporting_entities(conn, issuer_id, documents)
    manifest_id = select_exact_corpus_coordinate(
        conn,
        documents,
        cutoff,
        observed_through=observed,
    )
    lexical_id, vector_id, promotion_id = select_retrieval_coordinates(
        conn,
        manifest_id,
        cutoff,
        observed_through=observed,
    )
    ontology_id = _ontology_coordinate(conn, cutoff, observed)
    resolution_id = _resolution_coordinate(
        conn,
        issuer_id,
        reporting_entities,
        cutoff,
        observed,
    )
    projection_id = _canonical_projection_coordinate(
        conn,
        resolution_id,
        ontology_id,
        cutoff,
        observed,
    )
    obligation_ids = _obligation_coordinates(conn, manifest_id, issuer_id)
    publication_ids = _publication_coordinates(conn, resolution_id, cutoff, observed)
    identity_payload = {
        "canonical_fact_projection_run_id": projection_id,
        "canonical_fact_resolution_snapshot_id": resolution_id,
        "corpus_manifest_id": manifest_id,
        "cutoff_at": _db_time(cutoff),
        "issuer_id": issuer_id,
        "ontology_snapshot_id": ontology_id,
        "processing_snapshot_id": processing_snapshot_id,
        "source_fact_publication_ids": list(publication_ids),
    }
    research_snapshot_id = "research-snapshot:" + _digest(identity_payload)
    return ResearchSnapshotRequest(
        research_snapshot_id=research_snapshot_id,
        idempotency_key=research_snapshot_id,
        research_universe=ResearchUniverse(
            issuer_id=issuer_id,
            reporting_entity_ids=reporting_entities,
            document_version_ids=documents,
            source_obligation_revision_ids=obligation_ids,
        ),
        processing_snapshot_ids=(processing_snapshot_id,),
        corpus_bundles=(
            CorpusProjectionBundle(
                corpus_manifest_id=manifest_id,
                lexical_index_run_id=lexical_id,
                vector_index_run_id=vector_id,
                embedding_promotion_id=promotion_id,
            ),
        ),
        source_fact_publication_ids=publication_ids,
        ontology_snapshot_id=ontology_id,
        canonical_fact_resolution_snapshot_id=resolution_id,
        canonical_fact_projection_run_id=projection_id,
        cutoff_at=cutoff,
        recorded_at=observed,
    )


def _require_schema(conn: sqlite3.Connection) -> None:
    required = {
        "canonical_fact_projection_audit_receipts",
        "canonical_fact_projection_generations",
        "canonical_fact_projection_scope_bindings",
        "canonical_fact_projection_seals",
        "canonical_fact_candidate_dispositions",
        "canonical_fact_resolution_snapshot_members",
        "canonical_fact_resolution_snapshot_seals",
        "canonical_fact_resolution_snapshot_scope_headers",
        "canonical_fact_resolution_snapshot_scope_members",
        "canonical_fact_resolution_snapshot_scope_seals",
        "document_processing_snapshot_headers",
        "document_processing_snapshot_members",
        "document_processing_snapshot_seals",
        "expected_document_obligation_bindings",
        "expected_documents",
        "ontology_snapshot_headers",
        "ontology_snapshot_seals",
        "research_snapshot_headers",
        "research_snapshot_seals",
        "search_corpus_manifest_seals",
        "search_corpus_manifests",
        "search_corpus_document_memberships",
        "search_embedding_model_promotions",
        "search_manifest_source_inventories",
        "search_projection_seals",
        "source_fact_publications",
        "source_fact_publication_seals",
        "source_obligation_revisions",
        "v_evidence_document_versions_canonical",
    }
    present = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    missing = sorted(required - present)
    if missing:
        raise RuntimeError("research_snapshot_population_schema_missing:" + ",".join(missing))


def _issuer_ids(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime | None = None,
) -> tuple[str, ...]:
    observed = cutoff if observed_through is None else observed_through
    placeholders = ",".join("?" for _ in _REPORTING_FAMILIES)
    rows = conn.execute(
        "SELECT DISTINCT obligation.issuer_id "
        "FROM source_obligation_revisions obligation "
        "WHERE obligation.obligation_state IN ('required','optional') "
        f"AND obligation.document_family IN ({placeholders}) "  # nosec B608 -- fixed policy vocabulary
        "AND obligation.reporting_entity_id IS NOT NULL "
        "AND datetime(obligation.active_from)<=datetime(?) "
        "AND (obligation.active_to IS NULL "
        "OR datetime(obligation.active_to)>datetime(?)) "
        "AND datetime(obligation.knowledge_at)<=datetime(?) "
        "AND datetime(obligation.recorded_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM source_obligation_revisions newer "
        "WHERE newer.obligation_key=obligation.obligation_key "
        "AND newer.revision>obligation.revision "
        "AND datetime(newer.knowledge_at)<=datetime(?) "
        "AND datetime(newer.recorded_at)<=datetime(?)) "
        "ORDER BY obligation.issuer_id",
        (
            *_REPORTING_FAMILIES,
            _db_time(cutoff),
            _db_time(cutoff),
            _db_time(cutoff),
            _db_time(observed),
            _db_time(cutoff),
            _db_time(observed),
        ),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _verify_terminal_research_requests(
    conn: sqlite3.Connection,
    *,
    issuer_ids: tuple[str, ...],
    knowledge: datetime,
    observed: datetime,
) -> None:
    rows = conn.execute(
        "SELECT universe.issuer_id,header.research_snapshot_id,header.request_json,"
        "header.request_sha256,universe.canonical_universe_json,"
        "universe.universe_sha256 "
        "FROM research_snapshot_universe_commitments universe "
        "JOIN research_snapshot_headers header "
        "ON header.research_snapshot_id=universe.research_snapshot_id "
        "JOIN research_snapshot_seals seal "
        "ON seal.research_snapshot_id=universe.research_snapshot_id "
        "WHERE datetime(universe.cutoff_at)=datetime(?) "
        "AND datetime(universe.recorded_at)=datetime(?) "
        "AND datetime(header.recorded_at)=datetime(?) "
        "AND datetime(seal.sealed_at)=datetime(?) "
        "ORDER BY universe.issuer_id,header.research_snapshot_id",
        (
            _db_time(knowledge),
            _db_time(observed),
            _db_time(observed),
            _db_time(observed),
        ),
    ).fetchall()
    expected_issuers = set(issuer_ids)
    actual_issuers = {str(row["issuer_id"]) for row in rows}
    if actual_issuers - expected_issuers:
        raise ValueError("research snapshot terminal issuer set differs from expected K,O scope")
    for row in rows:
        issuer_id = str(row["issuer_id"])
        request = assemble_research_snapshot_request(
            conn,
            issuer_id,
            knowledge,
            observed_through=observed,
        )
        request_json = canonical_json(request)
        universe_json = canonical_json(
            {
                "document_version_ids": list(request.research_universe.document_version_ids),
                "issuer_id": request.research_universe.issuer_id,
                "reporting_entity_ids": list(request.research_universe.reporting_entity_ids),
                "source_obligation_revision_ids": list(
                    request.research_universe.source_obligation_revision_ids
                ),
            }
        )
        if (
            str(row["research_snapshot_id"]) != request.research_snapshot_id
            or str(row["request_json"]) != request_json
            or str(row["request_sha256"]) != digest_text(request_json)
            or str(row["canonical_universe_json"]) != universe_json
            or str(row["universe_sha256"]) != digest_text(universe_json)
        ):
            raise ValueError(
                "research snapshot terminal request differs from assembled K,O request"
            )
        verify_research_snapshot(conn, request.research_snapshot_id)


def _processing_coordinate(
    conn: sqlite3.Connection,
    issuer_id: str,
    cutoff: datetime,
    observed: datetime,
) -> tuple[str, tuple[str, ...]]:
    rows = conn.execute(
        "SELECT DISTINCT header.processing_snapshot_id "
        "FROM document_processing_snapshot_headers header "
        "JOIN document_processing_snapshot_seals seal "
        "ON seal.processing_snapshot_id=header.processing_snapshot_id "
        "JOIN document_processing_snapshot_members member "
        "ON member.processing_snapshot_id=header.processing_snapshot_id "
        "JOIN v_evidence_document_versions_canonical document "
        "ON document.document_version_id=member.document_version_id "
        "WHERE document.issuer_id=? AND datetime(header.cutoff_at)=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "ORDER BY datetime(header.recorded_at) DESC,"
        "datetime(seal.sealed_at) DESC,header.processing_snapshot_id DESC",
        (
            issuer_id,
            _db_time(cutoff),
            _db_time(observed),
            _db_time(observed),
        ),
    ).fetchall()
    if not rows:
        raise ResearchSnapshotPlanError("processing_snapshot_missing_or_ambiguous")
    snapshot_id = str(rows[0][0])
    documents = tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT member.document_version_id "
            "FROM document_processing_snapshot_members member "
            "JOIN v_evidence_document_versions_canonical document "
            "ON document.document_version_id=member.document_version_id "
            "WHERE member.processing_snapshot_id=? AND document.issuer_id=? "
            "ORDER BY member.document_version_id",
            (snapshot_id, issuer_id),
        )
    )
    if not documents:
        raise ResearchSnapshotPlanError("processing_snapshot_empty")
    return snapshot_id, documents


def _reporting_entities(
    conn: sqlite3.Connection,
    issuer_id: str,
    documents: tuple[str, ...],
) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT DISTINCT reporting_entity_id "
        "FROM v_evidence_document_versions_canonical "
        "WHERE issuer_id=? "
        "AND document_version_id IN (SELECT value FROM json_each(?)) "
        "ORDER BY reporting_entity_id",
        (issuer_id, json.dumps(documents)),
    ).fetchall()
    if any(row[0] is None for row in rows):
        raise ResearchSnapshotPlanError("research_document_subject_missing")
    entities = tuple(str(row[0]) for row in rows)
    if not entities:
        raise ResearchSnapshotPlanError("research_reporting_entity_missing")
    return entities


def select_exact_corpus_coordinate(
    conn: sqlite3.Connection,
    documents: tuple[str, ...],
    cutoff: datetime,
    *,
    observed_through: datetime | None = None,
) -> str:
    observed = cutoff if observed_through is None else _utc(observed_through)
    rows = conn.execute(
        "SELECT manifest.manifest_id "
        "FROM search_corpus_manifests manifest "
        "JOIN search_corpus_manifest_seals seal "
        "ON seal.manifest_id=manifest.manifest_id "
        "WHERE seal.completion_status='complete' "
        "AND manifest.knowledge_cutoff IS NOT NULL "
        "AND datetime(manifest.knowledge_cutoff)=datetime(?) "
        "AND datetime(manifest.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "ORDER BY manifest.revision DESC,manifest.manifest_id",
        (_db_time(cutoff), _db_time(observed), _db_time(observed)),
    ).fetchall()
    expected = frozenset(documents)
    matches: list[str] = []
    for row in rows:
        manifest_id = str(row[0])
        included = frozenset(
            str(member[0])
            for member in conn.execute(
                "SELECT document_version_id "
                "FROM search_corpus_document_memberships "
                "WHERE manifest_id=? AND membership_status='included' "
                "ORDER BY document_version_id",
                (manifest_id,),
            )
            if member[0] is not None
        )
        if included == expected:
            matches.append(manifest_id)
    if not matches:
        raise ResearchSnapshotPlanError("exact_search_corpus_missing_or_ambiguous")
    return matches[0]


def select_retrieval_coordinates(
    conn: sqlite3.Connection,
    manifest_id: str,
    cutoff: datetime,
    *,
    observed_through: datetime | None = None,
) -> tuple[str, str, str]:
    observed = cutoff if observed_through is None else _utc(observed_through)
    by_kind: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT * FROM search_projection_seals "
        "WHERE manifest_id=? AND datetime(sealed_at)<=datetime(?) "
        "ORDER BY datetime(sealed_at) DESC,index_run_id",
        (manifest_id, _db_time(observed)),
    ):
        by_kind.setdefault(str(row["index_kind"]), []).append(row)
    lexical_rows = by_kind.get("lexical", [])
    vector_rows = by_kind.get("vector", [])
    if not lexical_rows:
        raise ResearchSnapshotPlanError("lexical_projection_seal_missing_or_ambiguous")
    if not vector_rows:
        raise ResearchSnapshotPlanError("vector_projection_seal_missing_or_ambiguous")
    lexical = lexical_rows[0]
    vector = vector_rows[0]
    if (
        vector["provider"] is None
        or vector["model"] is None
        or vector["dimensions"] is None
        or vector["runtime_artifact_sha256"] is None
    ):
        raise ResearchSnapshotPlanError("vector_runtime_coordinate_missing")
    promotions = conn.execute(
        "SELECT promotion_id FROM search_embedding_model_promotions "
        "WHERE purpose=? AND provider=? AND model=? AND dimensions=? "
        "AND runtime_artifact_sha256=? "
        "AND datetime(approved_at)<=datetime(?) "
        "AND datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM search_embedding_model_promotions newer "
        "WHERE newer.purpose=search_embedding_model_promotions.purpose "
        "AND newer.revision>search_embedding_model_promotions.revision "
        "AND datetime(newer.knowledge_at)<=datetime(?) "
        "AND datetime(newer.recorded_at)<=datetime(?)) "
        "ORDER BY revision DESC,promotion_id",
        (
            PURPOSE,
            str(vector["provider"]),
            str(vector["model"]),
            int(vector["dimensions"]),
            str(vector["runtime_artifact_sha256"]),
            _db_time(cutoff),
            _db_time(cutoff),
            _db_time(observed),
            _db_time(cutoff),
            _db_time(observed),
        ),
    ).fetchall()
    if len(promotions) != 1:
        raise ResearchSnapshotPlanError("embedding_model_not_uniquely_promoted")
    return str(lexical["index_run_id"]), str(vector["index_run_id"]), str(promotions[0][0])


def _ontology_coordinate(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime | None = None,
) -> str:
    observed = cutoff if observed_through is None else _utc(observed_through)
    rows = conn.execute(
        "SELECT header.ontology_snapshot_id "
        "FROM ontology_snapshot_headers header "
        "JOIN ontology_snapshot_seals seal "
        "ON seal.ontology_snapshot_id=header.ontology_snapshot_id "
        "WHERE datetime(header.cutoff_at)=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "ORDER BY datetime(header.recorded_at) DESC,"
        "datetime(seal.sealed_at) DESC,header.ontology_snapshot_id DESC",
        (_db_time(cutoff), _db_time(observed), _db_time(observed)),
    ).fetchall()
    if not rows:
        raise ResearchSnapshotPlanError("ontology_snapshot_missing_or_ambiguous")
    return str(rows[0][0])


def _resolution_coordinate(
    conn: sqlite3.Connection,
    issuer_id: str,
    reporting_entities: tuple[str, ...],
    cutoff: datetime,
    observed_through: datetime | None = None,
) -> str:
    observed = cutoff if observed_through is None else _utc(observed_through)
    rows = conn.execute(
        "SELECT header.resolution_snapshot_id,header.recorded_at,"
        "scope_seal.sealed_at,fact_seal.sealed_at "
        "FROM canonical_fact_resolution_snapshot_scope_headers header "
        "JOIN canonical_fact_resolution_snapshot_scope_seals scope_seal "
        "ON scope_seal.resolution_snapshot_id=header.resolution_snapshot_id "
        "JOIN canonical_fact_resolution_snapshot_seals fact_seal "
        "ON fact_seal.resolution_snapshot_id=header.resolution_snapshot_id "
        "WHERE header.issuer_id=? AND datetime(header.cutoff_at)=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?) "
        "AND datetime(scope_seal.sealed_at)<=datetime(?) "
        "AND datetime(fact_seal.sealed_at)<=datetime(?) "
        "ORDER BY datetime(header.recorded_at) DESC,"
        "datetime(scope_seal.sealed_at) DESC,"
        "datetime(fact_seal.sealed_at) DESC,"
        "header.resolution_snapshot_id DESC",
        (
            issuer_id,
            _db_time(cutoff),
            _db_time(observed),
            _db_time(observed),
            _db_time(observed),
        ),
    ).fetchall()
    matches: list[str] = []
    for row in rows:
        snapshot_id = str(row[0])
        members = tuple(
            str(member[0])
            for member in conn.execute(
                "SELECT reporting_entity_id "
                "FROM canonical_fact_resolution_snapshot_scope_members "
                "WHERE resolution_snapshot_id=? ORDER BY reporting_entity_id",
                (snapshot_id,),
            )
        )
        if members == reporting_entities:
            matches.append(snapshot_id)
    if not matches:
        raise ResearchSnapshotPlanError("issuer_scoped_canonical_resolution_missing_or_ambiguous")
    return matches[0]


def _canonical_projection_coordinate(
    conn: sqlite3.Connection,
    resolution_id: str,
    ontology_id: str,
    cutoff: datetime,
    observed_through: datetime | None = None,
) -> str:
    observed = cutoff if observed_through is None else _utc(observed_through)
    rows = conn.execute(
        "SELECT generation.generation_id "
        "FROM canonical_fact_projection_generations generation "
        "JOIN canonical_fact_projection_seals seal "
        "ON seal.generation_id=generation.generation_id "
        "JOIN canonical_fact_projection_audit_receipts audit "
        "ON audit.generation_id=generation.generation_id "
        "JOIN canonical_fact_projection_scope_bindings scope "
        "ON scope.generation_id=generation.generation_id "
        "WHERE generation.resolution_snapshot_id=? "
        "AND generation.ontology_snapshot_id=? "
        "AND scope.resolution_snapshot_id=? "
        "AND datetime(generation.cutoff_at)=datetime(?) "
        "AND datetime(generation.recorded_at)<=datetime(?) "
        "AND datetime(scope.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "AND datetime(audit.audited_at)<=datetime(?) "
        "ORDER BY datetime(generation.recorded_at) DESC,"
        "datetime(scope.recorded_at) DESC,"
        "datetime(seal.sealed_at) DESC,"
        "datetime(audit.audited_at) DESC,"
        "generation.generation_id DESC",
        (
            resolution_id,
            ontology_id,
            resolution_id,
            _db_time(cutoff),
            _db_time(observed),
            _db_time(observed),
            _db_time(observed),
            _db_time(observed),
        ),
    ).fetchall()
    if not rows:
        raise ResearchSnapshotPlanError("audited_canonical_projection_missing_or_ambiguous")
    return str(rows[0][0])


def _obligation_coordinates(
    conn: sqlite3.Connection,
    manifest_id: str,
    issuer_id: str,
) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT membership.membership_id,"
        "binding.source_obligation_revision_id,binding.issuer_id "
        "FROM search_corpus_document_memberships membership "
        "JOIN search_manifest_source_inventories inventory "
        "ON inventory.manifest_id=membership.manifest_id "
        "JOIN expected_documents expected "
        "ON expected.snapshot_id=inventory.snapshot_id "
        "AND expected.expected_document_key=membership.expected_document_key "
        "JOIN expected_document_obligation_bindings binding "
        "ON binding.expected_document_id=expected.expected_document_id "
        "WHERE membership.manifest_id=? "
        "ORDER BY membership.membership_id",
        (manifest_id,),
    ).fetchall()
    membership_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM search_corpus_document_memberships WHERE manifest_id=?",
            (manifest_id,),
        ).fetchone()[0]
    )
    if not rows or len(rows) != membership_count or any(str(row[2]) != issuer_id for row in rows):
        raise ResearchSnapshotPlanError("corpus_obligation_binding_incomplete")
    return tuple(sorted({str(row[1]) for row in rows}))


def _publication_coordinates(
    conn: sqlite3.Connection,
    resolution_id: str,
    cutoff: datetime,
    observed_through: datetime | None = None,
) -> tuple[str, ...]:
    observed = cutoff if observed_through is None else _utc(observed_through)
    required_rows = conn.execute(
        "SELECT DISTINCT candidate.source_publication_id "
        "FROM canonical_fact_resolution_snapshot_members member "
        "JOIN canonical_fact_candidate_dispositions candidate "
        "ON candidate.candidate_universe_id=member.candidate_universe_id "
        "WHERE member.resolution_snapshot_id=? "
        "AND candidate.source_publication_id IS NOT NULL "
        "ORDER BY candidate.source_publication_id",
        (resolution_id,),
    ).fetchall()
    required = tuple(str(row[0]) for row in required_rows)
    if not required:
        return ()
    rows = conn.execute(
        "SELECT publication.publication_id "
        "FROM source_fact_publications publication "
        "JOIN source_fact_publication_seals seal "
        "ON seal.publication_id=publication.publication_id "
        "WHERE publication.publication_id IN (SELECT value FROM json_each(?)) "
        "AND datetime(publication.created_at)<=datetime(?) "
        "AND datetime(publication.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "ORDER BY publication.publication_id",
        (
            json.dumps(required),
            _db_time(cutoff),
            _db_time(observed),
            _db_time(observed),
        ),
    ).fetchall()
    sealed = tuple(str(row[0]) for row in rows)
    if sealed != required:
        raise ResearchSnapshotPlanError("source_fact_publication_seal_missing")
    return required


def _issuer_document_count(
    conn: sqlite3.Connection,
    issuer_id: str,
    cutoff: datetime,
    observed: datetime,
) -> int:
    try:
        return len(_processing_coordinate(conn, issuer_id, cutoff, observed)[1])
    except ResearchSnapshotPlanError:
        return 0


def _request_input_manifest(
    conn: sqlite3.Connection,
    request: ResearchSnapshotRequest,
) -> dict[str, object]:
    """Freeze the complete assembled request plus every upstream seal/digest row."""

    processing_ids = request.processing_snapshot_ids
    manifest_ids = tuple(bundle.corpus_manifest_id for bundle in request.corpus_bundles)
    projection_ids = tuple(
        coordinate
        for bundle in request.corpus_bundles
        for coordinate in (bundle.lexical_index_run_id, bundle.vector_index_run_id)
        if coordinate is not None
    )
    promotion_ids = tuple(
        bundle.embedding_promotion_id
        for bundle in request.corpus_bundles
        if bundle.embedding_promotion_id is not None
    )
    publication_ids = request.source_fact_publication_ids
    obligation_ids = request.research_universe.source_obligation_revision_ids
    return {
        "issuer_id": request.research_universe.issuer_id,
        "request": request.model_dump(mode="json"),
        "upstream": {
            "canonical_fact_projection_audit_receipts": _rows_for_values(
                conn,
                "SELECT * FROM canonical_fact_projection_audit_receipts "
                "WHERE generation_id IN (SELECT value FROM json_each(?))",
                (request.canonical_fact_projection_run_id,),
            ),
            "canonical_fact_projection_generations": _rows_for_values(
                conn,
                "SELECT * FROM canonical_fact_projection_generations "
                "WHERE generation_id IN (SELECT value FROM json_each(?))",
                (request.canonical_fact_projection_run_id,),
            ),
            "canonical_fact_projection_scope_bindings": _rows_for_values(
                conn,
                "SELECT * FROM canonical_fact_projection_scope_bindings "
                "WHERE generation_id IN (SELECT value FROM json_each(?))",
                (request.canonical_fact_projection_run_id,),
            ),
            "canonical_fact_projection_seals": _rows_for_values(
                conn,
                "SELECT * FROM canonical_fact_projection_seals "
                "WHERE generation_id IN (SELECT value FROM json_each(?))",
                (request.canonical_fact_projection_run_id,),
            ),
            "canonical_fact_resolution_snapshot_scope_headers": _rows_for_values(
                conn,
                "SELECT * FROM canonical_fact_resolution_snapshot_scope_headers "
                "WHERE resolution_snapshot_id IN (SELECT value FROM json_each(?))",
                (request.canonical_fact_resolution_snapshot_id,),
            ),
            "canonical_fact_resolution_snapshot_scope_members": _rows_for_values(
                conn,
                "SELECT * FROM canonical_fact_resolution_snapshot_scope_members "
                "WHERE resolution_snapshot_id IN (SELECT value FROM json_each(?))",
                (request.canonical_fact_resolution_snapshot_id,),
            ),
            "canonical_fact_resolution_snapshot_scope_seals": _rows_for_values(
                conn,
                "SELECT * FROM canonical_fact_resolution_snapshot_scope_seals "
                "WHERE resolution_snapshot_id IN (SELECT value FROM json_each(?))",
                (request.canonical_fact_resolution_snapshot_id,),
            ),
            "canonical_fact_resolution_snapshot_seals": _rows_for_values(
                conn,
                "SELECT * FROM canonical_fact_resolution_snapshot_seals "
                "WHERE resolution_snapshot_id IN (SELECT value FROM json_each(?))",
                (request.canonical_fact_resolution_snapshot_id,),
            ),
            "document_processing_snapshot_headers": _rows_for_values(
                conn,
                "SELECT * FROM document_processing_snapshot_headers "
                "WHERE processing_snapshot_id IN (SELECT value FROM json_each(?))",
                processing_ids,
            ),
            "document_processing_snapshot_members": _rows_for_values(
                conn,
                "SELECT * FROM document_processing_snapshot_members "
                "WHERE processing_snapshot_id IN (SELECT value FROM json_each(?))",
                processing_ids,
            ),
            "document_processing_snapshot_seals": _rows_for_values(
                conn,
                "SELECT * FROM document_processing_snapshot_seals "
                "WHERE processing_snapshot_id IN (SELECT value FROM json_each(?))",
                processing_ids,
            ),
            "expected_document_obligation_bindings": _rows_for_values(
                conn,
                "SELECT * FROM expected_document_obligation_bindings "
                "WHERE source_obligation_revision_id "
                "IN (SELECT value FROM json_each(?))",
                obligation_ids,
            ),
            "ontology_snapshot_headers": _rows_for_values(
                conn,
                "SELECT * FROM ontology_snapshot_headers "
                "WHERE ontology_snapshot_id IN (SELECT value FROM json_each(?))",
                (request.ontology_snapshot_id,),
            ),
            "ontology_snapshot_seals": _rows_for_values(
                conn,
                "SELECT * FROM ontology_snapshot_seals "
                "WHERE ontology_snapshot_id IN (SELECT value FROM json_each(?))",
                (request.ontology_snapshot_id,),
            ),
            "search_corpus_document_memberships": _rows_for_values(
                conn,
                "SELECT * FROM search_corpus_document_memberships "
                "WHERE manifest_id IN (SELECT value FROM json_each(?))",
                manifest_ids,
            ),
            "search_corpus_manifest_seals": _rows_for_values(
                conn,
                "SELECT * FROM search_corpus_manifest_seals "
                "WHERE manifest_id IN (SELECT value FROM json_each(?))",
                manifest_ids,
            ),
            "search_corpus_manifests": _rows_for_values(
                conn,
                "SELECT * FROM search_corpus_manifests "
                "WHERE manifest_id IN (SELECT value FROM json_each(?))",
                manifest_ids,
            ),
            "search_embedding_model_promotions": _rows_for_values(
                conn,
                "SELECT * FROM search_embedding_model_promotions "
                "WHERE promotion_id IN (SELECT value FROM json_each(?))",
                promotion_ids,
            ),
            "search_manifest_source_inventories": _rows_for_values(
                conn,
                "SELECT * FROM search_manifest_source_inventories "
                "WHERE manifest_id IN (SELECT value FROM json_each(?))",
                manifest_ids,
            ),
            "search_projection_seals": _rows_for_values(
                conn,
                "SELECT * FROM search_projection_seals "
                "WHERE index_run_id IN (SELECT value FROM json_each(?))",
                projection_ids,
            ),
            "source_fact_publication_seals": _rows_for_values(
                conn,
                "SELECT * FROM source_fact_publication_seals "
                "WHERE publication_id IN (SELECT value FROM json_each(?))",
                publication_ids,
            ),
            "source_fact_publications": _rows_for_values(
                conn,
                "SELECT * FROM source_fact_publications "
                "WHERE publication_id IN (SELECT value FROM json_each(?))",
                publication_ids,
            ),
            "source_obligation_revisions": _rows_for_values(
                conn,
                "SELECT * FROM source_obligation_revisions "
                "WHERE obligation_revision_id IN (SELECT value FROM json_each(?))",
                obligation_ids,
            ),
        },
    }


def _population_input_commitment(
    cutoff: datetime,
    expected_issuer_ids: tuple[str, ...],
    selected_issuer_ids: tuple[str, ...],
    issuer_inputs: list[dict[str, object]],
) -> str:
    return _digest(
        {
            "cutoff_at": _db_time(cutoff),
            "expected_issuer_ids": list(expected_issuer_ids),
            "selected_issuer_ids": list(selected_issuer_ids),
            "issuer_inputs": sorted(
                issuer_inputs,
                key=lambda item: str(item["issuer_id"]),
            ),
        }
    )


def _population_plan_commitment(
    request: ResearchSnapshotPopulationRequest,
    *,
    input_commitment: str,
    selected_issuer_ids: tuple[str, ...],
) -> str:
    return _digest(
        {
            "cutoff_at": _db_time(request.cutoff_at),
            "input_commitment_sha256": input_commitment,
            "operation_recorded_at": _db_time(request.operation_recorded_at),
            "selected_issuer_ids": list(selected_issuer_ids),
        }
    )


def _verify_commitments(
    request: ResearchSnapshotPopulationRequest,
    *,
    input_sha: str,
    plan_sha: str,
) -> None:
    if request.input_commitment_sha256 is not None and request.input_commitment_sha256 != input_sha:
        raise ValueError("research snapshot input commitment changed")
    if request.plan_commitment_sha256 is not None and request.plan_commitment_sha256 != plan_sha:
        raise ValueError("research snapshot plan commitment changed")


def _research_plane_verification(
    *,
    scope: PopulationTemporalScope,
    expected: int,
    artifacts: tuple[PopulationArtifactSetCommitment, ...],
) -> PopulationPlaneVerification:
    if expected <= 0:
        raise ValueError("research snapshot expected universe is empty at K,O")
    materialized = artifacts[0].row_count
    if materialized > expected:
        raise ValueError("research snapshot artifact set exceeds expected universe")
    failed = expected - materialized
    details = cast(
        dict[str, JsonValue],
        {
            "knowledge_cutoff": _db_time(scope.knowledge_cutoff),
            "observed_through": _db_time(scope.observed_through),
            "selection_policy_id": _RESEARCH_SELECTION_POLICY,
        },
    )
    output_material = {
        "artifact_sets": [item.model_dump(mode="json") for item in artifacts],
        "details": details,
        "exclusion_counts": {},
        "expected_count": expected,
        "failed_count": failed,
        "materialized_count": materialized,
        "plane_name": "research_snapshot",
    }
    return PopulationPlaneVerification(
        plane_name="research_snapshot",
        expected_count=expected,
        materialized_count=materialized,
        excluded_count=0,
        failed_count=failed,
        exclusion_counts={},
        input_commitment_sha256=digest_text(
            canonical_json(
                {
                    "expected_count": expected,
                    "knowledge_cutoff": scope.knowledge_cutoff,
                    "observed_through": scope.observed_through,
                    "selection_policy_id": _RESEARCH_SELECTION_POLICY,
                }
            )
        ),
        output_commitment_sha256=digest_text(canonical_json(output_material)),
        artifact_sets=artifacts,
        details=details,
    )


def _rows_for_values(
    conn: sqlite3.Connection,
    sql: str,
    values: tuple[str, ...],
) -> list[dict[str, object]]:
    rows = [
        {str(key): _json_scalar(value) for key, value in dict(row).items()}
        for row in conn.execute(sql, (json.dumps(values),)).fetchall()
    ]
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def _json_scalar(value: object) -> object:
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, datetime):
        return _db_time(value)
    return value


def _output_commitment(conn: sqlite3.Connection, cutoff: datetime) -> str:
    rows = conn.execute(
        "SELECT header.research_snapshot_id,seal.member_set_sha256 "
        "FROM research_snapshot_headers header "
        "JOIN research_snapshot_seals seal "
        "ON seal.research_snapshot_id=header.research_snapshot_id "
        "WHERE datetime(header.cutoff_at)=datetime(?) "
        "ORDER BY header.research_snapshot_id",
        (_db_time(cutoff),),
    ).fetchall()
    return _digest([list(row) for row in rows])


def _blocker(exc: Exception) -> str:
    text = str(exc).strip()
    return (text or type(exc).__name__)[:256]


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return _utc(value).isoformat()
