"""Long-form financial / segment / KPI measurements.

Each fact: (ticker, period_end, line_item, value, currency, unit) keyed on a
source Document via source_doc_id. Long form is intentional — wide tables
explode when adding a metric, and we cannot afford to lose provenance per cell.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class FiscalPeriodType(StrEnum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    H1 = "H1"
    H2 = "H2"
    # Nine-months-ended cumulative period (a Q3 10-Q's YTD column) — the
    # missing sibling to H1/H2 (segment_quarterly_framework.md §4.2). Needed
    # so a Q3 filing's as-filed 9-months-ended cumulative column and its
    # derived discrete-Q3 value can coexist on the same (ticker, period_end,
    # source_doc_id) tuple without colliding on fiscal_period_type='Q3'.
    # Blast-radius note: any exhaustive if/elif/match over FiscalPeriodType
    # needs a safe default arm for this new member — see the framework doc's
    # §4.2 grep instruction before adding another exhaustive branch.
    NINE_MONTHS = "9M"
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
    TWD = "TWD"


class Unit(StrEnum):
    ACTUAL = "actual"
    THOUSANDS = "thousands"
    MILLIONS = "millions"
    BILLIONS = "billions"
    PERCENT = "percent"
    RATIO = "ratio"
    BPS = "bps"
    COUNT = "count"


class LocatorKind(StrEnum):
    """Discriminant for a v2 locator."""

    FMP_JSON_TABLE = "fmp_json_table"
    PDF_SLIDE = "pdf_slide"
    TRANSCRIPT_SPAN = "transcript_span"
    HTML_SPAN = "html_span"
    DERIVED = "derived"
    VENDOR_FIELD = "vendor_field"


class TableCellRef(BaseModel):
    """WHERE inside a cached JSON/HTML table a value sits."""

    section: str | None = None
    table_title: str | None = None
    row_label: str | None = None
    row_axis_path: list[str] = Field(default_factory=list[str])
    column_header: str | None = None
    cell_value_as_extracted: str | None = None
    json_path: str | None = None


class TranscriptSpanRef(BaseModel):
    doc_id: int | None = None
    transcript_line: int | None = None
    speaker_turn_index: int | None = None
    char_start: int | None = None
    char_end: int | None = None


class HtmlSpanRef(BaseModel):
    doc_id: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    quote: str | None = None


class VendorFieldRef(BaseModel):
    """The honest floor: a plain vendor endpoint value with no underlying
    filing/document at all (a live quote, a market-cap snapshot)."""

    endpoint: str
    field: str
    period: str | None = None


class DerivedInputRef(BaseModel):
    """One input to a derived value — mirrors kpi_facts.computed_from's
    existing inputs[] shape, promoted into the typed locator union."""

    ref: str
    fact_id: int | None = None
    item: str
    period_end: str | None = None
    doc_id: int | None = None
    tier: str | None = None


class DerivedRef(BaseModel):
    formula_id: int | None = None
    display: str | None = None
    method_flags: list[str] = Field(default_factory=list[str])
    inputs: list[DerivedInputRef] = Field(default_factory=list[DerivedInputRef])


class FactLocator(BaseModel):
    """Sub-document locator — WHERE inside the source document a value was read.

    Serialized to the nullable ``locator`` TEXT(JSON) column on
    financial_facts / kpi_facts (alembic 0075); contract documented in
    directives/data_provenance.md §7. All fields are optional — populate
    whichever the extractor actually knows; an all-empty locator serializes
    to None so the column stays NULL rather than holding a meaningless "{}".
    """

    # Parsed 10-K/10-Q section key (the top-level key in the
    # data/historical/fmp/{T}_form_10k_{Y}.json section-keyed text).
    section: str | None = None
    # Line id within the transcript the value was quoted from
    # (transcript_segments ordering for the source document).
    transcript_line: int | None = None
    # 1-based page number in the source PDF (IR decks, supplements).
    pdf_page: int | None = None
    # Pointer into an FMP JSON payload, e.g. "[3].netIncome" — record index
    # plus field name within the cached endpoint response.
    json_path: str | None = None

    locator_version: int = 1
    kind: LocatorKind | None = None
    table_cell: TableCellRef | None = None
    pdf_bbox: tuple[float, float, float, float] | None = None
    transcript_span: TranscriptSpanRef | None = None
    html_span: HtmlSpanRef | None = None
    vendor_field: VendorFieldRef | None = None
    derived: DerivedRef | None = None
    verbatim_snippet: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _kind_requires_matching_ref(self) -> FactLocator:
        """A ``kind`` set without its matching nested ref is invalid."""
        if self.kind is None:
            return self
        required: dict[LocatorKind, object | None] = {
            LocatorKind.FMP_JSON_TABLE: self.table_cell,
            LocatorKind.PDF_SLIDE: self.pdf_page,
            LocatorKind.TRANSCRIPT_SPAN: self.transcript_span,
            LocatorKind.HTML_SPAN: self.html_span,
            LocatorKind.DERIVED: self.derived,
            LocatorKind.VENDOR_FIELD: self.vendor_field,
        }
        if required[self.kind] is None:
            raise ValueError(
                f"FactLocator(kind={self.kind.value!r}) requires its matching nested ref"
            )
        return self

    def effective_kind(self) -> LocatorKind | None:
        """``kind`` if set (v2); else inferred from whichever v1 scalar is
        present. None when the locator carries nothing renderable."""
        if self.kind is not None:
            return self.kind
        if self.pdf_page is not None:
            return LocatorKind.PDF_SLIDE
        if self.transcript_line is not None:
            return LocatorKind.TRANSCRIPT_SPAN
        if self.section is not None or self.json_path is not None:
            return LocatorKind.FMP_JSON_TABLE
        return None

    def to_json(self) -> str | None:
        """Compact JSON for the DB column; None when no field is set.

        ``exclude_defaults`` (not ``exclude_none``) so ``locator_version``
        (default 1) and every unset v2 field disappear from a v1-shaped
        locator's JSON exactly as before this schema was extended — a
        pre-existing row's serialized shape is byte-identical whether or not
        the writer knows about v2 fields at all."""
        payload = self.model_dump(exclude_defaults=True, mode="json")
        if not payload:
            return None
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | None) -> FactLocator | None:
        """Inverse of to_json. None/blank input maps to None; malformed JSON
        raises (loud per the provenance contract, never silently dropped)."""
        if raw is None or not raw.strip():
            return None
        return cls.model_validate_json(raw)


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
    locator: FactLocator | None = None


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
    locator: FactLocator | None = None


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
    # --- segment_quarterly_framework.md §4.2 additive columns (alembic 0166) ---
    # 'discrete' | 'cumulative' | 'derived'. Default matches every pre-framework
    # row (all reported-discrete or FY-annual today).
    period_basis: str = "discrete"
    # The exact as-filed column header (e.g. "Six Months Ended June 30, 2025")
    # — audit trail back to the literal filing text. None for pre-framework rows.
    raw_period_label: str | None = None
    # Resolver version tag, e.g. "period_axis_v1". None for pre-framework rows.
    method_version: str | None = None


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
    # --- segment_quarterly_framework.md §4.1 additive columns (alembic 0165) ---
    # 'reported' | 'derived'. Mirrors kpi_facts/financial_facts provenance.
    disclosure_status: str = "reported"
    # e.g. "tenq_discrete_v1", "segment_q2q3_derive_v1", "llm:segment_crosstabs_v1".
    method_version: str | None = None
    # Mirrors financial_facts/kpi_facts.confidence — 1.0 for a deterministic,
    # reported-as-filed parse; derivation haircuts this (§2.6).
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    # Deterministic tag or "llm:<model>", same convention as kpi_facts.extracted_by.
    extracted_by: str | None = None
    # FactLocator-shaped JSON (section, json_path) — serialize via
    # FactLocator.to_json(), do not hand-roll a second locator shape.
    locator: str | None = None
    # kpi_facts.computed_from-shaped JSON: {"display": ..., "inputs": [...]}.
    derived_from: str | None = None
    # Recast chain link — self-FK to segment_dimensions.id (code-validated,
    # no DB-level FK per the FK-poisoning invariant).
    supersedes_id: int | None = None
