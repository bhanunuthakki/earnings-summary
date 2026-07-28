"""Contracts for explicit image semantic-review initialization."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from alembic.config import Config

from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    ExtractionRun,
    SourceObservation,
)
from provenance.semantic_disposition import (
    SemanticReviewInitializationRequest,
    initialize_semantic_review_queue,
)
from provenance.source_coverage import (
    CoverageAssessment,
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 16, 0)
SHA = "a" * 64
CONFIG_SHA = "b" * 64
INVENTORY_KEY = "issuer-acme:sec-submissions"


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "semantic-disposition.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0213_evidence_ledger_foundation")
    command.stamp(config, "0215_observation_resolution_ledger")
    command.upgrade(config, "0216_search_corpus_foundation")
    command.stamp(config, "0218_evidence_replica_links")
    command.upgrade(config, "0219_source_coverage_ledger")
    command.stamp(config, "0231_legacy_document_evidence_bindings")
    command.upgrade(config, "0232_document_semantic_dispositions")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    evidence = EvidenceLedger(conn)
    evidence.persist(
        ContentBlob(
            sha256=SHA,
            byte_size=12,
            media_type="image/jpeg",
            storage_uri="file:///chart.jpg",
            recorded_at=STAMP,
        )
    )
    evidence.persist(
        SourceObservation(
            observation_id="image-observation",
            idempotency_key="image-observation",
            source_kind="sec",
            source_url="https://sec.test/chart.jpg",
            blob_sha256=SHA,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="collector@1",
        )
    )
    evidence.persist(
        DocumentVersion(
            document_version_id="image-document",
            document_key="ACME:chart",
            version_sequence=1,
            observation_id="image-observation",
            blob_sha256=SHA,
            issuer_id="issuer-acme",
            ticker="ACME",
            document_type="sec_attachment",
            form_type="10-K",
            language="en",
            recorded_at=STAMP,
        )
    )
    evidence.persist(
        ExtractionRun(
            extraction_run_id="unsupported-fulltext",
            idempotency_key="unsupported-fulltext",
            document_version_id="image-document",
            input_sha256=SHA,
            extractor_name="fulltext-evidence-backfill",
            extractor_config_sha256=CONFIG_SHA,
            extractor_code_version="fulltext-evidence-backfill@1",
            output_sha256=CONFIG_SHA,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="failed",
        )
    )
    coverage = SourceCoverageLedger(conn)
    coverage.persist(
        SourceInventorySnapshot(
            snapshot_id="snapshot",
            idempotency_key="snapshot",
            inventory_key=INVENTORY_KEY,
            revision=1,
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://sec.test/submissions",
            source_observation_id="image-observation",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="inventory@1",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
        )
    )
    coverage.persist(
        ExpectedDocument(
            expected_document_id="expected-image",
            idempotency_key="expected-image",
            snapshot_id="snapshot",
            expected_document_key="ACME:chart",
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_filing",
            document_type="sec_attachment",
            source_url="https://sec.test/chart.jpg",
            expectation_basis="authoritative",
            recorded_at=STAMP,
        )
    )
    coverage.persist(
        CoverageAssessment(
            assessment_id="coverage-image",
            idempotency_key="coverage-image",
            expected_document_id="expected-image",
            revision=1,
            coverage_status="captured",
            document_version_id="image-document",
            reason_code="exact_document_evidence",
            reason_details=(("source", "SEC"),),
            decision_kind="deterministic",
            policy_name="capture",
            policy_version="1",
            policy_config_sha256=CONFIG_SHA,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            material_dissent=False,
        )
    )
    conn.commit()


def test_image_review_initialization_is_bounded_dry_run_first_and_replay_safe(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed(conn)
        request = SemanticReviewInitializationRequest(
            inventory_keys=(INVENTORY_KEY,),
            recorded_at=STAMP,
            batch_size=10,
        )

        dry = initialize_semantic_review_queue(conn, request)
        assert dry.assessments_planned == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM document_semantic_disposition_revisions").fetchone()[
                0
            ]
            == 0
        )

        applied = initialize_semantic_review_queue(
            conn,
            request.model_copy(update={"apply": True}),
        )
        assert applied.assessments_created == 1
        assert conn.execute(
            "SELECT semantic_status, decision_kind FROM v_document_semantic_dispositions_current"
        ).fetchone() == ("review_required", "deterministic")
        assert (
            initialize_semantic_review_queue(
                conn,
                request.model_copy(update={"apply": True}),
            ).assessments_planned
            == 0
        )
    finally:
        conn.close()
