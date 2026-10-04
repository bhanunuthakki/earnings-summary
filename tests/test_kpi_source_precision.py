"""A plain numeric cell does not discard a reviewed table qualifier."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from compute.thesis_metric_series import MetricExpression, calculate_metric_series
from pipeline.kpi_semantics import (
    KpiSourcePrecision,
    current_kpi_semantic_context,
    persist_kpi_semantic_context,
)
from tests.fixtures.kpi_revision_setup import NOW, semantic_fixture
from tests.test_thesis_metric_series import kpi_database_fixture


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
