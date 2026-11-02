"""Say-Do tab for recorded commitments and guidance outcomes."""

from __future__ import annotations

from datetime import date, datetime
from io import StringIO

from report.models import CellSource, SayDoHistoricalMetric, SayDoSection, SayDoVerdictRow
from report.render_clock import render_today
from report.renderers.workspace_sections._shared import (
    _esc,
    _inline_md,
    _missing_panel,
    _panel_head,
)
from ui import living_grid as lg
from ui.source_chip import viewer_href

__all__ = ["_fmt_made_period", "_outcome_pill", "_saydo_tab", "_saydo_verdicts_panel"]


def _saydo_tab(body: StringIO, section: SayDoSection) -> None:
    body.write('<div class="tab-body">')
    if section.status.value != "ok":
        _missing_panel(body, section.status, section.missing)
    _saydo_verdicts_panel(body, section.verdicts)
    if section.historical_metrics:
        _saydo_historical_ledger(body, section.historical_metrics)
    body.write("</div>")


def _saydo_verdicts_panel(body: StringIO, rows: list[SayDoVerdictRow]) -> None:
    """Render the source-linked management commitment ledger."""
    if not rows:
        return
    today = render_today()
    graded = sum(1 for row in rows if row.outcome is not None)
    body.write(
        _panel_head(
            "Say·Do commitment ledger",
            sub=f"{len(rows)} commitment{'s' if len(rows) != 1 else ''} tracked · "
            f"{graded} with a reported result",
            classes="saydo-verdicts-panel",
        )
    )
    if graded == 0:
        body.write(
            '<p class="muted saydo-pending-note">None have a reported result yet. '
            "The status column shows how long each commitment has to run.</p>"
        )
    body.write(lg.grid_open())
    body.write(lg.filter_bar(len(rows), noun="commitments"))
    body.write(
        '<table class="tbl"><thead><tr>'
        + lg.th("Made", "made", "text", num=False)
        + lg.th("Target period", "target", "text", num=False)
        + lg.th("KPI", "kpi", "text", num=False)
        + "<th>Promised</th><th>Realized</th>"
        + lg.th("Verdict", "verdict", "text", num=False)
        + "<th>Source</th>"
        + "</tr></thead><tbody>"
    )
    comp_map = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<", "eq": "≈"}
    for row in rows:
        promise = f"{comp_map.get(row.comparator.lower(), row.comparator)} {row.target_value:g} {row.unit}"
        realized = (
            f"{row.realized_value:g} {_esc(row.unit)}"
            if row.realized_value is not None
            else '<span class="muted">—</span>'
        )
        outcome = row.outcome or "no_data"
        outcome_pill = (
            _outcome_pill(outcome)
            if row.outcome
            else _saydo_pending_pill(row.period_target.date(), today)
        )
        made_label = _fmt_made_period(row.period_made)
        target_label = _fmt_made_period(row.period_target)
        data = (
            lg.data_text(f"{made_label} {row.kpi_name} {promise} {outcome}")
            + lg.data_text_key("made", row.period_made.isoformat())
            + lg.data_text_key("target", row.period_target.isoformat())
            + lg.data_text_key("kpi", row.kpi_name)
            + lg.data_text_key("verdict", outcome)
        )
        body.write(f"<tr{data}>")
        body.write(f'<td class="mono xsmall">{_esc(made_label)}</td>')
        body.write(f'<td class="mono xsmall">{_esc(target_label)}</td>')
        body.write(f'<td class="saydo-metric">{_inline_md(row.kpi_name)}</td>')
        body.write(f'<td class="saydo-guide">{_esc(promise)}</td>')
        body.write(f'<td class="saydo-actual"><strong>{realized}</strong></td>')
        body.write(f"<td>{outcome_pill}</td>")
        source = CellSource(
            source="transcript",
            doc_id=row.source_document_id,
            source_url=row.source_url,
        )
        source_href = viewer_href(source)
        source_cell = (
            f'<a href="{_esc(source_href)}" target="_blank" rel="noopener">Transcript ↗</a>'
            if source_href is not None
            else '<span class="muted">Source unavailable</span>'
        )
        body.write(f"<td>{source_cell}</td></tr>")
    body.write("</tbody></table>")
    body.write(lg.grid_close())
    body.write("</div>")


def _fmt_made_period(value: datetime) -> str:
    quarter = (value.month - 1) // 3 + 1
    return f"Q{quarter} {chr(0x2019)}{str(value.year)[2:]}"


def _outcome_pill(outcome: str) -> str:
    mapping = {
        "beat": ("ok", "BEAT"),
        "hit": ("ok", "HIT"),
        "met": ("ok", "MET"),
        "miss": ("bad", "MISS"),
        "missed": ("bad", "MISSED"),
        "mixed": ("warn", "MIXED"),
        "partial": ("warn", "PARTIAL"),
        "no_data": ("muted", "NO DATA"),
    }
    tone, label = mapping.get(outcome.lower(), ("muted", outcome.upper()))
    tone_class = {"ok": " k-chip-ok", "warn": " k-chip-warn", "bad": " k-chip-bad"}.get(tone, "")
    return f'<span class="k-chip k-chip-mono{tone_class}">{_esc(label)}</span>'


def _saydo_pending_pill(period_target: date, today: date) -> str:
    if period_target > today:
        label, title = "AWAITING", f"Target {_quarter_label(period_target)} has not reported yet"
    else:
        label, title = "PENDING", f"Target {_quarter_label(period_target)} has passed"
    return f'<span class="k-chip k-chip-mono" title="{_esc(title)}">{label}</span>'


def _quarter_label(value: date) -> str:
    quarter = (value.month - 1) // 3 + 1
    return f"Q{quarter} {chr(0x2019)}{str(value.year)[2:]}"


def _saydo_historical_ledger(body: StringIO, metrics: list[SayDoHistoricalMetric]) -> None:
    """Render the retained guidance outcomes history."""
    body.write(
        _panel_head(
            "Historical guidance comparisons",
            sub=f"{len(metrics)} recorded commitments",
        )
    )
    body.write('<table class="tbl"><thead><tr>')
    body.write("<th>Metric</th><th>Comparator</th><th>Guidance target</th>")
    body.write(
        "<th>Realized value</th><th>Outcome</th><th>Guidance period</th><th>Target period</th>"
    )
    body.write("</tr></thead><tbody>")
    comp_map = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<", "eq": "≈"}
    for metric in metrics:
        comparator = comp_map.get(metric.comparator.lower(), metric.comparator)
        target = f"{metric.target_value:g}"
        realized = f"{metric.realized_value:g}" if metric.realized_value is not None else "—"
        body.write("<tr>")
        body.write(f'<td class="saydo-metric">{_inline_md(metric.kpi_name)}</td>')
        body.write(f'<td class="mono">{_esc(comparator)}</td>')
        body.write(
            '<td class="saydo-guide">'
            f'{_esc(target)} <span class="muted xsmall">(unit unavailable)</span>'
        )
        if metric.guidance_narrative:
            body.write(f'<div class="muted xsmall">{_esc(metric.guidance_narrative)}</div>')
        body.write("</td>")
        body.write(
            '<td class="saydo-actual"><strong>'
            f'{_esc(realized)}</strong> <span class="muted xsmall">(unit unavailable)</span>'
        )
        if metric.realized_narrative:
            body.write(f'<div class="muted xsmall">{_esc(metric.realized_narrative)}</div>')
        body.write("</td>")
        body.write(f"<td>{_outcome_pill(metric.outcome or 'no_data')}</td>")
        body.write(f'<td class="saydo-guide">{_esc(_fmt_made_period(metric.period_made))}</td>')
        body.write(f'<td class="saydo-actual">{_esc(_fmt_made_period(metric.period_target))}</td>')
        body.write("</tr>")
    body.write("</tbody></table></div>")
