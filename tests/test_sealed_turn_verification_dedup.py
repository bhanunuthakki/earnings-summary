"""VAL-SEARCH-004 / VAL-SEARCH-008: one sealed Ask turn verifies each immutable
coordinate once and reuses one evidence block for the answer and the audit.

The fixture is deterministic and synthetic: a pytest-local SQLite ledger whose
projection seal, embedding promotion, retrieval promotion, research snapshot,
and population coordinates are defined here, an in-memory two-dimensional
vector projection, and stubbed LLM transports. No live Ask or Claude turn, no
SEC or source request, and no portfolio or bench database is used. The
deepest verifiers whose real substrate is out of scope (the population ledger,
the Research Snapshot verifier, and the bounded research-coordinate admission)
are replaced by counting doubles; everything between them and the engine
(promotion verification, cutover admission, the exact semantic runtime and its
full projection read/re-hash/ledger cross-check, trace write/verification,
evidence loading, prompt assembly, and audit packaging) is the production code.

Each turn runs twice on identical fresh fixtures: once with the in-request
verification cache and once with ``enabled=False`` (the un-deduplicated
reference). Fixture timings are not measured and no latency claim is made.

Setting ``SEARCH_VALIDATION_EVIDENCE_DIR`` records the evidence artifacts under
that directory; ordinary runs write nothing outside pytest tmp.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

import ask.engine as engine
import ask.sealed_retrieval as sealed
import search.exact_semantic as exact_semantic_module
import search.heterogeneous_retrieval as retrieval_module
from ask.audit_store import (
    AnswerAuditPackage,
    AnswerContextTurn,
    canonical_json,
    digest_text,
)
from ask.claims import required_claim_spans
from ask.context import ContextPack
from ask.engine import CLAIM_AUDIT_ADAPTER, AskTurn
from ask.sealed_retrieval import (
    RetrievalPromotion,
    RetrievalScope,
    SealedEvidenceItem,
    current_verifier_identity,
    derive_retrieval_scope_id,
)
from provenance.population_completeness import PopulationTemporalScope
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    ResearchSnapshotRequest,
    ResearchUniverse,
)
from provenance.search_index_lineage import (
    SearchProjectionSeal,
    manifest_chunk_commitment,
    persist_projection_seal,
    vector_artifact_commitment,
)
from search.canonical_fact_projection import (
    AUDIT_VERIFIER_CODE_SHA256,
    AUDIT_VERIFIER_NAME,
    AUDIT_VERIFIER_VERSION,
    admit_canonical_projection_for_read,
    digest_bucket,
    projection_audit_payload,
    search_canonical_facts,
)
from search.embedding_promotion import EmbeddingPromotion, LocalVectorRuntimeConfig
from search.embedding_runtime_artifact import (
    EmbeddingRuntimeArtifact,
    RuntimeArtifactFile,
    RuntimeComponentVersion,
)
from search.heterogeneous_retrieval import HeterogeneousRetrievalRequest, NarrativeBundle
from search.local_vector import vector_records_digest, vector_sha256
from search.request_verification import (
    NOT_VERIFIED,
    POPULATION_CUTOVER_CURRENT,
    POPULATION_RECEIPT,
    PROJECTION_GENERATION_ADMISSION,
    PROMOTION_COORDINATES,
    RESEARCH_COORDINATES,
    RETRIEVAL_TRACE,
    SEMANTIC_PROJECTION_READ,
    SEMANTIC_RUNTIME_COORDINATES,
    SEMANTIC_RUNTIME_INSTANCE,
    RequestVerificationCache,
    VerificationCacheError,
    active_connection_trace_dispatcher_count,
    active_connection_trace_observer_count,
    register_connection_trace_observer,
)
from sqlite_runtime import ManagedConnection, ManagedCursor

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)
SESSION_ID = "session-dedup"
QUESTION = "How did revenue grow"
ANSWER = "Revenue grew 12 percent on subscription demand [1]."
ISSUER_ID = "issuer-1"
REPORTING_ENTITY_ID = "reporting-1"
SOURCE_SCOPE_KEY = "ACME"
SOURCE_SCOPE_REVISION_ID = "scope-revision-ACME"
MANIFEST_ID = "manifest-1"
LEXICAL_RUN_ID = "lexical-1"
VECTOR_RUN_ID = "vector-1"
EMBEDDING_PROMOTION_ID = "embedding-promotion-1"
PROJECTION_SEAL_ID = "projection-seal-1"
MANIFEST_ID_BETA = "manifest-beta"
LEXICAL_RUN_ID_BETA = "lexical-beta"
VECTOR_RUN_ID_BETA = "vector-beta"
PROJECTION_SEAL_ID_BETA = "projection-seal-vector-beta"
RESEARCH_SNAPSHOT_ID = "research-1"
RESEARCH_SNAPSHOT_SHA256 = "d" * 64
FACT_GENERATION_ID = "facts-1"
FACT_PROJECTION_SEAL_SHA256 = "e" * 64
INVENTORY_ID = "inventory-1"
POPULATION_RUN_ID = "population-run-1"
POPULATION_RECEIPT_SHA256 = "c" * 64
CONFIG_SHA256 = "a" * 64
REQUEST_UUID = UUID(int=0x5EA1ED)

_EVIDENCE_DIR_ENV = "SEARCH_VALIDATION_EVIDENCE_DIR"
_SCOPE_NOTE = (
    "Deterministic synthetic fixture evidence only: pytest-local SQLite ledger, "
    "in-memory two-dimensional vector projection, stubbed LLM transports. No live "
    "Ask/Claude turn, no SEC or source request, no portfolio or bench database. "
    "No timing was measured; nothing here is a production latency claim or "
    "improvement, and no retrieval-quality claim is made."
)

_CHUNKS = (
    ("chunk-1", "Revenue grew 12 percent on subscription demand.", [1.0, 0.0]),
    ("chunk-2", "Gross margin narrowed on freight costs.", [0.0, 1.0]),
)

_SCHEMA = """
CREATE TABLE search_embedding_model_promotions (
    promotion_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
    purpose TEXT NOT NULL, revision INTEGER NOT NULL, provider TEXT NOT NULL,
    model TEXT NOT NULL, dimensions INTEGER NOT NULL, golden_sha256 TEXT NOT NULL,
    evaluation_artifact_sha256 TEXT NOT NULL, evaluation_metrics_json TEXT NOT NULL,
    runtime_artifact_json TEXT, runtime_artifact_sha256 TEXT,
    approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
    supersedes_promotion_id TEXT, knowledge_at TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE VIEW v_search_embedding_model_promotion_current AS
SELECT promotion.* FROM search_embedding_model_promotions AS promotion
WHERE NOT EXISTS (
    SELECT 1 FROM search_embedding_model_promotions AS newer
    WHERE newer.purpose = promotion.purpose AND newer.revision > promotion.revision
);
CREATE TABLE search_index_runs (
    index_run_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL, index_kind TEXT NOT NULL,
    config_sha256 TEXT NOT NULL, outcome TEXT NOT NULL
);
CREATE TABLE search_chunks (
    chunk_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL, content_sha256 TEXT NOT NULL,
    evidence_node_id TEXT NOT NULL, char_start INTEGER NOT NULL, char_end INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE search_lexical_chunks USING fts5(chunk_id UNINDEXED, text);
CREATE TABLE evidence_extraction_runs (
    extraction_run_id TEXT PRIMARY KEY, document_version_id TEXT NOT NULL
);
CREATE TABLE evidence_nodes (
    node_id TEXT PRIMARY KEY, extraction_run_id TEXT NOT NULL,
    node_kind TEXT NOT NULL, locator_json TEXT
);
CREATE TABLE v_evidence_document_versions_canonical (
    document_version_id TEXT PRIMARY KEY, issuer_id TEXT NOT NULL,
    reporting_entity_id TEXT NOT NULL
);
CREATE TABLE evidence_source_observations (
    observation_id TEXT PRIMARY KEY, source_url TEXT NOT NULL
);
CREATE TABLE evidence_document_versions (
    document_version_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL,
    issuer_id TEXT NOT NULL, ticker TEXT, document_type TEXT NOT NULL,
    form_type TEXT NOT NULL, period_start TEXT, period_end TEXT
);
CREATE TABLE search_embedding_artifacts (
    index_run_id TEXT NOT NULL, chunk_id TEXT NOT NULL, purpose TEXT NOT NULL,
    provider TEXT NOT NULL, model TEXT NOT NULL, dimensions INTEGER NOT NULL,
    vector_sha256 TEXT, storage_uri TEXT, input_sha256 TEXT NOT NULL,
    request_config_sha256 TEXT NOT NULL, outcome TEXT NOT NULL,
    runtime_artifact_sha256 TEXT
);
CREATE TABLE search_index_memberships (
    index_run_id TEXT NOT NULL, chunk_id TEXT NOT NULL, membership_status TEXT NOT NULL
);
CREATE TABLE search_projection_seals (
    projection_seal_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
    index_run_id TEXT UNIQUE NOT NULL, manifest_id TEXT NOT NULL, index_kind TEXT NOT NULL,
    chunk_count INTEGER NOT NULL, chunk_set_sha256 TEXT NOT NULL,
    projection_records_sha256 TEXT NOT NULL, artifact_set_sha256 TEXT, provider TEXT,
    model TEXT, dimensions INTEGER, runtime_artifact_sha256 TEXT,
    config_sha256 TEXT NOT NULL, storage_uri TEXT NOT NULL, sealed_at TEXT NOT NULL
);
CREATE TABLE ask_retrieval_scope_promotions (
    promotion_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, scope_key TEXT,
    source_scope_key TEXT, source_scope_revision_id TEXT, revision INTEGER,
    issuer_id TEXT, reporting_entity_id TEXT, research_snapshot_id TEXT,
    research_snapshot_sha256 TEXT, fact_generation_id TEXT,
    fact_projection_seal_sha256 TEXT, source_inventory_set_json TEXT,
    source_inventory_set_sha256 TEXT, narrative_bundles_json TEXT,
    narrative_bundles_sha256 TEXT, cutoff_at TEXT, population_run_id TEXT,
    population_receipt_set_sha256 TEXT, population_observed_through TEXT,
    policy_version TEXT, verifier_name TEXT, verifier_version TEXT,
    verifier_code_sha256 TEXT, verifier_config_sha256 TEXT, status TEXT,
    supersedes_promotion_id TEXT, recorded_at TEXT
);
CREATE VIEW v_ask_retrieval_scope_current AS SELECT * FROM ask_retrieval_scope_promotions;
CREATE TABLE v_issuer_reporting_scope_current (
    scope_revision_id TEXT, scope_key TEXT, issuer_id TEXT, inclusion_state TEXT
);
CREATE TABLE population_run_headers (
    population_run_id TEXT, knowledge_cutoff TEXT, observed_through TEXT
);
CREATE TABLE population_cutover_receipts (
    population_run_id TEXT, receipt_set_sha256 TEXT
);
CREATE VIEW v_population_cutover_current AS
SELECT run.population_run_id, run.knowledge_cutoff, run.observed_through,
       receipt.receipt_set_sha256
FROM population_run_headers run JOIN population_cutover_receipts receipt
USING (population_run_id);
CREATE TABLE research_snapshot_headers (
    research_snapshot_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, cutoff_at TEXT NOT NULL
);
CREATE TABLE research_snapshot_admission_receipts (
    research_snapshot_id TEXT PRIMARY KEY, research_snapshot_sha256 TEXT NOT NULL
);
CREATE TABLE canonical_fact_projection_seals (
    generation_id TEXT PRIMARY KEY, projection_seal_sha256 TEXT NOT NULL
);
CREATE TABLE v_source_inventory_sealed_complete (snapshot_id TEXT, issuer_id TEXT);
CREATE TABLE search_manifest_source_inventories (manifest_id TEXT, snapshot_id TEXT);
CREATE TABLE heterogeneous_retrieval_trace_headers (
    trace_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, research_snapshot_id TEXT,
    research_snapshot_sha256 TEXT, fact_generation_id TEXT,
    fact_projection_seal_sha256 TEXT, narrative_commitments_json TEXT,
    narrative_commitments_sha256 TEXT, semantic_receipts_json TEXT,
    semantic_receipts_sha256 TEXT, query_sha256 TEXT, query_json TEXT, ranker_json TEXT,
    ranker_sha256 TEXT, filters_json TEXT, filters_sha256 TEXT, candidate_limit INTEGER,
    result_limit INTEGER, cutoff_at TEXT, recorded_at TEXT, population_run_id TEXT,
    population_receipt_set_sha256 TEXT, population_observed_through TEXT
);
CREATE TABLE heterogeneous_retrieval_trace_candidates (
    trace_id TEXT, candidate_ordinal INTEGER, candidate_kind TEXT, candidate_id TEXT,
    source_commitment_sha256 TEXT, lexical_score TEXT, semantic_score TEXT,
    normalized_score TEXT, ranker_name TEXT, filter_outcome TEXT, filter_reason TEXT,
    evidence_locator_json TEXT, lineage_json TEXT, lineage_sha256 TEXT,
    candidate_json TEXT, candidate_sha256 TEXT
);
CREATE TABLE heterogeneous_retrieval_trace_results (
    trace_id TEXT, result_ordinal INTEGER, candidate_ordinal INTEGER, final_score TEXT,
    result_json TEXT, result_sha256 TEXT
);
CREATE TABLE heterogeneous_retrieval_trace_seals (
    trace_id TEXT, candidate_count INTEGER, result_count INTEGER,
    canonical_candidate_set_json TEXT, candidate_set_sha256 TEXT,
    canonical_result_set_json TEXT, result_set_sha256 TEXT, trace_json TEXT,
    trace_sha256 TEXT, sealed_at TEXT
);
"""


@dataclass
class _Probe:
    """Counters for verification work and prompt assembly within one turn."""

    projection_reads: list[str] = field(default_factory=lambda: [])
    population_verifications: list[str] = field(default_factory=lambda: [])
    research_snapshot_verifications: list[str] = field(default_factory=lambda: [])
    research_coordinate_derivations: list[str] = field(default_factory=lambda: [])
    evidence_block_assemblies: int = 0
    evidence_fragment_builds: int = 0
    caches: list[RequestVerificationCache] = field(default_factory=lambda: [])
    answer_prompts: list[str] = field(default_factory=lambda: [])
    audit_prompts: list[str] = field(default_factory=lambda: [])
    audit_evidence_inputs: list[str] = field(default_factory=lambda: [])
    packages: list[AnswerAuditPackage] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class _TurnResult:
    root: Path
    events: list[dict[str, object]]
    probe: _Probe
    evidence_block: str


@dataclass(frozen=True)
class _PopulationReceipt:
    receipt_set_sha256: str
    temporal_scope: PopulationTemporalScope


@dataclass(frozen=True)
class _SnapshotAdmission:
    member_set_sha256: str


@dataclass(frozen=True)
class _CallIdentity:
    call_id: int
    run_id: str
    model: str
    provider: str
    transport: str
    prompt_sha256: str
    response_sha256: str
    template_id: str
    template_version: str
    template_vars_sha256: str


@dataclass(frozen=True)
class _StructuredResult:
    value: object
    raw_response: str
    prompt: str


class _CountingIndex:
    """In-memory sealed vector projection that counts every full read."""

    def __init__(
        self,
        rows: list[dict[str, object]],
        storage_uri: str,
        probe: _Probe,
        *,
        rows_by_run: dict[str, list[dict[str, object]]] | None = None,
        uris_by_run: dict[str, str] | None = None,
    ) -> None:
        self._rows = rows
        self._storage_uri = storage_uri
        self._probe = probe
        self._rows_by_run = rows_by_run or {}
        self._uris_by_run = uris_by_run or {}

    def read_projection(self, index_run_id: str, *, expected_count: int) -> list[dict[str, object]]:
        self._probe.projection_reads.append(index_run_id)
        rows = self._rows_by_run.get(index_run_id, self._rows)
        return [dict(row) for row in rows]

    def published_storage_uri(self, index_run_id: str) -> str:
        return self._uris_by_run.get(index_run_id, self._storage_uri)


class _FixedQueryEncoder:
    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("exact retrieval must not encode passages")

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _text in texts]


def _runtime_artifact() -> EmbeddingRuntimeArtifact:
    return EmbeddingRuntimeArtifact(
        provider="local-test",
        model="deterministic-two-dimensional",
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


def _bundle() -> NarrativeBundle:
    return NarrativeBundle(
        corpus_manifest_id=MANIFEST_ID,
        lexical_index_run_id=LEXICAL_RUN_ID,
        vector_index_run_id=VECTOR_RUN_ID,
        embedding_promotion_id=EMBEDDING_PROMOTION_ID,
    )


def _scope() -> RetrievalScope:
    return RetrievalScope(
        scope_id=derive_retrieval_scope_id(
            source_scope_key=SOURCE_SCOPE_KEY,
            issuer_id=ISSUER_ID,
        ),
        source_scope_key=SOURCE_SCOPE_KEY,
        source_scope_revision_id=SOURCE_SCOPE_REVISION_ID,
        ticker="ACME",
        issuer_id=ISSUER_ID,
        reporting_entity_id=REPORTING_ENTITY_ID,
    )


def _research_request() -> ResearchSnapshotRequest:
    return ResearchSnapshotRequest(
        research_snapshot_id=RESEARCH_SNAPSHOT_ID,
        idempotency_key=RESEARCH_SNAPSHOT_ID,
        research_universe=ResearchUniverse(
            issuer_id=ISSUER_ID,
            reporting_entity_ids=(REPORTING_ENTITY_ID,),
            document_version_ids=("document-1",),
            source_obligation_revision_ids=("obligation-1",),
        ),
        processing_snapshot_ids=("processing-1",),
        corpus_bundles=(
            CorpusProjectionBundle(
                corpus_manifest_id=MANIFEST_ID,
                lexical_index_run_id=LEXICAL_RUN_ID,
                vector_index_run_id=VECTOR_RUN_ID,
                embedding_promotion_id=EMBEDDING_PROMOTION_ID,
            ),
        ),
        source_fact_publication_ids=(),
        ontology_snapshot_id="ontology-1",
        canonical_fact_resolution_snapshot_id="resolution-1",
        canonical_fact_projection_run_id=FACT_GENERATION_ID,
        cutoff_at=NOW,
        recorded_at=NOW,
    )


def _retrieval_promotion() -> RetrievalPromotion:
    policy, verifier_name, verifier_version, code_sha256, config_sha256 = (
        current_verifier_identity()
    )
    scope = _scope()
    return RetrievalPromotion(
        promotion_id="ask-promotion-1",
        idempotency_key="ask-promotion-1",
        scope_id=scope.scope_id,
        source_scope_key=SOURCE_SCOPE_KEY,
        source_scope_revision_id=SOURCE_SCOPE_REVISION_ID,
        revision=1,
        issuer_id=ISSUER_ID,
        reporting_entity_id=REPORTING_ENTITY_ID,
        research_snapshot_id=RESEARCH_SNAPSHOT_ID,
        research_snapshot_sha256=RESEARCH_SNAPSHOT_SHA256,
        fact_generation_id=FACT_GENERATION_ID,
        fact_projection_seal_sha256=FACT_PROJECTION_SEAL_SHA256,
        source_inventory_ids=(INVENTORY_ID,),
        narrative_bundles=(_bundle(),),
        cutoff_at=NOW,
        population_run_id=POPULATION_RUN_ID,
        population_receipt_set_sha256=POPULATION_RECEIPT_SHA256,
        population_observed_through=NOW,
        policy_version=policy,
        verifier_name=verifier_name,
        verifier_version=verifier_version,
        verifier_code_sha256=code_sha256,
        verifier_config_sha256=config_sha256,
        status="promoted",
        recorded_at=NOW,
    )


def _vector_record(chunk_id: str, text: str, vector: list[float]) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "vector": vector,
        "vector_sha256": vector_sha256(vector, dimensions=2),
        "dimensions": 2,
        "input_sha256": digest_text(text),
        "manifest_id": MANIFEST_ID,
        "issuer_id": ISSUER_ID,
        "recorded_issuer_id": ISSUER_ID,
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
        "runtime_artifact_sha256": _runtime_artifact().sha256(),
    }


def _seed_semantic_ledger(conn: sqlite3.Connection, storage_uri: str) -> list[dict[str, object]]:
    artifact = _runtime_artifact()
    promotion = EmbeddingPromotion(
        promotion_id=EMBEDDING_PROMOTION_ID,
        idempotency_key=EMBEDDING_PROMOTION_ID,
        revision=1,
        provider="local-test",
        model="deterministic-two-dimensional",
        dimensions=2,
        golden_sha256="1" * 64,
        evaluation_artifact_sha256="2" * 64,
        evaluation_metrics_json="{}",
        runtime_artifact_json=artifact.canonical_json(),
        runtime_artifact_sha256=artifact.sha256(),
        approved_by="owner",
        approved_at=NOW,
        knowledge_at=NOW,
        recorded_at=NOW,
    )
    conn.execute(
        "INSERT INTO search_embedding_model_promotions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            promotion.promotion_id,
            promotion.idempotency_key,
            promotion.purpose,
            promotion.revision,
            promotion.provider,
            promotion.model,
            promotion.dimensions,
            promotion.golden_sha256,
            promotion.evaluation_artifact_sha256,
            promotion.evaluation_metrics_json,
            promotion.runtime_artifact_json,
            promotion.runtime_artifact_sha256,
            promotion.approved_by,
            NOW.isoformat(),
            None,
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO search_index_runs VALUES (?,?,?,?,?)",
        (VECTOR_RUN_ID, MANIFEST_ID, "vector", CONFIG_SHA256, "succeeded"),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES (?,?)",
        ("observation-1", "https://example.test/acme-10k"),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES (?,?,?,?,?,?,?,?)",
        (
            "document-1",
            "observation-1",
            ISSUER_ID,
            "ACME",
            "annual_report",
            "10-K",
            "2025-01-01",
            "2025-12-31",
        ),
    )
    conn.execute(
        "INSERT INTO v_evidence_document_versions_canonical VALUES (?,?,?)",
        ("document-1", ISSUER_ID, REPORTING_ENTITY_ID),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?)",
        ("extraction-1", "document-1"),
    )
    records: list[dict[str, object]] = []
    for chunk_id, text, vector in _CHUNKS:
        node_id = f"node:{chunk_id}"
        content_sha256 = digest_text(text)
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,?,?)",
            (node_id, "extraction-1", "passage", canonical_json({"section": "MD&A"})),
        )
        conn.execute(
            "INSERT INTO search_chunks VALUES (?,?,?,?,?,?,?)",
            (chunk_id, MANIFEST_ID, content_sha256, node_id, 0, len(text), text),
        )
        conn.execute("INSERT INTO search_lexical_chunks VALUES (?,?)", (chunk_id, text))
        record = _vector_record(chunk_id, text, vector)
        records.append(record)
        conn.execute(
            "INSERT INTO search_embedding_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                VECTOR_RUN_ID,
                chunk_id,
                "passage",
                promotion.provider,
                promotion.model,
                promotion.dimensions,
                record["vector_sha256"],
                storage_uri,
                content_sha256,
                CONFIG_SHA256,
                "succeeded",
                artifact.sha256(),
            ),
        )
        conn.execute(
            "INSERT INTO search_index_memberships VALUES (?,?,?)",
            (VECTOR_RUN_ID, chunk_id, "included"),
        )
    chunk_count, chunk_sha256 = manifest_chunk_commitment(conn, manifest_id=MANIFEST_ID)
    artifact_count, artifact_sha256 = vector_artifact_commitment(conn, index_run_id=VECTOR_RUN_ID)
    assert artifact_count == chunk_count == len(records)
    persist_projection_seal(
        conn,
        SearchProjectionSeal(
            projection_seal_id=PROJECTION_SEAL_ID,
            idempotency_key=PROJECTION_SEAL_ID,
            index_run_id=VECTOR_RUN_ID,
            manifest_id=MANIFEST_ID,
            index_kind="vector",
            chunk_count=chunk_count,
            chunk_set_sha256=chunk_sha256,
            projection_records_sha256=vector_records_digest(records),
            artifact_set_sha256=artifact_sha256,
            provider=promotion.provider,
            model=promotion.model,
            dimensions=promotion.dimensions,
            runtime_artifact_sha256=artifact.sha256(),
            config_sha256=CONFIG_SHA256,
            storage_uri=storage_uri,
            sealed_at=NOW,
        ),
    )
    return records


def _seed_sealed_coordinates(conn: sqlite3.Connection) -> None:
    promotion = _retrieval_promotion()
    inventory_json = canonical_json(promotion.source_inventory_ids)
    bundles_json = canonical_json(
        [item.model_dump(mode="json") for item in promotion.narrative_bundles]
    )
    conn.execute(
        "INSERT INTO ask_retrieval_scope_promotions VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            promotion.promotion_id,
            promotion.idempotency_key,
            promotion.scope_id,
            promotion.source_scope_key,
            promotion.source_scope_revision_id,
            promotion.revision,
            promotion.issuer_id,
            promotion.reporting_entity_id,
            promotion.research_snapshot_id,
            promotion.research_snapshot_sha256,
            promotion.fact_generation_id,
            promotion.fact_projection_seal_sha256,
            inventory_json,
            digest_text(inventory_json),
            bundles_json,
            digest_text(bundles_json),
            NOW.isoformat(),
            promotion.population_run_id,
            promotion.population_receipt_set_sha256,
            NOW.isoformat(),
            promotion.policy_version,
            promotion.verifier_name,
            promotion.verifier_version,
            promotion.verifier_code_sha256,
            promotion.verifier_config_sha256,
            promotion.status,
            None,
            NOW.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO v_issuer_reporting_scope_current VALUES (?,?,?,?)",
        (SOURCE_SCOPE_REVISION_ID, SOURCE_SCOPE_KEY, ISSUER_ID, "core"),
    )
    conn.execute(
        "INSERT INTO population_run_headers VALUES (?,?,?)",
        (POPULATION_RUN_ID, NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO population_cutover_receipts VALUES (?,?)",
        (POPULATION_RUN_ID, POPULATION_RECEIPT_SHA256),
    )
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?)",
        (RESEARCH_SNAPSHOT_ID, _research_request().model_dump_json(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO research_snapshot_admission_receipts VALUES (?,?)",
        (RESEARCH_SNAPSHOT_ID, RESEARCH_SNAPSHOT_SHA256),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
        (FACT_GENERATION_ID, FACT_PROJECTION_SEAL_SHA256),
    )
    conn.execute(
        "INSERT INTO v_source_inventory_sealed_complete VALUES (?,?)",
        (INVENTORY_ID, ISSUER_ID),
    )
    conn.execute(
        "INSERT INTO search_manifest_source_inventories VALUES (?,?)",
        (MANIFEST_ID, INVENTORY_ID),
    )


def _research_commitments(bundles: Sequence[NarrativeBundle] = ()) -> dict[str, object]:
    active_bundles = bundles or (_bundle(),)
    narrative: list[dict[str, object]] = []
    for bundle in active_bundles:
        lanes = (
            ("corpus", "search_corpus_manifests", bundle.corpus_manifest_id, None),
            (
                "lexical_projection",
                "search_projection_seals",
                "lexical-seal-1"
                if bundle.lexical_index_run_id == LEXICAL_RUN_ID
                else f"lexical-seal-{bundle.lexical_index_run_id}",
                bundle.lexical_index_run_id,
            ),
            (
                "vector_projection",
                "search_projection_seals",
                PROJECTION_SEAL_ID
                if bundle.vector_index_run_id == VECTOR_RUN_ID
                else f"projection-seal-{bundle.vector_index_run_id}",
                bundle.vector_index_run_id,
            ),
            (
                "embedding_promotion",
                "search_embedding_model_promotions",
                bundle.embedding_promotion_id,
                None,
            ),
        )
        for kind, table, reference_id, index_run_id in lanes:
            commitment: dict[str, object] = {
                "reference_commitment_sha256": digest_text(f"{kind}:{reference_id}"),
                "reference_id": reference_id,
                "reference_table": table,
                "requested_lane": f"{kind}:{bundle.corpus_manifest_id}",
            }
            if index_run_id is not None:
                commitment["index_run_id"] = index_run_id
            narrative.append(commitment)
    return {
        "fact_projection_seal_sha256": FACT_PROJECTION_SEAL_SHA256,
        "narrative": narrative,
        "research_snapshot_sha256": RESEARCH_SNAPSHOT_SHA256,
    }


def _build_fixture(
    root: Path, storage_root: Path
) -> tuple[Path, LocalVectorRuntimeConfig, list[dict[str, object]]]:
    # ``storage_root`` is shared by the two compared turns: the sealed storage
    # URI is a committed coordinate (projection seal, exact-semantic backend
    # receipt, and therefore the retrieval-trace hash), so byte-for-byte
    # output comparison requires the identical URI in both runs. Nothing ever
    # reads the directory: the vector adapter is the counting double below.
    root.mkdir(parents=True)
    dataset = storage_root / f"run-{VECTOR_RUN_ID}" / "vectors.lance"
    storage_uri = f"lance://{dataset.resolve().as_posix()}#evidence_chunks"
    db_path = root / "sealed-fixture.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        records = _seed_semantic_ledger(conn, storage_uri)
        _seed_sealed_coordinates(conn)
        conn.commit()
    finally:
        conn.close()
    return (
        db_path,
        LocalVectorRuntimeConfig(index_root=storage_root, runtime_root=root / "runtime"),
        records,
    )


def _install_doubles(
    monkeypatch: pytest.MonkeyPatch,
    probe: _Probe,
    *,
    records: list[dict[str, object]],
    runtime: LocalVectorRuntimeConfig,
    enabled: bool,
    extra_records_by_run: dict[str, list[dict[str, object]]] | None = None,
    extra_uris_by_run: dict[str, str] | None = None,
) -> None:
    storage_uri = f"lance://{(runtime.index_root / f'run-{VECTOR_RUN_ID}' / 'vectors.lance').resolve().as_posix()}#evidence_chunks"

    def _index(_root: Path) -> _CountingIndex:
        return _CountingIndex(
            records,
            storage_uri,
            probe,
            rows_by_run=extra_records_by_run,
            uris_by_run=extra_uris_by_run,
        )

    def _encoder(*_args: object, **_kwargs: object) -> _FixedQueryEncoder:
        return _FixedQueryEncoder()

    class _PopulationLedger:
        def __init__(self, _conn: sqlite3.Connection) -> None:
            pass

        def verify(self, population_run_id: str) -> _PopulationReceipt:
            probe.population_verifications.append(population_run_id)
            return _PopulationReceipt(
                receipt_set_sha256=POPULATION_RECEIPT_SHA256,
                temporal_scope=PopulationTemporalScope(knowledge_cutoff=NOW, observed_through=NOW),
            )

    def _verify_snapshot(
        _conn: sqlite3.Connection, research_snapshot_id: str
    ) -> _SnapshotAdmission:
        probe.research_snapshot_verifications.append(research_snapshot_id)
        return _SnapshotAdmission(member_set_sha256=RESEARCH_SNAPSHOT_SHA256)

    def _derive_research(
        _conn: sqlite3.Connection,
        request: HeterogeneousRetrievalRequest,
        _verification: RequestVerificationCache | None,
    ) -> dict[str, object]:
        probe.research_coordinate_derivations.append(request.research_snapshot_id)
        return _research_commitments(request.narrative_bundles)

    def _no_facts(_conn: sqlite3.Connection, **_kwargs: object) -> list[object]:
        return []

    def _cache(conn: sqlite3.Connection) -> RequestVerificationCache:
        cache = RequestVerificationCache(conn, enabled=enabled)
        probe.caches.append(cache)
        return cache

    monkeypatch.setattr(exact_semantic_module, "LanceVectorIndex", _index)
    monkeypatch.setattr(retrieval_module, "LanceVectorIndex", _index)
    monkeypatch.setattr(exact_semantic_module.FastEmbedEncoder, "from_spec", _encoder)
    monkeypatch.setattr(sealed, "PopulationCompletenessLedger", _PopulationLedger)
    monkeypatch.setattr(retrieval_module, "PopulationCompletenessLedger", _PopulationLedger)
    monkeypatch.setattr(sealed, "verify_research_snapshot", _verify_snapshot)
    monkeypatch.setattr(retrieval_module, "_derive_research_coordinates", _derive_research)
    monkeypatch.setattr(retrieval_module, "search_canonical_facts", _no_facts)
    monkeypatch.setattr(engine, "RequestVerificationCache", _cache)


def _install_turn_seams(
    monkeypatch: pytest.MonkeyPatch,
    probe: _Probe,
    *,
    runtime: LocalVectorRuntimeConfig,
    audit_cites: tuple[int, ...] = (1,),
) -> None:
    engine_names = vars(engine)
    real_block = cast(
        Callable[[Sequence[SealedEvidenceItem]], tuple[tuple[str, ...], str]],
        engine_names["_sealed_evidence_block"],
    )
    real_fragment = cast(
        Callable[[SealedEvidenceItem], str],
        engine_names["_evidence_prompt_fragment"],
    )
    prompts: dict[str, str] = {}

    def _block(evidence: Sequence[SealedEvidenceItem]) -> tuple[tuple[str, ...], str]:
        probe.evidence_block_assemblies += 1
        return real_block(evidence)

    def _fragment(item: SealedEvidenceItem) -> str:
        probe.evidence_fragment_builds += 1
        return real_fragment(item)

    def _scope_for(ticker: str) -> RetrievalScope:
        return RetrievalScope(
            scope_id=derive_retrieval_scope_id(
                source_scope_key=ticker,
                issuer_id=ISSUER_ID,
            ),
            source_scope_key=ticker,
            source_scope_revision_id=f"scope-revision-{ticker}",
            ticker=ticker,
            issuer_id=ISSUER_ID,
            reporting_entity_id=REPORTING_ENTITY_ID,
        )

    def _scopes(
        _conn: sqlite3.Connection,
        *_args: object,
        requested_tickers: Sequence[str] | None = None,
        **_kwargs: object,
    ) -> tuple[RetrievalScope, ...]:
        if requested_tickers:
            return tuple(_scope_for(t) for t in requested_tickers)
        return (_scope_for("ACME"),)

    def _connect(path: Path, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(path)

    def _runtime() -> LocalVectorRuntimeConfig:
        return runtime

    def _bind(_turn: AskTurn, _text: str, **_kwargs: object) -> int:
        return 1

    def _context(
        session_id: str, *, user_text: str, **_kwargs: object
    ) -> tuple[tuple[AnswerContextTurn, ...], str]:
        return (
            (
                AnswerContextTurn(
                    turn_id=1,
                    session_id=session_id,
                    role="user",
                    text_sha256=digest_text(user_text),
                    created_at=NOW.isoformat(),
                ),
            ),
            "",
        )

    def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    def _answer(prompt: str, **_kwargs: object) -> str:
        probe.answer_prompts.append(prompt)
        prompts["ask_answer"] = prompt
        return ANSWER

    def _audit(prompt: str, **_kwargs: object) -> _StructuredResult:
        probe.audit_prompts.append(prompt)
        prompts["ask_claim_audit"] = prompt
        claims = [
            {
                "char_start": start,
                "char_end": end,
                "quote": quote,
                "cites": list(audit_cites),
                "supported": True,
            }
            for start, end, quote in required_claim_spans(ANSWER)
        ]
        raw = json.dumps({"claims": claims})
        return _StructuredResult(
            value=CLAIM_AUDIT_ADAPTER.validate_python({"claims": claims}),
            raw_response=raw,
            prompt=prompt,
        )

    def _identity(
        _db_path: Path, *, run_id: str, purpose: str, response_text: str
    ) -> _CallIdentity:
        return _CallIdentity(
            call_id=1 if purpose == "ask_answer" else 2,
            run_id=run_id,
            model="fixture-model",
            provider="fixture",
            transport="fixture",
            prompt_sha256=digest_text(prompts[purpose]),
            response_sha256=digest_text(response_text),
            template_id=f"fixture.{purpose}",
            template_version="1",
            template_vars_sha256=digest_text(purpose),
        )

    def _persist(_conn: sqlite3.Connection, package: AnswerAuditPackage) -> None:
        probe.packages.append(package)

    def _claim_audit_spy(
        answer: str, evidence_block: str, **kwargs: object
    ) -> tuple[object, object, object]:
        probe.audit_evidence_inputs.append(evidence_block)
        return real_claim_audit(answer, evidence_block, **kwargs)

    real_claim_audit = cast(
        Callable[..., tuple[object, object, object]],
        engine_names["_claim_audit"],
    )
    monkeypatch.setattr(engine, "_sealed_evidence_block", _block)
    monkeypatch.setattr(engine, "_evidence_prompt_fragment", _fragment)
    monkeypatch.setattr(engine, "_claim_audit", _claim_audit_spy)
    monkeypatch.setattr(engine, "connect_sqlite", _connect)
    monkeypatch.setattr(engine, "load_production_scopes", _scopes)
    monkeypatch.setattr(engine, "_sealed_runtime", _runtime)
    monkeypatch.setattr(engine, "_bind_sealed_user_turn", _bind)
    monkeypatch.setattr(engine, "_authoritative_context", _context)
    monkeypatch.setattr(engine, "_store_assert_user_tail", _noop)
    monkeypatch.setattr(engine, "_persist_sealed_assistant", _noop)
    monkeypatch.setattr(engine, "call_llm", _answer)
    monkeypatch.setattr(engine, "call_llm_structured_with_raw", _audit)
    monkeypatch.setattr(engine, "_governed_call_identity", _identity)
    monkeypatch.setattr(engine, "persist_answer_audit", _persist)
    monkeypatch.setattr(engine, "uuid4", lambda: REQUEST_UUID)


def _run_turn(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    enabled: bool,
    storage_root: Path,
) -> _TurnResult:
    db_path, runtime, records = _build_fixture(root, storage_root)
    probe = _Probe()
    with monkeypatch.context() as patch:
        _install_doubles(patch, probe, records=records, runtime=runtime, enabled=enabled)
        _install_turn_seams(patch, probe, runtime=runtime)
        events = list(
            engine.respond_turn(
                AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME"]),
                ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="ctx"),
                db_path=db_path,
                repo_root=root,
                retrieval_mode="sealed",
            )
        )
    assert [event["type"] for event in events] == ["stage", "delta", "citations", "final"], events
    assert len(probe.packages) == 1
    return _TurnResult(
        root=root,
        events=events,
        probe=probe,
        evidence_block=probe.packages[0].record.prompt_variables.evidence_block,
    )


def _journal_counts(turn: _TurnResult) -> dict[str, dict[str, int]]:
    # Fixture roots differ per run; normalize them so coordinates compare.
    (cache,) = turn.probe.caches
    roots = (str(turn.root.resolve()), str(turn.root))
    grouped: dict[str, dict[str, int]] = {}
    for (kind, coordinate), count in sorted(cache.counts().items()):
        key = "|".join(coordinate)
        for root in roots:
            key = key.replace(root, "<fixture-root>")
        grouped.setdefault(kind, {})[key] = grouped.get(kind, {}).get(key, 0) + count
    return grouped


def _package_projection(package: AnswerAuditPackage) -> dict[str, object]:
    record = package.record
    return {
        "prompt_variables": record.prompt_variables.model_dump(mode="json"),
        "claim_audit_prompt_variables": record.claim_audit_prompt_variables.model_dump(mode="json"),
        "retrieval_assembly": [item.model_dump(mode="json") for item in record.retrieval_assembly],
        "retrieval_prompt_fragments": list(record.retrieval_prompt_fragments),
        "retrievals": [
            item.model_dump(mode="json", exclude={"recorded_at"}) for item in package.retrievals
        ],
        "citations": [
            item.model_dump(mode="json", exclude={"recorded_at"}) for item in package.citations
        ],
        "claims": [
            item.model_dump(mode="json", exclude={"recorded_at"}) for item in package.claims
        ],
        "answer_text": record.answer_text,
        "prompt_sha256": record.prompt_sha256,
        "claim_audit_prompt_sha256": record.claim_audit_prompt_sha256,
    }


def _record_artifact(name: str, payload: dict[str, object]) -> None:
    root = os.environ.get(_EVIDENCE_DIR_ENV, "").strip()
    if not root:
        return
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture
def turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[_TurnResult, _TurnResult]]:
    monkeypatch.delenv("ASK_RETRIEVAL_MODE", raising=False)
    storage_root = tmp_path / "sealed-storage"
    cached = _run_turn(monkeypatch, tmp_path / "cached", enabled=True, storage_root=storage_root)
    reference = _run_turn(
        monkeypatch, tmp_path / "reference", enabled=False, storage_root=storage_root
    )
    yield cached, reference


def test_each_sealed_verification_runs_once_per_turn_with_outputs_preserved(
    turns: tuple[_TurnResult, _TurnResult],
) -> None:
    cached, reference = turns
    assert len(cached.probe.caches) == 1
    assert len(reference.probe.caches) == 1
    cached_counts = _journal_counts(cached)
    reference_counts = _journal_counts(reference)
    expected_kinds = {
        PROMOTION_COORDINATES,
        POPULATION_CUTOVER_CURRENT,
        POPULATION_RECEIPT,
        RESEARCH_COORDINATES,
        RETRIEVAL_TRACE,
        SEMANTIC_PROJECTION_READ,
        SEMANTIC_RUNTIME_COORDINATES,
        SEMANTIC_RUNTIME_INSTANCE,
    }
    assert set(cached_counts) == expected_kinds
    assert set(reference_counts) == expected_kinds
    # Every immutable coordinate is verified exactly once in the deduplicated turn.
    assert all(count == 1 for counts in cached_counts.values() for count in counts.values())
    for kind in expected_kinds:
        assert set(cached_counts[kind]) == set(reference_counts[kind])
        assert len(cached_counts[kind]) == 1
    assert cached_counts[SEMANTIC_PROJECTION_READ] == {
        f"{VECTOR_RUN_ID}|{EMBEDDING_PROMOTION_ID}|{PROJECTION_SEAL_ID}": 1
    }

    # Independent counters at the verifier boundaries agree with the journal.
    assert cached.probe.projection_reads == [VECTOR_RUN_ID]
    assert cached.probe.research_snapshot_verifications == [RESEARCH_SNAPSHOT_ID]
    assert cached.probe.population_verifications == [POPULATION_RUN_ID]
    assert cached.probe.research_coordinate_derivations == [RESEARCH_SNAPSHOT_ID]
    # The un-deduplicated reference repeats the same work across the turn.
    assert len(reference.probe.projection_reads) == 5
    assert len(reference.probe.research_snapshot_verifications) == 2
    assert len(reference.probe.population_verifications) == 7
    assert len(reference.probe.research_coordinate_derivations) == 4
    assert reference_counts[PROMOTION_COORDINATES] == {
        next(iter(cached_counts[PROMOTION_COORDINATES])): 2
    }

    # Candidates, evidence, verification receipts, and the delivered answer are
    # byte-identical to the reference run on the same fixture.
    assert cached.events == reference.events
    assert cached.evidence_block == reference.evidence_block
    cached_package = _package_projection(cached.probe.packages[0])
    reference_package = _package_projection(reference.probe.packages[0])
    assert canonical_json(cached_package) == canonical_json(reference_package)
    citations = cast(list[dict[str, object]], cached.events[2]["items"])
    assert [item["candidate_id"] for item in citations] == ["chunk-1"]
    assert len(cached.probe.packages[0].record.retrieval_assembly) == 2

    _record_artifact(
        "verification-dedup/val-search-004-verification-counts.json",
        {
            "assertion": "VAL-SEARCH-004",
            "scope_note": _SCOPE_NOTE,
            "fixture": {
                "vector_projection_seal": PROJECTION_SEAL_ID,
                "vector_index_run_id": VECTOR_RUN_ID,
                "embedding_promotion_id": EMBEDDING_PROMOTION_ID,
                "research_snapshot_id": RESEARCH_SNAPSHOT_ID,
                "population_run_id": POPULATION_RUN_ID,
                "projection_rows": len(_CHUNKS),
            },
            "deduplicated_turn": {
                "journal_counts_by_kind_and_coordinate": cached_counts,
                "full_projection_reads": len(cached.probe.projection_reads),
                "research_snapshot_verifications": len(
                    cached.probe.research_snapshot_verifications
                ),
                "population_ledger_verifications": len(cached.probe.population_verifications),
                "research_coordinate_derivations": len(
                    cached.probe.research_coordinate_derivations
                ),
            },
            "reference_turn_enabled_false": {
                "journal_counts_by_kind_and_coordinate": reference_counts,
                "full_projection_reads": len(reference.probe.projection_reads),
                "research_snapshot_verifications": len(
                    reference.probe.research_snapshot_verifications
                ),
                "population_ledger_verifications": len(reference.probe.population_verifications),
                "research_coordinate_derivations": len(
                    reference.probe.research_coordinate_derivations
                ),
            },
            "output_preservation": {
                "events_identical": cached.events == reference.events,
                "evidence_block_identical": cached.evidence_block == reference.evidence_block,
                "audit_package_identical_excluding_wall_clock": canonical_json(cached_package)
                == canonical_json(reference_package),
                "evidence_block_sha256": digest_text(cached.evidence_block),
                "retrieval_trace_sha256": [
                    item.trace_sha256 for item in cached.probe.packages[0].retrievals
                ],
            },
        },
    )


def test_one_evidence_block_is_reused_byte_for_byte_by_answer_and_audit(
    turns: tuple[_TurnResult, _TurnResult],
) -> None:
    cached, _reference = turns
    probe = cached.probe
    package = probe.packages[0]
    record = package.record
    block = cached.evidence_block

    assert probe.evidence_block_assemblies == 1
    assert probe.evidence_fragment_builds == len(record.retrieval_assembly) == 2
    assert block == "\n\n".join(record.retrieval_prompt_fragments)
    assert probe.audit_evidence_inputs == [block]
    assert record.claim_audit_prompt_variables.evidence == block
    assert len(probe.answer_prompts) == 1
    assert len(probe.audit_prompts) == 1
    answer_prompt = probe.answer_prompts[0]
    audit_prompt = probe.audit_prompts[0]
    assert answer_prompt.count(block) == 1
    assert audit_prompt.count(block) == 1
    assert record.prompt_sha256 == digest_text(answer_prompt)
    assert record.claim_audit_prompt_sha256 == digest_text(audit_prompt)
    for item, fragment in zip(
        record.retrieval_assembly, record.retrieval_prompt_fragments, strict=True
    ):
        assert item.prompt_text_sha256 == digest_text(fragment)
    cited = {citation.citation_number: citation for citation in package.citations}
    assert set(cited) == {1}
    assert (
        cited[1].source_commitment_sha256
        == record.retrieval_assembly[0].source_commitment_sha256
        == digest_text(_CHUNKS[0][1])
    )

    _record_artifact(
        "verification-dedup/val-search-008-evidence-reuse.json",
        {
            "assertion": "VAL-SEARCH-008",
            "scope_note": _SCOPE_NOTE,
            "evidence_block_assemblies_per_turn": probe.evidence_block_assemblies,
            "evidence_fragment_builds_per_turn": probe.evidence_fragment_builds,
            "evidence_block_sha256": digest_text(block),
            "answer_prompt_variables_sha256": digest_text(
                canonical_json(record.prompt_variables.model_dump(mode="json"))
            ),
            "answer_prompt_evidence_sha256": digest_text(record.prompt_variables.evidence_block),
            "claim_audit_prompt_variables_sha256": digest_text(
                canonical_json(record.claim_audit_prompt_variables.model_dump(mode="json"))
            ),
            "claim_audit_prompt_evidence_sha256": digest_text(
                record.claim_audit_prompt_variables.evidence
            ),
            "answer_prompt_sha256": record.prompt_sha256,
            "claim_audit_prompt_sha256": record.claim_audit_prompt_sha256,
            "retrieval_assembly": [
                {
                    "citation_number": item.citation_number,
                    "trace_id": item.trace_id,
                    "candidate_id": item.candidate_id,
                    "source_commitment_sha256": item.source_commitment_sha256,
                    "prompt_text_sha256": item.prompt_text_sha256,
                }
                for item in record.retrieval_assembly
            ],
            "provider_prompt_cache": (
                "not reported: the fixture transport exposes no verifiable cache telemetry, "
                "and local evidence reuse is not evidence of a provider cache hit"
            ),
        },
    )


def test_nested_verification_invalidation_across_connections(tmp_path: Path) -> None:
    """An external commit between outer start and nested completion invalidates only the outer check."""
    db_path = tmp_path / "dedup_tokens.db"
    conn1 = sqlite3.connect(db_path)
    conn2 = sqlite3.connect(db_path)
    conn1.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn1.commit()

    cache = RequestVerificationCache(conn1)

    # 1. Outer verification begins on coordinate ('outer',)
    assert cache.verified("outer_check", ("outer",)) is NOT_VERIFIED

    # 2. External commit occurs on conn2 while outer check is underway
    conn2.execute("INSERT INTO t VALUES (1, 'hello')")
    conn2.commit()

    # 3. Nested verification begins on coordinate ('nested',)
    assert cache.verified("nested_check", ("nested",)) is NOT_VERIFIED

    # 4. Nested verification completes and records its result at current token
    cache.record("nested_check", ("nested",), "nested_result")

    # 5. Outer verification completes and attempts to record its result
    cache.record("outer_check", ("outer",), "outer_result")

    # Outer verification MUST NOT be memoized because data_version changed during its execution
    assert cache.verified("outer_check", ("outer",)) is NOT_VERIFIED

    # Nested verification was executed entirely at the new token, so it IS memoized
    assert cache.verified("nested_check", ("nested",)) == "nested_result"

    cache.close()
    conn1.close()
    conn2.close()


def test_exact_semantic_runtime_enforces_connection_boundary(tmp_path: Path) -> None:
    """ExactSemanticRuntime fails closed when passed a verification cache bound to a different connection."""
    db_path, runtime, records = _build_fixture(tmp_path / "root", tmp_path / "storage")
    conn1 = sqlite3.connect(db_path)
    conn2 = sqlite3.connect(db_path)

    cache1 = RequestVerificationCache(conn1)
    probe = _Probe()
    storage_uri = f"lance://{(runtime.index_root / f'run-{VECTOR_RUN_ID}' / 'vectors.lance').resolve().as_posix()}#evidence_chunks"
    counting_index = _CountingIndex(records, storage_uri, probe)
    encoder = _FixedQueryEncoder()

    with pytest.raises(VerificationCacheError, match="bound to a different connection"):
        exact_semantic_module.ExactSemanticRuntime.from_verified_components_for_test(
            conn2,
            vector_index_run_id=VECTOR_RUN_ID,
            embedding_promotion_id=EMBEDDING_PROMOTION_ID,
            index=counting_index,
            encoder=encoder,
            verification=cache1,
        )

    cache1.close()
    conn1.close()
    conn2.close()


def test_verified_retrieval_trace_receipt_is_protected_from_caller_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutating a returned verified retrieval trace receipt cannot corrupt the cached copy."""
    storage_root = tmp_path / "storage"
    root = tmp_path / "run"
    db_path, runtime, records = _build_fixture(root, storage_root)
    probe = _Probe()
    _install_doubles(monkeypatch, probe, records=records, runtime=runtime, enabled=True)
    _install_turn_seams(monkeypatch, probe, runtime=runtime)

    list(
        engine.respond_turn(
            AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME"]),
            ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="ctx"),
            db_path=db_path,
            repo_root=root,
            retrieval_mode="sealed",
        )
    )
    assert len(probe.packages) == 1
    trace_id = probe.packages[0].record.retrieval_assembly[0].trace_id

    conn = sqlite3.connect(db_path)
    cache = RequestVerificationCache(conn)

    # 1. First verification loads and caches the receipt
    receipt1 = retrieval_module.verify_heterogeneous_retrieval_trace(
        conn,
        trace_id,
        local_vector_runtime=runtime,
        verification=cache,
    )
    assert len(receipt1.ordered_results) > 0
    original_score = receipt1.ordered_results[0]["final_score"]

    # 2. Mutate nested dictionary in receipt1
    receipt1.ordered_results[0]["final_score"] = "MUTATED"

    # 3. Second verification from cache returns an independent deep copy
    receipt2 = retrieval_module.verify_heterogeneous_retrieval_trace(
        conn,
        trace_id,
        local_vector_runtime=runtime,
        verification=cache,
    )
    assert receipt2.ordered_results[0]["final_score"] == original_score
    assert receipt2.ordered_results[0]["final_score"] != "MUTATED"

    cache.close()
    conn.close()


def test_unacknowledged_same_connection_mutation_invalidates_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unacknowledged same-connection write invalidates memoized state and prevents reuse."""
    storage_root = tmp_path / "storage"
    root = tmp_path / "run"
    db_path, runtime, records = _build_fixture(root, storage_root)
    probe = _Probe()
    _install_doubles(monkeypatch, probe, records=records, runtime=runtime, enabled=True)
    _install_turn_seams(monkeypatch, probe, runtime=runtime)

    list(
        engine.respond_turn(
            AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME"]),
            ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="ctx"),
            db_path=db_path,
            repo_root=root,
            retrieval_mode="sealed",
        )
    )
    assert len(probe.packages) == 1
    trace_id = probe.packages[0].record.retrieval_assembly[0].trace_id

    conn = sqlite3.connect(db_path)
    cache = RequestVerificationCache(conn)
    trace_coord = (
        trace_id,
        str(runtime.index_root),
        str(runtime.runtime_root),
    )

    # 1. First verification memoizes RETRIEVAL_TRACE
    retrieval_module.verify_heterogeneous_retrieval_trace(
        conn,
        trace_id,
        local_vector_runtime=runtime,
        verification=cache,
    )
    assert cache.counts()[(retrieval_module.RETRIEVAL_TRACE, trace_coord)] == 1

    # Second check hits cache without re-executing
    retrieval_module.verify_heterogeneous_retrieval_trace(
        conn,
        trace_id,
        local_vector_runtime=runtime,
        verification=cache,
    )
    assert cache.counts()[(retrieval_module.RETRIEVAL_TRACE, trace_coord)] == 1

    # 2. Mutate database on the SAME connection (unacknowledged write)
    conn.execute(
        "UPDATE heterogeneous_retrieval_trace_results SET final_score='0.99999999' WHERE trace_id=?",
        (trace_id,),
    )
    conn.commit()

    # 3. Third verification detects same-connection mutation, invalidates memo, and re-executes.
    # Because database state was tampered with, re-verification does NOT return the cached receipt
    # and instead re-runs against the database, failing closed with HeterogeneousRetrievalError!
    with pytest.raises(retrieval_module.HeterogeneousRetrievalError):
        retrieval_module.verify_heterogeneous_retrieval_trace(
            conn,
            trace_id,
            local_vector_runtime=runtime,
            verification=cache,
        )

    cache.close()
    conn.close()


def test_savepoint_rollback_invalidates_cached_trace_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a savepoint rolls back, cached trace receipts recorded within it are invalidated."""
    storage_root = tmp_path / "storage"
    root = tmp_path / "run"
    db_path, runtime, records = _build_fixture(root, storage_root)
    probe = _Probe()
    _install_doubles(monkeypatch, probe, records=records, runtime=runtime, enabled=True)
    _install_turn_seams(monkeypatch, probe, runtime=runtime)

    list(
        engine.respond_turn(
            AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME"]),
            ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="ctx"),
            db_path=db_path,
            repo_root=root,
            retrieval_mode="sealed",
        )
    )
    assert len(probe.packages) == 1
    trace_id = probe.packages[0].record.retrieval_assembly[0].trace_id

    conn = sqlite3.connect(db_path)
    # Save genuine trace rows
    header_rows = conn.execute(
        "SELECT * FROM heterogeneous_retrieval_trace_headers WHERE trace_id=?", (trace_id,)
    ).fetchall()
    seal_rows = conn.execute(
        "SELECT * FROM heterogeneous_retrieval_trace_seals WHERE trace_id=?", (trace_id,)
    ).fetchall()
    cand_rows = conn.execute(
        "SELECT * FROM heterogeneous_retrieval_trace_candidates WHERE trace_id=?", (trace_id,)
    ).fetchall()
    result_rows = conn.execute(
        "SELECT * FROM heterogeneous_retrieval_trace_results WHERE trace_id=?", (trace_id,)
    ).fetchall()

    # Remove trace from DB so it does not exist before the savepoint
    conn.execute("DELETE FROM heterogeneous_retrieval_trace_headers WHERE trace_id=?", (trace_id,))
    conn.execute("DELETE FROM heterogeneous_retrieval_trace_seals WHERE trace_id=?", (trace_id,))
    conn.execute(
        "DELETE FROM heterogeneous_retrieval_trace_candidates WHERE trace_id=?", (trace_id,)
    )
    conn.execute("DELETE FROM heterogeneous_retrieval_trace_results WHERE trace_id=?", (trace_id,))
    conn.commit()

    cache = RequestVerificationCache(conn)

    # Insert inside savepoint, verify (memoized into cache), then simulate failure & rollback
    with (
        pytest.raises(RuntimeError, match="simulated rollback after trace creation"),
        cache.savepoint("create_tx"),
    ):
        conn.executemany(
            "INSERT INTO heterogeneous_retrieval_trace_headers VALUES ("
            + ",".join("?" for _ in header_rows[0])
            + ")",
            header_rows,
        )
        conn.executemany(
            "INSERT INTO heterogeneous_retrieval_trace_seals VALUES ("
            + ",".join("?" for _ in seal_rows[0])
            + ")",
            seal_rows,
        )
        conn.executemany(
            "INSERT INTO heterogeneous_retrieval_trace_candidates VALUES ("
            + ",".join("?" for _ in cand_rows[0])
            + ")",
            cand_rows,
        )
        conn.executemany(
            "INSERT INTO heterogeneous_retrieval_trace_results VALUES ("
            + ",".join("?" for _ in result_rows[0])
            + ")",
            result_rows,
        )
        verified = retrieval_module.verify_heterogeneous_retrieval_trace(
            conn,
            trace_id,
            local_vector_runtime=runtime,
            verification=cache,
        )
        assert verified.trace_id == trace_id
        raise RuntimeError("simulated rollback after trace creation")

    # The savepoint rolled back. The inserted rows are removed from the database.
    # Verification with the same cache MUST fail because the trace no longer exists in DB!
    with pytest.raises(
        retrieval_module.HeterogeneousRetrievalError, match="retrieval_trace_not_fully_sealed"
    ):
        retrieval_module.verify_heterogeneous_retrieval_trace(
            conn,
            trace_id,
            local_vector_runtime=runtime,
            verification=cache,
        )

    cache.close()
    conn.close()


def test_canonical_fact_projection_generation_admission_deduplication() -> None:
    """admit_canonical_projection_for_read deduplicates admission through PROJECTION_GENERATION_ADMISSION."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE canonical_fact_projection_generations (
        generation_id TEXT PRIMARY KEY, generation_kind TEXT, parent_generation_id TEXT,
        resolution_snapshot_id TEXT, resolution_snapshot_sha256 TEXT,
        resolution_watermark_sha256 TEXT, ontology_snapshot_id TEXT, ontology_snapshot_sha256 TEXT,
        cutoff_at TEXT, recorded_at TEXT, generation_sha256 TEXT, config_json TEXT
    )"""
    )
    conn.execute(
        """CREATE TABLE canonical_fact_projection_seals (
        generation_id TEXT PRIMARY KEY, projection_seal_id TEXT, projection_seal_sha256 TEXT,
        change_count INTEGER, upsert_count INTEGER, tombstone_count INTEGER,
        effective_entry_count INTEGER, batch_count INTEGER, ordered_entry_set_sha256 TEXT
    )"""
    )
    conn.execute(
        """CREATE TABLE canonical_fact_projection_scope_bindings (
        generation_id TEXT PRIMARY KEY, resolution_scope_sha256 TEXT, resolution_snapshot_commitment_sha256 TEXT
    )"""
    )
    conn.execute(
        """CREATE TABLE canonical_fact_projection_audit_receipts (
        generation_id TEXT PRIMARY KEY, projection_seal_sha256 TEXT, verifier_name TEXT,
        verifier_version TEXT, verifier_code_sha256 TEXT, verifier_config_sha256 TEXT,
        audit_payload_json TEXT, audit_payload_sha256 TEXT
    )"""
    )

    gen_id = "gen-admission-dedup"
    config_json = "{}"
    header: dict[str, object] = {
        "generation_id": gen_id,
        "generation_kind": "checkpoint",
        "parent_generation_id": None,
        "resolution_snapshot_id": "res-1",
        "resolution_snapshot_sha256": "1" * 64,
        "resolution_watermark_sha256": "2" * 64,
        "ontology_snapshot_id": "ont-1",
        "ontology_snapshot_sha256": "3" * 64,
        "cutoff_at": "2026-07-28T12:00:00Z",
        "recorded_at": "2026-07-28T12:00:00Z",
        "generation_sha256": "4" * 64,
        "config_json": config_json,
    }
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(header.values()),
    )

    seal: dict[str, object] = {
        "generation_id": gen_id,
        "projection_seal_id": "seal-1",
        "projection_seal_sha256": "5" * 64,
        "change_count": 10,
        "upsert_count": 10,
        "tombstone_count": 0,
        "effective_entry_count": 10,
        "batch_count": 1,
        "ordered_entry_set_sha256": "6" * 64,
    }
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?,?,?,?,?,?,?,?)",
        tuple(seal.values()),
    )

    binding: dict[str, object] = {
        "generation_id": gen_id,
        "resolution_scope_sha256": "7" * 64,
        "resolution_snapshot_commitment_sha256": "8" * 64,
    }
    conn.execute(
        "INSERT INTO canonical_fact_projection_scope_bindings VALUES (?,?,?)",
        tuple(binding.values()),
    )

    payload = projection_audit_payload(header, seal, binding)
    payload_json = canonical_json(payload)
    receipt = {
        "generation_id": gen_id,
        "projection_seal_sha256": seal["projection_seal_sha256"],
        "verifier_name": AUDIT_VERIFIER_NAME,
        "verifier_version": AUDIT_VERIFIER_VERSION,
        "verifier_code_sha256": AUDIT_VERIFIER_CODE_SHA256,
        "verifier_config_sha256": digest_text(config_json),
        "audit_payload_json": payload_json,
        "audit_payload_sha256": digest_text(payload_json),
    }
    conn.execute(
        "INSERT INTO canonical_fact_projection_audit_receipts VALUES (?,?,?,?,?,?,?,?)",
        tuple(receipt.values()),
    )
    conn.commit()

    cache = RequestVerificationCache(conn)
    v1 = admit_canonical_projection_for_read(conn, gen_id, verification=cache)
    assert v1.generation_id == gen_id
    assert cache.counts()[(PROJECTION_GENERATION_ADMISSION, (gen_id,))] == 1

    # Second call returns the memoized admission without re-executing audit
    v2 = admit_canonical_projection_for_read(conn, gen_id, verification=cache)
    assert v2 == v1
    assert cache.counts()[(PROJECTION_GENERATION_ADMISSION, (gen_id,))] == 1

    cache.close()
    conn.close()


def test_same_connection_mutation_during_pending_verification_rejects_memoization() -> None:
    """A same-connection write occurring between verified() and record() must not store stale result."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    conn.commit()

    cache = RequestVerificationCache(conn)
    coord = ("coord_pending_1",)

    # 1. verified() lookup returns NOT_VERIFIED and captures state
    assert cache.verified("kind_test", coord) is NOT_VERIFIED

    # 2. An unacknowledged same-connection write occurs while verification is running
    conn.execute("INSERT INTO t VALUES (1, 'mutated')")

    # 3. record() is called with the value derived before the mutation
    cache.record("kind_test", coord, "stale_result")

    # 4. The stale result must NOT have been memoized; verified() must return NOT_VERIFIED
    assert cache.verified("kind_test", coord) is NOT_VERIFIED

    cache.close()
    conn.close()


def test_invalidation_during_pending_verification_rejects_memoization() -> None:
    """Explicit cache invalidation occurring between verified() and record() must not store result."""
    conn = sqlite3.connect(":memory:")
    cache = RequestVerificationCache(conn)
    coord = ("coord_pending_2",)

    assert cache.verified("kind_test", coord) is NOT_VERIFIED
    cache.invalidate()
    cache.record("kind_test", coord, "stale_result")
    assert cache.verified("kind_test", coord) is NOT_VERIFIED

    cache.close()
    conn.close()


def test_rollback_during_pending_verification_rejects_memoization() -> None:
    """A database rollback occurring between verified() and record() must not store result."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    conn.commit()

    cache = RequestVerificationCache(conn)
    coord = ("coord_pending_3",)

    assert cache.verified("kind_test", coord) is NOT_VERIFIED

    conn.execute("SAVEPOINT sp")
    conn.execute("INSERT INTO t VALUES (1, 'temp')")
    conn.execute("ROLLBACK TO SAVEPOINT sp")
    conn.execute("RELEASE SAVEPOINT sp")

    cache.record("kind_test", coord, "stale_result")
    assert cache.verified("kind_test", coord) is NOT_VERIFIED

    cache.close()
    conn.close()


def test_authorized_write_during_pending_verification_preserves_memoization() -> None:
    """Authorized same-connection trace writes do not invalidate pending verification."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trace (id INTEGER PRIMARY KEY, val TEXT)")
    conn.commit()

    cache = RequestVerificationCache(conn)
    coord = ("coord_pending_4",)

    assert cache.verified("kind_test", coord) is NOT_VERIFIED

    with cache.authorized_write():
        conn.execute("INSERT INTO trace VALUES (1, 'trace_row')")

    cache.record("kind_test", coord, "valid_result")
    assert cache.verified("kind_test", coord) == "valid_result"

    cache.close()
    conn.close()


def test_bound_query_containing_word_rollback_does_not_clear_cache() -> None:
    """SQL statements containing the word 'rollback' inside values or comments do not clear cache."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE log (id INTEGER PRIMARY KEY, question TEXT)")
    conn.commit()

    cache = RequestVerificationCache(conn)
    coord = ("coord_rollback_text",)

    assert cache.verified("kind_test", coord) is NOT_VERIFIED
    cache.record("kind_test", coord, "cached_val")
    assert cache.verified("kind_test", coord) == "cached_val"

    # Execute a query with bound text containing 'rollback'
    with cache.authorized_write():
        conn.execute("INSERT INTO log VALUES (1, ?)", ("How did the rollback affect revenue?",))
        conn.execute("SELECT * FROM log WHERE question LIKE '%rollback%'")

    # Cache must still hold the memoized value; plain text must not clear cache
    assert cache.verified("kind_test", coord) == "cached_val"

    # Now execute a real rollback statement
    conn.execute("SAVEPOINT sp")
    conn.execute("INSERT INTO log VALUES (2, 'temp')")
    conn.execute("ROLLBACK TO SAVEPOINT sp")

    # Real rollback clears cache
    assert cache.verified("kind_test", coord) is NOT_VERIFIED

    cache.close()
    conn.close()


def test_implicit_rollback_invalidates_cache_and_rejects_stale_verifications() -> None:
    """Automatic transaction rollback (e.g. INSERT OR ROLLBACK) invalidates verification cache."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INT PRIMARY KEY, val TEXT)")
    conn.commit()

    cache = RequestVerificationCache(conn)
    coord = ("coord_implicit_rollback",)

    # 1. Insert row inside authorized write
    with cache.authorized_write():
        conn.execute("INSERT INTO t VALUES (1, 'initial')")

    # 2. Verify row and cache result while transaction is uncommitted
    assert cache.verified("test_kind", coord) is NOT_VERIFIED
    cache.record("test_kind", coord, "cached_val")
    assert cache.verified("test_kind", coord) == "cached_val"

    # 3. Attempt duplicate insert with INSERT OR ROLLBACK, catching IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT OR ROLLBACK INTO t VALUES (1, 'dup')")

    # The transaction rolled back: row is gone from the database
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0

    # 4. Request cached verification again: MUST NOT return stale verification!
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    cache.close()
    conn.close()


def test_implicit_rollback_on_reused_shortcut_cursor_invalidates_cache() -> None:
    """Reused cursor returned by connection shortcut (conn.execute) detects automatic rollback."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INT PRIMARY KEY, val TEXT)")
    conn.commit()

    cache = RequestVerificationCache(conn)
    coord = ("coord_reused_shortcut_cursor",)

    # 1. Obtain cursor via connection shortcut
    shortcut_cursor = conn.execute("SELECT 1")
    assert isinstance(shortcut_cursor, ManagedCursor)

    # 2. Insert row inside authorized write
    with cache.authorized_write():
        conn.execute("INSERT INTO t VALUES (1, 'initial')")

    # 3. Verify row and cache result while transaction is uncommitted
    assert cache.verified("test_kind", coord) is NOT_VERIFIED
    cache.record("test_kind", coord, "cached_val")
    assert cache.verified("test_kind", coord) == "cached_val"

    # 4. Execute duplicate insert through the reused cursor from the shortcut
    with pytest.raises(sqlite3.IntegrityError):
        shortcut_cursor.execute("INSERT OR ROLLBACK INTO t VALUES (1, 'dup')")

    # Transaction rolled back: row is gone from database
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0

    # 5. Request cached verification again: must NOT return stale verification
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    cache.close()
    conn.close()


def test_same_connection_schema_changes_invalidate_cache() -> None:
    """Same-connection DDL (CREATE, DROP, ALTER) invalidates cache without changing total_changes."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE substrate (id INT PRIMARY KEY, val TEXT)")
    conn.commit()

    cache = RequestVerificationCache(conn)
    coord = ("coord_ddl",)

    assert cache.verified("test_kind", coord) is NOT_VERIFIED
    cache.record("test_kind", coord, "substrate_admission")
    assert cache.verified("test_kind", coord) == "substrate_admission"

    changes_before = conn.total_changes

    # Execute DDL (DROP TABLE) which modifies schema without changing total_changes
    conn.execute("DROP TABLE substrate")
    assert conn.total_changes == changes_before

    # Cache MUST be invalidated by the schema change!
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    # Also test CREATE TABLE
    cache.record("test_kind", coord, "substrate_recreated")
    assert cache.verified("test_kind", coord) == "substrate_recreated"

    conn.execute("CREATE TABLE substrate (id INT PRIMARY KEY, val TEXT)")
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    cache.close()
    conn.close()


def test_temporary_table_shadowing_verified_main_table_invalidates_cache() -> None:
    """Creating a temporary table that shadows a verified main table invalidates the cache."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE substrate (id INT PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO substrate VALUES (1, 'main_data')")
    conn.commit()

    cache = RequestVerificationCache(conn)
    coord = ("substrate", "1")

    assert cache.verified("test_kind", coord) is NOT_VERIFIED
    cache.record("test_kind", coord, "main_admission")
    assert cache.verified("test_kind", coord) == "main_admission"

    # CREATE TEMP TABLE shadows the existing main table without altering main schema or total_changes
    changes_before = conn.total_changes
    conn.execute("CREATE TEMP TABLE substrate (id INT PRIMARY KEY, val TEXT)")
    assert conn.total_changes == changes_before

    # Verification queries use unqualified table names, so the temporary table shadows the main table.
    # Cache MUST be invalidated by the temporary schema change!
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    # Also test dropping the shadowing temporary table invalidates cache
    cache.record("test_kind", coord, "temp_admission")
    assert cache.verified("test_kind", coord) == "temp_admission"

    conn.execute("DROP TABLE temp.substrate")
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    cache.close()
    conn.close()


def test_external_dml_on_attached_database_invalidates_cache(tmp_path: Path) -> None:
    """External commit to an attached database invalidates turn-scoped verification cache."""
    db_path = tmp_path / "attached.db"
    conn_ext = sqlite3.connect(db_path)
    conn_ext.execute("CREATE TABLE substrate (id INT PRIMARY KEY, val TEXT)")
    conn_ext.execute("INSERT INTO substrate VALUES (1, 'init')")
    conn_ext.commit()

    conn = sqlite3.connect(":memory:")
    conn.execute(f"ATTACH DATABASE '{db_path}' AS ext")

    cache = RequestVerificationCache(conn)
    coord = ("coord_attached_dml",)

    assert cache.verified("test_kind", coord) is NOT_VERIFIED
    cache.record("test_kind", coord, "initial_admission")
    assert cache.verified("test_kind", coord) == "initial_admission"

    # External connection updates and commits to the attached database
    conn_ext.execute("UPDATE substrate SET val = 'mutated' WHERE id = 1")
    conn_ext.commit()

    # Cached admission MUST be invalidated by the external commit to the attached database!
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    cache.close()
    conn.close()
    conn_ext.close()


def test_attached_database_replacement_invalidates_cache(tmp_path: Path) -> None:
    """Detaching and attaching another database under the same schema name invalidates cache."""
    db1_path = tmp_path / "attached1.db"
    conn_ext1 = sqlite3.connect(db1_path)
    conn_ext1.execute("CREATE TABLE substrate (id INT PRIMARY KEY, val TEXT)")
    conn_ext1.execute("INSERT INTO substrate VALUES (1, 'db1')")
    conn_ext1.commit()

    db2_path = tmp_path / "attached2.db"
    conn_ext2 = sqlite3.connect(db2_path)
    conn_ext2.execute("CREATE TABLE substrate (id INT PRIMARY KEY, val TEXT)")
    conn_ext2.execute("INSERT INTO substrate VALUES (1, 'db2')")
    conn_ext2.commit()

    conn = sqlite3.connect(":memory:")
    conn.execute(f"ATTACH DATABASE '{db1_path}' AS ext")

    cache = RequestVerificationCache(conn)
    coord = ("coord_attached_replacement",)

    assert cache.verified("test_kind", coord) is NOT_VERIFIED
    cache.record("test_kind", coord, "admission_db1")
    assert cache.verified("test_kind", coord) == "admission_db1"

    # Detach db1 and attach db2 under the same schema name
    conn.execute("DETACH DATABASE ext")
    conn.execute(f"ATTACH DATABASE '{db2_path}' AS ext")

    # Cache MUST be invalidated by attachment replacement!
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    cache.close()
    conn.close()
    conn_ext1.close()
    conn_ext2.close()


def test_attached_database_reattachment_same_file_with_leading_empty_statement_invalidates_cache(
    tmp_path: Path,
) -> None:
    """Detaching and reattaching the same file with leading semicolons/comments invalidates cache."""
    db_path = tmp_path / "attached_same.db"
    conn_ext = sqlite3.connect(db_path)
    conn_ext.execute("CREATE TABLE substrate (id INT PRIMARY KEY, val TEXT)")
    conn_ext.execute("INSERT INTO substrate VALUES (1, 'v1')")
    conn_ext.commit()

    conn = sqlite3.connect(":memory:")
    conn.execute(f"ATTACH DATABASE '{db_path}' AS ext")

    cache = RequestVerificationCache(conn)
    coord = ("coord_attached_same_file",)

    assert cache.verified("test_kind", coord) is NOT_VERIFIED
    cache.record("test_kind", coord, "admission_v1")
    assert cache.verified("test_kind", coord) == "admission_v1"

    # Execute DETACH with leading semicolons, whitespace, and comments
    conn.execute("; /* detach comment */ ; DETACH DATABASE ext")

    # Update through external connection and commit (advancing ext data_version externally)
    conn_ext.execute("UPDATE substrate SET val = 'v2' WHERE id = 1")
    conn_ext.commit()

    # Reattach the SAME file with leading semicolons, whitespace, and comments
    conn.execute(f"; -- reattach comment\n ; ATTACH DATABASE '{db_path}' AS ext")

    # Cached admission MUST be invalidated despite identical file path and restored data_version!
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    cache.close()
    conn.close()
    conn_ext.close()


def test_rollback_with_leading_empty_statement_invalidates_cache() -> None:
    """Rollback statements prefixed by leading semicolons and comments invalidate cache."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE log (id INT PRIMARY KEY, msg TEXT)")
    conn.commit()

    cache = RequestVerificationCache(conn)
    coord = ("coord_rollback_leading_semi",)

    assert cache.verified("test_kind", coord) is NOT_VERIFIED
    cache.record("test_kind", coord, "admitted_pre_rollback")
    assert cache.verified("test_kind", coord) == "admitted_pre_rollback"

    # Open savepoint so rollback is valid in SQLite
    conn.execute("SAVEPOINT sp")
    # Execute rollback with leading semicolons and comments
    conn.execute("; /* comment */ ; ROLLBACK TO SAVEPOINT sp")

    # Cache MUST be invalidated by the rollback
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    cache.close()
    conn.close()


def test_attached_database_reattachment_same_file_with_bom_invalidates_cache(
    tmp_path: Path,
) -> None:
    """Detaching and reattaching the same file with UTF-8 BOM invalidates cache."""
    db_path = tmp_path / "attached_bom.db"
    conn_ext = sqlite3.connect(db_path)
    conn_ext.execute("CREATE TABLE substrate (id INT PRIMARY KEY, val TEXT)")
    conn_ext.execute("INSERT INTO substrate VALUES (1, 'v1')")
    conn_ext.commit()

    conn = sqlite3.connect(":memory:")
    conn.execute(f"ATTACH DATABASE '{db_path}' AS ext")

    cache = RequestVerificationCache(conn)
    coord = ("coord_attached_bom",)

    assert cache.verified("test_kind", coord) is NOT_VERIFIED
    cache.record("test_kind", coord, "admission_v1")
    assert cache.verified("test_kind", coord) == "admission_v1"

    # Execute DETACH with leading UTF-8 BOM (\ufeff)
    conn.execute("\ufeffDETACH DATABASE ext")

    # Update through external connection and commit (advancing ext data_version externally)
    conn_ext.execute("UPDATE substrate SET val = 'v2' WHERE id = 1")
    conn_ext.commit()

    # Reattach the SAME file with leading UTF-8 BOM (\ufeff) and parameter binding
    conn.execute("\ufeffATTACH DATABASE ? AS ext", (str(db_path),))

    # Cached admission MUST be invalidated despite identical file path and restored data_version!
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    cache.close()
    conn.close()
    conn_ext.close()


def test_rollback_with_bom_invalidates_cache() -> None:
    """Rollback statements prefixed by leading UTF-8 BOM (\\ufeff) invalidate cache."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE log (id INT PRIMARY KEY, msg TEXT)")
    conn.commit()

    cache = RequestVerificationCache(conn)
    coord = ("coord_rollback_bom",)

    assert cache.verified("test_kind", coord) is NOT_VERIFIED
    cache.record("test_kind", coord, "admitted_pre_rollback")
    assert cache.verified("test_kind", coord) == "admitted_pre_rollback"

    # Open savepoint and execute rollback with leading UTF-8 BOM (\ufeff)
    conn.execute("SAVEPOINT sp")
    conn.execute("\ufeffROLLBACK TO SAVEPOINT sp")

    # Cache MUST be invalidated by the rollback
    assert cache.verified("test_kind", coord) is NOT_VERIFIED

    cache.close()
    conn.close()


def test_connection_trace_preserves_existing_observer_and_nested_caches() -> None:
    """Existing trace observer is preserved across cache creation, nested caches, and removal."""
    conn = sqlite3.connect(":memory:")
    statements: list[str] = []
    initial_dispatchers = active_connection_trace_dispatcher_count()

    # 1. Register an existing observer directly through standard conn.set_trace_callback()
    conn.set_trace_callback(statements.append)
    assert active_connection_trace_dispatcher_count() == initial_dispatchers
    assert active_connection_trace_observer_count(conn) == 0

    conn.execute("SELECT 1")
    assert any("SELECT 1" in s for s in statements)

    # 2. Outer verification cache created
    cache1 = RequestVerificationCache(conn)
    assert active_connection_trace_observer_count(conn) == 1
    assert active_connection_trace_dispatcher_count() == initial_dispatchers + 1

    # 3. Nested inner verification cache created
    cache2 = RequestVerificationCache(conn)
    assert active_connection_trace_observer_count(conn) == 2
    assert active_connection_trace_dispatcher_count() == initial_dispatchers + 1

    # Statement dispatched to all active observers and the user trace callback
    statements.clear()
    conn.execute("SELECT 2")
    assert any("SELECT 2" in s for s in statements)

    # 4. Nested inner cache closes
    cache2.close()
    assert active_connection_trace_observer_count(conn) == 1
    assert active_connection_trace_dispatcher_count() == initial_dispatchers + 1

    # 5. Outer cache closes
    cache1.close()
    assert active_connection_trace_observer_count(conn) == 0
    assert active_connection_trace_dispatcher_count() == initial_dispatchers

    # Existing observer installed via standard set_trace_callback still receives statements!
    statements.clear()
    conn.execute("SELECT 3")
    assert any("SELECT 3" in s for s in statements)

    # 6. Unregister standard trace callback -> stops receiving statements
    conn.set_trace_callback(None)
    statements.clear()
    conn.execute("SELECT 4")
    assert len(statements) == 0

    # 7. Also test registered observers cooperatively with standard trace callback
    unreg = register_connection_trace_observer(conn, statements.append)
    conn.set_trace_callback(statements.append)
    statements.clear()
    conn.execute("SELECT 5")
    assert len([s for s in statements if "SELECT 5" in s]) == 2
    unreg()
    conn.set_trace_callback(None)

    conn.close()


def test_managed_connect_handles_positional_and_keyword_factories() -> None:
    """_managed_connect preserves positional and keyword custom factories while wrapping defaults."""
    # 1. Default connect returns ManagedConnection
    conn_default = sqlite3.connect(":memory:")
    assert isinstance(conn_default, ManagedConnection)
    conn_default.close()

    # 2. Keyword explicit default returns ManagedConnection
    conn_kw_default = sqlite3.connect(":memory:", factory=sqlite3.Connection)
    assert isinstance(conn_kw_default, ManagedConnection)
    conn_kw_default.close()

    # 3. Custom factory via keyword returns custom class, not ManagedConnection
    class CustomKeywordConn(sqlite3.Connection):
        pass

    conn_kw_custom = sqlite3.connect(":memory:", factory=CustomKeywordConn)
    assert isinstance(conn_kw_custom, CustomKeywordConn)
    assert not isinstance(conn_kw_custom, ManagedConnection)
    conn_kw_custom.close()

    # 4. Positional default factory (index 5) returns ManagedConnection
    conn_pos_default = sqlite3.connect(":memory:", 5.0, 0, "DEFERRED", True, sqlite3.Connection)
    assert isinstance(conn_pos_default, ManagedConnection)
    conn_pos_default.close()

    # 5. Positional custom factory returns custom class, not ManagedConnection
    class CustomPositionalConn(sqlite3.Connection):
        pass

    conn_pos_custom = sqlite3.connect(":memory:", 5.0, 0, "DEFERRED", True, CustomPositionalConn)
    assert isinstance(conn_pos_custom, CustomPositionalConn)
    assert not isinstance(conn_pos_custom, ManagedConnection)
    conn_pos_custom.close()


def test_unmanaged_connection_fails_closed_without_modifying_trace_callback() -> None:
    """Unmanaged connection rejects verification cache and trace observer without replacing callbacks."""

    class UnmanagedConn(sqlite3.Connection):
        pass

    conn = sqlite3.connect(":memory:", factory=UnmanagedConn)
    assert not isinstance(conn, ManagedConnection)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    # Initial query reaches the user's trace callback
    conn.execute("SELECT 'initial'")
    assert any("SELECT 'initial'" in s for s in statements)
    statements.clear()

    # Registering observer directly fails closed
    with pytest.raises(VerificationCacheError, match="requires a ManagedConnection"):
        register_connection_trace_observer(conn, statements.append)

    # Cache instantiation fails closed
    with pytest.raises(VerificationCacheError, match="requires a ManagedConnection"):
        RequestVerificationCache(conn)

    # User callback is still intact and completely unmodified
    conn.execute("SELECT 'after_failure'")
    assert any("SELECT 'after_failure'" in s for s in statements)

    conn.set_trace_callback(None)
    conn.close()


def test_verification_cache_closed_guards_fail_closed() -> None:
    """Closed RequestVerificationCache rejects all memo operations while keeping journals readable."""
    conn = sqlite3.connect(":memory:")
    cache = RequestVerificationCache(conn)
    assert cache.is_closed is False
    cache.close()
    assert cache.is_closed is True

    # Operations fail closed
    with pytest.raises(VerificationCacheError, match="closed"):
        cache.verified("kind", ("coord",))

    with pytest.raises(VerificationCacheError, match="closed"):
        cache.record("kind", ("coord",), "val")

    with pytest.raises(VerificationCacheError, match="closed"):
        cache.ensure_bound(conn)

    with pytest.raises(VerificationCacheError, match="closed"):
        cache.invalidate()

    with pytest.raises(VerificationCacheError, match="closed"):
        cache.notify_rollback()

    with pytest.raises(VerificationCacheError, match="closed"), cache.authorized_write():
        pass

    with pytest.raises(VerificationCacheError, match="closed"), cache.savepoint("sp"):
        pass

    # Journals and counts remain readable for post-turn audit
    assert isinstance(cache.journal, list)
    assert isinstance(cache.counts(), dict)

    conn.close()


def test_turn_lifecycle_cleans_up_verification_cache_and_trace_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated successful and failed turns guarantee verification cache cleanup and empty registry."""
    import uuid

    storage_root = tmp_path / "storage"
    root = tmp_path / "run"
    db_path, runtime, records = _build_fixture(root, storage_root)
    probe = _Probe()
    _install_doubles(monkeypatch, probe, records=records, runtime=runtime, enabled=True)
    _install_turn_seams(monkeypatch, probe, runtime=runtime)
    monkeypatch.setattr(engine, "uuid4", uuid.uuid4)
    initial_dispatchers = active_connection_trace_dispatcher_count()

    # 1. Run first successful turn
    events1 = list(
        engine.respond_turn(
            AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME"]),
            ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="ctx"),
            db_path=db_path,
            repo_root=root,
            retrieval_mode="sealed",
        )
    )
    assert [event["type"] for event in events1] == ["stage", "delta", "citations", "final"]
    assert active_connection_trace_dispatcher_count() == initial_dispatchers

    # 2. Run second successful turn
    events2 = list(
        engine.respond_turn(
            AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME"]),
            ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="ctx"),
            db_path=db_path,
            repo_root=root,
            retrieval_mode="sealed",
        )
    )
    assert [event["type"] for event in events2] == ["stage", "delta", "citations", "final"]
    assert active_connection_trace_dispatcher_count() == initial_dispatchers

    # 3. Run a failed turn (e.g. unknown ticker causing promotion_missing)
    events3 = list(
        engine.respond_turn(
            AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["UNKNOWN_TICKER"]),
            ContextPack(
                scope="portfolio", default_tickers=["UNKNOWN_TICKER"], system_context="ctx"
            ),
            db_path=db_path,
            repo_root=root,
            retrieval_mode="sealed",
        )
    )
    assert any(event["type"] == "error" for event in events3)
    assert active_connection_trace_dispatcher_count() == initial_dispatchers

    # 4. Direct shadow retrieval lifecycle cleanup, proving execution and trace persistence
    conn_chk = sqlite3.connect(db_path)
    try:
        traces_before = conn_chk.execute(
            "SELECT count(*) FROM heterogeneous_retrieval_trace_headers"
        ).fetchone()[0]
    finally:
        conn_chk.close()

    cache_count_before = len(probe.caches)
    engine.run_shadow_retrieval(
        QUESTION,
        AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME"]),
        ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="ctx"),
        db_path=db_path,
    )
    assert len(probe.caches) == cache_count_before + 1
    shadow_cache = probe.caches[-1]
    assert shadow_cache.is_closed is True
    assert shadow_cache.entry_count == 0
    assert shadow_cache.in_flight_count == 0
    assert len(shadow_cache.journal) > 0
    assert active_connection_trace_dispatcher_count() == initial_dispatchers

    conn_chk = sqlite3.connect(db_path)
    try:
        traces_after = conn_chk.execute(
            "SELECT count(*) FROM heterogeneous_retrieval_trace_headers"
        ).fetchone()[0]
        assert traces_after == traces_before + 1
    finally:
        conn_chk.close()


def test_turn_with_multiple_scopes_and_multiple_narrative_bundles_verifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn spanning multiple scopes and distinct narrative bundles verifies each coordinate once across all bundles."""
    storage_root = tmp_path / "storage"
    root = tmp_path / "run"
    db_path, runtime, records = _build_fixture(root, storage_root)
    probe = _Probe()

    dataset_beta = storage_root / f"run-{VECTOR_RUN_ID_BETA}" / "vectors.lance"
    storage_uri_beta = f"lance://{dataset_beta.resolve().as_posix()}#evidence_chunks"

    beta_records = [
        {
            **_vector_record(
                "beta-chunk-1",
                "Subscription demand continued in international markets.",
                [0.5, 0.5],
            ),
            "manifest_id": MANIFEST_ID_BETA,
            "ticker": "ACME",
        }
    ]

    _install_doubles(
        monkeypatch,
        probe,
        records=records,
        runtime=runtime,
        enabled=True,
        extra_records_by_run={VECTOR_RUN_ID_BETA: beta_records},
        extra_uris_by_run={VECTOR_RUN_ID_BETA: storage_uri_beta},
    )
    _install_turn_seams(monkeypatch, probe, runtime=runtime)

    conn = sqlite3.connect(db_path)
    try:
        bundle_1 = _bundle()
        bundle_2 = NarrativeBundle(
            corpus_manifest_id=MANIFEST_ID_BETA,
            lexical_index_run_id=LEXICAL_RUN_ID_BETA,
            vector_index_run_id=VECTOR_RUN_ID_BETA,
            embedding_promotion_id=EMBEDDING_PROMOTION_ID,
        )
        two_bundles = (bundle_1, bundle_2)
        bundles_json = canonical_json([b.model_dump(mode="json") for b in two_bundles])
        bundles_sha256 = digest_text(bundles_json)

        # Update research snapshot to contain both bundles
        req = _research_request()
        req_updated = req.model_copy(
            update={
                "corpus_bundles": (
                    CorpusProjectionBundle(
                        corpus_manifest_id=MANIFEST_ID,
                        lexical_index_run_id=LEXICAL_RUN_ID,
                        vector_index_run_id=VECTOR_RUN_ID,
                        embedding_promotion_id=EMBEDDING_PROMOTION_ID,
                    ),
                    CorpusProjectionBundle(
                        corpus_manifest_id=MANIFEST_ID_BETA,
                        lexical_index_run_id=LEXICAL_RUN_ID_BETA,
                        vector_index_run_id=VECTOR_RUN_ID_BETA,
                        embedding_promotion_id=EMBEDDING_PROMOTION_ID,
                    ),
                )
            }
        )
        conn.execute(
            "UPDATE research_snapshot_headers SET request_json=? WHERE research_snapshot_id=?",
            (req_updated.model_dump_json(), RESEARCH_SNAPSHOT_ID),
        )

        # Update ACME promotion to have both bundles
        conn.execute(
            "UPDATE ask_retrieval_scope_promotions SET narrative_bundles_json=?, narrative_bundles_sha256=? WHERE promotion_id=?",
            (bundles_json, bundles_sha256, "ask-promotion-1"),
        )

        # Seed BETA scope and promotion with both bundles
        conn.execute(
            "INSERT INTO v_issuer_reporting_scope_current VALUES ('scope-revision-BETA', 'BETA', ?, 'core')",
            (ISSUER_ID,),
        )
        beta_scope_id = derive_retrieval_scope_id(source_scope_key="BETA", issuer_id=ISSUER_ID)
        policy, verifier_name, verifier_version, code_sha256, config_sha256 = (
            current_verifier_identity()
        )
        inventory_json = canonical_json((INVENTORY_ID,))
        conn.execute(
            "INSERT INTO ask_retrieval_scope_promotions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ask-promotion-beta",
                "ask-promotion-beta",
                beta_scope_id,
                "BETA",
                "scope-revision-BETA",
                1,
                ISSUER_ID,
                REPORTING_ENTITY_ID,
                RESEARCH_SNAPSHOT_ID,
                RESEARCH_SNAPSHOT_SHA256,
                FACT_GENERATION_ID,
                FACT_PROJECTION_SEAL_SHA256,
                inventory_json,
                digest_text(inventory_json),
                bundles_json,
                bundles_sha256,
                NOW.isoformat(),
                POPULATION_RUN_ID,
                POPULATION_RECEIPT_SHA256,
                NOW.isoformat(),
                policy,
                verifier_name,
                verifier_version,
                code_sha256,
                config_sha256,
                "promoted",
                None,
                NOW.isoformat(),
            ),
        )

        # Seed bundle 2 tables:
        conn.execute(
            "INSERT INTO search_manifest_source_inventories VALUES (?,?)",
            (MANIFEST_ID_BETA, INVENTORY_ID),
        )
        conn.execute(
            "INSERT INTO search_index_runs VALUES (?,?,?,?,?)",
            (VECTOR_RUN_ID_BETA, MANIFEST_ID_BETA, "vector", CONFIG_SHA256, "succeeded"),
        )
        chunk_text_beta = "Subscription demand continued in international markets."
        conn.execute(
            "INSERT INTO search_chunks VALUES (?,?,?,?,?,?,?)",
            (
                "beta-chunk-1",
                MANIFEST_ID_BETA,
                digest_text(chunk_text_beta),
                "node:beta-chunk-1",
                0,
                len(chunk_text_beta),
                chunk_text_beta,
            ),
        )
        conn.execute(
            "INSERT INTO search_lexical_chunks VALUES (?,?)",
            ("beta-chunk-1", chunk_text_beta),
        )
        conn.execute(
            "INSERT INTO search_index_memberships VALUES (?,?,?)",
            (VECTOR_RUN_ID_BETA, "beta-chunk-1", "included"),
        )
        conn.execute(
            "INSERT INTO search_embedding_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                VECTOR_RUN_ID_BETA,
                "beta-chunk-1",
                "passage",
                "local-test",
                "deterministic-two-dimensional",
                2,
                beta_records[0]["vector_sha256"],
                storage_uri_beta,
                beta_records[0]["input_sha256"],
                CONFIG_SHA256,
                "succeeded",
                _runtime_artifact().sha256(),
            ),
        )
        chunk_count, chunk_sha256 = manifest_chunk_commitment(conn, manifest_id=MANIFEST_ID_BETA)
        artifact_count, artifact_sha256 = vector_artifact_commitment(
            conn, index_run_id=VECTOR_RUN_ID_BETA
        )
        assert artifact_count == chunk_count == len(beta_records)
        persist_projection_seal(
            conn,
            SearchProjectionSeal(
                projection_seal_id=PROJECTION_SEAL_ID_BETA,
                idempotency_key=PROJECTION_SEAL_ID_BETA,
                index_run_id=VECTOR_RUN_ID_BETA,
                manifest_id=MANIFEST_ID_BETA,
                index_kind="vector",
                chunk_count=chunk_count,
                chunk_set_sha256=chunk_sha256,
                projection_records_sha256=vector_records_digest(beta_records),
                artifact_set_sha256=artifact_sha256,
                provider="local-test",
                model="deterministic-two-dimensional",
                dimensions=2,
                runtime_artifact_sha256=_runtime_artifact().sha256(),
                config_sha256=CONFIG_SHA256,
                storage_uri=storage_uri_beta,
                sealed_at=NOW,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    initial_dispatchers = active_connection_trace_dispatcher_count()

    # Execute a turn with both ACME and BETA tickers
    events = list(
        engine.respond_turn(
            AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME", "BETA"]),
            ContextPack(scope="portfolio", default_tickers=["ACME", "BETA"], system_context="ctx"),
            db_path=db_path,
            repo_root=root,
            retrieval_mode="sealed",
        )
    )

    # Reaches final event cleanly with no errors
    assert [event["type"] for event in events] == ["stage", "delta", "citations", "final"]

    # Both scopes and both distinct narrative bundles verified exactly once
    assert len(probe.caches) >= 1
    cache = probe.caches[0]
    pop_receipt_coord = (POPULATION_RUN_ID,)
    assert cache.counts()[(POPULATION_RECEIPT, pop_receipt_coord)] == 1
    research_coords = [coord for (kind, coord) in cache.counts() if kind == RESEARCH_COORDINATES]
    assert len(research_coords) == 1
    assert research_coords[0][0] == RESEARCH_SNAPSHOT_ID
    assert cache.counts()[(RESEARCH_COORDINATES, research_coords[0])] == 1
    promo_coords = {
        coord[0]: count
        for (kind, coord), count in cache.counts().items()
        if kind == PROMOTION_COORDINATES
    }
    assert promo_coords == {"ask-promotion-1": 1, "ask-promotion-beta": 1}

    # Distinct narrative bundle coordinates both verified once
    assert (
        cache.counts()[
            (SEMANTIC_PROJECTION_READ, (VECTOR_RUN_ID, EMBEDDING_PROMOTION_ID, PROJECTION_SEAL_ID))
        ]
        == 1
    )
    assert (
        cache.counts()[
            (
                SEMANTIC_PROJECTION_READ,
                (VECTOR_RUN_ID_BETA, EMBEDDING_PROMOTION_ID, PROJECTION_SEAL_ID_BETA),
            )
        ]
        == 1
    )
    assert (
        cache.counts()[(SEMANTIC_RUNTIME_COORDINATES, (VECTOR_RUN_ID, EMBEDDING_PROMOTION_ID))] == 1
    )
    assert (
        cache.counts()[(SEMANTIC_RUNTIME_COORDINATES, (VECTOR_RUN_ID_BETA, EMBEDDING_PROMOTION_ID))]
        == 1
    )
    assert all(count == 1 for count in cache.counts().values())
    assert active_connection_trace_dispatcher_count() == initial_dispatchers


def test_turn_with_real_fact_candidates_verifies_projection_admission_once_through_complete_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn with multiple real fact candidates runs real search_canonical_facts and verifies admission exactly once."""
    storage_root = tmp_path / "storage"
    root = tmp_path / "run"
    db_path, runtime, records = _build_fixture(root, storage_root)
    probe = _Probe()
    _install_doubles(monkeypatch, probe, records=records, runtime=runtime, enabled=True)
    _install_turn_seams(monkeypatch, probe, runtime=runtime, audit_cites=(1, 3))

    # Restore REAL search_canonical_facts on retrieval module
    monkeypatch.setattr(retrieval_module, "search_canonical_facts", search_canonical_facts)

    # Seed projection and fact tables in fixture DB
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_fact_projection_generations (
                generation_id TEXT PRIMARY KEY,
                parent_generation_id TEXT,
                generation_kind TEXT NOT NULL,
                resolution_snapshot_id TEXT NOT NULL,
                resolution_snapshot_sha256 TEXT NOT NULL,
                resolution_watermark_sha256 TEXT NOT NULL,
                ontology_snapshot_id TEXT NOT NULL,
                ontology_snapshot_sha256 TEXT NOT NULL,
                cutoff_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                change_count INTEGER NOT NULL,
                generation_sha256 TEXT NOT NULL,
                config_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_fact_projection_audit_receipts (
                generation_id TEXT PRIMARY KEY,
                projection_seal_sha256 TEXT NOT NULL,
                verifier_name TEXT NOT NULL,
                verifier_version TEXT NOT NULL,
                verifier_code_sha256 TEXT NOT NULL,
                verifier_config_sha256 TEXT NOT NULL,
                audit_payload_json TEXT NOT NULL,
                audit_payload_sha256 TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_fact_projection_scope_bindings (
                generation_id TEXT PRIMARY KEY,
                resolution_scope_sha256 TEXT NOT NULL,
                resolution_snapshot_commitment_sha256 TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_fact_projection_entries (
                generation_id TEXT NOT NULL,
                entry_ordinal INTEGER NOT NULL,
                change_kind TEXT NOT NULL,
                digest_bucket INTEGER NOT NULL,
                canonical_metric_cell_id TEXT NOT NULL,
                canonical_resolution_revision_id TEXT,
                selected_observation_id TEXT,
                source_fact_cell_id TEXT,
                source_publication_id TEXT,
                source_publication_seal_id TEXT,
                source_publication_member_id TEXT,
                source_publication_member_sha256 TEXT,
                source_record_commitment_sha256 TEXT,
                binding_revision_id TEXT,
                binding_commitment_sha256 TEXT,
                mapping_revision_id TEXT,
                mapping_commitment_sha256 TEXT,
                metric_definition_revision_id TEXT,
                metric_definition_commitment_sha256 TEXT,
                reporting_entity_id TEXT NOT NULL,
                scope_security_id TEXT,
                canonical_metric_name TEXT NOT NULL,
                period_kind TEXT NOT NULL,
                period_start TEXT,
                period_end TEXT NOT NULL,
                dimensions_json TEXT NOT NULL,
                unit_key TEXT NOT NULL,
                currency TEXT,
                value_kind TEXT NOT NULL,
                canonical_value TEXT NOT NULL,
                evidence_document_version_id TEXT NOT NULL,
                evidence_node_id TEXT NOT NULL,
                evidence_locator_json TEXT NOT NULL,
                evidence_locator_sha256 TEXT NOT NULL,
                canonical_search_text TEXT NOT NULL,
                entry_json TEXT NOT NULL,
                entry_sha256 TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_fact_projection_buckets (
                generation_id TEXT NOT NULL,
                digest_bucket INTEGER NOT NULL,
                canonical_entry_set_json TEXT NOT NULL,
                entry_set_sha256 TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_fact_publication_seals (
                publication_seal_id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_fact_publication_members (
                publication_member_id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL,
                canonical_member_sha256 TEXT NOT NULL,
                record_commitment_sha256 TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kpi_aliases (
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                canonical_name TEXT NOT NULL,
                reported_name TEXT NOT NULL,
                effective_to TEXT
            )
            """
        )

        config_json = "{}"
        header: dict[str, object] = {
            "generation_id": FACT_GENERATION_ID,
            "parent_generation_id": None,
            "generation_kind": "checkpoint",
            "resolution_snapshot_id": "res-1",
            "resolution_snapshot_sha256": "1" * 64,
            "resolution_watermark_sha256": "2" * 64,
            "ontology_snapshot_id": "onto-1",
            "ontology_snapshot_sha256": "3" * 64,
            "cutoff_at": NOW.isoformat(),
            "recorded_at": NOW.isoformat(),
            "change_count": 2,
            "generation_sha256": "4" * 64,
            "config_json": config_json,
        }
        conn.execute(
            "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(header.values()),
        )
        seal: dict[str, object] = {
            "generation_id": FACT_GENERATION_ID,
            "projection_seal_id": "seal-1",
            "projection_seal_sha256": FACT_PROJECTION_SEAL_SHA256,
            "change_count": 2,
            "upsert_count": 2,
            "tombstone_count": 0,
            "effective_entry_count": 2,
            "batch_count": 1,
            "ordered_entry_set_sha256": "6" * 64,
        }
        conn.execute("DROP TABLE IF EXISTS canonical_fact_projection_seals")
        conn.execute(
            """
            CREATE TABLE canonical_fact_projection_seals (
                generation_id TEXT PRIMARY KEY,
                projection_seal_id TEXT,
                projection_seal_sha256 TEXT,
                change_count INTEGER,
                upsert_count INTEGER,
                tombstone_count INTEGER,
                effective_entry_count INTEGER,
                batch_count INTEGER,
                ordered_entry_set_sha256 TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO canonical_fact_projection_seals VALUES (?,?,?,?,?,?,?,?,?)",
            tuple(seal.values()),
        )
        binding: dict[str, object] = {
            "generation_id": FACT_GENERATION_ID,
            "resolution_scope_sha256": "7" * 64,
            "resolution_snapshot_commitment_sha256": "8" * 64,
        }
        conn.execute(
            "INSERT INTO canonical_fact_projection_scope_bindings VALUES (?,?,?)",
            tuple(binding.values()),
        )
        payload = projection_audit_payload(header, seal, binding)
        payload_json = canonical_json(payload)
        receipt = {
            "generation_id": FACT_GENERATION_ID,
            "projection_seal_sha256": seal["projection_seal_sha256"],
            "verifier_name": AUDIT_VERIFIER_NAME,
            "verifier_version": AUDIT_VERIFIER_VERSION,
            "verifier_code_sha256": AUDIT_VERIFIER_CODE_SHA256,
            "verifier_config_sha256": digest_text(config_json),
            "audit_payload_json": payload_json,
            "audit_payload_sha256": digest_text(payload_json),
        }
        conn.execute(
            "INSERT INTO canonical_fact_projection_audit_receipts VALUES (?,?,?,?,?,?,?,?)",
            tuple(receipt.values()),
        )

        import search.canonical_fact_projection as projection_module

        payload_columns = cast(
            tuple[str, ...], getattr(projection_module, "_ENTRY_PAYLOAD_COLUMNS")
        )
        exact = "a" * 64
        cells = [
            (
                "canonical:revenue:2025",
                "Revenue",
                "Revenue | How did revenue grow | 2025-12-31",
                "2025-12-31",
            ),
            (
                "canonical:revenue_sub:2025",
                "Subscription Revenue",
                "Subscription Revenue | How did revenue grow | 2025-12-31",
                "2025-12-31",
            ),
        ]
        entries: list[dict[str, object]] = []
        for ord_idx, (cell_id, name, search_text, p_end) in enumerate(cells):
            entry_payload: dict[str, object] = {column: None for column in payload_columns}
            entry_payload.update(
                {
                    "binding_commitment_sha256": exact,
                    "binding_revision_id": f"binding-{ord_idx}",
                    "canonical_metric_cell_id": cell_id,
                    "canonical_metric_name": name,
                    "canonical_resolution_revision_id": f"resolution-{ord_idx}",
                    "canonical_search_text": search_text,
                    "canonical_value": "100",
                    "currency": "USD",
                    "dimensions_json": "[]",
                    "evidence_document_version_id": "document-1",
                    "evidence_locator_json": "{}",
                    "evidence_locator_sha256": digest_text("{}"),
                    "evidence_node_id": "node:chunk-1",
                    "mapping_commitment_sha256": exact,
                    "mapping_revision_id": f"mapping-{ord_idx}",
                    "metric_definition_commitment_sha256": exact,
                    "metric_definition_revision_id": f"definition-{ord_idx}",
                    "period_end": p_end,
                    "period_kind": "instant",
                    "reporting_entity_id": REPORTING_ENTITY_ID,
                    "selected_observation_id": f"observation-{ord_idx}",
                    "source_publication_id": "publication-1",
                    "source_publication_member_id": f"member-{ord_idx}",
                    "source_publication_member_sha256": exact,
                    "source_publication_seal_id": "publication-seal-1",
                    "source_record_commitment_sha256": exact,
                    "unit_key": "USD",
                    "value_kind": "numeric",
                }
            )
            committed = {
                **entry_payload,
                "change_kind": "upsert",
                "entry_version": "canonical_fact_projection_entry.v1",
            }
            e_json = canonical_json(committed)
            entries.append(
                {
                    "generation_id": FACT_GENERATION_ID,
                    "entry_ordinal": ord_idx,
                    "change_kind": "upsert",
                    "digest_bucket": digest_bucket(cell_id),
                    **entry_payload,
                    "entry_json": e_json,
                    "entry_sha256": digest_text(e_json),
                }
            )

        entry_columns = cast(tuple[str, ...], getattr(projection_module, "_ENTRY_COLUMNS"))
        conn.executemany(
            "INSERT INTO canonical_fact_projection_entries ("
            + ",".join(entry_columns)
            + ") VALUES ("
            + ",".join("?" for _ in entry_columns)
            + ")",
            [tuple(entry[col] for col in entry_columns) for entry in entries],
        )

        buckets: dict[int, list[str]] = {}
        for entry in entries:
            d = cast(int, entry["digest_bucket"])
            buckets.setdefault(d, []).append(str(entry["entry_sha256"]))
        for bucket, shas in buckets.items():
            p_load = canonical_json(sorted(shas))
            conn.execute(
                "INSERT INTO canonical_fact_projection_buckets VALUES (?,?,?,?)",
                (FACT_GENERATION_ID, bucket, p_load, digest_text(p_load)),
            )

        conn.execute(
            "INSERT INTO source_fact_publication_seals VALUES (?,?)",
            ("publication-seal-1", "publication-1"),
        )
        for ord_idx in range(len(cells)):
            conn.execute(
                "INSERT INTO source_fact_publication_members VALUES (?,?,?,?)",
                (f"member-{ord_idx}", "publication-1", exact, exact),
            )

        conn.commit()
    finally:
        conn.close()

    initial_dispatchers = active_connection_trace_dispatcher_count()

    events = list(
        engine.respond_turn(
            AskTurn(text=QUESTION, session_id=SESSION_ID, tickers=["ACME"]),
            ContextPack(scope="portfolio", default_tickers=["ACME"], system_context="ctx"),
            db_path=db_path,
            repo_root=root,
            retrieval_mode="sealed",
        )
    )

    # Reaches final event cleanly with citations
    assert [event["type"] for event in events] == ["stage", "delta", "citations", "final"]
    assert len(probe.caches) >= 1
    cache = probe.caches[0]

    assert cache.counts()[(PROJECTION_GENERATION_ADMISSION, (FACT_GENERATION_ID,))] == 1
    assert active_connection_trace_dispatcher_count() == initial_dispatchers

    conn_verify = sqlite3.connect(db_path)
    try:
        cand_rows = conn_verify.execute(
            "SELECT candidate_id, candidate_kind FROM heterogeneous_retrieval_trace_candidates"
        ).fetchall()
        cand_set = {(str(r[0]), str(r[1])) for r in cand_rows}
        assert ("canonical:revenue:2025", "fact") in cand_set
        assert ("canonical:revenue_sub:2025", "fact") in cand_set

        package = probe.packages[0]
        assembly_items = {
            (item.candidate_id, item.candidate_kind) for item in package.record.retrieval_assembly
        }
        assert ("canonical:revenue:2025", "fact") in assembly_items
        assert ("canonical:revenue_sub:2025", "fact") in assembly_items
    finally:
        conn_verify.close()

    citations_events = [event for event in events if event["type"] == "citations"]
    assert len(citations_events) == 1
    items = cast(list[dict[str, object]], citations_events[0].get("items"))
    assert isinstance(items, list)
    cited_fact_items = [item for item in items if item.get("kind") == "fact"]
    assert len(cited_fact_items) == 1
    assert cited_fact_items[0]["candidate_id"] == "canonical:revenue_sub:2025"
    assert cited_fact_items[0]["value"] == "100 USD"

    fact_citations = [
        c for c in probe.packages[0].citations if c.candidate_id == "canonical:revenue_sub:2025"
    ]
    assert len(fact_citations) == 1
    assert fact_citations[0].candidate_kind == "fact"
