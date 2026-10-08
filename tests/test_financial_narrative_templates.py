"""Financial narrative inputs must retain admitted meaning, not just numbers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Generator
from io import StringIO
from pathlib import Path
from typing import cast

import pytest

from report.models import (
    ExecCompRowModel,
    ExecCompSectionModel,
    FinancialsSection,
    InsiderSignalRowModel,
    QuarterlyLineItem,
    SectionStatus,
    SegmentSeries,
    SegmentsSection,
)
from report.renderers.workspace_sections.company import _customer_concentration_panel
from report.renderers.workspace_sections.exec_comp import _exec_comp_tab
from report.sections import bear_case, exec_compensation, financials
from tests import test_source_fact_repository as foundation
from tests.test_report_canonical_financials import STAMP, seed_table


@pytest.fixture
def database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Generator[sqlite3.Connection]:
    factory = cast(
        Callable[..., Generator[sqlite3.Connection]], getattr(foundation.conn, "__wrapped__")
    )
    yield from factory(tmp_path, migrated_db)


def test_empty_customer_rows_do_not_infer_disclosed_threshold() -> None:
    body = StringIO()
    _customer_concentration_panel(body, [])
    rendered = body.getvalue()
    assert "Customer concentration" in rendered
    assert "data unavailable" in rendered
    assert "does not establish" in rendered
    assert "5%" not in rendered
    assert "diversified" not in rendered


def test_bear_case_preserves_canonical_financial_context(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    facts = seed_table(
        database,
        [("operating_cash_flow", "2025-01-01", "2025-03-31", "Q1", "189000000", "EUR")],
        currency="EUR",
    )
    section = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    # A stale display row must not supersede the exact admitted selection.
    section.line_items[0].values = [999.0]
    rendered = cast(Callable[[FinancialsSection], str], getattr(bear_case, "_financials_md"))(
        section
    )
    payload = json.loads(rendered.split("```json\n", 1)[1].split("\n```", 1)[0])
    cell = payload["cells"][0]
    assert cell["concept"] == "operating_cash_flow"
    assert cell["canonical_resolution_revision_id"]
    assert cell["metric_definition_revision_id"]
    provenance = cell["provenance"]
    assert provenance["observation"]["observation_id"] == facts[0].observation.observation_id
    assert provenance["observation"]["decimal_value"] == "189000000"
    assert provenance["cell"]["currency"] == "EUR"
    assert provenance["cell"]["unit_key"] == "EUR"
    assert provenance["cell"]["consolidation_scope"] == "consolidated"
    assert provenance["cell"]["accounting_basis"] == "us_gaap"
    assert provenance["cell"]["fiscal_period"] == "Q1"
    assert provenance["cell"]["period_start"].startswith("2025-01-01")
    assert provenance["evidence"]["document_version_id"]
    assert provenance["evidence"]["source_locator"] == {"path": "/facts/0/value"}
    assert "free_cash_flow" not in rendered
    assert "line_items" not in payload


def test_bear_case_does_not_upgrade_legacy_numbers_without_admission() -> None:
    section = FinancialsSection(
        status=SectionStatus.OK,
        quarter_labels=["2025 Q1"],
        line_items=[
            QuarterlyLineItem(line_item="Free cash flow", unit="USD millions", values=[189.0])
        ],
    )
    rendered = cast(Callable[[FinancialsSection], str], getattr(bear_case, "_financials_md"))(
        section
    )
    assert "189" not in rendered
    assert "admitted financial evidence unavailable" in rendered


def test_bear_case_segment_inputs_preserve_unit_and_source_status() -> None:
    section = SegmentsSection(
        status=SectionStatus.OK,
        quarter_labels=["2025 Q1"],
        revenue_by_product=[
            SegmentSeries(
                segment_name="Synthetic segment",
                metric="revenue_by_product",
                values=[189.0],
                unit="EUR millions",
                source_label="vendor-only",
            )
        ],
    )
    rendered = cast(Callable[[SegmentsSection], str], getattr(bear_case, "_segments_md"))(section)
    assert "EUR millions" in rendered
    assert "vendor-only" in rendered
    assert "not canonical financial admission" in rendered


def test_bear_case_withholds_rejected_cell_values(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(
        database,
        [("revenue", "2025-01-01", "2025-03-31", "Q1", "189000000", "USD")],
    )
    section = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    projection = section.canonical_financial_table
    assert projection is not None
    rejected = projection.cells[0].model_copy(update={"reason_codes": ("source_review_failed",)})
    section.canonical_financial_table = projection.model_copy(update={"cells": (rejected,)})
    rendered = cast(Callable[[FinancialsSection], str], getattr(bear_case, "_financials_md"))(
        section
    )
    payload = json.loads(rendered.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert payload["cells"][0]["reason_codes"] == ["source_review_failed"]
    assert payload["cells"][0]["provenance"] is None
    assert "189000000" not in rendered


def test_missing_compensation_metrics_do_not_mean_none_disclosed() -> None:
    body = StringIO()
    _exec_comp_tab(
        body,
        ExecCompSectionModel(
            status=SectionStatus.OK,
            ticker="SYNTH",
            packages=[ExecCompRowModel(executive_name="Synthetic executive", fiscal_year=2025)],
        ),
    )
    rendered = body.getvalue()
    assert "Disclosure not available in this report" in rendered
    assert "none disclosed" not in rendered


def test_compensation_prompt_preserves_currency_and_missing_values() -> None:
    formatter = cast(
        Callable[[str, list[ExecCompRowModel], list[InsiderSignalRowModel], list[str]], str],
        getattr(exec_compensation, "_alignment_prompt"),
    )
    rows = [
        ExecCompRowModel(executive_name="Missing executive", fiscal_year=2025),
        ExecCompRowModel(
            executive_name="EUR executive",
            fiscal_year=2025,
            currency="EUR",
            total_comp_granted=0.0,
            total_comp_realized=189000.0,
        ),
    ]
    rendered = formatter("SYNTH", rows, [], [])
    assert "granted unavailable / realized unavailable" in rendered
    assert "granted EUR 0.00 / realized EUR 189,000.00" in rendered
    assert "disclosure unavailable in this report" in rendered
    assert "insider activity data unavailable in this report" in rendered
    assert "$0" not in rendered
    assert "none disclosed" not in rendered


def test_compensation_renderer_preserves_currency_without_unsourced_benchmark() -> None:
    body = StringIO()
    _exec_comp_tab(
        body,
        ExecCompSectionModel(
            status=SectionStatus.OK,
            ticker="SYNTH",
            packages=[
                ExecCompRowModel(
                    executive_name="EUR executive",
                    fiscal_year=2025,
                    currency="EUR",
                    total_comp_granted=189000.0,
                    is_ceo=True,
                    ceo_pay_ratio=212.0,
                ),
                ExecCompRowModel(executive_name="USD executive", fiscal_year=2025, currency="USD"),
            ],
            insider_signals=[
                InsiderSignalRowModel(
                    insider_name="Synthetic insider",
                    transaction_date="2025-01-01",
                    transaction_type="buy",
                    shares=100.0,
                    transaction_value=189000.0,
                    signal_strength=0.5,
                    rationale="Synthetic test only",
                )
            ],
        ),
    )
    rendered = body.getvalue()
    assert "mixed currencies" in rendered
    assert "EUR 189K" in rendered
    assert "189K (currency unavailable)" in rendered
    assert "$189" not in rendered
    assert "300x" not in rendered
    assert "212x" in rendered
