"""Portfolio composite consoles (Phase-5 IA: 8 sub-tabs → 3 destinations).

The owner's aggressive-redesign verdict: "too many surfaces with duplicative
functions." The eight Portfolio sub-tabs (Synthesis / Performance / Risk / Red
Team / Positioning / Decisions / Memos / Triggers) collapse into three composite
pages, each COMPOSING the existing builders behind an anchor-nav band — the S10
Provenance-console pattern (``pipeline/console_scaffold.py``):

* **Health** — thesis health & what could break it: Synthesis + Risk + Red Team.
* **Allocation** — where capital goes & how it's doing: Positioning + Performance.
* **Record** — the audit trail: Decisions + Memos + Triggers.

No builder logic is duplicated — every section is one of the existing
``render_*`` panel builders, each of which already degrades to a quiet stub on
missing data, so the consoles are robust by construction. The per-builder
``/api/panel/<id>`` fetch routes stay live (the old ids alias to these composites
via ``command_center_shell._LEGACY_PANEL_REDIRECTS``, and any direct fetch / peek
still hits the builder route).

Wave 1 of ``docs/design/surface_density_jit_redesign.md`` (owner walkthrough
2026-07-24): Allocation + Record are the D1 page-model reference
implementation — a Band-1 synthesized *read* leads, the sections lay out as a
dense multi-column tile grid instead of a full-width vertical stack, and the
What-if/Compare signpost section is gone (its actions live on the Next-dollar
tile; a nav chip jumps there — a section may never exist solely to say where
functionality lives). The briefs are deterministic composers over already-
governed artifacts and caches — no new LLM purpose, no render-path LLM call.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from identity import DEFAULT_USER_ID
from pipeline.console_scaffold import ConsoleSection, render_console
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# The D1 tile grid + Band-1 brief. Tokens only (design_language §2): auto-fit
# tiles ≥460px so a wide desktop viewport gets 2-3 columns and a narrow one
# degrades to a single column without a media query; `csec-wide` spans the
# brief and the landing section across every column.
_CONSOLE_CSS = """<style>
.console-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(460px, 1fr));
  gap: var(--sp-2); align-items: start; }
.console-grid .console-sec { min-width: 0; }
.console-grid .console-sec.csec-wide { grid-column: 1 / -1; }
.console-brief { background: var(--surface); border-radius: var(--radius);
  padding: 11px 14px; }
.console-brief h2 { margin: 0 0 4px; font-size: var(--fs-title); }
.console-brief .cb-sub { color: var(--muted); font-size: var(--fs-caption); margin: 0 0 6px; }
.console-brief p.cb-line { margin: 0 0 3px; font-size: var(--fs-body); line-height: 1.5; }
.console-brief .cb-links { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 7px; }
</style>"""


def _lazy_section_placeholder(endpoint: str, label: str) -> str:
    """A deferred console section (wave B B4a): a placeholder that HTMX swaps
    for the real builder fragment when it scrolls into view. The composite's
    landing section paints immediately; the heavy tracker/LLM builders load on
    reveal through the per-builder ``/api/panel/<id>`` routes (deliberately
    kept live). ``hx-swap="outerHTML"`` replaces only this inner div — the
    ``csec-*`` wrapper ``render_console`` adds stays, so the anchor-nav jump
    chips still land. The shell inlines HTMX and its ``injectHtml`` calls
    ``htmx.process`` on injected fragments, so ``revealed`` wires reliably."""
    return (
        f'<div hx-get="{escape(endpoint, quote=True)}" hx-trigger="revealed" '
        'hx-swap="outerHTML">'
        f'<p class="cc-loading">Loading {escape(label)}…</p>'
        "</div>"
    )


# Health console (owner directive 2026-07-30): the read, then exactly TWO
# chip-tab cards — nothing below the fold. Chips (.k-chip-btn + the kit's
# .is-on active state) swap panes in place; each pane fetches its fragment on
# first activation, so the console shell paints instantly. Layout-only CSS —
# the chips/pills are the kit.
_HEALTH_CSS = """<style>
.hc-card { min-width: 0; }
.hc-h { margin: 0 0 4px; font-size: var(--fs-title); }
.hc-tabs { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; }
.hc-pane[hidden] { display: none; }
</style>"""

# Chip switcher + fetch-on-first-activation pane loader. One guarded
# document-level listener (re-injected fragments never double-wire); the
# trailing scan runs on EVERY inject so the default pane of each card loads
# without a click. A failed fetch clears the loaded flag so pressing the chip
# again retries.
_HEALTH_TABS_JS = """
(function () {
  function loadPane(pane) {
    if (!pane || !pane.dataset.src || pane.dataset.loaded === '1') return;
    pane.dataset.loaded = '1';
    fetch(pane.dataset.src).then(function (r) { return r.text(); }).then(function (html) {
      pane.innerHTML = html;
      var scripts = pane.querySelectorAll('script');
      for (var i = 0; i < scripts.length; i++) {
        var old = scripts[i];
        var s = document.createElement('script');
        if (old.src) s.src = old.src; else s.textContent = old.textContent;
        old.parentNode.replaceChild(s, old);
      }
    }).catch(function () {
      pane.dataset.loaded = '';
      pane.innerHTML = '<p class="muted">Failed to load — press the chip again to retry.</p>';
    });
  }
  if (!window.__ccHealthTabs) {
    window.__ccHealthTabs = true;
    document.addEventListener('click', function (ev) {
      var b = ev.target && ev.target.closest ? ev.target.closest('[data-hc-pane]') : null;
      if (!b) return;
      var card = b.closest('.hc-card');
      var pane = document.getElementById(b.getAttribute('data-hc-pane'));
      if (!card || !pane) return;
      var chips = card.querySelectorAll('[data-hc-pane]');
      for (var i = 0; i < chips.length; i++) chips[i].classList.toggle('is-on', chips[i] === b);
      var panes = card.querySelectorAll('.hc-pane');
      for (var j = 0; j < panes.length; j++) {
        if (panes[j] === pane) panes[j].removeAttribute('hidden');
        else panes[j].setAttribute('hidden', '');
      }
      loadPane(pane);
    });
  }
  var open = document.querySelectorAll('.hc-pane:not([hidden])[data-src]');
  for (var k = 0; k < open.length; k++) loadPane(open[k]);
})();
""".strip()

# (anchor, header question, ((fragment key, chip label), ...)). Anchors keep
# the legacy csec-synthesis / csec-risk ids so old #portfolio_synthesis /
# #portfolio_risk deep-links still land on the right card. Max 4 chips per
# card — the owner's cap; anything that didn't earn a chip lives on the
# still-live /api/panel/portfolio_risk route or behind an Ask doorway.
_HEALTH_CARDS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "synthesis",
        "What could break?",
        (("thesis", "Theses"), ("exposure", "Exposure"), ("collisions", "Collisions")),
    ),
    (
        "risk",
        "How exposed is the book?",
        (("bets", "Bets"), ("drawdown", "Drawdown"), ("crowding", "Crowding"), ("tail", "Tail")),
    ),
)


def _health_card(anchor: str, question: str, tabs: tuple[tuple[str, str], ...]) -> str:
    chips = "".join(
        f'<button type="button" class="k-chip k-chip-btn{" is-on" if i == 0 else ""}" '
        f'data-hc-pane="hcp-{key}">{escape(label)}</button>'
        for i, (key, label) in enumerate(tabs)
    )
    panes = "".join(
        f'<div class="hc-pane" id="hcp-{key}"{"" if i == 0 else " hidden"} '
        f'data-src="/api/panel/portfolio_health?fragment={key}">'
        '<p class="cc-loading">Loading…</p></div>'
        for i, (key, _label) in enumerate(tabs)
    )
    return (
        f'<div class="console-sec hc-card" id="csec-{escape(anchor)}">'
        f'<h2 class="hc-h">{escape(question)}</h2>'
        f'<div class="hc-tabs">{chips}</div>{panes}</div>'
    )


def render_portfolio_health_panel(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """Portfolio → Health, redesigned (owner directive 2026-07-30): the Band-1
    read, then exactly TWO chip-tab cards side by side — Theses (thesis
    rollup / sector exposure / collision audit) and Book risk (implicit bets /
    drawdown / crowding & correlation / tail stress). Chips swap panes in
    place — no scrolling; each pane lazy-fetches
    ``/api/panel/portfolio_health?fragment=<key>``
    (``portfolio_panel.render_health_fragment``).

    Cut from the console, not from the platform: Red Team and the whole-book
    macro-stress lens are Ask questions now (``data-ask-q`` doorways on the
    brief — the shell's Law-2 hand-off); style/business factors, bear lint,
    the naked-position gate and the risk-vs-reward gap stay on the still-live
    ``/api/panel/portfolio_risk`` route."""
    del user_id
    cards = "".join(_health_card(a, q, tabs) for a, q, tabs in _HEALTH_CARDS)
    return (
        _CONSOLE_CSS
        + _HEALTH_CSS
        + '<div class="portfolio-health-console">'
        + _health_brief(db_path)
        + f'<div class="console-grid">{cards}</div>'
        + "</div>"
        + f"<script>{_HEALTH_TABS_JS}</script>"
    )


# --------------------------------------------------------------------------- #
# Band-1 briefs — the console's synthesized "read" (D1). Deterministic
# composition over already-governed caches; every fact is individually
# best-effort so a missing table drops its line, never the brief.
# --------------------------------------------------------------------------- #


def _brief_shell(title: str, sub: str, lines: list[str], links: list[str]) -> str:
    body = "".join(f'<p class="cb-line">{ln}</p>' for ln in lines) or (
        '<p class="cb-line muted">Not enough live data for a read yet — the tiles '
        "below carry the detail.</p>"
    )
    links_html = f'<div class="cb-links">{"".join(links)}</div>' if links else ""
    return (
        '<section class="panel console-brief"><h2>'
        f"{escape(title)}</h2>"
        f'<p class="cb-sub">{escape(sub)}</p>{body}{links_html}</section>'
    )


def _jump_chip(anchor: str, label: str) -> str:
    return (
        f'<button type="button" class="k-chip k-chip-btn" data-console-jump="csec-{anchor}">'
        f"{escape(label)}</button>"
    )


def _ask_chip(question: str, label: str) -> str:
    """A Law-2 Ask doorway chip: the shell's document-level ``data-ask-q``
    listener hands the question to the Ask panel on click. Red Team and the
    macro-stress lens live behind these now — on-demand questions, not
    standing console sections (owner directive 2026-07-30)."""
    return (
        f'<button type="button" class="k-chip k-chip-btn" data-ask-q="{escape(question, quote=True)}">'
        f"{escape(label)}</button>"
    )


def _health_brief(db_path: Path) -> str:
    """The Health read: where the theses stand and how fresh the risk picture
    is, plus the Ask doorways for the on-demand adversarial questions."""
    lines: list[str] = []
    links: list[str] = []

    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        conn = None
    if conn is not None:
        try:
            # Thesis health over the owner's REAL theses only — the same
            # non-stub predicate the trigger ladder uses (bulk-onboarded STUB
            # rows would otherwise read as 29 phantom breaches).
            try:
                counts = dict(
                    conn.execute(
                        "SELECT ts.breach_status, COUNT(*) FROM thesis_state ts "
                        "JOIN tracked_companies tc ON tc.ticker = ts.ticker "
                        "AND tc.archived_at IS NULL "
                        "AND tc.list_type IN ('portfolio', 'evaluation') "
                        "WHERE TRIM(COALESCE(ts.thesis, '')) <> '' "
                        "AND ts.thesis NOT LIKE '%STUB:%' "
                        "GROUP BY ts.breach_status"
                    ).fetchall()
                )
                if counts:
                    order = ("breach", "warn", "watch", "ok")
                    bits = [f"{counts[k]} {escape(k)}" for k in order if counts.get(k)] + [
                        f"{v} {escape(str(k))}" for k, v in counts.items() if k not in order and v
                    ]
                    tone_lead = "breach" in counts and counts["breach"] > 0
                    lead = (
                        "<strong>Thesis health needs eyes:</strong> "
                        if tone_lead
                        else "Thesis health: "
                    )
                    lines.append(lead + " &middot; ".join(bits) + ".")
            except sqlite3.Error:
                pass
        finally:
            conn.close()

    try:
        import portfolio_risk_snapshot_store as risk_store
        from ui.time import stamp_html

        snap = risk_store.read_latest_snapshot(db_path=db_path)
        if snap is not None and snap.captured_at:
            lines.append(
                "Whole-book risk picture "
                + stamp_html(snap.captured_at, mode="rel", prefix="as of ")
                + "."
            )
    except Exception:
        pass

    links.append(
        _ask_chip(
            "Red-team my portfolio: what are the strongest arguments against my current book?",
            "Red-team · Ask",
        )
    )
    links.append(
        _ask_chip(
            "How would my portfolio fare in a macro shock — rates, FX, or a LatAm selloff?",
            "Macro stress · Ask",
        )
    )

    return _brief_shell(
        "The read",
        "Where the theses stand and how fresh the risk picture is — the two "
        "cards below carry the evidence.",
        lines,
        links,
    )


def _allocation_brief(db_path: Path, repo_root: Path) -> str:
    """The Allocation read: where the book stands and what is waiting on the
    owner, in three lines with doorways into the tiles that carry the detail."""
    lines: list[str] = []
    links: list[str] = [_jump_chip("allocation_recommendation", "What-if / Compare")]

    try:
        import portfolio_risk_snapshot_store as risk_store
        from allocation.concentration import classify_zone
        from portfolio_weights import read_materialized_weights

        weights = read_materialized_weights(repo_root)
        top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:1]
        snap = risk_store.read_latest_snapshot(db_path=db_path)
        bits: list[str] = []
        if top:
            t, w = top[0]
            za = classify_zone(w * 100.0)
            zone_txt = f" ({escape(za.zone.replace('_', ' '))})" if za is not None else ""
            bits.append(f"<strong>{escape(t)}</strong> leads at {w * 100.0:.1f}%{zone_txt}")
        if snap is not None and snap.current_drawdown_pct is not None:
            bits.append(f"book is {snap.current_drawdown_pct:.1f}% off its high")
        if bits:
            lines.append("Top of book: " + "; ".join(bits) + ".")
            links.append(_jump_chip("risk_budget", "Risk budget"))
    except Exception:
        pass

    try:
        import llm_artifact_store

        artifact = llm_artifact_store.read_current(
            ticker=None,
            purpose="incremental_dollar_recommendation",
            scope="portfolio",
            db_path=db_path,
        )
        if artifact is None:
            lines.append(
                "No current next-dollar recommendation — generate one from the "
                "Next dollar tile when cash is ready."
            )
        else:
            lines.append("A next-dollar recommendation is on file — act on it or regenerate.")
        links.append(_jump_chip("allocation_recommendation", "Next dollar"))
    except Exception:
        pass

    try:
        from alerts import ALERT_STATUS_PENDING, list_alerts

        n_pending = len(
            list_alerts(user_id=DEFAULT_USER_ID, status=ALERT_STATUS_PENDING, db_path=db_path)
        )
        if n_pending:
            lines.append(
                f'<a href="/feed">{n_pending} decision(s) waiting in the inbox</a> — '
                "each settles in one click."
            )
    except Exception:
        pass

    return _brief_shell(
        "The read",
        "Where the book stands and what is waiting on you — details live in the tiles.",
        lines,
        links,
    )


def _record_brief(db_path: Path) -> str:
    """The Record read: the last thing that entered the record and the name
    farthest from fair value, each a doorway into its tile."""
    lines: list[str] = []
    links: list[str] = []

    try:
        from user_state.ledger import list_recent_entries

        entries = list_recent_entries(user_id=DEFAULT_USER_ID, limit=1, db_path=db_path)
        if entries:
            e = entries[0]
            when = str(e.created_at)[:10]
            lines.append(
                f"Last on the record: <strong>{escape(e.entry_kind.replace('_', ' '))}</strong> "
                f"on {escape(e.ticker or 'the book')} ({escape(when)})."
            )
            links.append(_jump_chip("decisions", "Decisions"))
    except Exception:
        pass

    try:
        from pipeline.analytical_dashboard import build_analytical_dashboard

        dash = build_analytical_dashboard(
            db_path, sections={"trigger_ladder"}, list_types=("portfolio", "evaluation")
        )
        rows = [r for r in dash.trigger_ladder if r.over_under_pct is not None]
        if rows:
            top = max(rows, key=lambda r: abs(r.over_under_pct or 0.0))
            lines.append(
                f"Farthest from fair: <strong>{escape(top.ticker)}</strong> at "
                f"{(top.over_under_pct or 0.0) * 100.0:+.0f}% vs its DCF."
            )
            links.append(_jump_chip("triggers", "Valuation triggers"))
    except Exception:
        pass

    return _brief_shell(
        "The read",
        "What last entered the audit trail and where valuation is most stretched.",
        lines,
        links,
    )


def render_portfolio_allocation_panel(
    db_path: Path, repo_root: Path | None = None, *, user_id: str = DEFAULT_USER_ID
) -> str:
    """Portfolio → Allocation: where capital goes and how it's doing (P0.4b,
    PRD §7.4/§7.5), laid out per the D1 page model: the Band-1 read leads,
    then ONE dense tile grid — Next dollar (wide landing), Risk Budget /
    Posture / Positioning as tiles, Performance still deferred to an on-reveal
    HTMX fragment (B4a) so nothing waits on a tracker round-trip. The former
    What-if/Compare signpost section is deleted: its actions live on the Next
    dollar tile and the brief's chip jumps there."""
    from pipeline.allocation_recommendation_panel import (
        render_allocation_recommendation_section,
        render_portfolio_posture_section,
        render_risk_budget_section,
    )
    from pipeline.positioning_panel import render_positioning_panel

    root = repo_root or db_path.parent.parent

    sections: list[ConsoleSection] = [
        ("brief", "Read", lambda: _allocation_brief(db_path, root)),
        (
            "performance",
            "Performance",
            lambda: _lazy_section_placeholder("/api/panel/portfolio", "Performance"),
        ),
        (
            "allocation_recommendation",
            "Next dollar",
            lambda: render_allocation_recommendation_section(db_path, root),
        ),
        ("risk_budget", "Risk Budget", lambda: render_risk_budget_section(db_path, root)),
        ("posture", "Posture", lambda: render_portfolio_posture_section(db_path, root)),
        ("positioning", "Positioning", lambda: render_positioning_panel(db_path, root)),
    ]
    return _CONSOLE_CSS + render_console(
        "Allocation",
        sections,
        wrap_class="portfolio-allocation-console",
        nav_exclude=("brief",),
        grid=True,
        wide=("brief", "allocation_recommendation", "performance"),
    )


def render_portfolio_record_panel(db_path: Path, *, user_id: str = DEFAULT_USER_ID) -> str:
    """Portfolio → Record: the audit trail, on the D1 page model — the Band-1
    read leads, Decisions spans wide (it is the record), Memos and the
    Triggers ladder sit as side-by-side tiles."""
    from pipeline.advisor_memos_panel import render_advisor_memos_panel
    from pipeline.allocation_decisions_panel import render_allocation_decisions_panel

    sections: list[ConsoleSection] = [
        ("brief", "Read", lambda: _record_brief(db_path)),
        (
            "decisions",
            "Decisions",
            lambda: render_allocation_decisions_panel(db_path, user_id=user_id),
        ),
        ("memos", "Memos", lambda: render_advisor_memos_panel(db_path, user_id=user_id)),
        ("triggers", "Triggers", lambda: _render_triggers(db_path)),
    ]
    return _CONSOLE_CSS + render_console(
        "Record",
        sections,
        wrap_class="portfolio-record-console",
        nav_exclude=("brief",),
        grid=True,
        wide=("brief", "decisions"),
    )


def _render_triggers(db_path: Path) -> str:
    """The Triggers ladder (old ``holdings`` panel id) — the trigger-ladder
    section of the analytical dashboard, rendered through the same seam the
    ``/api/panel/holdings`` route uses so there is no second code path."""
    from pipeline.analytical_dashboard import build_analytical_dashboard
    from pipeline.analytical_dashboard_html import render_panel_fragment

    # The owner's thesis'd names live on the portfolio + evaluation lists;
    # watchlist rows are bulk-onboarded stubs — exactly the irrelevant data
    # he flagged (2026-07-14) — so the Record console scopes them out. The
    # standalone /api/panel/holdings route keeps the builder's default scope.
    dash = build_analytical_dashboard(
        db_path,
        sections={"trigger_ladder"},
        ticker=None,
        list_types=("portfolio", "evaluation"),
    )
    fragment = render_panel_fragment(dash, "holdings")
    return fragment or '<section class="panel"><p class="muted">No triggers.</p></section>'
