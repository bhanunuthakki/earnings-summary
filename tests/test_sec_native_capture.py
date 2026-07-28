"""Evidence-native capture contracts for sealed SEC expected documents."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
from alembic.config import Config

from alembic import command
from execution import capture_expected_sec_documents as cli
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.fulltext_extractor_identity import (
    STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR,
)
from provenance.sec_native_capture import (
    SecNativeCaptureError,
    SecNativeCaptureHardStopError,
    SecNativeCaptureRequest,
    capture_expected_sec_documents,
)
from provenance.source_coverage import (
    CoverageAssessment,
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from provenance.source_coverage_refresh import (
    CoverageRefreshRequest,
    refresh_source_coverage,
)
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)
from search.corpus_builder import (
    CorpusBuildRequest,
    build_grounded_search_corpus,
)
from search.corpus_builder import (
    ExpectedDocument as CorpusExpectedDocument,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
CONFIG_SHA = "c" * 64
INVENTORY_KEY = "issuer-acme:sec-submissions"
SOURCE_URL = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/acme-20251231x10k.htm"
BODY = b"<html><body>Audited annual report</body></html>"


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
    def __init__(self, outcomes: list[FakeResponse | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
    ) -> FakeResponse:
        assert headers["User-Agent"] == "research-agent test@example.test"
        assert timeout == (10, 60)
        assert stream
        self.calls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path, *, source_url: str = SOURCE_URL) -> sqlite3.Connection:
    path = tmp_path / "sec-native-capture.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0220_source_inventory_seals")
    # Indexed coverage is a current-runtime contract. Projection seals were
    # added later without changing the SEC capture tables, so this focused
    # fixture fast-forwards only that additive search publication gate.
    command.stamp(config, "0232_document_semantic_dispositions")
    command.upgrade(config, "0233_search_projection_seals")
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE search_projection_seals ADD COLUMN runtime_artifact_sha256 TEXT")
    conn.execute("PRAGMA foreign_keys = ON")
    ledger = EvidenceLedger(conn)
    inventory_body = b'{"filings":{"recent":{}}}'
    inventory_sha = hashlib.sha256(inventory_body).hexdigest()
    ledger.persist(
        ContentBlob(
            sha256=inventory_sha,
            byte_size=len(inventory_body),
            media_type="application/json",
            storage_uri="file:///inventory.json",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="inventory-observation",
            idempotency_key="inventory-observation",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
            blob_sha256=inventory_sha,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="sec-inventory@test",
        )
    )
    coverage = SourceCoverageLedger(conn)
    coverage.persist(
        SourceInventorySnapshot(
            snapshot_id="inventory-snapshot",
            idempotency_key="inventory-snapshot",
            inventory_key=INVENTORY_KEY,
            revision=1,
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
            source_observation_id="inventory-observation",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="sec-inventory@test",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
            supersedes_snapshot_id=None,
        )
    )
    coverage.persist(
        ExpectedDocument(
            expected_document_id="expected-10k",
            idempotency_key="expected-10k",
            snapshot_id="inventory-snapshot",
            expected_document_key="issuer-acme:0000000001-26-000001",
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_filing",
            document_type="filing",
            form_type="10-K",
            accession_number="0000000001-26-000001",
            source_url=source_url,
            primary_document="acme-20251231x10k.htm",
            period_start=None,
            period_end=datetime(2025, 12, 31, tzinfo=UTC),
            filing_at=datetime(2026, 2, 10, tzinfo=UTC),
            expected_at=None,
            expectation_basis="authoritative",
            recorded_at=STAMP,
        )
    )
    coverage.persist(
        CoverageAssessment(
            assessment_id="coverage-available",
            idempotency_key="coverage-available",
            expected_document_id="expected-10k",
            revision=1,
            coverage_status="available",
            document_version_id=None,
            extraction_run_id=None,
            manifest_id=None,
            index_run_id=None,
            reason_code="sec_authority_inventory",
            reason_details=(("source", "submissions"),),
            decision_kind="deterministic",
            policy_name="source-coverage-reconcile",
            policy_version="1",
            policy_config_sha256=CONFIG_SHA,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            supersedes_assessment_id=None,
            material_dissent=False,
        )
    )
    component = InventoryComponent(
        component_id="inventory-component",
        idempotency_key="inventory-component",
        snapshot_id="inventory-snapshot",
        component_key="root",
        component_kind="primary",
        source_url="https://data.sec.gov/submissions/CIK0000000001.json",
        source_observation_id="inventory-observation",
        outcome="succeeded",
        required=True,
        failure_reason=None,
        ordinal=0,
        recorded_at=STAMP,
    )
    seals = SourceInventorySealStore(conn)
    seals.persist(component)
    seals.persist(
        InventorySeal(
            snapshot_id="inventory-snapshot",
            expected_component_count=1,
            component_digest_sha256=component_digest((component,)),
            completion_status="complete",
            sealed_at=STAMP,
        )
    )
    conn.commit()
    return conn


def _request(
    tmp_path: Path, *, apply: bool, task_id: str = "capture-10k"
) -> SecNativeCaptureRequest:
    return SecNativeCaptureRequest(
        inventory_keys=(INVENTORY_KEY,),
        checkpoint_root=tmp_path / "checkpoints",
        blob_root=tmp_path / "blobs",
        task_id=task_id,
        user_agent="research-agent test@example.test",
        apply=apply,
        batch_size=10,
        minimum_request_interval_seconds=0,
    )


def test_dry_run_fetches_to_checkpoint_without_database_or_durable_blob_writes(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    session = FakeSession([FakeResponse()])
    try:
        result = capture_expected_sec_documents(
            conn,
            _request(tmp_path, apply=False),
            session=session,
        )
        assert result.mode == "dry_run"
        assert result.fetched == 1
        assert session.calls == [SOURCE_URL]
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 0
        assert not (tmp_path / "blobs").exists()
        response_files = tuple(
            path
            for path in (tmp_path / "checkpoints" / "capture-10k" / "responses").iterdir()
            if path.is_file()
        )
        assert len(response_files) == 1
        assert response_files[0].read_bytes() == BODY
    finally:
        conn.close()


def test_apply_reuses_verified_checkpoint_and_atomically_persists_full_chain(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    request = _request(tmp_path, apply=False)
    dry_session = FakeSession([FakeResponse()])
    try:
        capture_expected_sec_documents(conn, request, session=dry_session)
        apply_session = FakeSession([])
        result = capture_expected_sec_documents(
            conn,
            request.model_copy(update={"apply": True}),
            session=apply_session,
        )
        assert result.records_created == 6
        assert apply_session.calls == []
        digest = hashlib.sha256(BODY).hexdigest()
        assert (tmp_path / "blobs" / digest[:2] / digest).read_bytes() == BODY
        document = conn.execute(
            "SELECT document_key, version_sequence, accession_number, legacy_document_id "
            "FROM evidence_document_versions"
        ).fetchone()
        assert document == (
            "issuer-acme:0000000001-26-000001",
            1,
            "0000000001-26-000001",
            None,
        )
        assert conn.execute(
            "SELECT link_kind FROM evidence_document_observation_links"
        ).fetchone() == ("primary",)
        assert (
            conn.execute(
                "SELECT coverage_status, document_version_id FROM v_source_coverage_current"
            ).fetchone()[0]
            == "captured"
        )

        replay = capture_expected_sec_documents(
            conn,
            request.model_copy(update={"apply": True}),
            session=FakeSession([]),
        )
        assert replay.considered == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 1
    finally:
        conn.close()


def test_extraction_lineage_promotes_current_coverage_without_rescanning_inventory(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        captured = capture_expected_sec_documents(
            conn,
            _request(tmp_path, apply=True),
            session=FakeSession([FakeResponse()]),
        )
        document_version_id = captured.items[0].document_version_id
        assert document_version_id is not None
        digest = hashlib.sha256(BODY).hexdigest()
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ExtractionRun(
                extraction_run_id="fulltext-run",
                idempotency_key="fulltext-run",
                document_version_id=document_version_id,
                input_sha256=digest,
                extractor_name="fulltext-evidence-backfill",
                extractor_config_sha256=(STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.config_sha256),
                extractor_code_version=(STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.code_version),
                output_sha256=CONFIG_SHA,
                started_at=STAMP,
                completed_at=STAMP,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id="fulltext-node",
                evidence_key="fulltext-node",
                revision=1,
                extraction_run_id="fulltext-run",
                node_kind="passage",
                text="Revenue grew.",
                recorded_at=STAMP,
            )
        )
        conn.commit()
        request = CoverageRefreshRequest(
            inventory_keys=(INVENTORY_KEY,),
            recorded_at=STAMP,
            apply=False,
        )

        dry_run = refresh_source_coverage(conn, request)
        assert dry_run.assessments_planned == 1
        assert conn.execute("SELECT coverage_status FROM v_source_coverage_current").fetchone() == (
            "captured",
        )

        applied = refresh_source_coverage(
            conn,
            request.model_copy(update={"apply": True}),
        )
        assert applied.assessments_created == 1
        assert conn.execute(
            "SELECT coverage_status, extraction_run_id FROM v_source_coverage_current"
        ).fetchone() == ("extracted", "fulltext-run")
        assert (
            refresh_source_coverage(
                conn,
                request.model_copy(update={"apply": True}),
            ).assessments_planned
            == 0
        )

        corpus = build_grounded_search_corpus(
            conn,
            CorpusBuildRequest(
                corpus_key="issuer-acme:reporting",
                revision=1,
                selector_code_version="corpus-builder@1",
                recorded_at=STAMP,
                expected_documents=(
                    CorpusExpectedDocument(
                        expected_document_key="issuer-acme:2025:10-K",
                        document_version_id=document_version_id,
                        membership_status="included",
                        reason="verified source evidence",
                    ),
                ),
                required_extractor_names=("fulltext-evidence-backfill",),
                apply=True,
            ),
        )
        indexed_plan = refresh_source_coverage(conn, request)

        assert corpus.completion_status == "complete"
        assert conn.execute("SELECT COUNT(*) FROM search_index_memberships").fetchone()[0] == 0
        assert indexed_plan.target_status_counts == {"indexed": 1}
        indexed = refresh_source_coverage(
            conn,
            request.model_copy(update={"apply": True}),
        )
        assert indexed.assessments_created == 1
        assert conn.execute(
            "SELECT coverage_status, manifest_id, index_run_id FROM v_source_coverage_current"
        ).fetchone() == (
            "indexed",
            corpus.manifest_id,
            corpus.lexical_index_run_id,
        )
    finally:
        conn.close()


def test_transient_failure_is_deferred_then_retried_and_audited(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    request = _request(tmp_path, apply=True)
    try:
        first = capture_expected_sec_documents(
            conn,
            request,
            session=FakeSession([requests.Timeout("secret response body")]),
        )
        assert first.deferred == 1
        assert first.items[0].reason_code == "sec_fetch_timeout"
        assert conn.execute(
            "SELECT coverage_status, reason_code FROM v_source_coverage_current"
        ).fetchone() == ("fetch_failed", "sec_fetch_timeout")

        second = capture_expected_sec_documents(
            conn,
            request,
            session=FakeSession([FakeResponse()]),
        )
        assert second.fetched == 1
        assert conn.execute("SELECT coverage_status FROM v_source_coverage_current").fetchone() == (
            "captured",
        )
        assert conn.execute("SELECT COUNT(*) FROM source_coverage_assessments").fetchone()[0] == 3
    finally:
        conn.close()


def test_sec_403_is_a_hard_stop_with_checkpoint_and_no_database_mutation(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        with pytest.raises(SecNativeCaptureHardStopError):
            capture_expected_sec_documents(
                conn,
                _request(tmp_path, apply=True),
                session=FakeSession([FakeResponse(status_code=403, body=b"do not log me")]),
            )
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 0
        checkpoint = (tmp_path / "checkpoints" / "capture-10k" / "state.json").read_text(
            encoding="utf-8"
        )
        assert "sec_authorization_hard_stop" in checkpoint
        assert "do not log me" not in checkpoint
    finally:
        conn.close()


def test_sealed_identity_mismatch_fails_before_network_access(tmp_path: Path) -> None:
    wrong = SOURCE_URL.replace("000000000126000001", "000000000126999999")
    conn = _conn(tmp_path, source_url=wrong)
    session = FakeSession([])
    try:
        with pytest.raises(SecNativeCaptureError, match="rejected identity"):
            capture_expected_sec_documents(
                conn,
                _request(tmp_path, apply=False),
                session=session,
            )
        assert session.calls == []
    finally:
        conn.close()


def test_metadata_conflict_rolls_back_the_entire_database_batch(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    digest = hashlib.sha256(BODY).hexdigest()
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(BODY),
            media_type="text/html",
            storage_uri="file:///prior-copy",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="prior-observation",
            idempotency_key="prior-observation",
            source_kind="sec_filing",
            source_url=SOURCE_URL,
            blob_sha256=digest,
            source_published_at=None,
            filing_at=STAMP,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="prior@test",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="conflicting-document",
            document_key="issuer-acme:0000000001-26-000001",
            version_sequence=1,
            observation_id="prior-observation",
            blob_sha256=digest,
            issuer_id="issuer-acme",
            ticker="ACME",
            document_type="filing",
            form_type="8-K",
            accession_number="0000000001-26-000001",
            exhibit_id=None,
            period_start=None,
            period_end=datetime(2025, 12, 31, tzinfo=UTC),
            as_of_at=STAMP,
            language="und",
            replaces_document_version_id=None,
            legacy_document_id=None,
            recorded_at=STAMP,
        )
    )
    conn.commit()
    observations_before = conn.execute(
        "SELECT COUNT(*) FROM evidence_source_observations"
    ).fetchone()[0]
    try:
        with pytest.raises(SecNativeCaptureError, match="metadata"):
            capture_expected_sec_documents(
                conn,
                _request(tmp_path, apply=True),
                session=FakeSession([FakeResponse()]),
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()[0]
            == observations_before
        )
        assert conn.execute("SELECT coverage_status FROM v_source_coverage_current").fetchone() == (
            "available",
        )
        assert (tmp_path / "blobs" / digest[:2] / digest).read_bytes() == BODY
    finally:
        conn.close()


def test_cli_defaults_to_read_only_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = _conn(tmp_path)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    session = FakeSession([FakeResponse()])
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    monkeypatch.setattr(cli, "sec_user_agent", lambda: "research-agent test@example.test")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    exit_code = cli.main(
        [
            "--db",
            str(db_path),
            "--inventory-key",
            INVENTORY_KEY,
            "--checkpoint-root",
            str(tmp_path / "cli-checkpoints"),
            "--blob-root",
            str(tmp_path / "cli-blobs"),
            "--task-id",
            "cli-dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"mode":"dry_run"' in captured.out
    assert "sec_native_capture_completed" in captured.err
    assert not (tmp_path / "cli-blobs").exists()
