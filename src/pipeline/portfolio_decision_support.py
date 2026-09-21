"""Risk Budget and Portfolio Posture presentation.

These read-only views are independent of any capital-allocation recommendation.
They retain their existing database and owner-profile authorities.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

import portfolio_risk_snapshot_store as risk_store
import wealth_context_store
from allocation.concentration import classify_zone
from owner_profile.store import list_facts
from pipeline.portfolio_styles import allocation_css
from portfolio_weights import read_materialized_weights, read_materialized_weights_as_of
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import ticker_label
from ui.time import stamp_html

_STYLE = allocation_css()


def _pct(v: float | None, *, digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}%"


def _num(v: float | None, *, digits: int = 2) -> str:
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
        "Legacy rate sensitivity quarantined; use current versioned estimates.</p>"
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


def render_portfolio_posture_section(
    db_path: Path,
    repo_root: Path,
    *,
    include_actions: bool = True,
) -> str:
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

    actions = (
        '<div class="posture-actions">'
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" id="posture-confirm" '
        f'data-narrative="{escape(narrative, quote=True)}">Mostly right</button>'
        '<button type="button" class="k-chip k-chip-btn" data-console-jump="csec-positioning">'
        "Adjust &rarr;</button>"
        '<span id="posture-status" class="muted" aria-live="polite"></span>'
        "</div>"
        if include_actions
        else '<p class="muted">Posture review remains available in the governed Positioning workflow.</p>'
    )
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
        + actions
        + "</div></section>"
        + (_POSTURE_JS if include_actions else "")
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

_TODAY_STYLE = allocation_css()


__all__ = [
    "render_portfolio_posture_section",
    "render_risk_budget_section",
]
