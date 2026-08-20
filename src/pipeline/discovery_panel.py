"""Research → Discovery panel (master build P5.4; rebuilt S6 PR2): the
candidate approval queue — the budget gate the directive locked ("Discovery:
queue, never auto-build") — rendered as ONE labeled instrument on the shared
control kit (design_language §6.1 + §10 "The Discovery rule").

What changed from the P5.4 original (the owner's feedback → the Discovery rule):
two stacked chrome bands (a title block over a filter block) collapse onto ONE
``panel_toolbar`` band; the native ``<select>`` status filter becomes ``.k-chip``
toggles; tall multi-line evidence rows become a ``.p-table`` with the weighted
``score`` as a ``.k-pill``, ``ticker_label`` for ticker+name, the one-line
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
from pipeline.operations_styles import DISCOVERY_STYLE as _PANEL_STYLE
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
# Plain string (not an f-string) so braces pass through untouched.
_PANEL_JS = """
(function () {
  var root = document.getElementById('dq-root');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function currentStatus() {
    var on = root.querySelector('.dq-statusfilter.is-on');
    return on ? on.getAttribute('data-status') : 'live';
  }
  // Lift the count the list fragment carries onto the toolbar band so it never
  // costs its own row. Runs on first paint and after every refresh.
  function relocateCount() {
    var target = el('dq-count');
    if (!target) return;
    var src = root.querySelector('#dq-list .dq-count');
    target.textContent = src ? src.textContent : '';
    if (src) src.remove();
  }
  function refresh() {
    var qs = new URLSearchParams({
      fragment: 'list', status: currentStatus(), min_score: el('dq-min-score').value
    });
    fetch('/api/panel/discovery?' + qs).then(function (r) { return r.text(); })
      .then(function (h) { el('dq-list').innerHTML = h; relocateCount(); });
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
  // In-card Dismiss editor (replaces the two sequential window.prompt calls):
  // swaps the row's own evidence-detail cell for a reason + revisit-condition
  // form (both optional, sent together — blank reason = queue-state-only
  // dismiss per the server contract), unhiding the row if it was collapsed
  // and restoring the prior content on cancel or failure.
  function beginDismiss(holder, id, tk) {
    if (holder.getAttribute('data-editing') === '1') return;
    var detail = el('dq-detail-' + id);
    // [data-dismiss-target] is structure-agnostic: the compact table's last
    // <td> and a card's detail <div> both carry it, so this works for either
    // rendering without assuming a table row (a bare <tr> outside a <table>
    // is invalid HTML and browsers mangle it).
    var cell = detail && detail.querySelector('[data-dismiss-target]');
    if (!cell) { return; }
    holder.setAttribute('data-editing', '1');
    var wasHidden = detail.hidden;
    var original = cell.innerHTML;
    detail.hidden = false;
    cell.innerHTML =
      '<div class="dq-dismiss-form">'
      + '<label class="dq-dismiss-label">Passing on ' + esc(tk) + '? Optionally note WHY — '
      + 'records a gradeable avoid so a passed name that later triples leaves a trace '
      + '(blank = just dismiss).</label>'
      + '<textarea class="dq-dismiss-ta" rows="2" data-dismiss-reason></textarea>'
      + '<label class="dq-dismiss-label">What would make you revisit ' + esc(tk) + '? '
      + '(optional — e.g. "a credible competitor stumbles", "valuation halves below $90")</label>'
      + '<input type="text" class="dq-dismiss-revisit" data-dismiss-revisit>'
      + '<div class="dq-dismiss-row">'
      + '<button type="button" class="k-btn k-btn-danger k-btn-sm" data-dismiss-save>Dismiss</button>'
      + '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-dismiss-cancel>Cancel</button>'
      + '</div></div>';
    function restore() {
      cell.innerHTML = original;
      detail.hidden = wasHidden;
      holder.removeAttribute('data-editing');
    }
    var reasonTa = cell.querySelector('[data-dismiss-reason]');
    var revisitInput = cell.querySelector('[data-dismiss-revisit]');
    if (reasonTa) reasonTa.focus();
    cell.querySelector('[data-dismiss-cancel]').addEventListener('click', restore);
    cell.querySelector('[data-dismiss-save]').addEventListener('click', function () {
      CCAction.busy(this, 'Dismissing\\u2026');
      var body = {status: 'dismissed'};
      var reason = (reasonTa && reasonTa.value || '').trim();
      var revisit = (revisitInput && revisitInput.value || '').trim();
      if (reason) body.reason = reason;
      if (revisit) body.revisit_if = revisit;
      fetch('/api/discovery/candidates/' + id + '/status', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      }).then(function (r) { if (r.ok) refresh(); else restore(); })
        .catch(restore);
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
    // Click-to-expand: a click anywhere on a candidate row/card — the score
    // pill, the status chip, the "why" text — reveals its evidence, EXCEPT on
    // a link / button / input / label (the ticker link, action buttons and
    // checkbox keep their own behavior). The explicit "evidence" peek stays
    // for keyboard users. [data-cand-id] covers both the compact table row
    // and the top-ten card.
    var candHolder = ev.target.closest('[data-cand-id]');
    if (candHolder && !ev.target.closest('a, button, input, label')) {
      var rowDetail = el('dq-detail-' + candHolder.getAttribute('data-cand-id'));
      if (rowDetail) rowDetail.hidden = !rowDetail.hidden;
      return;
    }
    var btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    var holder = btn.closest('[data-cand-id]');
    if (!holder) return;
    var id = holder.getAttribute('data-cand-id');
    var act = btn.getAttribute('data-act');
    if (act === 'build') { buildTickers([holder.getAttribute('data-cand-ticker')]); return; }
    if (act === 'dismiss') {
      beginDismiss(holder, id, holder.getAttribute('data-cand-ticker') || 'this name');
      return;
    }
    if (act === 'watch') {
      CCAction.busy(btn);
      postAction('/api/discovery/candidates/' + id + '/watch', {}).then(function (res) {
        CCAction.release(btn);
        if (res.ok) { logLine('Watching ' + (holder.getAttribute('data-cand-ticker') || '')); }
        else { logLine('watch failed: ' + (res.body.error || 'unknown')); }
      });
      return;
    }
    var status = {queue: 'queued', reopen: 'new'}[act];
    if (!status) return;
    CCAction.busy(btn);
    fetch('/api/discovery/candidates/' + id + '/status', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: status})
    }).then(function (r) { if (r.ok) refresh(); else CCAction.release(btn); })
      .catch(function () { CCAction.release(btn); });
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
  relocateCount();  // lift the server-rendered count onto the toolbar on first paint
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


# ----------------------------------------------------------------------------
# need_rank (PRD §8.2, P1-B) — the top-ten cards' portfolio-need read
# ----------------------------------------------------------------------------

#: Effort chip tone — light (well-cached, quick to evaluate) reads OK, heavy
#: (thin/no cache) reads as a warning about the owner's next click.
_EFFORT_TONE: dict[str, str] = {
    "light": "k-chip-ok",
    "medium": "k-chip-warn",
    "heavy": "k-chip-bad",
}


def _need_rank(cand: CandidateRow) -> dict[str, object] | None:
    """The ``score_json['need_rank']`` blob, or ``None`` for a pre-P1-B row
    (never scored) — every card function below degrades gracefully on None."""
    if not isinstance(cand.score_json, dict):
        return None
    raw = cand.score_json.get("need_rank")
    return cast("dict[str, object]", raw) if isinstance(raw, dict) else None


def _rank_composite(cand: CandidateRow) -> float | None:
    rank = _need_rank(cand)
    if rank is None:
        return None
    v = rank.get("composite")
    return float(v) if isinstance(v, (int, float)) else None


def _rank_sort_key(cand: CandidateRow) -> tuple[float, float, str]:
    """Descending sort key for the primary (``live``) view: the need_rank
    composite when present, falling back to the legacy weighted score — an
    old row (scored before this rank existed) still sorts sanely instead of
    collapsing to the bottom of the queue."""
    composite = _rank_composite(cand)
    primary = composite if composite is not None else cand.score
    return (-primary, -cand.score, cand.ticker)


def _hypothesis_html(cand: CandidateRow, rank: dict[str, object] | None) -> str:
    """ONE deterministic sentence: the strongest adjacency reason + the GARP
    read + the strongest raw signal (``score_evidence_line``) — no LLM."""
    parts: list[str] = []
    if rank is not None:
        reasons = rank.get("adjacency_reasons")
        if isinstance(reasons, list) and reasons:
            parts.append(str(cast("list[object]", reasons)[0]))
        garp_reason = rank.get("garp_reason")
        if (
            isinstance(garp_reason, str)
            and garp_reason
            and "no cached fundamentals" not in garp_reason
        ):
            parts.append(garp_reason)
    if isinstance(cand.score_json, dict):
        parts.append(score_evidence_line(cand.score_json))
    if not parts:
        return "No corroborating evidence yet — a raw screen/adjacency surface only."
    sentence = "; ".join(parts)
    return escape(sentence[:1].upper() + sentence[1:]) + "."


def _role_overlap_html(rank: dict[str, object] | None) -> str:
    """Likely portfolio role: adjacency reasons + the diversifier note, with a
    ``preliminary`` chip whenever the diversifier leg ran (it is ALWAYS coarse
    — the PRD's "never full evaluation-grade precision")."""
    if rank is None:
        return '<span class="dq-src">not yet ranked — run discovery</span>'
    bits: list[str] = []
    reasons = rank.get("adjacency_reasons")
    if isinstance(reasons, list) and reasons:
        bits.append(escape("; ".join(str(r) for r in cast("list[object]", reasons))))
    note = rank.get("diversifier_note")
    if isinstance(note, str) and note:
        prelim = (
            ' <span class="k-chip k-chip-warn">preliminary</span>'
            if rank.get("preliminary")
            else ""
        )
        bits.append(escape(note) + prelim)
    if not bits:
        return (
            '<span class="dq-src">no portfolio-role read yet (not in the coarse-fit top 25)</span>'
        )
    return " · ".join(bits)


def _first_rejection_html(rank: dict[str, object] | None) -> str:
    if rank is None:
        return '<span class="dq-src">not yet ranked</span>'
    reason = rank.get("first_rejection_reason")
    if isinstance(reason, str) and reason:
        return f'<span class="k-chip k-chip-bad">{escape(reason)}</span>'
    return '<span class="k-chip k-chip-ok">no obvious red flag</span>'


def _effort_chip_html(rank: dict[str, object] | None) -> str:
    if rank is None:
        return ""
    effort = rank.get("effort")
    if not isinstance(effort, str):
        return ""
    tone = _EFFORT_TONE.get(effort, "")
    return f'<span class="k-chip {tone}">{escape(effort)} effort</span>'


def _next_workflow_hint(rank: dict[str, object] | None) -> str:
    if rank is None:
        return "Run discovery to compute a portfolio-need read for this name."
    reason = rank.get("first_rejection_reason")
    if isinstance(reason, str) and reason:
        return "Confirm or refute the flagged risk before building."
    reasons = rank.get("adjacency_reasons")
    if isinstance(reasons, list) and reasons:
        return "Corroborates active diligence — Build to formalize, or Watch to track passively."
    return "Build for a full evaluation brief, or Watch to keep an eye on it."


def _candidate_card_html(cand: CandidateRow) -> str:
    """One top-ten card (PRD §8.2's Candidate card): why-surfaced-now,
    hypothesis, likely role, first-rejection risk, effort, next workflow, and
    the five actions (Build · Compare · Dismiss · Watch · Open evidence)."""
    rank = _need_rank(cand)
    status = cand.status
    chip = _STATUS_TONE.get(status, "")
    composite = _rank_composite(cand)
    rank_pill = (
        f'<span class="k-pill k-pill-accent">need {composite:.1f}</span>'
        if composite is not None
        else _score_pill(cand.score)
    )
    tq = escape(cand.ticker, quote=True)
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
            '<button type="button" class="k-btn k-btn-sm k-btn-quiet" data-act="watch">Watch</button>'
        )
        acts.append(
            f'<a class="k-chip k-chip-btn" data-peek-url="/api/peek/discovery-compare?tickers={tq}" '
            f'data-peek-title="Compare · {escape(cand.ticker)}" href="/ticker/{tq}">Compare</a>'
        )
        acts.append(
            '<button type="button" class="k-btn k-btn-sm k-btn-danger" data-act="dismiss">Dismiss</button>'
        )
    elif status in ("dismissed", "built"):
        acts.append(
            '<button type="button" class="k-btn k-btn-sm k-btn-quiet" data-act="reopen">Re-open</button>'
        )
        acts.append(
            f'<a class="k-chip k-chip-btn" data-peek-url="/api/peek/discovery-compare?tickers={tq}" '
            f'data-peek-title="Compare · {escape(cand.ticker)}" href="/ticker/{tq}">Compare</a>'
        )
    pick = (
        f'<input type="checkbox" data-pick="{escape(cand.ticker)}">'
        if status in ("new", "queued")
        else ""
    )
    href = f"/api/panel/holding?ticker={tq}"
    head = (
        '<div class="dq-card-head">'
        f"{pick}{ticker_label(cand.ticker, cand.name, href=href)}"
        f'<span class="k-chip {chip}">{escape(status)}</span>'
        f"{rank_pill}{_effort_chip_html(rank)}"
        f'<button type="button" class="dq-peek" data-cand="{cand.id}">Open evidence</button>'
        "</div>"
    )
    rows = (
        f'<div class="dq-card-row"><span class="dq-card-label">Why now</span>'
        f"<span>{_why_line(cand)}</span></div>"
        f'<div class="dq-card-row"><span class="dq-card-label">Hypothesis</span>'
        f"<span>{_hypothesis_html(cand, rank)}</span></div>"
        f'<div class="dq-card-row"><span class="dq-card-label">Portfolio role</span>'
        f"<span>{_role_overlap_html(rank)}</span></div>"
        f'<div class="dq-card-row"><span class="dq-card-label">First risk</span>'
        f"<span>{_first_rejection_html(rank)}</span></div>"
        f'<div class="dq-card-row"><span class="dq-card-label">Next</span>'
        f"<span>{escape(_next_workflow_hint(rank))}</span></div>"
    )
    return (
        f'<div class="dq-card k-well" data-cand-id="{cand.id}" data-cand-ticker="{escape(cand.ticker)}">'
        f'{head}{rows}<div class="dq-acts">{"".join(acts)}</div>'
        "</div>"
        f'<div class="dq-detail" id="dq-detail-{cand.id}" hidden>'
        f"<div data-dismiss-target>{_evidence_detail_html(cand)}</div></div>"
    )


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
            '<button type="button" class="k-btn k-btn-sm k-btn-quiet" data-act="watch">Watch</button>'
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
        f'<td></td><td colspan="5" data-dismiss-target>{_evidence_detail_html(cand)}</td></tr>'
    )


#: The primary (``live``) view's card count (PRD §8.2: "at most ten candidates").
TOP_CARD_N = 10

_TABLE_HEAD = (
    '<tr><th></th><th>Ticker</th><th class="num">Score</th><th>Status</th>'
    "<th>Why surfaced</th><th>Actions</th></tr>"
)


def _table_html(rows: list[CandidateRow]) -> str:
    body = "".join(_row_html(c) for c in rows)
    return f'<table class="p-table"><thead>{_TABLE_HEAD}</thead><tbody>{body}</tbody></table>'


def render_discovery_list(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    status: str = "live",
    min_score: float = 0.0,
) -> str:
    """The candidates fragment (the ``?fragment=list`` fragment), capped to a
    ranked top-N. The primary (``live``) view shows the top ten by need_rank
    composite as cards (PRD §8.2, P1-B) — falling back to the legacy weighted
    score for a candidate never re-scored — with the rest collapsed under
    "More candidates"; any other explicit status bucket (a lifecycle-bucket
    browse, not the ranked queue) keeps the original compact table."""
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

    if status != "live":
        shown = rows[:RENDER_TOP_N]
        elided = f" (top {RENDER_TOP_N} of {total})" if total > RENDER_TOP_N else ""
        count = f'<div class="dq-count">{len(shown)} candidate(s){elided}</div>'
        return f"{count}{_table_html(shown)}"

    ranked = sorted(rows, key=_rank_sort_key)
    shown = ranked[:RENDER_TOP_N]
    top, rest = shown[:TOP_CARD_N], shown[TOP_CARD_N:]
    cards = f'<div class="dq-cards">{"".join(_candidate_card_html(c) for c in top)}</div>'
    more = (
        f'<details class="dq-more"><summary>More candidates ({len(rest)})</summary>'
        f"{_table_html(rest)}</details>"
        if rest
        else ""
    )
    elided = f" (top {RENDER_TOP_N} of {total})" if total > RENDER_TOP_N else ""
    count = f'<div class="dq-count">{len(shown)} candidate(s){elided}</div>'
    return f"{count}{cards}{more}"


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
        '<span id="dq-count" class="dq-count"></span>'
        f'<div class="dq-statuschips">{chips}</div>'
        '<label class="k-label">min score</label>'
        f'<input id="dq-min-score" type="number" min="0" max="10" step="0.5" '
        f'value="{min_score:g}" class="dq-srcwt">'
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
