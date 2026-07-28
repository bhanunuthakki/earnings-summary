"""Official IR homepage verification is distinct from inventory completeness."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from ir_pipeline.home_authority import (
    IRHomeAuthorityError,
    IRHomeAuthorityRequest,
    verify_ir_home_authority,
)
from ir_pipeline.home_authority_batch import (
    IRHomeBatchRequest,
    verify_ir_home_candidates,
)
from ir_pipeline.home_authority_registry import IRHomeAuthorityCandidate
from provenance.issuer_registry import (
    IssuerEntity,
    IssuerRegistry,
    LegacyIssuerBindingRevision,
)

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0231_legacy_document_evidence_bindings"
STAMP = datetime(2026, 7, 28, 2, 0, tzinfo=UTC)
URL = "https://investor.acme.test/"
BODY = b"<html><title>Acme Investor Relations</title><body>Quarterly Results</body></html>"


class _Response:
    def __init__(
        self,
        status_code: int,
        *,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        assert chunk_size > 0
        yield self._body

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.calls: list[str] = []
        self.closed = False

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
        allow_redirects: bool,
    ) -> _Response:
        assert headers["User-Agent"] == "Acme research contact@example.test"
        assert timeout == (5, 10)
        assert stream
        assert not allow_redirects
        self.calls.append(url)
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "ir-home-authority.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
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
            binding_revision_id="legacy-acme-1",
            idempotency_key="legacy-acme-1",
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
    return conn


def _request(
    tmp_path: Path,
    *,
    body: bytes = BODY,
    apply: bool = True,
    recorded_at: datetime = STAMP,
) -> IRHomeAuthorityRequest:
    return IRHomeAuthorityRequest(
        issuer_id="issuer-acme",
        ticker="ACME",
        requested_url=URL,
        final_url=URL,
        raw_body=body,
        media_type="text/html",
        required_marker_groups=(
            ("Acme",),
            ("Investor Relations", "Quarterly Results"),
        ),
        verification_method="analyst_reviewed_publisher_identity_markers",
        blob_root=tmp_path / "blobs",
        apply=apply,
        recorded_at=recorded_at,
    )


def test_verified_home_is_hash_bound_but_does_not_claim_inventory_completeness(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    first = verify_ir_home_authority(conn, request=_request(tmp_path))
    exact = verify_ir_home_authority(conn, request=_request(tmp_path))
    refreshed = verify_ir_home_authority(
        conn,
        request=_request(
            tmp_path,
            body=BODY.replace(b"</body>", b"<p>Updated</p></body>"),
            recorded_at=STAMP + timedelta(hours=1),
        ),
    )

    assert first.records_created == 4
    assert exact.records_created == 0
    assert refreshed.records_created == 3
    assert conn.execute(
        "SELECT surface_kind, status, source_url FROM v_issuer_authority_surfaces_current"
    ).fetchone() == ("ir_home", "verified", URL)
    assert conn.execute("SELECT COUNT(*) FROM issuer_authority_surface_revisions").fetchone() == (
        1,
    )
    assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (2,)
    assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshot_seals").fetchone() == (0,)
    conn.close()


def test_marker_or_identity_failure_emits_no_evidence_or_surface(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    with pytest.raises(IRHomeAuthorityError, match="marker"):
        verify_ir_home_authority(
            conn,
            request=_request(tmp_path, body=b"<html>unrelated publisher</html>"),
        )
    with pytest.raises(IRHomeAuthorityError, match="canonical"):
        verify_ir_home_authority(
            conn,
            request=_request(tmp_path).model_copy(update={"issuer_id": "issuer-other"}),
        )
    assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM issuer_authority_surface_revisions").fetchone() == (
        0,
    )
    assert not (tmp_path / "blobs").exists()
    conn.close()


def test_dry_run_validates_without_writes(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    result = verify_ir_home_authority(
        conn,
        request=_request(tmp_path, apply=False),
    )
    assert result.mode == "dry_run"
    assert result.records_created == 0
    assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (0,)
    assert not (tmp_path / "blobs").exists()
    conn.close()


def test_batch_verifies_bounded_redirect_and_deduplicates_canonical_issuer(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    IssuerRegistry(conn).persist(
        LegacyIssuerBindingRevision(
            binding_revision_id="legacy-acme-alias-1",
            idempotency_key="legacy-acme-alias-1",
            recorded_issuer_id="legacy-ticker:ACMA",
            revision=1,
            issuer_id="issuer-acme",
            outcome="selected",
            decision_kind="deterministic",
            reason_code="test_fixture",
            reason_details=(("ticker", "ACMA"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    conn.commit()
    redirect = _Response(
        302,
        headers={"Location": "https://investor.acme.test/home"},
    )
    homepage = _Response(
        200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        body=BODY,
    )
    session = _Session([redirect, homepage])
    robots_calls: list[str] = []

    def _robots(url: str) -> tuple[Callable[[str], bool], float]:
        robots_calls.append(url)
        return lambda candidate_url: candidate_url == url, 0.0

    candidates = (
        IRHomeAuthorityCandidate(
            ticker="ACME",
            requested_url=URL,
            required_marker_groups=(("Acme",), ("Investor Relations",)),
        ),
        IRHomeAuthorityCandidate(
            ticker="ACMA",
            requested_url="https://investor.acme.test/alias",
            required_marker_groups=(("Acme",), ("Investor Relations",)),
        ),
    )
    result = verify_ir_home_candidates(
        conn,
        request=IRHomeBatchRequest(
            candidates=candidates,
            blob_root=tmp_path / "blobs",
            apply=True,
            recorded_at=STAMP,
            user_agent="Acme research contact@example.test",
            connect_timeout_seconds=5,
            read_timeout_seconds=10,
            max_body_bytes=1_000,
            max_redirects=2,
        ),
        session=session,
        robots_resolver=_robots,
        sleeper=lambda _seconds: None,
    )

    assert [item.outcome for item in result.items] == [
        "verified",
        "skipped_duplicate_issuer",
    ]
    assert result.items[0].records_created == 4
    assert session.calls == [URL, "https://investor.acme.test/home"]
    assert robots_calls == session.calls
    assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (1,)
    conn.close()


def test_batch_robots_denial_is_explicit_and_writes_nothing(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    session = _Session([])
    result = verify_ir_home_candidates(
        conn,
        request=IRHomeBatchRequest(
            candidates=(
                IRHomeAuthorityCandidate(
                    ticker="ACME",
                    requested_url=URL,
                    required_marker_groups=(("Acme",), ("Investor Relations",)),
                ),
            ),
            blob_root=tmp_path / "blobs",
            apply=True,
            recorded_at=STAMP,
            user_agent="Acme research contact@example.test",
        ),
        session=session,
        robots_resolver=lambda _url: (lambda _candidate_url: False, 0.0),
        sleeper=lambda _seconds: None,
    )

    assert result.items[0].outcome == "failed"
    assert result.items[0].reason_code == "robots_denied"
    assert session.calls == []
    assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (0,)
    conn.close()


def test_batch_parallelizes_fetches_but_persists_in_candidate_order(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    registry = IssuerRegistry(conn)
    registry.persist(
        IssuerEntity(
            issuer_id="issuer-beta",
            idempotency_key="issuer-beta",
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    registry.persist(
        LegacyIssuerBindingRevision(
            binding_revision_id="legacy-beta-1",
            idempotency_key="legacy-beta-1",
            recorded_issuer_id="legacy-ticker:BETA",
            revision=1,
            issuer_id="issuer-beta",
            outcome="selected",
            decision_kind="deterministic",
            reason_code="test_fixture",
            reason_details=(("ticker", "BETA"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    conn.commit()
    sessions = [
        _Session(
            [
                _Response(
                    200,
                    headers={"Content-Type": "text/html"},
                    body=BODY,
                )
            ]
        )
        for _index in range(2)
    ]
    factory_lock = threading.Lock()

    def _factory() -> _Session:
        with factory_lock:
            return sessions.pop()

    result = verify_ir_home_candidates(
        conn,
        request=IRHomeBatchRequest(
            candidates=(
                IRHomeAuthorityCandidate(
                    ticker="ACME",
                    requested_url=URL,
                    required_marker_groups=(("Acme",), ("Investor Relations",)),
                ),
                IRHomeAuthorityCandidate(
                    ticker="BETA",
                    requested_url="https://investor.beta.test/",
                    required_marker_groups=(("Acme",), ("Investor Relations",)),
                ),
            ),
            blob_root=tmp_path / "blobs",
            apply=True,
            recorded_at=STAMP,
            user_agent="Acme research contact@example.test",
            connect_timeout_seconds=5,
            read_timeout_seconds=10,
            max_workers=2,
        ),
        session_factory=_factory,
        robots_resolver=lambda url: (lambda candidate_url: candidate_url == url, 0.0),
        sleeper=lambda _seconds: None,
    )

    assert [item.ticker for item in result.items] == ["ACME", "BETA"]
    assert [item.outcome for item in result.items] == ["verified", "verified"]
    assert conn.execute("SELECT COUNT(*) FROM issuer_authority_surface_revisions").fetchone() == (
        2,
    )
    conn.close()
