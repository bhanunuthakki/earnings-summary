"""HTML renderer for the analytical portfolio dashboard.

Pure render layer: takes the :class:`AnalyticalDashboard` dataclass produced by
``pipeline.analytical_dashboard.build_analytical_dashboard`` and emits one
self-contained HTML document.

Split out of ``execution/build_analytical_dashboard.py`` so BOTH the static
exporter (that CLI) and the live command center (``execution/comments_server.py``
→ ``GET /analytical``) render identical markup from a single code path — no
query/markup divergence between the two surfaces.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from pipeline.analytical_dashboard import (
    AnalyticalDashboard,
    DecisionsPanel,
    InsiderEventRow,
    LlmBudgetPanel,
    LlmBudgetRow,
    PortfolioLensRow,
    PredictionOutcomeRow,
    TriggerLadderRow,
)
from ui import living_grid as lg
from ui.controls import controls_css, k_empty, ticker_label
from ui.prose import render_prose
from ui.time import stamp_html
from ui.tokens import FAVICON_LINK, palette_css

_TRIGGER_TONE: dict[str, str] = {
    "sell": "tone-sell",
    "trim": "tone-trim",
    "hold": "tone-hold",
    "initiate_candidate": "tone-init",
    "unknown": "tone-muted",
    # DCF trust gate (dcf_runs.sanity_flag): a flagged model gets a loud tone, no
    # action signal — the number behind the row needs review before it means anything.
    "unreviewed": "tone-bad",
}


def render_html(
    dash: AnalyticalDashboard,
    *,
    generated_at: datetime,
    tier_coverage: dict[str, dict[str, int]] | None = None,
) -> str:
    parts: list[str] = [
        _PAGE_HEAD.format(generated_at=stamp_html(generated_at, prefix="updated ")),
        _tier_coverage_strip(tier_coverage or {}),
        # Top of page: portfolio-wide synthesis when cached
        _portfolio_synthesis_section(dash.portfolio_synthesis_md),
        _per_ticker_reread_section(dash.per_ticker_reread),
        _decisions_section(dash.decisions),
        _trigger_ladder_section(dash.trigger_ladder),
        _insider_events_section(dash.insider_events),
        _predictions_section(dash.prediction_outcomes),
        _llm_budget_section(dash.llm_budgets),
        _PAGE_FOOT,
    ]
    return "".join(parts)


# Command-center panel name -> the build_analytical_dashboard `sections` key it
# needs. Drives the lazy `GET /api/panel/<name>` FALLBACK fetch in
# comments_server — only panels with no dedicated route remain here. P6.1
# retired the dead entries the theme migration left: insiders / predictions
# were removed from the nav (P1.1) with no remaining fetchers, and
# "decisions" was superseded by the Decisions record (P2.2,
# /api/panel/decisions_record). "prereads" stays: its ?ticker= fragment is a
# tested HTTP contract and the Holding tab renders the same fragment
# in-process. "portfolio" short-circuits into its dedicated route before
# this map is consulted.
PANEL_TO_SECTION: dict[str, str] = {
    "holdings": "trigger_ladder",
    "prereads": "rereads",
    "budget": "llm_budgets",
}


def render_tier_coverage_strip(coverage: dict[str, dict[str, int]]) -> str:
    """Public seam for the command-center shell's Overview tab — the one-line
    tier-staleness strip, reusing the same renderer the full page uses."""
    return _tier_coverage_strip(coverage)


def render_panel_fragment(dash: AnalyticalDashboard, name: str) -> str | None:
    """Render ONE analytical panel as a head/foot-less HTML fragment — the same
    ``_<name>_section`` the full page uses, minus the page chrome. Serves the
    comments_server fallback route (PANEL_TO_SECTION names) plus two direct
    in-process callers: portfolio_panel ("portfolio") and the Holding tab
    ("prereads"). Returns None for an unknown panel name. (The insiders /
    predictions / decisions fragments were retired in P6.1 — nothing fetched
    them after the theme migration; their sections still render inside the
    full static page via ``render_html``.)"""
    if name == "portfolio":
        return _portfolio_synthesis_section(dash.portfolio_synthesis_md)
    if name == "holdings":
        return _trigger_ladder_section(dash.trigger_ladder)
    if name == "prereads":
        return _per_ticker_reread_section(dash.per_ticker_reread)
    if name == "budget":
        return _llm_budget_section(dash.llm_budgets)
    return None


def _decisions_section(panel: DecisionsPanel) -> str:
    """Recent decisions table + hit-rate strip + calibration strip.
    Shows even when empty so the operator sees the panel exists."""
    if not panel.recent and not panel.hit_rate_by_kind:
        return (
            '<section class="panel"><div class="panel-head">'
            "<h2>Decisions (LLM recommendations · audit ledger)</h2></div>"
            '<div class="panel-body">'
            + k_empty("No decisions recorded yet — extract them from cached rereads.")
            + "<details><summary>run manually</summary>"
            '<pre class="cli-hint">python execution/record_decisions.py</pre></details>'
            "</div></section>"
        )

    out: list[str] = [
        '<section class="panel"><div class="panel-head">'
        "<h2>Decisions (LLM recommendations · audit ledger)</h2>"
        '<p class="sub">Every five-min-reread recommendation extracted into a durable ledger. '
        "Outcomes graded against realized price moves; calibration curve below.</p>"
        '</div><div class="panel-body">',
    ]

    # Hit-rate strip — one card per kind, with correct% when graded
    if panel.hit_rate_by_kind:
        out.append('<div class="kpi-strip">')
        for kind in sorted(panel.hit_rate_by_kind.keys()):
            c = panel.hit_rate_by_kind[kind]
            graded = c.get("correct", 0) + c.get("wrong", 0) + c.get("mixed", 0)
            pending = c.get("pending", 0)
            total = graded + pending
            hit_rate = (100 * c.get("correct", 0) / graded) if graded > 0 else None
            hit_str = f"{hit_rate:.0f}%" if hit_rate is not None else "—"
            tone = (
                "tone-good"
                if hit_rate is not None and hit_rate >= 60
                else "tone-warn"
                if hit_rate is not None and hit_rate >= 40
                else "tone-bad"
                if hit_rate is not None
                else "tone-muted"
            )
            out.append(
                f'<div class="kpi-card {tone}">'
                f'<div class="kpi-label">{escape(kind.upper())}</div>'
                f'<div class="kpi-value">{hit_str}</div>'
                f'<div class="kpi-sub">{graded} graded · {pending} pending · {total} total</div>'
                "</div>"
            )
        out.append("</div>")

    # Calibration sparkline — conviction bucket → correct%
    if panel.calibration_by_conviction:
        out.append('<h3 class="panel-h3">Calibration · correct% by stated conviction</h3>')
        out.append('<div class="calib-strip">')
        for conv in ("high", "medium", "low", "unstated"):
            if conv not in panel.calibration_by_conviction:
                continue
            c = panel.calibration_by_conviction[conv]
            graded = c.get("correct", 0) + c.get("wrong", 0) + c.get("mixed", 0)
            hit = (100 * c.get("correct", 0) / graded) if graded > 0 else None
            hit_str = f"{hit:.0f}%" if hit is not None else "—"
            bar_width = int(hit) if hit is not None else 0
            out.append(
                '<div class="calib-row">'
                f'<div class="calib-label">{escape(conv)}</div>'
                f'<div class="calib-bar"><div class="calib-fill" style="width:{bar_width}%"></div></div>'
                f'<div class="calib-value">{hit_str} ({graded})</div>'
                "</div>"
            )
        out.append("</div>")

    # Recent decisions table
    if panel.recent:
        out.append(
            '<h3 class="panel-h3">Recent recommendations</h3>'
            '<table class="decisions-table"><thead><tr>'
            "<th>When</th><th>Ticker</th><th>Recommendation</th>"
            '<th>Conviction</th><th class="num">Outcome %</th>'
            "<th>Outcome</th></tr></thead><tbody>"
        )
        for d in panel.recent:
            kind_label = d.recommendation_kind.upper()
            if d.recommendation_value is not None:
                kind_label = f"{kind_label} {d.recommendation_value:g}%"
            outcome_tone = (
                "outcome-correct"
                if d.outcome_label == "correct"
                else "outcome-wrong"
                if d.outcome_label == "wrong"
                else "outcome-mixed"
                if d.outcome_label == "mixed"
                else "outcome-pending"
            )
            pct_str = f"{d.outcome_pct * 100:+.1f}%" if d.outcome_pct is not None else "—"
            out.append(
                "<tr>"
                f"<td>{stamp_html(d.made_at, mode='date')}</td>"
                f"<td>{ticker_label(d.ticker, href=f'../research/{d.ticker}/')}</td>"
                f"<td>{escape(kind_label)}</td>"
                f"<td>{escape(d.conviction or '—')}</td>"
                f'<td class="num">{pct_str}</td>'
                f'<td class="{outcome_tone}">{escape(d.outcome_label or "pending")}</td>'
                "</tr>"
            )
        out.append("</tbody></table>")

    out.append("</div></section>")
    return "".join(out)


_BUDGET_PANEL_SCRIPT = """<script>
(function () {
  document.querySelectorAll('.budget-table tbody tr[data-purpose]').forEach(function (tr) {
    var btn = tr.querySelector('.budget-save');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var purpose = tr.getAttribute('data-purpose');
      var msg = tr.querySelector('.budget-msg');
      var payload = {
        cap_usd: parseFloat(tr.querySelector('.budget-cap').value),
        on_exceed: tr.querySelector('.budget-mode').value
      };
      msg.textContent = 'saving…';
      fetch('/api/llm-budgets/' + encodeURIComponent(purpose), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (b) {
          msg.textContent = res.ok ? 'saved \\u2713' : ('error: ' + (b.error || res.status));
        });
      }).catch(function () { msg.textContent = 'network error'; });
    });
  });
})();
</script>"""


def _llm_budget_section(panel: LlmBudgetPanel) -> str:
    """LLM Spend & Budget panel — per-purpose progress bars + MTD totals.

    Empty-state hint when the budget tables haven't been migrated yet so
    the dashboard works on older repos without a hard failure."""
    if not panel.rows:
        return (
            '<section class="panel"><div class="panel-head"><h2>LLM spend & budget</h2></div>'
            '<div class="panel-body">'
            + k_empty("No LLM budget data yet — the budget migration hasn't run.")
            + "<details><summary>run manually</summary>"
            '<pre class="cli-hint">python -m alembic upgrade head</pre></details>'
            "</div></section>"
        )
    out: list[str] = [
        '<section class="panel"><div class="panel-head"><h2>LLM spend & budget</h2>'
        f'<p class="sub">Per-purpose monthly caps · {escape(panel.month_label)} · '
        "edit the cap or mode below and click Save. "
        "<code>skip</code> forgoes the call when over cap (and flags it in the brief); "
        "<code>block</code> fails the build; <code>warn</code> overspends.</p>"
        '</div><div class="panel-body">',
        '<table class="budget-table"><thead><tr>',
        '<th title="The LLM call site this cap governs (one row per purpose)">Purpose</th>',
        '<th class="num" title="Spent this month against this purpose">Spend</th>',
        '<th class="num" title="Monthly cap in USD - edit and Save">Cap</th>',
        '<th title="Share of the cap consumed">Burn</th>',
        '<th class="num" title="Cap remaining as % (negative = over)">Headroom</th>',
        '<th title="What happens once over cap: skip forgoes the call, '
        'block fails the build, warn just overspends">Mode</th><th></th>',
        "</tr></thead><tbody>",
    ]
    for r in panel.rows:
        out.append(_budget_row_html(r))
    out.append("</tbody></table>")
    pct = (
        100.0 * panel.total_spend_mtd_usd / panel.projected_month_end_usd
        if panel.projected_month_end_usd > 0
        else 0.0
    )
    out.append(
        '<p class="budget-footer">'
        f"<strong>MTD total:</strong> ${panel.total_spend_mtd_usd:,.2f} · "
        f"<strong>Projected month-end:</strong> ${panel.projected_month_end_usd:,.2f} "
        f'<span class="muted">(MTD = {pct:.0f}% of projection)</span>'
        "</p>"
    )
    if panel.by_ticker:
        by_ticker_total = sum(t.current_spend_usd for t in panel.by_ticker)
        out.append(
            f"<h3>By ticker · {escape(panel.month_label)}</h3>"
            '<p class="sub">All LLM calls this month grouped by attributed ticker '
            "(every purpose, budgeted or not — so this can exceed the capped total above).</p>"
            + lg.grid_open()
            + lg.filter_bar(len(panel.by_ticker), noun="tickers", placeholder="Filter by ticker…")
            + '<table class="budget-table"><thead><tr>'
            + lg.th("Ticker", "ticker", "text", num=False)
            + lg.th("Spend", "spend", "num")
            + lg.th("Calls", "calls", "num")
            + "</tr></thead><tbody>"
        )
        for t in panel.by_ticker:
            data = (
                lg.data_text(t.ticker)
                + lg.data_text_key("ticker", t.ticker)
                + lg.data_num("spend", t.current_spend_usd)
                + lg.data_num("calls", float(t.call_count))
            )
            out.append(
                f"<tr{data}><td>{ticker_label(t.ticker)}</td>"
                f'<td class="num">${t.current_spend_usd:,.2f}</td>'
                f'<td class="num">{t.call_count}</td></tr>'
            )
        out.append(
            "</tbody></table>"
            + lg.grid_close()
            + f'<p class="budget-footer"><strong>By-ticker total:</strong> '
            f"${by_ticker_total:,.2f}</p>"
        )
    out.append(_BUDGET_PANEL_SCRIPT)
    out.append("</div></section>")
    return "".join(out)


def _budget_row_html(r: LlmBudgetRow) -> str:
    """One progress-bar row. Colour bands:
    OVER (red)     — headroom_pct <= 0
    WARN (amber)   — headroom_pct < (1 - warn_threshold_pct)
    OK (green)     — otherwise
    """
    burn_pct = max(0.0, min(1.0, 1.0 - r.headroom_pct))
    if r.headroom_pct <= 0:
        bar_tone = "burn-over"
    elif r.headroom_pct < (1.0 - r.warn_threshold_pct):
        bar_tone = "burn-warn"
    else:
        bar_tone = "burn-ok"
    bar_width_pct = int(burn_pct * 100)
    purpose_esc = escape(r.purpose)
    mode_titles = {
        "skip": "Over cap: forgo the call and flag it in the brief",
        "block": "Over cap: fail the build loudly",
        "warn": "Over cap: keep calling, just log the overage",
    }
    mode_opts = "".join(
        f'<option value="{m}" title="{escape(mode_titles[m], quote=True)}"'
        f"{' selected' if m == r.on_exceed else ''}>{m}</option>"
        for m in ("skip", "block", "warn")
    )
    # Render >100% as a full bar with the "over" tone — visual cap, the
    # number column still shows the real headroom_pct so the over-spend
    # is auditable.
    return (
        f'<tr data-purpose="{purpose_esc}">'
        f"<td><code>{purpose_esc}</code></td>"
        f'<td class="num">${r.current_spend_usd:,.2f}</td>'
        f'<td class="num"><input class="budget-cap" type="number" min="0" step="1" '
        f'value="{r.monthly_cap_usd:.2f}" aria-label="cap for {purpose_esc}" '
        f'title="Monthly cap in USD for {purpose_esc}"></td>'
        f'<td class="burn-cell"><div class="burn-bar">'
        f'<div class="burn-fill {bar_tone}" style="width: {min(100, bar_width_pct)}%"></div>'
        f"</div></td>"
        f'<td class="num">{r.headroom_pct * 100:+.0f}%</td>'
        f'<td><select class="budget-mode" aria-label="mode for {purpose_esc}" '
        f'title="What happens once {purpose_esc} is over cap">{mode_opts}</select></td>'
        f'<td><button type="button" class="budget-save k-btn k-btn-quiet k-btn-sm" '
        f'title="Apply this cap + mode for {purpose_esc}">Save</button> '
        f'<span class="budget-msg muted"></span></td>'
        "</tr>"
    )


def _tier_coverage_strip(coverage: dict[str, dict[str, int]]) -> str:
    """Compact one-line summary of "how stale is each tier right now?".

    Each chip is a peek trigger (UX9d): clicking opens the portfolio-wide
    data-provenance card — per-source ages with inline refresh — while the
    cron-hint tooltips stay the hover layer and /#system stays the real href
    for middle-click (and for the static export, which has no peek host).

    Empty-coverage case (no tracked tickers / no DB) renders nothing rather
    than an empty bar — keeps the dashboard clean for first-run setups.
    """
    if not coverage:
        return ""
    populated = any(v.get("total", 0) > 0 for v in coverage.values())
    if not populated:
        return ""

    parts: list[str] = [
        '<div class="tier-strip"><span class="tier-strip-label">Data freshness:</span>'
    ]
    peek = 'href="/#system" data-peek-url="/api/peek/provenance" data-peek-title="Data provenance"'
    chips: list[str] = []
    for tier in ("P1", "P2", "P3"):
        c = coverage.get(tier, {})
        fresh = int(c.get("fresh", 0))
        stale = int(c.get("stale", 0))
        total = int(c.get("total", 0))
        if total == 0:
            chips.append(f'<a {peek} class="k-chip">{tier}: 0 tracked</a>')
            continue
        # Pretty-printer for large counts ("1.8k / 2.3k") on P3.
        fresh_disp = _fmt_count(fresh)
        total_disp = _fmt_count(total)
        if stale == 0:
            chips.append(
                f'<a {peek} class="k-chip k-chip-ok" title="{tier} — all fresh">'
                f"{tier}: {fresh_disp} / {total_disp} fresh</a>"
            )
        else:
            # P3 is the deep-history backfill tier — thousands of old rows
            # pending is routine, not an incident, so it renders muted (the
            # plain .k-chip tone + a muted count) instead of shouting red on
            # the landing page (PR1); other tiers take the warn tone.
            tone_cls = "" if tier == "P3" else " k-chip-warn"
            count_cls = "tier-stale-count-muted" if tier == "P3" else "tier-stale-count"
            chips.append(
                f'<a {peek} class="k-chip{tone_cls}" title="To force-refresh: '
                f"python execution/daily_fetch_and_brief.py --ignore-tier "
                f'(or run the {_tier_cron_hint(tier)} cron)">'
                f"{tier}: {fresh_disp} / {total_disp} fresh "
                f'<span class="{count_cls}">({stale} stale)</span></a>'
            )
    # Bordered radius-full chips delimit themselves; the flex gap spaces them
    # (no middot separator needed now that each chip has its own outline).
    parts.append("".join(chips))
    parts.append("</div>")
    return "".join(parts)


def _fmt_count(n: int) -> str:
    """1843 → '1.8k'; 47 → '47'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _tier_cron_hint(tier: str) -> str:
    """Pretty hint for which cron entry forces a stale-tier rebuild."""
    if tier == "P1":
        return "daily"
    if tier == "P2":
        return "weekly P2 lens"
    return "monthly P3"


def _portfolio_synthesis_section(content_md: str | None) -> str:
    """The cross_portfolio_synthesis lens output as the lead panel."""
    if not content_md:
        return (
            '<section class="panel"><div class="panel-head"><h2>Portfolio synthesis</h2></div>'
            '<div class="panel-body">'
            + k_empty("No cross-portfolio synthesis cached yet.")
            + "<details><summary>run manually</summary>"
            '<pre class="cli-hint">python execution/run_lens.py --lens cross_portfolio_synthesis</pre></details>'
            "</div></section>"
        )
    # Render markdown minimally — preserve headers + bold + bullets
    rendered = light_markdown_to_html(content_md)
    return (
        '<section class="panel synthesis-panel"><div class="panel-head">'
        "<h2>Portfolio synthesis</h2>"
        '<p class="sub">Cross-ticker patterns · this week\'s deeper-look · capital-allocation suggestions.</p>'
        '</div><div class="panel-body">'
        f'<div class="synthesis-body">{rendered}</div>'
        "</div></section>"
    )


def _per_ticker_reread_section(rows: list[PortfolioLensRow]) -> str:
    """Compact 5-min reread cards for every holding that has one cached."""
    if not rows:
        return (
            '<section class="panel"><div class="panel-head"><h2>Per-holding 5-min rereads</h2></div>'
            '<div class="panel-body">'
            + k_empty("No per-holding 5-min rereads cached yet.")
            + "<details><summary>run manually</summary>"
            '<pre class="cli-hint">python execution/run_lens.py --tickers AMZN,GOOG,META '
            "--lens five_min_reread  # or --all for every lens</pre></details>"
            "</div></section>"
        )
    out: list[str] = [
        '<section class="panel"><div class="panel-head"><h2>Per-holding 5-min rereads</h2>'
        '<p class="sub">Decision-oriented per-ticker artifact. Click a ticker for the full memo.</p>'
        '</div><div class="panel-body">',
        '<div class="reread-grid">',
    ]
    for r in rows:
        out.append(_reread_card(r))
    out.append("</div></div></section>")
    return "".join(out)


def _reread_card(r: PortfolioLensRow) -> str:
    """One collapsible 5-min-reread card. Extracted so the grid panel and the
    dropdown-driven Holding tab (PR 8) render an identical card from one place."""
    rendered = light_markdown_to_html(r.content_md[:8000])
    return (
        f'<details class="reread-card"><summary>'
        f"{ticker_label(r.ticker, href=f'../research/{r.ticker}/')}"
        f"{stamp_html(r.generated_at, mode='date', css='reread-stamp')}"
        f"</summary>"
        f'<div class="reread-body">{rendered}</div>'
        "</details>"
    )


def light_markdown_to_html(md: str) -> str:
    """Render stored markdown prose to HTML — thin re-export of the one prose
    render boundary (:func:`ui.prose.render_prose`).

    Was a divergent second renderer (##/### headers, bold, bullets — but no
    tables or italics); collapsed in the Instrument Paradigm "one render per
    content-kind" pass so the dashboard, the advisor Memos panel, and the
    workspace report all render identical markdown identically. New code imports
    ``ui.prose.render_prose`` directly."""
    return render_prose(md)


# Display order for the ladder's list-type group bands (D3.1: group by
# semantic kind, the owner's book first).
_LADDER_GROUP_ORDER = ("portfolio", "evaluation", "watchlist")


def _trigger_ladder_section(rows: list[TriggerLadderRow]) -> str:
    """The valuation-trigger ladder, on the Wave-1 density rules
    (surface_density_jit_redesign.md):

    * D7 — the header states the question the table answers.
    * D3.1 — rows GROUP by list type (portfolio first) under band rows,
      instead of repeating an identical "evaluation" cell down a column.
    * D5 — a column that is constant across every row is dead weight: when all
      rows share one trigger status (prod: 11× UNREVIEWED — every model
      sanity-flagged), the status lifts into a single header pill and the
      column disappears.
    """
    if not rows:
        return (
            '<section class="panel"><div class="panel-head"><h2>Trigger ladder</h2></div>'
            '<div class="panel-body">'
            + k_empty("No DCF runs yet.")
            + "<details><summary>run manually</summary>"
            '<pre class="cli-hint">python execution/refresh_dcf.py --all-named</pre></details>'
            "</div></section>"
        )

    statuses = {(r.trigger_status or "unknown") for r in rows}
    lifted_status = statuses.pop() if len(statuses) == 1 else None
    status_note = ""
    if lifted_status is not None:
        label = escape(lifted_status.replace("_", " "))
        status_note = (
            f' <span class="k-pill k-pill-warn" title="every row shares this trigger status; '
            f'the per-row column is elided">all {label}</span>'
            if lifted_status == "unreviewed"
            else f' <span class="k-chip">all {label}</span>'
        )

    out: list[str] = [
        '<section class="panel"><div class="panel-head"><h2>Trigger ladder'
        f"{status_note}</h2>"
        '<p class="sub">Should I trim or add anything? — each name\'s DCF gap vs your '
        "margin-of-safety bar, biggest deviation first.</p>"
        '</div><div class="panel-body">',
        '<table class="trigger-table"><thead><tr>',
        "<th>Ticker</th><th>Verdict</th>",
        '<th class="num">Live</th><th class="num">Fair value</th>',
        '<th class="num">Over/under</th><th class="num">MoS bar</th>',
        ("" if lifted_status is not None else "<th>Trigger</th>"),
        "</tr></thead><tbody>",
    ]
    n_cols = 6 if lifted_status is not None else 7

    def _group_rank(list_type: str) -> int:
        try:
            return _LADDER_GROUP_ORDER.index(list_type)
        except ValueError:
            return len(_LADDER_GROUP_ORDER)

    grouped = sorted(rows, key=lambda r: _group_rank(r.list_type))
    current_group: str | None = None
    for r in grouped:
        if r.list_type != current_group:
            current_group = r.list_type
            n_in_group = sum(1 for x in rows if x.list_type == current_group)
            out.append(
                f'<tr class="tl-group"><td colspan="{n_cols}">'
                f"{escape(current_group.replace('_', ' ').title())} &middot; {n_in_group}"
                "</td></tr>"
            )
        tone = _TRIGGER_TONE.get(r.trigger_status or "unknown", "tone-muted")
        ou = f"{(r.over_under_pct or 0) * 100:+.1f}%" if r.over_under_pct is not None else "—"
        mos = f"{(r.mos_bar or 0) * 100:.0f}%" if r.mos_bar is not None else "—"
        # Precompute the conditional numeric cells. (Inlining the ``if/else`` into
        # the f-string below would bind the ternary to the WHOLE concatenated
        # string and drop the row opener + ticker/verdict cells whenever a
        # value is NULL — exactly the "empty rows with just values" bug for
        # watchlist tickers, which have no live price / over-under / MoS.)
        live = (
            f'<td class="num">${r.live_price:.0f}</td>'
            if r.live_price is not None
            else '<td class="num muted">—</td>'
        )
        fair = (
            f'<td class="num">${r.dcf_fair_value:.0f}</td>'
            if r.dcf_fair_value is not None
            else '<td class="num muted">—</td>'
        )
        trigger_cell = (
            ""
            if lifted_status is not None
            else (
                '<td class="trigger-cell">'
                f"{escape((r.trigger_status or 'unknown').replace('_', ' '))}</td>"
            )
        )
        out.append(
            f'<tr class="{tone}">'
            f"<td>{ticker_label(r.ticker, href=f'../research/{r.ticker}/')}</td>"
            f"<td>{escape(r.verdict or '—')}</td>"
            f"{live}{fair}"
            f'<td class="num">{ou}</td><td class="num">{mos}</td>'
            f"{trigger_cell}"
            "</tr>"
        )
    out.append("</tbody></table></div></section>")
    return "".join(out)


def _insider_events_section(rows: list[InsiderEventRow]) -> str:
    if not rows:
        return (
            '<section class="panel"><div class="panel-head"><h2>Cross-ticker insider activity (last 90d)</h2></div>'
            '<div class="panel-body">'
            + k_empty("No insider-transaction data yet.")
            + "<details><summary>run manually</summary>"
            '<pre class="cli-hint">python execution/backfill_insider_transactions.py '
            "--since 2024-01-01</pre></details>"
            "</div></section>"
        )
    out: list[str] = [
        '<section class="panel"><div class="panel-head"><h2>Cross-ticker insider activity (last 90d)</h2>'
        '<p class="sub">Discretionary trades only · ranked by conviction signal · 10b5-1 sells filtered out.</p>'
        '</div><div class="panel-body">',
        '<table class="insider-table"><thead><tr>',
        "<th>Date</th><th>Ticker</th><th>Insider</th><th>Role</th><th>Action</th>",
        '<th class="num">Shares</th><th class="num">Value</th><th class="num">Signal</th><th>Why</th>',
        "</tr></thead><tbody>",
    ]
    for r in rows:
        tone = (
            "tx-buy"
            if "buy" in r.transaction_type
            else "tx-sell"
            if "sell" in r.transaction_type
            else ""
        )
        signal_pct = int(r.signal_strength * 100)
        strength_tone = (
            "signal-strong"
            if r.signal_strength >= 0.6
            else "signal-medium"
            if r.signal_strength >= 0.3
            else "signal-weak"
        )
        out.append(
            f'<tr class="{tone}">'
            f"<td>{escape(r.transaction_date)}</td>"
            f"<td>{ticker_label(r.ticker, href=f'../research/{r.ticker}/')}</td>"
            f"<td>{escape(r.insider_name)}</td>"
            f"<td>{escape(r.insider_role or '?')}</td>"
            f"<td>{escape(r.transaction_type.replace('_', ' '))}</td>"
            f'<td class="num">{r.shares:,.0f}</td>'
        )
        if r.transaction_value is not None:
            v = r.transaction_value
            v_str = (
                f"${v / 1e9:.1f}B"
                if v >= 1e9
                else f"${v / 1e6:.1f}M"
                if v >= 1e6
                else f"${v / 1e3:.0f}K"
            )
            out.append(f'<td class="num">{v_str}</td>')
        else:
            out.append('<td class="num muted">—</td>')
        out.append(
            f'<td class="num {strength_tone}">{signal_pct}</td><td>{escape(r.rationale)}</td></tr>'
        )
    out.append("</tbody></table></div></section>")
    return "".join(out)


def _predictions_section(rows: list[PredictionOutcomeRow]) -> str:
    if not rows:
        return ""
    # Aggregate by source_kind into a per-ticker grid
    from collections import defaultdict

    by_ticker: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        by_ticker[r.ticker][r.source_kind][r.outcome] = r.count

    out: list[str] = [
        '<section class="panel"><div class="panel-head"><h2>Predictions outcomes (cross-ticker)</h2>'
        '<p class="sub">SayDo, LLM bear-case, risk-factor materialization tallied across all forward-looking sources.</p>'
        '</div><div class="panel-body">',
        '<table class="predictions-table"><thead><tr>',
        "<th>Ticker</th><th>Source</th>",
        '<th class="num">Pending</th><th class="num">Met</th>',
        '<th class="num">Mixed</th><th class="num">Missed</th>',
        '<th class="num">Hit rate</th></tr></thead><tbody>',
    ]
    for ticker in sorted(by_ticker.keys()):
        for source_kind in sorted(by_ticker[ticker].keys()):
            outcomes = by_ticker[ticker][source_kind]
            graded = outcomes.get("met", 0) + outcomes.get("missed", 0) + outcomes.get("mixed", 0)
            hit_rate = f"{100 * outcomes.get('met', 0) / graded:.0f}%" if graded > 0 else "—"
            out.append(
                "<tr>"
                f"<td>{ticker_label(ticker, href=f'../research/{ticker}/')}</td>"
                f"<td>{escape(source_kind)}</td>"
                f'<td class="num muted">{outcomes.get("pending", 0)}</td>'
                f'<td class="num k-num-pos">{outcomes.get("met", 0)}</td>'
                f'<td class="num">{outcomes.get("mixed", 0)}</td>'
                f'<td class="num k-num-neg">{outcomes.get("missed", 0)}</td>'
                f'<td class="num">{hit_rate}</td>'
                "</tr>"
            )
    out.append("</tbody></table></div></section>")
    return "".join(out)


# The whole concatenated head goes through str.format(), so the palette block
# (literal CSS braces) must be brace-escaped before splicing.
_PAGE_HEAD = (
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio · analytical dashboard</title>
"""
    + FAVICON_LINK
    + "\n<style>\n"
    + (palette_css("dark") + controls_css("dark")).replace("{", "{{").replace("}", "}}")
    + """
  body {{ margin: 0; padding: 24px; font-family: var(--sans); background: var(--bg); color: var(--fg); line-height: 1.5; font-size: var(--fs-body); }}
  h1 {{ font-size: var(--fs-display); margin: 0 0 8px; font-weight: 600; }}
  h2 {{ font-size: var(--fs-title); margin: 0 0 6px; font-weight: 600; }}
  .stamp {{ color: var(--muted); font-size: var(--fs-caption); font-family: var(--mono); margin-bottom: var(--sp-3); }}
  .panel {{ margin-bottom: var(--sp-4); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }}
  .panel-head {{ padding: 10px 16px; border-bottom: 1px solid var(--hairline); }}
  .panel-head h2 {{ margin: 0; }}
  .panel-head .sub {{ margin: 4px 0 0; }}
  .panel-body {{ padding: 14px 16px; }}
  .panel-foot {{ padding: 10px 16px; border-top: 1px solid var(--hairline); background: var(--paper); }}
  .panel .sub {{ color: var(--muted); font-size: var(--fs-caption); margin: 0 0 10px; }}
  .muted {{ color: var(--muted); }}
  table {{ width: 100%; border-collapse: collapse; font-size: var(--fs-body); font-variant-numeric: tabular-nums; }}
  th {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid var(--hairline); vertical-align: top; }}
  tbody tr:hover td {{ background: var(--paper); }}
  td.num {{ text-align: right; }}
  td.muted {{ color: var(--muted); }}
  tr.tone-sell {{ background: color-mix(in srgb, var(--bad) 6%, transparent); }}
  tr.tone-trim {{ background: color-mix(in srgb, var(--warn) 4%, transparent); }}
  tr.tone-init {{ background: color-mix(in srgb, var(--ok) 6%, transparent); }}
  tr.tx-buy {{ background: color-mix(in srgb, var(--ok) 4%, transparent); }}
  tr.tx-sell {{ background: color-mix(in srgb, var(--bad) 2%, transparent); }}
  td.trigger-cell {{ font-family: var(--sans); font-size: var(--fs-caption); text-transform: uppercase; }}
  tr.tl-group td {{ color: var(--muted); font-size: var(--fs-caption); font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.06em; padding-top: 10px; border-bottom: 0; }}
  tr.tone-sell .trigger-cell {{ color: var(--bad); }}
  tr.tone-trim .trigger-cell {{ color: var(--warn); }}
  tr.tone-init .trigger-cell {{ color: var(--ok); }}
  td.signal-strong {{ color: var(--ok); font-weight: 600; }}
  td.signal-medium {{ color: var(--warn); }}
  td.signal-weak {{ color: var(--muted); }}
  /* Synthesis panel — lead panel distinguished by placement + panel anatomy,
     not a decorative status rail (status color is reserved for value status). */
  .synthesis-body {{ font-size: var(--fs-body); line-height: 1.65; }}
  .synthesis-body h2, .synthesis-body h3, .synthesis-body h4,
  .synthesis-body h5, .synthesis-body h6 {{ color: var(--fg); margin-top: 1.2em; margin-bottom: 6px; }}
  .synthesis-body h2 {{ font-size: var(--fs-title); }}
  .synthesis-body h3 {{ font-size: var(--fs-title); }}
  /* h4-h6 share the body size: the one prose boundary maps deep markdown
     headings (###/####) here, and panels own the h2/h3 levels above them. */
  .synthesis-body h4, .synthesis-body h5, .synthesis-body h6 {{ font-size: var(--fs-body); color: var(--fg); }}
  .synthesis-body strong {{ color: var(--fg); }}
  .synthesis-body code {{ background: var(--paper); padding: 1px 5px; border-radius: var(--radius); font-family: var(--mono); font-size: 0.93em; }}
  .synthesis-body ul {{ padding-left: 22px; }}
  .synthesis-body li {{ margin-bottom: 4px; }}
  .synthesis-body hr {{ border: none; border-top: 1px solid var(--border); margin: 16px 0; }}
  /* Reread grid */
  .reread-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 12px; margin-top: 8px; }}
  .reread-card {{ background: var(--surface); border-radius: var(--radius); padding: 12px 14px; }}
  .reread-card summary {{ cursor: pointer; list-style: none; display: flex; justify-content: space-between; align-items: baseline; font-size: var(--fs-title); font-weight: 600; }}
  .reread-card summary::-webkit-details-marker {{ display: none; }}
  .reread-card summary::before {{ content: '▸ '; color: var(--muted); font-family: var(--mono); }}
  .reread-card[open] summary::before {{ content: '▾ '; }}
  .reread-stamp {{ color: var(--muted); font-size: var(--fs-caption); font-family: var(--mono); font-weight: 400; }}
  .reread-body {{ font-size: var(--fs-body); line-height: 1.55; margin-top: 10px; }}
  .reread-body h2, .reread-body h3, .reread-body h4 {{ color: var(--fg); margin: 10px 0 4px; }}
  .reread-body h2 {{ font-size: var(--fs-title); color: var(--fg); }}
  .reread-body h3 {{ font-size: var(--fs-body); }}
  .reread-body strong {{ color: var(--fg); }}
  .reread-body ul {{ padding-left: 18px; }}
  .reread-body hr {{ border: none; border-top: 1px solid var(--border); margin: 10px 0; }}
  .cli-hint {{ font-family: var(--mono); font-size: var(--fs-caption); padding: 10px 12px; background: var(--paper); border-radius: var(--radius); color: var(--fg-soft); overflow-x: auto; margin: 6px 0 0; }}
  /* Decisions panel */
  .panel-h3 {{ font-size: var(--fs-title); margin: 18px 0 8px; font-weight: 600; color: var(--fg); }}
  .kpi-strip {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin: 8px 0 12px; }}
  .kpi-card {{ background: var(--paper); border-radius: var(--radius); padding: 10px 12px; text-align: center; }}
  .kpi-card.tone-good {{ border-left: 3px solid var(--ok); }}
  .kpi-card.tone-warn {{ border-left: 3px solid var(--warn); }}
  .kpi-card.tone-bad {{ border-left: 3px solid var(--bad); }}
  .kpi-card.tone-muted {{ border-left: 3px solid var(--muted); }}
  .kpi-label {{ font-size: var(--fs-caption); color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase; }}
  .kpi-value {{ font-size: var(--fs-display); font-weight: 600; margin: 2px 0; color: var(--fg); font-variant-numeric: tabular-nums; }}
  .kpi-sub {{ font-size: var(--fs-caption); color: var(--muted); font-family: var(--sans); }}
  .calib-strip {{ display: flex; flex-direction: column; gap: 6px; margin: 8px 0 18px; }}
  .calib-row {{ display: grid; grid-template-columns: 80px 1fr 110px; gap: 12px; align-items: center; font-size: var(--fs-caption); }}
  .calib-label {{ color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }}
  .calib-bar {{ background: var(--paper); border-radius: var(--radius-full); height: 14px; overflow: hidden; }}
  .calib-fill {{ background: linear-gradient(90deg, var(--bad) 0%, var(--warn) 50%, var(--ok) 100%); height: 100%; }}
  .calib-value {{ font-family: var(--mono); color: var(--fg-soft); text-align: right; }}
  .decisions-table td.outcome-correct {{ color: var(--ok); }}
  .decisions-table td.outcome-wrong {{ color: var(--bad); }}
  .decisions-table td.outcome-mixed {{ color: var(--warn); }}
  .decisions-table td.outcome-pending {{ color: var(--muted); }}
  /* LLM budget panel */
  .budget-table td code {{ font-family: var(--mono); font-size: 0.93em; color: var(--fg); background: transparent; padding: 0; }}
  .budget-table .budget-cap {{ width: 80px; }}
  .burn-cell {{ width: 200px; padding: 6px 10px; }}
  .burn-bar {{ width: 100%; height: 8px; background: var(--paper); border-radius: var(--radius-full); overflow: hidden; }}
  .burn-fill {{ height: 100%; transition: width var(--transition); }}
  .burn-ok {{ background: var(--ok); }}
  .burn-warn {{ background: var(--warn); }}
  .burn-over {{ background: var(--bad); }}
  .block-hard {{ font-family: var(--mono); font-size: var(--fs-caption); color: var(--bad); font-weight: 600; }}
  .block-soft {{ font-family: var(--mono); font-size: var(--fs-caption); color: var(--muted); }}
  .budget-footer {{ margin-top: 12px; font-size: var(--fs-body); color: var(--fg-soft); }}
  .budget-footer strong {{ color: var(--fg); }}
  /* Tier coverage strip */
  .tier-strip {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 8px 14px; margin-bottom: var(--sp-4); font-size: var(--fs-body); display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }}
  .tier-strip-label {{ color: var(--muted); font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.06em; margin-right: 8px; }}
  a.k-chip {{ text-decoration: none; }}
  .tier-stale-count {{ color: var(--bad); font-weight: 600; }}
  .tier-stale-count-muted {{ color: var(--muted); font-weight: 400; }}
</style>
</head>
<body>
<h1>Portfolio · analytical dashboard</h1>
<div class="stamp">{generated_at}</div>
"""
)

_PAGE_FOOT = "</body></html>"
