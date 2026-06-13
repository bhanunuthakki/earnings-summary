"""Research → Discovery panel (master build P5.4; rebuilt S6 PR2): the
candidate approval queue — the budget gate the directive locked ("Discovery:
queue, never auto-build") — rendered as ONE labeled instrument on the shared
control kit (design_language §6.1 + §10 "The Discovery rule").

What changed from the P5.4 original (the owner's feedback → the Discovery rule):
two stacked chrome bands (a title block over a filter block) collapse onto ONE
``panel_toolbar`` band; the native ``<select>`` status filter becomes ``.k-chip``
toggles; tall multi-line evidence rows become a ``.p-table`` with the weighted
``score`` as a ``.p-pill``, ``ticker_label`` for ticker+name, the one-line
``score_evidence_line`` inline and the full per-signal breakdown behind a peek;
and the print-all-500 list is capped to a ranked top-N. A collapsible Sources
editor exposes the ``discovery_sources`` weight registry (the POST weight route
re-ranks the queue).

What it renders, unchanged in contract:

  Queue / Dismiss / Re-open — status moves via POST
      /api/discovery/candidates/<id>/status (dismissed names never resurface;
      re-running the pipelines refreshes evidence/score only)
  Build — POST /actions/discovery-build, ONE name or the checked set (capped;
      each build ≈ 25 min + LLM spend, and the click is the approval). Streams
      into the log pane via /actions/stream.
  Run discovery — POST /actions/discovery-run re-runs the P5.3 pipelines.

``?fragment=list`` returns just the table for the panel JS's refreshes;
``?fragment=sources`` returns just the Sources editor.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path
from typing import cast

from discovery.scoring import score_evidence_line
from discovery.sources import SourceRow, list_sources
from discovery.store import CANDIDATE_STATUSES, CandidateRow, list_candidates
from identity import DEFAULT_USER_ID
from ui.controls import panel_toolbar, ticker_label

_STATUS_FILTERS: tuple[str, ...] = ("live", *CANDIDATE_STATUSES)

#: The ranked render cap (the owner's "cap the render to a ranked top-N", paired
#: with the generation-side ENTRY_THRESHOLD). The queue scrolls no further than
#: this; the count line discloses how many were elided.
RENDER_TOP_N = 60

#: Status → kit chip tone (design_language §3; never a freehand color).
_STATUS_TONE: dict[str, str] = {
    "new": "k-chip-accent",
    "queued": "k-chip-warn",
    "building": "k-chip-warn",
    "built": "k-chip-ok",
    "dismissed": "",
}

# Layout only — the kit owns every color/font/shape. (No raw hex, no off-scale
# font-size, no font-family: this surface is conformant so S7 skips it.)
_PANEL_STYLE = """<style>
.dq-count { color: var(--muted); font-size: var(--fs-caption); margin-left: auto; }
.dq-why { color: var(--fg-soft); }
.dq-why-line { display: flex; align-items: baseline; gap: var(--sp-2); }
.dq-peek { background: none; border: none; color: var(--muted); cursor: pointer;
  font-size: var(--fs-caption); padding: 0; }
.dq-peek:hover { color: var(--accent); }
.dq-detail[hidden] { display: none; }
.dq-detail td { padding-top: 0; }
.dq-evtable { width: 100%; border-collapse: collapse; font-size: var(--fs-caption);
  font-variant-numeric: tabular-nums; color: var(--muted); }
.dq-evtable td { padding: 2px var(--sp-2); border: none; }
.dq-evtable .dq-src { font-family: var(--mono); color: var(--fg-soft); }
.dq-acts { display: flex; gap: var(--sp-1); flex-wrap: wrap; }
.dq-srcwt { width: 4.5rem; }
.dq-sources { margin-top: var(--sp-3); }
.dq-saved { color: var(--ok); font-size: var(--fs-caption); margin-left: var(--sp-2);
  opacity: 0; transition: opacity var(--transition); }
.dq-saved.is-on { opacity: 1; }
.dq-log { margin-top: var(--sp-3); max-height: 280px; overflow-y: auto;
  font-family: var(--mono); font-size: var(--fs-caption); color: var(--fg-soft);
  white-space: pre-wrap; display: none; }
.dq-log.is-on { display: block; }
.dq-hint { color: var(--muted); font-size: var(--fs-caption); margin-top: var(--sp-3); }
.dq-empty { color: var(--muted); padding: var(--sp-4) 0; }
</style>"""

# Plain string (not an f-string) so braces pass through untouched.
_PANEL_JS = """
(function () {
  var root = document.getElementById('dq-root');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  function el(id) { return document.getElementById(id); }
  function currentStatus() {
    var on = root.querySelector('.dq-statusfilter.is-on');
    return on ? on.getAttribute('data-status') : 'live';
  }
  function refresh() {
    var qs = new URLSearchParams({
      fragment: 'list', status: currentStatus(), min_score: el('dq-min-score').value
    });
    fetch('/api/panel/discovery?' + qs).then(function (r) { return r.text(); })
      .then(function (h) { el('dq-list').innerHTML = h; });
  }
  function refreshSources() {
    fetch('/api/panel/discovery?fragment=sources').then(function (r) { return r.text(); })
      .then(function (h) { el('dq-sources-body').innerHTML = h; });
  }
  function checked() {
    return Array.prototype.slice.call(
      root.querySelectorAll('input[data-pick]:checked')
    ).map(function (c) { return c.getAttribute('data-pick'); });
  }
  function logLine(s) {
    var pane = el('dq-log');
    pane.classList.add('is-on');
    pane.textContent += s + '\\n';
    pane.scrollTop = pane.scrollHeight;
  }
  function streamJob(job) {
    logLine('=== ' + job.kind + ' ' + job.job_id + ' started ===');
    var es = new EventSource(job.stream_url);
    es.onmessage = function (ev) {
      var f = {};
      try { f = JSON.parse(ev.data); } catch (e) { return; }
      if (f.event === 'log') logLine(f.line);
      if (f.event === 'done') {
        logLine('=== done (exit ' + f.exit_code + ') ===');
        es.close();
        refresh();
      }
    };
    es.onerror = function () { es.close(); };
  }
  function postAction(url, body) {
    return fetch(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, body: j}; });
    });
  }
  function buildTickers(tickers) {
    if (!tickers.length) return;
    var est = tickers.length + ' build(s) x ~25 min + LLM spend each';
    if (!window.confirm('Start eval build for ' + tickers.join(', ') + '?\\n' + est)) return;
    postAction('/actions/discovery-build', {tickers: tickers}).then(function (res) {
      if (res.ok) { streamJob(res.body); refresh(); }
      else { logLine('build rejected: ' + (res.body.error || 'unknown')); }
    });
  }
  // Status chips behave like a radio group.
  root.addEventListener('click', function (ev) {
    var chip = ev.target.closest('.dq-statusfilter');
    if (chip) {
      root.querySelectorAll('.dq-statusfilter').forEach(function (c) { c.classList.remove('is-on'); });
      chip.classList.add('is-on');
      refresh();
      return;
    }
    var peek = ev.target.closest('.dq-peek');
    if (peek) {
      var detail = el('dq-detail-' + peek.getAttribute('data-cand'));
      if (detail) detail.hidden = !detail.hidden;
      return;
    }
    var srcBtn = ev.target.closest('button[data-src-save]');
    if (srcBtn) {
      var key = srcBtn.getAttribute('data-src-save');
      var input = root.querySelector('input[data-src-weight="' + key + '"]');
      postAction('/api/discovery/sources/' + encodeURIComponent(key) + '/weight',
                 {weight: parseFloat(input.value)}).then(function (res) {
        if (res.ok) {
          var flag = root.querySelector('.dq-saved[data-src-saved="' + key + '"]');
          if (flag) { flag.classList.add('is-on'); setTimeout(function () { flag.classList.remove('is-on'); }, 1500); }
          refresh();  // a weight edit re-ranks the queue
        }
      });
      return;
    }
    var btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    var holder = btn.closest('[data-cand-id]');
    if (!holder) return;
    var id = holder.getAttribute('data-cand-id');
    var act = btn.getAttribute('data-act');
    if (act === 'build') { buildTickers([holder.getAttribute('data-cand-ticker')]); return; }
    var status = {queue: 'queued', dismiss: 'dismissed', reopen: 'new'}[act];
    if (!status) return;
    fetch('/api/discovery/candidates/' + id + '/status', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: status})
    }).then(function (r) { if (r.ok) refresh(); });
  });
  el('dq-min-score').addEventListener('change', refresh);
  el('dq-run-discovery').addEventListener('click', function () {
    postAction('/actions/discovery-run', {}).then(function (res) {
      if (res.ok) streamJob(res.body);
      else logLine('discovery run rejected: ' + (res.body.error || 'unknown'));
    });
  });
  el('dq-build-selected').addEventListener('click', function () { buildTickers(checked()); });
  var srcToggle = el('dq-sources-toggle');
  srcToggle.addEventListener('click', function () {
    var box = el('dq-sources');
    var open = box.hasAttribute('hidden');
    if (open) { box.removeAttribute('hidden'); refreshSources(); } else { box.setAttribute('hidden', ''); }
    srcToggle.classList.toggle('is-on', open);
  });
})();
"""


def _score_pill(score: float) -> str:
    tone = "k-pill-accent" if score >= 3.0 else "k-pill-warn" if score >= 2.0 else ""
    return f'<span class="k-pill {tone}">{score:g}</span>'


def _evidence_detail_html(cand: CandidateRow) -> str:
    """The per-signal contribution table shown behind the peek — from score_json
    when present, else the legacy verbatim evidence list."""
    why = cand.score_json
    signals = why.get("signals") if isinstance(why, dict) else None
    rows: list[str] = []
    if isinstance(signals, list):
        for raw in cast("list[object]", signals):
            if not isinstance(raw, dict):
                continue
            s = cast("dict[str, object]", raw)
            src = escape(f"{s.get('class', '')}:{s.get('source_key', '')}")
            contrib = s.get("contribution")
            contrib_s = f"{float(contrib):.2f}" if isinstance(contrib, (int, float)) else "-"
            detail = escape(str(s.get("detail") or ""))
            rows.append(
                f'<tr><td class="dq-src">{src}</td><td>{contrib_s}</td><td>{detail}</td></tr>'
            )
    if not rows:  # legacy / pre-scoring rows: fall back to verbatim evidence
        for e in cand.evidence:
            src = escape(str(e.get("source") or ""))
            detail = escape(str(e.get("detail") or ""))
            rows.append(f'<tr><td class="dq-src">{src}</td><td></td><td>{detail}</td></tr>')
    if not rows:
        return '<span class="dq-src">no evidence recorded</span>'
    return f'<table class="dq-evtable"><tbody>{"".join(rows)}</tbody></table>'


def _why_line(cand: CandidateRow) -> str:
    if isinstance(cand.score_json, dict):
        return escape(score_evidence_line(cand.score_json))
    # legacy row: summarize the verbatim evidence sources
    srcs = sorted({str(e.get("source") or "").split(":", 1)[0] for e in cand.evidence if e})
    return escape(" · ".join(s for s in srcs if s)) or "—"


def _row_html(cand: CandidateRow) -> str:
    status = cand.status
    chip = _STATUS_TONE.get(status, "")
    acts: list[str] = []
    if status in ("new", "queued"):
        if status == "new":
            acts.append(
                '<button type="button" class="k-btn k-btn-sm k-btn-quiet" data-act="queue">Queue</button>'
            )
        acts.append(
            '<button type="button" class="k-btn k-btn-sm k-btn-primary" data-act="build">Build</button>'
        )
        acts.append(
            '<button type="button" class="k-btn k-btn-sm k-btn-danger" data-act="dismiss">Dismiss</button>'
        )
    elif status in ("dismissed", "built"):
        acts.append(
            '<button type="button" class="k-btn k-btn-sm k-btn-quiet" data-act="reopen">Re-open</button>'
        )
    pick = (
        f'<input type="checkbox" data-pick="{escape(cand.ticker)}">'
        if status in ("new", "queued")
        else ""
    )
    href = f"/api/panel/holding?ticker={escape(cand.ticker, quote=True)}"
    return (
        f'<tr data-cand-id="{cand.id}" data-cand-ticker="{escape(cand.ticker)}">'
        f"<td>{pick}</td>"
        f"<td>{ticker_label(cand.ticker, cand.name, href=href)}</td>"
        f'<td class="num">{_score_pill(cand.score)}</td>'
        f'<td><span class="k-chip {chip}">{escape(status)}</span></td>'
        '<td class="dq-why"><div class="dq-why-line">'
        f"<span>{_why_line(cand)}</span>"
        f'<button type="button" class="dq-peek" data-cand="{cand.id}">details</button>'
        "</div></td>"
        f'<td><div class="dq-acts">{"".join(acts)}</div></td>'
        "</tr>"
        f'<tr class="dq-detail" id="dq-detail-{cand.id}" hidden>'
        f'<td></td><td colspan="5">{_evidence_detail_html(cand)}</td></tr>'
    )


def render_discovery_list(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    status: str = "live",
    min_score: float = 0.0,
) -> str:
    """Just the candidates table (the ``?fragment=list`` fragment), capped to a
    ranked top-N."""
    list_status = None if status == "live" else status
    if list_status is not None and list_status not in CANDIDATE_STATUSES:
        list_status = None
    try:
        rows = list_candidates(user_id=user_id, status=list_status, db_path=db_path)
    except (sqlite3.Error, FileNotFoundError, RuntimeError):
        rows = []  # pre-0081 schema / missing DB degrades to empty
    rows = [c for c in rows if c.score >= min_score]
    total = len(rows)
    if not rows:
        return (
            '<div class="dq-empty">No candidates match. Run discovery (button above) to '
            "sweep the screens + adjacency miners, or relax the filter.</div>"
        )
    shown = rows[:RENDER_TOP_N]
    head = (
        '<tr><th></th><th>Ticker</th><th class="num">Score</th><th>Status</th>'
        "<th>Why surfaced</th><th>Actions</th></tr>"
    )
    body = "".join(_row_html(c) for c in shown)
    elided = f" (top {RENDER_TOP_N} of {total})" if total > RENDER_TOP_N else ""
    count = f'<div class="dq-count">{len(shown)} candidate(s){elided}</div>'
    return f'{count}<table class="p-table"><thead>{head}</thead><tbody>{body}</tbody></table>'


def _source_row_html(src: SourceRow) -> str:
    cik = escape(src.cik) if src.cik else '<span class="dq-src">—</span>'
    return (
        "<tr>"
        f"<td>{escape(src.display_name)}</td>"
        f'<td><span class="k-chip">{escape(src.signal_class)}</span></td>'
        f'<td class="dq-src">{escape(src.tier)}</td>'
        f'<td class="num"><input type="number" class="dq-srcwt" min="0" max="3" step="0.05" '
        f'value="{src.base_weight:g}" data-src-weight="{escape(src.source_key, quote=True)}"></td>'
        f'<td><button type="button" class="k-btn k-btn-sm k-btn-quiet" '
        f'data-src-save="{escape(src.source_key, quote=True)}">Save</button>'
        f'<span class="dq-saved" data-src-saved="{escape(src.source_key, quote=True)}">saved</span></td>'
        f'<td class="dq-src">{cik}</td>'
        "</tr>"
    )


def render_sources_editor(db_path: Path) -> str:
    """The ``?fragment=sources`` fragment: the discovery_sources weight registry,
    one editable ``base_weight`` per source — editing a weight re-ranks the
    queue (the Discovery rule's live lever)."""
    try:
        sources = list_sources(db_path=db_path)
    except (sqlite3.Error, FileNotFoundError, RuntimeError):
        sources = []
    if not sources:
        return '<div class="dq-empty">No source registry (run alembic upgrade to seed it).</div>'
    head = (
        '<tr><th>Source</th><th>Class</th><th>Tier</th><th class="num">Weight</th>'
        "<th></th><th>CIK</th></tr>"
    )
    body = "".join(_source_row_html(s) for s in sources)
    return f'<table class="p-table"><thead>{head}</thead><tbody>{body}</tbody></table>'


def render_discovery_panel(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    status: str = "live",
    min_score: float = 0.0,
) -> str:
    """The Research → Discovery tab fragment: ONE toolbar band (status chip
    filters + actions), the ranked queue, a collapsible Sources weight editor,
    and the job log. The nav owns the "Discovery" title, so the panel suppresses
    its own (design_language §6.1)."""
    if status not in _STATUS_FILTERS:
        status = "live"
    chips = "".join(
        f'<button type="button" class="k-chip k-chip-btn dq-statusfilter'
        f'{" is-on" if s == status else ""}" data-status="{escape(s)}">{escape(s)}</button>'
        for s in _STATUS_FILTERS
    )
    filters = (
        f'<div class="dq-statuschips">{chips}</div>'
        '<label class="k-label">min score</label>'
        f'<input id="dq-min-score" type="number" min="0" max="10" step="0.5" '
        f'value="{min_score:g}" style="width:4rem">'
    )
    actions = (
        '<button type="button" id="dq-sources-toggle" class="k-btn k-btn-sm k-btn-quiet"'
        ' title="Tune the source weight registry">Sources</button>'
        '<button type="button" id="dq-run-discovery" class="k-btn k-btn-sm k-btn-quiet"'
        ' title="Re-run the screens + adjacency miners (deterministic, no LLM)">Run discovery</button>'
        '<button type="button" id="dq-build-selected" class="k-btn k-btn-sm k-btn-primary"'
        ' title="Eval-build every checked name — each is ~25 min + LLM spend">Build selected</button>'
    )
    toolbar = panel_toolbar(suppress_title=True, filters=filters, actions=actions)
    listing = render_discovery_list(db_path, user_id=user_id, status=status, min_score=min_score)
    return f"""{_PANEL_STYLE}
<div id="dq-root">
{toolbar}
<div id="dq-list">{listing}</div>
<section class="dq-sources k-well" id="dq-sources" hidden>
  <div class="k-label">Source weight registry — editing a weight re-ranks the queue</div>
  <div id="dq-sources-body"></div>
</section>
<pre class="dq-log" id="dq-log"></pre>
<p class="dq-hint">Build = promote to the evaluation list + onboard (FMP, transcripts) +
 full eval brief with LLM sections. The click is the approval — nothing builds on its
 own, and a build is ~25 minutes + LLM spend per name. Dismissed names stay dismissed
 across discovery re-runs. Chat knows the same verbs: /discovery list &middot;
 /discovery queue T &middot; /discovery dismiss T &middot; /discovery build T.</p>
</div>
<script>{_PANEL_JS}</script>"""
