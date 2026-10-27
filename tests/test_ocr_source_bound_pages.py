"""Image quarantine selection preserves native assessments and financial admission."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import fitz
import pytest
from pydantic import ValidationError

from provenance.document_processing_evidence import (
    DocumentProcessingEvidenceIntegrityError,
    read_pdf_table_artifact,
)
from provenance.ocr_extraction import (
    OCRBackfillRequest,
    OCRPageOutput,
    OCRPageSelection,
    OCRProviderError,
    PDFPreflight,
    PDFPreflightPage,
    PypdfPDFInspector,
    backfill_ocr_evidence,
)
from provenance.pdf_table_extraction import PdfTableExtractionArtifact
from runtime.deterministic import content_sha256
from sqlite_runtime import register_sqlite_integrity_functions
from tests import test_document_processing_evidence as table_fixtures
from tests import test_ocr_extraction_governance as ocr_fixtures
from tests import test_pdf_table_evidence_backfill as document_fixtures

SelectedDocument = tuple[
    sqlite3.Connection, OCRBackfillRequest, OCRPageSelection, PdfTableExtractionArtifact
]


@pytest.fixture
def selected_document(
    tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> Iterator[SelectedDocument]:
    db = migrated_db(tmp_path / "fixture.db")
    document = fitz.open()
    # Image chart positive and title-only negative have the same structural signal.
    # Selection is explicit; the detector does not classify financial content.
    for title in (
        "Trailing revenues by geography chart with native heading only",
        "Supplemental information section divider with native heading only",
    ):
        page = document.new_page(width=320, height=220)
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
        pixmap.clear_with(255)
        page.insert_image((0, 0, 320, 220), pixmap=pixmap)
        page.insert_text((10, 20), title, fontsize=6)
    raw = document.tobytes(garbage=4, deflate=True)
    document.close()
    root = tmp_path / "repo"
    getattr(document_fixtures, "_seed_document")(db, root / "blobs", "image-deck", raw)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    artifact, artifact_id = getattr(
        table_fixtures, "_seed_pdf_table_artifact_for_existing_document"
    )(
        conn,
        document_version_id="image-deck",
        raw_pdf_bytes=raw,
    )
    conn.commit()
    assert all(page.quarantine_reason == "scanned_page" for page in artifact.pages)
    assert (
        PypdfPDFInspector().inspect(raw, minimum_native_characters=32).outcome
        == "native_sufficient"
    )
    selection = OCRPageSelection(
        document_version_id="image-deck",
        source_sha256=artifact.raw_pdf_sha256,
        artifact_id=artifact_id,
        artifact_sha256=content_sha256(artifact.model_dump(mode="json")),
        page_number=1,
        page_sha256=content_sha256(artifact.pages[0].model_dump(mode="json")),
    )
    request = OCRBackfillRequest(
        repo_root=root, source_lane="evidence_native", document_version_ids=("image-deck",)
    )
    try:
        yield conn, request, selection, artifact
    finally:
        conn.close()


def test_native_title_does_not_block_explicit_image_page_and_negative_is_not_selected(
    selected_document: SelectedDocument,
) -> None:
    conn, request, selection, artifact = selected_document
    old = backfill_ocr_evidence(conn, request.model_copy(update={"apply": True}))
    assert old.documents_native_sufficient == 1
    before = conn.execute("SELECT * FROM ocr_document_assessments").fetchall()
    provider = getattr(ocr_fixtures, "_Provider")(
        [OCRPageOutput(page_number=1, text="Chart label 15%", mean_confidence=99)]
    )
    targeted = request.model_copy(update={"page_selections": (selection,)})
    dry = backfill_ocr_evidence(conn, targeted, provider=provider)
    assert dry.pages_requiring_ocr == 1
    assert dry.verified_page_selections == [selection]
    assert provider.calls == []
    assert conn.execute("SELECT * FROM ocr_document_assessments").fetchall() == before
    applied = backfill_ocr_evidence(
        conn, targeted.model_copy(update={"apply": True}), provider=provider
    )
    assert applied.pages_ocr_accepted == 1
    assert provider.calls == [(1,)]
    config_json = conn.execute(
        "SELECT engine_config_json FROM ocr_extraction_governance"
    ).fetchone()[0]
    assert json.loads(config_json)["page_selections"] == [selection.model_dump(mode="json")]
    assert json.loads(config_json)["page_selection_policy"] == "pinned-image-quarantine@1"
    assert [
        row[0]
        for row in conn.execute("SELECT outcome FROM ocr_document_assessments ORDER BY outcome")
    ] == ["native_sufficient", "ocr_required"]
    assert (
        conn.execute("SELECT COUNT(*) FROM ocr_preflight_pages WHERE requires_ocr=1").fetchone()[0]
        == 1
    )
    replay = backfill_ocr_evidence(
        conn, targeted.model_copy(update={"apply": True}), provider=provider
    )
    assert replay.documents_skipped_covered == 1
    assert provider.calls == [(1,)]
    assert (
        read_pdf_table_artifact(
            conn,
            document_version_id="image-deck",
            artifact_id=selection.artifact_id,
            cutoff_at=datetime.now(UTC),
        )
        == artifact
    )
    assert artifact.disposition == "quarantined"
    assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_sha256", "a" * 64),
        ("artifact_sha256", "b" * 64),
        ("page_sha256", "c" * 64),
        ("page_number", 3),
        ("artifact_id", "missing"),
    ],
)
def test_selection_binding_fails_before_provider_or_assessment(
    selected_document: SelectedDocument, field: str, value: str | int
) -> None:
    conn, request, selection, _ = selected_document
    provider = getattr(ocr_fixtures, "_Provider")()
    changed = selection.model_copy(update={field: value})
    with pytest.raises((OCRProviderError, RuntimeError)):
        backfill_ocr_evidence(
            conn,
            request.model_copy(update={"apply": True, "page_selections": (changed,)}),
            provider=provider,
        )
    assert provider.calls == []
    assert conn.execute("SELECT COUNT(*) FROM ocr_document_assessments").fetchone()[0] == 0


@pytest.mark.parametrize(
    "table,column",
    [
        ("pdf_table_extraction_artifact_members", "canonical_object_sha256"),
        ("pdf_table_extraction_artifact_seals", "member_set_sha256"),
        ("pdf_table_extraction_artifact_headers", "artifact_sha256"),
    ],
)
def test_existing_inventory_verification_rejects_corruption(
    selected_document: SelectedDocument, table: str, column: str
) -> None:
    conn, request, selection, _ = selected_document
    # Corruption fixture deliberately bypasses append-only triggers, never production code.
    triggers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall()
    for (name,) in triggers:
        conn.execute(f'DROP TRIGGER "{name}"')
    conn.execute(f"UPDATE {table} SET {column}=?", ("f" * 64,))
    conn.commit()
    with pytest.raises(DocumentProcessingEvidenceIntegrityError):
        backfill_ocr_evidence(conn, request.model_copy(update={"page_selections": (selection,)}))


def test_selected_provider_failure_retains_unresolved_page(
    selected_document: SelectedDocument,
) -> None:
    conn, request, selection, _ = selected_document
    provider = getattr(ocr_fixtures, "_Provider")(failure_reason="local_engine_failed")
    result = backfill_ocr_evidence(
        conn,
        request.model_copy(update={"apply": True, "page_selections": (selection,)}),
        provider=provider,
    )
    assert result.documents_ocr_failed == 1
    assert result.pages_ocr_accepted == 0
    assert provider.calls == [(1,)]
    assert (
        conn.execute("SELECT outcome FROM ocr_document_assessments").fetchone()[0] == "ocr_required"
    )


def test_selection_requires_explicit_unique_target(tmp_path: Path) -> None:
    selection = OCRPageSelection(
        document_version_id="d",
        source_sha256="a" * 64,
        artifact_id="a",
        artifact_sha256="b" * 64,
        page_number=1,
        page_sha256="c" * 64,
    )
    with pytest.raises(ValidationError):
        OCRBackfillRequest(repo_root=tmp_path, page_selections=(selection,))
    with pytest.raises(ValidationError):
        OCRBackfillRequest(
            repo_root=tmp_path,
            source_lane="evidence_native",
            document_version_ids=("d",),
            page_selections=(selection, selection),
        )


def test_cli_keeps_explicit_selection_and_never_constructs_engine_in_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution import backfill_ocr_evidence as cli

    selection = OCRPageSelection(
        document_version_id="d",
        source_sha256="a" * 64,
        artifact_id="a",
        artifact_sha256="b" * 64,
        page_number=19,
        page_sha256="c" * 64,
    )
    captured: list[OCRBackfillRequest] = []

    class Connection:
        def close(self) -> None:
            pass

    class Result:
        def model_dump_json(self) -> str:
            return "{}"

    def backfill(conn: object, request: OCRBackfillRequest, *, provider: object) -> Result:
        assert isinstance(conn, Connection)
        assert provider is None
        captured.append(request)
        return Result()

    def connect(*args: object, **kwargs: object) -> Connection:
        return Connection()

    monkeypatch.setattr(cli, "connect_sqlite", connect)
    monkeypatch.setattr(cli, "backfill_ocr_evidence", backfill)
    assert (
        cli.main(
            [
                "--db",
                str(tmp_path / "synthetic.db"),
                "--source-lane",
                "evidence_native",
                "--document-version-id",
                "d",
                "--page-selection",
                selection.model_dump_json(),
            ]
        )
        == 0
    )
    assert len(captured) == 1
    assert captured[0].page_selections == (selection,)
    assert captured[0].apply is False


def test_selected_apply_requires_provider_before_assessment(
    selected_document: SelectedDocument,
) -> None:
    conn, request, selection, _ = selected_document
    with pytest.raises(OCRProviderError, match="selected_ocr_requires_provider"):
        backfill_ocr_evidence(
            conn, request.model_copy(update={"apply": True, "page_selections": (selection,)})
        )
    assert conn.execute("SELECT COUNT(*) FROM ocr_document_assessments").fetchone()[0] == 0


def test_selection_supplements_existing_low_text_pages(selected_document: SelectedDocument) -> None:
    conn, request, selection, _ = selected_document
    inspector = getattr(ocr_fixtures, "_Inspector")(
        PDFPreflight(
            outcome="ocr_required",
            page_count=2,
            native_output_sha256="a" * 64,
            pages=[
                PDFPreflightPage(
                    page_number=1,
                    native_character_count=116,
                    native_text_sha256="b" * 64,
                    requires_ocr=False,
                ),
                PDFPreflightPage(
                    page_number=2,
                    native_character_count=0,
                    native_text_sha256="c" * 64,
                    requires_ocr=True,
                ),
            ],
        )
    )
    result = backfill_ocr_evidence(
        conn,
        request.model_copy(update={"page_selections": (selection,)}),
        inspector=inspector,
    )
    assert result.pages_requiring_ocr == 2
    assert result.verified_page_selections == [selection]
    assert result.dry_run is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("  Chart\r\n15%  \rQ2\t", "Chart\n15%\nQ2"),
        ("\uff21\uff22\uff23\u00a0\u2460\t\n\ufb01nancial  ", "ABC 1\nfinancial"),
        ("\n  indented\t\n\nsecond  \n", "indented\n\nsecond"),
        ("Revenue \u221220\u201430%\n", "Revenue \u221220\u201430%"),
        ("one\u2028two\u200b", "one\u2028two\u200b"),
    ],
)
def test_nfkc_lines_normalization_is_shared_and_byte_stable(raw: str, expected: str) -> None:
    from provenance.ocr_extraction import normalize_extracted_text as legacy_public_name
    from runtime.deterministic import normalize_extracted_text

    assert legacy_public_name is normalize_extracted_text
    assert normalize_extracted_text(raw).encode("utf-8") == expected.encode("utf-8")
