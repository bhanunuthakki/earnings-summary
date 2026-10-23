"""Report page scope preserves quarantine and rejects deferred-source use."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import fitz
import pytest
from pydantic import ValidationError

from provenance.analysis_scope import (
    AnalysisEvidenceScope,
    AnalysisScopeRequest,
    ReportPdfTableScope,
)
from provenance.document_processing_evidence import (
    DocumentProcessingEvidenceIntegrityError,
    DocumentProcessingEvidenceMissingError,
    publish_document_processing_evidence,
    verify_document_processing_evidence,
)
from provenance.evidence_ledger import EvidenceLocator
from provenance.research_snapshot import ResearchSnapshotRequest
from research import decision_brief as briefs
from research.memo_report_evidence import (
    MELI_REPORT_EVIDENCE_REQUIREMENTS,
    MemoReportSourcePopulation,
)
from tests import test_document_processing_evidence as pdf_fixtures
from tests.test_analysis_issuer_pdf_scope import pdf_scope as pdf_scope
from tests.test_document_processing_evidence import T0, T1, T2
from tests.test_report_canonical_financials import database as database


def _sha(value: object, *, ascii_only: bool = True) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=ascii_only).encode()
    ).hexdigest()


def _population() -> dict[str, object]:
    payload: dict[str, object] = {
        "recipe": "meli_integrated_report.v1",
        "requirements": [
            {
                "requirement": key,
                "evidence_node_ids": ["native-sec-support"],
                "observation_ids": [],
                "rationale": "Exact independently selected synthetic source for this requirement.",
            }
            for key in MELI_REPORT_EVIDENCE_REQUIREMENTS
        ],
    }
    return {**payload, "population_sha256": _sha(payload, ascii_only=False)}


def _seed_scope(conn: sqlite3.Connection, *, incomplete: bool = False) -> ReportPdfTableScope:
    def sha_text(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    conn.create_function("fact_sha256", 1, sha_text)
    document = fitz.open(stream=getattr(pdf_fixtures, "_pdf_table_fixture")(), filetype="pdf")
    page = document.new_page(width=320, height=220)
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
    pixmap.clear_with(255)
    page.insert_image((20, 20, 300, 200), pixmap=pixmap)
    raw = document.tobytes(garbage=4, deflate=True)
    document.close()
    from provenance import pdf_table_extraction as extraction

    extracted = extraction.extract_pdf_tables(raw)
    if incomplete:
        draft = extracted.model_copy(update={"pdf_page_count": 3})
        seal = getattr(extraction, "_digest")(getattr(extraction, "_document_seal_payload")(draft))
        extracted = draft.model_copy(update={"ordered_page_table_seal_sha256": seal})
    artifact, artifact_id = getattr(pdf_fixtures, "_seed_pdf_table_artifact")(
        conn, document_version_id="scoped-pdf", raw_pdf_bytes=raw, artifact=extracted
    )
    assert artifact.disposition == "quarantined"
    assert artifact.pages[0].disposition == "tables_detected"
    assert artifact.pages[1].disposition == "quarantined"
    payload: dict[str, object] = {
        "document_version_id": "scoped-pdf",
        "raw_pdf_sha256": artifact.raw_pdf_sha256,
        "artifact_id": artifact_id,
        "artifact_sha256": _sha(artifact.model_dump(mode="json"), ascii_only=False),
        "pages": [
            {
                "page_number": page.page_number,
                "page_sha256": _sha(page.model_dump(mode="json"), ascii_only=False),
                "table_requirement": "required" if page.page_number == 1 else "deferred",
                "replacement_evidence_node_ids": [],
                "replacement_observation_ids": [],
                "rationale": "Content-backed synthetic report review for the exact source page.",
                **({"defer_basis": "outside_report_population"} if page.page_number == 2 else {}),
            }
            for page in artifact.pages
        ],
        "source_population": _population(),
        "reviewer": "synthetic-source-review",
        "reviewed_at": T0.isoformat().replace("+00:00", "Z"),
        "rationale": "This isolated synthetic report requires the first table, not the scanned appendix.",
        "schema_version": "report-pdf-table-scope/v1",
    }
    return ReportPdfTableScope.model_validate({**payload, "scope_sha256": _sha(payload)})


def _recommit(scope: ReportPdfTableScope, **changes: object) -> ReportPdfTableScope:
    payload = {**scope.model_dump(mode="json", exclude={"scope_sha256"}), **changes}
    return ReportPdfTableScope.model_validate({**payload, "scope_sha256": _sha(payload)})


def test_native_scoped_certificate_retains_full_quarantine_and_full_default_rejects(
    database: sqlite3.Connection,
) -> None:
    scope = _seed_scope(database)
    with pytest.raises(DocumentProcessingEvidenceMissingError):
        publish_document_processing_evidence(
            database,
            document_version_id="scoped-pdf",
            processing_lane="pdf_table",
            cutoff_at=T1,
            recorded_at=T2,
        )
    receipt = publish_document_processing_evidence(
        database,
        document_version_id="scoped-pdf",
        processing_lane="pdf_table",
        cutoff_at=T1,
        recorded_at=T2,
        report_pdf_table_scope=scope,
    )
    verified = verify_document_processing_evidence(
        database,
        receipt.evidence_seal_id,
        document_version_id="scoped-pdf",
        processing_lane="pdf_table",
        cutoff_at=T1,
        report_pdf_table_scope=scope,
    )
    assert verified.member_count > 1
    locators = database.execute(
        "SELECT locator_json FROM document_processing_evidence_members WHERE evidence_seal_id=?",
        (receipt.evidence_seal_id,),
    ).fetchall()
    assert {json.loads(str(row[0]))["page_number"] for row in locators} == {1}
    assert (
        database.execute(
            "SELECT disposition FROM pdf_table_extraction_artifact_headers WHERE artifact_id=?",
            (scope.artifact_id,),
        ).fetchone()[0]
        == "quarantined"
    )
    assert (
        database.execute(
            "SELECT COUNT(*) FROM pdf_table_extraction_artifact_members WHERE artifact_id=? AND member_kind='page'",
            (scope.artifact_id,),
        ).fetchone()[0]
        == 2
    )
    header = database.execute(
        "SELECT native_scope_json,adapter_version FROM document_processing_evidence_headers WHERE evidence_seal_id=?",
        (receipt.evidence_seal_id,),
    ).fetchone()
    assert (
        json.loads(str(header[0]))["native"]["certificate_schema"]
        == "report-pdf-table-certificate/v1"
    )
    assert header[1] == "v3-report-pdf-tables@1"
    with pytest.raises(DocumentProcessingEvidenceIntegrityError, match="adapter_identity"):
        verify_document_processing_evidence(
            database,
            receipt.evidence_seal_id,
            document_version_id="scoped-pdf",
            processing_lane="pdf_table",
            cutoff_at=T1,
        )


@pytest.mark.parametrize("failure", ["required_quarantine", "page_sha", "artifact_sha", "raw_sha"])
def test_scoped_certificate_rejects_required_quarantine_or_source_tamper(
    database: sqlite3.Connection,
    failure: str,
) -> None:
    scope = _seed_scope(database)
    payload = scope.model_dump(mode="json", exclude={"scope_sha256"})
    if failure == "required_quarantine":
        pages = [page.model_dump(mode="json") for page in scope.pages]
        pages[1]["table_requirement"] = "required"
        pages[1].pop("defer_basis")
        scope = _recommit(scope, pages=pages)
    elif failure == "page_sha":
        pages = [page.model_dump(mode="json") for page in scope.pages]
        pages[1]["page_sha256"] = "0" * 64
        scope = _recommit(scope, pages=pages)
    else:
        scope = _recommit(
            scope,
            **{
                failure.replace("_sha", "_sha256")
                if failure == "artifact_sha"
                else "raw_pdf_sha256": "0" * 64
            },
        )
    with pytest.raises(
        (DocumentProcessingEvidenceIntegrityError, DocumentProcessingEvidenceMissingError)
    ):
        publish_document_processing_evidence(
            database,
            document_version_id="scoped-pdf",
            processing_lane="pdf_table",
            cutoff_at=T1,
            recorded_at=T2,
            report_pdf_table_scope=scope,
        )
    assert payload["artifact_id"] == scope.artifact_id


def test_independent_source_population_and_all_page_inventory_are_closed(
    database: sqlite3.Connection,
) -> None:
    scope = _seed_scope(database)
    with pytest.raises(ValidationError, match="every page"):
        _recommit(scope, pages=[scope.pages[1].model_dump(mode="json")])
    source = _population()
    source["requirements"] = []
    with pytest.raises(ValidationError):
        MemoReportSourcePopulation.model_validate(source)
    with pytest.raises(ValidationError, match="complete-report"):
        AnalysisScopeRequest(
            purpose="synthetic",
            issuer_id="synthetic",
            inventory_key="synthetic",
            required_period_ends=(T0.date(),),
            cutoff_at=T1,
            observed_through=T2,
            report_pdf_tables=(scope,),
        )


def test_deferred_fulltext_page_and_document_root_cannot_supply_report_claim(
    database: sqlite3.Connection,
) -> None:
    scope = _seed_scope(database)
    analysis = AnalysisEvidenceScope.model_construct(
        request=SimpleNamespace(report_pdf_tables=(scope,))
    )
    snapshot = ResearchSnapshotRequest.model_construct(
        research_universe=SimpleNamespace(analysis_scope=analysis)
    )
    nodes = getattr(pdf_fixtures, "_seed_run")(
        database,
        document_version_id="scoped-pdf",
        blob_sha=scope.raw_pdf_sha256,
        run_id="text-run",
        extractor_name="synthetic-text",
        extractor_code_version="test@1",
        extractor_config_sha256="a" * 64,
        children=tuple(
            (
                "pdf_page",
                f"Source page{page}",
                EvidenceLocator(source_ref="synthetic", page_number=page),
            )
            for page in (1, 2)
        ),
    )
    getattr(briefs, "_verify_report_pdf_node")(database, nodes[1].node_id, snapshot)
    with pytest.raises(briefs.MemoEvidenceError, match="deferred_pdf_page"):
        getattr(briefs, "_verify_report_pdf_node")(database, nodes[2].node_id, snapshot)
    with pytest.raises(briefs.MemoEvidenceError, match="page_unknown"):
        getattr(briefs, "_verify_report_pdf_node")(database, nodes[0].node_id, snapshot)


def test_derived_financial_leaf_cannot_hide_deferred_pdf_source(
    database: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _seed_scope(database)
    snapshot = ResearchSnapshotRequest.model_construct(
        research_universe=SimpleNamespace(
            analysis_scope=SimpleNamespace(request=SimpleNamespace(report_pdf_tables=(scope,)))
        )
    )
    nodes = getattr(pdf_fixtures, "_seed_run")(
        database,
        document_version_id="scoped-pdf",
        blob_sha=scope.raw_pdf_sha256,
        run_id="leaf-run",
        extractor_name="synthetic-text",
        extractor_code_version="test@1",
        extractor_config_sha256="a" * 64,
        children=(
            (
                "pdf_page",
                "Deferred source numeric table",
                EvidenceLocator(source_ref="synthetic", page_number=2),
            ),
        ),
    )
    bundles = {
        "derived": SimpleNamespace(
            evidence=None, derivation=SimpleNamespace(input_observation_ids=("reported",))
        ),
        "reported": SimpleNamespace(
            evidence=SimpleNamespace(evidence_node_id=nodes[1].node_id), derivation=None
        ),
    }

    def source_bundle(self: object, identity: str, *, cutoff: object) -> object:
        return bundles[identity]

    monkeypatch.setattr(briefs.FactReadModel, "provenance_bundle", source_bundle)
    with pytest.raises(briefs.MemoEvidenceError, match="deferred_pdf_page"):
        getattr(briefs, "_verify_report_pdf_observation")(database, "derived", snapshot, cutoff=T1)


def test_v7_native_obligations_select_only_the_exact_page_scope(
    pdf_scope: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    from provenance import population_document_processing as processing
    from provenance.analysis_scope import (
        build_analysis_scope,
        require_analysis_documents,
        verify_analysis_scope,
    )
    from provenance.research_snapshot import (
        DocumentProcessingPolicy,
        DocumentProcessingScope,
        derive_obligations,
    )
    from tests.test_analysis_scope import K

    conn, payload = pdf_scope
    original = build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))
    documents = require_analysis_documents(conn, original, K, K)
    policy = DocumentProcessingPolicy(policy_name="synthetic-report-page-scope", policy_version="1")
    full = derive_obligations(
        conn,
        DocumentProcessingScope(document_version_ids=documents, analysis_scope=original),
        K,
        policy,
        recorded_at=K,
    )
    candidate = _seed_scope(conn)
    raw_sha = conn.execute(
        "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id='issuer-pdf-version'"
    ).fetchone()[0]
    candidate = _recommit(
        candidate, document_version_id="issuer-pdf-version", raw_pdf_sha256=raw_sha
    )
    scoped = build_analysis_scope(
        conn,
        AnalysisScopeRequest.model_validate(
            {**payload, "report_pdf_tables": [candidate.model_dump(mode="json")]}
        ),
    )
    assert scoped.schema_version == "analysis-evidence-scope/v7"
    verify_analysis_scope(conn, scoped)
    exact = derive_obligations(
        conn,
        DocumentProcessingScope(document_version_ids=documents, analysis_scope=scoped),
        K,
        policy,
        recorded_at=K,
    )
    old_pdf = {
        item.processing_obligation_revision_id
        for item in full
        if item.processing_lane == "pdf_table" and item.document_version_id == "issuer-pdf-version"
    }
    new_pdf = {
        item.processing_obligation_revision_id
        for item in exact
        if item.processing_lane == "pdf_table" and item.document_version_id == "issuer-pdf-version"
    }
    assert old_pdf and new_pdf and not old_pdf.intersection(new_pdf)
    selection = json.dumps([candidate.model_dump(mode="json")])
    rows = getattr(processing, "_obligation_rows")(
        conn,
        K,
        K,
        document_version_ids=documents,
        after=None,
        limit=None,
        defer_filing_xbrl=True,
        report_pdf_table_scopes_json=selection,
    )
    selected = {str(row["processing_obligation_revision_id"]) for row in rows}
    assert new_pdf <= selected and not old_pdf.intersection(selected)
    totals = getattr(processing, "_obligation_totals")(
        conn, K, documents, K, defer_filing_xbrl=True, report_pdf_table_scopes_json=selection
    )
    assert totals["total"] == len(rows)
    # The page plan is not table admission. A parent from another document
    # cannot become an eligible certificate just because this scope exists.
    with pytest.raises(DocumentProcessingEvidenceMissingError):
        publish_document_processing_evidence(
            conn,
            document_version_id="issuer-pdf-version",
            processing_lane="pdf_table",
            cutoff_at=K,
            recorded_at=K,
            report_pdf_table_scope=candidate,
        )


def test_incomplete_parent_page_inventory_cannot_claim_scoped_coverage(
    database: sqlite3.Connection,
) -> None:
    scope = _seed_scope(database, incomplete=True)
    with pytest.raises(DocumentProcessingEvidenceIntegrityError, match="scope_mismatch"):
        publish_document_processing_evidence(
            database,
            document_version_id="scoped-pdf",
            processing_lane="pdf_table",
            cutoff_at=T1,
            recorded_at=T2,
            report_pdf_table_scope=scope,
        )
