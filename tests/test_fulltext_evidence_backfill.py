"""Contracts for dry-run-first full-text evidence extraction."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance import fulltext_backfill as fulltext_backfill_module
from provenance.evidence_backfill import BackfillRequest, backfill_legacy_evidence
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    LedgerRecord,
    PersistResult,
    SourceObservation,
)
from provenance.fulltext_backfill import FullTextBackfillRequest, backfill_fulltext_evidence
from provenance.fulltext_extractor_identity import (
    PDF_TABLE_EXTRACTOR_NAME,
    pdf_table_extractor_code_version,
)
from provenance.source_coverage import (
    CoverageAssessment,
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_BASE_REVISION = "0213_decision_draft_provider_id"


def test_pdf_table_detector_runtime_identity_is_exact_and_closed() -> None:
    assert PDF_TABLE_EXTRACTOR_NAME == "PyMuPDF.Page.find_tables"
    assert (
        pdf_table_extractor_code_version(
            detector_version="pymupdf-dual-table-detector@1",
            pymupdf_version="1.27.0",
            mupdf_version="1.27.0",
        )
        == "pymupdf-dual-table-detector@1;pymupdf=1.27.0;mupdf=1.27.0"
    )
    with pytest.raises(ValueError, match="invalid_pdf_table_detector_version_identity"):
        pdf_table_extractor_code_version(
            detector_version="pymupdf-dual-table-detector@1;forged",
            pymupdf_version="1.27.0",
            mupdf_version="1.27.0",
        )


def _ooxml_bytes(parts: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        for name, body in parts.items():
            archive.writestr(name, body)
    return output.getvalue()


def _zip_bytes(
    parts: list[tuple[str, bytes | str]], *, compression: int = zipfile.ZIP_DEFLATED
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, body in parts:
            archive.writestr(name, body)
    return output.getvalue()


def _pptx_bytes() -> bytes:
    return _ooxml_bytes(
        {
            "ppt/presentation.xml": (
                '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/'
                '2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships"><p:sldIdLst><p:sldId id="256" r:id="rId2"/>'
                '<p:sldId id="257" r:id="rId1"/></p:sldIdLst></p:presentation>'
            ),
            "ppt/_rels/presentation.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.'
                'org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/>'
                "</Relationships>"
            ),
            "ppt/slides/slide1.xml": (
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Appendix</a:t></a:r>"
                "</a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
            ),
            "ppt/slides/slide2.xml": (
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Q2 Revenue</a:t>"
                "</a:r><a:r><a:t> +20%</a:t></a:r></a:p></p:txBody></p:sp>"
                "</p:spTree></p:cSld></p:sld>"
            ),
        }
    )


def _xlsx_bytes() -> bytes:
    return _ooxml_bytes(
        {
            "xl/workbook.xml": (
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Income Statement" sheetId="1" r:id="rId1"/></sheets>'
                "</workbook>"
            ),
            "xl/_rels/workbook.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.'
                'org/officeDocument/2006/relationships/worksheet" '
                'Target="/xl/worksheets/sheet1.xml"/></Relationships>'
            ),
            "xl/sharedStrings.xml": (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<si><t>Revenue</t></si></sst>"
            ),
            "xl/worksheets/sheet1.xml": (
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
                '<c r="B1"><v>100</v></c><c r="C1"><f>B1*1.2</f><v>120</v></c>'
                "</row></sheetData></worksheet>"
            ),
        }
    )


def _config(db_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _connection(
    tmp_path: Path,
    *,
    suffix: str = ".txt",
    content: bytes = b"Revenue grew 20%.",
    accession_number: str | None = None,
    coverage_schema: bool = False,
) -> tuple[sqlite3.Connection, Path]:
    db_path = tmp_path / "portfolio.db"
    repo_root = tmp_path / "repo"
    artifact = repo_root / "data" / f"ACME{suffix}"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE documents (
              id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL,
              doc_type TEXT NOT NULL, period_start TIMESTAMP, period_end TIMESTAMP,
              file_path TEXT NOT NULL, sha256 TEXT NOT NULL, fetched_at TIMESTAMP NOT NULL,
              fetch_status TEXT NOT NULL, raw_bytes_size INTEGER NOT NULL, source_url TEXT,
              accession_number TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO documents VALUES (1, 'ACME', 'sec_edgar', '10-Q', NULL, NULL, ?, ?, "
            "'2026-07-20 12:00:00', 'ok', ?, NULL, ?)",
            (f"data/ACME{suffix}", digest, len(content), accession_number),
        )
        conn.commit()
    finally:
        conn.close()
    config = _config(db_path)
    command.stamp(config, LEGACY_BASE_REVISION)
    command.upgrade(config, "0213_evidence_ledger_foundation")
    command.stamp(config, "0217_fact_selection_ledger")
    command.upgrade(config, "0218_evidence_replica_links")
    if coverage_schema:
        support = sqlite3.connect(db_path)
        try:
            support.executescript(
                """
                CREATE TABLE search_corpus_manifests (
                  manifest_id TEXT PRIMARY KEY
                );
                CREATE TABLE search_index_runs (
                  index_run_id TEXT PRIMARY KEY,
                  manifest_id TEXT NOT NULL,
                  outcome TEXT NOT NULL
                );
                """
            )
            support.commit()
        finally:
            support.close()
        command.upgrade(config, "0220_source_inventory_seals")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    backfill_legacy_evidence(conn, BackfillRequest(repo_root=repo_root, apply=True))
    return conn, repo_root


def _request(
    repo_root: Path, *, apply: bool, task_id: str = "fulltext-test"
) -> FullTextBackfillRequest:
    return FullTextBackfillRequest(repo_root=repo_root, apply=apply, task_id=task_id)


def _add_legacy_documents(
    conn: sqlite3.Connection,
    repo_root: Path,
    contents: list[bytes],
) -> None:
    first_id = int(conn.execute("SELECT max(id) FROM documents").fetchone()[0]) + 1
    for offset, content in enumerate(contents):
        document_id = first_id + offset
        relative_path = f"data/ACME-{document_id}.txt"
        artifact = repo_root / relative_path
        artifact.write_bytes(content)
        conn.execute(
            "INSERT INTO documents VALUES (?, 'ACME', 'sec_edgar', '10-Q', NULL, NULL, "
            "?, ?, '2026-07-20 12:00:00', 'ok', ?, NULL, NULL)",
            (
                document_id,
                relative_path,
                hashlib.sha256(content).hexdigest(),
                len(content),
            ),
        )
    conn.commit()
    backfill_legacy_evidence(
        conn,
        BackfillRequest(
            repo_root=repo_root,
            apply=True,
            task_id=f"bridge-added-{first_id}",
        ),
    )


def _seed_sealed_inventory(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    inventory_key: str,
    issuer_id: str,
    observation_id: str,
    recorded_at: datetime,
) -> None:
    SourceCoverageLedger(conn).persist(
        SourceInventorySnapshot(
            snapshot_id=snapshot_id,
            idempotency_key=snapshot_id,
            inventory_key=inventory_key,
            revision=1,
            issuer_id=issuer_id,
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://www.sec.gov/submissions/test.json",
            source_observation_id=observation_id,
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256="d" * 64,
            collector_code_version="test@1",
            started_at=recorded_at,
            completed_at=recorded_at,
            recorded_at=recorded_at,
            supersedes_snapshot_id=None,
        )
    )
    component = InventoryComponent(
        component_id=f"component:{snapshot_id}",
        idempotency_key=f"component:{snapshot_id}",
        snapshot_id=snapshot_id,
        component_key="primary",
        component_kind="primary",
        source_url="https://www.sec.gov/submissions/test.json",
        source_observation_id=observation_id,
        outcome="succeeded",
        required=True,
        failure_reason=None,
        ordinal=0,
        recorded_at=recorded_at,
    )
    store = SourceInventorySealStore(conn)
    store.persist(component)
    store.persist(
        InventorySeal(
            snapshot_id=snapshot_id,
            expected_component_count=1,
            component_digest_sha256=component_digest((component,)),
            completion_status="complete",
            sealed_at=recorded_at,
        )
    )


def _seed_covered_document(
    conn: sqlite3.Connection,
    *,
    expected_document_id: str,
    snapshot_id: str,
    issuer_id: str,
    accession_number: str,
    document_version_id: str,
    recorded_at: datetime,
) -> None:
    ledger = SourceCoverageLedger(conn)
    ledger.persist(
        ExpectedDocument(
            expected_document_id=expected_document_id,
            idempotency_key=expected_document_id,
            snapshot_id=snapshot_id,
            expected_document_key=f"expected-key:{expected_document_id}",
            issuer_id=issuer_id,
            ticker="ACME",
            source_kind="sec_filing",
            document_type="10-Q",
            form_type="10-Q",
            accession_number=accession_number,
            source_url=f"https://www.sec.gov/Archives/{expected_document_id}",
            primary_document=expected_document_id,
            period_start=None,
            period_end=None,
            filing_at=recorded_at,
            expected_at=None,
            expectation_basis="authoritative",
            recorded_at=recorded_at,
        )
    )
    ledger.persist(
        CoverageAssessment(
            assessment_id=f"coverage:{expected_document_id}",
            idempotency_key=f"coverage:{expected_document_id}",
            expected_document_id=expected_document_id,
            revision=1,
            coverage_status="captured",
            document_version_id=document_version_id,
            extraction_run_id=None,
            manifest_id=None,
            index_run_id=None,
            reason_code="captured_for_test",
            reason_details=(("source_inventory_snapshot_id", snapshot_id),),
            decision_kind="deterministic",
            policy_name="test",
            policy_version="1",
            policy_config_sha256="e" * 64,
            effective_at=recorded_at,
            knowledge_at=recorded_at,
            recorded_at=recorded_at,
            supersedes_assessment_id=None,
            material_dissent=False,
        )
    )


def _seed_archive_replica_lineage(
    conn: sqlite3.Connection,
    repo_root: Path,
    image_bytes: bytes,
    *,
    mismatch: str | None = None,
) -> str | None:
    recorded_at = datetime(2026, 7, 20, 12, 0, 0)
    archive = conn.execute(
        "SELECT document.document_version_id, document.issuer_id, "
        "document.accession_number, document.observation_id "
        "FROM evidence_document_versions AS document WHERE document.legacy_document_id = 1"
    ).fetchone()
    archive_document_version_id = str(archive[0])
    archive_issuer_id = str(archive[1])
    archive_accession = str(archive[2])
    archive_observation_id = str(archive[3])
    _seed_sealed_inventory(
        conn,
        snapshot_id="snapshot:archive",
        inventory_key="inventory:archive",
        issuer_id=archive_issuer_id,
        observation_id=archive_observation_id,
        recorded_at=recorded_at,
    )
    _seed_covered_document(
        conn,
        expected_document_id="expected:archive",
        snapshot_id="snapshot:archive",
        issuer_id=archive_issuer_id,
        accession_number=archive_accession,
        document_version_id=archive_document_version_id,
        recorded_at=recorded_at,
    )
    if mismatch == "missing":
        conn.commit()
        return None

    replica_issuer = "issuer:wrong" if mismatch == "issuer" else archive_issuer_id
    replica_accession = "wrong-accession" if mismatch == "accession" else archive_accession
    replica_snapshot = (
        "snapshot:replica" if mismatch in {"issuer", "snapshot"} else "snapshot:archive"
    )
    if replica_snapshot != "snapshot:archive":
        _seed_sealed_inventory(
            conn,
            snapshot_id=replica_snapshot,
            inventory_key=f"inventory:{mismatch}",
            issuer_id=replica_issuer,
            observation_id=archive_observation_id,
            recorded_at=recorded_at,
        )
    digest = hashlib.sha256(image_bytes).hexdigest()
    replica_path = repo_root / "data" / f"replica-{mismatch or 'exact'}.png"
    replica_path.write_bytes(image_bytes)
    evidence = EvidenceLedger(conn)
    evidence.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(image_bytes),
            media_type="image/png",
            storage_uri=replica_path.as_uri(),
            recorded_at=recorded_at,
        )
    )
    observation_id = f"replica-observation:{mismatch or 'exact'}"
    evidence.persist(
        SourceObservation(
            observation_id=observation_id,
            idempotency_key=observation_id,
            source_kind="sec_filing_document",
            source_url=f"https://www.sec.gov/replica-{mismatch or 'exact'}.png",
            blob_sha256=digest,
            source_published_at=None,
            filing_at=recorded_at,
            accepted_at=recorded_at,
            observed_at=recorded_at,
            retrieved_at=recorded_at,
            retrieval_config_sha256="f" * 64,
            collector_code_version="test@1",
        )
    )
    replica_document_version_id = f"replica-document:{mismatch or 'exact'}"
    evidence.persist(
        DocumentVersion(
            document_version_id=replica_document_version_id,
            document_key=f"replica-key:{mismatch or 'exact'}",
            version_sequence=1,
            observation_id=observation_id,
            blob_sha256=digest,
            issuer_id=replica_issuer,
            ticker="ACME",
            document_type="exhibit_image",
            form_type="10-Q",
            accession_number=replica_accession,
            exhibit_id=None,
            period_start=None,
            period_end=None,
            as_of_at=recorded_at,
            language="und",
            replaces_document_version_id=None,
            legacy_document_id=None,
            recorded_at=recorded_at,
        )
    )
    _seed_covered_document(
        conn,
        expected_document_id=f"expected:replica:{mismatch or 'exact'}",
        snapshot_id=replica_snapshot,
        issuer_id=replica_issuer,
        accession_number=replica_accession,
        document_version_id=replica_document_version_id,
        recorded_at=recorded_at,
    )
    conn.commit()
    return replica_document_version_id


def test_dry_run_detects_placeholder_only_document_without_writing(tmp_path: Path) -> None:
    conn, repo_root = _connection(tmp_path)
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=False))
        assert result.documents_considered == 1
        assert result.documents_planned == 1
        assert result.substantive_nodes_planned == 1
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == 1
    finally:
        conn.close()


def test_apply_writes_separate_run_and_replays_exactly(tmp_path: Path) -> None:
    conn, repo_root = _connection(tmp_path)
    try:
        first = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert first.documents_extracted == 1
        assert first.substantive_nodes_created == 1
        nodes = conn.execute(
            "SELECT node_kind, text, locator_json FROM evidence_nodes "
            "WHERE extraction_run_id LIKE 'fulltext-run-%' ORDER BY node_id"
        ).fetchall()
        assert [row[0] for row in nodes] == ["document", "passage"]
        assert nodes[1][1] == "Revenue grew 20%."
        assert '"char_start":0' in str(nodes[1][2])
        second = backfill_fulltext_evidence(
            conn, _request(repo_root, apply=True, task_id="fulltext-replay")
        )
        assert second.documents_skipped_covered == 1
        assert second.records_created == 0
    finally:
        conn.close()


def test_explicit_document_id_extracts_only_that_document_without_checkpoint(
    tmp_path: Path,
) -> None:
    conn, repo_root = _connection(tmp_path)
    try:
        _add_legacy_documents(conn, repo_root, [b"Second document.", b"Third document."])
        request = FullTextBackfillRequest(
            repo_root=repo_root,
            apply=True,
            document_id=2,
            task_id="target-fulltext-two",
        )

        first = backfill_fulltext_evidence(conn, request)
        second = backfill_fulltext_evidence(conn, request)

        assert first.documents_considered == 1
        assert first.last_document_id_before == 1
        assert first.last_document_id_after == 2
        assert first.has_more is False
        assert second.documents_skipped_covered == 1
        assert not (repo_root / ".tmp" / request.task_id / "state.json").exists()
        texts = {
            str(row[0])
            for row in conn.execute(
                "SELECT node.text FROM evidence_nodes AS node "
                "JOIN evidence_extraction_runs AS run "
                "ON run.extraction_run_id=node.extraction_run_id "
                "WHERE run.document_version_id='legacy-doc-2'"
            )
        }
        assert "Second document." in texts
        assert "Third document." not in texts
    finally:
        conn.close()


def test_dry_run_resumes_from_existing_checkpoint_without_advancing_it(
    tmp_path: Path,
) -> None:
    conn, repo_root = _connection(tmp_path)
    request = _request(repo_root, apply=True, task_id="fulltext-resume-parity")
    try:
        applied = backfill_fulltext_evidence(conn, request)
        dry_run = backfill_fulltext_evidence(
            conn,
            request.model_copy(update={"apply": False}),
        )

        assert applied.last_document_id_after == 1
        assert dry_run.last_document_id_before == 1
        assert dry_run.last_document_id_after == 1
        assert dry_run.documents_considered == 0
    finally:
        conn.close()


def test_record_and_node_budget_advances_only_through_fitting_prefix(
    tmp_path: Path,
) -> None:
    conn, repo_root = _connection(tmp_path)
    _add_legacy_documents(conn, repo_root, [b"Second document.", b"Third document."])
    request = FullTextBackfillRequest(
        repo_root=repo_root,
        apply=True,
        task_id="bounded-prefix",
        batch_size=100,
        max_records_per_batch=3,
        max_nodes_per_batch=1,
    )
    try:
        first = backfill_fulltext_evidence(conn, request)
        assert first.last_document_id_before == 0
        assert first.last_document_id_after == 1
        assert first.documents_sized == 2
        assert first.documents_deferred_by_budget == 2
        assert first.budget_exhausted is True
        assert first.records_budget_used == 3
        assert first.nodes_budget_used == 1
        assert first.records_budget_utilization == 1.0
        assert first.nodes_budget_utilization == 1.0
        assert first.has_more is True

        second = backfill_fulltext_evidence(conn, request)
        assert second.last_document_id_before == 1
        assert second.last_document_id_after == 2
        assert second.documents_deferred_by_budget == 1
        assert second.has_more is True

        third = backfill_fulltext_evidence(conn, request)
        assert third.last_document_id_before == 2
        assert third.last_document_id_after == 3
        assert third.documents_deferred_by_budget == 0
        assert third.has_more is False
        assert (
            conn.execute(
                "SELECT COUNT(DISTINCT document_version_id) "
                "FROM evidence_extraction_runs "
                "WHERE extractor_name = 'fulltext-evidence-backfill' "
                "AND outcome = 'succeeded'"
            ).fetchone()[0]
            == 3
        )
    finally:
        conn.close()


def test_interrupted_budgeted_transaction_rolls_back_and_resumes_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, repo_root = _connection(tmp_path)
    _add_legacy_documents(conn, repo_root, [b"Second document."])
    request = FullTextBackfillRequest(
        repo_root=repo_root,
        apply=True,
        task_id="bounded-interruption",
        max_records_per_batch=6,
        max_nodes_per_batch=2,
    )
    original_persist = EvidenceLedger.persist
    calls = 0

    def _interrupt_on_second_document(
        ledger: EvidenceLedger, record: LedgerRecord
    ) -> PersistResult:
        nonlocal calls
        result = original_persist(ledger, record)
        calls += 1
        if calls == 4:
            raise RuntimeError("simulated interruption")
        return result

    monkeypatch.setattr(EvidenceLedger, "persist", _interrupt_on_second_document)
    try:
        with pytest.raises(RuntimeError, match="simulated interruption"):
            backfill_fulltext_evidence(conn, request)
        checkpoint = repo_root / ".tmp" / "bounded-interruption" / "state.json"
        assert not checkpoint.exists()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_extraction_runs "
                "WHERE extractor_name = 'fulltext-evidence-backfill'"
            ).fetchone()[0]
            == 0
        )

        monkeypatch.setattr(EvidenceLedger, "persist", original_persist)
        resumed = backfill_fulltext_evidence(conn, request)
        assert resumed.last_document_id_before == 0
        assert resumed.last_document_id_after == 2
        assert resumed.documents_extracted == 2
        assert resumed.records_created == 6
        assert resumed.has_more is False
    finally:
        conn.close()


def test_one_per_document_bounded_oversized_candidate_is_admitted(
    tmp_path: Path,
) -> None:
    content = b"<html><body><p>One</p><p>Two</p><p>Three</p><p>Four</p></body></html>"
    conn, repo_root = _connection(tmp_path, suffix=".html", content=content)
    request = FullTextBackfillRequest(
        repo_root=repo_root,
        apply=True,
        task_id="bounded-oversized",
        max_records_per_batch=3,
        max_nodes_per_batch=2,
    )
    try:
        result = backfill_fulltext_evidence(conn, request)
        assert result.oversized_document_admitted is True
        assert result.documents_deferred_by_budget == 0
        assert result.records_budget_used == 6
        assert result.nodes_budget_used == 4
        assert result.records_budget_utilization == 2.0
        assert result.nodes_budget_utilization == 2.0
        assert result.documents_extracted == 1
        assert result.last_document_id_after == 1
        assert result.has_more is False
    finally:
        conn.close()


def test_office_scope_skips_other_formats_and_seals_checkpoint_scope(tmp_path: Path) -> None:
    conn, repo_root = _connection(tmp_path)
    conn.execute(
        "INSERT INTO documents VALUES (2, 'ACME', 'sec_edgar', '10-Q', NULL, NULL, "
        "'data/unbridged.txt', ?, '2026-07-20 12:00:00', 'ok', 1, NULL, NULL)",
        ("0" * 64,),
    )
    conn.commit()
    request = FullTextBackfillRequest(
        repo_root=repo_root,
        apply=True,
        task_id="office-scope",
        format_scope="office",
    )
    try:
        result = backfill_fulltext_evidence(conn, request)
        assert result.format_scope == "office"
        assert result.documents_considered == 2
        assert result.documents_planned == 0
        assert result.documents_quarantined == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == 1
        checkpoint = repo_root / ".tmp" / "office-scope" / "state.json"
        assert '"format_scope":"office"' in checkpoint.read_text(encoding="utf-8")
        with pytest.raises(RuntimeError, match="format scope"):
            backfill_fulltext_evidence(
                conn,
                FullTextBackfillRequest(
                    repo_root=repo_root,
                    apply=True,
                    task_id="office-scope",
                    format_scope="all",
                ),
            )
    finally:
        conn.close()


def test_partial_legacy_nodes_do_not_claim_fulltext_coverage(tmp_path: Path) -> None:
    conn, repo_root = _connection(tmp_path)
    try:
        EvidenceLedger(conn).persist(
            EvidenceNode(
                node_id="legacy-partial-section-1",
                evidence_key="legacy-partial-section:1",
                revision=1,
                extraction_run_id="legacy-run-doc-1",
                parent_node_id="legacy-node-doc-1",
                supersedes_node_id=None,
                node_kind="section",
                text="Only one section was previously parsed.",
                locator=EvidenceLocator(source_ref="data/ACME.txt"),
                recorded_at=datetime(2026, 7, 20, 12, 0, 0),
            )
        )
        conn.commit()
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 1
        assert result.documents_skipped_covered == 0
    finally:
        conn.close()


def test_html_preserves_hierarchy_dom_paths_and_exact_source_spans(tmp_path: Path) -> None:
    content = (
        b"<html><body><h1>Results</h1><script>ignore()</script>"
        b"<p>Margin expanded.</p><table><tr><th>Metric</th><th>Q2</th></tr>"
        b"<tr><td>Revenue</td><td>120</td></tr></table></body></html>"
    )
    conn, repo_root = _connection(tmp_path, suffix=".html", content=content)
    try:
        document_version_id = str(
            conn.execute(
                "SELECT document_version_id FROM evidence_document_versions "
                "WHERE legacy_document_id = 1"
            ).fetchone()[0]
        )
        stable_token = hashlib.sha256(document_version_id.encode("utf-8")).hexdigest()[:48]
        old_run_id = "old-html-run"
        recorded_at = datetime(2026, 7, 20, 12, 0, 0)
        old_node_id = "old-monolithic-html-passage"
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ExtractionRun(
                extraction_run_id=old_run_id,
                idempotency_key="old-html-run",
                document_version_id=document_version_id,
                input_sha256=hashlib.sha256(content).hexdigest(),
                extractor_name="fulltext-evidence-backfill",
                extractor_config_sha256="b" * 64,
                extractor_code_version="fulltext-evidence-backfill@1",
                output_sha256="c" * 64,
                started_at=recorded_at,
                completed_at=recorded_at,
                outcome="succeeded",
            )
        )
        ledger.persist(
            ExtractionRun(
                extraction_run_id="old-structured-html-run",
                idempotency_key=(f"fulltext-structured-web-archive:{stable_token}"),
                document_version_id=document_version_id,
                input_sha256=hashlib.sha256(content).hexdigest(),
                extractor_name="fulltext-evidence-backfill",
                extractor_config_sha256="d" * 64,
                extractor_code_version=("fulltext-evidence-backfill@3-structured-web-archive"),
                output_sha256="e" * 64,
                started_at=recorded_at,
                completed_at=recorded_at,
                outcome="failed",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id=old_node_id,
                evidence_key=f"fulltext-content:{stable_token}:1",
                revision=1,
                extraction_run_id=old_run_id,
                parent_node_id=None,
                supersedes_node_id=None,
                node_kind="passage",
                text="Results Margin expanded.",
                locator=EvidenceLocator(source_ref="data/ACME.html"),
                recorded_at=recorded_at,
            )
        )
        conn.commit()
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 1
        assert result.substantive_nodes_created == 9
        assert result.reference_nodes_created == 0
        assert result.substantive_node_kind_counts == {
            "passage": 1,
            "section": 1,
            "table": 1,
            "table_cell": 4,
            "table_row": 2,
        }
        assert result.largest_document_node_count == 9
        rows = conn.execute(
            "SELECT node_kind, text, locator_json, parent_node_id, node_id "
            "FROM evidence_nodes WHERE extraction_run_id LIKE 'fulltext-run-%' "
            "AND node_kind <> 'document' ORDER BY rowid"
        ).fetchall()
        assert [row["node_kind"] for row in rows] == [
            "section",
            "passage",
            "table",
            "table_row",
            "table_cell",
            "table_cell",
            "table_row",
            "table_cell",
            "table_cell",
        ]
        assert [row["text"] for row in rows if row["node_kind"] in {"section", "passage"}] == [
            "Results",
            "Margin expanded.",
        ]
        assert all("ignore" not in str(row["text"]) for row in rows)
        assert rows[2]["text"] == ("HTML table structure; child rows carry reported content.")
        assert rows[3]["text"] == "Metric \u241f Q2"
        assert rows[6]["text"] == "Revenue \u241f 120"
        decoded = content.decode("utf-8")
        for row in rows:
            locator = json.loads(row["locator_json"])
            assert locator["filing_section_key_raw"].startswith("/")
            source_span = decoded[locator["char_start"] : locator["char_end"]]
            if row["node_kind"] in {"section", "passage", "table_cell"}:
                assert source_span == row["text"]
            elif row["node_kind"] == "table":
                assert source_span.startswith("<table>")
                assert source_span.endswith("</table>")
            else:
                assert source_span.startswith("<tr>")
                assert source_span.endswith("</tr>")
        section_id = rows[0]["node_id"]
        assert rows[1]["parent_node_id"] == section_id
        table_id = rows[2]["node_id"]
        assert rows[3]["parent_node_id"] == table_id
        assert rows[4]["parent_node_id"] == rows[3]["node_id"]
        first_cell_locator = json.loads(rows[4]["locator_json"])
        assert first_cell_locator["table_row_index"] == 1
        assert first_cell_locator["table_column_index"] == 1
        run = conn.execute(
            "SELECT extractor_code_version, idempotency_key "
            "FROM evidence_extraction_runs WHERE extraction_run_id LIKE 'fulltext-run-%'"
        ).fetchone()
        assert run[0] == "fulltext-evidence-backfill@4-replica-aware-web-archive"
        assert str(run[1]).startswith("fulltext-structured-web-archive-v4:")
        replacement = conn.execute(
            "SELECT text, revision, supersedes_node_id FROM v_evidence_current "
            "WHERE evidence_key = ?",
            (f"fulltext-content:{stable_token}:1",),
        ).fetchone()
        assert tuple(replacement) == ("Results", 2, old_node_id)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_evidence_current WHERE text = ?",
                ("Results Margin expanded.",),
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_html_row_aggregation_preserves_nested_cell_text_once_with_exact_row_span(
    tmp_path: Path,
) -> None:
    content = (
        b"<html><body><table><tr><th>Metric</th><th>Value</th></tr>"
        b"<tr class='data'><td>Revenue <strong>growth</strong></td>"
        b"<td><span>120</span></td></tr><tr></tr></table></body></html>"
    )
    conn, repo_root = _connection(tmp_path, suffix=".html", content=content)
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 1
        assert result.reference_nodes_planned == 0
        assert result.reference_nodes_created == 0
        rows = conn.execute(
            "SELECT text, locator_json FROM evidence_nodes "
            "WHERE extraction_run_id LIKE 'fulltext-run-%' "
            "AND node_kind = 'table_row' ORDER BY rowid"
        ).fetchall()
        assert [row["text"] for row in rows] == [
            "Metric \u241f Value",
            "Revenue growth \u241f 120",
        ]
        assert rows[1]["text"].count("Revenue") == 1
        assert rows[1]["text"].count("growth") == 1
        assert rows[1]["text"].count("120") == 1
        locator = json.loads(rows[1]["locator_json"])
        decoded = content.decode()
        source_span = decoded[locator["char_start"] : locator["char_end"]]
        assert source_span == (
            "<tr class='data'><td>Revenue <strong>growth</strong></td>"
            "<td><span>120</span></td></tr>"
        )
        cell_texts = [
            str(row[0])
            for row in conn.execute(
                "SELECT text FROM evidence_nodes "
                "WHERE extraction_run_id LIKE 'fulltext-run-%' "
                "AND node_kind = 'table_cell' ORDER BY rowid"
            ).fetchall()
        ]
        assert cell_texts == ["Metric", "Value", "Revenue ", "growth", "120"]
    finally:
        conn.close()


def test_html_long_dom_path_uses_bounded_full_path_commitment(tmp_path: Path) -> None:
    depth = 80
    content = (
        "<html><body>"
        + ("<div>" * depth)
        + "<p>Deep reported fact.</p>"
        + ("</div>" * depth)
        + "</body></html>"
    ).encode()
    conn, repo_root = _connection(tmp_path, suffix=".html", content=content)
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 1
        row = conn.execute(
            "SELECT text, locator_json FROM evidence_nodes "
            "WHERE extraction_run_id LIKE 'fulltext-run-%' AND node_kind = 'passage'"
        ).fetchone()
        locator = json.loads(row["locator_json"])
        full_path = "/html[1]/body[1]" + ("/div[1]" * depth) + "/p[1]"
        digest = hashlib.sha256(full_path.encode()).hexdigest()
        bounded_path = locator["filing_section_key_raw"]
        assert len(bounded_path) == 512
        assert bounded_path.startswith(f"dom-path-sha256:{digest}:suffix:")
        assert full_path.endswith(bounded_path.partition(":suffix:")[2])
        decoded = content.decode()
        assert decoded[locator["char_start"] : locator["char_end"]] == row["text"]
    finally:
        conn.close()


def test_html_node_explosion_is_loudly_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"<html><body><p>One</p><p>Two</p><p>Three</p><p>Four</p></body></html>"
    conn, repo_root = _connection(tmp_path, suffix=".html", content=content)
    monkeypatch.setattr(fulltext_backfill_module, "_MAX_HTML_NODES", 3)
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 0
        assert result.documents_quarantined == 1
        assert result.finding_counts == {"html_node_count_limit_exceeded": 1}
        assert result.substantive_nodes_planned == 0
        assert (
            conn.execute(
                "SELECT outcome FROM evidence_extraction_runs "
                "WHERE extraction_run_id LIKE 'fulltext-run-%'"
            ).fetchone()[0]
            == "failed"
        )
    finally:
        conn.close()


def test_zip_extracts_supported_members_in_canonical_path_order(tmp_path: Path) -> None:
    content = _zip_bytes(
        [
            ("z-notes.csv", "metric,value\nRevenue,120\n"),
            ("reports/q2.html", "<h1>Q2</h1><p>Margin expanded.</p>"),
            ("facts.xml", "<facts><revenue>120</revenue></facts>"),
        ]
    )
    conn, repo_root = _connection(tmp_path, suffix=".zip", content=content)
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 1
        nodes = conn.execute(
            "SELECT node_kind, text, locator_json FROM evidence_nodes "
            "WHERE extraction_run_id LIKE 'fulltext-run-%' AND node_kind <> 'document' "
            "ORDER BY rowid"
        ).fetchall()
        refs = [json.loads(row["locator_json"])["source_ref"] for row in nodes]
        assert refs == sorted(refs)
        assert refs[0].endswith("!/facts.xml")
        assert any(ref.endswith("!/reports/q2.html") for ref in refs)
        assert refs[-1].endswith("!/z-notes.csv")
        assert any(row["node_kind"] == "section" and row["text"] == "Q2" for row in nodes)
        assert any(row["text"] == "metric,value\nRevenue,120\n" for row in nodes)
    finally:
        conn.close()


def test_zip_accepts_exact_same_package_binary_replica_and_replays_stably(
    tmp_path: Path,
) -> None:
    image = b"\x89PNG\r\n\x1a\nexact-image-bytes"
    content = _zip_bytes(
        [
            ("facts.csv", "metric,value\nRevenue,120\n"),
            ("images/logo.png", image),
        ]
    )
    conn, repo_root = _connection(
        tmp_path,
        suffix=".zip",
        content=content,
        accession_number="0000000000-26-000001",
        coverage_schema=True,
    )
    replica_document_version_id = _seed_archive_replica_lineage(conn, repo_root, image)
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 1
        assert result.substantive_nodes_created == 1
        assert result.reference_nodes_created == 1
        reference = conn.execute(
            "SELECT text, locator_json, parent_node_id FROM evidence_nodes "
            "WHERE extraction_run_id LIKE 'fulltext-run-%' "
            "AND node_kind = 'document' AND parent_node_id IS NOT NULL"
        ).fetchone()
        payload = json.loads(reference["text"])
        assert payload == {
            "archive_member": "images/logo.png",
            "member_sha256": hashlib.sha256(image).hexdigest(),
            "record_kind": "archive_binary_replica_reference",
            "replica_document_version_id": replica_document_version_id,
        }
        locator = json.loads(reference["locator_json"])
        assert locator["source_ref"].endswith("!/images/logo.png")
        assert locator["filing_section_key_raw"] == (
            "archive-member-sha256:" + hashlib.sha256(image).hexdigest()
        )
        node_count = conn.execute(
            "SELECT COUNT(*) FROM evidence_nodes WHERE extraction_run_id LIKE 'fulltext-run-%'"
        ).fetchone()[0]

        replay = backfill_fulltext_evidence(
            conn,
            _request(repo_root, apply=True, task_id="replica-replay"),
        )
        assert replay.documents_skipped_covered == 1
        assert replay.records_created == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_nodes WHERE extraction_run_id LIKE 'fulltext-run-%'"
            ).fetchone()[0]
            == node_count
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("mismatch", "reason"),
    [
        ("issuer", "archive_binary_replica_wrong_issuer"),
        ("accession", "archive_binary_replica_wrong_accession"),
        ("snapshot", "archive_binary_replica_wrong_snapshot"),
        ("missing", "archive_binary_replica_missing"),
    ],
)
def test_zip_rejects_binary_replica_outside_exact_sealed_package(
    tmp_path: Path,
    mismatch: str,
    reason: str,
) -> None:
    image = b"\x89PNG\r\n\x1a\nreplica-mismatch"
    content = _zip_bytes(
        [("facts.xml", "<facts><revenue>120</revenue></facts>"), ("logo.png", image)]
    )
    conn, repo_root = _connection(
        tmp_path,
        suffix=".zip",
        content=content,
        accession_number="0000000000-26-000001",
        coverage_schema=True,
    )
    _seed_archive_replica_lineage(conn, repo_root, image, mismatch=mismatch)
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 0
        assert result.documents_quarantined == 1
        assert result.finding_counts == {reason: 1}
        assert result.reference_nodes_created == 0
        assert (
            conn.execute(
                "SELECT outcome FROM evidence_extraction_runs "
                "WHERE extractor_code_version = "
                "'fulltext-evidence-backfill@4-replica-aware-web-archive'"
            ).fetchone()[0]
            == "failed"
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("parts", "reason"),
    [
        ([("../escape.txt", "not safe")], "archive_unsafe_member_path"),
        (
            [("image.jpg", b"\xff\xd8\xff")],
            "archive_binary_replica_package_unresolved",
        ),
        (
            [("e\u0301.txt", "one"), ("\u00e9.txt", "two")],
            "archive_duplicate_member_path",
        ),
    ],
)
def test_zip_quarantines_unsafe_unsupported_or_conflicting_members(
    tmp_path: Path,
    parts: list[tuple[str, bytes | str]],
    reason: str,
) -> None:
    conn, repo_root = _connection(tmp_path, suffix=".zip", content=_zip_bytes(parts))
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 0
        assert result.documents_quarantined == 1
        assert result.finding_counts == {reason: 1}
        run = conn.execute(
            "SELECT outcome, extractor_code_version FROM evidence_extraction_runs "
            "WHERE extraction_run_id LIKE 'fulltext-run-%'"
        ).fetchone()
        assert run["outcome"] == "failed"
        assert run["extractor_code_version"] == (
            "fulltext-evidence-backfill@4-replica-aware-web-archive"
        )
    finally:
        conn.close()


def test_zip_compression_bomb_is_quarantined_before_member_read(tmp_path: Path) -> None:
    content = _zip_bytes([("bomb.txt", b"A" * (4 * 1024 * 1024))])
    conn, repo_root = _connection(tmp_path, suffix=".zip", content=content)
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 0
        assert result.finding_counts == {"archive_compression_ratio_exceeded": 1}
    finally:
        conn.close()


def test_pdf_emits_one_page_anchored_node_per_substantive_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pypdf

    class _Page:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _Reader:
        def __init__(self) -> None:
            self.is_encrypted = False
            self.pages = [_Page("First page."), _Page(""), _Page("Third page.")]

    def _reader(_: object) -> _Reader:
        return _Reader()

    monkeypatch.setattr(pypdf, "PdfReader", _reader)
    conn, repo_root = _connection(tmp_path, suffix=".pdf", content=b"not-a-real-pdf")
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.substantive_nodes_created == 2
        nodes = conn.execute(
            "SELECT text, locator_json FROM evidence_nodes WHERE node_kind = 'pdf_page' "
            "ORDER BY locator_json"
        ).fetchall()
        assert [(row[0], row[1]) for row in nodes] == [
            ("First page.", '{"page_number":1,"source_ref":"data/ACME.pdf"}'),
            ("Third page.", '{"page_number":3,"source_ref":"data/ACME.pdf"}'),
        ]
    finally:
        conn.close()


def test_pptx_preserves_presentation_order_and_slide_locators(tmp_path: Path) -> None:
    conn, repo_root = _connection(tmp_path, suffix=".pptx", content=_pptx_bytes())
    try:
        first = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert first.documents_extracted == 1
        assert first.substantive_nodes_created == 6
        nodes = conn.execute(
            "SELECT node.text, node.locator_json FROM evidence_nodes AS node "
            "JOIN evidence_extraction_runs AS run "
            "ON run.extraction_run_id = node.extraction_run_id "
            "WHERE run.extractor_code_version = "
            "'fulltext-evidence-backfill@3-office-native-inventories' "
            "AND node.node_kind = 'passage' "
            "AND json_extract(node.locator_json,'$.office_object_kind') IS NULL "
            "ORDER BY node.rowid"
        ).fetchall()
        assert [(row[0], row[1]) for row in nodes] == [
            (
                "Q2 Revenue +20%",
                '{"slide_number":1,"source_ref":"data/ACME.pptx"}',
            ),
            ("Appendix", '{"slide_number":2,"source_ref":"data/ACME.pptx"}'),
        ]
        run = conn.execute(
            "SELECT idempotency_key, extractor_name FROM evidence_extraction_runs "
            "WHERE extractor_code_version = "
            "'fulltext-evidence-backfill@3-office-native-inventories'"
        ).fetchone()
        assert run[0].startswith("fulltext-office-v3:")
        assert run[1] == "fulltext-evidence-backfill"
        replay = backfill_fulltext_evidence(
            conn, _request(repo_root, apply=True, task_id="pptx-replay")
        )
        assert replay.documents_skipped_covered == 1
        assert replay.records_created == 0
    finally:
        conn.close()


def test_xlsx_preserves_sheet_row_formula_and_cached_value(tmp_path: Path) -> None:
    conn, repo_root = _connection(tmp_path, suffix=".xlsx", content=_xlsx_bytes())
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_extracted == 1
        assert result.substantive_nodes_created == 4
        nodes = conn.execute(
            "SELECT node.node_kind, node.text, node.locator_json, node.parent_node_id, "
            "node.node_id FROM evidence_nodes AS node JOIN evidence_extraction_runs AS run "
            "ON run.extraction_run_id = node.extraction_run_id "
            "WHERE run.extractor_code_version = "
            "'fulltext-evidence-backfill@3-office-native-inventories' "
            "AND node.node_kind <> 'document' "
            "AND json_extract(node.locator_json,'$.office_object_kind') IS NULL "
            "ORDER BY node.rowid"
        ).fetchall()
        assert nodes[0][0] == "table"
        assert nodes[0][1] == "Worksheet: Income Statement (state=visible)"
        assert nodes[1][0] == "table_row"
        assert nodes[1][1] == ('A1: "Revenue" | B1: raw=100 | C1: formula="=B1*1.2"; cached="120"')
        assert nodes[1][2] == (
            '{"cell_range":"A1:C1","sheet_name":"Income Statement",'
            '"source_ref":"data/ACME.xlsx","table_name":"Income Statement"}'
        )
        assert nodes[1][3] == nodes[0][4]
    finally:
        conn.close()


def test_legacy_binary_office_is_loudly_quarantined(tmp_path: Path) -> None:
    conn, repo_root = _connection(tmp_path, suffix=".xls", content=b"legacy-binary")
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_quarantined == 1
        assert result.finding_counts == {"unsupported_legacy_office_format": 1}
        run = conn.execute(
            "SELECT outcome, extractor_code_version, idempotency_key "
            "FROM evidence_extraction_runs WHERE extraction_run_id LIKE 'fulltext-run-%'"
        ).fetchone()
        assert run[0] == "failed"
        assert run[1] == "fulltext-evidence-backfill@3-office-native-inventories"
        assert run[2].startswith("fulltext-office-v3:")
    finally:
        conn.close()


def test_unsupported_bytes_are_recorded_as_loud_failed_coverage(tmp_path: Path) -> None:
    conn, repo_root = _connection(tmp_path, suffix=".bin", content=b"\x00\x01")
    try:
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_quarantined == 1
        assert result.finding_counts == {"unsupported_format": 1}
        assert conn.execute("SELECT COUNT(*) FROM evidence_nodes").fetchone()[0] == 1
        outcome = conn.execute(
            "SELECT outcome FROM evidence_extraction_runs WHERE extraction_run_id LIKE 'fulltext-run-%'"
        ).fetchone()[0]
        assert outcome == "failed"
    finally:
        conn.close()


def test_hash_mismatch_is_quarantined_before_parse(tmp_path: Path) -> None:
    conn, repo_root = _connection(tmp_path)
    try:
        (repo_root / "data" / "ACME.txt").write_text("tampered", encoding="utf-8")
        result = backfill_fulltext_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_quarantined == 1
        assert result.finding_counts == {"sha256_mismatch": 1}
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_extraction_runs "
                "WHERE extraction_run_id LIKE 'fulltext-run-%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_evidence_native_lane_extracts_extensionless_content_without_legacy_row(
    tmp_path: Path,
) -> None:
    conn, repo_root = _connection(tmp_path)
    body = b"<html><body><h1>Investor day</h1><p>Target margin: 30%.</p></body></html>"
    digest = hashlib.sha256(body).hexdigest()
    blob_path = repo_root / ".tmp" / "evidence-blobs" / digest[:2] / digest
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(body)
    recorded_at = datetime(2026, 7, 25, 12, 0, 0)
    ledger = EvidenceLedger(conn)
    try:
        ledger.persist(
            ContentBlob(
                sha256=digest,
                byte_size=len(body),
                media_type="text/html",
                storage_uri=blob_path.as_uri(),
                recorded_at=recorded_at,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="native-observation",
                idempotency_key="native-observation",
                source_kind="sec_filing_document",
                source_url="https://issuer.test/download?id=investor-day",
                blob_sha256=digest,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=recorded_at,
                retrieved_at=recorded_at,
                retrieval_config_sha256="a" * 64,
                collector_code_version="test@1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id="native-version",
                document_key="issuer:native-investor-day",
                version_sequence=1,
                observation_id="native-observation",
                blob_sha256=digest,
                issuer_id="issuer",
                ticker="ACME",
                document_type="investor_presentation",
                form_type="IR",
                language="en",
                legacy_document_id=None,
                recorded_at=recorded_at,
            )
        )
        conn.commit()

        result = backfill_fulltext_evidence(
            conn,
            FullTextBackfillRequest(
                repo_root=repo_root,
                apply=True,
                task_id="native-fulltext",
                source_lane="evidence_native",
            ),
        )
        assert result.source_lane == "evidence_native"
        assert result.documents_extracted == 1
        assert result.last_evidence_rowid_after > 0
        assert result.last_document_version_id_after == "native-version"
        passage = conn.execute(
            "SELECT text, locator_json FROM evidence_nodes "
            "WHERE extraction_run_id LIKE 'fulltext-run-%' AND node_kind = 'passage' "
            "AND text LIKE '%Target margin%'"
        ).fetchone()
        assert passage is not None
        assert passage["text"] == "Target margin: 30%."
        heading = conn.execute(
            "SELECT text FROM evidence_nodes "
            "WHERE extraction_run_id LIKE 'fulltext-run-%' AND node_kind = 'section'"
        ).fetchone()
        assert heading["text"] == "Investor day"
        locator = json.loads(passage["locator_json"])
        assert locator["source_ref"] == "https://issuer.test/download?id=investor-day"
        assert locator["filing_section_key_raw"] == "/html[1]/body[1]/p[1]"
        checkpoint = repo_root / ".tmp" / "native-fulltext" / "evidence-native-state.json"
        assert checkpoint.exists()
        assert '"source_lane":"evidence_native"' in checkpoint.read_text(encoding="utf-8")
    finally:
        conn.close()
