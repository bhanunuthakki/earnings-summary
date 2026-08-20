"""Company tab: description, filing intelligence, targets/concentration/leases.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO

from industry_classifier import (
    SECTION_CUSTOMER_CONCENTRATION,
    SECTION_LEASE_LADDER,
    SECTION_STRATEGIC_TARGETS,
)
from report.models import (
    CompanyDescriptionSection,
    EvaluationSnapshotSection,
    FilingIntelligenceSection,
    IrDocsSection,
    SectionStatus,
    SegmentWeighting,
)
from report.renderers.numfmt import fmt_reltime
from report.renderers.workspace_sections._shared import (
    _empty_panel,
    _esc,
    _missing_panel,
    _panel_head,
    _quarter_selector,
    _render_markdown,
    _xlink_html,
)
from report.renderers.workspace_sections.eval_screen import (
    _eval_screen_panels,
    _peer_comp_panel,
)
from report.sections.comp_set_context import CompSetContextSection, CompSetMetricLine
from report.sections.p3_data import (
    CustomerConcentrationRow,
    LeaseLadderRow,
    PeerCompRow,
    StrategicTargetRow,
)
from ui import living_grid as lg
from ui.controls import ticker_label

__all__ = [
    "_comp_set_context_panel",
    "_company_tab",
    "_customer_concentration_panel",
    "_lease_bucket_label",
    "_lease_ladder_panel",
    "_render_filing_intelligence",
    "_segment_breakdown_panel",
    "_strategic_targets_panel",
]


def _company_tab(
    body: StringIO,
    cd: CompanyDescriptionSection,
    ir: IrDocsSection,
    filing: FilingIntelligenceSection | None = None,
    strategic_targets: list[StrategicTargetRow] | None = None,
    customer_concentrations: list[CustomerConcentrationRow] | None = None,
    lease_ladder: list[LeaseLadderRow] | None = None,
    suppressed_sections: frozenset[str] | None = None,
    eval_snap: EvaluationSnapshotSection | None = None,
    peer_comp: list[PeerCompRow] | None = None,
    comp_set_context: CompSetContextSection | None = None,
) -> None:
    body.write('<div class="tab-body">')
    body.write('<div class="row-split"><div>')
    eyebrow_bits = ["What this company does"]
    if cd.source_fiscal_year:
        eyebrow_bits.append(f"FY{cd.source_fiscal_year} 10-K")
    if cd.cached_at is not None:
        eyebrow_bits.append(f"cached {fmt_reltime(cd.cached_at.isoformat())}")
    body.write(f'<div class="eyebrow">{_esc(" · ".join(eyebrow_bits))}</div>')
    body.write(f'<h2 class="section-title">{_esc(cd.sector or "Company description")}</h2>')
    if cd.industry:
        body.write(f'<p class="lede">{_esc(cd.industry)}</p>')
    body.write("</div></div>")

    if cd.elevator_pitch:
        body.write(f'<div class="elevator-block">{_esc(cd.elevator_pitch)}</div>')

    # Evaluation flavor (PR7): the landing tab carries the screen numbers +
    # comps right under the pitch — description first, then the data.
    if eval_snap is not None:
        _eval_screen_panels(body, eval_snap, peer_comp)
    elif peer_comp:
        # Owner decision 2026-07-02: the peer-comp panel is no longer an
        # eval-only scope gate (directives/peer_selection_llm.md). Portfolio
        # (and any non-eval) flavor has no quick-categorization table, but
        # still gets the comparable-company panel on its own —
        # `_peer_comp_panel` already hides itself when empty.
        _peer_comp_panel(body, peer_comp)

    if comp_set_context is not None:
        _comp_set_context_panel(body, comp_set_context)

    if cd.business_overview or cd.revenue_model:
        body.write('<div class="grid-2col">')
        if cd.business_overview:
            body.write(
                _panel_head(
                    "Business overview",
                    sub="analytical take",
                    attrs='data-commentable="true" data-anchor-type="company_overview" '
                    'data-anchor-key="company_overview" data-anchor-tab="company"',
                )
                + f'<div class="prose-pad">{_render_markdown(cd.business_overview)}</div></div>'
            )
        if cd.revenue_model:
            body.write(
                _panel_head("Revenue mechanics", sub="unit economics + mix")
                + f'<div class="prose-pad">{_render_markdown(cd.revenue_model)}</div></div>'
            )
        body.write("</div>")

    if cd.segment_breakdown:
        _segment_breakdown_panel(body, "Segment breakdown", cd.segment_breakdown)
    if cd.geographic_breakdown:
        _segment_breakdown_panel(body, "Geographic breakdown", cd.geographic_breakdown)

    # P3 panels (strategic targets / customer concentrations / lease ladder):
    # P4.2 hide-don't-stub — strategic targets + lease ladder hide entirely
    # when cold (the Governance coverage report carries the inventory of
    # gaps); customer concentration keeps an informative empty state because
    # "no customer ≥ 5%" is itself a disclosure fact. Panels structurally
    # irrelevant to the business model (a bank has no operating-lease ladder)
    # are suppressed via `suppressed_sections` regardless. See
    # industry_classifier.suppressed_sections_for_ticker.
    suppressed: frozenset[str] = suppressed_sections or frozenset()
    if SECTION_STRATEGIC_TARGETS not in suppressed:
        _strategic_targets_panel(body, strategic_targets or [])
    if SECTION_CUSTOMER_CONCENTRATION not in suppressed:
        _customer_concentration_panel(body, customer_concentrations or [])
    if SECTION_LEASE_LADDER not in suppressed:
        _lease_ladder_panel(body, lease_ladder or [])

    if ir.cards:
        # Quarter-toggle, mirroring the earnings tab: show one quarter at a
        # time, newest first, swapped by the generic data-quarter-group JS.
        # A quarter can carry two cards (press release + presentation brief);
        # both share one data-quarter value and surface together when that
        # quarter's button is selected, so the selector labels are de-duped.
        ordered = sorted(
            ir.cards,
            key=lambda c: (c.year, int(c.quarter[1:]) if c.quarter[1:].isdigit() else 0),
            reverse=True,
        )
        labels: list[str] = []
        for c in ordered:
            lbl = f"{c.quarter} {c.year}"
            if lbl not in labels:
                labels.append(lbl)
        active = labels[0] if labels else ""
        body.write(_panel_head("IR documents", sub=f"{len(ir.cards)} on file"))
        _quarter_selector(body, labels, group="ir")
        for c in ordered:
            qid = f"{c.quarter} {c.year}"
            hidden = "" if qid == active else " hidden"
            body.write(
                f'<div class="ir-card" data-quarter-card data-quarter-group="ir" '
                f'data-quarter="{_esc(qid)}"{hidden}><div class="ir-card-head">'
            )
            body.write(f'<span class="ir-type">{_esc(c.doc_type)}</span>')
            body.write(f'<span class="ir-quarter">{_esc(c.quarter)} {c.year}</span>')
            if c.source_url:
                body.write(
                    f'<a class="ir-link" href="{_esc(c.source_url)}" '
                    'target="_blank" rel="noopener">source ↗</a>'
                )
            body.write("</div>")
            if c.summary_md:
                body.write(f'<div class="ir-summary">{_render_markdown(c.summary_md)}</div>')
            body.write("</div>")
        body.write("</div>")

    if filing and filing.status == SectionStatus.OK:
        _render_filing_intelligence(body, filing)

    if cd.status != SectionStatus.OK and not cd.elevator_pitch:
        _missing_panel(body, cd.status, cd.missing, title="Company description")
    body.write("</div>")


def _render_filing_intelligence(body: StringIO, section: FilingIntelligenceSection) -> None:
    """§7.5 — buy-side 10-K narrative synthesis rendered in the Company tab.

    Layout: header + optional buy-side synthesis panel + 2-col (segment-shifts /
    exec-comp) grid + optional investment-signals table. Severity chips are
    explicit: High = k-chip-bad, Medium = k-chip-warn, Low = k-chip-mono (no tone).
    """
    fy_label = f"FY {section.fiscal_year}" if section.fiscal_year else "Latest filing"
    body.write('<div class="row-split company-filing-heading"><div>')
    body.write('<div class="eyebrow">10-K Narrative Intelligence</div>')
    body.write(f'<h2 class="section-title">{_esc(f"Filing review · {fy_label}")}</h2>')
    body.write("</div></div>")

    if section.raw_synthesis_md:
        body.write(
            _panel_head(
                "Buy-side narrative synthesis",
                sub="Critical operational shifts & strategic takeaways",
            )
        )
        body.write(
            f'<div class="prose-pad">{_render_markdown(section.raw_synthesis_md)}</div></div>'
        )

    seg = section.segment_changes
    comp = section.executive_comp
    if seg or comp:
        body.write('<div class="grid-2col">')

        if seg is not None:
            pill = (
                '<span class="k-chip k-chip-mono k-chip-warn">DETECTED SHIFT</span>'
                if seg.has_changes
                else '<span class="k-chip k-chip-mono k-chip-ok">NO CHANGE</span>'
            )
            body.write(_panel_head("Reporting & segment boundary changes", sub_html=pill))
            body.write('<div class="prose-pad">')
            seg_desc = seg.description or (
                "No reporting segment boundary changes or reclassifications detected in footnote disclosures."
            )
            body.write(f"<p>{_esc(seg_desc)}</p>")
            body.write("</div></div>")

        if comp is not None:
            body.write(_panel_head("Executive compensation alignment"))
            body.write('<div class="prose-pad">')
            metrics_str = ", ".join(comp.metrics_used) if comp.metrics_used else "—"
            body.write(f"<p><strong>Metrics tracked:</strong> {_esc(metrics_str)}</p>")
            body.write(
                f"<p><strong>Targets:</strong> {_esc(comp.targets_and_thresholds or '—')}</p>"
            )
            body.write(
                '<p class="comp-alignment-verdict">'
                f"<strong>Thesis alignment:</strong> {_esc(comp.alignment_verdict or '—')}</p>"
            )
            body.write("</div></div>")

        body.write("</div>")

    metric = section.metric_redefinitions
    if metric is not None and (metric.has_changes or metric.description):
        pill = (
            '<span class="k-chip k-chip-mono k-chip-warn">DEFINITION SHIFT</span>'
            if metric.has_changes
            else '<span class="k-chip k-chip-mono k-chip-ok">UNCHANGED</span>'
        )
        body.write(_panel_head("Metric redefinitions", sub_html=pill))
        body.write('<div class="prose-pad">')
        body.write(
            f"<p>{_esc(metric.description or 'No operational/financial metric redefinitions detected.')}</p>"
        )
        body.write("</div></div>")

    if section.investment_signals:
        body.write(
            _panel_head(
                "Investment signals & tail risks",
                sub="Surfaced from commitments, litigation, and tax footnotes",
            )
        )
        body.write('<table class="tbl"><thead><tr>')
        body.write("<th>Signal type</th><th>Severity</th><th>Analytical insight</th>")
        body.write("</tr></thead><tbody>")
        # k-chip-mono is the outline mono micro chip; High/Medium add a tone,
        # Low/unknown stay tone-less (the report's old pill-neutral/-muted).
        sev_tone = {"High": " k-chip-bad", "Medium": " k-chip-warn"}
        for sig in section.investment_signals:
            tone = sev_tone.get(sig.severity, "")
            body.write("<tr>")
            body.write(f'<td class="saydo-metric">{_esc(sig.signal_type)}</td>')
            body.write(
                f'<td><span class="k-chip k-chip-mono{tone}">{_esc(sig.severity.upper())}</span></td>'
            )
            body.write(f'<td class="saydo-guide">{_esc(sig.description)}</td>')
            body.write("</tr>")
        body.write("</tbody></table></div>")


def _fmt_metric_value(line: CompSetMetricLine, value: float | None) -> str:
    """Render one metric's number per its display kind (§11 card): a bare
    multiple ("15.2x") or a percentage. ``fcf_yield_ttm`` is a raw FMP
    fraction (scale by 100); ``rev_yoy`` is already percent-scaled by the
    compute layer (compute.comp_set_metrics.load_member_snapshot)."""
    if value is None:
        return '<span class="muted">—</span>'
    if line.is_pct:
        pct = value * 100.0 if line.metric == "fcf_yield_ttm" else value
        return f"{pct:+.1f}%" if line.metric == "rev_yoy" else f"{pct:.1f}%"
    return f"{value:.1f}x"


def _comp_set_metric_row(line: CompSetMetricLine) -> str:
    flag_chips = "".join(
        f'<span class="k-chip k-chip-mono k-chip-warn">{_esc(f.upper())}</span> '
        for f in line.flags
        if f in ("coverage", "ev_daily_approximated", "excluded_non_usd_n")
    )
    row_cls = ' class="muted"' if line.secondary else ""
    coverage_tone = " k-chip-warn" if line.coverage_pct_median < 0.5 else ""
    no_flags = '<span class="muted">—</span>'
    return (
        f"<tr{row_cls}>"
        f"<td>{_esc(line.label)}</td>"
        f'<td class="num">{_fmt_metric_value(line, line.subject_value)}</td>'
        f'<td class="num">{_fmt_metric_value(line, line.median_value)} '
        f'<span class="k-chip k-chip-mono{coverage_tone}">{line.n_valid_median}/{line.n_members}</span></td>'
        f'<td class="num">{_fmt_metric_value(line, line.aggregate_value)}</td>'
        f"<td>{flag_chips or no_flags}</td>"
        "</tr>"
    )


_MEMBERSHIP_LABEL: dict[str, str] = {
    "industry_seed": "industry",
    "sector_widened": "sector",
    "llm_ratified": "LLM-ratified",
    "pinned_override": "owner pin",
    "industry_slice": "industry",
    "sector_slice": "sector",
}


def _comp_set_context_panel(body: StringIO, section: CompSetContextSection) -> None:
    """Company tab "Sector context" card (docs/design/
    comparable_sets_bottoms_up.md §11, Phase 3) — the first render consumer
    of ``comp_set_metrics_daily``: subject vs comp-set median/aggregate vs
    pool-wide industry/sector benchmark, with honest coverage/staleness
    chips (never hidden per §5.5 — thin/stale is shown, not suppressed).

    ``ui.controls`` kit primitives only (k-chip/k-well/ticker_label);
    layout-only local CSS classes (``.comp-set-*``) live in
    ``workspace_styles.py``.
    """
    stale_chip = (
        '<span class="k-chip k-chip-warn">STALE</span>'
        if section.stale
        else '<span class="k-chip k-chip-ok">CURRENT</span>'
    )
    as_of_label = section.as_of_date.isoformat() if section.as_of_date else "no metrics run yet"
    sub_text = (
        f"{section.n_members} comparable{'s' if section.n_members != 1 else ''} · "
        f"{section.metric_class} · as of {as_of_label}"
    )
    body.write(
        _panel_head(
            "Sector context",
            sub_html=f'<span class="panel-sub">{_esc(sub_text)}</span> {stale_chip}',
            classes="comp-set-context-panel",
        )
    )

    if section.as_of_date is None:
        body.write(
            '<div class="prose-pad muted">Comparable set resolved but no metrics computed '
            "yet — run <code>execution/track_comp_metrics.py</code>.</div></div>"
        )
        return

    if section.metric_class == "reit":
        body.write(
            '<div class="prose-pad muted">P/FFO-proxy metric deferred (§10/§13 of the design '
            "doc) — not yet computed for REIT-class comp sets.</div>"
        )
    else:
        body.write(
            '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
            "<th>Metric</th>"
            '<th class="num">Subject</th>'
            '<th class="num">Comp-set median</th>'
            '<th class="num">Comp-set aggregate</th>'
            "<th>Flags</th>"
            "</tr></thead><tbody>"
        )
        for line in section.primary_metrics:
            body.write(_comp_set_metric_row(line))
        for line in section.secondary_metrics:
            body.write(_comp_set_metric_row(line))
        body.write("</tbody></table></div>")

    # Pool-wide industry/sector benchmark (§4.1 point 2 — bottoms-up, not
    # derived from the ETF's holdings) + the ratified ETF performance proxy.
    body.write('<div class="comp-set-benchmark-row">')
    for scope, label in ((section.industry_scope, "Industry"), (section.sector_scope, "Sector")):
        if scope is None:
            continue
        tone = " k-chip-warn" if scope.stale else ""
        pe = f"{scope.pe_ttm_median:.1f}x" if scope.pe_ttm_median is not None else "—"
        as_of_txt = scope.as_of_date.isoformat() if scope.as_of_date is not None else "—"
        body.write(
            f'<span class="k-well"><strong>{_esc(label)}</strong> {_esc(scope.scope_key)}: '
            f"P/E {pe} · {scope.n_members} names · {as_of_txt}"
            f'<span class="k-chip{tone}">{"STALE" if scope.stale else "CURRENT"}</span></span>'
        )
    if section.benchmark_etf or section.benchmark_sector_etf:
        proxy_bits = " / ".join(
            ticker_label(t, href=f"/reports/{t}")
            for t in (section.benchmark_etf, section.benchmark_sector_etf)
            if t
        )
        body.write(f'<span class="k-well k-well-accent">Benchmark proxy: {proxy_bits}</span>')
    else:
        body.write(
            f'<span class="k-chip">No ratified benchmark — {_esc(section.benchmark_note)}</span>'
        )
    body.write("</div>")

    # Roster — every row a doorway (feed-density standard): dense table,
    # ticker links to that member's own report. context_only members (§3.1
    # Step C: market-cap-only LLM-suggested peers) are visibly tagged, never
    # silently mixed into the contributing count.
    if section.members:
        body.write(
            '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
            "<th>Member</th><th>Basis</th><th>Contributes</th>"
            "</tr></thead><tbody>"
        )
        for m in section.members:
            reason = _MEMBERSHIP_LABEL.get(m.membership_reason, m.membership_reason)
            contributes = (
                '<span class="k-chip k-chip-mono">context only</span>'
                if m.context_only
                else '<span class="k-chip k-chip-mono k-chip-ok">yes</span>'
            )
            body.write(
                f"<tr><td>{ticker_label(m.ticker, m.name, href=f'/reports/{m.ticker}')}</td>"
                f"<td>{_esc(reason)}</td><td>{contributes}</td></tr>"
            )
        body.write("</tbody></table></div>")

    body.write("</div>")


def _strategic_targets_panel(body: StringIO, rows: list[StrategicTargetRow]) -> None:
    """P3-20 strategic targets table — long-term mgmt commitments from decks.

    Hidden when no targets are on file for this ticker (P4.2 hide-don't-stub
    — the Governance coverage report carries the gap).
    """
    if not rows:
        return
    body.write(
        _panel_head(
            "Strategic targets",
            sub=f"{len(rows)} long-term commitment{'s' if len(rows) != 1 else ''} "
            "· from investor decks",
            classes="strategic-targets-panel",
        )
    )
    body.write(lg.grid_open())
    body.write(lg.filter_bar(len(rows), noun="targets"))
    body.write(
        '<table class="tbl"><thead><tr>'
        + lg.th("Target", "target", "text", num=False)
        + lg.th("Value", "value", "num")
        + lg.th("Period", "period", "text", num=False)
        + lg.th("Conf.", "conf", "num")
        + "<th>Source excerpt</th>"
        + "</tr></thead><tbody>"
    )
    for r in rows:
        data = (
            lg.data_text(f"{r.target_kind} {r.target_period} {r.narrative_excerpt}")
            + lg.data_text_key("target", r.target_kind)
            + lg.data_text_key("period", r.target_period)
            + lg.data_num("value", r.target_value)
            + lg.data_num("conf", r.confidence)
        )
        body.write(f"<tr{data}>")
        body.write(f"<td><strong>{_esc(r.target_kind)}</strong></td>")
        if r.target_value is not None:
            cur = f"{r.target_currency} " if r.target_currency else ""
            body.write(
                f'<td class="num">{_esc(cur)}{r.target_value:,.1f} '
                f'<span class="muted xsmall">{_esc(r.target_unit)}</span></td>'
            )
        else:
            body.write(f'<td class="num muted">{_esc(r.target_unit)}</td>')
        body.write(f"<td>{_esc(r.target_period)}</td>")
        body.write(f'<td class="num">{r.confidence * 100:.0f}%</td>')
        body.write(f'<td class="seg-desc"><em>&ldquo;{_esc(r.narrative_excerpt)}&rdquo;</em></td>')
        body.write("</tr>")
    body.write("</tbody></table>")
    body.write(lg.grid_close())
    body.write("</div>")


def _customer_concentration_panel(body: StringIO, rows: list[CustomerConcentrationRow]) -> None:
    """P3-19a customer concentration table — named customers ≥ 5% of revenue.

    Empty-state when none reported (most large-cap diversified businesses).
    Accessor filters out sub-5% rows so this is "material concentration only".
    """
    if not rows:
        _empty_panel(
            body,
            "Customer concentration",
            "No named customer reaches 5% of revenue in disclosure — either "
            "genuinely diversified, or concentration hasn't been disclosed "
            "for this name.",
            reason="none ≥ 5% reported",
            classes="customer-concentration-panel",
        )
        return
    body.write(
        _panel_head(
            "Customer concentration",
            sub=f"{len(rows)} customer{'s' if len(rows) != 1 else ''} ≥ 5% of revenue",
            links=_xlink_html("bear", "bear case →", "panel-failure-modes"),
            classes="customer-concentration-panel",
        )
    )
    body.write(lg.grid_open())
    body.write(lg.filter_bar(len(rows), noun="customers"))
    body.write(
        '<table class="tbl"><thead><tr>'
        + lg.th("Period", "period", "text", num=False)
        + lg.th("Customer", "customer", "text", num=False)
        + lg.th("% of revenue", "pct", "num")
        + lg.th("Revenue", "revenue", "num")
        + "</tr></thead><tbody>"
    )
    for r in rows:
        period = f"{r.fiscal_period} {r.fiscal_period_type}"
        share_pct = r.pct_of_revenue * 100
        # Bar width visualizes the share so a 35% concentration is visually
        # distinct from 6%. Cap to keep cells from blowing out the column.
        bar_w = min(100.0, max(2.0, share_pct * 2.0))
        if r.revenue_amount is not None and r.revenue_currency:
            rev_cell = f"{r.revenue_currency} {r.revenue_amount:,.0f}"
        else:
            rev_cell = '<span class="muted">—</span>'
        data = (
            lg.data_text(f"{period} {r.customer_label}")
            + lg.data_text_key("period", period)
            + lg.data_text_key("customer", r.customer_label)
            + lg.data_num("pct", share_pct)
            + lg.data_num("revenue", r.revenue_amount)
        )
        body.write(f"<tr{data}>")
        body.write(f'<td class="mono xsmall">{_esc(period)}</td>')
        body.write(f"<td><strong>{_esc(r.customer_label)}</strong></td>")
        body.write(
            f'<td class="num">{share_pct:.1f}%'
            f' <span class="seg-bar" style="width:{bar_w:.1f}px"></span></td>'
        )
        body.write(f'<td class="num">{rev_cell}</td>')
        body.write("</tr>")
    body.write("</tbody></table>")
    body.write(lg.grid_close())
    body.write("</div>")


def _lease_ladder_panel(body: StringIO, rows: list[LeaseLadderRow]) -> None:
    """P3-19b lease maturity ladder — Y1..Y5..Thereafter for the latest FY.

    Accessor pre-orders rows Y1..Thereafter then total/imputed/liability.
    Hidden when no rows exist for the ticker (P4.2 hide-don't-stub — the
    Governance coverage report carries the gap).
    """
    if not rows:
        return
    fy = rows[0].fiscal_year
    unit = rows[0].unit
    curr = rows[0].currency
    body.write(
        _panel_head(
            "Operating lease maturity ladder",
            sub=f"FY{fy} · {curr} {unit}",
            as_of=rows[0].as_of_date.isoformat(),
            classes="lease-ladder-panel",
        )
    )
    body.write(
        '<table class="tbl"><thead><tr>'
        "<th>Bucket</th>"
        '<th class="num">Amount</th>'
        "<th>Calendar year</th>"
        "</tr></thead><tbody>"
    )
    # Subtotal rows (TotalPayments / ImputedInterest / LeaseLiability) get
    # the `emph` row class so they read as summary lines.
    summary_buckets = {"TotalPayments", "ImputedInterest", "LeaseLiability"}
    for r in rows:
        tr_cls = ' class="emph"' if r.ladder_year in summary_buckets else ""
        cal = (
            str(r.ladder_calendar_year)
            if r.ladder_calendar_year is not None
            else '<span class="muted">—</span>'
        )
        body.write(f"<tr{tr_cls}>")
        body.write(f"<td><strong>{_esc(_lease_bucket_label(r.ladder_year))}</strong></td>")
        body.write(f'<td class="num">{r.amount:,.0f}</td>')
        body.write(f'<td class="mono xsmall">{cal}</td>')
        body.write("</tr>")
    body.write("</tbody></table></div>")


def _lease_bucket_label(bucket: str) -> str:
    """Friendlier display labels for the ladder buckets."""
    mapping = {
        "Y1": "Year 1",
        "Y2": "Year 2",
        "Y3": "Year 3",
        "Y4": "Year 4",
        "Y5": "Year 5",
        "Thereafter": "Thereafter",
        "TotalPayments": "Total payments",
        "ImputedInterest": "Less: imputed interest",
        "LeaseLiability": "Lease liability",
    }
    return mapping.get(bucket, bucket)


def _segment_breakdown_panel(body: StringIO, title: str, rows: list[SegmentWeighting]) -> None:
    # Show OI columns only when at least 2 rows carry an OI value — a single
    # OI row alongside many "—" rows reads as missing data rather than as a
    # ticker that reports OI on only one sub-segment. Common shape: AMZN's
    # product table has OI only on the AWS row (other products aren't P&L
    # segments), so the product table stays 4-column while the geography
    # table (3 rows, all with OI) gets the 6-column view.
    oi_row_count = sum(1 for r in rows if r.operating_income_usd_m is not None)
    has_oi = oi_row_count >= 2

    body.write(
        _panel_head(title, sub="latest period") + '<table class="tbl"><thead><tr>'
        "<th>Segment</th>"
        '<th class="num">Revenue ($M)</th>'
        '<th class="num">Share</th>'
    )
    if has_oi:
        body.write('<th class="num">Op income ($M)</th><th class="num">OI share</th>')
    body.write("<th>Description</th></tr></thead><tbody>")
    for r in rows:
        body.write(f"<tr><td><strong>{_esc(r.name)}</strong></td>")
        body.write(
            f'<td class="num">{r.revenue_usd_m:.0f}</td>'
            if r.revenue_usd_m is not None
            else '<td class="num muted">—</td>'
        )
        if r.share_pct is not None:
            bar_w = max(2.0, r.share_pct * 100)
            body.write(
                f'<td class="num">{r.share_pct * 100:.1f}%'
                f' <span class="seg-bar" style="width:{bar_w:.1f}px"></span></td>'
            )
        else:
            body.write('<td class="num muted">—</td>')
        if has_oi:
            if r.operating_income_usd_m is not None:
                # OI can be negative (loss-making segment) — keep the sign
                # visible via standard comma-separated number formatting.
                body.write(f'<td class="num">{r.operating_income_usd_m:,.0f}</td>')
            else:
                body.write('<td class="num muted">—</td>')
            if r.oi_share_pct is not None:
                body.write(f'<td class="num">{r.oi_share_pct * 100:.1f}%</td>')
            else:
                body.write('<td class="num muted">—</td>')
        body.write(f'<td class="seg-desc">{_esc(r.description or "")}</td></tr>')
    body.write("</tbody></table></div>")
