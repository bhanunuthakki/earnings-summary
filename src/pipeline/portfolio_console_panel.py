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
via the Work OS deep-link map, and any direct fetch / peek
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
from collections.abc import Callable
from html import escape
from pathlib import Path

from identity import DEFAULT_USER_ID
from pipeline.console_scaffold import ConsoleSection, render_console
from pipeline.portfolio_styles import console_css
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# The D1 tile grid + Band-1 brief. Tokens only (design_language §2): auto-fit
# tiles ≥460px so a wide desktop viewport gets 2-3 columns and a narrow one
# degrades to a single column without a media query; `csec-wide` spans the
# brief and the landing section across every column.
_CONSOLE_CSS = console_css()


# Health console (owner directive 2026-07-30): the read, then exactly TWO
# chip-tab cards — nothing below the fold. Chips (.k-chip-btn.k-chip-tab +
# the kit's .is-on active state, restyled 2026-08-02 to a 2px underline —
# never a filled pill) swap panes in place; each pane fetches its fragment on
# first activation, so the console shell paints instantly. Layout-only CSS —
# the chips/pills/sticky band are all the kit (.k-chip-tabs-sticky).
_HEALTH_CSS = console_css()

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
        f'<button type="button" class="k-chip k-chip-btn k-chip-tab{" is-on" if i == 0 else ""}" '
        f'data-hc-pane="hcp-{key}">{escape(label)}</button>'
        for i, (key, label) in enumerate(tabs)
    )
    panes = "".join(
        f'<div class="hc-pane" id="hcp-{key}"{"" if i == 0 else " hidden"} '
        f'data-src="/api/panel/portfolio_health?fragment={key}">'
        '<p class="cc-loading">Loading…</p></div>'
        for i, (key, _label) in enumerate(tabs)
    )
    # k-chip-tabs-sticky (ui/controls.py): the pane-switcher row pins below the
    # shell topbar too — each card's fetched pane (thesis rollup, drawdown/
    # crowding tables, …) can run well past one screen, and without this the
    # chip row that swaps panes scrolls away with it (owner directive
    # 2026-08-02).
    return (
        f'<article class="console-sec hc-card k-card k-card-section" id="csec-{escape(anchor)}">'
        '<header class="k-card-head"><div class="k-card-heading">'
        f'<h2 class="k-card-title hc-h">{escape(question)}</h2>'
        f'</div></header><div class="hc-tabs k-chip-tabs-sticky">{chips}</div>{panes}</article>'
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
        + '<section class="k-card k-card-section console-health-brief">'
        + '<header class="k-card-head"><div class="k-card-heading">'
        + '<h2 class="k-card-title">The read</h2></div></header>'
        + _health_brief(db_path)
        + "</section>"
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
        '<div class="console-brief" data-brief-title="'
        f'{escape(title)}"><p class="cb-sub">{escape(sub)}</p>'
        f"{body}{links_html}</div>"
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
    db_path: Path,
    repo_root: Path | None = None,
    *,
    user_id: str = DEFAULT_USER_ID,
    performance_renderer: Callable[[], str] | None = None,
) -> str:
    """Portfolio → Allocation: where capital goes and how it's doing (P0.4b,
    PRD §7.4/§7.5), laid out per the D1 page model: the Band-1 read leads,
    then ONE dense tile grid — Next dollar (wide landing), Risk Budget /
    Posture / Positioning as tiles. Performance renders in this on-demand route
    request so the Work OS never strands an HTMX placeholder. The former
    What-if/Compare signpost section is deleted: its actions live on the Next
    dollar tile and the brief's chip jumps there."""
    from pipeline.allocation_recommendation_panel import (
        render_allocation_recommendation_section,
        render_portfolio_posture_section,
        render_risk_budget_section,
    )
    from pipeline.portfolio_panel import render_portfolio_panel
    from pipeline.positioning_panel import render_positioning_panel

    root = repo_root or db_path.parent.parent
    render_performance = performance_renderer or (lambda: render_portfolio_panel(db_path=db_path))

    sections: list[ConsoleSection] = [
        ("brief", "Read", lambda: _allocation_brief(db_path, root)),
        (
            "performance",
            "Performance",
            render_performance,
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
