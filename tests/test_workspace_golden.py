"""Golden-render regression harness for the workspace renderer (S13).

``workspace_html.py`` is being split into per-section modules and its
card/chip/table markup deduplicated onto the ``src/ui`` component family.
The contract for both refactors is NO output change. These tests pin the
rendered document — shell chrome, every section pane, the CSS bundle and
the JS bundle — as per-part golden files under ``tests/golden/workspace/``
so any unintended drift fails with a named part and a readable diff.

Mechanics
---------
- Two deterministic fixture specs: a rich PORTFOLIO held spec exercising
  every tab with data, and an EVALUATION-flavor spec (research-first group
  order, eval screen panels inside the Company tab, standalone Decisions
  group). P3 panel rows are injected by patching
  ``workspace_html.load_workspace_p3_panels`` — ``render()`` resolves that
  name from its own module globals, and a seam-guard test below fails if
  the injected content stops reaching the document.
- The document is decomposed into named parts: ``styles`` (the <style>
  blocks), ``styles_sorted`` (same rules, whitespace-normalized and sorted —
  survives reordering/moving rules between modules while still catching
  dropped or edited rules), ``scripts`` (plain <script> blocks; the JSON
  boot payloads stay in the shell), one part per section pane
  (``pane_<data-tab>``), and ``shell`` (everything else: head, identity
  strip, KPI strip, tab scaffold, footer, sidebar shells).
- Normalization: the tmp repo_root path is replaced with ``[REPO]``,
  wall-clock-relative strings ("3d ago", "just now", …) become
  ``[RELTIME]``, and a newline is inserted between adjacent tags so diffs
  are line-oriented. Fixture prose must not itself contain reltime-shaped
  strings.
- Regenerate: ``GOLDEN_REGEN=1 python -m pytest tests/test_workspace_golden.py``
  then review the golden diff in git. An intentional rendering change MUST
  land with its regenerated goldens and a PR callout of the before/after.
"""

from __future__ import annotations

import difflib
import os
import re
from datetime import UTC, date, datetime
from itertools import islice
from pathlib import Path
from typing import Literal
from unittest import mock

import pytest

from report.models import (
    AppendixSection,
    BearCaseSection,
    BreakRuleEvaluation,
    BreakRuleObservation,
    BudgetSkip,
    CellSource,
    CompanyDescriptionSection,
    CoverageRow,
    DecisionBadge,
    EarningsSection,
    EvaluationSnapshotSection,
    ExecCompRowModel,
    ExecCompSectionModel,
    ExecutiveCompAlignmentDetail,
    FailureMode,
    FilingIntelligenceSection,
    FinancialsSection,
    GrowthMetrics,
    InsiderSignalRowModel,
    InvestmentSignalDetail,
    IrDocsSection,
    KpiLedgerRow,
    KpiSeries,
    KpiSnapshotRow,
    MetricRedefinitionDetail,
    PortfolioPositionAccountRow,
    PortfolioPositionDecision,
    PortfolioPositionSection,
    PortfolioPositionTransaction,
    ProvenanceSection,
    QAEntry,
    QARosterQuarter,
    QARosterSection,
    QuarterlyEarningsCard,
    QuarterlyLineItem,
    QuickCategorizationRow,
    QuoteSnippet,
    RecentDevelopmentsSection,
    ReportFlavor,
    ReportSpec,
    SayDoCard,
    SayDoHistoricalMetric,
    SayDoSection,
    SectionStatus,
    SegmentChangeDetail,
    SegmentSecondaryExpansion,
    SegmentSeries,
    SegmentsSection,
    SegmentWeighting,
    SignalRow,
    SignalsSection,
    SnapshotSection,
    SoftRuleEvaluation,
    SourceDocRow,
    SurpriseScorecardCard,
    SynthesisLensRow,
    SynthesisSection,
    ThemeRollup,
    ThesisSection,
    TranscriptEntry,
    ValidationIssueRow,
    ValuationBasisHistoricalPoint,
    ValuationBasisSection,
    ValuationSnapshot,
)
from report.renderers import workspace_html
from report.renderers.workspace_data import WorkspaceP3Panels
from report.sections.p3_data import (
    CustomerConcentrationRow,
    DecisionHistorySummary,
    DecisionRow,
    LeaseLadderRow,
    MacroSensitivityRow,
    PeerCompRow,
    SayDoVerdictRow,
    StrategicTargetRow,
)
from user_state.notes import AnalystNoteRow

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "workspace"
REGEN = os.environ.get("GOLDEN_REGEN") == "1"

# A fixed "now-ish" stamp for every datetime in the fixtures (naive UTC per
# repo convention). The absolute value doesn't matter — anything rendered
# relative to the wall clock is normalized to [RELTIME].
_TS = datetime(2026, 6, 1, 12, 0, 0)
# Synthesis lens stamps are timezone-AWARE in production (the renderer
# subtracts them from datetime.now(UTC), which would crash on naive input).
_TS_AWARE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_GEN_DATE = date(2026, 6, 11)


# ---------------------------------------------------------------------------
# Fixture specs
# ---------------------------------------------------------------------------


def _quarter_labels(n: int, *, newest_year: int = 2026, newest_q: int = 1) -> list[str]:
    """n labels oldest → newest ending at Q<newest_q> <newest_year>."""
    out: list[str] = []
    y, q = newest_year, newest_q
    for _ in range(n):
        out.append(f"Q{q} {y}")
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return list(reversed(out))


def _cell_sources(values: list[float | None]) -> list[CellSource | None]:
    """One CellSource per non-None value, cycling through the interesting
    provenance shapes (plain tier, scored + extracted_by, derived w/ lineage,
    disagreement issue)."""
    out: list[CellSource | None] = []
    for i, v in enumerate(values):
        if v is None:
            out.append(None)
            continue
        variant = i % 4
        if variant == 0:
            out.append(
                CellSource(
                    source="sec_filing",
                    fetched_at="2026-05-02T08:00:00",
                    source_url="https://www.sec.gov/Archives/edgar/data/0000000000/test.htm",
                    doc_type="10-Q",
                    accession_number="0000000000-26-000001",
                    filing_date="2026-05-01",
                    doc_id=101,
                    confidence=0.97,
                    extracted_by="sec_xbrl",
                )
            )
        elif variant == 1:
            out.append(
                CellSource(
                    source="aggregator",
                    fetched_at="2026-05-03T08:00:00",
                    doc_type="fmp_income_statement",
                    doc_id=102,
                    confidence=0.84,
                    extracted_by="fmp",
                )
            )
        elif variant == 2:
            out.append(
                CellSource(
                    source="derived",
                    doc_id=103,
                    confidence=0.78,
                    extracted_by="compute",
                    computed_from=(
                        '{"display": "operating_income / revenue", "inputs": '
                        '[{"ref": "operating_income", "item": "operating_income", '
                        '"period_end": "2026-03-31", "doc_id": 101, "tier": "sec_filing"}, '
                        '{"ref": "revenue", "item": "revenue", '
                        '"period_end": "2026-03-31", "doc_id": 102, "tier": "aggregator"}]}'
                    ),
                )
            )
        else:
            out.append(
                CellSource(
                    source="ir_document",
                    fetched_at="2026-05-04T08:00:00",
                    doc_type="press_release",
                    doc_id=104,
                    confidence=0.66,
                    extracted_by="llm:claude-sonnet-4-6",
                    issues=["SEC says $1,201M vs FMP $1,198M (0.25% delta)"],
                )
            )
    return out


def _line_item(
    name: str,
    unit: str,
    digits: int,
    series: list[float | None],
    labels_full: list[str],
) -> QuarterlyLineItem:
    assert len(series) == len(labels_full)
    return QuarterlyLineItem(
        line_item=name,
        unit=unit,
        digits=digits,
        quarters=labels_full[-12:],
        values=series[-12:],
        growth=GrowthMetrics(qoq=0.031, yoy=0.182, cagr_1y_ttm=0.176, cagr_3y_ttm=0.221),
        levels_full=series,
        sources_full=_cell_sources(series),
    )


def _financials() -> FinancialsSection:
    labels_full = _quarter_labels(16)
    revenue: list[float | None] = [
        800.0,
        850.0,
        905.0,
        980.0,
        1010.0,
        1065.0,
        1130.0,
        1220.0,
        1255.0,
        1320.0,
        1400.0,
        1500.0,
        1530.0,
        None,
        1710.0,
        1810.0,
    ]
    op_margin: list[float | None] = [
        18.1,
        18.4,
        18.9,
        19.5,
        19.8,
        20.2,
        20.4,
        21.0,
        21.3,
        21.9,
        22.4,
        23.0,
        23.2,
        23.6,
        24.1,
        24.8,
    ]
    eps: list[float | None] = [
        0.41,
        0.44,
        0.48,
        0.53,
        0.55,
        0.59,
        0.64,
        0.71,
        0.74,
        0.79,
        0.86,
        0.95,
        0.97,
        1.04,
        1.13,
        1.24,
    ]
    kpi_levels: list[float | None] = [
        61.0,
        63.0,
        66.0,
        70.0,
        73.0,
        77.0,
        82.0,
        88.0,
        92.0,
        97.0,
        103.0,
        110.0,
        114.0,
        119.0,
        126.0,
        134.0,
    ]
    return FinancialsSection(
        status=SectionStatus.OK,
        quarter_labels=labels_full[-12:],
        line_items=[
            _line_item("revenue", "USD millions", 0, revenue, labels_full),
            _line_item("operating_margin", "%", 1, op_margin, labels_full),
            _line_item("eps_diluted", "USD", 2, eps, labels_full),
        ],
        annual_years=[2022, 2023, 2024, 2025],
        annual_line_items=[],
        chart_priorities=["revenue", "Active customers"],
        kpi_chart_series=[
            KpiSeries(
                name="Active customers",
                unit="M",
                quarters=labels_full[-12:],
                values=kpi_levels[-12:],
                levels_full=kpi_levels,
                sources_full=_cell_sources(kpi_levels),
                confidence_full=[0.9 if v is not None else None for v in kpi_levels],
            )
        ],
        annual_kpi_chart_series=[],
        annual_kpi_years=[],
        quarter_labels_full=labels_full,
        currency="USD",
    )


def _segments() -> SegmentsSection:
    labels_full = _quarter_labels(16)

    def seg(
        name: str,
        metric: Literal[
            "revenue_by_product",
            "revenue_by_geography",
            "operating_income",
            "capex_by_segment",
            "headcount_by_segment",
        ],
        base: float,
        step: float,
    ) -> SegmentSeries:
        levels: list[float | None] = [base + step * i for i in range(16)]
        return SegmentSeries(
            segment_name=name,
            metric=metric,
            quarters=labels_full[-12:],
            values=levels[-12:],
            growth=GrowthMetrics(qoq=0.02, yoy=0.15),
            unit="USD millions",
            levels_full=levels,
        )

    return SegmentsSection(
        status=SectionStatus.OK,
        quarter_labels=labels_full[-12:],
        revenue_by_product=[
            seg("Subscriptions", "revenue_by_product", 520.0, 24.0),
            seg("Services", "revenue_by_product", 280.0, 11.0),
        ],
        revenue_by_geography=[seg("Americas", "revenue_by_geography", 600.0, 26.0)],
        operating_income=[seg("Subscriptions", "operating_income", 140.0, 9.0)],
        capex_by_segment=[seg("Subscriptions", "capex_by_segment", 40.0, 2.0)],
        headcount_by_segment=[],
        segment_definitions={
            "Subscriptions": "Recurring platform subscriptions, billed monthly or annually.",
            "Services": "Implementation, migration and premium support engagements.",
        },
        segment_definitions_fiscal_year=2025,
        quarter_labels_full=labels_full,
        secondary_expansions=[
            SegmentSecondaryExpansion(
                dim_type="geography",
                parent_label="Subscriptions",
                rows=[seg("EMEA", "revenue_by_geography", 180.0, 8.0)],
            )
        ],
        ts_context_md="- Subscriptions revenue: steady **uptrend**, no inflection flagged.",
    )


def _signals() -> SignalsSection:
    return SignalsSection(
        status=SectionStatus.OK,
        red_signals=[
            SignalRow(
                metric_name="services_revenue",
                metric_kind="segment",
                signal_type="anomaly",
                severity="red",
                narrative="Services revenue printed 2.8 sigma below its 8Q trend.",
                value_summary="z=-2.8",
                severity_magnitude=2.8,
            )
        ],
        yellow_signals=[
            SignalRow(
                metric_name="operating_margin",
                metric_kind="financial",
                signal_type="yoy_acceleration",
                severity="yellow",
                narrative="Margin expansion pace halved vs the prior four quarters.",
                value_summary="delta=-180bps",
                severity_magnitude=1.8,
            )
        ],
        green_signals=[
            SignalRow(
                metric_name="Active customers",
                metric_kind="kpi",
                signal_type="trend",
                severity="green",
                narrative="Customer growth trend intact.",
                value_summary="slope=+11%/yr",
                severity_magnitude=0.4,
            )
        ],
    )


def _thesis() -> ThesisSection:
    return ThesisSection(
        status=SectionStatus.OK,
        thesis_full=(
            "**Platform compounding:** TEST converts category share into pricing "
            "power.\n\n- Net revenue retention stays above 115%\n- Subscription "
            "gross margin holds the high-70s\n- Services stays a sidecar, not "
            "the growth engine"
        ),
        last_updated=date(2026, 5, 20),
        break_conditions=[
            "NRR below 110% for two consecutive quarters",
            "Subscription gross margin below 72%",
        ],
        competitive_watchlist=["RIVL", "MEGA"],
        qualitative_breakers=["Founder-CEO departure", "Forced platform unbundling"],
        kpi_ledger=[
            KpiLedgerRow(
                name="Net revenue retention",
                tier="tier_1",
                unit="%",
                break_condition="< 110% for 2Q",
                history=[
                    ("2024-12-31", 121.0),
                    ("2025-03-31", 120.0),
                    ("2025-06-30", 119.0),
                    ("2025-09-30", 118.0),
                    ("2025-12-31", 117.5),
                    ("2026-03-31", 117.0),
                ],
                current_status="green",
                definition="Trailing-12M expansion net of churn, subscription cohorts only.",
                latest_source_excerpt="NRR remained above 117% in the quarter.",
            ),
            KpiLedgerRow(
                name="Subscription gross margin",
                tier="tier_1",
                unit="%",
                break_condition="< 72%",
                history=[
                    ("2025-03-31", 77.8),
                    ("2025-06-30", 77.2),
                    ("2025-09-30", 76.6),
                    ("2025-12-31", 76.1),
                    ("2026-03-31", 75.4),
                ],
                current_status="yellow",
                definition="Subscription revenue less hosting + support delivery cost.",
            ),
            KpiLedgerRow(
                name="Services attach rate",
                tier="tier_2",
                unit="%",
                history=[("2025-03-31", 34.0), ("2025-06-30", 31.0)],
                current_status="red",
            ),
        ],
        overall_breach_status="warn",
        break_rule_evaluations=[
            BreakRuleEvaluation(
                rule_id="u_fcf_negative_4q",
                kpi_name="fcf",
                comparator="lt",
                threshold=0.0,
                consecutive_periods=4,
                tier="universal",
                status="ok",
                detail="FCF positive in all of the last 4 quarters.",
                narrative="Catastrophic tripwire only — not thesis-calibrated.",
                observations=[
                    BreakRuleObservation(period_end="2026-03-31", value=312.0, unit="USD millions")
                ],
            ),
            BreakRuleEvaluation(
                rule_id="bm_nrr_floor",
                kpi_name="Net revenue retention",
                comparator="lt",
                threshold=110.0,
                consecutive_periods=2,
                tier="business_model",
                status="warn",
                detail="Latest 117.0 vs floor 110.0 — slope negative 6 quarters running.",
                narrative="The compounding engine: expansion within installed base.",
                observations=[
                    BreakRuleObservation(period_end="2025-12-31", value=117.5, unit="%"),
                    BreakRuleObservation(period_end="2026-03-31", value=117.0, unit="%"),
                ],
            ),
        ],
        soft_rule_evaluations=[
            SoftRuleEvaluation(
                rule_name="revenue_decel_watch",
                status="yellow",
                evidence="YoY growth decelerated 240bps over two quarters.",
                details={"decel_bps": 240, "window_quarters": 2},
            ),
            SoftRuleEvaluation(
                rule_name="sbc_ratio_watch",
                status="green",
                evidence="SBC/revenue flat at 11% — within band.",
            ),
        ],
        last_evaluated_at=_TS,
        ts_context_md="- revenue: **uptrend** (slope +18%/yr), no anomaly in latest quarter.",
    )


def _earnings() -> EarningsSection:
    return EarningsSection(
        status=SectionStatus.OK,
        surprise_scorecard=SurpriseScorecardCard(
            total_quarters=8,
            eps_beats=7,
            eps_misses=1,
            eps_no_data=0,
            eps_beat_rate_pct=87.5,
            eps_avg_surprise_pct=4.2,
            eps_latest_surprise_pct=2.1,
            revenue_beats=6,
            revenue_misses=1,
            revenue_no_data=1,
            revenue_beat_rate_pct=85.7,
            revenue_avg_surprise_pct=1.3,
            revenue_latest_surprise_pct=0.8,
        ),
        full_quarters=[
            QuarterlyEarningsCard(
                quarter="Q1",
                year=2026,
                summary_md=(
                    "## Q1 2026 readout\n\n"
                    "Revenue grew **18% YoY** to $1.81B with operating margin at "
                    "24.8%.\n\n- Subscriptions led, up 21%\n- Services declined as "
                    "planned\n- Management raised FY26 revenue guidance by 1%"
                ),
                transcript_path="data/transcripts/TEST_Q1_2026.txt",
                is_recent=True,
            )
        ],
        digest_quarters=[
            QuarterlyEarningsCard(
                quarter="Q4",
                year=2025,
                digest_md=(
                    "Clean beat-and-raise: revenue +19% YoY, margin +160bps; "
                    "guidance assumed no macro improvement."
                ),
            )
        ],
        prepared_remarks_themes=[
            ThemeRollup(
                theme_name="AI workloads",
                last_4q_count=14,
                mentions_per_quarter={"Q1 2026": 6, "Q4 2025": 4, "Q3 2025": 3, "Q2 2025": 1},
                evidence=[
                    QuoteSnippet(
                        period="Q1 2026",
                        text="AI workloads now touch a third of subscription seats.",
                        speaker="Pat Founder",
                    )
                ],
            )
        ],
        qa_themes=[
            ThemeRollup(
                theme_name="Pricing power",
                last_4q_count=9,
                mentions_per_quarter={"Q1 2026": 3, "Q4 2025": 2, "Q3 2025": 2, "Q2 2025": 2},
                evidence=[
                    QuoteSnippet(
                        period="Q4 2025",
                        text="We have not seen elasticity pushback at the new tiers.",
                        speaker="Chris Finance",
                    )
                ],
            )
        ],
        themes_note=None,
    )


def _qa_roster() -> QARosterSection:
    return QARosterSection(
        status=SectionStatus.OK,
        quarters=[
            QARosterQuarter(
                quarter="Q1",
                year=2026,
                entries=[
                    QAEntry(
                        analysts="Jordan Sell (Big Bank)",
                        topic="Subscription pricing uplift durability",
                        tag="PRICING",
                        question=(
                            "How much of the NRR print came from the January list-price "
                            "change versus seat expansion?"
                        ),
                        answers=[
                            (
                                "Chris Finance",
                                "Roughly a third was price; the rest was seats and tier mix.",
                            ),
                            ("Pat Founder", "Price was the smallest of the three drivers."),
                        ],
                        follow_up="Would you take price again in FY27 if churn stays flat?",
                        transcript_ref="p.7",
                    ),
                    QAEntry(
                        analysts="Sam Side (Boutique)",
                        topic="Services drag on margin",
                        tag="MARGIN",
                        question="When does the services decline stop being a margin headwind?",
                        answers=[("Chris Finance", "It is margin-accretive from Q3 onward.")],
                    ),
                ],
            )
        ],
    )


def _saydo() -> SayDoSection:
    return SayDoSection(
        status=SectionStatus.OK,
        cards=[
            SayDoCard(
                current_quarter="Q1",
                current_year=2026,
                prior_quarter="Q4",
                prior_year=2025,
                saydo_md=(
                    "## Print vs guide\n\n"
                    "| Metric | Guide | Actual | Verdict |\n"
                    "| --- | --- | --- | --- |\n"
                    "| Revenue | $1.76-1.79B | $1.81B | beat |\n"
                    "| Operating margin | ~24% | 24.8% | beat |\n"
                    "| FY26 revenue | $7.4B | raised to $7.5B | in-line |\n\n"
                    "Management delivered above the top end of its own range.\n\n"
                    "Performance Rating: EXCEEDED\n\nThesis View: Bullish\n\n"
                    "Attribution: Guidance conservatism plus genuine demand pull-forward."
                ),
                rating="EXCEEDED",
                thesis_view="Bullish",
                attribution="Guidance conservatism plus genuine demand pull-forward.",
            ),
            SayDoCard(
                current_quarter="Q4",
                current_year=2025,
                prior_quarter="Q3",
                prior_year=2025,
                saydo_md="Narrative-only quarter: commitments tracked qualitatively.",
                rating="MET",
                thesis_view="Neutral",
            ),
        ],
        historical_metrics=[
            SayDoHistoricalMetric(
                id=1,
                period_made=datetime(2025, 11, 5),
                period_target=datetime(2026, 3, 31),
                kpi_name="Revenue",
                comparator="gte",
                target_value=1760.0,
                realized_value=1810.0,
                outcome="met",
                guidance_narrative="Q1 revenue of $1.76-1.79B.",
                realized_narrative="Printed $1.81B.",
            ),
            SayDoHistoricalMetric(
                id=2,
                period_made=datetime(2026, 2, 4),
                period_target=datetime(2026, 6, 30),
                kpi_name="Operating margin",
                comparator="gte",
                target_value=24.0,
                realized_value=None,
                outcome=None,
                guidance_narrative="Q2 margin at or above 24%.",
            ),
        ],
    )


def _valuation_basis() -> ValuationBasisSection:
    history = [
        ValuationBasisHistoricalPoint(period_end=date(2024, 6, 30), value=11.2),
        ValuationBasisHistoricalPoint(period_end=date(2024, 9, 30), value=12.0),
        ValuationBasisHistoricalPoint(period_end=date(2024, 12, 31), value=13.1),
        ValuationBasisHistoricalPoint(period_end=date(2025, 3, 31), value=None),
        ValuationBasisHistoricalPoint(period_end=date(2025, 6, 30), value=12.4),
        ValuationBasisHistoricalPoint(period_end=date(2025, 9, 30), value=13.0),
        ValuationBasisHistoricalPoint(period_end=date(2025, 12, 31), value=13.8),
        ValuationBasisHistoricalPoint(period_end=date(2026, 3, 31), value=14.6),
    ]
    return ValuationBasisSection(
        status=SectionStatus.OK,
        multiple_name="EV/NTM Revenue",
        rationale=(
            "Pre-FCF-inflection software compounding name: forward revenue is the "
            "only multiple comparable across its peer set."
        ),
        current_value=14.6,
        current_value_display="14.6x",
        current_period_end=date(2026, 3, 31),
        history=history,
        historical_min=11.2,
        historical_max=14.6,
        historical_median=13.0,
        rich_cheap_verdict="rich vs 8Q median 13.0x",
        notes="Re-rate risk dominates below 12x; thesis works without multiple expansion.",
    )


def _snapshot() -> SnapshotSection:
    return SnapshotSection(
        status=SectionStatus.OK,
        ticker="TEST",
        company_name="Testable Platforms, Inc.",
        thesis_one_liner="Category-leading platform converting share into pricing power.",
        verdict="intact",
        valuation=ValuationSnapshot(
            consolidated_npv_per_share=86.0,
            current_price=72.4,
            implied_upside_pct=0.188,
            wacc=0.094,
            terminal_growth=0.025,
            valuation_date=date(2026, 5, 28),
            model_link="dcf/TEST_dcf.xlsx",
            sheet_url="https://docs.google.com/spreadsheets/d/test-sheet-id",
            over_under_pct=-0.158,
            mos_bar=0.25,
            trigger_status="hold",
            live_price_at=_TS,
            bull_npv_per_share=104.0,
            bear_npv_per_share=58.0,
            valuation_model_label="FCFF DCF",
            assumptions_sync_status="synced",
            assumptions_synced_at=_TS,
        ),
        tier_1_kpi_row=[
            KpiSnapshotRow(
                name="Net revenue retention",
                current="117.0%",
                prior="117.5%",
                threshold="110%",
                status="green",
            ),
            KpiSnapshotRow(
                name="Subscription gross margin",
                current="75.4%",
                prior="76.1%",
                threshold="72%",
                status="yellow",
            ),
        ],
        recent_decisions=[
            DecisionBadge(
                date_short="2026-04",
                recommendation_kind="add",
                outcome_label="pending",
                rationale_short="Added on the post-print dip; thesis KPIs intact.",
            ),
            DecisionBadge(
                date_short="2025-11",
                recommendation_kind="hold",
                outcome_label="correct",
                rationale_short="Held through the services reorg noise.",
            ),
        ],
    )


def _company_description() -> CompanyDescriptionSection:
    return CompanyDescriptionSection(
        status=SectionStatus.OK,
        elevator_pitch=(
            "Testable Platforms sells the workflow system of record for mid-market "
            "operations teams, monetized as tiered seat subscriptions."
        ),
        platform_diagram=(
            "+-----------+     +-----------+\n"
            "| Ingest    | --> | Workflow  |\n"
            "+-----------+     +-----------+"
        ),
        platform_caption="Data flows from ingest connectors into the workflow core.",
        business_overview=(
            "Two lines of business: the subscription platform (84% of revenue) and "
            "a deliberately shrinking services arm that seeds enterprise accounts."
        ),
        revenue_model="Per-seat subscription tiers; usage-based add-ons for AI features.",
        segment_breakdown=[
            SegmentWeighting(
                name="Subscriptions",
                revenue_usd_m=1520.0,
                share_pct=0.84,
                operating_income_usd_m=420.0,
                oi_share_pct=0.93,
                description="Recurring platform subscriptions across three tiers.",
            ),
            SegmentWeighting(
                name="Services",
                revenue_usd_m=290.0,
                share_pct=0.16,
                operating_income_usd_m=31.0,
                oi_share_pct=0.07,
                description="Implementation and premium support.",
            ),
        ],
        geographic_breakdown=[
            SegmentWeighting(name="Americas", revenue_usd_m=1180.0, share_pct=0.65),
            SegmentWeighting(name="EMEA", revenue_usd_m=450.0, share_pct=0.25),
            SegmentWeighting(name="APAC", revenue_usd_m=180.0, share_pct=0.10),
        ],
        source_fiscal_year=2025,
        cached_at=_TS,
        sector="Technology",
        industry="Software - Application",
    )


def _filing_intelligence() -> FilingIntelligenceSection:
    return FilingIntelligenceSection(
        status=SectionStatus.OK,
        ticker="TEST",
        fiscal_year=2025,
        analyzed_at="2026-04-02T09:00:00",
        segment_changes=SegmentChangeDetail(
            has_changes=True,
            description="Services folded its training sub-line into implementation.",
        ),
        metric_redefinitions=MetricRedefinitionDetail(
            has_changes=False,
            description=None,
        ),
        executive_comp=ExecutiveCompAlignmentDetail(
            metrics_used=["Revenue growth", "Net revenue retention", "Adj. operating margin"],
            targets_and_thresholds="NRR threshold at 112% for target bonus payout.",
            alignment_verdict="Comp design references two of three thesis tier-1 KPIs.",
        ),
        investment_signals=[
            InvestmentSignalDetail(
                signal_type="Footnote risk",
                severity="Medium",
                description="Customer-concentration footnote added a second 10% customer.",
            ),
            InvestmentSignalDetail(
                signal_type="Accounting change",
                severity="Low",
                description="Capitalized commission amortization extended to 5 years.",
            ),
        ],
        raw_synthesis_md="**Net read:** disclosure quality improved year over year.",
    )


def _exec_comp() -> ExecCompSectionModel:
    return ExecCompSectionModel(
        status=SectionStatus.OK,
        ticker="TEST",
        fiscal_year_latest=2025,
        packages=[
            ExecCompRowModel(
                executive_name="Pat Founder",
                role="CEO",
                is_ceo=True,
                fiscal_year=2025,
                base_salary=850000.0,
                cash_bonus_actual=1200000.0,
                cash_bonus_target=1000000.0,
                equity_grant_value=12000000.0,
                total_comp_granted=14050000.0,
                total_comp_realized=9800000.0,
                realized_vs_granted_pct=-0.302,
                ceo_pay_ratio=212.0,
                performance_metrics_summary="Revenue growth (50%), NRR (30%), margin (20%)",
                metrics_have_thesis_kpi=True,
            ),
            ExecCompRowModel(
                executive_name="Chris Finance",
                role="CFO",
                fiscal_year=2025,
                base_salary=560000.0,
                cash_bonus_actual=520000.0,
                cash_bonus_target=500000.0,
                equity_grant_value=4200000.0,
                total_comp_granted=5280000.0,
                total_comp_realized=4400000.0,
                realized_vs_granted_pct=-0.167,
                performance_metrics_summary="Revenue growth (40%), FCF (40%), margin (20%)",
                metrics_have_thesis_kpi=True,
            ),
        ],
        insider_signals=[
            InsiderSignalRowModel(
                insider_name="Pat Founder",
                role="CEO",
                transaction_date="2026-03-12",
                transaction_type="P-Purchase",
                shares=25000.0,
                transaction_value=1790000.0,
                signal_strength=0.82,
                rationale="Open-market buy outside any 10b5-1 plan, two days post-print.",
            )
        ],
        alignment_narrative_md=(
            "Comp design is **directionally aligned** with the thesis: NRR carries "
            "real weight, though the margin leg uses an adjusted definition."
        ),
        anomaly_flags=["CEO realized comp trails granted by 30% over the 3y window."],
    )


def _synthesis() -> SynthesisSection:
    return SynthesisSection(
        status=SectionStatus.OK,
        ticker="TEST",
        lenses=[
            SynthesisLensRow(
                name="contrarian",
                content_md=(
                    "The consensus treats services decline as planned; the contrarian "
                    "read is that enterprise seeding slows with it."
                ),
                model="claude-opus-4-8",
                generated_at=_TS_AWARE,
            ),
            SynthesisLensRow(
                name="capital_allocation",
                content_md="Buyback pace covers dilution 1.4x at the current run-rate.",
                model="claude-sonnet-4-6",
                generated_at=_TS_AWARE,
                is_dirty=True,
                is_stale=True,
            ),
        ],
    )


def _position() -> PortfolioPositionSection:
    return PortfolioPositionSection(
        status=SectionStatus.OK,
        held=True,
        accounts=[
            PortfolioPositionAccountRow(
                account_name="Taxable",
                quantity=420.0,
                cost_basis=24150.0,
                cost_basis_source=None,
                market_value=30408.0,
                unrealized_pnl=6258.0,
                unrealized_pct=0.259,
                snapshot_date=date(2026, 6, 10),
            ),
            PortfolioPositionAccountRow(
                account_name="IRA",
                quantity=180.0,
                cost_basis=11700.0,
                cost_basis_source="manual",
                market_value=13032.0,
                unrealized_pnl=1332.0,
                unrealized_pct=0.114,
                snapshot_date=date(2026, 6, 10),
            ),
        ],
        total_quantity=600.0,
        total_cost_basis=35850.0,
        total_market_value=43440.0,
        total_unrealized_pnl=7590.0,
        total_unrealized_pct=0.212,
        position_as_of=date(2026, 6, 10),
        recent_transactions=[
            PortfolioPositionTransaction(
                date=date(2026, 4, 14),
                account_name="Taxable",
                type="BUY",
                quantity=60.0,
                amount=4280.0,
            )
        ],
        open_decisions=[
            PortfolioPositionDecision(
                decision_date=date(2026, 4, 14),
                action="add",
                confidence="high",
                thesis="Post-print dip with thesis KPIs intact; sized to a 6% weight.",
                linked_brief_path="reports/TEST/2026-04-14_workspace.html",
            )
        ],
        closed_decisions=[
            PortfolioPositionDecision(
                decision_date=date(2025, 8, 2),
                action="trim",
                confidence="medium",
                thesis="Trimmed into the pre-print run-up above the DCF fair value.",
                outcome_status="validated",
                outcome_date=date(2025, 11, 6),
                outcome_notes="Bought back 9% lower after the guide-down overreaction.",
            )
        ],
    )


def _provenance() -> ProvenanceSection:
    return ProvenanceSection(
        status=SectionStatus.OK,
        coverage=[
            CoverageRow(
                quarter="Q1",
                year=2026,
                has_audio_file=False,
                has_transcript_file=True,
                has_release_file=True,
                has_slides_file=True,
                step_saydo_analyzed=True,
                step_llm_summarized=True,
            ),
            CoverageRow(
                quarter="Q4",
                year=2025,
                has_transcript_file=True,
                has_release_file=True,
                step_saydo_analyzed=True,
                step_llm_summarized=True,
            ),
        ],
        source_docs=[
            SourceDocRow(
                doc_type="10-Q",
                period_end="2026-03-31",
                file_path="data/historical/sec/TEST_10q_2026Q1.json",
                sha256="ab12cd34",
                fetched_at="2026-05-02T08:00:00",
                doc_id=101,  # deep-links the /source/<doc_id> viewer
            ),
            SourceDocRow(
                doc_type="transcript",
                period_end="2026-03-31",
                file_path="data/transcripts/TEST_Q1_2026.txt",
                fetched_at="2026-04-30T21:00:00",
                # doc_id left None — exercises the plain-path fallback for legacy rows
            ),
        ],
        open_validation_issues=2,
        open_issues_detail=[
            # Real severities the writer actually emits (models.validation.Severity:
            # halt/warn) — exercises the fixed severity tick so a HALT renders red,
            # not the muted grey the prior {error,warning} renderer produced.
            ValidationIssueRow(
                severity="halt",
                rule="magnitude_jump",
                raw_value="1810.0",
                expected="≤ 1600.0",
                raised_at="2026-05-03T08:30:00",
            ),
            ValidationIssueRow(
                severity="warn",
                rule="source_disagreement",
                raw_value="1810.0",
                expected="1807.0",
                raised_at="2026-05-02T09:15:00",
            ),
        ],
    )


def _news() -> RecentDevelopmentsSection:
    return RecentDevelopmentsSection(
        status=SectionStatus.OK,
        cached_at=_TS,
        news_days_window=7,
        content_md=(
            "## Product\n\n"
            "- **TEST ships AI workflow copilot to all tiers** - General availability "
            "lands two quarters ahead of the original roadmap. "
            "[Source: TechWire, 2026-06-09, https://example.com/copilot]\n"
            "- **Marketplace crosses 1,000 certified integrations** - Ecosystem "
            "expansion supports the pricing-power thesis. "
            "[Source: Company IR, 2026-06-06, https://example.com/marketplace]\n\n"
            "## Legal\n\n"
            "- **Patent suit from RIVL survives motion to dismiss** - Damages exposure "
            "unquantified; trial date not yet set. "
            "[Source: CourtWatch, 2026-06-05, https://example.com/suit]\n"
        ),
    )


def _bear() -> BearCaseSection:
    return BearCaseSection(
        status=SectionStatus.OK,
        failure_modes=[
            FailureMode(
                hypothesis="Seat-based pricing collapses under AI-driven seat compression",
                evidence_in_data="Seats per account flat YoY for the first time since 2022.",
                leading_indicator="Seats per account turning negative while NRR holds.",
                quantitative_impact="Each 5% seat compression costs ~340bps of growth.",
                refutation_criteria="Two quarters of seat growth above 3% YoY.",
            ),
            FailureMode(
                hypothesis="RIVL bundles the workflow core for free",
                evidence_in_data="RIVL's suite attach rate doubled in its latest disclosure.",
                leading_indicator="Win-rate commentary shifting in competitive deals.",
                quantitative_impact="Gross-new ARR could halve within four quarters.",
                refutation_criteria="Stable win rates through two RIVL release cycles.",
            ),
            FailureMode(
                hypothesis="Services decline overshoots and stalls enterprise lands",
                evidence_in_data="Services down 12% YoY vs the planned 8% glide path.",
                leading_indicator="Enterprise logo adds decelerating two quarters running.",
                quantitative_impact="A 20% enterprise-land shortfall is ~150bps of growth.",
                refutation_criteria="Enterprise logo adds reaccelerate by Q3 2026.",
            ),
        ],
        most_underweighted=(
            "Seat compression is the one the market is not pricing: every sell-side "
            "model still assumes seats grow with headcount."
        ),
        out_of_scope_flags=["Macro demand shock", "Acquisition of TEST itself"],
    )


def _p3_rich() -> WorkspaceP3Panels:
    return WorkspaceP3Panels(
        macro_sensitivities=[
            MacroSensitivityRow(
                series_id="DGS10",
                beta=-0.42,
                r_squared=0.31,
                lookback_window_days=365,
                computed_at=_TS,
            ),
            MacroSensitivityRow(
                series_id="DTWEXBGS",
                beta=-0.18,
                r_squared=0.12,
                lookback_window_days=365,
                computed_at=_TS,
            ),
        ],
        strategic_targets=[
            StrategicTargetRow(
                target_kind="operating_margin",
                target_value=40.0,
                target_unit="%",
                target_period="FY2028",
                target_currency=None,
                narrative_excerpt="Reach 40% operating margin by FY2028 at scale.",
                confidence=0.9,
            ),
            StrategicTargetRow(
                target_kind="revenue",
                target_value=10.0,
                target_unit="USD billions",
                target_period="FY2029",
                target_currency="USD",
                narrative_excerpt="A $10B revenue run-rate by the end of the decade.",
                confidence=0.7,
            ),
        ],
        customer_concentrations=[
            CustomerConcentrationRow(
                fiscal_period="2025",
                fiscal_period_type="FY",
                customer_label="Customer A",
                pct_of_revenue=11.0,
                revenue_amount=199.0,
                revenue_currency="USD",
            ),
            CustomerConcentrationRow(
                fiscal_period="2025",
                fiscal_period_type="FY",
                customer_label="Customer B",
                pct_of_revenue=10.2,
                revenue_amount=185.0,
                revenue_currency="USD",
            ),
        ],
        lease_ladder=[
            LeaseLadderRow(
                fiscal_year=2025,
                as_of_date=date(2025, 12, 31),
                lease_type="operating",
                ladder_year="year_1",
                ladder_calendar_year=2026,
                amount=42.0,
                currency="USD",
                unit="millions",
            ),
            LeaseLadderRow(
                fiscal_year=2025,
                as_of_date=date(2025, 12, 31),
                lease_type="operating",
                ladder_year="year_2",
                ladder_calendar_year=2027,
                amount=38.0,
                currency="USD",
                unit="millions",
            ),
            LeaseLadderRow(
                fiscal_year=2025,
                as_of_date=date(2025, 12, 31),
                lease_type="operating",
                ladder_year="thereafter",
                ladder_calendar_year=None,
                amount=96.0,
                currency="USD",
                unit="millions",
            ),
        ],
        decision_history=DecisionHistorySummary(
            total=3,
            by_kind={"add": 2, "trim": 1},
            by_conviction={"high": 1, "medium": 2},
            win_rate_overall=0.667,
            rows=[
                DecisionRow(
                    recommendation_kind="add",
                    recommendation_value=None,
                    conviction="high",
                    made_at=datetime(2026, 4, 14, 15, 30),
                    outcome_pct=None,
                    outcome_at=None,
                    rationale_excerpt="Post-print dip; thesis KPIs intact.",
                ),
                DecisionRow(
                    recommendation_kind="trim",
                    recommendation_value=0.02,
                    conviction="medium",
                    made_at=datetime(2025, 8, 2, 14, 0),
                    outcome_pct=0.09,
                    outcome_at=datetime(2025, 11, 6, 16, 0),
                    rationale_excerpt="Run-up above DCF fair value.",
                ),
            ],
        ),
        saydo_verdicts=[
            SayDoVerdictRow(
                period_made=datetime(2025, 11, 5),
                period_target=datetime(2026, 3, 31),
                kpi_name="Revenue",
                comparator="gte",
                target_value=1760.0,
                unit="USD millions",
                narrative="Q1 revenue of $1.76-1.79B.",
                realized_value=1810.0,
                outcome="met",
                evaluated_at=_TS,
            ),
            SayDoVerdictRow(
                period_made=datetime(2026, 2, 4),
                period_target=datetime(2026, 6, 30),
                kpi_name="Operating margin",
                comparator="gte",
                target_value=24.0,
                unit="%",
                narrative="Q2 margin at or above 24%.",
                realized_value=None,
                outcome=None,
                evaluated_at=None,
            ),
        ],
        peer_comp=[
            PeerCompRow(
                peer_ticker="RIVL",
                peer_name="Rival Systems",
                market_cap_usd=48_000_000_000.0,
                revenue_ttm_usd=5_900_000_000.0,
                net_margin_ttm=0.18,
                roic_ttm=0.21,
                match_reasons=("named rival", "same industry"),
            ),
            PeerCompRow(
                peer_ticker="MEGA",
                peer_name="Mega Suite Corp",
                market_cap_usd=310_000_000_000.0,
                revenue_ttm_usd=41_000_000_000.0,
                net_margin_ttm=0.27,
                roic_ttm=0.19,
                match_reasons=("adjacent platform", "similar buyer"),
            ),
        ],
        open_notes=[
            AnalystNoteRow(
                id=1,
                user_id="bhanu",
                ticker="TEST",
                kind="watch_item",
                status="open",
                body="Watch management's Q&A handling of seat-compression questions.",
                anchor_type="tab",
                anchor_key="earnings",
                source="manual",
                source_ref=None,
                supersedes_id=None,
                resolution_note=None,
                context=None,
                created_at=_TS,
                updated_at=_TS,
                resolved_at=None,
            ),
            AnalystNoteRow(
                id=2,
                user_id="bhanu",
                ticker="TEST",
                kind="followup",
                status="open",
                body="Re-run the DCF once FY26 capex guidance lands.",
                anchor_type=None,
                anchor_key=None,
                source="manual",
                source_ref=None,
                supersedes_id=None,
                resolution_note=None,
                context=None,
                created_at=_TS,
                updated_at=_TS,
                resolved_at=None,
            ),
        ],
    )


def _portfolio_spec(repo_root: str) -> ReportSpec:
    return ReportSpec(
        ticker="TEST",
        generation_date=_GEN_DATE,
        repo_root=repo_root,
        run_id="golden-fixture-run",
        flavor=ReportFlavor.PORTFOLIO,
        llm_enabled=False,
        forgone_due_to_budget=[
            BudgetSkip(
                section="Bear case (§7)",
                purpose="bear_case",
                cap_usd=20.0,
                spend_usd=20.4,
                headroom_pct=-0.02,
            )
        ],
        suppressed_sections=[],
        portfolio_position=_position(),
        valuation_basis=_valuation_basis(),
        snapshot=_snapshot(),
        evaluation_snapshot=None,
        company_description=_company_description(),
        thesis=_thesis(),
        financials=_financials(),
        signals=_signals(),
        segments=_segments(),
        earnings=_earnings(),
        saydo=_saydo(),
        ir_docs=IrDocsSection(status=SectionStatus.OK),
        recent_developments=_news(),
        bear_case=_bear(),
        provenance=_provenance(),
        appendix=AppendixSection(
            status=SectionStatus.OK,
            transcripts=[
                TranscriptEntry(
                    quarter="Q1",
                    year=2026,
                    source_path="data/transcripts/TEST_Q1_2026.txt",
                    text=(
                        "Operator: Good afternoon and welcome to the TEST first quarter "
                        "2026 earnings call.\nPat Founder: Thanks everyone for joining."
                    ),
                )
            ],
        ),
        qa_roster=_qa_roster(),
        filing_intelligence=_filing_intelligence(),
        exec_compensation=_exec_comp(),
        synthesis=_synthesis(),
    )


def _evaluation_spec(repo_root: str) -> ReportSpec:
    spec = _portfolio_spec(repo_root)
    return spec.model_copy(
        update={
            "ticker": "EVAL",
            "flavor": ReportFlavor.EVALUATION,
            "portfolio_position": None,
            "forgone_due_to_budget": [],
            "evaluation_snapshot": EvaluationSnapshotSection(
                status=SectionStatus.OK,
                ticker="EVAL",
                company_name="Evaluand Industries",
                sector="Industrials",
                market_cap=8_400_000_000.0,
                current_price=44.1,
                fiscal_years=[2023, 2024, 2025],
                rows=[
                    QuickCategorizationRow(
                        metric="Revenue",
                        unit="USD M",
                        digits=0,
                        lfy_minus_2=2100.0,
                        lfy_minus_1=2480.0,
                        lfy=2950.0,
                        ttm=3120.0,
                        cagr_3y=0.185,
                    ),
                    QuickCategorizationRow(
                        metric="EPS diluted",
                        unit="USD",
                        digits=2,
                        lfy_minus_2=1.10,
                        lfy_minus_1=1.52,
                        lfy=2.04,
                        ttm=2.21,
                        cagr_3y=0.362,
                    ),
                    QuickCategorizationRow(
                        metric="Operating margin",
                        unit="%",
                        digits=1,
                        lfy_minus_2=14.2,
                        lfy_minus_1=16.0,
                        lfy=17.8,
                        ttm=18.3,
                    ),
                    QuickCategorizationRow(
                        metric="FCF",
                        unit="USD M",
                        digits=0,
                        lfy_minus_2=180.0,
                        lfy_minus_1=None,
                        lfy=410.0,
                        ttm=455.0,
                    ),
                ],
            ),
        }
    )


# ---------------------------------------------------------------------------
# Render + decompose
# ---------------------------------------------------------------------------

# Section panes expected per fixture — pins the tab contract as a side effect.
PORTFOLIO_PANES = [
    "thesis",
    "valuation",
    "earnings",
    "saydo",
    "news",
    "financials",
    "bear",
    "company",
    "exec_comp",
    "synthesis",
    "position",
    "decisions",
    "sources",
]
# Evaluation fixture: not held (no position pane); decision history present, so
# Decisions survives as a standalone group (the exited-name rule).
EVALUATION_PANES = [p for p in PORTFOLIO_PANES if p != "position"]

NON_PANE_PARTS = ["shell", "styles", "styles_sorted", "scripts"]


def _part_names(panes: list[str]) -> list[str]:
    return NON_PANE_PARTS + [f"pane_{p}" for p in panes]


_STYLE_RX = re.compile(r"<style>(.*?)</style>", re.DOTALL)
_SCRIPT_RX = re.compile(r"<script>(.*?)</script>", re.DOTALL)
_PANE_OPEN_RX = re.compile(r'<div class="tab-pane subtab-pane(?: active)?" data-tab="([^"]+)">')
_DIV_TOKEN_RX = re.compile(r"<div\b|</div>")
_RELTIME_RX = re.compile(r"\b(?:just now|in \d+(?:mo|m|h|d)|\d+(?:mo|m|h|d) ago)\b")


def _balanced_div_end(html: str, open_end: int) -> int:
    """Index just past the </div> matching the <div ...> ending at open_end."""
    depth = 1
    pos = open_end
    while depth:
        m = _DIV_TOKEN_RX.search(html, pos)
        if m is None:  # pragma: no cover - malformed render
            msg = "unbalanced <div> while extracting section panes"
            raise AssertionError(msg)
        depth += 1 if m.group(0) != "</div>" else -1
        pos = m.end()
    return pos


def _readable(html: str) -> str:
    """Line-oriented form: newline between adjacent tags (pure display
    transform — both sides of every comparison get it)."""
    return html.replace("><", ">\n<")


def _css_blocks_sorted(css: str) -> str:
    """Top-level CSS blocks (brace-balanced, @media kept whole), each
    whitespace-collapsed, sorted. Survives moving rules between modules;
    catches dropped or edited rules."""
    blocks: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(css):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blocks.append(css[start : i + 1])
                start = i + 1
    tail = css[start:].strip()
    if tail:
        blocks.append(tail)
    norm = [re.sub(r"\s+", " ", b).strip() for b in blocks]
    return "\n".join(sorted(n for n in norm if n))


def _decompose(doc: str, repo_root: str) -> dict[str, str]:
    """Split a rendered workspace document into named, normalized parts."""
    # Environment-dependent path + wall-clock-relative strings → fixed tokens.
    for needle in (repo_root, repo_root.replace("\\", "/")):
        doc = doc.replace(needle, "[REPO]")
    doc = _RELTIME_RX.sub("[RELTIME]", doc)

    styles = "\n".join(m.group(1) for m in _STYLE_RX.finditer(doc))
    doc = _STYLE_RX.sub("<style>[EXTRACTED]</style>", doc)
    scripts = "\n".join(m.group(1) for m in _SCRIPT_RX.finditer(doc))
    doc = _SCRIPT_RX.sub("<script>[EXTRACTED]</script>", doc)

    parts: dict[str, str] = {}
    skeleton: list[str] = []
    pos = 0
    while True:
        m = _PANE_OPEN_RX.search(doc, pos)
        if m is None:
            skeleton.append(doc[pos:])
            break
        end = _balanced_div_end(doc, m.end())
        pane_id = m.group(1)
        parts[f"pane_{pane_id}"] = _readable(doc[m.start() : end])
        skeleton.append(doc[pos : m.start()])
        skeleton.append(f"<!--PANE:{pane_id}-->")
        pos = end
    parts["shell"] = _readable("".join(skeleton))
    parts["styles"] = _readable(styles)
    parts["styles_sorted"] = _css_blocks_sorted(styles)
    parts["scripts"] = scripts
    return parts


def _patched_p3_loader(_ticker: str, _root: Path) -> WorkspaceP3Panels:
    return _p3_rich()


def _render_parts(flavor: str, repo_root: Path) -> dict[str, str]:
    root = str(repo_root)
    spec = _portfolio_spec(root) if flavor == "portfolio" else _evaluation_spec(root)
    with mock.patch.object(workspace_html, "load_workspace_p3_panels", _patched_p3_loader):
        doc = workspace_html.render(spec)
        doc_again = workspace_html.render(spec)
    # Determinism guard: two renders of the same spec must be identical after
    # reltime normalization (catches accidental wall-clock / ordering leaks).
    assert _RELTIME_RX.sub("[RELTIME]", doc) == _RELTIME_RX.sub("[RELTIME]", doc_again)
    return _decompose(doc, root)


@pytest.fixture(scope="session")
def golden_repo_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("ws_golden_repo")


@pytest.fixture(scope="session")
def portfolio_parts(golden_repo_root: Path) -> dict[str, str]:
    return _render_parts("portfolio", golden_repo_root)


@pytest.fixture(scope="session")
def evaluation_parts(golden_repo_root: Path) -> dict[str, str]:
    return _render_parts("evaluation", golden_repo_root)


# ---------------------------------------------------------------------------
# Golden comparison
# ---------------------------------------------------------------------------


def _ext(part: str) -> str:
    if part.startswith("styles"):
        return "css"
    return "js" if part == "scripts" else "html"


# CSS + JS are module constants independent of the spec — both flavors must
# produce byte-identical bundles (asserted below), so they golden ONCE at the
# top level instead of per spec dir.
SHARED_PARTS = frozenset({"styles", "styles_sorted", "scripts"})


def _check_golden(spec_name: str, part: str, content: str) -> None:
    rel = f"{part}.{_ext(part)}" if part in SHARED_PARTS else f"{spec_name}/{part}.{_ext(part)}"
    path = GOLDEN_DIR / rel
    if REGEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return
    if not path.exists():
        pytest.fail(
            f"golden missing: {path}\n"
            "Run: GOLDEN_REGEN=1 python -m pytest tests/test_workspace_golden.py"
        )
    want = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if content != want:
        diff = "\n".join(
            islice(
                difflib.unified_diff(
                    want.splitlines(),
                    content.splitlines(),
                    fromfile=f"golden/{spec_name}/{part}",
                    tofile="rendered",
                    lineterm="",
                ),
                160,
            )
        )
        pytest.fail(
            f"workspace render drifted from golden part '{part}' ({spec_name}).\n"
            "If intentional, regenerate (GOLDEN_REGEN=1) and call the diff out "
            f"in the PR.\n{diff}"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_portfolio_part_inventory(portfolio_parts: dict[str, str]) -> None:
    assert sorted(portfolio_parts) == sorted(_part_names(PORTFOLIO_PANES))


def test_evaluation_part_inventory(evaluation_parts: dict[str, str]) -> None:
    assert sorted(evaluation_parts) == sorted(_part_names(EVALUATION_PANES))


@pytest.mark.parametrize("part", _part_names(PORTFOLIO_PANES))
def test_portfolio_golden(part: str, portfolio_parts: dict[str, str]) -> None:
    _check_golden("portfolio", part, portfolio_parts[part])


@pytest.mark.parametrize("part", _part_names(EVALUATION_PANES))
def test_evaluation_golden(part: str, evaluation_parts: dict[str, str]) -> None:
    _check_golden("evaluation", part, evaluation_parts[part])


def test_asset_bundles_flavor_independent(
    portfolio_parts: dict[str, str], evaluation_parts: dict[str, str]
) -> None:
    """The CSS/JS bundles are spec-independent module constants — pinned as
    shared goldens, so the two flavors must agree byte-for-byte."""
    for part in sorted(SHARED_PARTS):
        assert portfolio_parts[part] == evaluation_parts[part], part


def test_p3_seam_reaches_document(portfolio_parts: dict[str, str]) -> None:
    """The p3 injection seam (mock of workspace_html.load_workspace_p3_panels)
    must actually feed the render — if the renderer stops resolving that name
    from its module globals, the patch silently no-ops and the rich panels
    vanish. Anchor on content that ONLY the patched p3 provides."""
    assert "Reach 40% operating margin by FY2028 at scale." in portfolio_parts["pane_company"]
    assert (
        "Watch management&#x27;s Q&amp;A handling of seat-compression questions."
        in portfolio_parts["shell"]
    )


def test_portfolio_flavor_shows_peer_comp_panel(portfolio_parts: dict[str, str]) -> None:
    """Owner decision 2026-07-02 (directives/peer_selection_llm.md): the peer
    comparison panel is no longer gated to `--flavor evaluation`. Portfolio
    builds (the owner's actual holdings) must render it, populated with
    computed multiples, in the Company tab — same fixture peer rows
    (`_p3_rich`) that `test_p3_seam_reaches_document` anchors on."""
    html = portfolio_parts["pane_company"]
    assert 'class="panel peer-comp-panel"' in html
    assert "Peer comparison" in html
    assert "RIVL" in html
    assert "Rival Systems" in html
    assert "$48.0B" in html


def test_fixture_prose_carries_no_reltime_shapes(portfolio_parts: dict[str, str]) -> None:
    """[RELTIME] replacement must only ever hit wall-clock-derived strings;
    a fixture string that itself matches the pattern would golden as the
    token and mask drift. The fixtures keep such phrasing out — this guards
    future fixture edits."""
    joined = "\n".join(portfolio_parts.values())
    # Every remaining [RELTIME] token came from the replacement; the raw
    # pattern must no longer appear anywhere.
    assert _RELTIME_RX.search(joined) is None
