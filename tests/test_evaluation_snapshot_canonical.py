"""Evaluation snapshots consume the canonical fact graph at one cutoff."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Generator
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from provenance.metric_ontology import MetricOntology
from report.models import EvaluationSnapshotSection, QuickCategorizationRow, SectionStatus
from report.sections import evaluation_snapshot
from tests import test_report_canonical_financials as canonical
from tests import test_source_fact_repository as foundation
from tests.test_canonical_growth_screen import STAMP as MARKET_STAMP
from tests.test_discovery_financial_inputs import seed_market_context

STAMP = foundation.STAMP


@pytest.fixture
def database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Generator[sqlite3.Connection]:
    factory = cast(
        Callable[..., Generator[sqlite3.Connection]], getattr(foundation.conn, "__wrapped__")
    )
    yield from factory(tmp_path, migrated_db)


def _annual_anchor(value: str = "44000000000") -> tuple[str, str, str, str, str, str]:
    return ("revenue", "2025-01-01", "2025-12-31", "FY", value, "USD")


def _row(result: EvaluationSnapshotSection, metric: str) -> QuickCategorizationRow:
    return next(row for row in result.rows if row.metric == metric)


def test_malformed_four_quarter_span_has_real_annual_anchor_and_no_ttm(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    rows = [_annual_anchor()]
    rows.extend(
        ("revenue", start, end, quarter, "1000000", "USD")
        for quarter, start, end in (
            ("Q1", "2025-01-01", "2025-03-11"),
            ("Q2", "2025-03-12", "2025-05-20"),
            ("Q3", "2025-05-21", "2025-07-29"),
            ("Q4", "2025-07-30", "2025-10-07"),
        )
    )
    canonical.seed_table(database, rows)
    result = evaluation_snapshot.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert result.status == SectionStatus.OK
    assert _row(result, "Revenue").ttm is None
    assert result.unavailable_reasons["Revenue TTM"] == (
        "four_comparable_contiguous_quarters_unavailable",
    )


def test_missing_canonical_facts_ignore_tempting_legacy_rows(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    database.execute(
        "INSERT INTO documents(id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_quality_tier) "
        "VALUES (901,'LEGACY','fmp','fmp_income_statement','tempting.json','legacy','2026-01-01','ok',1,'fmp_normalized')"
    )
    database.execute(
        "INSERT INTO evidence_document_versions(document_version_id,document_key,version_sequence,observation_id,blob_sha256,issuer_id,ticker,document_type,form_type,language,legacy_document_id,recorded_at) "
        "SELECT 'legacy-only-document','legacy-only-document',1,observation_id,blob_sha256,issuer_id,'LEGACY','regulatory_filing','10-K','en',901,recorded_at "
        "FROM evidence_document_versions WHERE document_version_id='document-1'"
    )
    database.execute(
        "INSERT INTO financial_facts(ticker,period_end,fiscal_period_type,line_item,value,currency,unit,source_doc_id) "
        "VALUES ('LEGACY','2025-12-31','FY','revenue',999000000000,'USD','USD',901)"
    )
    database.commit()
    result = evaluation_snapshot.build("LEGACY", tmp_path, conn=database, as_of=STAMP)
    assert result.status == SectionStatus.MISSING_DATA and result.rows == []
    assert result.canonical_financial_table is not None
    assert result.canonical_financial_table.reason_codes == ("no_canonical_financial_cells",)


def test_wrong_issuer_cannot_supply_another_tickers_snapshot(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    canonical.seed_table(database, [_annual_anchor()])
    result = evaluation_snapshot.build("OTHER", tmp_path, conn=database, as_of=STAMP)
    assert result.status == SectionStatus.MISSING_DATA and result.rows == []
    assert result.canonical_financial_table is not None
    assert result.canonical_financial_table.ticker == "OTHER"


def test_mixed_currency_table_fails_closed(database: sqlite3.Connection, tmp_path: Path) -> None:
    canonical.seed_table(
        database,
        [
            _annual_anchor(),
            ("operating_income", "2025-01-01", "2025-12-31", "FY", "3124000000", "EUR"),
        ],
        currencies={1: "EUR"},
    )
    result = evaluation_snapshot.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert result.status == SectionStatus.MISSING_DATA and result.rows == []
    assert result.canonical_financial_table is not None
    assert all(
        "mixed_currency_table" in cell.reason_codes
        for cell in result.canonical_financial_table.cells
    )


def test_definition_revision_invalidates_current_snapshot_but_not_prior_cutoff(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    canonical.seed_table(database, [_annual_anchor()])
    before = evaluation_snapshot.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    projection = before.canonical_financial_table
    assert projection is not None
    definition = MetricOntology(database).metric_definition_as_known(
        projection.cells[0].metric_id, STAMP
    )
    assert definition is not None
    later = STAMP + timedelta(days=1)
    MetricOntology(database).persist_metric_definition(
        definition.model_copy(
            update={
                "metric_definition_revision_id": "evaluation-changed-definition",
                "idempotency_key": "evaluation-changed-definition",
                "revision": 2,
                "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
                "definition_text": "Incompatible synthetic business scope",
                "effective_at": later,
                "knowledge_at": later,
                "recorded_at": later,
            }
        )
    )
    database.commit()
    current = evaluation_snapshot.build("SYNTH", tmp_path, conn=database, as_of=later)
    assert before.status == SectionStatus.OK
    assert current.status == SectionStatus.MISSING_DATA
    assert current.canonical_financial_table is not None
    assert "active_metric_definition_or_binding_unavailable" in (
        current.canonical_financial_table.cells[0].reason_codes
    )


def test_ttm_margin_requires_exactly_matched_period_windows(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    rows = [_annual_anchor()]
    for quarter, start, end in (
        ("Q1", "2025-01-01", "2025-03-31"),
        ("Q2", "2025-04-01", "2025-06-30"),
        ("Q3", "2025-07-01", "2025-09-30"),
        ("Q4", "2025-10-01", "2025-12-31"),
    ):
        rows.append(("revenue", start, end, quarter, "100000000", "USD"))
        shifted_start = "2025-01-02" if quarter == "Q1" else start
        rows.append(("operating_income", shifted_start, end, quarter, "10000000", "USD"))
    canonical.seed_table(database, rows)
    result = evaluation_snapshot.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert _row(result, "Revenue").ttm == 400.0
    assert _row(result, "Operating margin").ttm is None
    assert result.unavailable_reasons["Operating margin TTM"] == (
        "matched_four_quarter_numerator_and_revenue_window_unavailable",
    )


@pytest.mark.parametrize(
    "periods",
    [
        (
            ("Q1", "2025-01-01", "2025-03-31"),
            ("Q2", "2025-04-02", "2025-07-01"),
            ("Q3", "2025-07-02", "2025-09-30"),
            ("Q4", "2025-10-01", "2025-12-31"),
        ),
        (
            ("Q1", "2025-01-01", "2025-03-11"),
            ("Q2", "2025-03-12", "2025-05-20"),
            ("Q3", "2025-05-21", "2025-07-29"),
            ("Q4", "2025-07-30", "2025-10-07"),
        ),
    ],
)
def test_ttm_rejects_gapped_or_short_quarter_spans(
    database: sqlite3.Connection,
    tmp_path: Path,
    periods: tuple[tuple[str, str, str], ...],
) -> None:
    rows = [_annual_anchor()]
    rows.extend(
        ("revenue", start, end, quarter, "100000000", "USD") for quarter, start, end in periods
    )
    canonical.seed_table(database, rows)
    result = evaluation_snapshot.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert _row(result, "Revenue").ttm is None


def test_sparse_and_gapped_history_keeps_slots_and_suppresses_cagr(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    canonical.seed_table(
        database,
        [
            ("revenue", "2022-01-01", "2022-12-31", "FY", "30000000000", "USD"),
            _annual_anchor(),
        ],
    )
    result = evaluation_snapshot.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    revenue = _row(result, "Revenue")
    assert result.fiscal_years == [2023, 2024, 2025]
    assert (revenue.lfy_minus_2, revenue.lfy_minus_1, revenue.lfy) == (None, None, 44_000.0)
    assert revenue.cagr_3y is None
    assert "Revenue 3y CAGR" not in result.source_manifest


@pytest.mark.parametrize("mode", ["future", "stale"])
def test_market_price_requires_a_fresh_capture_known_at_cutoff(
    database: sqlite3.Connection, tmp_path: Path, mode: str
) -> None:
    canonical.seed_table(database, [_annual_anchor()])
    database.execute(
        "INSERT OR IGNORE INTO tracked_companies(ticker,name,list_type) VALUES ('SYNTH','Synthetic','evaluation')"
    )
    recorded_at = MARKET_STAMP + timedelta(days=1) if mode == "future" else MARKET_STAMP
    seed_market_context(
        database,
        tmp_path / "data" / "historical" / "fmp",
        ticker="SYNTH",
        price=25,
        recorded_at=recorded_at,
    )
    cutoff = MARKET_STAMP if mode == "future" else MARKET_STAMP + timedelta(days=2)
    result = evaluation_snapshot.build("SYNTH", tmp_path, conn=database, as_of=cutoff)
    assert result.status == SectionStatus.OK
    assert result.current_price is None and result.market_cap is None
    assert result.market_context is not None
    expected = "unavailable" if mode == "future" else "degraded"
    assert result.market_context.status == expected
    if mode == "stale":
        assert result.market_context.reason_codes[-1] == "market_snapshot_freshness_stale"


def test_projection_and_market_manifests_remain_typed_and_serializable(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    canonical.seed_table(database, [_annual_anchor()])
    result = evaluation_snapshot.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    payload = result.model_dump(mode="json")
    assert payload["canonical_financial_table"]["schema_version"] == (
        "canonical-financial-table/v1"
    )
    assert payload["market_context"]["authority"] == "captured_provider_market_snapshot"
    assert json.dumps(payload, sort_keys=True)
