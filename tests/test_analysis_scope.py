"""Analysis selection cannot hide missing required evidence or change history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from provenance.analysis_scope import (
    AnalysisAccessionSelection,
    AnalysisEvidenceScope,
    AnalysisScopeRequest,
    build_analysis_scope,
    require_analysis_documents,
    resolve_analysis_coverage,
    verify_analysis_scope,
)
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
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

CONFIG_SHA = "c" * 64
INVENTORY_KEY = "issuer-acme:sec-submissions"
STAMP = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

K = datetime(2026, 7, 28, tzinfo=UTC)


def add_expected(
    conn: sqlite3.Connection,
    key: str,
    accession: str,
    *,
    period: datetime | None = datetime(2025, 12, 31, tzinfo=UTC),
    form: str = "10-K",
    document_type: str = "filing",
    filing_at: datetime = datetime(2026, 2, 10, tzinfo=UTC),
    recorded_at: datetime = STAMP,
) -> ExpectedDocument:
    document = ExpectedDocument(
        expected_document_id=key,
        idempotency_key=key,
        snapshot_id="inventory-snapshot",
        expected_document_key=key,
        issuer_id="issuer-acme",
        ticker="ACME",
        source_kind="sec_filing",
        document_type=document_type,
        form_type=form,
        accession_number=accession,
        source_url="https://www.sec.gov/Archives/edgar/data/1/"
        + accession.replace("-", "")
        + "/"
        + key
        + ".htm",
        primary_document=key + ".htm",
        period_end=period,
        filing_at=filing_at,
        expectation_basis="authoritative",
        recorded_at=recorded_at,
    )
    SourceCoverageLedger(conn).persist(document)
    return document


def capture_document(
    conn: sqlite3.Connection,
    expected: ExpectedDocument,
    *,
    recorded: datetime = STAMP,
    coverage_knowledge_at: datetime | None = None,
    coverage_recorded_at: datetime | None = None,
) -> str:
    ledger = EvidenceLedger(conn)
    digest = hashlib.sha256(expected.expected_document_id.encode()).hexdigest()
    observation = "observation:" + expected.expected_document_id
    document_id = "document:" + expected.expected_document_id
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=1,
            media_type="text/html",
            storage_uri="file:///synthetic/" + digest,
            recorded_at=recorded,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=observation,
            idempotency_key=observation,
            source_kind="sec_filing",
            source_url=expected.source_url or "",
            source_published_at=None,
            filing_at=expected.filing_at,
            accepted_at=None,
            blob_sha256=digest,
            observed_at=recorded,
            retrieved_at=recorded,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="test@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=document_id,
            document_key=expected.expected_document_key,
            version_sequence=1,
            observation_id=observation,
            blob_sha256=digest,
            issuer_id=expected.issuer_id,
            ticker="ACME",
            document_type=expected.document_type,
            form_type=expected.form_type or "",
            accession_number=expected.accession_number,
            period_start=expected.period_start,
            period_end=expected.period_end,
            language="en",
            recorded_at=recorded,
        )
    )
    prior = conn.execute(
        "SELECT assessment_id,revision FROM source_coverage_assessments WHERE expected_document_id=? ORDER BY revision DESC LIMIT 1",
        (expected.expected_document_id,),
    ).fetchone()
    coverage_id = "captured:" + expected.expected_document_id
    SourceCoverageLedger(conn).persist(
        CoverageAssessment(
            assessment_id=coverage_id,
            idempotency_key=coverage_id,
            expected_document_id=expected.expected_document_id,
            revision=1 if prior is None else int(prior[1]) + 1,
            coverage_status="captured",
            document_version_id=document_id,
            reason_code="synthetic_capture",
            reason_details=(("source", "synthetic"),),
            decision_kind="deterministic",
            policy_name="test",
            policy_version="1",
            policy_config_sha256=CONFIG_SHA,
            effective_at=recorded,
            knowledge_at=recorded if coverage_knowledge_at is None else coverage_knowledge_at,
            recorded_at=recorded if coverage_recorded_at is None else coverage_recorded_at,
            supersedes_assessment_id=None if prior is None else str(prior[0]),
            material_dissent=False,
        )
    )
    return document_id


def scope_db(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> tuple[sqlite3.Connection, AnalysisEvidenceScope]:
    db_path = tmp_path / "analysis-scope.db"
    migrated_db(db_path)
    conn = sqlite3.connect(db_path)
    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys=ON")
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
            STAMP,
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
        "INSERT INTO source_obligation_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "sec-current:v1",
            "sec-current:v1",
            "sec-current",
            1,
            "issuer-acme",
            None,
            "sec_edgar",
            "continuous_disclosure",
            "required",
            "regulator_inventory",
            STAMP,
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
    blob = hashlib.sha256(b"synthetic inventory").hexdigest()
    ledger.persist(
        ContentBlob(
            sha256=blob,
            byte_size=19,
            media_type="application/json",
            storage_uri="file:///synthetic-inventory.json",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="inventory-observation",
            idempotency_key="inventory-observation",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
            blob_sha256=blob,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="test@1",
        )
    )
    SourceCoverageLedger(conn).persist(
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
            collector_code_version="test@1",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
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
    primary = add_expected(conn, "expected-10k", "0000000001-26-000001")
    capture_document(conn, primary)
    dependency = add_expected(
        conn,
        "generated-report",
        primary.accession_number or "",
        document_type="sec_financial_report",
    )
    capture_document(conn, dependency)
    add_expected(
        conn, "old-missing", "0000000001-25-000001", period=datetime(2024, 12, 31, tzinfo=UTC)
    )
    scope = build_analysis_scope(conn, scope_request())
    return conn, scope


def scope_request(**changes: object) -> AnalysisScopeRequest:
    return AnalysisScopeRequest.model_validate(
        {
            "purpose": "current_valuation",
            "issuer_id": "issuer-acme",
            "inventory_key": INVENTORY_KEY,
            "required_period_ends": (date(2025, 12, 31),),
            "cutoff_at": K,
            "observed_through": K,
        }
        | changes
    )


def test_selected_current_package_qualifies_without_completing_old_archive(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    before = conn.total_changes
    assert require_analysis_documents(conn, scope, K, K) == ("document:expected-10k",)
    coverage = resolve_analysis_coverage(conn, scope, K, K)
    assert (
        next(
            item for item in coverage if item.expected_document_id == "old-missing"
        ).coverage_status
        == "unassessed"
    )
    assert (
        next(item for item in coverage if item.expected_document_id == "generated-report").role
        == "package_dependency"
    )
    assert conn.total_changes == before


def test_missing_dependency_blocks_even_when_primary_is_captured(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    add_expected(
        conn, "required-missing", "0000000001-26-000001", document_type="sec_financial_report"
    )
    scope = build_analysis_scope(conn, scope_request())
    with pytest.raises(ValueError, match="capture is incomplete"):
        require_analysis_documents(conn, scope, K, K)


def test_known_amendment_cannot_be_omitted(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    amendment = add_expected(conn, "amendment", "0000000001-26-000002", form="10-K/A")
    scope = build_analysis_scope(conn, scope_request())
    assert scope.accession_numbers == ("0000000001-26-000001", "0000000001-26-000002")
    with pytest.raises(ValueError, match="capture is incomplete"):
        require_analysis_documents(conn, scope, K, K)
    capture_document(conn, amendment)
    assert require_analysis_documents(conn, scope, K, K) == (
        "document:amendment",
        "document:expected-10k",
    )


@pytest.mark.parametrize(
    "case", ["unknown_amendment", "duplicate_base", "latest_omitted", "wrong_issuer"]
)
def test_invalid_selection_fails_closed(
    tmp_path: Path, migrated_db: Callable[..., Path], case: str
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    request = scope_request()
    if case == "unknown_amendment":
        add_expected(conn, "unknown", "0000000001-26-000002", form="10-K/A", period=None)
    elif case == "duplicate_base":
        add_expected(conn, "duplicate", "0000000001-26-000002")
    elif case == "latest_omitted":
        request = scope_request(required_period_ends=(date(2024, 12, 31),))
    else:
        request = scope_request(issuer_id="other")
    with pytest.raises(ValueError):
        build_analysis_scope(conn, request)


def test_extra_current_report_is_explicit_and_not_inferred_by_period(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    add_expected(conn, "correction", "0000000001-26-000003", form="8-K", period=None)
    scope = build_analysis_scope(
        conn,
        scope_request(
            extra_accessions=(
                AnalysisAccessionSelection(
                    accession_number="0000000001-26-000003", reason="restatement disclosure"
                ),
            )
        ),
    )
    assert "0000000001-26-000003" in scope.accession_numbers
    assert (
        next(item for item in scope.entries if item.expected_document_id == "correction").role
        == "research_document"
    )


def test_self_rehashed_omission_fails_database_reconstruction(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    data = scope.model_dump(mode="json", exclude={"scope_id", "scope_sha256"})
    data["entries"] = [
        entry.model_dump(mode="json")
        for entry in scope.entries
        if entry.expected_document_id != "generated-report"
    ]
    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    tampered = AnalysisEvidenceScope.model_validate(
        data | {"scope_sha256": digest, "scope_id": "analysis-scope:" + digest}
    )
    with pytest.raises(ValueError, match="fabricated"):
        verify_analysis_scope(conn, tampered)


def test_purpose_changes_identity_and_invalid_hash_rejects(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    other = build_analysis_scope(conn, scope_request(purpose="earnings_update"))
    assert scope.scope_id != other.scope_id
    assert scope.entries == other.entries
    with pytest.raises(ValidationError, match="commitment differs"):
        AnalysisEvidenceScope.model_validate(
            scope.model_dump(mode="json") | {"scope_sha256": "0" * 64}
        )


def test_new_inventory_population_invalidates_retained_scope(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    add_expected(conn, "new-disclosure", "0000000001-26-000003", form="8-K", period=None)
    with pytest.raises(ValueError, match="stale"):
        verify_analysis_scope(conn, scope)


def test_future_capture_is_not_visible_at_prior_observation(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    amendment = add_expected(conn, "later-capture", "0000000001-26-000002", form="10-K/A")
    scope = build_analysis_scope(conn, scope_request())
    capture_document(conn, amendment, recorded=datetime(2026, 7, 29, tzinfo=UTC))
    with pytest.raises(ValueError, match="capture is incomplete"):
        require_analysis_documents(conn, scope, K, K)


def test_scope_clocks_are_fixed_for_selected_knowledge(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    with pytest.raises(ValueError, match="clocks differ"):
        resolve_analysis_coverage(conn, scope, STAMP, K)


def test_unknown_old_period_remains_visible_without_blocking_current_selection(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    add_expected(
        conn,
        "unknown-old",
        "0000000001-24-000001",
        period=None,
        filing_at=datetime(2024, 2, 10, tzinfo=UTC),
    )
    scope = build_analysis_scope(conn, scope_request())
    unknown = next(entry for entry in scope.entries if entry.expected_document_id == "unknown-old")
    assert unknown.role == "outside_scope"
    assert unknown.reason == "outside_scope_reporting_period_unknown"
    assert require_analysis_documents(conn, scope, K, K) == ("document:expected-10k",)


def test_conflicting_support_metadata_cannot_join_selected_package(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    add_expected(
        conn,
        "wrong-support",
        "0000000001-26-000001",
        document_type="sec_financial_report",
        period=datetime(2024, 12, 31, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="differs from primary"):
        build_analysis_scope(conn, scope_request())


def test_current_report_amendment_without_original_linkage_is_not_guessed(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    add_expected(conn, "current-amendment", "0000000001-26-000003", form="8-K/A", period=None)
    request = scope_request(
        extra_accessions=(
            AnalysisAccessionSelection(
                accession_number="0000000001-26-000003", reason="current report correction"
            ),
        )
    )
    with pytest.raises(ValueError, match="related-original evidence"):
        build_analysis_scope(conn, request)


@pytest.mark.parametrize("field", ["period_end", "form_type", "accession_number", "source_url"])
def test_positive_coverage_cannot_substitute_other_filing_metadata(
    tmp_path: Path, migrated_db: Callable[..., Path], field: str
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    expected = add_expected(conn, "amendment", "0000000001-26-000002", form="10-K/A")
    scope = build_analysis_scope(conn, scope_request())
    changes: dict[str, object] = {
        "period_end": datetime(2024, 12, 31, tzinfo=UTC),
        "form_type": "10-Q/A",
        "accession_number": "0000000001-26-000099",
        "source_url": "https://www.sec.gov/Archives/edgar/data/1/other.htm",
    }
    capture_document(conn, expected.model_copy(update={field: changes[field]}))
    with pytest.raises(ValueError, match=r"(identity|period)"):
        require_analysis_documents(conn, scope, K, K)


def test_backdated_coverage_cannot_hide_source_retrieved_after_knowledge_cutoff(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    expected = add_expected(conn, "late-amendment", "0000000001-26-000002", form="10-K/A")
    scope = build_analysis_scope(conn, scope_request())
    later = datetime(2026, 7, 29, tzinfo=UTC)
    document_id = capture_document(conn, expected, recorded=later)
    SourceCoverageLedger(conn).persist(
        CoverageAssessment(
            assessment_id="backdated",
            idempotency_key="backdated",
            expected_document_id=expected.expected_document_id,
            revision=2,
            coverage_status="captured",
            document_version_id=document_id,
            reason_code="synthetic_backdate",
            reason_details=(("source", "test"),),
            decision_kind="deterministic",
            policy_name="test",
            policy_version="1",
            policy_config_sha256=CONFIG_SHA,
            effective_at=K,
            knowledge_at=K,
            recorded_at=later,
            supersedes_assessment_id="captured:" + expected.expected_document_id,
            material_dissent=False,
        )
    )
    with pytest.raises(ValueError, match="exceeds knowledge cutoff"):
        require_analysis_documents(conn, scope, K, later)


def test_unsealed_inventory_fails_before_selection(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    SourceCoverageLedger(conn).persist(
        SourceInventorySnapshot(
            snapshot_id="inventory-v2",
            idempotency_key="inventory-v2",
            inventory_key=INVENTORY_KEY,
            revision=2,
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
            source_observation_id="inventory-observation",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="test@1",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
            supersedes_snapshot_id=scope.inventory.snapshot_id,
        )
    )
    with pytest.raises(ValueError, match="current complete authoritative"):
        verify_analysis_scope(conn, scope)
    with pytest.raises(ValueError, match="current complete authoritative"):
        verify_analysis_scope(conn, scope, require_current_inventory=False)


def test_retained_scope_reconstructs_original_inventory_after_new_revision(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    later = datetime(2026, 7, 29, tzinfo=UTC)
    SourceCoverageLedger(conn).persist(
        SourceInventorySnapshot(
            snapshot_id="inventory-later",
            idempotency_key="inventory-later",
            inventory_key=INVENTORY_KEY,
            revision=2,
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
            source_observation_id="inventory-observation",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="test@1",
            started_at=later,
            completed_at=later,
            recorded_at=later,
            supersedes_snapshot_id=scope.inventory.snapshot_id,
        )
    )
    with pytest.raises(ValueError, match="current complete authoritative"):
        verify_analysis_scope(conn, scope)
    verify_analysis_scope(conn, scope, require_current_inventory=False)
    assert require_analysis_documents(conn, scope, K, K, require_current_inventory=False) == (
        "document:expected-10k",
    )
    assert build_analysis_scope(conn, scope.request, require_current_inventory=False) == scope
    with pytest.raises(ValueError, match="current complete authoritative"):
        build_analysis_scope(
            conn, scope_request(observed_through=later), require_current_inventory=False
        )


def test_unknown_old_amendment_does_not_block_future_declared_base(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    add_expected(
        conn,
        "unknown-old-amendment",
        "0000000001-24-000002",
        form="10-K/A",
        period=None,
        filing_at=datetime(2024, 2, 10, tzinfo=UTC),
    )
    scope = build_analysis_scope(conn, scope_request())
    entry = next(
        item for item in scope.entries if item.expected_document_id == "unknown-old-amendment"
    )
    assert entry.role == "outside_scope"
    assert entry.reason == "outside_scope_reporting_period_unknown"
    assert require_analysis_documents(conn, scope, K, K) == ("document:expected-10k",)
    with pytest.raises(ValueError, match="declared required reporting period"):
        build_analysis_scope(
            conn,
            scope_request(
                extra_accessions=(
                    AnalysisAccessionSelection(
                        accession_number="0000000001-24-000002",
                        reason="explicit historical correction",
                    ),
                )
            ),
        )


@pytest.mark.parametrize(
    "filing_at", [datetime(2026, 2, 10, tzinfo=UTC), datetime(2026, 3, 10, tzinfo=UTC)]
)
def test_unknown_potentially_related_amendment_still_blocks(
    tmp_path: Path, migrated_db: Callable[..., Path], filing_at: datetime
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    add_expected(
        conn,
        "ambiguous-amendment",
        "0000000001-26-000002",
        form="10-K/A",
        period=None,
        filing_at=filing_at,
    )
    with pytest.raises(ValueError, match="unknown reporting period"):
        build_analysis_scope(conn, scope_request())


@pytest.mark.parametrize("late_clock", ["knowledge", "observation"])
def test_fractional_coverage_clock_cannot_enter_earlier_cutoff(
    tmp_path: Path, migrated_db: Callable[..., Path], late_clock: str
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    early = K.replace(microsecond=100_000)
    late = K.replace(microsecond=900_000)
    amendment = add_expected(conn, "fractional-amendment", "0000000001-26-000002", form="10-K/A")
    scope = build_analysis_scope(conn, scope_request(cutoff_at=early, observed_through=early))
    capture_document(
        conn,
        amendment,
        coverage_knowledge_at=late if late_clock == "knowledge" else early,
        coverage_recorded_at=late,
    )
    resolved = resolve_analysis_coverage(conn, scope, early, early)
    assert (
        next(
            item for item in resolved if item.expected_document_id == amendment.expected_document_id
        ).coverage_status
        == "unassessed"
    )
    with pytest.raises(ValueError, match="capture is incomplete"):
        require_analysis_documents(conn, scope, early, early)


def test_fractional_coverage_exact_cutoff_remains_usable(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    exact = K.replace(microsecond=100_000)
    amendment = add_expected(conn, "exact-amendment", "0000000001-26-000002", form="10-K/A")
    scope = build_analysis_scope(conn, scope_request(cutoff_at=exact, observed_through=exact))
    capture_document(conn, amendment, coverage_knowledge_at=exact, coverage_recorded_at=exact)
    assert "document:exact-amendment" in require_analysis_documents(conn, scope, exact, exact)


@pytest.mark.parametrize("newer_fraction", [50_000, 900_000])
def test_fractional_inventory_visibility_preserves_retained_selection(
    tmp_path: Path, migrated_db: Callable[..., Path], newer_fraction: int
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    frozen = K.replace(microsecond=100_000)
    scope = build_analysis_scope(conn, scope_request(cutoff_at=frozen, observed_through=frozen))
    newer_at = K.replace(microsecond=newer_fraction)
    SourceCoverageLedger(conn).persist(
        SourceInventorySnapshot(
            snapshot_id="inventory-fractional",
            idempotency_key="inventory-fractional",
            inventory_key=INVENTORY_KEY,
            revision=2,
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
            source_observation_id="inventory-observation",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="test@1",
            started_at=newer_at,
            completed_at=newer_at,
            recorded_at=newer_at,
            supersedes_snapshot_id=scope.inventory.snapshot_id,
        )
    )
    with pytest.raises(ValueError, match="current complete authoritative"):
        verify_analysis_scope(conn, scope)
    if newer_fraction < 100_000:
        with pytest.raises(ValueError, match="current complete authoritative"):
            verify_analysis_scope(conn, scope, require_current_inventory=False)
    else:
        verify_analysis_scope(conn, scope, require_current_inventory=False)
        assert require_analysis_documents(
            conn, scope, frozen, frozen, require_current_inventory=False
        ) == ("document:expected-10k",)


@pytest.mark.parametrize("metadata_fraction", [50_000, 900_000])
def test_fractional_expected_metadata_visibility_uses_original_observation_time(
    tmp_path: Path, migrated_db: Callable[..., Path], metadata_fraction: int
) -> None:
    conn, _scope = scope_db(tmp_path, migrated_db)
    frozen = K.replace(microsecond=100_000)
    scope = build_analysis_scope(conn, scope_request(cutoff_at=frozen, observed_through=frozen))
    add_expected(
        conn,
        "fractional-disclosure",
        "0000000001-26-000003",
        form="8-K",
        period=None,
        recorded_at=K.replace(microsecond=metadata_fraction),
    )
    if metadata_fraction < 100_000:
        with pytest.raises(ValueError, match="stale, changed or fabricated"):
            verify_analysis_scope(conn, scope, require_current_inventory=False)
    else:
        verify_analysis_scope(conn, scope, require_current_inventory=False)
        with pytest.raises(ValueError, match="observation time"):
            verify_analysis_scope(conn, scope)


def test_fractional_component_after_selection_cannot_satisfy_inventory_seal(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    frozen = K.replace(microsecond=100_000)
    late = K.replace(microsecond=900_000)
    snapshot_id = "inventory-component-time"
    SourceCoverageLedger(conn).persist(
        SourceInventorySnapshot(
            snapshot_id=snapshot_id,
            idempotency_key=snapshot_id,
            inventory_key=INVENTORY_KEY,
            revision=2,
            issuer_id="issuer-acme",
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
            source_observation_id="inventory-observation",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="test@1",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
            supersedes_snapshot_id=scope.inventory.snapshot_id,
        )
    )
    component = InventoryComponent(
        component_id="late-component",
        idempotency_key="late-component",
        snapshot_id=snapshot_id,
        component_key="root",
        component_kind="primary",
        source_url="https://data.sec.gov/submissions/CIK0000000001.json",
        source_observation_id="inventory-observation",
        outcome="succeeded",
        required=True,
        ordinal=0,
        recorded_at=late,
    )
    store = SourceInventorySealStore(conn)
    store.persist(component)
    store.persist(
        InventorySeal(
            snapshot_id=snapshot_id,
            expected_component_count=1,
            component_digest_sha256=component_digest((component,)),
            completion_status="complete",
            sealed_at=frozen,
        )
    )
    with pytest.raises(ValueError, match="component population"):
        build_analysis_scope(
            conn,
            scope_request(cutoff_at=frozen, observed_through=frozen),
            require_current_inventory=False,
        )


@pytest.mark.parametrize("field", ["cutoff_at", "observed_through"])
@pytest.mark.parametrize("as_json", [False, True])
def test_analysis_clocks_require_explicit_time_zone(field: str, as_json: bool) -> None:
    values = scope_request().model_dump(mode="json" if as_json else "python")
    values[field] = "2026-07-28T00:00:00" if as_json else K.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="explicit time zone"):
        if as_json:
            AnalysisScopeRequest.model_validate_json(json.dumps(values))
        else:
            AnalysisScopeRequest.model_validate(values)
