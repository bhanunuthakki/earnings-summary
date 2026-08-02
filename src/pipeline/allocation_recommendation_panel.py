"""Portfolio -> Allocation's owner-facing surfaces for the P0.4 Incremental
Dollar Recommendation (PRD ``docs/design/personal_investment_partner_prd.md``
§7.4 frontend / §7.1 decision-facing Risk Budget / §7.5 Portfolio Posture /
§12 design-system requirements). P0.4b — the UX layer over the P0.4a backend
(``allocation.recommendation_artifact.generate_recommendation``,
``allocation.actions.act_on_recommendation``, the
``/api/allocation/recommendation``, ``/adopt`` and ``/compare`` routes, all
already on main).

Three render entry points, each a pure read (GET-safe — no tracker call, no
LLM call):

* :func:`render_allocation_recommendation_section` — the current recommendation
  artifact, in one of four DISTINCT §12.2 states (never collapsed into one
  empty card): no artifact yet, fresh, stale/dirty, or a labeled mechanical
  fallback (``selection_mode="deterministic_fallback"`` — this backend never
  persists a bare "generation failed" row; a fallback IS the failure signal,
  carrying its own reason in ``central_hypothesis``).
* :func:`render_risk_budget_section` — the §7.1 four decision-facing
  categories only (concentration+zones, correlated/shared-driver exposure,
  downside/stress, capacity/liquidity), current vs prior valid snapshot delta,
  secondary metrics behind a ``<details>``.
* :func:`render_portfolio_posture_section` — a short derived paragraph (§7.5),
  affirmed facts stated as constraints, inferred behavior phrased as a
  proposal with Mostly-right/Adjust actions. Confirmation persists through
  ``owner_profile.store.append_fact`` (category="behavioral") — no parallel
  profile table.

:func:`render_allocation_today_card` is the compact Today doorway (§7.4
surface-parity exit gate): same artifact id, one line, quiet when there is
nothing to show.

All three console sections are pure reads over materialized caches / the
local DB — safe to render eagerly (no tracker round-trip), matching the
Positioning section's own GET-path discipline.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

import llm_artifact_store
import portfolio_risk_snapshot_store as risk_store
import wealth_context_store
from allocation.concentration import classify_zone
from allocation.recommendation_schema import IncrementalDollarRecommendation, RecommendationPlan
from owner_profile.store import list_facts
from pipeline.calibration_receipt import render_calibration_receipt_for
from portfolio_weights import read_materialized_weights, read_materialized_weights_as_of
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import ticker_label
from ui.prose import render_prose
from ui.time import stamp_html

PURPOSE = "incremental_dollar_recommendation"

_STYLE = """<style>
.alloc-grid { display:flex; flex-direction:column; gap:14px; }
.alloc-cash-form { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }
.alloc-cash-form input[type="number"] { width:160px; }
.alloc-cash-form input[type="text"] { width:200px; }
.alloc-error { color:var(--bad); font-size:var(--fs-body); margin-top:6px; white-space:pre-wrap; }
.alloc-plan-row { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px;
  padding:5px 0; border-bottom:1px dashed var(--border); }
.alloc-plan-row:last-child { border-bottom:none; }
.alloc-alt-grid { display:grid; grid-template-columns: 1fr 1fr; gap:12px; align-items:start; }
@media (max-width: 900px) { .alloc-alt-grid { grid-template-columns: 1fr; } }
.alloc-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; align-items:center; }
/* Calibration receipt (pipeline/calibration_receipt.py) — "when you've been
   here before," a muted read just ahead of the adopt/disposition buttons. */
.cr-receipt { color:var(--muted); font-size:var(--fs-caption); margin:8px 0 0; }
.alloc-compare-out { margin-top:10px; font-size:var(--fs-body); }
.alloc-compare-table { width:100%; border-collapse:collapse; font-size:var(--fs-body); }
.alloc-compare-table th, .alloc-compare-table td { padding:5px 8px; text-align:left;
  border-bottom:1px solid var(--border); }
.alloc-fallback-banner { margin-bottom:10px; }
.alloc-rationale { margin-top:10px; }
.alloc-rationale > summary { cursor:pointer; color:var(--fg); font-weight:600; padding:8px 0; }
.risk-cat-grid { display:grid; grid-template-columns: 1fr 1fr; gap:12px; }
@media (max-width: 900px) { .risk-cat-grid { grid-template-columns: 1fr; } }
.risk-cat { padding:4px 0; }
.risk-cat h4 { margin:0 0 6px; font-size:var(--fs-body); }
.risk-row { display:flex; justify-content:space-between; gap:8px; padding:2px 0;
  font-size:var(--fs-body); }
.posture-actions { display:flex; gap:8px; margin-top:10px; }
</style>"""


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def _usd(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _usd0(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}"


def _pct(v: float | None, *, digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}%"


def _num(v: float | None, *, digits: int = 2) -> str:
    """A plain scalar metric, rounded for display — never a raw float repr.

    Every numeric that reaches HTML goes through a formatter. An unrounded
    ``R²: 0.0976124298052155`` is noise, not information, and the UI guard in
    ``tests/test_ui_controls.py`` is token/component-shaped — it cannot see a
    float that was f-stringed straight into the markup. ``None`` renders as an
    em-dash, the same absent-value convention as ``_pct`` / ``_money``.
    """
    if v is None:
        return "—"
    return f"{v:,.{digits}f}"


def _zone_chip(zone: str | None) -> str:
    if not zone:
        return ""
    tone = {
        "ordinary": "",
        "meaningful": " k-chip-accent",
        "concentrated": " k-chip-warn",
        "highly_concentrated": " k-chip-warn",
        "exceptional": " k-chip-bad",
    }.get(zone, "")
    return f'<span class="k-chip{tone}">{escape(zone.replace("_", " "))}</span>'


def _parse_recommendation(content_json: object) -> IncrementalDollarRecommendation | None:
    if not isinstance(content_json, dict):
        return None
    try:
        return IncrementalDollarRecommendation.model_validate(content_json)
    except Exception:
        return None


def _cash_usd_of(rec: IncrementalDollarRecommendation) -> float:
    total = sum(a.dollars for a in rec.preferred_plan.allocations)
    return round(total + rec.preferred_plan.cash_retained_usd, 2)


# --------------------------------------------------------------------------- #
# 1. Incremental Dollar Recommendation section
# --------------------------------------------------------------------------- #

_CASH_FORM = """<form id="alloc-cash-form" class="alloc-cash-form">
<label class="k-label" for="alloc-cash-input">New cash</label>
<input type="number" id="alloc-cash-input" name="cash_usd" min="0" step="0.01"
  placeholder="e.g. 2500" required>
<label class="k-label" for="alloc-horizon-input">Horizon (optional)</label>
<input type="text" id="alloc-horizon-input" name="horizon" placeholder="e.g. 12-18mo">
<button type="submit" class="k-btn k-btn-primary" id="alloc-cash-submit">Get recommendation</button>
</form>
<div class="alloc-error" id="alloc-cash-error" hidden></div>"""


def _plan_html(plan: RecommendationPlan, *, label: str) -> str:
    if not plan.allocations:
        return (
            f'<div class="alloc-plan-row"><strong>{escape(label)}:</strong> '
            f"retain {_usd(plan.cash_retained_usd)} in cash</div>"
        )
    rows = "".join(
        f'<div class="alloc-plan-row">{ticker_label(a.ticker)}'
        f"<span>{_usd(a.dollars)} ({a.pct_of_cash:.0f}% of cash) &rarr; "
        f"{_pct(a.resulting_weight_pct, digits=2)} of book</span>"
        f"{_zone_chip(a.zone)}</div>"
        for a in plan.allocations
    )
    retained = (
        f'<div class="alloc-plan-row"><span>Cash retained: {_usd(plan.cash_retained_usd)}</span></div>'
        if plan.cash_retained_usd > 0.01
        else ""
    )
    return f"<div><strong>{escape(label)}</strong>{rows}{retained}</div>"


def _receipt_html(db_path: Path, top_ticker: str | None) -> str:
    """The calibration receipt for an 'add' (deploying new cash is always an
    add) on the plan's top ticker — the owner's own recent track record on
    calls like this one. "" when there's no ticker, the DB can't be read, or
    the cohort has fewer than 2 graded rows (render_calibration_receipt_for's
    own signal-quality suppression). Best-effort: never raises."""
    if not top_ticker:
        return ""
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return ""
    try:
        return render_calibration_receipt_for(conn, action="add", ticker=top_ticker)
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def _actions_row(artifact_id: int, rec: IncrementalDollarRecommendation, db_path: Path) -> str:
    cash_usd = _cash_usd_of(rec)
    top_ticker = (
        rec.preferred_plan.allocations[0].ticker if rec.preferred_plan.allocations else None
    )
    receipt = _receipt_html(db_path, top_ticker)
    simulate = ""
    if top_ticker:
        tq = escape(top_ticker, quote=True)
        simulate = (
            f'<a class="k-chip k-chip-btn" data-peek-url="/api/peek/whatif?ticker={tq}" '
            f'data-peek-title="What-if &middot; {tq}" href="/ticker/{tq}">Simulate weight &rarr;</a>'
        )
    ask_q = escape(
        f"Why is {top_ticker or 'this plan'} the preferred allocation for "
        f"${cash_usd:,.0f} of new cash?",
        quote=True,
    )
    return (
        f"{receipt}"
        '<div class="alloc-actions">'
        f'<button type="button" class="k-chip k-chip-btn" id="alloc-compare-toggle" '
        f'data-cash="{cash_usd:g}">Compare</button>'
        f'<button type="button" class="k-chip k-chip-btn" id="alloc-change-amount">Change amount</button>'
        f"{simulate}"
        f'<button type="button" class="k-chip k-chip-btn" data-ask-q="{ask_q}">Ask why</button>'
        f'<button type="button" class="k-btn k-btn-primary k-btn-sm" data-alloc-verb="save_intent" '
        f'data-alloc-id="{artifact_id}">Save as provisional intent</button>'
        f'<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-alloc-verb="hold_accountable" '
        f'data-alloc-id="{artifact_id}">Hold this view accountable</button>'
        f'<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-alloc-verb="dismiss" '
        f'data-alloc-id="{artifact_id}">Dismiss</button>'
        "</div>"
        '<div class="alloc-compare-out" id="alloc-compare-out" hidden></div>'
        '<div class="alloc-status" id="alloc-action-status" aria-live="polite"></div>'
    )


def _details_expansion(rec: IncrementalDollarRecommendation) -> str:
    """§7.4: "A details expansion shows component scores, data freshness,
    frontier construction, and provenance." The persisted artifact schema
    (``IncrementalDollarRecommendation``) does not carry the deterministic
    frontier's raw component scores or per-source freshness map (those are
    P0.4a generation-time inputs, not persisted output fields) — this shows
    everything the artifact DOES carry (frontier plan ids drawn from,
    source refs, engine/prompt versions, scenario reasoning, follow-up
    research) and points to Compare for live component-level reads
    (Sharpe delta, zone) on any candidate."""
    frontier_ids = ", ".join(rec.frontier_plan_ids) or "—"
    refs = "".join(f"<li>{escape(r)}</li>" for r in rec.source_refs) or "<li>none cited</li>"
    followup = "".join(f"<li>{escape(f)}</li>" for f in rec.followup_research)
    return (
        "<details><summary>Details &mdash; frontier, provenance, versions</summary>"
        '<div class="k-well">'
        f"<p><strong>Frontier plans drawn from:</strong> {escape(frontier_ids)}</p>"
        f"<p><strong>Source references:</strong></p><ul>{refs}</ul>"
        + (
            f"<p><strong>Suggested follow-up research:</strong></p><ul>{followup}</ul>"
            if followup
            else ""
        )
        + f"<p><strong>Scenario reasoning:</strong> {escape(rec.scenario_reasoning or '—')}</p>"
        f"<p><strong>Engine:</strong> {escape(rec.engine_version)} &middot; "
        f"<strong>Prompt:</strong> {escape(rec.prompt_version)} &middot; "
        f"<strong>Input hash:</strong> {escape(rec.input_sha[:12])}&hellip;</p>"
        '<p class="muted">For live component scores (Sharpe delta, zone) on any '
        "candidate, use Compare above.</p>"
        "</div></details>"
    )


def _card_body(artifact_id: int, rec: IncrementalDollarRecommendation, db_path: Path) -> str:
    fallback_banner = ""
    if rec.selection_mode == "deterministic_fallback":
        fallback_banner = (
            '<div class="k-well k-well-warn alloc-fallback-banner">'
            '<span class="k-pill k-pill-warn">mechanical fallback</span> '
            "No governed judgment ran for this plan &mdash; this is the deterministic "
            "frontier&#39;s balanced (or retain-cash) plan, unmodified. No synthesized "
            "confidence is shown.</div>"
        )
    alts = "".join(
        [
            f"<div>{_plan_html(rec.best_alternative, label='Best alternative')}</div>"
            if rec.best_alternative
            else "",
            f"<div>{_plan_html(rec.best_diversifier, label='Best diversifier')}</div>"
            if rec.best_diversifier
            else "",
        ]
    )
    confidence = (
        ""
        if rec.selection_mode == "deterministic_fallback"
        else (
            f'<p><span class="k-chip k-chip-mono">confidence: {escape(rec.confidence_verbal)}</span></p>'
        )
    )
    unknowns = "".join(f"<li>{escape(u)}</li>" for u in rec.main_unknowns)
    disconfirm = "".join(f"<li>{escape(d)}</li>" for d in rec.disconfirming_evidence)
    rationale = (
        '<details class="alloc-rationale"><summary>Why this plan &middot; '
        "uncertainties &amp; disconfirmers</summary>"
        f'<div class="k-well"><h3>Why this plan</h3>'
        f"{render_prose(rec.central_hypothesis)}"
        f"{render_prose(rec.personalization_why)}"
        f"{confidence}</div>"
        f'<div class="k-well"><h3>Main uncertainty &amp; disconfirming evidence</h3>'
        + (
            f"<p><strong>What could change this:</strong></p><ul>{unknowns}</ul>"
            if unknowns
            else ""
        )
        + (
            f"<p><strong>Disconfirming evidence:</strong></p><ul>{disconfirm}</ul>"
            if disconfirm
            else ""
        )
        + f"<p>{render_prose(rec.confidence_basis)}</p></div></details>"
    )
    return (
        f"{fallback_banner}"
        f'<div class="k-well"><h3>Preferred plan &middot; '
        f'<span class="k-chip k-chip-mono">{escape(rec.status.replace("_", " "))}</span></h3>'
        f"{_plan_html(rec.preferred_plan, label='Preferred')}</div>"
        f"{rationale}"
        + (f'<div class="alloc-alt-grid">{alts}</div>' if alts else "")
        + _actions_row(artifact_id, rec, db_path)
        + _details_expansion(rec)
    )


def render_allocation_recommendation_section(db_path: Path, repo_root: Path) -> str:
    """The §7.4 Incremental Dollar Recommendation card. Distinct §12.2
    states: no artifact yet, fresh, stale/dirty, or a labeled mechanical
    fallback. Never raises — a read failure degrades to the no-artifact
    state (the same UI an owner who never generated one sees)."""
    del repo_root  # reserved for a future live-context read; unused today
    try:
        artifact = llm_artifact_store.read_current(
            ticker=None, purpose=PURPOSE, scope="portfolio", db_path=db_path
        )
    except Exception:
        artifact = None

    head = '<section class="panel"><h2>Incremental Dollar Recommendation</h2>'
    sub = (
        '<p class="sub">What the next dollar of new cash should do &mdash; a governed '
        "read over the deterministic frontier, your affirmed capacity, and current "
        "Risk Budget. Nothing is deployed until you act on it.</p>"
    )

    if artifact is None:
        return (
            f"{_STYLE}{head}{sub}"
            '<p class="muted">Not generated yet &mdash; enter an amount below to get a '
            "recommendation.</p>"
            f"{_CASH_FORM}</section>{_ALLOC_JS}"
        )

    rec = _parse_recommendation(artifact.content_json)
    if rec is None:
        # A stored row that no longer parses against the current schema —
        # distinct from "never generated" (§12.2: never collapse these).
        return (
            f"{_STYLE}{head}{sub}"
            '<div class="k-well k-well-bad"><p>The stored recommendation failed to '
            "read back against the current schema &mdash; generate a fresh one.</p></div>"
            f"{_CASH_FORM}</section>{_ALLOC_JS}"
        )

    stale = bool(artifact.dirty) or bool(
        artifact.expires_at and artifact.expires_at < datetime.now(UTC).replace(tzinfo=None)
    )
    stamp = stamp_html(artifact.generated_at, mode="rel", prefix="as of ")
    stale_badge = (
        f' <span class="k-pill k-pill-warn">stale</span> '
        f'<button type="button" class="k-btn k-btn-quiet k-btn-sm" id="alloc-refresh-btn" '
        f'data-cash="{_cash_usd_of(rec):g}" data-horizon="">Refresh</button>'
        if stale
        else ""
    )
    head_with_id = head.replace(
        '<section class="panel">', f'<section class="panel" data-artifact-id="{artifact.id}">'
    )
    return (
        f"{_STYLE}{head_with_id}{sub}"
        f'<p class="alloc-meta">{stamp}{stale_badge}</p>'
        f'<details id="alloc-cash-details"><summary>Change amount</summary>{_CASH_FORM}</details>'
        f"{_card_body(artifact.id, rec, db_path)}"
        f"</section>{_ALLOC_JS}"
    )


_ALLOC_JS = """<script>
(function () {
  var section = document.currentScript ? document.currentScript.closest('section') : null;
  if (!section || section.__allocWired) return;
  section.__allocWired = true;

  function statusEl() { return section.querySelector('#alloc-action-status'); }
  function refreshSection() {
    fetch('/api/panel/allocation_recommendation').then(function (r) {
      return r.ok ? r.text() : null;
    }).then(function (html) {
      if (html == null) return;
      section.outerHTML = html;
    }).catch(function () {});
  }

  var cashForm = section.querySelector('#alloc-cash-form');
  if (cashForm) {
    cashForm.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var cash = parseFloat(section.querySelector('#alloc-cash-input').value);
      var horizon = (section.querySelector('#alloc-horizon-input') || {}).value || '';
      var errEl = section.querySelector('#alloc-cash-error');
      var submitBtn = section.querySelector('#alloc-cash-submit');
      if (!(cash > 0)) {
        if (errEl) { errEl.hidden = false; errEl.textContent = 'Enter an amount greater than 0.'; }
        return;
      }
      if (errEl) errEl.hidden = true;
      if (submitBtn) CCAction.busy(submitBtn, 'Working…');
      fetch('/api/allocation/recommendation', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cash_usd: cash, horizon: horizon || undefined })
      }).then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      }).then(function (res) {
        if (!res.ok) {
          if (submitBtn) CCAction.release(submitBtn);
          if (errEl) { errEl.hidden = false; errEl.textContent = res.data.error || 'Recommendation failed.'; }
          return;
        }
        if (submitBtn) CCAction.receipt(submitBtn, '✓ Recommendation generated');
        setTimeout(refreshSection, 500);
      }).catch(function (err) {
        if (submitBtn) CCAction.release(submitBtn);
        if (errEl) { errEl.hidden = false; errEl.textContent = 'Recommendation failed: ' + err; }
      });
    });
  }

  var refreshBtn = section.querySelector('#alloc-refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () {
      var cash = parseFloat(refreshBtn.getAttribute('data-cash') || '0');
      var horizon = refreshBtn.getAttribute('data-horizon') || '';
      CCAction.busy(refreshBtn, 'Refreshing…');
      fetch('/api/allocation/recommendation', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cash_usd: cash, horizon: horizon || undefined })
      }).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        CCAction.receipt(refreshBtn, '✓ Refreshed');
        setTimeout(refreshSection, 500);
      }).catch(function () {
        CCAction.release(refreshBtn);
      });
    });
  }

  var changeAmount = section.querySelector('#alloc-change-amount');
  if (changeAmount) {
    changeAmount.addEventListener('click', function () {
      var d = section.querySelector('#alloc-cash-details');
      if (d) { d.open = true; d.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
    });
  }

  section.querySelectorAll('[data-alloc-verb]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var verb = btn.getAttribute('data-alloc-verb');
      var id = btn.getAttribute('data-alloc-id');
      CCAction.busy(btn, '…');
      fetch('/api/allocation/recommendation/' + id + '/adopt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ verb: verb })
      }).then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      }).then(function (res) {
        var st = statusEl();
        if (res.ok) {
          if (st) st.textContent = res.data.status || 'Disposition saved.';
          CCAction.receipt(btn, '✓ Disposition saved');
        } else {
          if (st) st.textContent = res.data.error || 'Save failed.';
          CCAction.release(btn);
        }
      }).catch(function () {
        var st = statusEl();
        if (st) st.textContent = 'Save failed — network error.';
        CCAction.release(btn);
      });
    });
  });

  var compareToggle = section.querySelector('#alloc-compare-toggle');
  if (compareToggle) {
    compareToggle.addEventListener('click', function () {
      var out = section.querySelector('#alloc-compare-out');
      if (!out) return;
      var cash = parseFloat(compareToggle.getAttribute('data-cash') || '0');
      var tickers = [];
      section.querySelectorAll('.k-tick-sym').forEach(function (el) {
        var t = (el.textContent || '').trim().toUpperCase();
        if (t && tickers.indexOf(t) < 0 && tickers.length < 3) tickers.push(t);
      });
      if (!tickers.length) { out.hidden = false; out.textContent = 'No tickers to compare.'; return; }
      out.hidden = false; out.textContent = 'Comparing...';
      fetch('/api/allocation/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tickers: tickers, cash_usd: cash })
      }).then(function (r) { return r.json(); }).then(function (data) {
        var rows = (data.tickers || []).map(function (row) {
          return '<tr><td>' + row.ticker + '</td><td>' + (row.eligible ? 'eligible' : 'ineligible')
            + '</td><td>' + (row.zone || '') + '</td><td>'
            + (row.current_weight_pct != null ? row.current_weight_pct.toFixed(1) + '%' : '')
            + '</td><td>' + (row.sharpe_delta_bps != null ? row.sharpe_delta_bps.toFixed(0) + 'bp' : '')
            + '</td></tr>';
        }).join('');
        out.innerHTML = '<table class="alloc-compare-table"><thead><tr><th>Ticker</th>'
          + '<th>Status</th><th>Zone</th><th>Current wt</th><th>&Delta;SR</th></tr></thead>'
          + '<tbody>' + rows + '</tbody></table>';
      }).catch(function (err) { out.textContent = 'error: ' + err; });
    });
  }
})();
</script>"""


# --------------------------------------------------------------------------- #
# 2. Risk Budget section (§7.1 decision-facing four categories)
# --------------------------------------------------------------------------- #


def _delta(cur: float | None, prior: float | None, *, digits: int = 1, suffix: str = "pp") -> str:
    if cur is None or prior is None:
        return ""
    d = cur - prior
    if abs(d) < 10 ** (-digits) / 2:
        return ""
    arrow = "&uarr;" if d > 0 else "&darr;"
    return f' <span class="muted">({arrow} {abs(d):.{digits}f}{suffix} vs prior)</span>'


def render_risk_budget_section(db_path: Path, repo_root: Path) -> str:
    """The §7.1 decision-facing Risk Budget: exactly four categories, current
    vs prior valid snapshot delta, secondary metrics behind a details
    expansion. Never renders a null metric as zero (an absent value shows
    an em-dash, not 0)."""
    head = '<section class="panel"><h2>Risk Budget</h2>'
    try:
        history = risk_store.read_history(limit=2, db_path=db_path)
    except Exception:
        history = []
    snap = history[0] if history else None
    if snap is None:
        try:
            snap = risk_store.read_latest_snapshot(db_path=db_path)
        except Exception:
            snap = None
    if snap is None:
        return (
            f"{_STYLE}{head}"
            '<p class="muted">No risk snapshot yet &mdash; run the morning pipeline or '
            "<code>execution/refresh_portfolio_risk_snapshot.py</code>.</p></section>"
        )
    prior = history[1] if len(history) > 1 else None

    # PRD §7.1.9: a "vs prior" delta must never be rendered across a
    # metric-version / rebase-basis change — that is a false delta, not a
    # real risk move. When the two rows aren't comparable, null out `prior`
    # so every _delta(...) call below degrades to "" automatically, and
    # surface the reason once near the timestamp (same stale-pill treatment).
    comparability_note = ""
    if prior is not None and not risk_store.comparable(snap, prior):
        reason = risk_store.incomparable_reason(snap, prior)
        if reason:
            comparability_note = f' <span class="k-pill k-pill-warn">{escape(reason)}</span>'
        prior = None

    is_stale = False
    try:
        captured = datetime.fromisoformat(snap.captured_at) if snap.captured_at else None
        if captured is not None:
            if captured.tzinfo is not None:
                captured = captured.astimezone(UTC).replace(tzinfo=None)
            is_stale = (datetime.now(UTC).replace(tzinfo=None) - captured) > timedelta(days=3)
    except Exception:
        is_stale = False
    stamp = stamp_html(snap.captured_at or None, mode="rel", prefix="as of ")
    stale_pill = ' <span class="k-pill k-pill-warn">stale</span>' if is_stale else ""

    # 1. Single-name concentration + zones (materialized weights — no tracker call).
    try:
        weights = read_materialized_weights(repo_root)
    except Exception:
        weights = {}
    top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:5]

    def _conc_row(t: str, w: float) -> str:
        za = classify_zone(w * 100.0)
        return (
            f'<div class="risk-row">{ticker_label(t)}<span>{_pct(w * 100.0, digits=1)} '
            f"{_zone_chip(za.zone if za is not None else None)}</span></div>"
        )

    conc_rows = (
        "".join(_conc_row(t, w) for t, w in top)
        or '<p class="muted">No materialized weights on file.</p>'
    )
    top1_delta = _delta(snap.top1_weight_pct, prior.top1_weight_pct if prior else None)
    concentration_cat = (
        '<div class="risk-cat"><h4>1. Single-name concentration</h4>'
        f"<p>Top-1 weight: {_pct(snap.top1_weight_pct)}{top1_delta} &middot; "
        f"HHI: {_num(snap.hhi, digits=0)}</p>"
        f"{conc_rows}</div>"
    )

    # 2. Correlated/shared-driver exposure + thesis collision (degrades silently).
    collision_note = ""
    try:
        from thesis_collision import read_cached_report

        cached = read_cached_report(db_path)
        if cached is not None:
            n_clusters = len(cached.report.clusters)
            n_contra = len(cached.report.contradictions)
            if n_clusters or n_contra:
                collision_note = (
                    f"<p>Thesis collision audit: {n_clusters} shared-driver cluster(s), "
                    f"{n_contra} contradiction(s) &mdash; "
                    '<a class="k-chip k-chip-btn" href="/#portfolio_health">see Health &rarr;</a></p>'
                )
    except Exception:
        collision_note = ""
    corr_val = (
        f"{snap.weighted_avg_correlation_spy:+.2f}"
        if snap.weighted_avg_correlation_spy is not None
        else "—"
    )
    shared_driver_cat = (
        '<div class="risk-cat"><h4>2. Correlated / shared-driver exposure</h4>'
        f"<p>Weighted avg correlation to SPY: {corr_val}</p>{collision_note}</div>"
    )

    # 3. Downside/stress and drawdown posture.
    dd_cat = (
        '<div class="risk-cat"><h4>3. Downside / stress</h4>'
        f"<p>Current drawdown: {_pct(snap.current_drawdown_pct)} &middot; "
        f"Max drawdown: {_pct(snap.max_drawdown_pct)}"
        f"{_delta(snap.current_drawdown_pct, prior.current_drawdown_pct if prior else None)}</p></div>"
    )

    # 4. Capacity/liquidity + any affirmed hard limit.
    cash_band = "—"
    try:
        wc = wealth_context_store.read_latest(db_path=db_path)
        if wc is not None and wc.snapshot is not None and wc.snapshot.cash_need_band:
            band_age = stamp_html(wc.as_of, mode="rel", prefix="as of ")
            cash_band = (
                f'{escape(wc.snapshot.cash_need_band)} <span class="muted">({band_age})</span>'
            )
    except Exception:
        pass
    limits_note = _affirmed_capacity_limits(db_path)
    capacity_cat = (
        '<div class="risk-cat"><h4>4. Capacity / liquidity</h4>'
        f"<p>Cash-need posture: {cash_band}</p>{limits_note}</div>"
    )

    secondary = (
        '<details><summary>Secondary metrics</summary><div class="k-well">'
        f"<p>Sharpe: {_num(snap.sharpe)} &middot; "
        f"Sortino: {_num(snap.sortino)} &middot; "
        f"Beta: {_num(snap.beta)} &middot; "
        f"R&sup2;: {_num(snap.r_squared)}</p>"
        f"<p>Top5 weight: {_pct(snap.top5_weight_pct)} &middot; "
        f"Top10 weight: {_pct(snap.top10_weight_pct)} &middot; "
        f"Effective holdings: {_num(snap.effective_holdings, digits=1)}</p>"
        f"<p>Growth tilt: {_num(snap.growth_tilt)} &middot; "
        f"Rate beta (10y): {_num(snap.rate_beta_10y)}</p>"
        "</div></details>"
    )

    return (
        f"{_STYLE}{head}"
        f'<p class="alloc-meta">{stamp}{stale_pill}{comparability_note}</p>'
        f'<div class="risk-cat-grid">{concentration_cat}{shared_driver_cat}{dd_cat}{capacity_cat}</div>'
        f"{secondary}</section>"
    )


def _affirmed_capacity_limits(db_path: Path) -> str:
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return ""
    try:
        rows = list_facts(conn, status="affirmed", category="capacity")
    except Exception:
        return ""
    finally:
        conn.close()
    limits = [r for r in rows if r.key.startswith("human_capital.")]
    if not limits:
        return ""
    items = "".join(f"<li>{escape(r.narrative)[:160]}</li>" for r in limits[:5])
    return f"<p>Affirmed hard limits:</p><ul>{items}</ul>"


# --------------------------------------------------------------------------- #
# 3. Portfolio Posture section (§7.5)
# --------------------------------------------------------------------------- #


def _posture_paragraph(
    snap: risk_store.RiskSnapshot | None, weights_top: list[tuple[str, float]]
) -> str:
    bits: list[str] = []
    if snap is not None and snap.growth_tilt is not None:
        tilt_word = (
            "growth-tilted"
            if snap.growth_tilt > 0.1
            else ("value-tilted" if snap.growth_tilt < -0.1 else "style-neutral")
        )
        bits.append(tilt_word)
    if weights_top:
        top_t, top_w = weights_top[0]
        zone = classify_zone(top_w * 100.0)
        if zone is not None and zone.zone in ("concentrated", "highly_concentrated", "exceptional"):
            bits.append(f"concentrated (led by {top_t} at {top_w * 100.0:.0f}%)")
        else:
            bits.append("diversified across current holdings")
    if (
        snap is not None
        and snap.weighted_avg_correlation_spy is not None
        and snap.weighted_avg_correlation_spy > 0.6
    ):
        bits.append("meaningfully correlated to the broad market")
    if not bits:
        return "Not enough live data to characterize the current book yet."
    return "Your book currently reads as " + ", ".join(bits) + "."


def render_portfolio_posture_section(db_path: Path, repo_root: Path) -> str:
    """§7.5: a short derived paragraph, affirmed facts as stated constraints,
    inferred behavior as a proposal with Mostly-right/Adjust actions.
    Confirmation persists via ``owner_profile.store.append_fact`` — no
    parallel profile table."""
    head = '<section class="panel"><h2>Portfolio Posture</h2>'
    try:
        snap = risk_store.read_latest_snapshot(db_path=db_path)
    except Exception:
        snap = None
    try:
        weights = read_materialized_weights(repo_root)
    except Exception:
        weights = {}
    top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:3]

    if snap is None and not weights:
        return (
            f"{_STYLE}{head}"
            '<p class="muted">Not enough live data yet to describe the current book.</p>'
            "</section>"
        )

    narrative = _posture_paragraph(snap, top)
    constraints = _affirmed_constraint_lines(db_path)
    weights_as_of = read_materialized_weights_as_of(repo_root) or ""

    return (
        f"{_STYLE}{head}"
        f'<div class="k-well"><p>{escape(narrative)}</p>'
        f'<p class="muted">Derived from live book weights and the latest Risk Budget'
        + (f" (as of {escape(weights_as_of[:10])})" if weights_as_of else "")
        + ".</p>"
        + (
            f"<p><strong>Stated constraints (affirmed):</strong></p><ul>{constraints}</ul>"
            if constraints
            else ""
        )
        + '<div class="posture-actions">'
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" id="posture-confirm" '
        f'data-narrative="{escape(narrative, quote=True)}">Mostly right</button>'
        '<button type="button" class="k-chip k-chip-btn" data-console-jump="csec-positioning">'
        "Adjust &rarr;</button>"
        '<span id="posture-status" class="muted" aria-live="polite"></span>'
        "</div></div></section>"
        f"{_POSTURE_JS}"
    )


def _affirmed_constraint_lines(db_path: Path) -> str:
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return ""
    try:
        rows = list_facts(conn, status="affirmed")
    except Exception:
        return ""
    finally:
        conn.close()
    if not rows:
        return ""
    return "".join(f"<li>[{escape(r.category)}] {escape(r.narrative)[:160]}</li>" for r in rows[:5])


_POSTURE_JS = """<script>
(function () {
  var section = document.currentScript ? document.currentScript.closest('section') : null;
  if (!section || section.__postureWired) return;
  section.__postureWired = true;
  var confirmBtn = section.querySelector('#posture-confirm');
  if (!confirmBtn) return;
  confirmBtn.addEventListener('click', function () {
    var st = section.querySelector('#posture-status');
    CCAction.busy(confirmBtn, 'Confirming…');
    fetch('/api/positioning/confirm-posture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ narrative: confirmBtn.getAttribute('data-narrative') || '' })
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.ok) {
        if (st) st.textContent = 'Confirmed — recorded as your affirmed read.';
        CCAction.receipt(confirmBtn, '✓ Confirmed');
      } else {
        if (st) st.textContent = data.error || 'Confirm failed.';
        CCAction.release(confirmBtn);
      }
    }).catch(function () {
      if (st) st.textContent = 'Confirm failed — network error.';
      CCAction.release(confirmBtn);
    });
  });
})();
</script>"""


# --------------------------------------------------------------------------- #
# Today compact card (§7.4 surface-parity exit gate)
# --------------------------------------------------------------------------- #

_TODAY_STYLE = """<style>
.cc-alloc-today { display:flex; align-items:baseline; gap:8px; margin:0 0 10px;
  font-size:var(--fs-body); }
</style>"""


def render_allocation_today_card(db_path: Path | str | None) -> str:
    """The Today compact doorway: preferred plan headline + staleness + a
    doorway into `/#portfolio_allocation`. Renders nothing when there is no
    current recommendation (Today stays quiet — never a "no recommendation"
    stub on the landing page). Never raises."""
    if db_path is None:
        return ""
    try:
        artifact = llm_artifact_store.read_current(
            ticker=None, purpose=PURPOSE, scope="portfolio", db_path=Path(db_path)
        )
    except Exception:
        return ""
    if artifact is None:
        return ""
    rec = _parse_recommendation(artifact.content_json)
    if rec is None:
        return ""
    if not rec.preferred_plan.allocations:
        headline = f"Next dollar: retain {_usd0(rec.preferred_plan.cash_retained_usd)} in cash"
    else:
        a = rec.preferred_plan.allocations[0]
        extra = (
            f" +{len(rec.preferred_plan.allocations) - 1} more"
            if len(rec.preferred_plan.allocations) > 1
            else ""
        )
        zone = classify_zone(a.resulting_weight_pct)
        zone_word = f" ({zone.zone.replace('_', ' ')} zone)" if zone else ""
        headline = f"Next dollar: {a.ticker} {_usd0(a.dollars)}{zone_word}{extra}"
    stamp = stamp_html(artifact.generated_at, mode="rel")
    return (
        f"{_TODAY_STYLE}"
        f'<div class="cc-alloc-today k-well" data-artifact-id="{artifact.id}">'
        f'<a class="k-chip k-chip-btn" href="/#portfolio_allocation">{escape(headline)}</a>'
        f'<span class="muted">{stamp}</span></div>'
    )


__all__ = [
    "render_allocation_recommendation_section",
    "render_allocation_today_card",
    "render_portfolio_posture_section",
    "render_risk_budget_section",
]
