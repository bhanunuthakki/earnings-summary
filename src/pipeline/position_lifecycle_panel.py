"""Position-lifecycle timeline for the holding page (fund-grade S5, PR 2).

Renders ``position_entries`` (alembic 0088) for one ticker as a vertical
timeline inside the Holding tab's Ops drawer: the open stint first (entry
date/price, conviction, the thesis excerpt and falsifiable conditions
snapshotted at entry), then closed stints newest-first, each carrying the
analyst's post-exit grading — and, when that grading is still blank, an
inline form that POSTs it to ``/api/position-entries/<id>`` (exit_reason ·
lessons · outcome_vs_thesis) and refreshes the section from
``/api/position-lifecycle/<ticker>``.

Live-rendered per fragment request (no report rebuild needed). Styling rides
the token system + control kit only — no raw hex (enrolled in the
test_ui_controls hex guard).
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import cast

from position_lifecycle import OUTCOME_VOCAB, PositionEntry, list_entries

_PANEL_STYLE = """<style>
.plc-timeline { list-style:none; margin:8px 0 0; padding:0; }
.plc-timeline li { position:relative; padding:0 0 14px 22px;
  border-left:2px solid var(--border); margin-left:6px; }
.plc-timeline li:last-child { padding-bottom:2px; }
.plc-timeline li::before { content:""; position:absolute; left:-6px; top:4px;
  width:10px; height:10px; border-radius:var(--radius-full);
  background:var(--muted); border:2px solid var(--surface); }
.plc-timeline li.open::before { background:var(--ok); }
.plc-head { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.plc-dates { font-family:var(--mono); font-weight:600; font-size:var(--fs-body); }
.plc-price { color:var(--muted); font-size:var(--fs-caption); font-family:var(--mono); }
.plc-meta { color:var(--muted); font-size:var(--fs-caption); margin-top:2px; }
.plc-thesis { font-size:var(--fs-caption); line-height:1.5; margin:4px 0 0;
  color:var(--muted); }
.plc-conds { margin:4px 0 0; padding-left:16px; font-size:var(--fs-caption);
  color:var(--muted); }
.plc-conds li { padding:1px 0; border:none; margin:0; }
.plc-conds li::before { display:none; }
.plc-grade { margin-top:6px; display:grid; gap:6px; max-width:520px; }
.plc-grade textarea, .plc-grade select { width:100%; box-sizing:border-box; }
.plc-grade .plc-grade-row { display:flex; gap:8px; align-items:center; }
.plc-lessons { font-size:var(--fs-caption); line-height:1.5; margin:4px 0 0; }
.plc-note { color:var(--muted); font-size:var(--fs-caption); }
</style>"""

_OUTCOME_LABELS: dict[str, str] = {
    "played_out": "thesis played out",
    "broke": "thesis broke",
    "mixed": "mixed",
    "unrelated": "exit unrelated to thesis",
}

# Re-run on every fragment inject (the shell re-creates <script> tags); the
# data-wired guard keeps a re-inject from double-wiring forms already present.
_PANEL_SCRIPT = """<script>
(function () {
  var root = document.querySelector('[data-plc-root]');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  root.addEventListener('submit', function (ev) {
    var form = ev.target.closest('form[data-plc-grade]');
    if (!form) return;
    ev.preventDefault();
    var entryId = form.getAttribute('data-plc-grade');
    var payload = {
      exit_reason: (form.querySelector('[name=exit_reason]') || {}).value || '',
      lessons: (form.querySelector('[name=lessons]') || {}).value || '',
      outcome_vs_thesis: (form.querySelector('[name=outcome_vs_thesis]') || {}).value || ''
    };
    fetch('/api/position-entries/' + entryId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (!r.ok) throw new Error('save failed (' + r.status + ')');
      return fetch('/api/position-lifecycle/' + root.getAttribute('data-plc-ticker'));
    }).then(function (r) { return r.text(); }).then(function (html) {
      var holder = root.parentElement;
      root.outerHTML = html;
      void holder; // the fresh fragment carries its own script + wiring guard
    }).catch(function (err) {
      var note = form.querySelector('.plc-note');
      if (note) note.textContent = String(err);
    });
  });
})();
</script>"""


def render_position_lifecycle_section(db_path: Path, ticker: str, *, user_id: str) -> str:
    """The Ops-drawer section: timeline of this name's lifecycle rows, with an
    inline grading form on closed-but-ungraded stints. Empty-state explains
    where rows come from (the morning reconciler)."""
    t = ticker.upper()
    entries = list_entries(db_path=db_path, ticker=t, user_id=user_id)
    inner: str
    if not entries:
        inner = (
            '<p class="muted">No lifecycle rows yet. The morning reconciler opens one when '
            "this name enters the portfolio (and closes it on exit) — entry price/date come "
            "from the tracker when it's online.</p>"
        )
    else:
        inner = (
            _PANEL_STYLE
            + '<ul class="plc-timeline">'
            + "".join(_entry_li(e) for e in entries)
            + "</ul>"
            + _PANEL_SCRIPT
        )
    return (
        f'<section class="panel" data-plc-root data-plc-ticker="{escape(t, quote=True)}">'
        f"<h2>Position lifecycle</h2>{inner}</section>"
    )


def _entry_li(entry: PositionEntry) -> str:
    cls = "open" if entry.is_open else "closed"
    parts: list[str] = [f'<li class="{cls}">', '<div class="plc-head">']

    entry_label = entry.entry_date or "date unknown"
    if entry.is_open:
        parts.append(f'<span class="plc-dates">{escape(entry_label)} → open</span>')
        parts.append('<span class="k-pill k-pill-ok">OPEN</span>')
    else:
        parts.append(
            f'<span class="plc-dates">{escape(entry_label)} → {escape(entry.exit_date or "?")}</span>'
        )
        outcome = entry.outcome_vs_thesis
        if outcome:
            tone = {"broke": "k-pill-bad", "played_out": "k-pill-ok"}.get(outcome, "")
            cls = f"k-pill {tone}".strip()
            parts.append(
                f'<span class="{cls}">{escape(_OUTCOME_LABELS.get(outcome, outcome))}</span>'
            )
    parts.append(_price_span(entry))
    if entry.entry_conviction:
        parts.append(f'<span class="k-pill">{escape(entry.entry_conviction)} conviction</span>')
    parts.append("</div>")

    meta_bits: list[str] = [f"source: {entry.source}"]
    if entry.entry_date is None:
        meta_bits.append("opened before this ledger existed")
    parts.append(f'<div class="plc-meta">{escape(" · ".join(meta_bits))}</div>')

    if entry.entry_thesis_excerpt:
        parts.append(
            f'<p class="plc-thesis">Entry thesis: {escape(entry.entry_thesis_excerpt)}</p>'
        )
    parts.append(_conditions_list(entry.entry_conditions))

    if not entry.is_open:
        if entry.exit_reason:
            parts.append(
                f'<p class="plc-lessons"><strong>Why exited:</strong> {escape(entry.exit_reason)}</p>'
            )
        if entry.lessons:
            parts.append(
                f'<p class="plc-lessons"><strong>Lessons:</strong> {escape(entry.lessons)}</p>'
            )
        if not (entry.exit_reason and entry.lessons and entry.outcome_vs_thesis):
            parts.append(_grade_form(entry))

    parts.append("</li>")
    return "".join(parts)


def _price_span(entry: PositionEntry) -> str:
    def fmt(price: float | None) -> str:
        return f"${price:,.2f}" if price is not None else "—"

    if entry.is_open:
        label = f"entry {fmt(entry.entry_price)}"
    else:
        label = f"{fmt(entry.entry_price)} → {fmt(entry.exit_price)}"
        if entry.entry_price and entry.exit_price:
            change = (entry.exit_price / entry.entry_price - 1.0) * 100.0
            label += f" ({change:+.1f}%)"
    return f'<span class="plc-price">{escape(label)}</span>'


def _conditions_list(raw: str | None) -> str:
    """The entry-time falsifiable-conditions snapshot, as a compact list.
    Tolerant of a corrupt column (renders nothing)."""
    if not raw:
        return ""
    try:
        decoded: object = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(decoded, list) or not decoded:
        return ""
    items: list[str] = []
    for raw_cond in cast("list[object]", decoded)[:6]:
        if not isinstance(raw_cond, dict):
            continue
        cond = cast("dict[str, object]", raw_cond)
        metric = cond.get("metric")
        op = cond.get("op")
        threshold = cond.get("threshold")
        unit = cond.get("unit")
        if not isinstance(metric, str):
            continue
        op_label = {"lt": "<", "le": "<=", "gt": ">", "ge": ">="}.get(str(op), str(op))
        items.append(
            f"<li>{escape(metric)} {escape(op_label)} {escape(str(threshold))} "
            f"{escape(str(unit or ''))}</li>"
        )
    if not items:
        return ""
    return (
        '<ul class="plc-conds" title="Falsifiable conditions snapshotted at entry">'
        + "".join(items)
        + "</ul>"
    )


def _grade_form(entry: PositionEntry) -> str:
    """Inline post-exit grading for a closed stint missing any of the three
    fields. Pre-fills what exists; the script POSTs and refreshes."""
    options = ['<option value="">outcome vs thesis…</option>']
    for value in sorted(OUTCOME_VOCAB):
        selected = " selected" if entry.outcome_vs_thesis == value else ""
        options.append(
            f'<option value="{escape(value)}"{selected}>{escape(_OUTCOME_LABELS[value])}</option>'
        )
    return (
        f'<form class="plc-grade" data-plc-grade="{entry.id}">'
        f'<textarea name="exit_reason" rows="2" placeholder="Why did you exit?">'
        f"{escape(entry.exit_reason or '')}</textarea>"
        f'<textarea name="lessons" rows="2" placeholder="What did this position teach you?">'
        f"{escape(entry.lessons or '')}</textarea>"
        '<div class="plc-grade-row">'
        f'<select name="outcome_vs_thesis">{"".join(options)}</select>'
        '<button type="submit" class="k-btn">Save grading</button>'
        '<span class="plc-note"></span>'
        "</div></form>"
    )
