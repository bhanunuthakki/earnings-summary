"""Contracts for immutable expected-source inventory and coverage assessments."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.source_coverage import (
    CoverageAssessment,
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)

ROOT = Path(__file__).resolve().parents[1]
PRIOR = "0218_evidence_replica_links"
HEAD = "0219_source_coverage_ledger"
STAMP = datetime(2026, 7, 26, 20, 0, 0)
A, B = "a" * 64, "b" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "coverage.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0213_evidence_ledger_foundation")
    command.stamp(config, "0215_observation_resolution_ledger")
    command.upgrade(config, "0216_search_corpus_foundation")
    command.stamp(config, PRIOR)
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    EvidenceLedger(conn).persist(
        ContentBlob(
            sha256=A,
            byte_size=10,
            media_type="application/json",
            storage_uri="file:///sec/submissions.json",
            recorded_at=STAMP,
        )
    )
    EvidenceLedger(conn).persist(
        SourceObservation(
            observation_id="sec-inventory-observation",
            idempotency_key="sec-inventory-observation",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
            blob_sha256=A,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=B,
            collector_code_version="sec-inventory@1",
        )
    )
    return conn


def _snapshot(
    *,
    snapshot_id: str = "sec-snapshot-r1",
    idempotency_key: str = "sec-snapshot-r1",
    revision: int = 1,
    supersedes_snapshot_id: str | None = None,
) -> SourceInventorySnapshot:
    return SourceInventorySnapshot(
        snapshot_id=snapshot_id,
        idempotency_key=idempotency_key,
        inventory_key="issuer-acme:sec-submissions",
        revision=revision,
        issuer_id="issuer-acme",
        ticker="ACME",
        source_kind="sec_submissions",
        source_url="https://data.sec.gov/submissions/CIK0000000001.json",
        source_observation_id="sec-inventory-observation",
        outcome="succeeded",
        authoritative=True,
        retrieval_config_sha256=B,
        collector_code_version="sec-inventory@1",
        started_at=STAMP,
        completed_at=STAMP,
        recorded_at=STAMP,
        supersedes_snapshot_id=supersedes_snapshot_id,
    )


def _expected() -> ExpectedDocument:
    return ExpectedDocument(
        expected_document_id="expected-acme-10k-2025",
        idempotency_key="expected-acme-10k-2025",
        snapshot_id="sec-snapshot-r1",
        expected_document_key="ACME:10-K:0000000001-26-000001",
        issuer_id="issuer-acme",
        ticker="ACME",
        source_kind="sec_filing",
        document_type="annual_report",
        form_type="10-K",
        accession_number="0000000001-26-000001",
        source_url="https://www.sec.gov/Archives/acme-10k.htm",
        primary_document="acme-20251231x10k.htm",
        period_end=STAMP,
        filing_at=STAMP,
        expected_at=STAMP,
        expectation_basis="authoritative",
        recorded_at=STAMP,
    )


def _assessment(
    *,
    assessment_id: str = "assessment-r1",
    revision: int = 1,
    status: str = "available",
    supersedes_assessment_id: str | None = None,
    document_version_id: str | None = None,
) -> CoverageAssessment:
    return CoverageAssessment(
        assessment_id=assessment_id,
        idempotency_key=assessment_id,
        expected_document_id="expected-acme-10k-2025",
        revision=revision,
        coverage_status=status,
        document_version_id=document_version_id,
        extraction_run_id=None,
        manifest_id=None,
        index_run_id=None,
        reason_code="source_inventory_reconciled",
        reason_details=(("accession", "0000000001-26-000001"),),
        decision_kind="deterministic",
        policy_name="source-coverage-reconcile",
        policy_version="1",
        policy_config_sha256=B,
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
        supersedes_assessment_id=supersedes_assessment_id,
        material_dissent=False,
    )


def test_inventory_expected_document_and_assessment_are_append_only_and_current(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        ledger = SourceCoverageLedger(conn)
        assert ledger.persist(_snapshot()).created
        assert ledger.persist(_expected()).created
        assert ledger.persist(_assessment()).created
        assert not ledger.persist(_assessment()).created
        current = conn.execute("SELECT coverage_status FROM v_source_coverage_current").fetchone()
        assert current == ("available",)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE source_coverage_assessments SET coverage_status = 'fetch_failed'")
    finally:
        conn.close()


def test_revision_chains_are_same_scope_and_failed_inventory_cannot_claim_documents(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        ledger = SourceCoverageLedger(conn)
        ledger.persist(_snapshot())
        ledger.persist(
            _snapshot(
                snapshot_id="sec-snapshot-r2",
                idempotency_key="sec-snapshot-r2",
                revision=2,
                supersedes_snapshot_id="sec-snapshot-r1",
            )
        )
        assert conn.execute("SELECT snapshot_id FROM v_source_inventory_current").fetchone() == (
            "sec-snapshot-r2",
        )
        failed = SourceInventorySnapshot.model_validate(
            _snapshot().model_dump()
            | {
                "snapshot_id": "failed",
                "idempotency_key": "failed",
                "inventory_key": "issuer-acme:ir-crawl",
                "source_kind": "ir_crawl",
                "source_observation_id": None,
                "outcome": "failed",
                "authoritative": False,
            }
        )
        ledger.persist(failed)
        with pytest.raises(ValueError, match="successful or partial inventory"):
            ledger.persist(
                ExpectedDocument.model_validate(
                    _expected().model_dump()
                    | {
                        "expected_document_id": "bad-expected",
                        "idempotency_key": "bad-expected",
                        "snapshot_id": "failed",
                    }
                )
            )
    finally:
        conn.close()


def test_coverage_status_requires_progressively_stronger_lineage(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="document_version_id"):
        _assessment(status="captured")
    with pytest.raises(ValidationError, match="extraction_run_id"):
        CoverageAssessment.model_validate(
            _assessment(status="captured", document_version_id="document-v1").model_dump()
            | {"coverage_status": "extracted"}
        )
    with pytest.raises(ValidationError, match="manifest_id"):
        CoverageAssessment.model_validate(
            _assessment(status="captured", document_version_id="document-v1").model_dump()
            | {
                "coverage_status": "indexed",
                "extraction_run_id": "extract-v1",
            }
        )


def test_document_anchor_and_assessment_revision_are_validated(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        ledger = SourceCoverageLedger(conn)
        ledger.persist(_snapshot())
        ledger.persist(_expected())
        with pytest.raises(ValueError, match="document version"):
            ledger.persist(_assessment(status="captured", document_version_id="missing-document"))
        EvidenceLedger(conn).persist(
            DocumentVersion(
                document_version_id="document-v1",
                document_key="ACME:10-K:0000000001-26-000001",
                version_sequence=1,
                observation_id="sec-inventory-observation",
                blob_sha256=A,
                issuer_id="issuer-acme",
                ticker="ACME",
                document_type="annual_report",
                form_type="10-K",
                accession_number="0000000001-26-000001",
                language="en",
                recorded_at=STAMP,
            )
        )
        first = _assessment(status="captured", document_version_id="document-v1")
        ledger.persist(first)
        ledger.persist(
            _assessment(
                assessment_id="assessment-r2",
                revision=2,
                status="captured",
                supersedes_assessment_id=first.assessment_id,
                document_version_id="document-v1",
            )
        )
        assert conn.execute("SELECT revision FROM v_source_coverage_current").fetchone() == (2,)
    finally:
        conn.close()


def test_migration_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    config = _config(path)
    command.stamp(config, PRIOR)
    command.upgrade(config, HEAD)
    command.downgrade(config, PRIOR)
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'source_inventory_snapshots'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
