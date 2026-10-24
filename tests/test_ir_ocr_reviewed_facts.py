"""An OCR candidate needs explicit visual review and present source bytes on read."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from provenance.document_processing_evidence import GovernedOCRPageProjection
from provenance.fact_read_model import FactAdmissionError, FactReadModel
from provenance.ir_reported_facts import (
    IrCandidateRetentionRequest,
    IrOCRContextRegion,
    IrOCRRawRow,
    IrOCRRetentionSelection,
    IrOCRReviewedCandidate,
    IrOCRReviewWitness,
    IrOCRSourceContextReview,
    IrOCRVisualPage,
    IrRawExchange,
    IrReportedPublicationRequest,
    persist_ir_source_context_review,
    publish_ir_reported_facts,
    retain_ir_pdf_candidates,
)
from provenance.ocr_layout import OCRPageLayout, OCRWord
from provenance.source_read_context import SourceReadContext
from tests.test_ir_reported_facts import (
    PDF_BYTES,
    STAMP,
    add_ir_document,
    candidate,
    payload_sha,
    seed_foundation,
    sha,
)

TEXT = "Margin North 10% Apr 1 Jun 30 2026 Q2 management segment North"
QUOTE = "Margin North 10%"
PERIOD = "Apr 1 Jun 30 2026 Q2"
SCOPE = "segment North"


def _retention_selection(projection: GovernedOCRPageProjection) -> IrOCRRetentionSelection:
    return IrOCRRetentionSelection(
        document_version_id=projection.document_version_id,
        source_sha256=projection.source_sha256,
        evidence_seal_id=projection.evidence_seal_id,
        extraction_run_id=projection.extraction_run_id,
        page_result_ids=(projection.page_result_id,),
        cutoff_at=STAMP,
    )


@pytest.fixture
def conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    db_path = migrated_db(tmp_path / "ocr-reviewed.db")
    database = sqlite3.connect(db_path)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    seed_foundation(database)
    database.commit()
    add_ir_document(database)
    try:
        yield database
    finally:
        database.close()


def _projection_and_request() -> tuple[
    GovernedOCRPageProjection, IrCandidateRetentionRequest, tuple[IrOCRContextRegion, ...]
]:
    words: list[OCRWord] = []
    cursor = 0
    for ordinal, token in enumerate(TEXT.split()):
        start = TEXT.index(token, cursor)
        words.append(
            OCRWord(
                ordinal=ordinal,
                block=1,
                paragraph=1,
                line=ordinal // 7 + 1,
                word=ordinal % 7 + 1,
                text=token,
                confidence=95,
                left=10 + ordinal % 7 * 40,
                top=12 + ordinal // 7 * 20,
                width=35,
                height=15,
                char_start=start,
                char_end=start + len(token),
            )
        )
        cursor = start + len(token)
    layout = OCRPageLayout(
        width=300,
        height=300,
        dpi=300,
        media_box=(0.0, 0.0, 72.0, 72.0),
        rotation=0,
        raster_to_pdf=(0.24, 0.0, 0.0, 0.0, -0.24, 72.0),
        tsv_sha256="a" * 64,
        render_sha256="b" * 64,
        words=tuple(words),
    )
    projection = GovernedOCRPageProjection(
        document_version_id="ir-document-v1",
        source_sha256=sha(PDF_BYTES),
        evidence_seal_id="seal-1",
        extraction_run_id="ocr-run-1",
        page_number=1,
        page_result_id="page-result-1",
        page_node_id="page-node-1",
        text=TEXT,
        text_sha256=sha(TEXT),
        layout_node_id="layout-node-1",
        layout_sha256=sha(layout.model_dump_json()),
        layout_json=layout.model_dump_json(),
        engine_config_sha256="c" * 64,
    )

    def region(quote: str, fields: tuple[str, ...]) -> IrOCRContextRegion:
        start = TEXT.index(quote)
        end = start + len(quote)
        return IrOCRContextRegion(
            page_number=1,
            exact_quote=quote,
            field_names=fields,
            char_start=start,
            char_end=end,
            words=tuple(word for word in words if word.char_start < end and word.char_end > start),
        )

    contexts = (
        region(
            PERIOD,
            ("reported_period_start", "reported_period_end", "period_kind", "source_column_header"),
        ),
        region("management", ("accounting_basis",)),
        region(SCOPE, ("consolidation_scope", "dimensions")),
    )
    raw_row: dict[str, object] = {
        "label": "Margin",
        "source_row_label": "Margin",
        "source_excerpt": QUOTE,
        "source_value_text": "10%",
        "period_role": "current",
        "range_bound": None,
        "context_witnesses": [
            {"page": 1, "quote": item.exact_quote, "fields": list(item.field_names)}
            for item in contexts
        ],
    }
    raw_response = json.dumps([raw_row])
    response = IrRawExchange(
        raw_response=raw_response,
        response_sha256=sha(raw_response),
        prompt_sha256="d" * 64,
        model="fixture-model",
        provider="fixture-provider",
        transport="fixture-transport",
        llm_call_id=1,
    )
    row = IrOCRRawRow(
        page_number=1,
        response_sha256=response.response_sha256,
        exchange_sha256=response.exchange_sha256,
        response_row_ordinal=0,
        raw_row=raw_row,
        ocr_source_quote=QUOTE,
        ocr_char_start=0,
        ocr_char_end=len(QUOTE),
        words=tuple(words[:3]),
        ocr_page_manifest={
            "source_representation": "governed_ocr",
            "document_version_id": projection.document_version_id,
            "source_sha256": projection.source_sha256,
            "evidence_seal_id": projection.evidence_seal_id,
            "extraction_run_id": projection.extraction_run_id,
            "page_result_id": projection.page_result_id,
            "page_node_id": projection.page_node_id,
            "layout_node_id": projection.layout_node_id,
            "layout_sha256": projection.layout_sha256,
            "text_sha256": projection.text_sha256,
            "engine_config_sha256": projection.engine_config_sha256,
            "tsv_sha256": layout.tsv_sha256,
            "render_sha256": layout.render_sha256,
            "coordinate_space": layout.version,
            "cutoff_at": STAMP.isoformat(),
        },
        context_regions=contexts,
    )
    request = IrCandidateRetentionRequest(
        document_version_id="ir-document-v1",
        source_sha256=sha(PDF_BYTES),
        extractor_name="ir-supplement-ocr-continuation",
        extractor_code_version="ir-supplement-ocr-continuation-v1-candidates",
        extractor_config_sha256="e" * 64,
        extraction_started_at=STAMP,
        extraction_completed_at=STAMP,
        responses=(response,),
        rows=(row,),
    )
    return projection, request, contexts


def _review(
    population: object,
    projection: GovernedOCRPageProjection,
    contexts: tuple[IrOCRContextRegion, ...],
) -> IrOCRSourceContextReview:
    from provenance.ir_reported_facts import IrCandidatePopulation

    assert isinstance(population, IrCandidatePopulation)
    fact = candidate(QUOTE, "10%", "10").model_copy(
        update={"source_row_key": population.candidate_node_ids[0]}
    )
    main = IrOCRContextRegion(
        page_number=1,
        exact_quote=QUOTE,
        field_names=(),
        char_start=0,
        char_end=len(QUOTE),
        words=OCRPageLayout.model_validate_json(projection.layout_json).words[:3],
    )
    region_for_field = {
        "value": main,
        "source_definition": main,
        "period_kind": contexts[0],
        "period_start": contexts[0],
        "period_end": contexts[0],
        "fiscal_year": contexts[0],
        "fiscal_period": contexts[0],
        "unit_key": main,
        "source_unit_scale": main,
        "accounting_basis": contexts[1],
        "consolidation_scope": contexts[2],
        "dimensions": contexts[2],
    }
    entry = IrOCRReviewedCandidate(
        source_representation="governed_ocr",
        candidate_node_id=population.candidate_node_ids[0],
        disposition="accepted",
        fact=fact,
        visual_form="table",
        witnesses=tuple(
            IrOCRReviewWitness.model_validate(
                {"field_name": field, "region": region, "review_note": f"Visually checked {field}"}
            )
            for field, region in region_for_field.items()
        ),
    )
    payload = {
        "schema_version": "ir-pdf-source-context-review-ocr.v1",
        "document_version_id": population.document_version_id,
        "source_sha256": population.source_sha256,
        "extraction_run_id": population.extraction_run_id,
        "extractor_config_sha256": population.extractor_config_sha256,
        "candidate_population_sha256": population.candidate_population_sha256,
        "ocr_selection": _retention_selection(projection).model_dump(mode="json"),
        "visual_pages": [
            IrOCRVisualPage(
                page_number=1,
                render_sha256="b" * 64,
                tsv_sha256="a" * 64,
                layout_sha256=projection.layout_sha256,
            ).model_dump(mode="json")
        ],
        "reviewer_identity": "fixture-analyst",
        "reviewer_kind": "agent_analyst",
        "review_method": "visual_source_context_review",
        "reviewed_at": (STAMP + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "review_evidence": "Synthetic visual-region fixture review",
        "entries": [entry.model_dump(mode="json")],
    }
    return IrOCRSourceContextReview.model_validate(
        {**payload, "review_sha256": payload_sha(payload)}
    )


def test_ocr_review_requires_representation_tag() -> None:
    with pytest.raises(ValidationError):
        IrOCRReviewedCandidate.model_validate(
            {"candidate_node_id": "candidate", "disposition": "rejected", "reason_code": "no"}
        )


def test_reviewed_ocr_fact_requires_source_context_on_read(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    projection, request, contexts = _projection_and_request()

    def selected(*_args: object, **_kwargs: object) -> tuple[GovernedOCRPageProjection, ...]:
        return (projection,)

    def native_pages(_raw: bytes) -> tuple[str, ...]:
        return (TEXT,)

    monkeypatch.setattr(
        "provenance.ir_reported_facts.resolve_governed_ocr_pages",
        selected,
    )
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", native_pages)
    population = retain_ir_pdf_candidates(
        conn,
        request,
        raw_pdf_bytes=PDF_BYTES,
        page_projection=native_pages,
        ocr_repo_root=tmp_path,
        ocr_selection=_retention_selection(projection),
    )
    review = _review(population, projection, contexts)
    review_id = persist_ir_source_context_review(
        conn, review, population, raw_pdf_bytes=PDF_BYTES, ocr_repo_root=tmp_path
    )
    fact = review.entries[0].fact
    assert fact is not None
    publication = IrReportedPublicationRequest(
        document_version_id=population.document_version_id,
        source_sha256=population.source_sha256,
        subject_binding_revision_id="binding-1",
        reporting_entity_id="reporting-1",
        concept_namespace="urn:fixture:issuer-source-label",
        extractor_name=request.extractor_name,
        extractor_code_version=request.extractor_code_version,
        extractor_config_sha256=request.extractor_config_sha256,
        candidate_population_sha256=population.candidate_population_sha256,
        review_node_id=review_id,
        extraction_started_at=STAMP,
        extraction_completed_at=STAMP,
        knowledge_at=STAMP + timedelta(hours=2),
        recorded_at=STAMP + timedelta(hours=2),
        candidates=(fact,),
    )
    receipt = publish_ir_reported_facts(
        conn,
        publication,
        raw_pdf_bytes=PDF_BYTES,
        ocr_repo_root=tmp_path,
        ocr_selection=_retention_selection(projection),
    )
    observation_id = receipt.observation_ids[0]
    with pytest.raises(FactAdmissionError, match="ir_ocr_source_context_unavailable"):
        FactReadModel(conn).provenance_bundle(observation_id, cutoff=publication.recorded_at)

    def retained_bytes(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(raw_bytes=PDF_BYTES)

    monkeypatch.setattr(
        "provenance.ir_reported_facts.read_verified_local_evidence_bytes",
        retained_bytes,
    )
    context = SourceReadContext.for_product_state_root(tmp_path)
    admitted = FactReadModel(conn, source_context=context).provenance_bundle(
        observation_id, cutoff=publication.recorded_at
    )
    assert admitted.observation.observation_id == observation_id
    assert admitted.observation.observation_id.startswith("ir-pdf-ocr-observation:")
    assert admitted.evidence is not None
    geometry = admitted.evidence.source_locator.root["ocr_page_geometry"]
    assert isinstance(geometry, dict)
    assert cast("dict[str, object]", geometry)["raster_to_pdf"] == [
        0.24,
        0.0,
        0.0,
        0.0,
        -0.24,
        72.0,
    ]
    wrong = projection.model_copy(update={"text": TEXT.replace("10%", "11%")})

    def changed(*_args: object, **_kwargs: object) -> tuple[GovernedOCRPageProjection, ...]:
        return (wrong,)

    monkeypatch.setattr(
        "provenance.ir_reported_facts.resolve_governed_ocr_pages",
        changed,
    )
    with pytest.raises(FactAdmissionError, match="ir_ocr_source_reconstruction_failed"):
        FactReadModel(conn, source_context=context).provenance_bundle(
            observation_id, cutoff=publication.recorded_at
        )
