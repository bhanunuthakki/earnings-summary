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
    PROMOTION_COORDINATES,
    RESEARCH_COORDINATES,
    RETRIEVAL_TRACE,
    SEMANTIC_PROJECTION_READ,
    SEMANTIC_RUNTIME_COORDINATES,
    SEMANTIC_RUNTIME_INSTANCE,
    RequestVerificationCache,
    VerificationCacheError,
)

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

    def __init__(self, rows: list[dict[str, object]], storage_uri: str, probe: _Probe) -> None:
        self._rows = rows
        self._storage_uri = storage_uri
        self._probe = probe

    def read_projection(self, index_run_id: str, *, expected_count: int) -> list[dict[str, object]]:
        assert index_run_id == VECTOR_RUN_ID
        assert expected_count == len(self._rows)
        self._probe.projection_reads.append(index_run_id)
        return [dict(row) for row in self._rows]

    def published_storage_uri(self, index_run_id: str) -> str:
        assert index_run_id == VECTOR_RUN_ID
        return self._storage_uri


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


def _research_commitments() -> dict[str, object]:
    lanes = (
        ("corpus", "search_corpus_manifests", MANIFEST_ID, None),
        ("lexical_projection", "search_projection_seals", "lexical-seal-1", LEXICAL_RUN_ID),
        ("vector_projection", "search_projection_seals", PROJECTION_SEAL_ID, VECTOR_RUN_ID),
        (
            "embedding_promotion",
            "search_embedding_model_promotions",
            EMBEDDING_PROMOTION_ID,
            None,
        ),
    )
    narrative: list[dict[str, object]] = []
    for kind, table, reference_id, index_run_id in lanes:
        commitment: dict[str, object] = {
            "reference_commitment_sha256": digest_text(f"{kind}:{reference_id}"),
            "reference_id": reference_id,
            "reference_table": table,
            "requested_lane": f"{kind}:{MANIFEST_ID}",
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
) -> None:
    storage_uri = f"lance://{(runtime.index_root / f'run-{VECTOR_RUN_ID}' / 'vectors.lance').resolve().as_posix()}#evidence_chunks"

    def _index(_root: Path) -> _CountingIndex:
        return _CountingIndex(records, storage_uri, probe)

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
        return _research_commitments()

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

    def _connect(path: Path, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(path)

    def _scopes(*_args: object, **_kwargs: object) -> tuple[RetrievalScope, ...]:
        return (_scope(),)

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
            {"char_start": start, "char_end": end, "quote": quote, "cites": [1], "supported": True}
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

    conn.close()
