"""Thesis & risk: thesis tab, decisions tab, bear tab, hygiene/break-rule panels.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from datetime import date
from io import StringIO

from report.kpi_naming import clean_kpi_name
from report.models import (
    BearCaseSection,
    BreakRuleEvaluation,
    DecisionBadge,
    FailureMode,
    KpiLedgerRow,
    SectionStatus,
    SnapshotSection,
    SoftRuleEvaluation,
    ThesisSection,
    ValuationSnapshot,
)
from report.renderers.numfmt import fmt_date, fmt_reltime
from report.renderers.workspace_charts import sparkline
from report.renderers.workspace_data import format_ledger_value, kpi_is_stale, kpi_trend_delta
from report.renderers.workspace_dcf import (
    dcf_inject_button,
    dcf_inject_for_kpi,
    render_dcf_editor,
)
from report.renderers.workspace_sections._shared import (
    _empty_panel,
    _esc,
    _fmt_price,
    _missing_panel,
    _panel_head,
    _render_markdown,
    _xlink_html,
)
from report.renderers.workspace_sections.valuation import _TIMES
from report.sections.p3_data import DecisionHistorySummary, MacroSensitivityRow
from ui import living_grid as lg
from ui.controls import fact_anchor_attrs

__all__ = [
    "_assumptions_sync_row",
    "_bear_tab",
    "_break_rules_panel",
    "_decision_conviction_outcome_panel",
    "_decision_history_panel",
    "_decisions_tab",
    "_failure_mode_card",
    "_failure_modes_panel",
    "_kpi_has_data",
    "_kpi_ledger_row",
    "_macro_beta_tone",
    "_macro_sensitivity_panel",
    "_macro_series_label",
    "_most_underweighted_panel",
    "_scenario_range_block",
    "_soft_rules_panel",
    "_summarize_rule",
    "_thesis_hygiene_panels",
    "_thesis_tab",
    "_val_row",
    "_valuation_summary_panel",
]


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
        eyebrow_bits.append(f"updated {fmt_date(thesis.last_updated.isoformat())}")
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

    # In-app DCF modify→recompute loop (L4): editable assumptions under the
    # valuation card that live-recompute the scenarios + sensitivity heatmap and
    # save through the override ledger. Static shell here; the JS hydrates it from
    # /api/dcf/inputs (degrades to a collapsed launcher when the server is off).
    render_dcf_editor(body, snap.ticker)

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
    _thesis_hygiene_panels(body, thesis, report_date, ticker=snap.ticker)
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
        # P4.2 hide-don't-stub: the h2 above already says "No decisions
        # recorded" — no panel needed under it.
        body.write("</div>")
        return

    # Headline counters: total · win-rate · by-kind chips.
    sub = (
        f"{history.win_rate_overall * 100:.0f}% win rate on graded decisions"
        if history.win_rate_overall is not None
        else "no graded outcomes yet"
    )
    body.write(_panel_head("Summary", sub=sub, classes="decision-history-panel"))
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
        _panel_head(
            "Decision ledger",
            sub=f"{len(history.rows)} row{'s' if len(history.rows) != 1 else ''} · newest first",
        )
        + lg.grid_open()
        + lg.filter_bar(len(history.rows), noun="decisions")
        + '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
        + lg.th("Made", "made", "text", num=False)
        + lg.th("Kind", "kind", "text", num=False)
        + lg.th("Conviction", "conviction", "text", num=False)
        + lg.th("Outcome %", "outcome", "num")
        + "<th>Rationale</th>"
        + "</tr></thead><tbody>"
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
        made = r.made_at.strftime("%Y-%m-%d")
        kind = r.recommendation_kind.upper()
        data = (
            lg.data_text(f"{made} {kind} {r.conviction or ''} {rationale}")
            + lg.data_text_key("made", made)
            + lg.data_text_key("kind", kind)
            + lg.data_text_key("conviction", r.conviction)
            + lg.data_num("outcome", r.outcome_pct)
        )
        body.write(f"<tr{data}>")
        body.write(f'<td class="mono xsmall">{_esc(made)}</td>')
        body.write(f"<td><strong>{_esc(kind)}</strong></td>")
        body.write(f"<td>{_esc(r.conviction or '—')}</td>")
        body.write(outcome_cell)
        body.write(f'<td class="seg-desc">{_esc(rationale)}</td>')
        body.write("</tr>")
    body.write("</tbody></table></div>")
    body.write(lg.grid_close())
    body.write("</div>")
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
        _panel_head(f"Conviction {_TIMES} outcome", sub="graded decisions only")
        + '<table class="tbl"><thead><tr>'
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
        _missing_panel(body, bear.status, bear.missing, title="Bear case")
        body.write("</div>")
        return
    _most_underweighted_panel(body, bear)
    _failure_modes_panel(body, bear)
    if not bear.most_underweighted and not bear.failure_modes:
        _empty_panel(
            body,
            "Bear case",
            "No bear case has been written on this build. Failure modes and "
            "the most-underweighted callout land with the next full analysis "
            "build, and later summaries cross-reference those named risks.",
            reason="not generated",
        )
    body.write("</div>")


def _thesis_hygiene_panels(
    body: StringIO,
    thesis: ThesisSection,
    report_date: date | None = None,
    *,
    ticker: str | None = None,
) -> None:
    """Render break_conditions / qualitative_breakers / competitive_watchlist
    side-by-side, then the full KPI ledger as a collapsible details panel.

    ``ticker`` builds each ledger row's ``fact_ref`` doorway handle
    (``kpi:{ticker}:{def_id}``); None degrades the rows to name-keyed anchors.
    """
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
            body.write(_panel_head(title, sub=sub) + '<ul class="thesis-list">')
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
        sub = f"{len(shown)} tracked"
        if tracked_only:
            sub += f" · {len(tracked_only)} awaiting data"
        body.write(
            '<details class="panel" open id="panel-kpi-ledger" data-persist="kpi-ledger">'
            '<summary class="panel-head">'
            '<span class="panel-title">KPI ledger detail</span>'
            '<span class="panel-meta">'
            + _xlink_html("valuation", "valuation →")
            + f'<span class="panel-sub">{_esc(sub)}</span></span>'
            "</summary>"
            + lg.grid_open()
            + lg.filter_bar(len(shown), noun="KPIs")
            + '<div class="table-scroll"><table class="tbl tbl-nowrap kpi-ledger-table"><thead><tr>'
            + "<th>KPI</th>"
            + lg.th("Tier", "tier", "text", num=False)
            + lg.th("Unit", "unit", "text", num=False)
            + lg.th("Latest", "latest", "num")
            + "<th>Trend</th>"
            + lg.th("Status", "status", "text", num=False)
            + lg.th("Break condition", "break_cond", "text", num=False)
            + lg.th("Source", "source", "text", num=False)
            + "</tr></thead><tbody>"
        )
        for r in shown:
            _kpi_ledger_row(body, r, report_date, ticker=ticker)
        body.write("</tbody></table></div>")
        body.write(lg.grid_close())
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


def _kpi_ledger_row(
    body: StringIO,
    r: KpiLedgerRow,
    report_date: date | None,
    *,
    ticker: str | None = None,
) -> None:
    """One enriched ledger row: clean name + definition gloss, latest value with
    a staleness flag, and a sparkline + YoY/QoQ delta off ``r.history``.

    When the KPI resolves to a definition id and a ``ticker`` is known, the row
    carries a ``fact_ref`` doorway (``kpi:{ticker}:{def_id}``) and the name
    renders as a ``.fact-doorway`` button: a click opens Ask on the EXACT
    series by PK (Instrument Paradigm Law 2). Otherwise it degrades to the
    plain name-keyed anchor."""
    fact_ref: str | None = None
    if ticker and r.kpi_definition_id is not None:
        fact_ref = f"kpi:{ticker.upper()}:{r.kpi_definition_id}"
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
    latest_num: float | None = None
    if r.history:
        period_label, value = r.history[-1]
        latest_num = value
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
    # duplicated inside it. When the row has a fact_ref the name becomes a
    # doorway button (Law 2 — a number/KPI with depth is a clickable element,
    # never an inert span); without one it stays a plain bold label.
    clean_name = _esc(clean_kpi_name(r.name))
    name_html = (
        f'<button type="button" class="fact-doorway">{clean_name}</button>'
        if fact_ref
        else f"<strong>{clean_name}</strong>"
    )
    if r.definition:
        name_html += f'<div class="ledger-def muted xsmall">{_esc(r.definition)}</div>'

    # Wave 5: when this KPI maps to a DCF assumption (name + unit-safe value), a
    # "→ DCF" affordance injects it into the editor's driver and recomputes.
    inject = dcf_inject_for_kpi(clean_kpi_name(r.name), latest_num, r.unit)
    inject_btn = dcf_inject_button(inject[0], inject[1], clean_kpi_name(r.name)) if inject else ""

    lg_data = (
        lg.data_text(
            f"{clean_kpi_name(r.name)} {r.tier} {r.unit or ''} "
            f"{r.current_status} {r.break_condition or ''} {r.source_hint or ''}"
        )
        + lg.data_text_key("tier", r.tier.replace("_", " "))
        + lg.data_text_key("unit", r.unit)
        + lg.data_text_key("status", r.current_status)
        + lg.data_text_key("break_cond", r.break_condition)
        + lg.data_text_key("source", r.source_hint)
        + lg.data_num("latest", latest_num)
    )
    body.write(
        f'<tr class="{row_cls}" data-commentable="true" data-anchor-type="kpi_ledger_row" '
        f'{fact_anchor_attrs(fact_ref, r.name)} data-anchor-tab="thesis"{lg_data}>'
        f"<td>{name_html}</td>"
        f"<td>{_esc(r.tier.replace('_', ' '))}</td>"
        f"<td>{_esc(r.unit or '')}</td>"
        f'<td class="num"{tooltip_attr}>{latest_html}{inject_btn}</td>'
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
        _panel_head(
            "Most underweighted by consensus",
            sub="analyst judgment",
            classes="underweighted-panel",
        )
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
        _panel_head(
            "Valuation summary",
            # S12: the sub slot names the archetype that produced the number
            # (SOTP / NAV, Excess return, FCFF DCF, ...); generic for legacy rows.
            sub=v.valuation_model_label or "DCF",
            as_of=v.valuation_date.isoformat() if v.valuation_date is not None else None,
        )
        + '<div class="val-stack">'
    )
    _sanity_flag_row(body, v)
    # S6: when the dcf_runs snapshot carries Bull/Bear scenario values, the
    # single-point readout becomes a bear·base·bull range with upside at each;
    # runs predating scenarios (or non-FCFF archetypes) keep the legacy row.
    has_range = v.bull_npv_per_share is not None or v.bear_npv_per_share is not None
    if has_range:
        _scenario_range_block(body, v)
    else:
        _val_row(body, "Consolidated NPV / share", _fmt_price(v.consolidated_npv_per_share))
    if v.sum_of_segments_npv_per_share is not None:
        _val_row(body, "Sum-of-segments NPV", _fmt_price(v.sum_of_segments_npv_per_share))
    _val_row(body, "Current price", _fmt_price(v.current_price), emph=True)
    if not has_range and v.current_price and v.consolidated_npv_per_share:
        # The range block already shows upside per scenario — this legacy row
        # would just duplicate its Base cell.
        implied = (v.consolidated_npv_per_share - v.current_price) / v.current_price * 100
        _val_row(body, "Implied vs consolidated", f"{implied:+.0f}%")
    if v.mos_bar is not None:
        _val_row(body, "Margin-of-safety bar", f"{v.mos_bar * 100:.0f}%")
    if v.trigger_status and v.trigger_status != "unknown":
        _val_row(body, "Trigger status", v.trigger_status.upper())
    _priced_in_block(body, v)
    _scenario_prior_block(body, v)
    _assumptions_sync_row(body, v)
    # (Valuation date rides in the header's as-of slot — P4.1 anatomy.)
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


def _sanity_flag_row(body: StringIO, v: ValuationSnapshot) -> None:
    """The DCF trust gate (dcf_runs.sanity_flag, migration 0182), rendered LOUD
    at the top of the valuation stack: the persisted fair value sits past the
    sanity limit, so every number below it is suspect until the model is
    reviewed. Quiet (no row) for an unflagged run."""
    if not v.sanity_flag:
        return
    body.write(
        '<div class="val-row"><span>Model status</span>'
        '<strong class="neg" title="|over/under| exceeds the sanity limit — '
        "more likely a broken model (stale assumptions, unit/FX defect) than a real "
        'mispricing">UNREVIEWED MODEL — fair value flagged as outlier; '
        "review assumptions before trusting this card</strong></div>"
    )


def _assumptions_sync_row(body: StringIO, v: ValuationSnapshot) -> None:
    """The workbook↔assumptions-JSON sync state (S11, dcf_runs 0091).

    A failed sync means workbook edits are NOT mirrored to the from-scratch
    build default — a from-scratch rebuild would revert them — so it renders
    as a loud warning. Quiet (no row) when the run predates the columns or
    the archetype doesn't sync.
    """
    if not v.assumptions_sync_status:
        return
    when = v.assumptions_synced_at.date().isoformat() if v.assumptions_synced_at else ""
    status = v.assumptions_sync_status
    if status.startswith("failed"):
        body.write(
            '<div class="val-row"><span>Assumptions JSON</span>'
            f'<strong class="neg" title="{_esc(status)}">sync FAILED'
            f"{' · ' + _esc(when) if when else ''} — workbook edits not mirrored; "
            "a from-scratch rebuild would revert them</strong></div>"
        )
        return
    label = "created from workbook" if status == "created" else "synced"
    body.write(
        '<div class="val-row muted"><span>Assumptions JSON</span>'
        f"<strong>{_esc(label)}{' · ' + _esc(when) if when else ''}</strong></div>"
    )


def _scenario_range_block(body: StringIO, v: ValuationSnapshot) -> None:
    """Bear · Base · Bull fair values with upside at each, plus a bear→bull
    track showing where the live price sits in the range.

    Base is ``consolidated_npv_per_share`` (the dcf_runs value-of-record);
    Bull/Bear come from the run's scenario snapshot. An un-valuable scenario
    (persisted as null) renders an em-dash cell; the track only draws when both
    ends and the price exist.
    """
    price = v.current_price
    cells: list[tuple[str, str, float | None]] = [
        ("bear", "Bear", v.bear_npv_per_share),
        ("base", "Base", v.consolidated_npv_per_share),
        ("bull", "Bull", v.bull_npv_per_share),
    ]
    body.write('<div class="scenario-range">')
    for key, label, fair_value in cells:
        body.write(f'<div class="scenario-cell {key}">')
        body.write(f'<div class="scenario-label">{_esc(label)}</div>')
        body.write(f"<strong>{_esc(_fmt_price(fair_value))}</strong>")
        if fair_value is not None and price:
            up = (fair_value - price) / price * 100
            tone = "pos" if up >= 0 else "neg"
            body.write(f'<span class="scenario-upside {tone}">{up:+.0f}%</span>')
        else:
            body.write('<span class="scenario-upside muted">—</span>')
        body.write("</div>")
    body.write("</div>")

    bear, bull = v.bear_npv_per_share, v.bull_npv_per_share
    if bear is not None and bull is not None and bull > bear and price:

        def pct_pos(x: float) -> float:
            return max(2.0, min(98.0, (x - bear) / (bull - bear) * 100))

        body.write('<div class="scenario-bar"><div class="scenario-bar-track">')
        base = v.consolidated_npv_per_share
        if base is not None:
            body.write(
                f'<div class="scenario-bar-base" style="left:{pct_pos(base):.1f}%" '
                f'title="Base {_esc(_fmt_price(base))}"></div>'
            )
        body.write(
            f'<div class="scenario-bar-price" style="left:{pct_pos(price):.1f}%" '
            f'title="Price {_esc(_fmt_price(price))}"></div>'
        )
        body.write("</div>")
        body.write(
            '<div class="scenario-bar-legend">'
            f"<span>price ▍{_esc(_fmt_price(price))}</span>"
            "<span>bear → bull</span></div>"
        )
        body.write("</div>")


def _priced_in_block(body: StringIO, v: ValuationSnapshot) -> None:
    """Reverse-DCF "what's priced in": the market-implied assumption set at
    today's price vs the analyst's base case, per lever (implied 5y revenue CAGR
    + implied terminal lever).

    Redesigned FCFF names only; a bespoke archetype renders a one-line n/a (no
    honest single-lever inversion). A pure consumer of the persisted block —
    never recomputed on render.
    """
    card = v.priced_in
    if card is None:
        # Honest n/a for a bespoke archetype ("FCFF DCF" is the only invertible
        # label); stay silent for a pre-block / no-DCF row.
        if v.valuation_model_label and v.valuation_model_label != "FCFF DCF":
            _val_row(body, "Priced in", f"n/a for {v.valuation_model_label} model", muted=True)
        return
    _val_row(body, "Priced in vs your case", "what today's price implies", muted=True)
    for lev in (card.growth, card.terminal):
        gap = lev.gap_display
        if gap is not None:
            value = f"{lev.base_display} -> {lev.implied_display} ({gap})"
        else:
            # Unsolved lever: the price was unreachable inside model bounds.
            value = f"{lev.base_display} -> n/a"
            if lev.note:
                value += f" - {lev.note}"
        _val_row(body, lev.label, value)


_SCEN_SRC_LABEL = {"llm": "LLM", "owner": "owner", "global": "default"}


def _scenario_prior_block(body: StringIO, v: ValuationSnapshot) -> None:
    """Scenario prior on the card: the per-name Bull/Base/Bear odds + the
    probability-weighted expected value E[V], its skew, and the LLM/owner rationale
    (until now only the allocation surfaces saw E[V]/skew). Silent when the run
    carries no scenario_prior block."""
    w = v.scenario_weights
    if w is None:
        return
    src = _SCEN_SRC_LABEL.get(v.scenario_set_by or "", "")
    weights = f"{w['bull'] * 100:.0f}/{w['base'] * 100:.0f}/{w['bear'] * 100:.0f}"
    _val_row(
        body,
        "Scenario prior",
        f"{weights} bull/base/bear" + (f" ({src})" if src else ""),
        muted=True,
    )
    if v.scenario_expected_return is not None:
        skew_txt = f" · skew {v.scenario_skew * 100:+.1f}pts" if v.scenario_skew is not None else ""
        _val_row(body, "Expected value E[V]", f"{v.scenario_expected_return * 100:+.0f}%{skew_txt}")
    if v.scenario_rationale:
        _val_row(body, "Why", v.scenario_rationale)


def _break_rules_panel(body: StringIO, thesis: ThesisSection) -> None:
    rules = thesis.break_rule_evaluations
    if not rules:
        _empty_panel(
            body,
            "Universal break rules",
            "No break-rule evaluations on file for this name yet.",
            reason="not evaluated",
        )
        return
    body.write(
        _panel_head(
            "Universal break rules",
            sub=str(thesis.overall_breach_status),
            as_of=(
                fmt_reltime(thesis.last_evaluated_at.isoformat())
                if thesis.last_evaluated_at is not None
                else None
            ),
        )
    )
    body.write('<table class="tbl"><thead><tr>')
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
    escalation semantics stay visually distinct: hard rules can go RED, soft
    rules can't. `unresolved` (couldn't be evaluated — no data, or a
    data-quality guard tripped) renders in the same amber (`break-status-warn`,
    `var(--warn)`) as a fired rule — visible and distinct from a clean GREEN —
    per `directives/monthly_red_team.md` Phase 1's "never silently green"
    contract; only its label differs (UNRESOLVED vs YELLOW) so a reader can
    tell "flagged" apart from "couldn't check".
    """
    fired = sum(1 for ev in soft_evals if ev.status == "yellow")
    unresolved = sum(1 for ev in soft_evals if ev.status == "unresolved")
    sub = f"{fired} of {len(soft_evals)} fired"
    if unresolved:
        sub += f", {unresolved} unresolved"
    body.write(_panel_head("Soft signals", sub=sub))
    body.write('<table class="tbl"><thead><tr>')
    body.write('<th>Rule</th><th>Evidence</th><th class="num">Status</th></tr></thead><tbody>')
    for ev in soft_evals:
        body.write('<tr class="break-row">')
        body.write(f"<td><strong>{_esc(ev.rule_name)}</strong></td>")
        body.write(f'<td><span class="xsmall muted">{_esc(ev.evidence)}</span></td>')
        status_cls = "break-status-ok" if ev.status == "green" else "break-status-warn"
        label = {"yellow": "YELLOW", "unresolved": "UNRESOLVED"}.get(ev.status, "OK")
        body.write(f'<td class="num {status_cls}">{label}</td>')
        body.write("</tr>")
    body.write("</tbody></table></div>")


def _summarize_rule(r: BreakRuleEvaluation) -> str:
    return f"{r.kpi_name} ({r.consecutive_periods}q)"


# Decision outcome → kit .k-pill tone suffix. correct/wrong/mixed carry status
# meaning (ok/bad/warn); pending + unfalsifiable are the neutral bare pill.
_OUTCOME_PILL_TONE: dict[str, str] = {
    "correct": "k-pill-ok",
    "wrong": "k-pill-bad",
    "mixed": "k-pill-warn",
}


def _decision_history_panel(body: StringIO, decisions: list[DecisionBadge]) -> None:
    """Last 3 LLM recommendations from the decisions audit ledger.

    Skips itself entirely when the list is empty so the thesis tab doesn't
    show a "no decisions yet" placeholder. Each badge surfaces date · kind
    · outcome chip, with the rationale excerpt as a hover tooltip.
    """
    if not decisions:
        return
    body.write(
        _panel_head(
            "Recent decisions",
            sub=f"last {len(decisions)} recommendation{'s' if len(decisions) != 1 else ''}",
            classes="decision-history-panel",
        )
        + '<div class="decision-list">'
    )
    for d in decisions:
        tooltip = f' title="{_esc(d.rationale_short)}"' if d.rationale_short else ""
        # The outcome chip is the control kit's .k-pill (+ tone by meaning); the
        # outer .decision-badge stays the grid layout container. .decision-outcome
        # rides alongside k-pill for its uppercase-micro typographic refinement.
        tone = _OUTCOME_PILL_TONE.get(d.outcome_label, "")
        outcome_cls = f"k-pill {tone} decision-outcome".replace("  ", " ").strip()
        body.write(
            f'<div class="decision-badge"{tooltip}>'
            f'<span class="decision-date mono">{_esc(d.date_short)}</span>'
            f'<span class="decision-kind">{_esc(d.recommendation_kind.upper())}</span>'
            f'<span class="{outcome_cls}">{_esc(d.outcome_label)}</span>'
            "</div>"
        )
    body.write("</div></div>")


def _macro_sensitivity_panel(body: StringIO, rows: list[MacroSensitivityRow]) -> None:
    """P3-18 macro factor sensitivity table — β / R² / lookback per series.

    Hidden when the table has no rows for this ticker (P4.2 hide-don't-stub
    — the Governance coverage report carries the gap). Rows are pre-sorted
    by ``|beta|`` descending by ``load_macro_sensitivities``.
    """
    if not rows:
        return
    body.write(
        _panel_head(
            "Macro factor sensitivity",
            sub=f"{len(rows)} factor{'s' if len(rows) != 1 else ''} "
            f"· lookback {rows[0].lookback_window_days}d",
            classes="macro-sens-panel",
        )
    )
    body.write(
        '<table class="tbl"><thead><tr>'
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
    if not bear.failure_modes:
        # P4.2 hide-don't-stub: the bear tab's own empty state covers the
        # nothing-at-all case; an extra per-panel stub is noise.
        return
    body.write(
        _panel_head(
            "Failure modes",
            sub=f"{len(bear.failure_modes)} hypotheses tracked",
            links=_xlink_html("earnings", "earnings themes ↔", "panel-themes"),
            panel_id="panel-failure-modes",
        )
    )
    for i, fm in enumerate(bear.failure_modes):
        _failure_mode_card(body, i, fm)
    body.write("</div>")


def _failure_mode_card(body: StringIO, idx: int, fm: FailureMode) -> None:
    anchor_key = _esc(fm.hypothesis[:80])
    # Only emit a meta row when its value is present — owner feedback 2026-07-14:
    # the fixed 4-row label grid stacked empty labels and killed the density.
    meta_pairs = (
        ("Evidence", fm.evidence_in_data),
        ("Leading", fm.leading_indicator),
        ("Quant impact", fm.quantitative_impact),
        ("Refutation", fm.refutation_criteria),
    )
    meta = "".join(
        f'<span class="failure-label">{label}</span><span>{_esc(value)}</span>'
        for label, value in meta_pairs
        if value and value.strip()
    )
    meta_html = f'<div class="failure-meta">{meta}</div>' if meta else ""
    body.write(
        f'<div class="failure" data-commentable="true" data-anchor-type="failure_mode" '
        f'data-anchor-key="{anchor_key}" data-anchor-tab="bear">'
        f'<div class="failure-num">{idx + 1:02d}</div>'
        '<div class="failure-body">'
        f'<div class="failure-title">{_esc(fm.hypothesis)}</div>'
        f"{meta_html}</div></div>"
    )
