"""Raw publisher authority surfaces are bounded, immutable, and hash-bound."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlunsplit

import pytest

from execution import capture_ir_authority_surfaces as cli
from ir_pipeline.authority import IRAuthorityEvidence, PublisherSurfaceEvidence, SurfaceOutcome
from ir_pipeline.authority_capture import (
    IRAuthorityCaptureError,
    IRAuthorityCaptureIdentityError,
    IRAuthorityCaptureRequest,
    IRAuthorityCaptureSpec,
    MZCatalogPost,
    capture_ir_authority_surfaces,
    verified_mz_authority_documents,
)
from ir_pipeline.discover import generic
from ir_pipeline.discover._docmeta import CandidateDoc
from ir_pipeline.discover.generic import CrawlPageOutcome, DocumentDiscoveryInventory
from ir_pipeline.discover.mz import catalog_post_body
from ir_pipeline.source_inventory import source_inventory_request, sync_ir_source_inventory
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.evidence_native_candidates import resolve_local_storage_uri
from provenance.issuer_registry import (
    IssuerEntity,
    IssuerRegistry,
    LegacyIssuerBindingRevision,
)
from run_lock import hold_run_lock, lock_path_for
from sqlite_runtime import SQLiteConnectionRole

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 21, 0, tzinfo=UTC)
URL = "https://ir.acme.test/archive"
DOCUMENT_URL = "https://ir.acme.test/q4-2025-results.pdf"
BODY = b"<html><a href='/q4-2025-results.pdf'>Q4 results</a></html>"


def _url_with_userinfo(userinfo_value: str = "placeholder") -> str:
    return urlunsplit(("https", f"user:{userinfo_value}@ir.acme.test", "/archive", "", ""))


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
            "Content-Type": "text/html; charset=utf-8",
            "Content-Length": str(len(body)),
        }
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for offset in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: Sequence[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
        allow_redirects: bool,
    ) -> FakeResponse:
        assert timeout == (3, 7)
        assert stream
        assert not allow_redirects
        assert set(headers) == {"User-Agent", "Accept"}
        self.calls.append((url, headers))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(
        self,
        url: str,
        *,
        data: bytes,
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
        allow_redirects: bool,
    ) -> FakeResponse:
        assert timeout == (3, 7)
        assert stream and not allow_redirects
        assert headers["Content-Type"] == "application/json"
        self.calls.append((url, {**headers, "body_sha256": hashlib.sha256(data).hexdigest()}))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    path = tmp_path / "authority-capture.db"
    migrated_db(
        path,
        stamp="0213_decision_draft_provider_id",
        archived=True,
        target="0227_issuer_reporting_registry",
    )
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_registry(conn)
    return conn


def _full_conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    """Full at-head DB via the squashed active template (which carries the
    baseline tables ``db.init_db()`` used to provide before the chain ran)."""
    path = tmp_path / "authority-capture-full.db"
    migrated_db(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_registry(conn)
    return conn


def _seed_registry(conn: sqlite3.Connection) -> None:
    registry = IssuerRegistry(conn)
    registry.persist(
        IssuerEntity(
            issuer_id="issuer-acme",
            idempotency_key="issuer-acme",
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    registry.persist(
        LegacyIssuerBindingRevision(
            binding_revision_id="binding-acme-1",
            idempotency_key="binding-acme-1",
            recorded_issuer_id="legacy-ticker:ACME",
            revision=1,
            issuer_id="issuer-acme",
            outcome="selected",
            decision_kind="deterministic",
            reason_code="test_fixture",
            reason_details=(("ticker", "ACME"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    conn.commit()


def _request(
    *,
    outcome: SurfaceOutcome = "exhausted",
    source_url: str = URL,
    document_url: str = DOCUMENT_URL,
    maximum: int = 1_000_000,
) -> IRAuthorityCaptureRequest:
    return IRAuthorityCaptureRequest(
        issuer_id="issuer-acme",
        ticker="ACME",
        authority_basis="publisher_archive",
        asserted_at=STAMP,
        user_agent="research-agent test@example.test",
        connect_timeout_seconds=3,
        read_timeout_seconds=7,
        max_surface_bytes=maximum,
        max_redirects=2,
        surfaces=(
            IRAuthorityCaptureSpec(
                surface_key="archive",
                surface_kind="archive",
                source_url=source_url,
                traversal_kind="pagination",
                outcome=outcome,
                required=True,
                terminal_condition=("next_link_absent" if outcome == "exhausted" else None),
                observed_document_urls=(document_url,),
                verification_method="publisher_archive_html",
                revision=1,
                supersedes_surface_revision_id=None,
            ),
        ),
    )


MZ_BASE = "https://api.mziq.com/mzfilemanager"
MZ_RESULTS = "https://www.investidores.nu/financials/results-center/"
MZ_FILE_2025 = "https://api.mziq.com/mzfilemanager/v2/d/issuer-id/q4-2025"
MZ_FILE_2026 = "https://api.mziq.com/mzfilemanager/v2/d/issuer-id/q1-2026"
MZ_PAGE = (
    b"<script>const fmId='issuer-id';const fmBase='https://api.mziq.com/mzfilemanager';"
    b"categories.push({internal_name:'release_resultados'});</script>"
)


def test_mz_catalog_retains_accented_publisher_category_bytes() -> None:
    category = "videoconferência"
    post = MZCatalogPost(results_surface_key="results", categories=(category,), year=2026)
    assert post.categories == (category,)
    assert catalog_post_body(post.categories, year=None) == (
        b'{"categoryInternalNames": ["videoconfer\\u00eancia"], "language_code": "en_US"}'
    )
    assert catalog_post_body(post.categories, year=2026) == (
        b'{"year": "2026", "categories": ["videoconfer\\u00eancia"], '
        b'"language": "en_US", "published": true}'
    )


@pytest.mark.parametrize(
    "category",
    [
        "bad category",
        "bad/category",
        "bad-category",
        "bad\ncategory",
        "bad\u202ecategory",
        "bad\u200dcategory",
        "x" * 129,
    ],
)
def test_mz_catalog_rejects_unsafe_publisher_categories(category: str) -> None:
    with pytest.raises(ValueError, match="MZ categories"):
        MZCatalogPost(results_surface_key="results", categories=(category,))


def test_mz_catalog_rejects_duplicate_publisher_category() -> None:
    with pytest.raises(ValueError, match="MZ categories"):
        MZCatalogPost(
            results_surface_key="results", categories=("videoconferência", "videoconferência")
        )


def _mz_request(*, years: tuple[int, ...] = (2025, 2026)) -> IRAuthorityCaptureRequest:
    categories = ("release_resultados",)
    results = IRAuthorityCaptureSpec(
        surface_key="results",
        surface_kind="primary_landing",
        source_url=MZ_RESULTS,
        traversal_kind="single_response",
        outcome="observed",
        required=False,
        observed_document_urls=(),
        verification_method="retained_results_page",
        revision=1,
    )
    years_spec = IRAuthorityCaptureSpec(
        surface_key="years",
        surface_kind="publisher_api",
        source_url=f"{MZ_BASE}/company/issuer-id/categoryInternalName/document/language/years",
        traversal_kind="single_response",
        outcome="observed",
        required=False,
        observed_document_urls=(),
        verification_method="retained_mz_years",
        revision=1,
        mz_catalog_post=MZCatalogPost(results_surface_key="results", categories=categories),
    )
    documents = tuple(
        IRAuthorityCaptureSpec(
            surface_key=f"documents-{year}",
            surface_kind="publisher_api",
            source_url=f"{MZ_BASE}/company/issuer-id/filter/categories/year/meta",
            traversal_kind="single_response",
            outcome="exhausted",
            required=True,
            terminal_condition="publisher_total_reconciled",
            observed_document_urls=(MZ_FILE_2025 if year == 2025 else MZ_FILE_2026,),
            verification_method="retained_mz_document_year",
            revision=1,
            mz_catalog_post=MZCatalogPost(
                results_surface_key="results", categories=categories, year=year
            ),
        )
        for year in years
    )
    return IRAuthorityCaptureRequest(
        issuer_id="issuer-acme",
        ticker="ACME",
        authority_basis="publisher_api",
        asserted_at=STAMP,
        user_agent="research-agent test@example.test",
        connect_timeout_seconds=3,
        read_timeout_seconds=7,
        max_surface_bytes=1_000_000,
        max_redirects=2,
        surfaces=(results, years_spec, *documents),
    )


def _mz_responses(*, years: tuple[int, ...] = (2025, 2026)) -> list[FakeResponse]:
    responses = [
        FakeResponse(body=MZ_PAGE),
        FakeResponse(
            body=json.dumps({"success": True, "data": list(years)}).encode(),
            headers={"Content-Type": "application/json"},
        ),
    ]
    for year in years:
        responses.append(
            FakeResponse(
                body=json.dumps(
                    {
                        "success": True,
                        "data": {
                            "document_metas": [
                                {
                                    "internal_name": "release_resultados",
                                    "file_title": f"Results {year}",
                                    "link_url": (MZ_FILE_2025 if year == 2025 else MZ_FILE_2026),
                                }
                            ]
                        },
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
        )
    return responses


def test_required_exhausted_surface_requires_at_least_one_observed_document() -> None:
    with pytest.raises(ValueError, match="observed document"):
        IRAuthorityCaptureSpec(
            surface_key="archive",
            surface_kind="archive",
            source_url=URL,
            traversal_kind="pagination",
            outcome="exhausted",
            required=True,
            terminal_condition="next_link_absent",
            observed_document_urls=(),
            verification_method="publisher_archive_html",
            revision=1,
        )


def test_claimed_document_must_be_present_in_captured_surface_bytes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(document_url="https://ir.acme.test/not-in-body.pdf"),
            blob_root=tmp_path / "blobs",
            apply=True,
            session=FakeSession([FakeResponse()]),
        )
        assert result.complete is False
        assert result.failed == 1
        assert result.items[0].reason_code == "claimed_document_not_in_surface"
        assert conn.execute(
            "SELECT COUNT(*) FROM issuer_authority_surface_revisions"
        ).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (0,)
    finally:
        conn.close()


def test_exhausted_surface_rejects_an_unclaimed_document_reference(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    q1_url = "https://ir.acme.test/q1-2026-results.pdf"
    body = (
        b"<html>"
        b"<a href='/q1-2026-results.pdf'>Q1 results</a>"
        b"<a href='/q2-2026-results.pdf'>Q2 results</a>"
        b"</html>"
    )
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(document_url=q1_url),
            blob_root=tmp_path / "blobs",
            apply=True,
            session=FakeSession([FakeResponse(body=body)]),
        )
        assert result.complete is False
        assert result.failed == 1
        assert result.items[0].reason_code == "unclaimed_document_in_surface"
        assert conn.execute(
            "SELECT COUNT(*) FROM issuer_authority_surface_revisions"
        ).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (0,)
    finally:
        conn.close()


def test_exhausted_html_ignores_navigation_assets_but_keeps_document_links(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    body = (
        b"<html><head><link rel='stylesheet' href='/assets/archive.css'></head>"
        b"<body><nav><a href='/press-releases'>Press Releases</a></nav>"
        b"<img src='https://s25.q4cdn.com/issuer/logo.png'>"
        b"<a href='/q4-2025-results.pdf'>Q4 results</a></body></html>"
    )
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=tmp_path / "blobs",
            apply=False,
            session=FakeSession([FakeResponse(body=body)]),
        )
        assert result.complete
        assert result.failed == 0
    finally:
        conn.close()


def test_exhausted_html_retains_a_pdf_link_inside_navigation(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    body = (
        b"<html><nav><a href='/archive'>Archive</a>"
        b"<a href='/q4-2025-results.pdf'>Q4 results PDF</a></nav></html>"
    )
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=tmp_path / "blobs",
            apply=False,
            session=FakeSession([FakeResponse(body=body)]),
        )
        assert result.complete
    finally:
        conn.close()


def test_exhausted_json_retains_bare_document_urls_array(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    body = json.dumps({"title": "Quarterly archive", "urls": [DOCUMENT_URL]}).encode()
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=tmp_path / "blobs",
            apply=False,
            session=FakeSession(
                [FakeResponse(body=body, headers={"Content-Type": "application/json"})]
            ),
        )
        assert result.complete
    finally:
        conn.close()


def test_exhausted_html_keeps_opaque_cdn_file_without_claiming_cdn_authority(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn(tmp_path, migrated_db)
    opaque = "https://s25.q4cdn.com/issuer/files/quarterly-download.dat"
    body = (
        b"<html><img src='https://s25.q4cdn.com/issuer/logo.png'>"
        b"<a href='/q4-2025-results.pdf'>Q4 results</a>"
        b"<a href='https://s25.q4cdn.com/issuer/files/quarterly-download.dat'>"
        b"Quarterly document</a></html>"
    )
    request = _request()
    archive = request.surfaces[0].model_copy(
        update={"observed_document_urls": (DOCUMENT_URL, opaque)}
    )
    request = request.model_copy(update={"surfaces": (archive,)})

    def literal_public_url(url: str) -> str:
        return url

    monkeypatch.setattr(generic, "ensure_safe_public_url", literal_public_url)
    try:
        result = capture_ir_authority_surfaces(
            conn,
            request,
            blob_root=tmp_path / "blobs",
            apply=False,
            session=FakeSession([FakeResponse(body=body)]),
        )
        assert result.complete
        assert result.authority_evidence is not None
        assert result.authority_evidence.surfaces[0].observed_document_urls == (
            DOCUMENT_URL,
            opaque,
        )
    finally:
        conn.close()


def test_exhausted_json_ignores_titles_but_requires_each_document_url(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    body = json.dumps(
        {
            "title": "Quarterly Results Archive",
            "document_metas": [
                {
                    "file_title": "Q4 2025 results",
                    "link_url": DOCUMENT_URL,
                }
            ],
        }
    ).encode()
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=tmp_path / "blobs",
            apply=False,
            session=FakeSession(
                [FakeResponse(body=body, headers={"Content-Type": "application/json"})]
            ),
        )
        assert result.complete
        assert result.failed == 0
    finally:
        conn.close()


def test_exhausted_surface_keeps_opaque_download_paths_in_closed_set(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    requested = "https://ir.acme.test/files/download/opaque-one"
    body = (
        b"<a href='/files/download/opaque-one'>Quarterly document</a>"
        b"<a href='/files/download/opaque-two'>Supplemental document</a>"
    )
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(document_url=requested),
            blob_root=tmp_path / "blobs",
            apply=False,
            session=FakeSession([FakeResponse(body=body)]),
        )
        assert result.failed == 1
        assert result.items[0].reason_code == "unclaimed_document_in_surface"
    finally:
        conn.close()


def test_mz_catalog_retains_exact_post_and_response_bytes_with_replay(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    request = _mz_request()
    blob_root = tmp_path / "blobs"
    try:
        first_session = FakeSession(_mz_responses())
        first = capture_ir_authority_surfaces(
            conn, request, blob_root=blob_root, apply=True, session=first_session
        )
        assert first.complete
        assert first.failed == 0
        assert len(first_session.calls) == 4
        config_sha = conn.execute(
            "SELECT retrieval_config_sha256 FROM evidence_source_observations WHERE source_url = ?",
            (MZ_RESULTS,),
        ).fetchone()[0]
        config_uri = conn.execute(
            "SELECT storage_uri FROM evidence_content_blobs WHERE sha256 = ?",
            (config_sha,),
        ).fetchone()[0]
        config = json.loads(Path(config_uri.removeprefix("file://")).read_bytes())
        assert config["collector"] == "ir-authority-surface-capture@2-mz-catalog"
        assert set(config["post_requests"]) == {
            "years",
            "documents-2025",
            "documents-2026",
        }
        retained_requests: dict[str, object] = {}
        for key, post_request in config["post_requests"].items():
            assert post_request["method"] == "POST"
            digest = post_request["body_sha256"]
            storage_uri = conn.execute(
                "SELECT storage_uri FROM evidence_content_blobs WHERE sha256 = ?",
                (digest,),
            ).fetchone()[0]
            raw_request = Path(storage_uri.removeprefix("file://")).read_bytes()
            assert hashlib.sha256(raw_request).hexdigest() == digest
            retained_requests[key] = json.loads(raw_request)
        assert retained_requests["years"] == {
            "categoryInternalNames": ["release_resultados"],
            "language_code": "en_US",
        }
        assert retained_requests["documents-2026"] == {
            "year": "2026",
            "categories": ["release_resultados"],
            "language": "en_US",
            "published": True,
        }
        assert [call[1]["body_sha256"] for call in first_session.calls[1:]] == [
            config["post_requests"][key]["body_sha256"]
            for key in ("years", "documents-2025", "documents-2026")
        ]
        for response in _mz_responses():
            assert conn.execute(
                "SELECT 1 FROM evidence_content_blobs WHERE sha256 = ?",
                (hashlib.sha256(response.body).hexdigest(),),
            ).fetchone() == (1,)
        second = capture_ir_authority_surfaces(
            conn, request, blob_root=blob_root, apply=True, session=FakeSession(_mz_responses())
        )
        assert second.complete
        assert second.records_created == 0
        assert second.records_replayed == first.records_created
    finally:
        conn.close()


def test_retained_mz_population_closes_source_inventory_despite_failed_generic_page(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    blob_root = tmp_path / "blobs"
    try:
        captured = capture_ir_authority_surfaces(
            conn,
            _mz_request(),
            blob_root=blob_root,
            apply=True,
            session=FakeSession(_mz_responses()),
        )
        assert captured.authority_evidence is not None
        conn.row_factory = sqlite3.Row
        discovery = DocumentDiscoveryInventory(
            candidates=(
                CandidateDoc(
                    url=MZ_FILE_2026,
                    link_text="2026 results",
                    filename_hint="2026.pdf",
                    doc_type_guess="press_release",
                    year_guess=2026,
                    quarter_guess=None,
                    source_page=MZ_RESULTS,
                ),
            ),
            pages=(
                CrawlPageOutcome(
                    page_url=MZ_RESULTS,
                    outcome="failed",
                    anchor_count=0,
                    failure_reason="generic_page_unavailable",
                ),
            ),
            crawl_complete=False,
            crawl_stop_reason="page_failure",
        )
        request = source_inventory_request(
            issuer_id="issuer-acme",
            ticker="ACME",
            ir_url=MZ_RESULTS,
            revision=1,
            inventory=discovery,
            authority=captured.authority_evidence,
            retrieval_config_sha256="c" * 64,
            collector_code_version="sync-ir-source-inventory@2",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
            reconciled_at=STAMP,
            apply=False,
        )
        result = sync_ir_source_inventory(conn, request, blob_root=blob_root)
        assert result.complete
        assert result.candidate_count == 2
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 0
        with pytest.raises(IRAuthorityCaptureError):
            sync_ir_source_inventory(
                conn,
                request.model_copy(update={"issuer_id": "issuer-other"}),
                blob_root=blob_root,
            )
        earlier = STAMP - timedelta(minutes=1)
        with pytest.raises(ValueError, match="captured after inventory completion"):
            sync_ir_source_inventory(
                conn,
                request.model_copy(
                    update={
                        "started_at": earlier,
                        "completed_at": earlier,
                        "recorded_at": earlier,
                        "reconciled_at": earlier,
                    }
                ),
                blob_root=blob_root,
            )
    finally:
        conn.close()


def test_mz_source_inventory_rejects_changed_retained_response_before_writes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    blob_root = tmp_path / "blobs"
    try:
        captured = capture_ir_authority_surfaces(
            conn,
            _mz_request(),
            blob_root=blob_root,
            apply=True,
            session=FakeSession(_mz_responses()),
        )
        assert captured.authority_evidence is not None
        conn.row_factory = sqlite3.Row
        digest = captured.authority_evidence.surfaces[-1].raw_sha256
        (blob_root / digest[:2] / digest).write_bytes(b"changed")
        request = source_inventory_request(
            issuer_id="issuer-acme",
            ticker="ACME",
            ir_url=MZ_RESULTS,
            revision=1,
            inventory=DocumentDiscoveryInventory(
                candidates=(),
                pages=(),
                crawl_complete=False,
                crawl_stop_reason="page_failure",
            ),
            authority=captured.authority_evidence,
            retrieval_config_sha256="c" * 64,
            collector_code_version="sync-ir-source-inventory@2",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
            reconciled_at=STAMP,
            apply=True,
        )
        before = conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()[0]
        with pytest.raises(IRAuthorityCaptureError):
            sync_ir_source_inventory(conn, request, blob_root=blob_root)
        assert (
            conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()[0]
            == before
        )
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 0
    finally:
        conn.close()


def test_historical_get_capture_identity_remains_byte_identical(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=tmp_path / "blobs",
            apply=False,
            session=FakeSession([FakeResponse()]),
        )
        assert result.authority_evidence is not None
        expected_ir_sha256 = "bc7587b66da3efb6bb12ea4e3a3c0ec45eb850cc3f3133ff9c3105c302e9a2ff"
        assert result.authority_evidence.surfaces[0].source_observation_id == (
            "ir-authority-observation:" + expected_ir_sha256
        )
    finally:
        conn.close()


def test_historical_authority_projection_accepts_tuple_and_row_results(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    blob_root = tmp_path / "blobs"
    try:
        captured = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=blob_root,
            apply=True,
            session=FakeSession([FakeResponse()]),
        )
        assert captured.authority_evidence is not None
        for factory in (None, sqlite3.Row):
            conn.row_factory = factory
            assert (
                verified_mz_authority_documents(
                    conn,
                    captured.authority_evidence,
                    blob_root=blob_root,
                    issuer_id="issuer-acme",
                    ticker="ACME",
                )
                is None
            )
    finally:
        conn.close()


def _collector_authority(
    conn: sqlite3.Connection,
    tmp_path: Path,
    collectors: tuple[str | None, ...],
) -> IRAuthorityEvidence:
    ledger = EvidenceLedger(conn)
    surfaces: list[PublisherSurfaceEvidence] = []
    for ordinal, collector in enumerate(collectors):
        observation_id = f"collector-probe-{ordinal}"
        source_url = f"{URL}/{ordinal}"
        raw = f"surface-{ordinal}".encode()
        digest = hashlib.sha256(raw).hexdigest()
        if collector is not None:
            path = tmp_path / observation_id
            path.write_bytes(raw)
            ledger.persist(
                ContentBlob(
                    sha256=digest,
                    byte_size=len(raw),
                    media_type="text/html",
                    storage_uri=path.as_uri(),
                    recorded_at=STAMP,
                )
            )
            ledger.persist(
                SourceObservation(
                    observation_id=observation_id,
                    idempotency_key=observation_id,
                    source_kind="ir_publisher_authority",
                    source_url=source_url,
                    blob_sha256=digest,
                    source_published_at=None,
                    filing_at=None,
                    accepted_at=None,
                    observed_at=STAMP,
                    retrieved_at=STAMP,
                    retrieval_config_sha256="a" * 64,
                    collector_code_version=collector,
                )
            )
        surfaces.append(
            PublisherSurfaceEvidence(
                surface_key=f"archive-{ordinal}",
                surface_kind="archive",
                source_url=source_url,
                source_observation_id=observation_id,
                raw_sha256=digest,
                traversal_kind="pagination",
                outcome="exhausted",
                terminal_condition="next_link_absent",
                observed_document_urls=(DOCUMENT_URL,),
            )
        )
    return IRAuthorityEvidence(
        authority_basis="publisher_archive", asserted_at=STAMP, surfaces=tuple(surfaces)
    )


def test_generic_authority_collectors_keep_existing_inventory_route(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    try:
        authority = _collector_authority(
            conn, tmp_path, ("synthetic@1", "ir-authority-surface-capture@1")
        )
        assert (
            verified_mz_authority_documents(
                conn, authority, blob_root=tmp_path, issuer_id="issuer-acme", ticker="ACME"
            )
            is None
        )
    finally:
        conn.close()


def test_mz_authority_rejects_mixed_collector_rows(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    try:
        authority = _collector_authority(
            conn, tmp_path, ("ir-authority-surface-capture@2-mz-catalog", "synthetic@1")
        )
        with pytest.raises(IRAuthorityCaptureError, match="mixes collector versions"):
            verified_mz_authority_documents(
                conn, authority, blob_root=tmp_path, issuer_id="issuer-acme", ticker="ACME"
            )
    finally:
        conn.close()


def test_authority_adapter_rejects_missing_surface_observation(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    try:
        authority = _collector_authority(conn, tmp_path, ("synthetic@1", None))
        with pytest.raises(IRAuthorityCaptureError, match="observation is absent"):
            verified_mz_authority_documents(
                conn, authority, blob_root=tmp_path, issuer_id="issuer-acme", ticker="ACME"
            )
    finally:
        conn.close()


def test_mz_catalog_rejects_missing_declared_year_before_document_posts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    session = FakeSession(_mz_responses(years=(2026,)))
    try:
        result = capture_ir_authority_surfaces(
            conn, _mz_request(), blob_root=tmp_path / "blobs", apply=False, session=session
        )
        assert not result.complete
        assert [item.reason_code for item in result.items] == [
            "surface_fetched",
            "mz_year_set_mismatch",
            "mz_years_unverified",
            "mz_years_unverified",
        ]
        assert len(session.calls) == 2
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("years_response", "reason"),
    [
        (FakeResponse(status_code=403), "http_status"),
        (
            FakeResponse(body=b'{"success":true,"data":"bad"}'),
            "mz_years_invalid",
        ),
        (FakeResponse(status_code=302, headers={"Location": MZ_BASE}), "mz_post_redirect"),
        (
            FakeResponse(
                body=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(2 * 1024 * 1024 + 1),
                },
            ),
            "surface_too_large",
        ),
    ],
)
def test_mz_catalog_years_hardstop_prevents_document_posts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    years_response: FakeResponse,
    reason: str,
) -> None:
    conn = _conn(tmp_path, migrated_db)
    session = FakeSession([FakeResponse(body=MZ_PAGE), years_response])
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _mz_request(),
            blob_root=tmp_path / "blobs",
            apply=False,
            session=session,
        )
        assert not result.complete
        assert result.items[1].reason_code == reason
        assert len(session.calls) == 2
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (0,)
        assert not (tmp_path / "blobs").exists()
    finally:
        conn.close()


def test_mz_catalog_rejects_document_set_mismatch(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    responses = _mz_responses()
    responses[2] = FakeResponse(
        body=json.dumps({"success": True, "data": {"document_metas": []}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _mz_request(),
            blob_root=tmp_path / "blobs",
            apply=False,
            session=FakeSession(responses),
        )
        assert not result.complete
        assert result.items[2].reason_code == "document_set_mismatch"
    finally:
        conn.close()


def test_mz_catalog_rejects_endpoint_not_in_retained_results_page(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    request = _mz_request()
    years = request.surfaces[1].model_copy(
        update={"source_url": "https://api.mziq.com/mzfilemanager/company/other/years"}
    )
    changed = request.model_copy(
        update={"surfaces": (request.surfaces[0], years, *request.surfaces[2:])}
    )
    session = FakeSession([FakeResponse(body=MZ_PAGE)])
    try:
        result = capture_ir_authority_surfaces(
            conn, changed, blob_root=tmp_path / "blobs", apply=False, session=session
        )
        assert not result.complete
        assert result.items[1].reason_code == "mz_catalog_config_mismatch"
        assert len(session.calls) == 1
    finally:
        conn.close()


def test_dry_run_fetches_without_database_or_durable_blob_writes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    blob_root = tmp_path / "blobs"
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=blob_root,
            apply=False,
            session=FakeSession([FakeResponse()]),
        )
        assert result.mode == "dry_run"
        assert result.authority_evidence is not None
        assert result.complete
        assert result.records_created == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM issuer_authority_surface_revisions"
        ).fetchone() == (0,)
        assert not blob_root.exists()
    finally:
        conn.close()


def test_apply_persists_hash_bound_evidence_and_verified_surface(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    blob_root = tmp_path / "blobs"
    digest = hashlib.sha256(BODY).hexdigest()
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=blob_root,
            apply=True,
            session=FakeSession([FakeResponse()]),
        )
        authority = result.authority_evidence
        assert authority is not None
        assert result.complete
        assert authority.surfaces[0].raw_sha256 == digest
        observation_id = authority.surfaces[0].source_observation_id
        assert conn.execute(
            "SELECT blob_sha256, source_url FROM evidence_source_observations "
            "WHERE observation_id = ?",
            (observation_id,),
        ).fetchone() == (digest, URL)
        assert conn.execute(
            "SELECT status, source_observation_id FROM issuer_authority_surface_revisions"
        ).fetchone() == ("verified", observation_id)
        assert conn.execute(
            "SELECT availability_state, verified_sha256 FROM evidence_blob_location_observations"
        ).fetchone() == ("present", digest)
        assert (blob_root / digest[:2] / digest).read_bytes() == BODY
    finally:
        conn.close()


def test_exact_apply_replay_is_idempotent(tmp_path: Path, migrated_db: Callable[..., Path]) -> None:
    conn = _conn(tmp_path, migrated_db)
    blob_root = tmp_path / "blobs"
    try:
        first = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=blob_root,
            apply=True,
            session=FakeSession([FakeResponse()]),
        )
        second = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=blob_root,
            apply=True,
            session=FakeSession([FakeResponse()]),
        )
        assert first.authority_evidence == second.authority_evidence
        assert second.records_created == 0
        assert second.records_replayed == 4
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM issuer_authority_surface_revisions"
        ).fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.parametrize("outcome", ["observed", "exhausted"])
def test_fresh_identical_capture_retains_location_and_new_source_clock(
    tmp_path: Path, migrated_db: Callable[..., Path], outcome: SurfaceOutcome
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    blob_root = tmp_path / "blobs"
    request = _request(outcome=outcome)
    try:
        first = capture_ir_authority_surfaces(
            conn, request, blob_root=blob_root, apply=True, session=FakeSession([FakeResponse()])
        )
        first_blob = conn.execute("SELECT * FROM evidence_content_blobs").fetchall()
        first_location = conn.execute(
            "SELECT * FROM evidence_blob_location_observations"
        ).fetchall()
        later = STAMP + timedelta(seconds=1)
        updates: dict[str, object] = {"asserted_at": later}
        if outcome == "exhausted":
            previous = conn.execute(
                "SELECT surface_revision_id FROM issuer_authority_surface_revisions"
            ).fetchone()
            assert previous is not None
            updates["surfaces"] = (
                request.surfaces[0].model_copy(
                    update={"revision": 2, "supersedes_surface_revision_id": previous[0]}
                ),
            )
        renewed = request.model_copy(update=updates)
        second = capture_ir_authority_surfaces(
            conn, renewed, blob_root=blob_root, apply=True, session=FakeSession([FakeResponse()])
        )
        assert conn.execute("SELECT * FROM evidence_content_blobs").fetchall() == first_blob
        assert (
            conn.execute("SELECT * FROM evidence_blob_location_observations").fetchall()
            == first_location
        )
        assert first.items[0].source_observation_id != second.items[0].source_observation_id
        rows = conn.execute(
            "SELECT blob_sha256,observed_at,retrieved_at,retrieval_config_sha256 "
            "FROM evidence_source_observations ORDER BY observed_at"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == rows[1][0] == hashlib.sha256(BODY).hexdigest()
        assert datetime.fromisoformat(rows[1][1]) == later
        assert datetime.fromisoformat(rows[1][2]) == later
        assert rows[0][3] != rows[1][3]
        assert second.records_replayed == 2
        assert second.records_created == (2 if outcome == "exhausted" else 1)
        replay = capture_ir_authority_surfaces(
            conn, renewed, blob_root=blob_root, apply=True, session=FakeSession([FakeResponse()])
        )
        assert replay.records_created == 0
    finally:
        conn.close()


@pytest.mark.parametrize("failure", ["missing", "corrupt", "outside_root", "reparse"])
def test_fresh_capture_rejects_unusable_retained_blob_without_repair(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    failure: str,
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    blob_root = tmp_path / "blobs"
    request = _request(outcome="observed")
    try:
        capture_ir_authority_surfaces(
            conn, request, blob_root=blob_root, apply=True, session=FakeSession([FakeResponse()])
        )
        uri = conn.execute("SELECT storage_uri FROM evidence_content_blobs").fetchone()[0]
        path = resolve_local_storage_uri(uri, allowed_roots=(blob_root,))
        assert path is not None and path.read_bytes() == BODY
        directory = path.parent
        if failure == "missing":
            path.unlink()
        elif failure == "corrupt":
            path.write_bytes(b"wrong retained bytes")
        elif failure == "outside_root":
            blob_root = tmp_path / "different-approved-root"
        else:
            retained = directory.with_name("retained-original")
            directory.rename(retained)
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(directory), str(retained)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            else:
                directory.symlink_to(retained, target_is_directory=True)
        before_blobs = conn.execute("SELECT * FROM evidence_content_blobs").fetchall()
        before_locations = conn.execute(
            "SELECT * FROM evidence_blob_location_observations"
        ).fetchall()
        before_observations = conn.execute("SELECT * FROM evidence_source_observations").fetchall()
        with pytest.raises(IRAuthorityCaptureError):
            capture_ir_authority_surfaces(
                conn,
                request.model_copy(update={"asserted_at": STAMP + timedelta(seconds=1)}),
                blob_root=blob_root,
                apply=True,
                session=FakeSession([FakeResponse()]),
            )
        assert conn.execute("SELECT * FROM evidence_content_blobs").fetchall() == before_blobs
        assert (
            conn.execute("SELECT * FROM evidence_blob_location_observations").fetchall()
            == before_locations
        )
        assert (
            conn.execute("SELECT * FROM evidence_source_observations").fetchall()
            == before_observations
        )
        assert not conn.in_transaction
        if failure == "missing":
            assert not path.exists()
        elif failure == "corrupt":
            assert path.read_bytes() == b"wrong retained bytes"
        elif failure == "outside_root":
            assert not blob_root.exists()
        else:
            assert path.read_bytes() == BODY
            if os.name == "nt":
                directory.rmdir()
            else:
                directory.unlink()
    finally:
        conn.close()


def test_failed_required_surface_is_not_verified_or_complete(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(outcome="failed"),
            blob_root=tmp_path / "blobs",
            apply=True,
            session=FakeSession([FakeResponse()]),
        )
        assert result.authority_evidence is not None
        assert not result.complete
        assert conn.execute(
            "SELECT COUNT(*) FROM issuer_authority_surface_revisions"
        ).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("response", "reason_code"),
    [
        (FakeResponse(status_code=503), "http_status"),
        (
            FakeResponse(
                body=b"0123456789",
                headers={"Content-Type": "text/html"},
            ),
            "surface_too_large",
        ),
    ],
)
def test_failed_or_oversized_fetch_emits_no_unbound_authority(
    tmp_path: Path,
    response: FakeResponse,
    reason_code: str,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    maximum = 8 if reason_code == "surface_too_large" else 1_000_000
    try:
        result = capture_ir_authority_surfaces(
            conn,
            _request(maximum=maximum),
            blob_root=tmp_path / "blobs",
            apply=True,
            session=FakeSession([response]),
        )
        assert result.authority_evidence is None
        assert not result.complete
        assert result.items[0].reason_code == reason_code
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (0,)
        assert not (tmp_path / "blobs").exists()
    finally:
        conn.close()


def test_redirects_are_bounded_and_credential_redirect_is_rejected(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)

    def redirect(location: str) -> FakeResponse:
        return FakeResponse(
            status_code=302,
            body=b"",
            headers={"Location": location},
        )

    try:
        bounded = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=tmp_path / "blobs",
            apply=False,
            session=FakeSession(
                [
                    redirect("/archive?page=2"),
                    redirect("/archive?page=3"),
                    redirect("/archive?page=4"),
                ]
            ),
        )
        assert bounded.items[0].reason_code == "redirect_limit"
        credentialed = capture_ir_authority_surfaces(
            conn,
            _request(),
            blob_root=tmp_path / "blobs",
            apply=False,
            session=FakeSession([redirect(_url_with_userinfo())]),
        )
        assert credentialed.items[0].reason_code == "credentialed_url"
        assert credentialed.authority_evidence is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    "source_url",
    [
        _url_with_userinfo(),
        "https://ir.acme.test/archive?api_key=secret",
        "http://ir.acme.test/archive",
    ],
)
def test_request_rejects_credentials_and_non_https(source_url: str) -> None:
    with pytest.raises(ValueError):
        _request(source_url=source_url)


def test_canonical_ticker_mismatch_stops_before_network(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    session = FakeSession([])
    try:
        with pytest.raises(IRAuthorityCaptureIdentityError):
            capture_ir_authority_surfaces(
                conn,
                _request().model_copy(update={"issuer_id": "issuer-other"}),
                blob_root=tmp_path / "blobs",
                apply=False,
                session=session,
            )
        assert session.calls == []
    finally:
        conn.close()


def test_cli_uses_job_lock_and_json_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_db: Callable[..., Path],
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    request_path = tmp_path / "request.json"
    request_path.write_text(_request().model_dump_json(), encoding="utf-8")
    entered: list[tuple[str, tuple[str, ...]]] = []

    class FakeLock:
        def __init__(
            self,
            _root: Path,
            job_name: str,
            write_sets: list[str],
        ) -> None:
            entered.append((job_name, tuple(write_sets)))

        def __enter__(self) -> FakeLock:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(cli, "JobLock", FakeLock)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db_path))
    monkeypatch.setattr(cli.requests, "Session", lambda: FakeSession([FakeResponse()]))
    exit_code = cli.main(
        [
            "--db",
            str(db_path),
            "--request",
            str(request_path),
            "--blob-root",
            str(tmp_path / "blobs"),
            "--apply",
        ]
    )
    output = capsys.readouterr()
    assert exit_code == 0, output
    assert entered[0][0] == "ir-authority-surface-capture"
    assert entered[0][1] == ("portfolio-db", f"evidence-blobs:{(tmp_path / 'blobs').resolve()}")
    payload = json.loads(output.out)
    assert payload["authority_evidence"]["surfaces"][0]["raw_sha256"]
    assert "ir_authority_capture_completed" in output.err


def test_cli_canonical_writer_lock_blocks_before_connection_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_db: Callable[..., Path],
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    request_path = tmp_path / "request.json"
    request_path.write_text(_request().model_dump_json(), encoding="utf-8")
    session = FakeSession([FakeResponse()])
    connections: list[Path] = []
    connect = cli.connect_sqlite

    def observed_connect(
        path: Path, *, role: SQLiteConnectionRole, schema_preflight: bool | None = None
    ) -> sqlite3.Connection:
        connections.append(path)
        return connect(path, role=role, schema_preflight=schema_preflight)

    monkeypatch.setattr(cli, "connect_sqlite", observed_connect)
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db_path))
    monkeypatch.setenv("ES_JOB_LOCK_WAIT_S", "0")
    with hold_run_lock(db_path, owner="synthetic-canonical-writer", timeout_s=0):
        before = lock_path_for(db_path).read_bytes()
        result = cli.main(
            [
                "--db",
                str(db_path),
                "--request",
                str(request_path),
                "--blob-root",
                str(tmp_path / "blobs"),
                "--apply",
            ]
        )
        assert result == 1
        assert lock_path_for(db_path).read_bytes() == before
    assert connections == []
    assert session.calls == []
    assert "JobAlreadyRunningError" in capsys.readouterr().err
    assert not lock_path_for(db_path).exists()


def test_cli_rejects_apply_database_outside_configured_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_db: Callable[..., Path],
) -> None:
    conn = _full_conn(tmp_path, migrated_db)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    request_path = tmp_path / "request.json"
    request_path.write_text(_request().model_dump_json(), encoding="utf-8")
    session = FakeSession([FakeResponse()])
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "other-authority.db"))
    result = cli.main(
        [
            "--db",
            str(db_path),
            "--request",
            str(request_path),
            "--blob-root",
            str(tmp_path / "blobs"),
            "--apply",
        ]
    )
    assert result == 1
    assert session.calls == []
    assert "ValueError" in capsys.readouterr().err


@pytest.mark.parametrize("configured", [None, "   "])
def test_cli_rejects_unconfigured_apply_before_checkout_fallback(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configured: str | None,
) -> None:
    root = tmp_path / "synthetic-checkout"
    (root / "data").mkdir(parents=True)
    db = migrated_db(root / "data" / "portfolio.db")
    conn = sqlite3.connect(db)
    _seed_registry(conn)
    conn.close()
    request_path = root / "request.json"
    request_path.write_text(_request().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(cli, "PROJECT_ROOT", root)
    if configured is None:
        monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    else:
        monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", configured)
    session = FakeSession([FakeResponse()])
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    result = cli.main(
        [
            "--db",
            str(db),
            "--request",
            str(request_path),
            "--blob-root",
            str(root / "blobs"),
            "--apply",
        ]
    )
    assert result == 1
    assert session.calls == []
    assert "ValueError" in capsys.readouterr().err
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (0,)
    finally:
        conn.close()
