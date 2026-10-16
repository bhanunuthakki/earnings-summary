"""VAL-SEARCH-005: lexical-lane query quality on a synthetic sealed fixture corpus.

The lexical lanes previously compiled every question as OR-of-all-tokens
(audit ``.tmp/audit/search.md`` finding 6: the FTS compiler in
``src/search/grounded.py`` and the sealed narrative lane's inline OR join in
``src/search/heterogeneous_retrieval.py``). This suite proves the three
repaired behaviors end to end:

1. stopword handling — stopword-heavy questions retain the intended results
   and stopword-only questions retrieve nothing;
2. phrase queries — quoted multi-word phrases retrieve exactly the sealed
   fixture documents that carry the phrase adjacently;
3. kpi_aliases expansion — representative alias queries retrieve the expected
   sealed fixture document and canonical fact identities, and an absent or
   retired alias row is inert (the query then retrieves nothing).

Everything runs against an explicitly synthetic sealed fixture corpus defined
by this file. No portfolio or bench database is opened, no live LLM is called,
and no corpus/index/population production state is touched. Setting
``SEARCH_VALIDATION_EVIDENCE_DIR`` records the machine-readable
query-to-expected/actual document and fact IDs for the synthetic corpus under
that directory.

Sealed-lane fixture note: the strict research-snapshot/population admission
machinery is orthogonal to lexical query compilation, so the narrative
integration cases double ``_derive_research_coordinates`` (the same module
seam the per-turn verification-dedup suite uses) while the lexical lanes,
ranking, trace persistence, and full trace re-verification — including the
re-planned lexical recompute — run real. The fact-lane cases double only
``admit_canonical_projection_for_read`` (the upstream strict-admission
receipt) while the real candidate scan, per-hit entry/bucket commitment
verification, and publication-lineage verification run against the synthetic
projection. The legacy ``kpi_aliases`` table is dropped at the checkout's
``0002_drop_dead_tables`` migration; fixtures recreate its historical shape to
prove expansion, and the production reader resolves an empty map when the
table is absent.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import search.canonical_fact_projection as projection_module
import search.heterogeneous_retrieval as retrieval_module
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionSnapshotScope,
)
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.metric_ontology import MetricOntology, OntologySnapshot
from provenance.search_index_lineage import (
    SearchProjectionSeal,
    lexical_projection_commitment,
    manifest_chunk_commitment,
    persist_projection_seal,
)
from provenance.source_fact_stream import bind_resolution_snapshot_watermark
from search.canonical_fact_projection import (
    ProjectionConfig,
    ProjectionGenerationRequest,
    VerifiedCanonicalProjectionGeneration,
    build_canonical_projection_generation,
    canonical_json,
    digest_bucket,
    digest_text,
    search_canonical_facts,
)
from search.corpus_builder import lexical_index_config_sha256
from search.grounded import (
    CorpusDocumentMembership,
    CorpusManifest,
    CorpusManifestSeal,
    GroundedSearchStore,
    HybridRetriever,
    IndexMembership,
    IndexRun,
    SearchChunk,
    SearchFilter,
    membership_digest,
)
from search.heterogeneous_retrieval import (
    HeterogeneousRetrievalRequest,
    NarrativeBundle,
    RetrievalFilters,
    retrieve_heterogeneous,
)
from search.lexical_query import (
    MAX_OPERANDS,
    kpi_alias_expansions,
    plan_lexical_query,
)
from search.request_verification import RequestVerificationCache

STAMP = datetime(2026, 7, 26, 20, 0, 0)
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
A, B, C = "a" * 64, "b" * 64, "c" * 64

_EVIDENCE_DIR_ENV = "SEARCH_VALIDATION_EVIDENCE_DIR"
_SCOPE_NOTE = (
    "Deterministic synthetic fixture evidence only: hand-built sealed corpus "
    "documents, canonical fact projection entries, and kpi_aliases rows defined "
    "by the test. No live Ask/Claude turn, no SEC or source request, no bench or "
    "portfolio database access. The kpi_aliases table is dropped at the "
    "checkout's 0002 migration; the fixture recreates the legacy shape to prove "
    "expansion and the production reader resolves an empty map when it is "
    "absent. No retrieval-quality, latency, or production reachability claim is "
    "made."
)

_ALIAS_ROW_SQL = (
    "INSERT INTO kpi_aliases "
    "(id,ticker,canonical_name,reported_name,effective_from,effective_to,concept_id) "
    "VALUES (1,NULL,'Monthly Active Users','MAU',NULL,?,NULL)"
)

_KPI_ALIASES_DDL = """
CREATE TABLE kpi_aliases (
    id INTEGER PRIMARY KEY,
    ticker TEXT,
    canonical_name TEXT NOT NULL,
    reported_name TEXT NOT NULL,
    effective_from TEXT,
    effective_to TEXT,
    concept_id INTEGER
);
"""


def _record_evidence(name: str, payload: dict[str, object]) -> None:
    evidence_dir = os.environ.get(_EVIDENCE_DIR_ENV)
    if not evidence_dir:
        return
    directory = Path(evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Planner unit cases: stopword handling, phrase operands, alias expansion,
# bounded operands, inert compilation.
# ---------------------------------------------------------------------------


def test_stopword_heavy_question_plans_content_terms_only() -> None:
    plan = plan_lexical_query("What is the and of why did margins fall in 2024?")
    assert plan is not None
    assert plan.operands == ("margins", "fall", "2024")
    assert plan.groups == (("margins",), ("fall",), ("2024",))
    assert plan.match_all == '"margins" AND "fall" AND "2024"'
    assert plan.match_any == '"margins" OR "fall" OR "2024"'


def test_stopword_only_question_has_no_plan() -> None:
    assert plan_lexical_query("what is the and of") is None
    assert plan_lexical_query("") is None


def test_quoted_phrase_compiles_to_adjacent_phrase_operand() -> None:
    plan = plan_lexical_query('why did "operating margin" decline in 2025')
    assert plan is not None
    assert plan.groups == (("operating margin",), ("decline",), ("2025",))
    assert plan.match_all == '"operating margin" AND "decline" AND "2025"'
    assert plan.match_any == '"operating margin" OR "decline" OR "2025"'


def test_single_word_quote_is_a_plain_token() -> None:
    plan = plan_lexical_query('the "growth" story')
    assert plan is not None
    assert plan.operands == ("growth", "story")


def test_kpi_alias_expansion_or_variants_into_and_groups() -> None:
    aliases = {"mau": ("monthly active users",), "monthly active users": ("mau",)}
    plan = plan_lexical_query("What drove MAU growth?", aliases=aliases)
    assert plan is not None
    assert plan.groups == (("drove",), ("MAU", "monthly active users"), ("growth",))
    assert plan.match_all == '"drove" AND ("MAU" OR "monthly active users") AND "growth"'
    assert plan.match_any == '"drove" OR "MAU" OR "monthly active users" OR "growth"'


def test_alias_expansion_of_a_phrase_goes_the_other_direction() -> None:
    aliases = {"mau": ("monthly active users",), "monthly active users": ("mau",)}
    plan = plan_lexical_query('why did "monthly active users" decline', aliases=aliases)
    assert plan is not None
    assert plan.groups == (("monthly active users", "mau"), ("decline",))


def test_hostile_text_compiles_to_inert_quoted_operands() -> None:
    # Operator-looking words become quoted strings, never FTS5 operators.
    plan = plan_lexical_query('why did margins* NEAR("operating margin", 5) drop')
    assert plan is not None
    assert plan.match_all == '"operating margin" AND "margins" AND "NEAR" AND "drop"'


def test_operand_cap_bounds_expression_deterministically() -> None:
    question = " ".join(f"word{index}" for index in range(60))
    plan = plan_lexical_query(question)
    assert plan is not None
    assert len(plan.operands) == MAX_OPERANDS
    assert plan.operands == tuple(f"word{index}" for index in range(MAX_OPERANDS))


def test_plan_is_deterministic_for_repeated_questions() -> None:
    aliases = {"mau": ("monthly active users",), "monthly active users": ("mau",)}
    first = plan_lexical_query('why did "operating margin" MAU decline', aliases=aliases)
    second = plan_lexical_query('why did "operating margin" MAU decline', aliases=aliases)
    assert first == second


def test_kpi_alias_reader_reads_only_unretired_rows() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        assert kpi_alias_expansions(conn) == {}
        conn.execute(_KPI_ALIASES_DDL)
        conn.execute(_ALIAS_ROW_SQL, (None,))
        conn.execute(
            _ALIAS_ROW_SQL.replace("VALUES (1,", "VALUES (2,").replace("'MAU'", "'Churned'"),
            ("2020-01-01",),
        )
        conn.commit()
        expansions = kpi_alias_expansions(conn)
        assert expansions == {"mau": ("monthly active users",), "monthly active users": ("mau",)}
        assert "churned" not in expansions
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Grounded narrative lane (public HybridRetriever) over a synthetic sealed
# fixture corpus built through the public store API.
# ---------------------------------------------------------------------------

_GROUNDED_CHUNKS = (
    ("chunk-margin", "Operating margin expanded on pricing discipline."),
    ("chunk-scatter", "Operating leverage improved while margin compression eased."),
    ("chunk-mau", "Monthly active users grew twelve percent year over year."),
    ("chunk-noise", "the what and the of"),
)


def _grounded_conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    # Session-cached migrated template at the active head, copied per test —
    # the repository's canonical current-schema application path (the same
    # ``migrated_db`` fixture the migration-cost ratchet exists to enforce),
    # instead of replaying Alembic upgrades inside a test file.
    path = migrated_db(tmp_path / "grounded-search.db", target="head")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(_KPI_ALIASES_DDL)
    return conn


def _seed_grounded_corpus(conn: sqlite3.Connection) -> None:
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=A,
            byte_size=1,
            media_type="text/plain",
            storage_uri="file:///acme",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="obs",
            idempotency_key="obs",
            source_kind="sec",
            source_url="https://sec.test/acme",
            blob_sha256=A,
            source_published_at=STAMP,
            filing_at=STAMP,
            accepted_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=B,
            collector_code_version="collector@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="doc",
            document_key="ACME:10-Q",
            version_sequence=1,
            observation_id="obs",
            blob_sha256=A,
            issuer_id="issuer",
            ticker="ACME",
            document_type="filing",
            form_type="10-Q",
            language="en",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id="run",
            idempotency_key="run",
            document_version_id="doc",
            input_sha256=A,
            extractor_name="parser",
            extractor_config_sha256=B,
            extractor_code_version="parser@1",
            output_sha256=C,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    for chunk_id, text in _GROUNDED_CHUNKS:
        ledger.persist(
            EvidenceNode(
                node_id=f"node-{chunk_id}",
                evidence_key=f"ACME:{chunk_id}",
                revision=1,
                extraction_run_id="run",
                node_kind="passage",
                text=text,
                recorded_at=STAMP,
            )
        )
    store = GroundedSearchStore(conn)
    member = CorpusDocumentMembership(
        membership_id="manifest:included:doc",
        manifest_id="manifest",
        expected_document_key="ACME:10-Q",
        document_version_id="doc",
        membership_status="included",
        reason="eligible",
        recorded_at=STAMP,
    )
    store.persist(
        CorpusManifest(
            manifest_id="manifest",
            idempotency_key="manifest",
            corpus_key="all-company-reporting",
            revision=1,
            selection_config_sha256=A,
            selector_code_version="selector@1",
            recorded_at=STAMP,
        )
    )
    store.persist(member)
    for chunk_id, text in _GROUNDED_CHUNKS:
        store.persist(
            SearchChunk(
                chunk_id=chunk_id,
                idempotency_key=chunk_id,
                manifest_id="manifest",
                evidence_node_id=f"node-{chunk_id}",
                chunk_key=f"manifest:ACME:{chunk_id}:0",
                chunk_revision=1,
                text=text,
                char_start=0,
                char_end=len(text),
                chunker_config_sha256=B,
                chunker_code_version="chunker@1",
                available_at=STAMP,
                recorded_at=STAMP,
            )
        )
    store.persist(
        IndexRun(
            index_run_id="manifest:lexical:1",
            idempotency_key="manifest:lexical:1",
            index_key="manifest:lexical",
            revision=1,
            manifest_id="manifest",
            index_kind="lexical",
            config_sha256=lexical_index_config_sha256(conn, manifest_id="manifest"),
            code_version="indexer@1",
            outcome="succeeded",
            failure_reason=None,
            started_at=STAMP,
            completed_at=STAMP,
        )
    )
    for chunk_id, _text in _GROUNDED_CHUNKS:
        store.persist(
            IndexMembership(
                index_run_id="manifest:lexical:1",
                chunk_id=chunk_id,
                membership_status="included",
                recorded_at=STAMP,
            )
        )
    store.persist(
        CorpusManifestSeal(
            manifest_id="manifest",
            expected_document_count=1,
            membership_digest_sha256=membership_digest([member]),
            completion_status="complete",
            sealed_at=STAMP,
        )
    )
    # The active-head view ``v_search_index_successful`` admits an index run
    # only through its sealed physical projection, so the fixture seals the
    # lexical projection with the real commitment helpers. The insert-time
    # run-contract trigger requires the complete manifest seal above.
    chunk_count, chunk_set_sha256 = manifest_chunk_commitment(conn, manifest_id="manifest")
    _projection_count, projection_records_sha256 = lexical_projection_commitment(
        conn, manifest_id="manifest"
    )
    persist_projection_seal(
        conn,
        SearchProjectionSeal(
            projection_seal_id="manifest:lexical:seal:1",
            idempotency_key="manifest:lexical:seal:1",
            index_run_id="manifest:lexical:1",
            manifest_id="manifest",
            index_kind="lexical",
            chunk_count=chunk_count,
            chunk_set_sha256=chunk_set_sha256,
            projection_records_sha256=projection_records_sha256,
            config_sha256=lexical_index_config_sha256(conn, manifest_id="manifest"),
            storage_uri="sqlite:///fixture/lexical",
            sealed_at=STAMP,
        ),
    )


def _grounded_ids(conn: sqlite3.Connection, query: str) -> list[str]:
    return [
        bundle.chunk_id
        for bundle in HybridRetriever(conn).search(
            query, "manifest", SearchFilter(issuer_id="issuer"), limit=4
        )
    ]


def test_grounded_lane_stopword_heavy_question_retains_intended_documents(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _grounded_conn(tmp_path, migrated_db)
    try:
        _seed_grounded_corpus(conn)
        ids = _grounded_ids(conn, "what did the company say about margin and the")
        # The intended documents are still retrieved; the stopword-only decoy
        # chunk that the old OR-of-all-tokens compiler matched on filler words
        # ("what", "the", "and", "of") no longer occupies a candidate slot.
        assert "chunk-margin" in ids
        assert "chunk-scatter" in ids
        assert "chunk-noise" not in ids
        assert _grounded_ids(conn, "what is the and of") == []
        _record_evidence(
            "val-search-005-grounded-stopwords.json",
            {
                "cases": [
                    {
                        "query": "what did the company say about margin and the",
                        "expected_ids": [
                            "chunk-margin",
                            "chunk-scatter",
                        ],
                        "expected_excluded_ids": ["chunk-noise"],
                        "actual_ids": ids,
                    },
                    {
                        "query": "what is the and of",
                        "expected_ids": [],
                        "actual_ids": _grounded_ids(conn, "what is the and of"),
                    },
                ],
                "fixture_corpus": {
                    "chunks": dict(_GROUNDED_CHUNKS),
                },
                "scope_note": _SCOPE_NOTE,
            },
        )
    finally:
        conn.close()


def test_grounded_lane_phrase_query_matches_only_adjacent_documents(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _grounded_conn(tmp_path, migrated_db)
    try:
        _seed_grounded_corpus(conn)
        # The quoted phrase requires adjacency: chunk-scatter carries both
        # words non-adjacently and must not match the phrase operand.
        assert _grounded_ids(conn, '"operating margin"') == ["chunk-margin"]
        assert _grounded_ids(conn, 'why did "operating margin" change') == ["chunk-margin"]
        # Without quotes the words stay independent tokens, so both documents
        # that carry the two words are retrieved.
        assert set(_grounded_ids(conn, "operating margin")) == {"chunk-margin", "chunk-scatter"}
        _record_evidence(
            "val-search-005-grounded-phrases.json",
            {
                "cases": [
                    {
                        "query": '"operating margin"',
                        "expected_ids": ["chunk-margin"],
                        "expected_excluded_ids": ["chunk-scatter"],
                        "actual_ids": _grounded_ids(conn, '"operating margin"'),
                    },
                    {
                        "query": 'why did "operating margin" change',
                        "expected_ids": ["chunk-margin"],
                        "actual_ids": _grounded_ids(conn, 'why did "operating margin" change'),
                    },
                ],
                "fixture_corpus": {"chunks": dict(_GROUNDED_CHUNKS)},
                "scope_note": _SCOPE_NOTE,
            },
        )
    finally:
        conn.close()


def test_grounded_lane_kpi_alias_query_retrieves_alias_document(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _grounded_conn(tmp_path, migrated_db)
    try:
        _seed_grounded_corpus(conn)
        conn.execute(_ALIAS_ROW_SQL, (None,))
        conn.commit()
        # The fixture document spells the metric out; only the alias expansion
        # bridges the "MAU" query to it.
        assert _grounded_ids(conn, "did MAU grow in the recent release") == ["chunk-mau"]
        # An absent alias row is inert: the same question retrieves nothing.
        conn.execute("DELETE FROM kpi_aliases")
        conn.commit()
        assert _grounded_ids(conn, "did MAU grow in the recent release") == []
        _record_evidence(
            "val-search-005-grounded-kpi-aliases.json",
            {
                "cases": [
                    {
                        "query": "did MAU grow in the recent release",
                        "alias_rows": [
                            "(canonical_name='Monthly Active Users', reported_name='MAU')"
                        ],
                        "expected_ids": ["chunk-mau"],
                        "actual_ids": ["chunk-mau"],
                    },
                    {
                        "query": "did MAU grow in the recent release",
                        "alias_rows": [],
                        "expected_ids": [],
                        "actual_ids": [],
                    },
                ],
                "fixture_corpus": {
                    "chunks": dict(_GROUNDED_CHUNKS),
                },
                "scope_note": _SCOPE_NOTE,
            },
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sealed narrative lane (public retrieve_heterogeneous + full trace
# re-verification) over a hand-built synthetic sealed corpus.
# ---------------------------------------------------------------------------

_SEALED_CHUNKS = (
    ("sealed-phrase", "Operating margin expanded on pricing discipline."),
    ("sealed-scatter", "Operating leverage improved while margin compression eased."),
    ("sealed-mau", "Monthly active users grew twelve percent year over year."),
    ("sealed-noise", "the what and the of"),
)

_SEALED_SCHEMA = (
    """
CREATE VIRTUAL TABLE search_lexical_chunks USING fts5(chunk_id UNINDEXED, text);
CREATE TABLE search_chunks (
    chunk_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL, content_sha256 TEXT NOT NULL,
    evidence_node_id TEXT NOT NULL, char_start INTEGER NOT NULL, char_end INTEGER NOT NULL,
    text TEXT NOT NULL
);
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
CREATE TABLE research_snapshot_headers (
    research_snapshot_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, cutoff_at TEXT NOT NULL
);
CREATE TABLE heterogeneous_retrieval_trace_headers (
    trace_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, research_snapshot_id TEXT,
    research_snapshot_sha256 TEXT, fact_generation_id TEXT,
    fact_projection_seal_sha256 TEXT, narrative_commitments_json TEXT,
    narrative_commitments_sha256 TEXT, semantic_receipts_json TEXT,
    semantic_receipts_sha256 TEXT, query_sha256 TEXT, query_json TEXT, ranker_json TEXT,
    ranker_sha256 TEXT, filters_json TEXT, filters_sha256 TEXT, candidate_limit INTEGER,
    result_limit INTEGER, cutoff_at TEXT, recorded_at TEXT
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
    + _KPI_ALIASES_DDL
)


def _sealed_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "sealed-fixture.sqlite")
    conn.executescript(_SEALED_SCHEMA)
    for chunk_id, text in _SEALED_CHUNKS:
        conn.execute("INSERT INTO search_lexical_chunks VALUES (?,?)", (chunk_id, text))
        conn.execute(
            "INSERT INTO search_chunks VALUES (?,?,?,?,?,?,?)",
            (chunk_id, "manifest", digest_text(text), f"node-{chunk_id}", 0, len(text), text),
        )
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,?,?)",
            (
                f"node-{chunk_id}",
                "extraction-run",
                "passage",
                canonical_json({"section": "MD&A"}),
            ),
        )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?)", ("extraction-run", "sealed-doc")
    )
    conn.execute(
        "INSERT INTO v_evidence_document_versions_canonical VALUES (?,?,?)",
        ("sealed-doc", "issuer", "reporting-entity"),
    )
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?)",
        ("research-snapshot", "{}", NOW.isoformat()),
    )
    conn.commit()
    return conn


def _research_commitments() -> dict[str, object]:
    return {
        "fact_projection_seal_sha256": "f" * 64,
        "narrative": [
            {
                "reference_commitment_sha256": digest_text("corpus:manifest"),
                "reference_id": "manifest",
                "reference_table": "search_corpus_manifests",
                "requested_lane": "corpus:manifest",
            },
            {
                "reference_commitment_sha256": digest_text("lexical_projection:lexical"),
                "reference_id": "lexical-seal",
                "reference_table": "search_projection_seals",
                "requested_lane": "lexical_projection:manifest",
                "index_run_id": "lexical",
            },
        ],
        "research_snapshot_sha256": "e" * 64,
    }


def _derive_research(
    _conn: sqlite3.Connection,
    _request: HeterogeneousRetrievalRequest,
    _verification: RequestVerificationCache | None,
) -> dict[str, object]:
    return _research_commitments()


def _sealed_request(trace_id: str, query: str) -> HeterogeneousRetrievalRequest:
    return HeterogeneousRetrievalRequest(
        trace_id=trace_id,
        idempotency_key=trace_id,
        research_snapshot_id="research-snapshot",
        fact_generation_id="generation-facts",
        narrative_bundles=(
            NarrativeBundle(corpus_manifest_id="manifest", lexical_index_run_id="lexical"),
        ),
        query_text=query,
        candidate_limit=10,
        result_limit=4,
        filters=RetrievalFilters(include_narrative=True, include_facts=False),
        cutoff_at=NOW,
        recorded_at=NOW,
    )


def _sealed_ids(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, trace_id: str, query: str
) -> list[str]:
    monkeypatch.setattr(retrieval_module, "_derive_research_coordinates", _derive_research)
    receipt = retrieve_heterogeneous(conn, _sealed_request(trace_id, query))
    return [str(item["candidate_id"]) for item in receipt.ordered_results]


def test_sealed_lane_stopword_heavy_question_retains_intended_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _sealed_conn(tmp_path)
    try:
        ids = _sealed_ids(
            conn, monkeypatch, "trace-stopwords", "what did the company say about margin and the"
        )
        assert set(ids) == {"sealed-phrase", "sealed-scatter"}
        assert "sealed-noise" not in ids
        empty_ids = _sealed_ids(conn, monkeypatch, "trace-stopword-only", "what is the and of")
        assert empty_ids == []
        _record_evidence(
            "val-search-005-sealed-stopwords.json",
            {
                "cases": [
                    {
                        "query": "what did the company say about margin and the",
                        "expected_ids": ["sealed-phrase", "sealed-scatter"],
                        "expected_excluded_ids": ["sealed-noise"],
                        "actual_ids": ids,
                    },
                    {
                        "query": "what is the and of",
                        "expected_ids": [],
                        "actual_ids": empty_ids,
                    },
                ],
                "fixture_corpus": {"chunks": dict(_SEALED_CHUNKS)},
                "scope_note": _SCOPE_NOTE,
            },
        )
    finally:
        conn.close()


def test_sealed_lane_phrase_query_matches_only_adjacent_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _sealed_conn(tmp_path)
    try:
        phrase_ids = _sealed_ids(conn, monkeypatch, "trace-phrase", '"operating margin"')
        assert phrase_ids == ["sealed-phrase"]
        change_ids = _sealed_ids(
            conn, monkeypatch, "trace-phrase-change", 'why did "operating margin" change'
        )
        assert change_ids == ["sealed-phrase"]
        unquoted_ids = _sealed_ids(conn, monkeypatch, "trace-phrase-unquoted", "operating margin")
        assert set(unquoted_ids) == {"sealed-phrase", "sealed-scatter"}
        _record_evidence(
            "val-search-005-sealed-phrases.json",
            {
                "cases": [
                    {
                        "query": '"operating margin"',
                        "expected_ids": ["sealed-phrase"],
                        "expected_excluded_ids": ["sealed-scatter"],
                        "actual_ids": phrase_ids,
                    },
                    {
                        "query": 'why did "operating margin" change',
                        "expected_ids": ["sealed-phrase"],
                        "actual_ids": change_ids,
                    },
                ],
                "fixture_corpus": {"chunks": dict(_SEALED_CHUNKS)},
                "scope_note": _SCOPE_NOTE,
            },
        )
    finally:
        conn.close()


def test_sealed_lane_kpi_alias_query_retrieves_alias_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _sealed_conn(tmp_path)
    try:
        conn.execute(_ALIAS_ROW_SQL, (None,))
        conn.commit()
        ids = _sealed_ids(conn, monkeypatch, "trace-alias", "did MAU grow in the recent release")
        assert ids == ["sealed-mau"]
        # Retire the alias: the same question must retrieve nothing.
        conn.execute("UPDATE kpi_aliases SET effective_to='2020-01-01'")
        conn.commit()
        retired_ids = _sealed_ids(
            conn, monkeypatch, "trace-alias-retired", "did MAU grow in the recent release"
        )
        assert retired_ids == []
        # The earlier sealed trace MUST remain verifiable even after alias retirement
        reverified = retrieval_module.verify_heterogeneous_retrieval_trace(conn, "trace-alias")
        assert [str(item["candidate_id"]) for item in reverified.ordered_results] == ["sealed-mau"]
        _record_evidence(
            "val-search-005-sealed-kpi-aliases.json",
            {
                "cases": [
                    {
                        "query": "did MAU grow in the recent release",
                        "alias_rows": [
                            "(canonical_name='Monthly Active Users', reported_name='MAU', active)"
                        ],
                        "expected_ids": ["sealed-mau"],
                        "actual_ids": ids,
                    },
                    {
                        "query": "did MAU grow in the recent release",
                        "alias_rows": [
                            "(canonical_name='Monthly Active Users', reported_name='MAU', retired)"
                        ],
                        "expected_ids": [],
                        "actual_ids": retired_ids,
                    },
                ],
                "fixture_corpus": {"chunks": dict(_SEALED_CHUNKS)},
                "scope_note": _SCOPE_NOTE,
            },
        )
    finally:
        conn.close()


def test_sealed_lane_unquoted_multiword_kpi_alias_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unquoted multiword alias term expands to its counterpart and retrieves the sealed document."""
    conn = _sealed_conn(tmp_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kpi_definitions (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kpi_definition_revisions (
                kpi_definition_revision_id TEXT PRIMARY KEY,
                kpi_definition_id INTEGER NOT NULL,
                reported_label TEXT NOT NULL,
                status TEXT NOT NULL,
                lifecycle TEXT NOT NULL
            )
            """
        )
        conn.execute("INSERT INTO kpi_definitions VALUES (1, 'AAPL', 'Subscription Revenue')")
        conn.execute(
            "INSERT INTO kpi_definition_revisions VALUES ('rev-1', 1, 'Services ARR', 'admitted', 'active')"
        )
        chunk_text = "Subscription revenue expanded thirty percent on cloud growth."
        conn.execute(
            "INSERT INTO search_lexical_chunks VALUES ('sealed-sub-rev', ?)", (chunk_text,)
        )
        conn.execute(
            "INSERT INTO search_chunks VALUES ('sealed-sub-rev', 'manifest', ?, 'node-sub-rev', 0, ?, ?)",
            (digest_text(chunk_text), len(chunk_text), chunk_text),
        )
        conn.execute(
            "INSERT INTO evidence_nodes VALUES ('node-sub-rev', 'extraction-run', 'passage', ?)",
            (canonical_json({"section": "MD&A"}),),
        )
        conn.commit()

        # Unquoted question with multiword alias term 'Services ARR'
        query = "What drove Services ARR in recent period"
        ids = _sealed_ids(conn, monkeypatch, "trace-multiword-alias", query)
        assert ids == ["sealed-sub-rev"]

        # Verify trace re-verification succeeds
        reverified = retrieval_module.verify_heterogeneous_retrieval_trace(
            conn, "trace-multiword-alias"
        )
        assert [str(item["candidate_id"]) for item in reverified.ordered_results] == [
            "sealed-sub-rev"
        ]

        # Discontinue the revision
        conn.execute(
            "UPDATE kpi_definition_revisions SET lifecycle='discontinued' WHERE kpi_definition_revision_id='rev-1'"
        )
        conn.commit()

        # The earlier sealed trace MUST remain verifiable even after retirement
        reverified_after = retrieval_module.verify_heterogeneous_retrieval_trace(
            conn, "trace-multiword-alias"
        )
        assert [str(item["candidate_id"]) for item in reverified_after.ordered_results] == [
            "sealed-sub-rev"
        ]

        # A new query after retirement no longer expands the alias
        retired_ids = _sealed_ids(conn, monkeypatch, "trace-multiword-retired", query)
        assert retired_ids == []
    finally:
        conn.close()


def test_sealed_lane_unquoted_multiword_alias_and_canonical_name_replay_after_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a query contains both an unquoted multiword alias and its canonical name, trace replays cleanly even after alias retirement."""
    conn = _sealed_conn(tmp_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kpi_definitions (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kpi_definition_revisions (
                kpi_definition_revision_id TEXT PRIMARY KEY,
                kpi_definition_id INTEGER NOT NULL,
                reported_label TEXT NOT NULL,
                status TEXT NOT NULL,
                lifecycle TEXT NOT NULL
            )
            """
        )
        conn.execute("INSERT INTO kpi_definitions VALUES (1, 'AAPL', 'Subscription Revenue')")
        conn.execute(
            "INSERT INTO kpi_definition_revisions VALUES ('rev-1', 1, 'Services ARR', 'admitted', 'active')"
        )
        chunk_text = "Services ARR subscription revenue expanded thirty percent on cloud growth."
        conn.execute(
            "INSERT INTO search_lexical_chunks VALUES ('sealed-both-sub', ?)", (chunk_text,)
        )
        conn.execute(
            "INSERT INTO search_chunks VALUES ('sealed-both-sub', 'manifest', ?, 'node-both-sub', 0, ?, ?)",
            (digest_text(chunk_text), len(chunk_text), chunk_text),
        )
        conn.execute(
            "INSERT INTO evidence_nodes VALUES ('node-both-sub', 'extraction-run', 'passage', ?)",
            (canonical_json({"section": "MD&A"}),),
        )
        conn.commit()

        # Query contains both unquoted multiword alias 'Services ARR' AND its canonical counterpart 'subscription revenue'
        query = "Services ARR subscription revenue cloud growth"
        ids = _sealed_ids(conn, monkeypatch, "trace-both-terms", query)
        assert ids == ["sealed-both-sub"]

        # Verify initial trace verification succeeds
        reverified = retrieval_module.verify_heterogeneous_retrieval_trace(conn, "trace-both-terms")
        assert [str(item["candidate_id"]) for item in reverified.ordered_results] == [
            "sealed-both-sub"
        ]

        # Retire the alias definition
        conn.execute(
            "UPDATE kpi_definition_revisions SET lifecycle='discontinued' WHERE kpi_definition_revision_id='rev-1'"
        )
        conn.commit()

        # Re-verification MUST still succeed using the committed alias plan in candidate lineage
        reverified_after = retrieval_module.verify_heterogeneous_retrieval_trace(
            conn, "trace-both-terms"
        )
        assert [str(item["candidate_id"]) for item in reverified_after.ordered_results] == [
            "sealed-both-sub"
        ]
    finally:
        conn.close()


def test_verify_legacy_trace_without_lexical_plan_in_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy trace generated without lexical_plan in candidate lineage verifies via legacy replay."""
    conn = _sealed_conn(tmp_path)
    try:
        query = "operating margin"
        ids = _sealed_ids(conn, monkeypatch, "trace-legacy-compat", query)
        assert len(ids) > 0

        # Strip lexical_plan from candidate lineage to simulate a legacy trace
        candidates = conn.execute(
            "SELECT candidate_ordinal, candidate_json, lineage_json FROM heterogeneous_retrieval_trace_candidates WHERE trace_id='trace-legacy-compat'"
        ).fetchall()
        updated_candidates: list[dict[str, object]] = []
        for ordinal, c_json_str, l_json_str in candidates:
            c_dict = cast(dict[str, object], json.loads(str(c_json_str)))
            l_dict = cast(dict[str, object], json.loads(str(l_json_str)))
            l_dict.pop("lexical_plan", None)
            new_l_json = canonical_json(l_dict)
            new_l_sha = digest_text(new_l_json)
            c_dict["lineage_sha256"] = new_l_sha
            new_c_json = canonical_json(c_dict)
            updated_candidates.append(c_dict)
            conn.execute(
                "UPDATE heterogeneous_retrieval_trace_candidates SET lineage_json=?, lineage_sha256=?, candidate_json=?, candidate_sha256=? WHERE trace_id='trace-legacy-compat' AND candidate_ordinal=?",
                (new_l_json, new_l_sha, new_c_json, digest_text(new_c_json), ordinal),
            )

        cand_set_json = canonical_json(updated_candidates)
        cand_set_sha = digest_text(cand_set_json)
        seal_row = conn.execute(
            "SELECT trace_json FROM heterogeneous_retrieval_trace_seals WHERE trace_id='trace-legacy-compat'"
        ).fetchone()
        assert seal_row is not None
        seal_dict = cast(dict[str, object], json.loads(str(seal_row[0])))
        seal_dict["candidate_set_sha256"] = cand_set_sha
        new_trace_json = canonical_json(seal_dict)
        conn.execute(
            "UPDATE heterogeneous_retrieval_trace_seals SET canonical_candidate_set_json=?, candidate_set_sha256=?, trace_json=?, trace_sha256=? WHERE trace_id='trace-legacy-compat'",
            (cand_set_json, cand_set_sha, new_trace_json, digest_text(new_trace_json)),
        )
        conn.commit()

        monkeypatch.setattr(retrieval_module, "_derive_research_coordinates", _derive_research)
        receipt = retrieval_module.verify_heterogeneous_retrieval_trace(conn, "trace-legacy-compat")
        assert [str(item["candidate_id"]) for item in receipt.ordered_results] == ids
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sealed fact lane (public search_canonical_facts) over a hand-built synthetic
# canonical fact projection; only the upstream strict-admission receipt is
# doubled, the scan and per-hit commitment verification are real.
# ---------------------------------------------------------------------------

_FACT_CELLS = (
    (
        "canonical:revenue:2024",
        "Revenue",
        "Total Revenue | Revenue recognized from customer contracts | 2024-06-30",
        "2024-06-30T00:00:00.000000Z",
    ),
    (
        "canonical:revenue:2023",
        "Revenue",
        "Total Revenue | Revenue recognized from customer contracts | 2023-06-30",
        "2023-06-30T00:00:00.000000Z",
    ),
    (
        "canonical:mau:2025",
        "Monthly Active Users",
        "Monthly Active Users | Users active in the trailing thirty days | 2025-12-31",
        "2025-12-31T00:00:00.000000Z",
    ),
)


def _fact_entry(
    generation_id: str,
    ordinal: int,
    *,
    cell_id: str,
    metric_name: str,
    search_text: str,
    period_end: str,
) -> dict[str, object]:
    payload_columns = cast(tuple[str, ...], getattr(projection_module, "_ENTRY_PAYLOAD_COLUMNS"))
    exact = "a" * 64
    payload: dict[str, object] = {column: None for column in payload_columns}
    payload.update(
        {
            "binding_commitment_sha256": exact,
            "binding_revision_id": f"binding-{ordinal}",
            "canonical_metric_cell_id": cell_id,
            "canonical_metric_name": metric_name,
            "canonical_resolution_revision_id": f"resolution-{ordinal}",
            "canonical_search_text": search_text,
            "canonical_value": str(ordinal),
            "currency": "USD",
            "dimensions_json": "[]",
            "evidence_document_version_id": f"document-{ordinal}",
            "evidence_locator_json": "{}",
            "evidence_locator_sha256": digest_text("{}"),
            "evidence_node_id": f"node-{ordinal}",
            "mapping_commitment_sha256": exact,
            "mapping_revision_id": f"mapping-{ordinal}",
            "metric_definition_commitment_sha256": exact,
            "metric_definition_revision_id": f"definition-{ordinal}",
            "period_end": period_end,
            "period_kind": "instant",
            "reporting_entity_id": "entity",
            "selected_observation_id": f"observation-{ordinal}",
            "source_publication_id": "publication-1",
            "source_publication_member_id": f"member-{ordinal}",
            "source_publication_member_sha256": exact,
            "source_publication_seal_id": "publication-seal-1",
            "source_record_commitment_sha256": exact,
            "unit_key": "USD",
            "value_kind": "numeric",
        }
    )
    committed_payload = {
        **payload,
        "change_kind": "upsert",
        "entry_version": "canonical_fact_projection_entry.v1",
    }
    entry_json = canonical_json(committed_payload)
    return {
        "generation_id": generation_id,
        "entry_ordinal": ordinal,
        "change_kind": "upsert",
        "digest_bucket": digest_bucket(cell_id),
        **payload,
        "entry_json": entry_json,
        "entry_sha256": digest_text(entry_json),
    }


def _admit_generation(
    _conn: sqlite3.Connection,
    generation_id: str,
    _verification: RequestVerificationCache | None = None,
) -> VerifiedCanonicalProjectionGeneration:
    return VerifiedCanonicalProjectionGeneration(
        generation_id=generation_id,
        projection_seal_id="projection-seal",
        projection_seal_sha256="f" * 64,
        generation_kind="checkpoint",
        parent_generation_id=None,
        resolution_snapshot_id="resolution-snapshot",
        resolution_snapshot_sha256="1" * 64,
        resolution_scope_sha256="2" * 64,
        resolution_snapshot_commitment_sha256="3" * 64,
        resolution_watermark_sha256="4" * 64,
        ontology_snapshot_id="ontology-snapshot",
        ontology_snapshot_sha256="5" * 64,
        cutoff_at=NOW,
        knowledge_at=NOW,
        recorded_at=NOW,
        change_count=len(_FACT_CELLS),
        upsert_count=len(_FACT_CELLS),
        tombstone_count=0,
        effective_entry_count=len(_FACT_CELLS),
        batch_count=1,
        generation_sha256="6" * 64,
        ordered_entry_set_sha256="7" * 64,
    )


def _seed_fact_entries(
    conn: sqlite3.Connection,
    generation_id: str,
    cells: Sequence[tuple[str, str, str, str]],
) -> list[dict[str, object]]:
    entry_columns = cast(tuple[str, ...], getattr(projection_module, "_ENTRY_COLUMNS"))
    entries = [
        _fact_entry(
            generation_id,
            ordinal,
            cell_id=cell_id,
            metric_name=metric_name,
            search_text=search_text,
            period_end=period_end,
        )
        for ordinal, (cell_id, metric_name, search_text, period_end) in enumerate(cells)
    ]
    conn.executemany(
        "INSERT INTO canonical_fact_projection_entries (" + ",".join(entry_columns) + ") "
        "VALUES (" + ",".join("?" for _ in entry_columns) + ")",
        [tuple(entry[column] for column in entry_columns) for entry in entries],
    )
    return entries


def _fact_conn() -> sqlite3.Connection:
    entry_columns = cast(tuple[str, ...], getattr(projection_module, "_ENTRY_COLUMNS"))
    integer_columns = {"entry_ordinal", "digest_bucket"}
    entries_ddl = (
        "CREATE TABLE canonical_fact_projection_entries (\n    "
        + ",\n    ".join(
            f"{column} {'INTEGER' if column in integer_columns else 'TEXT'}"
            for column in entry_columns
        )
        + "\n);"
    )
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
CREATE TABLE canonical_fact_projection_generations (
    generation_id TEXT PRIMARY KEY,
    parent_generation_id TEXT,
    resolution_snapshot_id TEXT NOT NULL,
    ontology_snapshot_id TEXT NOT NULL,
    cutoff_at TEXT NOT NULL
);
"""
        + entries_ddl
        + """
CREATE TABLE canonical_fact_projection_buckets (
    generation_id TEXT NOT NULL, digest_bucket INTEGER NOT NULL,
    canonical_entry_set_json TEXT NOT NULL, entry_set_sha256 TEXT NOT NULL
);
CREATE TABLE source_fact_publication_seals (
    publication_seal_id TEXT PRIMARY KEY, publication_id TEXT NOT NULL
);
CREATE TABLE source_fact_publication_members (
    publication_member_id TEXT PRIMARY KEY, publication_id TEXT NOT NULL,
    canonical_member_sha256 TEXT NOT NULL, record_commitment_sha256 TEXT NOT NULL
);
"""
        + _KPI_ALIASES_DDL
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?,?,?)",
        ("generation-facts", None, "resolution-snapshot", "ontology-snapshot", NOW.isoformat()),
    )
    entries = _seed_fact_entries(conn, "generation-facts", _FACT_CELLS)
    buckets: dict[int, list[str]] = {}
    for entry in entries:
        digest = cast(int, entry["digest_bucket"])
        buckets.setdefault(digest, []).append(str(entry["entry_sha256"]))
    for bucket, shas in buckets.items():
        payload = canonical_json(sorted(shas))
        conn.execute(
            "INSERT INTO canonical_fact_projection_buckets VALUES (?,?,?,?)",
            ("generation-facts", bucket, payload, digest_text(payload)),
        )
    conn.execute(
        "INSERT INTO source_fact_publication_seals VALUES (?,?)",
        ("publication-seal-1", "publication-1"),
    )
    for ordinal in range(len(_FACT_CELLS)):
        conn.execute(
            "INSERT INTO source_fact_publication_members VALUES (?,?,?,?)",
            (f"member-{ordinal}", "publication-1", "a" * 64, "a" * 64),
        )
    conn.commit()
    return conn


def _fact_ids(conn: sqlite3.Connection, query: str) -> list[str]:
    return [
        hit.canonical_metric_cell_id
        for hit in search_canonical_facts(
            conn, generation_id="generation-facts", query_text=query, limit=10
        )
    ]


def test_fact_lane_kpi_alias_query_retrieves_expected_canonical_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _fact_conn()
    try:
        monkeypatch.setattr(
            projection_module, "admit_canonical_projection_for_read", _admit_generation
        )
        conn.execute(_ALIAS_ROW_SQL, (None,))
        conn.commit()
        # Only the alias expansion bridges "MAU" to the canonical fact; the
        # fixture entry's sealed search text never contains the alias.
        assert _fact_ids(conn, "MAU growth 2025") == ["canonical:mau:2025"]
        # Retire the alias: the same question retrieves nothing.
        conn.execute("UPDATE kpi_aliases SET effective_to='2020-01-01'")
        conn.commit()
        assert _fact_ids(conn, "MAU growth 2025") == []
        _record_evidence(
            "val-search-005-fact-kpi-aliases.json",
            {
                "cases": [
                    {
                        "query": "MAU growth 2025",
                        "alias_rows": [
                            "(canonical_name='Monthly Active Users', reported_name='MAU', active)"
                        ],
                        "expected_fact_ids": ["canonical:mau:2025"],
                        "actual_fact_ids": ["canonical:mau:2025"],
                    },
                    {
                        "query": "MAU growth 2025",
                        "alias_rows": [
                            "(canonical_name='Monthly Active Users', reported_name='MAU', retired)"
                        ],
                        "expected_fact_ids": [],
                        "actual_fact_ids": [],
                    },
                ],
                "fixture_corpus": {
                    "fact_cells": [
                        {"cell_id": cell_id, "metric_name": metric_name, "search_text": search_text}
                        for cell_id, metric_name, search_text, _period_end in _FACT_CELLS
                    ],
                },
                "scope_note": _SCOPE_NOTE,
            },
        )
    finally:
        conn.close()


def test_fact_lane_keeps_metric_terms_and_requested_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _fact_conn()
    try:
        monkeypatch.setattr(
            projection_module, "admit_canonical_projection_for_read", _admit_generation
        )
        ids = _fact_ids(conn, "Revenue growth in 2024 versus 2023")
        assert set(ids) == {"canonical:revenue:2024", "canonical:revenue:2023"}
        assert "canonical:mau:2025" not in ids
        _record_evidence(
            "val-search-005-fact-stopwords.json",
            {
                "cases": [
                    {
                        "query": "Revenue growth in 2024 versus 2023",
                        "expected_fact_ids": [
                            "canonical:revenue:2024",
                            "canonical:revenue:2023",
                        ],
                        "actual_fact_ids": ids,
                    }
                ],
                "fixture_corpus": {
                    "fact_cells": [
                        {"cell_id": cell_id, "metric_name": metric_name, "search_text": search_text}
                        for cell_id, metric_name, search_text, _period_end in _FACT_CELLS
                    ],
                },
                "scope_note": _SCOPE_NOTE,
            },
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration-boundary regression pin: the legacy alias table is dead at head,
# so hermetic head-migrated databases must resolve an empty alias map and the
# empty-generation build must keep working through the real chain.
# ---------------------------------------------------------------------------


def test_head_migrated_database_has_no_kpi_aliases_table(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(tmp_path / "head-template.db", target="head")
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='kpi_aliases'"
            ).fetchone()[0]
            == 0
        )
        assert kpi_alias_expansions(conn) == {}
    finally:
        conn.close()


def _empty_projection_request() -> ProjectionGenerationRequest:
    return ProjectionGenerationRequest(
        generation_id="generation-empty",
        idempotency_key="generation-empty",
        generation_kind="checkpoint",
        resolution_snapshot_id="resolution-empty",
        ontology_snapshot_id="ontology-empty",
        cutoff_at=T0,
        recorded_at=T0,
        config=ProjectionConfig(max_batch_facts=2),
    )


def test_real_empty_generation_still_builds_and_admits(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(tmp_path / "empty-generation.db", target="head")
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-empty", "issuer-empty", "operating_company", T0.isoformat()),
        )
        conn.execute(
            "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
            (
                "reporting-empty",
                "reporting-empty",
                "issuer-empty",
                "legal_registrant",
                "Empty issuer",
                T0.isoformat(),
            ),
        )
        MetricOntology(conn).seal_snapshot(
            OntologySnapshot(
                ontology_snapshot_id="ontology-empty",
                idempotency_key="ontology-empty",
                cutoff_at=T0,
                recorded_at=T0,
            )
        )
        resolver = CanonicalFactResolutionEngine(conn)
        resolver.seal_snapshot(
            "resolution-empty",
            T0,
            T0,
            ResolutionSnapshotScope(
                issuer_id="issuer-empty",
                reporting_entity_ids=("reporting-empty",),
            ),
        )
        bind_resolution_snapshot_watermark(
            conn,
            resolution_snapshot_id="resolution-empty",
            cutoff_at=T0,
            recorded_at=T0,
        )
        verified = build_canonical_projection_generation(
            conn,
            _empty_projection_request(),
        )
        assert verified.change_count == 0
    finally:
        conn.close()


def test_kpi_alias_expansions_from_current_head_schema() -> None:
    """kpi_alias_expansions resolves bidirectional variants from kpi_definition_revisions at head."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE kpi_definition_revisions (
            kpi_definition_revision_id TEXT PRIMARY KEY,
            kpi_definition_id INTEGER NOT NULL,
            reported_label TEXT NOT NULL,
            status TEXT NOT NULL,
            lifecycle TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO kpi_definitions VALUES (1, 'AAPL', 'Subscription Revenue')")
    conn.execute("INSERT INTO kpi_definitions VALUES (2, 'MSFT', 'Cloud Gross Margin')")
    conn.execute(
        "INSERT INTO kpi_definition_revisions VALUES ('rev-1', 1, 'Services ARR', 'admitted', 'active')"
    )
    conn.execute(
        "INSERT INTO kpi_definition_revisions VALUES ('rev-2', 2, 'Intelligent Cloud Margin', 'admitted', 'active')"
    )
    # Quarantined or discontinued rows must be excluded
    conn.execute(
        "INSERT INTO kpi_definition_revisions VALUES ('rev-3', 1, 'Inactive Label', 'quarantined', 'active')"
    )
    conn.execute(
        "INSERT INTO kpi_definition_revisions VALUES ('rev-4', 2, 'Retired Label', 'admitted', 'discontinued')"
    )

    expansions = kpi_alias_expansions(conn)

    assert expansions["services arr"] == ("subscription revenue",)
    assert expansions["subscription revenue"] == ("services arr",)
    assert expansions["intelligent cloud margin"] == ("cloud gross margin",)
    assert expansions["cloud gross margin"] == ("intelligent cloud margin",)
    assert "inactive label" not in expansions
    assert "retired label" not in expansions

    conn.close()


def test_plan_lexical_query_unquoted_multiword_alias() -> None:
    """An unquoted multiword alias in ordinary query text expands to its counterpart."""
    aliases = {"services arr": ("subscription revenue",)}
    plan = plan_lexical_query("What drove Services ARR in recent quarter?", aliases=aliases)
    assert plan is not None
    assert ("Services ARR", "subscription revenue") in plan.groups
    assert '("Services ARR" OR "subscription revenue")' in plan.match_all
    assert '"Services ARR" OR "subscription revenue"' in plan.match_any


def test_plan_and_search_query_containing_both_alias_and_canonical_kpi_names() -> None:
    """When a query contains both a KPI alias and its canonical name, documents with only the canonical name and documents with both names are retrieved."""
    aliases = {"mau": ("monthly active users",), "monthly active users": ("mau",)}

    # 1. Query planning: both terms merge into the same equivalence group
    plan = plan_lexical_query("MAU monthly active users", aliases=aliases)
    assert plan is not None
    assert len(plan.groups) == 1
    assert set(plan.groups[0]) == {"monthly active users", "mau"}

    # 2. SQLite FTS5 evaluation
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE search_lexical_chunks USING fts5(chunk_id UNINDEXED, text)"
        )
        conn.execute(
            "INSERT INTO search_lexical_chunks VALUES ('doc-canonical-only', 'In Q2 monthly active users grew 20 percent year over year.')"
        )
        conn.execute(
            "INSERT INTO search_lexical_chunks VALUES ('doc-both-names', 'In Q2 MAU (monthly active users) reached all-time record highs.')"
        )
        conn.commit()

        matched = conn.execute(
            "SELECT chunk_id FROM search_lexical_chunks WHERE search_lexical_chunks MATCH ? ORDER BY chunk_id",
            (plan.match_all,),
        ).fetchall()
        matched_ids = [row[0] for row in matched]
        assert matched_ids == ["doc-both-names", "doc-canonical-only"]
    finally:
        conn.close()


def test_fact_trace_replays_cleanly_after_alias_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a fact is retrieved via an active KPI alias, trace re-verification succeeds even after alias retirement."""
    monkeypatch.setattr(projection_module, "admit_canonical_projection_for_read", _admit_generation)
    monkeypatch.setattr(retrieval_module, "_derive_research_coordinates", _derive_research)

    conn = _fact_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE research_snapshot_headers (
                research_snapshot_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, cutoff_at TEXT NOT NULL
            );
            CREATE TABLE heterogeneous_retrieval_trace_headers (
                trace_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, research_snapshot_id TEXT,
                research_snapshot_sha256 TEXT, fact_generation_id TEXT,
                fact_projection_seal_sha256 TEXT, narrative_commitments_json TEXT,
                narrative_commitments_sha256 TEXT, semantic_receipts_json TEXT,
                semantic_receipts_sha256 TEXT, query_sha256 TEXT, query_json TEXT, ranker_json TEXT,
                ranker_sha256 TEXT, filters_json TEXT, filters_sha256 TEXT, candidate_limit INTEGER,
                result_limit INTEGER, cutoff_at TEXT, recorded_at TEXT
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
        )
        conn.execute(
            "INSERT INTO research_snapshot_headers VALUES (?,?,?)",
            ("research-snapshot", "{}", NOW.isoformat()),
        )
        conn.execute(_ALIAS_ROW_SQL, (None,))
        conn.commit()

        req = HeterogeneousRetrievalRequest(
            trace_id="trace-fact-alias",
            idempotency_key="trace-fact-alias",
            research_snapshot_id="research-snapshot",
            fact_generation_id="generation-facts",
            narrative_bundles=(),
            query_text="MAU growth 2025",
            candidate_limit=10,
            result_limit=4,
            filters=RetrievalFilters(include_narrative=False, include_facts=True),
            cutoff_at=NOW,
            recorded_at=NOW,
        )
        receipt = retrieve_heterogeneous(conn, req)
        assert [str(item["candidate_id"]) for item in receipt.ordered_results] == [
            "canonical:mau:2025"
        ]

        verified = retrieval_module.verify_heterogeneous_retrieval_trace(conn, "trace-fact-alias")
        assert [str(item["candidate_id"]) for item in verified.ordered_results] == [
            "canonical:mau:2025"
        ]

        # Retire alias
        conn.execute("UPDATE kpi_aliases SET effective_to='2020-01-01'")
        conn.commit()

        # Re-verification MUST succeed using committed aliases in fact candidate lineage
        reverified = retrieval_module.verify_heterogeneous_retrieval_trace(conn, "trace-fact-alias")
        assert [str(item["candidate_id"]) for item in reverified.ordered_results] == [
            "canonical:mau:2025"
        ]
    finally:
        conn.close()


def test_fact_trace_replays_cleanly_after_alias_addition_with_empty_initial_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trace sealed with no aliases stores an empty alias map and replays cleanly after new aliases are added."""
    conn = _fact_conn()
    try:
        monkeypatch.setattr(
            projection_module, "admit_canonical_projection_for_read", _admit_generation
        )
        monkeypatch.setattr(retrieval_module, "_derive_research_coordinates", _derive_research)

        conn.executescript(
            """
            CREATE TABLE research_snapshot_headers (
                research_snapshot_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, cutoff_at TEXT NOT NULL
            );
            CREATE TABLE heterogeneous_retrieval_trace_headers (
                trace_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, research_snapshot_id TEXT,
                research_snapshot_sha256 TEXT, fact_generation_id TEXT,
                fact_projection_seal_sha256 TEXT, narrative_commitments_json TEXT,
                narrative_commitments_sha256 TEXT, semantic_receipts_json TEXT,
                semantic_receipts_sha256 TEXT, query_sha256 TEXT, query_json TEXT, ranker_json TEXT,
                ranker_sha256 TEXT, filters_json TEXT, filters_sha256 TEXT, candidate_limit INTEGER,
                result_limit INTEGER, cutoff_at TEXT, recorded_at TEXT
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
        )
        conn.execute(
            "INSERT INTO research_snapshot_headers VALUES (?,?,?)",
            ("research-snapshot", "{}", NOW.isoformat()),
        )
        conn.commit()

        # Query "revenue 2024" initially has NO aliases defined in kpi_aliases
        req = HeterogeneousRetrievalRequest(
            trace_id="trace-fact-no-alias",
            idempotency_key="trace-fact-no-alias",
            research_snapshot_id="research-snapshot",
            fact_generation_id="generation-facts",
            narrative_bundles=(),
            query_text="revenue 2024",
            candidate_limit=10,
            result_limit=4,
            filters=RetrievalFilters(include_narrative=False, include_facts=True),
            cutoff_at=NOW,
            recorded_at=NOW,
        )
        receipt = retrieve_heterogeneous(conn, req)
        assert [str(item["candidate_id"]) for item in receipt.ordered_results] == [
            "canonical:revenue:2024"
        ]

        # Verify candidate lineage explicitly committed empty alias map {"aliases": {}}
        cand_row = conn.execute(
            "SELECT lineage_json FROM heterogeneous_retrieval_trace_candidates WHERE trace_id='trace-fact-no-alias'"
        ).fetchone()
        assert cand_row is not None
        lineage = json.loads(str(cand_row[0]))
        assert "aliases" in lineage
        assert lineage["aliases"] == {}

        # Initial verification succeeds
        verified = retrieval_module.verify_heterogeneous_retrieval_trace(
            conn, "trace-fact-no-alias"
        )
        assert [str(item["candidate_id"]) for item in verified.ordered_results] == [
            "canonical:revenue:2024"
        ]

        # Add a new alias later to the authority
        conn.execute(
            "INSERT INTO kpi_aliases VALUES (2, NULL, 'Revenue', 'Sales ARR', NULL, NULL, NULL)"
        )
        conn.commit()

        # Re-verification MUST still succeed using the committed empty alias map without expanding newly added aliases
        reverified = retrieval_module.verify_heterogeneous_retrieval_trace(
            conn, "trace-fact-no-alias"
        )
        assert [str(item["candidate_id"]) for item in reverified.ordered_results] == [
            "canonical:revenue:2024"
        ]
    finally:
        conn.close()
