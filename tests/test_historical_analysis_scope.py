"""Historical public selection retains physical capture clocks and exact versions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import cast

import pytest

from provenance.analysis_scope import (
    AnalysisEvidenceScope,
    AnalysisScopeRequest,
    HistoricalIssuerDaySelection,
    HistoricalSecAuthority,
    build_analysis_scope,
    require_analysis_documents,
    verify_analysis_scope,
    verify_historical_issuer_day_witness,
    verify_historical_retrieval_witness,
)
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.issuer_registry import IdentifierAssertion, IdentifierResolution, IssuerRegistry
from provenance.reporting_entity_registry import ReportingEntityRegistry, SourceObligationRevision
from provenance.sec_native_capture import load_captured_sec_filing_package
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
EVENT_ACCESSION = "0000000001-26-000002"
EVENT_URL = (
    f"https://www.sec.gov/Archives/edgar/data/1/{EVENT_ACCESSION.replace('-', '')}/event.htm"
)
ROOT_URL = "https://data.sec.gov/submissions/CIK0000000001.json"
CONFIG = "c" * 64


def test_scoped_loader_loads_selected_original_exhibit_not_cover_filing(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    scope = build_analysis_scope(
        conn,
        AnalysisScopeRequest.model_validate(
            payload | {"readiness_profile": "complete_report_evidence/v1"}
        ),
    )
    members = load_captured_sec_filing_package(
        conn,
        inventory_key=scope.request.inventory_key,
        accession_number=ACCESSION,
        analysis_scope=scope,
    )
    assert [
        (item.expected_document_id, item.document_version_id, item.document_type)
        for item in members
    ] == [("financial", "original-financial", "financial_statement")]


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
    pre_mode = getattr(request, "param", "") == "q1_pre"
    reported_end = (
        datetime(2026, 3, 31, tzinfo=UTC) if pre_mode else datetime(2026, 6, 30, tzinfo=UTC)
    )
    filing_at = datetime(2026, 5, 14, tzinfo=UTC) if pre_mode else datetime(2026, 8, 13, tzinfo=UTC)
    accepted_at = datetime(2026, 5, 15, 1, 2, 14, 123456, tzinfo=UTC) if pre_mode else E
    period_text = "March 31, 2026" if pre_mode else "June 30, 2026"
    event_mode = getattr(request, "param", "") in {"with_event", "late_event"}
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
        filingDate=[filing_at.date().isoformat()],
        reportDate=[reported_end.date().isoformat()],
        acceptanceDateTime=[
            accepted_at.isoformat()
            if getattr(request, "param", "") == "future_entity" or event_mode or pre_mode
            else str(getattr(request, "param", E.isoformat()))
        ],
        form=["6-K"],
        primaryDocument=["financial.htm"],
    )
    if event_mode:
        event_values = {
            "accessionNumber": EVENT_ACCESSION,
            "filingDate": "2026-07-10",
            "reportDate": "",
            "acceptanceDateTime": "2026-08-18T01:00:00Z"
            if request.param == "late_event"
            else "2026-07-10T15:00:00Z",
            "form": "6-K",
            "primaryDocument": "event.htm",
        }
        for key, values in columns.items():
            values.append(event_values.get(key, values[0]))
    metadata = json.dumps(
        {"cik": "1", "name": "Synthetic issuer", "filings": {"recent": columns, "files": []}},
        separators=(",", ":"),
    )
    _observation(conn, "metadata", metadata.encode(), ROOT_URL, "sec_submissions", root=tmp_path)
    heading = "Consolidated financial statements for the quarter ended " + period_text
    body = f'<html><h1 id="period">{heading}</h1></html>'.encode()
    digest = _observation(conn, "financial-source", body, URL, "sec_filing", root=tmp_path)
    event_heading = "Authorization of Mexico operations and material regulatory changes"
    event_digest = (
        _observation(
            conn,
            "event-source",
            f'<html><h1 id="event">{event_heading}</h1></html>'.encode(),
            EVENT_URL,
            "sec_filing",
            root=tmp_path,
        )
        if event_mode
        else None
    )
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
                    "period_end": reported_end.date().isoformat(),
                    "period_end_text": period_text,
                    "rationale": "The exact heading identifies the reported financial quarter.",
                },
                *(
                    [
                        {
                            "accession_number": EVENT_ACCESSION,
                            "source_url": EVENT_URL,
                            "source_sha256": event_digest,
                            "raw_path": "event.htm",
                            "document_type": "investor_update",
                            "heading_selector": "h1#event",
                            "heading_text": event_heading,
                            "period_end": None,
                            "period_end_text": None,
                            "rationale": "The exact source heading identifies a material operating authorization event.",
                        }
                    ]
                    if event_mode
                    else []
                ),
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
            period_end=reported_end,
            filing_at=filing_at,
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
            period_end=reported_end,
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
        "subject_period_end_text": period_text,
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
    if event_mode:
        assert event_digest is not None
        coverage.persist(
            ExpectedDocument(
                expected_document_id="event",
                idempotency_key="event",
                snapshot_id="inventory",
                expected_document_key="event",
                issuer_id=ISSUER,
                ticker="SYNTH",
                source_kind="sec_filing",
                document_type="investor_update",
                form_type="6-K",
                accession_number=EVENT_ACCESSION,
                source_url=EVENT_URL,
                primary_document="event.htm",
                period_end=None,
                filing_at=datetime(2026, 7, 10, tzinfo=UTC),
                expectation_basis="authoritative",
                recorded_at=K,
            )
        )
        EvidenceLedger(conn).persist(
            DocumentVersion(
                document_version_id="original-event",
                document_key="event",
                version_sequence=1,
                observation_id="event-source",
                blob_sha256=event_digest,
                issuer_id=ISSUER,
                ticker="SYNTH",
                document_type="investor_update",
                form_type="6-K",
                accession_number=EVENT_ACCESSION,
                period_end=None,
                language="en",
                recorded_at=K,
            )
        )
        event_details = dict(details) | {
            "subject_source_sha256": event_digest,
            "subject_heading_selector": "h1#event",
            "subject_heading_text": event_heading,
            "subject_rationale": "The exact source heading identifies a material operating authorization event.",
        }
        event_details.pop("subject_period_end_text")
        coverage.persist(
            CoverageAssessment(
                assessment_id="event-capture",
                idempotency_key="event-capture",
                expected_document_id="event",
                revision=1,
                coverage_status="captured",
                document_version_id="original-event",
                reason_code="synthetic_event_capture",
                reason_details=tuple(sorted(event_details.items())),
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
        "required_period_ends": (reported_end.date(),),
        "require_latest_period": False,
        "cutoff_at": K,
        "observed_through": K,
        "historical_reconstruction": {
            "label": "historical_reconstruction",
            "public_cutoff_at": accepted_at,
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


def test_report_scope_labels_sec_acceptance_as_event_evidence(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    scope = build_analysis_scope(
        conn,
        AnalysisScopeRequest.model_validate(
            payload | {"readiness_profile": "complete_report_evidence/v1"}
        ),
    )
    assert scope.schema_version == "analysis-evidence-scope/v3"
    reasons = [entry.reason for entry in scope.entries if entry.role == "research_document"]
    assert reasons and all(":sec_accepted_at:" in reason for reason in reasons)
    assert all("available_by" not in reason for reason in reasons)


def _historical_package_child(conn: sqlite3.Connection, document_type: str) -> ExpectedDocument:
    document = ExpectedDocument(
        expected_document_id="original-child",
        idempotency_key="original-child",
        snapshot_id="inventory",
        expected_document_key="original-child",
        issuer_id=ISSUER,
        ticker="SYNTH",
        source_kind="sec_filing",
        document_type=document_type,
        form_type="6-K",
        accession_number=ACCESSION,
        source_url=URL.removesuffix("financial.htm") + "original-child.htm",
        primary_document="original-child.htm",
        period_end=datetime(2026, 6, 30, tzinfo=UTC),
        filing_at=datetime(2026, 8, 13, tzinfo=UTC),
        expectation_basis="authoritative",
        recorded_at=K,
    )
    SourceCoverageLedger(conn).persist(document)
    return document


def _capture_historical_package_child(
    conn: sqlite3.Connection, document: ExpectedDocument, root: Path
) -> None:
    assert document.source_url is not None
    digest = _observation(
        conn,
        "child-source",
        b"<html><p>Original package document.</p></html>",
        document.source_url,
        "sec_filing",
        root=root,
    )
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="captured-original-child",
            document_key="original-child",
            version_sequence=1,
            observation_id="child-source",
            blob_sha256=digest,
            issuer_id=ISSUER,
            ticker="SYNTH",
            document_type=document.document_type,
            form_type="6-K",
            accession_number=ACCESSION,
            period_end=document.period_end,
            language="en",
            recorded_at=K,
        )
    )
    SourceCoverageLedger(conn).persist(
        CoverageAssessment(
            assessment_id="child-capture",
            idempotency_key="child-capture",
            expected_document_id=document.expected_document_id,
            revision=1,
            coverage_status="captured",
            document_version_id="captured-original-child",
            reason_code="synthetic_original_capture",
            reason_details=(("source", "synthetic_fixture"),),
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


@pytest.mark.parametrize(
    "document_type",
    [
        "filing",
        "financial_statement",
        "supplement",
        "earnings_release",
        "sec_exhibit",
        "unclassified_original",
    ],
)
def test_historical_report_profile_requires_every_original_package_document(
    historical: tuple[sqlite3.Connection, dict[str, object]], tmp_path: Path, document_type: str
) -> None:
    conn, payload = historical
    child = _historical_package_child(conn, document_type)
    scope = build_analysis_scope(
        conn,
        AnalysisScopeRequest.model_validate(
            payload | {"readiness_profile": "complete_report_evidence/v1"}
        ),
    )
    assert (
        next(
            entry
            for entry in scope.entries
            if entry.expected_document_id == child.expected_document_id
        ).role
        == "package_dependency"
    )
    with pytest.raises(ValueError, match="analysis selected package capture is incomplete"):
        require_analysis_documents(conn, scope, K, K)
    _capture_historical_package_child(conn, child, tmp_path)
    # Capture closes the dependency, but does not promote it into the research
    # document set or change the retained original comparator selection.
    assert require_analysis_documents(conn, scope, K, K) == ("original-financial",)


@pytest.mark.parametrize("document_type", ["sec_financial_report", "sec_supporting_attachment"])
def test_historical_report_profile_keeps_generated_and_supporting_children_deferred(
    historical: tuple[sqlite3.Connection, dict[str, object]], document_type: str
) -> None:
    conn, payload = historical
    _historical_package_child(conn, document_type)
    scope = build_analysis_scope(
        conn,
        AnalysisScopeRequest.model_validate(
            payload | {"readiness_profile": "complete_report_evidence/v1"}
        ),
    )
    assert require_analysis_documents(conn, scope, K, K) == ("original-financial",)


def _public_request(payload: dict[str, object], at: datetime) -> AnalysisScopeRequest:
    historical = dict(cast(dict[str, object], payload["historical_reconstruction"]))
    historical.update(
        public_cutoff_at=at,
        public_witness_policy="historical-public-witness/v1",
        public_witness_owner_sha256=CONFIG,
        public_window="nu-q2-2026-post/v1",
    )
    return AnalysisScopeRequest.model_validate(payload | {"historical_reconstruction": historical})


def test_public_policy_replays_zone_envelope_and_retains_physical_intake(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    at = datetime(2026, 8, 18, 4, tzinfo=UTC)
    before = conn.total_changes
    scope = build_analysis_scope(conn, _public_request(payload, at))
    assert scope.schema_version == "analysis-evidence-scope/v4"
    assert len(scope.public_witnesses) == 1
    witness = scope.public_witnesses[0]
    assert witness.lower_at == E
    assert witness.upper_at == at - timedelta(seconds=1)
    assert witness.precision == "zone_ambiguous"
    assert witness.calendar_sha256 is not None
    assert scope.request.cutoff_at == K
    assert require_analysis_documents(conn, scope, K, K) == ("original-financial",)
    verify_analysis_scope(conn, AnalysisEvidenceScope.model_validate_json(scope.model_dump_json()))
    assert conn.total_changes == before


@pytest.mark.parametrize("seconds", [-1, 0, 1])
def test_public_policy_requires_strict_after_its_upper_bound(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    seconds: int,
) -> None:
    conn, payload = historical
    at = datetime(2026, 8, 18, 3, 59, 59, tzinfo=UTC) + timedelta(seconds=seconds)
    if seconds <= 0:
        with pytest.raises(ValueError, match="public bound"):
            build_analysis_scope(conn, _public_request(payload, at))
    else:
        assert build_analysis_scope(conn, _public_request(payload, at)).public_witnesses


@pytest.mark.parametrize(
    ("historical", "expected"),
    [
        ("2026-05-15T15:00:00Z", datetime(2026, 5, 19, 3, 59, 59, tzinfo=UTC)),
        ("2026-05-22T15:00:00Z", datetime(2026, 5, 27, 3, 59, 59, tzinfo=UTC)),
        ("2026-07-02T15:00:00Z", datetime(2026, 7, 7, 3, 59, 59, tzinfo=UTC)),
    ],
    indirect=["historical"],
)
def test_public_policy_weekend_and_authoritative_holiday_rollover(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    expected: datetime,
) -> None:
    conn, payload = historical
    scope = build_analysis_scope(conn, _public_request(payload, expected + timedelta(seconds=1)))
    assert scope.public_witnesses[0].upper_at == expected
    # Preserve the exact declared metadata text, including its Z suffix.
    raw_metadata = json.loads(
        Path(
            conn.execute(
                "SELECT storage_uri FROM evidence_content_blobs WHERE sha256=("
                "SELECT blob_sha256 FROM evidence_source_observations WHERE observation_id='metadata')"
            )
            .fetchone()[0]
            .removeprefix("file://")
        ).read_bytes()
    )
    assert (
        scope.public_witnesses[0].raw_value
        == raw_metadata["filings"]["recent"]["acceptanceDateTime"][0]
    )


@pytest.mark.parametrize(
    "historical", ["2025-01-08T15:00:00Z", "2027-01-04T15:00:00Z"], indirect=True
)
def test_public_policy_unknown_calendar_year_or_exception_range_holds(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    request = _public_request(payload, datetime(2026, 8, 19, tzinfo=UTC))
    with pytest.raises(ValueError, match=r"calendar|available by"):
        build_analysis_scope(conn, request)


def test_public_policy_post_ceiling_cannot_be_extended(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    _conn, payload = historical
    ceiling = datetime(2026, 8, 20, 3, 59, 59, tzinfo=UTC)
    assert _public_request(payload, ceiling).historical_reconstruction is not None
    with pytest.raises(ValueError, match="fixed August 19 ceiling"):
        _public_request(payload, ceiling + timedelta(seconds=1))


def _issuer_publication(
    conn: sqlite3.Connection,
    root: Path,
    *,
    published: str = "2026-08-14T00:00:00.000Z",
    wrong_file: bool = False,
) -> HistoricalIssuerDaySelection:
    url = "https://api.mziq.com/mzfilemanager/v2/d/synthetic-company/synthetic-file?origin=2"
    pdf = b"%PDF-1.7\nSynthetic source bytes for native lineage tests.\n"
    digest = _observation(conn, "call-source", pdf, url, "ir_pdf", root=root)
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="issuer-call",
            document_key="issuer-call",
            version_sequence=1,
            observation_id="call-source",
            blob_sha256=digest,
            issuer_id=ISSUER,
            ticker="SYNTH",
            document_type="earnings_transcript",
            form_type="IR",
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            language="en",
            recorded_at=K,
        )
    )
    metadata = json.dumps(
        {
            "success": True,
            "data": {
                "document_metas": [
                    {
                        "id": "synthetic-record",
                        "company_id": "synthetic-company",
                        "download_link_id": "wrong-file" if wrong_file else "synthetic-file",
                        "file_url": url,
                        "is_published": True,
                        "language_code": "en_US",
                        "file_published_date": published,
                        "file_size": str(len(pdf)),
                    }
                ]
            },
        }
    ).encode()
    metadata_sha = _observation(
        conn,
        "issuer-metadata",
        metadata,
        "https://api.mziq.com/mzfilemanager/v2/synthetic-company/results",
        "ir_json",
        root=root,
    )
    return HistoricalIssuerDaySelection(
        document_version_id="issuer-call",
        record_id="synthetic-record",
        metadata=HistoricalSecAuthority(
            source_observation_id="issuer-metadata", expected_blob_sha256=metadata_sha
        ),
    )


def test_issuer_day_witness_replays_exact_pdf_record_and_date_precision(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    tmp_path: Path,
) -> None:
    conn, _payload = historical
    selection = _issuer_publication(conn, tmp_path)
    witness_for = partial(
        verify_historical_issuer_day_witness,
        conn,
        selection,
        issuer_id=ISSUER,
        cutoff_at=K,
        observed_through=K,
        owner_approval_sha256=CONFIG,
        public_window="nu-q2-2026-post/v1",
    )
    at = datetime(2026, 8, 15, 4, tzinfo=UTC)
    witness = witness_for(public_cutoff_at=at)
    assert witness.lower_at == datetime(2026, 8, 14, 3, tzinfo=UTC)
    assert witness.upper_at == datetime(2026, 8, 15, 3, 59, 59, tzinfo=UTC)
    assert witness.precision == "day" and witness.issuer_record_id == "synthetic-record"
    assert witness_for(public_cutoff_at=at) == witness
    with pytest.raises(ValueError, match="public bound"):
        witness_for(public_cutoff_at=witness.upper_at)


@pytest.mark.parametrize(
    "wrong_file,published", [(True, "2026-08-14T00:00:00.000Z"), (False, "2026-08-14T12:30:00Z")]
)
def test_issuer_day_rejects_unbound_file_and_fabricated_intraday_precision(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    tmp_path: Path,
    wrong_file: bool,
    published: str,
) -> None:
    conn, _payload = historical
    selection = _issuer_publication(conn, tmp_path, wrong_file=wrong_file, published=published)
    with pytest.raises(ValueError, match=r"identity binding|date-precision"):
        verify_historical_issuer_day_witness(
            conn,
            selection,
            issuer_id=ISSUER,
            cutoff_at=K,
            observed_through=K,
            owner_approval_sha256=CONFIG,
            public_cutoff_at=datetime(2026, 8, 18, tzinfo=UTC),
            public_window="nu-q2-2026-post/v1",
        )


def test_required_late_capture_stays_held_under_fixed_post_window(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, _payload = historical
    at = datetime(2026, 8, 18, 4, tzinfo=UTC)
    with pytest.raises(ValueError, match="public bound"):
        verify_historical_retrieval_witness(
            conn,
            "original-financial",
            issuer_id=ISSUER,
            cutoff_at=K,
            observed_through=K,
            owner_approval_sha256=CONFIG,
            public_cutoff_at=at,
            public_window="nu-q2-2026-post/v1",
        )
    assert at == datetime(2026, 8, 18, 4, tzinfo=UTC)


def _event_request(payload: dict[str, object], *, only_event: bool = False) -> dict[str, object]:
    historical = dict(cast(dict[str, object], payload["historical_reconstruction"]))
    selected = [{"expected_document_id": "event", "document_version_id": "original-event"}]
    if not only_event:
        selected.append(
            {"expected_document_id": "financial", "document_version_id": "original-financial"}
        )
    historical["documents"] = selected
    return payload | {"historical_reconstruction": historical}


@pytest.mark.parametrize("historical", ["with_event"], indirect=True)
def test_v4_pinned_periodless_event_joins_research_sources_without_becoming_period_anchor(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    request = _public_request(_event_request(payload), datetime(2026, 8, 18, 4, tzinfo=UTC))
    scope = build_analysis_scope(conn, request)
    event = next(entry for entry in scope.entries if entry.expected_document_id == "event")
    assert scope.schema_version == "analysis-evidence-scope/v4"
    assert event.role == "research_document" and event.expected_document.period_end is None
    assert scope.request.required_period_ends == (date(2026, 6, 30),)
    witness = next(
        item for item in scope.public_witnesses if item.document_version_id == "original-event"
    )
    historical_request = request.historical_reconstruction
    assert historical_request is not None
    assert (
        witness.source_url == EVENT_URL and witness.upper_at < historical_request.public_cutoff_at
    )
    assert require_analysis_documents(conn, scope, K, K) == ("original-event", "original-financial")
    verify_analysis_scope(conn, scope)


@pytest.mark.parametrize("historical", ["late_event"], indirect=True)
def test_v4_event_after_the_public_bound_is_rejected(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    with pytest.raises(ValueError, match="public bound"):
        build_analysis_scope(
            conn, _public_request(_event_request(payload), datetime(2026, 8, 18, 4, tzinfo=UTC))
        )


@pytest.mark.parametrize("historical", ["with_event"], indirect=True)
@pytest.mark.parametrize("profile", [None, "complete_report_evidence/v1"])
def test_legacy_historical_scopes_keep_their_periodless_event_rejection(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    profile: str | None,
) -> None:
    conn, payload = historical
    request = _event_request(payload)
    if profile is not None:
        request["readiness_profile"] = profile
    with pytest.raises(ValueError, match="quarterly SEC subject"):
        build_analysis_scope(conn, AnalysisScopeRequest.model_validate(request))


@pytest.mark.parametrize("historical", ["with_event"], indirect=True)
def test_v4_event_alone_cannot_cover_a_required_financial_period(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    with pytest.raises(ValueError, match="do not cover the declared periods"):
        build_analysis_scope(
            conn,
            _public_request(
                _event_request(payload, only_event=True), datetime(2026, 8, 18, 4, tzinfo=UTC)
            ),
        )
