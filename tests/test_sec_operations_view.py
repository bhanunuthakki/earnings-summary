"""SEC Operations consumes the actual immutable capture/coverage boundaries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pipeline.sec_operations_view import read_sec_coverage_state
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.evidence_links import BlobLocationObservation, EvidenceLinkLedger
from provenance.sec_companyfacts_capture import (
    SecCompanyFactsCaptureRequest,
    capture_sec_companyfacts,
    parse_companyfacts_body,
)
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
from sqlite_runtime import register_sqlite_integrity_functions

STAMP = datetime(2026, 9, 19, 12)
CONFIG = "a" * 64
ACCESSION = "0000000001-26-000001"


def seed_sec_fixture(conn: sqlite3.Connection, root: Path) -> None:
    """Explicitly synthetic bytes written through production evidence/coverage APIs."""
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    conn.execute(
        "INSERT INTO tracked_companies(ticker,name,list_type,sec_validated,filing_regime,instrument_type) VALUES('ACME','Synthetic issuer','portfolio',1,'10-K','equity')"
    )
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
            datetime(2026, 1, 1),
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
    ledger = EvidenceLedger(conn)
    for identity, body, kind in [
        ("inventory", b'{"synthetic_inventory":true}', "sec_submissions"),
        ("native", b"<html>Synthetic annual filing</html>", "sec_filing"),
    ]:
        path = root / f"{identity}.txt"
        path.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        ledger.persist(
            ContentBlob(
                sha256=digest,
                byte_size=len(body),
                media_type="text/plain",
                storage_uri=path.as_uri(),
                recorded_at=STAMP,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id=identity,
                idempotency_key=identity,
                source_kind=kind,
                source_url=f"https://www.sec.gov/Archives/synthetic-{identity}",
                blob_sha256=digest,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=STAMP,
                retrieved_at=STAMP,
                retrieval_config_sha256=CONFIG,
                collector_code_version="synthetic-test.v1",
            )
        )
        EvidenceLinkLedger(conn).persist_location(
            BlobLocationObservation(
                location_observation_id=f"{identity}-location",
                idempotency_key=f"{identity}-location",
                blob_sha256=digest,
                storage_uri=path.as_uri(),
                location_kind="local",
                availability_state="present",
                location_sequence=1,
                verified_at=STAMP,
                verified_byte_size=len(body),
                verified_sha256=digest,
                recorded_at=STAMP,
            )
        )
        if identity == "native":
            ledger.persist(
                DocumentVersion(
                    document_version_id="native-v1",
                    document_key="native-key",
                    version_sequence=1,
                    observation_id="native",
                    blob_sha256=digest,
                    issuer_id="issuer-acme",
                    ticker="ACME",
                    document_type="annual_report",
                    form_type="10-K/A",
                    accession_number=ACCESSION,
                    period_end=datetime(2025, 12, 31),
                    language="en",
                    recorded_at=STAMP,
                )
            )
            ledger.persist(
                ExtractionRun(
                    extraction_run_id="native-run",
                    idempotency_key="native-run",
                    document_version_id="native-v1",
                    input_sha256=digest,
                    extractor_name="synthetic",
                    extractor_config_sha256=CONFIG,
                    extractor_code_version="v1",
                    output_sha256=CONFIG,
                    started_at=STAMP,
                    completed_at=STAMP,
                    outcome="succeeded",
                )
            )
            ledger.persist(
                EvidenceNode(
                    node_id="native-node",
                    evidence_key="native-node",
                    revision=1,
                    extraction_run_id="native-run",
                    node_kind="document",
                    text="Synthetic annual filing",
                    locator=EvidenceLocator(
                        source_ref="https://www.sec.gov/Archives/synthetic-native"
                    ),
                    recorded_at=STAMP,
                )
            )
    coverage = SourceCoverageLedger(conn)
    coverage.persist(
        SourceInventorySnapshot(
            snapshot_id="inventory-v1",
            idempotency_key="inventory-v1",
            inventory_key="issuer-acme:sec",
            revision=1,
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
            source_observation_id="inventory",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=CONFIG,
            collector_code_version="synthetic",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
        )
    )
    coverage.persist(
        ExpectedDocument(
            expected_document_id="expected-native",
            idempotency_key="expected-native",
            snapshot_id="inventory-v1",
            expected_document_key="expected-native-key",
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_filing",
            document_type="annual_report",
            form_type="10-K/A",
            accession_number=ACCESSION,
            source_url="https://www.sec.gov/Archives/synthetic-native",
            primary_document="synthetic-native",
            period_end=datetime(2025, 12, 31),
            filing_at=STAMP,
            expected_at=STAMP,
            expectation_basis="authoritative",
            recorded_at=STAMP,
        )
    )
    coverage.persist(
        CoverageAssessment(
            assessment_id="assessment-v1",
            idempotency_key="assessment-v1",
            expected_document_id="expected-native",
            revision=1,
            coverage_status="captured",
            document_version_id="native-v1",
            reason_code="source_captured",
            reason_details=(("source", "synthetic"),),
            decision_kind="deterministic",
            policy_name="test",
            policy_version="v1",
            policy_config_sha256=CONFIG,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            material_dissent=False,
        )
    )
    component = InventoryComponent(
        component_id="inventory-component",
        idempotency_key="inventory-component",
        snapshot_id="inventory-v1",
        component_key="root",
        component_kind="primary",
        required=True,
        source_url="https://data.sec.gov/submissions/CIK0000000001.json",
        source_observation_id="inventory",
        outcome="succeeded",
        ordinal=0,
        recorded_at=STAMP,
    )
    seal = SourceInventorySealStore(conn)
    seal.persist(component)
    seal.persist(
        InventorySeal(
            snapshot_id="inventory-v1",
            expected_component_count=1,
            component_digest_sha256=component_digest((component,)),
            completion_status="complete",
            sealed_at=STAMP,
        )
    )
    body = json.dumps({"cik": 1, "entityName": "Synthetic issuer", "facts": {}}).encode()
    digest = hashlib.sha256(body).hexdigest()
    conn.execute(
        "INSERT INTO documents(ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url,source_quality_tier) VALUES('ACME','sec_xbrl','sec_companyfacts_snapshot',?,?,?,'ok',?,'https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json','sec_official')",
        (str((root / "blobs" / digest[:2] / f"{digest}.json").resolve()), digest, STAMP, len(body)),
    )
    doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    capture_sec_companyfacts(
        conn,
        SecCompanyFactsCaptureRequest(
            ticker="ACME",
            normalized_cik="0000000001",
            issuer_id="issuer-acme",
            source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
            raw_body=body,
            payload=parse_companyfacts_body(body, expected_cik="0000000001"),
            snapshot_document_id=doc_id,
            blob_root=root / "blobs",
            observed_at=STAMP,
            retrieved_at=STAMP,
        ),
    )
    conn.commit()


def test_observed_coverage_is_separate_from_freshness_and_native_identity(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "coverage.db")
    with sqlite3.connect(db) as conn:
        seed_sec_fixture(conn, tmp_path)
    view = read_sec_coverage_state(db, as_of=STAMP.replace(tzinfo=UTC))
    assert view.state == "available"
    company = view.companies[0]
    assert company.expected_native_count == company.captured_native_count == 1
    assert company.companyfacts_snapshot_count == 1
    assert company.coverage_status == "Covered / freshness unknown"
    assert company.coverage_tone != "ok"
    assert [doc.family for doc in company.documents] == ["10-K/A", "CompanyFacts aggregate"]
    assert company.documents[0].amendment
    assert all(doc.exact_bytes and doc.locator_available for doc in company.documents)


def test_full_schema_empty_roster_is_empty(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "empty.db")
    assert read_sec_coverage_state(db).state == "empty"


def test_lost_byte_location_remains_partial_despite_captured_assessment(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "lost.db")
    with sqlite3.connect(db) as conn:
        seed_sec_fixture(conn, tmp_path)
        digest = hashlib.sha256((tmp_path / "native.txt").read_bytes()).hexdigest()
        EvidenceLinkLedger(conn).persist_location(
            BlobLocationObservation(
                location_observation_id="native-missing",
                idempotency_key="native-missing",
                blob_sha256=digest,
                storage_uri=(tmp_path / "native.txt").as_uri(),
                location_kind="local",
                availability_state="missing",
                location_sequence=2,
                supersedes_location_observation_id="native-location",
                verified_at=STAMP,
                recorded_at=STAMP,
            )
        )
        conn.commit()
    company = read_sec_coverage_state(db, as_of=STAMP.replace(tzinfo=UTC)).companies[0]
    assert company.coverage_status == "Partial"
    assert company.captured_native_count == 0
    assert company.documents[0].state == "provenance_incomplete"
    assert not company.documents[0].exact_bytes


def test_old_complete_population_does_not_become_current_and_future_is_unavailable(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "clock.db")
    with sqlite3.connect(db) as conn:
        seed_sec_fixture(conn, tmp_path)
    old = read_sec_coverage_state(
        db, as_of=(STAMP + timedelta(days=90)).replace(tzinfo=UTC)
    ).companies[0]
    assert old.coverage_status == "Covered / freshness unknown"
    assert old.documents[0].capture_age_seconds == 90 * 86400
    future = read_sec_coverage_state(
        db, as_of=(STAMP - timedelta(days=1)).replace(tzinfo=UTC)
    ).companies[0]
    assert future.coverage_status == "Unavailable"
    assert future.captured_native_count == 0


def test_operations_headline_uses_same_coverage_gap_projection(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from operations.registry import build_operations_registry
    from operations.snapshot import collect_operations_snapshot
    from pipeline.operations_panel import build_operations_panel_view

    db = migrated_db(tmp_path / "attention.db")
    registry = build_operations_registry(Path(__file__).resolve().parents[1])
    with sqlite3.connect(db) as conn:
        seed_sec_fixture(conn, tmp_path)
        complete = collect_operations_snapshot(
            registry, repo_root=tmp_path, conn=conn, observed_at=STAMP.replace(tzinfo=UTC)
        )
        conn.execute("UPDATE tracked_companies SET sec_validated=0 WHERE ticker='ACME'")
        conn.commit()
        missing = collect_operations_snapshot(
            registry, repo_root=tmp_path, conn=conn, observed_at=STAMP.replace(tzinfo=UTC)
        )
    assert complete.data_coverage.unknown_freshness_count == 1
    assert missing.data_coverage.attention_count == complete.data_coverage.attention_count + 1
    assert (
        build_operations_panel_view(registry, missing).attention_count
        == build_operations_panel_view(registry, complete).attention_count + 1
    )
