"""Contracts for immutable, complete, evidence-grounded hybrid search."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest

from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from search.corpus_builder import lexical_index_config_sha256
from search.embedding_runtime_artifact import (
    EmbeddingRuntimeArtifact,
    RuntimeArtifactFile,
    RuntimeComponentVersion,
)
from search.embedding_runtime_registration import (
    persist_runtime_registration,
    registration_from_artifact,
)
from search.grounded import (
    CorpusDocumentMembership,
    CorpusManifest,
    CorpusManifestSeal,
    EmbeddingArtifact,
    GroundedSearchStore,
    HybridRetriever,
    IndexMembership,
    IndexRun,
    SearchCapabilityError,
    SearchChunk,
    SearchFilter,
    VectorCandidate,
    membership_digest,
)
from sqlite_runtime import register_sqlite_integrity_functions

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_HEAD = "0213_evidence_ledger_foundation"
PRIOR = "0215_observation_resolution_ledger"
HEAD = "0216_search_corpus_foundation"
STAMP = datetime(2026, 7, 26, 20, 0, 0)
A, B, C = "a" * 64, "b" * 64, "c" * 64
_RUNTIME_ARTIFACT = EmbeddingRuntimeArtifact(
    provider="future-provider",
    model="future-model",
    dimensions=3,
    execution_provider="CPUExecutionProvider",
    execution_settings=(),
    component_versions=(RuntimeComponentVersion(component="fastembed", version="1.2.3"),),
    files=(RuntimeArtifactFile(logical_name="model.onnx", role="model", size_bytes=3, sha256=C),),
)
_RUNTIME_SHA = _RUNTIME_ARTIFACT.sha256()
_MIGRATED_DB_BUILDER: Callable[..., Path] | None = None


@pytest.fixture(autouse=True)
def _bind_migrated_db(migrated_db: Callable[..., Path]) -> None:
    global _MIGRATED_DB_BUILDER
    _MIGRATED_DB_BUILDER = migrated_db


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "search.db"
    assert _MIGRATED_DB_BUILDER is not None
    _MIGRATED_DB_BUILDER(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    register_sqlite_integrity_functions(conn)
    return conn


def _seed_evidence(conn: sqlite3.Connection) -> None:
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=A,
            byte_size=22,
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
    ledger.persist(
        EvidenceNode(
            node_id="node",
            evidence_key="ACME:node",
            revision=1,
            extraction_run_id="run",
            node_kind="passage",
            text="Revenue grew strongly.",
            recorded_at=STAMP,
        )
    )
    persist_runtime_registration(
        conn,
        registration_from_artifact(
            _RUNTIME_ARTIFACT,
            registered_at=STAMP,
            purpose="evidence_vector_retrieval",
        ),
    )


def _seed_other_ticker_evidence(conn: sqlite3.Connection) -> None:
    ledger = EvidenceLedger(conn)
    ledger.persist(
        DocumentVersion(
            document_version_id="doc-other",
            document_key="OTHER:10-Q",
            version_sequence=1,
            observation_id="obs",
            blob_sha256=A,
            issuer_id="other-issuer",
            ticker="OTHER",
            document_type="filing",
            form_type="10-Q",
            language="en",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id="run-other",
            idempotency_key="run-other",
            document_version_id="doc-other",
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
    ledger.persist(
        EvidenceNode(
            node_id="node-other",
            evidence_key="OTHER:node",
            revision=1,
            extraction_run_id="run-other",
            node_kind="passage",
            text="Profit expanded strongly.",
            recorded_at=STAMP,
        )
    )


def _manifest(
    manifest_id: str = "manifest", corpus_key: str = "all-company-reporting"
) -> CorpusManifest:
    return CorpusManifest(
        manifest_id=manifest_id,
        idempotency_key=manifest_id,
        corpus_key=corpus_key,
        revision=1,
        selection_config_sha256=A,
        selector_code_version="selector@1",
        recorded_at=STAMP,
    )


def _membership(
    manifest_id: str = "manifest",
    document_id: str | None = "doc",
    status: Literal["included", "missing", "quarantined"] = "included",
) -> CorpusDocumentMembership:
    return CorpusDocumentMembership(
        membership_id=f"{manifest_id}:{status}:{document_id or 'expected'}",
        manifest_id=manifest_id,
        expected_document_key="ACME:10-Q",
        document_version_id=document_id,
        membership_status=status,
        reason="eligible" if status == "included" else "not yet available",
        recorded_at=STAMP,
    )


def _chunk(manifest_id: str = "manifest", chunk_id: str = "chunk") -> SearchChunk:
    text = "Revenue grew strongly."
    return SearchChunk(
        chunk_id=chunk_id,
        idempotency_key=chunk_id,
        manifest_id=manifest_id,
        evidence_node_id="node",
        chunk_key=f"{manifest_id}:ACME:node:0",
        chunk_revision=1,
        text=text,
        char_start=0,
        char_end=len(text),
        chunker_config_sha256=B,
        chunker_code_version="chunker@1",
        available_at=STAMP,
        recorded_at=STAMP,
    )


def _seal(
    store: GroundedSearchStore,
    memberships: list[CorpusDocumentMembership],
    status: Literal["complete", "incomplete"] = "complete",
    manifest_id: str = "manifest",
) -> None:
    store.persist(
        CorpusManifestSeal(
            manifest_id=manifest_id,
            expected_document_count=len(memberships),
            membership_digest_sha256=membership_digest(memberships),
            completion_status=status,
            sealed_at=STAMP,
        )
    )


def _seal_projection(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    manifest_id: str,
    kind: Literal["lexical", "vector"],
    chunk_count: int = 1,
) -> None:
    config_sha = (
        lexical_index_config_sha256(conn, manifest_id=manifest_id) if kind == "lexical" else A
    )
    conn.execute(
        "INSERT INTO search_projection_seals "
        "(projection_seal_id, idempotency_key, index_run_id, manifest_id, index_kind, "
        "chunk_count, chunk_set_sha256, projection_records_sha256, artifact_set_sha256, "
        "provider, model, dimensions, config_sha256, storage_uri, sealed_at, "
        "runtime_artifact_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"seal:{run_id}",
            f"seal:{run_id}",
            run_id,
            manifest_id,
            kind,
            chunk_count,
            A,
            B,
            C if kind == "vector" else None,
            "future-provider" if kind == "vector" else None,
            "future-model" if kind == "vector" else None,
            3 if kind == "vector" else None,
            config_sha,
            f"sqlite://{kind}/{run_id}",
            STAMP,
            _RUNTIME_SHA if kind == "vector" else None,
        ),
    )


def _index(
    conn: sqlite3.Connection,
    store: GroundedSearchStore,
    manifest_id: str = "manifest",
    chunk_id: str = "chunk",
    kind: Literal["lexical", "vector"] = "lexical",
    run_id: str | None = None,
    index_key: str | None = None,
    revision: int = 1,
    outcome: Literal["succeeded", "failed"] = "succeeded",
    *,
    seal: bool = True,
) -> str:
    run_id = run_id or f"{manifest_id}:{kind}:{revision}"
    store.persist(
        IndexRun(
            index_run_id=run_id,
            idempotency_key=run_id,
            index_key=index_key or f"{manifest_id}:{kind}",
            revision=revision,
            manifest_id=manifest_id,
            index_kind=kind,
            config_sha256=(
                lexical_index_config_sha256(conn, manifest_id=manifest_id)
                if kind == "lexical"
                else A
            ),
            code_version="indexer@1",
            outcome=outcome,
            failure_reason=None if outcome == "succeeded" else "build failed",
            started_at=STAMP,
            completed_at=STAMP,
        )
    )
    if outcome == "succeeded":
        store.persist(
            IndexMembership(
                index_run_id=run_id,
                chunk_id=chunk_id,
                membership_status="included",
                recorded_at=STAMP,
            )
        )
        if kind == "vector":
            chunk_row = conn.execute(
                "SELECT content_sha256 FROM search_chunks WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()
            chunk_input_sha = str(chunk_row[0]) if chunk_row is not None else A
            store.persist(
                EmbeddingArtifact(
                    embedding_artifact_id=f"{run_id}:seed:{chunk_id}",
                    idempotency_key=f"{run_id}:seed:{chunk_id}",
                    index_run_id=run_id,
                    chunk_id=chunk_id,
                    purpose="grounded_search",
                    provider="future-provider",
                    model="future-model",
                    dimensions=3,
                    vector_sha256=C,
                    storage_uri=f"vector://index/{run_id}/{chunk_id}",
                    input_sha256=chunk_input_sha,
                    request_config_sha256=A,
                    runtime_artifact_sha256=_RUNTIME_SHA,
                    outcome="succeeded",
                    started_at=STAMP,
                    completed_at=STAMP,
                )
            )
        if seal:
            _seal_projection(conn, run_id=run_id, manifest_id=manifest_id, kind=kind)
    return run_id


def _ready_store(conn: sqlite3.Connection, vector: bool = True) -> GroundedSearchStore:
    _seed_evidence(conn)
    store = GroundedSearchStore(conn)
    member = _membership()
    store.persist(_manifest())
    store.persist(member)
    store.persist(_chunk())
    _seal(store, [member])
    _index(conn, store)
    if vector:
        _index(conn, store, kind="vector")
    return store


def test_retrieval_stack_inventory_has_one_grounded_and_fact_projection_path() -> None:
    root = Path(__file__).resolve().parents[1]
    search = root / "src" / "search"
    ask = root / "src" / "ask"
    assert (search / "grounded.py").is_file()
    assert (search / "canonical_fact_projection.py").is_file()
    assert not (search / "fact_projection.py").exists()
    assert not (ask / "grounded_retrieval.py").exists()
    assert {path.name for path in search.glob("*fact_projection*.py")} == {
        "canonical_fact_projection.py"
    }
    for source_root in (root / "src", root / "execution"):
        for source in source_root.rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            assert "ask.grounded_retrieval" not in text
            assert "search.fact_projection" not in text


def test_hybrid_retrieval_preserves_candidate_identity_and_evidence_metadata(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "retrieval-parity.db")
    conn = sqlite3.connect(path)
    register_sqlite_integrity_functions(conn)
    try:
        _ready_store(conn)
        bundles = HybridRetriever(conn).search(
            "Revenue",
            "manifest",
            SearchFilter(issuer_id="issuer"),
            limit=12,
        )
        assert [
            (
                item.chunk_id,
                item.text,
                item.node_id,
                item.node_kind,
                item.locator,
                item.document_version_id,
                item.issuer_id,
                item.recorded_issuer_id,
                item.ticker,
                item.form_type,
                item.source_url,
                item.filing_at,
                item.observed_at,
                item.retrieved_at,
            )
            for item in bundles
        ] == [
            (
                "chunk",
                "Revenue grew strongly.",
                "node",
                "passage",
                None,
                "doc",
                "issuer",
                "issuer",
                "ACME",
                "10-Q",
                "https://sec.test/acme",
                STAMP,
                STAMP,
                STAMP,
            )
        ]
        assert [item.score for item in bundles] == pytest.approx([1 / 61])
    finally:
        conn.close()


def test_expected_missing_membership_and_seal_are_auditable_and_immutable(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        store = GroundedSearchStore(conn)
        included = _membership()
        missing = CorpusDocumentMembership(
            membership_id="manifest:missing:10k",
            manifest_id="manifest",
            expected_document_key="ACME:10-K",
            membership_status="missing",
            reason="filing not captured",
            recorded_at=STAMP,
        )
        store.persist(_manifest())
        store.persist(included)
        store.persist(missing)
        _seal(store, [included, missing], "incomplete")
        assert conn.execute(
            "SELECT expected_document_count, included_document_count, missing_document_count FROM v_search_corpus_coverage"
        ).fetchone() == (2, 1, 1)
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            store.persist(
                CorpusDocumentMembership(
                    membership_id="late",
                    manifest_id="manifest",
                    expected_document_key="ACME:deck",
                    membership_status="missing",
                    reason="late",
                    recorded_at=STAMP,
                )
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE search_corpus_manifest_seals SET completion_status = 'complete'")
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            store.persist(_chunk())
    finally:
        conn.close()


def test_chunk_fts_is_transactional_and_embedding_contract_is_explicit(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        store = GroundedSearchStore(conn)
        member = _membership()
        store.persist(_manifest())
        store.persist(member)
        chunk = _chunk()
        store.persist(chunk)
        assert conn.execute("SELECT chunk_id FROM search_lexical_chunks").fetchone() == ("chunk",)
        direct_text = "Revenue grew strongly."
        conn.execute(
            "INSERT INTO search_chunks "
            "(chunk_id, idempotency_key, manifest_id, evidence_node_id, chunk_key, chunk_revision, text, "
            "content_sha256, char_start, char_end, chunker_config_sha256, chunker_code_version, available_at, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "direct-chunk",
                "direct-chunk",
                "manifest",
                "node",
                "direct:node:0",
                1,
                direct_text,
                sha256(direct_text.encode("utf-8")).hexdigest(),
                0,
                len(direct_text),
                B,
                "chunker@1",
                STAMP,
                STAMP,
            ),
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM search_lexical_chunks WHERE search_lexical_chunks MATCH 'Revenue'"
        ).fetchone() == (2,)
        assert store.persist(chunk).created is False
        vector_run_id = _index(conn, store, kind="vector", seal=False)
        store.persist(
            EmbeddingArtifact(
                embedding_artifact_id="embed",
                idempotency_key="embed",
                index_run_id=vector_run_id,
                chunk_id="chunk",
                purpose="grounded_search",
                provider="future-provider",
                model="future-model",
                dimensions=3,
                vector_sha256=C,
                storage_uri="vector://index/embed",
                input_sha256=chunk.content_sha256 or "",
                request_config_sha256=A,
                runtime_artifact_sha256=_RUNTIME_SHA,
                outcome="succeeded",
                started_at=STAMP,
                completed_at=STAMP,
            )
        )
        store.persist(
            EmbeddingArtifact(
                embedding_artifact_id="embed-failed",
                idempotency_key="embed-failed",
                index_run_id=vector_run_id,
                chunk_id="chunk",
                purpose="grounded_search",
                provider="future-provider",
                model="future-model",
                dimensions=3,
                input_sha256=chunk.content_sha256 or "",
                request_config_sha256=A,
                outcome="failed",
                failure_reason="provider unavailable",
                started_at=STAMP,
                completed_at=STAMP,
            )
        )
        with pytest.raises(ValueError, match="completed_at"):
            EmbeddingArtifact(
                embedding_artifact_id="bad-clock",
                idempotency_key="bad-clock",
                index_run_id=vector_run_id,
                chunk_id="chunk",
                purpose="grounded_search",
                provider="future-provider",
                model="future-model",
                dimensions=3,
                input_sha256=chunk.content_sha256 or "",
                request_config_sha256=A,
                outcome="failed",
                started_at=STAMP.replace(hour=1),
                completed_at=STAMP.replace(hour=0),
            )
    finally:
        conn.close()


def test_hybrid_rrf_requires_sealed_complete_manifest_and_current_indexes(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _ready_store(conn)

        class Vectors:
            def search(
                self, query: str, filters: SearchFilter, limit: int
            ) -> list[VectorCandidate]:
                return [VectorCandidate("chunk", 0.99, "manifest:vector:1")]

        bundles = HybridRetriever(conn, Vectors()).search(
            "Revenue",
            "manifest",
            SearchFilter(
                ticker="ACME", form_types=("10-Q",), node_kinds=("passage",), knowledge_cutoff=STAMP
            ),
        )
        assert [
            (bundle.chunk_id, bundle.document_version_id, bundle.source_url) for bundle in bundles
        ] == [("chunk", "doc", "https://sec.test/acme")]
        _index(
            conn,
            GroundedSearchStore(conn),
            run_id="manifest:lexical:2",
            index_key="manifest:lexical",
            revision=2,
            outcome="failed",
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_search_index_successful WHERE index_key = 'manifest:lexical'"
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(ValueError, match="successful current lexical"):
            HybridRetriever(conn).search("Revenue", "manifest")
    finally:
        conn.close()


def test_vector_candidates_are_db_filtered_before_rrf(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_evidence(conn)
        _seed_other_ticker_evidence(conn)
        store = GroundedSearchStore(conn)
        acme = _membership()
        other = CorpusDocumentMembership(
            membership_id="manifest:included:other",
            manifest_id="manifest",
            expected_document_key="OTHER:10-Q",
            document_version_id="doc-other",
            membership_status="included",
            reason="eligible",
            recorded_at=STAMP,
        )
        store.persist(_manifest())
        store.persist(acme)
        store.persist(other)
        store.persist(_chunk())
        other_text = "Profit expanded strongly."
        store.persist(
            SearchChunk(
                chunk_id="aaa-other",
                idempotency_key="aaa-other",
                manifest_id="manifest",
                evidence_node_id="node-other",
                chunk_key="OTHER:node:0",
                chunk_revision=1,
                text=other_text,
                char_start=0,
                char_end=len(other_text),
                chunker_config_sha256=B,
                chunker_code_version="chunker@1",
                available_at=STAMP,
                recorded_at=STAMP,
            )
        )
        _index(conn, store, chunk_id="chunk", seal=False)
        store.persist(
            IndexMembership(
                index_run_id="manifest:lexical:1",
                chunk_id="aaa-other",
                membership_status="included",
                recorded_at=STAMP,
            )
        )
        _index(conn, store, chunk_id="chunk", kind="vector", seal=False)
        store.persist(
            IndexMembership(
                index_run_id="manifest:vector:1",
                chunk_id="aaa-other",
                membership_status="included",
                recorded_at=STAMP,
            )
        )
        store.persist(
            EmbeddingArtifact(
                embedding_artifact_id="manifest:vector:1:seed:aaa-other",
                idempotency_key="manifest:vector:1:seed:aaa-other",
                index_run_id="manifest:vector:1",
                chunk_id="aaa-other",
                purpose="grounded_search",
                provider="future-provider",
                model="future-model",
                dimensions=3,
                vector_sha256=C,
                storage_uri="vector://index/manifest:vector:1/aaa-other",
                input_sha256=sha256(other_text.encode("utf-8")).hexdigest(),
                request_config_sha256=A,
                runtime_artifact_sha256=_RUNTIME_SHA,
                outcome="succeeded",
                started_at=STAMP,
                completed_at=STAMP,
            )
        )
        _seal(store, [acme, other])
        _seal_projection(
            conn,
            run_id="manifest:lexical:1",
            manifest_id="manifest",
            kind="lexical",
            chunk_count=2,
        )
        _seal_projection(
            conn,
            run_id="manifest:vector:1",
            manifest_id="manifest",
            kind="vector",
            chunk_count=2,
        )

        class Vectors:
            def search(
                self, query: str, filters: SearchFilter, limit: int
            ) -> list[VectorCandidate]:
                return [
                    VectorCandidate("aaa-other", 1.0, "manifest:vector:1"),
                    VectorCandidate("chunk", 0.1, "manifest:vector:1"),
                ]

        bundles = HybridRetriever(conn, Vectors()).search(
            "Revenue", "manifest", SearchFilter(ticker="ACME"), limit=1
        )
        assert [bundle.chunk_id for bundle in bundles] == ["chunk"]
    finally:
        conn.close()


def test_retrieval_cannot_mix_manifests_or_unindexed_vectors(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _ready_store(conn, vector=False)
        store = GroundedSearchStore(conn)
        member = _membership("other")
        store.persist(_manifest("other", "other-company-reporting"))
        store.persist(member)
        store.persist(_chunk("other", "other-chunk"))
        _seal(store, [member], manifest_id="other")
        _index(conn, store, "other", "other-chunk")
        _index(conn, store, "other", "other-chunk", "vector")

        class Vectors:
            def search(
                self, query: str, filters: SearchFilter, limit: int
            ) -> list[VectorCandidate]:
                return [VectorCandidate("other-chunk", 1.0, "other:vector:1")]

        bundles = HybridRetriever(conn, Vectors()).search("Revenue", "manifest")
        assert [bundle.chunk_id for bundle in bundles] == ["chunk"]
        with pytest.raises(ValueError, match="sealed"):
            HybridRetriever(conn).search("Revenue", "unknown")
    finally:
        conn.close()


def test_fts_capability_and_migration(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _ready_store(conn, vector=False)
        conn.execute("DROP TABLE search_lexical_chunks")
        with pytest.raises(SearchCapabilityError, match="FTS5"):
            HybridRetriever(conn).search("Revenue", "manifest")
    finally:
        conn.close()
    path = tmp_path / "roundtrip.db"
    assert _MIGRATED_DB_BUILDER is not None
    _MIGRATED_DB_BUILDER(path)
