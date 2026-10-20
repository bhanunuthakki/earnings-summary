"""Exact publisher PDFs join one report scope without expanding archive claims."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from provenance.analysis_scope import (
    AnalysisScopeRequest,
    build_analysis_scope,
    require_analysis_documents,
    verify_analysis_scope,
)
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.issuer_registry import AuthoritySurfaceRevision, IssuerRegistry
from provenance.reporting_entity_registry import ReportingEntityRegistry, SourceObligationRevision
from provenance.research_snapshot import (
    DocumentProcessingPolicy,
    DocumentProcessingScope,
    derive_obligations,
)
from provenance.source_coverage import (
    CoverageAssessment,
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from tests.test_analysis_scope import STAMP, K, scope_request
from tests.test_analysis_scope_processing import processing_db
from tests.test_historical_analysis_scope import (
    ISSUER as HISTORICAL_ISSUER,
)
from tests.test_historical_analysis_scope import (
    K as HISTORICAL_K,
)
from tests.test_historical_analysis_scope import (
    historical as historical,
)

ISSUER = "issuer-acme"
HOME = "https://investor.acme.test/"
PDF_URL = HOME + "open-file?file=exact-letter.pdf"
PROFILE = "complete_report_evidence/v1"
CONFIG = "c" * 64


def _source(
    conn: sqlite3.Connection,
    root: Path,
    identity: str,
    url: str,
    raw: bytes,
    kind: str,
    *,
    future: bool = False,
    at: datetime = STAMP,
) -> None:
    at = K + timedelta(seconds=1) if future else at
    path = root / identity
    path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=sha,
            byte_size=len(raw),
            media_type=(
                "application/pdf"
                if raw.startswith(b"%PDF-")
                else "application/json"
                if kind == "ir_json"
                else "text/html"
            ),
            storage_uri=path.as_uri(),
            recorded_at=at,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=identity,
            idempotency_key=identity,
            source_kind=kind,
            source_url=url,
            blob_sha256=sha,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=at,
            retrieved_at=at,
            retrieval_config_sha256=CONFIG,
            collector_code_version="retained-test-source@1",
        )
    )


@pytest.fixture
def pdf_scope(
    tmp_path: Path, migrated_db: Callable[..., Path], request: pytest.FixtureRequest
) -> Iterator[tuple[sqlite3.Connection, dict[str, object]]]:
    failure = str(getattr(request, "param", ""))
    conn, _ = processing_db(tmp_path, migrated_db)
    conn.execute(
        "INSERT INTO issuer_entities VALUES(?,?,?,?)",
        ("issuer-other", "issuer-other", "operating_company", STAMP),
    )
    authority_raw = (
        b"<html><p>Unrelated company website.</p></html>"
        if failure == "authority_locator"
        else b"<html><p>Our investor relations website is investor.acme.test.</p></html>"
    )
    _source(
        conn,
        tmp_path,
        "authority-source",
        "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/annual.htm",
        authority_raw,
        "sec_filing",
    )
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="authority-baseline",
            document_key="authority-baseline",
            version_sequence=1,
            observation_id="authority-source",
            blob_sha256=hashlib.sha256(authority_raw).hexdigest(),
            issuer_id=ISSUER,
            ticker="ACME",
            document_type="filing",
            form_type="10-K",
            language="en",
            recorded_at=STAMP,
        )
    )
    authority_at = K + timedelta(seconds=1) if failure == "authority_clock" else STAMP
    IssuerRegistry(conn).persist(
        AuthoritySurfaceRevision(
            surface_revision_id="ir-home",
            idempotency_key="ir-home",
            issuer_id="issuer-other" if failure == "authority_issuer" else ISSUER,
            surface_key="ir-home",
            revision=1,
            surface_kind="ir_home",
            source_url=HOME,
            status="candidate" if failure == "authority_status" else "verified",
            authority_level="publisher",
            source_observation_id="authority-source",
            verification_method="issuer_sec_filing_website_locator",
            effective_at=authority_at,
            knowledge_at=authority_at,
            recorded_at=authority_at,
        )
    )
    registry = ReportingEntityRegistry(conn)
    registry.persist(
        SourceObligationRevision(
            obligation_revision_id="issuer-pdf-duty",
            idempotency_key="issuer-pdf-duty",
            obligation_key="issuer-pdf-duty",
            revision=1,
            issuer_id=ISSUER,
            reporting_entity_id="reporting-acme",
            authority_kind="issuer_publisher",
            document_family="issuer_earnings_materials",
            obligation_state="optional",
            completeness_rule="manual_exception",
            active_from=STAMP,
            active_to=None,
            decision_kind="manual",
            reason_code="bounded_report_source_selection",
            reason_details=(("scope", "exact selected issuer PDF only"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    _source(
        conn,
        tmp_path,
        "partial-publisher-source",
        HOME,
        b'<html><a href="open-file?file=exact-letter.pdf">Annual letter</a></html>',
        "ir_crawl",
    )
    coverage = SourceCoverageLedger(conn)
    coverage.persist(
        SourceInventorySnapshot(
            snapshot_id="partial-ir",
            idempotency_key="partial-ir",
            inventory_key="issuer-acme:ir",
            revision=1,
            issuer_id=ISSUER,
            ticker="ACME",
            source_kind="ir_crawl",
            source_url=HOME,
            source_observation_id="partial-publisher-source",
            outcome="partial",
            authoritative=False,
            retrieval_config_sha256=CONFIG,
            collector_code_version="retained-publisher-candidates@1",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
        )
    )
    expected = ExpectedDocument(
        expected_document_id="issuer-pdf",
        idempotency_key="issuer-pdf",
        snapshot_id="partial-ir",
        expected_document_key="issuer-pdf",
        issuer_id=ISSUER,
        ticker="ACME",
        source_kind="ir_document",
        document_type="earnings_material",
        form_type="IR",
        source_url=PDF_URL,
        period_end=None
        if failure == "period"
        else datetime.combine(scope_request().required_period_ends[0], datetime.min.time(), UTC),
        expectation_basis="publisher_candidate",
        recorded_at=STAMP,
        source_obligation_revision_id="issuer-pdf-duty",
    )
    coverage.persist(expected)
    pdf = b"%PDF-1.4\nExact retained issuer letter.\n%%EOF\n"
    _source(
        conn,
        tmp_path,
        "pdf-source",
        PDF_URL + "&served=other" if failure == "url" else PDF_URL,
        pdf,
        "ir_document",
        future=failure == "pdf_clock",
    )
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="issuer-pdf-version",
            document_key="issuer-pdf",
            version_sequence=1,
            observation_id="pdf-source",
            blob_sha256=hashlib.sha256(pdf).hexdigest(),
            issuer_id="issuer-other" if failure == "document_issuer" else ISSUER,
            ticker="ACME",
            document_type="earnings_material",
            form_type="IR",
            language="en",
            period_end=expected.period_end,
            recorded_at=STAMP,
        )
    )
    coverage.persist(
        CoverageAssessment(
            assessment_id="pdf-capture",
            idempotency_key="pdf-capture",
            expected_document_id="issuer-pdf",
            revision=1,
            coverage_status="available" if failure == "coverage" else "captured",
            document_version_id=None if failure == "coverage" else "issuer-pdf-version",
            reason_code="retained_pdf_capture",
            reason_details=(("scope", "exact file; publisher archive remains partial"),),
            decision_kind="imported",
            policy_name="retained_pdf",
            policy_version="1",
            policy_config_sha256=CONFIG,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            material_dissent=False,
        )
    )
    if failure == "hash":
        (tmp_path / "pdf-source").write_bytes(pdf + b"changed")
    if failure == "authority_hash":
        (tmp_path / "authority-source").write_bytes(authority_raw + b"changed")
    conn.commit()
    payload = scope_request(readiness_profile=PROFILE).model_dump(mode="json")
    payload["issuer_documents"] = [
        {
            "expected_document_id": "issuer-pdf",
            "document_version_id": "issuer-pdf-version",
            "authority_surface_revision_id": "missing"
            if failure == "authority_missing"
            else "ir-home",
        }
    ]
    try:
        yield conn, payload
    finally:
        conn.close()


def test_exact_issuer_pdf_joins_v5_and_all_processing_readers_keep_the_same_set(
    pdf_scope: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = pdf_scope
    scope = build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))
    assert scope.schema_version == "analysis-evidence-scope/v5"
    assert require_analysis_documents(conn, scope, K, K) == (
        "document:expected-10k",
        "issuer-pdf-version",
    )
    pdf_entry = next(e for e in scope.entries if e.expected_document_id == "issuer-pdf")
    assert (
        pdf_entry.role == "research_document"
        and pdf_entry.expected_document.expectation_basis == "publisher_candidate"
    )
    assert conn.execute(
        "SELECT outcome,authoritative FROM source_inventory_snapshots WHERE snapshot_id='partial-ir'"
    ).fetchone() == ("partial", 0)
    verify_analysis_scope(conn, scope)
    witness = scope.model_dump(mode="json")["issuer_document_witnesses"][0]
    assert witness["authority_surface_revision_id"] == "ir-home"
    assert witness["document_version_id"] == "issuer-pdf-version"
    selected = DocumentProcessingScope(
        document_version_ids=("document:expected-10k", "issuer-pdf-version"), analysis_scope=scope
    )
    duties = derive_obligations(
        conn,
        selected,
        K,
        DocumentProcessingPolicy(policy_name="issuer-pdf-proof", policy_version="1"),
        recorded_at=K,
    )
    assert {
        d.processing_lane
        for d in duties
        if d.document_version_id == "issuer-pdf-version" and d.applicability == "applicable"
    } == {"pdf_text", "pdf_ocr", "pdf_table"}
    with pytest.raises(ValueError, match="document set differs"):
        derive_obligations(
            conn,
            DocumentProcessingScope(
                document_version_ids=("document:expected-10k",), analysis_scope=scope
            ),
            K,
            DocumentProcessingPolicy(policy_name="incomplete-pdf-proof", policy_version="1"),
            recorded_at=K,
        )


@pytest.mark.parametrize(
    "pdf_scope",
    [
        "authority_missing",
        "authority_issuer",
        "authority_status",
        "authority_clock",
        "authority_hash",
        "authority_locator",
        "hash",
        "url",
        "coverage",
        "pdf_clock",
        "period",
    ],
    indirect=True,
)
def test_issuer_pdf_selection_rejects_missing_or_misattributed_native_proof(
    pdf_scope: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = pdf_scope
    with pytest.raises(ValueError, match="issuer document"):
        build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))


def test_issuer_pdf_selection_requires_profile_and_preserves_empty_legacy_bytes(
    pdf_scope: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = pdf_scope
    payload.pop("readiness_profile")
    with pytest.raises(ValueError, match="issuer document"):
        AnalysisScopeRequest.model_validate(payload)
    for profile in (None, PROFILE):
        old = scope_request(readiness_profile=profile)
        empty = AnalysisScopeRequest.model_validate(old.model_dump() | {"issuer_documents": []})
        assert empty.model_dump_json() == old.model_dump_json()
        assert (
            build_analysis_scope(conn, old).model_dump_json()
            == build_analysis_scope(conn, empty).model_dump_json()
        )


def test_native_capture_owner_rejects_a_pdf_document_from_another_issuer(
    pdf_scope: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, _ = pdf_scope
    row = conn.execute(
        "SELECT "
        + ",".join(DocumentVersion.model_fields)
        + " FROM evidence_document_versions WHERE document_version_id='issuer-pdf-version'"
    ).fetchone()
    original = DocumentVersion.model_validate(
        dict(zip(DocumentVersion.model_fields, row, strict=True))
    )
    EvidenceLedger(conn).persist(
        original.model_copy(
            update={
                "document_version_id": "foreign-pdf-version",
                "document_key": "foreign-pdf-key",
                "issuer_id": "issuer-other",
            }
        )
    )
    with pytest.raises(ValueError, match="issuer does not match expected document"):
        SourceCoverageLedger(conn).persist(
            CoverageAssessment(
                assessment_id="foreign-pdf-capture",
                idempotency_key="foreign-pdf-capture",
                expected_document_id="issuer-pdf",
                revision=2,
                supersedes_assessment_id="pdf-capture",
                coverage_status="captured",
                document_version_id="foreign-pdf-version",
                reason_code="incorrect_source_subject",
                reason_details=(("scope", "synthetic negative"),),
                decision_kind="imported",
                policy_name="negative",
                policy_version="1",
                policy_config_sha256=CONFIG,
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
                material_dissent=False,
            )
        )


def test_two_exact_pdfs_share_the_scope_and_total_research_population_is_bounded(
    pdf_scope: tuple[sqlite3.Connection, dict[str, object]],
    tmp_path: Path,
) -> None:
    conn, payload = pdf_scope
    request = AnalysisScopeRequest.model_validate(payload)
    expected = ExpectedDocument(
        expected_document_id="issuer-pdf2",
        idempotency_key="issuer-pdf2",
        snapshot_id="partial-ir",
        expected_document_key="issuer-pdf2",
        issuer_id=ISSUER,
        ticker="ACME",
        source_kind="ir_document",
        document_type="earnings_material",
        form_type="IR",
        source_url=HOME + "release.pdf",
        period_end=datetime.combine(request.required_period_ends[0], datetime.min.time(), UTC),
        expectation_basis="publisher_candidate",
        recorded_at=STAMP,
        source_obligation_revision_id="issuer-pdf-duty",
    )
    ledger = SourceCoverageLedger(conn)
    ledger.persist(expected)
    raw = b"%PDF-1.4\nSecond exact retained issuer PDF.\n%%EOF\n"
    _source(conn, tmp_path, "second-pdf-source", expected.source_url or "", raw, "ir_document")
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="second-pdf-version",
            document_key="issuer-pdf2",
            version_sequence=1,
            observation_id="second-pdf-source",
            blob_sha256=hashlib.sha256(raw).hexdigest(),
            issuer_id=ISSUER,
            ticker="ACME",
            document_type="earnings_material",
            form_type="IR",
            period_end=expected.period_end,
            language="en",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        CoverageAssessment(
            assessment_id="second-pdf-capture",
            idempotency_key="second-pdf-capture",
            expected_document_id="issuer-pdf2",
            revision=1,
            coverage_status="captured",
            document_version_id="second-pdf-version",
            reason_code="retained_pdf_capture",
            reason_details=(("scope", "second exact issuer file"),),
            decision_kind="imported",
            policy_name="retained_pdf",
            policy_version="1",
            policy_config_sha256=CONFIG,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            material_dissent=False,
        )
    )
    second = request.issuer_documents[0].model_copy(
        update={"expected_document_id": "issuer-pdf2", "document_version_id": "second-pdf-version"}
    )
    request = request.model_copy(update={"issuer_documents": (*request.issuer_documents, second)})
    scope = build_analysis_scope(conn, request)
    assert len(scope.issuer_document_witnesses) == 2
    assert require_analysis_documents(conn, scope, K, K) == (
        "document:expected-10k",
        "issuer-pdf-version",
        "second-pdf-version",
    )
    primary = next(
        e.expected_document
        for e in scope.entries
        if e.expected_document.document_type == "filing" and e.role == "research_document"
    )
    for index in range(26):
        identity = f"selected-original-{index:02d}"
        ledger.persist(
            primary.model_copy(
                update={
                    "expected_document_id": identity,
                    "idempotency_key": identity,
                    "expected_document_key": identity,
                    "document_type": "sec_exhibit",
                    "source_url": (primary.source_url or "").rsplit("/", 1)[0]
                    + "/"
                    + identity
                    + ".htm",
                    "primary_document": identity + ".htm",
                    "source_obligation_revision_id": "sec-periodic:v2",
                }
            )
        )
        if index == 24:
            bounded = build_analysis_scope(conn, request)
            assert sum(e.role == "research_document" for e in bounded.entries) == 28
            with pytest.raises(ValueError, match="capture is incomplete"):
                require_analysis_documents(conn, bounded, K, K)
    with pytest.raises(ValueError, match="exceeds 28"):
        build_analysis_scope(conn, request)


NU_COMPANY = "59a081d2-0d63-4bb5-b786-4c07ae26bc74"
NU_METADATA = (
    "https://api.mziq.com/mzfilemanager/company/" + NU_COMPANY + "/filter/categories/year/meta"
)
NU_PAGE = "https://www.investidores.nu/financials/results-center/"
NU_CALL = "https://api.mziq.com/mzfilemanager/v2/d/" + NU_COMPANY + "/synthetic-file?origin=2"


def _historical_call(
    conn: sqlite3.Connection,
    root: Path,
    payload: dict[str, object],
    failure: str = "",
    *,
    pre: bool = False,
) -> AnalysisScopeRequest:
    pdf = b"%PDF-1.4\nSynthetic exact historical call.\n%%EOF\n"
    period = datetime(2026, 3, 31, tzinfo=UTC) if pre else datetime(2026, 6, 30, tzinfo=UTC)
    company = "different-company" if failure == "page_company" else NU_COMPANY
    page = (
        "<html><script>const fmId='"
        + company
        + "'; const fmBase='https://api.mziq.com/mzfilemanager';</script></html>"
    ).encode()
    _source(conn, root, "call-publisher-page", NU_PAGE, page, "ir_crawl", at=HISTORICAL_K)
    record: dict[str, object] = {
        "id": "synthetic-record",
        "company_id": NU_COMPANY,
        "download_link_id": "synthetic-file",
        "file_url": NU_CALL + "&other=1" if failure == "record_url" else NU_CALL,
        "is_published": True,
        "language_code": "en_US",
        "file_size": str(len(pdf)),
        "file_published_date": "2026-08-18T00:00:00.000Z"
        if failure == "late"
        else "2026-05-15T00:00:00.000Z"
        if pre
        else "2026-08-14T00:00:00.000Z",
        "file_date": "2026-03-31T00:00:00.000Z"
        if failure == "period"
        else "2026-03-31T00:00:00.000Z"
        if pre
        else "2026-06-30T00:00:00.000Z",
    }
    if failure == "publication_missing":
        record.pop("file_published_date")
    metadata = json.dumps({"success": True, "data": {"document_metas": [record]}}).encode()
    _source(
        conn, root, "call-publisher-metadata", NU_METADATA, metadata, "ir_json", at=HISTORICAL_K
    )
    _source(conn, root, "historical-call-source", NU_CALL, pdf, "earnings_call", at=HISTORICAL_K)
    IssuerRegistry(conn).persist(
        AuthoritySurfaceRevision(
            surface_revision_id="nu-ir-archive",
            idempotency_key="nu-ir-archive",
            issuer_id=HISTORICAL_ISSUER,
            surface_key="nu-ir-archive",
            revision=1,
            surface_kind="ir_archive",
            source_url=NU_METADATA,
            status="verified",
            authority_level="publisher",
            source_observation_id="call-publisher-page",
            verification_method="reviewed_issuer_results_center_api_binding",
            effective_at=HISTORICAL_K,
            knowledge_at=HISTORICAL_K,
            recorded_at=HISTORICAL_K,
        )
    )
    ReportingEntityRegistry(conn).persist(
        SourceObligationRevision(
            obligation_revision_id="nu-call-duty",
            idempotency_key="nu-call-duty",
            obligation_key="nu-call-duty",
            revision=1,
            issuer_id=HISTORICAL_ISSUER,
            reporting_entity_id=None,
            authority_kind="issuer_publisher",
            document_family="issuer_earnings_materials",
            obligation_state="optional",
            completeness_rule="manual_exception",
            active_from=HISTORICAL_K,
            active_to=None,
            decision_kind="manual",
            reason_code="bounded_historical_call",
            reason_details=(("scope", "exact retained call; archive remains partial"),),
            effective_at=HISTORICAL_K,
            knowledge_at=HISTORICAL_K,
            recorded_at=HISTORICAL_K,
        )
    )
    coverage = SourceCoverageLedger(conn)
    coverage.persist(
        SourceInventorySnapshot(
            snapshot_id="nu-partial-ir",
            idempotency_key="nu-partial-ir",
            inventory_key="nu-partial-ir",
            revision=1,
            issuer_id=HISTORICAL_ISSUER,
            ticker="SYNTH",
            source_kind="ir_crawl",
            source_url=NU_METADATA,
            source_observation_id="call-publisher-metadata",
            outcome="partial",
            authoritative=False,
            retrieval_config_sha256=CONFIG,
            collector_code_version="retained-test-publisher@1",
            started_at=HISTORICAL_K,
            completed_at=HISTORICAL_K,
            recorded_at=HISTORICAL_K,
        )
    )
    coverage.persist(
        ExpectedDocument(
            expected_document_id="nu-call",
            idempotency_key="nu-call",
            snapshot_id="nu-partial-ir",
            expected_document_key="nu-call",
            issuer_id=HISTORICAL_ISSUER,
            ticker="SYNTH",
            source_kind="earnings_call",
            document_type="earnings_transcript",
            form_type="IR",
            source_url=NU_CALL,
            period_end=period,
            expectation_basis="publisher_candidate",
            source_obligation_revision_id="nu-call-duty",
            recorded_at=HISTORICAL_K,
        )
    )
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="nu-call-version",
            document_key="nu-call",
            version_sequence=1,
            observation_id="historical-call-source",
            blob_sha256=hashlib.sha256(pdf).hexdigest(),
            issuer_id=HISTORICAL_ISSUER,
            ticker="SYNTH",
            document_type="earnings_transcript",
            form_type="IR",
            period_end=period,
            language="en",
            recorded_at=HISTORICAL_K,
        )
    )
    coverage.persist(
        CoverageAssessment(
            assessment_id="nu-call-capture",
            idempotency_key="nu-call-capture",
            expected_document_id="nu-call",
            revision=1,
            coverage_status="captured",
            document_version_id="nu-call-version",
            reason_code="native_retained_capture",
            reason_details=(("scope", "exact call; no archive completeness claim"),),
            decision_kind="imported",
            policy_name="retained_pdf",
            policy_version="1",
            policy_config_sha256=CONFIG,
            effective_at=HISTORICAL_K,
            knowledge_at=HISTORICAL_K,
            recorded_at=HISTORICAL_K,
            material_dissent=False,
        )
    )
    conn.commit()
    historical_request = dict(cast(dict[str, object], payload["historical_reconstruction"]))
    historical_request.update(
        public_cutoff_at=datetime(2026, 8, 12, 23, 59, 59, tzinfo=UTC)
        if pre
        else datetime(2026, 8, 18, 4, tzinfo=UTC),
        public_witness_policy="historical-public-witness/v1",
        public_witness_owner_sha256=CONFIG,
        public_window=None if pre else "nu-q2-2026-post/v1",
    )
    return AnalysisScopeRequest.model_validate(
        payload
        | {
            "readiness_profile": PROFILE,
            "historical_reconstruction": historical_request,
            "issuer_documents": [
                {
                    "expected_document_id": "nu-call",
                    "document_version_id": "nu-call-version",
                    "authority_surface_revision_id": "nu-ir-archive",
                    "issuer_day": {
                        "document_version_id": "nu-call-version",
                        "metadata": {
                            "source_observation_id": "call-publisher-metadata",
                            "expected_blob_sha256": hashlib.sha256(metadata).hexdigest(),
                        },
                        "record_id": "absent"
                        if failure == "record_missing"
                        else "synthetic-record",
                    },
                }
            ],
        }
    )


def test_historical_call_joins_same_v6_plane_with_public_day_and_physical_clocks(
    historical: tuple[sqlite3.Connection, dict[str, object]], tmp_path: Path
) -> None:
    conn, payload = historical
    request = _historical_call(conn, tmp_path, payload)
    before = conn.total_changes
    scope = build_analysis_scope(conn, request)
    assert scope.schema_version == "analysis-evidence-scope/v6"
    assert require_analysis_documents(conn, scope, HISTORICAL_K, HISTORICAL_K) == (
        "nu-call-version",
        "original-financial",
    )
    call = next(w for w in scope.public_witnesses if w.document_version_id == "nu-call-version")
    assert call.witness_kind == "issuer_declared_day"
    assert call.raw_value == "2026-08-14T00:00:00.000Z"
    assert call.upper_at == datetime(2026, 8, 15, 3, 59, 59, tzinfo=UTC)
    assert scope.request.cutoff_at == HISTORICAL_K
    assert scope.issuer_document_witnesses[0].inventory_authoritative is False
    verify_analysis_scope(conn, scope)
    assert conn.total_changes == before


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("late", "public bound"),
        ("page_company", "page/API"),
        ("record_missing", "record is absent"),
        ("record_url", "exact file URL"),
        ("period", "fiscal period"),
        ("publication_missing", "date-precision"),
    ],
)
def test_historical_pdf_requires_exact_authority_record_period_and_publication(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    tmp_path: Path,
    failure: str,
    reason: str,
) -> None:
    conn, payload = historical
    request = _historical_call(conn, tmp_path, payload, failure)
    with pytest.raises(ValueError, match=reason):
        build_analysis_scope(conn, request)


def test_historical_pdf_cannot_omit_public_day_policy_or_exceed_combined_cap(
    historical: tuple[sqlite3.Connection, dict[str, object]], tmp_path: Path
) -> None:
    conn, payload = historical
    request = _historical_call(conn, tmp_path, payload)
    value = request.model_dump(mode="json")
    selections = cast(list[dict[str, object]], value["issuer_documents"])
    selections[0].pop("issuer_day")
    with pytest.raises(ValueError, match="requires public witnesses"):
        AnalysisScopeRequest.model_validate(value)
    value = request.model_dump(mode="json")
    reconstruction = cast(dict[str, object], value["historical_reconstruction"])
    reconstruction.pop("public_witness_policy")
    reconstruction.pop("public_witness_owner_sha256")
    reconstruction.pop("public_window")
    with pytest.raises(ValueError, match="requires public witnesses"):
        AnalysisScopeRequest.model_validate(value)
    value = request.model_dump(mode="json")
    reconstruction = cast(dict[str, object], value["historical_reconstruction"])
    reconstruction["documents"] = [
        {"expected_document_id": f"doc-{i:02}", "document_version_id": f"version-{i:02}"}
        for i in range(12)
    ]
    with pytest.raises(ValueError, match="at most 12 documents"):
        AnalysisScopeRequest.model_validate(value)
