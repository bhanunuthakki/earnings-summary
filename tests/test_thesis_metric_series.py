"""Calculated thesis metrics use admitted, comparable quarterly observations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from compute import thesis_metric_series
from compute.thesis_metric_series import (
    MetricExpression,
    MetricSeriesPoint,
    MetricSeriesResult,
    calculate_metric_series,
)
from models.facts import Unit
from pipeline.kpi_definition_revisions import persist_kpi_definition_revision
from pipeline.kpi_semantics import persist_kpi_semantic_context
from tests import test_source_fact_repository as foundation
from tests.fixtures.kpi_revision_setup import (
    NOW,
    definition_fixture,
    fact_fixture,
    revision_database,
    semantic_fixture,
)
from tests.test_report_canonical_financials import seed_table


@pytest.fixture
def canonical_conn(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Generator[sqlite3.Connection, None, None]:
    factory = cast(
        Callable[..., Generator[sqlite3.Connection, None, None]],
        getattr(foundation.conn, "__wrapped__"),
    )
    for database in factory(tmp_path, migrated_db):
        database.row_factory = sqlite3.Row
        yield database


def _rows(series: dict[str, list[int]]) -> list[tuple[str, str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for metric, values in series.items():
        for index, value in enumerate(values):
            year = 2022 + index // 4
            quarter = index % 4 + 1
            month = quarter * 3
            day = 31 if month in (3, 12) else 30
            rows.append(
                (
                    metric,
                    f"{year}-{month - 2:02d}-01",
                    f"{year}-{month:02d}-{day}",
                    f"Q{quarter}",
                    str(value),
                    "USD",
                )
            )
    return rows


def _level(name: str) -> MetricExpression:
    return MetricExpression(operation="level", source="financial", name=name)


def test_ttm_ratio_is_ratio_of_sums_and_retains_sources(canonical_conn: sqlite3.Connection) -> None:
    seed_table(
        canonical_conn,
        _rows({"revenue": [100, 200, 100, 100, 100], "operating_income": [10, 40, 10, 10, 20]}),
    )
    expression = MetricExpression(
        operation="ttm_ratio", numerator=_level("operating_income"), denominator=_level("revenue")
    )
    result = calculate_metric_series(canonical_conn, "SYNTH", expression)
    assert result.status == "available"
    assert [point.value for point in result.points] == [Decimal(14), Decimal(16)]
    assert [point.fiscal_period for point in result.points] == ["Q4", "Q1"]
    assert result.source_manifests
    assert "observation_id" in str(result.source_manifests)


def test_yoy_pp_uses_year_matched_checkpoint(canonical_conn: sqlite3.Connection) -> None:
    seed_table(
        canonical_conn, _rows({"revenue": [100] * 9, "operating_income": [10] * 4 + [20] * 5})
    )
    ratio = MetricExpression(
        operation="ttm_ratio", numerator=_level("operating_income"), denominator=_level("revenue")
    )
    result = calculate_metric_series(
        canonical_conn, "SYNTH", MetricExpression(operation="yoy_pp", input=ratio)
    )
    assert result.status == "available"
    assert [point.value for point in result.points] == [Decimal(10), Decimal("7.5")]


@pytest.mark.parametrize("shorter_metric", ["revenue", "operating_income"])
def test_ttm_ratio_matches_common_start_and_retains_full_sources(
    canonical_conn: sqlite3.Connection, shorter_metric: str
) -> None:
    rows = _rows({"revenue": [100] * 9, "operating_income": [10] * 9})
    rows = [row for row in rows if row[0] != shorter_metric or row[2] >= "2023-03-31"]
    seed_table(canonical_conn, rows)
    result = calculate_metric_series(
        canonical_conn,
        "SYNTH",
        MetricExpression(
            operation="ttm_ratio",
            numerator=_level("operating_income"),
            denominator=_level("revenue"),
        ),
    )
    assert result.status == "available"
    assert [point.value for point in result.points] == [Decimal(10), Decimal(10)]
    assert [point.period_end.date().isoformat() for point in result.points] == [
        "2023-12-31",
        "2024-03-31",
    ]
    for metric in ("revenue", "operating_income"):
        manifest = result.source_manifests[f"financial:{metric}"]
        assert isinstance(manifest, dict)
        source_manifest = cast("dict[str, object]", manifest)
        observations = source_manifest["observations"]
        assert isinstance(observations, list)
        source_observations = cast("list[object]", observations)
        assert len(source_observations) == (5 if metric == shorter_metric else 9)


def test_fcf_matches_three_different_starts(canonical_conn: sqlite3.Connection) -> None:
    rows = _rows(
        {
            "revenue": [100] * 9,
            "operating_cash_flow": [40] * 9,
            "capital_expenditure": [-10] * 9,
        }
    )
    starts = {
        "revenue": "2022-03-31",
        "operating_cash_flow": "2022-09-30",
        "capital_expenditure": "2023-03-31",
    }
    seed_table(canonical_conn, [row for row in rows if row[2] >= starts[row[0]]])
    result = calculate_metric_series(
        canonical_conn, "SYNTH", MetricExpression(operation="ttm_fcf_margin")
    )
    assert result.status == "available"
    assert [point.value for point in result.points] == [Decimal(30), Decimal(30)]
    for metric, count in (
        ("revenue", 9),
        ("operating_cash_flow", 7),
        ("capital_expenditure", 5),
    ):
        manifest = result.source_manifests[f"financial:{metric}"]
        assert isinstance(manifest, dict)
        source_manifest = cast("dict[str, object]", manifest)
        observations = source_manifest["observations"]
        assert isinstance(observations, list)
        source_observations = cast("list[object]", observations)
        assert len(source_observations) == count


@pytest.mark.parametrize("shorter_metric", ["revenue", "operating_income"])
def test_ttm_ratio_rejects_unequal_latest_periods(
    canonical_conn: sqlite3.Connection, shorter_metric: str
) -> None:
    rows = _rows({"revenue": [100] * 5, "operating_income": [10] * 5})
    rows = [row for row in rows if row[0] != shorter_metric or row[2] != "2023-03-31"]
    seed_table(canonical_conn, rows)
    result = calculate_metric_series(
        canonical_conn,
        "SYNTH",
        MetricExpression(
            operation="ttm_ratio",
            numerator=_level("operating_income"),
            denominator=_level("revenue"),
        ),
    )
    assert result.status == "unavailable"
    assert result.reason_code == "expression_exact_period_mismatch"
    assert result.points == ()
    assert "financial:revenue" in result.source_manifests
    assert "financial:operating_income" in result.source_manifests


def test_one_source_interior_gap_is_unresolved(canonical_conn: sqlite3.Connection) -> None:
    rows = _rows({"revenue": [100] * 5, "operating_income": [10] * 5})
    rows = [row for row in rows if row[0] != "operating_income" or row[2] != "2022-06-30"]
    seed_table(canonical_conn, rows)
    result = calculate_metric_series(
        canonical_conn,
        "SYNTH",
        MetricExpression(
            operation="ttm_ratio",
            numerator=_level("operating_income"),
            denominator=_level("revenue"),
        ),
    )
    assert result.status == "unavailable"
    assert result.reason_code == "quarterly_duration_gap_or_overlap"
    assert result.points == ()


def test_fcf_uses_signed_cash_outflow(canonical_conn: sqlite3.Connection) -> None:
    seed_table(
        canonical_conn,
        _rows(
            {
                "revenue": [100] * 5,
                "operating_cash_flow": [40] * 5,
                "capital_expenditure": [-10] * 5,
            }
        ),
    )
    result = calculate_metric_series(
        canonical_conn, "SYNTH", MetricExpression(operation="ttm_fcf_margin")
    )
    assert result.status == "available"
    assert [point.value for point in result.points] == [Decimal(30), Decimal(30)]


def test_positive_capex_is_unresolved(canonical_conn: sqlite3.Connection) -> None:
    seed_table(
        canonical_conn,
        _rows(
            {"revenue": [100] * 4, "operating_cash_flow": [40] * 4, "capital_expenditure": [10] * 4}
        ),
    )
    result = calculate_metric_series(
        canonical_conn, "SYNTH", MetricExpression(operation="ttm_fcf_margin")
    )
    assert result.status == "unavailable"
    assert result.reason_code == "capital_expenditure_requires_signed_outflow"


def test_quarter_gap_never_becomes_ttm(canonical_conn: sqlite3.Connection) -> None:
    rows = _rows({"revenue": [100] * 5, "operating_income": [10] * 5})
    rows = [row for row in rows if row[2] != "2022-06-30"]
    seed_table(canonical_conn, rows)
    result = calculate_metric_series(
        canonical_conn,
        "SYNTH",
        MetricExpression(
            operation="ttm_ratio",
            numerator=_level("operating_income"),
            denominator=_level("revenue"),
        ),
    )
    assert result.status == "unavailable"
    assert result.reason_code == "quarterly_duration_gap_or_overlap"


def test_cumulative_cash_flow_is_not_a_quarter(canonical_conn: sqlite3.Connection) -> None:
    rows = _rows({"revenue": [100] * 4, "capital_expenditure": [-10] * 4})
    rows += [("operating_cash_flow", "2022-01-01", "2022-09-30", "Q3", "120", "USD")]
    seed_table(canonical_conn, rows)
    result = calculate_metric_series(
        canonical_conn, "SYNTH", MetricExpression(operation="ttm_fcf_margin")
    )
    assert result.status == "unavailable"
    assert result.points == ()


def test_mixed_currency_ratio_is_unresolved(canonical_conn: sqlite3.Connection) -> None:
    rows = _rows({"revenue": [100] * 4, "operating_income": [10] * 4})
    seed_table(canonical_conn, rows, currencies={index: "EUR" for index in range(4, 8)})
    result = calculate_metric_series(
        canonical_conn,
        "SYNTH",
        MetricExpression(
            operation="ttm_ratio",
            numerator=_level("operating_income"),
            denominator=_level("revenue"),
        ),
    )
    assert result.status == "unavailable"
    assert result.reason_code == "expression_input_coordinate_mismatch"


def test_raw_financial_rows_do_not_supply_a_metric() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        result = calculate_metric_series(conn, "BKNG", _level("revenue"))
        assert result.status == "unavailable"
        assert result.reason_code == "canonical_financial_schema_unavailable"
    finally:
        conn.close()


def test_cutoff_requires_aware_clock(canonical_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="timezone"):
        calculate_metric_series(
            canonical_conn, "SYNTH", _level("revenue"), cutoff=datetime(2026, 1, 1)
        )
    result = calculate_metric_series(
        canonical_conn, "SYNTH", _level("revenue"), cutoff=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert result.status == "unavailable"


def _kpi_database(text: str = "12.5") -> sqlite3.Connection:
    conn = revision_database()
    definition = persist_kpi_definition_revision(conn, definition_fixture())
    fact_id = fact_fixture(conn)
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=semantic_fixture().model_copy(update={"source_value_text": text}),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )
    return conn


def test_admitted_kpi_level_has_revision_and_source_manifest() -> None:
    conn = _kpi_database()
    try:
        result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(operation="level", source="kpi", name="Monthly ARPAC"),
            cutoff=NOW,
        )
        assert result.status == "available"
        assert result.points[0].value == Decimal("12.5")
        assert result.points[0].currency == "USD"
        assert result.points[0].unit == "USD"
        assert "definition-r1" in str(result.source_manifests)
        assert "source_value_text" in str(result.source_manifests)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "text", ["mid-60s", "approximately 12.5", "12\u201313", "~12.5", "<13", ""]
)
def test_approximate_or_missing_kpi_precision_is_unresolved(text: str) -> None:
    conn = _kpi_database(text)
    try:
        result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(operation="level", source="kpi", name="Monthly ARPAC"),
            cutoff=NOW,
        )
        assert result.status == "unavailable"
        assert result.reason_code == (
            "approximate_kpi_source_value" if text else "kpi_source_precision_unavailable"
        )
        assert result.points == ()
    finally:
        conn.close()


def test_population_requirements_fail_closed() -> None:
    conn = _kpi_database()
    try:
        result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(
                operation="level",
                source="kpi",
                name="Monthly ARPAC",
                required_dimensions={"geography": "global", "lodging_category": "accommodation"},
            ),
            cutoff=NOW,
        )
        assert result.status == "unavailable"
        assert result.reason_code == "required_source_population_unavailable"
    finally:
        conn.close()


def test_kpi_proportion_is_converted_to_percentage_points() -> None:
    conn = revision_database()
    try:
        conn.execute("UPDATE kpi_definitions SET unit='ratio'")
        definition = persist_kpi_definition_revision(
            conn,
            definition_fixture(
                unit_family="ratio",
                unit_key=Unit.RATIO,
                currency_disposition="not_applicable",
                currency=None,
                stock_flow_behavior="ratio",
            ),
        )
        fact_id = fact_fixture(conn, value="0.65")
        conn.execute("UPDATE kpi_facts SET unit='ratio',currency=NULL WHERE id=?", (fact_id,))
        conn.execute(
            "UPDATE reported_observations SET unit='ratio',numeric_value='0.65',currency=NULL"
        )
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=fact_id,
            context=semantic_fixture().model_copy(update={"source_value_text": "0.65"}),
            reviewed_by="owner",
            knowledge_at=NOW,
            kpi_definition_revision_id=definition.kpi_definition_revision_id,
        )
        result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(operation="level", source="kpi", name="Monthly ARPAC"),
            cutoff=NOW,
        )
        assert result.status == "available"
        assert result.points[0].value == Decimal(65)
        assert result.points[0].unit == "percent"
    finally:
        conn.close()


def test_unbound_definition_never_supplies_a_kpi() -> None:
    conn = _kpi_database()
    try:
        conn.execute("UPDATE kpi_fact_semantic_contexts SET kpi_definition_revision_id=NULL")
        result = calculate_metric_series(
            conn,
            "NU",
            MetricExpression(operation="level", source="kpi", name="Monthly ARPAC"),
            cutoff=NOW,
        )
        assert result.status == "unavailable"
        assert result.points == ()
    finally:
        conn.close()


@pytest.mark.parametrize("permit", [False, True])
def test_company_market_difference_requires_explicit_population(
    monkeypatch: pytest.MonkeyPatch,
    permit: bool,
) -> None:
    population = {
        "geography": "global",
        "lodging_category": "all_accommodation",
        "measurement": "room_nights_booked",
        "window": "quarterly",
    }

    def read_level(_: object, metric: str) -> MetricSeriesResult:
        return MetricSeriesResult(
            status="available",
            points=(
                MetricSeriesPoint(
                    period_end=datetime(2025, 3, 31, tzinfo=UTC),
                    fiscal_year=2025,
                    fiscal_period="Q1",
                    value=Decimal(7 if metric == "company" else 9),
                    unit="percent",
                    currency=None,
                    reporting_entity_id=metric,
                    accounting_basis="management",
                    consolidation_scope="consolidated",
                    dimensions=json.dumps(population),
                ),
            ),
        )

    monkeypatch.setattr(thesis_metric_series, "_read_financial", read_level)
    expression = MetricExpression(
        operation="difference",
        left=MetricExpression(operation="level", name="company", required_dimensions=population),
        right=MetricExpression(operation="level", name="market", required_dimensions=population),
        allow_distinct_entities=permit,
    )
    conn = sqlite3.connect(":memory:")
    try:
        result = calculate_metric_series(conn, "SYNTH", expression)
        assert result.status == ("available" if permit else "unavailable")
        if permit:
            assert result.points[0].value == Decimal(-2)
        else:
            assert result.reason_code == "expression_input_coordinate_mismatch"
    finally:
        conn.close()


def test_distinct_entities_without_population_is_invalid() -> None:
    with pytest.raises(ValueError, match="matching explicit source populations"):
        MetricExpression(
            operation="difference",
            left=_level("revenue"),
            right=_level("operating_income"),
            allow_distinct_entities=True,
        )
