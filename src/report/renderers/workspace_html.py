"""ReportSpec → workspace HTML research doc.

Implements the Anthropic-design ``GOOG Workspace.html`` (tabbed editorial
workspace, monochrome palette, paper/white/dark themes) as a single self-
contained HTML file. Same contract as ``html.py`` — no runtime JS framework,
no CDN dependencies beyond the Google Fonts stylesheet.

Every section of the ReportSpec has a home. UX9 folds the original 12-14
section tabs into ~6 grouped top-level tabs (Overview / Quarter / Financials
/ Research / Position / Sources); inside a multi-section group a slim
sub-tab pill row switches the section panes client-side. Section panes keep
their legacy per-section ``data-tab`` ids so saved comment anchors,
``data-xtab`` cross-links and ``#tab=<id>`` deep links resolve unchanged.
Sections with no data HIDE rather than stub (P4.2 hide-don't-stub); the
Governance coverage matrix carries the gap inventory.

S13 split: the per-section renderers live in ``workspace_sections/`` (one
module per tab group + ``_shared`` helpers + ``chrome``/``boot``); this
module keeps the public entry point (``render``), the document shell, and
the tab scaffold. ``tests/test_workspace_golden.py`` pins the rendered
output byte-for-byte across both.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from pipeline.cc_overlay import CC_OVERLAY_JS
from report.models import ReportFlavor, ReportSpec
from report.renderers.charts_v2 import CSS as CHARTS_V2_CSS
from report.renderers.workspace_chat import CSS as CHAT_CSS
from report.renderers.workspace_chat import JS as CHAT_JS
from report.renderers.workspace_comments import CSS as COMMENTS_CSS
from report.renderers.workspace_comments import JS as COMMENTS_JS
from report.renderers.workspace_data import WorkspaceP3Panels, load_workspace_p3_panels
from report.renderers.workspace_dcf import CSS as DCF_CSS
from report.renderers.workspace_dcf import JS as DCF_JS
from report.renderers.workspace_script import JS

# Section renderers + shared helpers: moved to workspace_sections/ in the
# S13 split. Re-imported here (and listed in __all__) so every historical
# `from report.renderers.workspace_html import _x` keeps resolving; new
# code should import from the section modules directly.
from report.renderers.workspace_sections._shared import (
    _STATUS_EMPTY_REASON,
    TabDef,
    TabGroup,
    TabRenderFn,
    _empty_panel,
    _esc,
    _fmt_pct,
    _fmt_price,
    _fmt_usd,
    _inline_md,
    _missing_panel,
    _panel_head,
    _quarter_selector,
    _render_markdown,
    _source_chip_html,
    _source_hover_title,
    _xlink_html,
)
from report.renderers.workspace_sections.boot import (
    _chat_drawer_shell,
    _comment_boot_data,
    _comment_sidebar_shell,
)
from report.renderers.workspace_sections.chrome import (
    _footer,
    _forgone_strip,
    _identity,
    _kpi_strip,
    _kpi_tile,
    _news_tab,
    _news_tile,
    _open_items_strip,
    _thesis_strip,
    _val_stat,
    _verdict_badge,
)
from report.renderers.workspace_sections.company import (
    _company_tab,
    _customer_concentration_panel,
    _lease_bucket_label,
    _lease_ladder_panel,
    _render_filing_intelligence,
    _segment_breakdown_panel,
    _strategic_targets_panel,
)
from report.renderers.workspace_sections.earnings import (
    _beat_rate_scorecard_panel,
    _earnings_narrative_panel,
    _earnings_tab,
    _earnings_themes_panel,
    _financial_highlights_panel,
    _financials_for_card,
    _find_qa_quarter,
    _fmt_line_value,
    _growth_pair_cell,
    _pos_of_card,
    _qa_roster_panel_for_quarter,
    _qa_row,
    _source_for_display_index,
    _surprise_tone,
    _theme_list,
    _ws_period_sort_key,
)
from report.renderers.workspace_sections.eval_screen import (
    _eval_cell,
    _eval_screen_panels,
    _fmt_usd_compact,
    _peer_comp_panel,
)
from report.renderers.workspace_sections.exec_comp import _exec_comp_tab, _fmt_usd_short
from report.renderers.workspace_sections.financials import (
    _SIGNALS_SEVERITIES,
    _annual_kpi_series_yoy_panel,
    _financials_tab,
    _growth_cell,
    _kpi_series_yoy_panel,
    _line_items_levels_panel,
    _line_items_yoy_panel,
    _segment_drill_table,
    _segment_secondary_expansions_panel,
    _segments_real,
    _segments_yoy_panel,
    _segments_yoy_panel_for_metric,
    _signal_card_workspace,
    _signal_severity_class,
    _signals_panel,
    _signals_table_workspace,
    _sum_segments_at,
    _validation_panel,
)
from report.renderers.workspace_sections.position import _position_stat, _position_tab
from report.renderers.workspace_sections.saydo import (
    _fmt_made_period,
    _outcome_pill,
    _rating_pill,
    _saydo_historical_ledger,
    _saydo_print_vs_guide,
    _saydo_summary_table,
    _saydo_tab,
    _saydo_verdicts_panel,
)
from report.renderers.workspace_sections.sources import _prompt_quality_panel, _sources_tab
from report.renderers.workspace_sections.synthesis import _LENS_LABELS, _synthesis_tab
from report.renderers.workspace_sections.thesis_risk import (
    _assumptions_sync_row,
    _bear_tab,
    _break_rules_panel,
    _decision_conviction_outcome_panel,
    _decision_history_panel,
    _decisions_tab,
    _failure_mode_card,
    _failure_modes_panel,
    _kpi_has_data,
    _kpi_ledger_row,
    _macro_beta_tone,
    _macro_sensitivity_panel,
    _macro_series_label,
    _most_underweighted_panel,
    _scenario_range_block,
    _soft_rules_panel,
    _summarize_rule,
    _thesis_hygiene_panels,
    _thesis_tab,
    _val_row,
    _valuation_summary_panel,
)
from report.renderers.workspace_sections.valuation import _TIMES, _valuation_tab
from report.renderers.workspace_styles import CSS
from ui.living_grid import head_assets as _living_grid_head_assets
from ui.source_chip import SOURCE_CHIP_JS
from ui.tokens import FAVICON_LINK

# Alpine + the livingGrid factory + grid CSS, inlined once into the report head.
# Self-contained (no asset fetches) so the report's living grids work offline
# (file://) — the same module the live shell loads (Wave 1).
_LIVING_GRID_HEAD = _living_grid_head_assets()

__all__ = [
    "_LENS_LABELS",
    "_SIGNALS_SEVERITIES",
    "_STATUS_EMPTY_REASON",
    "_TIMES",
    "TabDef",
    "TabGroup",
    "TabRenderFn",
    "_annual_kpi_series_yoy_panel",
    "_assumptions_sync_row",
    "_bear_tab",
    "_beat_rate_scorecard_panel",
    "_break_rules_panel",
    "_chat_drawer_shell",
    "_comment_boot_data",
    "_comment_sidebar_shell",
    "_company_tab",
    "_customer_concentration_panel",
    "_decision_conviction_outcome_panel",
    "_decision_history_panel",
    "_decisions_tab",
    "_earnings_narrative_panel",
    "_earnings_tab",
    "_earnings_themes_panel",
    "_empty_panel",
    "_esc",
    "_eval_cell",
    "_eval_screen_panels",
    "_exec_comp_tab",
    "_failure_mode_card",
    "_failure_modes_panel",
    "_financial_highlights_panel",
    "_financials_for_card",
    "_financials_tab",
    "_find_qa_quarter",
    "_fmt_line_value",
    "_fmt_made_period",
    "_fmt_pct",
    "_fmt_price",
    "_fmt_usd",
    "_fmt_usd_compact",
    "_fmt_usd_short",
    "_footer",
    "_forgone_strip",
    "_growth_cell",
    "_growth_pair_cell",
    "_identity",
    "_inline_md",
    "_kpi_has_data",
    "_kpi_ledger_row",
    "_kpi_series_yoy_panel",
    "_kpi_strip",
    "_kpi_tile",
    "_lease_bucket_label",
    "_lease_ladder_panel",
    "_line_items_levels_panel",
    "_line_items_yoy_panel",
    "_macro_beta_tone",
    "_macro_sensitivity_panel",
    "_macro_series_label",
    "_missing_panel",
    "_most_underweighted_panel",
    "_news_tab",
    "_news_tile",
    "_open_items_strip",
    "_outcome_pill",
    "_panel_head",
    "_peer_comp_panel",
    "_pos_of_card",
    "_position_stat",
    "_position_tab",
    "_prompt_quality_panel",
    "_qa_roster_panel_for_quarter",
    "_qa_row",
    "_quarter_selector",
    "_rating_pill",
    "_render_filing_intelligence",
    "_render_markdown",
    "_saydo_historical_ledger",
    "_saydo_print_vs_guide",
    "_saydo_summary_table",
    "_saydo_tab",
    "_saydo_verdicts_panel",
    "_scenario_range_block",
    "_segment_breakdown_panel",
    "_segment_drill_table",
    "_segment_secondary_expansions_panel",
    "_segments_real",
    "_segments_yoy_panel",
    "_segments_yoy_panel_for_metric",
    "_signal_card_workspace",
    "_signal_severity_class",
    "_signals_panel",
    "_signals_table_workspace",
    "_soft_rules_panel",
    "_source_chip_html",
    "_source_for_display_index",
    "_source_hover_title",
    "_sources_tab",
    "_strategic_targets_panel",
    "_sum_segments_at",
    "_summarize_rule",
    "_surprise_tone",
    "_synthesis_tab",
    "_theme_list",
    "_thesis_hygiene_panels",
    "_thesis_strip",
    "_thesis_tab",
    "_val_row",
    "_val_stat",
    "_validation_panel",
    "_valuation_summary_panel",
    "_valuation_tab",
    "_verdict_badge",
    "_ws_period_sort_key",
    "_xlink_html",
    "render",
]


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

    # P3 panel data (macro sensitivities, strategic targets, customer
    # concentrations, lease ladder, decision history, say-do verdicts,
    # peer comp, open notes) — pre-loaded once; the open-notes strip leads
    # the page (P4.4: builds lead with the owner's standing watch-items)
    # and the rest threads into the tab definitions.
    p3 = load_workspace_p3_panels(spec.ticker, Path(spec.repo_root))

    _open_items_strip(body, p3.open_notes)
    _kpi_strip(body, spec.thesis.kpi_ledger)

    body.write('<div class="l1-tabs-wrap">')
    groups = _tab_groups(spec, p3)
    _tabs(body, groups)
    for gi, (gid, _glabel, sections) in enumerate(groups):
        # First group is active on load so its pane renders immediately. The
        # tab BUTTON already gets `active` at gi==0 (see _tabs); without the
        # matching pane class every pane stays display:none until the user
        # clicks one, which is why the report opened blank on the active tab.
        g_active = " active" if gi == 0 else ""
        body.write(f'<div class="tab-group-pane{g_active}" data-tab-group="{_esc(gid)}">')
        if len(sections) > 1:
            _subtabs(body, sections)
        for si, (sid, _slabel, _scount, render_fn) in enumerate(sections):
            # Section panes keep the legacy per-section `data-tab` ids and the
            # `tab-pane` class: saved comment anchors (`tab: earnings`) and
            # data-xtab cross-links resolve against `[data-tab=...].tab-pane`.
            s_active = " active" if si == 0 else ""
            body.write(f'<div class="tab-pane subtab-pane{s_active}" data-tab="{_esc(sid)}">')
            render_fn(body)
            body.write("</div>")
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
<style>{DCF_CSS}</style>
{_LIVING_GRID_HEAD}
</head>
<body>
{body}
<script>{JS}</script>
<!-- The one transient-surface primitive (S4, Law 3), inlined again in this
     separate document so the chat + comments sidebars register against one
     open-surface stack (one Escape, one scrim listener) instead of the old
     cross-document window.__close* handshake. SOURCE_CHIP_JS gives the report's
     source-chip <details> Escape-only dismissal. -->
<script>{CC_OVERLAY_JS}</script>
<script>{SOURCE_CHIP_JS}</script>
<script>{COMMENTS_JS}</script>
<script>{CHAT_JS}</script>
<script>{DCF_JS}</script>
</body>
</html>
"""


def _tab_defs(spec: ReportSpec, p3: WorkspaceP3Panels) -> list[TabDef]:
    """Return every section tab as [(tab_id, label, count_or_None, render_fn), ...].

    This is the flat per-section inventory; ``_tab_groups`` folds it into the
    grouped top-level tabs (UX9) and owns ordering + flavor defaults. PR7: the
    old standalone "Eval Screen" tab folded into the Company tab (description
    first, numbers + comps right under the pitch), so evaluation lands on one
    coherent intro.
    """
    pos = spec.portfolio_position  # narrow for the closure
    eval_snap = spec.evaluation_snapshot
    is_eval = spec.flavor == ReportFlavor.EVALUATION
    company_eval_snap = eval_snap if is_eval else None
    company_peers = p3.peer_comp if is_eval else None
    tabs: list[TabDef] = [
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
                eval_snap=company_eval_snap,
                peer_comp=company_peers,
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


def _tab_groups(spec: ReportSpec, p3: WorkspaceP3Panels) -> list[TabGroup]:
    """Fold the flat section tabs into the ~6 grouped top-level tabs (UX9).

    Group order: portfolio/watchlist leads with Overview (thesis + valuation,
    the analytical anchor); evaluation reports lead with Research and put
    Company first inside it, since the reader hasn't internalized the business
    yet. Single-section groups reuse the section id as the group id so deep
    links and data-xtab cross-links resolve without a pill row.
    """
    by_id = {t[0]: t for t in _tab_defs(spec, p3)}
    is_eval = spec.flavor == ReportFlavor.EVALUATION
    research_ids = ["bear", "company", "exec_comp", "synthesis"]
    if is_eval:
        research_ids = ["company", "bear", "exec_comp", "synthesis"]
    research: tuple[str, str, list[str]] = ("research", "Research", research_ids)
    core: list[tuple[str, str, list[str]]] = [
        ("overview", "Overview", ["thesis", "valuation"]),
        ("quarter", "Quarter", ["earnings", "saydo", "news"]),
        ("financials", "Financials", ["financials"]),
    ]
    plan = [research, *core] if is_eval else [*core, research]
    # Position group only when held (decision history rides along). An exited
    # name keeps its decision audit as a standalone Decisions tab; a name with
    # no position and no decisions hides both (P4.2 hide-don't-stub).
    if "position" in by_id:
        plan.append(("position", "Position", ["position", "decisions"]))
    elif by_id["decisions"][2] is not None:
        plan.append(("decisions", "Decisions", ["decisions"]))
    plan.append(("sources", "Sources", ["sources"]))
    return [(gid, label, [by_id[sid] for sid in sids]) for gid, label, sids in plan]


def _tabs(body: StringIO, groups: list[TabGroup]) -> None:
    body.write('<div class="tabs">')
    for i, (gid, label, sections) in enumerate(groups):
        # The badge aggregates across the group's sections (None when no
        # section carries a count).
        counts = [c for _sid, _slabel, c, _fn in sections if c is not None]
        count = sum(counts) if counts else None
        cls = "tab active" if i == 0 else "tab"
        body.write(f'<button class="{cls}" data-tab="{_esc(gid)}">')
        body.write(f'<span class="tab-label">{_esc(label)}</span>')
        if count is not None:
            body.write(f'<span class="tab-count">{count}</span>')
        body.write("</button>")
    body.write('<div class="tabs-spacer"></div></div>')


def _subtabs(body: StringIO, sections: list[TabDef]) -> None:
    """The slim pill row inside a multi-section group pane."""
    body.write('<div class="subtabs">')
    for i, (sid, label, count, _fn) in enumerate(sections):
        cls = "subtab active" if i == 0 else "subtab"
        body.write(f'<button class="{cls}" data-subtab="{_esc(sid)}">')
        body.write(f'<span class="subtab-label">{_esc(label)}</span>')
        if count is not None:
            body.write(f'<span class="subtab-count">{count}</span>')
        body.write("</button>")
    body.write("</div>")
