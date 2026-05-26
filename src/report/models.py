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


class ReportFlavor(str, Enum):
    """Which brief shape to render.

    PORTFOLIO renders the full §1 Snapshot (verdict, thesis, KPI strip).
    EVALUATION renders an EvaluationSnapshot at §1 instead — a 3y quick-
    categorization data table for "should I spend more time on this name?"
    screening. The rest of the brief renders identically.
    """

    PORTFOLIO = "portfolio"
    EVALUATION = "evaluation"


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

    # Phase 3 — populated by execution/refresh_dcf.py from dcf_runs audit cols.
    over_under_pct: float | None = None  # (live - fair) / fair; positive = over
    mos_bar: float | None = None  # initiation threshold from holdings JSON
    trigger_status: Literal["sell", "trim", "hold", "initiate_candidate", "unknown"] = "unknown"
    live_price_at: datetime | None = None  # timestamp on dcf_runs.live_price


class KpiSnapshotRow(BaseModel):
    name: str
    current: str | None = None
    prior: str | None = None
    threshold: str | None = None
    status: Literal["green", "yellow", "red", "unknown"] = "unknown"


class DecisionBadge(BaseModel):
    """One row of the decision-history sidebar in §1 Snapshot.

    A renderer-shaped projection of the `decisions` table (migration 0046).
    `date_short` is the made_at month ("YYYY-MM"); `rationale_short` is the
    first ~80 chars of `rationale_excerpt` with an ellipsis when truncated;
    `outcome_label` defaults to "pending" so the renderer always has a class
    to attach.
    """

    date_short: str
    recommendation_kind: str
    outcome_label: Literal["correct", "wrong", "mixed", "unfalsifiable", "pending"] = "pending"
    rationale_short: str = ""


class SnapshotSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    ticker: str
    company_name: str | None = None
    thesis_one_liner: str | None = None
    verdict: Literal["intact", "watch", "broken", "pending"] = "pending"
    valuation: ValuationSnapshot
    tier_1_kpi_row: list[KpiSnapshotRow] = Field(default_factory=list)
    # Last 3 LLM recommendations from the decisions audit ledger (migration
    # 0046). Empty when the table is absent or has no rows for this ticker —
    # the renderers omit the sidebar entirely in that case.
    recent_decisions: list[DecisionBadge] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §1 Evaluation snapshot (eval flavor only — replaces §1 Snapshot for
# new-name screening: 3y quick-categorization data table over ratios + metrics)
# ---------------------------------------------------------------------------


class QuickCategorizationRow(BaseModel):
    """One row of the eval-flavor §1 data table.

    Values are aligned to columns: lfy_minus_2, lfy_minus_1, lfy, ttm.
    `cagr_3y` is the LFY-2 → LFY CAGR when meaningful (absolute series only —
    margin/ratio CAGRs are not useful and stay None).
    """

    metric: str  # e.g. "Revenue", "EPS diluted", "Operating margin"
    unit: str  # "USD M", "USD", "%"
    digits: int = 0  # display precision
    lfy_minus_2: float | None = None
    lfy_minus_1: float | None = None
    lfy: float | None = None
    ttm: float | None = None
    cagr_3y: float | None = None  # decimal (0.12 = +12%)


class EvaluationSnapshotSection(BaseModel):
    """§1 for `flavor=evaluation` — 3y quick-categorization data table.

    Intended for new-name screening before deeper diligence. Pulled from the
    `metrics` and `ratios` views; no LLM in this section.
    """

    status: SectionStatus
    missing: MissingReason | None = None
    ticker: str
    company_name: str | None = None
    sector: str | None = None
    market_cap: float | None = None
    current_price: float | None = None
    rows: list[QuickCategorizationRow] = Field(default_factory=list)
    fiscal_years: list[int] = Field(default_factory=list)  # 3 years [LFY-2, LFY-1, LFY]


# ---------------------------------------------------------------------------
# §2 Company description — LLM-synthesized "what this company does", with
# expandable segment / geo weighting grounded in latest-period segment_facts.
# ---------------------------------------------------------------------------


class SegmentWeighting(BaseModel):
    """One row in the segment / geography weighting table.

    `share_pct` is a fraction of the bucket total in [0,1] for the latest
    period — computed by the section builder from `segment_facts` so the table
    always reflects current realities even if the LLM-generated descriptions
    are a quarter or two stale.
    """

    name: str
    revenue_usd_m: float | None = None  # latest-period revenue in USD millions
    share_pct: float | None = None  # share of the bucket (0..1)
    operating_income_usd_m: float | None = None  # latest-period segment OI in USD millions; None when segment_facts has no OI row for this segment (typical for geography rows + some sub-segments)
    oi_share_pct: float | None = None  # share of TOTAL segment OI (0..1); None when total is zero or this row has no OI
    description: str | None = None  # 1-2 sentence segment description from 10-K


class CompanyDescriptionSection(BaseModel):
    """§2 — "What does this company actually do?"

    Sourced from the FMP 10-K (Nature of Business / Description of Business /
    Information about Segments narrative) + the FMP profile.json description
    + latest-period segment_facts for weighting. Synthesized by an LLM call;
    cached under `data/company_description/{TICKER}.json` keyed by source
    sha256 so re-renders are free unless the 10-K changes.

    Display contract: `elevator_pitch` is always visible (a 1-2 sentence
    summary). The remaining fields render inside an expandable `<details>`
    block so the section stays skimmable for readers who already know the
    company.
    """

    status: SectionStatus
    missing: MissingReason | None = None

    elevator_pitch: str | None = None  # 1-2 sentence always-visible summary
    platform_diagram: str | None = None  # ASCII platform diagram (box-drawing chars)
    platform_caption: str | None = None  # 1-2 sentence caption under the diagram
    business_overview: str | None = None  # multi-paragraph: lines of business
    revenue_model: str | None = None  # how they make money
    segment_breakdown: list[SegmentWeighting] = Field(default_factory=list)
    geographic_breakdown: list[SegmentWeighting] = Field(default_factory=list)

    # Provenance
    source_fiscal_year: int | None = None
    cached_at: datetime | None = None
    sector: str | None = None  # from FMP profile.json
    industry: str | None = None  # from FMP profile.json


# ---------------------------------------------------------------------------
# §3 Thesis & tier-1 KPIs
# ---------------------------------------------------------------------------


class KpiLedgerRow(BaseModel):
    name: str
    tier: Literal["tier_1", "tier_2", "tier_3"]
    unit: str | None = None
    source_hint: str | None = None
    break_condition: str | None = None
    history: list[tuple[str, float | None]] = Field(default_factory=list)  # [(period, value)]
    current_status: Literal["green", "yellow", "red", "unknown"] = "unknown"
    # Verbatim quote / analyst statement that produced the latest history
    # point's value, when known. Populated by the kpi_facts.source_excerpt
    # column (added in migration 0033). Surfaced in the brief as a tooltip
    # on the latest value so the reader can trace it back to source.
    latest_source_excerpt: str | None = None


class BreakRuleObservation(BaseModel):
    period_end: str  # ISO YYYY-MM-DD
    value: float
    unit: str


class BreakRuleEvaluation(BaseModel):
    """One evaluated break rule from `thesis_evaluations.rule_evaluations_json`.

    `tier` partitions rules into catastrophic universal tripwires and per-ticker
    business-model breakers. Pre-tier persisted rows default to 'business_model'
    so they continue to render under the per-ticker table.
    """

    rule_id: str
    kpi_name: str
    comparator: str  # lt / le / gt / ge / eq
    threshold: float
    consecutive_periods: int
    tier: Literal["universal", "business_model"] = "business_model"
    status: Literal["ok", "warn", "breach"]
    detail: str
    narrative: str
    observations: list[BreakRuleObservation] = Field(default_factory=list)


class SoftRuleEvaluation(BaseModel):
    """One evaluated soft (predicate-style) rule.

    Soft rules emit only green / yellow — never red. The §2 renderer surfaces
    fired (yellow) rules inline with hard-rule tables, color-coded by status.
    `details` carries the predicate-specific payload (deceleration bps, ratio
    values, etc.) so the renderer can show numeric evidence beside the prose.
    """

    rule_name: str
    status: Literal["green", "yellow"]
    evidence: str
    details: dict[str, object] = Field(default_factory=dict)


class ThesisSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    thesis_full: str | None = None
    last_updated: date | None = None
    # When the holdings JSON has `_status: stub_regenerated_from_corruption` (or
    # any other non-empty marker), the renderer shows a banner above the thesis
    # so the user knows the content is placeholder, not ground truth.
    stub_warning: str | None = None
    break_conditions: list[str] = Field(default_factory=list)
    competitive_watchlist: list[str] = Field(default_factory=list)
    qualitative_breakers: list[str] = Field(default_factory=list)
    kpi_ledger: list[KpiLedgerRow] = Field(default_factory=list)
    overall_breach_status: Literal["ok", "warn", "breach", "unknown"] = "unknown"
    break_rule_evaluations: list[BreakRuleEvaluation] = Field(default_factory=list)
    soft_rule_evaluations: list[SoftRuleEvaluation] = Field(default_factory=list)
    last_evaluated_at: datetime | None = None


# ---------------------------------------------------------------------------
# §4 / §5 Financials & segments — 12-quarter wide-form
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
    # Underlying levels for the YoY matrix renderer — includes YoY lookback +
    # 3y-CAGR base periods (i.e. up to 24 quarters). Empty when not populated
    # (older built reports / non-quarterly contexts).
    levels_full: list[float | None] = Field(default_factory=list)


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
    # Full-history values aligned to FinancialsSection.quarter_labels_full —
    # used by the paired-chart renderer to compute YoY%. Empty when KPI lacks
    # 4+ quarters of history.
    levels_full: list[float | None] = Field(default_factory=list)


class FinancialsSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    quarter_labels: list[str] = Field(default_factory=list)  # 12 labels
    line_items: list[QuarterlyLineItem] = Field(default_factory=list)
    annual_years: list[int] = Field(default_factory=list)  # 10 fiscal years
    annual_line_items: list[AnnualLineItem] = Field(default_factory=list)
    chart_priorities: list[str] = Field(default_factory=list)  # display names, dynamic count
    kpi_chart_series: list[KpiSeries] = Field(default_factory=list)
    # Full-history quarter labels (parallel to QuarterlyLineItem.levels_full).
    # Used by the YoY matrix renderer; empty when not populated.
    quarter_labels_full: list[str] = Field(default_factory=list)


class SegmentSeries(BaseModel):
    segment_name: str
    metric: Literal["revenue_by_product", "revenue_by_geography", "operating_income"]
    quarters: list[str] = Field(default_factory=list)
    values: list[float | None] = Field(default_factory=list)
    growth: GrowthMetrics = Field(default_factory=GrowthMetrics)
    unit: str = "USD millions"
    # Full-history values aligned to SegmentsSection.quarter_labels_full —
    # used by the YoY matrix renderer. Empty when section was built without
    # full-history support.
    levels_full: list[float | None] = Field(default_factory=list)


class SegmentSecondaryExpansion(BaseModel):
    """Optional secondary-dim breakdown rendered under the primary segments table.

    Populated by the section builder when junction rows (segment_periods +
    segment_dimensions) carry a breakdown on an axis OTHER than the primary
    one — e.g. AWS revenue split by geography under AMZN's product-segment
    table. `dim_type` is the breakdown axis ("geography", "channel", ...);
    `parent_label` is the primary segment the breakdown sits under (or None
    for a standalone "by geography" expansion at the bucket level).
    """

    dim_type: str
    parent_label: str | None = None
    rows: list[SegmentSeries] = Field(default_factory=list)


class SegmentsSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    quarter_labels: list[str] = Field(default_factory=list)
    revenue_by_product: list[SegmentSeries] = Field(default_factory=list)
    revenue_by_geography: list[SegmentSeries] = Field(default_factory=list)
    operating_income: list[SegmentSeries] = Field(default_factory=list)
    segment_definitions: dict[str, str] = Field(default_factory=dict)
    segment_definitions_fiscal_year: int | None = None
    quarter_labels_full: list[str] = Field(default_factory=list)
    # When junction data carries secondary-dim breakdowns (migration 0053+),
    # the section builder pushes them here and the renderer surfaces them as
    # collapsible subtables below the primary segments grid. Empty list when
    # the junction tables are empty or unavailable.
    secondary_expansions: list[SegmentSecondaryExpansion] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §6 Earnings analysis
# ---------------------------------------------------------------------------


class QuarterlyEarningsCard(BaseModel):
    """One quarter's LLM summary + transcript path. Say-Do lives in its own model."""

    quarter: str  # "Q1"
    year: int
    summary_md: str | None = None  # llm_client.generate_summary output (full)
    digest_md: str | None = None  # ~1-paragraph executive-summary excerpt
    transcript_path: str | None = None
    is_recent: bool = False  # display flag — full content vs digest


class SurpriseScorecardCard(BaseModel):
    """Header block for §6 — analyst beat-rate scorecard over the last N
    reported quarters. Populated from `earnings_surprises` (FMP primary,
    yfinance fallback).

    Both sides nullable: when source coverage is empty (e.g. revenue side
    after an FMP plan lapse), the renderer skips that row rather than
    showing a misleading 0% beat rate. Floats here (not Decimal) to match
    the rest of models.py — Decimal precision is preserved in the compute
    layer; the boundary conversion happens in the section builder.
    """

    total_quarters: int
    # EPS side
    eps_beats: int
    eps_misses: int
    eps_no_data: int
    eps_beat_rate_pct: float | None = None
    eps_avg_surprise_pct: float | None = None
    eps_latest_surprise_pct: float | None = None
    # Revenue side — all-None after FMP coverage lapses since yfinance
    # doesn't publish historical revenue actual-vs-estimate.
    revenue_beats: int
    revenue_misses: int
    revenue_no_data: int
    revenue_beat_rate_pct: float | None = None
    revenue_avg_surprise_pct: float | None = None
    revenue_latest_surprise_pct: float | None = None


class QuoteSnippet(BaseModel):
    """One verbatim quote pulled from a transcript supporting a theme.

    `period` is the source quarter label, e.g. "Q1 2026". `speaker` is the
    name as it appears in the transcript when extractable; the LLM passes
    it through when the segment carries an attribution, otherwise empty.
    """

    period: str
    text: str
    speaker: str | None = None


class ThemeRollup(BaseModel):
    """One topical theme rolled up across the last 4 transcripts.

    `mentions_per_quarter` keys are period labels ("Q1 2026", "Q4 2025"…)
    and values are integer mention counts. `last_4q_count` is a convenience
    rollup the renderer uses for the headline chip; it equals
    sum(mentions_per_quarter.values()). `evidence` carries 1-2 verbatim
    quote snippets that anchor the theme to source.
    """

    theme_name: str
    last_4q_count: int
    mentions_per_quarter: dict[str, int] = Field(default_factory=dict)
    evidence: list[QuoteSnippet] = Field(default_factory=list)


class EarningsSection(BaseModel):
    """§6 — LLM summaries only, newest first.

    Most recent N quarters render in full; older ones collapse to a 1-paragraph
    digest. Full transcripts live in the appendix; pairwise Say-Do lives in §7.

    When `surprise_scorecard` is populated, it renders as a header table
    before the per-quarter cards: a 2-row beat-rate summary for EPS and
    Revenue over the lookback window.

    `prepared_remarks_themes` and `qa_themes` are the 4Q-rolling theme
    rollups split by transcript section — what management chose to say vs.
    what analysts pressed on. Populated by the earnings_themes_split LLM
    extractor when `--enable-llm` is passed AND at least one transcript
    in the 4Q window carries a parseable section. Either list is empty
    when the corresponding section was absent across the whole window
    (e.g. Q&A-only aggregator transcripts produce no prepared-remarks
    themes); the renderer shows a fallback note in that case.
    `themes_note` carries any one-line fallback / status message the
    renderer should surface above the theme blocks (e.g. "No Q&A sections
    available in transcripts").
    """

    status: SectionStatus
    missing: MissingReason | None = None
    surprise_scorecard: SurpriseScorecardCard | None = None
    full_quarters: list[QuarterlyEarningsCard] = Field(default_factory=list)
    digest_quarters: list[QuarterlyEarningsCard] = Field(default_factory=list)
    prepared_remarks_themes: list[ThemeRollup] = Field(default_factory=list)
    qa_themes: list[ThemeRollup] = Field(default_factory=list)
    themes_note: str | None = None


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


class SayDoHistoricalMetric(BaseModel):
    """One structured guidance→realized row from `saydo_historical_metrics`.

    Populated alongside `SayDoCard` so the renderer can show KPI-grain
    promise-vs-delivery rows beside the LLM's narrative pairwise cards.
    """

    id: int
    period_made: datetime
    period_target: datetime
    kpi_name: str
    comparator: str
    target_value: float
    realized_value: float | None = None
    outcome: str | None = None
    guidance_narrative: str | None = None
    realized_narrative: str | None = None


class SayDoSection(BaseModel):
    """§7 — pairwise Say-Do analyses across the available quarter sequence,
    newest first."""

    status: SectionStatus
    missing: MissingReason | None = None
    cards: list[SayDoCard] = Field(default_factory=list)
    historical_metrics: list[SayDoHistoricalMetric] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §7.5 Filing Intelligence — buy-side 10-K narrative synthesis
# ---------------------------------------------------------------------------


class SegmentChangeDetail(BaseModel):
    has_changes: bool
    description: str | None = None


class MetricRedefinitionDetail(BaseModel):
    has_changes: bool
    description: str | None = None


class ExecutiveCompAlignmentDetail(BaseModel):
    metrics_used: list[str] = Field(default_factory=list)
    targets_and_thresholds: str | None = None
    alignment_verdict: str | None = None


class InvestmentSignalDetail(BaseModel):
    signal_type: str
    severity: Literal["High", "Medium", "Low"]
    description: str


class FilingIntelligenceSection(BaseModel):
    """§7.5 — buy-side 10-K narrative synthesis cached under
    ``data/filing_intelligence/<T>.json`` by execution/analyze_filing_intelligence.py.

    Carries segment-boundary shifts, metric redefinitions, executive-comp
    alignment, and surfaced tail-risk signals from the Focus Algorithm's
    footnote extraction. Rendered in the Company Description tab.
    """

    status: SectionStatus
    missing: MissingReason | None = None
    ticker: str
    fiscal_year: int | None = None
    analyzed_at: str | None = None
    segment_changes: SegmentChangeDetail | None = None
    metric_redefinitions: MetricRedefinitionDetail | None = None
    executive_comp: ExecutiveCompAlignmentDetail | None = None
    investment_signals: list[InvestmentSignalDetail] = Field(default_factory=list)
    raw_synthesis_md: str | None = None


# ---------------------------------------------------------------------------
# §8 IR documents
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
# §10 Bear case
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
# §9 Recent developments (WebSearch-driven news brief, 7d cache)
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
# §11 Provenance & data quality
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


class ValidationIssueRow(BaseModel):
    """One open validation issue (resolved_at IS NULL). Populated alongside
    ``open_validation_issues`` so renderers can surface the actual list, not
    just the count."""

    severity: str  # "error" | "warning" | "info" (free text from the rule engine)
    rule: str
    raw_value: str | None = None
    expected: str | None = None
    raised_at: str | None = None


class ProvenanceSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    coverage: list[CoverageRow] = Field(default_factory=list)
    source_docs: list[SourceDocRow] = Field(default_factory=list)
    open_validation_issues: int = 0
    open_issues_detail: list[ValidationIssueRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §12 Appendix — full older quarter content (transcripts + analyses)
# ---------------------------------------------------------------------------


class TranscriptEntry(BaseModel):
    """One quarter's full earnings-call transcript text + provenance."""

    quarter: str
    year: int
    source_path: str  # absolute path on disk (PDF or .txt)
    text: str  # full extracted text


class AppendixSection(BaseModel):
    """§12 — full earnings-call transcripts, collapsible per quarter, newest first.

    Embedded inline (not a separate file) so the deliverable is a single
    self-contained HTML doc.
    """

    status: SectionStatus
    transcripts: list[TranscriptEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Hero quote — single thesis-anchoring quote pulled from the latest call
# ---------------------------------------------------------------------------


class HeroQuote(BaseModel):
    """A single quote selected by an LLM as the most thesis-relevant line
    from the most recent earnings call. Used by the workspace renderer's
    Earnings tab as the hero panel."""

    speaker: str  # full name as it appears in the transcript ("Sundar Pichai")
    role: str  # title + context ("CEO, Alphabet · Q1 2026 earnings call")
    body: str  # verbatim quote, with surrounding quote marks stripped
    rationale: str | None = None  # one-line why-this-matters from the LLM
    source_quarter: str | None = None
    source_year: int | None = None


class HeroQuoteSection(BaseModel):
    """Hero-quote container. Used by the workspace renderer."""

    status: SectionStatus
    missing: MissingReason | None = None
    quote: HeroQuote | None = None


# ---------------------------------------------------------------------------
# Q&A roster — structured analyst-Q&A rows for the workspace Earnings tab
# ---------------------------------------------------------------------------


class QAEntry(BaseModel):
    """One Q&A exchange: analyst question, company response, optional follow-up.

    ``analysts`` is the comma-joined "Name (Firm)" header from the transcript.
    ``answers`` collects each speaker's reply paragraph in order. ``follow_up``
    is set when the same analyst speaks again before the next operator hand-off.
    ``topic`` is the first short clause of the question — extracted heuristically
    for the panel's collapsed-row label.
    """

    analysts: str
    topic: str
    tag: str  # short uppercased keyword used as the colored chip in the design
    question: str
    answers: list[tuple[str, str]] = Field(default_factory=list)  # [(speaker, text)]
    follow_up: str | None = None
    transcript_ref: str | None = None  # offset / page when known


class QARosterQuarter(BaseModel):
    """Per-quarter Q&A roster — one bundle of entries from one transcript."""

    quarter: str
    year: int
    entries: list[QAEntry] = Field(default_factory=list)


class QARosterSection(BaseModel):
    """Container for parsed Q&A rosters across the most recent N transcripts.

    The workspace renderer drives display via the same quarter selector as the
    earnings cards: each ``QARosterQuarter`` is matched to its earnings card by
    (quarter, year). Older quarters whose transcripts haven't been parsed are
    simply absent from ``quarters`` — the renderer falls through to a stub.
    """

    status: SectionStatus
    missing: MissingReason | None = None
    quarters: list[QARosterQuarter] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# §14 Synthesis — cross-section LLM lens artifacts
# ---------------------------------------------------------------------------


class SynthesisLensRow(BaseModel):
    """One cached lens artifact: name + markdown content + provenance.

    Lens names match `synthesis_lenses.LENSES` keys. Renderer dispatches on
    name to label the panel; the content is rendered as markdown verbatim.
    """

    name: str
    content_md: str
    model: str | None = None
    generated_at: datetime | None = None
    is_dirty: bool = False
    is_stale: bool = False  # informational only; renderer can warn


class SynthesisSection(BaseModel):
    """§14 — the analytical thinking layer surfaced from lens artifacts.

    Renderers show this as a "Synthesis" tab with one collapsible panel per
    lens. Panels with no cached artifact are simply absent — the user
    regenerates them via `python execution/run_lens.py --ticker X --all`.
    """

    status: SectionStatus
    ticker: str
    lenses: list[SynthesisLensRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


class PortfolioPositionAccountRow(BaseModel):
    """One account's worth of the user's position in this name."""

    account_name: str
    quantity: float
    cost_basis: float | None
    cost_basis_source: str | None = None  # None=broker, 'manual', 'inferred_acats', etc.
    market_value: float | None
    unrealized_pnl: float | None
    unrealized_pct: float | None  # decimal, 0.12 = +12%


class PortfolioPositionTransaction(BaseModel):
    """Most-recent transaction in this ticker for context."""

    date: date
    account_name: str
    type: str
    quantity: float
    amount: float


class PortfolioPositionDecision(BaseModel):
    """A trade decision logged against this ticker — the user's own thesis
    against this name from portfolio-tracker's decision log.

    Same shape for open and closed: open decisions have
    outcome_status in (None, 'open'); closed have it in
    ('validated', 'invalidated', 'partial') with outcome_date and
    optional outcome_notes set.
    """

    decision_date: date
    action: str
    confidence: str | None
    thesis: str  # full text (caller decides whether to truncate)
    linked_brief_path: str | None = None
    outcome_status: str | None = None
    outcome_date: date | None = None
    outcome_notes: str | None = None


class PortfolioPositionSection(BaseModel):
    """Pre-§1 callout: 'your position in this name right now'.

    Reads from the companion portfolio-tracker project. When it's
    unavailable the section is hidden entirely. When the user doesn't
    hold the ticker (no position rows AND no recent decisions), it's
    also hidden — no point in noise.
    """

    status: SectionStatus
    missing: MissingReason | None = None
    held: bool = False
    accounts: list[PortfolioPositionAccountRow] = Field(default_factory=list)
    total_quantity: float = 0.0
    total_cost_basis: float | None = None
    total_market_value: float | None = None
    total_unrealized_pnl: float | None = None
    total_unrealized_pct: float | None = None
    recent_transactions: list[PortfolioPositionTransaction] = Field(default_factory=list)
    open_decisions: list[PortfolioPositionDecision] = Field(default_factory=list)
    closed_decisions: list[PortfolioPositionDecision] = Field(default_factory=list)


class ValuationBasisHistoricalPoint(BaseModel):
    """One historical observation of the chosen multiple. Used to render the
    sparkline + show the historical range so the analyst can eyeball whether
    the current print is rich/cheap vs trailing 8Q."""

    period_end: date
    value: float | None  # None for periods FMP doesn't disclose


class ValuationBasisSection(BaseModel):
    """The valuation tab. Opus picks ONE multiple per ticker (based on sector
    + thesis + financial profile + what's actually computable from the
    available analyst estimates), the compute layer fetches its current value
    and 8Q history, and the renderer shows current + trend + LLM rationale.

    The chosen multiple is cached at ``data/valuation_basis/<T>.json`` so
    repeat renders don't re-call Opus. The cache key incorporates the
    thesis SHA + the latest period_end so refreshing the thesis or a fresh
    quarterly print invalidates the cached choice.
    """

    status: SectionStatus
    missing: MissingReason | None = None
    multiple_name: str | None = None  # e.g. "EV/NTM Revenue", "P/B", "EV/LTM EBITDA"
    rationale: str | None = None  # 1-2 sentence Opus rationale
    current_value: float | None = None  # the multiple's current numeric value
    current_value_display: str | None = None  # formatted display, e.g. "15.1x"
    current_period_end: date | None = None
    history: list[ValuationBasisHistoricalPoint] = Field(default_factory=list)
    historical_min: float | None = None
    historical_max: float | None = None
    historical_median: float | None = None
    rich_cheap_verdict: str | None = None  # e.g. "rich vs 8Q median 12.4x"
    notes: str | None = None  # qualitative target band or caveats from Opus


# ---------------------------------------------------------------------------
# §13 Executive Compensation (Phase 5)
# ---------------------------------------------------------------------------


class ExecCompRowModel(BaseModel):
    """One Named Executive Officer's compensation package."""

    executive_name: str
    role: str | None = None
    is_ceo: bool = False
    fiscal_year: int
    currency: str = "USD"
    base_salary: float | None = None
    cash_bonus_actual: float | None = None
    cash_bonus_target: float | None = None
    equity_grant_value: float | None = None
    total_comp_granted: float | None = None
    total_comp_realized: float | None = None
    # realized / granted - 1; None when either side missing or zero
    realized_vs_granted_pct: float | None = None
    ceo_pay_ratio: float | None = None
    # Free-text "metric (weight%)" join — full breakdown lives in the table
    performance_metrics_summary: str | None = None
    # True when any thesis tier-1 KPI appears in the comp design — the
    # quick alignment-check signal
    metrics_have_thesis_kpi: bool = False


class InsiderSignalRowModel(BaseModel):
    """One scored insider event (post-conviction-scoring)."""

    insider_name: str
    role: str | None = None
    transaction_date: str  # ISO date
    transaction_type: str
    shares: float
    transaction_value: float | None = None
    signal_strength: float  # 0..1
    rationale: str


class ExecCompSectionModel(BaseModel):
    """§13 — executive compensation + insider activity + alignment narrative.

    Built only when at least one of:
      - exec_comp_packages has rows for this ticker
      - insider_transactions has rows for this ticker
    Otherwise the section is MISSING_DATA with a fix command.
    """

    status: SectionStatus
    missing: MissingReason | None = None
    ticker: str
    fiscal_year_latest: int | None = None
    packages: list[ExecCompRowModel] = Field(default_factory=list)
    insider_signals: list[InsiderSignalRowModel] = Field(default_factory=list)
    # LLM-driven alignment commentary. None when --no-llm or generation failed
    # (the section still renders the structured data, just without the memo).
    alignment_narrative_md: str | None = None
    anomaly_flags: list[str] = Field(default_factory=list)


class ReportSpec(BaseModel):
    """The unified report. One per (ticker, generation_date)."""

    ticker: str
    generation_date: date
    repo_root: str  # absolute path the build read from
    run_id: str | None = None  # ingestion_runs.run_id if produced under one
    flavor: ReportFlavor = ReportFlavor.PORTFOLIO
    # True when this build was invoked with --enable-llm. Renderers consult
    # this to decide whether to run optional LLM filters (e.g. SayDo
    # commitment importance ranking) without requiring per-call wiring.
    llm_enabled: bool = False

    portfolio_position: PortfolioPositionSection | None = None
    valuation_basis: ValuationBasisSection | None = None
    snapshot: SnapshotSection
    evaluation_snapshot: EvaluationSnapshotSection | None = None
    company_description: CompanyDescriptionSection
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
    # Workspace-renderer-specific sections — None when not produced; renderers
    # that don't consume them simply ignore the field.
    hero_quote: HeroQuoteSection | None = None
    qa_roster: QARosterSection | None = None
    filing_intelligence: FilingIntelligenceSection | None = None
    exec_compensation: ExecCompSectionModel | None = None
    synthesis: SynthesisSection | None = None
