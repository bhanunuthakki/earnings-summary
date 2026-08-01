"""Authoritative, read-only semantic qualification for a sealed rehearsal clone.

The generator reuses the production Ask verifier and retrieval-runtime
qualifier.  It emits evidence only after the committed composite registry,
every current Ask promotion, full nonempty corpora, local runtime bytes,
sealed vector projections, and grounded fact/lexical/semantic canaries agree.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ask.audit_store import canonical_json, digest_text
from ask.sealed_retrieval import (
    ReadyRetrievalScope,
    assess_retrieval_readiness,
    load_production_scopes,
)
from provenance.immutable_artifact import (
    ImmutableArtifactSnapshot,
    assert_artifact_unchanged,
    read_stable_artifact,
    require_no_reparse_points,
)
from provenance.latest_state_activation import require_checkpointed_sidecars
from provenance.latest_state_rehearsal import (
    SEMANTIC_FACT_CANARY_POLICY_MAX_MILLISECONDS,
    SEMANTIC_ISSUER_QUALIFICATION_POLICY_MAX_MILLISECONDS,
    ArtifactCommitment,
    DatabaseFileState,
    GroundedCanaryCommitment,
    SemanticIssuerQualification,
    SemanticQualificationEvidence,
    SemanticScopeQualification,
    build_semantic_qualification_evidence,
)
from provenance.population_retrieval_runtime import (
    RetrievalIssuerPopulationResult,
    RetrievalRuntimePopulationRequest,
    RetrievalRuntimePopulationResult,
    populate_retrieval_runtime,
)
from provenance.search_index_lineage import load_projection_seal
from search.canonical_fact_projection import search_canonical_facts
from search.embedding_promotion import LocalVectorRuntimeConfig, current_promotion
from search.exact_semantic import MAX_EXACT_VECTOR_ROWS
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticQualificationRequest(_FrozenModel):
    """Bounded operational inputs for one exact semantic qualification pass."""

    schema_version: Literal["latest-governed-semantic-qualification-request/v1"] = (
        "latest-governed-semantic-qualification-request/v1"
    )
    index_root: Path
    runtime_root: Path
    exact_row_cap: int = Field(ge=1, le=MAX_EXACT_VECTOR_ROWS)
    fact_canary_limit: int = Field(ge=1, le=1_000)
    max_fact_canary_milliseconds: float = Field(
        gt=0,
        le=SEMANTIC_FACT_CANARY_POLICY_MAX_MILLISECONDS,
        allow_inf_nan=False,
    )
    max_issuer_qualification_milliseconds: float = Field(
        gt=0,
        le=SEMANTIC_ISSUER_QUALIFICATION_POLICY_MAX_MILLISECONDS,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def _canonical_roots(self) -> Self:
        _require_current_roots(self.index_root, self.runtime_root)
        return self


class _CorpusCoverage(_FrozenModel):
    manifest_id: str
    membership_sha256: str
    expected_document_count: int
    included_document_count: int
    chunk_count: int


def _require_current_roots(index_root: Path, runtime_root: Path) -> None:
    roots = (index_root, runtime_root)
    for root in roots:
        require_no_reparse_points(root)
    if any(root != root.expanduser().resolve() or not root.is_dir() for root in roots):
        raise ValueError("semantic runtime roots must be existing canonical directories")
    if index_root == runtime_root:
        raise ValueError("semantic index and runtime roots must be distinct")


def _artifact_from_snapshot(snapshot: ImmutableArtifactSnapshot) -> ArtifactCommitment:
    return ArtifactCommitment(
        path=str(snapshot.path),
        device=snapshot.device,
        inode=snapshot.inode,
        size_bytes=snapshot.size_bytes,
        modified_time_ns=snapshot.modified_time_ns,
        changed_time_ns=snapshot.changed_time_ns,
        file_sha256=snapshot.file_sha256,
    )


def _load_request_artifact(
    request: SemanticQualificationRequest,
    artifact: ArtifactCommitment,
) -> ImmutableArtifactSnapshot:
    snapshot, payload = read_stable_artifact(Path(artifact.path))
    if _artifact_from_snapshot(snapshot) != artifact:
        raise ValueError("semantic qualification request identity changed")
    if SemanticQualificationRequest.model_validate_json(payload) != request:
        raise ValueError("semantic qualification request artifact differs from parsed request")
    return snapshot


def _corpus_coverage(conn: sqlite3.Connection, manifest_id: str) -> _CorpusCoverage:
    row = conn.execute(
        "SELECT seal.expected_document_count,seal.membership_digest_sha256,"
        "seal.completion_status,coverage.expected_document_count,"
        "coverage.included_document_count,coverage.missing_document_count,"
        "coverage.quarantined_document_count,"
        "(SELECT COUNT(*) FROM search_chunks chunk WHERE chunk.manifest_id=seal.manifest_id) "
        "FROM search_corpus_manifest_seals seal "
        "JOIN v_search_corpus_coverage coverage ON coverage.manifest_id=seal.manifest_id "
        "WHERE seal.manifest_id=?",
        (manifest_id,),
    ).fetchone()
    if row is None:
        raise ValueError("semantic qualification corpus seal or coverage is missing")
    sealed_expected = int(row[0])
    included = int(row[4])
    missing = int(row[5])
    quarantined = int(row[6])
    chunks = int(row[7])
    if (
        str(row[2]) != "complete"
        or sealed_expected < 1
        or int(row[3]) != sealed_expected
        or included != sealed_expected
        or missing
        or quarantined
        or chunks < 1
    ):
        raise ValueError("semantic qualification requires a full nonempty corpus")
    return _CorpusCoverage(
        manifest_id=manifest_id,
        membership_sha256=str(row[1]),
        expected_document_count=sealed_expected,
        included_document_count=included,
        chunk_count=chunks,
    )


def _require_issuer_result(
    result: RetrievalRuntimePopulationResult,
    *,
    issuer_id: str,
    expected_issuer_count: int,
) -> RetrievalIssuerPopulationResult:
    if (
        result.mode != "dry_run"
        or result.phase != "qualify"
        or result.expected_issuer_count != expected_issuer_count
        or result.processed_issuer_count != 1
        or result.ready_issuer_count != 1
        or result.failed_issuer_count != 0
        or len(result.issuer_results) != 1
    ):
        raise ValueError("semantic runtime qualifier did not cover the exact issuer cohort")
    item = result.issuer_results[0]
    if (
        item.issuer_id != issuer_id
        or item.outcome != "ready"
        or item.reason_codes
        or item.expected_obligation_count < 1
        or item.expected_document_count < 1
        or item.sealed_inventory_count < 1
        or item.manifest_id is None
        or item.lexical_index_run_id is None
        or item.vector_index_run_id is None
        or item.embedding_promotion_id is None
        or not item.lexical_canaries
        or item.semantic_canary is None
    ):
        raise ValueError("semantic runtime issuer qualification is incomplete")
    return item


def _fact_canary(
    conn: sqlite3.Connection,
    *,
    scope_id: str,
    reporting_entity_id: str,
    fact_generation_id: str,
    limit: int,
) -> GroundedCanaryCommitment:
    row = conn.execute(
        "SELECT canonical_metric_cell_id,canonical_metric_name,period_end,fact_generation_id "
        "FROM latest_governed_fact_entries WHERE scope_key=? "
        "ORDER BY canonical_metric_name,period_end DESC,canonical_metric_cell_id LIMIT 1",
        (scope_id,),
    ).fetchone()
    if row is None or str(row[3]) != fact_generation_id:
        raise ValueError("semantic fact canary lacks an exact latest projection coordinate")
    query = f"{row[1]} {str(row[2])[:4]}"
    started = time.perf_counter()
    hits = search_canonical_facts(
        conn,
        generation_id=fact_generation_id,
        query_text=query,
        limit=limit,
        reporting_entity_id=reporting_entity_id,
    )
    elapsed = max((time.perf_counter() - started) * 1_000, 0.001)
    expected_cell = str(row[0])
    rank = next(
        (
            ordinal
            for ordinal, item in enumerate(hits, start=1)
            if item.canonical_metric_cell_id == expected_cell
        ),
        None,
    )
    if not hits or rank is None:
        raise ValueError("semantic fact canary did not return its exact governed fact")
    payload = [item.model_dump(mode="json") for item in hits]
    return GroundedCanaryCommitment(
        canary_kind="fact",
        query_sha256=digest_text(query),
        result_set_sha256=digest_text(canonical_json(payload)),
        result_count=len(hits),
        elapsed_milliseconds=elapsed,
        expected_source_id=expected_cell,
        observed_rank=rank,
    )


def _issuer_qualification(
    conn: sqlite3.Connection,
    *,
    issuer_result: RetrievalIssuerPopulationResult,
    qualification_elapsed_milliseconds: float,
) -> SemanticIssuerQualification:
    coverage = _corpus_coverage(conn, str(issuer_result.manifest_id))
    if issuer_result.expected_document_count != coverage.expected_document_count:
        raise ValueError("semantic issuer plan and sealed corpus document counts differ")
    embedding = current_promotion(conn)
    if (
        embedding is None
        or embedding.promotion_id != issuer_result.embedding_promotion_id
        or embedding.runtime_registration_id is None
        or embedding.runtime_artifact_sha256 is None
    ):
        raise ValueError("semantic runtime artifact is not attributable to a current promotion")
    projection = load_projection_seal(conn, index_run_id=str(issuer_result.vector_index_run_id))
    if (
        projection is None
        or projection.artifact_set_sha256 is None
        or projection.runtime_artifact_sha256 != embedding.runtime_artifact_sha256
    ):
        raise ValueError("semantic vector projection is not attributable to the runtime artifact")
    semantic = issuer_result.semantic_canary
    assert semantic is not None
    return SemanticIssuerQualification(
        issuer_id=issuer_result.issuer_id,
        corpus_manifest_id=coverage.manifest_id,
        corpus_membership_sha256=coverage.membership_sha256,
        corpus_expected_document_count=coverage.expected_document_count,
        corpus_included_document_count=coverage.included_document_count,
        corpus_chunk_count=coverage.chunk_count,
        lexical_index_run_id=str(issuer_result.lexical_index_run_id),
        vector_index_run_id=str(issuer_result.vector_index_run_id),
        projection_seal_id=projection.projection_seal_id,
        projection_records_sha256=projection.projection_records_sha256,
        projection_artifact_set_sha256=projection.artifact_set_sha256,
        embedding_promotion_id=str(issuer_result.embedding_promotion_id),
        runtime_registration_id=embedding.runtime_registration_id,
        runtime_artifact_sha256=embedding.runtime_artifact_sha256,
        lexical_canaries=tuple(
            GroundedCanaryCommitment(
                canary_kind="lexical",
                query_sha256=item.query_sha256,
                result_set_sha256=item.hit_set_sha256,
                result_count=item.hit_count,
                document_family=item.document_family,
            )
            for item in issuer_result.lexical_canaries
        ),
        semantic_canary=GroundedCanaryCommitment(
            canary_kind="semantic",
            query_sha256=semantic.query_sha256,
            result_set_sha256=semantic.candidate_set_sha256,
            result_count=semantic.candidate_count,
            backend_receipt_sha256=semantic.backend_receipt_sha256,
            expected_source_id=semantic.seed_chunk_id,
            observed_rank=semantic.seed_candidate_rank,
        ),
        qualification_elapsed_milliseconds=max(qualification_elapsed_milliseconds, 0.001),
    )


def _scope_qualification(
    conn: sqlite3.Connection,
    *,
    ready_scope: ReadyRetrievalScope,
    issuer_qualification: SemanticIssuerQualification,
    request: SemanticQualificationRequest,
) -> SemanticScopeQualification:
    scope = ready_scope.scope
    promotion = ready_scope.promotion
    if (
        promotion.scope_id != scope.scope_id
        or promotion.source_scope_key != scope.source_scope_key
        or promotion.source_scope_revision_id != scope.source_scope_revision_id
        or promotion.issuer_id != scope.issuer_id
        or promotion.reporting_entity_id != scope.reporting_entity_id
    ):
        raise ValueError("Ask promotion differs from its composite production scope")
    bundles = tuple(promotion.narrative_bundles)
    if len(bundles) != 1:
        raise ValueError("semantic qualification requires one exact current corpus per scope")
    bundle = bundles[0]
    if (
        bundle.corpus_manifest_id != issuer_qualification.corpus_manifest_id
        or bundle.lexical_index_run_id != issuer_qualification.lexical_index_run_id
        or bundle.vector_index_run_id != issuer_qualification.vector_index_run_id
        or bundle.embedding_promotion_id != issuer_qualification.embedding_promotion_id
    ):
        raise ValueError("Ask promotion and qualified runtime coordinates differ")
    fact_canary = _fact_canary(
        conn,
        scope_id=scope.scope_id,
        reporting_entity_id=scope.reporting_entity_id,
        fact_generation_id=promotion.fact_generation_id,
        limit=request.fact_canary_limit,
    )
    inventory_ids = tuple(promotion.source_inventory_ids)
    if not inventory_ids:
        raise ValueError("semantic Ask promotion has no attributable source inventory")
    return SemanticScopeQualification(
        scope_id=scope.scope_id,
        source_scope_key=scope.source_scope_key,
        source_scope_revision_id=scope.source_scope_revision_id,
        issuer_id=scope.issuer_id,
        reporting_entity_id=scope.reporting_entity_id,
        ask_promotion_id=promotion.promotion_id,
        research_snapshot_id=promotion.research_snapshot_id,
        fact_generation_id=promotion.fact_generation_id,
        fact_projection_seal_sha256=promotion.fact_projection_seal_sha256,
        source_inventory_count=len(inventory_ids),
        source_inventory_set_sha256=digest_text(canonical_json(list(inventory_ids))),
        corpus_manifest_id=issuer_qualification.corpus_manifest_id,
        lexical_index_run_id=issuer_qualification.lexical_index_run_id,
        vector_index_run_id=issuer_qualification.vector_index_run_id,
        embedding_promotion_id=issuer_qualification.embedding_promotion_id,
        fact_canary=fact_canary,
    )


def generate_semantic_qualification_evidence(
    *,
    database_path: Path,
    registry_path: Path,
    cutoff_at: datetime,
    operation_recorded_at: datetime,
    request: SemanticQualificationRequest,
    request_artifact: ArtifactCommitment,
) -> SemanticQualificationEvidence:
    """Generate semantic readiness without mutating the admitted database."""

    database = database_path.expanduser().resolve()
    registry = registry_path.expanduser().resolve()
    for path in (database, registry):
        require_no_reparse_points(path)
    require_checkpointed_sidecars(database)
    _require_current_roots(request.index_root, request.runtime_root)
    request_snapshot = _load_request_artifact(request, request_artifact)
    registry_snapshot, registry_payload = read_stable_artifact(registry)
    registry_artifact = _artifact_from_snapshot(registry_snapshot)
    database_before = DatabaseFileState.from_path(database)
    runtime = LocalVectorRuntimeConfig(
        index_root=request.index_root,
        runtime_root=request.runtime_root,
    )
    conn = connect_sqlite(
        database,
        role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
        schema_preflight=False,
    )
    try:
        conn.row_factory = sqlite3.Row
        scopes = load_production_scopes(
            conn,
            registry,
            registry_payload=registry_payload,
        )
        if not scopes or tuple(sorted(scopes, key=lambda item: item.scope_id)) != scopes:
            raise ValueError("semantic qualification requires the exact composite scope registry")
        readiness = assess_retrieval_readiness(conn, scopes, runtime=runtime)
        if readiness.outcome != "ready" or len(readiness.scopes) != len(scopes):
            raise ValueError("semantic Ask promotions are not ready for the production registry")
        ready_by_scope = {item.scope.scope_id: item for item in readiness.scopes}
        if tuple(sorted(ready_by_scope)) != tuple(item.scope_id for item in scopes):
            raise ValueError("semantic Ask readiness differs from the production registry")
        issuer_ids = tuple(sorted({item.issuer_id for item in scopes}))
        issuer_results: dict[str, tuple[RetrievalIssuerPopulationResult, float]] = {}
        after_issuer_id: str | None = None
        for issuer_id in issuer_ids:
            started = time.perf_counter()
            result = populate_retrieval_runtime(
                conn,
                RetrievalRuntimePopulationRequest(
                    cutoff_at=cutoff_at,
                    operation_recorded_at=operation_recorded_at,
                    apply=False,
                    phase="qualify",
                    after_issuer_id=after_issuer_id,
                    max_issuers=1,
                    exact_row_cap=request.exact_row_cap,
                ),
                runtime=runtime,
            )
            elapsed = max((time.perf_counter() - started) * 1_000, 0.001)
            issuer_results[issuer_id] = (
                _require_issuer_result(
                    result,
                    issuer_id=issuer_id,
                    expected_issuer_count=len(issuer_ids),
                ),
                elapsed,
            )
            after_issuer_id = issuer_id
        issuer_qualifications = tuple(
            _issuer_qualification(
                conn,
                issuer_result=issuer_results[issuer_id][0],
                qualification_elapsed_milliseconds=issuer_results[issuer_id][1],
            )
            for issuer_id in issuer_ids
        )
        issuer_qualification_by_id = {item.issuer_id: item for item in issuer_qualifications}
        qualifications = tuple(
            _scope_qualification(
                conn,
                ready_scope=ready_by_scope[scope.scope_id],
                issuer_qualification=issuer_qualification_by_id[scope.issuer_id],
                request=request,
            )
            for scope in scopes
        )
    finally:
        conn.close()
    database_after = DatabaseFileState.from_path(database)
    if database_after != database_before:
        raise ValueError("semantic qualification changed the admitted database")
    assert_artifact_unchanged(registry_snapshot)
    if not registry_artifact.verify():
        raise ValueError("semantic production registry changed during qualification")
    assert_artifact_unchanged(request_snapshot)
    if not request_artifact.verify():
        raise ValueError("semantic qualification request changed during qualification")
    _require_current_roots(request.index_root, request.runtime_root)
    optional_fact_times = tuple(item.fact_canary.elapsed_milliseconds for item in qualifications)
    if any(item is None for item in optional_fact_times):
        raise ValueError("semantic fact canary latency is missing")
    fact_max = max(cast(float, item) for item in optional_fact_times)
    issuer_max = max(item.qualification_elapsed_milliseconds for item in issuer_qualifications)
    manifests = {
        item.corpus_manifest_id: item.corpus_expected_document_count
        for item in issuer_qualifications
    }
    return build_semantic_qualification_evidence(
        database_sha256=database_after.file_sha256,
        request_artifact=request_artifact,
        registry_artifact=registry_artifact,
        production_scope_ids=tuple(item.scope_id for item in qualifications),
        issuer_qualifications=issuer_qualifications,
        scope_qualifications=qualifications,
        corpus_document_count=sum(manifests.values()),
        grounded_fact_canary_count=len(qualifications),
        grounded_narrative_canary_count=sum(
            len(item.lexical_canaries) + 1 for item in issuer_qualifications
        ),
        failure_count=0,
        max_fact_canary_milliseconds=request.max_fact_canary_milliseconds,
        max_issuer_qualification_milliseconds=request.max_issuer_qualification_milliseconds,
        observed_max_fact_canary_milliseconds=fact_max,
        observed_max_issuer_qualification_milliseconds=issuer_max,
    )


def _stable_semantic_payload(evidence: SemanticQualificationEvidence) -> dict[str, object]:
    payload = evidence.model_dump(mode="json")
    payload.pop("qualification_sha256")
    payload.pop("observed_max_fact_canary_milliseconds")
    payload.pop("observed_max_issuer_qualification_milliseconds")
    scopes = payload["scope_qualifications"]
    issuers = payload["issuer_qualifications"]
    if not isinstance(scopes, list) or not isinstance(issuers, list):
        raise ValueError("semantic qualification scope payload is invalid")
    for raw_scope_value in cast(list[object], scopes):
        if not isinstance(raw_scope_value, dict):
            raise ValueError("semantic qualification scope payload is invalid")
        raw_scope = cast(dict[str, object], raw_scope_value)
        fact = raw_scope.get("fact_canary")
        if not isinstance(fact, dict):
            raise ValueError("semantic qualification canary payload is invalid")
        fact.pop("elapsed_milliseconds")
    for raw_issuer_value in cast(list[object], issuers):
        if not isinstance(raw_issuer_value, dict):
            raise ValueError("semantic qualification issuer payload is invalid")
        raw_issuer = cast(dict[str, object], raw_issuer_value)
        raw_issuer.pop("qualification_elapsed_milliseconds")
    return payload


def verify_semantic_qualification_current(
    *,
    database_path: Path,
    registry_path: Path,
    cutoff_at: datetime,
    operation_recorded_at: datetime,
    request: SemanticQualificationRequest,
    evidence: SemanticQualificationEvidence,
) -> None:
    """Re-run current semantic admission before composite readiness publication."""

    fresh = generate_semantic_qualification_evidence(
        database_path=database_path,
        registry_path=registry_path,
        cutoff_at=cutoff_at,
        operation_recorded_at=operation_recorded_at,
        request=request,
        request_artifact=evidence.request_artifact,
    )
    if _stable_semantic_payload(fresh) != _stable_semantic_payload(evidence):
        raise ValueError("semantic qualification inputs or grounded results changed")


__all__ = [
    "SemanticQualificationRequest",
    "generate_semantic_qualification_evidence",
    "verify_semantic_qualification_current",
]
