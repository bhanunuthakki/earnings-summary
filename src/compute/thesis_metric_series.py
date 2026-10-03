"""Source-bound quarterly calculations shared by thesis warnings and hard rules.

Cash-flow inputs must already be admitted standalone quarters. This reader does
not reinterpret a year-to-date source as a quarter or publish new observations.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from compute.kpi_resolver import (
    KpiRevisionSeriesStatus,
    resolve_kpi_definition_name,
    resolve_revision_aware_kpi_series,
    semantic_series_identity_sql,
)
from models.facts import Unit
from models.unit_convert import convert_unit
from pipeline.kpi_semantics import semantic_admission_sql
from provenance.financial_fact_resolution import canonical_fact_relation
from provenance.overrides import KPI, active_scalar_override_map
from sources.canonical_financial_series import (
    CanonicalFinancialSeriesReader,
    FinancialCadence,
    SeriesContinuity,
)


class MetricExpression(BaseModel):
    """Closed calculation tree; all monetary ratios are returned in percent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["level", "ttm_ratio", "yoy_pp", "difference", "ttm_fcf_margin"]
    name: str | None = None
    source: Literal["financial", "kpi"] = "financial"
    numerator: MetricExpression | None = None
    denominator: MetricExpression | None = None
    input: MetricExpression | None = None
    left: MetricExpression | None = None
    right: MetricExpression | None = None
    # A company's reconciled adjusted EBITDA / GAAP revenue is a deliberate
    # mixed-basis calculation. It must be requested explicitly by the owner.
    allow_mixed_basis: bool = False
    # Optional source-bound population requirements. A market comparison must
    # name its geography, accommodation category and denominator here; absence
    # of that metadata in admitted source context cannot establish parity.
    required_dimensions: dict[str, str] = Field(default_factory=dict)
    allow_distinct_entities: bool = False

    @model_validator(mode="after")
    def _operands(self) -> MetricExpression:
        if self.operation == "level" and not self.name:
            raise ValueError("level requires a metric name")
        if self.operation == "ttm_ratio" and (self.numerator is None or self.denominator is None):
            raise ValueError("ttm_ratio requires numerator and denominator")
        if self.operation == "yoy_pp" and self.input is None:
            raise ValueError("yoy_pp requires input")
        if self.operation == "difference" and (self.left is None or self.right is None):
            raise ValueError("difference requires left and right")
        if self.allow_distinct_entities:
            if self.operation != "difference" or self.left is None or self.right is None:
                raise ValueError("distinct entities require a difference expression")
            required = {"geography", "lodging_category", "measurement", "window"}
            if (
                self.left.operation != "level"
                or self.right.operation != "level"
                or self.left.required_dimensions != self.right.required_dimensions
                or not required.issubset(self.left.required_dimensions)
            ):
                raise ValueError("distinct entities require matching explicit source populations")
        return self


class MetricSeriesPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period_end: datetime
    value: Decimal
    fiscal_year: int
    fiscal_period: str
    period_start: datetime | None = None
    reporting_entity_id: str
    unit: str
    currency: str | None
    accounting_basis: str
    consolidation_scope: str
    dimensions: str = "{}"

    @property
    def fiscal_index(self) -> int:
        return self.fiscal_year * 4 + int(self.fiscal_period[1]) - 1


class MetricSeriesResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["available", "unavailable"]
    reason_code: str | None = None
    points: tuple[MetricSeriesPoint, ...] = ()
    source_manifests: dict[str, object] = Field(default_factory=dict)


def _unavailable(reason: str, manifests: dict[str, object]) -> MetricSeriesResult:
    return MetricSeriesResult(status="unavailable", reason_code=reason, source_manifests=manifests)


def _available(points: list[MetricSeriesPoint], manifests: dict[str, object]) -> MetricSeriesResult:
    if not points:
        return _unavailable("insufficient_comparable_quarterly_history", manifests)
    if any(not point.value.is_finite() for point in points):
        return _unavailable("nonfinite_metric_value", manifests)
    if len({point.period_end for point in points}) != len(points):
        return _unavailable("duplicate_quarterly_metric", manifests)
    for older, newer in pairwise(points):
        if (
            newer.fiscal_index != older.fiscal_index + 1
            or not 70 <= (newer.period_end - older.period_end).days <= 110
        ):
            return _unavailable("missing_adjacent_quarter", manifests)
    return MetricSeriesResult(status="available", points=tuple(points), source_manifests=manifests)


def _read_financial(reader: CanonicalFinancialSeriesReader, name: str) -> MetricSeriesResult:
    series = reader.read(
        name, cadence=FinancialCadence.QUARTERLY, continuity=SeriesContinuity.STRICT_CONTIGUOUS
    )
    manifests: dict[str, object] = {f"financial:{name}": series.manifest()}
    if series.status != "available":
        return _unavailable(
            series.reason_code or "canonical_financial_metric_unavailable", manifests
        )
    points = [
        MetricSeriesPoint(
            period_end=item.period_end,
            period_start=item.period_start,
            value=item.value,
            fiscal_year=item.fiscal_year,
            fiscal_period=item.fiscal_period,
            reporting_entity_id=item.reporting_entity_id,
            unit=item.unit,
            currency=item.currency,
            accounting_basis=item.accounting_basis,
            consolidation_scope=item.consolidation_scope,
            dimensions=json.dumps(
                [dimension.canonical_member for dimension in item.dimensions],
                sort_keys=True,
                separators=(",", ":"),
            )
            if item.dimensions
            else "{}",
        )
        for item in series.observations
    ]
    return _available(points, manifests)


_EXACT_SOURCE_NUMBER = re.compile(
    r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:%|bps|USD|EUR|GBP|million(?:s)?|billion(?:s)?)?$",
    re.IGNORECASE,
)


def _read_kpi(
    conn: sqlite3.Connection, ticker: str, name: str, cutoff: datetime
) -> MetricSeriesResult:
    manifests: dict[str, object] = {}
    resolved_name = resolve_kpi_definition_name(conn, ticker, name)
    if resolved_name is None:
        return _unavailable("admitted_kpi_definition_unavailable", manifests)
    if active_scalar_override_map(conn, ticker=ticker, fact_kind=KPI, fact_key=resolved_name):
        return _unavailable("kpi_unreviewed_scalar_override", manifests)
    definition = conn.execute(
        "SELECT id FROM kpi_definitions WHERE ticker=? AND name=?",
        (ticker.upper(), resolved_name),
    ).fetchone()
    if definition is None:
        return _unavailable("admitted_kpi_definition_unavailable", manifests)
    resolution = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=int(definition[0]),
        effective_at=cutoff,
        known_at=cutoff,
    )
    manifests[f"kpi:{resolved_name}"] = resolution.model_dump(mode="json")
    if resolution.status is not KpiRevisionSeriesStatus.ELIGIBLE or resolution.breaks:
        return _unavailable(f"kpi_definition_{resolution.status.value}", manifests)
    if resolution.exclusions:
        return _unavailable("kpi_definition_history_excluded", manifests)
    if not resolution.eligible_fact_ids:
        return _unavailable("admitted_kpi_history_unavailable", manifests)
    relation = canonical_fact_relation(conn, "kpi_facts")
    semantic_join, semantic_where = semantic_admission_sql(conn, fail_closed=True)
    identity_where = semantic_series_identity_sql(conn, fact_relation=relation.sql)
    rows = conn.execute(
        "SELECT kf.id,kf.period_end,kf.fiscal_period_type,kf.value,kf.unit,kf.currency, "
        "kf.source_doc_id,kf.locator,ksc.source_value_text,ksc.accounting_basis, "
        "ksc.consolidation_scope,ksc.dimensions_json,ksc.kpi_definition_revision_id, "
        "revision.reporting_entity_id,revision.source_document_version_id, "
        "(SELECT binding.observation_id FROM fact_observation_revisions binding "
        "WHERE binding.fact_table='kpi_facts' AND binding.fact_row_id=kf.id "
        "ORDER BY binding.fact_revision DESC LIMIT 1) "
        f"FROM {relation.sql} kf {semantic_join} "  # nosec B608 -- closed canonical relation
        "JOIN kpi_definition_revisions revision "
        "ON revision.kpi_definition_revision_id=ksc.kpi_definition_revision_id "
        "WHERE kf.ticker=? AND kf.kpi_definition_id=? AND "
        + semantic_where
        + " AND "
        + identity_where
        + " AND kf.fiscal_period_type IN ('Q1','Q2','Q3','Q4') ORDER BY kf.period_end",
        (ticker.upper(), int(definition[0])),
    ).fetchall()
    eligible = set(resolution.eligible_fact_ids)
    points: list[MetricSeriesPoint] = []
    observation_manifests: list[dict[str, object]] = []
    signatures: set[tuple[str, ...]] = set()
    for row in rows:
        if int(row[0]) not in eligible:
            continue
        period = datetime.fromisoformat(str(row[1])).replace(tzinfo=UTC)
        if period > cutoff:
            return _unavailable("future_kpi_period", manifests)
        text = str(row[8] or "").strip()
        if not text:
            return _unavailable("kpi_source_precision_unavailable", manifests)
        if not _EXACT_SOURCE_NUMBER.fullmatch(text):
            return _unavailable("approximate_kpi_source_value", manifests)
        source_unit = Unit(str(row[4]))
        value = Decimal(str(row[3]))
        currency = str(row[5]) if row[5] is not None else None
        if source_unit in (Unit.RATIO, Unit.PERCENT, Unit.BPS):
            converted = convert_unit(value, source_unit, Unit.PERCENT)
            if converted is None:
                return _unavailable("kpi_unit_unavailable", manifests)
            value, unit = converted, "percent"
        elif source_unit in (Unit.ACTUAL, Unit.THOUSANDS, Unit.MILLIONS, Unit.BILLIONS):
            if not currency:
                return _unavailable("kpi_monetary_currency_unavailable", manifests)
            converted = convert_unit(value, source_unit, Unit.ACTUAL)
            if converted is None:
                return _unavailable("kpi_unit_unavailable", manifests)
            value, unit = converted, currency
        else:
            unit = source_unit.value
        signatures.add((str(row[9]), str(row[10]), str(row[11]), unit, currency or ""))
        quarter = int(str(row[2])[1])
        # KPI storage lacks fiscal_year. Infer the year containing its fiscal
        # Q4 end from the admitted source quarter label, never from row order.
        fiscal_year = period.year + int(period.month + (4 - quarter) * 3 > 12)
        points.append(
            MetricSeriesPoint(
                period_end=period,
                value=value,
                fiscal_year=fiscal_year,
                fiscal_period=str(row[2]),
                reporting_entity_id=str(row[13]),
                unit=unit,
                currency=currency,
                accounting_basis=str(row[9]),
                consolidation_scope=str(row[10]),
                dimensions=str(row[11]),
            )
        )
        observation_manifests.append(
            {
                "kpi_fact_id": int(row[0]),
                "period_end": period.isoformat(),
                "definition_revision_id": str(row[12]),
                "source_document_id": int(row[6]),
                "locator": str(row[7]),
                "source_value_text": text,
                "stored_unit": str(row[4]),
                "stored_value": str(row[3]),
                "currency": currency,
                "document_version_id": str(row[14]),
                "observation_id": str(row[15]),
            }
        )
    manifests[f"kpi:{resolved_name}:observations"] = observation_manifests
    if len(signatures) > 1:
        return _unavailable("incomparable_kpi_series_coordinate", manifests)
    return _available(points, manifests)


def _matched(
    left: MetricSeriesResult,
    right: MetricSeriesResult,
    *,
    allow_mixed_basis: bool = False,
    allow_distinct_entities: bool = False,
) -> tuple[list[tuple[MetricSeriesPoint, MetricSeriesPoint]], str | None]:
    if left.status != "available" or right.status != "available":
        return [], left.reason_code or right.reason_code or "expression_input_unavailable"
    if not left.points or not right.points:
        return [], "expression_input_unavailable"
    if left.points[-1].period_end != right.points[-1].period_end:
        return [], "expression_exact_period_mismatch"
    # Sources can begin in different years. Trim only the older prefix; do not
    # intersect away an interior gap or accept one source's stale latest quarter.
    common_start = max(left.points[0].period_end, right.points[0].period_end)
    left_points = [point for point in left.points if point.period_end >= common_start]
    right_points = [point for point in right.points if point.period_end >= common_start]
    if [point.period_end for point in left_points] != [point.period_end for point in right_points]:
        return [], "expression_exact_period_mismatch"
    pairs = list(zip(left_points, right_points, strict=True))
    for a, b in pairs:
        if (
            a.fiscal_year,
            a.fiscal_period,
            a.unit,
            a.currency,
            a.consolidation_scope,
            a.dimensions,
        ) != (
            b.fiscal_year,
            b.fiscal_period,
            b.unit,
            b.currency,
            b.consolidation_scope,
            b.dimensions,
        ) or (
            a.period_start is not None
            and b.period_start is not None
            and a.period_start != b.period_start
        ):
            return [], "expression_input_coordinate_mismatch"
        if not allow_distinct_entities and a.reporting_entity_id != b.reporting_entity_id:
            return [], "expression_input_coordinate_mismatch"
        if not allow_mixed_basis and a.accounting_basis != b.accounting_basis:
            return [], "expression_accounting_basis_mismatch"
    return pairs, None


def _calculate(
    conn: sqlite3.Connection,
    ticker: str,
    expression: MetricExpression,
    reader: CanonicalFinancialSeriesReader,
    cutoff: datetime,
) -> MetricSeriesResult:
    if expression.operation == "level":
        assert expression.name is not None
        result = (
            _read_financial(reader, expression.name)
            if expression.source == "financial"
            else _read_kpi(conn, ticker, expression.name, cutoff)
        )
        if result.status == "available" and expression.required_dimensions:
            for point in result.points:
                dimensions_value: object = json.loads(point.dimensions)
                if not isinstance(dimensions_value, dict):
                    return _unavailable(
                        "required_source_population_unavailable", result.source_manifests
                    )
                dimensions = cast("dict[str, object]", dimensions_value)
                if not all(
                    dimensions.get(key) == value
                    for key, value in expression.required_dimensions.items()
                ):
                    return _unavailable(
                        "required_source_population_unavailable", result.source_manifests
                    )
        return result
    if expression.operation == "yoy_pp":
        assert expression.input is not None
        source = _calculate(conn, ticker, expression.input, reader, cutoff)
        if source.status != "available":
            return source
        by_quarter = {point.fiscal_index: point for point in source.points}
        points: list[MetricSeriesPoint] = []
        for point in source.points:
            prior = by_quarter.get(point.fiscal_index - 4)
            if prior is None:
                continue
            if point.unit != "percent" or prior.unit != "percent":
                return _unavailable("yoy_pp_requires_percentage_input", source.source_manifests)
            if (point.period_end - prior.period_end).days not in range(350, 381):
                return _unavailable("yoy_exact_year_mismatch", source.source_manifests)
            points.append(point.model_copy(update={"value": point.value - prior.value}))
        return _available(points, source.source_manifests)
    if expression.operation == "ttm_fcf_margin":
        operating = _read_financial(reader, "operating_cash_flow")
        capex = _read_financial(reader, "capital_expenditure")
        revenue = _read_financial(reader, "revenue")
        manifests = {
            **operating.source_manifests,
            **capex.source_manifests,
            **revenue.source_manifests,
        }
        pairs, reason = _matched(operating, capex)
        if reason:
            return _unavailable(reason, manifests)
        if any(capital.value > 0 for _, capital in pairs):
            return _unavailable("capital_expenditure_requires_signed_outflow", manifests)
        fcf = _available(
            [
                cash.model_copy(update={"value": cash.value + capital.value})
                for cash, capital in pairs
            ],
            manifests,
        )
        return _rolling_ratio(fcf, revenue, allow_mixed_basis=False)
    if expression.operation == "ttm_ratio":
        assert expression.numerator is not None and expression.denominator is not None
        numerator = _calculate(conn, ticker, expression.numerator, reader, cutoff)
        denominator = _calculate(conn, ticker, expression.denominator, reader, cutoff)
        return _rolling_ratio(
            numerator, denominator, allow_mixed_basis=expression.allow_mixed_basis
        )
    assert expression.left is not None and expression.right is not None
    left = _calculate(conn, ticker, expression.left, reader, cutoff)
    right = _calculate(conn, ticker, expression.right, reader, cutoff)
    manifests = {**left.source_manifests, **right.source_manifests}
    pairs, reason = _matched(
        left,
        right,
        allow_mixed_basis=expression.allow_mixed_basis,
        allow_distinct_entities=expression.allow_distinct_entities,
    )
    if reason:
        return _unavailable(reason, manifests)
    return _available(
        [a.model_copy(update={"value": a.value - b.value}) for a, b in pairs], manifests
    )


def _rolling_ratio(
    numerator: MetricSeriesResult,
    denominator: MetricSeriesResult,
    *,
    allow_mixed_basis: bool,
) -> MetricSeriesResult:
    manifests = {**numerator.source_manifests, **denominator.source_manifests}
    pairs, reason = _matched(numerator, denominator, allow_mixed_basis=allow_mixed_basis)
    if reason:
        return _unavailable(reason, manifests)
    if any(point.currency is None for pair in pairs for point in pair):
        return _unavailable("ttm_ratio_requires_monetary_inputs", manifests)
    # A Q2 checkpoint can disclose either a quarter or a year-to-date amount.
    # KPI storage supplies an end date but no typed start date. It cannot yet
    # prove four standalone durations, so those sums remain explicitly pending.
    if any(point.period_start is None for pair in pairs for point in pair):
        return _unavailable("ttm_input_duration_unavailable", manifests)
    points: list[MetricSeriesPoint] = []
    for end in range(3, len(pairs)):
        window = pairs[end - 3 : end + 1]
        denominator_sum = sum((b.value for _, b in window), Decimal(0))
        if denominator_sum <= 0:
            return _unavailable("nonpositive_ttm_denominator", manifests)
        value = Decimal(100) * sum((a.value for a, _ in window), Decimal(0)) / denominator_sum
        latest = window[-1][0]
        points.append(
            latest.model_copy(
                update={
                    "value": value,
                    "unit": "percent",
                    "currency": None,
                    "period_start": window[0][0].period_start,
                    "accounting_basis": "mixed_explicit"
                    if allow_mixed_basis
                    else latest.accounting_basis,
                }
            )
        )
    return _available(points, manifests)


def calculate_metric_series(
    conn: sqlite3.Connection,
    ticker: str,
    expression: MetricExpression,
    cutoff: datetime | None = None,
) -> MetricSeriesResult:
    """Read one caller-aware snapshot, without writes or raw-fact fallback."""
    known_at = cutoff or datetime.now(UTC)
    if known_at.tzinfo is None:
        raise ValueError("metric cutoff requires a timezone-aware clock")
    known_at = known_at.astimezone(UTC)
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        reader = CanonicalFinancialSeriesReader(conn, ticker, cutoff=known_at)
        result = _calculate(conn, ticker, expression, reader, known_at)
        return result.model_copy(
            update={
                "source_manifests": {
                    **result.source_manifests,
                    "expression": expression.model_dump(mode="json", exclude_none=True),
                    "knowledge_cutoff": known_at.isoformat(),
                }
            }
        )
    except (sqlite3.Error, ValueError) as exc:
        return _unavailable(
            "metric_source_schema_or_value_unavailable",
            {
                "expression": expression.model_dump(mode="json", exclude_none=True),
                "error_type": type(exc).__name__,
                "knowledge_cutoff": known_at.isoformat(),
            },
        )
    finally:
        if owns_snapshot and conn.in_transaction:
            conn.rollback()
