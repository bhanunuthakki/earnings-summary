"""Contracts for the additive canonical evidence ledger (0213).

The ledger intentionally does not alter existing document writers.  It records
new immutable evidence independently, and a view projects the latest revision
of each logical evidence node without rewriting history.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    EvidenceNodeKind,
    ExtractionRun,
    SourceObservation,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0213_decision_draft_provider_id"
HEAD = "0213_evidence_ledger_foundation"
_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_STAMP = datetime(2026, 7, 26, 12, 0, 0)


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _migrated_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "evidence-ledger.db"
    cfg = _config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ledger(conn: sqlite3.Connection) -> EvidenceLedger:
    return EvidenceLedger(conn)


def _blob() -> ContentBlob:
    return ContentBlob(
        sha256=_SHA_A,
        byte_size=42,
        media_type="text/plain",
        storage_uri="file:///evidence/acme-q2.txt",
        recorded_at=_STAMP,
    )


def _observation() -> SourceObservation:
    return SourceObservation(
        observation_id="obs-acme-q2",
        idempotency_key="sec:acme:2026q2:retrieval-1",
        source_kind="sec_filing",
        source_url="https://www.sec.gov/Archives/acme-q2.htm",
        blob_sha256=_SHA_A,
        source_published_at=datetime(2026, 7, 20, 8, 0, 0),
        filing_at=datetime(2026, 7, 20, 8, 1, 0),
        accepted_at=datetime(2026, 7, 20, 8, 2, 0),
        observed_at=datetime(2026, 7, 20, 8, 3, 0),
        retrieved_at=_STAMP,
        retrieval_config_sha256=_SHA_B,
        collector_code_version="collector@2026.07.26",
    )


def _document() -> DocumentVersion:
    return DocumentVersion(
        document_version_id="doc-acme-q2-v1",
        document_key="ACME:10-Q:2026-06-30",
        version_sequence=1,
        observation_id="obs-acme-q2",
        blob_sha256=_SHA_A,
        issuer_id="0000123456",
        ticker="ACME",
        document_type="10-Q",
        form_type="10-Q",
        accession_number="0000123456-26-000042",
        exhibit_id=None,
        period_start=datetime(2026, 4, 1, 0, 0, 0),
        period_end=datetime(2026, 6, 30, 0, 0, 0),
        as_of_at=datetime(2026, 6, 30, 0, 0, 0),
        language="en",
        replaces_document_version_id=None,
        legacy_document_id=73,
        recorded_at=_STAMP,
    )


def _run() -> ExtractionRun:
    return ExtractionRun(
        extraction_run_id="extract-acme-q2-v1",
        idempotency_key="extract:acme-q2:tables:v1",
        document_version_id="doc-acme-q2-v1",
        input_sha256=_SHA_A,
        extractor_name="filing-table-parser",
        extractor_config_sha256=_SHA_B,
        extractor_code_version="parser@2026.07.26",
        output_sha256=_SHA_C,
        started_at=_STAMP,
        completed_at=_STAMP,
        outcome="succeeded",
    )


def _node(
    *,
    node_id: str,
    revision: int,
    supersedes_node_id: str | None = None,
    node_kind: EvidenceNodeKind = "claim",
) -> EvidenceNode:
    return EvidenceNode(
        node_id=node_id,
        evidence_key="ACME:revenue:2026Q2",
        revision=revision,
        extraction_run_id="extract-acme-q2-v1",
        parent_node_id=None,
        supersedes_node_id=supersedes_node_id,
        node_kind=node_kind,
        text="Revenue was $100 million.",
        locator=EvidenceLocator(
            source_ref="0000123456-26-000042",
            filing_section_key_raw="Item 1",
            filing_ordinal=0,
            page_number=3,
            table_name="income_statement",
            table_row_index=2,
            table_column_index=1,
            legacy_table="filing_sections",
            legacy_row_id=42,
        ),
        recorded_at=_STAMP,
    )


def _seed_chain(conn: sqlite3.Connection) -> EvidenceLedger:
    ledger = _ledger(conn)
    assert ledger.persist(_blob()).created
    assert ledger.persist(_observation()).created
    assert ledger.persist(_document()).created
    assert ledger.persist(_run()).created
    return ledger


def test_models_require_hashes_and_typed_times() -> None:
    with pytest.raises(ValidationError):
        ContentBlob(
            sha256="not-a-hash",
            byte_size=1,
            media_type="text/plain",
            storage_uri="file:///x",
            recorded_at=_STAMP,
        )
    with pytest.raises(ValidationError):
        SourceObservation(
            observation_id="bad-time",
            idempotency_key="bad-time",
            source_kind="sec_filing",
            source_url="https://example.test/a",
            blob_sha256=_SHA_A,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=cast(datetime, "not-a-time"),
            retrieved_at=_STAMP,
            retrieval_config_sha256=_SHA_B,
            collector_code_version="collector@1",
        )


def test_models_reject_reverse_clocks_periods_and_locator_ranges() -> None:
    observation_values = _observation().model_dump()
    observation_values["retrieved_at"] = datetime(2026, 7, 20, 8, 2, 0)
    observation_values["observed_at"] = datetime(2026, 7, 20, 8, 3, 0)
    with pytest.raises(ValidationError, match="retrieved_at"):
        SourceObservation(**observation_values)

    document_values = _document().model_dump()
    document_values["period_start"] = datetime(2026, 7, 1)
    document_values["period_end"] = datetime(2026, 6, 30)
    with pytest.raises(ValidationError, match="period_end"):
        DocumentVersion(**document_values)

    run_values = _run().model_dump()
    run_values["started_at"] = datetime(2026, 7, 26, 12, 1)
    run_values["completed_at"] = datetime(2026, 7, 26, 12, 0)
    with pytest.raises(ValidationError, match="completed_at"):
        ExtractionRun(**run_values)

    with pytest.raises(ValidationError, match="bbox"):
        EvidenceLocator(page_number=1, bbox=(50, 40, 10, 20))
    with pytest.raises(ValidationError, match="legacy_table"):
        EvidenceLocator(legacy_row_id=42)


def test_migration_creates_all_ledger_tables_and_current_projection(tmp_path: Path) -> None:
    conn = _migrated_conn(tmp_path)
    try:
        objects = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert {
            "evidence_content_blobs",
            "evidence_source_observations",
            "evidence_document_versions",
            "evidence_extraction_runs",
            "evidence_nodes",
            "v_evidence_current",
        } <= objects
        doc_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(evidence_document_versions)")
        }
        assert {
            "issuer_id",
            "ticker",
            "form_type",
            "accession_number",
            "exhibit_id",
            "period_start",
            "period_end",
            "as_of_at",
            "language",
            "replaces_document_version_id",
            "legacy_document_id",
        } <= doc_columns
        run_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(evidence_extraction_runs)")
        }
        assert "output_sha256" in run_columns
        node_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(evidence_nodes)")}
        assert {"locator_json", "locator_sha256"} <= node_columns
    finally:
        conn.close()


def test_foreign_keys_and_cross_record_hash_contracts_are_enforced(tmp_path: Path) -> None:
    conn = _migrated_conn(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO evidence_source_observations "
                "(observation_id, idempotency_key, source_kind, source_url, blob_sha256, observed_at, "
                "retrieved_at, retrieval_config_sha256, collector_code_version) "
                "VALUES ('orphan', 'orphan', 'sec', 'https://example.test', ?, ?, ?, ?, 'collector@1')",
                (_SHA_A, _STAMP, _STAMP, _SHA_B),
            )
        ledger = _seed_chain(conn)
        with pytest.raises(sqlite3.IntegrityError, match="document version blob"):
            conn.execute(
                "INSERT INTO evidence_document_versions "
                "(document_version_id, document_key, version_sequence, observation_id, blob_sha256, "
                "document_type, recorded_at) VALUES ('wrong-blob', 'ACME:10-Q:2026-06-30', 2, "
                "'obs-acme-q2', ?, '10-Q', ?)",
                (_SHA_B, _STAMP),
            )
        assert ledger.persist(_run()).created is False
    finally:
        conn.close()


def test_idempotency_and_immutable_ledger_rows(tmp_path: Path) -> None:
    conn = _migrated_conn(tmp_path)
    try:
        ledger = _seed_chain(conn)
        assert ledger.persist(_blob()).created is False
        assert ledger.persist(_observation()).created is False
        assert ledger.persist(_document()).created is False
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE evidence_content_blobs SET storage_uri = 'file:///other' WHERE sha256 = ?",
                (_SHA_A,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM evidence_source_observations WHERE observation_id = 'obs-acme-q2'"
            )
    finally:
        conn.close()


def test_append_only_nodes_project_only_the_current_revision(tmp_path: Path) -> None:
    conn = _migrated_conn(tmp_path)
    try:
        ledger = _seed_chain(conn)
        assert ledger.persist(_node(node_id="node-r1", revision=1)).created
        assert ledger.persist(
            _node(node_id="node-r2", revision=2, supersedes_node_id="node-r1")
        ).created
        current = conn.execute(
            "SELECT node_id, revision FROM v_evidence_current WHERE evidence_key = ?",
            ("ACME:revenue:2026Q2",),
        ).fetchall()
        assert current == [("node-r2", 2)]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE evidence_nodes SET text = 'changed' WHERE node_id = 'node-r1'")
        with pytest.raises(sqlite3.IntegrityError, match="previous revision"):
            ledger.persist(_node(node_id="node-r4", revision=4, supersedes_node_id="node-r2"))
    finally:
        conn.close()


def test_failed_extraction_run_cannot_emit_evidence_nodes(tmp_path: Path) -> None:
    conn = _migrated_conn(tmp_path)
    try:
        ledger = _seed_chain(conn)
        failed_values = _run().model_dump()
        failed_values.update(
            extraction_run_id="extract-acme-q2-failed",
            idempotency_key="extract:acme-q2:tables:failed",
            extractor_config_sha256=_SHA_C,
            outcome="failed",
        )
        failed = ExtractionRun(**failed_values)
        assert ledger.persist(failed).created
        node_values = _node(node_id="failed-node", revision=1).model_dump()
        node_values["extraction_run_id"] = failed.extraction_run_id
        with pytest.raises(sqlite3.IntegrityError, match="succeeded extraction"):
            ledger.persist(EvidenceNode(**node_values))
    finally:
        conn.close()


def test_locator_is_typed_canonical_and_supports_the_full_corpus_node_kinds(tmp_path: Path) -> None:
    conn = _migrated_conn(tmp_path)
    try:
        ledger = _seed_chain(conn)
        node = _node(node_id="table-row", revision=1, node_kind="table_row")
        assert ledger.persist(node).created
        assert node.locator_sha256 is not None
        stored = conn.execute(
            "SELECT locator_json, locator_sha256 FROM evidence_nodes WHERE node_id = 'table-row'"
        ).fetchone()
        assert stored is not None
        assert stored[0] == node.locator.canonical_json if node.locator is not None else False
        assert stored[1] == node.locator_sha256
        with pytest.raises(ValidationError):
            EvidenceLocator.model_validate({"page_number": 3, "unknown": "not allowed"})
        with pytest.raises(ValidationError):
            _node(node_id="unsupported", revision=1, node_kind=cast(EvidenceNodeKind, "headline"))
    finally:
        conn.close()


def test_locator_closes_office_slide_and_worksheet_coordinates() -> None:
    slide = EvidenceLocator(source_ref="deck.pptx", slide_number=3, shape_index=0)
    assert slide.canonical_json == ('{"shape_index":0,"slide_number":3,"source_ref":"deck.pptx"}')
    cell_range = EvidenceLocator(
        source_ref="supplement.xlsx",
        sheet_name="Income Statement",
        cell_range="A10:XFD10",
    )
    assert '"cell_range":"A10:XFD10"' in cell_range.canonical_json
    with pytest.raises(ValidationError, match="shape_index requires slide_number"):
        EvidenceLocator(shape_index=1)
    with pytest.raises(ValidationError, match="require sheet_name"):
        EvidenceLocator(cell_address="B12")
    with pytest.raises(ValidationError, match="cannot be combined"):
        EvidenceLocator(slide_number=1, sheet_name="Sheet1")


def test_legacy_document_bridge_is_unique_and_validated_when_documents_exist(
    tmp_path: Path,
) -> None:
    conn = _migrated_conn(tmp_path)
    try:
        ledger = _ledger(conn)
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
        with pytest.raises(ValueError, match=r"legacy documents\.id 73"):
            ledger.persist(_document())
        conn.execute("INSERT INTO documents (id) VALUES (73)")
        assert ledger.persist(_blob()).created
        assert ledger.persist(_observation()).created
        assert ledger.persist(_document()).created
    finally:
        conn.close()


def test_migration_downgrade_round_trip_removes_only_ledger_objects(tmp_path: Path) -> None:
    db_path = tmp_path / "downgrade.db"
    cfg = _config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_path))
    try:
        remaining = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert "evidence_content_blobs" not in remaining
        assert "v_evidence_current" not in remaining
    finally:
        conn.close()
