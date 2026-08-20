"""Ledger -> Decisions fragment (P2.1, PRD Sec 9.3 "owner-first decision journal").

A NEW reader over v_decision_journal (migration 0179/0188/0195 -- the SQL
view itself stays untouched; owner-first behavior belongs in this renderer's
DEFAULT, not in the view). This is deliberately a SEPARATE module from
pipeline.journal_panel (the analyst_notes lifecycle UI -- a different
table entirely) and from
pipeline.allocation_decisions_panel._decision_journal_section (an
existing compact "recent decisions" teaser on the Allocation page, unfiltered
and un-chipped) -- this fragment is the Ledger console's own full, filtered
decision journal.

Default filter: Owner Decisions (decided_by = 'owner') -- historical
advisor-extracted rows are preserved but no longer dominate the default
timeline. Five chips select the PRD's five lenses:

  * owner       -- decided_by = 'owner' (default);
  * adopted     -- advisor-extracted rows the owner has since ADOPTED (a
                  reconciled user_action_kind or an explicit
                  owner_attested_change) -- "gradeable advisor views";
  * all         -- every row, unfiltered (the pre-P2.1 timeline);
  * lessons     -- rows with a scored process_quality (sound/flawed);
  * coaching    -- rows with a coach ping or a decision-nudge trail attached.

Process-quality prompt (Sec 9.3): a MATURED row (outcome_at set) with
process_quality still NULL gets an inline three-way prompt -- Sound /
Flawed / Need more evidence. Sound/Flawed POST the existing
/api/decisions/<id>/process-quality route (decision_extractor.
record_process_quality -- unchanged); "Need more evidence" writes NOTHING
(the PRD: "leave null and schedule no repeated pestering") and simply
dismisses the prompt client-side for this render -- there is deliberately no
nudge/suppression machinery, so the prompt is free to render again on a
future visit rather than needing a persisted "asked" flag. lucky is
NEVER offered here -- it stays a system-only outcome-aware calibration label
(PRD Sec 9.3), never a user-facing process choice.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from pipeline.research_panel_styles import RESEARCH_PANEL_STYLE
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import panel_toolbar, ticker_label

_PANEL_STYLE = RESEARCH_PANEL_STYLE

_FILTERS: tuple[str, ...] = ("owner", "adopted", "all", "lessons", "coaching")
_FILTER_LABEL: dict[str, str] = {
    "owner": "Owner Decisions",
    "adopted": "Adopted advisor views",
    "all": "All advice artifacts",
    "lessons": "Process lessons",
    "coaching": "Coaching history",
}

_WHERE_BY_FILTER: dict[str, str] = {
    "owner": "decided_by = 'owner'",
    "adopted": (
        "decided_by != 'owner' AND (user_action_kind IS NOT NULL OR owner_attested_change = 1)"
    ),
    "all": "1 = 1",
    "lessons": "process_quality IS NOT NULL",
    "coaching": "(coach_ping_class IS NOT NULL OR decision_nudge_id IS NOT NULL)",
}


def _open(db_path: Path) -> sqlite3.Connection | None:
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM v_decision_journal LIMIT 1")
        return conn
    except sqlite3.Error:
        return None


def _advice_chip(r: sqlite3.Row) -> str:
    bits: list[str] = []
    if r["advice_purpose"]:
        preceded = " (preceded)" if r["advice_preceded"] else ""
        bits.append(
            f'<span class="k-chip k-chip-mono">{escape(str(r["advice_purpose"]))}{preceded}</span>'
        )
    if r["source_draft_channel"]:
        bits.append(f'<span class="k-chip">via {escape(str(r["source_draft_channel"]))}</span>')
    if r["coach_ping_class"]:
        bits.append(f'<span class="k-chip">ping: {escape(str(r["coach_ping_class"]))}</span>')
    if r["decision_nudge_id"] is not None:
        bits.append('<span class="k-chip">nudged</span>')
    return "".join(bits) if bits else '<span class="dj-advice">&mdash; no advice on record</span>'


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
        f"{_advice_chip(r)}"
        f"{falsifier}"
        "</div>"
    )


def render_decision_journal_list(db_path: Path, *, filter_: str = "owner", limit: int = 100) -> str:
    """Just the filtered row list (the fragment /api/panel/ledger_decisions?fragment=list
    swaps in place). Best-effort empty state on any read failure / pre-0195 schema."""
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
<div id="dj-list">{body}</div>
</div>
<script>
(function () {{
  var root = document.getElementById('dj-root');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  root.addEventListener('click', function (ev) {{
    var chip = ev.target.closest('[data-dj-filter]');
    if (chip) {{
      var qs = new URLSearchParams({{fragment: 'list', filter: chip.getAttribute('data-dj-filter')}});
      fetch('/api/panel/ledger_decisions?' + qs).then(function (r) {{ return r.text(); }})
        .then(function (html) {{
          document.getElementById('dj-list').innerHTML = html;
          root.querySelectorAll('[data-dj-filter]').forEach(function (b) {{
            b.classList.toggle('k-chip-accent', b === chip);
          }});
        }});
      return;
    }}
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
