from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from pydantic import ValidationError

import search.exact_semantic as exact_semantic_module
import search.heterogeneous_retrieval as retrieval_module
from alembic import command
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionPolicy,
)
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.filing_xbrl_extraction_ledger import FilingXbrlExtractionLedger
from provenance.filing_xbrl_fact_adapter import FilingXbrlNormalizedOutput
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalMetric,
    CanonicalMetricCell,
    CanonicalMetricDefinitionRevision,
    MetricOntology,
    OntologySnapshot,
)
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    DocumentProcessingDisposition,
    DocumentProcessingPolicy,
    DocumentProcessingScope,
    ResearchSnapshotRequest,
    ResearchUniverse,
    build_research_snapshot,
    derive_obligations,
    record_disposition,
    seal_disposition,
    seal_processing_snapshot,
)
from provenance.search_index_lineage import (
    SearchProjectionSeal,
    manifest_chunk_commitment,
    persist_projection_seal,
    vector_artifact_commitment,
)
from provenance.source_fact_stream import (
    bind_resolution_snapshot_watermark,
)
from search.canonical_fact_projection import (
    ProjectionConfig,
    ProjectionGenerationRequest,
    build_canonical_projection_generation,
    canonical_json,
    digest_text,
    search_canonical_facts,
)
from search.corpus_builder import (
    ChunkerConfig,
    CorpusBuildRequest,
    ExpectedDocument,
    build_grounded_search_corpus,
)
from search.embedding_promotion import (
    EmbeddingPromotion,
    LocalVectorRuntimeConfig,
)
from search.embedding_runtime_artifact import (
    EmbeddingRuntimeArtifact,
    RuntimeArtifactFile,
    RuntimeComponentVersion,
)
from search.exact_semantic import (
    EXACT_SEMANTIC_ALGORITHM_VERSION,
    ExactSemanticError,
    ExactSemanticRuntime,
    backend_receipt_json,
)
from search.heterogeneous_retrieval import (
    HeterogeneousRetrievalError,
    HeterogeneousRetrievalRequest,
    NarrativeBundle,
    RetrievalFilters,
    RetrievalRanker,
    SemanticCandidate,
    SemanticSearchReceipt,
    _lexical_candidates,
    _rank_candidates,
    _require_recomputable_semantic_receipt,
    _verify_candidate_source,
    audit_research_snapshot_for_retrieval,
    retrieve_heterogeneous,
    verify_heterogeneous_retrieval_trace,
)
from search.local_vector import vector_records_digest, vector_sha256
from tests.test_canonical_fact_resolution import (
    NOW,
    SCOPE,
    _component,
    _mapping,
    _persist_taxonomy_assertion,
    _resolution_database,
)
from tests.test_filing_xbrl_extraction_ledger import _entry, _output

ROOT = Path(__file__).resolve().parents[1]


def _trace_tables(
    conn: sqlite3.Connection,
    *,
    population_columns: bool,
) -> None:
    population_sql = (
        ",population_run_id TEXT,population_receipt_set_sha256 TEXT,"
        "population_observed_through TEXT"
        if population_columns
        else ""
    )
    conn.executescript(
        f"""
        CREATE TABLE heterogeneous_retrieval_trace_headers (
            trace_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            research_snapshot_id TEXT,
            research_snapshot_sha256 TEXT,
            fact_generation_id TEXT,
            fact_projection_seal_sha256 TEXT,
            narrative_commitments_json TEXT,
            narrative_commitments_sha256 TEXT,
            semantic_receipts_json TEXT,
            semantic_receipts_sha256 TEXT,
            query_sha256 TEXT,
            query_json TEXT,
            ranker_json TEXT,
            ranker_sha256 TEXT,
            filters_json TEXT,
            filters_sha256 TEXT,
            candidate_limit INTEGER,
            result_limit INTEGER,
            cutoff_at TEXT,
            recorded_at TEXT
            {population_sql}
        );
        CREATE TABLE heterogeneous_retrieval_trace_candidates (
            trace_id TEXT,
            candidate_ordinal INTEGER,
            candidate_kind TEXT,
            candidate_id TEXT,
            source_commitment_sha256 TEXT,
            lexical_score TEXT,
            semantic_score TEXT,
            normalized_score TEXT,
            ranker_name TEXT,
            filter_outcome TEXT,
            filter_reason TEXT,
            evidence_locator_json TEXT,
            lineage_json TEXT,
            lineage_sha256 TEXT,
            candidate_json TEXT,
            candidate_sha256 TEXT
        );
        CREATE TABLE heterogeneous_retrieval_trace_results (
            trace_id TEXT,
            result_ordinal INTEGER,
            candidate_ordinal INTEGER,
            final_score TEXT,
            result_json TEXT,
            result_sha256 TEXT
        );
        CREATE TABLE heterogeneous_retrieval_trace_seals (
            trace_id TEXT,
            candidate_count INTEGER,
            result_count INTEGER,
            canonical_candidate_set_json TEXT,
            candidate_set_sha256 TEXT,
            canonical_result_set_json TEXT,
            result_set_sha256 TEXT,
            trace_json TEXT,
            trace_sha256 TEXT,
            sealed_at TEXT
        );
        """
    )


def _semantic_runtime_artifact(
    model: str = "deterministic-two-dimensional",
) -> EmbeddingRuntimeArtifact:
    return EmbeddingRuntimeArtifact(
        provider="local-test",
        model=model,
        dimensions=2,
        execution_provider="CPUExecutionProvider",
        execution_settings=(),
        component_versions=(RuntimeComponentVersion(component="test-runtime", version="1"),),
        files=(
            RuntimeArtifactFile(
                logical_name="model.bin",
                role="model",
                size_bytes=1,
                sha256="9" * 64,
            ),
        ),
    )


def _insert_legacy_promotion(
    conn: sqlite3.Connection,
    promotion: EmbeddingPromotion,
) -> None:
    """Seed one historical pre-0257 promotion without weakening the live writer."""

    values: dict[str, object] = {
        "promotion_id": promotion.promotion_id,
        "idempotency_key": promotion.idempotency_key,
        "purpose": promotion.purpose,
        "revision": promotion.revision,
        "provider": promotion.provider,
        "model": promotion.model,
        "dimensions": promotion.dimensions,
        "golden_sha256": promotion.golden_sha256,
        "evaluation_artifact_sha256": promotion.evaluation_artifact_sha256,
        "evaluation_metrics_json": promotion.evaluation_metrics_json,
        "runtime_artifact_json": promotion.runtime_artifact_json,
        "runtime_artifact_sha256": promotion.runtime_artifact_sha256,
        "approved_by": promotion.approved_by,
        "approved_at": promotion.approved_at,
        "supersedes_promotion_id": promotion.supersedes_promotion_id,
        "knowledge_at": promotion.knowledge_at or promotion.approved_at,
        "recorded_at": promotion.recorded_at or promotion.approved_at,
    }
    schema_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(search_embedding_model_promotions)")
    }
    columns = tuple(name for name in values if name in schema_columns)
    conn.execute(
        "INSERT INTO search_embedding_model_promotions "  # nosec B608 -- test-only fixed legacy column allowlist
        f"({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        tuple(values[name] for name in columns),
    )


def test_semantic_receipt_is_exact_and_tamper_evident() -> None:
    candidates = (SemanticCandidate(chunk_id="chunk-1", score="0.75"),)
    candidate_json = canonical_json([candidate.model_dump(mode="json") for candidate in candidates])
    backend_json = canonical_json(
        {
            "backend": "exact-vector-service",
            "request_id": "request-1",
        }
    )
    receipt = SemanticSearchReceipt(
        query_sha256=digest_text("revenue"),
        vector_index_run_id="vector-1",
        embedding_promotion_id="promotion-1",
        algorithm="exact_cosine",
        algorithm_version="1",
        reproducibility="exact",
        promotion_eval_sha256="a" * 64,
        candidates=candidates,
        ordered_candidate_set_sha256=digest_text(candidate_json),
        backend_receipt_json=backend_json,
        backend_receipt_sha256=digest_text(backend_json),
    )
    assert receipt.candidates[0].score == "0.75"
    with pytest.raises(ValidationError):
        receipt.model_copy(update={"ordered_candidate_set_sha256": "b" * 64}).model_validate(
            {
                **receipt.model_dump(mode="json"),
                "ordered_candidate_set_sha256": "b" * 64,
            }
        )


def test_mixed_fact_and_narrative_ranking_is_deterministic() -> None:
    request = HeterogeneousRetrievalRequest.model_construct(
        trace_id="trace-1",
        idempotency_key="trace-1",
        research_snapshot_id="research-1",
        fact_generation_id="generation-1",
        narrative_bundles=(),
        query_text=("Revenue growth in 2024 versus 2023 and management's explanation"),
        candidate_limit=10,
        result_limit=10,
        ranker=RetrievalRanker(),
        filters=RetrievalFilters(
            include_narrative=True,
            include_facts=True,
        ),
        cutoff_at="2026-01-01T00:00:00Z",
        recorded_at="2026-01-01T00:00:00Z",
    )
    candidates: list[dict[str, object]] = [
        {
            "candidate_id": "management-explanation",
            "candidate_kind": "narrative",
            "lexical_score": "0.8",
            "semantic_score": None,
        },
        {
            "candidate_id": "revenue-2023",
            "candidate_kind": "fact",
            "lexical_score": "1",
            "semantic_score": None,
        },
        {
            "candidate_id": "revenue-2024",
            "candidate_kind": "fact",
            "lexical_score": "1",
            "semantic_score": None,
        },
    ]
    ranked = _rank_candidates(request, candidates)
    assert [item["candidate_id"] for item in ranked] == [
        "revenue-2023",
        "revenue-2024",
        "management-explanation",
    ]
    assert ranked[0]["normalized_score"] == ranked[1]["normalized_score"]


def test_candidate_union_is_ranked_before_candidate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = HeterogeneousRetrievalRequest(
        trace_id="trace-rank-before-cap",
        idempotency_key="trace-rank-before-cap",
        research_snapshot_id="research-1",
        fact_generation_id="generation-1",
        narrative_bundles=(
            NarrativeBundle(
                corpus_manifest_id="manifest-1",
                lexical_index_run_id="lexical-1",
            ),
        ),
        query_text="revenue",
        candidate_limit=1,
        result_limit=1,
        filters=RetrievalFilters(
            include_narrative=True,
            include_facts=False,
        ),
        cutoff_at=NOW,
        recorded_at=NOW,
    )
    monkeypatch.setattr(
        retrieval_module,
        "_lexical_candidates",
        lambda *_args, **_kwargs: [
            {
                "candidate_id": "a-low-score",
                "candidate_kind": "narrative",
                "evidence_locator": {},
                "lexical_score": "0.1",
                "lineage": {},
                "semantic_score": None,
                "source_commitment_sha256": "a" * 64,
            },
            {
                "candidate_id": "z-high-score",
                "candidate_kind": "narrative",
                "evidence_locator": {},
                "lexical_score": "1",
                "lineage": {},
                "semantic_score": None,
                "source_commitment_sha256": "b" * 64,
            },
        ],
    )

    conn = sqlite3.connect(":memory:")
    try:
        candidates, _receipts = retrieval_module._collect_candidates(conn, request, ())
        ranked = _rank_candidates(request, candidates)

        assert [item["candidate_id"] for item in ranked] == [
            "z-high-score",
            "a-low-score",
        ]
    finally:
        conn.close()


def test_population_bound_trace_persists_v2_seal_and_conflicts_on_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    _trace_tables(conn, population_columns=True)
    request = HeterogeneousRetrievalRequest(
        trace_id="trace:population",
        idempotency_key="trace:population",
        research_snapshot_id="research:population",
        fact_generation_id="facts:population",
        narrative_bundles=(),
        query_text="Revenue",
        candidate_limit=10,
        result_limit=10,
        filters=RetrievalFilters(
            include_narrative=False,
            include_facts=True,
        ),
        cutoff_at=NOW,
        recorded_at=NOW,
        population_run_id="population-run:" + "a" * 64,
        population_receipt_set_sha256="b" * 64,
        population_observed_through=NOW,
    )
    monkeypatch.setattr(
        retrieval_module,
        "_verify_research_coordinates",
        lambda _conn, _request: {
            "fact_projection_seal_sha256": "c" * 64,
            "narrative": [],
            "research_snapshot_sha256": "d" * 64,
        },
    )
    monkeypatch.setattr(
        retrieval_module,
        "_collect_candidates",
        lambda _conn, _request, _runtimes, _local_runtime: ([], []),
    )
    monkeypatch.setattr(
        retrieval_module,
        "verify_heterogeneous_retrieval_trace",
        lambda *_args, **_kwargs: "verified",
    )
    try:
        assert retrieve_heterogeneous(conn, request) == "verified"
        header = conn.execute(
            "SELECT population_run_id,population_receipt_set_sha256,"
            "population_observed_through "
            "FROM heterogeneous_retrieval_trace_headers"
        ).fetchone()
        assert header == (
            request.population_run_id,
            request.population_receipt_set_sha256,
            retrieval_module.db_time(request.population_observed_through),
        )
        trace_payload = json.loads(
            str(
                conn.execute(
                    "SELECT trace_json FROM heterogeneous_retrieval_trace_seals"
                ).fetchone()[0]
            )
        )
        assert trace_payload["trace_version"] == "heterogeneous_retrieval_trace.v2"
        assert trace_payload["population_cutover"] == {
            "observed_through": retrieval_module.canonical_time(NOW),
            "population_run_id": request.population_run_id,
            "receipt_set_sha256": request.population_receipt_set_sha256,
        }
        assert retrieve_heterogeneous(conn, request) == "verified"
        with pytest.raises(
            HeterogeneousRetrievalError,
            match="retrieval_trace_idempotency_conflict",
        ):
            retrieve_heterogeneous(
                conn,
                request.model_copy(
                    update={"population_observed_through": NOW + timedelta(seconds=1)}
                ),
            )
    finally:
        conn.close()


def test_trace_population_schema_and_receipt_verification_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = sqlite3.connect(":memory:")
    _trace_tables(legacy, population_columns=False)
    population_request = HeterogeneousRetrievalRequest(
        trace_id="trace:population",
        idempotency_key="trace:population",
        research_snapshot_id="research:population",
        fact_generation_id="facts:population",
        narrative_bundles=(),
        query_text="Revenue",
        filters=RetrievalFilters(
            include_narrative=False,
            include_facts=True,
        ),
        cutoff_at=NOW,
        recorded_at=NOW,
        population_run_id="population-run:" + "a" * 64,
        population_receipt_set_sha256="b" * 64,
        population_observed_through=NOW,
    )
    try:
        with pytest.raises(
            HeterogeneousRetrievalError,
            match="retrieval_trace_population_schema_missing",
        ):
            retrieval_module._request_population_db_values(
                population_request,
                retrieval_module._trace_population_columns(legacy),
            )
        legacy.execute(
            "ALTER TABLE heterogeneous_retrieval_trace_headers ADD COLUMN population_run_id TEXT"
        )
        with pytest.raises(
            HeterogeneousRetrievalError,
            match="retrieval_trace_population_schema_partial",
        ):
            retrieval_module._trace_population_columns(legacy)
    finally:
        legacy.close()

    class _Ledger:
        def __init__(self, _conn: sqlite3.Connection) -> None:
            pass

        def verify(self, population_run_id: str) -> SimpleNamespace:
            assert population_run_id == population_request.population_run_id
            return SimpleNamespace(
                receipt_set_sha256="e" * 64,
                temporal_scope=SimpleNamespace(
                    knowledge_cutoff=NOW,
                    observed_through=NOW,
                ),
            )

    monkeypatch.setattr(retrieval_module, "PopulationCompletenessLedger", _Ledger)
    in_memory = sqlite3.connect(":memory:")
    try:
        with pytest.raises(
            HeterogeneousRetrievalError,
            match="retrieval_trace_population_cutover_mismatch",
        ):
            retrieval_module._verify_trace_population_cutover(
                in_memory,
                population_request,
            )
    finally:
        in_memory.close()


def test_candidates_excluded_over_cap_receive_persisted_dispositions() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE heterogeneous_retrieval_trace_headers (
            trace_id TEXT PRIMARY KEY
        );
        CREATE TABLE heterogeneous_retrieval_trace_candidates (
            trace_id TEXT,
            candidate_ordinal INTEGER,
            candidate_kind TEXT,
            candidate_id TEXT,
            source_commitment_sha256 TEXT,
            lexical_score TEXT,
            semantic_score TEXT,
            normalized_score TEXT,
            ranker_name TEXT,
            filter_outcome TEXT,
            filter_reason TEXT,
            evidence_locator_json TEXT,
            lineage_json TEXT,
            lineage_sha256 TEXT,
            candidate_json TEXT,
            candidate_sha256 TEXT
        );
        CREATE TABLE heterogeneous_retrieval_trace_results (
            trace_id TEXT,
            result_ordinal INTEGER,
            candidate_ordinal INTEGER,
            final_score TEXT,
            result_json TEXT,
            result_sha256 TEXT
        );
        CREATE TABLE heterogeneous_retrieval_trace_seals (
            trace_id TEXT,
            candidate_count INTEGER,
            result_count INTEGER,
            canonical_candidate_set_json TEXT,
            candidate_set_sha256 TEXT,
            canonical_result_set_json TEXT,
            result_set_sha256 TEXT,
            trace_json TEXT,
            trace_sha256 TEXT,
            sealed_at TEXT
        );
        INSERT INTO heterogeneous_retrieval_trace_headers VALUES
            ('trace-candidate-dispositions');
        """
    )
    request = HeterogeneousRetrievalRequest.model_construct(
        trace_id="trace-candidate-dispositions",
        idempotency_key="trace-candidate-dispositions",
        research_snapshot_id="research-1",
        fact_generation_id="generation-1",
        narrative_bundles=(),
        query_text="revenue",
        candidate_limit=1,
        result_limit=1,
        ranker=RetrievalRanker(),
        filters=RetrievalFilters(
            include_narrative=False,
            include_facts=True,
        ),
        cutoff_at=NOW,
        recorded_at=NOW,
    )
    candidates = _rank_candidates(
        request,
        [
            {
                "candidate_id": "z-best",
                "candidate_kind": "fact",
                "evidence_locator": {},
                "lexical_score": "1",
                "lineage": {},
                "semantic_score": None,
                "source_commitment_sha256": "a" * 64,
            },
            {
                "candidate_id": "a-tail",
                "candidate_kind": "narrative",
                "evidence_locator": {},
                "lexical_score": "0.1",
                "lineage": {},
                "semantic_score": None,
                "source_commitment_sha256": "b" * 64,
            },
        ],
    )
    try:
        retrieval_module._write_trace(
            conn,
            request,
            candidates,
            {
                "research_snapshot_sha256": "c" * 64,
                "fact_projection_seal_sha256": "d" * 64,
                "narrative": [],
                "semantic_receipts_sha256": "e" * 64,
            },
        )

        assert conn.execute(
            "SELECT candidate_id,filter_outcome,filter_reason "
            "FROM heterogeneous_retrieval_trace_candidates "
            "ORDER BY candidate_ordinal"
        ).fetchall() == [
            ("z-best", "included", None),
            ("a-tail", "filtered", "candidate_limit_exceeded"),
        ]
    finally:
        conn.close()


class _ExactFakeEncoder:
    def __init__(self, expected_query: str = "revenue") -> None:
        self._expected_query = expected_query

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("exact retrieval must not encode passages")

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        assert texts == [self._expected_query]
        return [[1.0, 0.0]]


class _ExactFakeIndex:
    def __init__(
        self,
        rows: list[dict[str, object]],
        storage_uri: str,
        *,
        index_run_id: str = "vector-1",
    ) -> None:
        self.rows = rows
        self.storage_uri = storage_uri
        self.index_run_id = index_run_id

    def read_projection(self, index_run_id: str, *, expected_count: int) -> list[dict[str, object]]:
        assert index_run_id == self.index_run_id
        assert expected_count == len(self.rows)
        return [dict(row) for row in self.rows]

    def published_storage_uri(self, index_run_id: str) -> str:
        assert index_run_id == self.index_run_id
        return self.storage_uri


def _exact_vector_record(chunk_id: str, vector: list[float]) -> dict[str, object]:
    input_sha256 = digest_text(f"text:{chunk_id}")
    return {
        "chunk_id": chunk_id,
        "vector": vector,
        "vector_sha256": vector_sha256(vector, dimensions=2),
        "dimensions": 2,
        "input_sha256": input_sha256,
        "manifest_id": "manifest-1",
        "issuer_id": "issuer-1",
        "recorded_issuer_id": "issuer-1",
        "ticker": "ACME",
        "form_type": "10-K",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "node_kind": "paragraph",
        "available_at": NOW.isoformat(),
        "observed_at": NOW.isoformat(),
        "retrieved_at": NOW.isoformat(),
        "latency_ms": 1,
        "embedding_started_at": NOW.isoformat(),
        "embedding_completed_at": NOW.isoformat(),
        "runtime_artifact_sha256": _semantic_runtime_artifact().sha256(),
    }


def _exact_semantic_runtime(
    *,
    exact_row_cap: int = 10,
    storage_uri: str = "fake://published/vector-1",
) -> tuple[sqlite3.Connection, _ExactFakeIndex, ExactSemanticRuntime]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE search_embedding_model_promotions (
            promotion_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE NOT NULL,
            purpose TEXT NOT NULL,
            revision INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            golden_sha256 TEXT NOT NULL,
            evaluation_artifact_sha256 TEXT NOT NULL,
            evaluation_metrics_json TEXT NOT NULL,
            runtime_artifact_json TEXT,
            runtime_artifact_sha256 TEXT,
            approved_by TEXT NOT NULL,
            approved_at DATETIME NOT NULL,
            supersedes_promotion_id TEXT,
            knowledge_at DATETIME NOT NULL,
            recorded_at DATETIME NOT NULL
        );
        CREATE VIEW v_search_embedding_model_promotion_current AS
        SELECT promotion.* FROM search_embedding_model_promotions AS promotion
        WHERE NOT EXISTS (
            SELECT 1 FROM search_embedding_model_promotions AS newer
            WHERE newer.purpose = promotion.purpose
            AND newer.revision > promotion.revision
        );
        CREATE TABLE search_index_runs (
            index_run_id TEXT PRIMARY KEY,
            manifest_id TEXT NOT NULL,
            index_kind TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            outcome TEXT NOT NULL
        );
        CREATE TABLE search_chunks (
            chunk_id TEXT PRIMARY KEY,
            manifest_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            evidence_node_id TEXT NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL
        );
        CREATE TABLE evidence_extraction_runs (
            extraction_run_id TEXT PRIMARY KEY,
            document_version_id TEXT NOT NULL
        );
        CREATE TABLE evidence_nodes (
            node_id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL
        );
        CREATE VIEW v_evidence_document_versions_canonical AS
        SELECT 'document-1' AS document_version_id,
               'issuer-1' AS issuer_id,
               'reporting-1' AS reporting_entity_id;
        CREATE TABLE search_embedding_artifacts (
            index_run_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            vector_sha256 TEXT,
            storage_uri TEXT,
            input_sha256 TEXT NOT NULL,
            request_config_sha256 TEXT NOT NULL,
            outcome TEXT NOT NULL,
            runtime_artifact_sha256 TEXT
        );
        CREATE TABLE search_index_memberships (
            index_run_id TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            membership_status TEXT NOT NULL
        );
        CREATE TABLE search_projection_seals (
            projection_seal_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE NOT NULL,
            index_run_id TEXT UNIQUE NOT NULL,
            manifest_id TEXT NOT NULL,
            index_kind TEXT NOT NULL,
            chunk_count INTEGER NOT NULL,
            chunk_set_sha256 TEXT NOT NULL,
            projection_records_sha256 TEXT NOT NULL,
            artifact_set_sha256 TEXT,
            provider TEXT,
            model TEXT,
            dimensions INTEGER,
            runtime_artifact_sha256 TEXT,
            config_sha256 TEXT NOT NULL,
            storage_uri TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        """
    )
    runtime_artifact = _semantic_runtime_artifact()
    promotion = EmbeddingPromotion(
        promotion_id="promotion-1",
        idempotency_key="promotion-1",
        revision=1,
        provider="local-test",
        model="deterministic-two-dimensional",
        dimensions=2,
        golden_sha256="a" * 64,
        evaluation_artifact_sha256="b" * 64,
        evaluation_metrics_json="{}",
        runtime_artifact_json=runtime_artifact.canonical_json(),
        runtime_artifact_sha256=runtime_artifact.sha256(),
        approved_by="owner",
        approved_at=NOW,
    )
    _insert_legacy_promotion(conn, promotion)
    records = [
        _exact_vector_record("chunk-1", [1.0, 0.0]),
        _exact_vector_record("chunk-2", [0.0, 1.0]),
        _exact_vector_record("chunk-3", [0.5, 0.5]),
    ]
    conn.execute(
        "INSERT INTO search_index_runs VALUES (?,?,?,?,?)",
        ("vector-1", "manifest-1", "vector", "c" * 64, "succeeded"),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?)",
        ("extraction-1", "document-1"),
    )
    for record in records:
        node_id = f"node:{record['chunk_id']}"
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?)",
            (node_id, "extraction-1"),
        )
        conn.execute(
            "INSERT INTO search_chunks VALUES (?,?,?,?,?,?)",
            (
                record["chunk_id"],
                record["manifest_id"],
                record["input_sha256"],
                node_id,
                0,
                10,
            ),
        )
        conn.execute(
            "INSERT INTO search_embedding_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "vector-1",
                record["chunk_id"],
                "passage",
                promotion.provider,
                promotion.model,
                promotion.dimensions,
                record["vector_sha256"],
                "fake://published/vector-1",
                record["input_sha256"],
                "c" * 64,
                "succeeded",
                runtime_artifact.sha256(),
            ),
        )
        conn.execute(
            "INSERT INTO search_index_memberships VALUES (?,?,?)",
            ("vector-1", record["chunk_id"], "included"),
        )
    chunk_count, chunk_sha256 = manifest_chunk_commitment(conn, manifest_id="manifest-1")
    artifact_count, artifact_sha256 = vector_artifact_commitment(conn, index_run_id="vector-1")
    assert artifact_count == chunk_count == len(records)
    persist_projection_seal(
        conn,
        SearchProjectionSeal(
            projection_seal_id="seal-1",
            idempotency_key="seal-1",
            index_run_id="vector-1",
            manifest_id="manifest-1",
            index_kind="vector",
            chunk_count=chunk_count,
            chunk_set_sha256=chunk_sha256,
            projection_records_sha256=vector_records_digest(records),
            artifact_set_sha256=artifact_sha256,
            provider=promotion.provider,
            model=promotion.model,
            dimensions=promotion.dimensions,
            runtime_artifact_sha256=runtime_artifact.sha256(),
            config_sha256="c" * 64,
            storage_uri=storage_uri,
            sealed_at=NOW,
        ),
    )
    index = _ExactFakeIndex(records, storage_uri)
    runtime = ExactSemanticRuntime.from_verified_components_for_test(
        conn,
        vector_index_run_id="vector-1",
        embedding_promotion_id="promotion-1",
        index=index,
        encoder=_ExactFakeEncoder(),
        exact_row_cap=exact_row_cap,
    )
    return conn, index, runtime


def test_production_semantic_constructor_uses_verified_local_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_root = tmp_path / "indexes"
    run_path = index_root / ("run-" + hashlib.sha256(b"vector-1").hexdigest())
    run_path.mkdir(parents=True)
    storage_uri = f"lance://{run_path.resolve().as_posix()}#evidence_chunks"
    conn, index, _test_runtime = _exact_semantic_runtime(storage_uri=storage_uri)
    try:
        monkeypatch.setattr(
            exact_semantic_module,
            "LanceVectorIndex",
            lambda _root: index,
        )
        monkeypatch.setattr(
            exact_semantic_module.FastEmbedEncoder,
            "from_spec",
            lambda *_args, **_kwargs: _ExactFakeEncoder(),
        )
        runtime = retrieval_module._semantic_runtime(
            conn,
            NarrativeBundle(
                corpus_manifest_id="manifest-1",
                lexical_index_run_id="lexical-1",
                vector_index_run_id="vector-1",
                embedding_promotion_id="promotion-1",
            ),
            (),
            trace_id="trace-production-runtime",
            local_vector_runtime=LocalVectorRuntimeConfig(
                index_root=index_root,
                runtime_root=tmp_path / "runtime",
            ),
        )

        assert runtime.search("revenue", limit=1).candidates[0].chunk_id == "chunk-1"
    finally:
        conn.close()


def test_production_semantic_constructor_fails_without_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EVIDENCE_VECTOR_RUNTIME_ENABLED", raising=False)
    conn, _index, _runtime = _exact_semantic_runtime()
    try:
        with pytest.raises(
            HeterogeneousRetrievalError,
            match="semantic_local_runtime_unavailable",
        ):
            retrieval_module._semantic_runtime(
                conn,
                NarrativeBundle(
                    corpus_manifest_id="manifest-1",
                    lexical_index_run_id="lexical-1",
                    vector_index_run_id="vector-1",
                    embedding_promotion_id="promotion-1",
                ),
                (),
                trace_id="trace-no-runtime",
            )
    finally:
        conn.close()


def _semantic_receipt(runtime: ExactSemanticRuntime) -> SemanticSearchReceipt:
    evidence = runtime.search("revenue", limit=2)
    candidates = tuple(
        SemanticCandidate(chunk_id=item.chunk_id, score=item.score) for item in evidence.candidates
    )
    candidate_json = canonical_json([candidate.model_dump(mode="json") for candidate in candidates])
    backend_json = backend_receipt_json(evidence)
    return SemanticSearchReceipt(
        query_sha256=digest_text("revenue"),
        vector_index_run_id="vector-1",
        embedding_promotion_id="promotion-1",
        algorithm="exact_cosine",
        algorithm_version=EXACT_SEMANTIC_ALGORITHM_VERSION,
        reproducibility="exact",
        promotion_eval_sha256="b" * 64,
        candidates=candidates,
        ordered_candidate_set_sha256=digest_text(candidate_json),
        backend_receipt_json=backend_json,
        backend_receipt_sha256=digest_text(backend_json),
    )


def test_nonempty_semantic_receipt_recomputes_exact_complete_top_k() -> None:
    conn, _index, runtime = _exact_semantic_runtime()
    try:
        receipt = _semantic_receipt(runtime)
        assert [candidate.chunk_id for candidate in receipt.candidates] == [
            "chunk-1",
            "chunk-3",
        ]
        assert receipt.candidates[0].score == "1"
        _require_recomputable_semantic_receipt(
            receipt,
            conn=conn,
            query_text="revenue",
            bundle=NarrativeBundle(
                corpus_manifest_id="manifest-1",
                lexical_index_run_id="lexical-1",
                vector_index_run_id="vector-1",
                embedding_promotion_id="promotion-1",
            ),
            limit=2,
            trace_id="trace-1",
            runtime=runtime,
        )
    finally:
        conn.close()


def test_exact_semantic_candidates_enter_the_heterogeneous_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, _index, runtime = _exact_semantic_runtime()
    try:
        monkeypatch.setattr(
            retrieval_module,
            "_lexical_candidates",
            lambda *_args, **_kwargs: [],
        )
        request = HeterogeneousRetrievalRequest(
            trace_id="trace-semantic-pool",
            idempotency_key="trace-semantic-pool",
            research_snapshot_id="research-1",
            fact_generation_id="facts-1",
            narrative_bundles=(
                NarrativeBundle(
                    corpus_manifest_id="manifest-1",
                    lexical_index_run_id="lexical-1",
                    vector_index_run_id="vector-1",
                    embedding_promotion_id="promotion-1",
                ),
            ),
            query_text="revenue",
            candidate_limit=4,
            result_limit=2,
            filters=RetrievalFilters(
                include_narrative=True,
                include_facts=False,
            ),
            cutoff_at=NOW,
            recorded_at=NOW,
        )
        candidates, receipts = retrieval_module._collect_candidates(conn, request, (runtime,))
        assert [candidate["candidate_id"] for candidate in candidates] == [
            "chunk-1",
            "chunk-3",
        ]
        assert all(candidate["semantic_score"] is not None for candidate in candidates)
        assert len(receipts) == 1
        assert len(receipts[0]["candidates"]) == 2
    finally:
        conn.close()


def test_exact_runtime_rejects_superseded_promotion() -> None:
    conn, _index, runtime = _exact_semantic_runtime()
    try:
        _insert_legacy_promotion(
            conn,
            EmbeddingPromotion(
                promotion_id="promotion-2",
                idempotency_key="promotion-2",
                revision=2,
                provider="local-test",
                model="different-two-dimensional-model",
                dimensions=2,
                golden_sha256="a" * 64,
                evaluation_artifact_sha256="f" * 64,
                evaluation_metrics_json="{}",
                runtime_artifact_json=_semantic_runtime_artifact(
                    "different-two-dimensional-model"
                ).canonical_json(),
                runtime_artifact_sha256=_semantic_runtime_artifact(
                    "different-two-dimensional-model"
                ).sha256(),
                approved_by="owner",
                approved_at=NOW,
                supersedes_promotion_id="promotion-1",
            ),
        )
        with pytest.raises(ExactSemanticError, match="current ledger promotion"):
            runtime.search("revenue", limit=2)
    finally:
        conn.close()


def test_exact_runtime_rejects_historical_unbound_coordinates() -> None:
    conn, index, _runtime = _exact_semantic_runtime()
    try:
        conn.execute(
            "UPDATE search_embedding_model_promotions "
            "SET runtime_artifact_json=NULL,runtime_artifact_sha256=NULL "
            "WHERE promotion_id='promotion-1'"
        )
        conn.execute(
            "UPDATE search_projection_seals SET runtime_artifact_sha256=NULL "
            "WHERE projection_seal_id='seal-1'"
        )
        conn.execute(
            "UPDATE search_embedding_artifacts SET runtime_artifact_sha256=NULL "
            "WHERE index_run_id='vector-1'"
        )
        with pytest.raises(ExactSemanticError, match="identities differ"):
            ExactSemanticRuntime.from_verified_components_for_test(
                conn,
                vector_index_run_id="vector-1",
                embedding_promotion_id="promotion-1",
                index=index,
                encoder=_ExactFakeEncoder(),
                exact_row_cap=10,
            )
    finally:
        conn.close()


def test_semantic_receipt_fails_closed_on_score_order_vector_query_and_cap() -> None:
    conn, index, runtime = _exact_semantic_runtime()
    try:
        receipt = _semantic_receipt(runtime)
        base = receipt.model_dump(mode="json")

        wrong_score = list(base["candidates"])
        wrong_score[0] = {**wrong_score[0], "score": "0.9"}
        wrong_score_json = canonical_json(wrong_score)
        tampered_score = SemanticSearchReceipt.model_validate(
            {
                **base,
                "candidates": wrong_score,
                "ordered_candidate_set_sha256": digest_text(wrong_score_json),
            }
        )
        with pytest.raises(
            HeterogeneousRetrievalError,
            match="semantic_receipt_exact_recomputation_failed",
        ):
            _require_recomputable_semantic_receipt(
                tampered_score,
                conn=conn,
                query_text="revenue",
                bundle=NarrativeBundle(
                    corpus_manifest_id="manifest-1",
                    lexical_index_run_id="lexical-1",
                    vector_index_run_id="vector-1",
                    embedding_promotion_id="promotion-1",
                ),
                limit=2,
                trace_id="trace-score",
                runtime=runtime,
            )

        reordered = list(reversed(base["candidates"]))
        reordered_json = canonical_json(reordered)
        tampered_order = SemanticSearchReceipt.model_validate(
            {
                **base,
                "candidates": reordered,
                "ordered_candidate_set_sha256": digest_text(reordered_json),
            }
        )
        with pytest.raises(HeterogeneousRetrievalError):
            _require_recomputable_semantic_receipt(
                tampered_order,
                conn=conn,
                query_text="revenue",
                bundle=NarrativeBundle(
                    corpus_manifest_id="manifest-1",
                    lexical_index_run_id="lexical-1",
                    vector_index_run_id="vector-1",
                    embedding_promotion_id="promotion-1",
                ),
                limit=2,
                trace_id="trace-order",
                runtime=runtime,
            )

        with pytest.raises(HeterogeneousRetrievalError):
            _require_recomputable_semantic_receipt(
                receipt,
                conn=conn,
                query_text="different query",
                bundle=NarrativeBundle(
                    corpus_manifest_id="manifest-1",
                    lexical_index_run_id="lexical-1",
                    vector_index_run_id="vector-1",
                    embedding_promotion_id="promotion-1",
                ),
                limit=2,
                trace_id="trace-query",
                runtime=runtime,
            )

        missing = list(base["candidates"][:-1])
        missing_json = canonical_json(missing)
        tampered_candidate_set = SemanticSearchReceipt.model_validate(
            {
                **base,
                "candidates": missing,
                "ordered_candidate_set_sha256": digest_text(missing_json),
            }
        )
        with pytest.raises(HeterogeneousRetrievalError):
            _require_recomputable_semantic_receipt(
                tampered_candidate_set,
                conn=conn,
                query_text="revenue",
                bundle=NarrativeBundle(
                    corpus_manifest_id="manifest-1",
                    lexical_index_run_id="lexical-1",
                    vector_index_run_id="vector-1",
                    embedding_promotion_id="promotion-1",
                ),
                limit=2,
                trace_id="trace-candidate",
                runtime=runtime,
            )

        backend = json.loads(receipt.backend_receipt_json)
        assert isinstance(backend, dict)
        for field, value in (
            ("query_vector_sha256", "d" * 64),
            ("model", "different-model"),
            ("embedding_promotion_sha256", "e" * 64),
        ):
            tampered_backend = {**backend, field: value}
            tampered_backend_json = canonical_json(tampered_backend)
            tampered_backend_receipt = SemanticSearchReceipt.model_validate(
                {
                    **base,
                    "backend_receipt_json": tampered_backend_json,
                    "backend_receipt_sha256": digest_text(tampered_backend_json),
                }
            )
            with pytest.raises(HeterogeneousRetrievalError):
                _require_recomputable_semantic_receipt(
                    tampered_backend_receipt,
                    conn=conn,
                    query_text="revenue",
                    bundle=NarrativeBundle(
                        corpus_manifest_id="manifest-1",
                        lexical_index_run_id="lexical-1",
                        vector_index_run_id="vector-1",
                        embedding_promotion_id="promotion-1",
                    ),
                    limit=2,
                    trace_id=f"trace-backend-{field}",
                    runtime=runtime,
                )

        evidence = runtime.search("revenue", limit=2)
        index.rows[0]["vector"] = [0.0, 1.0]
        with pytest.raises(ExactSemanticError):
            runtime.verify(
                evidence,
                query_text="revenue",
                limit=2,
            )
    finally:
        conn.close()

    capped_conn, _capped_index, capped_runtime = _exact_semantic_runtime(exact_row_cap=2)
    try:
        with pytest.raises(ExactSemanticError, match="row cap"):
            capped_runtime.search("revenue", limit=2)
    finally:
        capped_conn.close()


def _upgrade(path: Path, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, revision)


def _downgrade(path: Path, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.downgrade(config, revision)


def _sql_sha256(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _two_period_output() -> FilingXbrlNormalizedOutput:
    period_2023 = datetime(2023, 12, 31, tzinfo=UTC)
    period_2024 = datetime(2024, 12, 31, tzinfo=UTC)
    first = _entry(0, numeric_value=Decimal("100")).model_copy(
        update={
            "period_start": datetime(2023, 1, 1, tzinfo=UTC),
            "period_end": period_2023,
            "fiscal_year": 2023,
            "effective_at": period_2023,
        }
    )
    second = _entry(1, numeric_value=Decimal("120")).model_copy(
        update={
            "period_start": datetime(2024, 1, 1, tzinfo=UTC),
            "period_end": period_2024,
            "fiscal_year": 2024,
            "effective_at": period_2024,
        }
    )
    return _output((first, second))


def _seed_resolved_periods(
    conn: sqlite3.Connection,
) -> tuple[dict[int, BindingRevision], tuple[str, ...]]:
    ontology = MetricOntology(conn)
    ontology.persist_metric(
        CanonicalMetric(
            metric_id="revenue",
            idempotency_key="metric:revenue",
            canonical_name="Revenue",
            effective_at=NOW,
            knowledge_at=NOW,
            recorded_at=NOW,
        )
    )
    ontology.persist_metric_definition(
        CanonicalMetricDefinitionRevision(
            metric_definition_revision_id="metric:revenue:v1",
            idempotency_key="metric:revenue:v1",
            metric_id="revenue",
            revision=1,
            lifecycle="active",
            definition_text="Revenue recognized from customer contracts.",
            aliases=("sales", "top line"),
            value_kind="numeric",
            period_kind="duration",
            unit_family="currency",
            accounting_basis="us_gaap",
            scope_constraints={},
            effective_at=NOW,
            knowledge_at=NOW,
            recorded_at=NOW,
        )
    )
    rows = conn.execute(
        "SELECT cell.fact_cell_id,cell.concept_name,cell.period_start,"
        "cell.period_end,observation.observation_id "
        "FROM fact_cells_v2 cell JOIN fact_observations_v2 observation "
        "ON observation.fact_cell_id=cell.fact_cell_id "
        "JOIN filing_xbrl_extraction_dispositions disposition "
        "ON disposition.observation_id=observation.observation_id "
        "WHERE disposition.disposition='published' ORDER BY cell.period_end"
    ).fetchall()
    assert len(rows) == 2
    component = _component("Revenue")
    mapping = _mapping(component)
    ontology.persist_source_component(component)
    ontology.persist_mapping(mapping)
    bindings: dict[int, BindingRevision] = {}
    canonical_cells: list[str] = []
    for ordinal, row in enumerate(rows):
        year = datetime.fromisoformat(str(row[3])).year
        canonical_cell_id = f"canonical:revenue:{year}"
        _persist_taxonomy_assertion(
            conn,
            str(row[4]),
            str(row[0]),
            idempotency_key=f"taxonomy:{year}",
        )
        ontology.persist_canonical_metric_cell(
            CanonicalMetricCell(
                canonical_metric_cell_id=canonical_cell_id,
                idempotency_key=canonical_cell_id,
                metric_id="revenue",
                reporting_entity_id="reporting-1",
                period_kind="duration",
                period_start=datetime.fromisoformat(str(row[2])),
                period_end=datetime.fromisoformat(str(row[3])),
                unit_family="currency",
                accounting_basis="us_gaap",
                consolidation_scope="consolidated",
                effective_at=NOW,
                knowledge_at=NOW,
                recorded_at=NOW,
            )
        )
        binding = BindingRevision(
            binding_revision_id=f"binding:revenue:{year}:v1",
            idempotency_key=f"binding:revenue:{year}:v1",
            fact_cell_id=str(row[0]),
            source_observation_id=str(row[4]),
            revision=1,
            canonical_metric_cell_id=canonical_cell_id,
            mapping_revision_id=mapping.mapping_revision_id,
            source_component_id=component.component_id,
            effective_at=NOW,
            knowledge_at=NOW,
            recorded_at=NOW,
        )
        ontology.persist_binding(binding)
        bindings[year] = binding
        canonical_cells.append(canonical_cell_id)
    resolver = CanonicalFactResolutionEngine(conn)
    for canonical_cell_id in canonical_cells:
        result = resolver.resolve(
            canonical_cell_id,
            NOW,
            ResolutionPolicy(name="deterministic", version="v1", config={}),
            recorded_at=NOW,
        )
        assert result.status == "resolved"
    ontology.seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="ontology:checkpoint",
            idempotency_key="ontology:checkpoint",
            cutoff_at=NOW,
            recorded_at=NOW,
        )
    )
    resolver.seal_snapshot("resolution:checkpoint", NOW, NOW, SCOPE)
    return bindings, tuple(canonical_cells)


def _seed_management_narrative(conn: sqlite3.Connection) -> None:
    text = (
        "Management explained that revenue growth accelerated in 2024 because "
        "customer demand and pricing improved."
    )
    blob_sha = digest_text(text)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob_sha,
            byte_size=len(text.encode()),
            media_type="text/plain",
            storage_uri="file:///management-presentation.txt",
            recorded_at=NOW,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="management-observation",
            idempotency_key="management-observation",
            source_kind="issuer",
            source_url="https://issuer.test/investor-presentation",
            blob_sha256=blob_sha,
            source_published_at=NOW,
            filing_at=None,
            accepted_at=None,
            observed_at=NOW,
            retrieved_at=NOW,
            retrieval_config_sha256="a" * 64,
            collector_code_version="collector@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="management-document",
            document_key="ACME:investor-presentation:2024",
            version_sequence=1,
            observation_id="management-observation",
            blob_sha256=blob_sha,
            issuer_id="issuer-1",
            ticker="ACME",
            document_type="investor_presentation",
            form_type="presentation",
            language="en",
            recorded_at=NOW,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id="management-extraction",
            idempotency_key="management-extraction",
            document_version_id="management-document",
            input_sha256=blob_sha,
            extractor_name="parser",
            extractor_config_sha256="b" * 64,
            extractor_code_version="parser@1",
            output_sha256="c" * 64,
            started_at=NOW,
            completed_at=NOW,
            outcome="succeeded",
        )
    )
    ledger.persist(
        EvidenceNode(
            node_id="management-node",
            evidence_key="ACME:management-node",
            revision=1,
            extraction_run_id="management-extraction",
            node_kind="passage",
            text=text,
            recorded_at=NOW,
        )
    )
    conn.commit()


def _processing_snapshot(conn: sqlite3.Connection) -> str:
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "management-obligation:v1",
            "management-obligation:v1",
            "management-obligation",
            1,
            "issuer-1",
            "reporting-1",
            "issuer_publisher",
            "issuer_presentations",
            "required",
            "publisher_surface_exhaustion",
            NOW - timedelta(days=1),
            None,
            "deterministic",
            "test",
            "{}",
            NOW,
            NOW,
            NOW,
            None,
        ),
    )
    scope = DocumentProcessingScope(document_version_ids=("management-document",))
    policy = DocumentProcessingPolicy(policy_name="test", policy_version="v1")
    obligations = derive_obligations(conn, scope, NOW, policy)
    assert obligations
    for ordinal, obligation in enumerate(obligations):
        assert obligation.applicability == "not_applicable"
        disposition_id = f"management-disposition:{ordinal}"
        record_disposition(
            conn,
            DocumentProcessingDisposition(
                processing_disposition_id=disposition_id,
                idempotency_key=disposition_id,
                processing_obligation_revision_id=(obligation.processing_obligation_revision_id),
                terminal_status="not_applicable",
                reason_code="test",
                reason_details={"ordinal": ordinal},
                knowledge_at=NOW,
                recorded_at=NOW,
            ),
        )
        seal_disposition(conn, disposition_id, sealed_at=NOW)
    snapshot_id = "processing:management"
    seal_processing_snapshot(
        conn,
        processing_snapshot_id=snapshot_id,
        idempotency_key=snapshot_id,
        scope=scope,
        cutoff_at=NOW,
        policy=policy,
        recorded_at=NOW,
    )
    return snapshot_id


def _bind_management_manifest_source_obligation(
    conn: sqlite3.Connection,
    *,
    manifest_id: str,
) -> None:
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "management-inventory",
            "management-inventory",
            "management-inventory",
            1,
            "issuer-1",
            "ACME",
            "ir_crawl",
            "https://example.test/management",
            "management-observation",
            "succeeded",
            True,
            "a" * 64,
            "test",
            NOW,
            NOW,
            NOW,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "management-expected-document",
            "management-expected-document",
            "management-inventory",
            "management-presentation",
            "issuer-1",
            "ACME",
            "ir_document",
            "investor_presentation",
            "presentation",
            None,
            "https://example.test/management",
            None,
            None,
            None,
            None,
            NOW,
            "authoritative",
            NOW,
        ),
    )
    conn.execute(
        "INSERT INTO source_inventory_components VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "management-inventory-component",
            "management-inventory-component",
            "management-inventory",
            "primary",
            "primary",
            "https://example.test/management",
            "management-observation",
            "succeeded",
            True,
            None,
            0,
            NOW,
        ),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshot_seals VALUES (?,?,?,?,?)",
        ("management-inventory", 1, "b" * 64, "complete", NOW),
    )
    binding_json = canonical_json(
        {
            "document_family": "issuer_presentations",
            "expected_document_id": "management-expected-document",
            "issuer_id": "issuer-1",
            "reporting_entity_id": "reporting-1",
            "source_obligation_revision_id": "management-obligation:v1",
        }
    )
    conn.execute(
        "INSERT INTO expected_document_obligation_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "management-expected-binding",
            "management-expected-binding",
            "management-expected-document",
            "management-obligation:v1",
            "issuer-1",
            "reporting-1",
            "issuer_presentations",
            binding_json,
            digest_text(binding_json),
            NOW,
            NOW,
            NOW,
        ),
    )
    conn.execute(
        "INSERT INTO search_manifest_source_inventories VALUES (?,?,?)",
        (manifest_id, "management-inventory", NOW),
    )


def _seed_management_semantic_projection(
    conn: sqlite3.Connection,
    *,
    manifest_id: str,
    lexical_index_run_id: str,
) -> tuple[NarrativeBundle, ExactSemanticRuntime]:
    runtime_artifact = _semantic_runtime_artifact()
    promotion = EmbeddingPromotion(
        promotion_id="promotion:management",
        idempotency_key="promotion:management",
        revision=1,
        provider="local-test",
        model="deterministic-two-dimensional",
        dimensions=2,
        golden_sha256="a" * 64,
        evaluation_artifact_sha256="b" * 64,
        evaluation_metrics_json="{}",
        runtime_artifact_json=runtime_artifact.canonical_json(),
        runtime_artifact_sha256=runtime_artifact.sha256(),
        approved_by="owner",
        approved_at=NOW,
        knowledge_at=NOW,
        recorded_at=NOW,
    )
    _insert_legacy_promotion(conn, promotion)
    vector_run_id = "vector:management"
    conn.execute(
        "INSERT INTO search_index_runs ("
        "index_run_id,idempotency_key,index_key,revision,manifest_id,index_kind,"
        "config_sha256,code_version,outcome,failure_reason,started_at,completed_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            vector_run_id,
            vector_run_id,
            vector_run_id,
            1,
            manifest_id,
            "vector",
            "c" * 64,
            "test",
            "succeeded",
            None,
            NOW,
            NOW,
        ),
    )
    chunk = conn.execute(
        "SELECT chunk_id,content_sha256 FROM search_chunks WHERE manifest_id=? ORDER BY chunk_id",
        (manifest_id,),
    ).fetchone()
    assert chunk is not None
    chunk_id, input_sha256 = str(chunk[0]), str(chunk[1])
    vector = [1.0, 0.0]
    vector_digest = vector_sha256(vector, dimensions=2)
    storage_uri = "fake://published/vector-management"
    conn.execute(
        "INSERT INTO search_embedding_artifacts ("
        "embedding_artifact_id,idempotency_key,index_run_id,chunk_id,purpose,"
        "provider,model,dimensions,vector_sha256,storage_uri,input_sha256,"
        "request_config_sha256,outcome,failure_reason,cost_usd,latency_ms,"
        "started_at,completed_at,runtime_artifact_sha256"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "artifact:management",
            "artifact:management",
            vector_run_id,
            chunk_id,
            "passage",
            promotion.provider,
            promotion.model,
            promotion.dimensions,
            vector_digest,
            storage_uri,
            input_sha256,
            "c" * 64,
            "succeeded",
            None,
            None,
            1,
            NOW,
            NOW,
            runtime_artifact.sha256(),
        ),
    )
    conn.execute(
        "INSERT INTO search_index_memberships "
        "(index_run_id,chunk_id,membership_status,failure_reason,recorded_at) "
        "VALUES (?,?,?,?,?)",
        (vector_run_id, chunk_id, "included", None, NOW),
    )
    record = _exact_vector_record(chunk_id, vector)
    record.update(
        {
            "input_sha256": input_sha256,
            "manifest_id": manifest_id,
            "runtime_artifact_sha256": runtime_artifact.sha256(),
        }
    )
    records = [record]
    chunk_count, chunk_sha256 = manifest_chunk_commitment(conn, manifest_id=manifest_id)
    artifact_count, artifact_sha256 = vector_artifact_commitment(conn, index_run_id=vector_run_id)
    assert artifact_count == chunk_count == 1
    persist_projection_seal(
        conn,
        SearchProjectionSeal(
            projection_seal_id="seal:management",
            idempotency_key="seal:management",
            index_run_id=vector_run_id,
            manifest_id=manifest_id,
            index_kind="vector",
            chunk_count=chunk_count,
            chunk_set_sha256=chunk_sha256,
            projection_records_sha256=vector_records_digest(records),
            artifact_set_sha256=artifact_sha256,
            provider=promotion.provider,
            model=promotion.model,
            dimensions=promotion.dimensions,
            runtime_artifact_sha256=runtime_artifact.sha256(),
            config_sha256="c" * 64,
            storage_uri=storage_uri,
            sealed_at=NOW,
        ),
    )
    bundle = NarrativeBundle(
        corpus_manifest_id=manifest_id,
        lexical_index_run_id=lexical_index_run_id,
        vector_index_run_id=vector_run_id,
        embedding_promotion_id=promotion.promotion_id,
    )
    runtime = ExactSemanticRuntime.from_verified_components_for_test(
        conn,
        vector_index_run_id=vector_run_id,
        embedding_promotion_id=promotion.promotion_id,
        index=_ExactFakeIndex(
            records,
            storage_uri,
            index_run_id=vector_run_id,
        ),
        encoder=_ExactFakeEncoder(
            "Revenue growth in 2024 versus 2023 and management's explanation"
        ),
    )
    return bundle, runtime


def test_narrative_reporting_entity_filter_applies_to_collection_and_trace() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE VIRTUAL TABLE search_lexical_chunks
        USING fts5(chunk_id UNINDEXED, text);
        CREATE TABLE search_chunks (
            chunk_id TEXT PRIMARY KEY,
            content_sha256 TEXT NOT NULL,
            evidence_node_id TEXT NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL,
            manifest_id TEXT NOT NULL
        );
        CREATE TABLE evidence_nodes (
            node_id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL
        );
        CREATE TABLE evidence_extraction_runs (
            extraction_run_id TEXT PRIMARY KEY,
            document_version_id TEXT NOT NULL
        );
        CREATE TABLE v_evidence_document_versions_canonical (
            document_version_id TEXT PRIMARY KEY,
            reporting_entity_id TEXT NOT NULL
        );
        """
    )
    for ordinal, reporting_entity_id in enumerate(
        ("reporting-1", "reporting-2"),
        start=1,
    ):
        chunk_id = f"chunk-{ordinal}"
        node_id = f"node-{ordinal}"
        run_id = f"run-{ordinal}"
        document_id = f"document-{ordinal}"
        conn.execute(
            "INSERT INTO search_lexical_chunks VALUES (?,?)",
            (chunk_id, "Revenue growth and management explanation"),
        )
        conn.execute(
            "INSERT INTO search_chunks VALUES (?,?,?,?,?,?)",
            (chunk_id, str(ordinal) * 64, node_id, 0, 41, "manifest"),
        )
        conn.execute("INSERT INTO evidence_nodes VALUES (?,?)", (node_id, run_id))
        conn.execute(
            "INSERT INTO evidence_extraction_runs VALUES (?,?)",
            (run_id, document_id),
        )
        conn.execute(
            "INSERT INTO v_evidence_document_versions_canonical VALUES (?,?)",
            (document_id, reporting_entity_id),
        )
    bundle = NarrativeBundle(
        corpus_manifest_id="manifest",
        lexical_index_run_id="lexical",
    )
    filtered = _lexical_candidates(
        conn,
        bundle,
        "Revenue",
        10,
        reporting_entity_id="reporting-1",
    )
    assert [item["candidate_id"] for item in filtered] == ["chunk-1"]
    unfiltered = _lexical_candidates(conn, bundle, "Revenue", 10)
    wrong = next(item for item in unfiltered if item["candidate_id"] == "chunk-2")
    request = HeterogeneousRetrievalRequest(
        trace_id="trace-filter",
        idempotency_key="trace-filter",
        research_snapshot_id="research",
        fact_generation_id="facts",
        narrative_bundles=(bundle,),
        query_text="Revenue",
        candidate_limit=10,
        result_limit=10,
        filters=RetrievalFilters(
            reporting_entity_id="reporting-1",
            include_narrative=True,
            include_facts=False,
        ),
        cutoff_at=NOW,
        recorded_at=NOW,
    )
    with pytest.raises(
        HeterogeneousRetrievalError,
        match="narrative_candidate_evidence_mismatch",
    ):
        _verify_candidate_source(
            conn,
            request,
            {
                "candidate_id": wrong["candidate_id"],
                "candidate_kind": "narrative",
                "evidence_locator_json": canonical_json(wrong["evidence_locator"]),
                "lexical_score": wrong["lexical_score"],
                "lineage_json": canonical_json(wrong["lineage"]),
                "semantic_score": None,
                "source_commitment_sha256": wrong["source_commitment_sha256"],
            },
            semantic_expected={},
        )
    conn.close()


def test_nonempty_checkpoint_delta_and_mixed_trace_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_upgrade = command.upgrade

    def _bounded_fixture_upgrade(config: Config, revision: str) -> None:
        real_upgrade(
            config,
            "0244_canonical_fact_resolution" if revision == "head" else revision,
        )

    monkeypatch.setattr(command, "upgrade", _bounded_fixture_upgrade)
    output = _two_period_output()
    conn = _resolution_database(tmp_path, output)
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    try:
        conn.close()
        _upgrade(path, "0246_source_fact_publication_stream")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        FilingXbrlExtractionLedger(conn).publish(output)
        bindings, canonical_cells = _seed_resolved_periods(conn)
        conn.close()
        _upgrade(path, "0252_research_universe_closure")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.create_function(
            "fact_sha256",
            1,
            _sql_sha256,
        )
        bind_resolution_snapshot_watermark(
            conn,
            resolution_snapshot_id="resolution:checkpoint",
            cutoff_at=NOW,
            recorded_at=NOW,
        )
        checkpoint_request = ProjectionGenerationRequest(
            generation_id="projection:checkpoint",
            idempotency_key="projection:checkpoint",
            generation_kind="checkpoint",
            resolution_snapshot_id="resolution:checkpoint",
            ontology_snapshot_id="ontology:checkpoint",
            cutoff_at=NOW,
            recorded_at=NOW,
            config=ProjectionConfig(max_batch_facts=1),
        )
        checkpoint = build_canonical_projection_generation(conn, checkpoint_request)
        assert checkpoint.change_count == 2
        assert checkpoint.upsert_count == 2
        assert checkpoint.tombstone_count == 0
        assert checkpoint.effective_entry_count == 2
        assert build_canonical_projection_generation(conn, checkpoint_request) == checkpoint
        checkpoint_hits = [
            hit.canonical_metric_cell_id
            for hit in search_canonical_facts(
                conn,
                generation_id=checkpoint.generation_id,
                query_text="Revenue 2024 versus 2023",
                limit=10,
            )
        ]
        assert len(checkpoint_hits) == 2
        assert set(checkpoint_hits) == set(canonical_cells)

        later = NOW + timedelta(hours=1)
        old = bindings[2023]
        MetricOntology(conn).persist_binding(
            old.model_copy(
                update={
                    "binding_revision_id": "binding:revenue:2023:v2",
                    "idempotency_key": "binding:revenue:2023:v2",
                    "revision": 2,
                    "supersedes_binding_revision_id": old.binding_revision_id,
                    "binding_status": "retired",
                    "reason_code": "superseded_reported_period",
                    "reason_details": {"test": True},
                    "effective_at": later,
                    "knowledge_at": later,
                    "recorded_at": later,
                }
            )
        )
        resolver = CanonicalFactResolutionEngine(conn)
        for canonical_cell_id in canonical_cells:
            resolver.resolve(
                canonical_cell_id,
                later,
                ResolutionPolicy(name="deterministic", version="v1", config={}),
                recorded_at=later,
            )
        MetricOntology(conn).seal_snapshot(
            OntologySnapshot(
                ontology_snapshot_id="ontology:delta",
                idempotency_key="ontology:delta",
                cutoff_at=later,
                recorded_at=later,
            )
        )
        resolver.seal_snapshot("resolution:delta", later, later, SCOPE)
        bind_resolution_snapshot_watermark(
            conn,
            resolution_snapshot_id="resolution:delta",
            cutoff_at=later,
            recorded_at=later,
        )
        delta = build_canonical_projection_generation(
            conn,
            ProjectionGenerationRequest(
                generation_id="projection:delta",
                idempotency_key="projection:delta",
                generation_kind="delta",
                parent_generation_id=checkpoint.generation_id,
                resolution_snapshot_id="resolution:delta",
                ontology_snapshot_id="ontology:delta",
                cutoff_at=later,
                recorded_at=later,
                config=ProjectionConfig(max_batch_facts=1),
            ),
        )
        assert (delta.change_count, delta.upsert_count, delta.tombstone_count) == (
            2,
            1,
            1,
        )
        assert delta.effective_entry_count == 1
        assert [
            hit.canonical_metric_cell_id
            for hit in search_canonical_facts(
                conn,
                generation_id=delta.generation_id,
                query_text="Revenue 2024 versus 2023",
                limit=10,
            )
        ] == ["canonical:revenue:2024"]

        # The K/O projection assertions above exercise the real 0259 schema.
        # The remaining mixed-retrieval fixture intentionally models a
        # historical pre-population promotion, so return to its 0255 boundary.
        conn.commit()
        conn.close()
        _downgrade(path, "0255_scoped_canonical_resolution_snapshots")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.create_function(
            "fact_sha256",
            1,
            _sql_sha256,
        )
        _seed_management_narrative(conn)
        corpus = build_grounded_search_corpus(
            conn,
            CorpusBuildRequest(
                corpus_key="management-corpus",
                revision=1,
                selector_code_version="test@1",
                recorded_at=NOW,
                knowledge_cutoff=NOW,
                expected_documents=(
                    ExpectedDocument(
                        expected_document_key="management-presentation",
                        document_version_id="management-document",
                        membership_status="included",
                        reason="required investor presentation",
                    ),
                ),
                chunker=ChunkerConfig(),
                required_extractor_names=("parser",),
                apply=True,
            ),
        )
        processing_snapshot_id = _processing_snapshot(conn)
        _bind_management_manifest_source_obligation(
            conn,
            manifest_id=corpus.manifest_id,
        )
        bundle, semantic_runtime = _seed_management_semantic_projection(
            conn,
            manifest_id=corpus.manifest_id,
            lexical_index_run_id=corpus.lexical_index_run_id,
        )
        publication_ids = tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT disposition.source_publication_id "
                "FROM canonical_fact_resolution_snapshot_members member "
                "JOIN canonical_fact_candidate_dispositions disposition "
                "ON disposition.candidate_universe_id=member.candidate_universe_id "
                "WHERE member.resolution_snapshot_id=? "
                "AND disposition.source_publication_id IS NOT NULL "
                "ORDER BY disposition.source_publication_id",
                ("resolution:checkpoint",),
            )
        )
        assert corpus.chunks_planned == 1
        assert len(_lexical_candidates(conn, bundle, "Revenue", 5)) == 1
        assert (
            len(
                _lexical_candidates(
                    conn,
                    bundle,
                    ("Revenue growth in 2024 versus 2023 and management's explanation"),
                    5,
                )
            )
            == 1
        )
        build_research_snapshot(
            conn,
            ResearchSnapshotRequest(
                research_snapshot_id="research:checkpoint",
                idempotency_key="research:checkpoint",
                research_universe=ResearchUniverse(
                    issuer_id="issuer-1",
                    reporting_entity_ids=("reporting-1",),
                    document_version_ids=("management-document",),
                    source_obligation_revision_ids=("management-obligation:v1",),
                ),
                processing_snapshot_ids=(processing_snapshot_id,),
                corpus_bundles=(
                    CorpusProjectionBundle(
                        corpus_manifest_id=corpus.manifest_id,
                        lexical_index_run_id=corpus.lexical_index_run_id,
                        vector_index_run_id=bundle.vector_index_run_id,
                        embedding_promotion_id=bundle.embedding_promotion_id,
                    ),
                ),
                source_fact_publication_ids=publication_ids,
                ontology_snapshot_id="ontology:checkpoint",
                canonical_fact_resolution_snapshot_id=("resolution:checkpoint"),
                canonical_fact_projection_run_id=checkpoint.generation_id,
                cutoff_at=NOW,
                recorded_at=NOW,
            ),
        )
        audit_research_snapshot_for_retrieval(conn, "research:checkpoint", audited_at=NOW)
        request = HeterogeneousRetrievalRequest(
            trace_id="trace:mixed",
            idempotency_key="trace:mixed",
            research_snapshot_id="research:checkpoint",
            fact_generation_id=checkpoint.generation_id,
            narrative_bundles=(bundle,),
            query_text=("Revenue growth in 2024 versus 2023 and management's explanation"),
            candidate_limit=10,
            result_limit=10,
            cutoff_at=NOW,
            recorded_at=NOW,
        )
        trace = retrieve_heterogeneous(
            conn,
            request,
            semantic_runtimes=(semantic_runtime,),
        )
        assert (
            json.loads(
                str(
                    conn.execute(
                        "SELECT trace_json FROM heterogeneous_retrieval_trace_seals "
                        "WHERE trace_id=?",
                        (trace.trace_id,),
                    ).fetchone()[0]
                )
            )["trace_version"]
            == "heterogeneous_retrieval_trace.v1"
        )
        assert trace.candidate_count >= 3
        kinds = {str(result["candidate_kind"]) for result in trace.ordered_results}
        assert kinds == {"fact", "narrative"}
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM heterogeneous_retrieval_trace_candidates "
                "WHERE trace_id=? AND semantic_score IS NOT NULL",
                (trace.trace_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            verify_heterogeneous_retrieval_trace(
                conn,
                trace.trace_id,
                semantic_runtimes=(semantic_runtime,),
            )
            == trace
        )
        with monkeypatch.context() as verifier_change:
            verifier_change.setattr(
                retrieval_module,
                "_RESEARCH_AUDITOR_CODE_SHA256",
                "f" * 64,
            )
            with pytest.raises(
                HeterogeneousRetrievalError,
                match="research_snapshot_bounded_admission_tampered",
            ):
                verify_heterogeneous_retrieval_trace(
                    conn,
                    trace.trace_id,
                    semantic_runtimes=(semantic_runtime,),
                )

        admission_receipt = conn.execute(
            "SELECT verifier_config_sha256,audit_payload_json,audit_payload_sha256 "
            "FROM research_snapshot_admission_receipts "
            "WHERE research_snapshot_id='research:checkpoint'"
        ).fetchone()
        assert admission_receipt is not None
        conn.execute("DROP TRIGGER trg_research_snapshot_admission_receipts_append_only")
        false_payload = "{}"
        conn.execute(
            "UPDATE research_snapshot_admission_receipts "
            "SET verifier_config_sha256=?,audit_payload_json=?,audit_payload_sha256=? "
            "WHERE research_snapshot_id='research:checkpoint'",
            ("f" * 64, false_payload, digest_text(false_payload)),
        )
        with pytest.raises(
            HeterogeneousRetrievalError,
            match="research_snapshot_bounded_admission_tampered",
        ):
            verify_heterogeneous_retrieval_trace(
                conn,
                trace.trace_id,
                semantic_runtimes=(semantic_runtime,),
            )
        conn.execute(
            "UPDATE research_snapshot_admission_receipts "
            "SET verifier_config_sha256=?,audit_payload_json=?,audit_payload_sha256=? "
            "WHERE research_snapshot_id='research:checkpoint'",
            tuple(admission_receipt),
        )

        with pytest.raises(
            HeterogeneousRetrievalError,
            match="retrieval_must_cover_exact_research_corpus_bundle_set",
        ):
            retrieve_heterogeneous(
                conn,
                request.model_copy(
                    update={
                        "trace_id": "trace:missing-bundle",
                        "idempotency_key": "trace:missing-bundle",
                        "narrative_bundles": (),
                        "filters": RetrievalFilters(
                            include_narrative=False,
                            include_facts=True,
                        ),
                    }
                ),
            )

        conn.execute("DROP TRIGGER trg_heterogeneous_retrieval_trace_candidates_append_only")
        conn.execute(
            "UPDATE heterogeneous_retrieval_trace_candidates "
            "SET candidate_json='{}' WHERE trace_id=? AND candidate_ordinal=0",
            (trace.trace_id,),
        )
        with pytest.raises(
            HeterogeneousRetrievalError,
            match="retrieval_candidate_commitment_tampered",
        ):
            verify_heterogeneous_retrieval_trace(
                conn,
                trace.trace_id,
                semantic_runtimes=(semantic_runtime,),
            )
    finally:
        conn.close()
