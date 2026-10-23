"""Analysis purpose boundaries survive coordinate selection and receipt removal."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest

import provenance.population_document_processing as processing
import provenance.population_research_snapshots as research
import provenance.research_snapshot as snapshots
from provenance.analysis_scope import AnalysisEvidenceScope, build_analysis_scope
from provenance.population_research_snapshots import (
    ResearchSnapshotPlanError,
    select_exact_corpus_coordinate,
)
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    DocumentProcessingScope,
    ResearchSnapshotRequest,
    ResearchUniverse,
)
from tests.test_analysis_scope import K, scope_request
from tests.test_analysis_scope_processing import processing_db


@pytest.fixture
def purposes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Iterator[tuple[AnalysisEvidenceScope, AnalysisEvidenceScope]]:
    conn, valuation = processing_db(tmp_path, migrated_db)
    try:
        earnings = build_analysis_scope(conn, scope_request(purpose="earnings_update"))
        assert valuation.entries == earnings.entries
        assert valuation.scope_id != earnings.scope_id
        yield valuation, earnings
    finally:
        conn.close()


def _coordinates() -> sqlite3.Connection:
    """Minimal coordinate tables, as used by the established selector tests."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE document_processing_snapshot_headers (
            processing_snapshot_id TEXT, scope_json TEXT, cutoff_at TEXT, recorded_at TEXT
        );
        CREATE TABLE document_processing_snapshot_seals (
            processing_snapshot_id TEXT, member_set_sha256 TEXT, sealed_at TEXT
        );
        CREATE TABLE document_processing_snapshot_members (
            processing_snapshot_id TEXT, document_version_id TEXT
        );
        CREATE TABLE v_evidence_document_versions_canonical (
            document_version_id TEXT, issuer_id TEXT
        );
        CREATE TABLE search_corpus_manifests (
            manifest_id TEXT, corpus_key TEXT, revision INTEGER,
            knowledge_cutoff TEXT, recorded_at TEXT
        );
        CREATE TABLE search_corpus_manifest_seals (
            manifest_id TEXT, completion_status TEXT, sealed_at TEXT
        );
        CREATE TABLE search_corpus_document_memberships (
            manifest_id TEXT, document_version_id TEXT, membership_status TEXT
        );
        CREATE TABLE expected_document_obligation_bindings (expected_document_id TEXT);
        CREATE TABLE reporting_entities (reporting_entity_id TEXT, issuer_id TEXT);
        CREATE TABLE research_snapshot_universe_commitments (research_snapshot_id TEXT);
        CREATE TABLE search_manifest_source_inventories (manifest_id TEXT, snapshot_id TEXT);
        CREATE TABLE document_processing_evidence_headers (
            evidence_seal_id TEXT, cutoff_at TEXT, recorded_at TEXT
        );
        CREATE TABLE document_processing_evidence_seals (
            evidence_seal_id TEXT, member_set_sha256 TEXT, sealed_at TEXT
        );
        CREATE TABLE document_processing_disposition_headers (
            processing_disposition_id TEXT, knowledge_at TEXT, recorded_at TEXT
        );
        CREATE TABLE document_processing_disposition_seals (
            processing_disposition_id TEXT, member_set_sha256 TEXT, sealed_at TEXT
        );
        CREATE TABLE research_snapshot_headers (
            research_snapshot_id TEXT, request_json TEXT, cutoff_at TEXT, recorded_at TEXT
        );
        CREATE TABLE research_snapshot_seals (
            research_snapshot_id TEXT, member_set_sha256 TEXT, sealed_at TEXT
        );
    """)
    conn.execute(
        "INSERT INTO v_evidence_document_versions_canonical VALUES (?,?)",
        (
            "document:expected-10k",
            "issuer-acme",
        ),
    )
    return conn


def _add_processing(
    conn: sqlite3.Connection, identity: str, scope: AnalysisEvidenceScope | None
) -> None:
    selection = DocumentProcessingScope(
        document_version_ids=("document:expected-10k",), analysis_scope=scope
    )
    conn.execute(
        "INSERT INTO document_processing_snapshot_headers VALUES (?,?,?,?)",
        (
            identity,
            selection.model_dump_json(),
            K.isoformat(),
            K.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO document_processing_snapshot_seals VALUES (?,?,?)",
        (
            identity,
            "a" * 64,
            K.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO document_processing_snapshot_members VALUES (?,?)",
        (
            identity,
            "document:expected-10k",
        ),
    )


def _add_corpus(
    conn: sqlite3.Connection, identity: str, scope: AnalysisEvidenceScope | None
) -> None:
    conn.execute(
        "INSERT INTO search_corpus_manifests VALUES (?,?,?,?,?)",
        (
            identity,
            "archive-wide" if scope is None else scope.scope_id,
            1,
            K.isoformat(),
            K.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO search_corpus_manifest_seals VALUES (?,?,?)",
        (
            identity,
            "complete",
            K.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO search_corpus_document_memberships VALUES (?,?,?)",
        (
            identity,
            "document:expected-10k",
            "included",
        ),
    )


@pytest.mark.parametrize("coordinate", ["processing", "corpus"])
def test_identical_documents_cannot_cross_analysis_purposes(
    purposes: tuple[AnalysisEvidenceScope, AnalysisEvidenceScope], coordinate: str
) -> None:
    valuation, earnings = purposes
    conn = _coordinates()
    try:
        if coordinate == "processing":
            _add_processing(conn, "valuation-processing", valuation)
            select = getattr(research, "_processing_coordinate")
            assert (
                select(conn, "issuer-acme", K, K, analysis_scope=valuation)[0]
                == "valuation-processing"
            )
            with pytest.raises(ResearchSnapshotPlanError, match="processing_snapshot_missing"):
                select(conn, "issuer-acme", K, K, analysis_scope=earnings)
        else:
            _add_corpus(conn, "valuation-corpus", valuation)
            assert (
                select_exact_corpus_coordinate(
                    conn, ("document:expected-10k",), K, analysis_scope=valuation
                )
                == "valuation-corpus"
            )
            with pytest.raises(ResearchSnapshotPlanError, match="exact_search_corpus_missing"):
                select_exact_corpus_coordinate(
                    conn, ("document:expected-10k",), K, analysis_scope=earnings
                )
    finally:
        conn.close()


@pytest.mark.parametrize("coordinate", ["processing", "corpus"])
def test_unscoped_selection_rejects_scoped_coordinates(
    purposes: tuple[AnalysisEvidenceScope, AnalysisEvidenceScope], coordinate: str
) -> None:
    scope, _ = purposes
    conn = _coordinates()
    try:
        if coordinate == "processing":
            _add_processing(conn, "scoped-processing", scope)
            select = getattr(research, "_processing_coordinate")
            with pytest.raises(ResearchSnapshotPlanError, match="processing_snapshot_missing"):
                select(conn, "issuer-acme", K, K)
            _add_processing(conn, "archive-processing", None)
            assert select(conn, "issuer-acme", K, K)[0] == "archive-processing"
        else:
            _add_corpus(conn, "scoped-corpus", scope)
            with pytest.raises(ResearchSnapshotPlanError, match="exact_search_corpus_missing"):
                select_exact_corpus_coordinate(conn, ("document:expected-10k",), K)
            _add_corpus(conn, "archive-corpus", None)
            assert (
                select_exact_corpus_coordinate(conn, ("document:expected-10k",), K)
                == "archive-corpus"
            )
    finally:
        conn.close()


@pytest.mark.parametrize("coordinate", ["processing", "corpus"])
def test_removing_scope_receipt_cannot_disguise_scoped_research(
    purposes: tuple[AnalysisEvidenceScope, AnalysisEvidenceScope], coordinate: str
) -> None:
    scope, _ = purposes
    conn = _coordinates()
    try:
        _add_processing(conn, "processing", scope if coordinate == "processing" else None)
        _add_corpus(conn, "corpus", scope if coordinate == "corpus" else None)
        original = ResearchUniverse(
            issuer_id="issuer-acme",
            reporting_entity_ids=("reporting-acme",),
            document_version_ids=("document:expected-10k",),
            source_obligation_revision_ids=("sec-periodic:v2",),
            analysis_scope=scope,
        )
        stripped = ResearchUniverse.model_validate(original.model_dump(exclude={"analysis_scope"}))
        request = ResearchSnapshotRequest(
            research_snapshot_id="research",
            idempotency_key="research",
            research_universe=stripped,
            processing_snapshot_ids=("processing",),
            corpus_bundles=(
                CorpusProjectionBundle(
                    corpus_manifest_id="corpus",
                    lexical_index_run_id="lexical",
                ),
            ),
            ontology_snapshot_id="ontology",
            canonical_fact_resolution_snapshot_id="resolution",
            canonical_fact_projection_run_id="projection",
            source_fact_publication_ids=(),
            cutoff_at=K,
            recorded_at=K,
        )
        verify = getattr(snapshots, "_verify_research_universe")
        with pytest.raises(ValueError, match="requires its evidence scope receipt"):
            verify(conn, request, verify_fact_subjects=False)
    finally:
        conn.close()


def test_scoped_snapshots_do_not_change_archive_counts_or_output_commitments(
    purposes: tuple[AnalysisEvidenceScope, AnalysisEvidenceScope],
) -> None:
    scope, _ = purposes
    conn = _coordinates()
    try:
        _add_processing(conn, "archive-processing", None)
        count = getattr(processing, "_processing_snapshot_count")
        processing_output = getattr(processing, "_output_commitment")
        research_output = getattr(research, "_output_commitment")
        before = (count(conn, K, K), processing_output(conn, K, (), K), research_output(conn, K))
        _add_processing(conn, "scoped-processing", scope)
        conn.execute(
            "INSERT INTO research_snapshot_headers VALUES (?,?,?,?)",
            (
                "scoped-research",
                '{"research_universe":{"analysis_scope":{"scope_id":"' + scope.scope_id + '"}}}',
                K.isoformat(),
                K.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO research_snapshot_seals VALUES (?,?,?)",
            ("scoped-research", "a" * 64, K.isoformat()),
        )
        after = (count(conn, K, K), processing_output(conn, K, (), K), research_output(conn, K))
        assert before == after
        assert after[0] == 1
        assert count(conn, K, K, analysis_scope=scope) == 1
    finally:
        conn.close()


def test_scoped_research_output_ignores_snapshots_not_yet_observed(
    purposes: tuple[AnalysisEvidenceScope, AnalysisEvidenceScope],
) -> None:
    scope, _ = purposes
    conn = _coordinates()
    try:
        output = getattr(research, "_output_commitment")
        before = output(conn, K, analysis_scope=scope, observed_through=K)
        later = K + timedelta(hours=1)
        conn.execute(
            "INSERT INTO research_snapshot_headers VALUES (?,?,?,?)",
            (
                "later-research",
                '{"research_universe":{"analysis_scope":{"scope_id":"' + scope.scope_id + '"}}}',
                K.isoformat(),
                later.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO research_snapshot_seals VALUES (?,?,?)",
            ("later-research", "a" * 64, later.isoformat()),
        )
        assert output(conn, K, analysis_scope=scope, observed_through=K) == before
        assert output(conn, K, analysis_scope=scope, observed_through=later) != before
    finally:
        conn.close()
