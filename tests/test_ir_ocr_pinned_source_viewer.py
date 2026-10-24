"""OCR source clicks render the admitted bytes, not a mutable legacy PDF path."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import fitz
import pytest
from comments_server_content_routes import ContentRouteContext, register_content_routes
from flask import Flask
from flask.testing import FlaskClient

from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.source_read_context import SourceReadContext
from tests.test_source_fact_repository import seed_foundation

STAMP = datetime(2026, 7, 1, tzinfo=UTC)


def _pdf(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page(width=72, height=72)
    page.insert_text((8, 35), text, fontsize=9)
    raw = document.tobytes(garbage=4, deflate=True)
    document.close()
    return raw


@pytest.fixture
def pinned_source(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Iterator[tuple[FlaskClient, Path, Path, str]]:
    db_path = migrated_db(tmp_path / "ocr-viewer.db")
    state_root = tmp_path / "state"
    source_path = state_root / "ir_documents" / "source.pdf"
    source_path.parent.mkdir(parents=True)
    source = _pdf("ADMITTED")
    source_path.write_bytes(source)
    mutable_path = tmp_path / "legacy.pdf"
    mutable_path.write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    seed_foundation(conn)
    conn.execute(
        "INSERT INTO documents "
        "(id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
        "VALUES (1,'TEST','ir_doc','ir_supplement',?,?,?,?,?)",
        (str(mutable_path), digest, STAMP.isoformat(), "ok", len(source)),
    )
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(source),
            media_type="application/pdf",
            storage_uri=source_path.as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="ocr-viewer-source",
            idempotency_key="ocr-viewer-source",
            source_kind="ir_doc",
            source_url="https://example.test/issuer/supplement.pdf",
            blob_sha256=digest,
            source_published_at=STAMP,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="a" * 64,
            collector_code_version="fixture-v1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="ocr-viewer-version",
            document_key="ocr-viewer-document",
            version_sequence=1,
            observation_id="ocr-viewer-source",
            blob_sha256=digest,
            issuer_id="issuer-1",
            ticker="TEST",
            document_type="ir_supplement",
            form_type="ir_supplement",
            period_end=STAMP,
            as_of_at=STAMP,
            language="en",
            legacy_document_id=1,
            recorded_at=STAMP,
        )
    )
    conn.commit()
    app = Flask(__name__)
    register_content_routes(
        app,
        ContentRouteContext(
            repo_root=tmp_path,
            db_path=db_path,
            open_db=lambda: conn,
            get_read_db=lambda: conn,
            safe_ticker=lambda ticker: ticker,
            build_ticker_command_center=lambda _root, _ticker: pytest.fail("unrelated read"),
            linked_gsheet=lambda _root, _ticker: (None, None),
            probe_tracker=lambda: (False, "isolated"),
            fetch_live_portfolio=lambda: pytest.fail("tracker access prohibited"),
            default_user_id="synthetic",
            source_context=SourceReadContext.for_product_state_root(state_root),
        ),
    )
    try:
        yield app.test_client(), mutable_path, source_path, digest
    finally:
        conn.close()


def test_pinned_ocr_page_and_image_ignore_changed_legacy_path(
    pinned_source: tuple[FlaskClient, Path, Path, str],
) -> None:
    client, mutable_path, _source_path, digest = pinned_source
    query = {"version": "ocr-viewer-version", "sha256": digest, "page": 1, "bbox": "1,2,20,15"}
    page = client.get("/source/1", query_string=query)
    assert page.status_code == 200
    assert "sv-pdf-hit" in page.text
    assert "left:1.39%;top:2.78%;width:26.39%;height:18.06%" in page.text
    assert "version=ocr-viewer-version" in page.text
    image_url = f"/source/1/page/1.png?version=ocr-viewer-version&sha256={digest}"
    original = client.get(image_url)
    assert original.status_code == 200 and original.data.startswith(b"\x89PNG\r\n\x1a\n")
    mutable_path.write_bytes(_pdf("MUTATED"))
    assert client.get("/source/1", query_string=query).status_code == 200
    after = client.get(image_url)
    assert after.status_code == 200 and after.data == original.data


def test_pinned_ocr_source_fails_closed_when_version_or_retained_bytes_change(
    pinned_source: tuple[FlaskClient, Path, Path, str],
) -> None:
    client, _mutable_path, source_path, digest = pinned_source
    query = {"version": "ocr-viewer-version", "sha256": digest, "page": 1}
    assert client.get("/source/1", query_string={**query, "sha256": "a" * 64}).status_code == 404
    assert client.get("/source/1", query_string={**query, "version": "other"}).status_code == 404
    source_path.write_bytes(_pdf("CHANGED SOURCE"))
    assert client.get("/source/1", query_string=query).status_code == 404
    assert (
        client.get(f"/source/1/page/1.png?version=ocr-viewer-version&sha256={digest}").status_code
        == 404
    )
