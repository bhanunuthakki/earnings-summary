from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import fitz
import pytest
from alembic.config import Config

from alembic import command
from provenance.document_processing_evidence import (
    DocumentProcessingEvidenceIntegrityError,
    DocumentProcessingEvidenceMissingError,
    DocumentProcessingEvidenceUnsupportedError,
    publish_document_processing_evidence,
    record_pdf_table_extraction_artifact,
    verify_document_processing_evidence,
)
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
    OFFICE_FULLTEXT_EXTRACTOR,
    PDF_TABLE_EXTRACTOR_NAME,
    STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR,
    pdf_table_extractor_code_version,
)
from provenance.pdf_table_extraction import PdfTableExtractionArtifact, extract_pdf_tables

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 7, 27, 12, tzinfo=UTC)
T1 = datetime(2026, 7, 27, 13, tzinfo=UTC)
T2 = datetime(2026, 7, 27, 14, tzinfo=UTC)


def _sha(value: object) -> str:
    encoded = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _legacy_canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture(scope="session")
def processing_evidence_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    path = tmp_path_factory.mktemp("processing-evidence-schema") / "template.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        """
    )
    conn.close()
    base_revision = "0213_decision_draft_provider_id"
    config = _config(path)
    command.stamp(config, base_revision)
    command.upgrade(config, "0248_native_processing_closure_adapters")
    return path


@pytest.fixture
def conn(tmp_path: Path, processing_evidence_template: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "case.db"
    shutil.copy2(processing_evidence_template, path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.create_function("fact_sha256", 1, _sha)
    yield connection
    connection.close()


def _seed_document(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    media_type: str,
) -> str:
    blob_sha = _sha(f"bytes:{document_version_id}")
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob_sha,
            byte_size=100,
            media_type=media_type,
            storage_uri=f"file:///evidence/{document_version_id}",
            recorded_at=T0,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=f"obs:{document_version_id}",
            idempotency_key=f"obs:{document_version_id}",
            source_kind="issuer",
            source_url=f"https://issuer.example/{document_version_id}",
            blob_sha256=blob_sha,
            source_published_at=T0,
            filing_at=None,
            accepted_at=None,
            observed_at=T0,
            retrieved_at=T0,
            retrieval_config_sha256="a" * 64,
            collector_code_version="test@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=document_version_id,
            document_key=f"document:{document_version_id}",
            version_sequence=1,
            observation_id=f"obs:{document_version_id}",
            blob_sha256=blob_sha,
            issuer_id="issuer:ACME",
            ticker="ACME",
            document_type="investor_material",
            form_type="exhibit",
            accession_number=None,
            exhibit_id=None,
            period_start=None,
            period_end=T0,
            as_of_at=T0,
            language="en",
            replaces_document_version_id=None,
            legacy_document_id=None,
            recorded_at=T0,
        )
    )
    return blob_sha


def _pdf_table_fixture(*, table: bool = True, image_only: bool = False) -> bytes:
    document = fitz.open()
    page = document.new_page(width=320, height=220)
    if image_only:
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
        pixmap.clear_with(255)
        page.insert_image((20, 20, 300, 200), pixmap=pixmap)
    elif table:
        for y, values in zip(
            (40, 80, 120),
            (("Metric", "Q1"), ("Revenue", "10"), ("Margin", "40")),
            strict=True,
        ):
            for x, value in zip((30, 160), values, strict=True):
                page.insert_text((x, y), value)
        shape = page.new_shape()
        for x in (20, 150, 300):
            shape.draw_line((x, 20), (x, 150))
        for y in (20, 60, 100, 150):
            shape.draw_line((20, y), (300, y))
        shape.finish()
        shape.commit()
    else:
        page.insert_text((30, 60), "Narrative disclosure without tabular layout.")
    raw = document.tobytes(garbage=4, deflate=True)
    document.close()
    return raw


def _seed_pdf_table_artifact(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    raw_pdf_bytes: bytes,
    artifact: PdfTableExtractionArtifact | None = None,
    run_id: str = "pdf-table-run",
    code_version: str | None = None,
) -> tuple[PdfTableExtractionArtifact, str]:
    extracted = artifact or extract_pdf_tables(raw_pdf_bytes)
    blob_sha = hashlib.sha256(raw_pdf_bytes).hexdigest()
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob_sha,
            byte_size=len(raw_pdf_bytes),
            media_type="application/pdf",
            storage_uri=f"memory://{blob_sha}",
            recorded_at=T0,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=f"obs:{document_version_id}",
            idempotency_key=f"obs:{document_version_id}",
            source_kind="issuer",
            source_url=f"https://issuer.example/{document_version_id}",
            blob_sha256=blob_sha,
            source_published_at=T0,
            filing_at=None,
            accepted_at=None,
            observed_at=T0,
            retrieved_at=T0,
            retrieval_config_sha256="a" * 64,
            collector_code_version="test@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=document_version_id,
            document_key=f"document:{document_version_id}",
            version_sequence=1,
            observation_id=f"obs:{document_version_id}",
            blob_sha256=blob_sha,
            issuer_id="issuer:ACME",
            ticker="ACME",
            document_type="investor_material",
            form_type="exhibit",
            accession_number=None,
            exhibit_id=None,
            period_start=None,
            period_end=T0,
            as_of_at=T0,
            language="en",
            replaces_document_version_id=None,
            legacy_document_id=None,
            recorded_at=T0,
        )
    )
    return _seed_pdf_table_artifact_for_existing_document(
        conn,
        document_version_id=document_version_id,
        raw_pdf_bytes=raw_pdf_bytes,
        artifact=extracted,
        run_id=run_id,
        code_version=code_version,
    )


def _seed_pdf_table_artifact_for_existing_document(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    raw_pdf_bytes: bytes,
    artifact: PdfTableExtractionArtifact | None = None,
    run_id: str = "pdf-table-run",
    code_version: str | None = None,
) -> tuple[PdfTableExtractionArtifact, str]:
    extracted = artifact or extract_pdf_tables(raw_pdf_bytes)
    blob_sha = hashlib.sha256(raw_pdf_bytes).hexdigest()
    ledger = EvidenceLedger(conn)
    exact_code_version = pdf_table_extractor_code_version(
        detector_version=extracted.detector.detector_version,
        pymupdf_version=extracted.detector.pymupdf_version,
        mupdf_version=extracted.detector.mupdf_version,
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id=run_id,
            idempotency_key=f"run:{run_id}",
            document_version_id=document_version_id,
            input_sha256=blob_sha,
            extractor_name=PDF_TABLE_EXTRACTOR_NAME,
            extractor_config_sha256=extracted.detector.configuration_sha256,
            extractor_code_version=code_version or exact_code_version,
            output_sha256=extracted.ordered_page_table_seal_sha256,
            started_at=T0,
            completed_at=T0,
            outcome="succeeded",
        )
    )
    persistence = record_pdf_table_extraction_artifact(
        conn,
        document_version_id=document_version_id,
        extraction_run_id=run_id,
        raw_pdf_bytes=raw_pdf_bytes,
        artifact=extracted,
        recorded_at=T0,
    )
    return extracted, persistence.artifact_id


def _node(
    *,
    run_id: str,
    node_id: str,
    kind: str,
    text: str,
    locator: EvidenceLocator,
    parent_node_id: str | None,
) -> EvidenceNode:
    return EvidenceNode.model_validate(
        {
            "node_id": node_id,
            "evidence_key": f"key:{node_id}",
            "revision": 1,
            "extraction_run_id": run_id,
            "parent_node_id": parent_node_id,
            "supersedes_node_id": None,
            "node_kind": kind,
            "text": text,
            "locator": locator,
            "recorded_at": T0,
        }
    )


def _seed_run(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    blob_sha: str,
    run_id: str,
    extractor_name: str,
    extractor_code_version: str,
    extractor_config_sha256: str,
    children: tuple[tuple[str, str, EvidenceLocator], ...],
    legacy_ascii_output: bool = False,
) -> tuple[EvidenceNode, ...]:
    document_node = _node(
        run_id=run_id,
        node_id=f"{run_id}:document",
        kind="document",
        text=f"Document {document_version_id}",
        locator=EvidenceLocator(source_ref=document_version_id),
        parent_node_id=None,
    )
    nodes = (
        document_node,
        *(
            _node(
                run_id=run_id,
                node_id=f"{run_id}:{ordinal}",
                kind=kind,
                text=text,
                locator=locator,
                parent_node_id=document_node.node_id,
            )
            for ordinal, (kind, text, locator) in enumerate(children, start=1)
        ),
    )
    output_payload = [item.model_dump(mode="json", exclude_none=True) for item in nodes]
    output_sha = hashlib.sha256(
        (
            _legacy_canonical(output_payload) if legacy_ascii_output else _canonical(output_payload)
        ).encode()
    ).hexdigest()
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ExtractionRun(
            extraction_run_id=run_id,
            idempotency_key=f"run:{run_id}",
            document_version_id=document_version_id,
            input_sha256=blob_sha,
            extractor_name=extractor_name,
            extractor_config_sha256=extractor_config_sha256,
            extractor_code_version=extractor_code_version,
            output_sha256=output_sha,
            started_at=T0,
            completed_at=T0,
            outcome="succeeded",
        )
    )
    for item in nodes:
        ledger.persist(item)
    return nodes


def _seed_structured_run(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    blob_sha: str,
    run_id: str,
    children: tuple[
        tuple[str, str | None, str, str, EvidenceLocator],
        ...,
    ],
    extractor_code_version: str | None = None,
    extractor_config_sha256: str | None = None,
) -> tuple[EvidenceNode, ...]:
    document_node = _node(
        run_id=run_id,
        node_id=f"{run_id}:document",
        kind="document",
        text=f"Document {document_version_id}",
        locator=EvidenceLocator(source_ref=document_version_id),
        parent_node_id=None,
    )
    nodes_by_key = {"document": document_node}
    ordered = [document_node]
    for ordinal, (key, parent_key, kind, text, locator) in enumerate(children, start=1):
        parent = document_node if parent_key is None else nodes_by_key[parent_key]
        node = _node(
            run_id=run_id,
            node_id=f"{run_id}:{ordinal}",
            kind=kind,
            text=text,
            locator=locator,
            parent_node_id=parent.node_id,
        )
        nodes_by_key[key] = node
        ordered.append(node)
    nodes = tuple(ordered)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ExtractionRun(
            extraction_run_id=run_id,
            idempotency_key=f"run:{run_id}",
            document_version_id=document_version_id,
            input_sha256=blob_sha,
            extractor_name=OFFICE_FULLTEXT_EXTRACTOR.name,
            extractor_config_sha256=(
                extractor_config_sha256 or OFFICE_FULLTEXT_EXTRACTOR.config_sha256
            ),
            extractor_code_version=(
                extractor_code_version or OFFICE_FULLTEXT_EXTRACTOR.code_version
            ),
            output_sha256=_sha([item.model_dump(mode="json", exclude_none=True) for item in nodes]),
            started_at=T0,
            completed_at=T0,
            outcome="succeeded",
        )
    )
    for node in nodes:
        ledger.persist(node)
    return nodes


def _seed_exact_pptx_run(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    blob_sha: str,
    run_id: str,
) -> tuple[EvidenceNode, ...]:
    chart_sha = _sha("ppt/charts/chart1.xml")
    return _seed_structured_run(
        conn,
        document_version_id=document_version_id,
        blob_sha=blob_sha,
        run_id=run_id,
        children=(
            (
                "chart-inventory",
                None,
                "passage",
                "PPTX chart inventory: count=1",
                EvidenceLocator(
                    source_ref="deck.pptx",
                    slide_number=1,
                    office_object_kind="pptx_chart_inventory",
                    office_package_part="ppt/slides/slide1.xml",
                ),
            ),
            (
                "table-inventory",
                None,
                "passage",
                "PPTX table inventory: count=1",
                EvidenceLocator(
                    source_ref="deck.pptx",
                    slide_number=1,
                    office_object_kind="pptx_table_inventory",
                    office_package_part="ppt/slides/slide1.xml",
                ),
            ),
            (
                "table",
                "table-inventory",
                "table",
                "PPTX native table: rows=1; grid_columns=2; name=KPIs",
                EvidenceLocator(
                    source_ref="deck.pptx",
                    slide_number=1,
                    shape_index=0,
                    table_name="KPIs",
                    office_object_kind="pptx_table",
                    office_package_part="ppt/slides/slide1.xml",
                    office_object_ordinal=1,
                ),
            ),
            (
                "table-row",
                "table",
                "table_row",
                "PPTX native table row: cell_count=2",
                EvidenceLocator(
                    source_ref="deck.pptx",
                    slide_number=1,
                    shape_index=0,
                    table_name="KPIs",
                    table_row_index=1,
                    office_object_kind="pptx_table_row",
                    office_package_part="ppt/slides/slide1.xml",
                    office_object_ordinal=1,
                ),
            ),
            *(
                (
                    f"table-cell-{column}",
                    "table-row",
                    "table_cell",
                    f"text={text}",
                    EvidenceLocator(
                        source_ref="deck.pptx",
                        slide_number=1,
                        shape_index=0,
                        table_name="KPIs",
                        table_row_index=1,
                        table_column_index=column,
                        office_object_kind="pptx_table_cell",
                        office_package_part="ppt/slides/slide1.xml",
                        office_object_ordinal=1,
                    ),
                )
                for column, text in ((1, "Revenue"), (2, "100"))
            ),
            (
                "chart",
                "chart-inventory",
                "table",
                "PPTX chart: part=ppt/charts/chart1.xml; series_count=1",
                EvidenceLocator(
                    source_ref="deck.pptx",
                    slide_number=1,
                    shape_index=1,
                    office_object_kind="pptx_chart",
                    office_package_part="ppt/charts/chart1.xml",
                    office_relationship_id="rId9",
                    office_object_ordinal=1,
                    office_part_sha256=chart_sha,
                ),
            ),
            (
                "chart-series",
                "chart",
                "table_row",
                '{"series_index":0,"series_order":0}',
                EvidenceLocator(
                    source_ref="deck.pptx",
                    slide_number=1,
                    shape_index=1,
                    office_object_kind="pptx_chart_series",
                    office_package_part="ppt/charts/chart1.xml",
                    office_relationship_id="rId9",
                    office_object_ordinal=1,
                    office_series_ordinal=1,
                    office_part_sha256=chart_sha,
                ),
            ),
        ),
    )


def _seed_exact_xlsx_run(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    blob_sha: str,
    run_id: str,
) -> tuple[EvidenceNode, ...]:
    return _seed_structured_run(
        conn,
        document_version_id=document_version_id,
        blob_sha=blob_sha,
        run_id=run_id,
        children=(
            (
                "workbook-inventory",
                None,
                "passage",
                "XLSX named-table inventory: count=1",
                EvidenceLocator(
                    source_ref="book.xlsx",
                    office_object_kind="xlsx_named_table_inventory",
                    office_package_part="xl/workbook.xml",
                ),
            ),
            (
                "sheet",
                None,
                "table",
                "Worksheet: Income (state=visible)",
                EvidenceLocator(
                    source_ref="book.xlsx",
                    table_name="Income",
                    sheet_name="Income",
                ),
            ),
            (
                "sheet-inventory",
                "sheet",
                "passage",
                "XLSX sheet named-table inventory: count=1",
                EvidenceLocator(
                    source_ref="book.xlsx",
                    sheet_name="Income",
                    office_object_kind="xlsx_named_table_inventory",
                    office_package_part="xl/worksheets/sheet1.xml",
                ),
            ),
            (
                "named-table",
                "sheet-inventory",
                "table",
                (
                    "XLSX named table: id=7; name=Internal_Name; "
                    "displayName=Revenue_Table; ref=A1:B9"
                ),
                EvidenceLocator(
                    source_ref="book.xlsx",
                    table_name="Revenue_Table",
                    sheet_name="Income",
                    cell_range="A1:B9",
                    office_object_kind="xlsx_named_table",
                    office_package_part="xl/tables/table7.xml",
                    office_relationship_id="rIdTable",
                    office_object_ordinal=1,
                    office_part_sha256=_sha("xl/tables/table7.xml"),
                ),
            ),
        ),
    )


def _seed_pdf_ocr_governance(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    blob_sha: str,
    run_id: str,
    include_second_result: bool,
) -> tuple[EvidenceNode, ...]:
    nodes = _seed_run(
        conn,
        document_version_id=document_version_id,
        blob_sha=blob_sha,
        run_id=run_id,
        extractor_name="governed-pdf-ocr",
        extractor_code_version="governed-pdf-ocr@1",
        extractor_config_sha256="c" * 64,
        children=(
            (
                "pdf_page",
                "OCR page one",
                EvidenceLocator(source_ref="report.pdf", page_number=1),
            ),
            (
                "pdf_page",
                "OCR page two",
                EvidenceLocator(source_ref="report.pdf", page_number=2),
            ),
        ),
    )
    assessment_id = f"assessment:{run_id}"
    conn.execute(
        "INSERT INTO ocr_document_assessments ("
        "assessment_id,idempotency_key,document_version_id,input_sha256,"
        "detector_name,detector_config_sha256,detector_code_version,"
        "native_output_sha256,page_count,outcome,reason_code,assessed_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            assessment_id,
            assessment_id,
            document_version_id,
            blob_sha,
            "pdf-preflight",
            "d" * 64,
            "pdf-preflight@1",
            "e" * 64,
            2,
            "ocr_required",
            None,
            T0,
        ),
    )
    for page_number in (1, 2):
        conn.execute(
            "INSERT INTO ocr_preflight_pages VALUES (?,?,?,?,?)",
            (
                assessment_id,
                page_number,
                0,
                _sha(""),
                1,
            ),
        )
    conn.execute(
        "INSERT INTO ocr_extraction_governance ("
        "extraction_run_id,assessment_id,engine_name,engine_version,"
        "engine_binary_sha256,model_name,model_version,"
        "model_manifest_sha256,model_artifacts_json,languages_json,"
        "engine_config_json,extractor_config_sha256,renderer_name,"
        "renderer_version,renderer_binary_sha256,recorded_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            assessment_id,
            "tesseract",
            "5",
            "1" * 64,
            "eng",
            "1",
            "2" * 64,
            "[]",
            '["eng"]',
            "{}",
            "c" * 64,
            "poppler",
            "1",
            "3" * 64,
            T0,
        ),
    )
    for page_number, node in enumerate(nodes[1:], start=1):
        if page_number == 2 and not include_second_result:
            continue
        locator = EvidenceLocator(source_ref="report.pdf", page_number=page_number)
        conn.execute(
            "INSERT INTO ocr_page_results ("
            "extraction_run_id,page_number,node_id,outcome,output_sha256,"
            "mean_confidence,locator_json,locator_sha256,reason_code,"
            "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                page_number,
                node.node_id,
                "accepted",
                _sha(node.text),
                99.0,
                locator.canonical_json,
                locator.canonical_sha256,
                None,
                T0,
            ),
        )
    return nodes


def _seed_image_ocr_governance(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    blob_sha: str,
    run_id: str,
) -> None:
    nodes = _seed_run(
        conn,
        document_version_id=document_version_id,
        blob_sha=blob_sha,
        run_id=run_id,
        extractor_name="governed-image-ocr",
        extractor_code_version="governed-image-ocr@1",
        extractor_config_sha256="4" * 64,
        children=(
            (
                "passage",
                "Chart annotation",
                EvidenceLocator(source_ref="chart.png", page_number=1),
            ),
        ),
    )
    assessment_id = f"assessment:{run_id}"
    conn.execute(
        "INSERT INTO image_ocr_assessments ("
        "assessment_id,idempotency_key,document_version_id,input_sha256,"
        "observed_sha256,observed_byte_size,media_type,width,height,"
        "pixel_count,page_count,detector_name,detector_config_sha256,"
        "detector_code_version,outcome,reason_code,assessed_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            assessment_id,
            assessment_id,
            document_version_id,
            blob_sha,
            blob_sha,
            100,
            "image/png",
            10,
            10,
            100,
            1,
            "image-preflight",
            "5" * 64,
            "image-preflight@1",
            "ocr_required",
            None,
            T0,
        ),
    )
    conn.execute(
        "INSERT INTO image_ocr_extraction_governance ("
        "extraction_run_id,assessment_id,engine_name,engine_version,"
        "engine_binary_sha256,model_name,model_version,"
        "model_manifest_sha256,model_artifacts_json,languages_json,"
        "engine_config_json,extractor_config_sha256,recorded_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            assessment_id,
            "tesseract",
            "5",
            "6" * 64,
            "eng",
            "1",
            "7" * 64,
            "[]",
            '["eng"]',
            "{}",
            "4" * 64,
            T0,
        ),
    )
    locator = EvidenceLocator(source_ref="chart.png", page_number=1)
    conn.execute(
        "INSERT INTO image_ocr_results ("
        "extraction_run_id,page_number,node_id,outcome,output_sha256,"
        "mean_confidence,locator_json,locator_sha256,reason_code,"
        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            1,
            nodes[1].node_id,
            "accepted",
            _sha(nodes[1].text),
            98.0,
            locator.canonical_json,
            locator.canonical_sha256,
            None,
            T0,
        ),
    )


def test_pptx_slides_are_exact_ordered_replay_and_row_factory_neutral(
    conn: sqlite3.Connection,
) -> None:
    blob = _seed_document(
        conn,
        document_version_id="deck-v1",
        media_type=("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    )
    _seed_run(
        conn,
        document_version_id="deck-v1",
        blob_sha=blob,
        run_id="deck-run",
        extractor_name=OFFICE_FULLTEXT_EXTRACTOR.name,
        extractor_code_version=OFFICE_FULLTEXT_EXTRACTOR.code_version,
        extractor_config_sha256=OFFICE_FULLTEXT_EXTRACTOR.config_sha256,
        children=(
            (
                "passage",
                "First slide",
                EvidenceLocator(source_ref="deck.pptx", slide_number=1),
            ),
            (
                "passage",
                "Second slide",
                EvidenceLocator(source_ref="deck.pptx", slide_number=2),
            ),
        ),
    )
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="deck-v1",
        processing_lane="pptx_slides",
        cutoff_at=T1,
        recorded_at=T2,
    )
    assert receipt.member_count == 2
    assert receipt.exact_replay is False
    conn.row_factory = sqlite3.Row
    verified = verify_document_processing_evidence(
        conn,
        receipt.evidence_seal_id,
        document_version_id="deck-v1",
        processing_lane="pptx_slides",
        cutoff_at=T1,
    )
    assert verified.member_set_sha256 == receipt.member_set_sha256
    assert verified.knowledge_at == T0
    assert verified.recorded_at == T2
    with pytest.raises(
        DocumentProcessingEvidenceIntegrityError,
        match="processing_evidence_absent_at_observed_through",
    ):
        verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id="deck-v1",
            processing_lane="pptx_slides",
            cutoff_at=T1,
            observed_through=T1,
        )
    with pytest.raises(
        DocumentProcessingEvidenceIntegrityError,
        match="processing_evidence_coordinate_mismatch",
    ):
        verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id="wrong-document",
            processing_lane="pptx_slides",
            cutoff_at=T1,
        )
    with pytest.raises(
        DocumentProcessingEvidenceIntegrityError,
        match="processing_evidence_coordinate_mismatch",
    ):
        verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id="deck-v1",
            processing_lane="xlsx_sheets",
            cutoff_at=T1,
        )
    replay = publish_document_processing_evidence(
        conn,
        document_version_id="deck-v1",
        processing_lane="pptx_slides",
        cutoff_at=T1,
        recorded_at=T2,
    )
    assert replay.exact_replay is True


def test_unrecorded_pdf_table_lane_fails_closed(conn: sqlite3.Connection) -> None:
    _seed_document(
        conn,
        document_version_id="pdf-without-table-artifact",
        media_type="application/pdf",
    )
    with pytest.raises(
        DocumentProcessingEvidenceMissingError,
        match="sealed_pdf_table_artifact_missing_or_ambiguous",
    ):
        publish_document_processing_evidence(
            conn,
            document_version_id="pdf-without-table-artifact",
            processing_lane="pdf_table",
            cutoff_at=T1,
            recorded_at=T2,
        )


def test_exact_pdf_table_artifact_publishes_ordered_native_inventory(
    conn: sqlite3.Connection,
) -> None:
    raw = _pdf_table_fixture()
    artifact, artifact_id = _seed_pdf_table_artifact(
        conn,
        document_version_id="pdf-table-v1",
        raw_pdf_bytes=raw,
    )
    assert record_pdf_table_extraction_artifact(
        conn,
        document_version_id="pdf-table-v1",
        extraction_run_id="pdf-table-run",
        raw_pdf_bytes=raw,
        artifact=artifact,
        recorded_at=T0,
    ).model_dump() == {
        "artifact_id": artifact_id,
        "document_version_id": "pdf-table-v1",
        "extraction_run_id": "pdf-table-run",
        "disposition": "sealed",
        "member_count": 11,
        "member_set_sha256": conn.execute(
            "SELECT member_set_sha256 FROM pdf_table_extraction_artifact_seals WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()[0],
        "exact_replay": True,
    }
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="pdf-table-v1",
        processing_lane="pdf_table",
        cutoff_at=T1,
        recorded_at=T2,
    )
    kinds = conn.execute(
        "SELECT member_kind FROM pdf_table_extraction_artifact_members "
        "WHERE artifact_id=? ORDER BY member_ordinal",
        (artifact_id,),
    ).fetchall()
    assert [str(row[0]) for row in kinds] == [
        "page",
        "table",
        "row",
        "cell",
        "cell",
        "row",
        "cell",
        "cell",
        "row",
        "cell",
        "cell",
    ]
    assert receipt.member_count == len(kinds)
    verified = verify_document_processing_evidence(
        conn,
        receipt.evidence_seal_id,
        document_version_id="pdf-table-v1",
        processing_lane="pdf_table",
        cutoff_at=T1,
    )
    assert verified.native_output_sha256 == artifact.ordered_page_table_seal_sha256
    assert conn.execute(
        "SELECT assessment_table,assessment_id FROM document_processing_evidence_headers "
        "WHERE evidence_seal_id=?",
        (receipt.evidence_seal_id,),
    ).fetchone() == ("pdf_table_extraction_artifact_headers", artifact_id)


def test_pdf_table_explicit_no_table_page_is_publishable_but_quarantine_is_not(
    conn: sqlite3.Connection,
) -> None:
    raw = _pdf_table_fixture(table=False)
    _seed_pdf_table_artifact(
        conn,
        document_version_id="pdf-no-table-v1",
        raw_pdf_bytes=raw,
        run_id="pdf-no-table-run",
    )
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="pdf-no-table-v1",
        processing_lane="pdf_table",
        cutoff_at=T1,
        recorded_at=T2,
    )
    assert receipt.member_count == 1
    assert conn.execute(
        "SELECT member_kind,disposition FROM pdf_table_extraction_artifact_members "
        "WHERE artifact_id=(SELECT assessment_id "
        "FROM document_processing_evidence_headers WHERE evidence_seal_id=?)",
        (receipt.evidence_seal_id,),
    ).fetchone() == ("page", "no_tables_detected")

    quarantined_raw = _pdf_table_fixture(image_only=True)
    artifact, _ = _seed_pdf_table_artifact(
        conn,
        document_version_id="pdf-quarantined-v1",
        raw_pdf_bytes=quarantined_raw,
        run_id="pdf-quarantined-run",
    )
    assert artifact.disposition == "quarantined"
    with pytest.raises(
        DocumentProcessingEvidenceMissingError,
        match="sealed_pdf_table_artifact_missing_or_ambiguous",
    ):
        publish_document_processing_evidence(
            conn,
            document_version_id="pdf-quarantined-v1",
            processing_lane="pdf_table",
            cutoff_at=T1,
            recorded_at=T2,
        )


def test_pdf_table_old_identity_and_member_tampering_fail_closed(
    conn: sqlite3.Connection,
) -> None:
    raw = _pdf_table_fixture()
    artifact = extract_pdf_tables(raw)
    with pytest.raises(
        DocumentProcessingEvidenceIntegrityError,
        match="pdf_table_document_bytes_mismatch",
    ):
        _seed_pdf_table_artifact(
            conn,
            document_version_id="pdf-wrong-bytes-v1",
            raw_pdf_bytes=raw + b"tampered",
            artifact=artifact,
            run_id="pdf-wrong-bytes-run",
        )
    with pytest.raises(
        DocumentProcessingEvidenceMissingError,
        match="native_extraction_run_missing",
    ):
        _seed_pdf_table_artifact(
            conn,
            document_version_id="pdf-old-detector-v1",
            raw_pdf_bytes=raw,
            run_id="pdf-old-detector-run",
            code_version="pymupdf-dual-table-detector@0",
        )

    _artifact, artifact_id = _seed_pdf_table_artifact(
        conn,
        document_version_id="pdf-tampered-v1",
        raw_pdf_bytes=raw,
        run_id="pdf-tampered-run",
    )
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="pdf-tampered-v1",
        processing_lane="pdf_table",
        cutoff_at=T1,
        recorded_at=T2,
    )
    conn.execute("DROP TRIGGER trg_pdf_table_extraction_artifact_members_update_append_only")
    conn.execute(
        "DROP TRIGGER trg_pdf_table_extraction_artifact_members_canonical_object_sha256_exact"
    )
    row = conn.execute(
        "SELECT member_ordinal,canonical_object_json "
        "FROM pdf_table_extraction_artifact_members "
        "WHERE artifact_id=? AND member_kind='table'",
        (artifact_id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(str(row[1]))
    payload["bbox"]["x1"] = float(payload["bbox"]["x1"]) + 1.0
    tampered = _canonical(payload)
    conn.execute(
        "UPDATE pdf_table_extraction_artifact_members "
        "SET canonical_object_json=?,canonical_object_sha256=? "
        "WHERE artifact_id=? AND member_ordinal=?",
        (tampered, _sha(tampered), artifact_id, int(row[0])),
    )
    with pytest.raises(
        DocumentProcessingEvidenceIntegrityError,
        match="pdf_table_native_member_mismatch",
    ):
        verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id="pdf-tampered-v1",
            processing_lane="pdf_table",
            cutoff_at=T1,
        )


def test_processing_evidence_migration_supports_native_lanes_and_downgrades(
    tmp_path: Path,
    processing_evidence_template: Path,
) -> None:
    path = tmp_path / "migration-round-trip.db"
    shutil.copy2(processing_evidence_template, path)
    connection = sqlite3.connect(path)
    ddl = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='document_processing_evidence_headers'"
    ).fetchone()
    connection.close()
    assert ddl is not None
    assert all(
        lane in str(ddl[0]) for lane in ("pdf_table", "pptx_charts", "pptx_tables", "xlsx_tables")
    )
    connection = sqlite3.connect(path)
    assert {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'pdf_table_extraction_artifact_%'"
        ).fetchall()
    } == {
        "pdf_table_extraction_artifact_headers",
        "pdf_table_extraction_artifact_members",
        "pdf_table_extraction_artifact_seals",
    }
    connection.close()

    config = _config(path)
    command.downgrade(config, "0247_bounded_canonical_retrieval")
    downgraded = sqlite3.connect(path)
    assert (
        downgraded.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND (name LIKE 'document_processing_evidence_%' "
            "OR name LIKE 'pdf_table_extraction_artifact_%')"
        ).fetchone()[0]
        == 0
    )
    downgraded.close()


def test_exact_office_object_inventories_publish_all_native_lanes(
    conn: sqlite3.Connection,
) -> None:
    pptx_blob = _seed_document(
        conn,
        document_version_id="deck-native-v1",
        media_type=("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    )
    _seed_exact_pptx_run(
        conn,
        document_version_id="deck-native-v1",
        blob_sha=pptx_blob,
        run_id="deck-native-run",
    )
    xlsx_blob = _seed_document(
        conn,
        document_version_id="workbook-native-v1",
        media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    )
    _seed_exact_xlsx_run(
        conn,
        document_version_id="workbook-native-v1",
        blob_sha=xlsx_blob,
        run_id="workbook-native-run",
    )
    receipts = {
        lane: publish_document_processing_evidence(
            conn,
            document_version_id=document,
            processing_lane=lane,
            cutoff_at=T1,
            recorded_at=T2,
        )
        for lane, document in (
            ("pptx_charts", "deck-native-v1"),
            ("pptx_tables", "deck-native-v1"),
            ("xlsx_tables", "workbook-native-v1"),
        )
    }
    assert {lane: receipt.member_count for lane, receipt in receipts.items()} == {
        "pptx_charts": 3,
        "pptx_tables": 5,
        "xlsx_tables": 3,
    }
    assert all(
        verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id=receipt.document_version_id,
            processing_lane=lane,
            cutoff_at=T1,
        ).member_set_sha256
        == receipt.member_set_sha256
        for lane, receipt in receipts.items()
    )
    with pytest.raises(
        DocumentProcessingEvidenceIntegrityError,
        match="processing_evidence_coordinate_mismatch",
    ):
        verify_document_processing_evidence(
            conn,
            receipts["pptx_charts"].evidence_seal_id,
            document_version_id="deck-native-v1",
            processing_lane="pptx_tables",
            cutoff_at=T1,
        )
    with pytest.raises(
        DocumentProcessingEvidenceMissingError,
        match="document_media_type_does_not_match_processing_lane",
    ):
        publish_document_processing_evidence(
            conn,
            document_version_id="workbook-native-v1",
            processing_lane="pptx_charts",
            cutoff_at=T1,
            recorded_at=T2,
        )


def test_old_office_identity_cannot_satisfy_native_object_lane(
    conn: sqlite3.Connection,
) -> None:
    blob = _seed_document(
        conn,
        document_version_id="old-deck-v1",
        media_type=("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    )
    _seed_structured_run(
        conn,
        document_version_id="old-deck-v1",
        blob_sha=blob,
        run_id="old-deck-run",
        extractor_code_version="fulltext-evidence-backfill@2-office-ooxml",
        extractor_config_sha256="a" * 64,
        children=(
            (
                "chart-inventory",
                None,
                "passage",
                "PPTX chart inventory: count=0",
                EvidenceLocator(
                    source_ref="deck.pptx",
                    slide_number=1,
                    office_object_kind="pptx_chart_inventory",
                    office_package_part="ppt/slides/slide1.xml",
                ),
            ),
            (
                "table-inventory",
                None,
                "passage",
                "PPTX table inventory: count=0",
                EvidenceLocator(
                    source_ref="deck.pptx",
                    slide_number=1,
                    office_object_kind="pptx_table_inventory",
                    office_package_part="ppt/slides/slide1.xml",
                ),
            ),
        ),
    )
    with pytest.raises(
        DocumentProcessingEvidenceMissingError,
        match="native_extraction_run_missing",
    ):
        publish_document_processing_evidence(
            conn,
            document_version_id="old-deck-v1",
            processing_lane="pptx_charts",
            cutoff_at=T1,
            recorded_at=T2,
        )


def test_office_zero_object_lane_requires_explicit_per_slide_proofs(
    conn: sqlite3.Connection,
) -> None:
    blob = _seed_document(
        conn,
        document_version_id="zero-deck-v1",
        media_type=("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    )
    _seed_structured_run(
        conn,
        document_version_id="zero-deck-v1",
        blob_sha=blob,
        run_id="zero-deck-run",
        children=(
            (
                "chart-inventory",
                None,
                "passage",
                "PPTX chart inventory: count=0",
                EvidenceLocator(
                    source_ref="zero.pptx",
                    slide_number=1,
                    office_object_kind="pptx_chart_inventory",
                    office_package_part="ppt/slides/slide1.xml",
                ),
            ),
            (
                "table-inventory",
                None,
                "passage",
                "PPTX table inventory: count=0",
                EvidenceLocator(
                    source_ref="zero.pptx",
                    slide_number=1,
                    office_object_kind="pptx_table_inventory",
                    office_package_part="ppt/slides/slide1.xml",
                ),
            ),
        ),
    )
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="zero-deck-v1",
        processing_lane="pptx_charts",
        cutoff_at=T1,
        recorded_at=T2,
    )
    assert receipt.member_count == 1

    missing_blob = _seed_document(
        conn,
        document_version_id="absent-deck-v1",
        media_type=("application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    )
    _seed_structured_run(
        conn,
        document_version_id="absent-deck-v1",
        blob_sha=missing_blob,
        run_id="absent-deck-run",
        children=(),
    )
    with pytest.raises(
        DocumentProcessingEvidenceMissingError,
        match="pptx_object_inventory_incomplete",
    ):
        publish_document_processing_evidence(
            conn,
            document_version_id="absent-deck-v1",
            processing_lane="pptx_charts",
            cutoff_at=T1,
            recorded_at=T2,
        )


def test_transcript_speaker_lane_requires_immutable_speaker_and_timecodes(
    conn: sqlite3.Connection,
) -> None:
    blob = _seed_document(conn, document_version_id="transcript-v1", media_type="text/plain")
    _seed_run(
        conn,
        document_version_id="transcript-v1",
        blob_sha=blob,
        run_id="transcript-run",
        extractor_name="legacy-evidence-backfill",
        extractor_code_version="evidence-backfill@1",
        extractor_config_sha256="b" * 64,
        children=(
            (
                "transcript_turn",
                "Prepared remarks.",
                EvidenceLocator(
                    transcript_turn_sequence=0,
                    transcript_speaker="CEO",
                    transcript_time_code_start="00:00:01",
                    transcript_time_code_end="00:00:10",
                    legacy_table="transcript_segments",
                    legacy_row_id=1,
                ),
            ),
            (
                "transcript_turn",
                "Question.",
                EvidenceLocator(
                    transcript_turn_sequence=1,
                    transcript_speaker="Analyst",
                    transcript_time_code_start="00:00:11",
                    transcript_time_code_end="00:00:20",
                    legacy_table="transcript_segments",
                    legacy_row_id=2,
                ),
            ),
        ),
    )
    turns = publish_document_processing_evidence(
        conn,
        document_version_id="transcript-v1",
        processing_lane="transcript_turns",
        cutoff_at=T1,
        recorded_at=T2,
    )
    speakers = publish_document_processing_evidence(
        conn,
        document_version_id="transcript-v1",
        processing_lane="transcript_speakers",
        cutoff_at=T1,
        recorded_at=T2,
    )
    assert turns.member_count == speakers.member_count == 2

    blob_missing = _seed_document(
        conn, document_version_id="transcript-v2", media_type="text/plain"
    )
    _seed_run(
        conn,
        document_version_id="transcript-v2",
        blob_sha=blob_missing,
        run_id="transcript-run-missing-time",
        extractor_name="legacy-evidence-backfill",
        extractor_code_version="evidence-backfill@1",
        extractor_config_sha256="b" * 64,
        children=(
            (
                "transcript_turn",
                "No timestamps.",
                EvidenceLocator(
                    transcript_turn_sequence=0,
                    transcript_speaker="CEO",
                    legacy_table="transcript_segments",
                    legacy_row_id=3,
                ),
            ),
        ),
    )
    with pytest.raises(
        DocumentProcessingEvidenceUnsupportedError,
        match="transcript_speaker_or_timecode_not_immutably_bound",
    ):
        publish_document_processing_evidence(
            conn,
            document_version_id="transcript-v2",
            processing_lane="transcript_speakers",
            cutoff_at=T1,
            recorded_at=T2,
        )
    conn.execute("DROP TRIGGER trg_evidence_nodes_append_only_delete")
    conn.execute("DROP TRIGGER trg_evidence_nodes_processing_evidence_frozen")
    conn.execute(
        "DELETE FROM evidence_nodes "
        "WHERE extraction_run_id='transcript-run' "
        "AND node_kind='transcript_turn' "
        "AND locator_json LIKE '%\"transcript_turn_sequence\":1%'"
    )
    for receipt, lane in (
        (turns, "transcript_turns"),
        (speakers, "transcript_speakers"),
    ):
        with pytest.raises(
            DocumentProcessingEvidenceIntegrityError,
            match="native_extraction_output_commitment_mismatch",
        ):
            verify_document_processing_evidence(
                conn,
                receipt.evidence_seal_id,
                document_version_id="transcript-v1",
                processing_lane=lane,
                cutoff_at=T1,
            )


def test_pdf_ocr_rejects_partial_required_page_set_then_closes_exactly(
    conn: sqlite3.Connection,
) -> None:
    blob = _seed_document(conn, document_version_id="pdf-ocr-v1", media_type="application/pdf")
    nodes = _seed_pdf_ocr_governance(
        conn,
        document_version_id="pdf-ocr-v1",
        blob_sha=blob,
        run_id="pdf-ocr-run",
        include_second_result=False,
    )
    with pytest.raises(
        DocumentProcessingEvidenceMissingError,
        match="ocr_result_page_set_incomplete",
    ):
        publish_document_processing_evidence(
            conn,
            document_version_id="pdf-ocr-v1",
            processing_lane="pdf_ocr",
            cutoff_at=T1,
            recorded_at=T2,
        )
    locator = EvidenceLocator(source_ref="report.pdf", page_number=2)
    conn.execute(
        "INSERT INTO ocr_page_results ("
        "extraction_run_id,page_number,node_id,outcome,output_sha256,"
        "mean_confidence,locator_json,locator_sha256,reason_code,"
        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "pdf-ocr-run",
            2,
            nodes[2].node_id,
            "accepted",
            _sha(nodes[2].text),
            99.0,
            locator.canonical_json,
            locator.canonical_sha256,
            None,
            T0,
        ),
    )
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="pdf-ocr-v1",
        processing_lane="pdf_ocr",
        cutoff_at=T1,
        recorded_at=T2,
    )
    assert receipt.member_count == 2
    assert (
        verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id="pdf-ocr-v1",
            processing_lane="pdf_ocr",
            cutoff_at=T1,
        ).member_set_sha256
        == receipt.member_set_sha256
    )


def test_image_ocr_closes_exact_governance_model_and_output(
    conn: sqlite3.Connection,
) -> None:
    blob = _seed_document(conn, document_version_id="image-ocr-v1", media_type="image/png")
    _seed_image_ocr_governance(
        conn,
        document_version_id="image-ocr-v1",
        blob_sha=blob,
        run_id="image-ocr-run",
    )
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="image-ocr-v1",
        processing_lane="image_ocr",
        cutoff_at=T1,
        recorded_at=T2,
    )
    assert receipt.member_count == 1
    member = conn.execute(
        "SELECT native_commitment_json FROM "
        "document_processing_evidence_members WHERE evidence_seal_id=?",
        (receipt.evidence_seal_id,),
    ).fetchone()
    assert member is not None
    assert "governance_sha256" in str(member[0])


def test_member_omission_reorder_extra_and_native_loss_are_blocked_or_detected(
    conn: sqlite3.Connection,
) -> None:
    blob = _seed_document(conn, document_version_id="html-v1", media_type="text/html")
    nodes = _seed_run(
        conn,
        document_version_id="html-v1",
        blob_sha=blob,
        run_id="html-run",
        extractor_name=STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.name,
        extractor_code_version=(STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.code_version),
        extractor_config_sha256=(STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.config_sha256),
        children=(
            (
                "section",
                "Revenue",
                EvidenceLocator(source_ref="report.html", char_start=0, char_end=7),
            ),
            (
                "passage",
                "Revenue grew.",
                EvidenceLocator(source_ref="report.html", char_start=8, char_end=21),
            ),
        ),
    )
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="html-v1",
        processing_lane="html_native_hierarchy",
        cutoff_at=T1,
        recorded_at=T2,
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE document_processing_evidence_members "
            "SET member_ordinal=9 WHERE evidence_seal_id=? AND member_ordinal=0",
            (receipt.evidence_seal_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        conn.execute(
            "INSERT INTO document_processing_evidence_members SELECT "
            "evidence_seal_id,99,native_table,native_id||':extra',"
            "native_parent_id,locator_json,locator_sha256,content_sha256,"
            "native_commitment_json,native_commitment_sha256,"
            "canonical_member_json,member_sha256,native_knowledge_at,"
            "native_recorded_at FROM document_processing_evidence_members "
            "WHERE evidence_seal_id=? AND member_ordinal=0",
            (receipt.evidence_seal_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="run is sealed"):
        conn.execute(
            "INSERT INTO evidence_nodes SELECT "
            "'late-node','late-node',1,extraction_run_id,node_id,NULL,"
            "'passage','late',locator_json,locator_sha256,recorded_at "
            "FROM evidence_nodes WHERE node_id=?",
            (nodes[1].node_id,),
        )
    conn.execute("DROP TRIGGER trg_evidence_nodes_append_only_delete")
    conn.execute("DROP TRIGGER trg_evidence_nodes_processing_evidence_frozen")
    conn.execute("DELETE FROM evidence_nodes WHERE node_id=?", (nodes[2].node_id,))
    with pytest.raises(
        DocumentProcessingEvidenceIntegrityError,
        match="native_extraction_output_commitment_mismatch",
    ):
        verify_document_processing_evidence(
            conn,
            receipt.evidence_seal_id,
            document_version_id="html-v1",
            processing_lane="html_native_hierarchy",
            cutoff_at=T1,
        )


def test_html_lane_verifies_legacy_ascii_escaped_unicode_output_commitment(
    conn: sqlite3.Connection,
) -> None:
    blob = _seed_document(conn, document_version_id="html-unicode", media_type="text/html")
    _seed_run(
        conn,
        document_version_id="html-unicode",
        blob_sha=blob,
        run_id="html-unicode-run",
        extractor_name=STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.name,
        extractor_code_version=STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.code_version,
        extractor_config_sha256=STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.config_sha256,
        children=(
            (
                "passage",
                "Revenue grew — café expansion continued.",
                EvidenceLocator(source_ref="report.html", char_start=0, char_end=41),
            ),
        ),
        legacy_ascii_output=True,
    )

    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="html-unicode",
        processing_lane="html_native_hierarchy",
        cutoff_at=T1,
        recorded_at=T2,
    )

    assert receipt.member_count == 1
    assert receipt.exact_replay is False


def test_backdated_new_run_cannot_change_old_pinned_seal(
    conn: sqlite3.Connection,
) -> None:
    blob = _seed_document(conn, document_version_id="html-stable", media_type="text/html")
    _seed_run(
        conn,
        document_version_id="html-stable",
        blob_sha=blob,
        run_id="html-stable-run",
        extractor_name=STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.name,
        extractor_code_version=(STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.code_version),
        extractor_config_sha256=(STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.config_sha256),
        children=(
            (
                "passage",
                "Original",
                EvidenceLocator(source_ref="report.html", char_start=0, char_end=8),
            ),
        ),
    )
    receipt = publish_document_processing_evidence(
        conn,
        document_version_id="html-stable",
        processing_lane="html_native_hierarchy",
        cutoff_at=T1,
        recorded_at=T2,
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "backdated-other-run",
            "backdated-other-run",
            "html-stable",
            blob,
            STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.name,
            "f" * 64,
            STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.code_version,
            "e" * 64,
            T0,
            T0,
            "succeeded",
        ),
    )
    verified = verify_document_processing_evidence(
        conn,
        receipt.evidence_seal_id,
        document_version_id="html-stable",
        processing_lane="html_native_hierarchy",
        cutoff_at=T1,
    )
    assert verified.member_set_sha256 == receipt.member_set_sha256


def test_member_cap_fails_before_any_publication_rows(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("provenance.document_processing_evidence._MAX_NATIVE_MEMBERS", 2)
    blob = _seed_document(conn, document_version_id="oversized-html", media_type="text/html")
    _seed_run(
        conn,
        document_version_id="oversized-html",
        blob_sha=blob,
        run_id="oversized-html-run",
        extractor_name=STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.name,
        extractor_code_version=(STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.code_version),
        extractor_config_sha256=(STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.config_sha256),
        children=(
            (
                "passage",
                "one",
                EvidenceLocator(source_ref="x.html", char_start=0, char_end=3),
            ),
            (
                "passage",
                "two",
                EvidenceLocator(source_ref="x.html", char_start=4, char_end=7),
            ),
            (
                "passage",
                "three",
                EvidenceLocator(source_ref="x.html", char_start=8, char_end=13),
            ),
        ),
    )
    with pytest.raises(
        DocumentProcessingEvidenceMissingError,
        match="native_member_limit_exceeded",
    ):
        publish_document_processing_evidence(
            conn,
            document_version_id="oversized-html",
            processing_lane="html_native_hierarchy",
            cutoff_at=T1,
            recorded_at=T2,
        )
    assert (
        conn.execute("SELECT COUNT(*) FROM document_processing_evidence_headers").fetchone()[0] == 0
    )
