"""Real capture/matcher/publication/ontology/reader stages on a migrated DB."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dcf.cashflow_inputs import requirements_for
from dcf.input_evidence import ModelInputRequest
from pipeline import sec_xbrl
from pipeline.sec_xbrl import FetchedCompanyFacts, ingest_for_ticker
from provenance.companyfacts_statement_continuation import (
    CompanyFactsStatementContinuationRequest,
    ReviewedCompanyFactsStatementFact,
    ReviewedRawCompanyFactsStatementFact,
    continue_companyfacts_statements,
)
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.fact_read_model import FactReadModel
from provenance.financial_statement_admission import FinancialStatementContextReview
from provenance.legacy_fact_evidence_match import CompanyFactsRelocatedLocator
from provenance.metric_ontology import canonical_json
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntity,
    ReportingEntityRegistry,
)
from provenance.sec_companyfacts_capture import parse_companyfacts_body
from sources.report_financials import read_financial_table
from tests.test_sec_companyfacts_capture import (
    ACCESSION_ONE,
    CIK,
    ISSUER_ID,
    SOURCE_URL,
    STAMP,
    synthetic_companyfacts_body,
    synthetic_companyfacts_database,
)


def _sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _setup(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    filing_accession: str = ACCESSION_ONE,
    raw_body: bytes | None = None,
) -> tuple[sqlite3.Connection, CompanyFactsStatementContinuationRequest]:
    conn = synthetic_companyfacts_database(tmp_path, migrated_db)
    raw = raw_body or synthetic_companyfacts_body()
    monkeypatch.setitem(sec_xbrl.CIK_MAP, "ACME", CIK)

    def fetch(_cik: str) -> FetchedCompanyFacts:
        return FetchedCompanyFacts(
            source_url=SOURCE_URL,
            raw_body=raw,
            observed_at=STAMP - timedelta(seconds=1),
            retrieved_at=STAMP,
        )

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", fetch)
    assert ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path).facts_inserted == 2
    clock = datetime.now(UTC) + timedelta(seconds=1)
    registry = ReportingEntityRegistry(conn)
    registry.persist(
        ReportingEntity(
            reporting_entity_id="reporting-acme",
            idempotency_key="reporting-acme",
            issuer_id=ISSUER_ID,
            reporting_entity_kind="legal_registrant",
            display_name="Synthetic ACME",
            created_at=STAMP,
        )
    )
    registry.persist(
        EvidenceSubjectBindingRevision(
            binding_revision_id="acme-subject",
            idempotency_key="acme-subject",
            recorded_issuer_id=ISSUER_ID,
            issuer_id=ISSUER_ID,
            reporting_entity_id="reporting-acme",
            revision=1,
            outcome="selected",
            decision_kind="deterministic",
            material_dissent=False,
            reason_code="synthetic_exact_subject",
            reason_details=(("fixture", "synthetic-source"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    wording = "Combined carve-out statements of income"
    heading_bytes = wording.encode()
    heading_path = tmp_path / "filing.htm"
    heading_path.write_bytes(heading_bytes)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=_sha(heading_bytes),
            byte_size=len(heading_bytes),
            media_type="text/html",
            storage_uri=heading_path.as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="filing-source",
            idempotency_key="filing-source",
            source_kind="sec_edgar",
            source_url=f"https://www.sec.gov/Archives/edgar/data/1/{filing_accession.replace('-', '')}/filing.htm",
            blob_sha256=_sha(heading_bytes),
            source_published_at=STAMP,
            filing_at=STAMP,
            accepted_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=_sha("synthetic-filing"),
            collector_code_version="fixture-1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="filing-context",
            document_key="filing-context",
            version_sequence=1,
            observation_id="filing-source",
            blob_sha256=_sha(heading_bytes),
            issuer_id=ISSUER_ID,
            ticker="ACME",
            document_type="regulatory_filing",
            form_type="10-K",
            accession_number=filing_accession,
            language="en",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id="heading-run",
            idempotency_key="heading-run",
            document_version_id="filing-context",
            input_sha256=_sha(heading_bytes),
            extractor_name="synthetic-heading",
            extractor_config_sha256=_sha("heading-config"),
            extractor_code_version="1",
            output_sha256=_sha(wording),
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    locator = EvidenceLocator(source_ref="/statement/heading")
    ledger.persist(
        EvidenceNode(
            node_id="heading-node",
            evidence_key="heading-node",
            revision=1,
            extraction_run_id="heading-run",
            node_kind="section",
            text=wording,
            locator=locator,
            locator_sha256=locator.canonical_sha256,
            recorded_at=STAMP,
        )
    )
    match = conn.execute(
        "SELECT match_revision_id FROM legacy_fact_evidence_match_revisions WHERE fact_row_id=(SELECT id FROM financial_facts WHERE fiscal_period_type='FY')"
    ).fetchone()
    context = FinancialStatementContextReview(
        document_version_id="filing-context",
        document_sha256=_sha(heading_bytes),
        issuer_id=ISSUER_ID,
        reporting_entity_id="reporting-acme",
        evidence_node_id="heading-node",
        evidence_locator_sha256=locator.canonical_sha256,
        source_wording=wording,
        accounting_basis="us_gaap",
        consolidation_scope="other",
        source_scope_label="combined_carve_out",
        period_start=datetime(2025, 1, 1, tzinfo=UTC),
        period_end=datetime(2025, 12, 31, tzinfo=UTC),
        fiscal_year=2025,
        fiscal_period="FY",
        reviewer="synthetic-analyst",
        reviewed_at=clock,
        rationale="The exact retained filing heading identifies these combined carve-out statements.",
    )
    conn.commit()
    return conn, CompanyFactsStatementContinuationRequest(
        facts=(
            ReviewedCompanyFactsStatementFact(
                match_revision_id=str(match[0]), concept="revenue", context=context
            ),
        ),
        recorded_at=clock,
        apply=True,
    )


def test_companyfacts_capture_reaches_reader_without_rewriting_legacy(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, request = _setup(tmp_path, migrated_db, monkeypatch)
    try:
        original = [
            tuple(row)
            for row in conn.execute("SELECT * FROM reported_observations ORDER BY observation_id")
        ]
        planned = continue_companyfacts_statements(
            conn, request.model_copy(update={"apply": False})
        )
        assert planned.mode == "dry_run"
        assert read_financial_table(conn, "ACME", as_of=request.recorded_at).cells == ()
        result = continue_companyfacts_statements(conn, request)
        table = read_financial_table(conn, "ACME", as_of=request.recorded_at)
        assert len(table.cells) == 1
        cell = table.cells[0]
        assert cell.available, cell.reason_codes
        assert cell.source_scope_label == "combined_carve_out"
        assert cell.provenance is not None and cell.provenance.observation.decimal_value == 100
        assert cell.provenance.evidence is not None
        assert cell.provenance.evidence.document_version_id == result.source_document_version_id
        assert [
            tuple(row)
            for row in conn.execute("SELECT * FROM reported_observations ORDER BY observation_id")
        ] == original
        counts = tuple(
            conn.execute(
                "SELECT (SELECT COUNT(*) FROM fact_observations_v2),(SELECT COUNT(*) FROM canonical_metric_definition_revisions),(SELECT COUNT(*) FROM fact_cell_canonical_binding_revisions)"
            ).fetchone()
        )
        replay = continue_companyfacts_statements(
            conn,
            request.model_copy(update={"recorded_at": request.recorded_at + timedelta(days=1)}),
        )
        assert replay == result
        assert (
            tuple(
                conn.execute(
                    "SELECT (SELECT COUNT(*) FROM fact_observations_v2),(SELECT COUNT(*) FROM canonical_metric_definition_revisions),(SELECT COUNT(*) FROM fact_cell_canonical_binding_revisions)"
                ).fetchone()
            )
            == counts
        )
    finally:
        conn.close()


@pytest.mark.parametrize("change", ["period", "accession", "blob", "missing_blob"])
def test_companyfacts_context_failure_has_no_admitted_facts(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    conn, request = _setup(
        tmp_path,
        migrated_db,
        monkeypatch,
        filing_accession="0000000001-26-000099" if change == "accession" else ACCESSION_ONE,
    )
    try:
        fact = request.facts[0]
        if change == "period":
            fact = fact.model_copy(
                update={
                    "context": fact.context.model_copy(
                        update={"period_start": datetime(2025, 2, 1, tzinfo=UTC)}
                    )
                }
            )
            request = request.model_copy(update={"facts": (fact,)})
        elif change in {"blob", "missing_blob"}:
            # Use the actual capture ledger location, never a guessed blob path.
            from urllib.parse import urlparse
            from urllib.request import url2pathname

            row = conn.execute(
                "SELECT blob.storage_uri FROM evidence_content_blobs blob JOIN evidence_document_versions doc ON doc.blob_sha256=blob.sha256 WHERE doc.document_type='companyfacts_snapshot'"
            ).fetchone()
            path = Path(url2pathname(urlparse(str(row[0])).path))
            if change == "missing_blob":
                path.unlink()
            else:
                path.write_bytes(b"corrupt")
        with pytest.raises(ValueError):
            continue_companyfacts_statements(conn, request)
        assert conn.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone()[0] == 0
        assert read_financial_table(conn, "ACME", as_of=request.recorded_at).cells == ()
    finally:
        conn.close()


@pytest.mark.parametrize("change", ["none", "hash", "path", "accession", "period", "role"])
def test_raw_ytd_entry_is_preserved_without_a_legacy_quarter_match(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    payload = json.loads(synthetic_companyfacts_body())
    payload["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"] = {
        "label": "Operating cash flow",
        "description": "Reported cash flow from operating activities.",
        "units": {
            "USD": [
                {
                    "start": "2025-01-01",
                    "end": "2025-06-30",
                    "val": 60,
                    "accn": ACCESSION_ONE,
                    "fy": 2025,
                    "fp": "Q2",
                    "form": "10-Q",
                    "filed": "2025-08-01",
                }
            ]
        },
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    conn, original = _setup(tmp_path, migrated_db, monkeypatch, raw_body=raw)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM financial_facts WHERE line_item='operating_cash_flow'"
            ).fetchone()[0]
            == 0
        )
        snapshot_id = str(
            conn.execute(
                "SELECT document_version_id FROM evidence_document_versions WHERE document_type='companyfacts_snapshot'"
            ).fetchone()[0]
        )
        entry = (
            parse_companyfacts_body(raw, expected_cik=CIK)
            .facts["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]
            .units["USD"][0]
        )
        context = original.facts[0].context.model_copy(
            update={"period_end": datetime(2025, 6, 30, tzinfo=UTC), "fiscal_period": "YTD"}
        )
        locator = CompanyFactsRelocatedLocator(
            accession_number=ACCESSION_ONE,
            namespace="us-gaap",
            concept="NetCashProvidedByUsedInOperatingActivities",
            unit="USD",
            entry_index=0,
            json_path="facts.us-gaap.NetCashProvidedByUsedInOperatingActivities.units.USD[0]",
        )
        fact = ReviewedRawCompanyFactsStatementFact(
            snapshot_document_version_id=snapshot_id,
            locator=locator,
            source_entry_sha256=_sha(
                canonical_json(entry.model_dump(mode="json", exclude_none=False))
            ),
            concept="operating_cash_flow",
            context=context,
        )
        if change == "hash":
            fact = fact.model_copy(update={"source_entry_sha256": "0" * 64})
        elif change == "path":
            fact = fact.model_copy(
                update={"locator": locator.model_copy(update={"json_path": "facts.wrong"})}
            )
        elif change == "accession":
            fact = fact.model_copy(
                update={
                    "locator": locator.model_copy(
                        update={"accession_number": "0000000001-26-000099"}
                    )
                }
            )
        elif change == "period":
            fact = fact.model_copy(
                update={
                    "context": context.model_copy(
                        update={"period_start": datetime(2025, 4, 1, tzinfo=UTC)}
                    )
                }
            )
        elif change == "role":
            fact = fact.model_copy(update={"concept": "net_income"})
        request = original.model_copy(update={"facts": (fact,)})
        if change != "none":
            with pytest.raises(ValueError):
                continue_companyfacts_statements(conn, request)
            assert conn.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone()[0] == 0
            return
        result = continue_companyfacts_statements(conn, request)
        assert result.match_revision_ids == ()
        bundle = FactReadModel(conn).provenance_bundle(
            result.observation_ids[0], cutoff=request.recorded_at
        )
        assert bundle.cell.fiscal_period == "YTD"
        assert bundle.cell.period_start is not None
        assert (bundle.cell.period_end - bundle.cell.period_start).days == 180
        assert bundle.cell.period_kind == "duration" and bundle.observation.decimal_value == 60
        assert (
            bundle.evidence is not None
            and "companyfacts_raw_entry_review" in bundle.evidence.source_locator.root
        )
        table = read_financial_table(conn, "ACME", as_of=request.recorded_at)
        assert len(table.cells) == 1
        assert table.cells[0].cadence == "unsupported"
        assert table.cells[0].reason_codes == ("fiscal_cadence_or_year_unavailable",)
        definition_id = str(
            conn.execute(
                "SELECT metric_definition_revision_id FROM canonical_metric_definition_revisions WHERE json_extract(scope_constraints_json,'$.valuation_role')='cashflow_equity.operating_cash_flow' ORDER BY revision DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert definition_id
        recipe = ModelInputRequest(
            recipe="operating_cashflow_equity.v1",
            ticker="ACME",
            research_snapshot_id="partial-source-selection",
            financial_period_end=bundle.cell.period_end.date(),
            facts={},
            assumptions={},
            recipe_context={
                "economic_method": "nonfinancial_operating_company",
                "reporting_regime": "sec_domestic_10k_10q",
                "currency": "USD",
                "accounting_basis": "us_gaap",
                "source_scope_label": "combined_carve_out",
                "consolidation_scope": "other",
                "flow_periods": [
                    {"role": "fy", "start": "2024-01-01", "end": "2024-12-31"},
                    {"role": "current_ytd", "start": "2025-01-01", "end": "2025-06-30"},
                    {"role": "prior_ytd", "start": "2024-01-01", "end": "2024-06-30"},
                ],
                "balance_date": "2025-06-30",
                "shares_date": "2025-06-30",
                "cash_interest_and_tax": "included_in_operating_cash_flow",
                "capex_basis": "positive_cash_outflow",
                "sbc_basis": "reported_operating_cashflow_addback",
                "shares_basis": "period_end_common_shares_plus_analyst_dilution",
                "net_new_borrowing": "zero",
                "method_reviewer": "synthetic-analyst",
                "method_rationale": "Synthetic US-GAAP operating cash flow includes interest and tax; no new borrowing forecast.",
            },
        )
        operand = next(
            item
            for item in requirements_for(recipe)
            if item.key == "operating_cash_flow_current_ytd"
        )
        assert (
            operand.period_start,
            operand.period_end,
            operand.unit_key,
            operand.consolidation_scope,
        ) == (
            bundle.cell.period_start.date(),
            bundle.cell.period_end.date(),
            bundle.cell.unit_key,
            bundle.cell.consolidation_scope,
        )
        assert len(requirements_for(recipe)) == 12  # One selected entry is not a complete model.
        assert continue_companyfacts_statements(conn, request) == result
    finally:
        conn.close()
