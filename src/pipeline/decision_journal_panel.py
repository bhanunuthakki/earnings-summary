"""Ledger -> Decisions fragment for confirmed owner decisions.

This is deliberately separate from ``pipeline.journal_panel``, which owns the
analyst-notes lifecycle, and from the Allocation page's compact decision list.
This fragment is the Ledger console's full owner-decision timeline.

The filters show owner decisions and decisions with a recorded process grade.
An owner can record Sound or Flawed for a matured decision. Need more evidence
leaves the process grade empty. Lucky remains a system-only outcome label.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from pipeline.research_panel_styles import RESEARCH_PANEL_STYLE
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import panel_toolbar, ticker_label

_PANEL_STYLE = RESEARCH_PANEL_STYLE

_FILTERS: tuple[str, ...] = ("owner", "lessons")
_FILTER_LABEL: dict[str, str] = {
    "owner": "Owner Decisions",
    "lessons": "Process lessons",
}

_WHERE_BY_FILTER: dict[str, str] = {
    "owner": "decided_by = 'owner'",
    "lessons": "decided_by = 'owner' AND process_quality IS NOT NULL",
}


def _open(db_path: Path) -> sqlite3.Connection | None:
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM v_decision_journal LIMIT 1")
        return conn
    except sqlite3.Error:
        return None


def _process_quality_prompt(decision_id: int) -> str:
    return (
        f'<div class="dj-pq" data-pq-decision="{decision_id}">'
        '<span class="k-label">Process?</span>'
        f'<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-pq="sound" '
        f'data-decision-id="{decision_id}">Sound</button>'
        f'<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-pq="flawed" '
        f'data-decision-id="{decision_id}">Flawed</button>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-pq="skip">'
        "Need more evidence</button>"
        "</div>"
    )


def _row_html(r: sqlite3.Row) -> str:
    ticker = str(r["ticker"] or "")
    ticker_html = ticker_label(ticker) if ticker else '<span class="k-chip">PORTFOLIO</span>'
    kind = str(r["recommendation_kind"] or "").upper()
    decided_by = str(r["decided_by"] or "advisor")
    kind_chip = f'<span class="k-chip dj-kind">{escape(decided_by)}: {escape(kind)}</span>'
    made_at = str(r["made_at"] or "")[:10]
    outcome = ""
    if r["outcome_label"]:
        tone = {"correct": "ok", "wrong": "bad"}.get(str(r["outcome_label"]), "")
        cls = f"k-pill k-pill-{tone}" if tone else "k-pill"
        outcome = f'<span class="{cls}">{escape(str(r["outcome_label"]))}</span>'
    pq = ""
    if r["process_quality"]:
        pq = f'<span class="k-chip">process: {escape(str(r["process_quality"]))}</span>'
    elif r["outcome_at"] is not None:
        pq = _process_quality_prompt(int(r["decision_id"]))
    falsifier = ""
    if r["falsifier"]:
        falsifier = f'<div class="dj-note">Falsifier: {escape(str(r["falsifier"]))}</div>'
    return (
        f'<div class="dj-row" data-decision-id="{int(r["decision_id"])}">'
        f'<span class="dj-when">{escape(made_at)}</span>'
        f"{ticker_html}{kind_chip}{outcome}{pq}"
        f"{falsifier}"
        "</div>"
    )


def render_decision_journal_list(db_path: Path, *, filter_: str = "owner", limit: int = 100) -> str:
    """Render a filtered row list, with an empty state when the view is unavailable."""
    if filter_ not in _FILTERS:
        filter_ = "owner"
    conn = _open(db_path)
    if conn is None:
        return (
            '<div class="dj-empty">Decision journal unavailable -- run '
            "<code>alembic upgrade head</code> to create v_decision_journal.</div>"
        )
    try:
        where = _WHERE_BY_FILTER[filter_]
        rows = conn.execute(
            f"SELECT * FROM v_decision_journal WHERE {where} ORDER BY made_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return '<div class="dj-empty">Decision journal unavailable.</div>'
    finally:
        conn.close()
    if not rows:
        return (
            f'<div class="dj-empty">No decisions match &ldquo;'
            f"{escape(_FILTER_LABEL[filter_])}&rdquo; yet.</div>"
        )
    return "".join(_row_html(r) for r in rows)


def render_decision_journal_panel(
    db_path: Path, *, filter_: str = "owner", embedded: bool = False
) -> str:
    """The Ledger -> Decisions section: filter chips + the row list."""
    if filter_ not in _FILTERS:
        filter_ = "owner"
    chips = "".join(
        f'<button type="button" class="k-chip k-chip-btn{" k-chip-accent" if f == filter_ else ""}" '
        f'data-dj-filter="{f}">{escape(_FILTER_LABEL[f])}</button>'
        for f in _FILTERS
    )
    heading = '<h3 class="dj-h">Decisions</h3>' if embedded else "<h2>Decisions</h2>"
    body = render_decision_journal_list(db_path, filter_=filter_)
    # Embedded (inside the Ledger console): the console owns the ONE
    # `.k-toolbar` band (test_ledger_console_renders_one_chrome pins it), so
    # the filter chips ride a plain layout row. Standalone keeps the kit
    # toolbar.
    chip_row = f'<div class="dj-chips">{chips}</div>' if embedded else panel_toolbar(filters=chips)
    return f"""{_PANEL_STYLE}
{heading}
<div id="dj-root" data-active-filter="{filter_}">
{chip_row}
<div id="dj-feedback" class="k-well" role="status" aria-live="polite" hidden></div>
<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-dj-retry hidden>Retry</button>
<div id="dj-list">{body}</div>
</div>
<script>
(function () {{
  var root = document.getElementById('dj-root');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  var generation = 0;
  var controller = null;
  var requestedFilter = root.getAttribute('data-active-filter') || 'owner';
  function loadFilter(filter) {{
    requestedFilter = filter;
    var request = ++generation;
    if (controller) controller.abort();
    controller = new AbortController();
    var status = root.querySelector('#dj-feedback');
    var retry = root.querySelector('[data-dj-retry]');
    status.textContent = 'Loading decisions…'; status.hidden = false; retry.hidden = true;
    var qs = new URLSearchParams({{fragment: 'list', filter: filter}});
    window.uiFetch('/api/panel/ledger_decisions?' + qs, {{signal: controller.signal}})
      .then(function (r) {{ return r.text(); }})
      .then(function (html) {{
        if (request !== generation || !root.isConnected) return;
        root.querySelector('#dj-list').innerHTML = html;
        root.setAttribute('data-active-filter', filter);
        root.querySelectorAll('[data-dj-filter]').forEach(function (b) {{
          b.classList.toggle('k-chip-accent', b.getAttribute('data-dj-filter') === filter);
        }});
        status.hidden = true;
      }})
      .catch(function () {{
        if (request !== generation || !root.isConnected) return;
        status.textContent = 'Decisions could not refresh. Previous entries remain visible.';
        retry.hidden = false;
      }});
  }}
  root.addEventListener('click', function (ev) {{
    if (ev.target.closest('[data-dj-retry]')) {{ loadFilter(requestedFilter); return; }}
    var chip = ev.target.closest('[data-dj-filter]');
    if (chip) {{ loadFilter(chip.getAttribute('data-dj-filter')); return; }}
    var pqBtn = ev.target.closest('[data-pq]');
    if (!pqBtn) return;
    var quality = pqBtn.getAttribute('data-pq');
    var holder = pqBtn.closest('[data-pq-decision]');
    if (quality === 'skip') {{
      if (holder) CCAction.leave(holder);
      return;
    }}
    var decisionId = pqBtn.getAttribute('data-decision-id');
    CCAction.busy(pqBtn);
    fetch('/api/decisions/' + decisionId + '/process-quality', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{quality: quality}})
    }}).then(function (r) {{
      if (r.ok && holder) {{
        holder.outerHTML = '<span class="k-chip">process: ' + quality + '</span>';
      }} else {{
        CCAction.release(pqBtn);
      }}
    }}).catch(function () {{ CCAction.release(pqBtn); }});
  }});
}})();
</script>"""


__all__ = ["render_decision_journal_list", "render_decision_journal_panel"]
