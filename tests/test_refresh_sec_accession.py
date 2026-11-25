"""Hermetic contracts for single-accession SEC refresh and resumption (BHA-161)."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from filings.inline_xbrl_processor import (
    ApprovedProcessorBundle,
    ProcessorBundleManifest,
    load_processor_bundle_manifest,
)
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.sec_accession_refresh import (
    SecAccessionRefreshError,
    SecAccessionRefreshRequest,
    refresh_sec_accession,
)
from provenance.sec_filing_xbrl_ingest import FilingXbrlIngestResult
from provenance.sec_native_capture import SessionLike
from provenance.source_coverage import (
    CoverageAssessment,
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
CONFIG_SHA = "c" * 64
INVENTORY_KEY = "issuer-acme:sec-submissions"
ACCESSION = "0000000001-26-000001"
CIK = "0000000001"
SOURCE_URL = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/acme-20251231x10k.htm"
BODY = b"<html><body>Audited annual report</body></html>"


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, body: bytes = BODY) -> None:
        self.status_code = status_code
        self.body = body
        self.headers: Mapping[str, str] = {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Length": str(len(body)),
        }

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for offset in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        return None


class _FakeSession:
    def __init__(self, outcomes: list[_FakeResponse | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
    ) -> _FakeResponse:
        del headers, timeout, stream
        self.calls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _seed_db(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    *,
    form_type: str = "10-K",
) -> Path:
    db_path = tmp_path / "sec-refresh.db"
    migrated_db(db_path)
    conn = sqlite3.connect(db_path)
    from sqlite_runtime import register_sqlite_integrity_functions

    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
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
            "INSERT INTO tracked_companies(ticker,name,list_type,sec_validated,filing_regime,instrument_type) "
            "VALUES('ACME','Synthetic issuer','portfolio',1,'10-K','equity')"
        )
        ledger = EvidenceLedger(conn)
        inv_body = b'{"filings":{"recent":{}}}'
        inv_sha = hashlib.sha256(inv_body).hexdigest()
        ledger.persist(
            ContentBlob(
                sha256=inv_sha,
                byte_size=len(inv_body),
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
                source_url=f"https://data.sec.gov/submissions/CIK{CIK}.json",
                blob_sha256=inv_sha,
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
                source_url=f"https://data.sec.gov/submissions/CIK{CIK}.json",
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
                expected_document_key=f"issuer-acme:{ACCESSION}",
                issuer_id="issuer-acme",
                ticker="ACME",
                source_kind="sec_filing",
                document_type="filing",
                form_type=form_type,
                accession_number=ACCESSION,
                source_url=SOURCE_URL,
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
            source_url=f"https://data.sec.gov/submissions/CIK{CIK}.json",
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
    finally:
        conn.close()
    return db_path


@dataclass(frozen=True)
class _FakeApprovedBundle:
    manifest: ProcessorBundleManifest


def _fake_bundle() -> ApprovedProcessorBundle:
    manifest = load_processor_bundle_manifest(ROOT / "config" / "filing_xbrl_processor_bundle.json")
    return cast(ApprovedProcessorBundle, _FakeApprovedBundle(manifest=manifest))


def _request(tmp_path: Path, db_path: Path, **overrides: object) -> SecAccessionRefreshRequest:
    base: dict[str, object] = {
        "db_path": db_path,
        "inventory_key": INVENTORY_KEY,
        "accession_number": ACCESSION,
        "expected_cik": CIK,
        "expected_form_type": "10-K",
        "expected_ticker": "ACME",
        "task_id": "acme-10k-refresh",
        "checkpoint_root": tmp_path / "checkpoints",
        "blob_root": tmp_path / "blobs",
        "user_agent": "research-agent test@example.test",
        "project_root": tmp_path,
        "minimum_request_interval_seconds": 0.0,
        "runtime_root": tmp_path / "runtime",
        "bundle_python": tmp_path / "python",
        "sandbox_launcher": tmp_path / "launcher",
        "recorded_at": STAMP,
    }
    base.update(overrides)
    return SecAccessionRefreshRequest.model_validate(base)


def test_dry_run_then_apply_separates_receipts_and_replays_idempotently(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _seed_db(tmp_path, migrated_db)
    session = _FakeSession([_FakeResponse(), _FakeResponse()])
    approved_bundle = _fake_bundle()

    ingest_calls: list[bool] = []

    def fake_ingest(
        conn: sqlite3.Connection,
        req: object,
        *,
        approved_bundle: ApprovedProcessorBundle,
    ) -> FilingXbrlIngestResult:
        del conn, req, approved_bundle
        replay = bool(ingest_calls)
        ingest_calls.append(True)
        return FilingXbrlIngestResult(
            mode="apply",
            accession_number=ACCESSION,
            extraction_run_id="xbrl-run-1",
            processor_artifact_sha256="a" * 64,
            input_member_count=1,
            raw_fact_count=4,
            normalized_count=4,
            rejected_count=0,
            published_count=0 if replay else 4,
            duplicate_count=4 if replay else 0,
            quarantined_count=0,
            exact_replay=replay,
            publication_id="pub-1",
        )

    import provenance.sec_accession_refresh as mod

    monkeypatch.setattr(mod, "ingest_sec_filing_xbrl", fake_ingest)

    dry = refresh_sec_accession(
        _request(tmp_path, db_path, apply=False),
        session=cast(SessionLike, session),
    )
    assert dry.mode == "dry_run"
    assert dry.status == "completed"
    assert dry.acquisition_receipt.status == "dry_run_fetched"
    assert dry.acquisition_receipt.fetched == 1
    assert dry.extraction_receipt.status == "not_run"
    assert dry.extraction_receipt.reason_code == "capture_not_persisted_in_dry_run"
    assert not ingest_calls

    applied = refresh_sec_accession(
        _request(tmp_path, db_path, apply=True),
        session=cast(SessionLike, session),
        approved_bundle=approved_bundle,
    )
    assert applied.mode == "apply"
    assert applied.status == "completed"
    assert applied.acquisition_receipt.status == "captured"
    assert applied.extraction_receipt.status == "extracted"
    assert applied.extraction_receipt.published_count == 4
    # Dry-run checkpoint staged the payload, so apply commits without a duplicate HTTP GET.
    assert len(session.calls) == 1

    replayed = refresh_sec_accession(
        _request(tmp_path, db_path, apply=True),
        session=cast(SessionLike, _FakeSession([])),
        approved_bundle=approved_bundle,
    )
    assert replayed.acquisition_receipt.status == "already_captured"
    assert replayed.extraction_receipt.status == "exact_replay"
    assert replayed.extraction_receipt.exact_replay is True


def test_validation_refuses_before_locks_or_network(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _seed_db(tmp_path, migrated_db, form_type="10-K/A")
    session = _FakeSession([])
    locks_dir = tmp_path / ".tmp" / "locks"

    with pytest.raises(SecAccessionRefreshError) as amendment_exc:
        refresh_sec_accession(
            _request(tmp_path, db_path, expected_form_type="10-K"),
            session=cast(SessionLike, session),
        )
    assert amendment_exc.value.reason_code == "amendment_identity_mismatch"

    with pytest.raises(SecAccessionRefreshError) as cik_exc:
        refresh_sec_accession(
            _request(tmp_path, db_path, expected_cik="0000000002", expected_form_type="10-K/A"),
            session=cast(SessionLike, session),
        )
    assert cik_exc.value.reason_code == "cik_mismatch"

    with pytest.raises(SecAccessionRefreshError) as acc_exc:
        refresh_sec_accession(
            _request(
                tmp_path,
                db_path,
                accession_number="0000000001-26-999999",
                expected_form_type="10-K/A",
            ),
            session=cast(SessionLike, session),
        )
    assert acc_exc.value.reason_code == "accession_not_in_inventory"

    assert session.calls == []
    assert not locks_dir.exists() or list(locks_dir.iterdir()) == []


def test_altered_selection_refused_before_locks_and_cancel_is_idempotent(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _seed_db(tmp_path, migrated_db)
    first = refresh_sec_accession(
        _request(tmp_path, db_path, capture_only=True),
        session=cast(SessionLike, _FakeSession([_FakeResponse()])),
    )
    assert first.acquisition_receipt.status == "dry_run_fetched"
    assert first.extraction_receipt.status == "not_run"

    cancelled = refresh_sec_accession(
        _request(tmp_path, db_path, cancel_pending=True),
        session=cast(SessionLike, _FakeSession([])),
    )
    assert cancelled.mode == "cancel"
    assert cancelled.status == "cancelled"
    assert cancelled.selection_sha256 == first.selection_sha256

    conn = sqlite3.connect(db_path)
    from sqlite_runtime import register_sqlite_integrity_functions

    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        coverage = SourceCoverageLedger(conn)
        coverage.persist(
            SourceInventorySnapshot(
                snapshot_id="inventory-snapshot-v2",
                idempotency_key="inventory-snapshot-v2",
                inventory_key=INVENTORY_KEY,
                revision=2,
                issuer_id="issuer-acme",
                ticker="ACME",
                source_kind="sec_submissions",
                source_url=f"https://data.sec.gov/submissions/CIK{CIK}.json",
                source_observation_id="inventory-observation",
                outcome="succeeded",
                authoritative=True,
                retrieval_config_sha256=CONFIG_SHA,
                collector_code_version="sec-inventory@test",
                started_at=STAMP,
                completed_at=STAMP,
                recorded_at=STAMP,
                supersedes_snapshot_id="inventory-snapshot",
            )
        )
        coverage.persist(
            ExpectedDocument(
                expected_document_id="expected-10q",
                idempotency_key="expected-10q",
                snapshot_id="inventory-snapshot-v2",
                expected_document_key=f"issuer-acme:{ACCESSION}",
                issuer_id="issuer-acme",
                ticker="ACME",
                source_kind="sec_filing",
                document_type="filing",
                form_type="10-Q",
                accession_number=ACCESSION,
                source_url=SOURCE_URL,
                primary_document="acme-20251231x10k.htm",
                period_start=None,
                period_end=datetime(2025, 12, 31, tzinfo=UTC),
                filing_at=datetime(2026, 2, 10, tzinfo=UTC),
                expected_at=None,
                expectation_basis="authoritative",
                recorded_at=STAMP,
            )
        )
        component2 = InventoryComponent(
            component_id="inventory-component-v2",
            idempotency_key="inventory-component-v2",
            snapshot_id="inventory-snapshot-v2",
            component_key="root",
            component_kind="primary",
            source_url=f"https://data.sec.gov/submissions/CIK{CIK}.json",
            source_observation_id="inventory-observation",
            outcome="succeeded",
            required=True,
            failure_reason=None,
            ordinal=0,
            recorded_at=STAMP,
        )
        seals = SourceInventorySealStore(conn)
        seals.persist(component2)
        seals.persist(
            InventorySeal(
                snapshot_id="inventory-snapshot-v2",
                expected_component_count=1,
                component_digest_sha256=component_digest((component2,)),
                completion_status="complete",
                sealed_at=STAMP,
            )
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SecAccessionRefreshError) as altered:
        refresh_sec_accession(
            _request(tmp_path, db_path, expected_form_type="10-Q"),
            session=cast(SessionLike, _FakeSession([])),
        )
    assert altered.value.reason_code == "altered_selection"


def test_incomplete_capture_stops_before_extraction_and_lock_contention_fails_closed(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _seed_db(tmp_path, migrated_db)
    deferred = refresh_sec_accession(
        _request(tmp_path, db_path, apply=True),
        session=cast(SessionLike, _FakeSession([_FakeResponse(status_code=503)])),
        approved_bundle=_fake_bundle(),
    )
    assert deferred.status == "incomplete"
    assert deferred.acquisition_receipt.status == "incomplete"
    assert deferred.extraction_receipt.status == "not_run"
    assert deferred.extraction_receipt.reason_code == "acquisition_incomplete"

    with (
        JobLock(
            tmp_path,
            "other-worker",
            ["sec-accession-refresh-checkpoint:acme-10k-refresh"],
            wait_s=0.0,
        ),
        pytest.raises(JobAlreadyRunningError),
    ):
        refresh_sec_accession(
            _request(tmp_path, db_path, cancel_pending=True),
            session=cast(SessionLike, _FakeSession([])),
        )


def test_cli_entrypoint_validates_and_cancels(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import execution.refresh_sec_accession as cli

    db_path = _seed_db(tmp_path, migrated_db)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SEC_USER_AGENT", "research-agent test@example.test")

    bad_rc = cli.main(
        [
            "--db",
            str(db_path),
            "--inventory-key",
            INVENTORY_KEY,
            "--accession",
            "bad-accession",
            "--cik",
            CIK,
            "--task-id",
            "cli-task",
        ]
    )
    assert bad_rc == 3

    mismatch_rc = cli.main(
        [
            "--db",
            str(db_path),
            "--inventory-key",
            INVENTORY_KEY,
            "--accession",
            ACCESSION,
            "--cik",
            CIK,
            "--form-type",
            "10-K/A",
            "--task-id",
            "cli-task",
            "--checkpoint-root",
            str(tmp_path / "cp"),
            "--blob-root",
            str(tmp_path / "blobs"),
        ]
    )
    assert mismatch_rc == 2

    cancel_rc = cli.main(
        [
            "--db",
            str(db_path),
            "--inventory-key",
            INVENTORY_KEY,
            "--accession",
            ACCESSION,
            "--cik",
            CIK,
            "--form-type",
            "10-K",
            "--task-id",
            "cli-task",
            "--checkpoint-root",
            str(tmp_path / "cp"),
            "--blob-root",
            str(tmp_path / "blobs"),
            "--cancel",
        ]
    )
    assert cancel_rc == 0
    assert '"status":"cancelled"' in capsys.readouterr().out
