"""ReportSpec → Markdown long-form research doc.

Stub renderer: every section emits a header, status line, and either content
or a clear 'missing — fix command' block. The shape is what reviewers react
to first; content density grows as upstream sections fill in.
"""

from __future__ import annotations

from io import StringIO

from report.models import (
    AppendixSection,
    BearCaseSection,
    CompanyDescriptionSection,
    EarningsSection,
    EvaluationSnapshotSection,
    FinancialsSection,
    IrDocsSection,
    KpiLedgerRow,
    ProvenanceSection,
    QuarterlyEarningsCard,
    QuarterlyLineItem,
    RecentDevelopmentsSection,
    ReportFlavor,
    ReportSpec,
    SayDoSection,
    SectionStatus,
    SegmentSeries,
    SegmentsSection,
    SegmentWeighting,
    SnapshotSection,
    SurpriseScorecardCard,
    ThesisSection,
    TranscriptEntry,
    ValuationSnapshot,
)


def render(spec: ReportSpec) -> str:
    out = StringIO()
    _header(out, spec)
    if spec.portfolio_position is not None:
        _portfolio_position(out, spec)
    if spec.flavor == ReportFlavor.EVALUATION and spec.evaluation_snapshot is not None:
        _evaluation_snapshot(out, spec.evaluation_snapshot)
    else:
        _snapshot(out, spec.snapshot)
    _company_description(out, spec.company_description)
    _thesis(out, spec.thesis)
    _financials(out, spec.financials)
    _segments(out, spec.segments)
    _earnings(out, spec.earnings)
    _saydo(out, spec.saydo)
    _ir_docs(out, spec.ir_docs)
    _recent_developments(out, spec.recent_developments)
    _bear_case(out, spec.bear_case)
    _provenance(out, spec.provenance)
    _appendix(out, spec.appendix)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _header(out: StringIO, spec: ReportSpec) -> None:
    out.write(f"# {spec.ticker} — research report\n\n")
    out.write(f"_Generated: {spec.generation_date.isoformat()}_\n\n")
    out.write(f"_Repo root: `{spec.repo_root}`_\n\n")
    out.write("---\n\n")


def _portfolio_position(out: StringIO, spec: ReportSpec) -> None:
    pp = spec.portfolio_position
    if pp is None or pp.status == SectionStatus.NOT_APPLICABLE:
        return
    out.write(f"## Your position in {spec.ticker}\n\n")
    if pp.held and pp.total_market_value is not None:
        pct_str = (
            f"{pp.total_unrealized_pct * 100:+.1f}%"
            if pp.total_unrealized_pct is not None
            else "—"
        )
        pnl_str = (
            f"${pp.total_unrealized_pnl:+,.0f}"
            if pp.total_unrealized_pnl is not None
            else "—"
        )
        out.write(
            f"**{pp.total_quantity:,.4f} sh** · "
            f"cost **${pp.total_cost_basis or 0:,.0f}** · "
            f"value **${pp.total_market_value:,.0f}** · "
            f"unrealized **{pnl_str} ({pct_str})**\n\n"
        )
    elif pp.held:
        out.write(f"**{pp.total_quantity:,.4f} sh** held (cost basis unknown)\n\n")
    if pp.accounts:
        out.write("| Account | Qty | Cost | Value | Unrealized | Source |\n")
        out.write("| --- | ---: | ---: | ---: | ---: | --- |\n")
        for a in pp.accounts:
            cost_s = f"${a.cost_basis:,.0f}" if a.cost_basis is not None else "—"
            value_s = f"${a.market_value:,.0f}" if a.market_value is not None else "—"
            pnl_s = (
                f"${a.unrealized_pnl:+,.0f} ({a.unrealized_pct * 100:+.1f}%)"
                if a.unrealized_pnl is not None and a.unrealized_pct is not None
                else "—"
            )
            src = a.cost_basis_source or "broker"
            out.write(f"| {a.account_name} | {a.quantity:,.4f} | {cost_s} | {value_s} | {pnl_s} | {src} |\n")
        out.write("\n")
    if pp.recent_transactions:
        out.write("**Recent activity**\n\n")
        for t in pp.recent_transactions:
            out.write(
                f"- {t.date.isoformat()} · {t.account_name} · {t.type} "
                f"{abs(t.quantity):,.4f} sh · ${abs(t.amount):,.0f}\n"
            )
        out.write("\n")
    if pp.open_decisions:
        out.write("**Your open thesis on this name**\n\n")
        for d in pp.open_decisions:
            conf = f" ({d.confidence})" if d.confidence else ""
            out.write(
                f"- {d.decision_date.isoformat()} · **{d.action}**{conf}: "
                f"{d.thesis[:240]}{'…' if len(d.thesis) > 240 else ''}\n"
            )
            if d.linked_brief_path:
                out.write(f"  - linked brief: `{d.linked_brief_path}`\n")
        out.write("\n")
    out.write("---\n\n")


def _section_header(out: StringIO, num: int, title: str, status: SectionStatus) -> None:
    out.write(f"## §{num} {title}\n\n")
    out.write(f"_Status: `{status.value}`_\n\n")


def _missing_block(out: StringIO, status: SectionStatus, missing: object) -> bool:
    """Render the 'missing data' block. Returns True if it wrote (and caller should skip content)."""
    if status == SectionStatus.OK:
        return False
    if missing is None:
        return False
    stage = getattr(missing, "stage", "unknown")
    fix = getattr(missing, "fix_command", "")
    detail = getattr(missing, "detail", None)
    out.write(f"> **Pending stage:** `{stage}`\n>\n")
    out.write(f"> **Fix:** `{fix}`\n")
    if detail:
        out.write(f">\n> {detail}\n")
    out.write("\n")
    return status not in (SectionStatus.PARTIAL,)


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.1f}%"


def _fmt_num(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:,.{digits}f}"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _snapshot(out: StringIO, s: SnapshotSection) -> None:
    _section_header(out, 1, "Executive snapshot", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    out.write(f"**{s.ticker}** — {s.company_name or '—'}  \n")
    out.write(f"**Verdict:** `{s.verdict}`  \n")
    if s.thesis_one_liner:
        out.write(f"**Thesis:** {s.thesis_one_liner}\n\n")
    _valuation_card_md(out, s.valuation)
    if s.valuation.model_link:
        out.write(f"_DCF workbook: `{s.valuation.model_link}`_\n\n")


_TRIGGER_LABEL_MD: dict[str, str] = {
    "sell": "**SELL** — DCF says >20% over fair value",
    "trim": "**TRIM** — DCF says >10% over fair value",
    "hold": "HOLD — within trim/sell band",
    "initiate_candidate": "**INITIATE candidate** — beyond MoS bar",
    "unknown": "_DCF not yet computed_",
}


def _valuation_card_md(out: StringIO, v: ValuationSnapshot) -> None:
    if v.consolidated_npv_per_share is None and v.current_price is None:
        out.write(
            "> **DCF not yet computed.** Run "
            "`python execution/refresh_dcf.py --ticker <TICKER>` "
            "after the canonical workbook (`dcf/<TICKER>.xlsx`) is in place.\n\n"
        )
        return
    out.write("| Metric | Value |\n|---|---|\n")
    if v.consolidated_npv_per_share is not None:
        out.write(f"| Fair value / share | ${v.consolidated_npv_per_share:,.2f} |\n")
    if v.current_price is not None:
        suffix = ""
        if v.live_price_at is not None:
            suffix = f" *(as of {v.live_price_at.date().isoformat()})*"
        out.write(f"| Live price | ${v.current_price:,.2f}{suffix} |\n")
    if v.over_under_pct is not None:
        out.write(
            f"| Over/under | {v.over_under_pct * 100:+.1f}% — "
            f"{_TRIGGER_LABEL_MD.get(v.trigger_status, v.trigger_status)} |\n"
        )
    out.write("\n")
    meta_parts: list[str] = []
    if v.wacc is not None:
        meta_parts.append(f"WACC {v.wacc * 100:.1f}%")
    if v.mos_bar is not None:
        meta_parts.append(f"MoS bar {v.mos_bar * 100:.0f}%")
    if v.valuation_date is not None:
        meta_parts.append(f"Valued {v.valuation_date}")
    if meta_parts:
        out.write(f"_{' · '.join(meta_parts)}_\n\n")


def _evaluation_snapshot(out: StringIO, s: EvaluationSnapshotSection) -> None:
    _section_header(out, 1, "Evaluation snapshot", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    out.write(f"**{s.ticker}**")
    if s.company_name:
        out.write(f" — {s.company_name}")
    out.write("\n\n")
    chips: list[str] = []
    if s.sector:
        chips.append(f"Sector: {s.sector}")
    if s.market_cap is not None:
        chips.append(f"Market cap: ${_fmt_compact_usd(s.market_cap)}")
    if s.current_price is not None:
        chips.append(f"Current price: ${s.current_price:,.2f}")
    if chips:
        out.write(f"_{' · '.join(chips)}_\n\n")
    if not s.rows:
        out.write("_No metric rows available._\n\n")
        return
    year_labels = [str(y) for y in s.fiscal_years]
    while len(year_labels) < 3:
        year_labels.insert(0, "—")
    headers = ["Metric", "Unit", year_labels[0], year_labels[1], year_labels[2], "TTM", "3y CAGR"]
    out.write("| " + " | ".join(headers) + " |\n")
    out.write("|" + "|".join(["---"] * len(headers)) + "|\n")
    for r in s.rows:
        cells = [
            f"**{r.metric}**",
            r.unit,
            _fmt_metric_md(r.lfy_minus_2, r.unit, r.digits),
            _fmt_metric_md(r.lfy_minus_1, r.unit, r.digits),
            _fmt_metric_md(r.lfy, r.unit, r.digits),
            _fmt_metric_md(r.ttm, r.unit, r.digits),
            _fmt_pct(r.cagr_3y),
        ]
        out.write("| " + " | ".join(cells) + " |\n")
    out.write("\n")


def _fmt_metric_md(v: float | None, unit: str, digits: int) -> str:
    if v is None:
        return "—"
    if unit == "%":
        return f"{v * 100:.{digits}f}%"
    return f"{v:,.{digits}f}"


def _fmt_compact_usd(v: float) -> str:
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.0f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:,.0f}"


def _company_description(out: StringIO, s: CompanyDescriptionSection) -> None:
    _section_header(out, 2, "Company description", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    chips: list[str] = []
    if s.sector:
        chips.append(f"Sector: {s.sector}")
    if s.industry:
        chips.append(f"Industry: {s.industry}")
    if s.source_fiscal_year is not None:
        chips.append(f"Source: 10-K FY{s.source_fiscal_year}")
    if chips:
        out.write(f"_{' · '.join(chips)}_\n\n")
    if s.elevator_pitch:
        out.write(f"> {s.elevator_pitch}\n\n")
    if s.business_overview:
        out.write("### Lines of business\n\n")
        out.write(s.business_overview.strip() + "\n\n")
    if s.revenue_model:
        out.write("### How it makes money\n\n")
        out.write(s.revenue_model.strip() + "\n\n")
    if s.segment_breakdown:
        out.write("### Segment weighting (latest quarter)\n\n")
        _weighting_table_md(out, s.segment_breakdown, label="Segment")
    if s.geographic_breakdown:
        out.write("### Geographic weighting (latest quarter)\n\n")
        _weighting_table_md(out, s.geographic_breakdown, label="Geography")


def _weighting_table_md(out: StringIO, rows: list[SegmentWeighting], label: str) -> None:
    out.write(f"| {label} | Revenue (USD M) | Share | Description |\n")
    out.write("|---|---|---|---|\n")
    for r in rows:
        rev = _fmt_num(r.revenue_usd_m, 0)
        share = "—" if r.share_pct is None else f"{r.share_pct * 100:.1f}%"
        desc = r.description.replace("|", "\\|") if r.description else "—"
        out.write(f"| **{r.name}** | {rev} | {share} | {desc} |\n")
    out.write("\n")


def _thesis(out: StringIO, s: ThesisSection) -> None:
    _section_header(out, 3, "Thesis & tier-1 KPIs", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    if s.thesis_full:
        out.write(f"{s.thesis_full}\n\n")
    if s.last_updated:
        out.write(f"_Last updated: {s.last_updated.isoformat()}_\n\n")
    if s.break_conditions:
        out.write("### Break conditions\n\n")
        for bc in s.break_conditions:
            out.write(f"- {bc}\n")
        out.write("\n")
    if s.qualitative_breakers:
        out.write("### Qualitative thesis breakers\n\n")
        for q in s.qualitative_breakers:
            out.write(f"- {q}\n")
        out.write("\n")
    if s.competitive_watchlist:
        out.write("### Competitive watchlist\n\n")
        out.write(", ".join(s.competitive_watchlist) + "\n\n")
    _break_rules_block(out, s)
    if s.kpi_ledger:
        _kpi_ledger(out, s.kpi_ledger)


_COMPARATOR_SYMBOL_MD: dict[str, str] = {"lt": "<", "le": "≤", "gt": ">", "ge": "≥", "eq": "="}


def _break_rules_block(out: StringIO, s: ThesisSection) -> None:
    if s.overall_breach_status == "unknown" and not s.break_rule_evaluations:
        out.write("### Universal break rules\n\n")
        out.write(
            "_Not yet evaluated. Run `python execution/run_thesis_evaluator.py --ticker <T>` to populate `thesis_evaluations`._\n\n"
        )
        return
    out.write("### Universal break rules\n\n")
    eval_when = (
        f" — evaluated {s.last_evaluated_at.isoformat(timespec='seconds')}"
        if s.last_evaluated_at
        else ""
    )
    out.write(f"**Overall:** `{s.overall_breach_status}`{eval_when}\n\n")
    if not s.break_rule_evaluations:
        return
    out.write("| Status | Rule | Threshold | Latest | Detail |\n|---|---|---|---|---|\n")
    for ev in s.break_rule_evaluations:
        comp = _COMPARATOR_SYMBOL_MD.get(ev.comparator, ev.comparator)
        threshold = f"{comp} {ev.threshold:g} for {ev.consecutive_periods} consecutive periods"
        if ev.observations:
            latest_cells = []
            for o in ev.observations[: max(2, ev.consecutive_periods)]:
                unit = "%" if o.unit == "percent" else (f" {o.unit}" if o.unit else "")
                latest_cells.append(f"{o.period_end}: **{o.value:.2f}{unit}**")
            latest = "<br>".join(latest_cells)
        else:
            latest = "—"
        out.write(
            f"| `{ev.status}` | **{ev.kpi_name}** — {ev.narrative} | {threshold} | {latest} | {ev.detail} |\n"
        )
    out.write("\n")


def _kpi_ledger(out: StringIO, rows: list[KpiLedgerRow]) -> None:
    out.write("### KPI ledger\n\n")
    out.write("| Tier | KPI | Source | Break | Status |\n|---|---|---|---|---|\n")
    for r in rows:
        out.write(
            f"| {r.tier} | {r.name} | {r.source_hint or '—'} | {r.break_condition or '—'} | {r.current_status} |\n"
        )
    out.write("\n")


def _financials(out: StringIO, s: FinancialsSection) -> None:
    _section_header(out, 4, "Financials — last 12 quarters", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    if not s.line_items:
        out.write("_No line items rendered._\n\n")
        return
    _quarterly_table(out, s.quarter_labels, s.line_items)


def _quarterly_table(out: StringIO, quarters: list[str], rows: list[QuarterlyLineItem]) -> None:
    headers = ["Line item", "Unit", *quarters, "QoQ", "YoY", "1Y CAGR", "3Y CAGR"]
    out.write("| " + " | ".join(headers) + " |\n")
    out.write("|" + "|".join(["---"] * len(headers)) + "|\n")
    for r in rows:
        cells: list[str] = [r.line_item, r.unit]
        cells.extend(_fmt_num(v, r.digits) for v in r.values)
        cells.extend(
            [
                _fmt_pct(r.growth.qoq),
                _fmt_pct(r.growth.yoy),
                _fmt_pct(r.growth.cagr_1y_ttm),
                _fmt_pct(r.growth.cagr_3y_ttm),
            ]
        )
        out.write("| " + " | ".join(cells) + " |\n")
    out.write("\n")


def _segments(out: StringIO, s: SegmentsSection) -> None:
    _section_header(out, 5, "Segments — last 12 quarters", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    for label, group in (
        ("Revenue by product", s.revenue_by_product),
        ("Revenue by geography", s.revenue_by_geography),
        ("Operating income", s.operating_income),
    ):
        if not group:
            continue
        out.write(f"### {label}\n\n")
        _segments_table(out, s.quarter_labels, group)


def _segments_table(out: StringIO, quarters: list[str], rows: list[SegmentSeries]) -> None:
    headers = ["Segment", "Unit", *quarters, "QoQ", "YoY", "1Y CAGR", "3Y CAGR"]
    out.write("| " + " | ".join(headers) + " |\n")
    out.write("|" + "|".join(["---"] * len(headers)) + "|\n")
    for r in rows:
        cells: list[str] = [r.segment_name, r.unit]
        cells.extend(_fmt_num(v, 0) for v in r.values)
        cells.extend(
            [
                _fmt_pct(r.growth.qoq),
                _fmt_pct(r.growth.yoy),
                _fmt_pct(r.growth.cagr_1y_ttm),
                _fmt_pct(r.growth.cagr_3y_ttm),
            ]
        )
        out.write("| " + " | ".join(cells) + " |\n")
    out.write("\n")


def _earnings(out: StringIO, s: EarningsSection) -> None:
    _section_header(out, 6, "Earnings analysis", s.status)
    # Scorecard renders BEFORE the missing-data check because it draws from
    # earnings_surprises — a separate pipeline from the LLM summaries that
    # drive the MISSING_DATA status. A ticker can have a fully-populated
    # beat-rate scorecard while still waiting on process_ir_documents.
    _surprise_scorecard_block(out, s.surprise_scorecard)
    if _missing_block(out, s.status, s.missing):
        return
    cards = list(s.full_quarters) + list(s.digest_quarters)
    cards.sort(key=lambda c: (c.year, c.quarter), reverse=True)
    for q in cards:
        if q.is_recent:
            _full_card(out, q)
        else:
            _digest_card(out, q)


def _surprise_scorecard_block(out: StringIO, c: SurpriseScorecardCard | None) -> None:
    """Header table: last N quarters' EPS/Revenue beat-rate vs street.

    Each row shows beats / misses / beat rate / average surprise / latest
    surprise. A side with no_data == total_quarters means the source was
    absent for the entire window (post-FMP-lapse revenue is the typical
    case); we render '—' for those cells rather than a misleading 0%.
    """
    if c is None or c.total_quarters == 0:
        return
    out.write(f"**Analyst surprise — last {c.total_quarters} reported quarters**\n\n")
    out.write("| Metric | Beats | Misses | Beat rate | Avg surprise | Latest |\n")
    out.write("|:--- |---:|---:|---:|---:|---:|\n")
    out.write(
        f"| EPS | {c.eps_beats} | {c.eps_misses} | "
        f"{_fmt_surprise_pct(c.eps_beat_rate_pct, 1)} | {_fmt_surprise_pct(c.eps_avg_surprise_pct, 2)} | "
        f"{_fmt_surprise_pct(c.eps_latest_surprise_pct, 2)} |\n"
    )
    if c.revenue_no_data >= c.total_quarters:
        out.write("| Revenue | — | — | — | — | — _(source coverage absent)_ |\n")
    else:
        out.write(
            f"| Revenue | {c.revenue_beats} | {c.revenue_misses} | "
            f"{_fmt_surprise_pct(c.revenue_beat_rate_pct, 1)} | "
            f"{_fmt_surprise_pct(c.revenue_avg_surprise_pct, 2)} | "
            f"{_fmt_surprise_pct(c.revenue_latest_surprise_pct, 2)} |\n"
        )
    out.write("\n")


def _fmt_surprise_pct(v: float | None, places: int) -> str:
    """Format a percent (already in 0-100 scale) with sign + given dp.

    Distinct from `_fmt_pct(v)` above, which expects 0-1 decimal input and
    multiplies by 100. This one is for surprise/beat-rate values that come
    out of the compute layer already pre-scaled.

    `None` → '—'. Positive values get an explicit '+' sign so beat/miss
    direction reads at a glance.
    """
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{places}f}%"


def _digest_card(out: StringIO, q: QuarterlyEarningsCard) -> None:
    out.write(f"#### {q.quarter} {q.year} _(digest)_\n\n")
    if q.digest_md:
        out.write(q.digest_md.strip() + "\n\n")
    else:
        out.write("_(no digest available)_\n\n")


def _full_card(out: StringIO, q: QuarterlyEarningsCard) -> None:
    out.write(f"#### {q.quarter} {q.year}\n\n")
    if q.summary_md:
        out.write(q.summary_md.strip() + "\n\n")
    else:
        out.write("_(no LLM summary cached)_\n\n")
    if q.transcript_path:
        out.write(f"_Transcript source: `{q.transcript_path}`_\n\n")


def _saydo(out: StringIO, s: SayDoSection) -> None:
    _section_header(out, 7, "Say-Do analysis", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    for c in s.cards:
        out.write(
            f"### {c.current_quarter} {c.current_year} vs {c.prior_quarter} {c.prior_year}\n\n"
        )
        out.write(c.saydo_md.strip() + "\n\n")


def _ir_docs(out: StringIO, s: IrDocsSection) -> None:
    _section_header(out, 8, "IR documents", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    for c in s.cards:
        out.write(f"### {c.quarter} {c.year} — {c.doc_type}\n\n")
        if c.source_url:
            out.write(f"_Source: <{c.source_url}>_\n\n")
        if c.summary_md:
            out.write(c.summary_md.strip() + "\n\n")


def _recent_developments(out: StringIO, s: RecentDevelopmentsSection) -> None:
    _section_header(out, 9, "Recent developments", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    if s.cached_at is not None:
        out.write(
            f"_Window: last {s.news_days_window} days. "
            f"Cached at {s.cached_at.isoformat(timespec='seconds')}._\n\n"
        )
    if s.content_md:
        out.write(s.content_md.strip() + "\n\n")
    else:
        out.write("_No content available._\n\n")


def _bear_case(out: StringIO, s: BearCaseSection) -> None:
    _section_header(out, 10, "Bear case", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    for i, fm in enumerate(s.failure_modes, 1):
        out.write(f"### Failure mode {i}: {fm.hypothesis}\n\n")
        out.write(f"- **Evidence in data:** {fm.evidence_in_data}\n")
        out.write(f"- **Leading indicator:** {fm.leading_indicator}\n")
        out.write(f"- **Quantitative impact:** {fm.quantitative_impact}\n")
        out.write(f"- **Refutation:** {fm.refutation_criteria}\n\n")
    if s.most_underweighted:
        out.write(f"### Most underweighted by consensus\n\n{s.most_underweighted}\n\n")
    if s.out_of_scope_flags:
        out.write("### Flagged for manual review (out of scope)\n\n")
        for f in s.out_of_scope_flags:
            out.write(f"- {f}\n")
        out.write("\n")


def _provenance(out: StringIO, s: ProvenanceSection) -> None:
    _section_header(out, 11, "Provenance & data quality", s.status)
    if _missing_block(out, s.status, s.missing):
        return
    if s.coverage:
        out.write("### Coverage matrix\n\n")
        out.write("| Quarter | Audio | Transcript | Release | Slides | Say-Do | LLM summary |\n")
        out.write("|---|---|---|---|---|---|---|\n")
        for c in s.coverage:
            out.write(
                f"| {c.quarter} {c.year} | {_chk(c.has_audio_file)} | {_chk(c.has_transcript_file)} | "
                f"{_chk(c.has_release_file)} | {_chk(c.has_slides_file)} | "
                f"{_chk(c.step_saydo_analyzed)} | {_chk(c.step_llm_summarized)} |\n"
            )
        out.write("\n")
    if s.source_docs:
        out.write(f"### Source documents ({len(s.source_docs)})\n\n")
        out.write("| doc_type | period_end | file_path | sha256 |\n|---|---|---|---|\n")
        for d in s.source_docs[:50]:  # cap to keep the doc reviewable
            sha_prefix = (d.sha256 or "")[:10]
            out.write(
                f"| {d.doc_type} | {d.period_end or '—'} | `{d.file_path}` | `{sha_prefix}` |\n"
            )
        if len(s.source_docs) > 50:
            out.write(f"\n_…and {len(s.source_docs) - 50} more (see workbook Provenance tab)._\n")
        out.write("\n")
    out.write(f"_Open validation issues: **{s.open_validation_issues}**_\n\n")


def _chk(b: bool) -> str:
    return "✅" if b else "⬜"


def _appendix(out: StringIO, s: AppendixSection) -> None:
    out.write(
        f"## §12 Appendix — full earnings-call transcripts\n\n_Status: `{s.status.value}`_\n\n"
    )
    if s.status != SectionStatus.OK or not s.transcripts:
        out.write("_No transcripts available._\n\n")
        return
    for entry in s.transcripts:
        _transcript_block(out, entry)


def _transcript_block(out: StringIO, entry: TranscriptEntry) -> None:
    out.write(f"### {entry.quarter} {entry.year}\n\n")
    out.write(f"_Source: `{entry.source_path}` · {len(entry.text):,} chars_\n\n")
    out.write("```\n")
    out.write(entry.text)
    out.write("\n```\n\n")
