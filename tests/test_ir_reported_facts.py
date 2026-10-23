"""Synthetic, migrated-schema checks for reviewed IR PDF source facts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.fact_read_model import FactReadModel
from provenance.ir_reported_facts import (
    IrCandidateRetentionRequest,
    IrRawExchange,
    IrRawRow,
    IrReportedCandidate,
    IrReportedPublicationRequest,
    IrReviewedCandidate,
    IrSourceContextReview,
    IrSourceDimension,
    IrSourceWitness,
    find_exact_ir_source_review,
    persist_ir_source_context_review,
    publish_ir_reported_facts,
    retain_ir_pdf_candidates,
)
from provenance.source_fact_repository import SourceFactRepository
from tests.test_source_fact_repository import STAMP, seed_foundation

PDF_BYTES = b"synthetic IR PDF fixture bytes"
PAGE = (
    "Three months ended June 30, 2026. Prior year ended June 30, 2025. "
    "Management basis. Segment North. USD millions. "
    "USD actual. Margin North 10%. Margin North 8%. Margin 20%. Margin 30. "
    "People 3.6 billion people. Margin €3.6 million."
)
END = datetime(2026, 6, 30, tzinfo=UTC)
KNOWLEDGE = STAMP + timedelta(days=1)
CONFIG_SHA = hashlib.sha256(b"fixture extractor configuration").hexdigest()


def page_texts(raw: bytes) -> tuple[str, ...]:
    return (PAGE,)


def sha(value: bytes | str) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def payload_sha(value: object) -> str:
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def add_ir_document(
    conn: sqlite3.Connection,
    *,
    source_bytes: bytes = PDF_BYTES,
    version_id: str = "ir-document-v1",
    source_id: str = "ir-source-v1",
    version_sequence: int = 1,
    replaces: str | None = None,
) -> str:
    digest = sha(source_bytes)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(source_bytes),
            media_type="application/pdf",
            storage_uri=f"file:///synthetic/{version_id}.pdf",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=source_id,
            idempotency_key=source_id,
            source_kind="ir_doc",
            source_url="https://example.test/issuer/supplement.pdf",
            blob_sha256=digest,
            source_published_at=STAMP,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=sha("fixture retrieval"),
            collector_code_version="fixture-v1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=version_id,
            document_key="ir-supplement-document",
            version_sequence=version_sequence,
            observation_id=source_id,
            blob_sha256=digest,
            issuer_id="issuer-1",
            ticker="TEST",
            document_type="ir_supplement",
            form_type="ir_supplement",
            period_end=END,
            as_of_at=STAMP,
            language="en",
            replaces_document_version_id=replaces,
            recorded_at=STAMP,
        )
    )
    conn.commit()
    return digest


@pytest.fixture
def conn(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Generator[sqlite3.Connection, None, None]:
    path = tmp_path / "ir-reported-facts.db"
    migrated_db(path)
    database = sqlite3.connect(path)
    database.execute("PRAGMA foreign_keys=ON")
    seed_foundation(database)
    database.commit()
    add_ir_document(database)
    try:
        yield database
    finally:
        database.close()


def candidate(
    quote: str,
    token: str,
    value: str,
    *,
    period_start: datetime | None = datetime(2026, 4, 1, tzinfo=UTC),
    period_end: datetime = END,
    fiscal_year: int = 2026,
    scope: str = "segment",
    unit: str = "percent",
    currency: str | None = None,
    source_unit_scale: str = "none",
    source_unit_text: str = "%",
    raw_label: str = "Margin",
    supersedes: str | None = None,
) -> IrReportedCandidate:
    return IrReportedCandidate.model_validate(
        {
            "source_row_key": "pending",
            "page_number": 1,
            "source_quote": quote,
            "raw_label": raw_label,
            "full_wording": quote,
            "source_value_text": token,
            "numeric_value": Decimal(value),
            "period_kind": "duration",
            "period_start": period_start,
            "period_end": period_end,
            "fiscal_year": fiscal_year,
            "fiscal_period": "Q2",
            "accounting_basis": "management",
            "consolidation_scope": scope,
            "dimensions": (
                (
                    IrSourceDimension(
                        axis_namespace="urn:fixture:issuer-axis",
                        axis_name="Geography",
                        member_label="North",
                    ),
                )
                if scope == "segment"
                else ()
            ),
            "unit_key": unit,
            "source_unit_scale": source_unit_scale,
            "source_unit_text": source_unit_text,
            "currency": currency,
            "supersedes_observation_id": supersedes,
        }
    )


def stage(
    conn: sqlite3.Connection,
    *facts: IrReportedCandidate,
    source_sha256: str = sha(PDF_BYTES),
    document_version_id: str = "ir-document-v1",
    raw_pdf_bytes: bytes = PDF_BYTES,
    completed_at: datetime = STAMP,
    llm_call_id: int = 1,
    raw_period_role: str = "current",
) -> tuple[IrCandidateRetentionRequest, tuple[str, ...]]:
    raw_rows: list[dict[str, object]] = [
        {
            "label": fact.raw_label,
            "source_excerpt": fact.source_quote,
            "source_value_text": fact.source_value_text,
            "value": str(fact.numeric_value),
            "period_role": raw_period_role,
        }
        for fact in facts
    ]
    raw_response = json.dumps(raw_rows)
    response_sha = sha(raw_response)
    retention = IrCandidateRetentionRequest(
        document_version_id=document_version_id,
        source_sha256=source_sha256,
        extractor_name="fixture-ir-extractor",
        extractor_code_version="fixture-v1",
        extractor_config_sha256=CONFIG_SHA,
        extraction_started_at=STAMP,
        extraction_completed_at=completed_at,
        responses=(
            IrRawExchange(
                raw_response=raw_response,
                response_sha256=response_sha,
                prompt_sha256=sha("prompt"),
                model="fixture",
                provider="fixture",
                transport="fixture",
                llm_call_id=llm_call_id,
            ),
        ),
        rows=tuple(
            IrRawRow(
                page_number=1,
                response_sha256=response_sha,
                response_row_ordinal=index,
                raw_row=row,
            )
            for index, row in enumerate(raw_rows)
        ),
    )
    receipt = retain_ir_pdf_candidates(conn, retention, raw_pdf_bytes=raw_pdf_bytes)
    return retention, receipt.candidate_node_ids


def witnesses(fact: IrReportedCandidate) -> tuple[IrSourceWitness, ...]:
    period_header = (
        "Prior year ended June 30, 2025"
        if fact.fiscal_year == 2025
        else "Three months ended June 30, 2026"
    )
    fields = [
        "source_definition",
        "period_kind",
        "period_start",
        "period_end",
        "fiscal_year",
        "fiscal_period",
        "unit_key",
        "source_unit_scale",
        "accounting_basis",
        "consolidation_scope",
    ]
    if fact.currency is not None:
        fields.append("currency")
    if fact.dimensions:
        fields.append("dimensions")
    return tuple(
        IrSourceWitness.model_validate(
            {
                "field_name": field,
                "page_number": 1,
                "exact_quote": (
                    fact.source_quote
                    if field == "source_unit_scale" and fact.source_unit_text in fact.source_quote
                    else fact.source_unit_text
                    if field == "source_unit_scale"
                    else period_header
                ),
            }
        )
        for field in fields
    )


def reviewed_request(
    conn: sqlite3.Connection,
    *facts: IrReportedCandidate,
    source_sha256: str = sha(PDF_BYTES),
    document_version_id: str = "ir-document-v1",
    raw_pdf_bytes: bytes = PDF_BYTES,
    knowledge_at: datetime = KNOWLEDGE,
    raw_period_role: str = "current",
) -> IrReportedPublicationRequest:
    retention, ids = stage(
        conn,
        *facts,
        source_sha256=source_sha256,
        document_version_id=document_version_id,
        raw_pdf_bytes=raw_pdf_bytes,
        raw_period_role=raw_period_role,
    )
    run_id = str(
        conn.execute(
            "SELECT extraction_run_id FROM evidence_nodes WHERE node_id=?", (ids[0],)
        ).fetchone()[0]
    )
    output_sha = str(
        conn.execute(
            "SELECT output_sha256 FROM evidence_extraction_runs WHERE extraction_run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    population = retain_ir_pdf_candidates(conn, retention, raw_pdf_bytes=raw_pdf_bytes)
    approved = tuple(
        fact.model_copy(update={"source_row_key": node_id})
        for fact, node_id in zip(facts, ids, strict=True)
    )
    entries = tuple(
        IrReviewedCandidate(
            candidate_node_id=node_id,
            disposition="accepted",
            fact=fact,
            witnesses=witnesses(fact),
        )
        for node_id, fact in zip(ids, approved, strict=True)
    )
    review_payload = {
        "schema_version": "ir-pdf-source-context-review.v1",
        "document_version_id": document_version_id,
        "source_sha256": source_sha256,
        "extraction_run_id": run_id,
        "extractor_config_sha256": CONFIG_SHA,
        "candidate_population_sha256": output_sha,
        "reviewer_identity": "fixture-human-reviewer",
        "review_method": "manual_source_context_review",
        "reviewed_at": (STAMP + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "review_evidence": "fixture header, notes, and source row inspected",
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }
    review = IrSourceContextReview.model_validate(
        {
            **review_payload,
            "review_sha256": payload_sha(review_payload),
        }
    )
    review_id = persist_ir_source_context_review(
        conn, review, population, raw_pdf_bytes=raw_pdf_bytes
    )
    assert find_exact_ir_source_review(conn, population, raw_pdf_bytes=raw_pdf_bytes) == (
        review_id,
        review,
    )
    return IrReportedPublicationRequest(
        document_version_id=document_version_id,
        source_sha256=source_sha256,
        subject_binding_revision_id="binding-1",
        reporting_entity_id="reporting-1",
        concept_namespace="urn:fixture:issuer-source-label",
        extractor_name="fixture-ir-extractor",
        extractor_code_version="fixture-v1",
        extractor_config_sha256=CONFIG_SHA,
        candidate_population_sha256=output_sha,
        review_node_id=review_id,
        extraction_started_at=STAMP,
        extraction_completed_at=STAMP,
        knowledge_at=knowledge_at,
        recorded_at=knowledge_at,
        candidates=approved,
    )


def test_unreviewed_candidates_remain_outside_fact_read_model(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    fact = candidate("Margin North 10%", "10%", "10")
    retention, ids = stage(conn, fact)
    population = retain_ir_pdf_candidates(conn, retention, raw_pdf_bytes=PDF_BYTES)
    assert population.exact_replay
    assert ids == population.candidate_node_ids
    assert find_exact_ir_source_review(conn, population, raw_pdf_bytes=PDF_BYTES) is None
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
    ).fetchone() == (1,)


@pytest.mark.parametrize("raw_period_role", ["guidance", "unknown"])
def test_guidance_and_unknown_forecast_cannot_be_reviewed_as_reported_actual(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    raw_period_role: str,
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    with pytest.raises(ValueError, match="guidance or unresolved forecast"):
        reviewed_request(
            conn,
            candidate("Margin North 10%", "10%", "10"),
            raw_period_role=raw_period_role,
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
    ).fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (0,)


def test_reviewed_prior_comparator_keeps_its_own_reported_period(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    publication = reviewed_request(
        conn,
        candidate(
            "Margin North 8%",
            "8%",
            "8",
            period_start=datetime(2025, 4, 1, tzinfo=UTC),
            period_end=datetime(2025, 6, 30, tzinfo=UTC),
            fiscal_year=2025,
        ),
        raw_period_role="prior_year_comparator",
    )
    receipt = publish_ir_reported_facts(conn, publication, raw_pdf_bytes=PDF_BYTES)
    assert len(receipt.cell_ids) == 1
    assert FactReadModel(conn).as_reported(receipt.cell_ids[0], cutoff=publication.knowledge_at)
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone() == (0,)


def test_same_label_distinct_period_scope_unit_and_changed_clock_replay(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    publication = reviewed_request(
        conn,
        candidate("Margin North 10%", "10%", "10"),
        candidate(
            "Margin North 8%",
            "8%",
            "8",
            period_start=datetime(2025, 4, 1, tzinfo=UTC),
            period_end=datetime(2025, 6, 30, tzinfo=UTC),
            fiscal_year=2025,
        ),
        candidate("Margin 20%", "20%", "20", scope="consolidated"),
        candidate(
            "Margin 30", "30", "30", unit="actual", currency="USD", source_unit_text="USD actual"
        ),
    )
    first = publish_ir_reported_facts(conn, publication, raw_pdf_bytes=PDF_BYTES)
    later = publication.model_copy(
        update={
            "extraction_started_at": STAMP + timedelta(hours=2),
            "extraction_completed_at": STAMP + timedelta(hours=3),
            "knowledge_at": KNOWLEDGE + timedelta(days=1),
            "recorded_at": KNOWLEDGE + timedelta(days=1),
        }
    )
    second = publish_ir_reported_facts(conn, later, raw_pdf_bytes=PDF_BYTES)
    assert len(first.cell_ids) == len(set(first.cell_ids)) == 4
    assert len(first.observation_ids) == 4
    assert not first.exact_replay and second.exact_replay
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM fact_resolution_revisions_v2").fetchone() == (0,)
    reader = FactReadModel(conn)
    for cell_id, observation_id in zip(first.cell_ids, first.observation_ids, strict=True):
        assert len(reader.as_reported(cell_id, cutoff=later.knowledge_at)) == 1
        bundle = reader.provenance_bundle(observation_id, cutoff=later.knowledge_at)
        assert bundle.evidence is not None
        assert reader.current_resolved(cell_id, cutoff=later.knowledge_at) is None


def test_reviewed_count_scale_and_monetary_scale_follow_existing_semantics(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    publication = reviewed_request(
        conn,
        candidate(
            "People 3.6 billion people",
            "3.6",
            "3600000000",
            raw_label="People",
            unit="count",
            source_unit_scale="billions",
            source_unit_text="billion people",
        ),
        candidate(
            "Margin €3.6 million",
            "€3.6",
            "3.6",
            unit="millions",
            currency="EUR",
            source_unit_scale="millions",
            source_unit_text="million",
        ),
    )
    receipt = publish_ir_reported_facts(conn, publication, raw_pdf_bytes=PDF_BYTES)
    rows = conn.execute(
        "SELECT numeric_value,raw_lexical_value,source_locator_json "
        "FROM fact_observations_v2 WHERE observation_id IN (?,?) ORDER BY numeric_value DESC",
        receipt.observation_ids,
    ).fetchall()
    assert rows[0][0:2] == ("3600000000", "3.6")
    assert json.loads(str(rows[0][2]))["source_unit_scale"] == "billions"
    assert json.loads(str(rows[0][2]))["source_unit_text"] == "billion people"
    assert rows[1][0:2] == ("3.6", "€3.6")
    assert json.loads(str(rows[1][2]))["source_unit_scale"] == "millions"
    reader = FactReadModel(conn)
    assert all(
        reader.as_reported(cell_id, cutoff=publication.knowledge_at) for cell_id in receipt.cell_ids
    )


@pytest.mark.parametrize(
    ("unit", "scale"),
    [("count", "unknown"), ("percent", "billions"), ("millions", "none")],
)
def test_unreviewable_or_incompatible_source_scale_fails_closed(unit: str, scale: str) -> None:
    with pytest.raises(ValueError):
        candidate(
            "Margin North 10%",
            "10%",
            "10",
            unit=unit,
            currency="USD" if unit == "millions" else None,
            source_unit_scale=scale,
        )


def test_correction_retains_prior_observation(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    original = publish_ir_reported_facts(
        conn,
        reviewed_request(conn, candidate("Margin North 10%", "10%", "10")),
        raw_pdf_bytes=PDF_BYTES,
    )
    corrected_bytes = b"synthetic corrected IR PDF fixture bytes"
    corrected_sha = add_ir_document(
        conn,
        source_bytes=corrected_bytes,
        version_id="ir-document-v2",
        source_id="ir-source-v2",
        version_sequence=2,
        replaces="ir-document-v1",
    )
    correction = reviewed_request(
        conn,
        candidate("Margin North 8%", "8%", "8", supersedes=original.observation_ids[0]),
        source_sha256=corrected_sha,
        document_version_id="ir-document-v2",
        raw_pdf_bytes=corrected_bytes,
        knowledge_at=KNOWLEDGE + timedelta(days=1),
    )
    receipt = publish_ir_reported_facts(conn, correction, raw_pdf_bytes=corrected_bytes)
    assert receipt.cell_ids == original.cell_ids
    assert receipt.observation_ids != original.observation_ids
    assert conn.execute(
        "SELECT revision_kind,supersedes_observation_id FROM fact_observations_v2 WHERE observation_id=?",
        (receipt.observation_ids[0],),
    ).fetchone() == ("correction", original.observation_ids[0])


@pytest.mark.parametrize("failure", ["binding", "quote", "source", "numeric", "parent"])
def test_invalid_publication_rolls_back_financial_facts(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    parent = "missing-parent" if failure == "parent" else None
    publication = reviewed_request(
        conn, candidate("Margin North 10%", "10%", "10", supersedes=parent)
    )
    raw = PDF_BYTES
    if failure == "binding":
        publication = publication.model_copy(
            update={"subject_binding_revision_id": "wrong-binding"}
        )
    elif failure == "quote":
        row = publication.candidates[0].model_copy(update={"source_quote": "Margin North 99%"})
        publication = publication.model_copy(update={"candidates": (row,)})
    elif failure == "source":
        raw = b"wrong source bytes"
    elif failure == "numeric":
        row = publication.candidates[0].model_copy(update={"numeric_value": Decimal("99")})
        publication = publication.model_copy(update={"candidates": (row,)})
    with pytest.raises(ValueError):
        publish_ir_reported_facts(conn, publication, raw_pdf_bytes=raw)
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publications").fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
    ).fetchone() == (1,)


def test_publication_failure_preserves_retained_evidence(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    publication = reviewed_request(conn, candidate("Margin North 10%", "10%", "10"))

    def fail_publish(self: SourceFactRepository, publication: object) -> None:
        raise ValueError("synthetic publication failure")

    monkeypatch.setattr(SourceFactRepository, "publish", fail_publish)
    with pytest.raises(ValueError, match="synthetic publication failure"):
        publish_ir_reported_facts(conn, publication, raw_pdf_bytes=PDF_BYTES)
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
    ).fetchone() == (1,)


def test_review_rejects_missing_field_witness(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    fact = candidate("Margin North 10%", "10%", "10")
    _, ids = stage(conn, fact)
    approved = fact.model_copy(update={"source_row_key": ids[0]})
    with pytest.raises(ValueError, match="field-specific source witnesses"):
        IrReviewedCandidate(
            candidate_node_id=ids[0],
            disposition="accepted",
            fact=approved,
            witnesses=(
                IrSourceWitness(
                    field_name="period_end", page_number=1, exact_quote="June 30, 2026"
                ),
            ),
        )
