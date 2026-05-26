"""Long-form financial / segment / KPI measurements.

Each fact: (ticker, period_end, line_item, value, currency, unit) keyed on a
source Document via source_doc_id. Long form is intentional — wide tables
explode when adding a metric, and we cannot afford to lose provenance per cell.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class FiscalPeriodType(StrEnum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    H1 = "H1"
    H2 = "H2"
    FY = "FY"
    TTM = "TTM"


class Currency(StrEnum):
    """Issuer reporting currencies tracked across the book.

    Add values when a new ticker enters the book reporting in a new currency.
    Never default to USD — the source must declare it.
    """

    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    DKK = "DKK"
    BRL = "BRL"
    CAD = "CAD"
    INR = "INR"
    AUD = "AUD"
    KRW = "KRW"
    JPY = "JPY"
    CHF = "CHF"


class Unit(StrEnum):
    ACTUAL = "actual"
    THOUSANDS = "thousands"
    MILLIONS = "millions"
    BILLIONS = "billions"
    PERCENT = "percent"
    RATIO = "ratio"
    BPS = "bps"
    COUNT = "count"


class FinancialFact(BaseModel):
    """One atomic financial measurement, fully provenance-tagged."""

    id: int | None = None
    ticker: str
    period_end: datetime
    fiscal_period_type: FiscalPeriodType
    line_item: str
    value: Decimal
    currency: Currency | None
    unit: Unit
    source_doc_id: int
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class SegmentFact(BaseModel):
    """Segment-level measurement (revenue, OI, capex, headcount)."""

    id: int | None = None
    ticker: str
    period_end: datetime
    fiscal_period_type: FiscalPeriodType
    segment_name: str
    metric: str
    value: Decimal
    currency: Currency | None
    unit: Unit
    source_doc_id: int


class KpiFact(BaseModel):
    """Leading-indicator KPI value tied to a KpiDefinition."""

    id: int | None = None
    ticker: str
    period_end: datetime
    fiscal_period_type: FiscalPeriodType
    kpi_definition_id: int
    value: Decimal
    unit: Unit
    source_doc_id: int


class SegmentDimType(StrEnum):
    """Allowed dim_type values on segment_dimensions rows.

    A cross-tab cell carries one dim_type per axis. Adding a new dim_type
    means extending this enum and (usually) teaching the renderer about a
    new secondary-expansion bucket.
    """

    PRODUCT = "product"
    GEOGRAPHY = "geography"
    CHANNEL = "channel"
    CUSTOMER_SEGMENT = "customer_segment"
    BUSINESS_UNIT = "business_unit"


class SegmentPeriod(BaseModel):
    """Time + provenance anchor for one or more segment dimension cells.

    Unique by (ticker, period_end, fiscal_period_type, source_doc_id) — the
    same shape as segment_facts.uq_provenance so a single FMP / 10-K
    extraction maps to one period row.
    """

    id: int | None = None
    ticker: str
    period_end: datetime
    fiscal_period_type: FiscalPeriodType
    source_doc_id: int
    currency: Currency | None = None
    unit: Unit
    created_at: datetime | None = None


class SegmentDimension(BaseModel):
    """One cell of a segment cross-section.

    `dim_type` says WHICH axis the label sits on (product vs geography vs ...);
    `dim_name` is the label itself (e.g. "AWS", "United States"); `metric` is
    the measurement kind (revenue / operating_income / ...). Multiple cells
    sharing a period_id form a cross-section or a true cross-tab.

    `unit` is an optional per-dim override of the period's unit. Used when a
    period anchor needs to host mixed-unit cells — most often Unit.ACTUAL
    (revenue / OI / capex) alongside Unit.COUNT (headcount) under the same
    (ticker, period, source_doc) tuple. Leave it `None` to inherit the
    period row's unit; readers compute the effective unit as
    `COALESCE(sd.unit, sp.unit)`.
    """

    id: int | None = None
    period_id: int | None = None
    dim_type: SegmentDimType
    dim_name: str
    value: Decimal
    metric: str
    unit: Unit | None = None
