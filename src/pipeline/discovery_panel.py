"""Research → Discovery panel (master build P5.4): the candidate approval
queue — the budget gate the directive locked ("Discovery: queue, never
auto-build").

What it renders: the discovery_candidates inbox ranked by score, each row
carrying its full "why surfaced" evidence (the screens' actual numbers,
which holding's watchlist/calls/news named it), the lifecycle status, and
the owner's actions:

  Queue / Dismiss / Re-open — status moves via POST
      /api/discovery/candidates/<id>/status (dismissed names never
      resurface; re-running the pipelines refreshes evidence only)
  Build — POST /actions/discovery-build, ONE name or the checked set
      (capped; each build ≈ 25 min + LLM spend, and the click is the
      approval). The job streams into the log pane via the existing
      /actions/stream SSE.
  Run discovery — POST /actions/discovery-run re-runs the P5.3 pipelines
      themselves (screens + adjacency; deterministic, LLM-free).

``?fragment=list`` returns just the table for the panel JS's refreshes.
Chat carries the same verbs (``/discovery list|queue|dismiss|build`` in
the workspace chat) through the same store + registry.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from discovery.store import CANDIDATE_STATUSES, CandidateRow, list_candidates
from identity import DEFAULT_USER_ID

_STATUS_FILTERS: tuple[str, ...] = ("live", *CANDIDATE_STATUSES)

_PANEL_STYLE = """<style>
.dq-bar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:4px 0 12px; }
/* Inputs/selects: skinned by the shared control kit (ui/controls.py). */
.dq-bar button { background:var(--accent-soft); color:var(--accent); border:1px solid var(--accent);
  border-radius:var(--radius); padding:5px 12px; font-size:var(--fs-body); cursor:pointer;
  transition:filter var(--transition); }
.dq-bar button:hover { filter:brightness(1.15); }
.dq-bar button[disabled] { opacity:.45; cursor:default; }
.dq-count { color:var(--muted); font-size:var(--fs-caption); margin-left:auto; }
.dq-table { border-collapse:collapse; width:100%; font-size:var(--fs-body);
  font-variant-numeric:tabular-nums; }
.dq-table th, .dq-table td { padding:6px 8px; border-bottom:1px solid var(--hairline);
  text-align:left; vertical-align:top; }
.dq-table th { color:var(--muted); font-size:var(--fs-caption); text-transform:uppercase;
  letter-spacing:.06em; font-weight:600; }
.dq-table tbody tr:hover td { background:rgba(255,255,255,0.025); }
.dq-tick { font-family:var(--mono, monospace); font-weight:600; }
.dq-score { font-family:var(--mono, monospace); }
.dq-status { font-size:var(--fs-micro); font-weight:600; text-transform:uppercase; letter-spacing:.05em; }
.dq-status-new { color:var(--accent); }
.dq-status-queued { color:var(--warn); }
.dq-status-building { color:var(--warn); font-weight:700; }
.dq-status-built { color:var(--ok); }
.dq-status-dismissed { color:var(--muted); }
.dq-ev { margin:0; padding-left:16px; }
.dq-ev li { margin:1px 0; color:var(--fg-soft, var(--fg)); }
.dq-ev .dq-src { font-family:var(--mono, monospace); font-size:var(--fs-micro); color:var(--muted);
  margin-right:5px; }
.dq-acts { display:flex; gap:5px; flex-wrap:wrap; }
.dq-acts button { background:var(--paper, #1a1d23); color:var(--fg-soft, var(--fg));
  border:1px solid var(--border); border-radius:var(--radius); padding:3px 9px; font-size:var(--fs-caption);
  cursor:pointer; transition:color var(--transition), border-color var(--transition); }
.dq-acts button:hover { border-color:var(--accent); color:var(--accent); }
.dq-acts button[data-act="build"]:hover { border-color:var(--warn); color:var(--warn); }
.dq-empty { color:var(--muted); padding:18px 0; }
.dq-log { background:var(--paper, #14161b); border:1px solid var(--border); border-radius:var(--radius);
  padding:8px 12px; margin-top:12px; max-height:280px; overflow-y:auto;
  font-family:var(--mono, monospace); font-size:var(--fs-caption); color:var(--fg-soft, var(--fg));
  white-space:pre-wrap; display:none; }
.dq-hint { color:var(--muted); font-size:var(--fs-caption); margin-top:10px; }
.dq-warnrow { color:var(--warn); font-size:var(--fs-caption); margin:6px 0; }
</style>"""

# Plain string (not an f-string) so braces pass through untouched.
_PANEL_JS = """
(function () {
  var root = document.getElementById('dq-root');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  function el(id) { return document.getElementById(id); }
  function refresh() {
    var qs = new URLSearchParams({
      fragment: 'list', status: el('dq-status').value, min_score: el('dq-min-score').value
    });
    fetch('/api/panel/discovery?' + qs).then(function (r) { return r.text(); })
      .then(function (h) { el('dq-list').innerHTML = h; });
  }
  function checked() {
    return Array.prototype.slice.call(
      root.querySelectorAll('input[data-pick]:checked')
    ).map(function (c) { return c.getAttribute('data-pick'); });
  }
  function logLine(s) {
    var pane = el('dq-log');
    pane.style.display = 'block';
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
  el('dq-filters').addEventListener('submit', function (ev) { ev.preventDefault(); refresh(); });
  el('dq-run-discovery').addEventListener('click', function () {
    postAction('/actions/discovery-run', {}).then(function (res) {
      if (res.ok) streamJob(res.body);
      else logLine('discovery run rejected: ' + (res.body.error || 'unknown'));
    });
  });
  el('dq-build-selected').addEventListener('click', function () { buildTickers(checked()); });
  root.addEventListener('click', function (ev) {
    var btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    var holder = btn.closest('[data-cand-id]');
    if (!holder) return;
    var id = holder.getAttribute('data-cand-id');
    var act = btn.getAttribute('data-act');
    if (act === 'build') {
      buildTickers([holder.getAttribute('data-cand-ticker')]);
      return;
    }
    var status = {queue: 'queued', dismiss: 'dismissed', reopen: 'new'}[act];
    if (!status) return;
    fetch('/api/discovery/candidates/' + id + '/status', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: status})
    }).then(function (r) { if (r.ok) refresh(); });
  });
})();
"""


def _evidence_html(cand: CandidateRow) -> str:
    items: list[str] = []
    for e in cand.evidence:
        src = escape(str(e.get("source") or ""))
        detail = escape(str(e.get("detail") or ""))
        items.append(f'<li><span class="dq-src">{src}</span>{detail}</li>')
    if not items:
        return '<span class="dq-src">no evidence recorded</span>'
    return f'<ul class="dq-ev">{"".join(items)}</ul>'


def _row_html(cand: CandidateRow) -> str:
    status = escape(cand.status)
    name = f' <span class="dq-src">{escape(cand.name)}</span>' if cand.name else ""
    acts: list[str] = []
    if cand.status in ("new", "queued"):
        if cand.status == "new":
            acts.append('<button type="button" data-act="queue">Queue</button>')
        acts.append('<button type="button" data-act="build">Build</button>')
        acts.append('<button type="button" data-act="dismiss">Dismiss</button>')
    elif cand.status in ("dismissed", "built"):
        acts.append('<button type="button" data-act="reopen">Re-open</button>')
    pick = (
        f'<input type="checkbox" data-pick="{escape(cand.ticker)}">'
        if cand.status in ("new", "queued")
        else ""
    )
    return (
        f'<tr data-cand-id="{cand.id}" data-cand-ticker="{escape(cand.ticker)}">'
        f"<td>{pick}</td>"
        f'<td class="dq-tick">{escape(cand.ticker)}{name}</td>'
        f'<td class="dq-score">{cand.score:g}</td>'
        f'<td><span class="dq-status dq-status-{status}">{status}</span></td>'
        f"<td>{_evidence_html(cand)}</td>"
        f'<td><div class="dq-acts">{"".join(acts)}</div></td>'
        "</tr>"
    )


def render_discovery_list(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    status: str = "live",
    min_score: float = 0.0,
) -> str:
    """Just the candidates table (the ``?fragment=list`` fragment)."""
    list_status = None if status == "live" else status
    if list_status is not None and list_status not in CANDIDATE_STATUSES:
        list_status = None
    try:
        rows = list_candidates(user_id=user_id, status=list_status, db_path=db_path)
    except (sqlite3.Error, FileNotFoundError, RuntimeError):
        rows = []  # pre-0081 schema / missing DB degrades to empty
    rows = [c for c in rows if c.score >= min_score]
    if not rows:
        return (
            '<div class="dq-empty">No candidates match. Run discovery (button above) to '
            "sweep the screens + adjacency miners, or relax the filter.</div>"
        )
    head = (
        "<tr><th></th><th>Ticker</th><th>Score</th><th>Status</th>"
        "<th>Why surfaced</th><th>Actions</th></tr>"
    )
    body = "".join(_row_html(c) for c in rows)
    count = f'<div class="dq-count">{len(rows)} candidate(s)</div>'
    return f'{count}<table class="dq-table"><thead>{head}</thead><tbody>{body}</tbody></table>'


def render_discovery_panel(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    status: str = "live",
    min_score: float = 0.0,
) -> str:
    """The Research → Discovery tab fragment: filter bar + queue + job log."""
    if status not in _STATUS_FILTERS:
        status = "live"
    status_opts = "".join(
        f'<option value="{escape(s)}"{" selected" if s == status else ""}>{escape(s)}</option>'
        for s in _STATUS_FILTERS
    )
    listing = render_discovery_list(db_path, user_id=user_id, status=status, min_score=min_score)
    return f"""{_PANEL_STYLE}
<h2>Discovery</h2>
<div id="dq-root">
<form class="dq-bar" id="dq-filters">
  <label>status</label>
  <select id="dq-status">{status_opts}</select>
  <label>min score</label>
  <input id="dq-min-score" type="number" min="0" max="10" step="1" value="{min_score:g}"
    style="width:60px">
  <button type="submit">Filter</button>
  <button type="button" id="dq-run-discovery"
    title="Re-run the screens + adjacency miners (deterministic, no LLM)">Run discovery</button>
  <button type="button" id="dq-build-selected"
    title="Eval-build every checked name — each is ~25 min + LLM spend">Build selected</button>
</form>
<div id="dq-list">{listing}</div>
<pre class="dq-log" id="dq-log"></pre>
<p class="dq-hint">Build = promote to the evaluation list + onboard (FMP, transcripts) +
 full eval brief with LLM sections. The click is the approval — nothing builds on its
 own, and a build is ~25 minutes + LLM spend per name. Dismissed names stay dismissed
 across discovery re-runs. Chat knows the same verbs: /discovery list &middot;
 /discovery queue T &middot; /discovery dismiss T &middot; /discovery build T.</p>
</div>
<script>{_PANEL_JS}</script>"""
