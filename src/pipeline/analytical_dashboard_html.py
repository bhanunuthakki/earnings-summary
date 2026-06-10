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
from ui.tokens import FAVICON_LINK

_TRIGGER_TONE: dict[str, str] = {
    "sell": "tone-sell",
    "trim": "tone-trim",
    "hold": "tone-hold",
    "initiate_candidate": "tone-init",
    "unknown": "tone-muted",
}


def render_html(
    dash: AnalyticalDashboard,
    *,
    generated_at: datetime,
    tier_coverage: dict[str, dict[str, int]] | None = None,
) -> str:
    parts: list[str] = [
        _PAGE_HEAD.format(generated_at=escape(generated_at.isoformat(timespec="seconds"))),
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
# needs. Drives the lazy `GET /api/panel/<name>` fetch: build one section, render
# one fragment. (Overview / Holding / Portfolio-live tabs are assembled elsewhere.)
PANEL_TO_SECTION: dict[str, str] = {
    "portfolio": "portfolio_synthesis",
    "holdings": "trigger_ladder",
    "prereads": "rereads",
    "insiders": "insider_events",
    "predictions": "prediction_outcomes",
    "decisions": "decisions",
    "budget": "llm_budgets",
}


def render_tier_coverage_strip(coverage: dict[str, dict[str, int]]) -> str:
    """Public seam for the command-center shell's Overview tab — the one-line
    tier-staleness strip, reusing the same renderer the full page uses."""
    return _tier_coverage_strip(coverage)


def render_panel_fragment(dash: AnalyticalDashboard, name: str) -> str | None:
    """Render ONE analytical panel as a head/foot-less HTML fragment — the same
    ``_<name>_section`` the full page uses, minus the page chrome — for the lazy
    command-center shell. Returns None for an unknown panel name."""
    if name == "portfolio":
        return _portfolio_synthesis_section(dash.portfolio_synthesis_md)
    if name == "holdings":
        return _trigger_ladder_section(dash.trigger_ladder)
    if name == "prereads":
        return _per_ticker_reread_section(dash.per_ticker_reread)
    if name == "insiders":
        return _insider_events_section(dash.insider_events)
    if name == "predictions":
        return _predictions_section(dash.prediction_outcomes)
    if name == "decisions":
        return _decisions_section(dash.decisions)
    if name == "budget":
        return _llm_budget_section(dash.llm_budgets)
    return None


def _decisions_section(panel: DecisionsPanel) -> str:
    """Recent decisions table + hit-rate strip + calibration strip.
    Shows even when empty so the operator sees the panel exists."""
    if not panel.recent and not panel.hit_rate_by_kind:
        return (
            '<section class="panel"><h2>Decisions (LLM recommendations · audit ledger)</h2>'
            '<p class="muted">No decisions recorded yet. Extract from existing rereads via:</p>'
            '<pre class="cli-hint">python execution/record_decisions.py</pre>'
            "</section>"
        )

    out: list[str] = [
        '<section class="panel"><h2>Decisions (LLM recommendations · audit ledger)</h2>',
        '<p class="sub">Every five-min-reread recommendation extracted into a durable ledger. '
        "Outcomes graded against realized price moves; calibration curve below.</p>",
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
                f"<td>{escape(d.made_at[:10])}</td>"
                f'<td><a href="../research/{escape(d.ticker)}/" class="ticker-link">{escape(d.ticker)}</a></td>'
                f"<td>{escape(kind_label)}</td>"
                f"<td>{escape(d.conviction or '—')}</td>"
                f'<td class="num">{pct_str}</td>'
                f'<td class="{outcome_tone}">{escape(d.outcome_label or "pending")}</td>'
                "</tr>"
            )
        out.append("</tbody></table>")

    out.append("</section>")
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
            '<section class="panel"><h2>LLM spend & budget</h2>'
            '<p class="muted">No budget data. Run <code>python -m alembic upgrade head</code> '
            "to install migration 0052, then revisit.</p>"
            "</section>"
        )
    out: list[str] = [
        '<section class="panel"><h2>LLM spend & budget</h2>',
        f'<p class="sub">Per-purpose monthly caps · {escape(panel.month_label)} · '
        "edit the cap or mode below and click Save. "
        "<code>skip</code> forgoes the call when over cap (and flags it in the brief); "
        "<code>block</code> fails the build; <code>warn</code> overspends.</p>",
        '<table class="budget-table"><thead><tr>',
        '<th>Purpose</th><th class="num">Spend</th><th class="num">Cap</th>',
        '<th>Burn</th><th class="num">Headroom</th><th>Mode</th><th></th>',
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
            '<table class="budget-table"><thead><tr>'
            '<th>Ticker</th><th class="num">Spend</th><th class="num">Calls</th>'
            "</tr></thead><tbody>"
        )
        for t in panel.by_ticker:
            out.append(
                f"<tr><td><code>{escape(t.ticker)}</code></td>"
                f'<td class="num">${t.current_spend_usd:,.2f}</td>'
                f'<td class="num">{t.call_count}</td></tr>'
            )
        out.append(
            "</tbody></table>"
            f'<p class="budget-footer"><strong>By-ticker total:</strong> '
            f"${by_ticker_total:,.2f}</p>"
        )
    out.append(_BUDGET_PANEL_SCRIPT)
    out.append("</section>")
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
    mode_opts = "".join(
        f'<option value="{m}"{" selected" if m == r.on_exceed else ""}>{m}</option>'
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
        f'style="width:80px" value="{r.monthly_cap_usd:.2f}" aria-label="cap for {purpose_esc}"></td>'
        f'<td class="burn-cell"><div class="burn-bar">'
        f'<div class="burn-fill {bar_tone}" style="width: {min(100, bar_width_pct)}%"></div>'
        f"</div></td>"
        f'<td class="num">{r.headroom_pct * 100:+.0f}%</td>'
        f'<td><select class="budget-mode" aria-label="mode for {purpose_esc}">{mode_opts}</select></td>'
        f'<td><button type="button" class="budget-save">Save</button> '
        f'<span class="budget-msg muted"></span></td>'
        "</tr>"
    )


def _tier_coverage_strip(coverage: dict[str, dict[str, int]]) -> str:
    """Compact one-line summary of "how stale is each tier right now?".

    Empty-coverage case (no tracked tickers / no DB) renders nothing rather
    than an empty bar — keeps the dashboard clean for first-run setups.
    """
    if not coverage:
        return ""
    populated = any(v.get("total", 0) > 0 for v in coverage.values())
    if not populated:
        return ""

    parts: list[str] = [
        '<div class="tier-strip"><span class="tier-strip-label">Tier coverage:</span>'
    ]
    chips: list[str] = []
    for tier in ("P1", "P2", "P3"):
        c = coverage.get(tier, {})
        fresh = int(c.get("fresh", 0))
        stale = int(c.get("stale", 0))
        total = int(c.get("total", 0))
        if total == 0:
            chips.append(f'<span class="tier-chip tier-empty">{tier}: 0 tracked</span>')
            continue
        # Pretty-printer for large counts ("1.8k / 2.3k") on P3.
        fresh_disp = _fmt_count(fresh)
        total_disp = _fmt_count(total)
        if stale == 0:
            chips.append(
                f'<span class="tier-chip tier-ok" title="{tier} — all fresh">'
                f"{tier}: {fresh_disp} / {total_disp} fresh</span>"
            )
        else:
            chips.append(
                f'<span class="tier-chip tier-stale" title="To force-refresh: '
                f"python execution/daily_fetch_and_brief.py --ignore-tier "
                f'(or run the {_tier_cron_hint(tier)} cron)">'
                f"{tier}: {fresh_disp} / {total_disp} fresh "
                f'<span class="tier-stale-count">({stale} stale)</span></span>'
            )
    parts.append(" · ".join(chips))
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
            '<section class="panel"><h2>Portfolio synthesis</h2>'
            '<p class="muted">No cross-portfolio synthesis cached. Run:</p>'
            '<pre class="cli-hint">python execution/run_lens.py --lens cross_portfolio_synthesis</pre>'
            "</section>"
        )
    # Render markdown minimally — preserve headers + bold + bullets
    rendered = light_markdown_to_html(content_md)
    return (
        '<section class="panel synthesis-panel">'
        "<h2>Portfolio synthesis</h2>"
        '<p class="sub">Cross-ticker patterns · this week\'s deeper-look · capital-allocation suggestions.</p>'
        f'<div class="synthesis-body">{rendered}</div>'
        "</section>"
    )


def _per_ticker_reread_section(rows: list[PortfolioLensRow]) -> str:
    """Compact 5-min reread cards for every holding that has one cached."""
    if not rows:
        return (
            '<section class="panel"><h2>Per-holding 5-min rereads</h2>'
            '<p class="muted">No per-holding rereads cached. Generate via <code>python execution/run_lens.py --tickers AMZN,GOOG,META --lens five_min_reread</code> (or --all for every lens).</p>'
            "</section>"
        )
    out: list[str] = [
        '<section class="panel"><h2>Per-holding 5-min rereads</h2>',
        '<p class="sub">Decision-oriented per-ticker artifact. Click a ticker for the full memo.</p>',
        '<div class="reread-grid">',
    ]
    for r in rows:
        out.append(_reread_card(r))
    out.append("</div></section>")
    return "".join(out)


def _reread_card(r: PortfolioLensRow) -> str:
    """One collapsible 5-min-reread card. Extracted so the grid panel and the
    dropdown-driven Holding tab (PR 8) render an identical card from one place."""
    rendered = light_markdown_to_html(r.content_md[:8000])
    return (
        f'<details class="reread-card"><summary>'
        f'<a href="../research/{escape(r.ticker)}/" class="ticker-link">{escape(r.ticker)}</a>'
        f'<span class="reread-stamp">{escape(r.generated_at[:10])}</span>'
        f"</summary>"
        f'<div class="reread-body">{rendered}</div>'
        "</details>"
    )


def light_markdown_to_html(md: str) -> str:
    """Cheap markdown subset: ##/### headers, **bold**, bullets, paragraphs.
    Avoids a full markdown library so the dashboard stays dependency-free.
    Handles enough to render lens outputs faithfully. Public: the advisor
    Memos panel (P2.3) renders memo bodies through the same subset."""
    import re

    lines = md.splitlines()
    out: list[str] = []
    in_list = False
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("")
            continue
        if line.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h4>{escape(line[4:])}</h4>")
            continue
        if line.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{escape(line[3:])}</h3>")
            continue
        if line.startswith("# "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{escape(line[2:])}</h2>")
            continue
        # Horizontal rule
        if line.strip() in {"---", "***"}:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<hr>")
            continue
        # Bullet
        bullet_match = re.match(r"^[-*]\s+(.+)", line)
        if bullet_match:
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = _inline_md(bullet_match.group(1))
            out.append(f"<li>{content}</li>")
            continue
        # Paragraph
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<p>{_inline_md(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _inline_md(text: str) -> str:
    import re

    s = escape(text)
    # Bold then italic — order matters
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)


def _trigger_ladder_section(rows: list[TriggerLadderRow]) -> str:
    if not rows:
        return (
            '<section class="panel"><h2>Trigger ladder</h2>'
            '<p class="muted">No DCF runs yet. Run <code>python execution/refresh_dcf.py --all-named</code>.</p></section>'
        )

    out: list[str] = [
        '<section class="panel"><h2>Trigger ladder</h2>',
        '<p class="sub">Every holding positioned by DCF over/under vs MoS bar. Sorted by absolute deviation.</p>',
        '<table class="trigger-table"><thead><tr>',
        "<th>Ticker</th><th>List</th><th>Verdict</th>",
        '<th class="num">Live</th><th class="num">Fair value</th>',
        '<th class="num">Over/under</th><th class="num">MoS bar</th>',
        "<th>Trigger</th></tr></thead><tbody>",
    ]
    for r in rows:
        tone = _TRIGGER_TONE.get(r.trigger_status or "unknown", "tone-muted")
        ou = f"{(r.over_under_pct or 0) * 100:+.1f}%" if r.over_under_pct is not None else "—"
        mos = f"{(r.mos_bar or 0) * 100:.0f}%" if r.mos_bar is not None else "—"
        # Precompute the conditional numeric cells. (Inlining the ``if/else`` into
        # the f-string below would bind the ternary to the WHOLE concatenated
        # string and drop the row opener + ticker/list/verdict cells whenever a
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
        out.append(
            f'<tr class="{tone}">'
            f'<td><a href="../research/{escape(r.ticker)}/" class="ticker-link">{escape(r.ticker)}</a></td>'
            f"<td>{escape(r.list_type)}</td>"
            f"<td>{escape(r.verdict or '—')}</td>"
            f"{live}{fair}"
            f'<td class="num">{ou}</td><td class="num">{mos}</td>'
            f'<td class="trigger-cell">{escape((r.trigger_status or "unknown").replace("_", " "))}</td>'
            "</tr>"
        )
    out.append("</tbody></table></section>")
    return "".join(out)


def _insider_events_section(rows: list[InsiderEventRow]) -> str:
    if not rows:
        return (
            '<section class="panel"><h2>Cross-ticker insider activity (last 90d)</h2>'
            '<p class="muted">No insider data. Run <code>python execution/backfill_insider_transactions.py --since 2024-01-01</code>.</p></section>'
        )
    out: list[str] = [
        '<section class="panel"><h2>Cross-ticker insider activity (last 90d)</h2>',
        '<p class="sub">Discretionary trades only · ranked by conviction signal · 10b5-1 sells filtered out.</p>',
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
            f'<td><a href="../research/{escape(r.ticker)}/" class="ticker-link">{escape(r.ticker)}</a></td>'
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
    out.append("</tbody></table></section>")
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
        '<section class="panel"><h2>Predictions outcomes (cross-ticker)</h2>',
        '<p class="sub">SayDo, LLM bear-case, risk-factor materialization tallied across all forward-looking sources.</p>',
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
                f'<td><a href="../research/{escape(ticker)}/" class="ticker-link">{escape(ticker)}</a></td>'
                f"<td>{escape(source_kind)}</td>"
                f'<td class="num muted">{outcomes.get("pending", 0)}</td>'
                f'<td class="num pos">{outcomes.get("met", 0)}</td>'
                f'<td class="num">{outcomes.get("mixed", 0)}</td>'
                f'<td class="num neg">{outcomes.get("missed", 0)}</td>'
                f'<td class="num">{hit_rate}</td>'
                "</tr>"
            )
    out.append("</tbody></table></section>")
    return "".join(out)


_PAGE_HEAD = (
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio · analytical dashboard</title>
"""
    + FAVICON_LINK
    + """
<style>
  body {{ margin: 0; padding: 24px; font-family: 'Inter', -apple-system, sans-serif; background: #0c0d10; color: #e5e5e2; line-height: 1.5; font-size: 14px; }}
  h1 {{ font-size: 24px; margin: 0 0 8px; font-weight: 600; }}
  h2 {{ font-size: 18px; margin: 0 0 6px; font-weight: 600; }}
  .stamp {{ color: #888; font-size: 12px; font-family: 'JetBrains Mono', monospace; margin-bottom: 24px; }}
  .panel {{ margin-bottom: 32px; background: #16171a; border: 1px solid #2a2c30; border-radius: 8px; padding: 18px 20px; }}
  .panel .sub {{ color: #999; font-size: 12px; margin: 0 0 16px; }}
  .muted {{ color: #888; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px 10px; border-bottom: 2px solid #2a2c30; font-family: 'JetBrains Mono', monospace; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: #888; font-weight: 600; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #1f2125; vertical-align: top; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.muted {{ color: #666; }}
  td.pos {{ color: #4ade80; }}
  td.neg {{ color: #f87171; }}
  .ticker-link {{ color: #f5f5f0; text-decoration: none; font-weight: 600; }}
  .ticker-link:hover {{ color: #aaa; }}
  tr.tone-sell {{ background: rgba(248, 113, 113, 0.06); }}
  tr.tone-trim {{ background: rgba(251, 191, 36, 0.04); }}
  tr.tone-init {{ background: rgba(74, 222, 128, 0.06); }}
  tr.tx-buy {{ background: rgba(74, 222, 128, 0.04); }}
  tr.tx-sell {{ background: rgba(248, 113, 113, 0.02); }}
  td.trigger-cell {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; text-transform: uppercase; }}
  tr.tone-sell .trigger-cell {{ color: #f87171; }}
  tr.tone-trim .trigger-cell {{ color: #fbbf24; }}
  tr.tone-init .trigger-cell {{ color: #4ade80; }}
  td.signal-strong {{ color: #4ade80; font-weight: 600; }}
  td.signal-medium {{ color: #fbbf24; }}
  td.signal-weak {{ color: #888; }}
  /* Synthesis panel */
  .synthesis-panel {{ border-left: 3px solid #4ade80; }}
  .synthesis-body {{ font-size: 14px; line-height: 1.65; }}
  .synthesis-body h2, .synthesis-body h3, .synthesis-body h4 {{ color: #f5f5f0; margin-top: 1.2em; margin-bottom: 6px; }}
  .synthesis-body h2 {{ font-size: 18px; }}
  .synthesis-body h3 {{ font-size: 15px; }}
  .synthesis-body h4 {{ font-size: 13px; color: #4ade80; }}
  .synthesis-body strong {{ color: #f5f5f0; }}
  .synthesis-body code {{ background: #1f2125; padding: 1px 5px; border-radius: 3px; font-family: 'JetBrains Mono', monospace; font-size: 12px; }}
  .synthesis-body ul {{ padding-left: 22px; }}
  .synthesis-body li {{ margin-bottom: 4px; }}
  .synthesis-body hr {{ border: none; border-top: 1px solid #2a2c30; margin: 16px 0; }}
  /* Reread grid */
  .reread-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 12px; margin-top: 8px; }}
  .reread-card {{ background: #16171a; border: 1px solid #2a2c30; border-radius: 6px; padding: 12px 14px; }}
  .reread-card summary {{ cursor: pointer; list-style: none; display: flex; justify-content: space-between; align-items: baseline; font-size: 16px; font-weight: 600; }}
  .reread-card summary::-webkit-details-marker {{ display: none; }}
  .reread-card summary::before {{ content: '▸ '; color: #888; font-family: 'JetBrains Mono', monospace; }}
  .reread-card[open] summary::before {{ content: '▾ '; }}
  .reread-stamp {{ color: #888; font-size: 11px; font-family: 'JetBrains Mono', monospace; font-weight: 400; }}
  .reread-body {{ font-size: 13px; line-height: 1.55; margin-top: 10px; }}
  .reread-body h2, .reread-body h3, .reread-body h4 {{ color: #f5f5f0; margin: 10px 0 4px; }}
  .reread-body h2 {{ font-size: 14px; color: #4ade80; }}
  .reread-body h3 {{ font-size: 13px; }}
  .reread-body strong {{ color: #f5f5f0; }}
  .reread-body ul {{ padding-left: 18px; }}
  .reread-body hr {{ border: none; border-top: 1px solid #2a2c30; margin: 10px 0; }}
  .cli-hint {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; padding: 10px 12px; background: #1f2125; border-radius: 4px; color: #4ade80; overflow-x: auto; margin: 6px 0 0; }}
  /* Decisions panel */
  .panel-h3 {{ font-size: 14px; margin: 18px 0 8px; font-weight: 600; color: #f5f5f0; font-family: 'JetBrains Mono', monospace; text-transform: uppercase; letter-spacing: 0.4px; }}
  .kpi-strip {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin: 8px 0 12px; }}
  .kpi-card {{ background: #1f2125; border: 1px solid #2a2c30; border-radius: 6px; padding: 10px 12px; text-align: center; }}
  .kpi-card.tone-good {{ border-left: 3px solid #4ade80; }}
  .kpi-card.tone-warn {{ border-left: 3px solid #fbbf24; }}
  .kpi-card.tone-bad {{ border-left: 3px solid #f87171; }}
  .kpi-card.tone-muted {{ border-left: 3px solid #555; }}
  .kpi-label {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #888; letter-spacing: 0.5px; }}
  .kpi-value {{ font-size: 22px; font-weight: 700; margin: 2px 0; color: #f5f5f0; }}
  .kpi-sub {{ font-size: 10px; color: #777; font-family: 'JetBrains Mono', monospace; }}
  .calib-strip {{ display: flex; flex-direction: column; gap: 6px; margin: 8px 0 18px; }}
  .calib-row {{ display: grid; grid-template-columns: 80px 1fr 110px; gap: 12px; align-items: center; font-size: 12px; }}
  .calib-label {{ font-family: 'JetBrains Mono', monospace; color: #aaa; text-transform: uppercase; }}
  .calib-bar {{ background: #1f2125; border-radius: 3px; height: 14px; overflow: hidden; }}
  .calib-fill {{ background: linear-gradient(90deg, #f87171 0%, #fbbf24 50%, #4ade80 100%); height: 100%; }}
  .calib-value {{ font-family: 'JetBrains Mono', monospace; color: #ccc; text-align: right; }}
  .decisions-table td.outcome-correct {{ color: #4ade80; }}
  .decisions-table td.outcome-wrong {{ color: #f87171; }}
  .decisions-table td.outcome-mixed {{ color: #fbbf24; }}
  .decisions-table td.outcome-pending {{ color: #888; }}
  /* LLM budget panel */
  .budget-table td code {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #f5f5f0; background: transparent; padding: 0; }}
  .burn-cell {{ width: 200px; padding: 6px 10px; }}
  .burn-bar {{ width: 100%; height: 8px; background: #1f2125; border-radius: 4px; overflow: hidden; }}
  .burn-fill {{ height: 100%; transition: width 0.2s; }}
  .burn-ok {{ background: #4ade80; }}
  .burn-warn {{ background: #fbbf24; }}
  .burn-over {{ background: #f87171; }}
  .block-hard {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #f87171; font-weight: 600; }}
  .block-soft {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #888; }}
  .budget-footer {{ margin-top: 12px; font-size: 13px; color: #ccc; }}
  .budget-footer strong {{ color: #f5f5f0; }}
  /* Tier coverage strip */
  .tier-strip {{ background: #16171a; border: 1px solid #2a2c30; border-radius: 6px; padding: 10px 14px; margin-bottom: 22px; font-size: 13px; display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }}
  .tier-strip-label {{ color: #888; font-family: 'JetBrains Mono', monospace; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; margin-right: 8px; }}
  .tier-chip {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; padding: 2px 6px; border-radius: 3px; cursor: help; }}
  .tier-ok {{ color: #4ade80; }}
  .tier-stale {{ color: #fbbf24; }}
  .tier-stale-count {{ color: #f87171; font-weight: 600; }}
  .tier-empty {{ color: #666; }}
</style>
</head>
<body>
<h1>Portfolio · analytical dashboard</h1>
<div class="stamp">generated {generated_at}</div>
"""
)

_PAGE_FOOT = "</body></html>"
