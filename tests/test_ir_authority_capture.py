"""Raw publisher authority surfaces are bounded, immutable, and hash-bound."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

import db as dbmod
from alembic import command
from execution import capture_ir_authority_surfaces as cli
from ir_pipeline.authority import SurfaceOutcome
from ir_pipeline.authority_capture import (
    IRAuthorityCaptureIdentityError,
    IRAuthorityCaptureRequest,
    IRAuthorityCaptureSpec,
    capture_ir_authority_surfaces,
)
from provenance.issuer_registry import (
    IssuerEntity,
    IssuerRegistry,
    LegacyIssuerBindingRevision,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 21, 0, tzinfo=UTC)
URL = "https://ir.acme.test/archive"
DOCUMENT_URL = "https://ir.acme.test/q4-2025-results.pdf"
BODY = b"<html><a href='/q4-2025-results.pdf'>Q4 results</a></html>"


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
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
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

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "authority-capture.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0227_issuer_reporting_registry")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    _seed_registry(conn)
    return conn


def _full_conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "authority-capture-full.db"
    saved_paths = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    try:
        dbmod.set_db_path(str(path))
        dbmod.init_db()
        command.stamp(_config(path), "0000_baseline")
        command.upgrade(_config(path), "head")
    finally:
        dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved_paths
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
) -> None:
    conn = _conn(tmp_path)
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


def test_dry_run_fetches_without_database_or_durable_blob_writes(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
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
) -> None:
    conn = _conn(tmp_path)
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


def test_exact_apply_replay_is_idempotent(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
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


def test_failed_required_surface_is_not_verified_or_complete(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
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
) -> None:
    conn = _conn(tmp_path)
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
) -> None:
    conn = _conn(tmp_path)

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
            session=FakeSession([redirect("https://user:secret@ir.acme.test/archive")]),
        )
        assert credentialed.items[0].reason_code == "credentialed_url"
        assert credentialed.authority_evidence is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    "source_url",
    [
        "https://user:password@ir.acme.test/archive",
        "https://ir.acme.test/archive?api_key=secret",
        "http://ir.acme.test/archive",
    ],
)
def test_request_rejects_credentials_and_non_https(source_url: str) -> None:
    with pytest.raises(ValueError):
        _request(source_url=source_url)


def test_canonical_ticker_mismatch_stops_before_network(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
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
) -> None:
    conn = _full_conn(tmp_path)
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
    assert exit_code == 0
    assert entered[0][0] == "ir-authority-surface-capture"
    payload = json.loads(output.out)
    assert payload["authority_evidence"]["surfaces"][0]["raw_sha256"]
    assert "ir_authority_capture_completed" in output.err
