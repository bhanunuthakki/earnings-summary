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
    BUDGET_SKIPPED = "budget_skipped"  # LLM call forgone to stay under a monthly budget cap


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


class BudgetSkip(BaseModel):
    """Why an LLM analysis was forgone: its per-purpose monthly budget cap was
    reached (``on_exceed='skip'``). Carries the numbers so the renderer can show
    "$X of $Y spent" and the dashboard can offer an override. ``section`` is the
    human label; ``purpose`` is the ``llm_budgets`` key (links the skip to its
    cap). See ``report.sections._common.budget_gate``."""

    section: str  # human label, e.g. "Bear case (§7)"
    purpose: str  # llm_budgets purpose key, e.g. "bear_case"
    cap_usd: float
    spend_usd: float
    headroom_pct: float


# ---------------------------------------------------------------------------
# §1 Executive snapshot
# ---------------------------------------------------------------------------


class PricedInLever(BaseModel):
    """One reverse-DCF lever on the valuation card: the value today's price
    implies vs the analyst's base case for the same lever.

    ``implied_value`` is None when the price is unreachable inside the model's
    bounds (``note`` then carries the direction). ``unit`` is "pct" (a rate — a
    CAGR or terminal g) or "turns" (an EV multiple), so the renderer formats
    without re-deciding. Parsed from
    ``dcf_runs.assumption_snapshot_json["priced_in"]``; produced by
    ``dcf.reverse.PricedIn.to_snapshot_dict``.
    """

    lever: str
    label: str
    unit: Literal["pct", "turns"]
    base_value: float
    implied_value: float | None = None
    note: str = ""

    def _fmt(self, x: float) -> str:
        """A lever value in its own unit: a rate as a percent, a multiple in
        turns. Shared by every renderer so pct/turns formatting can't drift."""
        return f"{x * 100:.1f}%" if self.unit == "pct" else f"{x:.1f}x"

    @property
    def base_display(self) -> str:
        return self._fmt(self.base_value)

    @property
    def implied_display(self) -> str:
        """The market-implied value, or 'n/a' when the price was unreachable
        inside the model's bounds (the honest-None case)."""
        return "n/a" if self.implied_value is None else self._fmt(self.implied_value)

    @property
    def gap_display(self) -> str | None:
        """implied − base in the lever's own unit (percentage *points* for a
        rate, turns for a multiple), signed. None when the lever is unsolved."""
        if self.implied_value is None:
            return None
        delta = self.implied_value - self.base_value
        return f"{delta * 100:+.1f}pts" if self.unit == "pct" else f"{delta:+.1f}x"


class PricedInCard(BaseModel):
    """Reverse-DCF "what's priced in" block on the §1 valuation card: what
    today's price implies about the analyst's own FCFF model, per lever.

    Only redesigned FCFF names carry this (bespoke archetypes have no honest
    single-lever inversion — the card shows an explicit n/a). None on the parent
    ValuationSnapshot when the run predates the block or the archetype can't be
    inverted."""

    price: float
    base_value_per_share_usd: float
    growth: PricedInLever
    terminal: PricedInLever


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
    sheet_url: str | None = None  # live Google Sheet URL when dcf_defaults.gsheet_id is set

    # Phase 3 — populated by execution/refresh_dcf.py from dcf_runs audit cols.
    over_under_pct: float | None = None  # (live - fair) / fair; positive = over
    mos_bar: float | None = None  # initiation threshold from holdings JSON
    trigger_status: Literal["sell", "trim", "hold", "add", "initiate_candidate", "unknown"] = (
        "unknown"
    )
    live_price_at: datetime | None = None  # timestamp on dcf_runs.live_price

    # S6 — Bull/Bear scenario fair values, parsed from dcf_runs.
    # assumption_snapshot_json["scenarios"] (written by refresh_dcf). None when
    # the run predates scenarios or that scenario was un-valuable;
    # consolidated_npv_per_share stays the Base value.
    bull_npv_per_share: float | None = None
    bear_npv_per_share: float | None = None

    # S12 — which valuation archetype produced the number ("FCFF DCF",
    # "SOTP / NAV", "Excess return", "Platform DCF", ...), parsed from the
    # dcf_runs snapshot. None when the run predates model tagging.
    valuation_model_label: str | None = None

    # Reverse-DCF — the market-implied assumption set at today's price
    # ("what's priced in"), parsed from dcf_runs.assumption_snapshot_json
    # ["priced_in"] (written by refresh_dcf). None for bespoke archetypes and
    # runs predating the block; the card then shows an explicit n/a.
    priced_in: PricedInCard | None = None

    # Feature 2 — the per-name DCF scenario prior surfaced on the card: the
    # LLM/owner Bull/Base/Bear weights + rationale (dcf_runs.assumption_snapshot_json
    # ["scenario_prior"]), plus the probability-weighted expected value E[V] and its
    # skew vs the base point estimate (dcf.scenario_reward over the live price +
    # scenario fair values). Until now only the allocation surfaces saw E[V]/skew.
    # None when the run carries no scenario_prior block / no usable reward.
    # scenario_set_by is "llm" / "owner" / "global".
    scenario_weights: dict[str, float] | None = None
    scenario_rationale: str | None = None
    scenario_set_by: str | None = None
    scenario_expected_return: float | None = None  # E[V], a fraction (+0.12 = +12%)
    scenario_skew: float | None = None  # E[V] - base point estimate, a fraction

    # S11 — workbook→assumptions-JSON sync outcome from dcf_runs (migration
    # 0091): 'synced' / 'created' / 'failed: <detail>' + the naive-UTC stamp.
    # None when the run predates the columns, the DB lacks them, or the
    # archetype doesn't run the redesign sync — the card stays quiet then.
    assumptions_sync_status: str | None = None
    assumptions_synced_at: datetime | None = None


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
    # thesis_state.last_updated (persist_verdict writes verdict.evaluated_at
    # there) — the timestamp the "Thesis Intact"/"Watch"/"Broken" badge is
    # dated against. None when no thesis_state row exists (verdict stays
    # "pending" in that case too). Threaded to the chrome badge so it can grey
    # out a verdict that predates the newest reported quarter instead of
    # rendering a stale read as fresh green (honest-grey, never fake-green).
    verdict_as_of: datetime | None = None
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
    operating_income_usd_m: float | None = (
        None  # latest-period segment OI in USD millions; None when segment_facts has no OI row for this segment (typical for geography rows + some sub-segments)
    )
    oi_share_pct: float | None = (
        None  # share of TOTAL segment OI (0..1); None when total is zero or this row has no OI
    )
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
    # Stable PK handle for the metric (kpi_definitions.id), resolved at build
    # time alongside the definition metadata. This is the metric's identity
    # at write-time (Instrument Paradigm Law 2): the renderer emits it as a
    # `fact_ref` doorway (`kpi:{ticker}:{id}`) so a click resolves the exact
    # series by PK instead of re-phrase-matching the fragile display name.
    # None when the name carries no resolvable definition (degrade to the
    # name-keyed anchor).
    kpi_definition_id: int | None = None
    break_condition: str | None = None
    history: list[tuple[str, float | None]] = Field(default_factory=list)  # [(period, value)]
    current_status: Literal["green", "yellow", "red", "unknown"] = "unknown"
    # Short gloss of what the metric measures, so the ledger reads as
    # definitions + data rather than a bare list of names. Populated by
    # `thesis._build_ledger` from the resolved `kpi_definitions` row (the
    # curator `notes`, else the name's parenthetical qualifier). None when the
    # name carries no qualifier and the definition has no notes.
    definition: str | None = None
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
    status: Literal["ok", "warn", "breach", "unresolved"]
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
    overall_breach_status: Literal["ok", "warn", "breach", "unresolved", "unknown"] = "unknown"
    break_rule_evaluations: list[BreakRuleEvaluation] = Field(default_factory=list)
    soft_rule_evaluations: list[SoftRuleEvaluation] = Field(default_factory=list)
    last_evaluated_at: datetime | None = None
    # Pre-rendered markdown block of persisted time-series signals over
    # this ticker's tier-1 KPIs + revenue / OI / FCF. Empty string when
    # the signals table is absent (migration 0053 not run) or the writer
    # hasn't profiled this ticker. Renderers surface it directly so the
    # thesis statement reads alongside the current trend / inflection /
    # anomaly state.
    ts_context_md: str = ""


# ---------------------------------------------------------------------------
# §4 / §5 Financials & segments — 12-quarter wide-form
# ---------------------------------------------------------------------------


class GrowthMetrics(BaseModel):
    """Computed against the 16-quarter underlying series (display: 12)."""

    qoq: float | None = None  # Q0/Q-1 - 1
    yoy: float | None = None  # Q0/Q-4 - 1
    cagr_1y_ttm: float | None = None  # TTM(0..-3)/TTM(-4..-7) - 1
    cagr_3y_ttm: float | None = None  # (TTM(0..-3)/TTM(-12..-15))^(1/3) - 1


class CellSource(BaseModel):
    """Per-number provenance for the source chip (P3.3).

    Mirrors timeseries.loaders.load_financial_cell_provenance's per-cell
    payload: the tier (or source_type fallback) of the document the
    displayed value came from, when it was fetched, and the document
    identity needed to open the source (URL, EDGAR accession, sub-document
    locator JSON from 0075).
    """

    source: str  # source_quality_tier value, or source_type, or "unknown"
    fetched_at: str | None = None
    source_url: str | None = None
    doc_type: str | None = None
    accession_number: str | None = None
    filing_date: str | None = None
    locator: str | None = None  # raw locator JSON off the fact row
    # documents.id of the winning row — lets the chip deep-link the in-app
    # /source/<doc_id> viewers (P4.3) instead of only the raw source_url.
    doc_id: int | None = None
    # Scored fact confidence in [0, 1] (pipeline.confidence's documented
    # formula: tier base + extraction-method delta - validation penalties).
    # None on legacy DBs / pre-scoring rows — the chip then shows no %.
    confidence: float | None = None
    # Audit trail of HOW the value left its document (the fact row's
    # extracted_by tag: 'fmp', 'sec_xbrl', 'llm:<model>', 'manual_*', …).
    # Rendered as a popover row (S2 PR3); None on legacy rows.
    extracted_by: str | None = None
    # Raw kpi_facts.computed_from lineage JSON (alembic 0087) for derived
    # rows — {"display": "...", "inputs": [{ref,item,period_end,doc_id,tier}]}.
    # The chip popover renders "derived from: <display>" plus one mini-chip
    # per input. None for directly-extracted facts.
    computed_from: str | None = None
    # Pre-formatted unresolved validation_issues strings targeting this fact
    # ("⚠ SEC says $101M, 0.99% delta", manual-override reasons, …) — built
    # by pipeline.confidence.display_issues_for_fact; rendered as warn rows.
    issues: list[str] = []
    # Compact "overridden by" label when a company-doc fact_overrides record
    # supersedes FMP for this cell (provenance-override P6) — e.g.
    # "sec_8k · 0001652044-26-000012 · ex991.htm". The other source fields are
    # swapped to the override's so the chip honestly describes the filing the
    # displayed number came from. None for non-overridden cells.
    override: str | None = None


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
    # Per-period source provenance aligned to levels_full (P3.3 source
    # chips). Empty on older built reports; None entries where the period
    # has no matching provenance row. Plain [] default (pydantic copies it
    # per instance) — Field(default_factory=list) infers list[Unknown] here
    # and trips the pyright strict ratchet.
    sources_full: list[CellSource | None] = []


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
    # Per-period provenance aligned to levels_full (KPI chip universality,
    # S2 PR2) — the kpi_facts twin of QuarterlyLineItem.sources_full. Empty
    # on older built reports / legacy DBs without a documents table; None
    # entries where the period has no provenance row. Plain [] defaults
    # (not Field(default_factory=list)) per the pyright-strict note above.
    sources_full: list[CellSource | None] = []
    # Scalar mirror of sources_full[i].confidence for renderers that only
    # need the per-cell % (heatmap hover/affordance) without the full chip.
    confidence_full: list[float | None] = []


class AnnualKpiSeries(BaseModel):
    """A kpi_facts time series for an ANNUAL-cadence KPI, aligned to a fiscal-year
    axis (not the quarterly axis).

    Used for metrics an issuer discloses only annually (bank Basel III capital
    ratios, other 20-F/10-K-only figures). The renderer shows these as a clean
    annual series with year-over-year shading rather than the gappy quarterly
    heatmap that quarterly-axis alignment would produce. ``years`` are fiscal
    years (oldest → newest); ``values`` align to them.
    """

    name: str
    unit: str  # "%", "ratio", etc.
    # Plain typed defaults (not Field(default_factory=list)): pyright strict infers
    # the empty-list default against the declared element type, so these stay
    # strict-clean where the bare-`list` factory reads as list[Unknown].
    years: list[int] = []
    values: list[float | None] = []
    # Per-year provenance + scored-confidence mirror, aligned to ``years``
    # (KPI chip universality, S2 PR2 — see KpiSeries.sources_full).
    sources_full: list[CellSource | None] = []
    confidence_full: list[float | None] = []


class FinancialsSection(BaseModel):
    status: SectionStatus
    missing: MissingReason | None = None
    quarter_labels: list[str] = Field(default_factory=list)  # 12 labels
    line_items: list[QuarterlyLineItem] = Field(default_factory=list)
    annual_years: list[int] = Field(default_factory=list)  # 10 fiscal years
    annual_line_items: list[AnnualLineItem] = Field(default_factory=list)
    chart_priorities: list[str] = Field(default_factory=list)  # display names, dynamic count
    kpi_chart_series: list[KpiSeries] = Field(default_factory=list)
    # ANNUAL-cadence KPIs (kpi_definitions.reporting_cadence='annual') resolved
    # from chart_priorities — rendered on the fiscal-year axis below, not the
    # quarterly heatmap. `annual_kpi_years` is the shared year axis (oldest →
    # newest); each series' values align to it. Empty when the ticker tracks no
    # annual KPIs.
    annual_kpi_chart_series: list[AnnualKpiSeries] = []
    annual_kpi_years: list[int] = []
    # Full-history quarter labels (parallel to QuarterlyLineItem.levels_full).
    # Used by the YoY matrix renderer; empty when not populated.
    quarter_labels_full: list[str] = Field(default_factory=list)
    # Reporting currency from the metrics view (e.g. "USD", "EUR", "BRL"). Drives
    # the "<ccy> millions" unit label instead of a hardcoded "USD millions".
    currency: str = "USD"


class SegmentSeries(BaseModel):
    segment_name: str
    metric: Literal[
        "revenue_by_product",
        "revenue_by_geography",
        "operating_income",
        "capex_by_segment",
        "headcount_by_segment",
    ]
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
    # Capex + headcount by segment land here when the 10-K segment-note
    # extractor emits the corresponding `capex` / `headcount` metric rows.
    # Empty list when the filing's segment notes don't break them out (most
    # service businesses) — surfaces a dedicated bucket only when populated.
    capex_by_segment: list[SegmentSeries] = Field(default_factory=list)
    headcount_by_segment: list[SegmentSeries] = Field(default_factory=list)
    segment_definitions: dict[str, str] = Field(default_factory=dict)
    segment_definitions_fiscal_year: int | None = None
    quarter_labels_full: list[str] = Field(default_factory=list)
    # When junction data carries secondary-dim breakdowns (migration 0053+),
    # the section builder pushes them here and the renderer surfaces them as
    # collapsible subtables below the primary segments grid. Empty list when
    # the junction tables are empty or unavailable.
    secondary_expansions: list[SegmentSecondaryExpansion] = Field(default_factory=list)
    # Pre-rendered markdown block of persisted segment-level signals
    # (metric_kind='segment' rows in timeseries_signals). Empty string
    # when the signals table is absent or the writer hasn't produced
    # segment signals for this ticker.
    ts_context_md: str = ""


# ---------------------------------------------------------------------------
# §3.5 Time-series signals — renderer-shaped projection of timeseries_signals
# ---------------------------------------------------------------------------


class SignalRow(BaseModel):
    """One persisted timeseries_signals row, renderer-shaped.

    `severity_magnitude` is the within-tier sort key: |zscore| for anomaly,
    |slope_pct_of_mean| for trend, |magnitude| for inflection, |delta| for
    yoy_acceleration, seasonal_strength for seasonal. None when the payload
    didn't carry a usable magnitude scalar — the accessor pushes it to the
    end of its tier.
    """

    metric_name: str
    metric_kind: Literal["financial", "kpi", "segment"]
    signal_type: Literal[
        "trend", "inflection", "anomaly", "yoy_acceleration", "seasonal", "correlation"
    ]
    severity: Literal["green", "yellow", "red"]
    narrative: str | None = None
    value_summary: str | None = None  # short numeric hint, e.g. "z=2.8", "slope=-12%/yr"
    severity_magnitude: float | None = None


class SignalsSection(BaseModel):
    """§3.5 — current time-series signals for one ticker.

    Three lists, pre-bucketed by severity so the renderer can show the
    red/yellow "Fires" block above the green "All signals" expander. Within
    each tier rows are ordered by `severity_magnitude` descending so the
    most extreme signal shows first.

    `red_signals + yellow_signals + green_signals` is the full current row
    set — one row per (metric, signal_type). Empty lists when the
    `timeseries_signals` table is empty or absent.
    """

    status: SectionStatus
    missing: MissingReason | None = None
    red_signals: list[SignalRow] = Field(default_factory=list)
    yellow_signals: list[SignalRow] = Field(default_factory=list)
    green_signals: list[SignalRow] = Field(default_factory=list)


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
    budget_skip: BudgetSkip | None = None  # set when the themes LLM was forgone (budget)
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
    budget_skip: BudgetSkip | None = None
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
    budget_skip: BudgetSkip | None = None
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
    # documents.id — lets the Sources tab deep-link each row into the in-app
    # /source/<doc_id> viewer (S10 PR2). None on legacy built reports.
    doc_id: int | None = None


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
    budget_skip: BudgetSkip | None = None  # set when the topic-labeling LLM was forgone (budget)
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
    # L12: when the lens authored its prose with inline ``[n]`` evidence
    # markers (numbered from its ordered ``source_doc_ids``), this carries the
    # ``ui.cite_marks`` chip payloads the renderer linkifies them against — so
    # the paragraphs interpreting the facts are as traceable as the fact cells.
    # None / empty for lenses that don't cite (the prose renders unchanged).
    citations: tuple[dict[str, object], ...] = ()


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
    snapshot_date: date | None = None  # tracker snapshot this row was valued at


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
    # Max tracker snapshot_date across the held accounts — the "as of" date for
    # this position. None when no dated snapshot rows were returned. The position
    # is a build-time snapshot, so this exposes how stale the figures are.
    position_as_of: date | None = None
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
    budget_skip: BudgetSkip | None = None
    multiple_name: str | None = None  # e.g. "EV/NTM Revenue", "P/B", "EV/LTM EBITDA"
    rationale: str | None = None  # 1-2 sentence Opus rationale
    current_value: float | None = None  # the multiple's current numeric value
    current_value_display: str | None = None  # formatted display, e.g. "15.1x"
    # PEG = P/E(NTM) ÷ forward EPS growth%. Populated only when the chosen
    # multiple is the earnings multiple P/E (NTM) AND forward EPS growth is
    # positive (see compute.valuation_basis._compute_peg) — None for book-value /
    # EV / FCF multiples and unprofitable / negative-growth names. peg_growth_pct
    # is the forward EPS growth rate the renderer shows beside the ratio.
    peg_ratio: float | None = None
    peg_growth_pct: float | None = None
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
    budget_skip: BudgetSkip | None = (
        None  # set when the alignment-narrative LLM was forgone (budget)
    )
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
    # Sections whose LLM analysis was forgone to stay under a monthly budget cap
    # (on_exceed='skip'). Drives the brief's "forgone due to budget" header
    # rollup + the dashboard indicator. Empty when nothing was budget-skipped.
    forgone_due_to_budget: list[BudgetSkip] = Field(default_factory=list)
    # Workspace section keys to OMIT for this ticker's business model (e.g. a
    # bank hides the operating-lease ladder + customer-concentration panels).
    # Computed once in the builder via
    # industry_classifier.suppressed_sections_for_ticker(); empty = show
    # everything (the default for unclassified tickers). The workspace renderer
    # gates the Company-tab P3 panels on this set.
    suppressed_sections: list[str] = Field(default_factory=list)

    portfolio_position: PortfolioPositionSection | None = None
    valuation_basis: ValuationBasisSection | None = None
    snapshot: SnapshotSection
    evaluation_snapshot: EvaluationSnapshotSection | None = None
    company_description: CompanyDescriptionSection
    thesis: ThesisSection
    financials: FinancialsSection
    signals: SignalsSection | None = None
    segments: SegmentsSection
    earnings: EarningsSection
    saydo: SayDoSection
    ir_docs: IrDocsSection
    recent_developments: RecentDevelopmentsSection
    bear_case: BearCaseSection
    provenance: ProvenanceSection
    appendix: AppendixSection
    # Workspace-renderer-specific sections — None when not produced; renderers
    # that don't consume them simply ignore the field. (The hero_quote slot
    # was retired in P6.1 — defined since the design bundle, never produced
    # or rendered.)
    qa_roster: QARosterSection | None = None
    filing_intelligence: FilingIntelligenceSection | None = None
    exec_compensation: ExecCompSectionModel | None = None
    synthesis: SynthesisSection | None = None
