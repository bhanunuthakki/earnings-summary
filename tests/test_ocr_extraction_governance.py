"""Governed, dry-run-first OCR extraction contracts."""

from __future__ import annotations

import hashlib
import io
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from alembic.config import Config

from alembic import command
from execution import backfill_ocr_evidence as cli
from provenance.document_processing_evidence import (
    DocumentProcessingEvidenceError,
    publish_document_processing_evidence,
    resolve_governed_ocr_pages,
)
from provenance.evidence_backfill import BackfillRequest, backfill_legacy_evidence
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    LedgerRecord,
    PersistResult,
    SourceObservation,
)
from provenance.ocr_extraction import (
    OCRBackfillRequest,
    OCREngineDescriptor,
    OCRPageOutput,
    PDFPreflight,
    PDFPreflightPage,
    PypdfPDFInspector,
    backfill_ocr_evidence,
)
from provenance.ocr_layout import (
    OCRLayoutError,
    parse_tesseract_pdf_tsv,
    verify_ocr_layout_bytes,
)
from runtime.deterministic import content_sha256_or_text
from sqlite_runtime import register_sqlite_integrity_functions

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_REVISION = "0221_ask_retrieval_traces"
OCR_REVISION = "0222_ocr_extraction_governance"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
MODEL_MANIFEST_SHA = hashlib.sha256(('{"eng":"' + SHA_B + '"}').encode("utf-8")).hexdigest()


def test_cli_can_persist_native_preflight_without_ocr_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class _Connection:
        def close(self) -> None:
            captured["closed"] = True

    class _Result:
        def model_dump_json(self) -> str:
            return '{"mode":"apply"}'

    def _connect(*_args: object, **_kwargs: object) -> _Connection:
        return _Connection()

    monkeypatch.setattr(cli, "connect_sqlite", _connect)

    def _backfill(conn: object, request: OCRBackfillRequest, *, provider: object) -> _Result:
        captured["request"] = request
        captured["provider"] = provider
        return _Result()

    monkeypatch.setattr(cli, "backfill_ocr_evidence", _backfill)

    assert (
        cli.main(
            [
                "--db",
                str(tmp_path / "portfolio.db"),
                "--apply",
                "--preflight-only",
            ]
        )
        == 0
    )
    request = captured["request"]
    assert isinstance(request, OCRBackfillRequest)
    assert request.apply is True
    assert captured["provider"] is None
    assert captured["closed"] is True
    assert capsys.readouterr().out == '{"mode":"apply"}\n'


def _config(db_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


@pytest.mark.parametrize("password", ["", "required-to-open"])
def test_pdf_preflight_distinguishes_restrictions_from_opening_password(password: str) -> None:
    from pypdf import PdfWriter
    from pypdf.constants import UserAccessPermissions

    writer = PdfWriter()
    writer.add_blank_page(width=300, height=200)
    writer.encrypt(
        password,
        owner_password="dummy-owner-only",  # pragma: allowlist secret -- synthetic PDF fixture
        permissions_flag=UserAccessPermissions.PRINT,
    )
    output = io.BytesIO()
    writer.write(output)
    raw = output.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    result = PypdfPDFInspector().inspect(raw, minimum_native_characters=10)
    if password:
        assert result.outcome == "encrypted"
        assert result.reason_code == "encrypted_pdf"
        assert result.page_count == 0
    else:
        assert result.outcome == "ocr_required"
        assert result.reason_code is None
        assert result.page_count == 1
        assert result.pages[0].requires_ocr is True
    assert hashlib.sha256(raw).hexdigest() == digest


@pytest.mark.parametrize("permissions_valid", [False, None])
def test_pdf_preflight_rejects_invalid_permissions(
    monkeypatch: pytest.MonkeyPatch, permissions_valid: bool | None
) -> None:
    import pypdf

    class Reader:
        is_encrypted = True
        are_permissions_valid = permissions_valid

        def __init__(self, _stream: object) -> None:
            pass

        def decrypt(self, password: str) -> int:
            assert password == ""
            return 1

        @property
        def pages(self) -> list[object]:
            pytest.fail("Invalid PDF permissions must block page access")

    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    result = PypdfPDFInspector().inspect(b"retained-source", minimum_native_characters=10)
    assert result.outcome == "unreadable"
    assert result.reason_code == "invalid_pdf_permissions"


def _connection(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    content: bytes = b"%PDF-governed-ocr-test",
) -> tuple[sqlite3.Connection, Path]:
    db_path = tmp_path / "portfolio.db"
    repo_root = tmp_path / "repo"
    artifact = repo_root / "data" / "ACME.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    migrated_db(db_path, target="head")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    register_sqlite_integrity_functions(conn)
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        "fetch_status, raw_bytes_size) "
        "VALUES (1, 'ACME', 'issuer_ir', 'presentation', ?, ?, "
        "'2026-07-20 12:00:00', 'ok', ?)",
        ("data/ACME.pdf", digest, len(content)),
    )
    conn.commit()
    backfill_legacy_evidence(conn, BackfillRequest(repo_root=repo_root, apply=True))
    return conn, repo_root


class _Inspector:
    def __init__(self, result: PDFPreflight) -> None:
        self.result = result
        self.calls = 0

    def inspect(self, raw_bytes: bytes, *, minimum_native_characters: int) -> PDFPreflight:
        assert raw_bytes.startswith(b"%PDF-")
        assert minimum_native_characters == 32
        self.calls += 1
        return self.result


class _Provider:
    descriptor = OCREngineDescriptor(
        engine_name="test-ocr",
        engine_version="test-ocr 4.2",
        engine_binary_sha256=SHA_A,
        model_name="test-traineddata",
        model_version="model-manifest-sha256:" + MODEL_MANIFEST_SHA,
        model_manifest_sha256=MODEL_MANIFEST_SHA,
        model_artifacts={"eng": SHA_B},
        renderer_name="test-renderer",
        renderer_version="test-renderer 1.0",
        renderer_binary_sha256=SHA_C,
    )

    def __init__(
        self,
        outputs: list[OCRPageOutput] | None = None,
        *,
        failure_reason: str | None = None,
    ) -> None:
        self.outputs = outputs or []
        self.failure_reason = failure_reason
        self.calls: list[tuple[int, ...]] = []

    def extract_pages(
        self,
        raw_bytes: bytes,
        *,
        page_numbers: tuple[int, ...],
        languages: tuple[str, ...],
        dpi: int,
        page_segmentation_mode: int,
        engine_mode: int,
        timeout_seconds: int,
    ) -> list[OCRPageOutput]:
        from provenance.ocr_extraction import OCRProviderError

        assert raw_bytes.startswith(b"%PDF-")
        assert languages == ("eng",)
        assert dpi == 300
        assert page_segmentation_mode == 6
        assert engine_mode == 1
        assert timeout_seconds == 120
        self.calls.append(page_numbers)
        if self.failure_reason is not None:
            raise OCRProviderError(self.failure_reason)
        return self.outputs


def _required_preflight(*page_numbers: int) -> PDFPreflight:
    return PDFPreflight(
        outcome="ocr_required",
        page_count=max(page_numbers),
        pages=[
            PDFPreflightPage(
                page_number=page_number,
                native_character_count=0,
                native_text_sha256=hashlib.sha256(b"").hexdigest(),
                requires_ocr=True,
            )
            for page_number in page_numbers
        ],
        native_output_sha256=hashlib.sha256(b'{"pages":[{"page_number":1,"text":""}]}').hexdigest(),
        reason_code=None,
    )


def _request(repo_root: Path, *, apply: bool, task_id: str = "ocr-test") -> OCRBackfillRequest:
    return OCRBackfillRequest(repo_root=repo_root, apply=apply, task_id=task_id)


def test_migration_round_trip_and_append_only_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "roundtrip.db"
    config = _config(db_path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0213_evidence_ledger_foundation")
    command.stamp(config, PRIOR_REVISION)
    command.upgrade(config, OCR_REVISION)
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "ocr_document_assessments",
            "ocr_preflight_pages",
            "ocr_extraction_governance",
            "ocr_page_results",
        } <= tables
        triggers = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert "trg_ocr_document_assessments_append_only" in triggers
        assert "trg_ocr_page_results_append_only_delete" in triggers
    finally:
        conn.close()
    command.downgrade(config, PRIOR_REVISION)
    conn = sqlite3.connect(db_path)
    try:
        remaining = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'ocr_%'"
            )
        }
        assert remaining == set()
    finally:
        conn.close()


def test_dry_run_detects_ocr_need_without_provider_or_writes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    inspector = _Inspector(_required_preflight(1))
    try:
        result = backfill_ocr_evidence(
            conn,
            _request(repo_root, apply=False),
            inspector=inspector,
        )
        assert result.documents_considered == 1
        assert result.documents_requiring_ocr == 1
        assert result.pages_requiring_ocr == 1
        assert result.records_planned == 7
        assert conn.execute("SELECT COUNT(*) FROM ocr_document_assessments").fetchone()[0] == 0
        assert not (repo_root / ".tmp" / "ocr-test" / "state.json").exists()
    finally:
        conn.close()


def test_native_preflight_is_page_complete_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pypdf

    class _Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class _Reader:
        def __init__(self) -> None:
            self.is_encrypted = False
            self.pages = [_Page("Revenue grew substantially."), _Page("x")]

    def _reader(_stream: object) -> _Reader:
        return _Reader()

    monkeypatch.setattr(pypdf, "PdfReader", _reader)
    inspector = PypdfPDFInspector()
    first = inspector.inspect(b"%PDF-fake", minimum_native_characters=10)
    second = inspector.inspect(b"%PDF-fake", minimum_native_characters=10)
    assert first == second
    assert first.outcome == "ocr_required"
    assert first.page_count == 2
    assert [page.requires_ocr for page in first.pages] == [False, True]
    assert all(len(page.native_text_sha256) == 64 for page in first.pages)


def test_apply_records_exact_governance_page_confidence_and_replays(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    inspector = _Inspector(_required_preflight(1))
    provider = _Provider(
        [
            OCRPageOutput(
                page_number=1,
                text="Revenue increased to $2.0 billion.",
                mean_confidence=97.25,
            )
        ]
    )
    try:
        first = backfill_ocr_evidence(
            conn,
            _request(repo_root, apply=True),
            inspector=inspector,
            provider=provider,
        )
        assert first.documents_ocr_succeeded == 1
        assert first.pages_ocr_accepted == 1
        run = conn.execute(
            "SELECT run.input_sha256, run.output_sha256, run.extractor_config_sha256, "
            "governance.engine_name, governance.engine_version, "
            "governance.engine_binary_sha256, governance.model_name, "
            "governance.model_version, governance.model_manifest_sha256, "
            "governance.model_artifacts_json, governance.languages_json, "
            "governance.engine_config_json, "
            "governance.extractor_config_sha256 AS governance_config_sha256 "
            "FROM evidence_extraction_runs AS run JOIN ocr_extraction_governance AS governance "
            "ON governance.extraction_run_id = run.extraction_run_id"
        ).fetchone()
        assert run is not None
        assert run["input_sha256"] == hashlib.sha256(b"%PDF-governed-ocr-test").hexdigest()
        assert len(run["output_sha256"]) == 64
        assert len(run["extractor_config_sha256"]) == 64
        assert tuple(run[3:9]) == (
            "test-ocr",
            "test-ocr 4.2",
            SHA_A,
            "test-traineddata",
            "model-manifest-sha256:" + MODEL_MANIFEST_SHA,
            MODEL_MANIFEST_SHA,
        )
        assert run["model_artifacts_json"] == '{"eng":"' + SHA_B + '"}'
        assert run["languages_json"] == '["eng"]'
        assert '"dpi":300' in run["engine_config_json"]
        assert run["governance_config_sha256"] == run["extractor_config_sha256"]
        page = conn.execute(
            "SELECT result.page_number, result.outcome, result.output_sha256, "
            "result.mean_confidence, result.locator_json, node.text "
            "FROM ocr_page_results AS result JOIN evidence_nodes AS node "
            "ON node.node_id = result.node_id"
        ).fetchone()
        assert page is not None
        assert tuple(page[:2]) == (1, "accepted")
        assert (
            page["output_sha256"]
            == hashlib.sha256(b"Revenue increased to $2.0 billion.").hexdigest()
        )
        assert page["mean_confidence"] == 97.25
        assert page["locator_json"] == '{"page_number":1,"source_ref":"data/ACME.pdf"}'
        assert page["text"] == "Revenue increased to $2.0 billion."

        run_id, document_id, source_sha = conn.execute(
            "SELECT extraction_run_id,document_version_id,input_sha256 "
            "FROM evidence_extraction_runs WHERE extractor_name='governed-pdf-ocr'"
        ).fetchone()
        cutoff = datetime(2026, 8, 1, tzinfo=UTC)
        text_only_seal = publish_document_processing_evidence(
            conn,
            document_version_id=document_id,
            processing_lane="pdf_ocr",
            cutoff_at=cutoff,
            recorded_at=datetime(2026, 8, 2, tzinfo=UTC),
        )
        assert text_only_seal.member_count == 1
        with pytest.raises(
            DocumentProcessingEvidenceError, match="ocr_projection_layout_missing_or_ambiguous"
        ):
            resolve_governed_ocr_pages(
                conn,
                repo_root=repo_root,
                document_version_id=document_id,
                source_sha256=source_sha,
                evidence_seal_id=text_only_seal.evidence_seal_id,
                extraction_run_id=run_id,
                page_result_ids=(f"{run_id}:1",),
                cutoff_at=cutoff,
            )

        replay = backfill_ocr_evidence(
            conn,
            _request(repo_root, apply=True, task_id="ocr-replay"),
            inspector=inspector,
            provider=provider,
        )
        assert replay.documents_skipped_covered == 1
        assert replay.records_created == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == 2
    finally:
        conn.close()


def test_native_sufficient_is_persisted_without_calling_ocr(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    native_text = "Native text is already sufficiently complete."
    preflight = PDFPreflight(
        outcome="native_sufficient",
        page_count=1,
        pages=[
            PDFPreflightPage(
                page_number=1,
                native_character_count=36,
                native_text_sha256=hashlib.sha256(native_text.encode()).hexdigest(),
                requires_ocr=False,
            )
        ],
        native_output_sha256=hashlib.sha256(native_text.encode()).hexdigest(),
        reason_code=None,
    )
    inspector = _Inspector(preflight)
    provider = _Provider()
    try:
        result = backfill_ocr_evidence(
            conn,
            _request(repo_root, apply=True),
            inspector=inspector,
            provider=provider,
        )
        assert result.documents_native_sufficient == 1
        assert provider.calls == []
        row = conn.execute("SELECT outcome, reason_code FROM ocr_document_assessments").fetchone()
        assert tuple(row) == ("native_sufficient", None)
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("outcome", ["encrypted", "unreadable", "unsupported"])
def test_preflight_failures_are_explicit_and_quarantined(
    tmp_path: Path,
    outcome: Literal["encrypted", "unreadable", "unsupported"],
    migrated_db: Callable[..., Path],
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    preflight = PDFPreflight(
        outcome=outcome,
        page_count=0,
        pages=[],
        native_output_sha256=hashlib.sha256(b"").hexdigest(),
        reason_code=f"{outcome}_pdf",
    )
    inspector = _Inspector(preflight)
    provider = _Provider()
    try:
        result = backfill_ocr_evidence(
            conn,
            _request(repo_root, apply=True),
            inspector=inspector,
            provider=provider,
        )
        assert result.documents_quarantined == 1
        assert result.finding_counts == {f"{outcome}_pdf": 1}
        row = conn.execute("SELECT outcome, reason_code FROM ocr_document_assessments").fetchone()
        assert tuple(row) == (outcome, f"{outcome}_pdf")
        assert provider.calls == []
    finally:
        conn.close()


def test_low_confidence_output_records_failed_run_without_evidence_node(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    inspector = _Inspector(_required_preflight(1))
    provider = _Provider([OCRPageOutput(page_number=1, text="uncertain", mean_confidence=12.5)])
    try:
        result = backfill_ocr_evidence(
            conn,
            _request(repo_root, apply=True),
            inspector=inspector,
            provider=provider,
        )
        assert result.documents_ocr_failed == 1
        assert result.documents_quarantined == 1
        assert result.finding_counts == {"confidence_below_threshold": 1}
        run = conn.execute(
            "SELECT outcome FROM evidence_extraction_runs WHERE extractor_name = 'governed-pdf-ocr'"
        ).fetchone()
        assert tuple(run) == ("failed",)
        page = conn.execute(
            "SELECT outcome, node_id, output_sha256, mean_confidence, reason_code "
            "FROM ocr_page_results"
        ).fetchone()
        assert tuple(page) == (
            "quarantined",
            None,
            hashlib.sha256(b"uncertain").hexdigest(),
            12.5,
            "confidence_below_threshold",
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_nodes WHERE extraction_run_id LIKE 'ocr-run-%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_provider_failure_is_recorded_for_every_required_page(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    inspector = _Inspector(_required_preflight(1, 2))
    provider = _Provider(failure_reason="engine_timeout")
    try:
        result = backfill_ocr_evidence(
            conn,
            _request(repo_root, apply=True),
            inspector=inspector,
            provider=provider,
        )
        assert result.documents_ocr_failed == 1
        assert result.finding_counts == {"engine_timeout": 1}
        rows = conn.execute(
            "SELECT page_number, outcome, reason_code FROM ocr_page_results ORDER BY page_number"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (1, "failed", "engine_timeout"),
            (2, "failed", "engine_timeout"),
        ]
    finally:
        conn.close()


def test_apply_advances_checkpoint_only_after_bounded_transaction(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    inspector = _Inspector(_required_preflight(1))
    provider = _Provider([OCRPageOutput(page_number=1, text="Auditable.", mean_confidence=99.0)])
    try:
        result = backfill_ocr_evidence(
            conn,
            OCRBackfillRequest(
                repo_root=repo_root,
                apply=True,
                batch_size=1,
                task_id="ocr-checkpoint",
            ),
            inspector=inspector,
            provider=provider,
        )
        assert result.last_document_id_after == 1
        checkpoint = repo_root / ".tmp" / "ocr-checkpoint" / "state.json"
        assert checkpoint.exists()
        assert '"last_document_id":1' in checkpoint.read_text(encoding="utf-8")
    finally:
        conn.close()


def test_evidence_native_lane_preflights_extensionless_pdf_without_legacy_row(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    body = b"%PDF-evidence-native"
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
                media_type="application/pdf",
                storage_uri=blob_path.as_uri(),
                recorded_at=recorded_at,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="native-pdf-observation",
                idempotency_key="native-pdf-observation",
                source_kind="sec_filing_document",
                source_url="https://issuer.test/download?id=annual-report",
                blob_sha256=digest,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=recorded_at,
                retrieved_at=recorded_at,
                retrieval_config_sha256="d" * 64,
                collector_code_version="test@1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id="native-pdf-version",
                document_key="issuer:native-annual-report",
                version_sequence=1,
                observation_id="native-pdf-observation",
                blob_sha256=digest,
                issuer_id="issuer",
                ticker="ACME",
                document_type="annual_report",
                form_type="10-K",
                language="en",
                legacy_document_id=None,
                recorded_at=recorded_at,
            )
        )
        conn.commit()
        inspector = _Inspector(_required_preflight(1))
        result = backfill_ocr_evidence(
            conn,
            OCRBackfillRequest(
                repo_root=repo_root,
                apply=True,
                task_id="native-ocr",
                source_lane="evidence_native",
                document_version_ids=("native-pdf-version",),
            ),
            inspector=inspector,
        )
        assert result.source_lane == "evidence_native"
        assert result.documents_considered == 1
        assert result.documents_requiring_ocr == 1
        assert result.last_evidence_rowid_after > 0
        assert result.last_document_version_id_after == "native-pdf-version"
        assert inspector.calls == 1
        assert (
            conn.execute("SELECT outcome FROM ocr_document_assessments").fetchone()[0]
            == "ocr_required"
        )
        assert conn.execute("SELECT COUNT(*) FROM ocr_preflight_pages").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_extraction_runs "
                "WHERE extractor_name = 'governed-pdf-ocr'"
            ).fetchone()[0]
            == 0
        )
        assert not (repo_root / ".tmp" / "native-ocr" / "evidence-native-state.json").exists()
    finally:
        conn.close()


def _output() -> tuple[bytes, bytes, OCRPageOutput]:
    raw_tsv = (
        b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        b"5\t1\t1\t1\t1\t1\t10\t20\t35\t12\t98\tRevenue\n"
        b"5\t1\t1\t1\t1\t2\t50\t20\t20\t12\t97\t25%\n"
    )
    render_png = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (300).to_bytes(4, "big") * 2
        + b"synthetic-render"
    )
    output = parse_tesseract_pdf_tsv(
        1,
        raw_tsv,
        render_png=render_png,
        width=300,
        height=300,
        dpi=300,
        media_box=(0.0, 0.0, 72.0, 72.0),
        rotation=0,
    )
    return raw_tsv, render_png, output


def test_layout_parser_retains_exact_bytes_boxes_order_and_offsets() -> None:
    raw_tsv, render_png, output = _output()
    assert output.text == "Revenue 25%"
    assert output.layout is not None
    assert [
        (word.ordinal, word.char_start, word.char_end, word.left, word.top)
        for word in output.layout.words
    ] == [
        (0, 0, 7, 10, 20),
        (1, 8, 11, 50, 20),
    ]
    assert output.layout.tsv_sha256 == hashlib.sha256(raw_tsv).hexdigest()
    assert output.layout.render_sha256 == hashlib.sha256(render_png).hexdigest()
    verify_ocr_layout_bytes(output.layout, raw_tsv, render_png)
    with pytest.raises(OCRLayoutError, match="ocr_layout_reconstruction_mismatch"):
        verify_ocr_layout_bytes(output.layout, raw_tsv.replace(b"25%", b"26%"), render_png)
    with pytest.raises(OCRLayoutError, match="ocr_layout_reconstruction_mismatch"):
        verify_ocr_layout_bytes(output.layout, raw_tsv, render_png + b"changed")


def test_unicode_layout_writer_and_reader_keep_exact_raw_bytes_and_offsets(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    raw_tsv, render_png, _ = _output()
    unicode_tsv = raw_tsv.replace(b"Revenue", "\ufb03".encode()).replace(
        b"25%", "\uff12\uff15\u00a0\uff05".encode()
    )
    output = parse_tesseract_pdf_tsv(
        1,
        unicode_tsv,
        render_png=render_png,
        width=300,
        height=300,
        dpi=300,
        media_box=(0.0, 0.0, 72.0, 72.0),
        rotation=0,
    )
    assert output.text == "ffi 25 %"
    assert output.layout is not None
    assert [word.text for word in output.layout.words] == ["ffi", "25 %"]
    assert [(word.char_start, word.char_end) for word in output.layout.words] == [
        (0, 3),
        (4, 8),
    ]
    assert output.raw_tsv == unicode_tsv
    assert output.layout.tsv_sha256 == hashlib.sha256(unicode_tsv).hexdigest()
    assert verify_ocr_layout_bytes(output.layout, unicode_tsv, render_png) == output
    assert raw_tsv != unicode_tsv
    conn, repo_root = _connection(tmp_path, migrated_db)
    try:
        summary = backfill_ocr_evidence(
            conn,
            OCRBackfillRequest(repo_root=repo_root, apply=True, task_id="unicode-layout"),
            inspector=_Inspector(_required_preflight(1)),
            provider=_Provider([output]),
        )
        assert summary.pages_ocr_accepted == 1
        run_id, document_id, source_sha = conn.execute(
            "SELECT extraction_run_id,document_version_id,input_sha256 "
            "FROM evidence_extraction_runs WHERE extractor_name='governed-pdf-ocr'"
        ).fetchone()
        cutoff = datetime(2026, 8, 1, tzinfo=UTC)
        receipt = publish_document_processing_evidence(
            conn,
            document_version_id=document_id,
            processing_lane="pdf_ocr",
            cutoff_at=cutoff,
            recorded_at=datetime(2026, 8, 2, tzinfo=UTC),
        )
        projection = resolve_governed_ocr_pages(
            conn,
            repo_root=repo_root,
            document_version_id=document_id,
            source_sha256=source_sha,
            evidence_seal_id=receipt.evidence_seal_id,
            extraction_run_id=run_id,
            page_result_ids=(f"{run_id}:1",),
            cutoff_at=cutoff,
        )
        assert projection[0].text == "ffi 25 %"
        retained_tsv = (
            repo_root
            / "data"
            / "ocr_evidence"
            / output.layout.tsv_sha256[:2]
            / output.layout.tsv_sha256
        )
        assert retained_tsv.read_bytes() == unicode_tsv
    finally:
        conn.close()


@pytest.mark.parametrize("mutation", ["text", "confidence", "layout"])
def test_layout_output_disagreement_fails_before_acceptance(
    mutation: str, tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    _raw_tsv, _render_png, output = _output()
    assert output.layout is not None
    if mutation == "text":
        contradictory = output.model_copy(update={"text": "Revenue 26%"})
    elif mutation == "confidence":
        contradictory = output.model_copy(update={"mean_confidence": 99.0})
    else:
        words = (
            output.layout.words[0],
            output.layout.words[1].model_copy(update={"text": "26%"}),
        )
        contradictory = output.model_copy(
            update={
                "text": "Revenue 26%",
                "layout": output.layout.model_copy(update={"words": words}),
            }
        )
    try:
        summary = backfill_ocr_evidence(
            conn,
            OCRBackfillRequest(repo_root=repo_root, apply=True, task_id=f"bad-{mutation}"),
            inspector=_Inspector(_required_preflight(1)),
            provider=_Provider([contradictory]),
        )
        assert summary.documents_ocr_failed == 1
        assert summary.pages_ocr_accepted == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ocr_page_results WHERE outcome='accepted'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_content_blobs WHERE media_type IN "
                "('text/tab-separated-values','image/png')"
            ).fetchone()[0]
            == 0
        )
        assert not any(path.is_file() for path in (repo_root / "data" / "ocr_evidence").rglob("*"))
    finally:
        conn.close()


def test_layout_blob_publication_rolls_back_on_later_database_failure(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    _raw_tsv, _render_png, output = _output()
    original = EvidenceLedger.persist

    def fail_after_blobs(self: EvidenceLedger, record: LedgerRecord) -> PersistResult:
        if isinstance(record, EvidenceNode) and record.node_kind == "passage":
            raise RuntimeError("synthetic layout node write failure")
        return original(self, record)

    monkeypatch.setattr(EvidenceLedger, "persist", fail_after_blobs)
    before = conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0]
    try:
        with pytest.raises(RuntimeError, match="synthetic layout node write failure"):
            backfill_ocr_evidence(
                conn,
                OCRBackfillRequest(repo_root=repo_root, apply=True, task_id="rollback-layout"),
                inspector=_Inspector(_required_preflight(1)),
                provider=_Provider([output]),
            )
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == before
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_content_blobs WHERE media_type IN "
                "('text/tab-separated-values','image/png')"
            ).fetchone()[0]
            == 0
        )
        assert not any(path.is_file() for path in (repo_root / "data" / "ocr_evidence").rglob("*"))
        monkeypatch.setattr(EvidenceLedger, "persist", original)
        succeeded = backfill_ocr_evidence(
            conn,
            OCRBackfillRequest(repo_root=repo_root, apply=True, task_id="first-layout"),
            inspector=_Inspector(_required_preflight(1)),
            provider=_Provider([output]),
        )
        assert succeeded.pages_ocr_accepted == 1
        retained = {
            path: path.read_bytes()
            for path in (repo_root / "data" / "ocr_evidence").rglob("*")
            if path.is_file()
        }
        assert len(retained) == 2
        monkeypatch.setattr(EvidenceLedger, "persist", fail_after_blobs)
        with pytest.raises(RuntimeError, match="synthetic layout node write failure"):
            backfill_ocr_evidence(
                conn,
                OCRBackfillRequest(
                    repo_root=repo_root,
                    apply=True,
                    task_id="replay-layout",
                    minimum_mean_confidence=40.0,
                ),
                inspector=_Inspector(_required_preflight(1)),
                provider=_Provider([output]),
            )
        assert {path: path.read_bytes() for path in retained} == retained
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == (
            before + 1
        )
    finally:
        conn.close()


def test_projection_requires_exact_seal_source_page_set_and_retained_bytes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    _raw_tsv, _render_png, output = _output()
    try:
        summary = backfill_ocr_evidence(
            conn,
            OCRBackfillRequest(repo_root=repo_root, apply=True, task_id="layout-test"),
            inspector=_Inspector(_required_preflight(1)),
            provider=_Provider([output]),
        )
        assert summary.pages_ocr_accepted == 1
        run_id, document_id, source_sha = conn.execute(
            "SELECT extraction_run_id,document_version_id,input_sha256 FROM evidence_extraction_runs "
            "WHERE extractor_name='governed-pdf-ocr'"
        ).fetchone()
        cutoff = datetime(2026, 8, 1, tzinfo=UTC)
        receipt = publish_document_processing_evidence(
            conn,
            document_version_id=document_id,
            processing_lane="pdf_ocr",
            cutoff_at=cutoff,
            recorded_at=datetime(2026, 8, 2, tzinfo=UTC),
        )

        def resolve(
            *,
            source: str = source_sha,
            run: str = run_id,
            results: tuple[str, ...] = (f"{run_id}:1",),
            at: datetime = cutoff,
        ) -> tuple[object, ...]:
            return resolve_governed_ocr_pages(
                conn,
                repo_root=repo_root,
                document_version_id=document_id,
                source_sha256=source,
                evidence_seal_id=receipt.evidence_seal_id,
                extraction_run_id=run,
                page_result_ids=results,
                cutoff_at=at,
            )

        projection = resolve_governed_ocr_pages(
            conn,
            repo_root=repo_root,
            document_version_id=document_id,
            source_sha256=source_sha,
            evidence_seal_id=receipt.evidence_seal_id,
            extraction_run_id=run_id,
            page_result_ids=(f"{run_id}:1",),
            cutoff_at=cutoff,
        )
        assert len(projection) == 1
        assert projection[0].text == "Revenue 25%"
        assert projection[0].source_representation == "governed_ocr"
        assert (
            projection[0].layout_sha256
            == hashlib.sha256(projection[0].layout_json.encode()).hexdigest()
        )
        with pytest.raises(DocumentProcessingEvidenceError):
            resolve(source="0" * 64)
        with pytest.raises(DocumentProcessingEvidenceError):
            resolve(run="wrong-engine-run")
        with pytest.raises(DocumentProcessingEvidenceError):
            resolve(results=(f"{run_id}:2",))
        with pytest.raises(DocumentProcessingEvidenceError):
            resolve(results=(f"{run_id}:1", f"{run_id}:2"))
        with pytest.raises(DocumentProcessingEvidenceError):
            resolve(at=datetime(2026, 7, 1, tzinfo=UTC))
        assert output.layout is not None
        retained_tsv = (
            repo_root
            / "data"
            / "ocr_evidence"
            / output.layout.tsv_sha256[:2]
            / output.layout.tsv_sha256
        )
        retained_tsv.write_bytes(retained_tsv.read_bytes().replace(b"25%", b"26%"))
        with pytest.raises(
            DocumentProcessingEvidenceError, match="ocr_projection_blob_bytes_mismatch"
        ):
            resolve()
    finally:
        conn.close()


def test_projection_rejects_sealed_confidence_unlike_retained_tsv(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, repo_root = _connection(tmp_path, migrated_db)
    _raw_tsv, _render_png, output = _output()
    try:
        backfill_ocr_evidence(
            conn,
            OCRBackfillRequest(repo_root=repo_root, apply=True, task_id="reader-confidence"),
            inspector=_Inspector(_required_preflight(1)),
            provider=_Provider([output]),
        )
        run_id, document_id, source_sha = conn.execute(
            "SELECT extraction_run_id,document_version_id,input_sha256 "
            "FROM evidence_extraction_runs WHERE extractor_name='governed-pdf-ocr'"
        ).fetchone()
        result = dict(
            conn.execute(
                "SELECT * FROM ocr_page_results WHERE extraction_run_id=?", (run_id,)
            ).fetchone()
        )
        layout_json = conn.execute(
            "SELECT text FROM evidence_nodes WHERE extraction_run_id=? AND node_kind='passage'",
            (run_id,),
        ).fetchone()[0]
        # Model an older or external writer that seals a self-consistent run
        # commitment but records a mean not present in the retained TSV.
        conn.execute("DROP TRIGGER trg_ocr_page_results_append_only")
        conn.execute("DROP TRIGGER trg_evidence_extraction_runs_append_only")
        conn.execute(
            "UPDATE ocr_page_results SET mean_confidence=99 WHERE extraction_run_id=?",
            (run_id,),
        )
        result["mean_confidence"] = 99.0
        run_output_sha = content_sha256_or_text(
            [
                {
                    "page_number": result["page_number"],
                    "outcome": result["outcome"],
                    "output_sha256": result["output_sha256"],
                    "mean_confidence": result["mean_confidence"],
                    "locator_sha256": result["locator_sha256"],
                    "reason_code": result["reason_code"],
                    "layout_sha256": content_sha256_or_text(layout_json),
                }
            ]
        )
        conn.execute(
            "UPDATE evidence_extraction_runs SET output_sha256=? WHERE extraction_run_id=?",
            (run_output_sha, run_id),
        )
        conn.commit()
        cutoff = datetime(2026, 8, 1, tzinfo=UTC)
        receipt = publish_document_processing_evidence(
            conn,
            document_version_id=document_id,
            processing_lane="pdf_ocr",
            cutoff_at=cutoff,
            recorded_at=datetime(2026, 8, 2, tzinfo=UTC),
        )
        with pytest.raises(
            DocumentProcessingEvidenceError, match="ocr_projection_confidence_mismatch"
        ):
            resolve_governed_ocr_pages(
                conn,
                repo_root=repo_root,
                document_version_id=document_id,
                source_sha256=source_sha,
                evidence_seal_id=receipt.evidence_seal_id,
                extraction_run_id=run_id,
                page_result_ids=(f"{run_id}:1",),
                cutoff_at=cutoff,
            )
    finally:
        conn.close()
