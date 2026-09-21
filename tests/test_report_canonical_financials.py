"""Synthetic canonical financial-table fixtures retain exact cell coordinates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast

import pytest

from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
)
from provenance.metric_ontology import CanonicalMetricDefinitionRevision, MetricOntology
from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
    populate_metric_ontology,
)
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from report.models import (
    FinancialsSection,
    QuarterlyLineItem,
    SectionStatus,
    SegmentSeries,
    SegmentsSection,
)
from report.renderers.charts_v2 import MatrixRow, yoy_heatmap_table
from report.renderers.workspace_sections import financials as financial_renderer
from report.sections import financials
from sources.report_financials import read_financial_table
from tests import test_source_fact_repository as foundation

STAMP = foundation.STAMP


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Generator[sqlite3.Connection]:
    factory = cast(
        Callable[..., Generator[sqlite3.Connection]], getattr(foundation.conn, "__wrapped__")
    )
    yield from factory(tmp_path, migrated_db)


def seed_table(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str, str, str, str]],
    *,
    ticker: str = "SYNTH",
    publication_prefix: str = "report",
    populate: bool = True,
    currency: str = "USD",
    currencies: dict[int, str] | None = None,
    fiscal_years: dict[int, int | None] | None = None,
    legacy_document_id: int | None = None,
    source_observation_id: str | None = None,
    legacy_scope_node: bool = False,
) -> tuple[ReportedSourceFact, ...]:
    """concept/start/end/fiscal-period/value/unit; one sealed synthetic document."""
    document_id = f"{publication_prefix}-document"
    run_id = f"{publication_prefix}-run"
    default_subject = ticker == "SYNTH" and publication_prefix == "report"
    reporting_entity_id = "reporting-1" if default_subject else f"reporting-{ticker.casefold()}"
    binding_id = "binding-1" if default_subject else f"{publication_prefix}-binding"
    if not default_subject:
        conn.execute(
            "INSERT OR IGNORE INTO reporting_entities VALUES (?,?,?,?,?,?)",
            (
                reporting_entity_id,
                f"reporting-key-{ticker.casefold()}",
                "issuer-1",
                "legal_registrant",
                ticker,
                STAMP,
            ),
        )
        prior_binding = conn.execute(
            "SELECT binding_revision_id,revision FROM recorded_subject_binding_revisions "
            "WHERE recorded_issuer_id='issuer-1' ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        assert prior_binding is not None
        revision = int(prior_binding[1]) + 1
        conn.execute(
            "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                binding_id,
                f"{publication_prefix}-binding-key",
                "issuer-1",
                revision,
                "issuer-1",
                reporting_entity_id,
                None,
                "selected",
                "deterministic",
                "exact_subject",
                "{}",
                0,
                STAMP,
                STAMP,
                STAMP,
                str(prior_binding[0]),
            ),
        )
    conn.execute(
        "INSERT INTO evidence_document_versions(document_version_id,document_key,"
        "version_sequence,observation_id,blob_sha256,issuer_id,ticker,document_type,"
        "form_type,language,legacy_document_id,recorded_at) "
        "SELECT ?,?,1,COALESCE(?,observation_id),blob_sha256,issuer_id,?,'regulatory_filing',"
        "'10-K','en',?,recorded_at FROM evidence_document_versions "
        "WHERE document_version_id='document-1'",
        (document_id, document_id, source_observation_id, ticker, legacy_document_id),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs SELECT ?,?,?,input_sha256,extractor_name,extractor_config_sha256,extractor_code_version,output_sha256,started_at,completed_at,outcome FROM evidence_extraction_runs WHERE extraction_run_id='run-1'",
        (run_id, run_id, document_id),
    )
    facts: list[ReportedSourceFact] = []
    for index, (concept, start, end, fiscal, value, unit) in enumerate(rows):
        suffix = f"{publication_prefix}-{index}"
        locator = {"path": f"/facts/{index}/value"}
        locator_json = json.dumps(locator, sort_keys=True, separators=(",", ":"))
        node = f"node-{suffix}"
        node_kind = "section" if legacy_scope_node and index == 0 else "table_cell"
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,?, ?,NULL,NULL,?,?,?, ?,?)",
            (
                node,
                node,
                1,
                run_id,
                node_kind,
                value,
                locator_json,
                _sha(locator_json),
                STAMP,
            ),
        )
        cell = FactCellV2.model_validate(
            {
                **foundation.make_cell(suffix).model_dump(),
                "reporting_entity_id": reporting_entity_id,
                "semantic_key_sha256": None,
                "concept_namespace": "urn:earnings-summary:legacy:financial",
                "concept_name": concept,
                "taxonomy_name": "earnings-summary-legacy",
                "taxonomy_version": "2026",
                "period_start": datetime.fromisoformat(start).replace(tzinfo=UTC),
                "period_end": datetime.fromisoformat(end).replace(tzinfo=UTC),
                "fiscal_year": (fiscal_years or {}).get(index, int(end[:4])),
                "fiscal_period": fiscal or None,
                "dimensions": (),
                "unit_key": unit,
                "currency": (currencies or {}).get(index, currency),
            }
        )
        observation = foundation.make_report(cell, suffix, numeric_value=value).model_copy(
            update={
                "document_version_id": document_id,
                "evidence_node_id": node,
                "source_locator": CanonicalJSONObject.model_validate(locator),
                "source_locator_sha256": _sha(locator_json),
                "subject_binding_revision_id": binding_id,
            }
        )
        facts.append(ReportedSourceFact(cell=cell, observation=observation))
    conn.commit()
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id=f"{publication_prefix}-publication",
            idempotency_key=f"{publication_prefix}-publication",
            reported_facts=tuple(facts),
            extraction_seals=(
                ExtractionRunCompletenessSealV2(
                    extraction_seal_id=f"{publication_prefix}-seal",
                    idempotency_key=f"{publication_prefix}-seal",
                    extraction_run_id=run_id,
                    expected_node_count=len(facts),
                    completeness_policy_name="all-run-nodes",
                    completeness_policy_version="v1",
                    completeness_policy_sha256=_sha("report-completeness"),
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                ),
            ),
        )
    )
    if not populate:
        return tuple(facts)
    ontology = populate_metric_ontology(
        conn,
        MetricOntologyPopulationRequest(
            knowledge_cutoff=STAMP, operation_recorded_at=STAMP, apply=True
        ),
    )
    assert ontology.snapshot_id is not None
    receipt = populate_canonical_resolution(
        conn,
        CanonicalResolutionPopulationRequest(
            cutoff_at=STAMP, operation_recorded_at=STAMP, apply=True
        ),
    )
    assert receipt.resolved_cell_count >= len(rows)
    conn.commit()
    return tuple(facts)


OFFCAL = [
    ("2015-11-02", "2016-01-31", "Q1", 2257, 286),
    ("2016-02-01", "2016-05-01", "Q2", 2450, 320),
    ("2016-05-02", "2016-07-31", "Q3", 2821, 505),
    ("2016-08-01", "2016-10-30", "Q4", 3297, 610),
]


def test_actual_report_build_keeps_offcalendar_coordinates_and_reconciliation(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    rows = [
        (concept, start, end, fiscal, str(value * 1_000_000), "USD")
        for start, end, fiscal, revenue, income in OFFCAL
        for concept, value in (("revenue", revenue), ("net_income", income))
    ]
    rows.extend(
        [
            (concept, "2015-11-02", "2016-10-30", "FY", str(value * 1_000_000), "USD")
            for concept, value in (("revenue", 10825), ("net_income", 1721))
        ]
    )
    seed_table(database, rows)
    projection = read_financial_table(database, "SYNTH", as_of=STAMP)
    assert len(projection.cells) == 10 and all(cell.available for cell in projection.cells)
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert result.status == SectionStatus.OK
    revenue = next(item for item in result.line_items if item.line_item == "Revenue")
    assert dict(zip(revenue.quarters, revenue.values, strict=True)) == {
        "2016 Q1": 2257.0,
        "2016 Q2": 2450.0,
        "2016 Q3": 2821.0,
        "2016 Q4": 3297.0,
    }
    quarterly = next(item for item in result.line_items if item.line_item == "Net income")
    annual = next(item for item in result.annual_line_items if item.line_item == "Net income")
    assert (
        sum(value for value in quarterly.values if value is not None) == annual.values[0] == 1721.0
    )
    assert result.canonical_financial_table == projection
    assert len(annual.sources_full) == 1 and annual.sources_full[0] is not None


def test_native_currency_and_per_share_source_units(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(
        database,
        [
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "250000000", "EUR"),
            ("eps_diluted", "2025-01-01", "2025-03-31", "Q1", "2.50", "EUR/shares"),
        ],
        currency="EUR",
    )
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert [(item.unit, item.values) for item in result.line_items] == [
        ("EUR millions", [250.0]),
        ("EUR/share", [2.5]),
    ]
    projection = result.canonical_financial_table
    assert projection is not None
    for cell in projection.cells:
        assert cell.provenance is not None and cell.provenance.evidence is not None
        assert cell.provenance.observation.currency == "EUR"
        assert cell.provenance.evidence.document_version_id == "report-document"
        assert cell.source_retrieved_at is not None
    assert projection.cells[0].provenance is not None
    assert projection.cells[0].provenance.observation.unit_key == "EUR/shares"
    output = StringIO()
    levels_panel(output, result, SegmentsSection(status=SectionStatus.MISSING_DATA))
    assert (
        "EUR/share" in output.getvalue()
        and ">2.50</td>" in output.getvalue()
        and ">250.0</td>" in output.getvalue()
    )


@pytest.mark.parametrize(
    ("fiscal", "year", "unit", "reason"),
    [
        ("", 2025, "USD", "fiscal_cadence_or_year_unavailable"),
        ("FY", None, "USD", "fiscal_cadence_or_year_unavailable"),
        ("Q1", None, "USD", "fiscal_cadence_or_year_unavailable"),
        ("H1", 2025, "USD", "fiscal_cadence_or_year_unavailable"),
        ("YTD", 2025, "USD", "fiscal_cadence_or_year_unavailable"),
        ("Q1", 2025, "USD millions", "currency_or_scale_unavailable"),
    ],
)
def test_unproven_fiscal_or_scale_never_becomes_value(
    database: sqlite3.Connection,
    tmp_path: Path,
    fiscal: str,
    year: int | None,
    unit: str,
    reason: str,
) -> None:
    seed_table(
        database,
        [("revenue", "2025-01-01", "2025-03-31", fiscal, "250", unit)],
        fiscal_years={0: year},
    )
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert result.line_items == [] and result.annual_line_items == []
    assert result.canonical_financial_table is not None
    cell = result.canonical_financial_table.cells[0]
    assert reason in cell.reason_codes and cell.provenance is not None
    assert cell.provenance.observation.decimal_value is not None


def test_eps_must_prove_per_share_basis(database: sqlite3.Connection, tmp_path: Path) -> None:
    seed_table(database, [("eps_diluted", "2025-01-01", "2025-03-31", "Q1", "2.5", "USD")])
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert result.line_items == []
    assert result.canonical_financial_table is not None
    assert result.canonical_financial_table.cells[0].reason_codes == (
        "currency_or_scale_unavailable",
    )


def test_mixed_currency_does_not_borrow_global_label(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(
        database,
        [
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "250000000", "USD"),
            ("net_income", "2025-01-01", "2025-03-31", "Q1", "12000000", "EUR"),
        ],
        currencies={1: "EUR"},
    )
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert not result.line_items and result.currency == "unavailable"
    assert result.canonical_financial_table is not None
    assert {
        cell.provenance.cell.currency
        for cell in result.canonical_financial_table.cells
        if cell.provenance
    } == {"USD", "EUR"}
    assert all(
        "mixed_currency_table" in cell.reason_codes
        for cell in result.canonical_financial_table.cells
    )


def test_sparse_history_preserves_gap_and_suppresses_growth(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(
        database,
        [
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "250000000", "USD"),
            ("revenue", "2025-07-01", "2025-09-30", "Q3", "300000000", "USD"),
        ],
    )
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    row = result.line_items[0]
    assert row.quarters == ["2025 Q1", "2025 Q2", "2025 Q3"]
    assert row.values == [250.0, None, 300.0] and row.growth.qoq is None and row.growth.yoy is None
    assert row.sources_full[1] is None
    assert row.comparison_eligible_edges == [False, False, False]


@pytest.mark.parametrize(
    ("start", "end", "available"),
    [("2024-12-30", "2025-04-06", True), ("2025-01-01", "2025-06-30", False)],
)
def test_53_week_quarter_allowed_but_mislabeled_half_year_rejected(
    database: sqlite3.Connection, start: str, end: str, available: bool
) -> None:
    seed_table(database, [("revenue", start, end, "Q1", "100000000", "USD")])
    projection = read_financial_table(database, "SYNTH", as_of=STAMP)
    assert projection.cells[0].available is available
    if not available:
        assert "unsupported_quarter_duration" in projection.cells[0].reason_codes


def test_duplicate_calendar_coordinate_is_ambiguous(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(
        database,
        [
            ("revenue", "2025-01-01", "2025-03-30", "Q1", "100000000", "USD"),
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "101000000", "USD"),
        ],
    )
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert not result.line_items and result.canonical_financial_table is not None
    assert all(
        "ambiguous_table_coordinate" in cell.reason_codes
        for cell in result.canonical_financial_table.cells
    )


def test_as_of_before_publication_cannot_see_later_evidence(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")])
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP - timedelta(seconds=1))
    assert not result.line_items
    assert (
        result.canonical_financial_table is not None and not result.canonical_financial_table.cells
    )


def test_missing_configuration_never_creates_checkout_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def absent(_root: Path) -> None:
        return None

    def required(_path: Path | str | None) -> Path:
        raise RuntimeError("not configured")

    monkeypatch.setattr(financials, "configured_db_path", absent)
    monkeypatch.setattr(financials, "require_db_path", required)
    assert financials.build("SYNTH", tmp_path).status == SectionStatus.MISSING_DATA
    assert not (tmp_path / "data" / "portfolio.db").exists()


Panel = Callable[[StringIO, FinancialsSection, SegmentsSection], None]
levels_panel = cast(Panel, getattr(financial_renderer, "_line_items_levels_panel"))
validation_panel = cast(Panel, getattr(financial_renderer, "_validation_panel"))


def _render_fixture(
    currency: str = "EUR",
    revenue: float = 250.0,
    segment: float = 250.0,
    segment_unit: str | None = None,
) -> tuple[FinancialsSection, SegmentsSection]:
    labels = ["2025 Q1"]
    fin = FinancialsSection(
        status=SectionStatus.OK,
        currency=currency,
        quarter_labels=labels,
        line_items=[
            QuarterlyLineItem(line_item=name, unit=unit, quarters=labels, values=[value])
            for name, unit, value in [
                ("Revenue", currency + " millions", revenue),
                ("Net income", currency + " millions", -12.0),
                ("Diluted EPS", currency + "/share", 2.50),
            ]
        ],
    )
    seg = SegmentsSection(
        status=SectionStatus.OK,
        quarter_labels=labels,
        revenue_by_product=[
            SegmentSeries(
                metric="revenue_by_product",
                segment_name="Synthetic A",
                unit=segment_unit or currency + " millions",
                quarters=labels,
                values=[segment],
            )
        ],
    )
    return fin, seg


@pytest.mark.parametrize("currency", ["USD", "EUR"])
def test_levels_renderer_uses_declared_millions_and_per_share(currency: str) -> None:
    output = StringIO()
    levels_panel(output, *_render_fixture(currency))
    html = output.getvalue()
    assert f"{currency} millions" in html and f"{currency}/share" in html
    assert '<td class="num">250.0</td>' in html
    assert '<td class="num neg">(12.0)</td>' in html
    assert '<td class="num">2.50</td>' in html
    assert "<strong>250.0</strong>" in html


def test_tie_out_is_numeric_only_and_keeps_correct_billions_scale() -> None:
    output = StringIO()
    validation_panel(output, *_render_fixture(segment=260.0))
    html = output.getvalue()
    assert "0.25B" in html and "0.26B" in html and "4.0%" in html
    assert "Source, definition, and exact fiscal-period parity are unverified" in html
    assert "FMP" not in html


@pytest.mark.parametrize(("revenue", "segment_unit"), [(0.0, None), (250.0, "USD millions")])
def test_tie_out_cannot_compare_zero_or_different_currency(
    revenue: float, segment_unit: str | None
) -> None:
    output = StringIO()
    validation_panel(output, *_render_fixture(revenue=revenue, segment_unit=segment_unit))
    assert "numeric match" not in output.getvalue() and "DRIFT" not in output.getvalue()
    if segment_unit is not None:
        assert "cannot tie" in output.getvalue()


def test_restatement_changes_selected_observation_only_after_knowledge_cutoff(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    original = seed_table(
        database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")]
    )[0]
    revised_at = STAMP + timedelta(days=1)
    locator = CanonicalJSONObject.model_validate({"path": "/restated/revenue"})
    locator_json = locator.model_dump_json()
    database.execute(
        "INSERT INTO evidence_extraction_runs SELECT 'restated-run','restated-run','report-document',input_sha256,extractor_name,extractor_config_sha256,'restatement-v2',output_sha256,?,? ,outcome FROM evidence_extraction_runs WHERE extraction_run_id='run-1'",
        (revised_at, revised_at),
    )
    database.execute(
        "INSERT INTO evidence_nodes VALUES ('restated-node','restated-node',1,'restated-run',NULL,NULL,'table_cell','110000000',?,?,?)",
        (locator_json, _sha(locator_json), revised_at),
    )
    database.commit()
    observation = foundation.make_report(
        original.cell, "restated", numeric_value="110000000", at=revised_at
    ).model_copy(
        update={
            "revision_kind": "restatement",
            "supersedes_observation_id": original.observation.observation_id,
            "document_version_id": "report-document",
            "evidence_node_id": "restated-node",
            "source_locator": locator,
            "source_locator_sha256": _sha(locator_json),
        }
    )
    SourceFactRepository(database).publish(
        SourceFactPublication(
            publication_id="restated-publication",
            idempotency_key="restated-publication",
            reported_facts=(ReportedSourceFact(cell=original.cell, observation=observation),),
            extraction_seals=(
                ExtractionRunCompletenessSealV2(
                    extraction_seal_id="restated-seal",
                    idempotency_key="restated-seal",
                    extraction_run_id="restated-run",
                    expected_node_count=1,
                    completeness_policy_name="all-run-nodes",
                    completeness_policy_version="v1",
                    completeness_policy_sha256=_sha("report-completeness"),
                    knowledge_at=revised_at,
                    recorded_at=revised_at,
                ),
            ),
        )
    )
    populate_metric_ontology(
        database,
        MetricOntologyPopulationRequest(
            knowledge_cutoff=revised_at, operation_recorded_at=revised_at, apply=True
        ),
    )
    populate_canonical_resolution(
        database,
        CanonicalResolutionPopulationRequest(
            cutoff_at=revised_at, operation_recorded_at=revised_at, apply=True
        ),
    )
    database.commit()
    before = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    after = financials.build("SYNTH", tmp_path, conn=database, as_of=revised_at)
    assert before.line_items[0].values == [100.0]
    assert after.line_items[0].values == [110.0]
    assert (
        before.canonical_financial_table is not None and after.canonical_financial_table is not None
    )
    prior = before.canonical_financial_table.cells[0]
    latest = after.canonical_financial_table.cells[0]
    assert prior.provenance is not None and latest.provenance is not None
    assert prior.provenance.observation.observation_id == original.observation.observation_id
    assert latest.provenance.observation.observation_id == observation.observation_id
    assert prior.canonical_resolution_revision_id != latest.canonical_resolution_revision_id
    assert after.line_items[0].sources_full[0] is not None
    assert after.line_items[0].sources_full[0].locator == locator_json


def test_per_metric_uses_same_selected_evidence_and_capex_key(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(
        database, [("capital_expenditure", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")]
    )
    result = financials.build_per_metric("SYNTH", tmp_path, conn=database)
    assert set(result) == {"capex_q_latest"}
    value = result["capex_q_latest"]
    assert value["canonical_resolution_revision_id"]
    assert value["provenance"] and value["source_url"]


def test_definition_change_invalidates_current_use_but_preserves_as_known(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")])
    before = read_financial_table(database, "SYNTH", as_of=STAMP)
    ontology = MetricOntology(database)
    definition = ontology.metric_definition_as_known(before.cells[0].metric_id, STAMP)
    assert definition is not None
    later = STAMP + timedelta(days=1)
    ontology.persist_metric_definition(
        definition.model_copy(
            update={
                "metric_definition_revision_id": "changed-definition",
                "idempotency_key": "changed-definition",
                "revision": 2,
                "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
                "definition_text": "Synthetic incompatible revised business scope",
                "effective_at": later,
                "knowledge_at": later,
                "recorded_at": later,
            }
        )
    )
    database.commit()
    prior = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    current = financials.build("SYNTH", tmp_path, conn=database, as_of=later)
    assert prior.line_items[0].values == [100.0]
    assert not current.line_items and current.canonical_financial_table is not None
    assert (
        "active_metric_definition_or_binding_unavailable"
        in current.canonical_financial_table.cells[0].reason_codes
    )


def test_comparability_edges_stop_at_fiscal_break_then_recover(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(
        database,
        [
            ("revenue", "2024-01-01", "2024-03-31", "Q1", "100000000", "USD"),
            ("revenue", "2024-04-02", "2024-06-30", "Q2", "110000000", "USD"),
            ("revenue", "2024-07-01", "2024-09-30", "Q3", "120000000", "USD"),
            ("revenue", "2024-10-01", "2024-12-31", "Q4", "130000000", "USD"),
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "140000000", "USD"),
            ("revenue", "2025-04-01", "2025-06-30", "Q2", "150000000", "USD"),
        ],
    )
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    row = result.line_items[0]
    assert row.values == [100.0, 110.0, 120.0, 130.0, 140.0, 150.0]
    assert row.comparison_eligible_edges == [False, False, True, True, True, True]
    html = yoy_heatmap_table(
        [
            MatrixRow(
                name="Synthetic revenue",
                levels=row.levels_full,
                unit=row.unit,
                comparison_eligible_edges=row.comparison_eligible_edges,
            )
        ],
        row.quarters,
        title="",
    )
    assert "+40.0%" not in html and "+36.4%" in html
    assert row.growth.yoy == pytest.approx(150 / 110 - 1)


@pytest.mark.parametrize("edges", [[], [False] * 6, [False, False, True, True, True, False]])
@pytest.mark.parametrize("unit", ["EUR millions", "%"])
def test_matrix_never_recomputes_growth_across_explicit_unknown_or_break(
    edges: list[bool], unit: str
) -> None:
    row = MatrixRow(
        name="Synthetic",
        levels=[100.0, 110.0, 120.0, 130.0, 140.0, 150.0],
        unit=unit,
        comparison_eligible_edges=edges,
    )
    html = yoy_heatmap_table([row], [str(index) for index in range(6)], title="", cagr_periods=(4,))
    assert "+40.0%" not in html and "+36.4%" not in html and "+40.0pp" not in html
    if unit == "%":
        assert ">150.0%</td>" in html
    else:
        assert '<td class="cv2-matrix-cagr-cell" style="background:transparent">—</td>' in html


def test_annual_axis_uses_reported_fiscal_year_not_calendar_end(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(
        database,
        [("revenue", "2024-02-05", "2025-02-02", "FY", "100000000", "USD")],
        fiscal_years={0: 2024},
    )
    original_factory = database.row_factory
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert result.annual_years == [2024] and result.annual_line_items[0].values == [100.0]
    assert database.row_factory is original_factory
    assert result.canonical_financial_table is not None
    source = result.canonical_financial_table.cells[0].provenance
    assert source is not None and source.cell.period_end.year == 2025


@pytest.mark.parametrize(("first_end", "days"), [(date(2024, 3, 31), 71), (date(2024, 1, 14), 105)])
def test_actual_report_rejects_nonannual_source_span_without_hiding_qoq(
    database: sqlite3.Connection, tmp_path: Path, first_end: date, days: int
) -> None:
    start = first_end - timedelta(days=days - 1)
    rows: list[tuple[str, str, str, str, str, str]] = []
    for index in range(5):
        end = start + timedelta(days=days - 1)
        rows.append(
            (
                "revenue",
                start.isoformat(),
                end.isoformat(),
                f"Q{index % 4 + 1}",
                str((100 + 10 * index) * 1_000_000),
                "USD",
            )
        )
        start = end + timedelta(days=1)
    seed_table(database, rows, fiscal_years={index: 2024 + index // 4 for index in range(5)})
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    row = result.line_items[0]
    assert row.values == [100.0, 110.0, 120.0, 130.0, 140.0]
    assert row.growth.qoq == pytest.approx(140 / 130 - 1)
    assert row.growth.yoy is None
    output = StringIO()
    render = cast(
        Callable[[StringIO, FinancialsSection], None],
        getattr(financial_renderer, "_line_items_yoy_panel"),
    )
    render(output, result)
    assert "+40.0%" not in output.getvalue()
    levels = StringIO()
    levels_panel(levels, result, SegmentsSection(status=SectionStatus.MISSING_DATA))
    assert ">140.0</td>" in levels.getvalue()


def test_actual_report_uses_one_snapshot_during_concurrent_definition_append(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_table(
        database,
        [
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD"),
            ("revenue", "2025-04-01", "2025-06-30", "Q2", "110000000", "USD"),
        ],
    )
    database.execute("PRAGMA journal_mode=WAL")
    path = str(database.execute("PRAGMA database_list").fetchone()[2])
    original = MetricOntology.metric_definition_as_known
    calls = 0
    appended = False
    later = STAMP + timedelta(hours=1)

    def observe(
        self: MetricOntology, metric_id: str, cutoff: datetime
    ) -> CanonicalMetricDefinitionRevision | None:
        nonlocal calls, appended
        definition = original(self, metric_id, cutoff)
        calls += 1
        if calls == 2:
            assert definition is not None
            writer = sqlite3.connect(path)
            try:
                MetricOntology(writer).persist_metric_definition(
                    definition.model_copy(
                        update={
                            "metric_definition_revision_id": "concurrent-definition",
                            "idempotency_key": "concurrent-definition",
                            "revision": 2,
                            "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
                            "definition_text": "A new incompatible synthetic scope",
                            "effective_at": later,
                            "knowledge_at": later,
                            "recorded_at": later,
                        }
                    )
                )
                writer.commit()
                appended = True
            finally:
                writer.close()
        return definition

    monkeypatch.setattr(MetricOntology, "metric_definition_as_known", observe)
    initial_factory = database.row_factory
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=later)
    assert appended
    assert result.line_items[0].values == [100.0, 110.0]
    assert not database.in_transaction and database.row_factory is initial_factory
    subsequent = financials.build("SYNTH", tmp_path, conn=database, as_of=later)
    assert not subsequent.line_items


@pytest.mark.parametrize("final_days", [91, 98])
def test_actual_report_accepts_52_and_53_week_year_comparisons(
    database: sqlite3.Connection, tmp_path: Path, final_days: int
) -> None:
    start = date(2023, 12, 25)
    rows: list[tuple[str, str, str, str, str, str]] = []
    for index in range(5):
        days = final_days if index == 4 else 91
        end = start + timedelta(days=days - 1)
        rows.append(
            (
                "revenue",
                start.isoformat(),
                end.isoformat(),
                f"Q{index % 4 + 1}",
                str((100 + 10 * index) * 1_000_000),
                "USD",
            )
        )
        start = end + timedelta(days=1)
    seed_table(database, rows, fiscal_years={index: 2024 + index // 4 for index in range(5)})
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    row = result.line_items[0]
    assert row.growth.yoy == pytest.approx(0.4)
    assert row.growth.qoq == pytest.approx(140 / 130 - 1)
    output = StringIO()
    render = cast(
        Callable[[StringIO, FinancialsSection], None],
        getattr(financial_renderer, "_line_items_yoy_panel"),
    )
    render(output, result)
    assert "+40.0%" in output.getvalue()


@pytest.mark.parametrize(
    "dates",
    [
        [],
        [date(2024, 3, 31)],
        [date(2024, 3, 31), None, date(2024, 9, 30), date(2024, 12, 31), date(2025, 3, 31)],
    ],
)
@pytest.mark.parametrize("unit", ["USD millions", "%"])
def test_matrix_supplied_incomplete_dates_never_fall_back_to_legacy_growth(
    dates: list[date | None], unit: str
) -> None:
    row = MatrixRow(
        name="Synthetic",
        levels=[100.0, 110.0, 120.0, 130.0, 140.0],
        unit=unit,
        comparison_eligible_edges=[False, True, True, True, True],
        comparison_period_ends=dates,
    )
    rendered = yoy_heatmap_table(
        [row], [str(index) for index in range(5)], title="", cagr_periods=(4,)
    )
    assert "+40.0%" not in rendered and "+40.0pp" not in rendered
    if unit == "%":
        assert ">140.0%</td>" in rendered


def test_actual_multiyear_report_cannot_average_away_short_and_long_years(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    start = date(2022, 1, 14) - timedelta(days=90)
    rows: list[tuple[str, str, str, str, str, str]] = []
    for index, days in enumerate([91] * 4 + [105] * 4 + [77] * 4 + [91] * 4):
        end = start + timedelta(days=days - 1)
        rows.append(
            (
                "revenue",
                start.isoformat(),
                end.isoformat(),
                f"Q{index % 4 + 1}",
                str((100 + 10 * index) * 1_000_000),
                "USD",
            )
        )
        start = end + timedelta(days=1)
    seed_table(database, rows, fiscal_years={index: 2022 + index // 4 for index in range(16)})
    result = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    row = result.line_items[0]
    assert row.levels_full == [float(100 + 10 * index) for index in range(16)]
    assert row.growth.cagr_3y_ttm is None
    assert row.growth.cagr_1y_ttm is None  # previous TTM only 308 days
    assert row.growth.yoy == pytest.approx(250 / 210 - 1)  # later valid 364-day comparison recovers
    matrix = MatrixRow(
        name="Synthetic",
        levels=row.levels_full,
        unit=row.unit,
        comparison_eligible_edges=row.comparison_eligible_edges,
        comparison_period_ends=row.comparison_period_ends,
    )
    assert not matrix.comparison_available(3, 15)
    assert matrix.comparison_available(11, 15)
    rendered = yoy_heatmap_table(
        [matrix], [str(index) for index in range(16)], title="", cagr_periods=(4, 12)
    )
    assert "+19.0%" in rendered
    assert 'class="cv2-matrix-cagr-cell" style="background:transparent">—</td>' in rendered


def test_reader_preserves_caller_transaction_and_row_factory(database: sqlite3.Connection) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")])
    database.execute("CREATE TEMP TABLE caller_marker (value TEXT)")
    database.execute("INSERT INTO caller_marker VALUES ('pending')")
    original_factory = database.row_factory
    assert database.in_transaction
    result = read_financial_table(database, "SYNTH", as_of=STAMP)
    assert result.cells[0].available
    assert database.in_transaction and database.row_factory is original_factory
    database.rollback()
    assert database.execute("SELECT count(*) FROM caller_marker").fetchone()[0] == 0
