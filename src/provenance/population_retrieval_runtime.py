"""Populate and qualify the governed investor-reporting retrieval plane.

The lexical corpus is derived only from active source obligations whose
expected documents have exact obligation bindings and complete sealed source
inventories.  Semantic readiness is a separate, stricter gate: an evaluated
current embedding promotion, a complete sealed vector projection, a matching
local runtime artifact, and recomputable provenance canaries must all pass.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

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
    stream_population_artifact_set,
)
from provenance.search_index_lineage import (
    load_projection_seal,
    verify_ledger_projection_seal,
)
from search.corpus_builder import (
    ChunkerConfig,
    CorpusBuildRequest,
    ExpectedDocument,
    build_grounded_search_corpus,
)
from search.embedding_promotion import PURPOSE, LocalVectorRuntimeConfig
from search.exact_semantic import MAX_EXACT_VECTOR_ROWS, ExactSemanticRuntime
from search.grounded import HybridRetriever, SearchFilter

_POLICY_VERSION = "investor-reporting-retrieval-population.v1"
_SELECTOR_VERSION = "governed-investor-reporting-selector@1"
_RETRIEVAL_SELECTION_POLICY = "retrieval-runtime-terminal-at-k-observed-through-o.v1"
_REPORTING_FAMILIES = (
    "annual_securities_report",
    "continuous_disclosure",
    "investment_company_periodic",
    "issuer_earnings_materials",
    "issuer_financial_statements",
    "issuer_presentations",
    "operating_company_periodic",
)
_POSITIVE_COVERAGE = frozenset({"captured", "extracted", "indexed"})
_QUARANTINED_COVERAGE = frozenset({"quarantined", "unsupported"})


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalRuntimePopulationRequest(_FrozenModel):
    cutoff_at: datetime
    operation_recorded_at: datetime = Field(
        validation_alias=AliasChoices("operation_recorded_at", "recorded_at")
    )
    apply: bool = False
    phase: Literal["corpus", "qualify", "all"] = "all"
    after_issuer_id: str | None = None
    max_issuers: int | None = Field(default=None, ge=1)
    selector_code_version: str = Field(
        default=_SELECTOR_VERSION,
        min_length=1,
        max_length=255,
    )
    required_extractor_names: tuple[str, ...] = (
        "fulltext-evidence-backfill",
        "governed-pdf-ocr",
        "governed-image-ocr",
    )
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    exact_row_cap: int = Field(default=MAX_EXACT_VECTOR_ROWS, ge=1, le=MAX_EXACT_VECTOR_ROWS)
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
    def _exact_cutoff(self) -> Self:
        if _utc(self.operation_recorded_at) < _utc(self.cutoff_at):
            raise ValueError("operation_recorded_at must not precede cutoff_at")
        if not self.required_extractor_names:
            raise ValueError("at least one governed extractor is required")
        if tuple(sorted(set(self.required_extractor_names))) != tuple(
            sorted(self.required_extractor_names)
        ):
            raise ValueError("required extractor names must be unique")
        if (self.input_commitment_sha256 is None) != (self.plan_commitment_sha256 is None):
            raise ValueError("population commitments must be supplied together")
        if self.apply and self.after_issuer_id is not None and self.input_commitment_sha256 is None:
            raise ValueError("a resumed apply requires population commitments")
        return self


class RetrievalCanaryReceipt(_FrozenModel):
    document_family: str = Field(min_length=1, max_length=64)
    query_sha256: str = Field(min_length=64, max_length=64)
    hit_set_sha256: str = Field(min_length=64, max_length=64)
    hit_count: int = Field(gt=0)


class SemanticCanaryReceipt(_FrozenModel):
    query_sha256: str = Field(min_length=64, max_length=64)
    candidate_set_sha256: str = Field(min_length=64, max_length=64)
    backend_receipt_sha256: str = Field(min_length=64, max_length=64)
    candidate_count: int = Field(gt=0)


class RetrievalIssuerPopulationResult(_FrozenModel):
    issuer_id: str
    expected_obligation_count: int = Field(ge=0)
    expected_document_count: int = Field(ge=0)
    sealed_inventory_count: int = Field(ge=0)
    manifest_id: str | None = None
    lexical_index_run_id: str | None = None
    vector_index_run_id: str | None = None
    embedding_promotion_id: str | None = None
    lexical_canaries: tuple[RetrievalCanaryReceipt, ...] = ()
    semantic_canary: SemanticCanaryReceipt | None = None
    outcome: Literal["ready", "blocked"]
    reason_codes: tuple[str, ...]
    input_commitment_sha256: str
    output_commitment_sha256: str

    @model_validator(mode="after")
    def _outcome_contract(self) -> Self:
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("retrieval reason codes must be sorted and unique")
        fully_qualified = (
            self.manifest_id is not None
            and self.lexical_index_run_id is not None
            and self.vector_index_run_id is not None
            and self.embedding_promotion_id is not None
            and bool(self.lexical_canaries)
            and self.semantic_canary is not None
        )
        if self.outcome == "ready" and (self.reason_codes or not fully_qualified):
            raise ValueError("ready retrieval requires every lexical and semantic coordinate")
        if self.outcome == "blocked" and not self.reason_codes:
            raise ValueError("blocked retrieval requires an explicit reason")
        return self


class RetrievalRuntimePopulationResult(_FrozenModel):
    mode: Literal["dry_run", "apply"]
    phase: str
    expected_issuer_count: int = Field(gt=0)
    processed_issuer_count: int = Field(ge=0)
    ready_issuer_count: int = Field(ge=0)
    failed_issuer_count: int = Field(ge=0)
    lexical_manifest_count: int = Field(ge=0)
    lexical_canary_count: int = Field(ge=0)
    vector_projection_count: int = Field(ge=0)
    semantic_canary_count: int = Field(ge=0)
    failed_reason_counts: dict[str, int]
    last_issuer_id: str | None
    input_commitment_sha256: str
    plan_commitment_sha256: str
    output_commitment_sha256: str
    issuer_results: tuple[RetrievalIssuerPopulationResult, ...]


class _Obligation(_FrozenModel):
    obligation_revision_id: str
    issuer_id: str
    reporting_entity_id: str
    document_family: str


class _ScopePlan(_FrozenModel):
    issuer_id: str
    obligations: tuple[_Obligation, ...]
    expected_documents: tuple[ExpectedDocument, ...]
    source_inventory_snapshot_ids: tuple[str, ...]
    source_inventory_commitments: dict[str, str]
    family_document_ids: dict[str, tuple[str, ...]]
    source_scope_sha256: str
    reason_codes: tuple[str, ...]

    @property
    def input_commitment_sha256(self) -> str:
        return _digest_json(self.model_dump(mode="json"))


def verify_retrieval_runtime(
    conn: sqlite3.Connection,
    scope: PopulationTemporalScope,
    *,
    runtime: LocalVectorRuntimeConfig | None = None,
) -> PopulationPlaneVerification:
    """Verify persisted K-scoped retrieval artifacts and the O-scoped promotion."""

    knowledge, observed = _utc(scope.knowledge_cutoff), _utc(scope.observed_through)
    expected = len({item.issuer_id for item in _active_obligations(conn, knowledge, observed)})
    manifests = stream_population_artifact_set(
        conn,
        table="search_corpus_manifests",
        query=(
            "SELECT manifest.manifest_id AS artifact_id,"
            "manifest.selection_config_sha256 AS payload_sha256,"
            "seal.membership_digest_sha256 AS seal_sha256,"
            "manifest.knowledge_cutoff AS knowledge_at,"
            "seal.sealed_at AS recorded_at "
            "FROM search_corpus_manifests manifest "
            "JOIN search_corpus_manifest_seals seal ON seal.manifest_id=manifest.manifest_id "
            "WHERE datetime(manifest.knowledge_cutoff)=datetime(?) "
            "AND datetime(manifest.recorded_at)<=datetime(?) "
            "AND datetime(seal.sealed_at)<=datetime(?) "
            "AND seal.completion_status='complete' "
            "AND NOT EXISTS (SELECT 1 FROM search_corpus_manifests newer "
            "WHERE newer.corpus_key=manifest.corpus_key AND newer.revision>manifest.revision "
            "AND datetime(newer.knowledge_cutoff)=datetime(?) "
            "AND datetime(newer.recorded_at)<=datetime(?)) "
            "ORDER BY manifest.corpus_key,manifest.manifest_id"
        ),
        params=(
            _db_time(knowledge),
            _db_time(observed),
            _db_time(observed),
            _db_time(knowledge),
            _db_time(observed),
        ),
        selection_policy_id=_RETRIEVAL_SELECTION_POLICY + ".corpus",
    )
    lexical = _retrieval_projection_artifact_set(
        conn, knowledge=knowledge, observed=observed, index_kind="lexical"
    )
    vector = _retrieval_projection_artifact_set(
        conn, knowledge=knowledge, observed=observed, index_kind="vector"
    )
    promotions = stream_population_artifact_set(
        conn,
        table="search_embedding_model_promotions",
        query=(
            "SELECT promotion.promotion_id AS artifact_id,"
            "promotion.evaluation_artifact_sha256 AS payload_sha256,"
            "promotion.runtime_artifact_sha256 AS seal_sha256,"
            "promotion.knowledge_at AS knowledge_at,"
            "promotion.recorded_at AS recorded_at "
            "FROM search_embedding_model_promotions promotion "
            "WHERE promotion.purpose=? "
            "AND datetime(promotion.approved_at)<=datetime(?) "
            "AND datetime(promotion.knowledge_at)<=datetime(?) "
            "AND datetime(promotion.recorded_at)<=datetime(?) "
            "AND EXISTS (SELECT 1 FROM search_projection_seals projection "
            "JOIN search_corpus_manifests manifest ON manifest.manifest_id=projection.manifest_id "
            "WHERE projection.index_kind='vector' "
            "AND projection.provider=promotion.provider "
            "AND projection.model=promotion.model "
            "AND projection.dimensions=promotion.dimensions "
            "AND projection.runtime_artifact_sha256=promotion.runtime_artifact_sha256 "
            "AND datetime(manifest.knowledge_cutoff)=datetime(?) "
            "AND datetime(projection.sealed_at)<=datetime(?)) "
            "AND NOT EXISTS (SELECT 1 FROM search_embedding_model_promotions newer "
            "WHERE newer.purpose=promotion.purpose AND newer.revision>promotion.revision "
            "AND datetime(newer.approved_at)<=datetime(?) "
            "AND datetime(newer.knowledge_at)<=datetime(?) "
            "AND datetime(newer.recorded_at)<=datetime(?)) "
            "ORDER BY promotion.purpose,promotion.promotion_id"
        ),
        params=(
            PURPOSE,
            _db_time(observed),
            _db_time(observed),
            _db_time(observed),
            _db_time(knowledge),
            _db_time(observed),
            _db_time(observed),
            _db_time(observed),
            _db_time(observed),
        ),
        selection_policy_id=_RETRIEVAL_SELECTION_POLICY + ".promotion-control",
    )
    artifacts = tuple(
        sorted(
            (manifests, lexical, vector, promotions),
            key=lambda item: (item.table, item.selection_policy_id),
        )
    )
    return _retrieval_plane_verification(
        scope=scope,
        expected=expected,
        artifacts=artifacts,
        runtime=runtime,
    )


def _retrieval_projection_artifact_set(
    conn: sqlite3.Connection,
    *,
    knowledge: datetime,
    observed: datetime,
    index_kind: Literal["lexical", "vector"],
) -> PopulationArtifactSetCommitment:
    payload_column = (
        "projection.config_sha256" if index_kind == "lexical" else "projection.artifact_set_sha256"
    )
    seal_column = (
        "projection.projection_records_sha256"
        if index_kind == "lexical"
        else "projection.runtime_artifact_sha256"
    )
    return stream_population_artifact_set(
        conn,
        table="search_projection_seals",
        query=(
            "SELECT projection.projection_seal_id AS artifact_id,"  # nosec B608 -- columns come from a closed Literal mapping; values are bound
            f"{payload_column} AS payload_sha256,"
            f"{seal_column} AS seal_sha256,"
            "manifest.knowledge_cutoff AS knowledge_at,"
            "projection.sealed_at AS recorded_at "
            "FROM search_projection_seals projection "
            "JOIN search_corpus_manifests manifest "
            "ON manifest.manifest_id=projection.manifest_id "
            "JOIN search_corpus_manifest_seals corpus_seal "
            "ON corpus_seal.manifest_id=manifest.manifest_id "
            "WHERE projection.index_kind=? "
            "AND datetime(manifest.knowledge_cutoff)=datetime(?) "
            "AND datetime(manifest.recorded_at)<=datetime(?) "
            "AND datetime(corpus_seal.sealed_at)<=datetime(?) "
            "AND corpus_seal.completion_status='complete' "
            "AND datetime(projection.sealed_at)<=datetime(?) "
            f"AND {payload_column} IS NOT NULL "
            f"AND {seal_column} IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM search_corpus_manifests newer "
            "WHERE newer.corpus_key=manifest.corpus_key "
            "AND newer.revision>manifest.revision "
            "AND datetime(newer.knowledge_cutoff)=datetime(?) "
            "AND datetime(newer.recorded_at)<=datetime(?)) "
            "ORDER BY manifest.corpus_key,projection.projection_seal_id"
        ),
        params=(
            index_kind,
            _db_time(knowledge),
            _db_time(observed),
            _db_time(observed),
            _db_time(observed),
            _db_time(knowledge),
            _db_time(observed),
        ),
        selection_policy_id=f"{_RETRIEVAL_SELECTION_POLICY}.{index_kind}-projection",
    )


def _retrieval_plane_verification(
    *,
    scope: PopulationTemporalScope,
    expected: int,
    artifacts: tuple[PopulationArtifactSetCommitment, ...],
    runtime: LocalVectorRuntimeConfig | None,
) -> PopulationPlaneVerification:
    if expected < 1:
        raise ValueError("retrieval verification requires an eligible issuer")
    by_policy = {item.selection_policy_id: item.row_count for item in artifacts}
    required_counts = (
        by_policy.get(_RETRIEVAL_SELECTION_POLICY + ".corpus", 0),
        by_policy.get(_RETRIEVAL_SELECTION_POLICY + ".lexical-projection", 0),
        by_policy.get(_RETRIEVAL_SELECTION_POLICY + ".vector-projection", 0),
    )
    promotion_count = by_policy.get(
        _RETRIEVAL_SELECTION_POLICY + ".promotion-control",
        0,
    )
    exact = all(count == expected for count in required_counts) and promotion_count == 1
    materialized = expected if exact else 0
    failed = expected - materialized
    details: dict[str, JsonValue] = {
        "knowledge_cutoff": _db_time(scope.knowledge_cutoff),
        "observed_through": _db_time(scope.observed_through),
        "promotion_count": promotion_count,
        "runtime_supplied": runtime is not None,
        "selection_policy_id": _RETRIEVAL_SELECTION_POLICY,
    }
    output_material = {
        "artifact_sets": [item.model_dump(mode="json") for item in artifacts],
        "details": details,
        "exclusion_counts": {},
        "expected_count": expected,
        "failed_count": failed,
        "materialized_count": materialized,
        "plane_name": "retrieval_runtime",
    }
    return PopulationPlaneVerification(
        plane_name="retrieval_runtime",
        expected_count=expected,
        materialized_count=materialized,
        excluded_count=0,
        failed_count=failed,
        exclusion_counts={},
        input_commitment_sha256=_digest_json(
            {
                "knowledge_cutoff": _utc(scope.knowledge_cutoff),
                "observed_through": _utc(scope.observed_through),
                "selection_policy_id": _RETRIEVAL_SELECTION_POLICY,
            }
        ),
        output_commitment_sha256=_digest_json(output_material),
        artifact_sets=artifacts,
        details=details,
    )


def populate_retrieval_runtime(
    conn: sqlite3.Connection,
    request: RetrievalRuntimePopulationRequest,
    *,
    runtime: LocalVectorRuntimeConfig | None = None,
) -> RetrievalRuntimePopulationResult:
    """Build bounded issuer corpora and prove exact lexical/semantic readiness."""

    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        obligations = _active_obligations(
            conn,
            request.cutoff_at,
            request.operation_recorded_at,
        )
        if not obligations:
            raise ValueError("retrieval population requires active reporting obligations")
        grouped: dict[str, list[_Obligation]] = {}
        for obligation in obligations:
            grouped.setdefault(obligation.issuer_id, []).append(obligation)
        all_issuer_ids = tuple(sorted(grouped))
        selected_ids = tuple(
            issuer_id
            for issuer_id in all_issuer_ids
            if request.after_issuer_id is None or issuer_id > request.after_issuer_id
        )
        if request.max_issuers is not None:
            selected_ids = selected_ids[: request.max_issuers]
        plans = tuple(
            _scope_plan(
                conn,
                issuer_id,
                tuple(sorted(grouped[issuer_id], key=lambda item: item.obligation_revision_id)),
                request.cutoff_at,
                request.operation_recorded_at,
            )
            for issuer_id in selected_ids
        )
        input_sha = _digest_json(
            {
                "cutoff_at": _utc(request.cutoff_at),
                "policy_version": _POLICY_VERSION,
                "scopes": [plan.model_dump(mode="json") for plan in plans],
            }
        )
        plan_sha = _population_plan_commitment(
            request,
            input_sha=input_sha,
            selected_ids=selected_ids,
            runtime=runtime,
        )
        _verify_commitments(request, input_sha=input_sha, plan_sha=plan_sha)
        result_items: list[RetrievalIssuerPopulationResult] = []
        last_successful_issuer_id = request.after_issuer_id
        for plan in plans:
            result = _populate_issuer(conn, request, plan, runtime=runtime)
            result_items.append(result)
            if not request.apply:
                last_successful_issuer_id = result.issuer_id
                continue
            if not _phase_completed(request.phase, result):
                break
            last_successful_issuer_id = result.issuer_id
        results = tuple(result_items)
        reason_counts: dict[str, int] = {}
        for result in results:
            for reason in result.reason_codes:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        output_sha = _digest_json(
            {
                "cutoff_at": _utc(request.cutoff_at),
                "policy_version": _POLICY_VERSION,
                "issuer_results": [item.model_dump(mode="json") for item in results],
            }
        )
        ready = sum(item.outcome == "ready" for item in results)
        return RetrievalRuntimePopulationResult(
            mode="apply" if request.apply else "dry_run",
            phase=request.phase,
            expected_issuer_count=len(all_issuer_ids),
            processed_issuer_count=len(results),
            ready_issuer_count=ready,
            failed_issuer_count=len(results) - ready,
            lexical_manifest_count=sum(item.manifest_id is not None for item in results),
            lexical_canary_count=sum(len(item.lexical_canaries) for item in results),
            vector_projection_count=sum(item.vector_index_run_id is not None for item in results),
            semantic_canary_count=sum(item.semantic_canary is not None for item in results),
            failed_reason_counts=dict(sorted(reason_counts.items())),
            last_issuer_id=last_successful_issuer_id,
            input_commitment_sha256=input_sha,
            plan_commitment_sha256=plan_sha,
            output_commitment_sha256=output_sha,
            issuer_results=results,
        )
    finally:
        conn.row_factory = original_row_factory


def _populate_issuer(
    conn: sqlite3.Connection,
    request: RetrievalRuntimePopulationRequest,
    plan: _ScopePlan,
    *,
    runtime: LocalVectorRuntimeConfig | None,
) -> RetrievalIssuerPopulationResult:
    reasons = list(plan.reason_codes)
    manifest_id: str | None = None
    lexical_index_run_id: str | None = None
    vector_index_run_id: str | None = None
    promotion_id: str | None = None
    lexical_canaries: tuple[RetrievalCanaryReceipt, ...] = ()
    semantic_canary: SemanticCanaryReceipt | None = None
    if not reasons and request.phase in {"corpus", "all"}:
        try:
            corpus = build_grounded_search_corpus(
                conn,
                CorpusBuildRequest(
                    corpus_key=_corpus_key(plan.issuer_id),
                    revision=_corpus_revision(conn, plan.issuer_id, request.cutoff_at),
                    selector_code_version=request.selector_code_version,
                    recorded_at=request.operation_recorded_at,
                    knowledge_cutoff=request.cutoff_at,
                    expected_documents=plan.expected_documents,
                    source_inventory_snapshot_ids=plan.source_inventory_snapshot_ids,
                    chunker=request.chunker,
                    required_extractor_names=request.required_extractor_names,
                    apply=request.apply,
                ),
            )
            if corpus.completion_status != "complete":
                reasons.extend(
                    _incomplete_manifest_reasons(conn, corpus.manifest_id)
                    if request.apply
                    else ("lexical_corpus_incomplete",)
                )
            elif not request.apply:
                reasons.append("lexical_corpus_not_materialized")
            else:
                manifest_id = corpus.manifest_id
                lexical_index_run_id = corpus.lexical_index_run_id
        except (RuntimeError, ValueError, sqlite3.Error):
            reasons.append("lexical_corpus_population_failed")
    elif not reasons:
        manifest = _manifest_at_cutoff(
            conn,
            plan.issuer_id,
            request.cutoff_at,
            request.operation_recorded_at,
        )
        if manifest is None:
            reasons.append("lexical_corpus_missing")
        else:
            manifest_id, lexical_index_run_id = manifest
    if (
        not reasons
        and manifest_id is not None
        and lexical_index_run_id is not None
        and request.phase in {"qualify", "all"}
    ):
        try:
            lexical_canaries = _verify_lexical_canaries(
                conn,
                manifest_id=manifest_id,
                issuer_id=plan.issuer_id,
                family_document_ids=plan.family_document_ids,
                cutoff_at=request.cutoff_at,
            )
        except (RuntimeError, ValueError, sqlite3.Error):
            reasons.append("grounded_lexical_canary_failed")
        if not lexical_canaries:
            reasons.append("grounded_lexical_canary_failed")
        if not reasons:
            promotion = _promotion_at_cutoff(
                conn,
                request.cutoff_at,
                request.operation_recorded_at,
            )
            if promotion is None:
                reasons.append("embedding_model_not_promoted")
            else:
                promotion_id, provider, model, dimensions, runtime_artifact_sha = promotion
                vector_index_run_id = _vector_projection_at_cutoff(
                    conn,
                    manifest_id=manifest_id,
                    provider=provider,
                    model=model,
                    dimensions=dimensions,
                    runtime_artifact_sha256=runtime_artifact_sha,
                    cutoff_at=request.cutoff_at,
                    observed_through=request.operation_recorded_at,
                )
                if vector_index_run_id is None:
                    reasons.append("semantic_projection_unsealed")
                elif runtime is None:
                    reasons.append("semantic_runtime_unavailable")
                else:
                    try:
                        _require_projection_within_root(
                            conn,
                            vector_index_run_id=vector_index_run_id,
                            index_root=runtime.index_root,
                        )
                        semantic_canary = _verify_semantic_canary(
                            conn,
                            manifest_id=manifest_id,
                            vector_index_run_id=vector_index_run_id,
                            embedding_promotion_id=promotion_id,
                            runtime_root=runtime.runtime_root,
                            exact_row_cap=request.exact_row_cap,
                        )
                    except (RuntimeError, ValueError, OSError, sqlite3.Error):
                        reasons.append("grounded_semantic_canary_failed")
    elif not reasons:
        reasons.append("retrieval_qualification_not_run")
    reasons_tuple = tuple(sorted(set(reasons)))
    output_payload = {
        "embedding_promotion_id": promotion_id,
        "issuer_id": plan.issuer_id,
        "lexical_canaries": [item.model_dump(mode="json") for item in lexical_canaries],
        "lexical_index_run_id": lexical_index_run_id,
        "manifest_id": manifest_id,
        "reason_codes": reasons_tuple,
        "semantic_canary": (
            None if semantic_canary is None else semantic_canary.model_dump(mode="json")
        ),
        "vector_index_run_id": vector_index_run_id,
    }
    return RetrievalIssuerPopulationResult(
        issuer_id=plan.issuer_id,
        expected_obligation_count=len(plan.obligations),
        expected_document_count=len(plan.expected_documents),
        sealed_inventory_count=len(plan.source_inventory_snapshot_ids),
        manifest_id=manifest_id,
        lexical_index_run_id=lexical_index_run_id,
        vector_index_run_id=vector_index_run_id,
        embedding_promotion_id=promotion_id,
        lexical_canaries=lexical_canaries,
        semantic_canary=semantic_canary,
        outcome="blocked" if reasons_tuple else "ready",
        reason_codes=reasons_tuple,
        input_commitment_sha256=plan.input_commitment_sha256,
        output_commitment_sha256=_digest_json(output_payload),
    )


def _phase_completed(
    phase: Literal["corpus", "qualify", "all"],
    result: RetrievalIssuerPopulationResult,
) -> bool:
    if phase == "corpus":
        return set(result.reason_codes) <= {"retrieval_qualification_not_run"}
    return result.outcome == "ready"


def _population_plan_commitment(
    request: RetrievalRuntimePopulationRequest,
    *,
    input_sha: str,
    selected_ids: tuple[str, ...],
    runtime: LocalVectorRuntimeConfig | None,
) -> str:
    return _digest_json(
        {
            "after_issuer_id": request.after_issuer_id,
            "chunker": request.chunker.model_dump(mode="json"),
            "cutoff_at": _utc(request.cutoff_at),
            "exact_row_cap": request.exact_row_cap,
            "input_commitment_sha256": input_sha,
            "max_issuers": request.max_issuers,
            "phase": request.phase,
            "operation_recorded_at": _utc(request.operation_recorded_at),
            "required_extractor_names": list(request.required_extractor_names),
            "runtime": (
                None
                if runtime is None
                else {
                    "index_root": str(runtime.index_root.resolve()),
                    "runtime_root": str(runtime.runtime_root.resolve()),
                }
            ),
            "selected_issuer_ids": list(selected_ids),
            "selector_code_version": request.selector_code_version,
        }
    )


def _verify_commitments(
    request: RetrievalRuntimePopulationRequest,
    *,
    input_sha: str,
    plan_sha: str,
) -> None:
    if request.input_commitment_sha256 is not None and request.input_commitment_sha256 != input_sha:
        raise ValueError("retrieval runtime input commitment changed")
    if request.plan_commitment_sha256 is not None and request.plan_commitment_sha256 != plan_sha:
        raise ValueError("retrieval runtime plan commitment changed")


def _active_obligations(
    conn: sqlite3.Connection,
    cutoff_at: datetime,
    observed_through: datetime,
) -> tuple[_Obligation, ...]:
    placeholders = ",".join("?" for _ in _REPORTING_FAMILIES)
    rows = conn.execute(
        "SELECT obligation.obligation_revision_id,obligation.issuer_id,"
        "obligation.reporting_entity_id,obligation.document_family "
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
        "ORDER BY obligation.issuer_id,obligation.obligation_revision_id",
        (
            *_REPORTING_FAMILIES,
            _db_time(cutoff_at),
            _db_time(cutoff_at),
            _db_time(cutoff_at),
            _db_time(observed_through),
            _db_time(cutoff_at),
            _db_time(observed_through),
        ),
    ).fetchall()
    return tuple(
        _Obligation(
            obligation_revision_id=str(row[0]),
            issuer_id=str(row[1]),
            reporting_entity_id=str(row[2]),
            document_family=str(row[3]),
        )
        for row in rows
    )


def _scope_plan(
    conn: sqlite3.Connection,
    issuer_id: str,
    obligations: tuple[_Obligation, ...],
    cutoff_at: datetime,
    observed_through: datetime,
) -> _ScopePlan:
    expected: list[ExpectedDocument] = []
    inventory_ids: set[str] = set()
    inventory_commitments: dict[str, str] = {}
    family_documents: dict[str, set[str]] = {}
    commitment_rows: list[dict[str, object]] = []
    reasons: set[str] = set()
    for obligation in obligations:
        rows = conn.execute(
            "SELECT binding.expected_document_id,binding.document_family,"
            "expected.expected_document_key,expected.snapshot_id,"
            "assessment.coverage_status,assessment.document_version_id,"
            "assessment.reason_code,assessment.assessment_id "
            "FROM expected_document_obligation_bindings binding "
            "JOIN expected_documents expected "
            "ON expected.expected_document_id=binding.expected_document_id "
            "LEFT JOIN source_coverage_assessments assessment "
            "ON assessment.expected_document_id=expected.expected_document_id "
            "AND datetime(assessment.knowledge_at)<=datetime(?) "
            "AND datetime(assessment.recorded_at)<=datetime(?) "
            "AND NOT EXISTS (SELECT 1 FROM source_coverage_assessments newer "
            "WHERE newer.expected_document_id=assessment.expected_document_id "
            "AND newer.revision>assessment.revision "
            "AND datetime(newer.knowledge_at)<=datetime(?) "
            "AND datetime(newer.recorded_at)<=datetime(?)) "
            "WHERE binding.source_obligation_revision_id=? "
            "AND binding.issuer_id=? "
            "AND binding.reporting_entity_id=? "
            "AND binding.document_family=? "
            "AND datetime(binding.effective_at)<=datetime(?) "
            "AND datetime(binding.knowledge_at)<=datetime(?) "
            "AND datetime(binding.recorded_at)<=datetime(?) "
            "AND datetime(expected.recorded_at)<=datetime(?) "
            "ORDER BY expected.expected_document_key",
            (
                _db_time(cutoff_at),
                _db_time(observed_through),
                _db_time(cutoff_at),
                _db_time(observed_through),
                obligation.obligation_revision_id,
                issuer_id,
                obligation.reporting_entity_id,
                obligation.document_family,
                _db_time(cutoff_at),
                _db_time(cutoff_at),
                _db_time(observed_through),
                _db_time(observed_through),
            ),
        ).fetchall()
        if not rows:
            reasons.add("expected_document_binding_missing")
            continue
        for row in rows:
            snapshot_id = str(row[3])
            inventory_ids.add(snapshot_id)
            status = None if row[4] is None else str(row[4])
            document_version_id = None if row[5] is None else str(row[5])
            if status in _POSITIVE_COVERAGE and document_version_id is not None:
                membership_status: Literal["included", "missing", "quarantined"] = "included"
                family_documents.setdefault(obligation.document_family, set()).add(
                    document_version_id
                )
            elif status in _QUARANTINED_COVERAGE:
                membership_status = "quarantined"
                document_version_id = None
            else:
                membership_status = "missing"
                document_version_id = None
            expected.append(
                ExpectedDocument(
                    expected_document_key=str(row[2]),
                    document_version_id=document_version_id,
                    membership_status=membership_status,
                    reason=f"coverage:{status or 'absent'}:{row[6] or 'assessment_missing'}",
                )
            )
            commitment_rows.append(
                {
                    "assessment_id": None if row[7] is None else str(row[7]),
                    "coverage_status": status,
                    "document_family": str(row[1]),
                    "document_version_id": document_version_id,
                    "expected_document_id": str(row[0]),
                    "expected_document_key": str(row[2]),
                    "obligation_revision_id": obligation.obligation_revision_id,
                    "snapshot_id": snapshot_id,
                }
            )
    keys = [item.expected_document_key for item in expected]
    if len(keys) != len(set(keys)):
        reasons.add("expected_document_identity_conflict")
    for snapshot_id in sorted(inventory_ids):
        inventory_commitment = _inventory_commitment(
            conn,
            snapshot_id,
            observed_through,
        )
        if inventory_commitment is None:
            reasons.add("source_inventory_incomplete")
        else:
            inventory_commitments[snapshot_id] = inventory_commitment
    if not expected and not reasons:
        reasons.add("expected_document_binding_missing")
    return _ScopePlan(
        issuer_id=issuer_id,
        obligations=obligations,
        expected_documents=tuple(sorted(expected, key=lambda item: item.expected_document_key)),
        source_inventory_snapshot_ids=tuple(sorted(inventory_ids)),
        source_inventory_commitments=dict(sorted(inventory_commitments.items())),
        family_document_ids={
            family: tuple(sorted(document_ids))
            for family, document_ids in sorted(family_documents.items())
        },
        source_scope_sha256=_digest_json(
            {
                "expected_document_rows": sorted(
                    commitment_rows,
                    key=lambda item: (
                        str(item["obligation_revision_id"]),
                        str(item["expected_document_id"]),
                    ),
                ),
                "inventory_commitments": dict(sorted(inventory_commitments.items())),
                "obligations": [
                    item.model_dump(mode="json")
                    for item in sorted(
                        obligations,
                        key=lambda item: item.obligation_revision_id,
                    )
                ],
            }
        ),
        reason_codes=tuple(sorted(reasons)),
    )


def _inventory_commitment(
    conn: sqlite3.Connection,
    snapshot_id: str,
    observed_through: datetime,
) -> str | None:
    row = conn.execute(
        "SELECT inventory.inventory_key,inventory.revision,"
        "seal.expected_component_count,seal.component_digest_sha256,"
        "seal.completion_status,seal.sealed_at "
        "FROM source_inventory_snapshots inventory "
        "JOIN source_inventory_snapshot_seals seal "
        "ON seal.snapshot_id=inventory.snapshot_id "
        "WHERE inventory.snapshot_id=? "
        "AND datetime(inventory.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM source_inventory_snapshots newer "
        "WHERE newer.inventory_key=inventory.inventory_key "
        "AND newer.revision>inventory.revision "
        "AND datetime(newer.recorded_at)<=datetime(?))",
        (
            snapshot_id,
            _db_time(observed_through),
            _db_time(observed_through),
            _db_time(observed_through),
        ),
    ).fetchone()
    if row is None or str(row[4]) != "complete":
        return None
    return _digest_json(
        {
            "component_digest_sha256": str(row[3]),
            "expected_component_count": int(row[2]),
            "inventory_key": str(row[0]),
            "revision": int(row[1]),
            "sealed_at": str(row[5]),
            "snapshot_id": snapshot_id,
        }
    )


def _corpus_key(issuer_id: str) -> str:
    return f"investor-reporting:{issuer_id}"


def _corpus_revision(
    conn: sqlite3.Connection,
    issuer_id: str,
    cutoff_at: datetime,
) -> int:
    rows = conn.execute(
        "SELECT revision FROM search_corpus_manifests "
        "WHERE corpus_key=? AND datetime(knowledge_cutoff)=datetime(?) "
        "ORDER BY revision",
        (_corpus_key(issuer_id), _db_time(cutoff_at)),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("multiple corpus revisions exist at one exact cutoff")
    if rows:
        return int(rows[0][0])
    row = conn.execute(
        "SELECT COALESCE(MAX(revision),0) FROM search_corpus_manifests WHERE corpus_key=?",
        (_corpus_key(issuer_id),),
    ).fetchone()
    return int(row[0]) + 1


def _manifest_at_cutoff(
    conn: sqlite3.Connection,
    issuer_id: str,
    cutoff_at: datetime,
    observed_through: datetime,
) -> tuple[str, str] | None:
    rows = conn.execute(
        "SELECT manifest.manifest_id,run.index_run_id "
        "FROM search_corpus_manifests manifest "
        "JOIN search_corpus_manifest_seals seal "
        "ON seal.manifest_id=manifest.manifest_id "
        "JOIN search_index_runs run ON run.manifest_id=manifest.manifest_id "
        "JOIN search_projection_seals projection "
        "ON projection.index_run_id=run.index_run_id "
        "WHERE manifest.corpus_key=? "
        "AND datetime(manifest.knowledge_cutoff)=datetime(?) "
        "AND datetime(manifest.recorded_at)<=datetime(?) "
        "AND seal.completion_status='complete' "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "AND run.index_kind='lexical' AND run.outcome='succeeded' "
        "AND datetime(run.completed_at)<=datetime(?) "
        "AND projection.index_kind='lexical' "
        "AND datetime(projection.sealed_at)<=datetime(?) "
        "ORDER BY manifest.revision DESC,run.index_run_id",
        (
            _corpus_key(issuer_id),
            _db_time(cutoff_at),
            _db_time(observed_through),
            _db_time(observed_through),
            _db_time(observed_through),
            _db_time(observed_through),
        ),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("lexical corpus projection is ambiguous at cutoff")
    return None if not rows else (str(rows[0][0]), str(rows[0][1]))


def _incomplete_manifest_reasons(
    conn: sqlite3.Connection,
    manifest_id: str,
) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT membership_status,reason FROM search_corpus_document_memberships "
        "WHERE manifest_id=? AND membership_status<>'included' "
        "ORDER BY expected_document_key",
        (manifest_id,),
    ).fetchall()
    reasons: set[str] = set()
    for status, reason in rows:
        text = str(reason)
        if text.startswith("extraction:"):
            reasons.add("document_extraction_incomplete")
        elif str(status) == "quarantined":
            reasons.add("source_coverage_quarantined")
        else:
            reasons.add("source_coverage_incomplete")
    return tuple(sorted(reasons or {"lexical_corpus_incomplete"}))


def _verify_lexical_canaries(
    conn: sqlite3.Connection,
    *,
    manifest_id: str,
    issuer_id: str,
    family_document_ids: dict[str, tuple[str, ...]],
    cutoff_at: datetime,
) -> tuple[RetrievalCanaryReceipt, ...]:
    if not family_document_ids:
        raise ValueError("lexical canaries require included governed documents")
    allowed = {
        str(row[0])
        for row in conn.execute(
            "SELECT document_version_id FROM search_corpus_document_memberships "
            "WHERE manifest_id=? AND membership_status='included'",
            (manifest_id,),
        )
        if row[0] is not None
    }
    retriever = HybridRetriever(conn)
    receipts: list[RetrievalCanaryReceipt] = []
    for family, document_ids in sorted(family_document_ids.items()):
        scoped_ids = tuple(document_id for document_id in document_ids if document_id in allowed)
        if not scoped_ids:
            raise ValueError("governed document family is absent from the sealed corpus")
        placeholders = ",".join("?" for _ in scoped_ids)
        row = conn.execute(
            "SELECT chunk.text FROM search_chunks chunk "
            "JOIN evidence_nodes node ON node.node_id=chunk.evidence_node_id "
            "JOIN evidence_extraction_runs run "
            "ON run.extraction_run_id=node.extraction_run_id "
            f"WHERE chunk.manifest_id=? AND run.document_version_id IN ({placeholders}) "  # nosec B608 -- placeholder count only
            "ORDER BY chunk.chunk_id LIMIT 1",
            (manifest_id, *scoped_ids),
        ).fetchone()
        if row is None:
            raise ValueError("governed document family has no search chunk")
        query = _canary_query(str(row[0]))
        hits = retriever.search(
            query,
            manifest_id,
            filters=SearchFilter(issuer_id=issuer_id, knowledge_cutoff=cutoff_at),
            limit=10,
        )
        family_hits = [hit for hit in hits if hit.document_version_id in scoped_ids]
        if not family_hits or any(
            hit.document_version_id not in allowed
            or hit.issuer_id != issuer_id
            or not hit.source_url
            or not hit.chunk_id
            or not hit.node_id
            for hit in hits
        ):
            raise ValueError("lexical canary did not return grounded family evidence")
        hit_payload = [
            {
                "chunk_id": hit.chunk_id,
                "document_version_id": hit.document_version_id,
                "issuer_id": hit.issuer_id,
                "node_id": hit.node_id,
                "source_url_sha256": _digest_text(hit.source_url),
            }
            for hit in hits
        ]
        receipts.append(
            RetrievalCanaryReceipt(
                document_family=family,
                query_sha256=_digest_text(query),
                hit_set_sha256=_digest_json(hit_payload),
                hit_count=len(hits),
            )
        )
    return tuple(receipts)


def _promotion_at_cutoff(
    conn: sqlite3.Connection,
    cutoff_at: datetime,
    observed_through: datetime,
) -> tuple[str, str, str, int, str] | None:
    rows = conn.execute(
        "SELECT promotion_id,provider,model,dimensions,runtime_artifact_sha256 "
        "FROM search_embedding_model_promotions promotion "
        "WHERE purpose=? AND runtime_artifact_json IS NOT NULL "
        "AND runtime_artifact_sha256 IS NOT NULL "
        "AND datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM search_embedding_model_promotions newer "
        "WHERE newer.purpose=promotion.purpose AND newer.revision>promotion.revision "
        "AND datetime(newer.knowledge_at)<=datetime(?) "
        "AND datetime(newer.recorded_at)<=datetime(?)) "
        "ORDER BY revision DESC",
        (
            PURPOSE,
            _db_time(observed_through),
            _db_time(observed_through),
            _db_time(observed_through),
            _db_time(observed_through),
        ),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("embedding promotion is ambiguous at cutoff")
    row = rows[0]
    return str(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4])


def _vector_projection_at_cutoff(
    conn: sqlite3.Connection,
    *,
    manifest_id: str,
    provider: str,
    model: str,
    dimensions: int,
    runtime_artifact_sha256: str,
    cutoff_at: datetime,
    observed_through: datetime,
) -> str | None:
    rows = conn.execute(
        "SELECT seal.index_run_id FROM search_projection_seals seal "
        "JOIN search_index_runs run ON run.index_run_id=seal.index_run_id "
        "WHERE seal.manifest_id=? AND seal.index_kind='vector' "
        "AND seal.provider=? AND seal.model=? AND seal.dimensions=? "
        "AND seal.runtime_artifact_sha256=? "
        "AND run.outcome='succeeded' "
        "AND datetime(run.completed_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "ORDER BY datetime(seal.sealed_at) DESC,seal.index_run_id",
        (
            manifest_id,
            provider,
            model,
            dimensions,
            runtime_artifact_sha256,
            _db_time(observed_through),
            _db_time(observed_through),
        ),
    ).fetchall()
    for row in rows:
        index_run_id = str(row[0])
        seal = load_projection_seal(conn, index_run_id=index_run_id)
        if seal is None:
            continue
        try:
            verify_ledger_projection_seal(conn, seal)
        except RuntimeError:
            continue
        return index_run_id
    return None


def _require_projection_within_root(
    conn: sqlite3.Connection,
    *,
    vector_index_run_id: str,
    index_root: Path,
) -> None:
    row = conn.execute(
        "SELECT storage_uri FROM search_projection_seals "
        "WHERE index_run_id=? AND index_kind='vector'",
        (vector_index_run_id,),
    ).fetchone()
    if row is None:
        raise ValueError("sealed vector projection is absent")
    storage_uri = str(row[0])
    prefix = "lance://"
    suffix = "#evidence_chunks"
    if not storage_uri.startswith(prefix) or not storage_uri.endswith(suffix):
        raise ValueError("sealed vector projection URI is invalid")
    projection_path = Path(storage_uri[len(prefix) : -len(suffix)])
    if not projection_path.is_absolute() or ".." in projection_path.parts:
        raise ValueError("sealed vector projection path is unsafe")
    projection_path.resolve().parent.relative_to(index_root.resolve())


def _verify_semantic_canary(
    conn: sqlite3.Connection,
    *,
    manifest_id: str,
    vector_index_run_id: str,
    embedding_promotion_id: str,
    runtime_root: Path,
    exact_row_cap: int,
) -> SemanticCanaryReceipt:
    row = conn.execute(
        "SELECT text FROM search_chunks WHERE manifest_id=? ORDER BY chunk_id LIMIT 1",
        (manifest_id,),
    ).fetchone()
    if row is None:
        raise ValueError("semantic canary requires a nonempty sealed corpus")
    query = _canary_query(str(row[0]))
    semantic = ExactSemanticRuntime.from_local_ledger(
        conn,
        vector_index_run_id=vector_index_run_id,
        embedding_promotion_id=embedding_promotion_id,
        runtime_root=runtime_root,
        exact_row_cap=exact_row_cap,
    )
    evidence = semantic.search(query, limit=10)
    semantic.verify(evidence, query_text=query, limit=10)
    if not evidence.candidates:
        raise ValueError("semantic canary returned no candidates")
    placeholders = ",".join("?" for _ in evidence.candidates)
    count = int(
        conn.execute(
            "SELECT COUNT(*) FROM search_chunks "
            f"WHERE manifest_id=? AND chunk_id IN ({placeholders})",  # nosec B608 -- placeholder count only
            (manifest_id, *(item.chunk_id for item in evidence.candidates)),
        ).fetchone()[0]
    )
    if count != len(evidence.candidates):
        raise ValueError("semantic canary returned a chunk outside the sealed manifest")
    candidates = [item.model_dump(mode="json") for item in evidence.candidates]
    return SemanticCanaryReceipt(
        query_sha256=_digest_text(query),
        candidate_set_sha256=_digest_json(candidates),
        backend_receipt_sha256=_digest_json(evidence.backend.model_dump(mode="json")),
        candidate_count=len(candidates),
    )


def _canary_query(text: str) -> str:
    tokens = list(dict.fromkeys(re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", text)))[:32]
    if not tokens:
        raise ValueError("retrieval canary source chunk contains no searchable token")
    return " ".join(tokens)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return _utc(value).replace(tzinfo=None).isoformat(sep=" ")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _digest_json(value: object) -> str:
    return _digest_text(_canonical_json(value))


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
