"""Historical public selection retains physical capture clocks and exact versions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from provenance.analysis_scope import (
    AnalysisEvidenceScope,
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
from provenance.issuer_registry import IdentifierAssertion, IdentifierResolution, IssuerRegistry
from provenance.reporting_entity_registry import ReportingEntityRegistry, SourceObligationRevision
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

K = datetime(2026, 10, 18, 5, tzinfo=UTC)
E = datetime(2026, 8, 14, 1, 2, 14, 123456, tzinfo=UTC)
ISSUER = "issuer:synthetic-canonical"
ACCESSION = "0000000001-26-000001"
URL = f"https://www.sec.gov/Archives/edgar/data/1/{ACCESSION.replace('-', '')}/financial.htm"
ROOT_URL = "https://data.sec.gov/submissions/CIK0000000001.json"
CONFIG = "c" * 64


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _observation(
    conn: sqlite3.Connection,
    identity: str,
    body: bytes,
    url: str,
    kind: str,
    *,
    root: Path,
    at: datetime = K,
) -> str:
    ledger = EvidenceLedger(conn)
    digest = _sha(body)
    raw_path = root / digest
    raw_path.write_bytes(body)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(body),
            media_type="application/json" if kind != "sec_filing" else "text/html",
            storage_uri=raw_path.as_uri(),
            recorded_at=at,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=identity,
            idempotency_key=identity,
            source_kind=kind,
            source_url=url,
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=at,
            retrieved_at=at,
            retrieval_config_sha256=CONFIG,
            collector_code_version="sec_package_subject_review.v1"
            if kind == "sec_package_subject_review"
            else "synthetic@1",
        )
    )
    return digest


@pytest.fixture
def historical(
    tmp_path: Path, migrated_db: Callable[..., Path], request: pytest.FixtureRequest
) -> Iterator[tuple[sqlite3.Connection, dict[str, object]]]:
    conn = sqlite3.connect(migrated_db(tmp_path / "historical.db"))
    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO issuer_entities VALUES(?,?,?,?)",
        (
            ISSUER,
            ISSUER,
            "operating_company",
            K + timedelta(days=1) if getattr(request, "param", "") == "future_entity" else K,
        ),
    )
    registry = IssuerRegistry(conn)
    registry.persist(
        IdentifierAssertion(
            assertion_id="cik",
            idempotency_key="cik",
            issuer_id=ISSUER,
            identifier_type="sec_cik",
            identifier_value="0000000001",
            normalized_value="0000000001",
            authority="manual",
            effective_at=K,
            knowledge_at=K,
            recorded_at=K,
        )
    )
    registry.persist(
        IdentifierResolution(
            resolution_id="cik-resolution",
            idempotency_key="cik-resolution",
            resolution_key="sec_cik:0000000001",
            revision=1,
            outcome="selected",
            selected_assertion_id="cik",
            candidate_digest_sha256=CONFIG,
            policy_name="synthetic",
            policy_version="1",
            policy_config_sha256=CONFIG,
            material_dissent=False,
            effective_at=K,
            knowledge_at=K,
            recorded_at=K,
            reason_code="synthetic",
            reason_details=(("scope", "test"),),
        )
    )
    ReportingEntityRegistry(conn).persist(
        SourceObligationRevision(
            obligation_revision_id="current-duty",
            idempotency_key="current-duty",
            obligation_key="current-duty",
            revision=1,
            issuer_id=ISSUER,
            reporting_entity_id=None,
            authority_kind="sec_edgar",
            document_family="continuous_disclosure",
            obligation_state="required",
            completeness_rule="regulator_inventory",
            active_from=K,
            active_to=None,
            decision_kind="manual",
            effective_at=K,
            knowledge_at=K,
            recorded_at=K,
            reason_code="synthetic",
            reason_details=(("scope", "test"),),
        )
    )
    columns: dict[str, list[object]] = {
        name: [""]
        for name in (
            "accessionNumber",
            "filingDate",
            "reportDate",
            "acceptanceDateTime",
            "act",
            "form",
            "fileNumber",
            "filmNumber",
            "items",
            "size",
            "isXBRL",
            "isInlineXBRL",
            "primaryDocument",
            "primaryDocDescription",
        )
    }
    columns.update(
        accessionNumber=[ACCESSION],
        filingDate=["2026-08-13"],
        reportDate=["2026-06-30"],
        acceptanceDateTime=[
            E.isoformat()
            if getattr(request, "param", "") == "future_entity"
            else str(getattr(request, "param", E.isoformat()))
        ],
        form=["6-K"],
        primaryDocument=["financial.htm"],
    )
    metadata = json.dumps(
        {"cik": "1", "name": "Synthetic issuer", "filings": {"recent": columns, "files": []}},
        separators=(",", ":"),
    )
    _observation(conn, "metadata", metadata.encode(), ROOT_URL, "sec_submissions", root=tmp_path)
    heading = "Consolidated financial statements for the quarter ended June 30, 2026"
    body = f'<html><h1 id="period">{heading}</h1></html>'.encode()
    digest = _observation(conn, "financial-source", body, URL, "sec_filing", root=tmp_path)
    review = json.dumps(
        {
            "schema_version": "sec_package_subject_review.v1",
            "cik": "0000000001",
            "ticker": "SYNTH",
            "reviewer": "synthetic-reviewer",
            "reviewed_at": K.isoformat(),
            "subjects": [
                {
                    "accession_number": ACCESSION,
                    "source_url": URL,
                    "source_sha256": digest,
                    "raw_path": "financial.htm",
                    "document_type": "financial_statement",
                    "heading_selector": "h1#period",
                    "heading_text": heading,
                    "period_end": "2026-06-30",
                    "period_end_text": "June 30, 2026",
                    "rationale": "The exact heading identifies the reported financial quarter.",
                }
            ],
        }
    )
    review_sha = _observation(
        conn,
        "review",
        review.encode(),
        ROOT_URL + "#reviewed-package-subjects",
        "sec_package_subject_review",
        root=tmp_path,
    )
    coverage = SourceCoverageLedger(conn)
    coverage.persist(
        SourceInventorySnapshot(
            snapshot_id="inventory",
            idempotency_key="inventory",
            inventory_key="synthetic:sec",
            revision=1,
            issuer_id=ISSUER,
            ticker="SYNTH",
            source_kind="sec_submissions",
            source_url=ROOT_URL,
            source_observation_id="metadata",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=CONFIG,
            collector_code_version="synthetic@1",
            started_at=K,
            completed_at=K,
            recorded_at=K,
        )
    )
    components = tuple(
        InventoryComponent(
            component_id=identity,
            idempotency_key=identity,
            snapshot_id="inventory",
            component_key=key,
            component_kind="primary" if identity == "root" else "other",
            source_url=url,
            source_observation_id=observation,
            outcome="succeeded",
            required=True,
            ordinal=index,
            recorded_at=K,
        )
        for index, (identity, key, url, observation) in enumerate(
            (
                ("root", "root", ROOT_URL, "metadata"),
                (
                    "subject-review",
                    "reviewed-package-subjects",
                    ROOT_URL + "#reviewed-package-subjects",
                    "review",
                ),
            )
        )
    )
    store = SourceInventorySealStore(conn)
    for component in components:
        store.persist(component)
    store.persist(
        InventorySeal(
            snapshot_id="inventory",
            expected_component_count=len(components),
            component_digest_sha256=component_digest(components),
            completion_status="complete",
            sealed_at=K,
        )
    )
    coverage.persist(
        ExpectedDocument(
            expected_document_id="financial",
            idempotency_key="financial",
            snapshot_id="inventory",
            expected_document_key="financial",
            issuer_id=ISSUER,
            ticker="SYNTH",
            source_kind="sec_filing",
            document_type="financial_statement",
            form_type="6-K",
            accession_number=ACCESSION,
            source_url=URL,
            primary_document="financial.htm",
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            filing_at=datetime(2026, 8, 13, tzinfo=UTC),
            expectation_basis="authoritative",
            recorded_at=K,
        )
    )
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="original-financial",
            document_key="financial",
            version_sequence=1,
            observation_id="financial-source",
            blob_sha256=digest,
            issuer_id=ISSUER,
            ticker="SYNTH",
            document_type="financial_statement",
            form_type="6-K",
            accession_number=ACCESSION,
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            language="en",
            recorded_at=K,
        )
    )
    details = {
        "subject_review_sha256": review_sha,
        "subject_source_sha256": digest,
        "subject_reviewer": "synthetic-reviewer",
        "subject_reviewed_at": K.isoformat(),
        "subject_heading_selector": "h1#period",
        "subject_heading_text": heading,
        "subject_rationale": "The exact heading identifies the reported financial quarter.",
        "subject_period_end_text": "June 30, 2026",
    }
    coverage.persist(
        CoverageAssessment(
            assessment_id="capture",
            idempotency_key="capture",
            expected_document_id="financial",
            revision=1,
            coverage_status="captured",
            document_version_id="original-financial",
            reason_code="synthetic_capture",
            reason_details=tuple(sorted(details.items())),
            decision_kind="manual",
            policy_name="synthetic",
            policy_version="1",
            policy_config_sha256=CONFIG,
            effective_at=K,
            knowledge_at=K,
            recorded_at=K,
            material_dissent=False,
        )
    )
    conn.commit()
    payload: dict[str, object] = {
        "purpose": "NU historical post reconstruction",
        "issuer_id": ISSUER,
        "inventory_key": "synthetic:sec",
        "required_period_ends": (date(2026, 6, 30),),
        "require_latest_period": False,
        "cutoff_at": K,
        "observed_through": K,
        "historical_reconstruction": {
            "label": "historical_reconstruction",
            "public_cutoff_at": E,
            "authorities": [
                {
                    "source_observation_id": "metadata",
                    "expected_blob_sha256": _sha(metadata.encode()),
                }
            ],
            "documents": [
                {"expected_document_id": "financial", "document_version_id": "original-financial"}
            ],
        },
    }
    try:
        yield conn, payload
    finally:
        conn.close()


def test_historical_scope_uses_public_clock_and_physical_capture(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    before = conn.total_changes
    scope = build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))
    assert scope.schema_version == "analysis-evidence-scope/v2"
    assert scope.request.cutoff_at == K and scope.request.observed_through == K
    assert require_analysis_documents(conn, scope, K, K) == ("original-financial",)
    assert conn.total_changes == before
    assert AnalysisEvidenceScope.model_validate_json(scope.model_dump_json()) == scope
    assert "raw_json" not in scope.model_dump_json()
    assert conn.execute(
        "SELECT retrieved_at FROM evidence_source_observations WHERE observation_id='financial-source'"
    ).fetchone()[0] == str(K)


@pytest.mark.parametrize(
    "failure",
    [
        "before_acceptance",
        "tampered_bytes",
        "forged_authority",
        "wrong_original",
        "missing_subject",
        "physical_capture_after_cutoff",
    ],
)
def test_historical_selection_fails_closed(
    historical: tuple[sqlite3.Connection, dict[str, object]], failure: str
) -> None:
    conn, payload = historical
    context = cast(dict[str, object], payload["historical_reconstruction"])
    if failure == "before_acceptance":
        context["public_cutoff_at"] = E - timedelta(microseconds=1)
    elif failure == "forged_authority":
        authorities = cast(list[dict[str, object]], context["authorities"])
        authorities[0]["expected_blob_sha256"] = "0" * 64
    elif failure == "tampered_bytes":
        from urllib.parse import urlsplit
        from urllib.request import url2pathname

        uri = conn.execute(
            "SELECT storage_uri FROM evidence_content_blobs WHERE sha256=(SELECT blob_sha256 FROM evidence_source_observations WHERE observation_id='metadata')"
        ).fetchone()[0]
        path = Path(url2pathname(urlsplit(str(uri)).path))
        path.write_bytes(path.read_bytes().replace(E.isoformat().encode(), b""))
    elif failure == "wrong_original":
        cast(list[dict[str, object]], context["documents"])[0]["document_version_id"] = (
            "later-comparative"
        )
    elif failure == "physical_capture_after_cutoff":
        payload["cutoff_at"] = K - timedelta(microseconds=1)
    else:
        conn.execute("DROP TRIGGER trg_source_coverage_assessments_append_only")
        conn.execute(
            "UPDATE source_coverage_assessments SET reason_details_json='{}' WHERE assessment_id='capture'"
        )
    with pytest.raises(ValueError):
        build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))


@pytest.mark.parametrize("damage", ["missing", "symlink", "hardlink"])
def test_authority_storage_cannot_use_missing_or_alias_bytes(
    historical: tuple[sqlite3.Connection, dict[str, object]], damage: str
) -> None:
    from urllib.parse import urlsplit
    from urllib.request import url2pathname

    conn, payload = historical
    uri = conn.execute(
        "SELECT storage_uri FROM evidence_content_blobs WHERE sha256=(SELECT blob_sha256 "
        "FROM evidence_source_observations WHERE observation_id='metadata')"
    ).fetchone()[0]
    path = Path(url2pathname(urlsplit(str(uri)).path))
    if damage == "missing":
        path.unlink()
    elif damage == "symlink":
        target = path.with_suffix(".alias")
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    else:
        path.with_suffix(".alias").hardlink_to(path)
    with pytest.raises(ValueError):
        build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))


@pytest.mark.parametrize("historical", ["", "2026-08-13", "2026-08-14T01:02:14"], indirect=True)
def test_missing_date_only_or_naive_acceptance_cannot_use_filing_date(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    with pytest.raises(ValueError):
        build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))


def test_later_capture_cannot_replace_original_comparator_version(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    tmp_path: Path,
) -> None:
    conn, payload = historical
    scope = build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))
    later = K + timedelta(days=1)
    digest = _observation(
        conn,
        "later-source",
        b"<html>Later comparative document</html>",
        URL,
        "sec_filing",
        root=tmp_path,
        at=later,
    )
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="later-copy",
            document_key="financial",
            version_sequence=2,
            observation_id="later-source",
            blob_sha256=str(digest),
            issuer_id=ISSUER,
            ticker="SYNTH",
            document_type="financial_statement",
            form_type="6-K",
            accession_number=ACCESSION,
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            language="en",
            recorded_at=later,
        )
    )
    details = json.loads(
        str(
            conn.execute(
                "SELECT reason_details_json FROM source_coverage_assessments WHERE assessment_id='capture'"
            ).fetchone()[0]
        )
    )
    SourceCoverageLedger(conn).persist(
        CoverageAssessment(
            assessment_id="later-capture",
            idempotency_key="later-capture",
            expected_document_id="financial",
            revision=2,
            coverage_status="captured",
            document_version_id="later-copy",
            reason_code="synthetic_recapture",
            reason_details=tuple(sorted((str(key), str(value)) for key, value in details.items())),
            decision_kind="manual",
            policy_name="synthetic",
            policy_version="1",
            policy_config_sha256=CONFIG,
            effective_at=later,
            knowledge_at=later,
            recorded_at=later,
            supersedes_assessment_id="capture",
            material_dissent=False,
        )
    )
    verify_analysis_scope(conn, scope, require_current_inventory=False)
    assert require_analysis_documents(conn, scope, K, K) == ("original-financial",)
    payload.update(cutoff_at=later, observed_through=later)
    with pytest.raises(ValueError, match="subject_capture_unavailable"):
        build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))


def test_two_metadata_authorities_require_one_canonical_order() -> None:
    payload: dict[str, object] = {
        "purpose": "historical authority order",
        "issuer_id": ISSUER,
        "inventory_key": "synthetic:sec",
        "required_period_ends": (date(2026, 6, 30),),
        "require_latest_period": False,
        "cutoff_at": K,
        "observed_through": K,
        "historical_reconstruction": {
            "label": "historical_reconstruction",
            "public_cutoff_at": E,
            "authorities": [
                {"source_observation_id": "metadata-a", "expected_blob_sha256": "a" * 64},
                {"source_observation_id": "metadata-b", "expected_blob_sha256": "b" * 64},
            ],
            "documents": [
                {"expected_document_id": "financial", "document_version_id": "original-financial"}
            ],
        },
    }
    ordered = AnalysisScopeRequest.model_validate(payload)
    context = cast(dict[str, object], payload["historical_reconstruction"])
    authorities = cast(list[dict[str, object]], context["authorities"])
    context["authorities"] = list(reversed(authorities))
    with pytest.raises(ValueError, match="ordered"):
        AnalysisScopeRequest.model_validate(payload)
    context["authorities"] = [authorities[0], authorities[0]]
    with pytest.raises(ValueError, match="unique"):
        AnalysisScopeRequest.model_validate(payload)
    assert AnalysisScopeRequest.model_validate_json(ordered.model_dump_json()) == ordered


@pytest.mark.parametrize("future_record", ["resolution", "assertion"])
def test_identity_recorded_after_observation_cannot_enter_historical_scope(
    historical: tuple[sqlite3.Connection, dict[str, object]], future_record: str
) -> None:
    conn, payload = historical
    registry = IssuerRegistry(conn)
    later = K + timedelta(days=1)
    assertion_id = "cik"
    if future_record == "assertion":
        assertion_id = "later-cik"
        registry.persist(
            IdentifierAssertion(
                assertion_id=assertion_id,
                idempotency_key=assertion_id,
                issuer_id=ISSUER,
                identifier_type="sec_cik",
                identifier_value="0000000001",
                normalized_value="0000000001",
                authority="manual",
                effective_at=K,
                knowledge_at=K,
                recorded_at=later,
            )
        )
    registry.persist(
        IdentifierResolution(
            resolution_id="later-resolution",
            idempotency_key="later-resolution",
            resolution_key="sec_cik:0000000001",
            revision=2,
            outcome="selected",
            selected_assertion_id=assertion_id,
            candidate_digest_sha256=CONFIG,
            policy_name="synthetic",
            policy_version="1",
            policy_config_sha256=CONFIG,
            material_dissent=False,
            effective_at=K,
            knowledge_at=K,
            recorded_at=later if future_record == "resolution" else K,
            supersedes_resolution_id="cik-resolution",
            reason_code="synthetic",
            reason_details=(("scope", "late recorded identity"),),
        )
    )
    # The existing registry resolves at K only; it can see these later-recorded rows.
    assert registry.resolve_identifier("sec_cik", "0000000001", knowledge_at=K).issuer_id == ISSUER
    with pytest.raises(ValueError, match=r"identity.*observed"):
        build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))
    payload["observed_through"] = later
    assert require_analysis_documents(
        conn, build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload)), K, later
    ) == ("original-financial",)


@pytest.mark.parametrize("historical", ["future_entity"], indirect=True)
def test_issuer_created_after_observation_cannot_enter_historical_scope(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    with pytest.raises(ValueError, match=r"identity.*observed"):
        build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))
