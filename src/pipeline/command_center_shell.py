"""Unified tabbed command-center shell.

One page, served at ``GET /`` by ``execution/comments_server.py``, that folds the
former three vertically-scrolling pages (``/``, ``/analytical``, ``/ticker/<t>``)
into a single dark-themed app with a horizontal tab bar.

Design — thin shell + lazy panels:

* **Overview is server-inlined** (instant first paint): the Research cockpit
  (one attention-ranked row per holding — thesis health · valuation · events,
  master build P1.2) followed by the tier-coverage strip. The IR-KPI +
  maintenance action blocks moved to the Governance theme's Actions tab
  (``/api/panel/actions``) — Overview is for reading, not operating.
* **Every other tab lazy-loads its HTML on first activation** via
  ``GET /api/panel/<name>`` (the head/foot-less fragments from PR 5), then caches
  it in the DOM. Inline ``<script>`` blocks inside a fragment (e.g. the budget
  Save-button wiring) are re-executed after injection — ``innerHTML`` alone does
  not run them.
* **Four primary sections + a System icon (UX9b over the PR2 IA)**: the top
  bar's nav carries Home / Companies / Ask / Portfolio; System demoted to a
  top-right icon button beside ⌘K and ⚙ Settings (same ``data-theme-target``
  contract, so its sub-tabs render exactly as before once opened). Only the
  active section's sub-tab row renders below the bar (single-tab sections
  render none). A Ctrl/Cmd+K — or Ctrl+Space — command palette jumps to
  tickers, tabs, actions, open journal notes, and saved views; anything else
  you type hands off to the Ask tab as a question. Legacy hashes (and the
  section names themselves) remap — see ``_LEGACY_PANEL_REDIRECTS``.
* **A shared ✎ Notes drawer in the top bar**: quick-add (kind · ticker ·
  body → ``POST /api/notes``) + the open-notes list, lazy-fetched from
  ``GET /api/panel/notes_drawer`` on every open; when the Holding tab is
  active its current ticker scopes the drawer (pre-filled quick-add + that
  name's notes and recent alerts). The full lifecycle stays on Journal.
* **The Holding drill-down is search-driven** (UX9c): a type-ahead combobox in
  the holding fragment's utility band (``cc-combo``) drives the same
  ``#holding=<T>`` hash the old ``cc-picker`` dropdown did; the shell re-fetches
  ``endpoint?ticker=<T>`` server-side on the resulting hashchange. While a
  holding is open the Companies sub-row is suppressed for a clean reading view.
* **Deep-linkable** via ``location.hash`` — ``#holdings``, ``#holding=NU`` —
  with ``hashchange`` driving back/forward; killed-panel hashes redirect.
* **Peek primitive (UX9)**: one shared quick-look popover + scrim. Links opt
  in with ``data-peek-url`` (href untouched — middle-click still navigates);
  ``/source/<doc_id>`` links peek their ``fragment=1`` variant automatically;
  ticker links grow a hover mini-card from ``/api/peek/ticker/<T>``. Report
  iframes are separate documents and stay drill-through.
* **The Ask dock is shell chrome (Ask v5)**: ``pipeline.ask_dock`` renders
  once into the body, OUTSIDE ``.cc-panels``, so the conversational dock
  persists across every tab switch. Three states — min pill / floating card /
  split column (``body[data-ask-split="1"]`` reflows the panels beside it) —
  persisted in localStorage; the thread tail survives reloads via
  sessionStorage.

The standalone ``/analytical`` and ``/ticker/<t>`` pages remain as working
deep-link targets (zero rewrite); this shell is purely additive.

All HTML is server-side f-strings + vanilla JS (no template engine), matching the
rest of the dashboard. ``SHELL_CSS`` / ``SHELL_JS`` are plain string constants so
their braces pass through untouched; only the small assembly bits interpolate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from dashboard.inbox import INBOX_CSS, INBOX_JS
from dashboard.upcoming import UPCOMING_CSS
from pipeline.ask_dock import render_ask_dock
from pipeline.research_cockpit import CockpitRow
from pipeline.source_viewers import VIEWER_CONTENT_CSS
from ui.time import stamp_html
from ui.tokens import FAVICON_LINK, palette_css

# Four primary sections + System-as-icon (UX9b over the PR2 five-section IA):
# the top bar's nav carries Home · Companies · Ask · Portfolio; System (the
# diagnostics surfaces) demoted to a top-right icon button in the utility
# cluster — it stays a full section (sub-tab row, deep links, palette entry)
# once opened. The active section's sub-tabs render in ONE row below the bar
# (sections with a single sub-tab suppress the row entirely, so Home and Ask
# have zero secondary chrome). "Governance" demoted into System alongside the
# settings drawer; "Research" split into Home (the cockpit) / Companies
# (per-name work) / Ask (the NL+ViewSpec surface).
#
# Sub-tab entries keep the original shape — (panel_id, label, endpoint,
# is_picker, picker_required) — and PANEL IDS ARE UNCHANGED, so every
# existing deep-link (#holding=NU, #explore, #validation …) still resolves;
# the JS variable names keep saying "theme" for the same reason (the
# attribute contract data-theme-target / data-cc-theme is load-bearing).
_SubTab = tuple[str, str, str | None, bool, bool]
_THEMES: tuple[tuple[str, str, tuple[_SubTab, ...]], ...] = (
    (
        "home",
        "Home",
        (("overview", "Overview", None, False, False),),
    ),
    (
        "companies",
        "Companies",
        (
            ("holding", "Holding", "/api/panel/holding", True, True),
            # New-name discovery queue (P5.4): screened/adjacency candidates
            # with why-surfaced evidence; approval gates every eval build.
            ("discovery", "Discovery", "/api/panel/discovery", False, False),
            # The analyst journal's lifecycle home (P4.5): list / filter /
            # resolve / reclassify / supersede over analyst_notes.
            ("journal", "Journal", "/api/panel/journal", False, False),
        ),
    ),
    (
        "ask",
        "Ask",
        # On-the-fly slice-and-dice (P5.1/P5.2): NL question -> validated
        # ViewSpec; becomes the conversational Ask thread in PR5.
        (("explore", "Ask", "/api/panel/explore", False, False),),
    ),
    (
        "portfolio",
        "Portfolio",
        (
            ("portfolio", "Performance", "/api/panel/portfolio", False, False),
            # UX round 4 — the portfolio-level reading layer surfaced out of
            # Performance's bottom strip: thesis rollup + sector exposure, the
            # next-dollar allocation distribution, the cross-portfolio lens memo.
            ("portfolio_synthesis", "Synthesis", "/api/panel/portfolio_synthesis", False, False),
            # P2.2 — the allocation-decisions record: sizing audit + the merged
            # decisions timeline (thesis ledger + sizing intents + decision
            # notes). The standalone Thesis Ledger tab folded into it.
            ("decisions_record", "Decisions", "/api/panel/decisions_record", False, False),
            # P2.3 — advisor memos: next-dollar + swap-discipline runs, the
            # deterministic swap screen, and the durable memo record.
            ("advisor_memos", "Memos", "/api/panel/advisor_memos", False, False),
            ("holdings", "Triggers", "/api/panel/holdings", False, False),
        ),
    ),
    (
        "system",
        "System",
        (
            # Per-ticker section coverage (P4.2): which report sections are
            # filled for which names — the visible counterpart of the
            # hide-don't-stub policy (reports no longer show cold stubs).
            ("section_coverage", "Coverage", "/api/panel/section_coverage", False, False),
            ("ir_coverage", "IR Docs", "/api/panel/ir_coverage", False, False),
            ("source_calls", "Data Cache", "/api/panel/source_calls", False, False),
            # Whole-book data-quality state over validation_issues (P3.4) —
            # previously reachable only per-ticker inside workspace reports.
            ("validation", "Validation", "/api/panel/validation", False, False),
            # "was X, now Y" over the supersede chains (P3.5), linking both
            # filings into the /source/<doc_id> viewers.
            ("restatements", "Restatements", "/api/panel/restatements", False, False),
        ),
    ),
)

# Old tab deep-links keep working: killed panels 302 (client-side) onto their
# new homes. Mirrored verbatim into SHELL_JS's REDIRECTS map — keep in sync.
# budget/actions became settings-drawer sections in P3.4: their hashes land on
# System and the JS auto-opens the drawer (see DRAWER_OPENERS in SHELL_JS).
# The four section names also alias to their landing panel (PR2) so #home,
# #companies, #ask, #system are stable shareable anchors.
_LEGACY_PANEL_REDIRECTS: dict[str, str] = {
    "prereads": "overview",
    "insiders": "overview",
    "predictions": "overview",
    "decisions": "decisions_record",
    "thesis_ledger": "decisions_record",
    "budget": "ir_coverage",
    "actions": "ir_coverage",
    "home": "overview",
    "companies": "holding",
    "ask": "explore",
    "system": "section_coverage",
}


def render_overview_panel(
    rows_by_list: dict[str, list[CockpitRow]],
    coverage: dict[str, dict[str, int]] | None,
    inbox_html: str | None = None,
    upcoming_html: str | None = None,
) -> str:
    """The inlined Home tab: the Research cockpit (the landing answer to
    "which holding needs my attention today?") with the tier-coverage strip
    below it, and — when provided — the unified Inbox in a right-hand rail
    (UX redesign PR3: what changed, beside what you hold), topped by the
    compact upcoming-earnings strip (``upcoming_html``, the surviving piece
    of the retired /digest page). Reuses the existing public seams so there
    is no second code path for any of this content. (The Ask dock is no
    longer panel content — it renders once in the shell chrome, see
    ``render_shell``, so it persists across tab switches.)"""
    from pipeline.analytical_dashboard_html import render_tier_coverage_strip
    from pipeline.research_cockpit import render_research_cockpit

    main = render_research_cockpit(rows_by_list) + render_tier_coverage_strip(coverage or {})
    if not inbox_html:
        return main
    # The badge carries the "new since you last looked" count; INBOX_JS (one
    # IIFE, embedded with the rail it drives) fills it from the per-surface
    # localStorage mark and wires the cards' hover ✓/✕ quick actions.
    rail = (
        '<aside class="cc-home-rail">'
        f"{upcoming_html or ''}"
        '<div class="cc-home-rail-head">'
        '<h2>Inbox<span class="ix-badge" data-ix-badge="home" hidden></span></h2>'
        '<span class="cc-home-rail-links"><a href="/feed">full feed</a></span></div>'
        f"{inbox_html}"
        f"<script>{INBOX_JS}</script>"
        "</aside>"
    )
    return f'<div class="cc-home-grid"><div class="cc-home-main">{main}</div>{rail}</div>'


def render_shell(
    *,
    overview_html: str,
    generated_at: datetime | None = None,
    themes: tuple[tuple[str, str, tuple[_SubTab, ...]], ...] = _THEMES,
) -> str:
    """Render the full command-center document.

    ``overview_html`` is the pre-built Overview panel (see ``render_overview_panel``)
    inlined for first paint; every other sub-tab is an empty placeholder that
    lazy-loads from its ``/api/panel/<name>`` endpoint on first activation.
    The persistent Ask dock mounts once here, outside the panel container.
    """
    stamp = stamp_html(generated_at or datetime.now(UTC), css="cc-stamp", prefix="updated ")
    flat_tabs = tuple(t for _tid, _tlabel, subs in themes for t in subs)
    return "".join(
        [
            _DOC_HEAD,
            # One sticky top bar: brand + the four primary sections + utility
            # cluster (⌘K · System icon · ✎ Notes · ⚙ Settings). No stacked tab
            # tiers — the active section's sub-row (if it has more than one
            # sub-tab) is the only other chrome.
            f'<div class="cc-topbar"><div class="cc-brand">Command Center</div>'
            f"{_render_section_nav(themes)}"
            f'<nav class="cc-links">'
            f'<a href="/feed">Alert feed</a>'
            f"</nav>"
            f'<button class="cc-palette-btn" id="cc-palette-open" type="button" '
            f'title="Jump to a ticker, tab, note, or saved view (Ctrl+K / Ctrl+Space)">⌘K</button>'
            f"{_render_system_button(themes)}"
            f'<button class="cc-notes-btn" id="cc-notes-toggle" type="button" '
            f'title="Quick note + open notes (scoped to the open holding)">✎</button>'
            f'<button class="cc-settings-btn" id="cc-settings-toggle" type="button" '
            f'title="Budgets · ticker settings · maintenance">⚙ Settings</button>'
            f"{stamp}</div>",
            _render_subnav_rows(themes),
            '<main class="cc-panels">',
            _render_panels(flat_tabs, overview_html),
            "</main>",
            _SETTINGS_DRAWER_HTML,
            _NOTES_DRAWER_HTML,
            _PALETTE_HTML,
            _PEEK_HTML,
            # The persistent Ask dock (Ask v5): shell chrome, not panel
            # content — outside .cc-panels so it survives every tab switch.
            render_ask_dock(),
            f"<script>{SHELL_JS}</script>",
            _DOC_FOOT,
        ]
    )


# The settings drawer (P3.4): admin stops being a tab. Each <details> section
# lazy-loads an existing panel fragment on first open through the same
# injectHtml used for tabs, so the budget Save buttons and the maintenance
# blocks' SSE wiring keep working unchanged inside the drawer.
# All sections ship COLLAPSED; SHELL_JS restores each section's last
# open/closed state from localStorage (key: cc-drawer-sec:<endpoint>).
_SETTINGS_DRAWER_HTML = (
    '<div class="cc-drawer-scrim" id="cc-drawer-scrim" hidden></div>'
    '<aside class="cc-drawer" id="cc-drawer" hidden aria-label="Settings">'
    '<div class="cc-drawer-head"><span>Settings &amp; maintenance</span>'
    '<button class="cc-drawer-close" id="cc-drawer-close" type="button" '
    'aria-label="Close">&times;</button></div>'
    '<div class="cc-drawer-body">'
    '<details class="cc-drawer-sec" data-endpoint="/api/panel/budget" data-loaded="0">'
    "<summary>LLM budgets</summary>"
    '<div class="cc-drawer-sec-body"><div class="cc-loading">Loading…</div></div></details>'
    '<details class="cc-drawer-sec" data-endpoint="/api/panel/ticker_settings" data-loaded="0">'
    "<summary>Ticker settings</summary>"
    '<div class="cc-drawer-sec-body"><div class="cc-loading">Loading…</div></div></details>'
    '<details class="cc-drawer-sec" data-endpoint="/api/panel/actions" data-loaded="0">'
    "<summary>Maintenance actions &amp; job streams</summary>"
    '<div class="cc-drawer-sec-body"><div class="cc-loading">Loading…</div></div></details>'
    "</div></aside>"
)


# The shared ✎ Notes drawer (UX9b): the same right-drawer pattern as Settings,
# but its body is ONE lazy fragment — /api/panel/notes_drawer — re-fetched on
# every open (notes change while you work) with the Holding tab's current
# ticker as scope when one is open. Quick-add wiring lives in the fragment; it
# calls window.ccReloadNotesDrawer (exposed by SHELL_JS) after a save.
_NOTES_DRAWER_HTML = (
    '<div class="cc-drawer-scrim" id="cc-notes-scrim" hidden></div>'
    '<aside class="cc-drawer cc-notes-drawer" id="cc-notes-drawer" hidden aria-label="Notes">'
    '<div class="cc-drawer-head"><span>Notes</span>'
    '<button class="cc-drawer-close" id="cc-notes-close" type="button" '
    'aria-label="Close">&times;</button></div>'
    '<div class="cc-drawer-body" id="cc-notes-drawer-body">'
    '<div class="cc-loading">Loading…</div></div>'
    "</aside>"
)

# Sections that live as utility icons beside ⌘K/⚙ instead of in the primary
# nav (UX9b). They keep full section behavior — data-theme-target, sub-tab
# row, palette entry, #<name> alias — only the button's placement and skin
# change.
_UTILITY_SECTIONS: frozenset[str] = frozenset({"system"})


def _render_section_nav(themes: tuple[tuple[str, str, tuple[_SubTab, ...]], ...]) -> str:
    """The primary section buttons, inline in the top bar — every section
    except the utility-icon ones (System). The attribute contract
    (``data-theme-target``) is unchanged from the theme era — the JS keys off
    it, only the visual placement moved."""
    out = ['<nav class="cc-topnav" role="tablist">']
    for tid, tlabel, _subs in themes:
        if tid in _UTILITY_SECTIONS:
            continue
        out.append(
            f'<button class="cc-tab cc-theme-tab" type="button" role="tab" '
            f'data-theme-target="{escape(tid)}">{escape(tlabel)}</button>'
        )
    out.append("</nav>")
    return "".join(out)


def _render_system_button(themes: tuple[tuple[str, str, tuple[_SubTab, ...]], ...]) -> str:
    """System as a top-right icon button (UX9b). Carries ``cc-theme-tab`` +
    ``data-theme-target`` so the activation JS treats it exactly like a nav
    section button; ``data-pal-label`` keeps its palette row readable (the
    button's visible text is just the glyph)."""
    for tid, tlabel, subs in themes:
        if tid not in _UTILITY_SECTIONS:
            continue
        sub_labels = " · ".join(label for _pid, label, _ep, _pk, _rq in subs)
        return (
            f'<button class="cc-theme-tab cc-system-btn" type="button" role="tab" '
            f'data-theme-target="{escape(tid)}" data-pal-label="{escape(tlabel)}" '
            f'aria-label="{escape(tlabel)}" title="{escape(f"{tlabel} · {sub_labels}", quote=True)}"'
            f">▦</button>"
        )
    return ""


def _render_subnav_rows(themes: tuple[tuple[str, str, tuple[_SubTab, ...]], ...]) -> str:
    """One sub-tab row per section, hidden unless its section is active.
    Sections with a single sub-tab mark the row ``data-single="1"`` — it stays
    in the DOM (the activation JS derives the active section from the sub-tab's
    ``data-cc-theme``) but CSS suppresses it, so Home and Ask carry no second
    chrome row. Sub-tab buttons keep the exact ``cc-tab``/``data-tab-target``
    contract the activation JS has always used."""
    out: list[str] = []
    for tid, _tlabel, subs in themes:
        single = ' data-single="1"' if len(subs) <= 1 else ""
        out.append(f'<nav class="cc-tabs cc-subtabs" data-cc-theme="{escape(tid)}"{single} hidden>')
        for pid, label, _endpoint, _picker, _required in subs:
            out.append(
                f'<button class="cc-tab" type="button" role="tab" '
                f'data-tab-target="{escape(pid)}" data-cc-theme="{escape(tid)}">'
                f"{escape(label)}</button>"
            )
        out.append("</nav>")
    return "".join(out)


# Ctrl/Cmd+K command palette (PR2): one input over sections, sub-tabs, tickers
# (lazy from /api/tickers on first open), and a few global actions. Pure
# overlay — no routing of its own, every result lands on an existing hash or
# URL.
_PALETTE_HTML = (
    '<div class="cc-palette-scrim" id="cc-palette-scrim" hidden></div>'
    '<div class="cc-palette" id="cc-palette" hidden role="dialog" aria-label="Command palette">'
    '<input id="cc-palette-input" type="text" '
    'placeholder="Jump to a ticker, tab, note, or view — or just ask…" '
    'autocomplete="off" spellcheck="false">'
    '<ul id="cc-palette-list" class="cc-palette-list"></ul>'
    "</div>"
)

# Peek / quick-look primitive (UX9): one positioned popover + scrim the whole
# shell shares, plus the ticker hover mini-card. Any shell link can opt in
# with ``data-peek-url`` (its href stays the real destination for middle-click
# and new-tab); ``/source/<doc_id>`` links peek automatically. The body is
# fetched lazily as an HTML fragment and injected through the same script
# re-execution path the lazy panels use.
_PEEK_HTML = (
    '<div class="cc-peek-scrim" id="cc-peek-scrim" hidden></div>'
    '<div class="cc-peek" id="cc-peek" hidden role="dialog" aria-label="Quick look">'
    '<div class="cc-peek-head">'
    '<span class="cc-peek-title" id="cc-peek-title"></span>'
    '<a class="cc-peek-openfull" id="cc-peek-openfull" href="#" hidden>open full ↗</a>'
    '<button class="cc-peek-close" id="cc-peek-close" type="button" '
    'aria-label="Close">&times;</button></div>'
    '<div class="cc-peek-body" id="cc-peek-body"></div>'
    "</div>"
    '<div class="cc-hovercard" id="cc-hovercard" hidden></div>'
)


def _render_panels(
    tabs: tuple[tuple[str, str, str | None, bool, bool], ...], overview_html: str
) -> str:
    out: list[str] = []
    for pid, _label, endpoint, picker, _required in tabs:
        if pid == "overview":
            out.append(
                f'<section class="cc-panel" data-panel="{escape(pid)}" data-loaded="1">'
                f'<div class="cc-panel-body">{overview_html}</div></section>'
            )
            continue
        ep = f' data-endpoint="{escape(endpoint)}"' if endpoint else ""
        # ``data-picker`` now means "ticker-scoped panel" — the shell passes the
        # hash ticker straight to loadBody. The picker UI itself moved into the
        # Holding fragment as a search combobox (UX9c), so there is no longer a
        # ``cc-picker`` <select> in the shell chrome. ``required`` is unused now:
        # the no-ticker fragment renders the combobox band, not a shell stub.
        pk = ' data-picker="1"' if picker else ""
        out.append(
            f'<section class="cc-panel" data-panel="{escape(pid)}"{ep}{pk} '
            f'data-loaded="0" data-current-ticker="" hidden>'
            '<div class="cc-panel-body"><div class="cc-loading">Loading…</div></div>'
            "</section>"
        )
    return "".join(out)


# Palette comes from the shared token source (src/ui/tokens.py); the alias
# block below maps this surface's legacy var names onto the canonical ones so
# its rules — and any later-inlined comment/chat CSS — resolve unchanged.
SHELL_CSS = (
    palette_css("dark")
    + """
:root {
  --panel: var(--surface);
  --panel-alt: var(--paper);
  --bg-card: var(--surface);
  --bg-elev: var(--paper);
  --row-hover: var(--paper);
  --ink: var(--fg);
  --ink-muted: var(--muted);
  --fg-muted: var(--muted);
  --link: var(--accent);
  --font-mono: var(--mono);
  --font-body: var(--sans);
}
* { box-sizing: border-box; }
body { margin: 0; padding: 0; font-family: var(--font-body); background: var(--bg);
  color: var(--ink); line-height: 1.5; font-size: var(--fs-body); }
a { color: var(--link); transition: color var(--transition); }
button { transition: color var(--transition), border-color var(--transition),
  background var(--transition); }

/* Standardized motion: overlays slide/fade IN over the standard timing
   (exit stays instant — the [hidden] toggle can't animate display:none). */
@keyframes cc-slide-in-right { from { transform: translateX(18px); opacity: 0; }
  to { transform: none; opacity: 1; } }
@keyframes cc-fade-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes cc-pop-in { from { transform: translateX(-50%) scale(0.985); opacity: 0; }
  to { transform: translateX(-50%) scale(1); opacity: 1; } }

/* One sticky top bar: brand + section nav + utility links. The only other
   chrome is the active section's single sub-row (suppressed entirely for
   single-tab sections), so content starts ~90px from the top instead of ~200. */
.cc-topbar { display: flex; align-items: center; gap: var(--sp-1);
  padding: 10px 24px; border-bottom: 1px solid var(--border);
  position: sticky; top: 0; background: var(--bg); z-index: 30; }
.cc-brand { font-size: var(--fs-title); font-weight: 700; letter-spacing: 0.2px;
  margin-right: 18px; white-space: nowrap; }
.cc-topnav { display: flex; gap: 2px; margin-right: auto; overflow-x: auto; }
.cc-topnav .cc-tab { padding: 8px 13px; font-size: var(--fs-section); }
.cc-links { display: flex; gap: 14px; margin: 0 14px 0 16px; }
.cc-links a { color: var(--muted); text-decoration: none; font-size: var(--fs-body); }
.cc-links a:hover { color: var(--link); }
.cc-stamp { color: var(--muted); font-size: var(--fs-caption); font-family: var(--font-mono);
  margin-left: 12px; white-space: nowrap; }
.cc-tabs { display: flex; gap: 2px; padding: 0 16px; border-bottom: 1px solid var(--border);
  overflow-x: auto; background: var(--bg); }
/* display:flex above beats the [hidden] UA rule — restate it. Without this
   every section's sub-row rendered at once (the old "four stacked menus"). */
.cc-tabs[hidden] { display: none; }
.cc-subtabs[data-single="1"] { display: none; }
.cc-tab { background: transparent; border: none; border-bottom: 2px solid transparent;
  color: var(--muted); padding: 10px 16px; font-size: var(--fs-body); font-weight: 600;
  cursor: pointer; white-space: nowrap; font-family: var(--font-body); }
.cc-tab:hover { color: var(--ink); }
.cc-tab.active { color: var(--ink); border-bottom-color: var(--accent); }
.cc-topnav .cc-tab { border-bottom-width: 2px; }

.cc-panels { padding: 22px 24px 64px; max-width: 1600px; margin: 0 auto; }

/* Home: cockpit + the unified Inbox rail (PR3) */
.cc-home-grid { display: grid; grid-template-columns: minmax(0, 1fr) 400px; gap: var(--sp-5);
  align-items: start; }
@media (max-width: 1180px) { .cc-home-grid { grid-template-columns: 1fr; } }
.cc-home-rail { position: sticky; top: 64px; }
.cc-home-rail-head { display: flex; align-items: baseline; justify-content: space-between;
  margin-bottom: var(--sp-2); }
.cc-home-rail-head h2 { font-size: var(--fs-section); text-transform: uppercase;
  letter-spacing: 0.5px; margin: 0; }
.cc-home-rail-links { font-size: var(--fs-caption); color: var(--muted); }
.cc-home-rail-links a { color: var(--muted); text-decoration: none; }
.cc-home-rail-links a:hover { color: var(--link); }
.cc-home-rail .ix-stream { max-height: calc(100vh - 140px); overflow-y: auto;
  padding-right: 2px; }
.cc-panel[hidden] { display: none; }
.cc-loading, .cc-empty { color: var(--muted); font-size: var(--fs-body); padding: 24px 4px; }
/* Skeleton shimmer under the loading text (PR8) — feedback that the panel
   is alive, without a spinner library. */
.cc-loading::after { content: ''; display: block; height: 10px; margin-top: 12px;
  border-radius: var(--radius-full); max-width: 420px;
  background: linear-gradient(90deg, var(--surface) 25%, var(--paper) 50%, var(--surface) 75%);
  background-size: 200% 100%; animation: cc-shimmer 1.2s ease-in-out infinite; }
@keyframes cc-shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

/* The Holding tab's ticker picker is now a search combobox inside the holding
   fragment's utility band (UX9c, .cc-combo) — no shell-chrome <select>. */

/* ============================================================
   Analytical / command-center panel vocabulary (dark)
   Lifted from analytical_dashboard_html + ticker_command_center so the
   lazy fragments + the inlined overview render identically here.
   ============================================================ */
h1 { font-size: var(--fs-display); margin: 0 0 8px; font-weight: 600; }
h2 { font-size: var(--fs-title); margin: 0 0 6px; font-weight: 600; }
h3 { font-size: var(--fs-section); margin: 0; font-weight: 600; }
/* Panels are elevation, not boxes: surface-on-bg with the one radius. */
.panel { margin-bottom: 28px; background: var(--panel);
  border-radius: var(--radius); padding: 18px 20px; }
.panel .sub { color: var(--muted); font-size: var(--fs-caption); margin: 0 0 16px; }
.muted { color: var(--muted); }
table { width: 100%; border-collapse: collapse; font-size: var(--fs-body);
  font-variant-numeric: tabular-nums; }
th { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border);
  font-size: var(--fs-caption); text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
td { padding: 8px 10px; border-bottom: 1px solid var(--hairline); vertical-align: top; }
tbody tr:hover td { background: rgba(255,255,255,0.025); }
td.num { text-align: right; }
td.muted { color: var(--muted-2); }
td.pos { color: var(--ok); }
td.neg { color: var(--bad); }
.ticker-link { color: var(--ink); text-decoration: none; font-weight: 600; }
.ticker-link:hover { color: var(--link); }
tr.tone-sell { background: rgba(248, 113, 113, 0.06); }
tr.tone-trim { background: rgba(251, 191, 36, 0.04); }
tr.tone-init { background: rgba(74, 222, 128, 0.06); }
tr.tx-buy { background: rgba(74, 222, 128, 0.04); }
tr.tx-sell { background: rgba(248, 113, 113, 0.02); }
td.trigger-cell { font-family: var(--font-mono); font-size: var(--fs-caption);
  text-transform: uppercase; }
tr.tone-sell .trigger-cell { color: var(--bad); }
tr.tone-trim .trigger-cell { color: var(--warn); }
tr.tone-init .trigger-cell { color: var(--ok); }
td.signal-strong { color: var(--ok); font-weight: 600; }
td.signal-medium { color: var(--warn); }
td.signal-weak { color: var(--muted); }
/* Inline code tracks the surrounding step (mono renders larger than sans at
   equal px — 0.93em is the optical correction, not an importance level). */
code { font-family: var(--font-mono); font-size: 0.93em; color: var(--fg-soft); }
.cli-hint { font-family: var(--font-mono); font-size: var(--fs-caption); padding: 10px 12px;
  background: var(--paper); border-radius: var(--radius); color: var(--ok);
  overflow-x: auto; margin: 6px 0 0; }
.panel-h3 { font-size: var(--fs-section); margin: 18px 0 8px; font-weight: 600;
  color: var(--ink); }
/* Synthesis */
.synthesis-panel { border-left: 3px solid var(--ok); }
.synthesis-body { font-size: var(--fs-section); line-height: 1.65; }
.synthesis-body h2, .synthesis-body h3, .synthesis-body h4 { color: var(--ink); margin-top: 1.2em; margin-bottom: 6px; }
.synthesis-body h2 { font-size: var(--fs-title); }
.synthesis-body h3 { font-size: var(--fs-section); }
.synthesis-body h4 { font-size: var(--fs-body); color: var(--ok); }
.synthesis-body strong { color: var(--ink); }
.synthesis-body code { background: var(--paper); padding: 1px 5px; border-radius: 3px; }
.synthesis-body ul { padding-left: 22px; }
.synthesis-body li { margin-bottom: 4px; }
.synthesis-body hr { border: none; border-top: 1px solid var(--border); margin: 16px 0; }
/* Reread grid + cards */
.reread-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: var(--sp-3); margin-top: 8px; }
.reread-card { background: var(--panel); border-radius: var(--radius); padding: 12px 14px; }
.reread-card summary { cursor: pointer; list-style: none; display: flex; justify-content: space-between; align-items: baseline; font-size: var(--fs-title); font-weight: 600; }
.reread-card summary::-webkit-details-marker { display: none; }
.reread-card summary::before { content: '▸ '; color: var(--muted); font-family: var(--font-mono); }
.reread-card[open] summary::before { content: '▾ '; }
.reread-stamp { color: var(--muted); font-size: var(--fs-caption); font-family: var(--font-mono); font-weight: 400; }
.reread-body { font-size: var(--fs-body); line-height: 1.55; margin-top: 10px; }
.reread-body h2, .reread-body h3, .reread-body h4 { color: var(--ink); margin: 10px 0 4px; }
.reread-body h2 { font-size: var(--fs-section); color: var(--ok); }
.reread-body h3 { font-size: var(--fs-body); }
.reread-body strong { color: var(--ink); }
.reread-body ul { padding-left: 18px; }
.reread-body hr { border: none; border-top: 1px solid var(--border); margin: 10px 0; }
/* KPI strip + decisions calibration */
.kpi-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin: 8px 0 12px; }
.kpi-card { background: var(--paper); border-radius: var(--radius); padding: 10px 12px; text-align: center; }
.kpi-card.tone-good { border-left: 3px solid var(--ok); }
.kpi-card.tone-warn { border-left: 3px solid var(--warn); }
.kpi-card.tone-bad { border-left: 3px solid var(--bad); }
.kpi-card.tone-muted { border-left: 3px solid var(--muted-2); }
.kpi-card.pos .kpi-value { color: var(--ok); }
.kpi-card.neg .kpi-value { color: var(--bad); }
.kpi-label { font-size: var(--fs-caption); color: var(--muted); letter-spacing: 0.06em;
  text-transform: uppercase; }
.kpi-value { font-size: var(--fs-display); font-weight: 700; margin: 2px 0; color: var(--ink);
  font-variant-numeric: tabular-nums; }
.kpi-sub { font-size: var(--fs-micro); color: var(--muted); font-family: var(--font-mono); }
.calib-strip { display: flex; flex-direction: column; gap: 6px; margin: 8px 0 18px; }
.calib-row { display: grid; grid-template-columns: 80px 1fr 110px; gap: 12px; align-items: center; font-size: var(--fs-caption); }
.calib-label { color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.calib-bar { background: var(--paper); border-radius: var(--radius-full); height: 14px; overflow: hidden; }
.calib-fill { background: linear-gradient(90deg, #f87171 0%, #fbbf24 50%, #4ade80 100%); height: 100%; }
.calib-value { font-family: var(--font-mono); color: var(--fg-soft); text-align: right; }
.decisions-table td.outcome-correct { color: var(--ok); }
.decisions-table td.outcome-wrong { color: var(--bad); }
.decisions-table td.outcome-mixed { color: var(--warn); }
.decisions-table td.outcome-pending { color: var(--muted); }
/* LLM budget */
.budget-table td code { font-family: var(--font-mono); font-size: 0.93em; color: var(--ink); background: transparent; padding: 0; }
.burn-cell { width: 200px; padding: 6px 10px; }
.burn-bar { width: 100%; height: 8px; background: var(--paper); border-radius: var(--radius-full); overflow: hidden; }
.burn-fill { height: 100%; transition: width 0.2s; }
.burn-ok { background: var(--ok); }
.burn-warn { background: var(--warn); }
.burn-over { background: var(--bad); }
.budget-footer { margin-top: 12px; font-size: var(--fs-body); color: var(--fg-soft); }
.budget-footer strong { color: var(--ink); }
.budget-table input, .budget-table select { background: var(--panel-alt); color: var(--ink);
  border: 1px solid var(--border); border-radius: var(--radius); padding: 4px 6px;
  font-size: var(--fs-body); }
.budget-save { background: var(--accent); color: #0d1117; border: none; padding: 5px 12px;
  border-radius: var(--radius); font-weight: 600; font-size: var(--fs-body); cursor: pointer; }
/* Tier coverage strip */
.tier-strip { background: var(--panel); border-radius: var(--radius);
  padding: 10px 14px; margin-bottom: 22px; font-size: var(--fs-body); display: flex;
  align-items: center; flex-wrap: wrap; gap: var(--sp-1); }
.tier-strip-label { color: var(--muted); font-size: var(--fs-caption);
  text-transform: uppercase; letter-spacing: 0.06em; margin-right: 8px; }
.tier-chip { font-family: var(--font-mono); font-size: var(--fs-caption); padding: 2px 6px;
  border-radius: 3px; cursor: help; }
.tier-ok { color: var(--ok); }
.tier-stale { color: var(--warn); }
.tier-stale-count { color: var(--bad); font-weight: 600; }
.tier-backfill { color: var(--muted); }
.tier-backfill .tier-stale-count { color: var(--muted); font-weight: 400; }
.tier-empty { color: var(--muted-2); }

/* ============================================================
   Overview status tables + action blocks (re-themed dark)
   ============================================================ */
.list-section { margin-bottom: var(--sp-5); }
.list-section h2 { font-size: var(--fs-section); text-transform: uppercase; letter-spacing: 0.5px; margin: 18px 0 10px; }
.list-section h2 .count { font-weight: 400; color: var(--muted); margin-left: 4px; }
.list-section table { background: var(--panel); border-radius: var(--radius); overflow: hidden; }
.list-section th { padding-top: 10px; }
td.ticker { font-family: var(--font-mono); font-weight: 600; }
td.ticker a { color: var(--ink); text-decoration: none; }
td.ticker a:hover { color: var(--link); }
.qa-yes, .qa-no { font-size: var(--fs-micro); padding: 1px 5px; border-radius: 3px; letter-spacing: 0.3px; }
.qa-yes { background: #14361f; color: #6ee7a0; }
.qa-no { background: #3a1f1f; color: #f0a0a0; }
.comments-open { color: var(--warn); font-weight: 500; }
.breach-badge { display: inline-block; padding: 2px 8px; border-radius: 3px;
  font-size: var(--fs-micro); color: white; text-transform: uppercase;
  letter-spacing: 0.05em; font-weight: 600; }
.open-link { color: var(--link); text-decoration: none; }
.open-link:hover { text-decoration: underline; }
.empty { color: var(--muted); font-style: italic; padding: 12px; }

/* ============================================================
   Holding drill-down tab (ticker_command_center sections + embedded report)
   ============================================================ */
.cc-holding-links { font-size: var(--fs-body); display: inline-flex; gap: 14px;
  align-items: center; }
.cc-holding-links a { color: var(--link); text-decoration: none; white-space: nowrap; }
.cc-holding-links a:hover { text-decoration: underline; }
.badges { display: inline-flex; gap: 4px; margin-left: 8px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: var(--fs-micro);
  text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; background: var(--border); }
.badge.b-ok { background: #14532d; color: var(--ok); }
.badge.b-warn { background: #422006; color: var(--warn); }
.badge.b-bad { background: #450a0a; color: var(--bad); }
.badge.b-muted { background: var(--border); color: var(--muted); }
.fresh-strip { display: flex; gap: 10px; margin-bottom: 22px; flex-wrap: wrap; }
.fresh-cell { background: var(--panel); border-radius: var(--radius);
  padding: 8px 14px; flex: 1; min-width: 140px; }
.fresh-label { font-size: var(--fs-micro); text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); }
.fresh-val { font-size: var(--fs-section); font-variant-numeric: tabular-nums; }
.ok-dot { color: var(--ok); }
.tcc-refresh, .tcc-refresh + .tcc-refresh { background: var(--accent); color: #0d1117; border: none;
  padding: 6px 12px; border-radius: var(--radius); font-weight: 600; font-size: var(--fs-body);
  cursor: pointer; margin-right: 4px; }
.artifact-table code { background: transparent; padding: 0; }
.cc-report-embed { padding-bottom: 8px; }
.cc-report-frame { width: 100%; height: calc(100vh - 220px); min-height: 560px;
  border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg);
  margin-top: 6px; }

/* Report split (master build P1.3): the embedded report with the analyst's
   open notes + recent alerts in a rail beside it. The rail scrolls
   independently, capped to the report frame's height; below 1100px the rail
   stacks under the report. */
.cc-holding-split { display: grid; grid-template-columns: minmax(0, 1fr) 360px;
  gap: 18px; align-items: start; }
.cc-holding-main { min-width: 0; }
.cc-holding-rail { display: flex; flex-direction: column; gap: 18px;
  max-height: calc(100vh - 150px); overflow-y: auto; position: sticky; top: 88px; }
.cc-rail-panel { margin-bottom: 0; padding: 14px 16px; }
.cc-rail-panel h2 { font-size: var(--fs-section); }
@media (max-width: 1100px) {
  .cc-holding-split { grid-template-columns: 1fr; }
  .cc-holding-rail { position: static; max-height: none; overflow-y: visible; }
}

/* Rail note cards — one per open analyst_notes row, color-keyed by kind.
   Accent stays interactive-only: the question kind keys off fg-soft. */
.rail-note { background: var(--paper);
  border-left: 3px solid var(--muted); border-radius: var(--radius); padding: 8px 10px;
  margin-bottom: 8px; }
.rail-note.nk-question { border-left-color: var(--fg-soft); }
.rail-note.nk-decision { border-left-color: var(--ok); }
.rail-note.nk-watch { border-left-color: var(--warn); }
.rail-note.nk-assumption { border-left-color: var(--fg-soft); }
.rail-note.nk-observation { border-left-color: var(--muted); }
.rail-note-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.rail-note-kind { font-size: var(--fs-micro); text-transform: uppercase; font-weight: 600;
  letter-spacing: 0.05em; color: var(--muted); }
.rail-note.nk-question .rail-note-kind { color: var(--fg-soft); }
.rail-note.nk-decision .rail-note-kind { color: var(--ok); }
.rail-note.nk-watch .rail-note-kind { color: var(--warn); }
.rail-note-when { font-family: var(--font-mono); font-size: var(--fs-micro); color: var(--muted); }
.rail-note-body { font-size: var(--fs-body); line-height: 1.5; margin: 4px 0; color: var(--fg-soft);
  overflow-wrap: anywhere; }
.rail-note-meta { font-size: var(--fs-micro); color: var(--muted); overflow-wrap: anywhere; }
.rail-note-meta code { font-size: var(--fs-micro); }

/* Alert cards + evidence drawer inside the rail — same class vocabulary the
   feed renders (src/dashboard/_card.py + evidence_drawer.py); rules
   lifted from src/dashboard/_styles.py and compacted for the 360px column.
   Palette vars are shared via ui.tokens, so only layout is duplicated. */
.alert-card { background: var(--surface); border-radius: var(--radius);
  padding: 10px 12px; margin-bottom: 10px; }
.alert-card-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 6px; }
.ticker-badge { display: inline-flex; align-items: center; padding: 2px 7px;
  border: 1px solid var(--border-2); background: var(--paper); color: var(--ink);
  border-radius: var(--radius); font-family: var(--font-mono); font-weight: 700;
  font-size: var(--fs-caption); letter-spacing: 0.05em; }
.trigger-badge, .status-badge { display: inline-flex; align-items: center; padding: 1px 6px;
  border: 1px solid var(--border-2); border-radius: var(--radius); color: var(--fg-soft);
  font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.05em;
  text-transform: uppercase; }
.status-pending { color: var(--warn); border-color: var(--warn); }
.status-approved { color: var(--ok); border-color: var(--ok); }
.status-dismissed { color: var(--muted); border-color: var(--muted); }
.status-expired { color: var(--muted-2); border-color: var(--muted-2); }
.fired-at { color: var(--muted); font-family: var(--font-mono); font-size: var(--fs-micro);
  margin-left: auto; }
.alert-memo { margin: 4px 0 8px; padding: 7px 9px; background: var(--paper);
  border-left: 3px solid var(--border-2); border-radius: 0 var(--radius) var(--radius) 0;
  font-size: var(--fs-body); color: var(--fg-soft); }
.alert-memo-pending { color: var(--muted); font-style: italic; }
.queued-actions { margin-top: 8px; }
.queued-actions h4 { font-size: var(--fs-caption); font-weight: 600; margin: 0 0 5px;
  color: var(--muted); letter-spacing: 0.05em; text-transform: uppercase; }
.queued-action { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-start;
  padding: 6px 8px; background: var(--paper);
  border-radius: var(--radius); margin-bottom: 5px; }
.qa-kind { display: inline-flex; align-items: center; padding: 1px 5px;
  border: 1px solid var(--border-2); background: var(--surface); color: var(--fg-soft);
  border-radius: 3px; font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.05em;
  text-transform: uppercase; }
.qa-body { flex: 1; min-width: 140px; color: var(--fg-soft); font-size: var(--fs-caption); }
.qa-actions { display: flex; gap: 6px; align-items: center; font-family: var(--font-mono);
  font-size: var(--fs-micro); }
.qa-actions a { display: inline-flex; padding: 2px 7px; border: 1px solid var(--border-2);
  border-radius: var(--radius); color: var(--accent); text-decoration: none; }
.qa-actions .qa-cli { color: var(--muted); overflow-wrap: anywhere; }
.qa-status-applied { color: var(--ok); }
.qa-status-cancelled { color: var(--muted); }
.evidence-drawer { margin-top: 8px; background: var(--paper);
  border-radius: var(--radius); }
.evidence-drawer > summary { cursor: pointer; padding: 6px 10px; color: var(--muted);
  font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.05em;
  text-transform: uppercase; user-select: none; }
.evidence-drawer[open] > summary { border-bottom: 1px solid var(--hairline); }
.evidence-body { padding: 8px 10px; }
.evidence-section { margin-bottom: 8px; }
.evidence-section:last-child { margin-bottom: 0; }
.evidence-section-title { font-size: var(--fs-micro); text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--muted); margin-bottom: 3px; }
.evidence-summary-text { color: var(--fg-soft); font-size: var(--fs-body); }
.evidence-malformed { padding: 8px; background: rgba(240, 138, 138, 0.08);
  border: 1px solid var(--bad); border-radius: var(--radius); color: var(--bad);
  margin-bottom: 8px; font-size: var(--fs-caption); }
.evidence-citations-table { width: 100%; border-collapse: collapse; font-size: var(--fs-caption); }
.evidence-citations-table th { text-align: left; padding: 3px 6px 3px 0; color: var(--muted);
  font-weight: 500; font-size: var(--fs-micro); text-transform: uppercase; letter-spacing: 0.05em;
  border-bottom: 1px solid var(--hairline); font-family: var(--font-body); }
.evidence-citations-table td { padding: 4px 6px 4px 0; vertical-align: top;
  border-bottom: 1px solid var(--hairline); }
.evidence-citations-table tr:last-child td { border-bottom: none; }
.cite-kind { color: var(--fg-soft); white-space: nowrap; }
.cite-locator { color: var(--muted); word-break: break-all; font-family: var(--font-mono);
  font-size: var(--fs-micro); }
.cite-excerpt { color: var(--fg-soft); }
.cite-prov { color: var(--muted); font-family: var(--font-mono); font-size: var(--fs-micro); }
.prov-source { color: var(--accent); }
.evidence-raw { margin-top: 4px; }
.evidence-raw > summary { cursor: pointer; color: var(--muted);
  font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.05em; }
.evidence-raw-pre { margin: 5px 0 0; padding: 7px 9px; background: var(--bg);
  border-radius: var(--radius); font-family: var(--font-mono);
  font-size: var(--fs-micro); color: var(--fg-soft); white-space: pre-wrap; word-break: break-all;
  max-height: 260px; overflow: auto; }

/* Three-theme nav (master build P1.1): primary theme row + per-theme sub-tab
   rows. Sub-tab rows reuse .cc-tab styling at a smaller size. */
.cc-theme-row { padding-top: 2px; }
.cc-theme-tab { font-size: var(--fs-section); font-weight: 600; letter-spacing: 0.01em; }
.cc-subtabs { z-index: 19; }
.cc-subtabs .cc-tab { font-size: var(--fs-body); }

/* Settings drawer (P3.4): admin-as-drawer instead of admin-as-tab. */
.cc-settings-btn { background: var(--panel-alt); color: var(--ink); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 5px 12px; font-size: var(--fs-body); font-weight: 600;
  cursor: pointer; font-family: var(--font-body); margin-right: 14px; margin-left: 6px; }
.cc-settings-btn:hover { border-color: var(--accent); color: var(--accent); }

/* System demoted to a utility icon (UX9b): same activation contract as a nav
   section button, skinned like its ⌘K / ⚙ neighbours. */
.cc-system-btn { background: transparent; border: 1px solid var(--border); color: var(--muted);
  border-radius: var(--radius); padding: 5px 10px; font-size: var(--fs-body); cursor: pointer;
  font-family: var(--font-body); margin-left: 6px; line-height: 1.2; }
.cc-system-btn:hover, .cc-system-btn.active { border-color: var(--accent); color: var(--accent); }

/* Shared ✎ Notes drawer trigger (UX9b). */
.cc-notes-btn { background: transparent; border: 1px solid var(--border); color: var(--muted);
  border-radius: var(--radius); padding: 5px 10px; font-size: var(--fs-body); cursor: pointer;
  font-family: var(--font-body); margin-left: 6px; line-height: 1.2; }
.cc-notes-btn:hover { border-color: var(--accent); color: var(--accent); }
.cc-notes-drawer { width: min(560px, 94vw); }
.cc-drawer-scrim { position: fixed; inset: 0; background: rgba(0,0,0,0.45); z-index: 38;
  animation: cc-fade-in var(--transition); }
.cc-drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(780px, 94vw);
  background: var(--bg); border-left: 1px solid var(--border); z-index: 39;
  display: flex; flex-direction: column; box-shadow: -12px 0 32px rgba(0,0,0,0.35);
  animation: cc-slide-in-right var(--transition); }
.cc-drawer[hidden], .cc-drawer-scrim[hidden] { display: none; }
.cc-drawer-head { display: flex; justify-content: space-between; align-items: center;
  padding: 14px 18px; border-bottom: 1px solid var(--border); font-weight: 700; }
.cc-drawer-close { background: transparent; border: none; color: var(--muted);
  font-size: 20px; cursor: pointer; line-height: 1; padding: 2px 6px; }
.cc-drawer-close:hover { color: var(--ink); }
.cc-drawer-body { overflow-y: auto; padding: 14px 18px 40px; }
.cc-drawer-sec { margin-bottom: 14px; border-radius: var(--radius);
  background: var(--panel); }
.cc-drawer-sec > summary { cursor: pointer; list-style: none; padding: 11px 14px;
  font-size: var(--fs-section); font-weight: 600; }
.cc-drawer-sec > summary::-webkit-details-marker { display: none; }
.cc-drawer-sec > summary::before { content: '▸ '; color: var(--muted); font-family: var(--font-mono); }
.cc-drawer-sec[open] > summary::before { content: '▾ '; }
.cc-drawer-sec-body { padding: 0 14px 12px; }
.cc-drawer-sec-body .panel { margin-bottom: 0; border: none; padding: 0; background: transparent; }

/* Command palette (Ctrl/Cmd+K) */
.cc-palette-btn { background: transparent; border: 1px solid var(--border); color: var(--muted);
  border-radius: var(--radius); padding: 5px 9px; font-size: var(--fs-caption); cursor: pointer;
  font-family: var(--font-mono); }
.cc-palette-btn:hover { border-color: var(--accent); color: var(--accent); }
.cc-palette-scrim { position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 48;
  animation: cc-fade-in var(--transition); }
.cc-palette { position: fixed; top: 14vh; left: 50%; transform: translateX(-50%);
  width: min(560px, 92vw); background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); z-index: 49; box-shadow: 0 18px 48px rgba(0,0,0,0.5);
  overflow: hidden; animation: cc-pop-in var(--transition); }
.cc-palette[hidden], .cc-palette-scrim[hidden] { display: none; }
.cc-palette input { width: 100%; box-sizing: border-box; background: transparent;
  border: none; border-bottom: 1px solid var(--border); color: var(--ink);
  padding: 13px 16px; font-size: var(--fs-section); font-family: var(--font-body); outline: none; }
.cc-palette-list { list-style: none; margin: 0; padding: 6px 0; max-height: 46vh;
  overflow-y: auto; }
.cc-palette-list li { display: flex; justify-content: space-between; gap: 12px;
  padding: 8px 16px; font-size: var(--fs-body); cursor: pointer; color: var(--ink);
  transition: background var(--transition); }
.cc-palette-list li.sel, .cc-palette-list li:hover { background: var(--paper); }
.cc-palette-list li .cc-pal-hint { color: var(--muted); font-size: var(--fs-micro);
  font-family: var(--font-mono); }
.cc-palette-list li.cc-pal-none { color: var(--muted); cursor: default; }

/* ============================================================
   Peek / quick-look (UX9): one shared popover instead of the
   drill-throughs — source excerpts, alert review, memos.
   z-order: ask dock (35) < drawer (39) < peek (45) < hover card (46)
   < palette (49).
   ============================================================ */
.cc-peek-scrim { position: fixed; inset: 0; background: rgba(0,0,0,0.35); z-index: 44; }
.cc-peek { position: fixed; z-index: 45; width: min(680px, 92vw);
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: 0 18px 48px rgba(0,0,0,0.5); display: flex; flex-direction: column;
  overflow: hidden; }
.cc-peek[hidden], .cc-peek-scrim[hidden], .cc-hovercard[hidden] { display: none; }
.cc-peek-head { display: flex; align-items: center; gap: 12px; padding: 9px 14px;
  border-bottom: 1px solid var(--border); flex: none; }
.cc-peek-title { font-weight: 700; font-size: var(--fs-body); margin-right: auto;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cc-peek-openfull { font-size: var(--fs-caption); color: var(--muted); text-decoration: none;
  white-space: nowrap; }
.cc-peek-openfull:hover { color: var(--accent); }
.cc-peek-close { background: transparent; border: none; color: var(--muted);
  font-size: 19px; cursor: pointer; line-height: 1; padding: 2px 6px; }
.cc-peek-close:hover { color: var(--ink); }
.cc-peek-body { overflow-y: auto; padding: 12px 14px; min-height: 60px; }
.cc-peek-body .alert-card:last-child { margin-bottom: 0; }
.cc-peek-foot { margin-top: 8px; font-size: var(--fs-caption); }
.cc-peek-foot a { color: var(--muted); text-decoration: none; }
.cc-peek-foot a:hover { color: var(--accent); }
.cc-peek-memo-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
.cc-peek-memo-head h2 { font-size: var(--fs-section); margin: 0; }
/* JS-applied stand-in for the full page's :target highlight (#L<n>). */
.cc-peek-target { background: rgba(245, 198, 106, 0.14);
  outline: 1px solid var(--warn); border-radius: 4px; }

/* Ticker hover mini-card */
.cc-hovercard { position: fixed; z-index: 46; width: 300px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 12px;
  box-shadow: 0 10px 28px rgba(0,0,0,0.45); font-size: var(--fs-body); }
.cc-mini-head { display: flex; align-items: center; gap: 8px; }
.cc-mini-ticker { font-family: var(--font-mono); font-weight: 700; font-size: var(--fs-section); }
.cc-mini-name { color: var(--muted); font-size: var(--fs-caption); margin: 1px 0 6px; }
.cc-mini-row { display: flex; justify-content: space-between; gap: 10px; padding: 2px 0; }
.cc-mini-row > span { color: var(--muted); }
.cc-mini-row b { font-weight: 600; font-variant-numeric: tabular-nums; }
.cc-mini-open { margin-top: 7px; padding-top: 6px; border-top: 1px solid var(--border);
  font-size: var(--fs-caption); }
.cc-mini-open a { color: var(--accent); text-decoration: none; }
"""
    + VIEWER_CONTENT_CSS
    + INBOX_CSS
    + "\n"
    + UPCOMING_CSS
).strip()


# Tab switching + lazy panel loading + ticker-scoped panels + hash deep-linking.
# Vanilla JS, no build step. Plain string (not an f-string / .format template) so its braces
# are literal.
SHELL_JS = r"""
(function () {
  var TICKERS = null;  // cached /api/tickers payload
  // Sub-tab buttons only — theme buttons share .cc-tab styling but carry
  // data-theme-target instead of data-tab-target.
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.cc-tab[data-tab-target]'));
  var themeTabs = Array.prototype.slice.call(document.querySelectorAll('.cc-theme-tab'));
  var subnavs = Array.prototype.slice.call(document.querySelectorAll('.cc-subtabs'));
  var panels = Array.prototype.slice.call(document.querySelectorAll('.cc-panel'));
  var lastPanelByTheme = {};  // remember each theme's last-active sub-tab

  // Killed nav surfaces (P1.1) + the section-name aliases (PR2) — legacy and
  // section hashes land on their panel homes. Kept in sync with
  // _LEGACY_PANEL_REDIRECTS in the Python module.
  var REDIRECTS = {
    prereads: 'overview',
    insiders: 'overview',
    predictions: 'overview',
    decisions: 'decisions_record',
    thesis_ledger: 'decisions_record',
    budget: 'ir_coverage',
    actions: 'ir_coverage',
    home: 'overview',
    companies: 'holding',
    ask: 'explore',
    system: 'section_coverage'
  };
  // Legacy panels that became settings-drawer sections (P3.4): their old
  // deep-links also auto-open the drawer after landing on Governance.
  var DRAWER_OPENERS = { budget: 1, actions: 1 };

  function firstPanelOfTheme(tid) {
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].getAttribute('data-cc-theme') === tid) {
        return tabs[i].getAttribute('data-tab-target');
      }
    }
    return null;
  }

  function panelById(id) {
    for (var i = 0; i < panels.length; i++) {
      if (panels[i].getAttribute('data-panel') === id) return panels[i];
    }
    return null;
  }

  // innerHTML does NOT execute <script> tags — re-create them so panel-local
  // wiring (e.g. the budget Save buttons) runs after a lazy fragment is injected.
  function injectHtml(container, html) {
    container.innerHTML = html;
    var scripts = container.querySelectorAll('script');
    for (var i = 0; i < scripts.length; i++) {
      var old = scripts[i];
      var s = document.createElement('script');
      if (old.src) s.src = old.src; else s.textContent = old.textContent;
      old.parentNode.replaceChild(s, old);
    }
  }

  function fetchTickers() {
    if (TICKERS) return Promise.resolve(TICKERS);
    return fetch('/api/tickers').then(function (r) { return r.json(); })
      .then(function (j) { TICKERS = (j && j.tickers) || []; return TICKERS; })
      .catch(function () { TICKERS = []; return TICKERS; });
  }

  function loadBody(panel, ticker) {
    var ep = panel.getAttribute('data-endpoint');
    if (!ep) return;
    var body = panel.querySelector('.cc-panel-body');
    var url = ep + (ticker ? ('?ticker=' + encodeURIComponent(ticker)) : '');
    body.innerHTML = '<div class="cc-loading">Loading…</div>';
    fetch(url).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function (html) {
      injectHtml(body, html);
      panel.setAttribute('data-current-ticker', ticker || '');
    }).catch(function (e) {
      body.innerHTML = '<div class="cc-empty">Failed to load (' + e.message + ').</div>';
    });
  }

  function activate(panelId, ticker) {
    var panel = panelById(panelId);
    if (!panel) { panel = panelById('overview'); panelId = 'overview'; ticker = null; }
    panels.forEach(function (p) { p.hidden = (p !== panel); });
    var activeTheme = null;
    tabs.forEach(function (t) {
      var on = t.getAttribute('data-tab-target') === panelId;
      t.classList.toggle('active', on);
      if (on) activeTheme = t.getAttribute('data-cc-theme');
    });
    if (activeTheme) {
      themeTabs.forEach(function (t) {
        t.classList.toggle('active', t.getAttribute('data-theme-target') === activeTheme);
      });
      // While a specific holding is open (Holding panel + a ticker), suppress
      // the Companies sub-row for a clean reading view (UX9c) — the band's
      // combobox switches holdings, and the row returns on the no-ticker state
      // / Discovery / Journal. Other sections show their row as usual.
      var holdingOpen = (panelId === 'holding' && !!ticker);
      subnavs.forEach(function (n) {
        var theme = n.getAttribute('data-cc-theme');
        n.hidden = (theme !== activeTheme) || (holdingOpen && theme === 'companies');
      });
      lastPanelByTheme[activeTheme] = panelId;
    }
    // ``data-picker`` panels are ticker-scoped: pass the hash ticker straight to
    // loadBody (the in-fragment combobox supplies it via the #holding=<T> hash).
    var isPicker = panel.getAttribute('data-picker') === '1';
    var loaded = panel.getAttribute('data-loaded') === '1';
    if (isPicker) {
      var cur = panel.getAttribute('data-current-ticker') || '';
      if (!loaded || cur !== (ticker || '')) {
        loadBody(panel, ticker || null);
        panel.setAttribute('data-loaded', '1');
      }
    } else if (!loaded && panel.getAttribute('data-endpoint')) {
      loadBody(panel, null);
      panel.setAttribute('data-loaded', '1');
    }
  }

  function parseHash() {
    var h = (location.hash || '').replace(/^#/, '');
    if (!h) return { panel: 'overview', ticker: null };
    var eq = h.indexOf('=');
    if (eq === -1) return { panel: h, ticker: null };
    return { panel: h.substring(0, eq), ticker: decodeURIComponent(h.substring(eq + 1)) };
  }

  function onHashChange() {
    closePeek();
    closeHover();
    var p = parseHash();
    var wasDrawerPanel = !!DRAWER_OPENERS[p.panel];
    if (REDIRECTS[p.panel]) { p = { panel: REDIRECTS[p.panel], ticker: null }; }
    activate(p.panel, p.ticker);
    if (wasDrawerPanel) openDrawer();
  }

  // ----- Settings drawer (P3.4) -----
  var drawer = document.getElementById('cc-drawer');
  var drawerScrim = document.getElementById('cc-drawer-scrim');

  function loadDrawerSection(sec) {
    if (!sec || sec.getAttribute('data-loaded') === '1') return;
    var ep = sec.getAttribute('data-endpoint');
    var body = sec.querySelector('.cc-drawer-sec-body');
    if (!ep || !body) return;
    sec.setAttribute('data-loaded', '1');
    fetch(ep).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function (html) {
      injectHtml(body, html);
    }).catch(function (e) {
      sec.setAttribute('data-loaded', '0');
      body.innerHTML = '<div class="cc-empty">Failed to load (' + e.message + ').</div>';
    });
  }

  function openDrawer() {
    if (!drawer) return;
    closeNotesDrawer();  // one right-drawer at a time
    drawer.hidden = false;
    if (drawerScrim) drawerScrim.hidden = false;
    var secs = drawer.querySelectorAll('.cc-drawer-sec[open]');
    for (var i = 0; i < secs.length; i++) loadDrawerSection(secs[i]);
  }

  function closeDrawer() {
    if (drawer) drawer.hidden = true;
    if (drawerScrim) drawerScrim.hidden = true;
  }

  var settingsBtn = document.getElementById('cc-settings-toggle');
  if (settingsBtn) settingsBtn.addEventListener('click', function () {
    if (drawer && drawer.hidden) openDrawer(); else closeDrawer();
  });
  var drawerClose = document.getElementById('cc-drawer-close');
  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  if (drawerScrim) drawerScrim.addEventListener('click', closeDrawer);

  // ----- Shared ✎ Notes drawer (UX9b) -----
  // One lazy fragment (/api/panel/notes_drawer), re-fetched on EVERY open so
  // the list is always current; the Holding tab's selection scopes it. The
  // quick-add form inside the fragment calls window.ccReloadNotesDrawer after
  // a successful POST /api/notes so the list refreshes in place.
  var notesDrawer = document.getElementById('cc-notes-drawer');
  var notesScrim = document.getElementById('cc-notes-scrim');
  var notesBody = document.getElementById('cc-notes-drawer-body');

  function holdingTicker() {
    // The drawer's ticker scope: the Holding panel's current selection, but
    // only while the user is actually ON the Holding tab.
    var p = panelById('holding');
    if (!p || p.hidden) return '';
    return p.getAttribute('data-current-ticker') || '';
  }

  function loadNotesDrawer() {
    if (!notesBody) return;
    var t = holdingTicker();
    notesBody.innerHTML = '<div class="cc-loading">Loading…</div>';
    fetch('/api/panel/notes_drawer' + (t ? '?ticker=' + encodeURIComponent(t) : ''))
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      }).then(function (html) {
        injectHtml(notesBody, html);
      }).catch(function (e) {
        notesBody.innerHTML = '<div class="cc-empty">Failed to load (' + e.message + ').</div>';
      });
  }
  window.ccReloadNotesDrawer = loadNotesDrawer;

  function openNotesDrawer() {
    if (!notesDrawer) return;
    closeDrawer();
    closePalette();
    notesDrawer.hidden = false;
    if (notesScrim) notesScrim.hidden = false;
    loadNotesDrawer();
  }

  function closeNotesDrawer() {
    if (notesDrawer) notesDrawer.hidden = true;
    if (notesScrim) notesScrim.hidden = true;
  }

  var notesBtn = document.getElementById('cc-notes-toggle');
  if (notesBtn) notesBtn.addEventListener('click', function () {
    if (notesDrawer && notesDrawer.hidden) openNotesDrawer(); else closeNotesDrawer();
  });
  var notesClose = document.getElementById('cc-notes-close');
  if (notesClose) notesClose.addEventListener('click', closeNotesDrawer);
  if (notesScrim) notesScrim.addEventListener('click', closeNotesDrawer);

  // Any ✎ button inside an injected fragment (e.g. the Holding header's)
  // opens the SAME shared drawer — delegated so lazily-injected panels count.
  document.addEventListener('click', function (ev) {
    if (!ev.target.closest) return;
    var b = ev.target.closest('[data-cc-notes-open]');
    if (!b) return;
    ev.preventDefault();
    openNotesDrawer();
  });

  // ----- Command palette (Ctrl/Cmd+K, PR2) -----
  // One input over sections, sub-tabs, tickers (lazy from /api/tickers), and
  // a few global actions. Every result lands on an existing hash or URL.
  var pal = document.getElementById('cc-palette');
  var palScrim = document.getElementById('cc-palette-scrim');
  var palInput = document.getElementById('cc-palette-input');
  var palList = document.getElementById('cc-palette-list');
  var palItems = [];
  var palMatches = [];
  var palSel = 0;

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function goHash(id) { return function () { location.hash = '#' + id; }; }
  function goUrl(url) { return function () { location.href = url; }; }
  function goAsk(q) {
    // Hand the typed query to the Ask panel: stash it, jump to #explore,
    // and poke any already-loaded panel (it consumes the stash at wire-up
    // when the tab loads lazily — see consumePaletteQuery in explore_panel).
    return function () {
      try { sessionStorage.setItem('cc-ask-q', q); } catch (e) {}
      location.hash = '#explore';
      window.dispatchEvent(new Event('cc-ask-q'));
    };
  }
  function goView(id) {
    // Saved-view palette pick (UX9b): same stash-and-jump handoff as goAsk,
    // but for a saved view id — the Ask/explore panel loads + runs its chip
    // (see consumePaletteView in explore_panel).
    return function () {
      try { sessionStorage.setItem('cc-view-id', String(id)); } catch (e) {}
      location.hash = '#explore';
      window.dispatchEvent(new Event('cc-view-id'));
    };
  }
  function notePalLabel(body) {
    var flat = String(body || '').replace(/\s+/g, ' ').trim();
    if (flat.length > 64) flat = flat.slice(0, 63) + '…';
    return '✎ ' + flat;
  }

  function palStatic() {
    var items = [];
    themeTabs.forEach(function (t) {
      var tid = t.getAttribute('data-theme-target');
      var first = firstPanelOfTheme(tid);
      // Icon-skinned section buttons (System) carry their readable name in
      // data-pal-label — the visible text is just a glyph.
      var lab = t.getAttribute('data-pal-label') || t.textContent;
      if (first) items.push({ label: lab, hint: 'section', run: goHash(first) });
    });
    tabs.forEach(function (t) {
      items.push({
        label: t.textContent,
        hint: t.getAttribute('data-cc-theme'),
        run: goHash(t.getAttribute('data-tab-target'))
      });
    });
    items.push({ label: 'Settings & maintenance', hint: 'drawer', run: openDrawer });
    items.push({ label: 'Alert feed', hint: 'page', run: goUrl('/feed') });
    items.push({ label: 'Export CIO workbook', hint: 'download', run: goUrl('/export/cio') });
    return items;
  }

  function renderPalette(q) {
    var ql = (q || '').trim().toLowerCase();
    palMatches = [];
    for (var i = 0; i < palItems.length; i++) {
      var lab = palItems[i].label.toLowerCase();
      var score = !ql ? 1 : (lab.indexOf(ql) === 0 ? 3 : (lab.indexOf(ql) !== -1 ? 2 : 0));
      if (score) palMatches.push({ s: score, it: palItems[i] });
    }
    palMatches.sort(function (a, b) { return b.s - a.s; });
    palMatches = palMatches.slice(0, 12);
    if (ql.length >= 3) {
      // Anything you can type is also a question: the last entry hands the
      // raw query to the Ask tab (data view or researched answer).
      palMatches.push({ s: 0, it: { label: 'Ask: “' + q.trim() + '”', hint: 'ask', run: goAsk(q.trim()) } });
    }
    if (palSel >= palMatches.length) palSel = palMatches.length ? palMatches.length - 1 : 0;
    var html = '';
    for (var j = 0; j < palMatches.length; j++) {
      html += '<li class="' + (j === palSel ? 'sel' : '') + '" data-idx="' + j + '">'
        + '<span>' + escHtml(palMatches[j].it.label) + '</span>'
        + '<span class="cc-pal-hint">' + escHtml(palMatches[j].it.hint) + '</span></li>';
    }
    palList.innerHTML = html || '<li class="cc-pal-none">No matches.</li>';
  }

  function openPalette() {
    if (!pal) return;
    closeDrawer();
    closeNotesDrawer();
    pal.hidden = false;
    if (palScrim) palScrim.hidden = false;
    palInput.value = '';
    palSel = 0;
    palItems = palStatic();
    renderPalette('');
    fetchTickers().then(function (list) {
      list.forEach(function (t) {
        palItems.push({
          label: t.ticker + (t.name ? ' · ' + t.name : ''),
          hint: 'ticker',
          run: goHash('holding=' + encodeURIComponent(t.ticker))
        });
      });
      renderPalette(palInput.value);
    });
    // Grown corpus (UX9b): open journal notes land on the Journal lifecycle
    // tab; saved views hand off to the Ask/explore builder. Both fetched fresh
    // per open (they change while you work) and best-effort — a failed fetch
    // just leaves that slice out of the corpus.
    fetch('/api/notes?status=open').then(function (r) { return r.json(); })
      .then(function (j) {
        ((j && j.notes) || []).forEach(function (n) {
          palItems.push({
            label: notePalLabel(n.body),
            hint: 'note' + (n.ticker ? ' · ' + n.ticker : ''),
            run: goHash('journal')
          });
        });
        renderPalette(palInput.value);
      }).catch(function () {});
    fetch('/api/views').then(function (r) { return r.json(); })
      .then(function (j) {
        ((j && j.views) || []).forEach(function (v) {
          palItems.push({ label: '▤ ' + v.name, hint: 'saved view', run: goView(v.id) });
        });
        renderPalette(palInput.value);
      }).catch(function () {});
    setTimeout(function () { palInput.focus(); }, 0);
  }

  function closePalette() {
    if (pal) pal.hidden = true;
    if (palScrim) palScrim.hidden = true;
  }

  function runPalSelection() {
    if (!palMatches.length) return;
    var it = palMatches[palSel].it;
    closePalette();
    it.run();
  }

  if (palInput) {
    palInput.addEventListener('input', function () { palSel = 0; renderPalette(palInput.value); });
    palInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'ArrowDown') { ev.preventDefault(); palSel = Math.min(palSel + 1, palMatches.length - 1); renderPalette(palInput.value); }
      else if (ev.key === 'ArrowUp') { ev.preventDefault(); palSel = Math.max(palSel - 1, 0); renderPalette(palInput.value); }
      else if (ev.key === 'Enter') { ev.preventDefault(); runPalSelection(); }
    });
  }
  if (palList) palList.addEventListener('click', function (ev) {
    var li = ev.target.closest('li[data-idx]');
    if (!li) return;
    palSel = parseInt(li.getAttribute('data-idx'), 10) || 0;
    runPalSelection();
  });
  if (palScrim) palScrim.addEventListener('click', closePalette);
  var palBtn = document.getElementById('cc-palette-open');
  if (palBtn) palBtn.addEventListener('click', openPalette);

  // ----- Peek / quick-look (UX9) -----
  // One shared popover for the shell's drill-throughs: any link carrying
  // data-peek-url opens its fragment in-context on a plain left click, and
  // /source/<doc_id> links peek automatically (fragment=1 variant). The href
  // is never rewritten — middle-click / ctrl-click / open-in-new-tab keep the
  // real destination. Report iframes are separate documents, so in-report
  // links are untouched by these document-level listeners.
  var peek = document.getElementById('cc-peek');
  var peekScrim = document.getElementById('cc-peek-scrim');
  var peekBody = document.getElementById('cc-peek-body');
  var peekTitle = document.getElementById('cc-peek-title');
  var peekFull = document.getElementById('cc-peek-openfull');
  var peekSeq = 0;       // stale-response guard across rapid open/close
  var peekFragUrl = null;  // current fragment URL (re-fetched after approve/dismiss)

  function positionPeek(anchor) {
    var w = Math.min(680, Math.round(window.innerWidth * 0.92));
    var left = Math.round((window.innerWidth - w) / 2);
    var top = Math.round(window.innerHeight * 0.08);
    if (anchor && anchor.getBoundingClientRect) {
      var r = anchor.getBoundingClientRect();
      left = Math.max(12, Math.min(Math.round(r.left), window.innerWidth - w - 12));
      // Anchor below the trigger when it sits in the upper half; otherwise the
      // near-top default keeps tall fragments readable.
      if (r.bottom < window.innerHeight * 0.5) top = Math.round(r.bottom + 8);
    }
    peek.style.left = left + 'px';
    peek.style.top = top + 'px';
    peek.style.maxHeight = Math.max(240, window.innerHeight - top - 24) + 'px';
  }

  function loadPeek(fragUrl, anchorId) {
    var seq = ++peekSeq;
    peekFragUrl = fragUrl;
    peekBody.innerHTML = '<div class="cc-loading">Loading…</div>';
    fetch(fragUrl).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function (html) {
      if (seq !== peekSeq) return;
      injectHtml(peekBody, html);
      peekBody.scrollTop = 0;
      if (anchorId && /^[A-Za-z][\w-]*$/.test(anchorId)) {
        var el = peekBody.querySelector('#' + anchorId);
        if (el) {
          el.classList.add('cc-peek-target');
          el.scrollIntoView({ block: 'center' });
        }
      }
    }).catch(function (e) {
      if (seq !== peekSeq) return;
      peekBody.innerHTML = '<div class="cc-empty">Failed to load (' + e.message + ').</div>';
    });
  }

  function openPeek(fragUrl, opts) {
    if (!peek) return;
    opts = opts || {};
    closeHover();
    peekTitle.textContent = opts.title || 'Quick look';
    if (opts.fullHref) { peekFull.href = opts.fullHref; peekFull.hidden = false; }
    else { peekFull.hidden = true; }
    peek.hidden = false;
    peekScrim.hidden = false;
    positionPeek(opts.anchor || null);
    loadPeek(fragUrl, opts.anchorId || null);
  }

  function closePeek() {
    if (!peek || peek.hidden) return;
    peek.hidden = true;
    peekScrim.hidden = true;
    peekBody.innerHTML = '';
    peekFragUrl = null;
    peekSeq++;
  }

  // /source/<id>[?...][#L<n>] -> its fragment variant + the line to highlight.
  function peekUrlForSource(href) {
    var anchorId = null;
    var hi = href.indexOf('#');
    if (hi !== -1) { anchorId = href.substring(hi + 1); href = href.substring(0, hi); }
    var sep = href.indexOf('?') === -1 ? '?' : '&';
    return { url: href + sep + 'fragment=1', anchorId: anchorId };
  }

  document.addEventListener('click', function (ev) {
    if (ev.defaultPrevented || ev.button !== 0) return;
    if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
    if (!ev.target.closest) return;
    var a = ev.target.closest('a[data-peek-url], a[href^="/source/"]');
    if (!a || a.id === 'cc-peek-openfull') return;
    ev.preventDefault();
    // A source-chip's viewer link lives inside its <details> popover — fold
    // the popover so it isn't left dangling under the peek scrim.
    var pop = a.closest('details.src-pop');
    if (pop) pop.removeAttribute('open');
    var explicit = a.getAttribute('data-peek-url');
    var info = explicit
      ? { url: explicit, anchorId: null }
      : peekUrlForSource(a.getAttribute('href'));
    if (a.closest('#cc-peek')) {
      // Inside the peek (e.g. the 10-K section nav): retarget in place.
      if (!explicit) { peekFull.href = a.getAttribute('href'); peekFull.hidden = false; }
      loadPeek(info.url, info.anchorId);
      return;
    }
    openPeek(info.url, {
      title: a.getAttribute('data-peek-title') || (explicit ? 'Quick look' : 'Source'),
      fullHref: a.getAttribute('href'),
      anchor: a,
      anchorId: info.anchorId
    });
  });

  // Approve / dismiss inside the peek: run the same GET /approve the cards
  // use, then re-fetch the fragment so the card shows its new status pill —
  // the review never leaves the page. (The route 303s back to a full page;
  // fetch follows it and the body is simply discarded.)
  if (peekBody) peekBody.addEventListener('click', function (ev) {
    if (ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
    var a = ev.target.closest ? ev.target.closest('a[href^="/approve"]') : null;
    if (!a) return;
    ev.preventDefault();
    ev.stopPropagation();
    fetch(a.getAttribute('href')).then(function (r) {
      // 409 = stale/double click — the re-fetch below shows the true state.
      if (!r.ok && r.status !== 409) throw new Error('HTTP ' + r.status);
      if (peekFragUrl) loadPeek(peekFragUrl, null);
    }).catch(function (e) {
      var d = document.createElement('div');
      d.className = 'cc-empty';
      d.textContent = 'Action failed (' + e.message + ').';
      peekBody.insertBefore(d, peekBody.firstChild);
    });
  });

  var peekClose = document.getElementById('cc-peek-close');
  if (peekClose) peekClose.addEventListener('click', closePeek);
  if (peekScrim) peekScrim.addEventListener('click', closePeek);

  // ----- Ticker hover mini-card (UX9) -----
  // Hovering a ticker link (cockpit rows, analytical .ticker-link cells, or
  // anything carrying data-peek-ticker) shows a small price/verdict/next-ER
  // card from /api/peek/ticker/<T>. Hover-capable pointers only; fragments
  // are cached per page load.
  var hovercard = document.getElementById('cc-hovercard');
  var hoverCache = {};
  var hoverShowTimer = null;
  var hoverHideTimer = null;
  var hoverSeq = 0;
  var HOVER_SEL = 'a.ticker-link, td.ticker a, [data-peek-ticker]';
  var TICKER_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;

  function closeHover() {
    if (hoverShowTimer) { clearTimeout(hoverShowTimer); hoverShowTimer = null; }
    if (hoverHideTimer) { clearTimeout(hoverHideTimer); hoverHideTimer = null; }
    if (hovercard && !hovercard.hidden) { hovercard.hidden = true; hovercard.innerHTML = ''; }
    hoverSeq++;
  }

  function positionHover(rect) {
    var w = 300;
    var left = Math.max(8, Math.min(Math.round(rect.left), window.innerWidth - w - 8));
    hovercard.style.left = left + 'px';
    if (rect.bottom + 230 > window.innerHeight && rect.top > 240) {
      hovercard.style.top = Math.round(rect.top - 6) + 'px';
      hovercard.style.transform = 'translateY(-100%)';
    } else {
      hovercard.style.top = Math.round(rect.bottom + 6) + 'px';
      hovercard.style.transform = '';
    }
  }

  function showHover(el) {
    var t = el.getAttribute('data-peek-ticker') || (el.textContent || '').trim().toUpperCase();
    if (!TICKER_RE.test(t)) return;
    var seq = ++hoverSeq;
    var rect = el.getBoundingClientRect();
    function render(html) {
      if (seq !== hoverSeq) return;
      hovercard.innerHTML = html;
      hovercard.hidden = false;
      positionHover(rect);
    }
    if (hoverCache[t]) { render(hoverCache[t]); return; }
    render('<div class="cc-loading">Loading…</div>');
    fetch('/api/peek/ticker/' + encodeURIComponent(t)).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function (html) {
      hoverCache[t] = html;
      render(html);
    }).catch(function () {
      // Unknown/untracked ticker — no card is the right answer.
      if (seq === hoverSeq) closeHover();
    });
  }

  var hoverCapable = !(window.matchMedia && window.matchMedia('(hover: none)').matches);
  if (hovercard && hoverCapable) {
    document.addEventListener('mouseover', function (ev) {
      if (!ev.target.closest) return;
      if (ev.target.closest('#cc-hovercard')) {
        if (hoverHideTimer) { clearTimeout(hoverHideTimer); hoverHideTimer = null; }
        return;
      }
      var el = ev.target.closest(HOVER_SEL);
      if (!el) return;
      if (hoverHideTimer) { clearTimeout(hoverHideTimer); hoverHideTimer = null; }
      if (hoverShowTimer) clearTimeout(hoverShowTimer);
      hoverShowTimer = setTimeout(function () { showHover(el); }, 240);
    });
    document.addEventListener('mouseout', function (ev) {
      if (!ev.target.closest) return;
      if (!ev.target.closest(HOVER_SEL) && !ev.target.closest('#cc-hovercard')) return;
      if (hoverShowTimer) { clearTimeout(hoverShowTimer); hoverShowTimer = null; }
      if (hoverHideTimer) clearTimeout(hoverHideTimer);
      hoverHideTimer = setTimeout(function () {
        if (hovercard && !hovercard.hidden) { hovercard.hidden = true; hovercard.innerHTML = ''; }
        hoverSeq++;
        hoverHideTimer = null;
      }, 200);
    });
    // The card anchors to a fixed position — close rather than drift on scroll.
    document.addEventListener('scroll', function () {
      if (hovercard && !hovercard.hidden) closeHover();
    }, true);
    hovercard.addEventListener('click', function (ev) {
      if (ev.target.closest && ev.target.closest('a')) closeHover();
    });
  }

  document.addEventListener('keydown', function (ev) {
    // Ctrl/Cmd+K, plus Ctrl+Space (UX9b) — ev.code so the spacebar binding is
    // keyboard-layout independent.
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'k' || ev.key === 'K' || ev.code === 'Space')) {
      ev.preventDefault();
      if (pal && pal.hidden) openPalette(); else closePalette();
      return;
    }
    if (ev.key === 'Escape') {
      if (pal && !pal.hidden) closePalette();
      else if (peek && !peek.hidden) closePeek();
      else if (hovercard && !hovercard.hidden) closeHover();
      else if (notesDrawer && !notesDrawer.hidden) closeNotesDrawer();
      else closeDrawer();
    }
  });
  // Drawer sections ship collapsed; each remembers its own open/closed state
  // across reloads (localStorage cc-drawer-sec:<endpoint>). The toggle handler
  // only fetches while the drawer is VISIBLE — the boot-time restore below
  // also fires toggle events, and openDrawer() already loads whatever is open.
  function drawerSecKey(sec) {
    return 'cc-drawer-sec:' + (sec.getAttribute('data-endpoint') || '');
  }
  if (drawer) {
    var allSecs = drawer.querySelectorAll('.cc-drawer-sec');
    for (var di = 0; di < allSecs.length; di++) {
      allSecs[di].addEventListener('toggle', function (ev) {
        try { localStorage.setItem(drawerSecKey(ev.target), ev.target.open ? '1' : '0'); }
        catch (e) { /* storage unavailable — state just won't persist */ }
        if (ev.target.open && !drawer.hidden) loadDrawerSection(ev.target);
      });
      try { allSecs[di].open = localStorage.getItem(drawerSecKey(allSecs[di])) === '1'; }
      catch (e) { /* storage unavailable — stay collapsed */ }
    }
  }

  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      location.hash = '#' + t.getAttribute('data-tab-target');
    });
  });

  themeTabs.forEach(function (t) {
    t.addEventListener('click', function () {
      var tid = t.getAttribute('data-theme-target');
      var target = lastPanelByTheme[tid] || firstPanelOfTheme(tid);
      if (target) location.hash = '#' + target;
    });
  });


  // A ticker link anywhere in the shell (analytical panels' .ticker-link, the
  // Overview status tables' td.ticker links) opens the per-ticker Holding tab
  // instead of navigating away. Delegated on document so it also catches links
  // inside lazily-injected panels. (Links inside the embedded report iframe live
  // in a separate document and are unaffected.)
  document.addEventListener('click', function (ev) {
    if (!ev.target.closest) return;
    var a = ev.target.closest('a.ticker-link, td.ticker a');
    if (!a) return;
    var t = (a.textContent || '').trim().toUpperCase();
    if (!t) return;
    ev.preventDefault();
    location.hash = '#holding=' + encodeURIComponent(t);
  });

  window.addEventListener('hashchange', onHashChange);
  onHashChange();  // initial activation from the current hash (or overview)
})();
""".strip()


_DOC_HEAD = (
    """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio · command center</title>
"""
    + FAVICON_LINK
    + """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
""".replace("{css}", SHELL_CSS)
)

_DOC_FOOT = "</body></html>"
