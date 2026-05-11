"""Pydantic models for the research-report payload.

ReportSpec is the single source of truth — every renderer (Markdown / PDF /
sections.json / xlsx) consumes one ReportSpec and emits its own format. Each
section carries an explicit `status` so a downstream renderer can show a clear
"missing — run stage X" stub instead of silently omitting content.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class SectionStatus(str, Enum):
    """Why a section is the shape it is."""

    OK = "ok"  # populated from real data
    MISSING_DATA = "missing_data"  # upstream pipeline stage hasn't run
    PARTIAL = "partial"  # some quarters/segments populated, others not
    LLM_PENDING = "llm_pending"  # LLM call deferred / not yet generated
    NOT_APPLICABLE = "not_applicable"  # ticker doesn't have this kind of data


class MissingReason(BaseModel):
    """Why a section is missing data + how to fix it."""

    stage: str  # e.g. "INGEST(fmp)", "COMPUTE(extract_facts)"
    fix_command: str  # e.g. "python execution/extract_facts.py --ticker GOOG"
    detail: str | None = None


# ---------------------------------------------------------------------------
# §1 Executive snapshot
# ---------------------------------------------------------------------------


class ValuationSnapshot(BaseModel):
    """Numbers shown on the §1 valuation card."""

    consolidated_npv_per_share: float | None = None
    sum_of_segments_npv_per_share: float | None = None
    current_price: float | None = None
    implied_upside_pct: float | None = None  # vs whichever NPV the user prefers
    wacc: float | None = None
    terminal_growth: float | None = None
    valuation_date: date | None = None
    model_link: str | None = None  # relative path to dcf xlsx


class KpiSnapshotRow(BaseModel):
    name: str
    current: str | None = None
    prior: str | None = None
    threshold: str | None = None
    status: Literal["green", "yellow", "red", "unknown"] = "unknown"


class SnapshotSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    ticker: str
    company_name: str | None = None
    thesis_one_liner: str | None = None
    verdict: Literal["intact", "watch", "broken", "pending"] = "pending"
    valuation: ValuationSnapshot
    tier_1_kpi_row: list[KpiSnapshotRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §2 Thesis & tier-1 KPIs
# ---------------------------------------------------------------------------


class KpiLedgerRow(BaseModel):
    name: str
    tier: Literal["tier_1", "tier_2", "tier_3"]
    unit: str | None = None
    source_hint: str | None = None
    break_condition: str | None = None
    history: list[tuple[str, float | None]] = Field(default_factory=list)  # [(period, value)]
    current_status: Literal["green", "yellow", "red", "unknown"] = "unknown"


class BreakRuleObservation(BaseModel):
    period_end: str  # ISO YYYY-MM-DD
    value: float
    unit: str


class BreakRuleEvaluation(BaseModel):
    """One evaluated break rule from `thesis_evaluations.rule_evaluations_json`."""

    rule_id: str
    kpi_name: str
    comparator: str  # lt / le / gt / ge / eq
    threshold: float
    consecutive_periods: int
    status: Literal["ok", "warn", "breach"]
    detail: str
    narrative: str
    observations: list[BreakRuleObservation] = Field(default_factory=list)


class ThesisSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    thesis_full: str | None = None
    last_updated: date | None = None
    break_conditions: list[str] = Field(default_factory=list)
    competitive_watchlist: list[str] = Field(default_factory=list)
    qualitative_breakers: list[str] = Field(default_factory=list)
    kpi_ledger: list[KpiLedgerRow] = Field(default_factory=list)
    overall_breach_status: Literal["ok", "warn", "breach", "unknown"] = "unknown"
    break_rule_evaluations: list[BreakRuleEvaluation] = Field(default_factory=list)
    last_evaluated_at: datetime | None = None


# ---------------------------------------------------------------------------
# §3 / §4 Financials & segments — 12-quarter wide-form
# ---------------------------------------------------------------------------


class GrowthMetrics(BaseModel):
    """Computed against the 16-quarter underlying series (display: 12)."""

    qoq: float | None = None  # Q0/Q-1 - 1
    yoy: float | None = None  # Q0/Q-4 - 1
    cagr_1y_ttm: float | None = None  # TTM(0..-3)/TTM(-4..-7) - 1
    cagr_3y_ttm: float | None = None  # (TTM(0..-3)/TTM(-12..-15))^(1/3) - 1


class QuarterlyLineItem(BaseModel):
    line_item: str  # e.g. "revenue", "operating_income"
    unit: str  # "USD millions" / "%" / etc
    digits: int = 0  # display precision (0 for $M, 2 for EPS / ratios)
    quarters: list[str] = Field(default_factory=list)  # 12 period labels (oldest → newest)
    values: list[float | None] = Field(default_factory=list)  # 12 values aligned to quarters
    growth: GrowthMetrics = Field(default_factory=GrowthMetrics)


class AnnualLineItem(BaseModel):
    line_item: str
    unit: str
    digits: int = 0
    years: list[int] = Field(default_factory=list)  # 10 fiscal years (oldest → newest)
    values: list[float | None] = Field(default_factory=list)


class KpiSeries(BaseModel):
    """A kpi_facts time series aligned to the financials quarter axis.

    Used when chart_priorities references a KPI name that lives in kpi_facts
    rather than in the metrics view (e.g. ARPAC, GMV growth, NIM).
    """

    name: str
    unit: str  # "%", "USD bn", etc.
    quarters: list[str] = Field(default_factory=list)
    values: list[float | None] = Field(default_factory=list)


class FinancialsSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    quarter_labels: list[str] = Field(default_factory=list)  # 12 labels
    line_items: list[QuarterlyLineItem] = Field(default_factory=list)
    annual_years: list[int] = Field(default_factory=list)  # 10 fiscal years
    annual_line_items: list[AnnualLineItem] = Field(default_factory=list)
    chart_priorities: list[str] = Field(default_factory=list)  # display names, dynamic count
    kpi_chart_series: list[KpiSeries] = Field(default_factory=list)


class SegmentSeries(BaseModel):
    segment_name: str
    metric: Literal["revenue_by_product", "revenue_by_geography", "operating_income"]
    quarters: list[str] = Field(default_factory=list)
    values: list[float | None] = Field(default_factory=list)
    growth: GrowthMetrics = Field(default_factory=GrowthMetrics)
    unit: str = "USD millions"


class SegmentsSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    quarter_labels: list[str] = Field(default_factory=list)
    revenue_by_product: list[SegmentSeries] = Field(default_factory=list)
    revenue_by_geography: list[SegmentSeries] = Field(default_factory=list)
    operating_income: list[SegmentSeries] = Field(default_factory=list)
    segment_definitions: dict[str, str] = Field(default_factory=dict)
    segment_definitions_fiscal_year: int | None = None


# ---------------------------------------------------------------------------
# §5 Earnings analysis
# ---------------------------------------------------------------------------


class QuarterlyEarningsCard(BaseModel):
    """One quarter's LLM summary + transcript path. Say-Do lives in its own model."""

    quarter: str  # "Q1"
    year: int
    summary_md: str | None = None  # llm_client.generate_summary output (full)
    digest_md: str | None = None  # ~1-paragraph executive-summary excerpt
    transcript_path: str | None = None
    is_recent: bool = False  # display flag — full content vs digest


class EarningsSection(BaseModel):
    """§5 — LLM summaries only, newest first.

    Most recent N quarters render in full; older ones collapse to a 1-paragraph
    digest. Full transcripts live in the appendix; pairwise Say-Do lives in §6.
    """

    status: SectionStatus
    missing: MissingReason | None = None
    full_quarters: list[QuarterlyEarningsCard] = Field(default_factory=list)
    digest_quarters: list[QuarterlyEarningsCard] = Field(default_factory=list)


class SayDoCard(BaseModel):
    """One pairwise Say-Do analysis: prior-quarter guidance vs current-quarter results.

    `rating` and `thesis_view` are parsed from the saydo_md body (LLM-produced
    "Performance Rating" + "Thesis View" lines). `unknown` if the prompt format
    drifts and we can't pin them.
    """

    current_quarter: str
    current_year: int
    prior_quarter: str
    prior_year: int
    saydo_md: str
    rating: Literal["MET", "MISSED", "EXCEEDED", "MIXED", "unknown"] = "unknown"
    thesis_view: str | None = (
        None  # "Bullish" / "Bearish" / "Neutral" (free text after "Thesis View:")
    )
    attribution: str | None = None  # one-line excerpt after "Attribution:"


class SayDoSection(BaseModel):
    """§6 — pairwise Say-Do analyses across the available quarter sequence,
    newest first."""

    status: SectionStatus
    missing: MissingReason | None = None
    cards: list[SayDoCard] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §7 IR documents
# ---------------------------------------------------------------------------


class IrDocCard(BaseModel):
    quarter: str
    year: int
    doc_type: Literal["press_release", "presentation", "transcript"]
    summary_md: str | None = None
    source_url: str | None = None
    local_path: str | None = None


class IrDocsSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    cards: list[IrDocCard] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §9 Bear case
# ---------------------------------------------------------------------------


class FailureMode(BaseModel):
    hypothesis: str
    evidence_in_data: str
    leading_indicator: str
    quantitative_impact: str
    refutation_criteria: str


class BearCaseSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    failure_modes: list[FailureMode] = Field(default_factory=list)
    most_underweighted: str | None = None
    out_of_scope_flags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §8 Recent developments (WebSearch-driven news brief, 7d cache)
# ---------------------------------------------------------------------------


class RecentDevelopmentsSection(BaseModel):
    """News + recent-developments brief sourced via Claude WebSearch.

    Cached under `.tmp/news_cache/<TICKER>.json` with `cached_at` so that
    successive brief regenerations within the TTL reuse the cached content.
    `content_md` is rendered as-is in the HTML (sources inline as URLs in the
    LLM output); no structural parsing keeps the section schema thin.
    """

    status: SectionStatus
    missing: MissingReason | None = None
    cached_at: datetime | None = None
    news_days_window: int = 7
    content_md: str | None = None


# ---------------------------------------------------------------------------
# §10 Provenance & data quality
# ---------------------------------------------------------------------------


class CoverageRow(BaseModel):
    quarter: str
    year: int
    has_audio_file: bool = False
    has_transcript_file: bool = False
    has_release_file: bool = False
    has_slides_file: bool = False
    step_saydo_analyzed: bool = False
    step_llm_summarized: bool = False


class SourceDocRow(BaseModel):
    doc_type: str
    period_end: str | None = None
    file_path: str
    sha256: str | None = None
    fetched_at: str | None = None


class ProvenanceSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    coverage: list[CoverageRow] = Field(default_factory=list)
    source_docs: list[SourceDocRow] = Field(default_factory=list)
    open_validation_issues: int = 0


# ---------------------------------------------------------------------------
# §11 Appendix — full older quarter content (transcripts + analyses)
# ---------------------------------------------------------------------------


class TranscriptEntry(BaseModel):
    """One quarter's full earnings-call transcript text + provenance."""

    quarter: str
    year: int
    source_path: str  # absolute path on disk (PDF or .txt)
    text: str  # full extracted text


class AppendixSection(BaseModel):
    """§11 — full earnings-call transcripts, collapsible per quarter, newest first.

    Embedded inline (not a separate file) so the deliverable is a single
    self-contained HTML doc.
    """

    status: SectionStatus
    transcripts: list[TranscriptEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


class ReportSpec(BaseModel):
    """The unified report. One per (ticker, generation_date)."""

    ticker: str
    generation_date: date
    repo_root: str  # absolute path the build read from
    run_id: str | None = None  # ingestion_runs.run_id if produced under one

    snapshot: SnapshotSection
    thesis: ThesisSection
    financials: FinancialsSection
    segments: SegmentsSection
    earnings: EarningsSection
    saydo: SayDoSection
    ir_docs: IrDocsSection
    recent_developments: RecentDevelopmentsSection
    bear_case: BearCaseSection
    provenance: ProvenanceSection
    appendix: AppendixSection
