"""A plain numeric cell does not discard a reviewed table qualifier."""

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from compute.thesis_metric_series import MetricExpression, calculate_metric_series
from pipeline.kpi_semantics import (
    KpiSourcePrecision,
    current_kpi_semantic_context,
    persist_kpi_semantic_context,
)
from tests.fixtures.kpi_revision_setup import NOW, semantic_fixture
from tests.fixtures.kpi_revision_setup import semantic_recording_clock as semantic_recording_clock
from tests.test_thesis_metric_series import kpi_database_fixture

pytestmark = pytest.mark.usefixtures("semantic_recording_clock")


@pytest.mark.parametrize("kind", ["approximate", "lower_bound", "upper_bound", "range"])
def test_numeric_token_with_qualifier_is_unavailable_to_exact_rule(kind: str) -> None:
    conn = kpi_database_fixture()
    try:
        precision = KpiSourcePrecision.model_validate(
            {"kind": kind, "qualifiers": ["Approximate values."]}
        )
        context = semantic_fixture().model_copy(
            update={"source_value_text": "12.5", "source_precision": precision}
        )
        persist_kpi_semantic_context(conn, kpi_fact_id=1, context=context, knowledge_at=NOW)
        result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(operation="level", source="kpi", name="Monthly ARPAC"),
            cutoff=datetime(2026, 10, 3, tzinfo=UTC),
        )
        assert result.status == "unavailable"
        assert result.reason_code == "approximate_kpi_source_value"
        assert "Approximate values." in str(result.source_manifests)
    finally:
        conn.close()


def test_unreviewed_precision_is_not_inferred_from_numeric_token() -> None:
    conn = kpi_database_fixture()
    try:
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=1,
            context=semantic_fixture().model_copy(update={"source_value_text": "12.5"}),
            knowledge_at=NOW,
        )
        result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(operation="level", source="kpi", name="Monthly ARPAC"),
            cutoff=datetime(2026, 10, 3, tzinfo=UTC),
        )
        assert result.reason_code == "kpi_source_precision_unavailable"
    finally:
        conn.close()


@pytest.mark.parametrize("kind", [None, "unknown", "approximate"])
def test_duration_and_precision_round_trip_without_qualifying_an_exact_rule(
    kind: str | None,
) -> None:
    conn = kpi_database_fixture()
    try:
        precision = (
            None
            if kind is None
            else KpiSourcePrecision.model_validate(
                {
                    "kind": kind,
                    "qualifiers": ["Amounts are rounded."] if kind == "approximate" else [],
                }
            )
        )
        context = semantic_fixture().model_copy(
            update={
                "reported_period_start": date(2024, 10, 1),
                "source_value_text": "12.5",
                "source_precision": precision,
            }
        )
        persist_kpi_semantic_context(conn, kpi_fact_id=1, context=context, knowledge_at=NOW)
        stored = current_kpi_semantic_context(conn, kpi_fact_id=1)
        assert stored is not None
        assert stored.context.reported_period_start == date(2024, 10, 1)
        assert stored.context.source_precision == precision
        result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(operation="level", source="kpi", name="Monthly ARPAC"),
            cutoff=datetime(2026, 10, 3, tzinfo=UTC),
        )
        assert result.status == "unavailable"
        assert result.reason_code == (
            "approximate_kpi_source_value"
            if kind == "approximate"
            else "kpi_source_precision_unavailable"
        )
    finally:
        conn.close()


def test_precision_contract_and_historical_payload_hash_shape() -> None:
    assert "source_precision" not in semantic_fixture().model_dump(mode="json")
    with pytest.raises(ValidationError, match="cannot carry"):
        KpiSourcePrecision(kind="exact", qualifiers=("Approximate values.",))
    with pytest.raises(ValidationError, match="requires source qualifiers"):
        KpiSourcePrecision(kind="approximate")


def test_later_precision_review_cannot_supply_a_historical_cutoff() -> None:
    conn = kpi_database_fixture()
    try:
        current = current_kpi_semantic_context(conn, kpi_fact_id=1)
        assert current is not None
        # The historical context has no reviewed precision. A future exact review
        # cannot make the earlier knowledge snapshot available.
        conn.execute("UPDATE kpi_fact_semantic_contexts SET source_precision_json=NULL")
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=1,
            context=current.context,
            reviewed_by="synthetic-reviewer",
            knowledge_at=NOW + timedelta(days=1),
            kpi_definition_revision_id=current.kpi_definition_revision_id,
        )
        result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(operation="level", source="kpi", name="Monthly ARPAC"),
            cutoff=NOW,
        )
        assert result.status == "unavailable"
        assert not result.points
    finally:
        conn.close()


def test_exact_source_token_must_match_the_stored_value() -> None:
    conn = kpi_database_fixture("99")
    try:
        result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(operation="level", source="kpi", name="Monthly ARPAC"),
            cutoff=datetime(2026, 10, 3, tzinfo=UTC),
        )
        assert result.status == "unavailable"
        assert result.reason_code == "kpi_source_numeric_mismatch"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "precision_kind,role,quarantine_reason",
    [
        ("approximate", "current", None),
        ("exact", "current", "source_precision_requires_review"),
        ("exact", "prior_period_comparator", "period_role_prior_period_comparator"),
        ("exact", "guidance", "period_role_guidance"),
    ],
)
def test_original_pdf_extraction_preserves_duration_precision_and_novel_counts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    precision_kind: str,
    role: str,
    quarantine_reason: str | None,
) -> None:
    fitz = pytest.importorskip("fitz", reason="PyMuPDF (fitz) not installed")
    import parser
    from compute import kpi_extract_summaries as summaries
    from models.facts import FactLocator

    qualifier = "Amounts are rounded to the nearest million."
    pdf = tmp_path / "presentation.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Reporting period: 2025-01-01 through 2025-03-31")
        page.insert_text((72, 100), qualifier)
        page.insert_text((72, 140), "Revenue was USD 1200000000.")
        page.insert_text((72, 180), "New active accounts: 118 million.")
        document.save(str(pdf))
    text = parser.extract_text_from_pdf(str(pdf))
    assert qualifier in text
    rows: list[dict[str, object]] = [
        {
            "label": "Revenue",
            "value": 1200000000,
            "unit": "actual",
            "currency": "USD",
            "source_excerpt": "Revenue was USD 1200000000.",
            "reported_period_start": "2025-01-01",
            "reported_period_end": "2025-03-31",
            "period_role": role,
            "accounting_basis": "gaap",
            "consolidation_scope": "consolidated",
            "source_unit_scale": "none",
            "source_precision": {
                "kind": precision_kind,
                "qualifiers": [qualifier] if precision_kind == "approximate" else [],
            },
        },
        {
            "label": "New active accounts",
            "value": 118000000,
            "unit": "count",
            "source_value_text": "118",
            "source_excerpt": "New active accounts: 118 million.",
            "reported_period_end": "2025-03-31",
            "period_role": "current",
            "accounting_basis": "management",
            "consolidation_scope": "consolidated",
            "source_unit_scale": "millions",
        },
    ]

    def model_response(prompt: str, **kwargs: object) -> object:
        assert text in prompt
        assert kwargs["purpose"] == "kpi_summary_enumerate"
        return rows

    monkeypatch.setattr(summaries, "call_llm_structured", model_response)
    from provenance.evidence_ledger import (
        ContentBlob,
        DocumentVersion,
        EvidenceLedger,
        EvidenceNode,
        ExtractionRun,
        SourceObservation,
    )

    with sqlite3.connect(migrated_db(tmp_path / "source-context.sqlite")) as conn:
        conn.row_factory = sqlite3.Row
        payload = pdf.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        stamp = datetime(2025, 4, 1)
        conn.execute(
            "INSERT INTO documents(id,ticker,source_type,doc_type,period_end,file_path,"
            "sha256,fetched_at,fetch_status,raw_bytes_size,source_quality_tier) "
            "VALUES (42,'NU','ir_doc','ir_presentation','2025-03-31',?,?,?,'ok',?,'ir_doc')",
            (str(pdf), digest, stamp, len(payload)),
        )
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ContentBlob(
                sha256=digest,
                byte_size=len(payload),
                media_type="application/pdf",
                storage_uri=pdf.as_uri(),
                recorded_at=stamp,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="r4-pdf",
                idempotency_key="r4-pdf",
                source_kind="ir_document",
                source_url="https://example.test/NU/presentation.pdf",
                blob_sha256=digest,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=stamp,
                retrieved_at=stamp,
                retrieval_config_sha256=digest,
                collector_code_version="synthetic-r4-v1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id="r4-pdf",
                document_key="r4-pdf",
                version_sequence=1,
                observation_id="r4-pdf",
                blob_sha256=digest,
                issuer_id="fixture:NU",
                ticker="NU",
                document_type="ir_presentation",
                form_type="presentation",
                language="en",
                legacy_document_id=42,
                recorded_at=stamp,
            )
        )
        ledger.persist(
            ExtractionRun(
                extraction_run_id="r4-pdf",
                idempotency_key="r4-pdf",
                document_version_id="r4-pdf",
                input_sha256=digest,
                extractor_name="synthetic-r4-pdf",
                extractor_config_sha256=digest,
                extractor_code_version="synthetic-r4-v1",
                output_sha256=hashlib.sha256(text.encode()).hexdigest(),
                started_at=stamp,
                completed_at=stamp,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id="r4-pdf",
                evidence_key="r4-pdf",
                revision=1,
                extraction_run_id="r4-pdf",
                node_kind="document",
                text=text,
                recorded_at=stamp,
            )
        )

        result = summaries.capture_for_ir_pdf_docs(
            "NU", tmp_path, conn, doc_types=("ir_presentation",)
        )
        assert result.error is None
        assert result.kpis_inserted_total == 2
        stored = conn.execute(
            "SELECT facts.id,definition.name,facts.value,facts.source_doc_id,facts.locator "
            "FROM kpi_facts AS facts JOIN kpi_definitions AS definition "
            "ON definition.id=facts.kpi_definition_id ORDER BY facts.id"
        ).fetchall()
        assert {str(row[1]) for row in stored} == {"Revenue", "New active accounts"}
        assert all(row[3] == 42 for row in stored)
        for row in stored:
            locator = FactLocator.from_json(str(row[4]))
            assert locator is not None
            assert locator.pdf_page == 1
            assert locator.verbatim_snippet is not None
            assert locator.verbatim_snippet in text
        revenue_context = current_kpi_semantic_context(conn, kpi_fact_id=int(stored[0][0]))
        assert revenue_context is not None
        assert revenue_context.context.reported_period_start == date(2025, 1, 1)
        assert revenue_context.context.source_precision is not None
        assert revenue_context.context.source_precision.kind == precision_kind
        if quarantine_reason is not None:
            assert revenue_context.context.status.value == "quarantined"
            assert revenue_context.context.reason_code == quarantine_reason
            assert revenue_context.context.source_precision.qualifiers == ()
        else:
            assert revenue_context.context.source_precision.qualifiers == (qualifier,)
        assert Decimal(str(stored[0][2])) == Decimal("1200000000")
        count_context = current_kpi_semantic_context(conn, kpi_fact_id=int(stored[1][0]))
        assert count_context is not None
        assert count_context.context.source_precision is None
        assert count_context.context.reported_period_start is None
        assert count_context.context.source_value_text == "118"
        assert Decimal(str(stored[1][2])) == Decimal("118000000")
        rule_result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(operation="level", source="kpi", name="Revenue"),
            cutoff=datetime(2026, 10, 23, tzinfo=UTC),
        )
        assert rule_result.status == "unavailable"
        assert not rule_result.points
