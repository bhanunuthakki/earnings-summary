"""IR evidence capture is bounded, authorized, raw-first, and provenance-native."""

from __future__ import annotations

import hashlib
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

from alembic import command
from execution import capture_observed_ir_documents as cli
from ir_pipeline import evidence_capture
from ir_pipeline._net import UnsafeURLError
from ir_pipeline.authority import PublisherEndpointRule
from ir_pipeline.discover import generic
from ir_pipeline.discover._docmeta import CandidateDoc
from ir_pipeline.discover.generic import CrawlPageOutcome, DocumentDiscoveryInventory
from ir_pipeline.evidence_capture import (
    ExactIRFetchRequest,
    IRDocumentCaptureError,
    IRDocumentCaptureRequest,
    IRDocumentCaptureResult,
    capture_observed_ir_documents,
    fetch_exact_ir_bytes,
)
from ir_pipeline.source_inventory import source_inventory_request, sync_ir_source_inventory

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
CONFIG_SHA = "d" * 64
INVENTORY_KEY = "issuer-acme:ir-crawl"
URL = "https://ir.acme.test/q4-2025-results.pdf"
EXACT_PUBLIC_URL = "https://93.184.216.34/q4-2025-results.pdf"
BODY = b"%PDF-1.7 investor presentation bytes"
ROBOTS_ALLOWS = cast(
    Callable[[str, str], bool],
    getattr(evidence_capture, "_robots_allows"),
)


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


class FakeRobotsResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, maximum: int = -1) -> bytes:
        return self.body if maximum < 0 else self.body[:maximum]

    def __enter__(self) -> FakeRobotsResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeRobotsOpener:
    def __init__(self, body: bytes | Exception) -> None:
        self.body = body
        self.requests: list[urllib.request.Request] = []

    def open(
        self,
        request: urllib.request.Request,
        *,
        timeout: int,
    ) -> FakeRobotsResponse:
        assert timeout == 15
        self.requests.append(request)
        if isinstance(self.body, Exception):
            raise self.body
        return FakeRobotsResponse(self.body)


def _safe_public_url(url: str) -> str:
    return url


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


def _exact_request(
    tmp_path: Path,
    *,
    authority_url: str = "https://ir.acme.test/financials",
    exact_url: str = EXACT_PUBLIC_URL,
    task_id: str = "exact-ir",
) -> ExactIRFetchRequest:
    parsed = urllib.parse.urlparse(exact_url)
    assert parsed.hostname is not None
    return ExactIRFetchRequest(
        candidate_id="a" * 64,
        authority_url=authority_url,
        exact_url=exact_url,
        publisher_file_rules=(
            PublisherEndpointRule(host=parsed.hostname, path_prefix=parsed.path),
        ),
        checkpoint_root=tmp_path / "exact-checkpoints",
        blob_root=tmp_path / "exact-blobs",
        task_id=task_id,
        user_agent="research-agent test@example.test",
    )


def test_exact_capture_resolves_authority_policy_and_applies_it_to_cdn_url(
    tmp_path: Path,
) -> None:
    authority = "https://investors.wix.com/financials"
    exact = "https://4f4a3186-9467-4c09-aa74-51fe1affec20.usrfiles.com/ugd/report.pdf"
    resolver_urls: list[str] = []
    predicate_urls: list[str] = []
    sleeps: list[float] = []

    def _resolver(url: str) -> tuple[Callable[[str], bool], float]:
        resolver_urls.append(url)

        def _predicate(candidate_url: str) -> bool:
            predicate_urls.append(candidate_url)
            return True

        return _predicate, 30.0

    session = FakeSession([FakeResponse()])
    result = fetch_exact_ir_bytes(
        _exact_request(tmp_path, authority_url=authority, exact_url=exact),
        session=session,
        robots_policy_resolver=_resolver,
        sleeper=sleeps.append,
    )

    assert result.network_fetched
    assert resolver_urls == [authority]
    assert predicate_urls == [exact]
    assert sleeps == [12.0]
    assert session.calls == [exact]


def test_exact_capture_robots_override_has_zero_delay_and_no_resolver(
    tmp_path: Path,
) -> None:
    session = FakeSession([FakeResponse()])

    def _unexpected_resolver(_url: str) -> tuple[Callable[[str], bool], float]:
        raise AssertionError("test override must bypass production policy resolution")

    result = fetch_exact_ir_bytes(
        _exact_request(tmp_path),
        session=session,
        robots_allows=lambda _url, _agent: True,
        robots_policy_resolver=_unexpected_resolver,
        sleeper=lambda _delay: (_ for _ in ()).throw(AssertionError("unexpected sleep")),
    )

    assert result.network_fetched


def test_exact_capture_rechecks_prior_robots_denial_and_can_recover(tmp_path: Path) -> None:
    request = _exact_request(tmp_path)
    with pytest.raises(IRDocumentCaptureError, match="ir_robots_denied"):
        fetch_exact_ir_bytes(
            request,
            session=FakeSession([]),
            robots_allows=lambda _url, _agent: False,
        )
    session = FakeSession([FakeResponse()])
    result = fetch_exact_ir_bytes(
        request,
        session=session,
        robots_allows=lambda _url, _agent: True,
    )

    assert result.network_fetched
    assert session.calls == [EXACT_PUBLIC_URL]


def test_exact_capture_fetched_checkpoint_replays_without_policy_or_network(
    tmp_path: Path,
) -> None:
    request = _exact_request(tmp_path)
    first = fetch_exact_ir_bytes(
        request,
        session=FakeSession([FakeResponse()]),
        robots_allows=lambda _url, _agent: True,
    )

    def _unexpected_resolver(_url: str) -> tuple[Callable[[str], bool], float]:
        raise AssertionError("fetched replay must not resolve robots policy")

    replay = fetch_exact_ir_bytes(
        request,
        session=FakeSession([]),
        robots_policy_resolver=_unexpected_resolver,
        sleeper=lambda _delay: (_ for _ in ()).throw(AssertionError("unexpected sleep")),
    )

    assert not replay.network_fetched
    assert replay.content_sha256 == first.content_sha256


def test_exact_capture_denial_prevents_document_or_blob_write(tmp_path: Path) -> None:
    with pytest.raises(IRDocumentCaptureError, match="ir_robots_denied"):
        fetch_exact_ir_bytes(
            _exact_request(tmp_path),
            session=FakeSession([]),
            robots_policy_resolver=lambda _authority: (lambda _url: False, 0.0),
        )

    assert not (tmp_path / "exact-blobs").exists()
    response_root = tmp_path / "exact-checkpoints" / "exact-ir" / "responses"
    assert not response_root.exists() or not tuple(response_root.iterdir())


def test_exact_capture_authorizes_url_before_resolving_robots_policy(tmp_path: Path) -> None:
    request = _exact_request(tmp_path).model_copy(
        update={
            "exact_url": "https://evil.example/forged.pdf",
            "publisher_file_rules": (
                PublisherEndpointRule(host="cdn.publisher.test", path_prefix="/approved/"),
            ),
        }
    )

    def _unexpected_resolver(_url: str) -> tuple[Callable[[str], bool], float]:
        raise AssertionError("authorization failure must precede robots policy")

    with pytest.raises(IRDocumentCaptureError, match="not authorized"):
        fetch_exact_ir_bytes(
            request,
            session=FakeSession([]),
            robots_policy_resolver=_unexpected_resolver,
        )


@pytest.mark.parametrize("failure", [404, 403, "unreachable"])
def test_canonical_missing_or_unreachable_authority_robots_allows_exact_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: int | str,
) -> None:
    class _UnavailableOpener:
        def open(self, _request: urllib.request.Request, *, timeout: int) -> FakeRobotsResponse:
            assert timeout == 15
            if isinstance(failure, int):
                raise urllib.error.HTTPError("robots", failure, "unavailable", Message(), None)
            raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(generic, "ensure_safe_public_url", _safe_public_url)
    monkeypatch.setattr(generic, "build_public_opener", _UnavailableOpener)
    monkeypatch.setattr(evidence_capture, "ensure_safe_public_url", _safe_public_url)
    result = fetch_exact_ir_bytes(
        _exact_request(tmp_path, task_id=f"robots-{failure}"),
        session=FakeSession([FakeResponse()]),
        sleeper=lambda _delay: None,
    )

    assert result.network_fetched


def test_canonical_unsafe_authority_robots_denies_exact_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unsafe_authority(_url: str) -> str:
        raise UnsafeURLError("private authority")

    monkeypatch.setattr(generic, "ensure_safe_public_url", _unsafe_authority)
    monkeypatch.setattr(evidence_capture, "ensure_safe_public_url", _safe_public_url)

    with pytest.raises(IRDocumentCaptureError, match="ir_robots_denied"):
        fetch_exact_ir_bytes(
            _exact_request(tmp_path),
            session=FakeSession([]),
            sleeper=lambda _delay: None,
        )


def test_canonical_authority_disallow_is_applied_to_exact_cdn_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = FakeRobotsOpener(b"User-agent: *\nDisallow: /ugd/\n")
    monkeypatch.setattr(generic, "ensure_safe_public_url", _safe_public_url)
    monkeypatch.setattr(generic, "build_public_opener", lambda: opener)
    monkeypatch.setattr(evidence_capture, "ensure_safe_public_url", _safe_public_url)
    session = FakeSession([])

    with pytest.raises(IRDocumentCaptureError, match="ir_robots_denied"):
        fetch_exact_ir_bytes(
            _exact_request(
                tmp_path,
                authority_url="https://investors.wix.com/financials",
                exact_url=(
                    "https://4f4a3186-9467-4c09-aa74-51fe1affec20.usrfiles.com/ugd/report.pdf"
                ),
            ),
            session=session,
            sleeper=lambda _delay: None,
        )

    assert opener.requests[0].full_url == "https://investors.wix.com/robots.txt"
    assert session.calls == []


@pytest.mark.parametrize(
    ("authority_url", "exact_url"),
    [
        (
            "https://investors.wix.com/financials",
            "https://4f4a3186-9467-4c09-aa74-51fe1affec20.usrfiles.com/ugd/report.pdf",
        ),
        (
            "https://ir.rubrik.com/financials/quarterly-results/default.aspx",
            "https://s203.q4cdn.com/667520861/files/doc_financials/report.pdf",
        ),
    ],
)
def test_exact_capture_accepts_approved_wix_and_rubrik_cdn_fixtures(
    tmp_path: Path,
    authority_url: str,
    exact_url: str,
) -> None:
    policy_urls: list[str] = []
    predicate_urls: list[str] = []

    def _resolver(url: str) -> tuple[Callable[[str], bool], float]:
        policy_urls.append(url)

        def _allows(candidate_url: str) -> bool:
            predicate_urls.append(candidate_url)
            return True

        return _allows, 0.0

    result = fetch_exact_ir_bytes(
        _exact_request(
            tmp_path,
            authority_url=authority_url,
            exact_url=exact_url,
            task_id="publisher-cdn",
        ),
        session=FakeSession([FakeResponse()]),
        robots_policy_resolver=_resolver,
    )

    assert result.final_url == exact_url
    assert policy_urls == [authority_url]
    assert predicate_urls == [exact_url]


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


def test_robots_fetch_uses_caller_agent_and_honors_permissive_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = FakeRobotsOpener(b"User-agent: *\nAllow: /\n")
    monkeypatch.setattr(evidence_capture, "ensure_safe_public_url", _safe_public_url)
    monkeypatch.setattr(evidence_capture, "build_public_opener", lambda: opener)

    assert ROBOTS_ALLOWS(
        URL,
        "research-agent test@example.test",
    )
    request = opener.requests[0]
    assert request.full_url == "https://ir.acme.test/robots.txt"
    assert request.get_header("User-agent") == "research-agent test@example.test"


def test_robots_fetch_honors_explicit_disallow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = FakeRobotsOpener(b"User-agent: research-agent\nDisallow: /private/\n")
    monkeypatch.setattr(evidence_capture, "ensure_safe_public_url", _safe_public_url)
    monkeypatch.setattr(evidence_capture, "build_public_opener", lambda: opener)

    assert not ROBOTS_ALLOWS(
        "https://ir.acme.test/private/q4.pdf",
        "research-agent test@example.test",
    )


@pytest.mark.parametrize(
    ("guard_error", "network_error"),
    [
        (UnsafeURLError("private address"), None),
        (None, OSError("network unavailable")),
    ],
)
def test_robots_fetch_fails_closed_on_unsafe_or_network_failure(
    monkeypatch: pytest.MonkeyPatch,
    guard_error: Exception | None,
    network_error: Exception | None,
) -> None:
    def _guard(url: str) -> str:
        if guard_error is not None:
            raise guard_error
        return url

    opener = FakeRobotsOpener(network_error or b"User-agent: *\nAllow: /\n")
    monkeypatch.setattr(evidence_capture, "ensure_safe_public_url", _guard)
    monkeypatch.setattr(evidence_capture, "build_public_opener", lambda: opener)

    assert not ROBOTS_ALLOWS(
        URL,
        "research-agent test@example.test",
    )


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
