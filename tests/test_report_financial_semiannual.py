"""Explicit half-year table coordinates retain fiscal identity and source dates."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
    populate_metric_ontology,
)
from sources.report_financials import (
    FinancialEvidenceReference,
    FinancialTableCell,
    read_financial_evidence,
    read_financial_table,
)
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database


@pytest.mark.parametrize(
    ("half", "start", "end", "fiscal_year"),
    [
        ("H1", "2026-01-01", "2026-06-30", 2026),
        ("H2", "2025-07-01", "2025-12-31", 2025),
        ("H1", "2025-11-03", "2026-05-03", 2027),
        ("H2", "2025-02-03", "2025-08-03", 2026),
    ],
)
def test_explicit_halves_preserve_own_fiscal_coordinate_and_sources(
    database: sqlite3.Connection, half: str, start: str, end: str, fiscal_year: int
) -> None:
    facts = seed_table(
        database,
        [
            ("operating_cash_flow", start, end, half, "100000000", "USD"),
            ("net_income", start, end, half, "20000000", "USD"),
        ],
        fiscal_years={0: fiscal_year, 1: fiscal_year},
    )
    table = read_financial_table(database, "SYNTH", as_of=STAMP)
    assert table.reason_codes == () and len(table.cells) == 2
    fact_ids = {fact.observation.observation_id for fact in facts}
    assert {cell.concept for cell in table.cells} == {"operating_cash_flow", "net_income"}
    for cell in table.cells:
        assert cell.available and cell.cadence == "semiannual"
        assert cell.display_coordinate == f"{fiscal_year} {half}"
        assert cell.provenance is not None and cell.provenance.evidence is not None
        assert cell.provenance.observation.observation_id in fact_ids
        assert cell.provenance.cell.fiscal_year == fiscal_year
        assert cell.provenance.cell.fiscal_period == half
        assert cell.provenance.cell.period_start == datetime.fromisoformat(start).replace(
            tzinfo=UTC
        )
        assert cell.provenance.cell.period_end == datetime.fromisoformat(end).replace(tzinfo=UTC)
        assert cell.provenance.cell.currency == "USD"
        assert cell.provenance.cell.accounting_basis == "us_gaap"
        assert cell.provenance.cell.consolidation_scope == "consolidated"
        assert cell.provenance.evidence.document_version_id == "report-document"
        assert cell.display_value == (
            Decimal(100) if cell.concept == "operating_cash_flow" else Decimal(20)
        )


@pytest.mark.parametrize(
    ("duration", "available"), [(174, False), (175, True), (200, True), (201, False)]
)
def test_half_year_duration_uses_existing_inclusive_bounds(
    database: sqlite3.Connection, duration: int, available: bool
) -> None:
    start = date(2025, 1, 1)
    end = start + timedelta(days=duration - 1)
    seed_table(
        database,
        [("operating_cash_flow", start.isoformat(), end.isoformat(), "H1", "100000000", "USD")],
    )
    cell = read_financial_table(database, "SYNTH", as_of=STAMP).cells[0]
    assert cell.available is available
    if available:
        assert cell.cadence == "semiannual" and cell.display_coordinate == "2025 H1"
    else:
        assert cell.cadence == "unsupported" and cell.display_coordinate is None
        assert "fiscal_cadence_or_year_unavailable" in cell.reason_codes


@pytest.mark.parametrize(
    ("start", "half", "year", "reason"),
    [
        ("", "H1", 2025, "exact_reported_numeric_duration_unavailable"),
        ("2025-01-01", "H1", None, "fiscal_cadence_or_year_unavailable"),
        ("2025-01-01", "H2", None, "fiscal_cadence_or_year_unavailable"),
        ("2025-01-01", "", 2025, "fiscal_cadence_or_year_unavailable"),
        ("2025-01-01", "YTD", 2025, "fiscal_cadence_or_year_unavailable"),
        ("2025-01-01", "Q2", 2025, "unsupported_quarter_duration"),
    ],
)
def test_half_year_requires_explicit_complete_source_identity(
    database: sqlite3.Connection, start: str, half: str, year: int | None, reason: str
) -> None:
    seed_table(
        database,
        [("operating_cash_flow", start, "2025-06-30", half, "100000000", "USD")],
        fiscal_years={0: year},
    )
    cell = read_financial_table(database, "SYNTH", as_of=STAMP).cells[0]
    assert not cell.available and cell.cadence != "semiannual"
    assert reason in cell.reason_codes


def test_half_year_duplicate_fiscal_coordinate_stays_ambiguous(
    database: sqlite3.Connection,
) -> None:
    seed_table(
        database,
        [
            ("net_income", "2025-01-01", "2025-06-29", "H1", "20000000", "USD"),
            ("net_income", "2025-01-01", "2025-06-30", "H1", "21000000", "USD"),
        ],
    )
    cells = read_financial_table(database, "SYNTH", as_of=STAMP).cells
    assert len(cells) == 2
    assert all(
        cell.cadence == "semiannual" and cell.display_coordinate == "2025 H1" for cell in cells
    )
    assert all(
        not cell.available and "ambiguous_table_coordinate" in cell.reason_codes for cell in cells
    )


def test_half_year_exact_reference_retains_later_resolution_clock(
    database: sqlite3.Connection,
) -> None:
    fact = seed_table(
        database,
        [("operating_cash_flow", "2025-01-01", "2025-06-30", "H1", "100000000", "USD")],
        populate=False,
    )[0]
    populate_metric_ontology(
        database,
        MetricOntologyPopulationRequest(
            knowledge_cutoff=STAMP, operation_recorded_at=STAMP, apply=True
        ),
    )
    observed = STAMP + timedelta(minutes=2)
    populate_canonical_resolution(
        database,
        CanonicalResolutionPopulationRequest(
            cutoff_at=STAMP, operation_recorded_at=observed, apply=True
        ),
    )
    database.commit()
    assert not read_financial_table(database, "SYNTH", as_of=STAMP).cells[0].available
    cell = read_financial_table(
        database, "SYNTH", as_of=STAMP, resolution_observed_through=observed
    ).cells[0]
    assert cell.available and cell.cadence == "semiannual"
    assert (
        cell.canonical_resolution_revision_id is not None
        and cell.metric_definition_revision_id is not None
    )
    reference = FinancialEvidenceReference(
        ticker="SYNTH",
        concept="operating_cash_flow",
        canonical_metric_cell_id=cell.canonical_metric_cell_id,
        observation_id=fact.observation.observation_id,
        canonical_resolution_revision_id=cell.canonical_resolution_revision_id,
        metric_definition_revision_id=cell.metric_definition_revision_id,
        as_of=STAMP,
        resolution_observed_through=observed,
    )
    exact = read_financial_evidence(database, reference)
    assert isinstance(exact, FinancialTableCell) and exact.provenance is not None
    assert exact.display_value == 100 and exact.display_coordinate == "2025 H1"
    assert exact.provenance.observation.observation_id == fact.observation.observation_id
    assert FinancialEvidenceReference.model_validate_json(reference.model_dump_json()) == reference
    assert (
        read_financial_evidence(
            database,
            reference.model_copy(
                update={"resolution_observed_through": observed - timedelta(seconds=1)}
            ),
        )
        is None
    )
    assert (
        read_financial_evidence(
            database,
            reference.model_copy(
                update={"canonical_resolution_revision_id": "different-resolution"}
            ),
        )
        is None
    )
