"""Say-Do tab: pairwise cards, print-vs-guide, verdicts overlay, historical ledger.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from datetime import date, datetime
from io import StringIO
from pathlib import Path

from report.models import ReportSpec, SayDoCard, SayDoHistoricalMetric, SayDoSection
from report.render_clock import render_today
from report.renderers.workspace_charts import verdict_bar
from report.renderers.workspace_data import (
    PrintVsGuideRow,
    filter_important_print_vs_guide,
    parse_print_vs_guide,
)
from report.renderers.workspace_sections._shared import (
    _esc,
    _inline_md,
    _missing_panel,
    _panel_head,
    _quarter_selector,
    _render_markdown,
)
from report.sections.p3_data import SayDoVerdictRow
from ui import living_grid as lg

__all__ = [
    "_fmt_made_period",
    "_outcome_pill",
    "_rating_pill",
    "_saydo_historical_ledger",
    "_saydo_print_vs_guide",
    "_saydo_summary_table",
    "_saydo_tab",
    "_saydo_verdicts_panel",
]


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
            body.write(f'<p class="lede">{_inline_md(card.thesis_view)}</p>')
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
        # (No parsed print-vs-guide table → no panel; the full narrative
        # below carries the comparison in prose. P4.2 hide-don't-stub.)

        body.write('<div class="grid-2col">')
        body.write(_panel_head("Thesis view", sub="Post-print read-through"))
        body.write(
            f'<div class="prose-pad">{_render_markdown(card.thesis_view or "—")}</div></div>'
        )
        body.write(_panel_head("Attribution", sub="Execution · exogenous · luck"))
        body.write(
            f'<div class="prose-pad">{_render_markdown(card.attribution or "—")}</div></div>'
        )
        body.write("</div>")

        body.write(
            _panel_head(
                "Full Say·Do narrative",
                sub=f"{card.current_quarter} {card.current_year}",
            )
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
    Hidden when the table has no rows for this ticker (P4.2 hide-don't-stub
    — the Governance coverage report carries the gap).
    """
    if not rows:
        return
    today = render_today()
    graded = sum(1 for r in rows if r.outcome is not None)
    n = len(rows)
    body.write(
        _panel_head(
            "Say·Do verdict ledger",
            sub=f"{n} commitment{'s' if n != 1 else ''} tracked · {graded} with a reported result",
            classes="saydo-verdicts-panel",
        )
    )
    if graded == 0:
        # All commitments are still open — the target quarters haven't reported
        # yet (or the reported figure hasn't matched). This is a legitimate
        # "not due yet" state, NOT missing data — owner feedback 2026-07-14:
        # the bare "0 graded / NO DATA" read as broken.
        body.write(
            '<p class="muted saydo-pending-note">None have a reported result yet — '
            "each commitment is graded automatically once its target quarter prints. "
            "The status column shows how long each has to run.</p>"
        )
    body.write(lg.grid_open())
    body.write(lg.filter_bar(len(rows), noun="commitments"))
    body.write(
        '<table class="tbl"><thead><tr>'
        + lg.th("Made", "made", "text", num=False)
        + lg.th("Target period", "target", "text", num=False)
        + lg.th("KPI", "kpi", "text", num=False)
        + "<th>Promised</th>"
        + "<th>Realized</th>"
        + lg.th("Verdict", "verdict", "text", num=False)
        + "</tr></thead><tbody>"
    )
    comp_map = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<", "eq": "≈"}
    for r in rows:
        promise = f"{comp_map.get(r.comparator.lower(), r.comparator)} {r.target_value:g} {r.unit}"
        if r.realized_value is not None:
            realized = f"{r.realized_value:g} {r.unit}"
        else:
            realized = '<span class="muted">—</span>'
        outcome = r.outcome if r.outcome else "no_data"
        outcome_pill = (
            _outcome_pill(outcome)
            if r.outcome
            else _saydo_pending_pill(r.period_target.date(), today)
        )
        made_label = _fmt_made_period(r.period_made)
        target_label = _fmt_made_period(r.period_target)
        # data-made/-target are ISO dates so the text sort orders chronologically
        # (the displayed cell is the quarter label).
        data = (
            lg.data_text(f"{made_label} {r.kpi_name} {promise} {outcome}")
            + lg.data_text_key("made", r.period_made.isoformat())
            + lg.data_text_key("target", r.period_target.isoformat())
            + lg.data_text_key("kpi", r.kpi_name)
            + lg.data_text_key("verdict", outcome)
        )
        body.write(f"<tr{data}>")
        body.write(f'<td class="mono xsmall">{_esc(made_label)}</td>')
        body.write(f'<td class="mono xsmall">{_esc(target_label)}</td>')
        body.write(f'<td class="saydo-metric">{_inline_md(r.kpi_name)}</td>')
        body.write(f'<td class="saydo-guide">{_esc(promise)}</td>')
        body.write(f'<td class="saydo-actual"><strong>{realized}</strong></td>')
        body.write(f"<td>{outcome_pill}</td>")
        body.write("</tr>")
    body.write("</tbody></table>")
    body.write(lg.grid_close())
    body.write("</div>")


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
        _panel_head("SayDo summary — all tracked pairs", sub=f"{len(cards)} pairs · newest first")
        + '<table class="tbl"><thead><tr>'
        "<th>Pair</th><th>Rating</th><th>Attribution</th><th>Thesis view</th>"
        "</tr></thead><tbody>"
    )
    for c in cards:
        body.write(
            "<tr>"
            f'<td class="saydo-metric mono">{_esc(c.prior_quarter)} {c.prior_year} '
            f"→ {_esc(c.current_quarter)} {c.current_year}</td>"
            f"<td>{_rating_pill(c.rating)}</td>"
            f'<td class="saydo-guide">{_inline_md((c.attribution or "—")[:140])}</td>'
            f'<td class="saydo-guide">{_inline_md((c.thesis_view or "—")[:140])}</td>'
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
        dropped = total_parsed - len(rows)
        sub = (
            f"showing {len(rows)} of {total_parsed} — {dropped} set aside as not "
            "thesis-relevant (not missing data)"
        )
    else:
        sub = f"{len(rows)} commitments scored"
    body.write(_panel_head("Print vs guide", sub=sub))
    body.write('<table class="tbl"><thead><tr>')
    body.write("<th>Metric</th>")
    body.write(f"<th>{_esc(card.prior_quarter)} {card.prior_year} actual</th>")
    body.write(f"<th>{_esc(card.current_quarter)} {card.current_year} actual</th>")
    body.write("<th>Verdict</th></tr></thead><tbody>")
    for r in rows:
        body.write("<tr>")
        body.write(f'<td class="saydo-metric">{_inline_md(r.metric)}</td>')
        body.write(f'<td class="saydo-guide">{_inline_md(r.guide)}</td>')
        body.write(f'<td class="saydo-actual"><strong>{_inline_md(r.actual)}</strong></td>')
        body.write(f"<td>{_rating_pill(r.verdict)}</td>")
        body.write("</tr>")
    body.write("</tbody></table></div>")


# Only ok/warn/bad carry a kit chip tone; neutral/muted ride the tone-less
# k-chip-mono (the report's old pill-neutral/-muted had no status color).
_CHIP_TONE = {"ok": " k-chip-ok", "warn": " k-chip-warn", "bad": " k-chip-bad"}


def _rating_pill(rating: str) -> str:
    mapping = {
        "EXCEEDED": "ok",
        "MET": "neutral",
        "MIXED": "warn",
        "MISSED": "bad",
        "REVISED UP": "warn",
        "unknown": "muted",
    }
    tone = _CHIP_TONE.get(mapping.get(rating, "muted"), "")
    return f'<span class="k-chip k-chip-mono{tone}">{_esc(rating)}</span>'


def _outcome_pill(outcome: str) -> str:
    mapping = {
        "beat": ("ok", "BEAT"),
        "hit": ("ok", "HIT"),
        "miss": ("bad", "MISS"),
        "no_data": ("muted", "NO DATA"),
    }
    tone_name, label = mapping.get(outcome.lower(), ("muted", outcome.upper()))
    tone = _CHIP_TONE.get(tone_name, "")
    return f'<span class="k-chip k-chip-mono{tone}">{_esc(label)}</span>'


def _saydo_pending_pill(period_target: date, today: date) -> str:
    """A self-explanatory pill for an as-yet-ungraded commitment.

    Replaces the bare "NO DATA" (owner feedback 2026-07-14: it read as broken
    data). A commitment is ungraded because its target quarter either hasn't
    arrived (AWAITING) or has passed without a matched print yet (PENDING) —
    both legitimate open states, distinguished so the reader can tell "not due"
    from "overdue".
    """
    if period_target > today:
        label, title = "AWAITING", f"Target {_quarter_label(period_target)} hasn't reported yet"
    else:
        label, title = (
            "PENDING",
            f"Target {_quarter_label(period_target)} has passed — awaiting the reported figure",
        )
    return f'<span class="k-chip k-chip-mono" title="{_esc(title)}">{label}</span>'


def _quarter_label(d: date) -> str:
    """``Q3 '25`` from a date (mirrors ``_fmt_made_period`` for a plain date)."""
    quarter = (d.month - 1) // 3 + 1
    return f"Q{quarter} {chr(0x2019)}{str(d.year)[2:]}"


def _saydo_historical_ledger(body: StringIO, metrics: list[SayDoHistoricalMetric]) -> None:
    """Persistent guidance-outcomes ledger sourced from saydo_historical_metrics."""
    body.write(
        _panel_head(
            "Persistent guidance outcomes ledger",
            sub=f"{len(metrics)} tracked commitments",
        )
    )
    body.write('<table class="tbl"><thead><tr>')
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
        body.write(f'<td class="saydo-metric">{_inline_md(m.kpi_name)}</td>')
        body.write(f'<td class="mono">{_esc(comp_symbol)}</td>')
        body.write(f'<td class="saydo-guide">{_esc(target_display)}</td>')
        body.write(f'<td class="saydo-actual"><strong>{_esc(realized_display)}</strong></td>')
        body.write(f"<td>{_outcome_pill(outcome_label)}</td>")
        body.write(f'<td class="saydo-guide">{_esc(_fmt_period(m.period_made))}</td>')
        body.write(f'<td class="saydo-actual">{_esc(_fmt_period(m.period_target))}</td>')
        body.write("</tr>")
    body.write("</tbody></table></div>")
