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
