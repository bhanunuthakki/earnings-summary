"""Thesis-ledger panel for the command-center shell.

Surfaces ``thesis_ledger_entries`` — the append-only history of every accepted,
alert-driven thesis edit (a thesis update, a bear-case append, an earnings-prep
note). This is the populated decision history the user actually built via the
alert -> queued-action -> approve loop; before this it reached the eye only
through the ``/digest`` route, never as a discoverable command-center tab (the
"Decisions" tab reads the separate, audit-only ``decisions`` table, which is
empty until decisions are scored). v6 re-grade, Richness & surfacing.

Reads via ``user_state.ledger.list_recent_entries`` (cross-ticker, newest-first).
Reuses the shell's dark panel / kpi-strip / table CSS vocabulary.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from html import escape
from pathlib import Path

from user_state.ledger import ThesisLedgerEntryRow, list_recent_entries

# Human labels for entry_kind — kept in lockstep with src/dashboard/digest.py.
_KIND_LABELS: Mapping[str, str] = {
    "thesis_update": "Thesis update",
    "bear_append": "Bear case",
    "earnings_prep_append": "Earnings prep",
}

_PANEL_STYLE = """<style>
.tl-table { width:100%; border-collapse:collapse; font-size:13px; }
.tl-table th, .tl-table td {
  padding:7px 10px; border-bottom:1px solid var(--border,#2a2d31); text-align:left;
  vertical-align:top; }
.tl-table td.tk { font-weight:600; white-space:nowrap; }
.tl-table td.when { color:var(--muted,#9aa0a6); white-space:nowrap; }
.tl-pill { display:inline-block; padding:2px 9px; border-radius:10px; font-size:11px;
  font-weight:600; white-space:nowrap; background:#1f2b3a; color:#8fb6e6; }
.tl-pill.bear_append { background:#3a1f1f; color:#f0a0a0; }
.tl-pill.thesis_update { background:#14361f; color:#6ee7a0; }
.tl-body { font-size:12.5px; line-height:1.5; }
</style>"""


def render_thesis_ledger_panel(db_path: Path, *, user_id: str) -> str:
    """The Thesis Ledger tab fragment: a KPI strip (entries · tickers · by-kind) +
    a newest-first table of accepted thesis edits. Empty-state when the ledger is
    empty / absent."""
    entries = list_recent_entries(user_id=user_id, limit=200, db_path=db_path)
    if not entries:
        return (
            '<section class="panel"><h2>Thesis ledger</h2>'
            '<p class="muted">No accepted thesis changes yet. Approving a queued action from '
            "an alert appends a durable entry here (a thesis update, bear-case append, or "
            "earnings-prep note).</p></section>"
        )
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>Thesis ledger</h2>',
            '<p class="sub">The append-only history of every accepted, alert-driven thesis '
            "edit — the decisions you actually committed via the alert &rarr; queued-action "
            "&rarr; approve loop.</p>",
            _kpi_strip(entries),
            _ledger_table(entries),
            "</section>",
        ]
    )


def _kpi_strip(entries: list[ThesisLedgerEntryRow]) -> str:
    by_kind: Counter[str] = Counter(e.entry_kind for e in entries)
    tickers = len({e.ticker for e in entries})
    top_kinds = ", ".join(f"{_KIND_LABELS.get(k, k)} {n}" for k, n in by_kind.most_common(3))
    return (
        '<div class="kpi-strip">'
        '<div class="kpi-card tone-good"><div class="kpi-label">Ledger entries</div>'
        f'<div class="kpi-value">{len(entries)}</div>'
        '<div class="kpi-sub">accepted thesis edits</div></div>'
        '<div class="kpi-card"><div class="kpi-label">Tickers</div>'
        f'<div class="kpi-value">{tickers}</div>'
        '<div class="kpi-sub">with a ledger history</div></div>'
        '<div class="kpi-card"><div class="kpi-label">By kind</div>'
        f'<div class="kpi-value" style="font-size:15px">{escape(top_kinds) or "—"}</div>'
        '<div class="kpi-sub">most common</div></div>'
        "</div>"
    )


def _ledger_table(entries: list[ThesisLedgerEntryRow]) -> str:
    body = "".join(_row(e) for e in entries)
    return (
        '<table class="tl-table"><thead><tr>'
        "<th>Date</th><th>Ticker</th><th>Kind</th><th>Change</th>"
        "</tr></thead><tbody>"
        f"{body}</tbody></table>"
    )


def _row(e: ThesisLedgerEntryRow) -> str:
    label = _KIND_LABELS.get(e.entry_kind, e.entry_kind)
    pill_cls = escape(e.entry_kind)
    when = escape(e.created_at.date().isoformat())
    return (
        "<tr>"
        f'<td class="when">{when}</td>'
        f'<td class="tk">{escape(e.ticker)}</td>'
        f'<td><span class="tl-pill {pill_cls}">{escape(label)}</span></td>'
        f'<td class="tl-body">{escape(e.body)}</td>'
        "</tr>"
    )
