"""Grounded mixed narrative + canonical-fact retrieval with an exact trace."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.research_snapshot import ResearchSnapshotRequest, verify_research_snapshot
from provenance.search_index_lineage import load_projection_seal
from provenance.verifier_identity import verifier_source_artifact_sha256
from search.canonical_fact_projection import (
    admit_canonical_projection_for_read,
    canonical_decimal,
    canonical_json,
    canonical_time,
    db_time,
    digest_text,
    search_canonical_facts,
)
from search.embedding_promotion import LocalVectorRuntimeConfig
from search.exact_semantic import (
    EXACT_SEMANTIC_ALGORITHM_VERSION,
    MAX_EXACT_VECTOR_ROWS,
    ExactSemanticError,
    ExactSemanticRuntime,
    backend_receipt_json,
    evidence_from_json,
)
from search.local_vector import LanceVectorIndex, LocalVectorCapabilityError

MAX_CANDIDATES = 1_000
_RESEARCH_AUDITOR_NAME = "strict-research-snapshot-admission-auditor"
_RESEARCH_AUDITOR_VERSION = "1"
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_RESEARCH_AUDITOR_CODE_SHA256 = verifier_source_artifact_sha256(
    {
        "provenance/research_snapshot.py": (_SOURCE_ROOT / "provenance" / "research_snapshot.py"),
        "provenance/verifier_identity.py": (_SOURCE_ROOT / "provenance" / "verifier_identity.py"),
        "search/canonical_fact_projection.py": (
            _SOURCE_ROOT / "search" / "canonical_fact_projection.py"
        ),
        "search/heterogeneous_retrieval.py": Path(__file__),
    }
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NarrativeBundle(_Frozen):
    corpus_manifest_id: str = Field(min_length=1, max_length=128)
    lexical_index_run_id: str = Field(min_length=1, max_length=128)
    vector_index_run_id: str | None = Field(default=None, max_length=128)
    embedding_promotion_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _semantic_pair(self) -> Self:
        if (self.vector_index_run_id is None) != (self.embedding_promotion_id is None):
            raise ValueError("semantic bundle requires both vector seal and promotion")
        return self


class SemanticCandidate(_Frozen):
    chunk_id: str = Field(min_length=1, max_length=128)
    score: str

    @model_validator(mode="after")
    def _score(self) -> Self:
        score = Decimal(canonical_decimal(self.score))
        if score < 0 or score > 1:
            raise ValueError("semantic candidate score must be in [0,1]")
        return self


class SemanticSearchReceipt(_Frozen):
    query_sha256: str = Field(min_length=64, max_length=64)
    vector_index_run_id: str = Field(min_length=1, max_length=128)
    embedding_promotion_id: str = Field(min_length=1, max_length=128)
    algorithm: Literal["exact_cosine", "auditable_ann"]
    algorithm_version: str = Field(min_length=1, max_length=64)
    reproducibility: Literal["exact", "auditable_nonreproducible"]
    promotion_eval_sha256: str = Field(min_length=64, max_length=64)
    candidates: tuple[SemanticCandidate, ...]
    ordered_candidate_set_sha256: str = Field(min_length=64, max_length=64)
    backend_receipt_json: str
    backend_receipt_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def _exact_receipt(self) -> Self:
        if (self.algorithm == "exact_cosine") != (self.reproducibility == "exact"):
            raise ValueError("semantic algorithm reproducibility is inconsistent")
        candidate_json = canonical_json(
            [candidate.model_dump(mode="json") for candidate in self.candidates]
        )
        if self.ordered_candidate_set_sha256 != digest_text(candidate_json):
            raise ValueError("semantic ordered candidate commitment is not exact")
        try:
            backend = json.loads(self.backend_receipt_json)
        except json.JSONDecodeError as exc:
            raise ValueError("semantic backend receipt is not JSON") from exc
        if not isinstance(backend, dict):
            raise ValueError("semantic backend receipt must be an object")
        canonical_backend = canonical_json(cast(dict[str, object], backend))
        if (
            self.backend_receipt_json != canonical_backend
            or self.backend_receipt_sha256 != digest_text(canonical_backend)
        ):
            raise ValueError("semantic backend receipt commitment is not exact")
        return self


class RetrievalRanker(_Frozen):
    name: str = Field(default="weighted-evidence-ranker", min_length=1, max_length=128)
    version: str = Field(default="1", min_length=1, max_length=64)
    lexical_weight: str = "0.45"
    semantic_weight: str = "0.35"
    fact_weight: str = "0.20"

    @model_validator(mode="after")
    def _weights(self) -> Self:
        values = tuple(
            Decimal(canonical_decimal(value))
            for value in (
                self.lexical_weight,
                self.semantic_weight,
                self.fact_weight,
            )
        )
        if any(value < 0 for value in values) or sum(values) != Decimal("1"):
            raise ValueError("retrieval ranker weights must be non-negative and sum to one")
        return self


class RetrievalFilters(_Frozen):
    reporting_entity_id: str | None = Field(default=None, max_length=128)
    include_narrative: bool = True
    include_facts: bool = True


class HeterogeneousRetrievalRequest(_Frozen):
    trace_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    research_snapshot_id: str = Field(min_length=1, max_length=128)
    fact_generation_id: str = Field(min_length=1, max_length=128)
    narrative_bundles: tuple[NarrativeBundle, ...]
    query_text: str = Field(min_length=1)
    candidate_limit: int = Field(default=100, ge=1, le=MAX_CANDIDATES)
    result_limit: int = Field(default=20, ge=1, le=MAX_CANDIDATES)
    ranker: RetrievalRanker = RetrievalRanker()
    filters: RetrievalFilters = RetrievalFilters()
    cutoff_at: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.result_limit > self.candidate_limit:
            raise ValueError("result limit cannot exceed candidate limit")
        manifest_ids = [item.corpus_manifest_id for item in self.narrative_bundles]
        if manifest_ids != sorted(set(manifest_ids)):
            raise ValueError("narrative bundles must be unique and sorted by manifest")
        if self.filters.include_narrative and not self.narrative_bundles:
            raise ValueError("narrative retrieval requires a sealed bundle")
        if len(self.narrative_bundles) > 64:
            raise ValueError("retrieval supports at most 64 narrative bundles")
        if _utc(self.recorded_at) < _utc(self.cutoff_at):
            raise ValueError("retrieval recording cannot precede cutoff")
        return self


class HeterogeneousRetrievalReceipt(_Frozen):
    trace_id: str
    trace_sha256: str
    research_snapshot_id: str
    research_snapshot_sha256: str
    fact_generation_id: str
    fact_projection_seal_sha256: str
    candidate_count: int = Field(ge=0)
    result_count: int = Field(ge=0)
    ordered_results: tuple[dict[str, object], ...]
    recorded_at: datetime


class HeterogeneousRetrievalError(RuntimeError):
    def __init__(self, reason_code: str, *, trace_id: str | None = None) -> None:
        self.reason_code = reason_code
        self.trace_id = trace_id
        super().__init__(reason_code)


def audit_research_snapshot_for_retrieval(
    conn: sqlite3.Connection,
    research_snapshot_id: str,
    *,
    audited_at: datetime,
) -> str:
    """Run the strict concrete verifier once and seal a read-admission receipt."""

    admission = verify_research_snapshot(conn, research_snapshot_id)
    seal_sha256, payload = _research_snapshot_admission_payload(conn, research_snapshot_id)
    if admission.member_count != _research_snapshot_member_count(conn, research_snapshot_id):
        raise HeterogeneousRetrievalError("research_snapshot_admission_member_count_changed")
    config_sha = digest_text("research_snapshot_admission_audit.default.v1")
    values = (
        research_snapshot_id,
        seal_sha256,
        _RESEARCH_AUDITOR_NAME,
        _RESEARCH_AUDITOR_VERSION,
        _RESEARCH_AUDITOR_CODE_SHA256,
        config_sha,
        payload,
        digest_text(payload),
        db_time(audited_at),
    )
    columns = (
        "research_snapshot_id",
        "research_snapshot_sha256",
        "verifier_name",
        "verifier_version",
        "verifier_code_sha256",
        "verifier_config_sha256",
        "audit_payload_json",
        "audit_payload_sha256",
        "audited_at",
    )
    with _savepoint(conn, "audit_research_snapshot_for_retrieval"):
        existing = _row(
            conn,
            "SELECT * FROM research_snapshot_admission_receipts WHERE research_snapshot_id=?",
            (research_snapshot_id,),
        )
        if existing is None:
            conn.execute(
                "INSERT INTO research_snapshot_admission_receipts "  # nosec B608 -- trusted internal SQL shape; values remain bound
                f"({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                values,
            )
        elif tuple(existing[column] for column in columns) != values:
            raise HeterogeneousRetrievalError("research_snapshot_admission_receipt_conflict")
    return digest_text(payload)


def _research_snapshot_member_count(conn: sqlite3.Connection, research_snapshot_id: str) -> int:
    seal = _row(
        conn,
        "SELECT member_count FROM research_snapshot_seals WHERE research_snapshot_id=?",
        (research_snapshot_id,),
    )
    if seal is None:
        raise HeterogeneousRetrievalError("research_snapshot_seal_missing")
    return _int(seal["member_count"])


def _research_snapshot_admission_payload(
    conn: sqlite3.Connection,
    research_snapshot_id: str,
) -> tuple[str, str]:
    seal = _row(
        conn,
        "SELECT member_count,member_set_sha256 FROM research_snapshot_seals "
        "WHERE research_snapshot_id=?",
        (research_snapshot_id,),
    )
    if seal is None:
        raise HeterogeneousRetrievalError("research_snapshot_seal_missing")
    projection_members = _rows(
        conn,
        "SELECT reference_commitment_sha256 FROM research_snapshot_members "
        "WHERE research_snapshot_id=? "
        "AND requested_lane LIKE 'canonical_fact_projection:%'",
        (research_snapshot_id,),
    )
    if len(projection_members) != 1:
        raise HeterogeneousRetrievalError("research_snapshot_projection_coordinate_missing")
    projection_audits = _rows(
        conn,
        "SELECT audit_payload_sha256 FROM canonical_fact_projection_audit_receipts "
        "WHERE projection_seal_sha256=? ORDER BY generation_id LIMIT 2",
        (projection_members[0]["reference_commitment_sha256"],),
    )
    if len(projection_audits) != 1:
        raise HeterogeneousRetrievalError("research_snapshot_projection_not_strictly_audited")
    seal_sha256 = str(seal["member_set_sha256"])
    payload = canonical_json(
        {
            "audit_version": "research_snapshot_admission_audit.v1",
            "member_count": _int(seal["member_count"]),
            "projection_audit_payload_sha256": projection_audits[0]["audit_payload_sha256"],
            "research_snapshot_id": research_snapshot_id,
            "research_snapshot_sha256": seal_sha256,
        }
    )
    return seal_sha256, payload


def retrieve_heterogeneous(
    conn: sqlite3.Connection,
    request: HeterogeneousRetrievalRequest,
    *,
    semantic_runtimes: Sequence[ExactSemanticRuntime] = (),
    local_vector_runtime: LocalVectorRuntimeConfig | None = None,
) -> HeterogeneousRetrievalReceipt:
    """Retrieve bounded candidates and durably seal the full investor-grade trace."""

    query_json = canonical_json(
        {
            "query_text": request.query_text,
            "query_version": "heterogeneous_query.v1",
        }
    )
    ranker_json = canonical_json(request.ranker)
    filters_json = canonical_json(request.filters)
    existing = _row(
        conn,
        "SELECT * FROM heterogeneous_retrieval_trace_headers WHERE trace_id=? OR idempotency_key=?",
        (request.trace_id, request.idempotency_key),
    )
    if existing is not None:
        if (
            str(existing["trace_id"]) != request.trace_id
            or str(existing["idempotency_key"]) != request.idempotency_key
            or str(existing["research_snapshot_id"]) != request.research_snapshot_id
            or str(existing["fact_generation_id"]) != request.fact_generation_id
            or str(existing["query_json"]) != query_json
            or str(existing["ranker_json"]) != ranker_json
            or str(existing["filters_json"]) != filters_json
            or _int(existing["candidate_limit"]) != request.candidate_limit
            or _int(existing["result_limit"]) != request.result_limit
            or canonical_time(existing["cutoff_at"]) != canonical_time(request.cutoff_at)
        ):
            raise HeterogeneousRetrievalError(
                "retrieval_trace_idempotency_conflict",
                trace_id=request.trace_id,
            )
        return verify_heterogeneous_retrieval_trace(
            conn,
            request.trace_id,
            semantic_runtimes=semantic_runtimes,
            local_vector_runtime=local_vector_runtime,
        )
    commitments = _verify_research_coordinates(conn, request)
    candidates, semantic_receipts = _collect_candidates(
        conn, request, semantic_runtimes, local_vector_runtime
    )
    narrative_json = canonical_json(commitments["narrative"])
    semantic_json = canonical_json(semantic_receipts)
    commitments["semantic_receipts_sha256"] = digest_text(semantic_json)
    header_values: tuple[object, ...] = (
        request.trace_id,
        request.idempotency_key,
        request.research_snapshot_id,
        commitments["research_snapshot_sha256"],
        request.fact_generation_id,
        commitments["fact_projection_seal_sha256"],
        narrative_json,
        digest_text(narrative_json),
        semantic_json,
        digest_text(semantic_json),
        digest_text(query_json),
        query_json,
        ranker_json,
        digest_text(ranker_json),
        filters_json,
        digest_text(filters_json),
        request.candidate_limit,
        request.result_limit,
        db_time(request.cutoff_at),
        db_time(request.recorded_at),
    )
    with _savepoint(conn, "heterogeneous_retrieval_trace"):
        conn.execute(
            "INSERT INTO heterogeneous_retrieval_trace_headers "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"({','.join(_HEADER_COLUMNS)}) VALUES "
            f"({','.join('?' for _ in _HEADER_COLUMNS)})",
            header_values,
        )
        ranked = _rank_candidates(request, candidates)
        _write_trace(conn, request, ranked, commitments)
        return verify_heterogeneous_retrieval_trace(
            conn,
            request.trace_id,
            semantic_runtimes=semantic_runtimes,
            local_vector_runtime=local_vector_runtime,
        )


def verify_heterogeneous_retrieval_trace(
    conn: sqlite3.Connection,
    trace_id: str,
    *,
    semantic_runtimes: Sequence[ExactSemanticRuntime] = (),
    local_vector_runtime: LocalVectorRuntimeConfig | None = None,
) -> HeterogeneousRetrievalReceipt:
    """Public no-callback verifier for a persisted retrieval trace."""

    header = _row(
        conn,
        "SELECT * FROM heterogeneous_retrieval_trace_headers WHERE trace_id=?",
        (trace_id,),
    )
    seal = _row(
        conn,
        "SELECT * FROM heterogeneous_retrieval_trace_seals WHERE trace_id=?",
        (trace_id,),
    )
    if header is None or seal is None:
        raise HeterogeneousRetrievalError("retrieval_trace_not_fully_sealed", trace_id=trace_id)
    query = _json_object(header["query_json"])
    ranker = RetrievalRanker.model_validate(_json_object(header["ranker_json"]))
    filters = RetrievalFilters.model_validate(_json_object(header["filters_json"]))
    research_cutoff = _research_cutoff(conn, str(header["research_snapshot_id"]))
    request = HeterogeneousRetrievalRequest(
        trace_id=trace_id,
        idempotency_key=str(header["idempotency_key"]),
        research_snapshot_id=str(header["research_snapshot_id"]),
        fact_generation_id=str(header["fact_generation_id"]),
        narrative_bundles=_bundles_from_commitments(
            _json_array(header["narrative_commitments_json"])
        ),
        query_text=str(query["query_text"]),
        candidate_limit=_int(header["candidate_limit"]),
        result_limit=_int(header["result_limit"]),
        ranker=ranker,
        filters=filters,
        cutoff_at=_datetime(header["cutoff_at"]),
        recorded_at=_datetime(header["recorded_at"]),
    )
    if canonical_time(research_cutoff) != canonical_time(request.cutoff_at):
        raise HeterogeneousRetrievalError("retrieval_trace_cutoff_mismatch", trace_id=trace_id)
    commitments = _verify_research_coordinates(conn, request)
    narrative_json = canonical_json(commitments["narrative"])
    semantic_receipts = _json_array(header["semantic_receipts_json"])
    lane_limit = max(1, request.candidate_limit // max(1, len(request.narrative_bundles) + 1))
    semantic_expected: dict[tuple[str, str], tuple[str, str]] = {}
    semantic_coordinates: set[tuple[str, str]] = set()
    for raw in semantic_receipts:
        if not isinstance(raw, dict):
            raise HeterogeneousRetrievalError("semantic_receipt_not_an_object", trace_id=trace_id)
        try:
            receipt = SemanticSearchReceipt.model_validate(raw)
        except ValueError as exc:
            raise HeterogeneousRetrievalError(
                "semantic_receipt_schema_invalid", trace_id=trace_id
            ) from exc
        bundle = next(
            (
                item
                for item in request.narrative_bundles
                if item.vector_index_run_id == receipt.vector_index_run_id
                and item.embedding_promotion_id == receipt.embedding_promotion_id
            ),
            None,
        )
        coordinate = (
            receipt.vector_index_run_id,
            receipt.embedding_promotion_id,
        )
        if bundle is None or coordinate in semantic_coordinates:
            raise HeterogeneousRetrievalError(
                "semantic_receipt_coordinate_or_cap_mismatch",
                trace_id=trace_id,
            )
        semantic_coordinates.add(coordinate)
        try:
            evidence = evidence_from_json(
                candidates=[candidate.model_dump(mode="json") for candidate in receipt.candidates],
                backend_receipt_json=receipt.backend_receipt_json,
            )
        except (ExactSemanticError, ValueError) as exc:
            raise HeterogeneousRetrievalError(
                "semantic_backend_receipt_invalid", trace_id=trace_id
            ) from exc
        runtime = _semantic_runtime(
            conn,
            bundle,
            semantic_runtimes,
            exact_row_cap=evidence.backend.exact_row_cap,
            trace_id=trace_id,
            local_vector_runtime=local_vector_runtime,
        )
        _require_recomputable_semantic_receipt(
            receipt,
            conn=conn,
            query_text=request.query_text,
            bundle=bundle,
            limit=lane_limit,
            trace_id=trace_id,
            runtime=runtime,
        )
        for candidate in receipt.candidates:
            semantic_expected[(bundle.corpus_manifest_id, candidate.chunk_id)] = (
                candidate.score,
                receipt.vector_index_run_id,
            )
    expected_coordinates = {
        (str(item.vector_index_run_id), str(item.embedding_promotion_id))
        for item in request.narrative_bundles
        if item.vector_index_run_id is not None
    }
    if semantic_coordinates != expected_coordinates:
        raise HeterogeneousRetrievalError(
            "semantic_receipt_bundle_coverage_mismatch", trace_id=trace_id
        )
    if (
        str(header["research_snapshot_sha256"]) != commitments["research_snapshot_sha256"]
        or str(header["fact_projection_seal_sha256"]) != commitments["fact_projection_seal_sha256"]
        or str(header["narrative_commitments_json"]) != narrative_json
        or str(header["narrative_commitments_sha256"]) != digest_text(narrative_json)
        or str(header["semantic_receipts_sha256"])
        != digest_text(str(header["semantic_receipts_json"]))
        or str(header["query_sha256"]) != digest_text(str(header["query_json"]))
        or str(header["ranker_sha256"]) != digest_text(str(header["ranker_json"]))
        or str(header["filters_sha256"]) != digest_text(str(header["filters_json"]))
    ):
        raise HeterogeneousRetrievalError("retrieval_trace_header_tampered", trace_id=trace_id)
    candidates = _rows(
        conn,
        "SELECT * FROM heterogeneous_retrieval_trace_candidates "
        "WHERE trace_id=? ORDER BY candidate_ordinal",
        (trace_id,),
    )
    candidate_jsons: list[dict[str, object]] = []
    for ordinal, candidate in enumerate(candidates):
        if _int(candidate["candidate_ordinal"]) != ordinal:
            raise HeterogeneousRetrievalError("retrieval_candidate_ordinal_gap", trace_id=trace_id)
        expected_filter = (
            ("included", None)
            if ordinal < request.candidate_limit
            else ("filtered", "candidate_limit_exceeded")
        )
        if (
            str(candidate["filter_outcome"]),
            (None if candidate["filter_reason"] is None else str(candidate["filter_reason"])),
        ) != expected_filter:
            raise HeterogeneousRetrievalError(
                "retrieval_candidate_limit_disposition_invalid",
                trace_id=trace_id,
            )
        payload = _candidate_payload(candidate)
        payload_json = canonical_json(payload)
        if (
            str(candidate["candidate_json"]) != payload_json
            or str(candidate["candidate_sha256"]) != digest_text(payload_json)
            or str(candidate["lineage_sha256"]) != digest_text(str(candidate["lineage_json"]))
            or str(candidate["normalized_score"])
            != _normalized_score(
                ranker,
                candidate_kind=str(candidate["candidate_kind"]),
                lexical_score=candidate["lexical_score"],
                semantic_score=candidate["semantic_score"],
            )
        ):
            raise HeterogeneousRetrievalError(
                "retrieval_candidate_commitment_tampered", trace_id=trace_id
            )
        _verify_candidate_source(
            conn,
            request,
            candidate,
            semantic_expected=semantic_expected,
        )
        candidate_jsons.append(payload)
    results = _rows(
        conn,
        "SELECT * FROM heterogeneous_retrieval_trace_results "
        "WHERE trace_id=? ORDER BY result_ordinal",
        (trace_id,),
    )
    result_jsons: list[dict[str, object]] = []
    expected_result_ordinals = [
        ordinal
        for ordinal, _candidate in sorted(
            enumerate(candidates),
            key=lambda pair: (
                -Decimal(str(pair[1]["normalized_score"])),
                str(pair[1]["candidate_kind"]),
                str(pair[1]["candidate_id"]),
            ),
        )
    ][: request.result_limit]
    for ordinal, result in enumerate(results):
        candidate_ordinal = _int(result["candidate_ordinal"])
        if (
            _int(result["result_ordinal"]) != ordinal
            or candidate_ordinal != expected_result_ordinals[ordinal]
            or candidate_ordinal >= len(candidates)
            or str(candidates[candidate_ordinal]["filter_outcome"]) != "included"
        ):
            raise HeterogeneousRetrievalError("retrieval_result_order_invalid", trace_id=trace_id)
        payload = {
            "candidate_id": candidates[candidate_ordinal]["candidate_id"],
            "candidate_kind": candidates[candidate_ordinal]["candidate_kind"],
            "candidate_ordinal": candidate_ordinal,
            "final_score": canonical_decimal(result["final_score"]),
            "result_ordinal": ordinal,
        }
        payload_json = canonical_json(payload)
        if str(result["result_json"]) != payload_json or str(
            result["result_sha256"]
        ) != digest_text(payload_json):
            raise HeterogeneousRetrievalError(
                "retrieval_result_commitment_tampered", trace_id=trace_id
            )
        result_jsons.append(payload)
    candidate_set_json = canonical_json(candidate_jsons)
    result_set_json = canonical_json(result_jsons)
    trace_payload = canonical_json(
        {
            "candidate_set_sha256": digest_text(candidate_set_json),
            "fact_projection_seal_sha256": header["fact_projection_seal_sha256"],
            "narrative_commitments_sha256": header["narrative_commitments_sha256"],
            "query_sha256": header["query_sha256"],
            "research_snapshot_sha256": header["research_snapshot_sha256"],
            "result_set_sha256": digest_text(result_set_json),
            "semantic_receipts_sha256": header["semantic_receipts_sha256"],
            "trace_id": trace_id,
            "trace_version": "heterogeneous_retrieval_trace.v1",
        }
    )
    if (
        _int(seal["candidate_count"]) != len(candidates)
        or _int(seal["result_count"]) != len(results)
        or str(seal["canonical_candidate_set_json"]) != candidate_set_json
        or str(seal["candidate_set_sha256"]) != digest_text(candidate_set_json)
        or str(seal["canonical_result_set_json"]) != result_set_json
        or str(seal["result_set_sha256"]) != digest_text(result_set_json)
        or str(seal["trace_json"]) != trace_payload
        or str(seal["trace_sha256"]) != digest_text(trace_payload)
    ):
        raise HeterogeneousRetrievalError("retrieval_final_seal_tampered", trace_id=trace_id)
    return HeterogeneousRetrievalReceipt(
        trace_id=trace_id,
        trace_sha256=str(seal["trace_sha256"]),
        research_snapshot_id=request.research_snapshot_id,
        research_snapshot_sha256=str(header["research_snapshot_sha256"]),
        fact_generation_id=request.fact_generation_id,
        fact_projection_seal_sha256=str(header["fact_projection_seal_sha256"]),
        candidate_count=len(candidates),
        result_count=len(results),
        ordered_results=tuple(result_jsons),
        recorded_at=request.recorded_at,
    )


def _verify_research_coordinates(
    conn: sqlite3.Connection, request: HeterogeneousRetrievalRequest
) -> dict[str, object]:
    _admit_research_snapshot_for_read(
        conn, request.research_snapshot_id, cutoff_at=request.cutoff_at
    )
    research_seal = _row(
        conn,
        "SELECT member_set_sha256 FROM research_snapshot_seals WHERE research_snapshot_id=?",
        (request.research_snapshot_id,),
    )
    research_header = _row(
        conn,
        "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
        (request.research_snapshot_id,),
    )
    universe = _row(
        conn,
        "SELECT issuer_id,reporting_entity_ids_json,document_version_ids_json,"
        "source_obligation_revision_ids_json,canonical_universe_json,"
        "universe_sha256,cutoff_at,recorded_at "
        "FROM research_snapshot_universe_commitments "
        "WHERE research_snapshot_id=?",
        (request.research_snapshot_id,),
    )
    generation = _row(
        conn,
        "SELECT resolution_snapshot_id,ontology_snapshot_id,cutoff_at "
        "FROM canonical_fact_projection_generations WHERE generation_id=?",
        (request.fact_generation_id,),
    )
    if research_seal is None or research_header is None or universe is None or generation is None:
        raise HeterogeneousRetrievalError(
            "retrieval_bound_snapshot_missing", trace_id=request.trace_id
        )
    snapshot_request = ResearchSnapshotRequest.model_validate_json(
        str(research_header["request_json"])
    )
    universe_payload = canonical_json(snapshot_request.research_universe.model_dump(mode="json"))
    if (
        str(universe["issuer_id"]) != snapshot_request.research_universe.issuer_id
        or str(universe["reporting_entity_ids_json"])
        != canonical_json(list(snapshot_request.research_universe.reporting_entity_ids))
        or str(universe["document_version_ids_json"])
        != canonical_json(list(snapshot_request.research_universe.document_version_ids))
        or str(universe["source_obligation_revision_ids_json"])
        != canonical_json(list(snapshot_request.research_universe.source_obligation_revision_ids))
        or str(universe["canonical_universe_json"]) != universe_payload
        or str(universe["universe_sha256"]) != digest_text(universe_payload)
        or canonical_time(universe["cutoff_at"]) != canonical_time(snapshot_request.cutoff_at)
        or canonical_time(universe["recorded_at"]) != canonical_time(snapshot_request.recorded_at)
    ):
        raise HeterogeneousRetrievalError(
            "retrieval_research_universe_tampered", trace_id=request.trace_id
        )
    if (
        request.filters.reporting_entity_id is not None
        and request.filters.reporting_entity_id
        not in snapshot_request.research_universe.reporting_entity_ids
    ):
        raise HeterogeneousRetrievalError(
            "retrieval_reporting_entity_outside_research_universe",
            trace_id=request.trace_id,
        )
    verified_generation = admit_canonical_projection_for_read(conn, request.fact_generation_id)
    members = _rows(
        conn,
        "SELECT requested_lane,reference_table,reference_id,"
        "reference_commitment_sha256 FROM research_snapshot_members "
        "WHERE research_snapshot_id=? ORDER BY member_ordinal LIMIT 1001",
        (request.research_snapshot_id,),
    )
    if len(members) > 1_000:
        raise HeterogeneousRetrievalError(
            "research_snapshot_exceeds_bounded_read_lane_cap",
            trace_id=request.trace_id,
        )
    by_lane = {str(member["requested_lane"]): member for member in members}
    fact_lane = "canonical_fact_projection:" + verified_generation.resolution_snapshot_id
    fact_reference = by_lane.get(fact_lane)
    ontology_reference = by_lane.get("ontology_snapshot")
    resolution_reference = by_lane.get("canonical_fact_resolution_snapshot")
    if (
        fact_reference is None
        or str(fact_reference["reference_commitment_sha256"])
        != verified_generation.projection_seal_sha256
        or ontology_reference is None
        or str(ontology_reference["reference_id"]) != verified_generation.ontology_snapshot_id
        or resolution_reference is None
        or str(resolution_reference["reference_id"]) != verified_generation.resolution_snapshot_id
    ):
        raise HeterogeneousRetrievalError(
            "retrieval_projection_not_bound_by_research_snapshot",
            trace_id=request.trace_id,
        )
    narrative: list[dict[str, object]] = []
    expected_narrative_lanes: set[str] = set()
    for bundle in request.narrative_bundles:
        lanes = [
            f"corpus:{bundle.corpus_manifest_id}",
            f"lexical_projection:{bundle.corpus_manifest_id}",
        ]
        if bundle.vector_index_run_id is not None:
            lanes.extend(
                (
                    f"vector_projection:{bundle.corpus_manifest_id}",
                    f"embedding_promotion:{bundle.corpus_manifest_id}",
                )
            )
        expected_narrative_lanes.update(lanes)
        for lane in lanes:
            member = by_lane.get(lane)
            if member is None:
                raise HeterogeneousRetrievalError(
                    "retrieval_narrative_bundle_not_in_research_snapshot",
                    trace_id=request.trace_id,
                )
            narrative.append(
                {
                    "reference_commitment_sha256": member["reference_commitment_sha256"],
                    "reference_id": member["reference_id"],
                    "reference_table": member["reference_table"],
                    "requested_lane": lane,
                }
            )
            commitment = narrative[-1]
            if lane.startswith(("lexical_projection:", "vector_projection:")):
                projection = _row(
                    conn,
                    "SELECT index_run_id,manifest_id FROM search_projection_seals "
                    "WHERE projection_seal_id=? LIMIT 1",
                    (member["reference_id"],),
                )
                expected_run_id = (
                    bundle.lexical_index_run_id
                    if lane.startswith("lexical_projection:")
                    else bundle.vector_index_run_id
                )
                if (
                    projection is None
                    or str(projection["index_run_id"]) != expected_run_id
                    or str(projection["manifest_id"]) != bundle.corpus_manifest_id
                ):
                    raise HeterogeneousRetrievalError(
                        "retrieval_projection_seal_coordinate_mismatch",
                        trace_id=request.trace_id,
                    )
                commitment["index_run_id"] = projection["index_run_id"]
            elif (
                lane.startswith("corpus:")
                and str(member["reference_id"]) != bundle.corpus_manifest_id
            ):
                raise HeterogeneousRetrievalError(
                    "retrieval_corpus_manifest_coordinate_mismatch",
                    trace_id=request.trace_id,
                )
            elif (
                lane.startswith("embedding_promotion:")
                and str(member["reference_id"]) != bundle.embedding_promotion_id
            ):
                raise HeterogeneousRetrievalError(
                    "retrieval_embedding_promotion_coordinate_mismatch",
                    trace_id=request.trace_id,
                )
    actual_narrative_lanes = {
        str(member["requested_lane"])
        for member in members
        if str(member["requested_lane"]).startswith(
            (
                "corpus:",
                "lexical_projection:",
                "vector_projection:",
                "embedding_promotion:",
            )
        )
    }
    if actual_narrative_lanes != expected_narrative_lanes:
        raise HeterogeneousRetrievalError(
            "retrieval_must_cover_exact_research_corpus_bundle_set",
            trace_id=request.trace_id,
        )
    return {
        "fact_projection_seal_sha256": verified_generation.projection_seal_sha256,
        "narrative": narrative,
        "research_snapshot_sha256": str(research_seal["member_set_sha256"]),
    }


def _admit_research_snapshot_for_read(
    conn: sqlite3.Connection,
    research_snapshot_id: str,
    *,
    cutoff_at: datetime,
) -> None:
    header = _row(
        conn,
        "SELECT request_json,request_sha256,cutoff_at "
        "FROM research_snapshot_headers WHERE research_snapshot_id=?",
        (research_snapshot_id,),
    )
    seal = _row(
        conn,
        "SELECT member_count,canonical_member_set_json,member_set_sha256 "
        "FROM research_snapshot_seals WHERE research_snapshot_id=?",
        (research_snapshot_id,),
    )
    receipt = _row(
        conn,
        "SELECT * FROM research_snapshot_admission_receipts WHERE research_snapshot_id=?",
        (research_snapshot_id,),
    )
    if header is None or seal is None or receipt is None:
        raise HeterogeneousRetrievalError("research_snapshot_strict_admission_receipt_missing")
    expected_seal_sha256, expected_audit_payload = _research_snapshot_admission_payload(
        conn, research_snapshot_id
    )
    expected_config_sha256 = digest_text("research_snapshot_admission_audit.default.v1")
    member_count = _int(seal["member_count"])
    members = _rows(
        conn,
        "SELECT canonical_member_json,member_sha256 "
        "FROM research_snapshot_members WHERE research_snapshot_id=? "
        "ORDER BY member_ordinal LIMIT 1001",
        (research_snapshot_id,),
    )
    if len(members) > 1_000:
        raise HeterogeneousRetrievalError("research_snapshot_exceeds_bounded_read_lane_cap")
    member_payload = canonical_json(
        [_json_object(member["canonical_member_json"]) for member in members]
    )
    if (
        canonical_time(header["cutoff_at"]) != canonical_time(cutoff_at)
        or str(header["request_sha256"]) != digest_text(str(header["request_json"]))
        or member_count != len(members)
        or str(seal["canonical_member_set_json"]) != member_payload
        or str(seal["member_set_sha256"]) != digest_text(member_payload)
        or any(
            str(member["member_sha256"]) != digest_text(str(member["canonical_member_json"]))
            for member in members
        )
        or str(receipt["research_snapshot_sha256"]) != str(seal["member_set_sha256"])
        or str(receipt["research_snapshot_sha256"]) != expected_seal_sha256
        or str(receipt["verifier_name"]) != _RESEARCH_AUDITOR_NAME
        or str(receipt["verifier_version"]) != _RESEARCH_AUDITOR_VERSION
        or str(receipt["verifier_code_sha256"]) != _RESEARCH_AUDITOR_CODE_SHA256
        or str(receipt["verifier_config_sha256"]) != expected_config_sha256
        or str(receipt["audit_payload_json"]) != expected_audit_payload
        or str(receipt["audit_payload_sha256"]) != digest_text(expected_audit_payload)
    ):
        raise HeterogeneousRetrievalError("research_snapshot_bounded_admission_tampered")


def _collect_candidates(
    conn: sqlite3.Connection,
    request: HeterogeneousRetrievalRequest,
    semantic_runtimes: Sequence[ExactSemanticRuntime],
    local_vector_runtime: LocalVectorRuntimeConfig | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    collected: dict[tuple[str, str], dict[str, object]] = {}
    semantic_receipts: list[dict[str, object]] = []
    lane_limit = max(1, request.candidate_limit // max(1, len(request.narrative_bundles) + 1))
    if request.filters.include_narrative:
        for bundle in request.narrative_bundles:
            for candidate in _lexical_candidates(
                conn,
                bundle,
                request.query_text,
                lane_limit,
                reporting_entity_id=request.filters.reporting_entity_id,
            ):
                collected[("narrative", str(candidate["candidate_id"]))] = candidate
            if bundle.vector_index_run_id is not None:
                runtime = _semantic_runtime(
                    conn,
                    bundle,
                    semantic_runtimes,
                    trace_id=request.trace_id,
                    local_vector_runtime=local_vector_runtime,
                )
                try:
                    evidence = runtime.search(request.query_text, limit=lane_limit)
                except ExactSemanticError as exc:
                    raise HeterogeneousRetrievalError(
                        "semantic_exact_retrieval_failed",
                        trace_id=request.trace_id,
                    ) from exc
                candidates = tuple(
                    SemanticCandidate(chunk_id=item.chunk_id, score=item.score)
                    for item in evidence.candidates
                )
                candidate_json = canonical_json(
                    [candidate.model_dump(mode="json") for candidate in candidates]
                )
                backend_json = backend_receipt_json(evidence)
                semantic = SemanticSearchReceipt(
                    query_sha256=digest_text(request.query_text),
                    vector_index_run_id=bundle.vector_index_run_id,
                    embedding_promotion_id=str(bundle.embedding_promotion_id),
                    algorithm="exact_cosine",
                    algorithm_version=EXACT_SEMANTIC_ALGORITHM_VERSION,
                    reproducibility="exact",
                    promotion_eval_sha256=(evidence.backend.promotion_eval_sha256),
                    candidates=candidates,
                    ordered_candidate_set_sha256=digest_text(candidate_json),
                    backend_receipt_json=backend_json,
                    backend_receipt_sha256=digest_text(backend_json),
                )
                _require_recomputable_semantic_receipt(
                    semantic,
                    conn=conn,
                    query_text=request.query_text,
                    bundle=bundle,
                    limit=lane_limit,
                    trace_id=request.trace_id,
                    runtime=runtime,
                )
                semantic_receipts.append(semantic.model_dump(mode="json"))
                for candidate in semantic.candidates:
                    semantic_item = _semantic_candidate(
                        conn,
                        bundle,
                        candidate,
                        reporting_entity_id=request.filters.reporting_entity_id,
                    )
                    if semantic_item is None:
                        continue
                    key = ("narrative", candidate.chunk_id)
                    existing = collected.get(key)
                    if existing is None:
                        collected[key] = semantic_item
                    else:
                        if (
                            existing["source_commitment_sha256"]
                            != semantic_item["source_commitment_sha256"]
                            or existing["evidence_locator"] != semantic_item["evidence_locator"]
                        ):
                            raise HeterogeneousRetrievalError(
                                "semantic_lexical_candidate_identity_mismatch",
                                trace_id=request.trace_id,
                            )
                        existing_lineage = cast(dict[str, object], existing["lineage"])
                        semantic_lineage = cast(dict[str, object], semantic_item["lineage"])
                        existing_lineage.update(semantic_lineage)
                        existing["semantic_score"] = semantic_item["semantic_score"]
    if request.filters.include_facts:
        facts = search_canonical_facts(
            conn,
            generation_id=request.fact_generation_id,
            query_text=request.query_text,
            limit=lane_limit,
            reporting_entity_id=request.filters.reporting_entity_id,
        )
        for fact in facts:
            collected[("fact", fact.canonical_metric_cell_id)] = {
                "candidate_id": fact.canonical_metric_cell_id,
                "candidate_kind": "fact",
                "evidence_locator": fact.evidence_locator,
                "lexical_score": "1",
                "lineage": fact.lineage,
                "semantic_score": None,
                "source_commitment_sha256": fact.entry_sha256,
            }
    return list(collected.values()), semantic_receipts


def _require_recomputable_semantic_receipt(
    receipt: SemanticSearchReceipt,
    *,
    conn: sqlite3.Connection,
    query_text: str,
    bundle: NarrativeBundle,
    limit: int,
    trace_id: str,
    runtime: ExactSemanticRuntime | None = None,
) -> None:
    """Admit only receipts whose scores the local verifier can recompute."""

    if (
        receipt.query_sha256 != digest_text(query_text)
        or receipt.vector_index_run_id != bundle.vector_index_run_id
        or receipt.embedding_promotion_id != bundle.embedding_promotion_id
        or len(receipt.candidates) > limit
        or receipt.algorithm != "exact_cosine"
        or receipt.reproducibility != "exact"
        or receipt.algorithm_version != EXACT_SEMANTIC_ALGORITHM_VERSION
    ):
        raise HeterogeneousRetrievalError(
            "semantic_receipt_coordinate_or_cap_mismatch",
            trace_id=trace_id,
        )
    try:
        evidence = evidence_from_json(
            candidates=[candidate.model_dump(mode="json") for candidate in receipt.candidates],
            backend_receipt_json=receipt.backend_receipt_json,
        )
        if (
            evidence.backend.query_sha256 != receipt.query_sha256
            or evidence.backend.vector_index_run_id != receipt.vector_index_run_id
            or evidence.backend.embedding_promotion_id != receipt.embedding_promotion_id
            or evidence.backend.promotion_eval_sha256 != receipt.promotion_eval_sha256
            or evidence.backend.requested_limit != limit
        ):
            raise ExactSemanticError("semantic receipt envelope differs from backend evidence")
        verifier = runtime or _semantic_runtime(
            conn,
            bundle,
            (),
            exact_row_cap=evidence.backend.exact_row_cap,
            trace_id=trace_id,
        )
        verifier.verify(evidence, query_text=query_text, limit=limit)
    except (ExactSemanticError, ValueError) as exc:
        raise HeterogeneousRetrievalError(
            "semantic_receipt_exact_recomputation_failed",
            trace_id=trace_id,
        ) from exc


def _semantic_runtime(
    conn: sqlite3.Connection,
    bundle: NarrativeBundle,
    runtimes: Sequence[ExactSemanticRuntime],
    *,
    exact_row_cap: int | None = None,
    trace_id: str,
    local_vector_runtime: LocalVectorRuntimeConfig | None = None,
) -> ExactSemanticRuntime:
    if bundle.vector_index_run_id is None or bundle.embedding_promotion_id is None:
        raise HeterogeneousRetrievalError("semantic_bundle_coordinates_missing", trace_id=trace_id)
    matches = [
        runtime
        for runtime in runtimes
        if runtime.vector_index_run_id == bundle.vector_index_run_id
        and runtime.embedding_promotion_id == bundle.embedding_promotion_id
        and runtime.manifest_id == bundle.corpus_manifest_id
    ]
    if len(matches) > 1:
        raise HeterogeneousRetrievalError(
            "semantic_runtime_coordinate_ambiguous", trace_id=trace_id
        )
    if matches:
        return matches[0]
    runtime_config = (
        local_vector_runtime
        if local_vector_runtime is not None
        else LocalVectorRuntimeConfig.from_environment()
    )
    if runtime_config is None:
        raise HeterogeneousRetrievalError("semantic_local_runtime_unavailable", trace_id=trace_id)
    try:
        seal = load_projection_seal(conn, index_run_id=bundle.vector_index_run_id)
        if seal is None:
            raise ExactSemanticError("semantic projection seal is unavailable")
        expected_storage_uri = LanceVectorIndex(runtime_config.index_root).published_storage_uri(
            bundle.vector_index_run_id
        )
        if seal.storage_uri != expected_storage_uri:
            raise ExactSemanticError(
                "semantic projection is outside the configured local index root"
            )
        return ExactSemanticRuntime.from_local_ledger(
            conn,
            vector_index_run_id=bundle.vector_index_run_id,
            embedding_promotion_id=bundle.embedding_promotion_id,
            runtime_root=runtime_config.runtime_root,
            exact_row_cap=(exact_row_cap if exact_row_cap is not None else MAX_EXACT_VECTOR_ROWS),
        )
    except (
        ExactSemanticError,
        LocalVectorCapabilityError,
        ValueError,
    ) as exc:
        raise HeterogeneousRetrievalError(
            "semantic_local_runtime_unavailable", trace_id=trace_id
        ) from exc


def _lexical_candidates(
    conn: sqlite3.Connection,
    bundle: NarrativeBundle,
    query_text: str,
    limit: int,
    *,
    reporting_entity_id: str | None = None,
) -> list[dict[str, object]]:
    expression = " OR ".join(
        f'"{token.replace(chr(34), chr(34) * 2)}"' for token in query_text.split() if token.strip()
    )
    if not expression:
        return []
    rows = _rows(
        conn,
        "SELECT chunk.chunk_id,chunk.content_sha256,chunk.evidence_node_id,"
        "chunk.char_start,chunk.char_end,chunk.manifest_id,"
        "node.extraction_run_id,run.document_version_id,"
        "bm25(search_lexical_chunks) AS lexical_rank "
        "FROM search_lexical_chunks "
        "JOIN search_chunks chunk ON chunk.chunk_id=search_lexical_chunks.chunk_id "
        "JOIN evidence_nodes node ON node.node_id=chunk.evidence_node_id "
        "JOIN evidence_extraction_runs run "
        "ON run.extraction_run_id=node.extraction_run_id "
        "JOIN v_evidence_document_versions_canonical document "
        "ON document.document_version_id=run.document_version_id "
        "WHERE search_lexical_chunks MATCH ? AND chunk.manifest_id=? "
        "AND (? IS NULL OR document.reporting_entity_id=?) "
        "ORDER BY lexical_rank,chunk.chunk_id LIMIT ?",
        (
            expression,
            bundle.corpus_manifest_id,
            reporting_entity_id,
            reporting_entity_id,
            limit,
        ),
    )
    return [
        {
            "candidate_id": str(row["chunk_id"]),
            "candidate_kind": "narrative",
            "evidence_locator": _narrative_locator(row),
            "lexical_score": canonical_decimal(
                Decimal(1) / (Decimal(1) + abs(Decimal(str(row["lexical_rank"]))))
            ),
            "lineage": {
                "corpus_manifest_id": bundle.corpus_manifest_id,
                "evidence_node_id": row["evidence_node_id"],
                "lexical_index_run_id": bundle.lexical_index_run_id,
            },
            "semantic_score": None,
            "source_commitment_sha256": str(row["content_sha256"]),
        }
        for row in rows
    ]


def _semantic_candidate(
    conn: sqlite3.Connection,
    bundle: NarrativeBundle,
    candidate: SemanticCandidate,
    *,
    reporting_entity_id: str | None = None,
) -> dict[str, object] | None:
    assert bundle.vector_index_run_id is not None
    row = _row(
        conn,
        "SELECT chunk.chunk_id,chunk.content_sha256,chunk.evidence_node_id,"
        "chunk.char_start,chunk.char_end,chunk.manifest_id,"
        "node.extraction_run_id,run.document_version_id,"
        "artifact.vector_sha256 "
        "FROM search_chunks chunk "
        "JOIN search_index_memberships membership "
        "ON membership.chunk_id=chunk.chunk_id "
        "AND membership.index_run_id=? AND membership.membership_status='included' "
        "JOIN search_embedding_artifacts artifact "
        "ON artifact.index_run_id=membership.index_run_id "
        "AND artifact.chunk_id=chunk.chunk_id AND artifact.outcome='succeeded' "
        "JOIN evidence_nodes node ON node.node_id=chunk.evidence_node_id "
        "JOIN evidence_extraction_runs run "
        "ON run.extraction_run_id=node.extraction_run_id "
        "JOIN v_evidence_document_versions_canonical document "
        "ON document.document_version_id=run.document_version_id "
        "WHERE chunk.chunk_id=? AND chunk.manifest_id=? "
        "AND (? IS NULL OR document.reporting_entity_id=?)",
        (
            bundle.vector_index_run_id,
            candidate.chunk_id,
            bundle.corpus_manifest_id,
            reporting_entity_id,
            reporting_entity_id,
        ),
    )
    if row is None:
        if reporting_entity_id is not None:
            return None
        raise HeterogeneousRetrievalError("semantic_candidate_not_in_sealed_index")
    return {
        "candidate_id": candidate.chunk_id,
        "candidate_kind": "narrative",
        "evidence_locator": _narrative_locator(row),
        "lexical_score": None,
        "lineage": {
            "corpus_manifest_id": bundle.corpus_manifest_id,
            "evidence_node_id": row["evidence_node_id"],
            "lexical_index_run_id": bundle.lexical_index_run_id,
            "vector_index_run_id": bundle.vector_index_run_id,
            "vector_sha256": row["vector_sha256"],
        },
        "semantic_score": canonical_decimal(candidate.score),
        "source_commitment_sha256": str(row["content_sha256"]),
    }


def _rank_candidates(
    request: HeterogeneousRetrievalRequest,
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    for candidate in candidates:
        candidate["normalized_score"] = _normalized_score(
            request.ranker,
            candidate_kind=str(candidate["candidate_kind"]),
            lexical_score=candidate["lexical_score"],
            semantic_score=candidate["semantic_score"],
        )
    return sorted(
        candidates,
        key=lambda item: (
            -Decimal(str(item["normalized_score"])),
            str(item["candidate_kind"]),
            str(item["candidate_id"]),
        ),
    )


def _normalized_score(
    ranker: RetrievalRanker,
    *,
    candidate_kind: str,
    lexical_score: object | None,
    semantic_score: object | None,
) -> str:
    lexical_weight = Decimal(canonical_decimal(ranker.lexical_weight))
    semantic_weight = Decimal(canonical_decimal(ranker.semantic_weight))
    fact_weight = Decimal(canonical_decimal(ranker.fact_weight))
    lexical = Decimal(canonical_decimal(lexical_score or "0"))
    semantic = Decimal(canonical_decimal(semantic_score or "0"))
    fact = Decimal(1) if candidate_kind == "fact" else Decimal(0)
    return canonical_decimal(
        lexical * lexical_weight + semantic * semantic_weight + fact * fact_weight
    )


def _write_trace(
    conn: sqlite3.Connection,
    request: HeterogeneousRetrievalRequest,
    candidates: list[dict[str, object]],
    commitments: dict[str, object],
) -> None:
    candidate_payloads: list[dict[str, object]] = []
    for ordinal, item in enumerate(candidates):
        included = ordinal < request.candidate_limit
        lineage_json = canonical_json(item["lineage"])
        locator_json = canonical_json(item["evidence_locator"])
        payload = {
            "candidate_id": item["candidate_id"],
            "candidate_kind": item["candidate_kind"],
            "candidate_ordinal": ordinal,
            "evidence_locator": item["evidence_locator"],
            "filter_outcome": "included" if included else "filtered",
            "filter_reason": None if included else "candidate_limit_exceeded",
            "lexical_score": item["lexical_score"],
            "lineage_sha256": digest_text(lineage_json),
            "normalized_score": item["normalized_score"],
            "ranker_name": request.ranker.name,
            "semantic_score": item["semantic_score"],
            "source_commitment_sha256": item["source_commitment_sha256"],
        }
        candidate_json = canonical_json(payload)
        candidate_payloads.append(payload)
        conn.execute(
            "INSERT INTO heterogeneous_retrieval_trace_candidates "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                request.trace_id,
                ordinal,
                item["candidate_kind"],
                item["candidate_id"],
                item["source_commitment_sha256"],
                item["lexical_score"],
                item["semantic_score"],
                item["normalized_score"],
                request.ranker.name,
                payload["filter_outcome"],
                payload["filter_reason"],
                locator_json,
                lineage_json,
                digest_text(lineage_json),
                candidate_json,
                digest_text(candidate_json),
            ),
        )
    results = candidates[: request.candidate_limit][: request.result_limit]
    result_payloads: list[dict[str, object]] = []
    for ordinal, item in enumerate(results):
        candidate_ordinal = candidates.index(item)
        payload = {
            "candidate_id": item["candidate_id"],
            "candidate_kind": item["candidate_kind"],
            "candidate_ordinal": candidate_ordinal,
            "final_score": item["normalized_score"],
            "result_ordinal": ordinal,
        }
        result_json = canonical_json(payload)
        result_payloads.append(payload)
        conn.execute(
            "INSERT INTO heterogeneous_retrieval_trace_results VALUES (?,?,?,?,?,?)",
            (
                request.trace_id,
                ordinal,
                candidate_ordinal,
                item["normalized_score"],
                result_json,
                digest_text(result_json),
            ),
        )
    candidate_json = canonical_json(candidate_payloads)
    result_json = canonical_json(result_payloads)
    trace_payload = canonical_json(
        {
            "candidate_set_sha256": digest_text(candidate_json),
            "fact_projection_seal_sha256": commitments["fact_projection_seal_sha256"],
            "narrative_commitments_sha256": digest_text(canonical_json(commitments["narrative"])),
            "query_sha256": digest_text(
                canonical_json(
                    {
                        "query_text": request.query_text,
                        "query_version": "heterogeneous_query.v1",
                    }
                )
            ),
            "research_snapshot_sha256": commitments["research_snapshot_sha256"],
            "result_set_sha256": digest_text(result_json),
            "semantic_receipts_sha256": commitments["semantic_receipts_sha256"],
            "trace_id": request.trace_id,
            "trace_version": "heterogeneous_retrieval_trace.v1",
        }
    )
    conn.execute(
        "INSERT INTO heterogeneous_retrieval_trace_seals VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            request.trace_id,
            len(candidates),
            len(results),
            candidate_json,
            digest_text(candidate_json),
            result_json,
            digest_text(result_json),
            trace_payload,
            digest_text(trace_payload),
            db_time(request.recorded_at),
        ),
    )


def _verify_candidate_source(
    conn: sqlite3.Connection,
    request: HeterogeneousRetrievalRequest,
    candidate: dict[str, object],
    *,
    semantic_expected: dict[tuple[str, str], tuple[str, str]],
) -> None:
    locator = _json_object(candidate["evidence_locator_json"])
    lineage = _json_object(candidate["lineage_json"])
    if str(candidate["candidate_kind"]) == "narrative":
        manifest_id = str(lineage.get("corpus_manifest_id", ""))
        lexical_index_run_id = str(lineage.get("lexical_index_run_id", ""))
        bundle = next(
            (
                item
                for item in request.narrative_bundles
                if item.corpus_manifest_id == manifest_id
                and item.lexical_index_run_id == lexical_index_run_id
            ),
            None,
        )
        if bundle is None:
            raise HeterogeneousRetrievalError(
                "narrative_candidate_not_in_bound_bundle",
                trace_id=request.trace_id,
            )
        row = _row(
            conn,
            "SELECT chunk.content_sha256,chunk.evidence_node_id,chunk.char_start,"
            "chunk.char_end,run.document_version_id,document.reporting_entity_id "
            "FROM search_chunks chunk "
            "JOIN evidence_nodes node "
            "ON node.node_id=chunk.evidence_node_id "
            "JOIN evidence_extraction_runs run "
            "ON run.extraction_run_id=node.extraction_run_id "
            "JOIN v_evidence_document_versions_canonical document "
            "ON document.document_version_id=run.document_version_id "
            "WHERE chunk.chunk_id=? AND chunk.manifest_id=?",
            (candidate["candidate_id"], manifest_id),
        )
        recomputed = (
            next(
                (
                    item
                    for item in _lexical_candidates(
                        conn,
                        bundle,
                        request.query_text,
                        request.candidate_limit,
                        reporting_entity_id=request.filters.reporting_entity_id,
                    )
                    if item["candidate_id"] == candidate["candidate_id"]
                ),
                None,
            )
            if candidate["lexical_score"] is not None
            else None
        )
        semantic = semantic_expected.get((manifest_id, str(candidate["candidate_id"])))
        semantic_score = candidate["semantic_score"]
        semantic_valid = (
            semantic_score is None
            and semantic is None
            and lineage.get("vector_index_run_id") is None
            and lineage.get("vector_sha256") is None
        )
        if semantic_score is not None and semantic is not None:
            semantic_candidate = _semantic_candidate(
                conn,
                bundle,
                SemanticCandidate(
                    chunk_id=str(candidate["candidate_id"]),
                    score=str(semantic_score),
                ),
                reporting_entity_id=request.filters.reporting_entity_id,
            )
            if semantic_candidate is not None:
                semantic_lineage = cast(dict[str, object], semantic_candidate["lineage"])
                semantic_valid = (
                    str(semantic_score) == semantic[0]
                    and str(lineage.get("vector_index_run_id")) == semantic[1]
                    and str(lineage.get("vector_sha256")) == str(semantic_lineage["vector_sha256"])
                )
        if (
            row is None
            or str(row["content_sha256"]) != str(candidate["source_commitment_sha256"])
            or locator != _narrative_locator(row)
            or str(lineage.get("evidence_node_id")) != str(row["evidence_node_id"])
            or (
                request.filters.reporting_entity_id is not None
                and str(row["reporting_entity_id"]) != request.filters.reporting_entity_id
            )
            or (
                candidate["lexical_score"] is not None
                and (
                    recomputed is None
                    or str(candidate["lexical_score"]) != str(recomputed["lexical_score"])
                )
            )
            or not semantic_valid
        ):
            raise HeterogeneousRetrievalError(
                "narrative_candidate_evidence_mismatch", trace_id=request.trace_id
            )
        return
    hits = search_canonical_facts(
        conn,
        generation_id=request.fact_generation_id,
        query_text=request.query_text,
        limit=request.candidate_limit,
        reporting_entity_id=request.filters.reporting_entity_id,
    )
    match = next(
        (hit for hit in hits if hit.canonical_metric_cell_id == candidate["candidate_id"]),
        None,
    )
    if (
        match is None
        or match.entry_sha256 != candidate["source_commitment_sha256"]
        or match.evidence_locator != locator
        or match.lineage != lineage
    ):
        raise HeterogeneousRetrievalError(
            "fact_candidate_evidence_mismatch", trace_id=request.trace_id
        )


def _candidate_payload(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "candidate_id": candidate["candidate_id"],
        "candidate_kind": candidate["candidate_kind"],
        "candidate_ordinal": _int(candidate["candidate_ordinal"]),
        "evidence_locator": _json_object(candidate["evidence_locator_json"]),
        "filter_outcome": candidate["filter_outcome"],
        "filter_reason": candidate["filter_reason"],
        "lexical_score": candidate["lexical_score"],
        "lineage_sha256": candidate["lineage_sha256"],
        "normalized_score": canonical_decimal(candidate["normalized_score"]),
        "ranker_name": candidate["ranker_name"],
        "semantic_score": candidate["semantic_score"],
        "source_commitment_sha256": candidate["source_commitment_sha256"],
    }


def _narrative_locator(row: dict[str, object]) -> dict[str, object]:
    return {
        "char_end": _int(row["char_end"]),
        "char_start": _int(row["char_start"]),
        "document_version_id": str(row["document_version_id"]),
        "evidence_node_id": str(row["evidence_node_id"]),
    }


def _bundles_from_commitments(
    commitments: Sequence[object],
) -> tuple[NarrativeBundle, ...]:
    grouped: dict[str, dict[str, str]] = {}
    for raw in commitments:
        if not isinstance(raw, dict):
            raise ValueError("narrative commitment must be an object")
        item = cast(dict[str, object], raw)
        lane = str(item["requested_lane"])
        if ":" not in lane:
            continue
        kind, manifest = lane.split(":", 1)
        group = grouped.setdefault(manifest, {})
        coordinate = (
            item.get("index_run_id")
            if kind in {"lexical_projection", "vector_projection"}
            else item["reference_id"]
        )
        group[kind] = str(coordinate)
    return tuple(
        NarrativeBundle(
            corpus_manifest_id=manifest,
            lexical_index_run_id=values["lexical_projection"],
            vector_index_run_id=values.get("vector_projection"),
            embedding_promotion_id=values.get("embedding_promotion"),
        )
        for manifest, values in sorted(grouped.items())
    )


def _bundles_from_members(
    members: list[dict[str, object]],
) -> tuple[NarrativeBundle, ...]:
    commitments = [
        {
            "reference_id": member["reference_id"],
            "requested_lane": member["requested_lane"],
        }
        for member in members
        if str(member["requested_lane"]).startswith(
            (
                "corpus:",
                "lexical_projection:",
                "vector_projection:",
                "embedding_promotion:",
            )
        )
    ]
    return _bundles_from_commitments(commitments)


def _research_cutoff(conn: sqlite3.Connection, research_snapshot_id: str) -> datetime:
    row = conn.execute(
        "SELECT cutoff_at FROM research_snapshot_headers WHERE research_snapshot_id=?",
        (research_snapshot_id,),
    ).fetchone()
    if row is None:
        raise HeterogeneousRetrievalError("research_snapshot_missing")
    return _datetime(row[0])


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str) -> Generator[None, None, None]:
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")


def _rows(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> list[dict[str, object]]:
    cursor = conn.execute(sql, params)
    names = tuple(item[0] for item in cursor.description or ())
    return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]


def _row(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> dict[str, object] | None:
    cursor = conn.execute(sql, params)
    names = tuple(item[0] for item in cursor.description or ())
    value = cursor.fetchone()
    return None if value is None else dict(zip(names, tuple(value), strict=True))


def _json_object(value: object) -> dict[str, object]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("expected canonical JSON object")
    return cast(dict[str, object], parsed)


def _json_array(value: object) -> list[object]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise ValueError("expected canonical JSON array")
    return cast(list[object], parsed)


def _datetime(value: object) -> datetime:
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _int(value: object) -> int:
    return int(str(value))


_HEADER_COLUMNS = (
    "trace_id",
    "idempotency_key",
    "research_snapshot_id",
    "research_snapshot_sha256",
    "fact_generation_id",
    "fact_projection_seal_sha256",
    "narrative_commitments_json",
    "narrative_commitments_sha256",
    "semantic_receipts_json",
    "semantic_receipts_sha256",
    "query_sha256",
    "query_json",
    "ranker_json",
    "ranker_sha256",
    "filters_json",
    "filters_sha256",
    "candidate_limit",
    "result_limit",
    "cutoff_at",
    "recorded_at",
)
