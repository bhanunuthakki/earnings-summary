# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable

import fitz
import pytest
from pydantic import ValidationError

import provenance.pdf_table_extraction as extraction
from provenance.pdf_table_extraction import (
    PdfTableDependencyError,
    PdfTableExtractionArtifact,
    PdfTableExtractionConfig,
    extract_pdf_tables,
)


def _document_bytes(
    builder: Callable[[fitz.Document], None],
) -> bytes:
    document = fitz.open()
    builder(document)
    raw = document.tobytes(garbage=4, deflate=True)
    document.close()
    return raw


def _add_table_page(
    document: fitz.Document,
    *,
    ruled: bool,
    rotation: int = 0,
    blank_cell: bool = False,
) -> None:
    page = document.new_page(width=320, height=220)
    xs = (30, 130, 230)
    ys = (40, 80, 120, 160)
    values = (
        ("Metric", "Q1", "Q2"),
        ("Revenue", "10", "12"),
        ("Margin", "40", "" if blank_cell else "42"),
        ("FCF", "7", "8"),
    )
    for y, row in zip(ys, values, strict=True):
        for x, value in zip(xs, row, strict=True):
            if value:
                page.insert_text((x, y), value)
    if ruled:
        shape = page.new_shape()
        for x in (20, 120, 220, 300):
            shape.draw_line((x, 20), (x, 180))
        for y in (20, 60, 100, 140, 180):
            shape.draw_line((20, y), (300, y))
        shape.finish()
        shape.commit()
    if rotation:
        page.set_rotation(rotation)


def _ruled_pdf(*, rotation: int = 0, blank_cell: bool = False) -> bytes:
    return _document_bytes(
        lambda document: _add_table_page(
            document,
            ruled=True,
            rotation=rotation,
            blank_cell=blank_cell,
        )
    )


def _borderless_pdf() -> bytes:
    return _document_bytes(lambda document: _add_table_page(document, ruled=False))


def test_ruled_table_dual_detector_dedupes_and_preserves_empty_cells() -> None:
    artifact = extract_pdf_tables(_ruled_pdf(blank_cell=True))

    assert artifact.disposition == "sealed"
    page = artifact.pages[0]
    assert page.disposition == "tables_detected"
    assert len(page.tables) == 1
    table = page.tables[0]
    assert table.detected_by == ("lines", "text")
    assert table.row_count == 4
    assert table.column_count == 3
    assert table.rows[2].cells[2].text == ""
    assert table.rows[2].cells[2].bbox is not None
    assert len(table.canonical_sha256) == 64
    assert len(table.rows[0].cells[0].canonical_sha256) == 64


def test_borderless_text_table_is_detected_without_claiming_semantic_exhaustiveness() -> None:
    artifact = extract_pdf_tables(_borderless_pdf())

    assert artifact.disposition == "sealed"
    table = artifact.pages[0].tables[0]
    assert table.detected_by == ("text",)
    assert table.column_count == 3
    assert [cell.text for cell in table.rows[0].cells] == ["Metric", "Q1", "Q2"]
    # PyMuPDF's pinned text policy emits inter-line empty rows.  They are
    # position-bearing detector output, not normalized away.
    assert any(all(cell.text == "" for cell in row.cells) for row in table.rows)


def test_no_table_page_has_an_explicit_terminal_disposition() -> None:
    def build(document: fitz.Document) -> None:
        page = document.new_page()
        page.insert_text((72, 72), "Narrative disclosure with no tabular layout.")

    artifact = extract_pdf_tables(_document_bytes(build))

    assert artifact.disposition == "sealed"
    assert artifact.pages[0].disposition == "no_tables_detected"
    assert artifact.pages[0].tables == ()
    assert artifact.pages[0].quarantine_reason is None


def test_rotated_page_records_visual_coordinate_context() -> None:
    artifact = extract_pdf_tables(_ruled_pdf(rotation=90))

    page = artifact.pages[0]
    assert page.disposition == "tables_detected"
    assert page.rotation_degrees == 90
    assert page.coordinate_space == "pymupdf-rotated-page-points"
    assert page.page_bbox.x1 == page.media_box.y1
    assert page.page_bbox.y1 == page.media_box.x1
    assert page.rotation_matrix != page.derotation_matrix
    assert page.tables[0].bbox.x1 <= page.page_bbox.x1
    assert page.tables[0].bbox.y1 <= page.page_bbox.y1


def test_image_only_page_quarantines_instead_of_claiming_no_table() -> None:
    def build(document: fitz.Document) -> None:
        page = document.new_page(width=200, height=200)
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
        pixmap.clear_with(255)
        page.insert_image((20, 20, 180, 180), pixmap=pixmap)

    artifact = extract_pdf_tables(_document_bytes(build))

    assert artifact.disposition == "quarantined"
    assert artifact.quarantine_reason == "one_or_more_pages_quarantined"
    assert artifact.pages[0].disposition == "quarantined"
    assert artifact.pages[0].quarantine_reason == "image_only_page"


def test_scanned_page_with_hidden_text_layer_still_quarantines() -> None:
    def build(document: fitz.Document) -> None:
        page = document.new_page(width=200, height=200)
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200), False)
        pixmap.clear_with(255)
        page.insert_image((0, 0, 200, 200), pixmap=pixmap)
        page.insert_text((20, 20), "OCR text layer")

    artifact = extract_pdf_tables(_document_bytes(build))

    assert artifact.disposition == "quarantined"
    assert artifact.pages[0].quarantine_reason == "scanned_page"


def test_malformed_pdf_returns_stable_document_quarantine() -> None:
    raw = b"%PDF-not-a-valid-document"

    artifact = extract_pdf_tables(raw)

    assert artifact.disposition == "quarantined"
    assert artifact.quarantine_reason == "malformed_pdf"
    assert artifact.pdf_page_count is None
    assert artifact.pages == ()
    assert artifact.raw_pdf_sha256 == hashlib.sha256(raw).hexdigest()


def test_page_cell_and_serialized_output_caps_fail_closed() -> None:
    def build_two_pages(document: fitz.Document) -> None:
        _add_table_page(document, ruled=True)
        _add_table_page(document, ruled=True)

    two_pages = _document_bytes(build_two_pages)
    page_capped = extract_pdf_tables(
        two_pages,
        config=PdfTableExtractionConfig(maximum_pages=1),
    )
    assert page_capped.quarantine_reason == "page_limit_exceeded"
    assert page_capped.pages == ()

    cell_capped = extract_pdf_tables(
        _ruled_pdf(),
        config=PdfTableExtractionConfig(maximum_cells_per_table=4),
    )
    assert cell_capped.disposition == "quarantined"
    assert cell_capped.pages[0].quarantine_reason == "cells_per_table_limit_exceeded"

    byte_capped = extract_pdf_tables(
        _borderless_pdf(),
        config=PdfTableExtractionConfig(maximum_serialized_bytes=4_096),
    )
    assert byte_capped.quarantine_reason == "serialized_output_limit_exceeded"
    assert byte_capped.pages == ()
    assert len(byte_capped.model_dump_json().encode("utf-8")) <= 4_096


def test_exact_bytes_replay_to_byte_identical_artifact_and_seal() -> None:
    raw = _ruled_pdf()

    first = extract_pdf_tables(raw)
    second = extract_pdf_tables(raw)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.ordered_page_table_seal_sha256 == second.ordered_page_table_seal_sha256
    assert first.detector.configuration_sha256 == second.detector.configuration_sha256
    assert first.raw_pdf_sha256 == hashlib.sha256(raw).hexdigest()


def test_closed_models_reject_extra_fields_and_commitment_tampering() -> None:
    artifact = extract_pdf_tables(_ruled_pdf())
    payload = artifact.model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        PdfTableExtractionArtifact.model_validate(payload)

    tampered = artifact.model_dump(mode="json")
    tampered["pages"][0]["tables"][0]["rows"][0]["cells"][0]["text"] = "tampered"
    with pytest.raises(ValidationError, match="cell commitment mismatch"):
        PdfTableExtractionArtifact.model_validate(tampered)


def test_overlapping_cross_strategy_disagreement_quarantines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def conflicting_detector(
        _page: object,
        strategy: extraction.DetectorStrategy,
        _config: PdfTableExtractionConfig,
    ) -> tuple[extraction._RawTable, ...]:
        text = "left" if strategy == "lines" else "right"
        return (
            extraction._RawTable(
                strategy=strategy,
                bbox=(20.0, 20.0, 200.0, 100.0),
                rows=(
                    (
                        extraction._RawCell(
                            bbox=(20.0, 20.0, 200.0, 100.0),
                            text=text,
                        ),
                    ),
                ),
                detected_by=(strategy,),
            ),
        )

    monkeypatch.setattr(extraction, "_detect_strategy", conflicting_detector)

    artifact = extract_pdf_tables(_ruled_pdf())

    assert artifact.disposition == "quarantined"
    assert artifact.pages[0].disposition == "quarantined"
    assert artifact.pages[0].quarantine_reason == "detector_disagreement"
    assert artifact.pages[0].tables == ()


def test_optional_dependency_failure_is_typed_and_import_is_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = importlib.import_module

    def missing_fitz(name: str, package: str | None = None) -> object:
        if name == "fitz":
            raise ImportError("not installed")
        return original_import(name, package)

    monkeypatch.setattr(importlib, "import_module", missing_fitz)

    with pytest.raises(PdfTableDependencyError, match="optional PyMuPDF"):
        extract_pdf_tables(_ruled_pdf())
