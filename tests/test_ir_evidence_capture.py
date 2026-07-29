"""IR evidence capture is bounded, authorized, raw-first, and provenance-native."""

from __future__ import annotations

import hashlib
import sqlite3
import urllib.parse
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from execution import capture_observed_ir_documents as cli
from ir_pipeline.authority import PublisherEndpointRule
from ir_pipeline.discover._docmeta import CandidateDoc
from ir_pipeline.discover.generic import CrawlPageOutcome, DocumentDiscoveryInventory
from ir_pipeline.evidence_capture import (
    IRDocumentCaptureError,
    IRDocumentCaptureRequest,
    IRDocumentCaptureResult,
    capture_observed_ir_documents,
)
from ir_pipeline.source_inventory import source_inventory_request, sync_ir_source_inventory

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
CONFIG_SHA = "d" * 64
INVENTORY_KEY = "issuer-acme:ir-crawl"
URL = "https://ir.acme.test/q4-2025-results.pdf"
BODY = b"%PDF-1.7 investor presentation bytes"


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        body: bytes = BODY,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {
            "Content-Type": "application/pdf",
            "Content-Length": str(len(body)),
        }
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for offset in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
        allow_redirects: bool,
    ) -> FakeResponse:
        assert headers["User-Agent"] == "research-agent test@example.test"
        assert timeout == (10, 60)
        assert stream
        assert not allow_redirects
        self.calls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path, *, source_url: str = URL) -> sqlite3.Connection:
    path = tmp_path / "ir-capture.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0220_source_inventory_seals")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    inventory = DocumentDiscoveryInventory(
        candidates=(
            CandidateDoc(
                url=source_url,
                link_text="Q4 2025 Results",
                filename_hint=source_url.rsplit("/", 1)[-1],
                doc_type_guess="press_release",
                year_guess=2025,
                quarter_guess=4,
                source_page="https://ir.acme.test/",
            ),
        ),
        pages=(
            CrawlPageOutcome(
                page_url="https://ir.acme.test/",
                outcome="succeeded",
                anchor_count=1,
                anchors=((source_url, "Q4 2025 Results"),),
            ),
        ),
        crawl_complete=True,
        crawl_stop_reason="frontier_exhausted",
    )
    request = source_inventory_request(
        issuer_id="issuer-acme",
        ticker="ACME",
        ir_url="https://ir.acme.test/",
        revision=1,
        inventory=inventory,
        retrieval_config_sha256=CONFIG_SHA,
        collector_code_version="ir-inventory@test",
        started_at=STAMP,
        completed_at=STAMP,
        recorded_at=STAMP,
        reconciled_at=STAMP,
        apply=True,
    )
    sync_ir_source_inventory(
        conn,
        request,
        blob_root=tmp_path / "inventory-blobs",
    )
    return conn


def _request(
    tmp_path: Path,
    *,
    apply: bool,
    rules: tuple[PublisherEndpointRule, ...] = (),
    maximum: int = 1_000_000,
) -> IRDocumentCaptureRequest:
    return IRDocumentCaptureRequest(
        inventory_keys=(INVENTORY_KEY,),
        publisher_file_rules=rules,
        checkpoint_root=tmp_path / "checkpoints",
        blob_root=tmp_path / "document-blobs",
        task_id="capture-ir",
        user_agent="research-agent test@example.test",
        apply=apply,
        max_document_bytes=maximum,
    )


def test_dry_run_streams_raw_checkpoint_without_database_or_durable_blob_write(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        result = capture_observed_ir_documents(
            conn,
            _request(tmp_path, apply=False),
            session=FakeSession([FakeResponse()]),
            robots_allows=lambda _url, _ua: True,
        )
        assert result.mode == "dry_run"
        assert result.fetched == 1
        assert result.incomplete_inventory_count == 1
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone() == (0,)
        digest = hashlib.sha256(BODY).hexdigest()
        assert (tmp_path / "checkpoints" / "capture-ir" / "responses" / digest).read_bytes() == BODY
        assert not (tmp_path / "document-blobs").exists()
    finally:
        conn.close()


def test_apply_promotes_verified_bytes_and_creates_legacy_free_document_version(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    dry = _request(tmp_path, apply=False)
    try:
        capture_observed_ir_documents(
            conn,
            dry,
            session=FakeSession([FakeResponse()]),
            robots_allows=lambda _url, _ua: True,
        )
        result = capture_observed_ir_documents(
            conn,
            dry.model_copy(update={"apply": True}),
            session=FakeSession([]),
            robots_allows=lambda _url, _ua: True,
        )
        assert result.fetched == 1
        assert conn.execute(
            "SELECT legacy_document_id, document_type, form_type FROM evidence_document_versions"
        ).fetchone() == (None, "press_release", "IR")
        assert conn.execute(
            "SELECT coverage_status, material_dissent FROM v_source_coverage_current"
        ).fetchone() == ("captured", 1)
        digest = hashlib.sha256(BODY).hexdigest()
        assert (tmp_path / "document-blobs" / digest[:2] / digest).read_bytes() == BODY
    finally:
        conn.close()


def test_robots_denial_is_explicit_and_prevents_network_access(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    session = FakeSession([])
    try:
        result = capture_observed_ir_documents(
            conn,
            _request(tmp_path, apply=True),
            session=session,
            robots_allows=lambda _url, _ua: False,
        )
        assert result.items[0].outcome == "robots_denied"
        assert result.items[0].reason_code == "ir_robots_denied"
        assert session.calls == []
        assert conn.execute("SELECT coverage_status FROM v_source_coverage_current").fetchone() == (
            "fetch_failed",
        )
    finally:
        conn.close()


def test_streaming_byte_budget_rejects_oversized_response_without_raw_artifact(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        result = capture_observed_ir_documents(
            conn,
            _request(tmp_path, apply=False, maximum=8),
            session=FakeSession(
                [
                    FakeResponse(
                        body=b"0123456789",
                        headers={"Content-Type": "application/pdf"},
                    )
                ]
            ),
            robots_allows=lambda _url, _ua: True,
        )
        assert result.items[0].outcome == "contract_failure"
        assert result.items[0].reason_code == "ir_document_too_large"
        response_root = tmp_path / "checkpoints" / "capture-ir" / "responses"
        assert not response_root.exists() or not tuple(response_root.iterdir())
    finally:
        conn.close()


def test_cross_host_redirect_requires_explicit_publisher_endpoint_rule(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    redirect = FakeResponse(
        status_code=302,
        body=b"",
        headers={"Location": "https://cdn.unapproved.test/q4.pdf"},
    )
    try:
        result = capture_observed_ir_documents(
            conn,
            _request(tmp_path, apply=False),
            session=FakeSession([redirect]),
            robots_allows=lambda _url, _ua: True,
        )
        assert result.items[0].outcome == "identity_rejected"
        assert result.items[0].reason_code == "ir_redirect_not_authorized"
    finally:
        conn.close()


def test_authorized_redirect_preserves_requested_and_final_url_observations(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    session = FakeSession(
        [
            FakeResponse(
                status_code=302,
                body=b"",
                headers={"Location": "/downloads/q4-final.pdf"},
            ),
            FakeResponse(),
        ]
    )
    try:
        result = capture_observed_ir_documents(
            conn,
            _request(tmp_path, apply=True),
            session=session,
            robots_allows=lambda _url, _ua: True,
        )
        assert result.fetched == 1
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT source_url FROM evidence_source_observations "
                "WHERE source_kind = 'ir_document'"
            ).fetchall()
        } == {
            URL,
            "https://ir.acme.test/downloads/q4-final.pdf",
        }
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT link_kind FROM evidence_document_observation_links"
            ).fetchall()
        } == {"primary", "retrieval"}
    finally:
        conn.close()


def test_explicit_cross_host_publisher_endpoint_can_be_captured(tmp_path: Path) -> None:
    source_url = "https://cdn.publisher.test/reports/q4.pdf"
    conn = _conn(tmp_path, source_url=source_url)
    try:
        result = capture_observed_ir_documents(
            conn,
            _request(
                tmp_path,
                apply=False,
                rules=(
                    PublisherEndpointRule(
                        host="cdn.publisher.test",
                        path_prefix="/reports/",
                    ),
                ),
            ),
            session=FakeSession([FakeResponse()]),
            robots_allows=lambda _url, _ua: True,
        )
        assert result.fetched == 1
    finally:
        conn.close()


def test_capture_fails_closed_when_raw_discovery_blob_is_tampered(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    storage_uri = conn.execute(
        "SELECT location.storage_uri FROM source_inventory_components AS component "
        "JOIN evidence_source_observations AS observation "
        "ON observation.observation_id = component.source_observation_id "
        "JOIN v_evidence_blob_locations_current AS location "
        "ON location.blob_sha256 = observation.blob_sha256 "
        "WHERE component.component_key = 'candidate-inventory'"
    ).fetchone()[0]
    parsed = urllib.parse.urlparse(str(storage_uri))
    path_text = urllib.parse.unquote(parsed.path)
    if len(path_text) >= 3 and path_text[0] == "/" and path_text[2] == ":":
        path_text = path_text[1:]
    Path(path_text).write_bytes(b"tampered")
    try:
        with pytest.raises(IRDocumentCaptureError, match="digest"):
            capture_observed_ir_documents(
                conn,
                _request(tmp_path, apply=False),
                session=FakeSession([]),
                robots_allows=lambda _url, _ua: True,
            )
    finally:
        conn.close()


def test_cli_defaults_to_locked_read_only_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = _conn(tmp_path)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    captured_request: list[IRDocumentCaptureRequest] = []

    def _capture(
        _conn: sqlite3.Connection,
        request: IRDocumentCaptureRequest,
        *,
        session: object,
    ) -> IRDocumentCaptureResult:
        assert session is fake_session
        captured_request.append(request)
        return IRDocumentCaptureResult(
            task_id=request.task_id,
            mode="dry_run",
            considered=0,
            fetched=0,
            deferred=0,
            failed=0,
            complete_inventory_count=0,
            incomplete_inventory_count=0,
            records_created=0,
            records_replayed=0,
            has_more=False,
            items=(),
        )

    fake_session = FakeSession([])
    monkeypatch.setattr(cli, "capture_observed_ir_documents", _capture)
    monkeypatch.setattr(cli.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    exit_code = cli.main(
        [
            "--db",
            str(db_path),
            "--inventory-key",
            INVENTORY_KEY,
            "--checkpoint-root",
            str(tmp_path / "checkpoints"),
            "--blob-root",
            str(tmp_path / "durable-blobs"),
            "--task-id",
            "cli-ir",
            "--user-agent",
            "research-agent test@example.test",
        ]
    )
    output = capsys.readouterr()
    assert exit_code == 0
    assert captured_request[0].apply is False
    assert '"mode":"dry_run"' in output.out
    assert "ir_evidence_capture_completed" in output.err
    assert not (tmp_path / "durable-blobs").exists()
