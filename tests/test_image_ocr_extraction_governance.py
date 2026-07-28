"""Governed standalone-image OCR contracts."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.image_ocr_extraction import (
    HeaderImageInspector,
    ImageInspection,
    ImageOCREngineDescriptor,
    ImageOCROutput,
    ImageOCRRequest,
    ImageOCRSummary,
    TesseractImageProvider,
    backfill_image_ocr_evidence,
    parse_tesseract_tsv,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 12, 0, 0)
SHA_B = "b" * 64
MODEL_MANIFEST = hashlib.sha256(('{"eng":"' + SHA_B + '"}').encode("utf-8")).hexdigest()


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture(scope="module")
def schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("image-ocr-schema") / "template.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0234_image_ocr_governance")
    return path


def _png_header(width: int = 640, height: int = 480) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def _jpeg_header(width: int, height: int, components: int) -> bytes:
    component_bytes = b"".join(bytes((index, 0x11, 0)) for index in range(1, components + 1))
    segment = (
        bytes((8 + 3 * components,))
        if 8 + 3 * components < 256
        else (8 + 3 * components).to_bytes(2, "big")
    )
    if len(segment) == 1:
        segment = b"\x00" + segment
    return (
        b"\xff\xd8\xff\xc0"
        + segment
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + bytes((components,))
        + component_bytes
        + b"\xff\xd9"
    )


def _connection(
    tmp_path: Path,
    schema_template: Path,
    *,
    contents: tuple[bytes, ...] = (_png_header(),),
) -> tuple[sqlite3.Connection, Path, tuple[str, ...]]:
    db_path = tmp_path / "portfolio.db"
    shutil.copy2(schema_template, db_path)
    content_root = tmp_path / "blobs"
    content_root.mkdir()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ledger = EvidenceLedger(conn)
    document_ids: list[str] = []
    for index, content in enumerate(contents, start=1):
        digest = hashlib.sha256(content).hexdigest()
        blob_path = content_root / digest
        if not blob_path.exists():
            blob_path.write_bytes(content)
            ledger.persist(
                ContentBlob(
                    sha256=digest,
                    byte_size=len(content),
                    media_type="image/png",
                    storage_uri=str(blob_path),
                    recorded_at=STAMP,
                )
            )
        observation_id = f"image-observation-{index}"
        ledger.persist(
            SourceObservation(
                observation_id=observation_id,
                idempotency_key=observation_id,
                source_kind="sec_filing_package",
                source_url=f"https://sec.test/image-{index}.png",
                blob_sha256=digest,
                source_published_at=STAMP,
                filing_at=STAMP,
                accepted_at=STAMP,
                observed_at=STAMP,
                retrieved_at=STAMP,
                retrieval_config_sha256="a" * 64,
                collector_code_version="test-collector@1",
            )
        )
        document_id = f"image-document-{index}"
        document_ids.append(document_id)
        ledger.persist(
            DocumentVersion(
                document_version_id=document_id,
                document_key=f"ACME:image:{index}",
                version_sequence=1,
                observation_id=observation_id,
                blob_sha256=digest,
                issuer_id="issuer-acme",
                ticker="ACME",
                document_type="sec_exhibit",
                form_type="10-K",
                accession_number="0000000001-26-000001",
                exhibit_id=f"IMAGE-{index}",
                language="en",
                recorded_at=STAMP,
            )
        )
    conn.commit()
    return conn, content_root, tuple(document_ids)


class _Inspector:
    def __init__(self, inspection: ImageInspection | None = None) -> None:
        self.inspection = inspection or ImageInspection(
            media_type="image/png", width=640, height=480
        )
        self.calls = 0

    def inspect(self, raw_bytes: bytes) -> ImageInspection:
        assert raw_bytes.startswith(b"\x89PNG")
        self.calls += 1
        return self.inspection


class _Provider:
    descriptor = ImageOCREngineDescriptor(
        engine_name="test-tesseract",
        engine_version="tesseract 5.5.0",
        engine_binary_sha256="c" * 64,
        model_name="test-traineddata",
        model_version=f"model-manifest-sha256:{MODEL_MANIFEST}",
        model_manifest_sha256=MODEL_MANIFEST,
        model_artifacts={"eng": SHA_B},
    )

    def __init__(
        self,
        output: ImageOCROutput | None = None,
    ) -> None:
        self.output = output or ImageOCROutput(
            text="Revenue increased to $2.0 billion.",
            mean_confidence=97.5,
        )
        self.calls = 0
        self.raw_inputs: list[bytes] = []
        self.page_segmentation_modes: list[int] = []

    def extract(
        self,
        raw_bytes: bytes,
        *,
        media_type: str,
        languages: tuple[str, ...],
        page_segmentation_mode: int,
        engine_mode: int,
        timeout_seconds: int,
    ) -> ImageOCROutput:
        assert media_type == "image/png"
        assert languages == ("eng",)
        assert engine_mode == 1
        assert timeout_seconds == 120
        self.calls += 1
        self.raw_inputs.append(raw_bytes)
        self.page_segmentation_modes.append(page_segmentation_mode)
        return self.output


def _request(
    root: Path,
    *,
    apply: bool,
    task_id: str = "image-ocr-test",
    document_ids: tuple[str, ...] = (),
    batch_size: int = 25,
    maximum_pixels: int = 40_000_000,
    page_segmentation_mode: int = 6,
) -> ImageOCRRequest:
    return ImageOCRRequest(
        repo_root=root,
        content_roots=(root,),
        apply=apply,
        task_id=task_id,
        document_version_ids=document_ids,
        batch_size=batch_size,
        maximum_pixels=maximum_pixels,
        page_segmentation_mode=page_segmentation_mode,
    )


def test_accepted_output_creates_governance_and_document_page_nodes(
    tmp_path: Path, schema_template: Path
) -> None:
    conn, root, document_ids = _connection(tmp_path, schema_template)
    provider = _Provider()
    try:
        result = backfill_image_ocr_evidence(
            conn,
            _request(root, apply=True, document_ids=document_ids),
            inspector=_Inspector(),
            provider=provider,
        )

        assert result.documents_accepted == 1
        assert provider.calls == 1
        assert tuple(
            conn.execute("SELECT extractor_name, outcome FROM evidence_extraction_runs").fetchone()
        ) == ("governed-image-ocr", "succeeded")
        assert tuple(
            conn.execute(
                "SELECT engine_version, model_manifest_sha256 FROM image_ocr_extraction_governance"
            ).fetchone()
        ) == ("tesseract 5.5.0", MODEL_MANIFEST)
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT node_kind FROM evidence_nodes ORDER BY node_kind"
            ).fetchall()
        ] == [("document",), ("passage",)]
        row = conn.execute(
            "SELECT result.outcome, result.page_number, result.mean_confidence, "
            "node.text, node.locator_json "
            "FROM image_ocr_results AS result "
            "JOIN evidence_nodes AS node ON node.node_id = result.node_id"
        ).fetchone()
        assert tuple(row[:4]) == (
            "accepted",
            1,
            97.5,
            "Revenue increased to $2.0 billion.",
        )
        assert row[4] == '{"page_number":1,"source_ref":"https://sec.test/image-1.png"}'
    finally:
        conn.close()


def test_low_confidence_is_quarantined_without_evidence_nodes(
    tmp_path: Path, schema_template: Path
) -> None:
    conn, root, document_ids = _connection(tmp_path, schema_template)
    provider = _Provider(ImageOCROutput(text="uncertain text", mean_confidence=12.0))
    try:
        result = backfill_image_ocr_evidence(
            conn,
            _request(root, apply=True, document_ids=document_ids),
            inspector=_Inspector(),
            provider=provider,
        )

        assert result.documents_quarantined == 1
        assert result.finding_counts == {"confidence_below_threshold": 1}
        assert tuple(conn.execute("SELECT outcome FROM evidence_extraction_runs").fetchone()) == (
            "failed",
        )
        assert tuple(
            conn.execute(
                "SELECT outcome, node_id, mean_confidence, reason_code FROM image_ocr_results"
            ).fetchone()
        ) == (
            "quarantined",
            None,
            12.0,
            "confidence_below_threshold",
        )
        assert conn.execute("SELECT COUNT(*) FROM evidence_nodes").fetchone()[0] == 0
    finally:
        conn.close()


def test_dry_run_assesses_without_engine_writes_or_checkpoint(
    tmp_path: Path, schema_template: Path
) -> None:
    conn, root, document_ids = _connection(tmp_path, schema_template)
    inspector = _Inspector()
    try:
        result = backfill_image_ocr_evidence(
            conn,
            _request(root, apply=False, document_ids=document_ids),
            inspector=inspector,
        )
        assert result.dry_run is True
        assert result.documents_eligible == 1
        assert result.records_planned == 6
        assert inspector.calls == 1
        assert conn.execute("SELECT COUNT(*) FROM image_ocr_assessments").fetchone()[0] == 0
        assert not (root / ".tmp" / "image-ocr-test" / "state.json").exists()
    finally:
        conn.close()


def test_hash_mismatch_is_explicit_and_never_invokes_engine(
    tmp_path: Path, schema_template: Path
) -> None:
    conn, root, document_ids = _connection(tmp_path, schema_template)
    provider = _Provider()
    next(root.iterdir()).write_bytes(b"tampered")
    try:
        result = backfill_image_ocr_evidence(
            conn,
            _request(root, apply=True, document_ids=document_ids),
            inspector=_Inspector(),
            provider=provider,
        )

        assert provider.calls == 0
        assert result.finding_counts == {"sha256_mismatch": 1}
        row = conn.execute(
            "SELECT outcome, reason_code, observed_sha256 FROM image_ocr_assessments"
        ).fetchone()
        assert tuple(row[:2]) == ("quarantined", "sha256_mismatch")
        assert row[2] == hashlib.sha256(b"tampered").hexdigest()
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_image_byte_limit_is_checked_before_inspection_or_engine(
    tmp_path: Path, schema_template: Path
) -> None:
    conn, root, document_ids = _connection(tmp_path, schema_template)
    provider = _Provider()
    inspector = _Inspector()
    request = _request(root, apply=True, document_ids=document_ids).model_copy(
        update={"maximum_image_bytes": 8}
    )
    try:
        result = backfill_image_ocr_evidence(
            conn,
            request,
            inspector=inspector,
            provider=provider,
        )
        assert inspector.calls == provider.calls == 0
        assert result.finding_counts == {"image_byte_limit_exceeded": 1}
        assert (
            conn.execute(
                "SELECT observed_sha256, outcome, reason_code FROM image_ocr_assessments"
            ).fetchone()[0]
            == hashlib.sha256(_png_header()).hexdigest()
        )
    finally:
        conn.close()


def test_replay_is_idempotent_and_does_not_call_engine_twice(
    tmp_path: Path, schema_template: Path
) -> None:
    conn, root, document_ids = _connection(tmp_path, schema_template)
    provider = _Provider()
    request = _request(root, apply=True, document_ids=document_ids)
    try:
        first = backfill_image_ocr_evidence(
            conn, request, inspector=_Inspector(), provider=provider
        )
        second = backfill_image_ocr_evidence(
            conn, request, inspector=_Inspector(), provider=provider
        )

        assert first.documents_accepted == 1
        assert second.documents_skipped_covered == 1
        assert second.records_created == 0
        assert second.records_replayed == 1
        assert provider.calls == 1
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == 1
    finally:
        conn.close()


def test_rollback_leaves_checkpoint_and_all_governance_unchanged(
    tmp_path: Path,
    schema_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import provenance.image_ocr_extraction as module

    conn, root, _ = _connection(tmp_path, schema_template)
    provider = _Provider()
    original = cast(
        Callable[
            [
                sqlite3.Connection,
                str,
                tuple[str, ...],
                tuple[object, ...],
                tuple[str, ...],
                tuple[object, ...],
                ImageOCRSummary,
            ],
            None,
        ],
        getattr(module, "_persist_exact"),
    )

    def _fail_governance(
        conn_arg: sqlite3.Connection,
        table: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        key_columns: tuple[str, ...],
        key_values: tuple[object, ...],
        summary: ImageOCRSummary,
    ) -> None:
        if table == "image_ocr_extraction_governance":
            raise RuntimeError("injected persistence failure")
        original(
            conn_arg,
            table,
            columns,
            values,
            key_columns,
            key_values,
            summary,
        )

    monkeypatch.setattr(module, "_persist_exact", _fail_governance)
    checkpoint = root / ".tmp" / "rollback-test" / "state.json"
    try:
        with pytest.raises(RuntimeError, match="injected persistence failure"):
            backfill_image_ocr_evidence(
                conn,
                _request(root, apply=True, task_id="rollback-test"),
                inspector=_Inspector(),
                provider=provider,
            )
        assert not checkpoint.exists()
        assert conn.execute("SELECT COUNT(*) FROM image_ocr_assessments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_image_dimension_bomb_is_quarantined_before_engine(
    tmp_path: Path, schema_template: Path
) -> None:
    conn, root, document_ids = _connection(tmp_path, schema_template)
    provider = _Provider()
    inspector = _Inspector(ImageInspection(media_type="image/png", width=10_000, height=10_000))
    try:
        result = backfill_image_ocr_evidence(
            conn,
            _request(
                root,
                apply=True,
                document_ids=document_ids,
                maximum_pixels=40_000_000,
            ),
            inspector=inspector,
            provider=provider,
        )

        assert inspector.calls == 1
        assert provider.calls == 0
        assert result.finding_counts == {"image_dimension_limit_exceeded": 1}
        assert tuple(
            conn.execute(
                "SELECT width, height, pixel_count, outcome, reason_code FROM image_ocr_assessments"
            ).fetchone()
        ) == (
            10_000,
            10_000,
            100_000_000,
            "quarantined",
            "image_dimension_limit_exceeded",
        )
    finally:
        conn.close()


def test_same_blob_across_batches_invokes_engine_once_and_fans_lineage(
    tmp_path: Path, schema_template: Path
) -> None:
    content = _png_header()
    conn, root, _ = _connection(
        tmp_path,
        schema_template,
        contents=(content, content),
    )
    provider = _Provider()
    request = _request(root, apply=True, task_id="dedup-test", batch_size=1)
    try:
        first = backfill_image_ocr_evidence(
            conn, request, inspector=_Inspector(), provider=provider
        )
        second = backfill_image_ocr_evidence(
            conn, request, inspector=_Inspector(), provider=provider
        )

        assert first.documents_accepted == second.documents_accepted == 1
        assert provider.calls == 1
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM image_ocr_results").fetchone()[0] == 2
        rows = conn.execute(
            "SELECT run.document_version_id, result.output_sha256, node.text "
            "FROM evidence_extraction_runs AS run "
            "JOIN image_ocr_results AS result "
            "ON result.extraction_run_id = run.extraction_run_id "
            "JOIN evidence_nodes AS node ON node.node_id = result.node_id "
            "ORDER BY run.document_version_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][1:] == rows[1][1:]
    finally:
        conn.close()


def test_blob_output_is_not_reused_across_engine_config_changes(
    tmp_path: Path, schema_template: Path
) -> None:
    content = _png_header()
    conn, root, document_ids = _connection(
        tmp_path,
        schema_template,
        contents=(content, content),
    )
    provider = _Provider()
    try:
        backfill_image_ocr_evidence(
            conn,
            _request(root, apply=True, document_ids=(document_ids[0],)),
            inspector=_Inspector(),
            provider=provider,
        )
        backfill_image_ocr_evidence(
            conn,
            _request(
                root,
                apply=True,
                document_ids=(document_ids[1],),
                page_segmentation_mode=11,
            ),
            inspector=_Inspector(),
            provider=provider,
        )
        assert provider.calls == 2
        assert provider.page_segmentation_modes == [6, 11]
    finally:
        conn.close()


@pytest.mark.parametrize("components", [1, 4])
def test_jpeg_l_and_cmyk_headers_are_admitted_without_conversion(
    components: int,
) -> None:
    raw = _jpeg_header(5_100, 6_600, components)
    inspection = HeaderImageInspector().inspect(raw)
    assert inspection == ImageInspection(
        media_type="image/jpeg",
        width=5_100,
        height=6_600,
    )


def test_tesseract_tsv_parser_preserves_lines_and_mean_word_confidence() -> None:
    raw = (
        b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
        b"width\theight\tconf\ttext\n"
        b"5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t95.0\tRevenue\n"
        b"5\t1\t1\t1\t1\t2\t10\t0\t10\t10\t85.0\tgrew\n"
        b"5\t1\t1\t1\t2\t1\t0\t10\t10\t10\t90.0\tquickly\n"
    )
    output = parse_tesseract_tsv(raw)
    assert output.text == "Revenue grew\nquickly"
    assert output.mean_confidence == 90.0


@pytest.mark.parametrize("components", [1, 4])
def test_tesseract_provider_passes_l_and_cmyk_jpeg_bytes_without_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    components: int,
) -> None:
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"test-tesseract-binary")
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    (tessdata / "eng.traineddata").write_bytes(b"test-traineddata")
    raw = _jpeg_header(5_100, 6_600, components)
    observed: list[bytes] = []
    tsv = (
        b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
        b"width\theight\tconf\ttext\n"
        b"5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t99.0\tRevenue\n"
    )

    def _run(
        arguments: list[str],
        *,
        check: bool,
        capture_output: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]:
        assert check is False
        assert capture_output is True
        if arguments[-1] == "--version":
            return subprocess.CompletedProcess(arguments, 0, b"tesseract 5.5.0\n", b"")
        observed.append(Path(arguments[1]).read_bytes())
        return subprocess.CompletedProcess(arguments, 0, tsv, b"")

    monkeypatch.setattr(subprocess, "run", _run)
    provider = TesseractImageProvider(
        tesseract_executable=executable,
        tessdata_directory=tessdata,
        languages=("eng",),
    )
    output = provider.extract(
        raw,
        media_type="image/jpeg",
        languages=("eng",),
        page_segmentation_mode=6,
        engine_mode=1,
        timeout_seconds=120,
    )
    assert observed == [raw]
    assert output.text == "Revenue"
    assert output.mean_confidence == 99.0
