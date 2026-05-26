"""ReportSpec → workspace HTML research doc.

Implements the Anthropic-design ``GOOG Workspace.html`` (tabbed editorial
workspace, monochrome palette, paper/white/dark themes) as a single self-
contained HTML file. Same contract as ``html.py`` — no runtime JS framework,
no CDN dependencies beyond the Google Fonts stylesheet.

The design's four tabs (Earnings / Say·Do / Financials / Thesis & Risk) are
extended with three more (Company / Position / Sources) so every section of
the ReportSpec has a home. Sections whose pipeline data isn't structured for
the design's slots (hero quote, Q&A roster, DCF sensitivity, peer comps) fall
through to discreet "compute pending" stubs rather than getting dropped or
faked.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import TypeAlias

from report.models import (
    AppendixSection,
    BearCaseSection,
    BreakRuleEvaluation,
    CompanyDescriptionSection,
    DecisionBadge,
    EarningsSection,
    ExecCompRowModel,
    ExecCompSectionModel,
    FilingIntelligenceSection,
    EvaluationSnapshotSection,
    FailureMode,
    FinancialsSection,
    InsiderSignalRowModel,
    IrDocsSection,
    KpiLedgerRow,
    MissingReason,
    PortfolioPositionSection,
    ValuationBasisSection,
    ProvenanceSection,
    QAEntry,
    QARosterQuarter,
    QARosterSection,
    QuarterlyEarningsCard,
    QuarterlyLineItem,
    RecentDevelopmentsSection,
    ReportFlavor,
    ReportSpec,
    SayDoCard,
    SayDoHistoricalMetric,
    SayDoSection,
    SectionStatus,
    SegmentSeries,
    SegmentsSection,
    SegmentWeighting,
    SnapshotSection,
    SoftRuleEvaluation,
    SurpriseScorecardCard,
    SynthesisLensRow,
    SynthesisSection,
    ThesisSection,
)
from report.renderers.charts_v2 import CSS as CHARTS_V2_CSS
from report.renderers.charts_v2 import MatrixRow, yoy_heatmap_table
from report.renderers.workspace_charts import sparkline, verdict_bar
from report.renderers.workspace_data import (
    KpiStripTile,
    NewsTile,
    PrintVsGuideRow,
    filter_important_print_vs_guide,
    parse_print_vs_guide,
    quarter_short,
    select_kpi_strip,
    structure_news_by_section,
)
from report.renderers.workspace_chat import CSS as CHAT_CSS
from report.renderers.workspace_chat import JS as CHAT_JS
from report.renderers.workspace_comments import CSS as COMMENTS_CSS
from report.renderers.workspace_comments import JS as COMMENTS_JS
from report.renderers.workspace_script import JS
from report.renderers.workspace_styles import CSS

# A tab tuple: (id, label, optional badge count, render-function-into-body).
TabRenderFn: TypeAlias = Callable[[StringIO], None]
TabDef: TypeAlias = tuple[str, str, int | None, TabRenderFn]

_BOLD_RX = re.compile(r"\*\*([^*]+)\*\*")
_ITAL_RX = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_INLINE_CODE_RX = re.compile(r"`([^`]+)`")

# Editorial typography: hoisted so call sites stay ruff-clean (RUF001).
# Built via chr() so the source stays ASCII-only.
_TIMES = chr(0x00D7)  # multiplication sign for WACC x g header


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def render(spec: ReportSpec) -> str:
    body = StringIO()
    # Comments + chat boot data: inlined as JSON so the JS UI can render
    # pins without a server fetch. Server is needed for posting NEW
    # comments + chat, but read-only display works file://.
    _comment_boot_data(body, spec)
    body.write('<div class="l1-root">')
    _identity(body, spec)
    _thesis_strip(body, spec.snapshot, spec.thesis)
    _kpi_strip(body, spec.thesis.kpi_ledger)

    body.write('<div class="l1-tabs-wrap">')
    tabs = _tab_defs(spec)
    _tabs(body, tabs)
    for tid, label, _count, render_fn in tabs:
        _ = label
        body.write(f'<div class="tab-pane" data-tab="{_esc(tid)}">')
        render_fn(body)
        body.write("</div>")
    body.write("</div>")  # /l1-tabs-wrap

    _footer(body, spec)
    body.write("</div>")  # /l1-root
    return _document(spec, body.getvalue())


def _document(spec: ReportSpec, body: str) -> str:
    title = f"{spec.ticker} · {spec.snapshot.company_name or 'Research'} · workspace"
    # Hardcoded dark + compact — no theme switcher, no chrome.
    return f"""<!doctype html>
<html lang="en" data-theme="dark" data-density="compact">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&display=swap" rel="stylesheet">
<style>{CSS}</style>
<style>{CHARTS_V2_CSS}</style>
<style>{COMMENTS_CSS}</style>
<style>{CHAT_CSS}</style>
</head>
<body>
{body}
<script>{JS}</script>
<script>{COMMENTS_JS}</script>
<script>{CHAT_JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Chrome / identity / thesis / KPI strip / news
# ---------------------------------------------------------------------------


def _identity(body: StringIO, spec: ReportSpec) -> None:
    snap = spec.snapshot
    val = snap.valuation
    body.write('<div class="l1-identity">')
    body.write('<div class="identity-left">')
    body.write(f'<div class="ticker-large">{_esc(spec.ticker)}</div>')
    body.write('<div class="company-row">')
    body.write(f'<span class="company-name">{_esc(snap.company_name or spec.ticker)}</span>')
    body.write(_verdict_badge(snap.verdict))
    body.write("</div>")
    body.write('<div class="company-meta">')
    body.write(f"<span>USD · Report dated {spec.generation_date.isoformat()}</span>")
    if val.valuation_date is not None:
        body.write('<span class="meta-pip">·</span>')
        body.write(f"<span>DCF dated {val.valuation_date.isoformat()}</span>")
    body.write("</div></div>")  # /identity-left

    # Valuation strip on the right.
    body.write('<div class="identity-right">')
    _val_stat(body, "Last price", _fmt_price(val.current_price))
    body.write('<div class="val-divider"></div>')
    _val_stat(body, "DCF / share", _fmt_price(val.consolidated_npv_per_share))
    body.write('<div class="val-divider"></div>')
    if val.current_price and val.consolidated_npv_per_share:
        upside = (val.consolidated_npv_per_share - val.current_price) / val.current_price * 100
        tone = "pos" if upside >= 0 else "neg"
        _val_stat(body, "Implied", f"{upside:+.0f}%", tone=tone)
    else:
        _val_stat(body, "Implied", "—")
    if val.mos_bar is not None:
        body.write('<div class="val-divider"></div>')
        _val_stat(body, "MoS bar", f"{val.mos_bar * 100:.0f}%", mono_sm=True)
    if val.trigger_status and val.trigger_status != "unknown":
        body.write('<div class="val-divider"></div>')
        _val_stat(body, "Trigger", val.trigger_status.upper(), mono_sm=True)
    body.write("</div></div>")  # /identity-right, /l1-identity


def _val_stat(
    body: StringIO,
    label: str,
    value: str,
    *,
    tone: str | None = None,
    mono_sm: bool = False,
) -> None:
    cls = "val-stat-value"
    if mono_sm:
        cls += " mono-sm"
    if tone:
        cls += f" {tone}"
    body.write('<div class="val-stat">')
    body.write(f'<div class="val-stat-label">{_esc(label)}</div>')
    body.write(f'<div class="{cls}">{_esc(value)}</div>')
    body.write("</div>")


def _verdict_badge(verdict: str) -> str:
    mapping = {
        "intact": ("Thesis Intact", "var(--ok)"),
        "watch": ("Watch", "var(--warn)"),
        "broken": ("Broken", "var(--bad)"),
        "pending": ("Pending", "var(--muted-2)"),
    }
    label, dot = mapping.get(verdict, mapping["pending"])
    return (
        f'<span class="badge"><span class="dot" style="background:{dot}"></span>'
        f"{_esc(label)}</span>"
    )


def _thesis_strip(body: StringIO, snap: SnapshotSection, thesis: ThesisSection) -> None:
    text = snap.thesis_one_liner or thesis.thesis_full
    if not text:
        return
    body.write(
        '<div class="l1-thesis" data-commentable="true" '
        'data-anchor-type="thesis_lede" data-anchor-key="thesis_lede" '
        'data-anchor-tab="thesis">'
    )
    body.write('<span class="thesis-label">Thesis</span>')
    body.write(f"<p>{_esc(text)}</p>")
    body.write("</div>")


def _kpi_strip(body: StringIO, rows: list[KpiLedgerRow]) -> None:
    tiles = select_kpi_strip(rows, n=4)
    if not tiles:
        return
    body.write('<div class="kpi-strip">')
    for t in tiles:
        _kpi_tile(body, t)
    # Pad with empty cells to keep the 4-up grid when fewer tiles available.
    for _ in range(4 - len(tiles)):
        body.write('<div class="kpi-tile" aria-hidden="true"></div>')
    body.write("</div>")


def _kpi_tile(body: StringIO, t: KpiStripTile) -> None:
    body.write('<div class="kpi-tile">')
    body.write(f'<div class="kpi-name">{_esc(t.name)}</div>')
    body.write('<div class="kpi-row">')
    body.write(f'<div class="kpi-value">{_esc(t.latest_display)}</div>')
    if t.delta_display:
        cls = f"kpi-delta {t.delta_sign}"
        body.write(f'<div class="{cls}">{_esc(t.delta_display)}</div>')
    body.write("</div>")
    body.write(f'<div class="kpi-spark">{sparkline(t.values, width=230, height=36)}</div>')
    body.write('<div class="kpi-axis">')
    body.write(f"<span>{_esc(t.labels[0][2:7])}</span>")
    body.write(f'<span class="kpi-trail">{len(t.values)}q trailing</span>')
    body.write(f"<span>{_esc(t.labels[-1][2:7])}</span>")
    body.write("</div></div>")


def _news_tab(body: StringIO, section: RecentDevelopmentsSection) -> None:
    """News tab — splits the recent-developments markdown into its source
    sections (Material events / Sector & regulatory context / Watch this
    week / …) and renders each as its own panel of tone-coded tiles.

    Falls back to a stub + raw markdown when the section is empty or the
    parser can't extract bullets in the expected shape.
    """
    body.write('<div class="tab-body">')
    body.write('<div class="row-split"><div>')
    eyebrow_bits = ["News & market context"]
    if section.cached_at is not None:
        eyebrow_bits.append(f"cached {section.cached_at.isoformat(timespec='minutes')}")
    body.write(f'<div class="eyebrow">{_esc(" · ".join(eyebrow_bits))}</div>')
    by_section = structure_news_by_section(section, limit_per_section=12)
    total = sum(len(v) for v in by_section.values())
    title = f"{total} item{'s' if total != 1 else ''}"
    if section.news_days_window:
        title += f" · last {section.news_days_window} days"
    body.write(f'<h2 class="section-title">{_esc(title)}</h2>')
    body.write("</div></div>")
    if not by_section:
        _missing_panel(body, section.status, section.missing)
        if section.content_md:
            body.write('<div class="panel"><div class="panel-head">')
            body.write('<span class="panel-title">Raw news brief</span>')
            body.write('<span class="panel-sub">unparsed markdown</span></div>')
            body.write(f'<div class="prose-pad">{_render_markdown(section.content_md)}</div>')
            body.write("</div>")
        body.write("</div>")
        return
    for sec_title, tiles in by_section.items():
        body.write(
            '<div class="panel"><div class="panel-head">'
            f'<span class="panel-title">{_esc(sec_title)}</span>'
            f'<span class="panel-sub">{len(tiles)} item{"s" if len(tiles) != 1 else ""}</span>'
            "</div>"
            '<div class="news-grid news-grid-tab">'
        )
        for t in tiles:
            _news_tile(body, t)
        body.write("</div></div>")
    body.write("</div>")


def _news_tile(body: StringIO, t: NewsTile) -> None:
    anchor_key = _esc(t.headline[:80])
    body.write(
        f'<article class="news-item tone-{_esc(t.tone)}" '
        f'data-commentable="true" data-anchor-type="news_item" '
        f'data-anchor-key="{anchor_key}" data-anchor-tab="news">'
    )
    body.write('<div class="news-meta">')
    if t.tag:
        body.write(f'<span class="news-tag">{_esc(t.tag)}</span>')
    if t.date:
        body.write(f'<span class="news-date mono">{_esc(t.date)}</span>')
    if t.source:
        body.write(f'<span class="news-src">{_esc(t.source)}</span>')
    body.write("</div>")
    headline = _esc(t.headline)
    if t.url:
        headline = f'<a href="{_esc(t.url)}" target="_blank" rel="noopener">{headline}</a>'
    body.write(f'<h4 class="news-headline">{headline}</h4>')
    if t.gloss:
        body.write(f'<p class="news-gloss">{_esc(t.gloss)}</p>')
    body.write("</article>")


# ---------------------------------------------------------------------------
# Tabs scaffold
# ---------------------------------------------------------------------------


def _tab_defs(spec: ReportSpec) -> list[TabDef]:
    """Return [(tab_id, label, count_or_None, render_fn), ...]."""
    pos = spec.portfolio_position  # narrow for the closure
    eval_snap = spec.evaluation_snapshot
    tabs: list[TabDef] = []
    if spec.flavor == ReportFlavor.EVALUATION and eval_snap is not None:
        tabs.append(
            (
                "eval",
                "Eval Screen",
                len(eval_snap.rows),
                lambda b: _eval_tab(b, eval_snap),
            )
        )
    # Tab order: portfolio/watchlist puts thesis first as the analytical
    # anchor; evaluation reports lead with Company since the reader hasn't
    # internalized the business yet.
    is_eval = spec.flavor == ReportFlavor.EVALUATION
    tab_blocks: list[TabDef] = [
        (
            "thesis",
            "Thesis",
            len(spec.thesis.kpi_ledger),
            lambda b: _thesis_tab(b, spec.snapshot, spec.thesis, spec.bear_case),
        ),
        (
            "earnings",
            "Earnings",
            len(spec.earnings.full_quarters) + len(spec.earnings.digest_quarters),
            lambda b: _earnings_tab(b, spec.earnings, spec.financials, spec.qa_roster),
        ),
        (
            "news",
            "News",
            None,
            lambda b: _news_tab(b, spec.recent_developments),
        ),
        (
            "saydo",
            "Say · Do",
            len(spec.saydo.cards),
            lambda b: _saydo_tab(b, spec.saydo, spec),
        ),
        (
            "financials",
            "Financials",
            len(spec.financials.quarter_labels),
            lambda b: _financials_tab(b, spec.financials, spec.segments),
        ),
        (
            "valuation",
            "Valuation",
            None,
            lambda b: _valuation_tab(b, spec.valuation_basis),
        ),
        (
            "bear",
            "Bear case",
            len(spec.bear_case.failure_modes),
            lambda b: _bear_tab(b, spec.bear_case),
        ),
        (
            "company",
            "Company",
            None,
            lambda b: _company_tab(b, spec.company_description, spec.ir_docs, spec.filing_intelligence),
        ),
        (
            "exec_comp",
            "Exec Comp",
            (
                len(spec.exec_compensation.insider_signals)
                + len(spec.exec_compensation.packages)
                if spec.exec_compensation is not None
                else None
            ),
            lambda b: _exec_comp_tab(b, spec.exec_compensation),
        ),
        (
            "synthesis",
            "Synthesis",
            len(spec.synthesis.lenses) if spec.synthesis is not None else None,
            lambda b: _synthesis_tab(b, spec.synthesis),
        ),
    ]
    if is_eval:
        # Evaluation reports: Company first (reader is new to the name), then
        # the rest of the order is preserved.
        company_block = [t for t in tab_blocks if t[0] == "company"]
        rest = [t for t in tab_blocks if t[0] != "company"]
        tabs.extend(company_block + rest)
    else:
        tabs.extend(tab_blocks)
    if pos is not None and pos.held:
        tabs.append(("position", "Position", len(pos.accounts), lambda b: _position_tab(b, pos)))
    tabs.append(
        (
            "sources",
            "Sources",
            len(spec.appendix.transcripts) if spec.appendix else None,
            lambda b: _sources_tab(b, spec.provenance, spec.appendix),
        )
    )
    return tabs


def _tabs(body: StringIO, tabs: list[TabDef]) -> None:
    body.write('<div class="tabs">')
    for i, (tid, label, count, _fn) in enumerate(tabs):
        cls = "tab active" if i == 0 else "tab"
        body.write(f'<button class="{cls}" data-tab="{_esc(tid)}">')
        body.write(f'<span class="tab-label">{_esc(label)}</span>')
        if count is not None:
            body.write(f'<span class="tab-count">{count}</span>')
        body.write("</button>")
    body.write('<div class="tabs-spacer"></div></div>')


# ---------------------------------------------------------------------------
# Earnings tab
# ---------------------------------------------------------------------------


def _earnings_tab(
    body: StringIO,
    section: EarningsSection,
    financials: FinancialsSection,
    qa: QARosterSection | None,
) -> None:
    cards = section.full_quarters + section.digest_quarters
    body.write('<div class="tab-body">')

    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Earnings calls</div>')
    body.write(
        '<h2 class="section-title">'
        f"{len(cards)} quarter{'s' if len(cards) != 1 else ''} on file — most recent surfaced first."
        "</h2>"
    )
    body.write("</div>")
    if cards:
        _quarter_selector(body, [c.quarter + " " + str(c.year) for c in cards], group="earnings")
    body.write("</div>")

    # Beat-rate scorecard — small panel above the quarter cards summarizing
    # how the analyst consensus has fared over the lookback window.
    if section.surprise_scorecard is not None:
        _beat_rate_scorecard_panel(body, section.surprise_scorecard)

    # Per-quarter blocks — show one at a time; the quarter selector swaps
    # which is visible. Each block carries: the FMP financial-highlights
    # table for the quarter, the LLM-summarized prepared remarks / press
    # release narrative, and the parsed Q&A roster for that same quarter.
    if not cards:
        _missing_panel(body, section.status, section.missing)
    for i, card in enumerate(cards):
        display = "" if i == 0 else "display:none"
        qid = f"{card.quarter} {card.year}"
        body.write(
            f'<div data-quarter-card data-quarter-group="earnings" '
            f'data-quarter="{_esc(qid)}" style="{display}">'
        )
        _financial_highlights_panel(body, card, _financials_for_card(card, financials))
        _earnings_narrative_panel(body, card)
        _qa_roster_panel_for_quarter(body, qa, card.quarter, card.year)
        body.write("</div>")

    body.write("</div>")


def _beat_rate_scorecard_panel(body: StringIO, scs: SurpriseScorecardCard) -> None:
    """Two-row table: EPS / Revenue × beat rate / avg surprise / latest /
    sample size. Rows whose side has no data at all are skipped entirely
    (e.g. revenue after the FMP coverage lapse) instead of showing
    misleading zeroes.
    """
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Analyst beat-rate scorecard</span>'
        f'<span class="panel-sub">last {scs.total_quarters} reported quarters</span></div>'
        '<table class="metrics-table"><thead><tr>'
        "<th>Series</th>"
        '<th class="num">Beat rate</th>'
        '<th class="num">Avg surprise</th>'
        '<th class="num">Latest surprise</th>'
        '<th class="num">Beats / Misses / N/A</th>'
        "</tr></thead><tbody>"
    )
    if scs.eps_beats + scs.eps_misses > 0:
        body.write(
            f"<tr><td>EPS</td>"
            f'<td class="num">{_fmt_pct(scs.eps_beat_rate_pct)}</td>'
            f'<td class="num">{_fmt_pct(scs.eps_avg_surprise_pct)}</td>'
            f'<td class="num{_surprise_tone(scs.eps_latest_surprise_pct)}">'
            f"{_fmt_pct(scs.eps_latest_surprise_pct)}</td>"
            f'<td class="num">{scs.eps_beats} / {scs.eps_misses} / {scs.eps_no_data}</td>'
            "</tr>"
        )
    if scs.revenue_beats + scs.revenue_misses > 0:
        body.write(
            f"<tr><td>Revenue</td>"
            f'<td class="num">{_fmt_pct(scs.revenue_beat_rate_pct)}</td>'
            f'<td class="num">{_fmt_pct(scs.revenue_avg_surprise_pct)}</td>'
            f'<td class="num{_surprise_tone(scs.revenue_latest_surprise_pct)}">'
            f"{_fmt_pct(scs.revenue_latest_surprise_pct)}</td>"
            f'<td class="num">{scs.revenue_beats} / {scs.revenue_misses}'
            f" / {scs.revenue_no_data}</td>"
            "</tr>"
        )
    body.write("</tbody></table></div>")


def _surprise_tone(v: float | None) -> str:
    if v is None:
        return ""
    if v > 0.5:
        return " pos"
    if v < -0.5:
        return " neg"
    return ""


def _earnings_narrative_panel(body: StringIO, card: QuarterlyEarningsCard) -> None:
    qid = f"{card.quarter} {card.year}"
    body.write('<div class="panel"><div class="panel-head">')
    body.write(
        f'<span class="panel-title">{_esc(qid)} — prepared remarks &amp; key takeaways</span>'
    )
    sub_parts = ["full" if card.is_recent else "digest"]
    if card.transcript_path:
        as_uri = card.transcript_path.replace("\\", "/")
        if not as_uri.startswith(("file://", "http://", "https://")):
            as_uri = "file:///" + as_uri.lstrip("/")
        sub_parts.append(
            f'<a href="{_esc(as_uri)}" target="_blank" rel="noopener" class="muted">'
            "transcript ↗</a>"
        )
    body.write(f'<span class="panel-sub">{" · ".join(sub_parts)}</span>')
    body.write("</div>")
    md = card.summary_md or card.digest_md or ""
    body.write(f'<div class="prose-pad">{_render_markdown(md)}</div>')
    body.write("</div>")


def _financial_highlights_panel(
    body: StringIO,
    card: QuarterlyEarningsCard,
    line_items: list[tuple[QuarterlyLineItem, float | None, float | None]],
) -> None:
    """Per-quarter FMP highlights table: revenue, op-inc, NI, EPS, FCF, capex.

    Each row carries the value FOR THIS quarter plus the prior-q and
    year-ago-q values for inline QoQ / YoY. Sourced from
    ``FinancialsSection.line_items`` — the same FMP-derived series the
    legacy renderer uses, but sliced per-quarter instead of always-latest.
    """
    qid = f"{card.quarter} {card.year}"
    body.write('<div class="panel"><div class="panel-head">')
    body.write(f'<span class="panel-title">{_esc(qid)} — financial highlights</span>')
    body.write('<span class="panel-sub">FMP fundamentals · QoQ / YoY</span></div>')
    if not line_items:
        body.write(
            '<div class="stub"><span class="stub-label">no fmp slice</span>'
            "FMP line items aren't aligned to this quarter; re-run the FMP "
            "extractor or check quarter labels.</div></div>"
        )
        return
    body.write(
        '<table class="metrics-table"><thead><tr>'
        "<th>Metric</th>"
        f'<th class="num">{_esc(qid)}</th>'
        '<th class="num">QoQ</th>'
        '<th class="num">YoY</th>'
        "</tr></thead><tbody>"
    )
    for li, prev, year_ago in line_items:
        if li.values and li.values[_pos_of_card(li, card)] is None:
            continue
        body.write(f"<tr><td>{_esc(li.line_item)}</td>")
        v = li.values[_pos_of_card(li, card)]
        body.write(f'<td class="num">{_fmt_line_value(li, v)}</td>')
        body.write(_growth_pair_cell(v, prev))
        body.write(_growth_pair_cell(v, year_ago))
        body.write("</tr>")
    body.write("</tbody></table></div>")


def _pos_of_card(li: QuarterlyLineItem, card: QuarterlyEarningsCard) -> int:
    """Position of the card's quarter in the line item's ``values`` list. -1 if not present."""
    label = f"{card.year} {card.quarter}"
    if label in li.quarters:
        return li.quarters.index(label)
    return -1


def _financials_for_card(
    card: QuarterlyEarningsCard, financials: FinancialsSection
) -> list[tuple[QuarterlyLineItem, float | None, float | None]]:
    """For each headline line item, return (item, prior_q_value, year_ago_q_value)
    when the card's quarter is present in the financials series; else []."""
    headline = {
        "revenue",
        "operating income",
        "net income",
        "eps (diluted)",
        "free cash flow",
        "capex",
        "gross profit",
        "operating cash flow",
    }
    out: list[tuple[QuarterlyLineItem, float | None, float | None]] = []
    for li in financials.line_items:
        if li.line_item.lower() not in headline:
            continue
        idx = _pos_of_card(li, card)
        if idx < 0:
            continue
        prev = li.values[idx - 1] if idx >= 1 else None
        year_ago = li.values[idx - 4] if idx >= 4 else None
        out.append((li, prev, year_ago))
    return out


def _fmt_line_value(li: QuarterlyLineItem, v: float | None) -> str:
    if v is None:
        return "—"
    if li.line_item.lower() == "eps (diluted)":
        return f"{v:.2f}"
    if abs(v) >= 1000:
        return f"{v / 1000:.1f}B"
    return f"{v:.0f}M"


def _growth_pair_cell(curr: float | None, base: float | None) -> str:
    if curr is None or base is None or base == 0:
        return '<td class="num muted">—</td>'
    pct = (curr / base - 1) * 100
    cls = "num pos" if pct >= 0 else "num neg"
    return f'<td class="{cls}">{pct:+.1f}%</td>'


def _qa_roster_panel_for_quarter(
    body: StringIO,
    qa: QARosterSection | None,
    quarter: str,
    year: int,
) -> None:
    """Render the analyst Q&A panel for the named quarter. Falls back to a
    stub when that quarter's transcript hasn't been parsed."""
    if qa is None:
        return
    matching = _find_qa_quarter(qa, quarter, year)
    body.write('<div class="panel"><div class="panel-head">')
    body.write('<span class="panel-title">Analyst Q&amp;A</span>')
    if matching is None:
        body.write(
            f'<span class="panel-sub">{_esc(quarter)} {year} call · not parsed</span></div>'
            '<div class="stub"><span class="stub-label">no transcript</span>'
            "No parsed transcript on file for this quarter. Older quarters often "
            "fall out of the aggregator window — fill via "
            f'<span class="mono">execution/fetch_audio_transcripts.py</span>.</div></div>'
        )
        return
    body.write(
        f'<span class="panel-sub">{_esc(matching.quarter)} {matching.year} call · '
        f"{len(matching.entries)} question{'s' if len(matching.entries) != 1 else ''}</span></div>"
    )
    body.write('<div class="qa-list">')
    for i, entry in enumerate(matching.entries):
        _qa_row(body, entry, is_first=i == 0)
    body.write("</div></div>")


def _find_qa_quarter(qa: QARosterSection, quarter: str, year: int) -> QARosterQuarter | None:
    for q in qa.quarters:
        if q.quarter == quarter and q.year == year:
            return q
    return None


def _qa_row(body: StringIO, entry: QAEntry, *, is_first: bool) -> None:
    open_cls = " open" if is_first else ""
    chev = "-" if is_first else "+"
    body.write(f'<div class="qa-row{open_cls}">')
    body.write('<button class="qa-head" type="button">')
    body.write(f'<span class="qa-chev">{chev}</span>')
    body.write(f'<span class="qa-tag">{_esc(entry.tag)}</span>')
    body.write(f'<span class="qa-topic">{_esc(entry.topic)}</span>')
    body.write(f'<span class="qa-analysts">{_esc(entry.analysts)}</span>')
    body.write("</button>")
    body.write('<div class="qa-body">')
    body.write(
        f'<div class="qa-q"><span class="qa-label">Q</span>'
        f"<span>{_esc(entry.question)}</span></div>"
    )
    if entry.answers:
        for speaker, text in entry.answers:
            body.write(
                f'<div class="qa-a"><span class="qa-label">A</span>'
                f"<span><strong>{_esc(speaker)}:</strong> {_esc(text)}</span></div>"
            )
    else:
        body.write(
            '<div class="qa-a"><span class="qa-label">A</span>'
            "<span><em>No response captured.</em></span></div>"
        )
    if entry.follow_up:
        body.write(
            '<div class="qa-followup">'
            '<span class="qa-followup-label">Follow-up</span>'
            f"<span>{_esc(entry.follow_up)}</span></div>"
        )
    body.write("</div></div>")


def _quarter_selector(body: StringIO, labels: list[str], group: str) -> None:
    if not labels:
        return
    body.write(f'<div class="quarter-select" data-quarter-group="{_esc(group)}">')
    body.write('<div class="quarter-select-label">Quarter</div>')
    body.write('<div class="quarter-select-btns">')
    for i, lbl in enumerate(labels):
        cls = "qbtn active" if i == 0 else "qbtn"
        body.write(
            f'<button class="{cls}" data-quarter="{_esc(lbl)}">{_esc(quarter_short(lbl))}</button>'
        )
    body.write("</div></div>")


# ---------------------------------------------------------------------------
# Say · Do tab
# ---------------------------------------------------------------------------


def _saydo_tab(body: StringIO, section: SayDoSection, spec: ReportSpec) -> None:
    cards = section.cards
    body.write('<div class="tab-body">')
    if not cards:
        _missing_panel(body, section.status, section.missing)
        body.write("</div>")
        return
    card = cards[0]
    ratings = [c.rating for c in cards][::-1]
    body.write('<div class="row-split">')
    body.write("<div>")
    body.write('<div class="eyebrow">Say · Do · Track</div>')
    body.write(
        '<h2 class="section-title">'
        f"{_esc(card.prior_quarter)} {card.prior_year} → "
        f"{_esc(card.current_quarter)} {card.current_year}"
        "</h2>"
    )
    if card.thesis_view:
        body.write(f'<p class="lede">{_esc(card.thesis_view)}</p>')
    body.write("</div>")
    body.write('<div class="saydo-meta">')
    body.write(_rating_pill(card.rating))
    body.write('<div class="saydo-meta-label">vs prior-quarter guide</div>')
    body.write('<div class="saydo-history">')
    body.write(verdict_bar(ratings))
    body.write(
        f'<div class="kpi-axis"><span>{len(ratings)} quarters tracked</span>'
        "<span>most recent →</span></div>"
    )
    body.write("</div></div>")
    body.write("</div>")

    # Cross-card summary table — every quarter pair on one row so the
    # trajectory is visible at a glance. Mirrors the legacy renderer's
    # §7 summary table; only render when there are ≥2 cards.
    if len(cards) >= 2:
        _saydo_summary_table(body, cards)

    if section.historical_metrics:
        _saydo_historical_ledger(body, section.historical_metrics)

    pvg = parse_print_vs_guide(card)
    if pvg:
        # LLM-filter to drop trivial commitments (FX, tax rate, share count
        # noise) when --enable-llm; otherwise show the parsed rows verbatim
        # (truncated). Both paths cache to disk so re-runs are free.
        filtered = filter_important_print_vs_guide(
            ticker=spec.ticker,
            repo_root=Path(spec.repo_root),
            card=card,
            rows=pvg,
            enable_llm=spec.llm_enabled,
        )
        _saydo_print_vs_guide(
            body, card, filtered, total_parsed=len(pvg), llm_filtered=spec.llm_enabled
        )
    else:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Print vs guide</span>'
            '<span class="panel-sub">extractor pending — see narrative below</span>'
            "</div>"
            '<div class="stub"><span class="stub-label">compute pending</span>'
            "Auto-extraction of the print-vs-guide table from the saydo narrative is "
            "not yet wired for this card's markdown shape. Full narrative renders "
            "below.</div></div>"
        )

    body.write('<div class="grid-2col">')
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Thesis view</span>'
        '<span class="panel-sub">Post-print read-through</span></div>'
    )
    body.write(f'<div class="prose-pad">{_render_markdown(card.thesis_view or "—")}</div></div>')
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Attribution</span>'
        '<span class="panel-sub">Execution · exogenous · luck</span></div>'
    )
    body.write(f'<div class="prose-pad">{_render_markdown(card.attribution or "—")}</div></div>')
    body.write("</div>")

    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Full Say·Do narrative</span>'
        f'<span class="panel-sub">{_esc(card.current_quarter)} {card.current_year}</span></div>'
    )
    body.write(f'<div class="prose-pad">{_render_markdown(card.saydo_md)}</div></div>')
    body.write("</div>")


def _saydo_summary_table(body: StringIO, cards: list[SayDoCard]) -> None:
    """All SayDo cards on one row each: pair → rating → attribution → thesis view.

    The detail panels below show the latest card in full; this table is the
    historical trajectory. Cards are newest-first per the section builder.
    """
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">SayDo summary — all tracked pairs</span>'
        f'<span class="panel-sub">{len(cards)} pairs · newest first</span></div>'
        '<table class="saydo-table"><thead><tr>'
        "<th>Pair</th><th>Rating</th><th>Attribution</th><th>Thesis view</th>"
        "</tr></thead><tbody>"
    )
    for c in cards:
        body.write(
            "<tr>"
            f'<td class="saydo-metric mono">{_esc(c.prior_quarter)} {c.prior_year} '
            f"→ {_esc(c.current_quarter)} {c.current_year}</td>"
            f"<td>{_rating_pill(c.rating)}</td>"
            f'<td class="saydo-guide">{_esc((c.attribution or "—")[:140])}</td>'
            f'<td class="saydo-guide">{_esc((c.thesis_view or "—")[:140])}</td>'
            "</tr>"
        )
    body.write("</tbody></table></div>")


def _saydo_print_vs_guide(
    body: StringIO,
    card: SayDoCard,
    rows: list[PrintVsGuideRow],
    *,
    total_parsed: int,
    llm_filtered: bool,
) -> None:
    if llm_filtered and total_parsed > len(rows):
        sub = f"{len(rows)} of {total_parsed} commitments · LLM-judged for thesis relevance"
    else:
        sub = f"{len(rows)} commitments scored"
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Print vs guide</span>'
        f'<span class="panel-sub">{_esc(sub)}</span></div>'
    )
    body.write('<table class="saydo-table"><thead><tr>')
    body.write("<th>Metric</th>")
    body.write(f"<th>{_esc(card.prior_quarter)} {card.prior_year} actual</th>")
    body.write(f"<th>{_esc(card.current_quarter)} {card.current_year} actual</th>")
    body.write("<th>Verdict</th></tr></thead><tbody>")
    for r in rows:
        body.write("<tr>")
        body.write(f'<td class="saydo-metric">{_esc(r.metric)}</td>')
        body.write(f'<td class="saydo-guide">{_esc(r.guide)}</td>')
        body.write(f'<td class="saydo-actual"><strong>{_esc(r.actual)}</strong></td>')
        body.write(f"<td>{_rating_pill(r.verdict)}</td>")
        body.write("</tr>")
    body.write("</tbody></table></div>")


def _rating_pill(rating: str) -> str:
    mapping = {
        "EXCEEDED": "ok",
        "MET": "neutral",
        "MIXED": "warn",
        "MISSED": "bad",
        "REVISED UP": "warn",
        "unknown": "muted",
    }
    tone = mapping.get(rating, "muted")
    return f'<span class="pill pill-{tone}">{_esc(rating)}</span>'


def _outcome_pill(outcome: str) -> str:
    mapping = {
        "beat": ("ok", "BEAT"),
        "hit": ("ok", "HIT"),
        "miss": ("bad", "MISS"),
        "no_data": ("muted", "NO DATA"),
    }
    tone, label = mapping.get(outcome.lower(), ("muted", outcome.upper()))
    return f'<span class="pill pill-{tone}">{_esc(label)}</span>'


def _saydo_historical_ledger(body: StringIO, metrics: list[SayDoHistoricalMetric]) -> None:
    """Persistent guidance-outcomes ledger sourced from saydo_historical_metrics."""
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Persistent guidance outcomes ledger</span>'
        f'<span class="panel-sub">{len(metrics)} tracked commitments · stored in database</span></div>'
    )
    body.write('<table class="saydo-table"><thead><tr>')
    body.write("<th>Metric</th>")
    body.write("<th>Comparator</th>")
    body.write("<th>Guidance Target</th>")
    body.write("<th>Realized Value</th>")
    body.write("<th>Outcome</th>")
    body.write("<th>Guidance Period</th>")
    body.write("<th>Target Period</th>")
    body.write("</tr></thead><tbody>")

    comp_map = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<", "eq": "≈"}

    def _fmt_period(dt: datetime) -> str:
        quarter = (dt.month - 1) // 3 + 1
        return f"Q{quarter} '{str(dt.year)[2:]}"

    for m in metrics:
        comp_symbol = comp_map.get(m.comparator.lower(), m.comparator)
        target_display = f"{m.target_value:.2f}%"
        realized_display = f"{m.realized_value:.2f}%" if m.realized_value is not None else "—"
        outcome_label = m.outcome if m.outcome else "no_data"
        body.write("<tr>")
        body.write(f'<td class="saydo-metric">{_esc(m.kpi_name)}</td>')
        body.write(f'<td class="mono">{_esc(comp_symbol)}</td>')
        body.write(f'<td class="saydo-guide">{_esc(target_display)}</td>')
        body.write(f'<td class="saydo-actual"><strong>{_esc(realized_display)}</strong></td>')
        body.write(f"<td>{_outcome_pill(outcome_label)}</td>")
        body.write(f'<td class="saydo-guide">{_esc(_fmt_period(m.period_made))}</td>')
        body.write(f'<td class="saydo-actual">{_esc(_fmt_period(m.period_target))}</td>')
        body.write("</tr>")
    body.write("</tbody></table></div>")


# ---------------------------------------------------------------------------
# Financials tab
# ---------------------------------------------------------------------------


def _financials_tab(body: StringIO, fin: FinancialsSection, seg: SegmentsSection) -> None:
    """Financials tab — YoY% heatmap (line items first, then per-segment) with
    a validation badge showing whether segment revenue sums tie to the
    consolidated revenue line (catches dropped segments and unit mismatches).
    The per-line-item 12-quarter level table is also rendered with a
    click-to-expand drill-down showing the underlying segment breakdown for
    revenue rows."""
    body.write('<div class="tab-body">')
    body.write(
        f'<div class="eyebrow">Financials · {len(fin.quarter_labels)} quarters · USD millions</div>'
    )

    if fin.status != SectionStatus.OK and not fin.line_items:
        _missing_panel(body, fin.status, fin.missing)
        body.write("</div>")
        return

    # 1) Validation: segments tie to total revenue?
    _validation_panel(body, fin, seg)

    # 2) YoY heatmap — line items.
    _line_items_yoy_panel(body, fin)

    # 3) YoY heatmap — segments (filtered to non-pseudo, real reporting units).
    _segments_yoy_panel(body, seg)

    # 3b) Same heatmap shape for the other two segment buckets (geography
    # mix, segment operating income) — separate panels because the row sets
    # often differ from the revenue-by-product set.
    _segments_yoy_panel_for_metric(
        body,
        title="YoY% — revenue by geography",
        rows=seg.revenue_by_geography,
        quarter_labels_full=seg.quarter_labels_full,
        quarter_labels=seg.quarter_labels,
        segment_definitions=seg.segment_definitions,
    )
    _segments_yoy_panel_for_metric(
        body,
        title="YoY% — segment operating income",
        rows=seg.operating_income,
        quarter_labels_full=seg.quarter_labels_full,
        quarter_labels=seg.quarter_labels,
        segment_definitions=seg.segment_definitions,
    )

    # 3c) KPI time-series matrices — analyst-tracked KPIs that aren't on
    # the line-items axis (ARPAC, GMV growth, NIM, etc.).
    _kpi_series_yoy_panel(body, fin)

    # 3d) Junction-driven secondary-dim expansions (geography / channel /
    # customer cross-tabs of segment data). Only renders when the section
    # builder found junction rows; otherwise the panel is silently skipped.
    _segment_secondary_expansions_panel(body, seg)

    # 4) 12-quarter levels with segment drill-down.
    _line_items_levels_panel(body, fin, seg)

    body.write("</div>")


def _segments_yoy_panel_for_metric(
    body: StringIO,
    *,
    title: str,
    rows: list[SegmentSeries],
    quarter_labels_full: list[str],
    quarter_labels: list[str],
    segment_definitions: dict[str, str],
) -> None:
    """Generic YoY heatmap for any segment bucket (geography, OI, ...).

    Filters pseudo-rows (Google Inc / Total / Consolidated) and rows with
    no positive data, then runs charts_v2.yoy_heatmap_table with the same
    12-quarter window the revenue-by-product panel uses. Skipped when the
    bucket is empty for this ticker.
    """
    filtered: list[SegmentSeries] = []
    for s in rows:
        name_l = s.segment_name.lower()
        if name_l.startswith("google inc") or name_l == "total" or "consolidat" in name_l:
            continue
        if not any(v for v in s.values if v is not None and v > 0):
            continue
        filtered.append(s)
    if not filtered:
        return
    filtered.sort(key=lambda s: -(s.values[-1] or 0))
    periods = quarter_labels_full or quarter_labels
    matrix_rows: list[MatrixRow] = []
    for s in filtered:
        levels = s.levels_full or s.values
        if not levels or all(v is None for v in levels):
            continue
        defn = segment_definitions.get(s.segment_name) if segment_definitions else None
        matrix_rows.append(
            MatrixRow(name=s.segment_name, levels=list(levels), unit=s.unit, tooltip=defn or "")
        )
    if not matrix_rows:
        return
    body.write('<div class="panel"><div class="panel-head">')
    body.write(f'<span class="panel-title">{_esc(title)}</span>')
    body.write(f'<span class="panel-sub">{len(matrix_rows)} rows · heat shading</span></div>')
    body.write('<div class="prose-pad">')
    body.write(yoy_heatmap_table(matrix_rows, list(periods), title="", display_quarters=12))
    body.write("</div></div>")


def _segment_secondary_expansions_panel(body: StringIO, seg: SegmentsSection) -> None:
    """Render junction-derived secondary-dim breakdowns as expandable panels.

    One panel per expansion (one per dim_type). Each panel shows a YoY heatmap
    over the same 12-quarter axis the rest of the tab uses. Silently skipped
    when the section builder didn't surface any expansions for this ticker.
    """
    expansions = getattr(seg, "secondary_expansions", None) or []
    if not expansions:
        return
    periods = seg.quarter_labels_full or seg.quarter_labels
    if not periods:
        return
    for exp in expansions:
        if not exp.rows:
            continue
        axis_label = exp.dim_type.replace("_", " ").title()
        matrix_rows: list[MatrixRow] = []
        for s in exp.rows:
            levels = s.levels_full or s.values
            if not levels or all(v is None for v in levels):
                continue
            matrix_rows.append(
                MatrixRow(name=s.segment_name, levels=list(levels), unit=s.unit, tooltip="")
            )
        if not matrix_rows:
            continue
        parent = f" — under {exp.parent_label}" if exp.parent_label else ""
        body.write('<div class="panel"><div class="panel-head">')
        body.write(
            f'<span class="panel-title">By {_esc(axis_label)}{_esc(parent)} '
            f"· cross-tab</span>"
        )
        body.write(
            f'<span class="panel-sub">{len(matrix_rows)} rows · junction data</span></div>'
        )
        body.write('<div class="prose-pad">')
        body.write(yoy_heatmap_table(matrix_rows, list(periods), title="", display_quarters=12))
        body.write("</div></div>")


def _kpi_series_yoy_panel(body: StringIO, fin: FinancialsSection) -> None:
    """YoY heatmap for the analyst-tracked KPIs surfaced as kpi_chart_series.

    These are KPIs the financials section flagged as worth charting that
    aren't already in the line_items axis (e.g. NDR, GMV, ARPAC). Same
    matrix shape so the visual treatment is consistent.
    """
    series = fin.kpi_chart_series
    if not series:
        return
    periods = fin.quarter_labels_full or fin.quarter_labels
    matrix_rows: list[MatrixRow] = []
    for s in series:
        levels = s.levels_full or s.values
        if not levels or all(v is None for v in levels):
            continue
        matrix_rows.append(MatrixRow(name=s.name, levels=list(levels), unit=s.unit))
    if not matrix_rows:
        return
    body.write('<div class="panel"><div class="panel-head">')
    body.write('<span class="panel-title">YoY% — tracked KPIs</span>')
    body.write(f'<span class="panel-sub">{len(matrix_rows)} analyst-tracked series</span></div>')
    body.write('<div class="prose-pad">')
    body.write(yoy_heatmap_table(matrix_rows, list(periods), title="", display_quarters=12))
    body.write("</div></div>")


def _segments_real(seg: SegmentsSection) -> list[SegmentSeries]:
    """Filter out the consolidated 'Google Inc' pseudo-rows that FMP sometimes
    emits alongside the real reporting units. Keep only series with at least
    one positive value, sorted by latest-period revenue descending."""
    filtered: list[SegmentSeries] = []
    for s in seg.revenue_by_product:
        name_l = s.segment_name.lower()
        if name_l.startswith("google inc") or name_l == "total" or "consolidat" in name_l:
            continue
        if not any(v for v in s.values if v is not None and v > 0):
            continue
        filtered.append(s)
    # Latest-period revenue, treating None as 0 for sort stability.
    filtered.sort(key=lambda s: -(s.values[-1] or 0))
    return filtered


def _validation_panel(body: StringIO, fin: FinancialsSection, seg: SegmentsSection) -> None:
    """Compare consolidated revenue vs sum-of-real-segments per quarter.
    Emit a single status row: OK / off-by-X — the user wanted a visible tie-out."""
    revenue = next((li for li in fin.line_items if li.line_item.lower() == "revenue"), None)
    if revenue is None or not seg.revenue_by_product:
        return
    real = _segments_real(seg)
    if not real:
        return
    # Align segment series to financials quarter axis. Common case: both share
    # the same labels in the same order. Bail out cleanly when they don't.
    if seg.quarter_labels != fin.quarter_labels:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Validation: segments ↔ revenue</span>'
            '<span class="panel-sub">quarter labels misaligned</span></div>'
            '<div class="stub"><span class="stub-label">cannot tie</span>'
            "Segment quarter labels don't match financials quarter labels — "
            "segment series may be on a different fiscal calendar.</div></div>"
        )
        return
    diffs: list[tuple[str, float | None]] = []
    for i, lbl in enumerate(fin.quarter_labels):
        total = revenue.values[i]
        if total is None:
            diffs.append((lbl, None))
            continue
        seg_sum = _sum_segments_at(real, i)
        if seg_sum == 0:
            diffs.append((lbl, None))
            continue
        diffs.append((lbl, (seg_sum - total) / total * 100))
    # Worst absolute deviation across quarters where we have a tie point.
    worst = max(
        (abs(d) for _, d in diffs if d is not None),
        default=None,
    )
    if worst is None:
        return
    if worst <= 0.5:
        status_cls = "pill-ok"
        status_text = f"ties to FMP (max drift {worst:.2f}%)"
    elif worst <= 2.0:
        status_cls = "pill-warn"
        status_text = f"minor drift (max {worst:.2f}%)"
    else:
        status_cls = "pill-bad"
        status_text = f"DRIFT — segments off by up to {worst:.1f}%"
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Validation: segments ↔ consolidated revenue</span>'
        f'<span class="pill {status_cls}">{_esc(status_text)}</span></div>'
    )
    # Compact per-quarter detail table — only show drift > 0.1% to keep it scannable.
    body.write('<div class="table-scroll"><table class="fin-table"><thead><tr>')
    body.write('<th>Quarter</th><th class="num">Revenue</th><th class="num">Σ segments</th>')
    body.write('<th class="num">Drift</th></tr></thead><tbody>')
    for i, lbl in enumerate(fin.quarter_labels):
        d = diffs[i][1]
        if d is None or abs(d) < 0.1:
            continue
        total = revenue.values[i]
        seg_sum = _sum_segments_at(real, i)
        cls = "num pos" if abs(d) < 1 else ("num neg" if d < 0 else "num warn")
        body.write(f"<tr><td>{_esc(quarter_short(lbl))}</td>")
        body.write(f'<td class="num">{(total or 0) / 1000:.2f}B</td>')
        body.write(f'<td class="num">{seg_sum / 1000:.2f}B</td>')
        body.write(f'<td class="{cls}">{d:+.2f}%</td></tr>')
    body.write("</tbody></table></div></div>")


def _line_items_yoy_panel(body: StringIO, fin: FinancialsSection) -> None:
    """YoY% matrix for the headline line items, using charts_v2.yoy_heatmap_table."""
    if not fin.line_items:
        return
    # Prefer levels_full when available (16-q with YoY lookback baseline);
    # otherwise fall back to the 12-q values list.
    periods = fin.quarter_labels_full or fin.quarter_labels
    rows: list[MatrixRow] = []
    for li in fin.line_items:
        levels = li.levels_full or li.values
        if not levels or all(v is None for v in levels):
            continue
        rows.append(MatrixRow(name=li.line_item, levels=list(levels), unit=li.unit))
    if not rows:
        return
    body.write('<div class="panel"><div class="panel-head">')
    body.write('<span class="panel-title">YoY% — line items</span>')
    body.write('<span class="panel-sub">12 quarters · heat shading</span></div>')
    body.write('<div class="prose-pad">')
    body.write(yoy_heatmap_table(rows, list(periods), title="", display_quarters=12))
    body.write("</div></div>")


def _segments_yoy_panel(body: StringIO, seg: SegmentsSection) -> None:
    real = _segments_real(seg)
    if not real:
        return
    periods = seg.quarter_labels_full or seg.quarter_labels
    rows: list[MatrixRow] = []
    for s in real:
        levels = s.levels_full or s.values
        if not levels or all(v is None for v in levels):
            continue
        defn = seg.segment_definitions.get(s.segment_name) if seg.segment_definitions else None
        rows.append(
            MatrixRow(
                name=s.segment_name,
                levels=list(levels),
                unit=s.unit,
                tooltip=defn or "",
            )
        )
    if not rows:
        return
    body.write('<div class="panel"><div class="panel-head">')
    body.write('<span class="panel-title">YoY% — revenue by segment</span>')
    sub_bits = [f"{len(rows)} segments · heat shading"]
    if seg.segment_definitions_fiscal_year:
        sub_bits.append(f"definitions from FY{seg.segment_definitions_fiscal_year} (hover 📖)")
    body.write(f'<span class="panel-sub">{_esc(" · ".join(sub_bits))}</span></div>')
    body.write('<div class="prose-pad">')
    body.write(yoy_heatmap_table(rows, list(periods), title="", display_quarters=12))
    body.write("</div></div>")


def _line_items_levels_panel(body: StringIO, fin: FinancialsSection, seg: SegmentsSection) -> None:
    """12-quarter levels table. Revenue row carries a click-to-expand
    drill-down showing the per-segment breakdown for the same quarters."""
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Line items · last 12 quarters</span>'
        '<span class="panel-sub">USD millions · QoQ · YoY · 3-yr CAGR · click ▶ to drill</span></div>'
        '<div class="table-scroll"><table class="fin-table"><thead><tr>'
        "<th>Line item</th>"
    )
    last_labels = fin.quarter_labels[-12:]
    for lbl in last_labels:
        body.write(f'<th class="num">{_esc(quarter_short(lbl))}</th>')
    body.write('<th class="num">QoQ</th><th class="num">YoY</th><th class="num">3y CAGR</th>')
    body.write("</tr></thead><tbody>")
    real_segments = _segments_real(seg)
    for li in fin.line_items:
        drillable = (
            li.line_item.lower() == "revenue"
            and bool(real_segments)
            and seg.quarter_labels == fin.quarter_labels
        )
        row_id = f"fin-row-{_esc(li.line_item.lower().replace(' ', '-'))}"
        chev = chr(0x25B6) if drillable else ""
        cls = "fin-row drillable" if drillable else "fin-row"
        body.write(
            f'<tr class="{cls}" data-drill-target="{row_id}">'
            f'<td><span class="fin-chev">{chev}</span> {_esc(li.line_item)}</td>'
        )
        for v in li.values[-12:]:
            if v is None:
                body.write('<td class="num muted">—</td>')
            elif li.unit == "USD":
                body.write(f'<td class="num">{v:.2f}</td>')
            elif v < 0:
                body.write(f'<td class="num neg">({abs(v) / 1000:.1f})</td>')
            else:
                body.write(f'<td class="num">{v / 1000:.1f}</td>')
        g = li.growth
        body.write(_growth_cell(g.qoq))
        body.write(_growth_cell(g.yoy))
        body.write(_growth_cell(g.cagr_3y_ttm, muted=True))
        body.write("</tr>")
        if drillable:
            n_cols = 1 + len(last_labels) + 3
            body.write(
                f'<tr class="fin-drill" id="{row_id}" style="display:none">'
                f'<td colspan="{n_cols}" class="fin-drill-cell">'
            )
            _segment_drill_table(body, real_segments, fin.quarter_labels)
            body.write("</td></tr>")
    body.write("</tbody></table></div></div>")


def _segment_drill_table(
    body: StringIO, real_segments: list[SegmentSeries], quarter_labels: list[str]
) -> None:
    last_labels = quarter_labels[-12:]
    body.write('<table class="fin-table fin-drill-table"><thead><tr>')
    body.write("<th>Segment</th>")
    for lbl in last_labels:
        body.write(f'<th class="num">{_esc(quarter_short(lbl))}</th>')
    body.write("</tr></thead><tbody>")
    for s in real_segments:
        body.write(f"<tr><td>{_esc(s.segment_name)}</td>")
        for v in s.values[-12:]:
            if v is None:
                body.write('<td class="num muted">—</td>')
            else:
                body.write(f'<td class="num">{v / 1000:.1f}</td>')
        body.write("</tr>")
    # Sum row.
    body.write('<tr class="fin-sum-row"><td><strong>Σ segments</strong></td>')
    for i in range(len(quarter_labels) - 12, len(quarter_labels)):
        if i < 0:
            body.write('<td class="num muted">—</td>')
            continue
        total = _sum_segments_at(real_segments, i)
        body.write(f'<td class="num"><strong>{total / 1000:.1f}</strong></td>')
    body.write("</tr></tbody></table>")


def _sum_segments_at(segments: list[SegmentSeries], idx: int) -> float:
    """Sum the per-segment value at the given quarter index, treating None
    as 0. Centralized so pyright sees a real ``float`` return type instead
    of choking on ``sum(generator[float|None])``."""
    out = 0.0
    for s in segments:
        if idx < 0 or idx >= len(s.values):
            continue
        v = s.values[idx]
        if v is not None:
            out += v
    return out


def _growth_cell(v: float | None, *, muted: bool = False) -> str:
    if v is None:
        return '<td class="num muted">—</td>'
    cls = "num muted" if muted else f"num {'pos' if v > 0 else 'neg'}"
    return f'<td class="{cls}">{v * 100:+.1f}%</td>'


# ---------------------------------------------------------------------------
# Valuation tab
# ---------------------------------------------------------------------------


def _valuation_tab(body: StringIO, vb: ValuationBasisSection | None) -> None:
    """Render the valuation tab. Headline: Opus-picked multiple + current value.
    Detail: 12Q trend sparkline + min/median/max band + rich/cheap verdict +
    Opus rationale + optional target band notes.

    Pure consumer of `ValuationBasisSection` — no analytical logic here, no
    multiple computation, no LLM calls. All of that lives in
    `src/compute/valuation_basis.py` and `src/report/sections/valuation.py`.
    """
    body.write('<div class="tab-body">')
    body.write('<div class="eyebrow">Valuation · Opus-picked multiple · 12Q context</div>')
    if vb is None or vb.status != SectionStatus.OK:
        if vb is None:
            body.write(
                '<div class="stub"><span class="stub-label">no data</span>'
                "Valuation basis section not in ReportSpec.</div>"
            )
        else:
            _missing_panel(body, vb.status, vb.missing)
        body.write("</div>")
        return

    # Headline panel: chosen multiple, current value, rich/cheap.
    body.write('<div class="panel valuation-headline">')
    body.write('<div class="panel-head">')
    body.write(
        f'<span class="panel-title">{_esc(vb.multiple_name or "—")}</span>'
    )
    if vb.current_period_end:
        body.write(
            f'<span class="panel-sub">as of {vb.current_period_end.isoformat()}</span>'
        )
    body.write("</div>")
    body.write('<div class="valuation-headline-row">')
    body.write(
        '<div class="valuation-current">'
        f'<div class="valuation-current-value">{_esc(vb.current_value_display or "—")}</div>'
        '<div class="valuation-current-label">current</div>'
        "</div>"
    )
    if vb.historical_median is not None:
        body.write(
            '<div class="valuation-band">'
            f'<div class="valuation-band-row">Range '
            f'<span class="mono">{vb.historical_min:.1f}x</span> – '
            f'<span class="mono">{vb.historical_max:.1f}x</span></div>'
            f'<div class="valuation-band-row">Median '
            f'<span class="mono">{vb.historical_median:.1f}x</span></div>'
            "</div>"
        )
    if vb.rich_cheap_verdict:
        body.write(
            f'<div class="valuation-verdict">{_esc(vb.rich_cheap_verdict)}</div>'
        )
    body.write("</div>")

    # Sparkline of 12Q history. Drop None values — sparkline doesn't
    # handle them (NTM history has gaps for periods where the
    # forward-4Q realized series isn't fully on file).
    hist_values = [h.value for h in vb.history if h.value is not None]
    if hist_values:
        body.write(
            f'<div class="valuation-spark">{sparkline(hist_values, width=560, height=60)}</div>'
        )
        if vb.history:
            body.write(
                '<div class="valuation-spark-axis">'
                f'<span>{vb.history[0].period_end.isoformat() if vb.history[0].period_end else "—"}</span>'
                f'<span class="muted">{len(vb.history)}q trailing</span>'
                f'<span>{vb.history[-1].period_end.isoformat() if vb.history[-1].period_end else "—"}</span>'
                "</div>"
            )
    body.write("</div>")  # /headline panel

    # Rationale panel.
    if vb.rationale:
        body.write(
            '<div class="panel" data-commentable="true" '
            'data-anchor-type="valuation_rationale" data-anchor-key="valuation_rationale" '
            'data-anchor-tab="valuation"><div class="panel-head">'
            '<span class="panel-title">Why this multiple</span>'
            '<span class="panel-sub">Opus rationale</span></div>'
            f'<div class="prose-pad">{_render_markdown(vb.rationale)}</div>'
            "</div>"
        )

    # Target band / notes.
    if vb.notes:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Target read</span>'
            '<span class="panel-sub">where this should trade</span></div>'
            f'<div class="prose-pad">{_render_markdown(vb.notes)}</div>'
            "</div>"
        )

    body.write("</div>")


# ---------------------------------------------------------------------------
# Thesis & Risk tab
# ---------------------------------------------------------------------------


def _thesis_tab(
    body: StringIO,
    snap: SnapshotSection,
    thesis: ThesisSection,
    bear: BearCaseSection,
) -> None:
    body.write('<div class="tab-body">')
    eyebrow_bits = ["Thesis", "Valuation", "Break conditions"]
    if thesis.last_updated is not None:
        eyebrow_bits.append(f"updated {thesis.last_updated.isoformat()}")
    body.write(f'<div class="eyebrow">{_esc(" · ".join(eyebrow_bits))}</div>')
    if thesis.stub_warning:
        body.write(
            f'<p class="lede stub-warning"><strong>Stub:</strong> {_esc(thesis.stub_warning)}</p>'
        )
    if thesis.thesis_full:
        body.write(f'<p class="lede">{_esc(thesis.thesis_full)}</p>')

    body.write('<div class="grid-thesis-top">')
    _valuation_summary_panel(body, snap)
    _break_rules_panel(body, thesis)
    body.write("</div>")

    # Decision history sidebar — only when the audit ledger has rows for this
    # ticker. Reads `snap.recent_decisions` (last 3 LLM recommendations).
    _decision_history_panel(body, snap.recent_decisions)

    # Thesis hygiene: break conditions (narrative thresholds), qualitative
    # breakers (soft thesis-breakers), competitive watchlist (who to track),
    # full tier-2/3 KPI ledger (collapsible). Each panel skips itself when
    # the underlying list is empty so the tab doesn't stack stub panels.
    _thesis_hygiene_panels(body, thesis)
    body.write("</div>")
    # NOTE: bear case (`bear` arg) is rendered separately by `_bear_tab` —
    # it's promoted to a first-class tab. Arg kept here for back-compat
    # with anything that still passes the section through.
    _ = bear


def _bear_tab(body: StringIO, bear: BearCaseSection) -> None:
    """Dedicated bear case tab — most_underweighted callout + failure modes
    + out_of_scope flags. Promoted from inside the Thesis tab so the
    structural short case is a first-class surface, matching the legacy
    §10 in the markdown renderer."""
    body.write('<div class="tab-body">')
    body.write(
        '<div class="eyebrow">Bear case · structural short thesis · adversarial review</div>'
    )
    if bear.status != SectionStatus.OK:
        _missing_panel(body, bear.status, bear.missing)
        body.write("</div>")
        return
    _most_underweighted_panel(body, bear)
    _failure_modes_panel(body, bear)
    if not bear.most_underweighted and not bear.failure_modes:
        body.write(
            '<div class="stub"><span class="stub-label">empty</span>'
            "Bear case is empty. Re-run with <code>--enable-llm</code> to "
            "populate failure modes and the most-underweighted callout. "
            "Subsequent per-quarter summaries and news will then cross-"
            "reference these named risks.</div>"
        )
    body.write("</div>")


def _thesis_hygiene_panels(body: StringIO, thesis: ThesisSection) -> None:
    """Render break_conditions / qualitative_breakers / competitive_watchlist
    side-by-side, then the full KPI ledger as a collapsible details panel."""
    cards: list[tuple[str, str, list[str]]] = []
    if thesis.break_conditions:
        cards.append(
            (
                "Break conditions",
                "thesis-breaker thresholds",
                list(thesis.break_conditions),
            )
        )
    if thesis.qualitative_breakers:
        cards.append(
            (
                "Qualitative breakers",
                "non-quantitative thesis risks",
                list(thesis.qualitative_breakers),
            )
        )
    if thesis.competitive_watchlist:
        cards.append(
            (
                "Competitive watchlist",
                "rivals to monitor",
                list(thesis.competitive_watchlist),
            )
        )
    if cards:
        body.write('<div class="grid-thesis-hygiene">')
        for title, sub, items in cards:
            body.write(
                '<div class="panel"><div class="panel-head">'
                f'<span class="panel-title">{_esc(title)}</span>'
                f'<span class="panel-sub">{_esc(sub)}</span></div>'
                '<ul class="thesis-list">'
            )
            for item in items:
                body.write(f"<li>{_esc(item)}</li>")
            body.write("</ul></div>")
        body.write("</div>")

    # Full KPI ledger — every tier, every row. Tier-1 has the source/break
    # metadata that the top sparkline strip can't carry; tier-2/3 don't make
    # the strip at all. Show open by default (high signal, low cost).
    if thesis.kpi_ledger:
        body.write(
            '<details class="thesis-ledger-details" open><summary>'
            f"KPI ledger detail ({len(thesis.kpi_ledger)} rows)</summary>"
            '<div class="table-scroll"><table class="fin-table"><thead><tr>'
            "<th>KPI</th><th>Tier</th><th>Unit</th>"
            '<th class="num">Latest</th>'
            "<th>Status</th><th>Break condition</th><th>Source</th>"
            "</tr></thead><tbody>"
        )
        # Sort: tier-1 first, then tier-2, then tier-3 — preserves analyst
        # priority in the visible list.
        tier_rank = {"tier_1": 0, "tier_2": 1, "tier_3": 2}
        rows_sorted = sorted(thesis.kpi_ledger, key=lambda r: (tier_rank.get(r.tier, 9), r.name))
        for r in rows_sorted:
            status_cls = {
                "green": "pos",
                "yellow": "warn",
                "red": "neg",
                "unknown": "muted",
            }.get(r.current_status, "muted")
            # Latest value cell. Shows `value (period)` when history exists,
            # `—` otherwise. When latest_source_excerpt is set, attach it
            # as a `title` tooltip so hovering reveals the verbatim quote /
            # analyst statement that produced the value.
            if r.history:
                period_label, value = r.history[-1]
                latest_text = f"{value:.2f}".rstrip("0").rstrip(".") if value is not None else "—"
                latest_html = (
                    f'{_esc(latest_text)} <span class="muted xsmall">{_esc(period_label[:7])}</span>'
                )
            else:
                latest_html = '<span class="muted">—</span>'
            tooltip_attr = (
                f' title="{_esc(r.latest_source_excerpt)}"' if r.latest_source_excerpt else ""
            )
            body.write(
                f'<tr data-commentable="true" data-anchor-type="kpi_ledger_row" '
                f'data-anchor-key="{_esc(r.name)}" data-anchor-tab="thesis">'
                f"<td>{_esc(r.name)}</td>"
                f"<td>{_esc(r.tier.replace('_', ' '))}</td>"
                f"<td>{_esc(r.unit or '')}</td>"
                f'<td class="num"{tooltip_attr}>{latest_html}</td>'
                f'<td class="num {status_cls}">{_esc(r.current_status.upper())}</td>'
                f"<td>{_esc(r.break_condition or '')}</td>"
                f"<td>{_esc(r.source_hint or '')}</td></tr>"
            )
        body.write("</tbody></table></div></details>")


def _most_underweighted_panel(body: StringIO, bear: BearCaseSection) -> None:
    """Render `bear.most_underweighted` as a callout paragraph above the
    failure-mode list, plus any out_of_scope_flags as a follow-up list.
    Skipped when both empty (LLM didn't fill them)."""
    if not bear.most_underweighted and not bear.out_of_scope_flags:
        return
    body.write(
        '<div class="panel underweighted-panel"><div class="panel-head">'
        '<span class="panel-title">Most underweighted by consensus</span>'
        '<span class="panel-sub">analyst judgment</span></div>'
    )
    if bear.most_underweighted:
        body.write(f'<div class="prose-pad">{_render_markdown(bear.most_underweighted)}</div>')
    if bear.out_of_scope_flags:
        body.write(
            '<div class="prose-pad" style="border-top:1px solid var(--hairline)">'
            "<strong>Out-of-scope flags</strong> "
            '<span class="muted xsmall">— risks real but not derivable from these '
            "inputs (regulatory, macro, etc.)</span>"
            "<ul class=\"thesis-list\" style='padding-left:18px;margin-top:8px'>"
        )
        for flag in bear.out_of_scope_flags:
            body.write(f"<li>{_esc(flag)}</li>")
        body.write("</ul></div>")
    body.write("</div>")


def _valuation_summary_panel(body: StringIO, snap: SnapshotSection) -> None:
    v = snap.valuation
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Valuation summary</span>'
        '<span class="panel-sub">DCF</span></div>'
        '<div class="val-stack">'
    )
    _val_row(body, "Consolidated NPV / share", _fmt_price(v.consolidated_npv_per_share))
    if v.sum_of_segments_npv_per_share is not None:
        _val_row(body, "Sum-of-segments NPV", _fmt_price(v.sum_of_segments_npv_per_share))
    _val_row(body, "Current price", _fmt_price(v.current_price), emph=True)
    if v.current_price and v.consolidated_npv_per_share:
        implied = (v.consolidated_npv_per_share - v.current_price) / v.current_price * 100
        _val_row(body, "Implied vs consolidated", f"{implied:+.0f}%")
    if v.mos_bar is not None:
        _val_row(body, "Margin-of-safety bar", f"{v.mos_bar * 100:.0f}%")
    if v.trigger_status and v.trigger_status != "unknown":
        _val_row(body, "Trigger status", v.trigger_status.upper())
    if v.valuation_date is not None:
        _val_row(body, "Valuation date", v.valuation_date.isoformat(), muted=True)
    if v.model_link:
        # The legacy renderer links directly to the .xlsx workbook so analysts
        # can open the source-of-truth DCF model in one click. The link is
        # relative to the report file (both live in output/research/<TICKER>/).
        body.write(
            '<div class="val-row muted"><span>DCF workbook</span>'
            f'<strong><a href="{_esc(v.model_link)}">{_esc(v.model_link)}</a>'
            "</strong></div>"
        )
    body.write("</div></div>")


def _val_row(
    body: StringIO,
    label: str,
    value: str,
    *,
    emph: bool = False,
    muted: bool = False,
) -> None:
    cls = "val-row"
    if emph:
        cls += " emph"
    if muted:
        cls += " muted"
    body.write(f'<div class="{cls}"><span>{_esc(label)}</span><strong>{_esc(value)}</strong></div>')


def _break_rules_panel(body: StringIO, thesis: ThesisSection) -> None:
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Universal break rules</span>'
    )
    sub_bits = [str(thesis.overall_breach_status)]
    if thesis.last_evaluated_at is not None:
        sub_bits.append(f"last eval {thesis.last_evaluated_at.isoformat(timespec='minutes')}")
    body.write(f'<span class="panel-sub">{_esc(" · ".join(sub_bits))}</span></div>')
    rules = thesis.break_rule_evaluations
    if not rules:
        body.write(
            '<div class="stub"><span class="stub-label">no rules evaluated</span>'
            "Break-rule evaluator hasn't run for this ticker yet.</div></div>"
        )
        return
    body.write('<table class="break-table"><thead><tr>')
    body.write('<th>Rule</th><th class="num">Latest</th><th class="num">Threshold</th>')
    body.write('<th class="num">Status</th></tr></thead><tbody>')
    for r in rules:
        body.write('<tr class="break-row">')
        body.write(
            f"<td><strong>{_esc(_summarize_rule(r))}</strong>"
            + (f'<div class="muted xsmall">{_esc(r.narrative)}</div>' if r.narrative else "")
            + "</td>"
        )
        latest = r.observations[-1] if r.observations else None
        latest_text = f"{latest.value:.1f}" if latest else "—"
        body.write(f'<td class="num">{_esc(latest_text)}</td>')
        body.write(f'<td class="num muted">{_esc(r.comparator)} {r.threshold:.1f}</td>')
        status_cls = f"break-status-{r.status}"
        status_label = {"ok": "OK", "warn": "WARN", "breach": "BREACH"}.get(r.status, r.status)
        body.write(f'<td class="num {status_cls}">{_esc(status_label)}</td>')
        body.write("</tr>")
        # Detail row — observations sparkline + narrative detail. Renders
        # underneath the main row, shaded, so the table stays compact.
        if r.detail or r.observations:
            obs_pieces: list[str] = []
            if r.observations:
                tail = r.observations[-6:]
                obs_pieces.append("  ".join(f"{o.period_end[:7]}: {o.value:.1f}" for o in tail))
            if r.detail:
                obs_pieces.append(r.detail)
            body.write(
                '<tr class="break-detail"><td colspan="4">'
                f'<span class="xsmall mono muted">{_esc(" · ".join(obs_pieces))}</span>'
                "</td></tr>"
            )
    body.write("</tbody></table></div>")
    if thesis.soft_rule_evaluations:
        _soft_rules_panel(body, thesis.soft_rule_evaluations)


def _soft_rules_panel(body: StringIO, soft_evals: list[SoftRuleEvaluation]) -> None:
    """Render predicate-style soft signals as a sibling panel to break rules.

    Lives next to (not inside) the break-rules panel so the YELLOW-only
    semantics stay visually distinct: hard rules can go RED, soft rules can't.
    """
    fired = sum(1 for ev in soft_evals if ev.status == "yellow")
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Soft signals</span>'
        f'<span class="panel-sub">{fired} of {len(soft_evals)} fired</span></div>'
    )
    body.write('<table class="break-table"><thead><tr>')
    body.write('<th>Rule</th><th>Evidence</th><th class="num">Status</th></tr></thead><tbody>')
    for ev in soft_evals:
        body.write('<tr class="break-row">')
        body.write(f"<td><strong>{_esc(ev.rule_name)}</strong></td>")
        body.write(f'<td><span class="xsmall muted">{_esc(ev.evidence)}</span></td>')
        status_cls = "break-status-warn" if ev.status == "yellow" else "break-status-ok"
        label = "YELLOW" if ev.status == "yellow" else "OK"
        body.write(f'<td class="num {status_cls}">{label}</td>')
        body.write("</tr>")
    body.write("</tbody></table></div>")


def _summarize_rule(r: BreakRuleEvaluation) -> str:
    return f"{r.kpi_name} ({r.consecutive_periods}q)"


def _decision_history_panel(body: StringIO, decisions: list[DecisionBadge]) -> None:
    """Last 3 LLM recommendations from the decisions audit ledger.

    Skips itself entirely when the list is empty so the thesis tab doesn't
    show a "no decisions yet" placeholder. Each badge surfaces date · kind
    · outcome chip, with the rationale excerpt as a hover tooltip.
    """
    if not decisions:
        return
    body.write(
        '<div class="panel decision-history-panel"><div class="panel-head">'
        '<span class="panel-title">Recent decisions</span>'
        f'<span class="panel-sub">last {len(decisions)} LLM recommendation'
        f'{"s" if len(decisions) != 1 else ""}</span></div>'
        '<div class="decision-list">'
    )
    for d in decisions:
        cls = f"decision-badge outcome-{_esc(d.outcome_label)}"
        tooltip = f' title="{_esc(d.rationale_short)}"' if d.rationale_short else ""
        body.write(
            f'<div class="{cls}"{tooltip}>'
            f'<span class="decision-date mono">{_esc(d.date_short)}</span>'
            f'<span class="decision-kind">{_esc(d.recommendation_kind.upper())}</span>'
            f'<span class="decision-outcome">{_esc(d.outcome_label)}</span>'
            "</div>"
        )
    body.write("</div></div>")


def _failure_modes_panel(body: StringIO, bear: BearCaseSection) -> None:
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Failure modes</span>'
        f'<span class="panel-sub">{len(bear.failure_modes)} hypotheses tracked</span></div>'
    )
    if not bear.failure_modes:
        body.write(
            '<div class="stub"><span class="stub-label">none tracked</span>'
            "Run the bear-case LLM phase (--enable-llm) to populate.</div></div>"
        )
        return
    for i, fm in enumerate(bear.failure_modes):
        _failure_mode_card(body, i, fm)
    body.write("</div>")


def _failure_mode_card(body: StringIO, idx: int, fm: FailureMode) -> None:
    anchor_key = _esc(fm.hypothesis[:80])
    body.write(
        f'<div class="failure" data-commentable="true" data-anchor-type="failure_mode" '
        f'data-anchor-key="{anchor_key}" data-anchor-tab="bear">'
        f'<div class="failure-num">{idx + 1:02d}</div>'
        '<div class="failure-body">'
        f'<div class="failure-title">{_esc(fm.hypothesis)}</div>'
        '<div class="failure-meta">'
        '<span class="failure-label">Evidence</span>'
        f"<span>{_esc(fm.evidence_in_data)}</span>"
        '<span class="failure-label">Leading</span>'
        f"<span>{_esc(fm.leading_indicator)}</span>"
        '<span class="failure-label">Quant impact</span>'
        f"<span>{_esc(fm.quantitative_impact)}</span>"
        '<span class="failure-label">Refutation</span>'
        f"<span>{_esc(fm.refutation_criteria)}</span>"
        "</div></div></div>"
    )


# ---------------------------------------------------------------------------
# Eval Screen tab (evaluation flavor only)
# ---------------------------------------------------------------------------


def _eval_tab(body: StringIO, eval_snap: EvaluationSnapshotSection) -> None:
    """Render the 3y quick-categorization data table for new-name screening.

    Only added to the tab list when ``spec.flavor == ReportFlavor.EVALUATION``
    and the evaluation snapshot is populated. Mirrors what the legacy renderer
    surfaces in §1 for the evaluation flavor — abs / margin / ratio metrics
    across LFY-2 / LFY-1 / LFY / TTM with a 3y CAGR column for absolute series.
    """
    body.write('<div class="tab-body">')
    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Eval Screen · 3-year quick categorization</div>')
    title = eval_snap.company_name or "Evaluation snapshot"
    body.write(f'<h2 class="section-title">{_esc(title)}</h2>')
    sub_bits: list[str] = []
    if eval_snap.sector:
        sub_bits.append(eval_snap.sector)
    if eval_snap.market_cap is not None:
        sub_bits.append(f"Market cap {_fmt_usd_compact(eval_snap.market_cap)}")
    if eval_snap.current_price is not None:
        sub_bits.append(f"Price {_fmt_usd(eval_snap.current_price)}")
    if sub_bits:
        body.write(f'<p class="lede">{_esc(" · ".join(sub_bits))}</p>')
    body.write("</div></div>")

    if eval_snap.status != SectionStatus.OK and not eval_snap.rows:
        _missing_panel(body, eval_snap.status, eval_snap.missing)
        body.write("</div>")
        return

    fy = eval_snap.fiscal_years
    lfy_minus_2_lbl = f"FY{fy[0]}" if len(fy) >= 1 else "LFY-2"
    lfy_minus_1_lbl = f"FY{fy[1]}" if len(fy) >= 2 else "LFY-1"
    lfy_lbl = f"FY{fy[2]}" if len(fy) >= 3 else "LFY"

    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Quick categorization</span>'
        f'<span class="panel-sub">{len(eval_snap.rows)} metrics · '
        f"{lfy_minus_2_lbl} -> TTM</span></div>"
        '<div class="table-scroll"><table class="fin-table"><thead><tr>'
        "<th>Metric</th>"
        f'<th class="num">{lfy_minus_2_lbl}</th>'
        f'<th class="num">{lfy_minus_1_lbl}</th>'
        f'<th class="num">{lfy_lbl}</th>'
        '<th class="num">TTM</th>'
        '<th class="num">3y CAGR</th>'
        "</tr></thead><tbody>"
    )
    for r in eval_snap.rows:
        body.write(f"<tr><td>{_esc(r.metric)}</td>")
        for v in (r.lfy_minus_2, r.lfy_minus_1, r.lfy, r.ttm):
            body.write(_eval_cell(v, r.unit, r.digits))
        if r.cagr_3y is not None:
            cls = "num pos" if r.cagr_3y >= 0 else "num neg"
            body.write(f'<td class="{cls}">{r.cagr_3y * 100:+.1f}%</td>')
        else:
            body.write('<td class="num muted">—</td>')
        body.write("</tr>")
    body.write("</tbody></table></div></div>")
    body.write("</div>")


def _eval_cell(v: float | None, unit: str, digits: int) -> str:
    if v is None:
        return '<td class="num muted">—</td>'
    if unit == "%":
        return f'<td class="num">{v:.{digits}f}%</td>'
    if unit.startswith("USD M") and abs(v) >= 1000:
        return f'<td class="num">{v / 1000:.1f}B</td>'
    return f'<td class="num">{v:,.{digits}f}</td>'


def _fmt_usd_compact(v: float) -> str:
    """Compact USD: $1.2T, $850B, $45M. Used in the eval header."""
    abs_v = abs(v)
    if abs_v >= 1e12:
        return f"${v / 1e12:.1f}T"
    if abs_v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if abs_v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


# ---------------------------------------------------------------------------
# Company tab
# ---------------------------------------------------------------------------


def _company_tab(
    body: StringIO,
    cd: CompanyDescriptionSection,
    ir: IrDocsSection,
    filing: FilingIntelligenceSection | None = None,
) -> None:
    body.write('<div class="tab-body">')
    body.write('<div class="row-split"><div>')
    eyebrow_bits = ["What this company does"]
    if cd.source_fiscal_year:
        eyebrow_bits.append(f"FY{cd.source_fiscal_year} 10-K")
    if cd.cached_at is not None:
        eyebrow_bits.append(f"cached {cd.cached_at.isoformat(timespec='minutes')}")
    body.write(f'<div class="eyebrow">{_esc(" · ".join(eyebrow_bits))}</div>')
    body.write(f'<h2 class="section-title">{_esc(cd.sector or "Company description")}</h2>')
    if cd.industry:
        body.write(f'<p class="lede">{_esc(cd.industry)}</p>')
    body.write("</div></div>")

    if cd.elevator_pitch:
        body.write(f'<div class="elevator-block">{_esc(cd.elevator_pitch)}</div>')

    if cd.business_overview or cd.revenue_model:
        body.write('<div class="grid-2col">')
        if cd.business_overview:
            body.write(
                '<div class="panel" data-commentable="true" '
                'data-anchor-type="company_overview" data-anchor-key="company_overview" '
                'data-anchor-tab="company"><div class="panel-head">'
                '<span class="panel-title">Business overview</span>'
                '<span class="panel-sub">analytical take</span></div>'
                f'<div class="prose-pad">{_render_markdown(cd.business_overview)}</div></div>'
            )
        if cd.revenue_model:
            body.write(
                '<div class="panel"><div class="panel-head">'
                '<span class="panel-title">Revenue mechanics</span>'
                '<span class="panel-sub">unit economics + mix</span></div>'
                f'<div class="prose-pad">{_render_markdown(cd.revenue_model)}</div></div>'
            )
        body.write("</div>")

    if cd.segment_breakdown:
        _segment_breakdown_panel(body, "Segment breakdown", cd.segment_breakdown)
    if cd.geographic_breakdown:
        _segment_breakdown_panel(body, "Geographic breakdown", cd.geographic_breakdown)

    if ir.cards:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">IR documents</span>'
            f'<span class="panel-sub">{len(ir.cards)} on file</span></div>'
        )
        for c in ir.cards:
            body.write('<div class="ir-card"><div class="ir-card-head">')
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
        _missing_panel(body, cd.status, cd.missing)
    body.write("</div>")


def _render_filing_intelligence(body: StringIO, section: FilingIntelligenceSection) -> None:
    """§7.5 — buy-side 10-K narrative synthesis rendered in the Company tab.

    Layout: header + optional buy-side synthesis panel + 2-col (segment-shifts /
    exec-comp) grid + optional investment-signals table. Severity pills are
    explicit: High = pill-bad, Medium = pill-warn, Low = pill-neutral.
    """
    fy_label = f"FY {section.fiscal_year}" if section.fiscal_year else "Latest filing"
    body.write('<div class="row-split" style="margin-top: 30px;"><div>')
    body.write('<div class="eyebrow">10-K Narrative Intelligence</div>')
    body.write(f'<h2 class="section-title">{_esc(f"Filing review · {fy_label}")}</h2>')
    body.write("</div></div>")

    if section.raw_synthesis_md:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Buy-side narrative synthesis</span>'
            '<span class="panel-sub">Critical operational shifts &amp; strategic takeaways</span></div>'
        )
        body.write(f'<div class="prose-pad">{_render_markdown(section.raw_synthesis_md)}</div></div>')

    seg = section.segment_changes
    comp = section.executive_comp
    if seg or comp:
        body.write('<div class="grid-2col">')

        if seg is not None:
            body.write(
                '<div class="panel"><div class="panel-head">'
                '<span class="panel-title">Reporting &amp; segment boundary changes</span>'
            )
            if seg.has_changes:
                body.write('<span class="pill pill-warn">DETECTED SHIFT</span>')
            else:
                body.write('<span class="pill pill-ok">NO CHANGE</span>')
            body.write('</div><div class="prose-pad">')
            seg_desc = seg.description or (
                "No reporting segment boundary changes or reclassifications detected in footnote disclosures."
            )
            body.write(f"<p>{_esc(seg_desc)}</p>")
            body.write("</div></div>")

        if comp is not None:
            body.write(
                '<div class="panel"><div class="panel-head">'
                '<span class="panel-title">Executive compensation alignment</span>'
                '</div><div class="prose-pad">'
            )
            metrics_str = ", ".join(comp.metrics_used) if comp.metrics_used else "—"
            body.write(f"<p><strong>Metrics tracked:</strong> {_esc(metrics_str)}</p>")
            body.write(
                f'<p><strong>Targets:</strong> {_esc(comp.targets_and_thresholds or "—")}</p>'
            )
            body.write(
                '<p style="margin-top: 10px; font-style: italic;">'
                f'<strong>Thesis alignment:</strong> {_esc(comp.alignment_verdict or "—")}</p>'
            )
            body.write("</div></div>")

        body.write("</div>")

    metric = section.metric_redefinitions
    if metric is not None and (metric.has_changes or metric.description):
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Metric redefinitions</span>'
        )
        if metric.has_changes:
            body.write('<span class="pill pill-warn">DEFINITION SHIFT</span>')
        else:
            body.write('<span class="pill pill-ok">UNCHANGED</span>')
        body.write('</div><div class="prose-pad">')
        body.write(f'<p>{_esc(metric.description or "No operational/financial metric redefinitions detected.")}</p>')
        body.write("</div></div>")

    if section.investment_signals:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Investment signals &amp; tail risks</span>'
            '<span class="panel-sub">Surfaced from commitments, litigation, and tax footnotes</span></div>'
        )
        body.write('<table class="saydo-table"><thead><tr>')
        body.write("<th>Signal type</th><th>Severity</th><th>Analytical insight</th>")
        body.write("</tr></thead><tbody>")
        sev_class = {"High": "bad", "Medium": "warn", "Low": "neutral"}
        for sig in section.investment_signals:
            tone = sev_class.get(sig.severity, "muted")
            body.write("<tr>")
            body.write(f'<td class="saydo-metric">{_esc(sig.signal_type)}</td>')
            body.write(f'<td><span class="pill pill-{tone}">{_esc(sig.severity.upper())}</span></td>')
            body.write(f'<td class="saydo-guide">{_esc(sig.description)}</td>')
            body.write("</tr>")
        body.write("</tbody></table></div>")


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
        f'<div class="panel"><div class="panel-head">'
        f'<span class="panel-title">{_esc(title)}</span>'
        f'<span class="panel-sub">latest period</span></div>'
        '<table class="seg-list"><thead><tr>'
        "<th>Segment</th>"
        '<th class="num">Revenue ($M)</th>'
        '<th class="num">Share</th>'
    )
    if has_oi:
        body.write(
            '<th class="num">Op income ($M)</th>'
            '<th class="num">OI share</th>'
        )
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


# ---------------------------------------------------------------------------
# Executive Compensation tab — Phase 5
# ---------------------------------------------------------------------------


def _exec_comp_tab(body: StringIO, section: ExecCompSectionModel | None) -> None:
    """§13 — NEO compensation packages + recent insider activity + alignment.

    Four panels in order:
      1. Alignment narrative (LLM-driven, may be absent on --no-llm runs)
      2. Anomaly flags ribbon
      3. NEO compensation table (latest year)
      4. Recent insider transactions ranked by conviction signal
    """
    body.write('<div class="tab-body">')
    if section is None or section.status == SectionStatus.MISSING_DATA:
        _missing_panel(
            body,
            section.status if section else SectionStatus.MISSING_DATA,
            section.missing if section else None,
        )
        body.write("</div>")
        return

    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Compensation &amp; Alignment</div>')
    yr_label = f"FY {section.fiscal_year_latest}" if section.fiscal_year_latest else "Latest data"
    body.write(f'<h2 class="section-title">Executive comp &amp; insider activity · {_esc(yr_label)}</h2>')
    body.write("</div></div>")

    # 1. Alignment narrative
    if section.alignment_narrative_md:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Alignment read</span>'
            '<span class="panel-sub">Do management\'s comp metrics reward the analyst\'s thesis?</span>'
            "</div>"
        )
        body.write(f'<div class="prose-pad">{_render_markdown(section.alignment_narrative_md)}</div></div>')
    else:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Alignment read</span></div>'
            '<div class="stub"><span class="stub-label">LLM pending</span>'
            "Rerun with <code>--enable-llm</code> to generate the alignment commentary.</div></div>"
        )

    # 2. Anomaly flags
    if section.anomaly_flags:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Flags</span></div>'
            '<ul class="flag-list">'
        )
        for f in section.anomaly_flags:
            tone = "flag-positive" if "POSITIVE" in f else "flag-warn"
            body.write(f'<li class="{tone}">{_esc(f)}</li>')
        body.write("</ul></div>")

    # 3. NEO comp table
    if section.packages:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Named-Executive-Officer compensation</span>'
            f'<span class="panel-sub">{len(section.packages)} executives · {section.packages[0].currency}</span>'
            "</div>"
            '<table class="comp-table"><thead><tr>'
            "<th>Executive</th><th>Role</th>"
            '<th class="num">Salary</th>'
            '<th class="num">Bonus (actual/target)</th>'
            '<th class="num">Equity grant</th>'
            '<th class="num">Total granted</th>'
            '<th class="num">Realized vs granted</th>'
            "<th>Performance metrics (weight%)</th>"
            "<th>KPI match</th>"
            "</tr></thead><tbody>"
        )
        for pkg in section.packages:
            ceo_pill = ' <span class="ceo-pill">CEO</span>' if pkg.is_ceo else ""
            body.write("<tr>")
            body.write(f"<td>{_esc(pkg.executive_name)}{ceo_pill}</td>")
            body.write(f"<td>{_esc(pkg.role or '?')}</td>")
            body.write(f'<td class="num">{_fmt_usd_short(pkg.base_salary)}</td>')
            bonus_pair = _fmt_usd_short(pkg.cash_bonus_actual)
            if pkg.cash_bonus_target is not None and pkg.cash_bonus_target != pkg.cash_bonus_actual:
                bonus_pair += f' <span class="muted">/ {_fmt_usd_short(pkg.cash_bonus_target)} tgt</span>'
            body.write(f'<td class="num">{bonus_pair}</td>')
            body.write(f'<td class="num">{_fmt_usd_short(pkg.equity_grant_value)}</td>')
            body.write(f'<td class="num">{_fmt_usd_short(pkg.total_comp_granted)}</td>')
            rvg = pkg.realized_vs_granted_pct
            if rvg is not None:
                tone = "pos" if rvg > 0 else "neg" if rvg < 0 else ""
                body.write(f'<td class="num {tone}">{rvg * 100:+.0f}%</td>')
            else:
                body.write('<td class="num muted">-</td>')
            body.write(f"<td>{_esc(pkg.performance_metrics_summary or '(none disclosed)')}</td>")
            match = "✓ matches thesis" if pkg.metrics_have_thesis_kpi else "—"
            cls = "kpi-match" if pkg.metrics_have_thesis_kpi else "muted"
            body.write(f'<td class="{cls}">{_esc(match)}</td>')
            body.write("</tr>")
        # CEO pay ratio footer if available
        ceo = next((p for p in section.packages if p.is_ceo), None)
        if ceo and ceo.ceo_pay_ratio:
            body.write(
                f'<tr><td colspan="9" class="table-footer">CEO pay ratio: '
                f'<strong>{ceo.ceo_pay_ratio:.0f}x</strong> median employee comp '
                f'(S&amp;P 500 average ~300x).</td></tr>'
            )
        body.write("</tbody></table></div>")

    # 4. Insider transactions
    if section.insider_signals:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Recent insider activity</span>'
            f'<span class="panel-sub">Top {len(section.insider_signals)} by conviction signal · last 12 months</span>'
            "</div>"
            '<table class="insider-table"><thead><tr>'
            "<th>Date</th><th>Insider</th><th>Role</th>"
            "<th>Action</th>"
            '<th class="num">Shares</th>'
            '<th class="num">Value</th>'
            '<th class="num">Signal</th>'
            "<th>Why</th>"
            "</tr></thead><tbody>"
        )
        for s in section.insider_signals:
            tone = (
                "tx-buy"
                if "buy" in s.transaction_type
                else "tx-sell"
                if "sell" in s.transaction_type
                else ""
            )
            strength_pct = f"{int(s.signal_strength * 100)}"
            strength_tone = (
                "signal-strong"
                if s.signal_strength >= 0.6
                else "signal-medium"
                if s.signal_strength >= 0.3
                else "signal-weak"
            )
            body.write(f'<tr class="{tone}">')
            body.write(f'<td>{_esc(s.transaction_date)}</td>')
            body.write(f"<td>{_esc(s.insider_name)}</td>")
            body.write(f"<td>{_esc(s.role or '?')}</td>")
            body.write(f"<td>{_esc(s.transaction_type.replace('_', ' '))}</td>")
            body.write(f'<td class="num">{s.shares:,.0f}</td>')
            body.write(f'<td class="num">{_fmt_usd_short(s.transaction_value)}</td>')
            body.write(f'<td class="num {strength_tone}">{strength_pct}</td>')
            body.write(f"<td>{_esc(s.rationale)}</td>")
            body.write("</tr>")
        body.write("</tbody></table></div>")

    body.write("</div>")


def _fmt_usd_short(v: float | None) -> str:
    """Compact USD formatter for the comp table."""
    if v is None:
        return '<span class="muted">-</span>'
    av = abs(v)
    if av >= 1e9:
        return f"${v / 1e9:.1f}B"
    if av >= 1e6:
        return f"${v / 1e6:.1f}M"
    if av >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"


# ---------------------------------------------------------------------------
# Synthesis tab — Lens artifacts (cross-section analytical layer)
# ---------------------------------------------------------------------------


_LENS_LABELS: dict[str, tuple[str, str]] = {
    # name -> (display_label, one-line description)
    "five_min_reread": ("5-min reread", "What changed · recommended action · what would change my mind"),
    "thesis_drift_qoq": ("Thesis drift Q/Q", "How this quarter engaged with the prior bear case"),
    "bull_case": ("Bull case", "What would have to happen for this to work spectacularly"),
    "reverse_dcf": ("Reverse DCF", "What the market is implying vs the thesis"),
    "underweighted_facts": ("Underweighted facts", "5 things consensus is missing this quarter"),
    "catalyst_calendar": ("Catalyst calendar", "Upcoming catalysts + bingo card"),
    "filing_diff_narrative": ("Filing diff narrative", "10-K Item 1A year-over-year changes"),
    "footnote_anomaly": ("Footnote anomalies", "Item 8 footnote signals consensus ignores"),
}


def _synthesis_tab(body: StringIO, section: SynthesisSection | None) -> None:
    """§14 — render cached lens artifacts as collapsible panels.

    The first lens (typically five_min_reread per LENS_ORDER) renders OPEN by
    default — it's the decision-layer artifact. Subsequent lenses render
    collapsed via <details>. Each panel header carries the lens label +
    age + model used.
    """
    body.write('<div class="tab-body">')
    if section is None or section.status != SectionStatus.OK or not section.lenses:
        body.write(
            '<div class="row-split"><div>'
            '<div class="eyebrow">Cross-section synthesis</div>'
            '<h2 class="section-title">Synthesis</h2></div></div>'
            '<div class="panel"><div class="stub"><span class="stub-label">no lens artifacts cached</span>'
            "Generate per-ticker analytical lenses with:</div>"
            '<pre class="cli-hint">python execution/run_lens.py --ticker '
            f'{section.ticker if section else "<TICKER>"} --all</pre></div>'
        )
        body.write("</div>")
        return

    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Cross-section synthesis</div>')
    body.write(
        f'<h2 class="section-title">Synthesis · {len(section.lenses)} lens artifacts</h2>'
    )
    body.write(
        '<p class="sub">Cached cross-section analytical reads. '
        f'Regenerate with <code>python execution/run_lens.py --ticker {_esc(section.ticker)} --all</code>.</p>'
    )
    body.write("</div></div>")

    for idx, lens in enumerate(section.lenses):
        label, sub = _LENS_LABELS.get(lens.name, (lens.name, ""))
        is_first = idx == 0
        age_str = ""
        if lens.generated_at:
            from datetime import UTC as _UTC, datetime as _dt
            age = _dt.now(_UTC) - lens.generated_at
            if age.days >= 1:
                age_str = f" · {age.days}d ago"
            else:
                age_str = f" · {int(age.total_seconds() / 3600)}h ago"
        warn = ' <span class="lens-warn">DIRTY</span>' if lens.is_dirty else (
            ' <span class="lens-stale">STALE</span>' if lens.is_stale else ""
        )
        model_str = f" · {lens.model}" if lens.model else ""

        # First lens (5-min reread typically) renders OPEN
        if is_first:
            body.write(
                f'<div class="panel lens-panel lens-{_esc(lens.name)}">'
                '<div class="panel-head">'
                f'<span class="panel-title">{_esc(label)}{warn}</span>'
                f'<span class="panel-sub">{_esc(sub)}{age_str}{model_str}</span>'
                "</div>"
                f'<div class="prose-pad lens-body">{_render_markdown(lens.content_md)}</div>'
                "</div>"
            )
        else:
            body.write(
                f'<details class="panel lens-panel lens-collapsed lens-{_esc(lens.name)}">'
                "<summary>"
                f'<span class="panel-title">{_esc(label)}{warn}</span>'
                f'<span class="panel-sub">{_esc(sub)}{age_str}{model_str}</span>'
                "</summary>"
                f'<div class="prose-pad lens-body">{_render_markdown(lens.content_md)}</div>'
                "</details>"
            )

    body.write("</div>")


# ---------------------------------------------------------------------------
# Position tab
# ---------------------------------------------------------------------------


def _position_tab(body: StringIO, pp: PortfolioPositionSection | None) -> None:
    body.write('<div class="tab-body">')
    if pp is None or not pp.held:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Position</span></div>'
            '<div class="stub"><span class="stub-label">not held</span>'
            "Portfolio tracker shows no position in this name.</div></div>"
        )
        body.write("</div>")
        return
    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Your position</div>')
    body.write(
        '<h2 class="section-title">'
        f"{pp.total_quantity:.0f} shares across {len(pp.accounts)} account"
        f"{'s' if len(pp.accounts) != 1 else ''}."
        "</h2>"
    )
    body.write("</div></div>")

    body.write('<div class="position-stats">')
    _position_stat(body, "Cost basis", _fmt_usd(pp.total_cost_basis))
    _position_stat(body, "Market value", _fmt_usd(pp.total_market_value))
    pnl_tone = ""
    if pp.total_unrealized_pnl is not None:
        pnl_tone = "pos" if pp.total_unrealized_pnl >= 0 else "neg"
    _position_stat(body, "Unrealized P&L", _fmt_usd(pp.total_unrealized_pnl), tone=pnl_tone)
    if pp.total_unrealized_pct is not None:
        _position_stat(
            body,
            "Return",
            f"{pp.total_unrealized_pct * 100:+.1f}%",
            tone=pnl_tone,
        )
    body.write("</div>")

    if pp.accounts:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Accounts</span>'
            f'<span class="panel-sub">{len(pp.accounts)} rows</span></div>'
            '<div class="table-scroll"><table class="fin-table"><thead><tr>'
            "<th>Account</th>"
            '<th class="num">Shares</th>'
            '<th class="num">Cost basis</th>'
            '<th class="num">Cost source</th>'
            '<th class="num">Market value</th>'
            '<th class="num">P&L</th>'
            '<th class="num">Return</th></tr></thead><tbody>'
        )
        for a in pp.accounts:
            pnl_cls = ""
            if a.unrealized_pnl is not None:
                pnl_cls = " pos" if a.unrealized_pnl >= 0 else " neg"
            body.write(f"<tr><td>{_esc(a.account_name)}</td>")
            body.write(f'<td class="num">{a.quantity:.0f}</td>')
            body.write(f'<td class="num">{_fmt_usd(a.cost_basis)}</td>')
            # Cost-basis source — 'broker' (None means broker), 'manual',
            # 'inferred_acats', etc. Surfaces data-quality at a glance.
            src = a.cost_basis_source or "broker"
            body.write(f'<td class="num muted xsmall">{_esc(src)}</td>')
            body.write(f'<td class="num">{_fmt_usd(a.market_value)}</td>')
            body.write(f'<td class="num{pnl_cls}">{_fmt_usd(a.unrealized_pnl)}</td>')
            pct = f"{a.unrealized_pct * 100:+.1f}%" if a.unrealized_pct is not None else "—"
            body.write(f'<td class="num{pnl_cls}">{_esc(pct)}</td></tr>')
        body.write("</tbody></table></div></div>")

    # Recent transactions panel — last N broker activity rows on this ticker.
    # Goes ABOVE the decisions section so the activity context is visible
    # before the analyst's own decision log.
    if pp.recent_transactions:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Recent transactions</span>'
            f'<span class="panel-sub">{len(pp.recent_transactions)} most-recent</span></div>'
            '<div class="table-scroll"><table class="fin-table"><thead><tr>'
            "<th>Date</th><th>Account</th><th>Type</th>"
            '<th class="num">Shares</th><th class="num">Amount</th>'
            "</tr></thead><tbody>"
        )
        for t in pp.recent_transactions:
            body.write(
                f"<tr><td>{t.date.isoformat()}</td>"
                f"<td>{_esc(t.account_name)}</td>"
                f"<td>{_esc(t.type)}</td>"
                f'<td class="num">{t.quantity:+.0f}</td>'
                f'<td class="num">{_fmt_usd(t.amount)}</td></tr>'
            )
        body.write("</tbody></table></div></div>")

    if pp.open_decisions or pp.closed_decisions:
        body.write('<div class="grid-2col">')
        for title, decisions in (
            ("Open decisions", pp.open_decisions),
            ("Closed decisions", pp.closed_decisions),
        ):
            body.write(
                f'<div class="panel"><div class="panel-head">'
                f'<span class="panel-title">{_esc(title)}</span>'
                f'<span class="panel-sub">{len(decisions)} logged</span></div>'
            )
            if not decisions:
                body.write(
                    '<div class="stub"><span class="stub-label">empty</span>'
                    "No decisions logged.</div>"
                )
            else:
                for d in decisions:
                    body.write('<div class="decision-card"><div class="decision-head">')
                    body.write(f'<span class="decision-date">{d.decision_date.isoformat()}</span>')
                    body.write(f'<span class="decision-action">{_esc(d.action)}</span>')
                    if d.confidence:
                        body.write(f'<span class="decision-confidence">{_esc(d.confidence)}</span>')
                    if d.outcome_status and d.outcome_status not in ("open", "None"):
                        body.write(
                            f'<span class="decision-outcome {_esc(d.outcome_status)}">'
                            f"{_esc(d.outcome_status)}</span>"
                        )
                    if d.linked_brief_path:
                        # Link directly to the brief that backed this decision.
                        as_uri = d.linked_brief_path.replace("\\", "/")
                        if not as_uri.startswith(("file://", "http://", "https://")):
                            as_uri = "file:///" + as_uri.lstrip("/")
                        body.write(
                            f'<a class="decision-brief-link" href="{_esc(as_uri)}" '
                            'target="_blank" rel="noopener">brief ↗</a>'
                        )
                    body.write("</div>")
                    body.write(f'<p class="decision-thesis">{_esc(d.thesis)}</p>')
                    # Closed-decision outcome notes/date — high-signal post-mortem.
                    if d.outcome_status not in (None, "open") and (
                        d.outcome_date or d.outcome_notes
                    ):
                        body.write('<div class="decision-outcome-block xsmall muted">')
                        if d.outcome_date is not None:
                            body.write(
                                f"<span><strong>Closed {d.outcome_date.isoformat()}.</strong> </span>"
                            )
                        if d.outcome_notes:
                            body.write(f"<span>{_esc(d.outcome_notes)}</span>")
                        body.write("</div>")
                    body.write("</div>")
            body.write("</div>")
        body.write("</div>")

    body.write("</div>")


def _position_stat(body: StringIO, label: str, value: str, *, tone: str = "") -> None:
    tone_cls = f" {tone}" if tone else ""
    body.write('<div class="position-stat">')
    body.write(f'<div class="position-stat-label">{_esc(label)}</div>')
    body.write(f'<div class="position-stat-value{tone_cls}">{_esc(value)}</div>')
    body.write("</div>")


# ---------------------------------------------------------------------------
# Sources tab
# ---------------------------------------------------------------------------


def _sources_tab(body: StringIO, prov: ProvenanceSection, app: AppendixSection) -> None:
    body.write('<div class="tab-body">')
    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Provenance &amp; sources</div>')
    body.write(
        '<h2 class="section-title">'
        f"{len(prov.coverage)} quarter{'s' if len(prov.coverage) != 1 else ''} of coverage · "
        f"{len(prov.source_docs)} source documents · "
        f"{len(app.transcripts)} transcript{'s' if len(app.transcripts) != 1 else ''} inline."
        "</h2>"
    )
    body.write("</div></div>")

    # Transcripts first — most-used scroll target per user request.
    if app.transcripts:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Earnings-call transcripts</span>'
            f'<span class="panel-sub">{len(app.transcripts)} on file · click to expand</span>'
            "</div>"
        )
        for t in app.transcripts:
            body.write('<details class="transcript-block">')
            body.write(f"<summary>{_esc(t.quarter)} {t.year} — {len(t.text):,} chars</summary>")
            body.write(f'<div class="transcript-text">{_esc(t.text)}</div>')
            body.write("</details>")
        body.write("</div>")

    # Surface open validation issues as a dedicated panel above the matrix
    # when there are any — the count alone (previously a small subhead) was
    # easy to miss. Severity-sorted (errors first), capped server-side at 50.
    if prov.open_issues_detail:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Open validation issues</span>'
            f'<span class="pill pill-warn">{prov.open_validation_issues} open</span></div>'
            '<div class="table-scroll"><table class="fin-table"><thead><tr>'
            "<th>Severity</th><th>Rule</th><th>Raw value</th><th>Expected</th><th>Raised</th>"
            "</tr></thead><tbody>"
        )
        for v in prov.open_issues_detail:
            sev_cls = {
                "error": "neg",
                "warning": "warn",
            }.get(v.severity.lower(), "muted")
            body.write(
                f"<tr>"
                f'<td class="num {sev_cls}">{_esc(v.severity.upper() or "—")}</td>'
                f"<td>{_esc(v.rule)}</td>"
                f"<td>{_esc(v.raw_value or '—')}</td>"
                f"<td>{_esc(v.expected or '—')}</td>"
                f"<td>{_esc((v.raised_at or '')[:10])}</td></tr>"
            )
        body.write("</tbody></table></div></div>")

    if prov.coverage:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Coverage matrix</span>'
            f'<span class="panel-sub">{prov.open_validation_issues} open validation issues</span>'
            "</div>"
            '<div class="table-scroll"><table class="coverage-table"><thead><tr>'
            "<th>Quarter</th>"
            '<th class="cov-cell">Audio</th>'
            '<th class="cov-cell">Transcript</th>'
            '<th class="cov-cell">Release</th>'
            '<th class="cov-cell">Slides</th>'
            '<th class="cov-cell">SayDo</th>'
            '<th class="cov-cell">Summary</th></tr></thead><tbody>'
        )
        for c in prov.coverage:
            body.write(f"<tr><td>{_esc(c.quarter)} {c.year}</td>")
            for present in (
                c.has_audio_file,
                c.has_transcript_file,
                c.has_release_file,
                c.has_slides_file,
                c.step_saydo_analyzed,
                c.step_llm_summarized,
            ):
                mark = "●" if present else "○"
                cls = "cov-yes" if present else "cov-no"
                body.write(f'<td class="cov-cell {cls}">{mark}</td>')
            body.write("</tr>")
        body.write("</tbody></table></div></div>")

    if prov.source_docs:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Source documents</span>'
            f'<span class="panel-sub">{len(prov.source_docs)} files</span></div>'
            '<div class="table-scroll"><table class="fin-table"><thead><tr>'
            "<th>Type</th><th>Period</th><th>Path</th><th>Fetched</th>"
            "</tr></thead><tbody>"
        )
        for d in prov.source_docs:
            body.write(
                f"<tr><td>{_esc(d.doc_type)}</td>"
                f"<td>{_esc(d.period_end or '—')}</td>"
                f"<td>{_esc(d.file_path)}</td>"
                f"<td>{_esc(d.fetched_at or '—')}</td></tr>"
            )
        body.write("</tbody></table></div></div>")

    body.write("</div>")


# ---------------------------------------------------------------------------
# Comment + chat boot data — inlined JSON for the JS UI
# ---------------------------------------------------------------------------


def _comment_boot_data(body: StringIO, spec: ReportSpec) -> None:
    """Embed `<script type="application/json">` blocks the JS modules pick up:
    - workspace-boot: ticker, report_date, server URL (default localhost:7421)
    - workspace-comments: existing comments for this (ticker, date) so pins
      render on first paint without a server fetch.

    No server connection required for read-only display (pins + side panel).
    POSTing new comments + chat needs the server (`python execution/comments_server.py`)."""
    import json as _json

    from comments import load_store, to_json_payload

    boot = {
        "ticker": spec.ticker,
        "report_date": spec.generation_date.isoformat(),
        "server_url": "http://localhost:7421",
    }
    body.write(
        '<script id="workspace-boot" type="application/json">'
        f"{_json.dumps(boot)}"
        "</script>"
    )
    try:
        store = load_store(Path(spec.repo_root), spec.ticker, spec.generation_date)
        payload = to_json_payload(store)
    except Exception:
        payload = {
            "ticker": spec.ticker,
            "report_date": spec.generation_date.isoformat(),
            "comments": [],
        }
    body.write(
        '<script id="workspace-comments" type="application/json">'
        f"{_json.dumps(payload)}"
        "</script>"
    )


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------


def _footer(body: StringIO, spec: ReportSpec) -> None:
    body.write('<div class="l1-footer">')
    body.write(
        f'<div class="footer-left">'
        f"<span>Research package · {_esc(spec.ticker)} · {spec.generation_date.isoformat()}</span>"
        "</div>"
    )
    body.write(
        '<div class="footer-mid">'
        '<span class="muted">DCF · earnings transcripts · Say-Do · KPI ledger · break rules</span>'
        "</div>"
    )
    body.write(
        '<div class="footer-right">'
        '<span class="muted mono">renderer · workspace · v0.1</span>'
        "</div>"
    )
    body.write("</div>")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _fmt_usd(v: float | None) -> str:
    """Full-precision dollar (2 decimals). Used for cost basis / P&L amounts."""
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _fmt_price(v: float | None) -> str:
    """Zero-decimal price ($388). Used in the identity strip and valuation summary."""
    if v is None:
        return "—"
    return f"${v:,.0f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.1f}%"


def _missing_panel(body: StringIO, status: SectionStatus, missing: MissingReason | None) -> None:
    body.write('<div class="panel"><div class="panel-head">')
    body.write(f'<span class="panel-title">Section status: {status.value}</span>')
    body.write("</div>")
    body.write(f'<div class="stub"><span class="stub-label">{status.value}</span>')
    if missing is not None:
        body.write(_esc(missing.detail or "No data."))
        if missing.fix_command:
            body.write(f'<br><span class="mono">{_esc(missing.fix_command)}</span>')
    else:
        body.write("Section returned no data.")
    body.write("</div></div>")


def _render_markdown(md: str) -> str:
    """Tiny markdown → HTML renderer.

    Handles: headings, paragraphs, bullet lists, bold, italic, inline code,
    pipe tables. Enough for the LLM-emitted earnings / saydo content. Anything
    fancier is escaped and presented as-is.
    """
    if not md:
        return ""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_ul = False
    in_table = False
    table_rows: list[list[str]] = []

    def flush_table() -> None:
        nonlocal in_table
        if not table_rows:
            return
        out.append('<div class="table-scroll"><table class="fin-table"><thead><tr>')
        for c in table_rows[0]:
            out.append(f"<th>{_inline_md(c)}</th>")
        out.append("</tr></thead><tbody>")
        for row in table_rows[2:]:  # skip separator at index 1
            out.append("<tr>")
            for c in row:
                out.append(f"<td>{_inline_md(c)}</td>")
            out.append("</tr>")
        out.append("</tbody></table></div>")
        table_rows.clear()
        in_table = False

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            table_rows.append(cells)
            in_table = True
            continue
        if in_table:
            flush_table()

        if not line.strip():
            if in_ul:
                out.append("</ul>")
                in_ul = False
            continue

        m_h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m_h:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            level = min(len(m_h.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline_md(m_h.group(2))}</h{level}>")
            continue

        if re.match(r"^\s*[-*]\s+", line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            content = re.sub(r"^\s*[-*]\s+", "", line)
            out.append(f"<li>{_inline_md(content)}</li>")
            continue

        if in_ul:
            out.append("</ul>")
            in_ul = False
        out.append(f"<p>{_inline_md(line)}</p>")

    if in_ul:
        out.append("</ul>")
    if in_table:
        flush_table()
    return "".join(out)


def _inline_md(text: str) -> str:
    text = _esc(text)
    text = _BOLD_RX.sub(r"<strong>\1</strong>", text)
    text = _ITAL_RX.sub(r"<em>\1</em>", text)
    return _INLINE_CODE_RX.sub(r"<code>\1</code>", text)
