"""NU historical ratios retain exact IR bytes, scope, definitions and clocks."""

from __future__ import annotations

import hashlib
import io
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import openpyxl
import pypdf
import pytest
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from provenance import nu_historical_backfill as nu
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.fact_read_model import FactReadModel
from provenance.fulltext_backfill import FullTextBackfillRequest, backfill_fulltext_evidence
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerRegistry,
    identifier_candidate_digest,
)
from provenance.population_metric_ontology import (
    ExactSourceAdmissionRequest,
    ExactSourceObservationReview,
    admit_exact_source_observations,
)
from sqlite_runtime import register_sqlite_integrity_functions
from tests.test_source_fact_repository import seed_foundation

CAPTURE = datetime(2026, 9, 1, tzinfo=UTC)
REVIEW = datetime(2030, 1, 1, tzinfo=UTC)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _workbook(sheet_name: str = nu.SHEET) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = sheet_name
    for address, value in {
        "D9": "30/06/2025",
        "B10": "Brazil Consumer Credit Portfolio",
        "B12": "15-90 days NPL",
        "D12": 0.044,
        "B13": "90+ NPL",
        "D13": 0.066,
        "B15": "Includes information of Brazilian operation only.",
    }.items():
        sheet[address] = value
    raw = io.BytesIO()
    book.save(raw)
    return raw.getvalue()


def _deck() -> bytes:
    writer = pypdf.PdfWriter()
    add_object = getattr(writer, "_add_object")  # Synthetic fixture construction only.
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for page_number in range(1, 36):
        page = writer.add_blank_page(width=600, height=800)
        text = {
            15: "Risk Adj. NIM Holdings annualized Q2'25 9.2%",
            16: "Brazil consumer credit card and loans excluding SMEs",
            35: "Non-Performing Loans balance / consumer credit portfolio excluding SMEs",
        }.get(page_number, "Other issuer content")
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 10 Tf 10 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): add_object(font)})}
        )
        page[NameObject("/Contents")] = add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


@pytest.fixture
def database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(migrated_db(tmp_path / "nu-history.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    register_sqlite_integrity_functions(conn)
    seed_foundation(conn)
    registry = IssuerRegistry(conn)
    assertion = IdentifierAssertion(
        assertion_id="nu-cik",
        idempotency_key="nu-cik",
        issuer_id="issuer-1",
        identifier_type="sec_cik",
        identifier_value="1691493",
        normalized_value="0001691493",
        authority="manual",
        effective_at=CAPTURE,
        knowledge_at=CAPTURE,
        recorded_at=CAPTURE,
    )
    registry.persist(assertion)
    registry.persist(
        IdentifierResolution(
            resolution_id="nu-cik-selection",
            idempotency_key="nu-cik-selection",
            resolution_key=assertion.resolution_key,
            revision=1,
            outcome="selected",
            selected_assertion_id=assertion.assertion_id,
            candidate_digest_sha256=identifier_candidate_digest((assertion,)),
            policy_name="synthetic-test",
            policy_version="1",
            policy_config_sha256="a" * 64,
            reason_code="test",
            reason_details=(("fixture", "synthetic"),),
            material_dissent=False,
            effective_at=CAPTURE,
            knowledge_at=CAPTURE,
            recorded_at=CAPTURE,
        )
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def request_for(
    conn: sqlite3.Connection,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sheet_name: str = nu.SHEET,
    source_kind: str = "ir_document",
) -> nu.NuHistoricalBackfillRequest:
    # Synthetic documents exercise graph mechanics; native Windows replay owns parser qualification.
    monkeypatch.setattr(pypdf, "__version__", nu.PDF_FULLTEXT_PYPDF_VERSION)
    refs: dict[str, nu.NuHistoricalSource] = {}
    ledger = EvidenceLedger(conn)
    for key, raw, media, url in (
        (
            "workbook",
            _workbook(sheet_name),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            nu.WORKBOOK_URL,
        ),
        ("deck", _deck(), "application/pdf", nu.DECK_URL),
    ):
        sha = _sha(raw)
        monkeypatch.setattr(nu, "WORKBOOK_SHA" if key == "workbook" else "DECK_SHA", sha)
        path = root / (key + (".xlsx" if key == "workbook" else ".pdf"))
        path.write_bytes(raw)
        ledger.persist(
            ContentBlob(
                sha256=sha,
                byte_size=len(raw),
                media_type=media,
                storage_uri=path.as_uri(),
                recorded_at=CAPTURE,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id=key,
                idempotency_key=key,
                source_kind=source_kind,
                source_url=url,
                blob_sha256=sha,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=CAPTURE,
                retrieved_at=CAPTURE,
                retrieval_config_sha256="a" * 64,
                collector_code_version="synthetic-test.v1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id=key,
                document_key=key,
                version_sequence=1,
                observation_id=key,
                blob_sha256=sha,
                issuer_id="issuer-1",
                ticker="NU",
                document_type="ir_historical_spreadsheet"
                if key == "workbook"
                else "ir_presentation",
                form_type="IR",
                period_end=datetime(2025, 6, 30, tzinfo=UTC),
                language="en",
                recorded_at=CAPTURE,
            )
        )
        conn.commit()
        extraction = backfill_fulltext_evidence(
            conn,
            FullTextBackfillRequest(
                repo_root=root,
                content_roots=(root,),
                source_lane="evidence_native",
                document_version_id=key,
                apply=True,
            ),
        )
        row = conn.execute(
            "SELECT extraction_run_id FROM evidence_extraction_runs WHERE document_version_id=?",
            (key,),
        ).fetchone()
        assert row is not None, extraction.model_dump_json()
        refs[key] = nu.NuHistoricalSource(document_version_id=key, fulltext_run_id=str(row[0]))
    return nu.NuHistoricalBackfillRequest(
        **refs, content_roots=(root,), reviewed_by="synthetic-reviewer", reviewed_at=REVIEW
    )


@pytest.mark.parametrize("source_kind", ["ir_doc", "ir_document"])
def test_exact_native_ir_publication_and_separate_semantic_admission(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    request = request_for(database, tmp_path, monkeypatch, source_kind=source_kind)
    before = database.total_changes
    database.execute("PRAGMA query_only=ON")
    plan = nu.prepare_nu_historical_backfill(database, request)
    assert database.total_changes == before
    database.execute("PRAGMA query_only=OFF")
    assert plan.canonical_admission == "not_performed"
    assert [item.observation.numeric_value for item in plan.publication.reported_facts] == [
        "0.092",
        "0.044",
        "0.066",
    ]
    receipt = nu.publish_nu_historical_backfill(
        database, request, expected_plan_sha256=plan.plan_sha256
    )
    assert len(receipt.observation_ids) == 3
    assert database.in_transaction
    path = database.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path) as independent_reader:
        assert (
            independent_reader.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone()[0]
            == 0
        )
    assert nu.publish_nu_historical_backfill(
        database, request, expected_plan_sha256=plan.plan_sha256
    ).exact_replay
    reader = FactReadModel(database)
    reviews: list[ExactSourceObservationReview] = []
    for fact in plan.publication.reported_facts:
        bundle = reader.provenance_bundle(fact.observation.observation_id, cutoff=REVIEW)
        assert bundle.evidence is not None
        assert (
            bundle.cell.accounting_basis == "management"
            and bundle.cell.consolidation_scope == "other"
        )
        assert bundle.cell.unit_key == "pure" and bundle.cell.currency is None
        lineage = bundle.evidence.source_locator.root
        assert lineage["source_kind"] == source_kind
        assert lineage["historical_public_witness_admission"] == "not_performed"
        if bundle.cell.period_kind == "instant":
            assert (
                "Brazil" in bundle.cell.concept_name
                and "excluding SMEs" in bundle.cell.concept_name
            )
            assert lineage["consolidated_npl_comparability"] == "not_comparable"
        else:
            assert (
                "annualized" in bundle.cell.concept_name and "Holdings" in bundle.cell.concept_name
            )
        reviews.append(
            ExactSourceObservationReview(
                observation_id=bundle.observation.observation_id,
                document_version_id=bundle.evidence.document_version_id,
                subject_binding_revision_id=bundle.evidence.subject_binding_revision_id,
                observation_payload_sha256=bundle.observation_payload_sha256,
                source_locator_sha256=bundle.evidence.source_locator_sha256,
                fact_cell_semantic_key_sha256=str(bundle.cell.semantic_key_sha256),
            )
        )
    admitted = admit_exact_source_observations(
        database,
        ExactSourceAdmissionRequest(
            observations=tuple(reviews),
            reviewer_identity=request.reviewed_by,
            review_evidence={"plan_sha256": plan.plan_sha256},
            knowledge_cutoff=REVIEW,
            operation_recorded_at=REVIEW,
        ),
    )
    assert (
        len(admitted.canonical_metric_cell_ids)
        == len(admitted.canonical_resolution_revision_ids)
        == 3
    )


@pytest.mark.parametrize("failure", ["hash", "raw_bytes", "run", "cutoff", "plan"])
def test_source_or_plan_changes_fail_before_publication(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    request = request_for(database, tmp_path, monkeypatch)
    plan = nu.prepare_nu_historical_backfill(database, request)
    if failure == "hash":
        monkeypatch.setattr(nu, "WORKBOOK_SHA", "0" * 64)
    elif failure == "raw_bytes":
        (tmp_path / "workbook.xlsx").write_bytes(b"changed")
    elif failure == "run":
        request = request.model_copy(
            update={
                "workbook": request.workbook.model_copy(update={"fulltext_run_id": "wrong-run"})
            }
        )
    elif failure == "cutoff":
        request = request.model_copy(update={"reviewed_at": datetime(2026, 8, 1, tzinfo=UTC)})
    before = database.total_changes
    with pytest.raises((ValueError, RuntimeError)):
        nu.publish_nu_historical_backfill(
            database,
            request,
            expected_plan_sha256="0" * 64 if failure == "plan" else plan.plan_sha256,
        )
    assert database.total_changes == before
    assert database.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone()[0] == 0


def test_renamed_sheet_does_not_use_a_fuzzy_alias(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = request_for(database, tmp_path, monkeypatch, sheet_name="NPLs")
    with pytest.raises(ValueError, match="exact page or worksheet row"):
        nu.prepare_nu_historical_backfill(database, request)
