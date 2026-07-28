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
from execution.backfill_pdf_table_evidence import main
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.pdf_table_backfill import (
    PdfTableBackfillIntegrityError,
    PdfTableBackfillRequest,
    backfill_pdf_table_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 7, 27, 12, tzinfo=UTC)
T1 = datetime(2026, 7, 27, 13, tzinfo=UTC)


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture(scope="session")
def pdf_table_backfill_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("pdf-table-backfill-schema") / "template.db"
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
    command.upgrade(config, "head")
    return path


@pytest.fixture
def db_path(
    tmp_path: Path,
    pdf_table_backfill_template: Path,
) -> Iterator[Path]:
    path = tmp_path / "case.db"
    shutil.copy2(pdf_table_backfill_template, path)
    yield path


def _pdf(*, image_only: bool = False) -> bytes:
    document = fitz.open()
    page = document.new_page(width=320, height=220)
    if image_only:
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
        pixmap.clear_with(255)
        page.insert_image((5, 5, 315, 215), pixmap=pixmap)
    else:
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
    raw = document.tobytes(garbage=4, deflate=True)
    document.close()
    return raw


def _seed_document(
    db_path: Path,
    content_root: Path,
    document_version_id: str,
    raw_pdf: bytes,
) -> Path:
    content_root.mkdir(parents=True, exist_ok=True)
    blob_sha256 = hashlib.sha256(raw_pdf).hexdigest()
    path = content_root / f"{blob_sha256}.pdf"
    path.write_bytes(raw_pdf)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob_sha256,
            byte_size=len(raw_pdf),
            media_type="application/pdf",
            storage_uri=path.as_uri(),
            recorded_at=T0,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=f"obs:{document_version_id}",
            idempotency_key=f"obs:{document_version_id}",
            source_kind="issuer",
            source_url=f"https://issuer.example/{document_version_id}.pdf",
            blob_sha256=blob_sha256,
            source_published_at=T0,
            filing_at=None,
            accepted_at=None,
            observed_at=T0,
            retrieved_at=T0,
            retrieval_config_sha256="a" * 64,
            collector_code_version="fixture@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=document_version_id,
            document_key=f"document:{document_version_id}",
            version_sequence=1,
            observation_id=f"obs:{document_version_id}",
            blob_sha256=blob_sha256,
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
    conn.commit()
    conn.close()
    return path


def _request(
    db_path: Path,
    tmp_path: Path,
    content_root: Path,
    ids: tuple[str, ...],
    *,
    apply: bool = False,
    task_id: str = "pdf-table-test",
    batch_size: int = 25,
) -> PdfTableBackfillRequest:
    return PdfTableBackfillRequest(
        db_path=db_path,
        repo_root=tmp_path,
        content_roots=(content_root,),
        document_version_ids=ids,
        recorded_at=T1,
        apply=apply,
        task_id=task_id,
        batch_size=batch_size,
    )


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def test_dry_run_is_default_and_writes_neither_database_nor_checkpoint(
    db_path: Path,
    tmp_path: Path,
) -> None:
    content_root = tmp_path / "blobs"
    _seed_document(db_path, content_root, "doc-1", _pdf())

    summary = backfill_pdf_table_evidence(_request(db_path, tmp_path, content_root, ("doc-1",)))

    assert summary.mode == "dry_run"
    assert summary.documents_sealed == 1
    assert summary.items[0].artifact_id is None
    assert _count(db_path, "evidence_extraction_runs") == 0
    assert _count(db_path, "pdf_table_extraction_artifact_headers") == 0
    assert not (tmp_path / ".tmp" / "pdf-table-test" / "state.json").exists()


def test_apply_is_atomic_and_exact_replay_is_idempotent(
    db_path: Path,
    tmp_path: Path,
) -> None:
    content_root = tmp_path / "blobs"
    _seed_document(db_path, content_root, "doc-1", _pdf())

    first = backfill_pdf_table_evidence(
        _request(
            db_path,
            tmp_path,
            content_root,
            ("doc-1",),
            apply=True,
            task_id="first-application",
        )
    )
    replay = backfill_pdf_table_evidence(
        _request(
            db_path,
            tmp_path,
            content_root,
            ("doc-1",),
            apply=True,
            task_id="exact-replay",
        )
    )

    assert first.extraction_runs_created == 1
    assert first.artifacts_created == 1
    assert first.artifacts_replayed == 0
    assert replay.extraction_runs_created == 0
    assert replay.artifacts_created == 0
    assert replay.artifacts_replayed == 1
    assert replay.items[0].exact_replay is True
    assert _count(db_path, "evidence_extraction_runs") == 1
    assert _count(db_path, "pdf_table_extraction_artifact_headers") == 1
    assert _count(db_path, "pdf_table_extraction_artifact_seals") == 1


def test_quarantine_is_persisted_but_never_admitted(
    db_path: Path,
    tmp_path: Path,
) -> None:
    content_root = tmp_path / "blobs"
    _seed_document(db_path, content_root, "doc-image", _pdf(image_only=True))

    summary = backfill_pdf_table_evidence(
        _request(
            db_path,
            tmp_path,
            content_root,
            ("doc-image",),
            apply=True,
        )
    )

    assert summary.documents_quarantined == 1
    assert summary.admitted_count == 0
    assert summary.items[0].quarantine_reason == "one_or_more_pages_quarantined"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT disposition FROM pdf_table_extraction_artifact_headers"
        ).fetchone() == ("quarantined",)
        assert conn.execute(
            "SELECT COUNT(*) FROM document_processing_evidence_headers"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_hash_mismatch_is_a_hard_stop_without_writes_or_checkpoint(
    db_path: Path,
    tmp_path: Path,
) -> None:
    content_root = tmp_path / "blobs"
    path = _seed_document(db_path, content_root, "doc-1", _pdf())
    raw = path.read_bytes()
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])

    with pytest.raises(PdfTableBackfillIntegrityError, match="SHA-256 mismatch"):
        backfill_pdf_table_evidence(
            _request(
                db_path,
                tmp_path,
                content_root,
                ("doc-1",),
                apply=True,
            )
        )

    assert _count(db_path, "evidence_extraction_runs") == 0
    assert _count(db_path, "pdf_table_extraction_artifact_headers") == 0
    assert not (tmp_path / ".tmp" / "pdf-table-test" / "state.json").exists()


def test_batch_cap_and_keyset_checkpoint_resume_exact_selection(
    db_path: Path,
    tmp_path: Path,
) -> None:
    content_root = tmp_path / "blobs"
    for document_version_id in ("doc-1", "doc-2", "doc-3"):
        _seed_document(db_path, content_root, document_version_id, _pdf())
    request = _request(
        db_path,
        tmp_path,
        content_root,
        ("doc-1", "doc-2", "doc-3"),
        apply=True,
        task_id="bounded-resume",
        batch_size=2,
    )

    first = backfill_pdf_table_evidence(request)
    second = backfill_pdf_table_evidence(request)

    assert first.documents_considered == 2
    assert first.has_more is True
    assert second.documents_considered == 1
    assert second.has_more is False
    assert second.last_evidence_rowid_before == first.last_evidence_rowid_after
    assert _count(db_path, "pdf_table_extraction_artifact_headers") == 3
    checkpoint = (tmp_path / ".tmp" / "bounded-resume" / "state.json").read_text(encoding="utf-8")
    assert '"last_document_version_id":"doc-3"' in checkpoint


def test_cli_keeps_data_on_stdout_and_structured_events_on_stderr(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    content_root = tmp_path / "blobs"
    _seed_document(db_path, content_root, "doc-1", _pdf())

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--document-version-id",
                "doc-1",
                "--content-root",
                str(content_root),
                "--repo-root",
                str(tmp_path),
                "--recorded-at",
                T1.isoformat(),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    events = [json.loads(line) for line in captured.err.splitlines()]
    assert output["mode"] == "dry_run"
    assert output["documents_considered"] == 1
    assert events[-1]["event"] == "pdf_table_evidence_backfill_completed"
