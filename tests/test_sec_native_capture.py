"""Evidence-native capture contracts for sealed SEC expected documents."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
from pydantic import ValidationError

from execution import capture_expected_sec_documents as cli
from execution import sync_sec_filing_inventory as inventory_cli
from filings.sec_submissions_inventory import SecFilingInventoryEntry
from pipeline.sec_operations_view import read_sec_coverage_state
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
from provenance.sec_execution import read_sec_executions
from provenance.sec_native_capture import (
    SecNativeCaptureError,
    SecNativeCaptureHardStopError,
    SecNativeCaptureRequest,
    capture_expected_sec_documents,
    load_captured_sec_filing_package,
    load_expected_sec_documents,
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


def _conn(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    *,
    source_url: str = SOURCE_URL,
    expected_period_end: datetime | None = datetime(2025, 12, 31, tzinfo=UTC),
) -> sqlite3.Connection:
    path = tmp_path / "sec-native-capture.db"
    migrated_db(path)
    conn = sqlite3.connect(path)
    from sqlite_runtime import register_sqlite_integrity_functions

    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO issuer_entities VALUES(?,?,?,?)",
        ("issuer-acme", "issuer-acme", "operating_company", STAMP),
    )
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "sec-periodic:v1",
            "sec-periodic:v1",
            "sec-periodic",
            1,
            "issuer-acme",
            None,
            "sec_edgar",
            "operating_company_periodic",
            "required",
            "regulator_inventory",
            datetime(2026, 1, 1, tzinfo=UTC),
            None,
            "deterministic",
            "test",
            "{}",
            STAMP,
            STAMP,
            STAMP,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO tracked_companies(ticker,name,list_type,sec_validated,filing_regime,instrument_type) VALUES('ACME','Synthetic issuer','portfolio',1,'10-K','equity')"
    )
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
            period_end=expected_period_end,
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


def test_sec_report_date_flows_through_expected_document_to_native_capture_and_replay(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    filing = SecFilingInventoryEntry(
        issuer_id="issuer-acme",
        ticker="ACME",
        accession_number="0000000001-26-000001",
        form_type="10-K",
        filing_date="2026-02-10",
        report_date="2025-12-31",
        accepted_at=None,
        primary_document="acme-20251231x10k.htm",
        primary_document_url=SOURCE_URL,
        source_component_name="CIK0000000001.json",
    )
    expected = inventory_cli.build_expected_documents(
        issuer_id=filing.issuer_id, filings=(filing,), packages=()
    )[0]
    assert expected.period_end == datetime(2025, 12, 31, tzinfo=UTC)
    conn = _conn(tmp_path, migrated_db, expected_period_end=expected.period_end)
    try:
        request = _request(tmp_path, apply=False)
        preview = capture_expected_sec_documents(
            conn, request, session=FakeSession([FakeResponse()])
        )
        assert preview.fetched == 1
        applied = capture_expected_sec_documents(
            conn, request.model_copy(update={"apply": True}), session=FakeSession([])
        )
        assert applied.fetched == 1
        replay = capture_expected_sec_documents(
            conn, request.model_copy(update={"apply": True}), session=FakeSession([])
        )
        assert replay.considered == 0
        version = conn.execute(
            "SELECT period_start,period_end FROM evidence_document_versions"
        ).fetchone()
        assert version is not None and version[0] is None
        assert datetime.fromisoformat(str(version[1])) == expected.period_end
    finally:
        conn.close()


def test_fetch_candidates_exclude_authority_omitted_locators(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    coverage = SourceCoverageLedger(conn)
    coverage.persist(
        ExpectedDocument(
            expected_document_id="expected-unnamed-exhibit",
            idempotency_key="expected-unnamed-exhibit",
            snapshot_id="inventory-snapshot",
            expected_document_key="issuer-acme:0000000001-26-000001:attachment:opaque",
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_filing",
            document_type="sec_exhibit",
            form_type="10-K",
            accession_number="0000000001-26-000001",
            source_url=None,
            primary_document=None,
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
            assessment_id="coverage-unnamed-exhibit",
            idempotency_key="coverage-unnamed-exhibit",
            expected_document_id="expected-unnamed-exhibit",
            revision=1,
            coverage_status="authority_unavailable",
            document_version_id=None,
            extraction_run_id=None,
            manifest_id=None,
            index_run_id=None,
            reason_code="sec_authority_attachment_locator_omitted",
            reason_details=(("locator_status", "authority_omitted"),),
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
    conn.commit()
    try:
        candidates, has_more = load_expected_sec_documents(
            conn,
            inventory_keys=(INVENTORY_KEY,),
            limit=10,
        )
        assert [item.expected_document_id for item in candidates] == ["expected-10k"]
        assert not has_more
    finally:
        conn.close()


def test_captured_package_excludes_authority_omitted_financial_locator(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    coverage = SourceCoverageLedger(conn)
    coverage.persist(
        ExpectedDocument(
            expected_document_id="expected-unnamed-financial",
            idempotency_key="expected-unnamed-financial",
            snapshot_id="inventory-snapshot",
            expected_document_key="issuer-acme:0000000001-26-000001:attachment:opaque-financial",
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_filing",
            document_type="sec_financial_report",
            form_type="10-K",
            accession_number="0000000001-26-000001",
            source_url=None,
            primary_document=None,
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
            assessment_id="coverage-unnamed-financial",
            idempotency_key="coverage-unnamed-financial",
            expected_document_id="expected-unnamed-financial",
            revision=1,
            coverage_status="authority_unavailable",
            document_version_id=None,
            extraction_run_id=None,
            manifest_id=None,
            index_run_id=None,
            reason_code="sec_authority_attachment_locator_omitted",
            reason_details=(("locator_status", "authority_omitted"),),
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
    conn.commit()
    try:
        capture_expected_sec_documents(
            conn,
            _request(tmp_path, apply=True),
            session=FakeSession([FakeResponse()]),
        )

        members = load_captured_sec_filing_package(
            conn,
            inventory_key=INVENTORY_KEY,
            accession_number="0000000001-26-000001",
        )

        assert [member.expected_document_id for member in members] == ["expected-10k"]
    finally:
        conn.close()


def test_dry_run_fetches_to_checkpoint_without_database_or_durable_blob_writes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    session = FakeSession([FakeResponse()])
    try:
        result = capture_expected_sec_documents(
            conn,
            _request(tmp_path, apply=False),
            session=session,
        )
        assert conn.execute("SELECT COUNT(*) FROM sec_execution_receipts").fetchone()[0] == 0
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
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
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
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        captured = capture_expected_sec_documents(
            conn,
            _request(tmp_path, apply=True),
            session=FakeSession([FakeResponse()]),
        )
        document_version_id = captured.items[0].document_version_id
        assert document_version_id is not None
        document_recorded_at = datetime.fromisoformat(
            str(
                conn.execute(
                    "SELECT recorded_at FROM evidence_document_versions "
                    "WHERE document_version_id=?",
                    (document_version_id,),
                ).fetchone()[0]
            )
        )
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
            recorded_at=document_recorded_at,
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
                recorded_at=document_recorded_at,
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
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
    request = _request(tmp_path, apply=True)
    try:
        first = capture_expected_sec_documents(
            conn,
            request,
            session=FakeSession([requests.Timeout("secret response body")]),
        )
        projected = read_sec_coverage_state(tmp_path / "sec-native-capture.db")
        assert projected.companies[0].documents[0].state == "deferred"
        assert projected.companies[0].executions[0].receipt.state == "deferred"
        assert projected.companies[0].executions[0].population_matches
        assert projected.execution_gap_count == 1
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
        assert read_sec_executions(conn, ticker="ACME")[0].state == "succeeded"
        assert second.fetched == 1
        assert conn.execute("SELECT coverage_status FROM v_source_coverage_current").fetchone() == (
            "captured",
        )
        assert conn.execute("SELECT COUNT(*) FROM source_coverage_assessments").fetchone()[0] == 3
    finally:
        conn.close()


def test_sec_403_retains_failure_with_no_document_admission(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
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
        assert read_sec_executions(conn, ticker="ACME")[0].state == "failed"
        assert "sec_authorization_hard_stop" in checkpoint
        assert "do not log me" not in checkpoint
    finally:
        conn.close()


def test_sealed_identity_mismatch_fails_before_network_access(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    wrong = SOURCE_URL.replace("000000000126000001", "000000000126999999")
    conn = _conn(tmp_path, migrated_db, source_url=wrong)
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
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
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
    migrated_db: Callable[..., Path],
) -> None:
    conn = _conn(tmp_path, migrated_db)
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
            "--accession-number",
            "0000000001-26-000001",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert '"mode":"dry_run"' in captured.out
    assert '"selection_scope":"accessions"' in captured.out
    assert "sec_native_capture_completed" in captured.err
    assert not (tmp_path / "cli-blobs").exists()


def test_actual_apply_retains_execution_completion_beyond_checkpoint(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        result = capture_expected_sec_documents(
            conn, _request(tmp_path, apply=True), session=FakeSession([FakeResponse()])
        )
        assert result.fetched == 1
        rows = conn.execute("SELECT state FROM sec_execution_receipts ORDER BY sequence").fetchall()
        assert [row[0] for row in rows] == ["requested", "running", "succeeded"]
    finally:
        conn.close()


def test_interrupted_apply_retains_running_without_terminal_success(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    class InterruptedSession(FakeSession):
        def get(
            self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int], stream: bool
        ) -> FakeResponse:
            raise KeyboardInterrupt("synthetic interruption")

    conn = _conn(tmp_path, migrated_db)
    try:
        with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
            capture_expected_sec_documents(
                conn, _request(tmp_path, apply=True), session=InterruptedSession([])
            )
        receipt = read_sec_executions(conn, ticker="ACME")[0]
        assert receipt.state == "running" and receipt.result is None
        assert (
            conn.execute("SELECT COUNT(*) FROM sec_execution_receipts WHERE sequence=2").fetchone()[
                0
            ]
            == 0
        )
        company = read_sec_coverage_state(tmp_path / "sec-native-capture.db").companies[0]
        assert company.executions[0].receipt.result is None
        assert company.executions[0].population_matches
        assert company.coverage_status != "Covered / freshness unknown"
    finally:
        conn.close()


FIRST = "0000000001-26-000001"
SECOND = "0000000001-26-000002"


def _second(conn: sqlite3.Connection) -> None:
    SourceCoverageLedger(conn).persist(
        ExpectedDocument(
            expected_document_id="expected-second",
            idempotency_key="expected-second",
            snapshot_id="inventory-snapshot",
            expected_document_key="issuer-acme:" + SECOND,
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_filing",
            document_type="filing",
            form_type="10-Q",
            accession_number=SECOND,
            source_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000002/second.htm",
            primary_document="second.htm",
            period_start=None,
            period_end=STAMP,
            filing_at=STAMP,
            expected_at=None,
            expectation_basis="authoritative",
            recorded_at=STAMP,
        )
    )
    conn.commit()


@pytest.mark.parametrize(
    "values",
    [
        (FIRST, FIRST),
        ("000000000126000001",),
        (" 0000000001-26-000001",),
        ("x' OR 1=1 --",),
        tuple(f"0000000001-26-{i:06d}" for i in range(251)),
    ],
)
def test_selector_is_exact_unique_and_bounded(tmp_path: Path, values: tuple[str, ...]) -> None:
    payload = _request(tmp_path, apply=False).model_dump()
    payload["accession_numbers"] = values
    with pytest.raises(ValidationError):
        SecNativeCaptureRequest.model_validate(payload)


def test_only_selected_accession_captured_and_receipt_bound(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        _second(conn)
        req = SecNativeCaptureRequest.model_validate(
            {**_request(tmp_path, apply=False).model_dump(), "accession_numbers": (SECOND,)}
        )
        session = FakeSession([FakeResponse()])
        preview = capture_expected_sec_documents(conn, req, session=session)
        assert session.calls == [
            "https://www.sec.gov/Archives/edgar/data/1/000000000126000002/second.htm"
        ]
        assert [x.expected_document_id for x in preview.items] == ["expected-second"]
        assert preview.selection_scope == "accessions" and preview.accession_numbers == (SECOND,)
        assert not preview.has_more and preview.pending_outside_selection == 1
        apply_session = FakeSession([])
        result = capture_expected_sec_documents(
            conn, req.model_copy(update={"apply": True}), session=apply_session
        )
        assert apply_session.calls == [] and result.fetched == 1
        receipt = read_sec_executions(conn, ticker="ACME")[0]
        assert receipt.scope.expected_document_ids == ("expected-second",)
        assert receipt.scope.snapshot_ids == ("inventory-snapshot",)
        assert receipt.result is not None and receipt.result.captured == 1
        remaining, more = load_expected_sec_documents(
            conn, inventory_keys=(INVENTORY_KEY,), limit=10
        )
        assert [x.expected_document_id for x in remaining] == ["expected-10k"] and not more
        completed = capture_expected_sec_documents(
            conn, req.model_copy(update={"apply": True}), session=FakeSession([])
        )
        assert completed.considered == 0 and completed.pending_outside_selection == 1
    finally:
        conn.close()


def test_unknown_selector_refuses_without_network_checkpoint_or_receipt(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        req = SecNativeCaptureRequest.model_validate(
            {**_request(tmp_path, apply=True).model_dump(), "accession_numbers": (SECOND,)}
        )
        session = FakeSession([])
        before = conn.total_changes
        with pytest.raises(SecNativeCaptureError, match="unknown or ambiguous"):
            capture_expected_sec_documents(conn, req, session=session)
        assert not session.calls and conn.total_changes == before
        assert not req.checkpoint_root.exists()
    finally:
        conn.close()


def test_checkpoint_cannot_change_selector_or_reuse_legacy_filtered(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        _second(conn)
        req = SecNativeCaptureRequest.model_validate(
            {**_request(tmp_path, apply=False).model_dump(), "accession_numbers": (FIRST,)}
        )
        capture_expected_sec_documents(conn, req, session=FakeSession([FakeResponse()]))
        state = req.checkpoint_root / req.task_id / "state.json"
        before = state.read_bytes()
        for accessions in [(SECOND,), ()]:
            with pytest.raises(SecNativeCaptureError, match="selection scope differs"):
                capture_expected_sec_documents(
                    conn,
                    req.model_copy(update={"accession_numbers": accessions, "apply": True}),
                    session=FakeSession([]),
                )
            assert state.read_bytes() == before
        payload = json.loads(before)
        payload.pop("scope_sha256")
        state.write_text(json.dumps(payload))
        with pytest.raises(SecNativeCaptureError, match="legacy checkpoint"):
            capture_expected_sec_documents(conn, req, session=FakeSession([]))
        # Existing unfiltered legacy replay still consumes retained exact bytes.
        result = capture_expected_sec_documents(
            conn,
            req.model_copy(update={"accession_numbers": (), "apply": True}),
            session=FakeSession([FakeResponse()]),
        )
        assert result.fetched == 2
    finally:
        conn.close()


def test_multiple_accessions_have_selected_batch_has_more(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        _second(conn)
        selected, more = load_expected_sec_documents(
            conn, inventory_keys=(INVENTORY_KEY,), accession_numbers=(SECOND, FIRST), limit=1
        )
        assert len(selected) == 1 and more
        assert selected[0].accession_number == FIRST
    finally:
        conn.close()


def test_cross_inventory_selectors_bind_unambiguously(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        cursor = conn.execute(
            "SELECT * FROM source_inventory_snapshots WHERE snapshot_id='inventory-snapshot'"
        )
        snapshot = SourceInventorySnapshot.model_validate(
            dict(zip((column[0] for column in cursor.description), cursor.fetchone(), strict=True))
        )
        SourceCoverageLedger(conn).persist(
            snapshot.model_copy(
                update={
                    "snapshot_id": "other-snapshot",
                    "idempotency_key": "other-snapshot",
                    "inventory_key": "other-inventory",
                }
            )
        )
        cursor = conn.execute(
            "SELECT * FROM source_inventory_components WHERE component_id='inventory-component'"
        )
        component = InventoryComponent.model_validate(
            dict(zip((column[0] for column in cursor.description), cursor.fetchone(), strict=True))
        ).model_copy(
            update={
                "snapshot_id": "other-snapshot",
                "component_id": "other-component",
                "idempotency_key": "other-component",
            }
        )
        sealstore = SourceInventorySealStore(conn)
        sealstore.persist(component)
        sealstore.persist(
            InventorySeal(
                snapshot_id="other-snapshot",
                expected_component_count=1,
                component_digest_sha256=component_digest((component,)),
                completion_status="complete",
                sealed_at=STAMP,
            )
        )
        cursor = conn.execute(
            "SELECT * FROM expected_documents WHERE expected_document_id='expected-10k'"
        )
        expected = ExpectedDocument.model_validate(
            dict(zip((column[0] for column in cursor.description), cursor.fetchone(), strict=True))
        )
        SourceCoverageLedger(conn).persist(
            expected.model_copy(
                update={
                    "expected_document_id": "other-expected",
                    "idempotency_key": "other-expected",
                    "snapshot_id": "other-snapshot",
                    "expected_document_key": "other-key",
                    "accession_number": SECOND,
                    "source_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000002/second.htm",
                    "primary_document": "second.htm",
                }
            )
        )
        conn.commit()
        candidates, more = load_expected_sec_documents(
            conn,
            inventory_keys=(INVENTORY_KEY, "other-inventory"),
            accession_numbers=(FIRST, SECOND),
            limit=10,
        )
        assert not more and {x.expected_document_id for x in candidates} == {
            "expected-10k",
            "other-expected",
        }
        # A second current inventory claiming the same accession must not silently fan out.
        SourceCoverageLedger(conn).persist(
            expected.model_copy(
                update={
                    "expected_document_id": "duplicate-expected",
                    "idempotency_key": "duplicate-expected",
                    "snapshot_id": "other-snapshot",
                    "expected_document_key": "duplicate-key",
                }
            )
        )
        conn.commit()
        with pytest.raises(SecNativeCaptureError, match="unknown or ambiguous"):
            load_expected_sec_documents(
                conn,
                inventory_keys=(INVENTORY_KEY, "other-inventory"),
                accession_numbers=(FIRST,),
                limit=10,
            )
    finally:
        conn.close()


def test_zero_work_still_pins_selector_and_receipt_identity(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _conn(tmp_path, migrated_db)
    try:
        _second(conn)
        capture_expected_sec_documents(
            conn,
            _request(tmp_path, apply=True, task_id="seed"),
            session=FakeSession([FakeResponse(), FakeResponse()]),
        )
        request = SecNativeCaptureRequest.model_validate(
            {
                **_request(tmp_path, apply=False, task_id="empty").model_dump(),
                "accession_numbers": (FIRST,),
            }
        )
        preview = capture_expected_sec_documents(conn, request, session=FakeSession([]))
        assert preview.considered == 0
        checkpoint = request.checkpoint_root / request.task_id / "state.json"
        assert checkpoint.exists()
        before = checkpoint.read_bytes()
        for selector in [(SECOND,), ()]:
            with pytest.raises(SecNativeCaptureError, match="selection scope differs"):
                capture_expected_sec_documents(
                    conn,
                    request.model_copy(update={"accession_numbers": selector, "apply": True}),
                    session=FakeSession([]),
                )
            assert checkpoint.read_bytes() == before
        capture_expected_sec_documents(
            conn, request.model_copy(update={"apply": True}), session=FakeSession([])
        )
        first_receipt = read_sec_executions(conn, ticker="ACME")[0]
        # Separate checkpoint roots permit isolated requests with the same task label.
        # Even when selected document IDs are empty, their accession intents differ.
        other = request.model_copy(
            update={
                "checkpoint_root": tmp_path / "other-checkpoints",
                "accession_numbers": (SECOND,),
                "apply": True,
            }
        )
        capture_expected_sec_documents(conn, other, session=FakeSession([]))
        second_receipt = read_sec_executions(conn, ticker="ACME")[0]
        assert (
            first_receipt.scope.expected_document_ids
            == second_receipt.scope.expected_document_ids
            == ()
        )
        assert first_receipt.request_id != second_receipt.request_id
    finally:
        conn.close()


# Shared migrated synthetic inventory for selected-accession orchestration tests.
seed_sec_capture_inventory = _conn
seed_second_sec_accession = _second
