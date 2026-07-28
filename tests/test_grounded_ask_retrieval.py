"""Grounded Ask selects only current complete source-linked corpora."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

import ask.grounded_retrieval as grounded_retrieval_module
from alembic import command
from ask.grounded_retrieval import persist_answer_grounding, retrieve_grounded_ask
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.integrity_audit import AuditOptions, audit_connection
from provenance.source_coverage_reconcile import (
    ExpectedDocumentImport,
    ExplicitAbsence,
    InventoryComponentImport,
    SourceCoverageImport,
    reconcile_source_coverage,
)
from search.corpus_builder import (
    CorpusBuildRequest,
    build_grounded_search_corpus,
    load_coverage_expected_document_inventory,
)
from search.embedding_promotion import LocalVectorRuntimeConfig
from search.local_vector import LocalVectorCapabilityError

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 6, 0, 0)
A, B, C = "a" * 64, "b" * 64, "c" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "ask.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0221_ask_retrieval_traces")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_complete_corpus(conn: sqlite3.Connection) -> str:
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=A,
            byte_size=37,
            media_type="text/plain",
            storage_uri="file:///acme-10q.txt",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="obs",
            idempotency_key="obs",
            source_kind="sec_submissions",
            source_url="https://sec.test/acme-10q",
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
            document_key="sec-cik:0000000001:accession",
            version_sequence=1,
            observation_id="obs",
            blob_sha256=A,
            issuer_id="sec-cik:0000000001",
            ticker="ACME",
            document_type="filing",
            form_type="10-Q",
            accession_number="0000000001-26-000001",
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
            evidence_key="sec-cik:0000000001:revenue",
            revision=1,
            extraction_run_id="run",
            node_kind="passage",
            text="Revenue increased 25 percent year over year.",
            recorded_at=STAMP,
        )
    )
    conn.commit()
    coverage = reconcile_source_coverage(
        conn,
        SourceCoverageImport(
            inventory_key="sec-cik:0000000001:submissions",
            revision=1,
            issuer_id="sec-cik:0000000001",
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://sec.test/submissions",
            source_observation_id="obs",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=B,
            collector_code_version="inventory@1",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
            reconciled_at=STAMP,
            components=(
                InventoryComponentImport(
                    component_key="primary",
                    component_kind="primary",
                    source_url="https://sec.test/submissions",
                    source_observation_id="obs",
                    outcome="succeeded",
                    ordinal=0,
                ),
            ),
            expected_documents=(
                ExpectedDocumentImport(
                    expected_document_key="sec-cik:0000000001:accession",
                    source_kind="sec_filing",
                    document_type="filing",
                    form_type="10-Q",
                    accession_number="0000000001-26-000001",
                    expectation_basis="authoritative",
                ),
            ),
            apply=True,
        ),
    )
    inventory, snapshots = load_coverage_expected_document_inventory(
        conn, ("sec-cik:0000000001:submissions",)
    )
    corpus = build_grounded_search_corpus(
        conn,
        CorpusBuildRequest(
            corpus_key="sec-cik:0000000001:reporting",
            revision=1,
            selector_code_version="selector@1",
            recorded_at=STAMP,
            expected_documents=inventory.expected_documents,
            source_inventory_snapshot_ids=snapshots,
            required_extractor_names=("parser",),
            apply=True,
        ),
    )
    assert coverage.snapshot_id == snapshots[0]
    return corpus.manifest_id


def test_ready_retrieval_and_answer_hashes_have_exact_lineage(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        manifest_id = _seed_complete_corpus(conn)
        result = retrieve_grounded_ask(
            conn,
            question="How fast did revenue increase?",
            tickers=("ACME",),
            created_at=STAMP,
        )
        assert result.outcome == "ready"
        assert result.manifest_ids == (manifest_id,)
        assert result.items[0].chunk_id.startswith("search-chunk:")
        assert result.items[0].source_url == "https://sec.test/acme-10q"
        assert result.trace_id is not None
        persist_answer_grounding(
            conn,
            trace_id=result.trace_id,
            prompt_sha256="d" * 64,
            answer="Revenue increased 25 percent.",
            recorded_at=STAMP,
            llm_call_id="17",
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM ask_retrieval_trace_items").fetchone() == (1,)
        assert conn.execute(
            "SELECT prompt_sha256,llm_call_id FROM ask_answer_groundings"
        ).fetchone() == ("d" * 64, "17")
    finally:
        conn.close()


def test_enabled_semantic_retrieval_activates_typed_local_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        manifest_id = "manifest-verified"
        runtime = LocalVectorRuntimeConfig(
            index_root=tmp_path / "indexes",
            runtime_root=tmp_path / "runtime",
        )
        observed: list[tuple[str, Path, Path]] = []

        class _Backend:
            def search(self, *_args: object, **_kwargs: object) -> tuple[()]:
                return ()

        def _backend(
            _conn: sqlite3.Connection,
            *,
            manifest_id: str,
            index_root: Path,
            runtime_root: Path,
        ) -> _Backend:
            observed.append((manifest_id, index_root, runtime_root))
            return _Backend()

        class _Retriever:
            def __init__(self, _conn: sqlite3.Connection, _backend: _Backend) -> None:
                pass

            def search(self, *_args: object, **_kwargs: object) -> tuple[()]:
                return ()

        monkeypatch.setattr(grounded_retrieval_module, "promoted_vector_backend", _backend)

        def _manifest(_conn: sqlite3.Connection, _ticker: str) -> tuple[str, str, str]:
            return manifest_id, "issuer-acme", "sealed_complete_corpus"

        monkeypatch.setattr(
            grounded_retrieval_module,
            "_manifest_for_ticker",
            _manifest,
        )
        monkeypatch.setattr(grounded_retrieval_module, "HybridRetriever", _Retriever)

        result = retrieve_grounded_ask(
            conn,
            question="How fast did revenue increase?",
            tickers=("ACME",),
            created_at=STAMP,
            persist_trace=False,
            local_vector_runtime=runtime,
        )

        assert observed == [(manifest_id, runtime.index_root, runtime.runtime_root)]
        assert result.reason_code == "sealed_complete_hybrid_corpus"
    finally:
        conn.close()


def test_enabled_semantic_retrieval_fails_when_verified_backend_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    try:

        def _missing_backend(
            _conn: sqlite3.Connection,
            *,
            manifest_id: str,
            index_root: Path,
            runtime_root: Path,
        ) -> None:
            return None

        def _manifest(_conn: sqlite3.Connection, _ticker: str) -> tuple[str, str, str]:
            return (
                "manifest-verified",
                "issuer-acme",
                "sealed_complete_corpus",
            )

        monkeypatch.setattr(
            grounded_retrieval_module,
            "promoted_vector_backend",
            _missing_backend,
        )
        monkeypatch.setattr(
            grounded_retrieval_module,
            "_manifest_for_ticker",
            _manifest,
        )

        with pytest.raises(
            LocalVectorCapabilityError,
            match="semantic retrieval is enabled",
        ):
            retrieve_grounded_ask(
                conn,
                question="How fast did revenue increase?",
                tickers=("ACME",),
                created_at=STAMP,
                persist_trace=False,
                local_vector_runtime=LocalVectorRuntimeConfig(
                    index_root=tmp_path / "indexes",
                    runtime_root=tmp_path / "runtime",
                ),
            )
    finally:
        conn.close()


def test_missing_source_inventory_returns_audited_unavailable_outcome(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        result = retrieve_grounded_ask(
            conn,
            question="What changed?",
            tickers=("MISSING",),
            created_at=STAMP,
        )
        assert result.outcome == "unavailable"
        assert result.reason_code == "source_inventory_unavailable"
        assert conn.execute("SELECT outcome, reason_code FROM ask_retrieval_traces").fetchone() == (
            "unavailable",
            "source_inventory_unavailable",
        )
    finally:
        conn.close()


def test_reused_ticker_across_canonical_issuers_fails_closed(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_complete_corpus(conn)
        reconcile_source_coverage(
            conn,
            SourceCoverageImport(
                inventory_key="issuer-other:submissions",
                revision=1,
                issuer_id="issuer-other",
                ticker="ACME",
                source_kind="sec_submissions",
                source_url="https://sec.test/other-submissions",
                source_observation_id="obs",
                outcome="succeeded",
                authoritative=True,
                retrieval_config_sha256=B,
                collector_code_version="inventory@1",
                started_at=STAMP,
                completed_at=STAMP,
                recorded_at=STAMP,
                reconciled_at=STAMP,
                components=(
                    InventoryComponentImport(
                        component_key="primary",
                        component_kind="primary",
                        source_url="https://sec.test/other-submissions",
                        source_observation_id="obs",
                        outcome="succeeded",
                        ordinal=0,
                    ),
                ),
                expected_documents=(
                    ExpectedDocumentImport(
                        expected_document_key="issuer-other:missing-filing",
                        source_kind="sec_filing",
                        document_type="filing",
                        form_type="10-K",
                        accession_number="0000000002-26-000001",
                        expectation_basis="authoritative",
                        absence=ExplicitAbsence(
                            coverage_status="not_published",
                            reason_code="not_published",
                            reason_details=(("basis", "test reused ticker identity"),),
                        ),
                    ),
                ),
                apply=True,
            ),
        )

        result = retrieve_grounded_ask(
            conn,
            question="What changed?",
            tickers=("ACME",),
            created_at=STAMP,
        )

        assert result.outcome == "unavailable"
        assert result.reason_code == "ticker_identity_ambiguous"
        filters_json = conn.execute(
            "SELECT filters_json FROM ask_retrieval_traces WHERE trace_id = ?",
            (result.trace_id,),
        ).fetchone()
        assert filters_json is not None
        assert '"canonical_issuer_ids":[]' in str(filters_json[0])
    finally:
        conn.close()


def test_trace_item_exact_replay_rejects_conflicting_stored_bundle(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_complete_corpus(conn)
        retrieve_grounded_ask(
            conn,
            question="How fast did revenue increase?",
            tickers=("ACME",),
            created_at=STAMP,
        )
        conn.execute("DROP TRIGGER trg_ask_retrieval_trace_items_append_only")
        conn.execute(
            "UPDATE ask_retrieval_trace_items SET bundle_sha256 = ?",
            ("f" * 64,),
        )
        conn.commit()

        with pytest.raises(
            ValueError,
            match="immutable Ask retrieval trace item conflicts with existing data",
        ):
            retrieve_grounded_ask(
                conn,
                question="How fast did revenue increase?",
                tickers=("ACME",),
                created_at=STAMP,
            )
    finally:
        conn.close()


def test_integrity_audit_recomputes_trace_bundle_and_manifest_membership(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_complete_corpus(conn)
        retrieve_grounded_ask(
            conn,
            question="How fast did revenue increase?",
            tickers=("ACME",),
            created_at=STAMP,
        )
        conn.execute("DROP TRIGGER trg_ask_retrieval_trace_items_append_only")
        conn.execute("DROP TRIGGER trg_ask_retrieval_traces_append_only")
        conn.execute(
            "UPDATE ask_retrieval_trace_items SET bundle_sha256 = ?",
            ("f" * 64,),
        )
        conn.execute(
            "UPDATE ask_retrieval_traces SET manifest_ids_json = '[]'",
        )
        conn.commit()

        summary = audit_connection(conn, AuditOptions())
        codes = {finding.code for finding in summary.findings}
        assert "ASK_TRACE_ITEM_BUNDLE_DIGEST_MISMATCH" in codes
        assert "ASK_TRACE_ITEM_OUTSIDE_MANIFEST_SET" in codes
    finally:
        conn.close()
