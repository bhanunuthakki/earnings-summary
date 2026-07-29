"""Contracts for deterministic, sealed evidence-corpus construction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import ValidationError

import search.corpus_builder as corpus_builder_module
from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.fulltext_extractor_identity import (
    BASE_FULLTEXT_EXTRACTOR,
    STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR,
)
from provenance.semantic_disposition import (
    SemanticDisposition,
    SemanticDispositionStore,
)
from search.corpus_builder import (
    ChunkerConfig,
    CorpusBuildRequest,
    ExpectedDocument,
    build_grounded_search_corpus,
    lexical_index_config_sha256,
)
from search.grounded import HybridRetriever

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 2, 0, 0)
A, B, C = "a" * 64, "b" * 64, "c" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "corpus.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0213_evidence_ledger_foundation")
    command.stamp(config, "0215_observation_resolution_ledger")
    command.upgrade(config, "0216_search_corpus_foundation")
    command.stamp(config, "0221_ask_retrieval_traces")
    command.upgrade(config, "0222_ocr_extraction_governance")
    command.stamp(config, "0231_legacy_document_evidence_bindings")
    command.upgrade(config, "0232_document_semantic_dispositions")
    command.upgrade(config, "0233_search_projection_seals")
    command.upgrade(config, "0234_image_ocr_governance")
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE search_projection_seals ADD COLUMN runtime_artifact_sha256 TEXT")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed(
    conn: sqlite3.Connection,
    *,
    text: str = "Revenue grew strongly year over year.",
    media_type: str = "text/plain",
    source_url: str = "https://sec.test/acme",
) -> None:
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=A,
            byte_size=22,
            media_type=media_type,
            storage_uri="file:///acme",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="obs",
            idempotency_key="obs",
            source_kind="sec",
            source_url=source_url,
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
            text=text,
            recorded_at=STAMP,
        )
    )
    conn.commit()


def _seed_structured_hierarchy(conn: sqlite3.Connection) -> None:
    _seed(
        conn,
        text="legacy parser text",
        media_type="text/html",
        source_url="https://sec.test/acme.html",
    )
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ExtractionRun(
            extraction_run_id="structured-run",
            idempotency_key="structured-run",
            document_version_id="doc",
            input_sha256=A,
            extractor_name="fulltext-evidence-backfill",
            extractor_config_sha256=(STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.config_sha256),
            extractor_code_version=(STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.code_version),
            output_sha256=C,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    nodes = (
        EvidenceNode(
            node_id="structured-heading",
            evidence_key="structured:heading",
            revision=1,
            extraction_run_id="structured-run",
            node_kind="section",
            text="Financial highlights",
            locator=EvidenceLocator(
                source_ref="https://sec.test/acme",
                filing_section_key_raw="/html[1]/body[1]/h2[1]",
                char_start=0,
                char_end=20,
            ),
            recorded_at=STAMP,
        ),
        EvidenceNode(
            node_id="structured-passage",
            evidence_key="structured:passage",
            revision=1,
            extraction_run_id="structured-run",
            parent_node_id="structured-heading",
            node_kind="passage",
            text="Demand remained durable.",
            recorded_at=STAMP,
        ),
        EvidenceNode(
            node_id="structured-table",
            evidence_key="structured:table",
            revision=1,
            extraction_run_id="structured-run",
            node_kind="table",
            text='<table class="financials">',
            recorded_at=STAMP,
        ),
        EvidenceNode(
            node_id="raw-row",
            evidence_key="structured:row:1",
            revision=1,
            extraction_run_id="structured-run",
            parent_node_id="structured-table",
            node_kind="table_row",
            text="<tr>",
            locator=EvidenceLocator(
                source_ref="https://sec.test/acme",
                table_name="financials",
                table_row_index=1,
            ),
            recorded_at=STAMP,
        ),
        EvidenceNode(
            node_id="raw-row-label",
            evidence_key="structured:row:1:cell:1",
            revision=1,
            extraction_run_id="structured-run",
            parent_node_id="raw-row",
            node_kind="table_cell",
            text="Revenue",
            locator=EvidenceLocator(
                source_ref="https://sec.test/acme",
                table_name="financials",
                table_row_index=1,
                table_column_index=1,
            ),
            recorded_at=STAMP,
        ),
        EvidenceNode(
            node_id="raw-row-value",
            evidence_key="structured:row:1:cell:2",
            revision=1,
            extraction_run_id="structured-run",
            parent_node_id="raw-row",
            node_kind="table_cell",
            text="$120 million",
            locator=EvidenceLocator(
                source_ref="https://sec.test/acme",
                table_name="financials",
                table_row_index=1,
                table_column_index=2,
            ),
            recorded_at=STAMP,
        ),
        EvidenceNode(
            node_id="context-row",
            evidence_key="structured:row:2",
            revision=1,
            extraction_run_id="structured-run",
            parent_node_id="structured-table",
            node_kind="table_row",
            text="Gross margin | 72%",
            locator=EvidenceLocator(
                source_ref="https://sec.test/acme",
                table_name="financials",
                table_row_index=2,
            ),
            recorded_at=STAMP,
        ),
        EvidenceNode(
            node_id="context-row-label",
            evidence_key="structured:row:2:cell:1",
            revision=1,
            extraction_run_id="structured-run",
            parent_node_id="context-row",
            node_kind="table_cell",
            text="Gross margin",
            recorded_at=STAMP,
        ),
        EvidenceNode(
            node_id="context-row-value",
            evidence_key="structured:row:2:cell:2",
            revision=1,
            extraction_run_id="structured-run",
            parent_node_id="context-row",
            node_kind="table_cell",
            text="72%",
            recorded_at=STAMP,
        ),
    )
    for node in nodes:
        ledger.persist(node)
    conn.commit()


def _seed_pdf_with_governed_ocr(conn: sqlite3.Connection) -> None:
    blob_sha = "d" * 64
    ocr_config_sha = "e" * 64
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob_sha,
            byte_size=100,
            media_type="application/pdf",
            storage_uri="file:///acme.pdf",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="pdf-observation",
            idempotency_key="pdf-observation",
            source_kind="sec",
            source_url="https://sec.test/acme.pdf",
            blob_sha256=blob_sha,
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
            document_version_id="pdf-document",
            document_key="ACME:annual-report",
            version_sequence=1,
            observation_id="pdf-observation",
            blob_sha256=blob_sha,
            issuer_id="issuer",
            ticker="ACME",
            document_type="annual_report",
            form_type="10-K",
            language="en",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id="native-pdf-run",
            idempotency_key="native-pdf-run",
            document_version_id="pdf-document",
            input_sha256=blob_sha,
            extractor_name="fulltext-evidence-backfill",
            extractor_config_sha256=BASE_FULLTEXT_EXTRACTOR.config_sha256,
            extractor_code_version=BASE_FULLTEXT_EXTRACTOR.code_version,
            output_sha256=C,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    native_page_1 = EvidenceNode(
        node_id="native-page-1",
        evidence_key="pdf:native:page:1",
        revision=1,
        extraction_run_id="native-pdf-run",
        node_kind="pdf_page",
        text="Native page one reliable",
        locator=EvidenceLocator(
            source_ref="https://sec.test/acme.pdf",
            page_number=1,
        ),
        recorded_at=STAMP,
    )
    native_page_2 = EvidenceNode(
        node_id="native-page-2",
        evidence_key="pdf:native:page:2",
        revision=1,
        extraction_run_id="native-pdf-run",
        node_kind="pdf_page",
        text="Native page two rejected",
        locator=EvidenceLocator(
            source_ref="https://sec.test/acme.pdf",
            page_number=2,
        ),
        recorded_at=STAMP,
    )
    ledger.persist(native_page_1)
    ledger.persist(native_page_2)
    ledger.persist(
        ExtractionRun(
            extraction_run_id="ocr-pdf-run",
            idempotency_key="ocr-pdf-run",
            document_version_id="pdf-document",
            input_sha256=blob_sha,
            extractor_name="governed-pdf-ocr",
            extractor_config_sha256=ocr_config_sha,
            extractor_code_version="governed-pdf-ocr@1",
            output_sha256=A,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    ocr_page_2_locator = EvidenceLocator(
        source_ref="https://sec.test/acme.pdf",
        page_number=2,
    )
    ocr_page_2 = EvidenceNode(
        node_id="ocr-page-2",
        evidence_key="pdf:ocr:page:2",
        revision=1,
        extraction_run_id="ocr-pdf-run",
        node_kind="pdf_page",
        text="OCR page two definitive",
        locator=ocr_page_2_locator,
        recorded_at=STAMP,
    )
    ledger.persist(ocr_page_2)
    conn.execute(
        "INSERT INTO ocr_document_assessments "
        "(assessment_id,idempotency_key,document_version_id,input_sha256,"
        "detector_name,detector_config_sha256,detector_code_version,"
        "native_output_sha256,page_count,outcome,reason_code,assessed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "pdf-assessment",
            "pdf-assessment",
            "pdf-document",
            blob_sha,
            "native-preflight",
            C,
            "native-preflight@1",
            A,
            2,
            "ocr_required",
            None,
            STAMP,
        ),
    )
    conn.executemany(
        "INSERT INTO ocr_preflight_pages "
        "(assessment_id,page_number,native_character_count,native_text_sha256,"
        "requires_ocr) VALUES (?,?,?,?,?)",
        (
            ("pdf-assessment", 1, 24, A, False),
            ("pdf-assessment", 2, 5, B, True),
        ),
    )
    conn.execute(
        "INSERT INTO ocr_extraction_governance "
        "(extraction_run_id,assessment_id,engine_name,engine_version,"
        "engine_binary_sha256,model_name,model_version,model_manifest_sha256,"
        "model_artifacts_json,languages_json,engine_config_json,"
        "extractor_config_sha256,renderer_name,renderer_version,"
        "renderer_binary_sha256,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "ocr-pdf-run",
            "pdf-assessment",
            "tesseract",
            "5",
            A,
            "eng",
            "1",
            B,
            '{"eng":"' + B + '"}',
            '["eng"]',
            "{}",
            ocr_config_sha,
            "pdftoppm",
            "1",
            C,
            STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO ocr_page_results "
        "(extraction_run_id,page_number,node_id,outcome,output_sha256,"
        "mean_confidence,locator_json,locator_sha256,reason_code,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "ocr-pdf-run",
            2,
            ocr_page_2.node_id,
            "accepted",
            hashlib.sha256(ocr_page_2.text.encode("utf-8")).hexdigest(),
            99.0,
            ocr_page_2_locator.canonical_json,
            ocr_page_2_locator.canonical_sha256,
            None,
            STAMP,
        ),
    )
    conn.commit()


def _request(
    *, apply: bool, inventory: tuple[ExpectedDocument, ...] | None = None
) -> CorpusBuildRequest:
    return CorpusBuildRequest(
        corpus_key="issuer:ACME:reporting",
        revision=1,
        selector_code_version="corpus-builder@1",
        recorded_at=STAMP,
        apply=apply,
        expected_documents=inventory
        or (
            ExpectedDocument(
                expected_document_key="ACME:2026Q1:10-Q",
                document_version_id="doc",
                membership_status="included",
                reason="verified source evidence",
            ),
        ),
        chunker=ChunkerConfig(max_characters=18, max_tokens=4),
        required_extractor_names=("parser",),
    )


def _structured_request(*, apply: bool = False) -> CorpusBuildRequest:
    return CorpusBuildRequest(
        corpus_key="issuer:ACME:structured-reporting",
        revision=1,
        selector_code_version="corpus-builder@structured-test",
        recorded_at=STAMP,
        apply=apply,
        expected_documents=(
            ExpectedDocument(
                expected_document_key="ACME:structured:10-Q",
                document_version_id="doc",
                membership_status="included",
                reason="verified structured evidence",
            ),
        ),
        chunker=ChunkerConfig(max_characters=1_000, max_tokens=100),
        required_extractor_names=("fulltext-evidence-backfill",),
    )


def test_dry_run_is_read_only_and_reports_exact_chunk_offsets(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed(conn)
        result = build_grounded_search_corpus(conn, _request(apply=False))

        assert result.mode == "dry_run"
        assert result.chunks_planned >= 2
        assert conn.execute("SELECT COUNT(*) FROM search_corpus_manifests").fetchone()[0] == 0
        assert all(
            chunk.char_end - chunk.char_start == len(chunk.text) for chunk in result.planned_chunks
        )
        assert (
            "".join(chunk.text for chunk in result.planned_chunks)
            == "Revenue grew strongly year over year."
        )
    finally:
        conn.close()


def test_v4_structured_nodes_choose_one_contextual_hierarchy_level(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_structured_hierarchy(conn)
        result = build_grounded_search_corpus(conn, _structured_request())

        chunks_by_node = {chunk.evidence_node_id: chunk.text for chunk in result.planned_chunks}
        assert chunks_by_node == {
            "structured-heading": "Financial highlights",
            "structured-passage": "Demand remained durable.",
            "raw-row-label": "Revenue",
            "raw-row-value": "$120 million",
            "context-row": "Gross margin | 72%",
        }
        assert all(not text.lstrip().startswith("<") for text in chunks_by_node.values())
        assert (
            not {
                "structured-table",
                "raw-row",
                "context-row-label",
                "context-row-value",
            }
            & chunks_by_node.keys()
        )

        locators = {
            str(row[0]): json.loads(str(row[1]))
            for row in conn.execute(
                "SELECT node_id, locator_json FROM evidence_nodes "
                "WHERE node_id IN ('raw-row-label', 'context-row')"
            ).fetchall()
        }
        assert locators["raw-row-label"]["table_row_index"] == 1
        assert locators["context-row"]["table_row_index"] == 2
    finally:
        conn.close()


def test_non_pdf_uses_only_exact_promoted_fulltext_extractor_identity(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_structured_hierarchy(conn)
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ExtractionRun(
                extraction_run_id="legacy-fulltext-run",
                idempotency_key="legacy-fulltext-run",
                document_version_id="doc",
                input_sha256=A,
                extractor_name="fulltext-evidence-backfill",
                extractor_config_sha256=B,
                extractor_code_version="fulltext-evidence-backfill@1",
                output_sha256=C,
                started_at=STAMP,
                completed_at=STAMP,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id="legacy-fulltext-passage",
                evidence_key="legacy-fulltext:passage",
                revision=1,
                extraction_run_id="legacy-fulltext-run",
                node_kind="passage",
                text="Duplicated whole-document text.",
                recorded_at=STAMP,
            )
        )
        conn.commit()

        result = build_grounded_search_corpus(conn, _structured_request())

        assert "legacy-fulltext-passage" not in {
            chunk.evidence_node_id for chunk in result.planned_chunks
        }
    finally:
        conn.close()


def test_non_pdf_does_not_auto_promote_unknown_future_extractor_generation(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_structured_hierarchy(conn)
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ExtractionRun(
                extraction_run_id="unapproved-future-run",
                idempotency_key="unapproved-future-run",
                document_version_id="doc",
                input_sha256=A,
                extractor_name="fulltext-evidence-backfill",
                extractor_config_sha256=B,
                extractor_code_version="fulltext-evidence-backfill@5-unapproved",
                output_sha256=C,
                started_at=STAMP,
                completed_at=STAMP,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id="unapproved-future-passage",
                evidence_key="unapproved-future:passage",
                revision=1,
                extraction_run_id="unapproved-future-run",
                node_kind="passage",
                text="This generation has not been promoted.",
                recorded_at=STAMP,
            )
        )
        conn.commit()

        result = build_grounded_search_corpus(conn, _structured_request())

        node_ids = {chunk.evidence_node_id for chunk in result.planned_chunks}
        assert "structured-passage" in node_ids
        assert "unapproved-future-passage" not in node_ids
    finally:
        conn.close()


def test_ungoverned_image_ocr_run_cannot_complete_corpus_membership(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed(
            conn,
            media_type="image/jpeg",
            source_url="https://sec.test/acme-chart.jpg",
        )
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ExtractionRun(
                extraction_run_id="ungoverned-image-ocr-run",
                idempotency_key="ungoverned-image-ocr-run",
                document_version_id="doc",
                input_sha256=A,
                extractor_name="governed-image-ocr",
                extractor_config_sha256=B,
                extractor_code_version="governed-image-ocr@1",
                output_sha256=C,
                started_at=STAMP,
                completed_at=STAMP,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id="ungoverned-image-ocr-passage",
                evidence_key="ungoverned-image-ocr:passage",
                revision=1,
                extraction_run_id="ungoverned-image-ocr-run",
                node_kind="passage",
                text="A plausible but ungoverned OCR result.",
                recorded_at=STAMP,
            )
        )
        conn.commit()
        request = _request(
            apply=False,
            inventory=(
                ExpectedDocument(
                    expected_document_key="ACME:image",
                    document_version_id="doc",
                    membership_status="included",
                    reason="coverage:extracted",
                ),
            ),
        ).model_copy(update={"required_extractor_names": ("governed-image-ocr",)})

        result = build_grounded_search_corpus(conn, request)

        assert result.completion_status == "incomplete"
        assert result.chunks_planned == 0
    finally:
        conn.close()


def test_current_governed_image_ocr_result_is_the_only_image_projection(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed(
            conn,
            media_type="image/jpeg",
            source_url="https://sec.test/acme-chart.jpg",
        )
        ledger = EvidenceLedger(conn)
        conn.execute(
            "INSERT INTO image_ocr_assessments "
            "(assessment_id,idempotency_key,document_version_id,input_sha256,"
            "observed_sha256,observed_byte_size,media_type,width,height,pixel_count,"
            "page_count,detector_name,detector_config_sha256,detector_code_version,"
            "outcome,reason_code,assessed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "image-assessment",
                "image-assessment",
                "doc",
                A,
                A,
                22,
                "image/jpeg",
                2,
                2,
                4,
                1,
                "image-header-preflight",
                B,
                "image-header-preflight@1",
                "ocr_required",
                None,
                STAMP,
            ),
        )
        ledger.persist(
            ExtractionRun(
                extraction_run_id="governed-image-ocr-run",
                idempotency_key="governed-image-ocr-run",
                document_version_id="doc",
                input_sha256=A,
                extractor_name="governed-image-ocr",
                extractor_config_sha256=B,
                extractor_code_version="governed-image-ocr@1",
                output_sha256=C,
                started_at=STAMP,
                completed_at=STAMP,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id="governed-image-ocr-passage",
                evidence_key="governed-image-ocr:passage",
                revision=1,
                extraction_run_id="governed-image-ocr-run",
                node_kind="passage",
                text="Revenue mix expanded to 42 percent.",
                recorded_at=STAMP,
            )
        )
        conn.execute(
            "INSERT INTO image_ocr_extraction_governance "
            "(extraction_run_id,assessment_id,engine_name,engine_version,"
            "engine_binary_sha256,model_name,model_version,model_manifest_sha256,"
            "model_artifacts_json,languages_json,engine_config_json,"
            "extractor_config_sha256,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "governed-image-ocr-run",
                "image-assessment",
                "tesseract",
                "5.5.3",
                A,
                "eng",
                "1",
                B,
                '{"eng":"' + B + '"}',
                '["eng"]',
                "{}",
                B,
                STAMP,
            ),
        )
        conn.execute(
            "INSERT INTO image_ocr_results "
            "(extraction_run_id,page_number,node_id,outcome,output_sha256,"
            "mean_confidence,locator_json,locator_sha256,reason_code,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "governed-image-ocr-run",
                1,
                "governed-image-ocr-passage",
                "accepted",
                C,
                95.0,
                '{"page_number":1}',
                A,
                None,
                STAMP,
            ),
        )
        conn.commit()
        request = _request(
            apply=False,
            inventory=(
                ExpectedDocument(
                    expected_document_key="ACME:image",
                    document_version_id="doc",
                    membership_status="included",
                    reason="coverage:extracted",
                ),
            ),
        ).model_copy(update={"required_extractor_names": ("governed-image-ocr",)})

        result = build_grounded_search_corpus(conn, request)

        assert result.completion_status == "complete"
        assert {chunk.evidence_node_id for chunk in result.planned_chunks} == {
            "governed-image-ocr-passage"
        }
        assert "".join(chunk.text for chunk in result.planned_chunks) == (
            "Revenue mix expanded to 42 percent."
        )
    finally:
        conn.close()


def test_manifest_and_chunk_config_commit_to_node_selection_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_structured_hierarchy(conn)
        baseline = build_grounded_search_corpus(conn, _structured_request())
        monkeypatch.setattr(
            corpus_builder_module,
            "NODE_SELECTION_POLICY_VERSION",
            "canonical-search-node-selection@test-change",
        )
        changed = build_grounded_search_corpus(conn, _structured_request())

        assert changed.manifest_config_sha256 != baseline.manifest_config_sha256
        assert changed.chunker_config_sha256 != baseline.chunker_config_sha256
        assert changed.manifest_id != baseline.manifest_id
        assert {chunk.chunk_id for chunk in changed.planned_chunks}.isdisjoint(
            chunk.chunk_id for chunk in baseline.planned_chunks
        )
    finally:
        conn.close()


def test_apply_seals_manifest_then_records_verified_lexical_memberships(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed(conn)
        request = _request(apply=True)
        first = build_grounded_search_corpus(conn, request)
        second = build_grounded_search_corpus(conn, request)

        assert first.completion_status == "complete"
        assert first.records_created > 0
        assert second.records_created == 0
        assert conn.execute("SELECT COUNT(*) FROM search_corpus_manifest_seals").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM search_lexical_chunks").fetchone()[0]
            == first.chunks_planned
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM search_projection_seals "
                "WHERE index_run_id = ? AND index_kind = 'lexical'",
                (first.lexical_index_run_id,),
            ).fetchone()[0]
            == 1
        )
        # Lexical membership is exactly the sealed manifest's chunk set.  It is
        # deliberately not duplicated once per chunk in the vector-membership
        # table; that would make bounded staging and atomic publication
        # mutually incompatible.
        assert conn.execute("SELECT COUNT(*) FROM search_index_memberships").fetchone()[0] == 0
        run = conn.execute(
            "SELECT config_sha256 FROM search_index_runs WHERE index_run_id = ?",
            (first.lexical_index_run_id,),
        ).fetchone()
        assert run == (
            lexical_index_config_sha256(
                conn,
                manifest_id=first.manifest_id,
            ),
        )
    finally:
        conn.close()


def test_human_nonsemantic_disposition_completes_image_obligation_without_chunks(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        ledger = EvidenceLedger(conn)
        image_sha = "d" * 64
        ledger.persist(
            ContentBlob(
                sha256=image_sha,
                byte_size=10,
                media_type="image/jpeg",
                storage_uri="file:///chart.jpg",
                recorded_at=STAMP,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="image-observation",
                idempotency_key="image-observation",
                source_kind="sec",
                source_url="https://sec.test/chart.jpg",
                blob_sha256=image_sha,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=STAMP,
                retrieved_at=STAMP,
                retrieval_config_sha256=B,
                collector_code_version="collector@1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id="image-document",
                document_key="ACME:chart",
                version_sequence=1,
                observation_id="image-observation",
                blob_sha256=image_sha,
                issuer_id="issuer",
                ticker="ACME",
                document_type="sec_attachment",
                form_type="10-K",
                language="en",
                recorded_at=STAMP,
            )
        )
        SemanticDispositionStore(conn).persist(
            SemanticDisposition(
                assessment_id="semantic-image-v1",
                idempotency_key="semantic-image-v1",
                document_version_id="image-document",
                revision=1,
                semantic_status="not_required",
                reason_code="publisher_logo",
                reason_details=(("review_basis", "visually verified issuer logo"),),
                decision_kind="human",
                reviewer_identity="research-owner",
                policy_name="semantic-image-review",
                policy_version="1",
                policy_config_sha256=B,
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        conn.commit()

        result = build_grounded_search_corpus(
            conn,
            _request(
                apply=True,
                inventory=(
                    ExpectedDocument(
                        expected_document_key="ACME:chart",
                        document_version_id="image-document",
                        membership_status="included",
                        reason="coverage:captured",
                    ),
                ),
            ),
        )

        assert result.completion_status == "complete"
        assert result.chunks_planned == 0
        assert conn.execute(
            "SELECT membership_status, reason FROM search_corpus_document_memberships"
        ).fetchone() == ("included", "semantic:not_required:semantic-image-v1")
    finally:
        conn.close()


def test_nonsemantic_exclusion_cannot_be_automated(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="requires a human decision"):
        SemanticDisposition(
            assessment_id="semantic-image-v1",
            idempotency_key="semantic-image-v1",
            document_version_id="image-document",
            revision=1,
            semantic_status="not_required",
            reason_code="small_image",
            reason_details=(("heuristic", "dimensions"),),
            decision_kind="deterministic",
            policy_name="unsafe-size-rule",
            policy_version="1",
            policy_config_sha256=B,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )


def test_pdf_chunks_select_native_or_accepted_ocr_once_per_page(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_pdf_with_governed_ocr(conn)
        request = _request(
            apply=False,
            inventory=(
                ExpectedDocument(
                    expected_document_key="ACME:annual-report",
                    document_version_id="pdf-document",
                    membership_status="included",
                    reason="coverage:extracted",
                ),
            ),
        ).model_copy(
            update={
                "required_extractor_names": (
                    "fulltext-evidence-backfill",
                    "governed-pdf-ocr",
                )
            }
        )

        result = build_grounded_search_corpus(conn, request)
        text = "".join(chunk.text for chunk in result.planned_chunks)
        node_ids = {chunk.evidence_node_id for chunk in result.planned_chunks}

        assert "Native page one reliable" in text
        assert "OCR page two definitive" in text
        assert "Native page two rejected" not in text
        assert node_ids == {"native-page-1", "ocr-page-2"}
    finally:
        conn.close()


def test_runtime_rejects_stale_human_nonsemantic_disposition(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        ledger = EvidenceLedger(conn)
        image_sha = "d" * 64
        ledger.persist(
            ContentBlob(
                sha256=image_sha,
                byte_size=10,
                media_type="image/jpeg",
                storage_uri="file:///chart.jpg",
                recorded_at=STAMP,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="stale-image-observation",
                idempotency_key="stale-image-observation",
                source_kind="sec",
                source_url="https://sec.test/chart.jpg",
                blob_sha256=image_sha,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=STAMP,
                retrieved_at=STAMP,
                retrieval_config_sha256=B,
                collector_code_version="collector@1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id="stale-image-document",
                document_key="ACME:stale-chart",
                version_sequence=1,
                observation_id="stale-image-observation",
                blob_sha256=image_sha,
                issuer_id="issuer",
                ticker="ACME",
                document_type="sec_attachment",
                form_type="10-K",
                language="en",
                recorded_at=STAMP,
            )
        )
        store = SemanticDispositionStore(conn)
        first = SemanticDisposition(
            assessment_id="semantic-stale-v1",
            idempotency_key="semantic-stale-v1",
            document_version_id="stale-image-document",
            revision=1,
            semantic_status="not_required",
            reason_code="publisher_logo",
            reason_details=(("review_basis", "visually verified issuer logo"),),
            decision_kind="human",
            reviewer_identity="research-owner",
            policy_name="semantic-image-review",
            policy_version="1",
            policy_config_sha256=B,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
        store.persist(first)
        conn.commit()
        result = build_grounded_search_corpus(
            conn,
            _request(
                apply=True,
                inventory=(
                    ExpectedDocument(
                        expected_document_key="ACME:stale-chart",
                        document_version_id="stale-image-document",
                        membership_status="included",
                        reason="coverage:captured",
                    ),
                ),
            ),
        )
        assert HybridRetriever(conn).search("logo", result.manifest_id) == []

        store.persist(
            SemanticDisposition(
                assessment_id="semantic-stale-v2",
                idempotency_key="semantic-stale-v2",
                document_version_id="stale-image-document",
                revision=2,
                semantic_status="required",
                reason_code="semantic_content_found",
                reason_details=(("review_basis", "chart contains reported values"),),
                decision_kind="human",
                reviewer_identity="research-owner",
                policy_name="semantic-image-review",
                policy_version="1",
                policy_config_sha256=B,
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
                supersedes_assessment_id=first.assessment_id,
            )
        )
        conn.commit()

        with pytest.raises(ValueError, match="no longer the current human decision"):
            HybridRetriever(conn).search("logo", result.manifest_id)
    finally:
        conn.close()


@pytest.mark.parametrize("tamper", ["delete", "duplicate", "update"])
def test_runtime_rejects_tampered_lexical_projection(
    tmp_path: Path,
    tamper: str,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed(conn)
        result = build_grounded_search_corpus(conn, _request(apply=True))
        retriever = HybridRetriever(conn)
        assert retriever.search("Revenue", result.manifest_id)
        row = conn.execute(
            "SELECT rowid, chunk_id, text FROM search_lexical_chunks ORDER BY rowid LIMIT 1"
        ).fetchone()
        assert row is not None
        if tamper == "delete":
            conn.execute("DELETE FROM search_lexical_chunks WHERE rowid = ?", (row[0],))
        elif tamper == "duplicate":
            conn.execute(
                "INSERT INTO search_lexical_chunks(chunk_id,text) VALUES (?,?)",
                (row[1], row[2]),
            )
        else:
            conn.execute(
                "UPDATE search_lexical_chunks SET text = ? WHERE rowid = ?",
                ("tampered text", row[0]),
            )
        conn.commit()

        with pytest.raises(
            ValueError,
            match=r"lexical projection|exact sealed-manifest chunk set",
        ):
            retriever.search("Revenue", result.manifest_id)
    finally:
        conn.close()


def test_apply_stages_chunks_in_bounded_transactions_and_publishes_only_at_end(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed(conn, text="one two three four five six seven eight nine ten")
        request = _request(apply=True).model_copy(update={"persist_batch_size": 2})
        statements: list[str] = []
        conn.set_trace_callback(statements.append)

        def interrupt_after_first_batch(completed: int) -> None:
            assert completed == 2
            raise RuntimeError("simulated interruption")

        with pytest.raises(RuntimeError, match="simulated interruption"):
            build_grounded_search_corpus(
                conn,
                request,
                on_chunk_batch_complete=interrupt_after_first_batch,
            )

        assert conn.execute("SELECT COUNT(*) FROM search_chunks").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM search_corpus_manifest_seals").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM search_index_runs").fetchone()[0] == 0
        with pytest.raises(ValueError, match="sealed corpus"):
            HybridRetriever(conn).search("one", _request_manifest_id(conn), limit=1)
        assert sum(statement == "BEGIN IMMEDIATE" for statement in statements) >= 2
        assert sum(statement == "COMMIT" for statement in statements) >= 2
    finally:
        conn.close()


def _request_manifest_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT manifest_id FROM search_corpus_manifests").fetchone()
    assert row is not None
    return str(row[0])


def test_missing_expected_document_remains_visible_and_produces_incomplete_seal(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed(conn)
        request = _request(
            apply=True,
            inventory=(
                ExpectedDocument(
                    expected_document_key="ACME:2026Q1:10-Q",
                    document_version_id="doc",
                    membership_status="included",
                    reason="verified source evidence",
                ),
                ExpectedDocument(
                    expected_document_key="ACME:2026Q1:deck",
                    membership_status="missing",
                    reason="issuer has not published the deck",
                ),
            ),
        )
        result = build_grounded_search_corpus(conn, request)

        assert result.completion_status == "incomplete"
        assert (
            conn.execute(
                "SELECT membership_status FROM search_corpus_document_memberships "
                "WHERE expected_document_key = 'ACME:2026Q1:deck'"
            ).fetchone()[0]
            == "missing"
        )
    finally:
        conn.close()


def test_included_document_requires_current_succeeded_evidence_anchor(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed(conn)
        request = _request(
            apply=False,
            inventory=(
                ExpectedDocument(
                    expected_document_key="ACME:2026Q1:10-Q",
                    document_version_id="unknown-document",
                    membership_status="included",
                    reason="claimed evidence",
                ),
            ),
        )

        with pytest.raises(ValueError, match="document version"):
            build_grounded_search_corpus(conn, request)
    finally:
        conn.close()


def test_included_document_quarantines_unapproved_or_placeholder_only_extraction(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        _seed(conn)
        request = _request(apply=False).model_copy(
            update={"required_extractor_names": ("fulltext-evidence-backfill",)}
        )

        result = build_grounded_search_corpus(conn, request)

        assert result.completion_status == "incomplete"
        membership = conn.execute(
            "SELECT membership_status, reason FROM search_corpus_document_memberships LIMIT 1"
        ).fetchone()
        assert membership is None
        assert result.planned_chunks == ()
    finally:
        conn.close()


def test_cli_uses_closed_inventory_file_and_defaults_to_read_only_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution.build_grounded_search_corpus import main

    conn = _conn(tmp_path)
    db_path = tmp_path / "corpus.db"
    try:
        _seed(conn)
    finally:
        conn.close()
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "expected_documents": [
                    {
                        "expected_document_key": "ACME:2026Q1:10-Q",
                        "document_version_id": "doc",
                        "membership_status": "included",
                        "reason": "verified source evidence",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--inventory",
                str(inventory_path),
                "--allow-unsealed-inventory",
                "--corpus-key",
                "issuer:ACME:reporting",
                "--revision",
                "1",
                "--selector-code-version",
                "corpus-builder@1",
                "--recorded-at",
                STAMP.isoformat(),
                "--extractor-name",
                "parser",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry_run"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM search_corpus_manifests").fetchone()[0] == 0
    finally:
        conn.close()
