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
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import TypeAlias

from industry_classifier import (
    SECTION_CUSTOMER_CONCENTRATION,
    SECTION_LEASE_LADDER,
    SECTION_STRATEGIC_TARGETS,
)
from llm.calibration import (
    VersionSummary,
    daily_avg_scores,
    summarize_by_prompt_version,
)
from report.kpi_naming import clean_kpi_name
from report.models import (
    AppendixSection,
    BearCaseSection,
    BreakRuleEvaluation,
    BudgetSkip,
    CellSource,
    CompanyDescriptionSection,
    DecisionBadge,
    EarningsSection,
    EvaluationSnapshotSection,
    ExecCompSectionModel,
    FailureMode,
    FilingIntelligenceSection,
    FinancialsSection,
    IrDocsSection,
    KpiLedgerRow,
    MissingReason,
    PortfolioPositionSection,
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
    SignalRow,
    SignalsSection,
    SnapshotSection,
    SoftRuleEvaluation,
    SurpriseScorecardCard,
    SynthesisSection,
    ThesisSection,
    ValuationBasisSection,
)
from report.renderers.charts_v2 import CSS as CHARTS_V2_CSS
from report.renderers.charts_v2 import MatrixRow, yoy_heatmap_table
from report.renderers.workspace_charts import sparkline, verdict_bar
from report.renderers.workspace_chat import CSS as CHAT_CSS
from report.renderers.workspace_chat import JS as CHAT_JS
from report.renderers.workspace_comments import CSS as COMMENTS_CSS
from report.renderers.workspace_comments import JS as COMMENTS_JS
from report.renderers.workspace_data import (
    KpiStripTile,
    NewsTile,
    PrintVsGuideRow,
    WorkspaceP3Panels,
    filter_important_print_vs_guide,
    format_ledger_value,
    kpi_is_stale,
    kpi_trend_delta,
    load_workspace_p3_panels,
    parse_print_vs_guide,
    quarter_short,
    select_kpi_strip,
    structure_news_by_section,
)
from report.renderers.workspace_script import JS
from report.renderers.workspace_styles import CSS
from report.sections.p3_data import (
    CustomerConcentrationRow,
    DecisionHistorySummary,
    LeaseLadderRow,
    MacroSensitivityRow,
    PeerCompRow,
    SayDoVerdictRow,
    StrategicTargetRow,
)
from ui.tokens import FAVICON_LINK

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
    _forgone_strip(body, spec.forgone_due_to_budget)
    _thesis_strip(body, spec.snapshot, spec.thesis)
    _kpi_strip(body, spec.thesis.kpi_ledger)

    # P3 panel data (macro sensitivities, strategic targets, customer
    # concentrations, lease ladder, decision history, say-do verdicts,
    # peer comp) — pre-loaded once and threaded into the tab definitions.
    p3 = load_workspace_p3_panels(spec.ticker, Path(spec.repo_root))

    body.write('<div class="l1-tabs-wrap">')
    tabs = _tab_defs(spec, p3)
    _tabs(body, tabs)
    for i, (tid, label, _count, render_fn) in enumerate(tabs):
        _ = label
        # First tab is active on load so its pane renders immediately. The tab
        # BUTTON already gets `active` at i==0 (see _tabs); without the matching
        # pane class every pane stays display:none until the user clicks one,
        # which is why the report opened blank on the active tab.
        active = " active" if i == 0 else ""
        body.write(f'<div class="tab-pane{active}" data-tab="{_esc(tid)}">')
        render_fn(body)
        body.write("</div>")
    body.write("</div>")  # /l1-tabs-wrap

    _footer(body, spec)
    body.write("</div>")  # /l1-root

    # Sidebar + chat-drawer shells are emitted at render time so the
    # `<body>` flex layout (.l1-root | .cmt-sidebar) is explicit in the
    # markup rather than assembled at runtime via appendChild. The JS
    # modules just toggle classes + populate the per-anchor list.
    _comment_sidebar_shell(body)
    _chat_drawer_shell(body, spec.ticker, spec.generation_date.isoformat())

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
{FAVICON_LINK}
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


def _tab_defs(spec: ReportSpec, p3: WorkspaceP3Panels) -> list[TabDef]:
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
                lambda b: _eval_tab(b, eval_snap, p3.peer_comp),
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
            lambda b: _thesis_tab(
                b,
                spec.snapshot,
                spec.thesis,
                spec.bear_case,
                p3.macro_sensitivities,
                spec.generation_date,
            ),
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
            lambda b: _saydo_tab(b, spec.saydo, spec, p3.saydo_verdicts),
        ),
        (
            "financials",
            "Financials",
            len(spec.financials.quarter_labels),
            lambda b: _financials_tab(b, spec.financials, spec.segments, spec.signals),
        ),
        (
            "decisions",
            "Decisions",
            p3.decision_history.total or None,
            lambda b: _decisions_tab(b, p3.decision_history),
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
            lambda b: _company_tab(
                b,
                spec.company_description,
                spec.ir_docs,
                spec.filing_intelligence,
                p3.strategic_targets,
                p3.customer_concentrations,
                p3.lease_ladder,
                suppressed_sections=frozenset(spec.suppressed_sections),
            ),
        ),
        (
            "exec_comp",
            "Exec Comp",
            (
                len(spec.exec_compensation.insider_signals) + len(spec.exec_compensation.packages)
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
            lambda b: _sources_tab(b, spec.provenance, spec.appendix, spec.repo_root),
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
    # (Removed the oversized "N quarters on file" section-title — the quarter
    # selector below already conveys how many quarters are on file.)
    body.write("</div>")
    if cards:
        _quarter_selector(body, [c.quarter + " " + str(c.year) for c in cards], group="earnings")
    body.write("</div>")

    # Beat-rate scorecard — small panel above the quarter cards summarizing
    # how the analyst consensus has fared over the lookback window.
    if section.surprise_scorecard is not None:
        _beat_rate_scorecard_panel(body, section.surprise_scorecard)

    # 4Q cross-quarter theme rollup — what management said vs what analysts
    # pressed on. Only renders when --enable-llm produced theme data; offline
    # builds skip silently.
    _earnings_themes_panel(body, section)

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


def _earnings_themes_panel(body: StringIO, section: EarningsSection) -> None:
    """4Q rolling theme rollup, split prepared vs Q&A.

    Skips when both sides are empty AND no themes_note exists (offline
    builds). When a side has no source material across the window, the
    builder leaves its list empty and sets themes_note so we surface the
    explanation rather than silently hiding the half.
    """
    has_any = bool(section.prepared_remarks_themes) or bool(section.qa_themes)
    if not has_any and not section.themes_note:
        return
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Cross-quarter themes</span>'
        '<span class="panel-sub">last 4 quarters · what management said vs what analysts pressed on</span>'
        "</div>"
    )
    if section.themes_note:
        body.write(f'<p class="muted theme-note">{_esc(section.themes_note)}</p>')
    if section.prepared_remarks_themes:
        body.write(
            '<div class="theme-bucket"><h4 class="theme-bucket-title">Prepared remarks themes</h4>'
        )
        _theme_list(body, section.prepared_remarks_themes)
        body.write("</div>")
    if section.qa_themes:
        body.write('<div class="theme-bucket"><h4 class="theme-bucket-title">Q&amp;A themes</h4>')
        _theme_list(body, section.qa_themes)
        body.write("</div>")
    body.write("</div>")


def _theme_list(body: StringIO, themes) -> None:
    body.write('<ul class="theme-rollup-list">')
    for theme in themes:
        body.write('<li class="theme-row">')
        body.write(
            f'<div class="theme-head"><strong>{_esc(theme.theme_name)}</strong>'
            f' <span class="muted">({theme.last_4q_count} mentions)</span></div>'
        )
        if theme.mentions_per_quarter:
            ordered = sorted(
                theme.mentions_per_quarter.items(),
                key=lambda kv: _ws_period_sort_key(kv[0]),
            )
            body.write('<div class="theme-spark">')
            for q, n in ordered:
                body.write(
                    f'<span class="theme-spark-cell">{_esc(q)}<span class="theme-spark-n">{n}</span></span>'
                )
            body.write("</div>")
        if theme.evidence:
            body.write('<ul class="theme-evidence">')
            for ev in theme.evidence:
                speaker = f"{_esc(ev.speaker)} · " if ev.speaker else ""
                body.write(
                    f"<li><em>&ldquo;{_esc(ev.text)}&rdquo;</em>"
                    f' <span class="muted">— {speaker}{_esc(ev.period)}</span></li>'
                )
            body.write("</ul>")
        body.write("</li>")
    body.write("</ul>")


def _ws_period_sort_key(period: str) -> tuple[int, int]:
    """Chronological sort key for 'Qx YYYY' (or 'YYYY Qx') period labels."""
    import re as _re

    y_match = _re.search(r"(20\d{2})", period)
    q_match = _re.search(r"Q([1-4])", period)
    if y_match and q_match:
        return (int(y_match.group(1)), int(q_match.group(1)))
    return (9999, 9)


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


_SOURCE_CHIP_ABBREV: dict[str, str] = {
    "sec_official": "SEC",
    "fmp_normalized": "FMP",
    "llm_extracted": "LLM",
    "yfinance_fallback": "YF",
    "s1_provisional": "S-1",
}


def _source_hover_title(src: CellSource) -> str:
    """Hover text for a sourced number: tier + fetched-at (P3.3 contract)."""
    parts = [src.source]
    if src.fetched_at:
        parts.append(f"fetched {src.fetched_at[:10]}")
    return " · ".join(parts)


def _source_chip_html(src: CellSource) -> str:
    """Clickable per-number source chip: hover = tier + fetched-at; click
    opens a JS-free <details> popover with the document identity (doc type,
    accession, filing date, sub-document locator) and the open-source link.
    """
    abbrev = _SOURCE_CHIP_ABBREV.get(src.source, src.source[:3].upper() or "?")
    tier_slug = src.source.replace("_", "-")
    rows: list[str] = [f'<div class="src-pop-row"><b>{_esc(src.source)}</b></div>']
    if src.fetched_at:
        rows.append(f'<div class="src-pop-row">fetched {_esc(src.fetched_at[:10])}</div>')
    if src.doc_type:
        rows.append(f'<div class="src-pop-row">{_esc(src.doc_type)}</div>')
    if src.accession_number:
        acc = _esc(src.accession_number)
        filed = f" · filed {_esc(src.filing_date)}" if src.filing_date else ""
        rows.append(f'<div class="src-pop-row mono">{acc}{filed}</div>')
    if src.locator:
        rows.append(f'<div class="src-pop-row mono src-pop-locator">{_esc(src.locator)}</div>')
    if src.source_url:
        rows.append(
            f'<div class="src-pop-row"><a href="{_esc(src.source_url)}" target="_blank" '
            'rel="noopener">open source ↗</a></div>'
        )
    return (
        '<details class="src-pop">'
        f'<summary class="src-chip src-{_esc(tier_slug)}" '
        f'title="{_esc(_source_hover_title(src))}">{_esc(abbrev)}</summary>'
        f'<div class="src-pop-body">{"".join(rows)}</div>'
        "</details>"
    )


def _source_for_display_index(li: QuarterlyLineItem, idx: int) -> CellSource | None:
    """CellSource for a position in ``li.values`` (the display window).

    ``sources_full`` aligns to ``levels_full``; ``values`` is its tail —
    translate the display index into full-series coordinates.
    """
    if not li.sources_full or idx < 0:
        return None
    base = len(li.levels_full) - len(li.values) if li.levels_full else 0
    j = base + idx
    if 0 <= j < len(li.sources_full):
        return li.sources_full[j]
    return None


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
        idx = _pos_of_card(li, card)
        v = li.values[idx]
        src = _source_for_display_index(li, idx)
        chip = f" {_source_chip_html(src)}" if src is not None else ""
        body.write(f'<td class="num">{_fmt_line_value(li, v)}{chip}</td>')
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


def _saydo_tab(
    body: StringIO,
    section: SayDoSection,
    spec: ReportSpec,
    verdicts: list[SayDoVerdictRow] | None = None,
) -> None:
    cards = section.cards
    body.write('<div class="tab-body">')
    if not cards:
        _missing_panel(body, section.status, section.missing)
        # Even on a stub SayDo, if verdicts exist they're worth surfacing.
        if verdicts:
            _saydo_verdicts_panel(body, verdicts)
        body.write("</div>")
        return
    ratings = [c.rating for c in cards][::-1]
    # Section eyebrow + the cross-quarter verdict trajectory (global), then a
    # quarter selector. The per-quarter detail blocks below swap on click — so
    # "Q1 2026" lands on the Q1 2026-vs-Q4 2025 read — mirroring the earnings
    # tab's quarter toggles.
    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Say · Do · Track</div>')
    body.write("</div>")
    body.write('<div class="saydo-meta"><div class="saydo-history">')
    body.write(verdict_bar(ratings))
    body.write(
        f'<div class="kpi-axis"><span>{len(ratings)} quarters tracked</span>'
        "<span>most recent →</span></div>"
    )
    body.write("</div></div></div>")
    _quarter_selector(body, [f"{c.current_quarter} {c.current_year}" for c in cards], group="saydo")

    # Cross-card panels are global (one trajectory, not per-quarter).
    if len(cards) >= 2:
        _saydo_summary_table(body, cards)
    if section.historical_metrics:
        _saydo_historical_ledger(body, section.historical_metrics)

    # Per-quarter detail blocks — one visible at a time, swapped by the selector.
    for i, card in enumerate(cards):
        display = "" if i == 0 else "display:none"
        qid = f"{card.current_quarter} {card.current_year}"
        body.write(
            f'<div data-quarter-card data-quarter-group="saydo" '
            f'data-quarter="{_esc(qid)}" style="{display}">'
        )
        body.write('<div class="row-split"><div>')
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
        body.write("</div></div>")

        pvg = parse_print_vs_guide(card)
        if pvg:
            # LLM-filter to drop trivial commitments (FX, tax, share-count noise)
            # when --enable-llm; else show parsed rows verbatim. Both cache to disk.
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
        body.write(
            f'<div class="prose-pad">{_render_markdown(card.thesis_view or "—")}</div></div>'
        )
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Attribution</span>'
            '<span class="panel-sub">Execution · exogenous · luck</span></div>'
        )
        body.write(
            f'<div class="prose-pad">{_render_markdown(card.attribution or "—")}</div></div>'
        )
        body.write("</div>")

        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Full Say·Do narrative</span>'
            f'<span class="panel-sub">{_esc(card.current_quarter)} {card.current_year}</span>'
            "</div>"
        )
        body.write(f'<div class="prose-pad">{_render_markdown(card.saydo_md)}</div></div>')
        body.write("</div>")

    # P3-21 grading overlay — management commitments + outcome ledger (global).
    _saydo_verdicts_panel(body, verdicts or [])

    body.write("</div>")


def _saydo_verdicts_panel(body: StringIO, rows: list[SayDoVerdictRow]) -> None:
    """P3-21 management-commitments verdict ledger.

    Lays the audit-trail outcomes alongside the narrative. Rows arrive
    newest-first from ``load_saydo_verdicts``; we render them in that order.
    Empty-state when the table has no rows for this ticker.
    """
    body.write(
        '<div class="panel saydo-verdicts-panel"><div class="panel-head">'
        '<span class="panel-title">Say·Do verdict ledger</span>'
    )
    if rows:
        graded = sum(1 for r in rows if r.outcome is not None)
        body.write(
            f'<span class="panel-sub">{len(rows)} commitment'
            f"{'s' if len(rows) != 1 else ''} · {graded} graded</span></div>"
        )
    else:
        body.write('<span class="panel-sub">no commitments extracted</span></div>')
        body.write(
            '<div class="stub"><span class="stub-label">cold ticker</span>'
            "No management_commitments rows yet for this ticker. Run the "
            "Say·Do commitment extractor (alembic 0017) against the recent "
            "earnings transcripts to populate.</div></div>"
        )
        return
    body.write(
        '<table class="saydo-table"><thead><tr>'
        "<th>Made</th>"
        "<th>Target period</th>"
        "<th>KPI</th>"
        "<th>Promised</th>"
        "<th>Realized</th>"
        "<th>Verdict</th>"
        "</tr></thead><tbody>"
    )
    comp_map = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<", "eq": "≈"}
    for r in rows:
        promise = f"{comp_map.get(r.comparator.lower(), r.comparator)} {r.target_value:g} {r.unit}"
        if r.realized_value is not None:
            realized = f"{r.realized_value:g} {r.unit}"
        else:
            realized = '<span class="muted">—</span>'
        outcome = r.outcome if r.outcome else "no_data"
        body.write("<tr>")
        body.write(f'<td class="mono xsmall">{_esc(_fmt_made_period(r.period_made))}</td>')
        body.write(f'<td class="mono xsmall">{_esc(_fmt_made_period(r.period_target))}</td>')
        body.write(f'<td class="saydo-metric">{_esc(r.kpi_name)}</td>')
        body.write(f'<td class="saydo-guide">{_esc(promise)}</td>')
        body.write(f'<td class="saydo-actual"><strong>{realized}</strong></td>')
        body.write(f"<td>{_outcome_pill(outcome)}</td>")
        body.write("</tr>")
    body.write("</tbody></table></div>")


def _fmt_made_period(dt: datetime) -> str:
    """Quarter-style label for a datetime: ``Q3 '25``."""
    quarter = (dt.month - 1) // 3 + 1
    return f"Q{quarter} {chr(0x2019)}{str(dt.year)[2:]}"


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


def _financials_tab(
    body: StringIO,
    fin: FinancialsSection,
    seg: SegmentsSection,
    signals: SignalsSection | None = None,
) -> None:
    """Financials tab — YoY% heatmap (line items first, then per-segment) with
    a validation badge showing whether segment revenue sums tie to the
    consolidated revenue line (catches dropped segments and unit mismatches).
    The per-line-item 12-quarter level table is also rendered with a
    click-to-expand drill-down showing the underlying segment breakdown for
    revenue rows. The §3.5 signals panel renders above the validation row
    when the time-series writer has materialized any current signals."""
    body.write('<div class="tab-body">')
    body.write(
        f'<div class="eyebrow">Financials · {len(fin.quarter_labels)} quarters · {fin.currency} millions</div>'
    )

    if fin.status != SectionStatus.OK and not fin.line_items:
        _missing_panel(body, fin.status, fin.missing)
        body.write("</div>")
        return

    # 0) Time-series signals — surfaced first so red/yellow fires read before
    # the heatmaps. Silently skipped when the section is absent or empty.
    if signals is not None:
        _signals_panel(body, signals)

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
    _segments_yoy_panel_for_metric(
        body,
        title="YoY% — capex by segment",
        rows=seg.capex_by_segment,
        quarter_labels_full=seg.quarter_labels_full,
        quarter_labels=seg.quarter_labels,
        segment_definitions=seg.segment_definitions,
    )
    _segments_yoy_panel_for_metric(
        body,
        title="YoY% — headcount by segment",
        rows=seg.headcount_by_segment,
        quarter_labels_full=seg.quarter_labels_full,
        quarter_labels=seg.quarter_labels,
        segment_definitions=seg.segment_definitions,
    )

    # 3c) KPI time-series matrices — analyst-tracked KPIs that aren't on
    # the line-items axis (ARPAC, GMV growth, NIM, etc.).
    _kpi_series_yoy_panel(body, fin)

    # 3c-annual) Annual-cadence KPIs (bank capital ratios, other 20-F/10-K-only
    # metrics) on a fiscal-year axis — kept off the quarterly heatmap so they
    # render as a clean annual series instead of a mostly-empty quarterly one.
    _annual_kpi_series_yoy_panel(body, fin)

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
            f'<span class="panel-title">By {_esc(axis_label)}{_esc(parent)} · cross-tab</span>'
        )
        body.write(f'<span class="panel-sub">{len(matrix_rows)} rows · junction data</span></div>')
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
    body.write('<span class="panel-title">Tracked KPIs</span>')
    body.write(f'<span class="panel-sub">{len(matrix_rows)} analyst-tracked series</span></div>')
    body.write('<div class="prose-pad">')
    body.write(yoy_heatmap_table(matrix_rows, list(periods), title="", display_quarters=12))
    body.write("</div></div>")


def _annual_kpi_series_yoy_panel(body: StringIO, fin: FinancialsSection) -> None:
    """YoY heatmap for ANNUAL-cadence KPIs on a fiscal-year axis.

    These are metrics the issuer discloses only annually (bank Basel III capital
    ratios, other 20-F/10-K-only figures). Rendering them on the quarterly axis
    produced a mostly-empty 12-quarter row; here they get their own panel with a
    fiscal-year axis and year-over-year shading (``period_stride=1``). Skipped
    when the ticker tracks no annual KPIs.
    """
    series = fin.annual_kpi_chart_series
    years = fin.annual_kpi_years
    if not series or not years:
        return
    matrix_rows: list[MatrixRow] = []
    for s in series:
        if not s.values or all(v is None for v in s.values):
            continue
        matrix_rows.append(MatrixRow(name=s.name, levels=list(s.values), unit=s.unit))
    if not matrix_rows:
        return
    periods = [str(y) for y in years]
    # Trailing CAGR/Δ columns in YEARS, trimmed to the available span so a short
    # annual history (NU CAR = 3 years) doesn't advertise an empty 3y column.
    cagr_years = tuple(y for y in (1, 2, 3) if y < len(years))
    body.write('<div class="panel"><div class="panel-head">')
    body.write('<span class="panel-title">Tracked KPIs — annual</span>')
    body.write(
        f'<span class="panel-sub">{len(matrix_rows)} annual-cadence '
        f"series · fiscal-year axis</span></div>"
    )
    body.write('<div class="prose-pad">')
    body.write(
        yoy_heatmap_table(
            matrix_rows,
            periods,
            title="",
            display_quarters=len(periods),
            cagr_periods=cagr_years,
            period_stride=1,
            periods_per_year=1,
        )
    )
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


def _signals_panel(body: StringIO, signals: SignalsSection) -> None:
    """§3.5 Signals — tier-bucketed cards + collapsible 'All signals' table.

    Silently omitted when no signal of any tier surfaced for this ticker —
    a §3.5 with nothing to say is worse than no §3.5 at all.
    """
    if not (signals.red_signals or signals.yellow_signals or signals.green_signals):
        return
    total = len(signals.red_signals) + len(signals.yellow_signals) + len(signals.green_signals)
    fires = list(signals.red_signals) + list(signals.yellow_signals)
    body.write('<div class="panel"><div class="panel-head">')
    body.write('<span class="panel-title">§3.5 Signals</span>')
    body.write(
        f'<span class="panel-sub">{len(signals.red_signals)} red · '
        f"{len(signals.yellow_signals)} yellow · {len(signals.green_signals)} green</span>"
    )
    body.write("</div>")
    if fires:
        body.write('<div class="signals-fires">')
        for r in fires:
            _signal_card_workspace(body, r)
        body.write("</div>")
    body.write(f'<details class="signals-all"><summary>All signals ({total})</summary>')
    all_rows = (
        list(signals.red_signals) + list(signals.yellow_signals) + list(signals.green_signals)
    )
    _signals_table_workspace(body, all_rows)
    body.write("</details>")
    body.write("</div>")


_SIGNALS_CARD_BG: dict[str, str] = {
    "red": "rgba(185,28,28,0.10)",
    "yellow": "rgba(185,124,0,0.10)",
    "green": "rgba(29,78,216,0.10)",
}
_SIGNALS_CARD_BORDER: dict[str, str] = {
    "red": "var(--bad)",
    "yellow": "var(--warn)",
    "green": "var(--ok)",
}


def _signal_card_workspace(body: StringIO, r: SignalRow) -> None:
    bg = _SIGNALS_CARD_BG.get(r.severity, "transparent")
    border = _SIGNALS_CARD_BORDER.get(r.severity, "var(--hairline)")
    style = (
        "border:1px solid var(--hairline);"
        f"border-left:3px solid {border};"
        f"background:{bg};"
        "border-radius:6px;padding:10px 12px;font-size:12.5px;"
    )
    type_label = r.signal_type.replace("_", " ")
    body.write(f'<div style="{style}">')
    body.write(
        '<div style="display:flex;justify-content:space-between;'
        'gap:8px;align-items:baseline;margin-bottom:4px;">'
    )
    body.write(
        f'<span style="font-weight:600;">{_esc(r.metric_name)}</span>'
        f'<span style="font-size:10.5px;text-transform:uppercase;'
        f'letter-spacing:0.4px;color:var(--muted);">{_esc(type_label)}</span>'
    )
    body.write("</div>")
    if r.narrative:
        body.write(f'<div style="line-height:1.45;margin:4px 0 6px;">{_esc(r.narrative)}</div>')
    if r.value_summary:
        body.write(
            "<div style=\"font-family:'JetBrains Mono',Consolas,monospace;"
            f'font-size:11.5px;color:var(--muted);">{_esc(r.value_summary)}</div>'
        )
    body.write("</div>")


def _signals_table_workspace(body: StringIO, rows: list[SignalRow]) -> None:
    body.write('<div class="prose-pad"><div class="table-scroll">')
    body.write('<table class="metrics-table"><thead><tr>')
    body.write(
        "<th>Sev</th><th>Metric</th><th>Kind</th><th>Signal</th><th>Narrative</th><th>Stat</th>"
    )
    body.write("</tr></thead><tbody>")
    sev_color = {"red": "var(--bad)", "yellow": "var(--warn)", "green": "var(--ok)"}
    for r in rows:
        color = sev_color.get(r.severity, "var(--muted)")
        narrative = _esc(r.narrative) if r.narrative else "—"
        stat = _esc(r.value_summary) if r.value_summary else "—"
        body.write(
            f'<tr><td style="color:{color};font-weight:600;">{_esc(r.severity)}</td>'
            f"<td><strong>{_esc(r.metric_name)}</strong></td>"
            f"<td>{_esc(r.metric_kind)}</td>"
            f"<td>{_esc(r.signal_type.replace('_', ' '))}</td>"
            f"<td>{narrative}</td>"
            f'<td class="mono">{stat}</td></tr>'
        )
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
        # P3.3 source chips: hover per cell (tier + fetched-at of the
        # current-quarter fact), click via the row-label chip (latest
        # sourced quarter's document identity + open-source link).
        cell_titles: list[str | None] | None = None
        label_suffix = ""
        if li.sources_full:
            cell_titles = [
                _source_hover_title(s) if s is not None else None for s in li.sources_full
            ]
            latest_src = next((s for s in reversed(li.sources_full) if s is not None), None)
            if latest_src is not None:
                label_suffix = _source_chip_html(latest_src)
        rows.append(
            MatrixRow(
                name=li.line_item,
                levels=list(levels),
                unit=li.unit,
                cell_titles=cell_titles,
                label_suffix_html=label_suffix,
            )
        )
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
        f'<span class="panel-sub">{fin.currency} millions · QoQ · YoY · 3-yr CAGR · click ▶ to drill</span></div>'
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
    body.write(f'<span class="panel-title">{_esc(vb.multiple_name or "—")}</span>')
    if vb.current_period_end:
        body.write(f'<span class="panel-sub">as of {vb.current_period_end.isoformat()}</span>')
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
    # PEG (only when the chosen multiple is P/E (NTM) and forward EPS growth is
    # positive — the compute layer leaves peg_ratio None elsewhere, so the row
    # self-skips for P/B banks, EV/EBITDA, FCF multiples, and unprofitable /
    # negative-growth names).
    if vb.peg_ratio is not None:
        growth_txt = f"{vb.peg_growth_pct:.1f}%" if vb.peg_growth_pct is not None else "—"
        pe_txt = vb.current_value_display or "—"
        body.write(
            '<div class="valuation-peg" '
            f'title="PEG = {_esc(pe_txt)} P/E (NTM) &divide; {_esc(growth_txt)} forward EPS growth">'
            f'<div class="valuation-peg-value">{vb.peg_ratio:.2f}</div>'
            '<div class="valuation-peg-label">PEG (NTM)</div>'
            f'<div class="valuation-peg-sub">{_esc(pe_txt)} &divide; {_esc(growth_txt)} fwd EPS growth</div>'
            "</div>"
        )
    if vb.rich_cheap_verdict:
        body.write(f'<div class="valuation-verdict">{_esc(vb.rich_cheap_verdict)}</div>')
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
                f"<span>{vb.history[0].period_end.isoformat() if vb.history[0].period_end else '—'}</span>"
                f'<span class="muted">{len(vb.history)}q trailing</span>'
                f"<span>{vb.history[-1].period_end.isoformat() if vb.history[-1].period_end else '—'}</span>"
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
    macro_sensitivities: list[MacroSensitivityRow] | None = None,
    report_date: date | None = None,
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

    # Macro factor sensitivity (P3-18) — β and R² for each tracked series.
    # Empty-state callout when the table has no rows so the panel stays
    # visible even on a cold ticker.
    _macro_sensitivity_panel(body, macro_sensitivities or [])

    # Thesis hygiene: break conditions (narrative thresholds), qualitative
    # breakers (soft thesis-breakers), competitive watchlist (who to track),
    # full tier-2/3 KPI ledger (collapsible). Each panel skips itself when
    # the underlying list is empty so the tab doesn't stack stub panels.
    _thesis_hygiene_panels(body, thesis, report_date)
    body.write("</div>")
    # NOTE: bear case (`bear` arg) is rendered separately by `_bear_tab` —
    # it's promoted to a first-class tab. Arg kept here for back-compat
    # with anything that still passes the section through.
    _ = bear


def _decisions_tab(body: StringIO, history: DecisionHistorySummary) -> None:
    """P3-17 decision-history tab — full audit ledger + conviction × outcome.

    Aggregates from the `decisions` table (alembic 0046) loaded by
    ``load_decision_history``. Always renders an empty-state callout when
    the table has no rows so the tab stays visible on cold tickers and the
    analyst can see whether the ledger ran.
    """
    body.write('<div class="tab-body">')
    body.write('<div class="row-split"><div>')
    body.write(f'<div class="eyebrow">Decision audit · recommendations {_TIMES} outcomes</div>')
    title = (
        f"{history.total} decision{'s' if history.total != 1 else ''} tracked"
        if history.total
        else "No decisions recorded for this ticker yet"
    )
    body.write(f'<h2 class="section-title">{_esc(title)}</h2>')
    body.write("</div></div>")

    if history.total == 0:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Decision ledger</span>'
            '<span class="panel-sub">empty</span></div>'
            '<div class="stub"><span class="stub-label">cold ticker</span>'
            "No decisions audit rows for this ticker. The decision-recorder "
            "(alembic 0046) writes a row each time an LLM lens emits an "
            "ADD/TRIM/HOLD/SELL recommendation — it will fill in on the "
            "next pipeline run.</div></div>"
        )
        body.write("</div>")
        return

    # Headline counters: total · win-rate · by-kind chips.
    body.write('<div class="panel decision-history-panel"><div class="panel-head">')
    body.write('<span class="panel-title">Summary</span>')
    if history.win_rate_overall is not None:
        body.write(
            f'<span class="panel-sub">{history.win_rate_overall * 100:.0f}% '
            "win rate on graded decisions</span>"
        )
    else:
        body.write('<span class="panel-sub">no graded outcomes yet</span>')
    body.write("</div>")
    body.write('<div class="decision-chips">')
    for kind, n in sorted(history.by_kind.items(), key=lambda kv: -kv[1]):
        body.write(
            f'<span class="decision-chip"><span class="decision-chip-label">{_esc(kind.upper())}</span>'
            f'<span class="decision-chip-n">{n}</span></span>'
        )
    body.write("</div>")
    if history.by_conviction:
        body.write('<div class="decision-chips decision-chips-sub">')
        for conv, n in sorted(history.by_conviction.items(), key=lambda kv: -kv[1]):
            body.write(
                f'<span class="decision-chip decision-chip-muted">'
                f'<span class="decision-chip-label">{_esc(conv)}</span>'
                f'<span class="decision-chip-n">{n}</span></span>'
            )
        body.write("</div>")
    body.write("</div>")

    # Conviction x outcome breakdown table.
    _decision_conviction_outcome_panel(body, history)

    # Full ledger.
    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Decision ledger</span>'
        f'<span class="panel-sub">{len(history.rows)} row'
        f"{'s' if len(history.rows) != 1 else ''} · newest first</span></div>"
        '<div class="table-scroll"><table class="fin-table"><thead><tr>'
        "<th>Made</th>"
        "<th>Kind</th>"
        "<th>Conviction</th>"
        '<th class="num">Outcome %</th>'
        "<th>Rationale</th>"
        "</tr></thead><tbody>"
    )
    for r in history.rows:
        if r.outcome_pct is not None:
            cls = (
                "num pos"
                if (
                    (r.recommendation_kind.upper() in ("TRIM", "SELL") and r.outcome_pct < 0)
                    or (r.recommendation_kind.upper() not in ("TRIM", "SELL") and r.outcome_pct > 0)
                )
                else "num neg"
            )
            outcome_cell = f'<td class="{cls}">{r.outcome_pct * 100:+.1f}%</td>'
        else:
            outcome_cell = '<td class="num muted">—</td>'
        rationale = r.rationale_excerpt or "—"
        body.write("<tr>")
        body.write(f'<td class="mono xsmall">{_esc(r.made_at.strftime("%Y-%m-%d"))}</td>')
        body.write(f"<td><strong>{_esc(r.recommendation_kind.upper())}</strong></td>")
        body.write(f"<td>{_esc(r.conviction or '—')}</td>")
        body.write(outcome_cell)
        body.write(f'<td class="seg-desc">{_esc(rationale)}</td>')
        body.write("</tr>")
    body.write("</tbody></table></div></div>")
    body.write("</div>")


def _decision_conviction_outcome_panel(body: StringIO, history: DecisionHistorySummary) -> None:
    """Cross-tab of conviction × outcome bucket — surfaces whether "high"
    convictions actually grade out better than "medium" or "low" ones.

    Skipped silently when there are no rows with both conviction and a
    graded outcome (still leaves the summary chips and full ledger in
    place above).
    """
    buckets: dict[tuple[str, str], int] = {}
    convictions: set[str] = set()
    outcomes: set[str] = set()
    for r in history.rows:
        if r.conviction is None or r.outcome_pct is None:
            continue
        # ADD/HOLD: positive return = win; TRIM/SELL: negative return = win.
        is_inverse = r.recommendation_kind.upper() in ("TRIM", "SELL")
        won = (r.outcome_pct < 0) if is_inverse else (r.outcome_pct > 0)
        outcome_label = "correct" if won else "wrong"
        key = (r.conviction, outcome_label)
        buckets[key] = buckets.get(key, 0) + 1
        convictions.add(r.conviction)
        outcomes.add(outcome_label)
    if not buckets:
        return
    conv_order = sorted(convictions, key=lambda c: {"high": 0, "medium": 1, "low": 2}.get(c, 9))
    outcome_order = ["correct", "wrong"]
    body.write(
        '<div class="panel"><div class="panel-head">'
        f'<span class="panel-title">Conviction {_TIMES} outcome</span>'
        '<span class="panel-sub">graded decisions only</span></div>'
        '<table class="metrics-table"><thead><tr>'
        "<th>Conviction</th>"
    )
    for out in outcome_order:
        body.write(f'<th class="num">{_esc(out.title())}</th>')
    body.write('<th class="num">Hit rate</th>')
    body.write("</tr></thead><tbody>")
    for conv in conv_order:
        body.write(f"<tr><td><strong>{_esc(conv)}</strong></td>")
        n_correct = buckets.get((conv, "correct"), 0)
        n_wrong = buckets.get((conv, "wrong"), 0)
        total = n_correct + n_wrong
        for out in outcome_order:
            n = buckets.get((conv, out), 0)
            body.write(f'<td class="num">{n}</td>')
        rate = (n_correct / total) if total else None
        if rate is not None:
            tone = "pos" if rate >= 0.5 else "neg"
            body.write(f'<td class="num {tone}">{rate * 100:.0f}%</td>')
        else:
            body.write('<td class="num muted">—</td>')
        body.write("</tr>")
    body.write("</tbody></table></div>")


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


def _thesis_hygiene_panels(
    body: StringIO, thesis: ThesisSection, report_date: date | None = None
) -> None:
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

    # Full KPI ledger — definitions + parsed data, not a bare list. Each row
    # carries a clean name + definition gloss, a sparkline + YoY/QoQ delta off
    # the loaded history, and a staleness flag. Tier-2/3 rows with zero facts
    # collapse into a "tracked, no data yet" footnote so the table isn't padded
    # with empty rows. Show open by default (high signal, low cost).
    if thesis.kpi_ledger:
        # Sort: tier-1 first, then tier-2, then tier-3 — preserves analyst
        # priority in the visible list.
        tier_rank = {"tier_1": 0, "tier_2": 1, "tier_3": 2}
        rows_sorted = sorted(thesis.kpi_ledger, key=lambda r: (tier_rank.get(r.tier, 9), r.name))
        # Keep every tier-1 row (its emptiness is itself thesis signal) but pull
        # zero-fact tier-2/3 rows out of the table into the footnote.
        shown = [r for r in rows_sorted if r.tier == "tier_1" or _kpi_has_data(r)]
        tracked_only = [r for r in rows_sorted if r.tier != "tier_1" and not _kpi_has_data(r)]
        summary = f"KPI ledger detail · {len(shown)} tracked"
        if tracked_only:
            summary += f", {len(tracked_only)} awaiting data"
        body.write(
            '<details class="thesis-ledger-details" open><summary>'
            f"{_esc(summary)}</summary>"
            '<div class="table-scroll"><table class="fin-table kpi-ledger-table"><thead><tr>'
            "<th>KPI</th><th>Tier</th><th>Unit</th>"
            '<th class="num">Latest</th><th>Trend</th>'
            "<th>Status</th><th>Break condition</th><th>Source</th>"
            "</tr></thead><tbody>"
        )
        for r in shown:
            _kpi_ledger_row(body, r, report_date)
        body.write("</tbody></table></div>")
        if tracked_only:
            names = ", ".join(_esc(clean_kpi_name(r.name)) for r in tracked_only)
            body.write(
                '<div class="ledger-tracked-only muted xsmall">'
                f"<strong>Tracked, no data yet</strong> ({len(tracked_only)}): {names}"
                "</div>"
            )
        body.write("</details>")


def _kpi_has_data(row: KpiLedgerRow) -> bool:
    return any(v is not None for _, v in row.history)


def _kpi_ledger_row(body: StringIO, r: KpiLedgerRow, report_date: date | None) -> None:
    """One enriched ledger row: clean name + definition gloss, latest value with
    a staleness flag, and a sparkline + YoY/QoQ delta off ``r.history``."""
    status_cls = {
        "green": "pos",
        "yellow": "warn",
        "red": "neg",
        "unknown": "muted",
    }.get(r.current_status, "muted")

    # Latest value cell — `value (period)` when history exists, with a `stale`
    # flag when the latest fact predates the report by more than ~2 quarters so
    # a year-old number doesn't read as current.
    stale = False
    if r.history:
        period_label, value = r.history[-1]
        latest_text = format_ledger_value(value, r.unit, r.name) if value is not None else "—"
        latest_html = (
            f'{_esc(latest_text)} <span class="muted xsmall">{_esc(period_label[:7])}</span>'
        )
        if (
            report_date is not None
            and value is not None
            and kpi_is_stale(period_label, report_date)
        ):
            stale = True
            latest_html += (
                ' <span class="ledger-stale" title="Latest fact is older than ~2 quarters '
                '— may not reflect the current period">stale</span>'
            )
    else:
        latest_html = '<span class="muted">—</span>'

    # Trend cell — sparkline + YoY/QoQ delta. Needs ≥2 real observations.
    values = [v for _, v in r.history if v is not None]
    if len(values) >= 2:
        spark = sparkline(values, width=84, height=22)
        label, sign = kpi_trend_delta(r.history, r.unit, r.name)
        delta_html = f' <span class="ledger-delta {sign}">{_esc(label)}</span>' if label else ""
        trend_html = f'<span class="ledger-spark">{spark}</span>{delta_html}'
    elif len(values) == 1:
        trend_html = '<span class="muted xsmall">single obs</span>'
    else:
        trend_html = '<span class="muted">—</span>'

    tooltip_attr = f' title="{_esc(r.latest_source_excerpt)}"' if r.latest_source_excerpt else ""
    row_cls = "kpi-ledger-row" + (" ledger-stale-row" if stale else "")

    # KPI cell — clean (de-parenthesized) name, with the qualifier demoted to a
    # muted definition line so the name reads cleanly and the definition isn't
    # duplicated inside it.
    name_html = f"<strong>{_esc(clean_kpi_name(r.name))}</strong>"
    if r.definition:
        name_html += f'<div class="ledger-def muted xsmall">{_esc(r.definition)}</div>'

    body.write(
        f'<tr class="{row_cls}" data-commentable="true" data-anchor-type="kpi_ledger_row" '
        f'data-anchor-key="{_esc(r.name)}" data-anchor-tab="thesis">'
        f"<td>{name_html}</td>"
        f"<td>{_esc(r.tier.replace('_', ' '))}</td>"
        f"<td>{_esc(r.unit or '')}</td>"
        f'<td class="num"{tooltip_attr}>{latest_html}</td>'
        f'<td class="ledger-trend">{trend_html}</td>'
        f'<td class="num {status_cls}">{_esc(r.current_status.upper())}</td>'
        f"<td>{_esc(r.break_condition or '')}</td>"
        f"<td>{_esc(r.source_hint or '')}</td></tr>"
    )


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
    if v.sheet_url:
        # A Google Sheet is linked (holdings dcf_defaults.gsheet_id) — point
        # straight at it and label it as such. The direct Sheet URL resolves both
        # served and as a file:// page (unlike the /dcf/<T> route below), and the
        # explicit label is the visible cue that an editable model exists — the
        # bare "Open .xlsx" gave no sign a Sheet was even there.
        body.write(
            '<div class="val-row muted"><span>DCF model</span>'
            f'<strong><a href="{_esc(v.sheet_url)}" target="_blank" '
            'rel="noopener">Open in Google Sheets ↗</a></strong></div>'
        )
    elif v.model_link:
        # No linked Sheet — fall back to the live /dcf/<ticker> route (served by
        # comments_server) instead of the bare relative filename: a report served
        # over HTTP at /reports/<ticker> can't resolve a sibling .xlsx path, so the
        # old link 404'd in the served app. The route streams dcf/<T>.xlsx (latest
        # dated workbook as fallback). NOTE: opening the report as a file:// page
        # needs the server running for this link to resolve.
        body.write(
            '<div class="val-row muted"><span>DCF workbook</span>'
            f'<strong><a href="/dcf/{_esc(snap.ticker)}">Open .xlsx</a>'
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
        status_label = {
            "ok": "OK",
            "warn": "WARN",
            "breach": "BREACH",
            "unresolved": "UNRESOLVED",
        }.get(r.status, r.status)
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
        f"{'s' if len(decisions) != 1 else ''}</span></div>"
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


def _macro_sensitivity_panel(body: StringIO, rows: list[MacroSensitivityRow]) -> None:
    """P3-18 macro factor sensitivity table — β / R² / lookback per series.

    Renders an empty-state callout instead of hiding so analysts notice when
    the macro_sensitivities table has no rows for this ticker. Rows are
    pre-sorted by ``|beta|`` descending by ``load_macro_sensitivities``.
    """
    body.write(
        '<div class="panel macro-sens-panel"><div class="panel-head">'
        '<span class="panel-title">Macro factor sensitivity</span>'
    )
    if rows:
        body.write(
            f'<span class="panel-sub">{len(rows)} factor'
            f"{'s' if len(rows) != 1 else ''} "
            f"· lookback {rows[0].lookback_window_days}d</span>"
        )
    else:
        body.write('<span class="panel-sub">no factors tracked</span>')
    body.write("</div>")
    if not rows:
        body.write(
            '<div class="stub"><span class="stub-label">cold ticker</span>'
            "No macro_sensitivities rows for this ticker. Run the macro β "
            "backfill (alembic 0045 must be applied) to populate.</div></div>"
        )
        return
    body.write(
        '<table class="metrics-table"><thead><tr>'
        "<th>Macro factor</th>"
        '<th class="num">β</th>'
        '<th class="num">R²</th>'
        '<th class="num">Lookback</th>'
        "<th>Computed</th>"
        "</tr></thead><tbody>"
    )
    for r in rows:
        body.write("<tr>")
        body.write(f"<td>{_esc(_macro_series_label(r.series_id))}</td>")
        body.write(f'<td class="num {_macro_beta_tone(r.beta)}">{r.beta:+.2f}</td>')
        r2 = (
            f"{r.r_squared:.2f}".lstrip("0")
            if r.r_squared is not None
            else '<span class="muted">—</span>'
        )
        body.write(f'<td class="num">{r2}</td>')
        body.write(f'<td class="num">{r.lookback_window_days}d</td>')
        body.write(f'<td class="mono xsmall">{_esc(r.computed_at.strftime("%Y-%m-%d"))}</td>')
        body.write("</tr>")
    body.write("</tbody></table></div>")


def _macro_series_label(series_id: str) -> str:
    """Friendly label for a macro series id (``10y_treasury`` -> ``10Y Treasury``)."""
    mapping = {
        "fed_funds_rate": "Fed funds rate",
        "10y_treasury": "10Y Treasury",
        "2y_treasury": "2Y Treasury",
        "usd_index": "USD index (DXY)",
        "wti_crude": "WTI crude",
        "vix": "VIX",
        "cpi_yoy": "CPI YoY",
        "unemployment_rate": "Unemployment rate",
    }
    return mapping.get(series_id, series_id.replace("_", " ").title())


def _macro_beta_tone(beta: float) -> str:
    """Color magnitude: strong |β|>0.5 stays accented, weak |β|<0.2 mutes."""
    abs_b = abs(beta)
    if abs_b >= 0.5:
        return "neg" if beta < 0 else "pos"
    if abs_b < 0.2:
        return "muted"
    return ""


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


def _eval_tab(
    body: StringIO,
    eval_snap: EvaluationSnapshotSection,
    peer_comp: list[PeerCompRow] | None = None,
) -> None:
    """Render the 3y quick-categorization data table for new-name screening.

    Only added to the tab list when ``spec.flavor == ReportFlavor.EVALUATION``
    and the evaluation snapshot is populated. Mirrors what the legacy renderer
    surfaces in §1 for the evaluation flavor — abs / margin / ratio metrics
    across LFY-2 / LFY-1 / LFY / TTM with a 3y CAGR column for absolute series.

    Followed by a peer-comp table when ``peer_comp`` is non-empty — gives
    the screen "premium to peers" context the legacy snapshot lacks.
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
        _peer_comp_panel(body, peer_comp or [])
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

    _peer_comp_panel(body, peer_comp or [])

    body.write("</div>")


def _peer_comp_panel(body: StringIO, rows: list[PeerCompRow]) -> None:
    """Peer comparison table for the Eval Screen — market cap, revenue,
    net margin, ROIC. Source: cached FMP peers + per-peer key-metrics-ttm
    JSONs under ``data/historical/fmp/``.

    Empty-state callout when the peers JSON isn't on disk yet so a cold
    evaluation run still shows the slot.
    """
    body.write(
        '<div class="panel peer-comp-panel"><div class="panel-head">'
        '<span class="panel-title">Peer comparison</span>'
    )
    if rows:
        body.write(
            f'<span class="panel-sub">{len(rows)} peer'
            f"{'s' if len(rows) != 1 else ''} · TTM key metrics from FMP</span></div>"
        )
    else:
        body.write('<span class="panel-sub">peers cache cold</span></div>')
        body.write(
            '<div class="stub"><span class="stub-label">no peers on disk</span>'
            "No <code>{TICKER}_peers.json</code> in <code>data/historical/fmp/</code>. "
            "Run the FMP peers fetch to populate, then re-render.</div></div>"
        )
        return
    body.write(
        '<table class="fin-table"><thead><tr>'
        "<th>Ticker</th>"
        "<th>Name</th>"
        '<th class="num">Market cap</th>'
        '<th class="num">Revenue TTM</th>'
        '<th class="num">Net margin TTM</th>'
        '<th class="num">ROIC TTM</th>'
        "</tr></thead><tbody>"
    )
    for r in rows:
        body.write("<tr>")
        body.write(f'<td><strong class="mono">{_esc(r.peer_ticker)}</strong></td>')
        body.write(f"<td>{_esc(r.peer_name or '—')}</td>")
        body.write(
            f'<td class="num">{_fmt_usd_compact(r.market_cap_usd)}</td>'
            if r.market_cap_usd is not None
            else '<td class="num muted">—</td>'
        )
        body.write(
            f'<td class="num">{_fmt_usd_compact(r.revenue_ttm_usd)}</td>'
            if r.revenue_ttm_usd is not None
            else '<td class="num muted">—</td>'
        )
        body.write(
            f'<td class="num">{r.net_margin_ttm * 100:.1f}%</td>'
            if r.net_margin_ttm is not None
            else '<td class="num muted">—</td>'
        )
        body.write(
            f'<td class="num">{r.roic_ttm * 100:.1f}%</td>'
            if r.roic_ttm is not None
            else '<td class="num muted">—</td>'
        )
        body.write("</tr>")
    body.write("</tbody></table></div>")


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
    strategic_targets: list[StrategicTargetRow] | None = None,
    customer_concentrations: list[CustomerConcentrationRow] | None = None,
    lease_ladder: list[LeaseLadderRow] | None = None,
    suppressed_sections: frozenset[str] | None = None,
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

    # P3 panels (strategic targets / customer concentrations / lease ladder)
    # render an empty-state when no rows so the analyst sees the slot rather
    # than wondering whether the extractor ran -- UNLESS the panel is
    # structurally irrelevant to this company's business model (e.g. a bank has
    # no operating-lease ladder), in which case the builder lists its key in
    # `suppressed_sections` and we omit it entirely instead of showing an empty
    # stub. See industry_classifier.suppressed_sections_for_ticker.
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
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">IR documents</span>'
            f'<span class="panel-sub">{len(ir.cards)} on file</span></div>'
        )
        _quarter_selector(body, labels, group="ir")
        for c in ordered:
            qid = f"{c.quarter} {c.year}"
            display = "" if qid == active else "display:none"
            body.write(
                f'<div class="ir-card" data-quarter-card data-quarter-group="ir" '
                f'data-quarter="{_esc(qid)}" style="{display}"><div class="ir-card-head">'
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
        body.write(
            f'<div class="prose-pad">{_render_markdown(section.raw_synthesis_md)}</div></div>'
        )

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
                f"<p><strong>Targets:</strong> {_esc(comp.targets_and_thresholds or '—')}</p>"
            )
            body.write(
                '<p style="margin-top: 10px; font-style: italic;">'
                f"<strong>Thesis alignment:</strong> {_esc(comp.alignment_verdict or '—')}</p>"
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
        body.write(
            f"<p>{_esc(metric.description or 'No operational/financial metric redefinitions detected.')}</p>"
        )
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
            body.write(
                f'<td><span class="pill pill-{tone}">{_esc(sig.severity.upper())}</span></td>'
            )
            body.write(f'<td class="saydo-guide">{_esc(sig.description)}</td>')
            body.write("</tr>")
        body.write("</tbody></table></div>")


def _strategic_targets_panel(body: StringIO, rows: list[StrategicTargetRow]) -> None:
    """P3-20 strategic targets table — long-term mgmt commitments from decks.

    Empty-state callout when the extractor hasn't seen any decks for this
    ticker so the slot stays visible.
    """
    body.write(
        '<div class="panel strategic-targets-panel"><div class="panel-head">'
        '<span class="panel-title">Strategic targets</span>'
    )
    if rows:
        body.write(
            f'<span class="panel-sub">{len(rows)} long-term '
            f"commitment{'s' if len(rows) != 1 else ''} · from investor decks</span></div>"
        )
    else:
        body.write('<span class="panel-sub">no decks extracted</span></div>')
        body.write(
            '<div class="stub"><span class="stub-label">cold ticker</span>'
            "No strategic_targets rows for this ticker. The investor-deck "
            "extractor (alembic 0053) hasn't populated long-term commitments "
            "yet — run it against the latest investor presentation.</div></div>"
        )
        return
    body.write(
        '<table class="metrics-table"><thead><tr>'
        "<th>Target</th>"
        '<th class="num">Value</th>'
        "<th>Period</th>"
        '<th class="num">Conf.</th>'
        "<th>Source excerpt</th>"
        "</tr></thead><tbody>"
    )
    for r in rows:
        body.write("<tr>")
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
    body.write("</tbody></table></div>")


def _customer_concentration_panel(body: StringIO, rows: list[CustomerConcentrationRow]) -> None:
    """P3-19a customer concentration table — named customers ≥ 5% of revenue.

    Empty-state when none reported (most large-cap diversified businesses).
    Accessor filters out sub-5% rows so this is "material concentration only".
    """
    body.write(
        '<div class="panel customer-concentration-panel"><div class="panel-head">'
        '<span class="panel-title">Customer concentration</span>'
    )
    if rows:
        body.write(
            f'<span class="panel-sub">{len(rows)} customer'
            f"{'s' if len(rows) != 1 else ''} ≥ 5% of revenue</span></div>"
        )
    else:
        body.write('<span class="panel-sub">none ≥ 5% reported</span></div>')
        body.write(
            '<div class="stub"><span class="stub-label">no material concentration</span>'
            "No named customer represents ≥ 5% of revenue in disclosure. "
            "(Either truly diversified, or the customer-concentration extractor "
            "hasn't run for this ticker yet — alembic 0040.)</div></div>"
        )
        return
    body.write(
        '<table class="metrics-table"><thead><tr>'
        "<th>Period</th>"
        "<th>Customer</th>"
        '<th class="num">% of revenue</th>'
        '<th class="num">Revenue</th>'
        "</tr></thead><tbody>"
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
        body.write("<tr>")
        body.write(f'<td class="mono xsmall">{_esc(period)}</td>')
        body.write(f"<td><strong>{_esc(r.customer_label)}</strong></td>")
        body.write(
            f'<td class="num">{share_pct:.1f}%'
            f' <span class="seg-bar" style="width:{bar_w:.1f}px"></span></td>'
        )
        body.write(f'<td class="num">{rev_cell}</td>')
        body.write("</tr>")
    body.write("</tbody></table></div>")


def _lease_ladder_panel(body: StringIO, rows: list[LeaseLadderRow]) -> None:
    """P3-19b lease maturity ladder — Y1..Y5..Thereafter for the latest FY.

    Accessor pre-orders rows Y1..Thereafter then total/imputed/liability.
    Empty-state when no rows exist for the ticker.
    """
    body.write(
        '<div class="panel lease-ladder-panel"><div class="panel-head">'
        '<span class="panel-title">Operating lease maturity ladder</span>'
    )
    if rows:
        fy = rows[0].fiscal_year
        unit = rows[0].unit
        curr = rows[0].currency
        body.write(
            f'<span class="panel-sub">FY{fy} · as of '
            f"{rows[0].as_of_date.isoformat()} · {_esc(curr)} {_esc(unit)}</span></div>"
        )
    else:
        body.write('<span class="panel-sub">no ladder on file</span></div>')
        body.write(
            '<div class="stub"><span class="stub-label">cold ticker</span>'
            "No lease_commitments rows for this ticker. The 10-K lease-ladder "
            "extractor (alembic 0047) hasn't populated yet — run it against "
            "the latest annual filing.</div></div>"
        )
        return
    body.write(
        '<table class="metrics-table"><thead><tr>'
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
        f'<div class="panel"><div class="panel-head">'
        f'<span class="panel-title">{_esc(title)}</span>'
        f'<span class="panel-sub">latest period</span></div>'
        '<table class="seg-list"><thead><tr>'
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
    body.write(
        f'<h2 class="section-title">Executive comp &amp; insider activity · {_esc(yr_label)}</h2>'
    )
    body.write("</div></div>")

    # 1. Alignment narrative
    if section.alignment_narrative_md:
        body.write(
            '<div class="panel"><div class="panel-head">'
            '<span class="panel-title">Alignment read</span>'
            "<span class=\"panel-sub\">Do management's comp metrics reward the analyst's thesis?</span>"
            "</div>"
        )
        body.write(
            f'<div class="prose-pad">{_render_markdown(section.alignment_narrative_md)}</div></div>'
        )
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
                bonus_pair += (
                    f' <span class="muted">/ {_fmt_usd_short(pkg.cash_bonus_target)} tgt</span>'
                )
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
                f"<strong>{ceo.ceo_pay_ratio:.0f}x</strong> median employee comp "
                f"(S&amp;P 500 average ~300x).</td></tr>"
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
            body.write(f"<td>{_esc(s.transaction_date)}</td>")
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
    "five_min_reread": (
        "5-min reread",
        "What changed · recommended action · what would change my mind",
    ),
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
            f"{section.ticker if section else '<TICKER>'} --all</pre></div>"
        )
        body.write("</div>")
        return

    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Cross-section synthesis</div>')
    body.write(f'<h2 class="section-title">Synthesis · {len(section.lenses)} lens artifacts</h2>')
    body.write(
        '<p class="sub">Cached cross-section analytical reads. '
        f"Regenerate with <code>python execution/run_lens.py --ticker {_esc(section.ticker)} --all</code>.</p>"
    )
    body.write("</div></div>")

    for idx, lens in enumerate(section.lenses):
        label, sub = _LENS_LABELS.get(lens.name, (lens.name, ""))
        is_first = idx == 0
        age_str = ""
        if lens.generated_at:
            from datetime import UTC as _UTC
            from datetime import datetime as _dt

            age = _dt.now(_UTC) - lens.generated_at
            if age.days >= 1:
                age_str = f" · {age.days}d ago"
            else:
                age_str = f" · {int(age.total_seconds() / 3600)}h ago"
        warn = (
            ' <span class="lens-warn">DIRTY</span>'
            if lens.is_dirty
            else (' <span class="lens-stale">STALE</span>' if lens.is_stale else "")
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
    if pp.position_as_of is not None:
        body.write(
            f'<div class="panel-sub">Tracker snapshot as of '
            f"{_esc(pp.position_as_of.isoformat())} (read at report build)</div>"
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


def _sources_tab(
    body: StringIO,
    prov: ProvenanceSection,
    app: AppendixSection,
    repo_root: str,
) -> None:
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

    _prompt_quality_panel(body, Path(repo_root) / "data" / "portfolio.db")
    body.write("</div>")


def _prompt_quality_panel(body: StringIO, db_path: Path) -> None:
    """Surface the prompt_calibration_scores table — grouped by (purpose,
    prompt_version) so the analyst can see whether each prompt version is
    improving over time without spelunking the per-call ledger.

    The graders (grade_bear_cases, grade_decisions) write the rows; this
    panel is the consumer side. Best-effort: missing DB / empty table
    degrades to a small "no data yet" stub rather than breaking the page.
    """
    window_days = 30
    # Stored scored_at is naive UTC ISO; compare with a naive cutoff so the
    # SQL string comparison is well-defined.
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=window_days)
    summaries: list[VersionSummary] = summarize_by_prompt_version(db_path=db_path, since=since)

    body.write(
        '<div class="panel"><div class="panel-head">'
        '<span class="panel-title">Prompt quality</span>'
        f'<span class="panel-sub">last {window_days} days · '
        "grader-scored, grouped by prompt version</span></div>"
    )

    if not summaries:
        body.write(
            '<div class="muted" style="padding:8px 12px">'
            "No calibration data yet — run "
            "<code>python execution/grade_bear_cases.py</code> or "
            "<code>python execution/grade_decisions.py</code> to populate."
            "</div></div>"
        )
        return

    daily = daily_avg_scores(db_path=db_path, since=since)
    body.write(
        '<div class="table-scroll"><table class="fin-table"><thead><tr>'
        "<th>Purpose</th><th>Version</th>"
        '<th class="num">n</th>'
        '<th class="num">avg</th>'
        '<th class="num">p25</th>'
        '<th class="num">p50</th>'
        '<th class="num">p75</th>'
        "<th>Last scored</th>"
        "<th>30d trend</th>"
        "</tr></thead><tbody>"
    )
    for s in summaries:
        spark_vals = [v for _, v in daily.get((s.purpose, s.prompt_version), [])]
        spark = sparkline(spark_vals, width=120, height=24) if spark_vals else "—"
        last = (s.last_scored_at or "—")[:19].replace("T", " ")
        body.write(
            "<tr>"
            f"<td>{_esc(s.purpose)}</td>"
            f"<td>{_esc(s.prompt_version)}</td>"
            f'<td class="num">{s.score_count}</td>'
            f'<td class="num">{s.avg_score:.3f}</td>'
            f'<td class="num">{s.p25:.3f}</td>'
            f'<td class="num">{s.p50:.3f}</td>'
            f'<td class="num">{s.p75:.3f}</td>'
            f"<td>{_esc(last)}</td>"
            f"<td>{spark}</td>"
            "</tr>"
        )
    body.write("</tbody></table></div></div>")


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
    body.write(f'<script id="workspace-boot" type="application/json">{_json.dumps(boot)}</script>')
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
        f'<script id="workspace-comments" type="application/json">{_json.dumps(payload)}</script>'
    )


def _comment_sidebar_shell(body: StringIO) -> None:
    """Static sidebar shell — flex sibling of .l1-root.

    The push-sidebar layout requires this `<aside>` to be a direct child
    of `<body>` (which is `display: flex; flex-direction: row`). Emitting
    it at render time makes that relationship explicit in the markup;
    the JS only toggles `.open` and writes into `#cmt-list` /
    `#cmt-anchor-label`. No outside-click dismissal — close via the ×
    button or Escape.
    """
    body.write(
        '<aside class="cmt-sidebar" id="cmt-sidebar" aria-hidden="true">'
        '<header class="cmt-sidebar-head">'
        "<div>"
        '<div class="cmt-sidebar-title">Comments</div>'
        '<div class="cmt-sidebar-sub" id="cmt-anchor-label"></div>'
        "</div>"
        '<button class="cmt-close" type="button" aria-label="close">&times;</button>'
        "</header>"
        '<div class="cmt-list" id="cmt-list"></div>'
        '<form class="cmt-form" id="cmt-form">'
        '<textarea name="comment" rows="3" required '
        'placeholder="Write a comment&hellip; '
        "(tip: prefix with /kpi /thesis /q /ask /fix /update /rewrite "
        'to skip auto-classify)"></textarea>'
        '<div class="cmt-form-row">'
        '<select name="intent" title="What should the processor do?">'
        '<option value="">Auto-classify</option>'
        '<option value="drop_kpi">Drop this KPI</option>'
        '<option value="edit_thesis">Edit thesis</option>'
        '<option value="ask_question">Ask question</option>'
        '<option value="fix_data">Flag data issue</option>'
        '<option value="rewrite_section">Rewrite this section</option>'
        "</select>"
        '<button type="submit">Post</button>'
        "</div>"
        '<div class="cmt-form-hint" id="cmt-form-hint"></div>'
        "</form>"
        "</aside>"
    )


def _chat_drawer_shell(body: StringIO, ticker: str, report_date: str) -> None:
    """Chat shell — a push-sidebar (`.chat-sidebar`) plus a fixed launcher
    (`.chat-drawer`).

    The panel is a flex sibling of `.l1-root`, mirroring
    `_comment_sidebar_shell`: opening it slides the document aside rather
    than floating an overlay. The chat JS toggles `.open`, sets
    `--sidebar-open-width`, and enforces one-open-at-a-time with the
    comments sidebar. The launcher pill stays `position: fixed` and rides
    the open sidebar's left edge.
    """
    body.write(
        '<aside class="chat-sidebar" id="chat-sidebar" aria-hidden="true">'
        '<header class="chat-head">'
        "<div>"
        f'<div class="chat-title">Ask Claude about {_esc(ticker)}</div>'
        f'<div class="chat-sub">{_esc(report_date)} '
        "&middot; streams from comments_server</div>"
        "</div>"
        '<button class="chat-close" type="button" aria-label="close">&times;</button>'
        "</header>"
        '<div class="chat-thread" id="chat-thread"></div>'
        '<form class="chat-form" id="chat-form">'
        '<textarea name="message" rows="3" required '
        'placeholder="Ask about a KPI, propose an edit, '
        'look up a quote in the transcript&hellip;"></textarea>'
        '<div class="chat-form-row">'
        '<span class="chat-hint" id="chat-hint">Cmd+Enter to send</span>'
        '<button type="submit">Send</button>'
        "</div>"
        "</form>"
        "</aside>"
        '<aside class="chat-drawer" id="chat-drawer">'
        '<button class="chat-toggle" id="chat-toggle" type="button" aria-label="Open chat">'
        '<span class="chat-toggle-icon">&#8984;</span>'
        '<span class="chat-toggle-label">Chat</span>'
        "</button>"
        "</aside>"
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
    is_budget = status == SectionStatus.BUDGET_SKIPPED
    title = "⏭ Forgone — budget" if is_budget else f"Section status: {status.value}"
    body.write('<div class="panel"><div class="panel-head">')
    body.write(f'<span class="panel-title">{title}</span>')
    body.write("</div>")
    stub_style = ' style="border-left:3px solid #d97706;"' if is_budget else ""
    body.write(f'<div class="stub"{stub_style}><span class="stub-label">{status.value}</span>')
    if missing is not None:
        body.write(_esc(missing.detail or "No data."))
        if missing.fix_command:
            body.write(f'<br><span class="mono">{_esc(missing.fix_command)}</span>')
    else:
        body.write("Section returned no data.")
    body.write("</div></div>")


def _forgone_strip(body: StringIO, forgone: list[BudgetSkip]) -> None:
    """Brief-wide banner listing the analyses forgone to stay under budget."""
    if not forgone:
        return
    n = len(forgone)
    word = "analysis" if n == 1 else "analyses"
    names = _esc(", ".join(f.section for f in forgone))
    body.write(
        '<div class="forgone-strip" style="margin:8px 0;padding:8px 12px;'
        "border-left:3px solid #d97706;background:rgba(217,119,6,0.10);"
        'border-radius:6px;font-size:13px;line-height:1.5;">'
        f"⏭ <strong>{n} {word} forgone to stay under budget:</strong> {names}. "
        "Raise the cap or override, then rebuild."
        "</div>"
    )


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
