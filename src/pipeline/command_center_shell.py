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
* **Sub-500ms perceived activations (S14)**: every lazy panel ships a
  content-shaped skeleton (``_SKELETON_KINDS``); fetched fragments are cached
  in sessionStorage and served stale-while-revalidate (the server ETags every
  ``/api/panel/`` response, so the background refresh is a 304 when nothing
  changed); top-bar/sub-tab hover and an idle pass after first paint prefetch
  likely-next panels (Portfolio's tracker round-trip especially). Each
  activation's fetch/render timings POST to ``/api/metrics/panel`` and read
  back in System → Data Cache.
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
* **Ask-q doorways (Law 2)**: any datum carrying ``data-ask-q`` outside the Ask
  panel (cockpit KPI chips, landing stats) hands its relative-window question to
  Ask over the goAsk stash-and-jump rail on a plain click — scoped to exclude
  ``data-panel="explore"`` (Ask wires its own chips). ``data-fact-ref`` (exact
  series) takes precedence over ``data-ask-q`` (relative window) on any cell
  carrying both — coordinated with the fact_ref session (directive §6).
* **The Ask dock is shell chrome (Ask v5)**: ``pipeline.ask_dock`` renders
  once into the body, OUTSIDE ``.cc-panels``, so the conversational dock
  persists across every tab switch. Three states — min pill / floating card /
  split column (``body[data-ask-split="1"]`` reflows the panels beside it) —
  persisted across sessions; the thread tail survives reloads.
* **One client state container (S14 PR2)**: ``pipeline.cc_state`` inlines
  ``window.CCState`` before every other script — namespaced ``cc:v1:*`` keys
  with explicit getters/setters replacing the scattered raw storage keys
  (palette→Ask handoffs, dock mode/thread, drawer sections, the SWR panel
  cache), legacy keys migrating forward on first read. The shell also tracks
  section/tab/ticker there, so a hash-less reload returns to where you were
  (same tab-session only). The full contract is the comment block atop
  ``CC_STATE_JS``.

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
from pipeline.cc_overlay import CC_OVERLAY_CSS, CC_OVERLAY_JS
from pipeline.cc_state import CC_STATE_JS
from pipeline.research_cockpit import CockpitRow
from pipeline.source_viewers import VIEWER_CONTENT_CSS
from ui.controls import controls_css, panel_section_title
from ui.source_chip import SOURCE_CHIP_JS
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
            # Information-diet curation layer (the alerts→diet split): the PULL
            # lane over the typed `signals` substrate — sell-side ratings + news
            # (non-decaying) + the forward investor-day agenda. Sibling to
            # Discovery: Discovery sources NEW names, Diet curates signal on the
            # names you already track.
            ("diet", "Diet", "/api/panel/diet", False, False),
            # The analyst journal's lifecycle home (P4.5): list / filter /
            # resolve / reclassify / supersede over analyst_notes.
            ("journal", "Journal", "/api/panel/journal", False, False),
            # The parked-comment disposition queue (S11): comments the classifier
            # couldn't route (`needs_triage`) — route / resolve / dismiss. A lens
            # over the same analyst_notes spine the Journal reads.
            ("triage", "Triage", "/api/panel/triage", False, False),
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
        # One consolidated Provenance console (S10): the old 8-tab diagnostics
        # strip (Coverage / IR Docs / Data Cache / Cron Health / DCF Coverage /
        # Evals / Validation / Restatements) collapsed into a single page that
        # composes all 8 builders (pipeline/provenance_panel.py), Coverage
        # prominent + an anchor-nav band. A single sub-tab suppresses the subnav
        # row entirely (data-single), so the System icon opens it directly. Every
        # old panel id aliases here via _LEGACY_PANEL_REDIRECTS, so existing
        # #section_coverage / #validation / #evals … deep-links still resolve.
        (("provenance", "Provenance", "/api/panel/provenance", False, False),),
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
    "budget": "provenance",
    "actions": "provenance",
    "home": "overview",
    "companies": "holding",
    "ask": "explore",
    "system": "provenance",
    # S10: the 8 System diagnostics tabs collapsed into the one Provenance
    # console — their old deep-links land there (the console's anchor-nav jumps
    # to each section, e.g. #prov-validation). The /api/panel/<id> fetch routes
    # for each builder stay live (the console + any direct fetch use them).
    "section_coverage": "provenance",
    "ir_coverage": "provenance",
    "source_calls": "provenance",
    "cron_health": "provenance",
    "dcf_coverage": "provenance",
    "evals": "provenance",
    "validation": "provenance",
    "restatements": "provenance",
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
    # Single-sub-tab sections (Home, Ask, System): the same discriminator that
    # hides the sub-tab row (data-single) marks the panels whose title the SHELL
    # owns — see _render_panels (design_language §6.1; the nav owns the title).
    single_sub_pids = frozenset(
        pid for _tid, _tlabel, subs in themes if len(subs) <= 1 for pid, *_rest in subs
    )
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
            f'aria-label="Command palette (Ctrl+K)" '
            f'title="Jump to a ticker, tab, note, or saved view (Ctrl+K / Ctrl+Space)">⌘K</button>'
            f"{_render_system_button(themes)}"
            f'<button class="cc-notes-btn" id="cc-notes-toggle" type="button" '
            f'aria-label="Quick notes" '
            f'title="Quick note + open notes (scoped to the open holding)">✎</button>'
            f'<button class="cc-settings-btn" id="cc-settings-toggle" type="button" '
            f'title="Budgets · ticker settings · maintenance">⚙ Settings</button>'
            f"{stamp}</div>",
            _render_subnav_rows(themes),
            '<main class="cc-panels" id="cc-main" tabindex="-1">',
            _render_panels(flat_tabs, overview_html, single_sub_pids),
            "</main>",
            _SETTINGS_DRAWER_HTML,
            _NOTES_DRAWER_HTML,
            _PALETTE_HTML,
            _PEEK_HTML,
            # The shared client state container (S14 PR2): one namespaced,
            # versioned window.CCState, inlined BEFORE the dock and shell
            # scripts (and therefore before any lazily-injected fragment
            # script) so every consumer sees the same store.
            f"<script>{CC_STATE_JS}</script>",
            # The one transient-surface primitive (S4, Law 3): window.CCOverlay,
            # inlined BEFORE the dock and shell scripts so both register their
            # overlays against the same open-surface stack.
            f"<script>{CC_OVERLAY_JS}</script>",
            # Escape-only dismissal for the JS-free source-chip <details>
            # popovers anywhere in the shell document (Law 3 / §3.1) — a
            # non-modal CCOverlay dismisser, not the full triad. The cite-mark
            # popover registers its own from CITE_MARKS_JS (loaded via the dock).
            f"<script>{SOURCE_CHIP_JS}</script>",
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
# open/closed state from the client store (key drawer:<endpoint>; the
# pre-S14 cc-drawer-sec:<endpoint> localStorage keys migrate on first read).
_SETTINGS_DRAWER_HTML = (
    # No per-surface scrim div — CCOverlay (S4) shows the one shared .k-scrim.
    '<aside class="cc-drawer" id="cc-drawer" role="dialog" aria-modal="true" hidden aria-label="Settings">'
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
    # No per-surface scrim div — CCOverlay (S4) shows the one shared .k-scrim.
    '<aside class="cc-drawer cc-notes-drawer" id="cc-notes-drawer" role="dialog" aria-modal="true" hidden aria-label="Notes">'
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
        out.append(
            f'<nav class="cc-tabs cc-subtabs" role="tablist" data-cc-theme="{escape(tid)}"{single} hidden>'
        )
        for pid, label, _endpoint, _picker, _required in subs:
            out.append(
                f'<button class="cc-tab" type="button" role="tab" '
                f'id="cc-tab-{escape(pid)}" aria-selected="false" '
                f'aria-controls="cc-panel-{escape(pid)}" '
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
    # No per-surface scrim div — CCOverlay (S4) shows the one shared .k-scrim.
    '<div class="cc-palette" id="cc-palette" hidden role="dialog" aria-modal="true" aria-label="Command palette">'
    # The triad's close control (x + Esc + scrim click-out) — corner-anchored
    # so it doesn't disturb the input/list layout.
    '<button class="cc-palette-close" id="cc-palette-close" type="button" '
    'aria-label="Close">&times;</button>'
    '<input id="cc-palette-input" type="text" role="combobox" '
    'aria-label="Search commands, tickers and views" '
    'aria-expanded="false" aria-controls="cc-palette-list" aria-autocomplete="list" '
    'placeholder="Jump to a ticker, tab, note, or view — or just ask…" '
    'autocomplete="off" spellcheck="false">'
    '<ul id="cc-palette-list" class="cc-palette-list" role="listbox"></ul>'
    "</div>"
)

# Peek / quick-look primitive (UX9): one positioned popover + scrim the whole
# shell shares, plus the ticker hover mini-card. Any shell link can opt in
# with ``data-peek-url`` (its href stays the real destination for middle-click
# and new-tab); ``/source/<doc_id>`` links peek automatically. The body is
# fetched lazily as an HTML fragment and injected through the same script
# re-execution path the lazy panels use.
_PEEK_HTML = (
    # No per-surface scrim div — CCOverlay (S4) shows the one shared .k-scrim.
    '<div class="cc-peek" id="cc-peek" hidden role="dialog" aria-modal="true" aria-label="Quick look">'
    '<div class="cc-peek-head">'
    '<span class="cc-peek-title" id="cc-peek-title"></span>'
    '<a class="cc-peek-openfull" id="cc-peek-openfull" href="#" hidden>open full ↗</a>'
    '<button class="cc-peek-close" id="cc-peek-close" type="button" '
    'aria-label="Close">&times;</button></div>'
    '<div class="cc-peek-body" id="cc-peek-body"></div>'
    "</div>"
    '<div class="cc-hovercard" id="cc-hovercard" hidden></div>'
)


# Structured skeletons (S14): each lazy panel's placeholder is shaped like the
# content it loads — a table panel shows ghost rows, a KPI panel shows ghost
# cards above rows — instead of one generic shimmer line. The kind map is the
# single source: the server renders the skeleton into the initial placeholder,
# and SHELL_JS captures that markup at boot so a cold (re)load can re-show it
# (e.g. the Holding panel switching tickers). Kinds:
#   table — heading + ghost header + 8 ghost rows (most System panels)
#   kpis  — heading + a 4-card KPI strip above the ghost table
#   cards — heading + a ghost card grid (memo / synthesis surfaces)
#   form  — a ghost input band + chip row + prose lines (the Ask builder)
#   band  — a thin utility band + one tall frame block (the Holding embed)
_SKELETON_KINDS: dict[str, str] = {
    "holding": "band",
    "discovery": "table",
    "diet": "table",
    "journal": "cards",
    "triage": "table",
    "explore": "form",
    "portfolio": "kpis",
    "portfolio_synthesis": "cards",
    "decisions_record": "table",
    "advisor_memos": "cards",
    "holdings": "table",
    # The 8 System diagnostics tabs collapsed into one Provenance console (S10) —
    # it leads with the Coverage matrix above several stacked panels.
    "provenance": "kpis",
}


def _skeleton_html(kind: str) -> str:
    """One panel-shaped loading skeleton. Pure presentational markup —
    ``aria-hidden`` so screen readers never narrate ghost rows."""
    head = (
        '<div class="cc-skel-line cc-skel-title"></div><div class="cc-skel-line cc-skel-sub"></div>'
    )
    table = (
        '<div class="cc-skel-table"><div class="cc-skel-row cc-skel-th"></div>'
        + '<div class="cc-skel-row"></div>' * 8
        + "</div>"
    )
    if kind == "kpis":
        body = (
            head
            + '<div class="cc-skel-kpis">'
            + '<div class="cc-skel-kpi"></div>' * 4
            + "</div>"
            + table
        )
    elif kind == "cards":
        body = (
            head + '<div class="cc-skel-cards">' + '<div class="cc-skel-card"></div>' * 6 + "</div>"
        )
    elif kind == "form":
        body = (
            '<div class="cc-skel-input"></div>'
            '<div class="cc-skel-chips">' + '<div class="cc-skel-chip"></div>' * 4 + "</div>"
            '<div class="cc-skel-line cc-skel-sub"></div>'
            '<div class="cc-skel-line cc-skel-sub"></div>'
        )
    elif kind == "band":
        body = '<div class="cc-skel-band"></div><div class="cc-skel-frame"></div>'
    else:  # table
        body = head + table
    return f'<div class="cc-skel" data-skel="{escape(kind)}" aria-hidden="true">{body}</div>'


def _render_panels(
    tabs: tuple[tuple[str, str, str | None, bool, bool], ...],
    overview_html: str,
    single_sub_pids: frozenset[str],
) -> str:
    out: list[str] = []
    for pid, label, endpoint, picker, _required in tabs:
        # The nav owns a single-sub-tab section's title: its sub-tab row is hidden
        # (data-single), so the SHELL — not the lazy panel fragment — decides the
        # title, and design_language §6.1 collapses it (suppressed=True). Routing
        # every single-sub panel through panel_section_title() keeps that decision
        # in ONE place instead of each fragment re-printing its own <h2> (the
        # redundant "Ask" bar). It sits OUTSIDE .cc-panel-body so a future
        # un-suppressed title would survive the lazy reload that rewrites the body.
        section_title = (
            panel_section_title(label, suppressed=True) if pid in single_sub_pids else ""
        )
        if pid == "overview":
            out.append(
                f'<section class="cc-panel" role="tabpanel" id="cc-panel-{escape(pid)}" '
                f'aria-labelledby="cc-tab-{escape(pid)}" '
                f'data-panel="{escape(pid)}" data-loaded="1">'
                f'{section_title}<div class="cc-panel-body">{overview_html}</div></section>'
            )
            continue
        ep = f' data-endpoint="{escape(endpoint)}"' if endpoint else ""
        # ``data-picker`` now means "ticker-scoped panel" — the shell passes the
        # hash ticker straight to loadBody. The picker UI itself moved into the
        # Holding fragment as a search combobox (UX9c), so there is no longer a
        # ``cc-picker`` <select> in the shell chrome. ``required`` is unused now:
        # the no-ticker fragment renders the combobox band, not a shell stub.
        pk = ' data-picker="1"' if picker else ""
        skel = _skeleton_html(_SKELETON_KINDS.get(pid, "table"))
        out.append(
            f'<section class="cc-panel" role="tabpanel" id="cc-panel-{escape(pid)}" '
            f'aria-labelledby="cc-tab-{escape(pid)}" '
            f'data-panel="{escape(pid)}"{ep}{pk} '
            f'data-loaded="0" data-current-ticker="" hidden>'
            f'{section_title}<div class="cc-panel-body">{skel}</div>'
            "</section>"
        )
    return "".join(out)


# Palette + control kit come from the shared token source (src/ui/tokens.py +
# src/ui/controls.py). This surface uses the CANONICAL token names directly:
# the legacy-alias :root (--panel/--ink/--link/--font-mono/…) was unforked in
# S1 PR2 so there is ONE vocabulary across every surface (design_language §2/§7,
# enforced by tests/test_ui_controls.py). The inlined VIEWER/INBOX/UPCOMING CSS
# below carries no aliases either.
SHELL_CSS = (
    palette_css("dark")
    + controls_css("dark")
    # CCOverlay (S4) close motion — paired with the kit's .k-scrim/.k-overlay
    # open motion so every overlay dismissal animates out instead of snapping.
    + CC_OVERLAY_CSS
    + """
* { box-sizing: border-box; }
body { margin: 0; padding: 0; font-family: var(--sans); background: var(--bg);
  color: var(--fg); line-height: 1.5; font-size: var(--fs-body); }
a { color: var(--accent); transition: color var(--transition); }
button { transition: color var(--transition), border-color var(--transition),
  background var(--transition); }

/* Standardized motion: overlays slide/fade IN over the standard timing; the
   symmetric exit is CCOverlay's (.cc-anim-out runs before the [hidden] toggle
   so dismissal animates too — see pipeline/cc_overlay.py). */
@keyframes cc-slide-in-right { from { transform: translateX(18px); opacity: 0; }
  to { transform: none; opacity: 1; } }
@keyframes cc-fade-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes cc-pop-in { from { transform: translateX(-50%) scale(0.985); opacity: 0; }
  to { transform: translateX(-50%) scale(1); opacity: 1; } }
@keyframes cc-rise-in { from { transform: translateY(6px); opacity: 0; }
  to { transform: none; opacity: 1; } }

/* Skip link — visible on :focus so keyboard users can bypass the top bar */
.cc-skip { position: absolute; top: -200px; left: 0; z-index: 100;
  background: var(--accent); color: var(--accent-contrast);
  padding: 8px 16px; border-radius: 0 0 var(--radius) 0; font-weight: 600;
  text-decoration: none; font-size: var(--fs-body); }
.cc-skip:focus-visible { top: 0; outline: none; }

/* Visually hidden but available to assistive technology */
.cc-sr-only { position: absolute; width: 1px; height: 1px; padding: 0;
  margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }

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
.cc-links a:hover { color: var(--accent); }
.cc-stamp { color: var(--muted); font-size: var(--fs-caption); font-family: var(--mono);
  margin-left: 12px; white-space: nowrap; }
.cc-tabs { display: flex; gap: 2px; padding: 0 16px; border-bottom: 1px solid var(--border);
  overflow-x: auto; background: var(--bg); }
/* display:flex above beats the [hidden] UA rule — restate it. Without this
   every section's sub-row rendered at once (the old "four stacked menus"). */
.cc-tabs[hidden] { display: none; }
.cc-subtabs[data-single="1"] { display: none; }
.cc-tab { background: transparent; border: none; border-bottom: 2px solid transparent;
  color: var(--muted); padding: 10px 16px; font-size: var(--fs-body); font-weight: 600;
  cursor: pointer; white-space: nowrap; font-family: var(--sans); }
.cc-tab:hover { color: var(--fg); }
.cc-tab.active { color: var(--fg); border-bottom-color: var(--accent); }
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
.cc-home-rail-links a:hover { color: var(--accent); }
.cc-home-rail .ix-stream { max-height: calc(100vh - 140px); max-height: calc(100dvh - 140px); overflow-y: auto;
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

/* Structured panel skeletons (S14): every ghost block shares the PR8 shimmer
   wash; the layouts echo the real panel anatomy (heading → KPI strip → table
   rows / card grid / input band) so the first paint reads as "the panel,
   loading" instead of "a spinner". Layout-only, token-skinned. */
.cc-skel { padding: 12px 0 24px; }
.cc-skel-line, .cc-skel-row, .cc-skel-kpi, .cc-skel-card, .cc-skel-input,
.cc-skel-chip, .cc-skel-band, .cc-skel-frame {
  background: linear-gradient(90deg, var(--surface) 25%, var(--paper) 50%, var(--surface) 75%);
  background-size: 200% 100%; animation: cc-shimmer 1.2s ease-in-out infinite;
  border-radius: var(--radius); }
.cc-skel-title { height: 16px; max-width: 240px; margin-bottom: 10px; }
.cc-skel-sub { height: 10px; max-width: 460px; margin-bottom: 18px; }
.cc-skel-table { display: flex; flex-direction: column; gap: 10px; }
.cc-skel-row { height: 12px; }
.cc-skel-row.cc-skel-th { height: 9px; max-width: 58%; }
.cc-skel-row:nth-child(3n) { max-width: 94%; }
.cc-skel-row:nth-child(3n+1) { max-width: 98%; }
.cc-skel-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin-bottom: 18px; }
.cc-skel-kpi { height: 64px; }
.cc-skel-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: var(--sp-3); }
.cc-skel-card { height: 96px; }
.cc-skel-input { height: 38px; max-width: 760px; margin-bottom: 12px; }
.cc-skel-chips { display: flex; gap: var(--sp-2); margin-bottom: 18px; }
.cc-skel-chip { height: 20px; width: 92px; border-radius: var(--radius-full); }
.cc-skel-band { height: 36px; margin-bottom: 14px; }
.cc-skel-frame { height: calc(100vh - 280px); height: calc(100dvh - 280px); min-height: 360px; }
@media (prefers-reduced-motion: reduce) {
  .cc-skel-line, .cc-skel-row, .cc-skel-kpi, .cc-skel-card, .cc-skel-input,
  .cc-skel-chip, .cc-skel-band, .cc-skel-frame, .cc-loading::after { animation: none; }
  /* Scrims are now the one shared .k-scrim (kit + CCOverlay handle its
     reduced-motion); the surfaces keep their own open keyframes. */
  .cc-drawer, .cc-palette, .cc-peek { animation: none; transition-duration: 0.01ms; }
}

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
.panel { margin-bottom: 28px; background: var(--surface);
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
.ticker-link { color: var(--fg); text-decoration: none; font-weight: 600; }
.ticker-link:hover { color: var(--accent); }
tr.tone-sell { background: rgba(248, 113, 113, 0.06); }
tr.tone-trim { background: rgba(251, 191, 36, 0.04); }
tr.tone-init { background: rgba(74, 222, 128, 0.06); }
tr.tx-buy { background: rgba(74, 222, 128, 0.04); }
tr.tx-sell { background: rgba(248, 113, 113, 0.02); }
td.trigger-cell { font-family: var(--mono); font-size: var(--fs-caption);
  text-transform: uppercase; }
tr.tone-sell .trigger-cell { color: var(--bad); }
tr.tone-trim .trigger-cell { color: var(--warn); }
tr.tone-init .trigger-cell { color: var(--ok); }
td.signal-strong { color: var(--ok); font-weight: 600; }
td.signal-medium { color: var(--warn); }
td.signal-weak { color: var(--muted); }
/* Inline code tracks the surrounding step (mono renders larger than sans at
   equal px — 0.93em is the optical correction, not an importance level). */
code { font-family: var(--mono); font-size: 0.93em; color: var(--fg-soft); }
.cli-hint { font-family: var(--mono); font-size: var(--fs-caption); padding: 10px 12px;
  background: var(--paper); border-radius: var(--radius); color: var(--ok);
  overflow-x: auto; margin: 6px 0 0; }
.panel-h3 { font-size: var(--fs-section); margin: 18px 0 8px; font-weight: 600;
  color: var(--fg); }
/* Synthesis */
.synthesis-panel { border-left: 3px solid var(--ok); }
.synthesis-body { font-size: var(--fs-section); line-height: 1.65; }
.synthesis-body h2, .synthesis-body h3, .synthesis-body h4,
.synthesis-body h5, .synthesis-body h6 { color: var(--fg); margin-top: 1.2em; margin-bottom: 6px; }
.synthesis-body h2 { font-size: var(--fs-title); }
.synthesis-body h3 { font-size: var(--fs-section); }
/* h4-h6 share the body size: the one prose boundary maps deep markdown headings
   (###/####) here, and panels already own the h2/h3 levels above them. */
.synthesis-body h4, .synthesis-body h5, .synthesis-body h6 { font-size: var(--fs-body); color: var(--ok); }
.synthesis-body strong { color: var(--fg); }
.synthesis-body code { background: var(--paper); padding: 1px 5px; border-radius: var(--radius); }
.synthesis-body ul { padding-left: 22px; }
.synthesis-body li { margin-bottom: 4px; }
.synthesis-body hr { border: none; border-top: 1px solid var(--border); margin: 16px 0; }
/* Reread grid + cards */
.reread-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: var(--sp-3); margin-top: 8px; }
.reread-card { background: var(--surface); border-radius: var(--radius); padding: 12px 14px; }
.reread-card summary { cursor: pointer; list-style: none; display: flex; justify-content: space-between; align-items: baseline; font-size: var(--fs-title); font-weight: 600; }
.reread-card summary::-webkit-details-marker { display: none; }
.reread-card summary::before { content: '▸ '; color: var(--muted); font-family: var(--mono); }
.reread-card[open] summary::before { content: '▾ '; }
.reread-stamp { color: var(--muted); font-size: var(--fs-caption); font-family: var(--mono); font-weight: 400; }
.reread-body { font-size: var(--fs-body); line-height: 1.55; margin-top: 10px; }
.reread-body h2, .reread-body h3, .reread-body h4 { color: var(--fg); margin: 10px 0 4px; }
.reread-body h2 { font-size: var(--fs-section); color: var(--ok); }
.reread-body h3 { font-size: var(--fs-body); }
.reread-body strong { color: var(--fg); }
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
.kpi-value { font-size: var(--fs-display); font-weight: 700; margin: 2px 0; color: var(--fg);
  font-variant-numeric: tabular-nums; }
.kpi-sub { font-size: var(--fs-micro); color: var(--muted); font-family: var(--mono); }
.calib-strip { display: flex; flex-direction: column; gap: 6px; margin: 8px 0 18px; }
.calib-row { display: grid; grid-template-columns: 80px 1fr 110px; gap: 12px; align-items: center; font-size: var(--fs-caption); }
.calib-label { color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.calib-bar { background: var(--paper); border-radius: var(--radius-full); height: 14px; overflow: hidden; }
.calib-fill { background: linear-gradient(90deg, var(--bad) 0%, var(--warn) 50%, var(--ok) 100%); height: 100%; }
.calib-value { font-family: var(--mono); color: var(--fg-soft); text-align: right; }
.decisions-table td.outcome-correct { color: var(--ok); }
.decisions-table td.outcome-wrong { color: var(--bad); }
.decisions-table td.outcome-mixed { color: var(--warn); }
.decisions-table td.outcome-pending { color: var(--muted); }
/* LLM budget */
.budget-table td code { font-family: var(--mono); font-size: 0.93em; color: var(--fg); background: transparent; padding: 0; }
.burn-cell { width: 200px; padding: 6px 10px; }
.burn-bar { width: 100%; height: 8px; background: var(--paper); border-radius: var(--radius-full); overflow: hidden; }
.burn-fill { height: 100%; }
.burn-ok { background: var(--ok); }
.burn-warn { background: var(--warn); }
.burn-over { background: var(--bad); }
.budget-footer { margin-top: 12px; font-size: var(--fs-body); color: var(--fg-soft); }
.budget-footer strong { color: var(--fg); }
/* Inputs/selects: skinned by the shared control kit (ui/controls.py). */
.budget-table input, .budget-table select { padding: 4px 6px; }
.budget-table select { padding-right: 26px; }
.budget-save { background: var(--accent); color: var(--accent-contrast); border: none;
  padding: 5px 12px; border-radius: var(--radius); font-weight: 600;
  font-size: var(--fs-body); cursor: pointer; }
/* Tier coverage strip */
.tier-strip { background: var(--surface); border-radius: var(--radius);
  padding: 10px 14px; margin-bottom: 22px; font-size: var(--fs-body); display: flex;
  align-items: center; flex-wrap: wrap; gap: var(--sp-1); }
.tier-strip-label { color: var(--muted); font-size: var(--fs-caption);
  text-transform: uppercase; letter-spacing: 0.06em; margin-right: 8px; }
.tier-chip { font-family: var(--mono); font-size: var(--fs-caption); padding: 2px 6px;
  border-radius: var(--radius-full); cursor: help; }
a.tier-chip { text-decoration: none; cursor: pointer; }
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
.list-section table { background: var(--surface); border-radius: var(--radius); overflow: hidden; }
.list-section th { padding-top: 10px; }
td.ticker { font-family: var(--mono); font-weight: 600; }
td.ticker a { color: var(--fg); text-decoration: none; }
td.ticker a:hover { color: var(--accent); }
.qa-yes, .qa-no { font-size: var(--fs-micro); padding: 1px 5px; border-radius: var(--radius-full); letter-spacing: 0.3px; }
.qa-yes { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.qa-no { background: color-mix(in srgb, var(--bad) 16%, transparent); color: var(--bad); }
.comments-open { color: var(--warn); font-weight: 500; }
.breach-badge { display: inline-block; padding: 2px 8px; border-radius: var(--radius-full);
  font-size: var(--fs-micro); color: white; text-transform: uppercase;
  letter-spacing: 0.05em; font-weight: 600; }
.open-link { color: var(--accent); text-decoration: none; }
.open-link:hover { text-decoration: underline; }
.empty { color: var(--muted); font-style: italic; padding: 12px; }

/* ============================================================
   Holding drill-down tab (ticker_command_center sections + embedded report)
   ============================================================ */
.cc-holding-links { font-size: var(--fs-body); display: inline-flex; gap: 14px;
  align-items: center; }
.cc-holding-links a { color: var(--accent); text-decoration: none; white-space: nowrap; }
.cc-holding-links a:hover { text-decoration: underline; }
.badges { display: inline-flex; gap: 4px; margin-left: 8px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: var(--radius-full);
  font-size: var(--fs-micro); text-transform: uppercase; letter-spacing: 0.05em;
  font-weight: 600; background: var(--border); }
.badge.b-ok { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.badge.b-warn { background: color-mix(in srgb, var(--warn) 16%, transparent); color: var(--warn); }
.badge.b-bad { background: color-mix(in srgb, var(--bad) 16%, transparent); color: var(--bad); }
.badge.b-muted { background: var(--paper); color: var(--muted); }
.fresh-strip { display: flex; gap: 10px; margin-bottom: 22px; flex-wrap: wrap; }
.fresh-cell { background: var(--surface); border-radius: var(--radius);
  padding: 8px 14px; flex: 1; min-width: 140px; }
.fresh-label { font-size: var(--fs-micro); text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); }
.fresh-val { font-size: var(--fs-section); font-variant-numeric: tabular-nums; }
.ok-dot { color: var(--ok); }
.tcc-refresh, .tcc-refresh + .tcc-refresh { background: var(--accent); color: var(--accent-contrast); border: none;
  padding: 6px 12px; border-radius: var(--radius); font-weight: 600; font-size: var(--fs-body);
  cursor: pointer; margin-right: 4px; }
.artifact-table code { background: transparent; padding: 0; }
.cc-report-embed { padding-bottom: 8px; }
.cc-report-frame { width: 100%; height: calc(100vh - 220px); height: calc(100dvh - 220px); min-height: 560px;
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
  max-height: calc(100vh - 150px); max-height: calc(100dvh - 150px); overflow-y: auto; position: sticky; top: 88px; }
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
.rail-note-when { font-family: var(--mono); font-size: var(--fs-micro); color: var(--muted); }
.rail-note-body { font-size: var(--fs-body); line-height: 1.5; margin: 4px 0; color: var(--fg-soft);
  overflow-wrap: anywhere; }
/* Rail note bodies render through ui.prose (markdown -> block HTML); collapse
   the outer paragraph margins so a one-line note keeps its tight box. */
.rail-note-body > :first-child { margin-top: 0; }
.rail-note-body > :last-child { margin-bottom: 0; }
.rail-note-body p { margin: 0 0 6px; }
.rail-note-body ul { margin: 0 0 6px; padding-left: 20px; }
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
  border: 1px solid var(--border-2); background: var(--paper); color: var(--fg);
  border-radius: var(--radius); font-family: var(--mono); font-weight: 700;
  font-size: var(--fs-caption); letter-spacing: 0.05em; }
.trigger-badge, .status-badge { display: inline-flex; align-items: center; padding: 1px 6px;
  border: 1px solid var(--border-2); border-radius: var(--radius); color: var(--fg-soft);
  font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.05em;
  text-transform: uppercase; }
.status-pending { color: var(--warn); border-color: var(--warn); }
.status-approved { color: var(--ok); border-color: var(--ok); }
.status-dismissed { color: var(--muted); border-color: var(--muted); }
.status-expired { color: var(--muted-2); border-color: var(--muted-2); }
.fired-at { color: var(--muted); font-family: var(--mono); font-size: var(--fs-micro);
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
  border-radius: var(--radius-full); font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.05em;
  text-transform: uppercase; }
.qa-body { flex: 1; min-width: 140px; color: var(--fg-soft); font-size: var(--fs-caption); }
.qa-actions { display: flex; gap: 6px; align-items: center; font-family: var(--mono);
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
  border-bottom: 1px solid var(--hairline); font-family: var(--sans); }
.evidence-citations-table td { padding: 4px 6px 4px 0; vertical-align: top;
  border-bottom: 1px solid var(--hairline); }
.evidence-citations-table tr:last-child td { border-bottom: none; }
.cite-kind { color: var(--fg-soft); white-space: nowrap; }
.cite-locator { color: var(--muted); word-break: break-all; font-family: var(--mono);
  font-size: var(--fs-micro); }
.cite-excerpt { color: var(--fg-soft); }
.cite-prov { color: var(--muted); font-family: var(--mono); font-size: var(--fs-micro); }
.prov-source { color: var(--accent); }
.evidence-raw { margin-top: 4px; }
.evidence-raw > summary { cursor: pointer; color: var(--muted);
  font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.05em; }
.evidence-raw-pre { margin: 5px 0 0; padding: 7px 9px; background: var(--bg);
  border-radius: var(--radius); font-family: var(--mono);
  font-size: var(--fs-micro); color: var(--fg-soft); white-space: pre-wrap; word-break: break-all;
  max-height: 260px; overflow: auto; }

/* Three-theme nav (master build P1.1): primary theme row + per-theme sub-tab
   rows. Sub-tab rows reuse .cc-tab styling at a smaller size. */
.cc-theme-row { padding-top: 2px; }
.cc-theme-tab { font-size: var(--fs-section); font-weight: 600; letter-spacing: 0.01em; }
.cc-subtabs { z-index: 19; }
.cc-subtabs .cc-tab { font-size: var(--fs-body); }

/* Settings drawer (P3.4): admin-as-drawer instead of admin-as-tab. */
.cc-settings-btn { background: var(--paper); color: var(--fg); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 5px 12px; font-size: var(--fs-body); font-weight: 600;
  cursor: pointer; font-family: var(--sans); margin-right: 14px; margin-left: 6px; }
.cc-settings-btn:hover { border-color: var(--accent); color: var(--accent); }

/* System demoted to a utility icon (UX9b): same activation contract as a nav
   section button, skinned like its ⌘K / ⚙ neighbours. */
.cc-system-btn { background: transparent; border: 1px solid var(--border); color: var(--muted);
  border-radius: var(--radius); padding: 5px 10px; font-size: var(--fs-body); cursor: pointer;
  font-family: var(--sans); margin-left: 6px; line-height: 1.2; }
.cc-system-btn:hover, .cc-system-btn.active { border-color: var(--accent); color: var(--accent); }

/* Shared ✎ Notes drawer trigger (UX9b). */
.cc-notes-btn { background: transparent; border: 1px solid var(--border); color: var(--muted);
  border-radius: var(--radius); padding: 5px 10px; font-size: var(--fs-body); cursor: pointer;
  font-family: var(--sans); margin-left: 6px; line-height: 1.2; }
.cc-notes-btn:hover { border-color: var(--accent); color: var(--accent); }
.cc-notes-drawer { width: min(560px, 94vw); }
.cc-drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(780px, 94vw);
  background: var(--bg); border-left: 1px solid var(--border); z-index: 39;
  display: flex; flex-direction: column; box-shadow: -12px 0 32px rgba(0,0,0,0.35);
  animation: cc-slide-in-right var(--transition); }
.cc-drawer[hidden] { display: none; }
.cc-drawer-head { display: flex; justify-content: space-between; align-items: center;
  padding: 14px 18px; border-bottom: 1px solid var(--border); font-weight: 700; }
.cc-drawer-close { background: transparent; border: none; color: var(--muted);
  font-size: var(--fs-display); cursor: pointer; line-height: 1; padding: 2px 6px; }
.cc-drawer-close:hover { color: var(--fg); }
.cc-drawer-body { overflow-y: auto; padding: 14px 18px 40px; }
.cc-drawer-sec { margin-bottom: 14px; border-radius: var(--radius);
  background: var(--surface); }
.cc-drawer-sec > summary { cursor: pointer; list-style: none; padding: 11px 14px;
  font-size: var(--fs-section); font-weight: 600; }
.cc-drawer-sec > summary::-webkit-details-marker { display: none; }
.cc-drawer-sec > summary::before { content: '▸ '; color: var(--muted); font-family: var(--mono); }
.cc-drawer-sec[open] > summary::before { content: '▾ '; }
.cc-drawer-sec-body { padding: 0 14px 12px; }
.cc-drawer-sec-body .panel { margin-bottom: 0; border: none; padding: 0; background: transparent; }

/* Command palette (Ctrl/Cmd+K) */
.cc-palette-btn { background: transparent; border: 1px solid var(--border); color: var(--muted);
  border-radius: var(--radius); padding: 5px 9px; font-size: var(--fs-caption); cursor: pointer;
  font-family: var(--mono); }
.cc-palette-btn:hover { border-color: var(--accent); color: var(--accent); }
.cc-palette { position: fixed; top: 14vh; left: 50%; transform: translateX(-50%);
  width: min(560px, 92vw); background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); z-index: 49; box-shadow: 0 18px 48px rgba(0,0,0,0.5);
  overflow: hidden; animation: cc-pop-in var(--transition); }
.cc-palette[hidden] { display: none; }
/* Corner close (the triad's x on the palette) — over the input's right edge. */
.cc-palette-close { position: absolute; top: 8px; right: 10px; z-index: 1;
  background: transparent; border: none; color: var(--muted); font-size: var(--fs-display);
  line-height: 1; cursor: pointer; padding: 0 4px; }
.cc-palette-close:hover { color: var(--fg); }
.cc-palette input { width: 100%; box-sizing: border-box; background: transparent;
  border: none; border-bottom: 1px solid var(--border); border-radius: 0; color: var(--fg);
  padding: 13px 40px 13px 16px; font-size: var(--fs-section); font-family: var(--sans); outline: none; }
/* The palette field is the dialog's own chrome — no kit focus ring. */
.cc-palette input:focus-visible { border-color: var(--border); border-bottom-color: var(--accent);
  box-shadow: none; }
.cc-palette-list { list-style: none; margin: 0; padding: 6px 0; max-height: 46vh; max-height: 46dvh;
  overflow-y: auto; }
.cc-palette-list li { display: flex; justify-content: space-between; gap: 12px;
  padding: 8px 16px; font-size: var(--fs-body); cursor: pointer; color: var(--fg);
  transition: background var(--transition); }
.cc-palette-list li.sel, .cc-palette-list li:hover { background: var(--paper); }
.cc-palette-list li .cc-pal-hint { color: var(--muted); font-size: var(--fs-micro);
  font-family: var(--mono); }
.cc-palette-list .k-tick-name { max-width: 36ch; }
.cc-palette-list li.cc-pal-none { color: var(--muted); cursor: default; }

/* ============================================================
   Peek / quick-look (UX9): one shared popover instead of the
   drill-throughs — source excerpts, alert review, memos.
   z-order: ask dock (35) < drawer (39) < peek (45) < hover card (46)
   < palette (49).
   ============================================================ */
.cc-peek { position: fixed; z-index: 45; width: min(680px, 92vw);
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: 0 18px 48px rgba(0,0,0,0.5); display: flex; flex-direction: column;
  overflow: hidden; animation: cc-rise-in var(--transition); }
.cc-peek[hidden], .cc-hovercard[hidden] { display: none; }
.cc-peek-head { display: flex; align-items: center; gap: 12px; padding: 9px 14px;
  border-bottom: 1px solid var(--border); flex: none; }
.cc-peek-title { font-weight: 700; font-size: var(--fs-body); margin-right: auto;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cc-peek-openfull { font-size: var(--fs-caption); color: var(--muted); text-decoration: none;
  white-space: nowrap; }
.cc-peek-openfull:hover { color: var(--accent); }
.cc-peek-close { background: transparent; border: none; color: var(--muted);
  font-size: var(--fs-display); cursor: pointer; line-height: 1; padding: 2px 6px; }
.cc-peek-close:hover { color: var(--fg); }
.cc-peek-body { overflow-y: auto; padding: 12px 14px; min-height: 60px; }
.cc-peek-body .alert-card:last-child { margin-bottom: 0; }
.cc-peek-foot { margin-top: 8px; font-size: var(--fs-caption); }
.cc-peek-foot a { color: var(--muted); text-decoration: none; }
.cc-peek-foot a:hover { color: var(--accent); }
.cc-peek-memo-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
.cc-peek-memo-head h2 { font-size: var(--fs-section); margin: 0; }
/* JS-applied stand-in for the full page's :target highlight (#L<n>). */
.cc-peek-target { background: rgba(245, 198, 106, 0.14);
  outline: 1px solid var(--warn); border-radius: var(--radius); }

/* Ticker hover mini-card */
.cc-hovercard { position: fixed; z-index: 46; width: 300px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 12px;
  box-shadow: 0 10px 28px rgba(0,0,0,0.45); font-size: var(--fs-body); }
.cc-mini-head { display: flex; align-items: center; gap: 8px; }
.cc-mini-ticker { font-family: var(--mono); font-weight: 700; font-size: var(--fs-section); }
.cc-mini-name { color: var(--muted); font-size: var(--fs-caption); margin: 1px 0 6px; }
.cc-mini-row { display: flex; justify-content: space-between; gap: 10px; padding: 2px 0; }
.cc-mini-row > span { color: var(--muted); }
.cc-mini-row b { font-weight: 600; font-variant-numeric: tabular-nums; }
.cc-mini-open { margin-top: 7px; padding-top: 6px; border-top: 1px solid var(--border);
  font-size: var(--fs-caption); }
.cc-mini-open a { color: var(--accent); text-decoration: none; }

/* ============================================================
   Responsive + touch-aware overrides (S16 PR1)
   ============================================================ */

/* Narrow topbar at ≤900: stamp + links go; nav already overflow-x auto. */
@media (max-width: 900px) {
  .cc-stamp, .cc-links { display: none; }
  .cc-topbar { padding: 8px 16px; }
}

/* Tablet portrait + phone: compress panels, full-width drawers, safe-area. */
@media (max-width: 768px) {
  .cc-panels { padding: 14px 16px 48px; }
  .cc-drawer { width: 100%; border-left: none; }
  .cc-notes-drawer { width: 100%; }
  .cc-drawer-body { padding-bottom: max(40px, env(safe-area-inset-bottom, 40px)); }
  .cc-palette { top: 6vh; }
}

/* Horizontal table scroll on smaller screens (prevents viewport overflow). */
@media (max-width: 1024px) {
  .panel table, .list-section table {
    display: block; overflow-x: auto; -webkit-overflow-scrolling: touch;
  }
}

/* Touch targets: give interactive chrome a comfortable tap area. */
@media (pointer: coarse) {
  .k-btn, .cc-tab, .cc-drawer-close, .cc-peek-close,
  .cc-settings-btn, .cc-notes-btn, .cc-system-btn { min-height: 44px; }
  a.tier-chip { min-height: 44px; display: inline-flex; align-items: center; }
}
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
    budget: 'provenance',
    actions: 'provenance',
    home: 'overview',
    companies: 'holding',
    ask: 'explore',
    system: 'provenance',
    // S10: the 8 System diagnostics tabs collapsed into one Provenance console.
    section_coverage: 'provenance',
    ir_coverage: 'provenance',
    source_calls: 'provenance',
    cron_health: 'provenance',
    dcf_coverage: 'provenance',
    evals: 'provenance',
    validation: 'provenance',
    restatements: 'provenance'
  };
  // Legacy panels that became settings-drawer sections (P3.4): their old
  // deep-links also auto-open the drawer after landing on Governance.
  var DRAWER_OPENERS = { budget: 1, actions: 1 };

  // ----- Accessibility helpers -----
  var liveRegion = document.getElementById('cc-live');
  function announce(msg) {
    if (!liveRegion) return;
    liveRegion.textContent = '';
    // Flush so the same message can re-announce.
    setTimeout(function () { liveRegion.textContent = msg; }, 50);
  }

  // Focus trap + restore is CCOverlay's now (pipeline/cc_overlay.py) — the
  // shell's former focusableIn()/trapFocus() helpers moved into the primitive.

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

  // ----- Perceived-latency plumbing (S14) -----
  // Three layers over one per-panel cache (session-scoped store entries,
  // key cc:v1:panel:<id>[:TICKER] -> {etag, html, ts}):
  //   1. Structured skeletons — the server renders a content-shaped
  //      placeholder per panel; captured at boot (SKEL) so cold (re)loads can
  //      re-show it (e.g. the Holding panel switching tickers).
  //   2. Stale-while-revalidate — a cached fragment paints instantly, then a
  //      background fetch with If-None-Match either 304s (server adds an ETag
  //      to every /api/panel/ response) or quietly swaps the fresh fragment
  //      in, preserving scroll and never while the user is typing inside it.
  //   3. Prefetch — top-bar/sub-tab hover plus an idle pass after first paint
  //      (WARM_PANELS) fill the cache, so likely-next activations are paints.
  // Every activation records {fetch,render,total} ms + which path served it:
  // a small ring on window.CCPerf and a fire-and-forget POST to
  // /api/metrics/panel (read back in System → Data Cache).
  var SKEL = {};            // panel id -> boot placeholder markup
  var INFLIGHT = {};        // cache key -> in-flight fragment promise
  var FRESH_MS = 30000;     // just-fetched window: skip revalidation
  var WARM_PANELS = ['portfolio', 'explore'];

  panels.forEach(function (p) {
    if (p.getAttribute('data-loaded') !== '1') {
      var b = p.querySelector('.cc-panel-body');
      if (b) SKEL[p.getAttribute('data-panel')] = b.innerHTML;
    }
  });

  function skelFor(pid) {
    return SKEL[pid] || '<div class="cc-loading">Loading…</div>';
  }

  // The fragment cache lives in the shared store (S14 PR2): same
  // cc:v1:panel:* entries PR1 wrote, addressed through CCState.panel so the
  // store owns panel cache metadata (quota eviction included).
  var cacheKey = window.CCState.panel.key;
  var cacheGet = window.CCState.panel.get;
  var cacheSet = window.CCState.panel.set;

  // One fetch per cache key at a time: a hover prefetch already in flight is
  // reused by the activation that follows it instead of double-fetching.
  function fetchFragment(url, key, etag) {
    if (INFLIGHT[key]) return INFLIGHT[key];
    var opts = etag ? { headers: { 'If-None-Match': etag } } : {};
    var p = fetch(url, opts).then(function (r) {
      if (r.status === 304) return { status: 304, etag: etag, html: null };
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text().then(function (html) {
        return { status: 200, etag: r.headers.get('ETag') || '', html: html };
      });
    });
    INFLIGHT[key] = p.then(
      function (res) { delete INFLIGHT[key]; return res; },
      function (err) { delete INFLIGHT[key]; throw err; }
    );
    return INFLIGHT[key];
  }

  var PERF = { samples: [] };
  window.CCPerf = PERF;
  function record(pid, cache, fetchMs, renderMs, totalMs, status) {
    function r1(x) { return x == null ? null : Math.round(x * 10) / 10; }
    var s = { panel: pid, cache: cache, fetch_ms: r1(fetchMs),
              render_ms: r1(renderMs), total_ms: r1(totalMs), status: status || null };
    PERF.samples.push(s);
    if (PERF.samples.length > 60) PERF.samples.shift();
    try {
      fetch('/api/metrics/panel', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(s), keepalive: true
      }).catch(function () {});
    } catch (e) {}
  }

  function loadBody(panel, ticker) {
    var ep = panel.getAttribute('data-endpoint');
    if (!ep) return;
    var pid = panel.getAttribute('data-panel');
    var body = panel.querySelector('.cc-panel-body');
    var url = ep + (ticker ? ('?ticker=' + encodeURIComponent(ticker)) : '');
    var key = cacheKey(pid, ticker || '');
    var cached = cacheGet(key);
    var t0 = performance.now();
    var served = null;  // the path that painted first, if any

    if (cached) {
      injectHtml(body, cached.html);
      panel.setAttribute('data-current-ticker', ticker || '');
      var paintMs = performance.now() - t0;
      served = (Date.now() - (cached.ts || 0) < FRESH_MS) ? 'prefetch' : 'swr';
      record(pid, served, null, paintMs, paintMs, 200);
      if (served === 'prefetch') return;  // just fetched — skip revalidation
    } else {
      body.innerHTML = skelFor(pid);
      announce((pid || 'panel') + ' loading…');
    }

    var tFetch = performance.now();
    fetchFragment(url, key, cached && cached.etag).then(function (res) {
      var fetchMs = performance.now() - tFetch;
      if (res.status === 304) {
        if (cached) { cached.ts = Date.now(); cacheSet(key, cached); }
        record(pid, 'revalidate', fetchMs, 0, fetchMs, 304);
        return;
      }
      var entry = { etag: res.etag, html: res.html, ts: Date.now() };
      if (served) {
        // Quiet refresh of an already-painted stale fragment. Never yank the
        // DOM out from under the user's typing — cache the fresh copy and let
        // the next pageload serve it instead.
        var ae = document.activeElement;
        if (ae && body.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) {
          cacheSet(key, entry);
          record(pid, 'revalidate', fetchMs, 0, fetchMs, 200);
          return;
        }
        var y = window.scrollY, st = body.scrollTop;
        var tR = performance.now();
        injectHtml(body, res.html);
        window.scrollTo(0, y);
        body.scrollTop = st;
        record(pid, 'revalidate', fetchMs, performance.now() - tR, fetchMs + (performance.now() - tR), 200);
      } else {
        var tR2 = performance.now();
        injectHtml(body, res.html);
        panel.setAttribute('data-current-ticker', ticker || '');
        record(pid, 'cold', fetchMs, performance.now() - tR2, performance.now() - t0, 200);
        announce((pid || 'panel') + ' ready');
      }
      cacheSet(key, entry);
    }).catch(function (e) {
      if (!served) {
        body.innerHTML = '<div class="cc-empty">Failed to load (' + e.message + ').</div>';
        announce((pid || 'panel') + ' failed to load');
      }
    });
  }

  // Warm a not-yet-loaded panel's cache (no DOM injection — fragment scripts
  // run at activation, when the panel is visible and can measure layout).
  // Ticker-scoped panels prefetch their no-ticker variant (the Holding
  // combobox band). Returns a promise so warmStart can run sequentially.
  function prefetchPanel(pid) {
    var panel = panelById(pid);
    if (!panel || panel.getAttribute('data-loaded') === '1') return Promise.resolve();
    var ep = panel.getAttribute('data-endpoint');
    if (!ep) return Promise.resolve();
    var key = cacheKey(pid, '');
    var cached = cacheGet(key);
    if (cached && Date.now() - (cached.ts || 0) < FRESH_MS) return Promise.resolve();
    var t0 = performance.now();
    return fetchFragment(ep, key, cached && cached.etag).then(function (res) {
      if (res.status === 304) {
        if (cached) { cached.ts = Date.now(); cacheSet(key, cached); }
      } else {
        cacheSet(key, { etag: res.etag, html: res.html, ts: Date.now() });
      }
      record(pid, 'revalidate', performance.now() - t0, 0, performance.now() - t0, res.status);
    }).catch(function () {});
  }

  // Hover intent on a section or sub-tab button warms its landing panel; the
  // 80ms delay skips drive-by passes on the way to something else.
  var prefetchHoverTimer = null;
  document.addEventListener('mouseover', function (ev) {
    if (!ev.target.closest) return;
    var t = ev.target.closest('.cc-theme-tab, .cc-tab[data-tab-target]');
    if (!t) return;
    if (prefetchHoverTimer) clearTimeout(prefetchHoverTimer);
    prefetchHoverTimer = setTimeout(function () {
      var pid = t.getAttribute('data-tab-target');
      if (!pid) {
        var tid = t.getAttribute('data-theme-target');
        pid = lastPanelByTheme[tid] || firstPanelOfTheme(tid);
      }
      if (pid) prefetchPanel(pid);
    }, 80);
  });

  // Idle warm-start after first paint: Portfolio (the tracker round-trip
  // makes it the slowest first hit) and Ask, one at a time so a burst of
  // panel builds never competes with the visible page.
  function warmStart() {
    WARM_PANELS.reduce(function (chain, pid) {
      return chain.then(function () { return prefetchPanel(pid); });
    }, Promise.resolve());
  }
  if (window.requestIdleCallback) window.requestIdleCallback(warmStart, { timeout: 4000 });
  else setTimeout(warmStart, 1500);

  function activate(panelId, ticker) {
    var panel = panelById(panelId);
    if (!panel) { panel = panelById('overview'); panelId = 'overview'; ticker = null; }
    panels.forEach(function (p) { p.hidden = (p !== panel); });
    var activeTheme = null;
    tabs.forEach(function (t) {
      var on = t.getAttribute('data-tab-target') === panelId;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on) activeTheme = t.getAttribute('data-cc-theme');
    });
    if (activeTheme) {
      themeTabs.forEach(function (t) {
        var on = t.getAttribute('data-theme-target') === activeTheme;
        t.classList.toggle('active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
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
      window.CCState.set('section', activeTheme);
    }
    // Track where the user IS (S14 PR2): tab always; ticker only on the
    // ticker-scoped panels so the last holding survives a detour through
    // other tabs. The boot path replays these on a hash-less reload.
    window.CCState.set('tab', panelId);
    // ``data-picker`` panels are ticker-scoped: pass the hash ticker straight to
    // loadBody (the in-fragment combobox supplies it via the #holding=<T> hash).
    var isPicker = panel.getAttribute('data-picker') === '1';
    if (isPicker && ticker) window.CCState.set('ticker', ticker);
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
  // Dismissal (x + Esc + scrim click-out + focus trap/restore) and the
  // one-right-drawer-at-a-time exclusion are CCOverlay's now (group
  // 'cc-primary'); this keeps only the drawer's content logic.
  var drawer = document.getElementById('cc-drawer');

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

  var drawerOv = drawer && window.CCOverlay.register(drawer, {
    priority: window.CCOverlay.PRIORITY.DRAWER,
    scrim: true, trapFocus: true, restoreFocus: true,
    motion: 'slide-right', group: 'cc-primary', closeId: 'cc-drawer-close',
    onOpen: function () {
      var secs = drawer.querySelectorAll('.cc-drawer-sec[open]');
      for (var i = 0; i < secs.length; i++) loadDrawerSection(secs[i]);
    }
  });
  function openDrawer() { if (drawerOv) drawerOv.open(); }
  function closeDrawer() { if (drawerOv) drawerOv.close(); }

  var settingsBtn = document.getElementById('cc-settings-toggle');
  if (settingsBtn) settingsBtn.addEventListener('click', function () {
    if (drawerOv && !drawerOv.isOpen()) openDrawer(); else closeDrawer();
  });

  // ----- Shared ✎ Notes drawer (UX9b) -----
  // One lazy fragment (/api/panel/notes_drawer), re-fetched on EVERY open so
  // the list is always current; the Holding tab's selection scopes it. The
  // quick-add form inside the fragment calls window.ccReloadNotesDrawer after
  // a successful POST /api/notes so the list refreshes in place.
  var notesDrawer = document.getElementById('cc-notes-drawer');
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

  // Same 'cc-primary' group as the settings drawer + palette, so opening any
  // one closes the others (replaces the old closeDrawer()/closePalette() calls).
  var notesOv = notesDrawer && window.CCOverlay.register(notesDrawer, {
    priority: window.CCOverlay.PRIORITY.DRAWER,
    scrim: true, trapFocus: true, restoreFocus: true,
    motion: 'slide-right', group: 'cc-primary', closeId: 'cc-notes-close',
    onOpen: loadNotesDrawer
  });
  function openNotesDrawer() { if (notesOv) notesOv.open(); }
  function closeNotesDrawer() { if (notesOv) notesOv.close(); }

  var notesBtn = document.getElementById('cc-notes-toggle');
  if (notesBtn) notesBtn.addEventListener('click', function () {
    if (notesOv && !notesOv.isOpen()) openNotesDrawer(); else closeNotesDrawer();
  });

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
    // Hand the typed query to the Ask panel: stash it in the store, jump to
    // #explore, and poke any already-loaded panel (it consumes the stash at
    // wire-up when the tab loads lazily — see consumePaletteQuery in
    // explore_panel). The event NAME stays 'cc-ask-q' — that string is the
    // poke contract, not a storage key.
    return function () {
      window.CCState.set('askQ', q);
      location.hash = '#explore';
      window.dispatchEvent(new Event('cc-ask-q'));
    };
  }
  function goView(id) {
    // Saved-view palette pick (UX9b): same stash-and-jump handoff as goAsk,
    // but for a saved view id — the Ask/explore panel loads + runs its chip
    // (see consumePaletteView in explore_panel).
    return function () {
      window.CCState.set('askViewId', String(id));
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
      var it = palMatches[j].it;
      var isSel = j === palSel;
      var optId = 'cc-pal-opt-' + j;
      // Ticker rows render the canonical two-part label (mono symbol +
      // muted name) instead of one concatenated string.
      var labelHtml = it.tick
        ? '<span class="k-tick"><span class="k-tick-sym">' + escHtml(it.tick) + '</span>'
          + (it.name ? '<span class="k-tick-name">' + escHtml(it.name) + '</span>' : '')
          + '</span>'
        : '<span>' + escHtml(it.label) + '</span>';
      html += '<li id="' + optId + '" role="option" aria-selected="' + (isSel ? 'true' : 'false') + '"'
        + ' class="' + (isSel ? 'sel' : '') + '" data-idx="' + j + '">'
        + labelHtml
        + '<span class="cc-pal-hint">' + escHtml(it.hint) + '</span></li>';
    }
    palList.innerHTML = html || '<li class="cc-pal-none">No matches.</li>';
    if (palInput) {
      var selId = palMatches.length ? 'cc-pal-opt-' + palSel : '';
      palInput.setAttribute('aria-activedescendant', selId);
    }
  }

  // Content setup, run on open — CCOverlay shows/hides the dialog, restores
  // focus, and closes the other 'cc-primary' surfaces; this only fills the
  // searchable corpus and focuses the input.
  function fillPalette() {
    if (palInput) palInput.setAttribute('aria-expanded', 'true');
    palInput.value = '';
    palSel = 0;
    palItems = palStatic();
    renderPalette('');
    fetchTickers().then(function (list) {
      list.forEach(function (t) {
        palItems.push({
          // label stays the full searchable text; tick/name drive the
          // two-part rendering in renderPalette.
          label: t.ticker + (t.name ? ' ' + t.name : ''),
          tick: t.ticker,
          name: t.name || '',
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

  var palOv = pal && window.CCOverlay.register(pal, {
    priority: window.CCOverlay.PRIORITY.PALETTE,
    scrim: true, trapFocus: true, restoreFocus: true,
    motion: 'pop', group: 'cc-primary', closeId: 'cc-palette-close',
    autofocus: false,  // fillPalette focuses the input itself
    onOpen: fillPalette,
    onClose: function () {
      if (palInput) palInput.setAttribute('aria-expanded', 'false');
    }
  });
  function openPalette() { if (palOv) palOv.open(); }
  function closePalette() { if (palOv) palOv.close(); }

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

  var peekOv = peek && window.CCOverlay.register(peek, {
    priority: window.CCOverlay.PRIORITY.PEEK,
    scrim: true, trapFocus: true, restoreFocus: true,
    motion: 'rise', closeId: 'cc-peek-close',
    onClose: function () {
      peekBody.innerHTML = '';
      peekFragUrl = null;
      peekSeq++;  // invalidate any in-flight fragment fetch
    }
  });

  function openPeek(fragUrl, opts) {
    if (!peekOv) return;
    opts = opts || {};
    closeHover();
    peekTitle.textContent = opts.title || 'Quick look';
    if (opts.fullHref) { peekFull.href = opts.fullHref; peekFull.hidden = false; }
    else { peekFull.hidden = true; }
    positionPeek(opts.anchor || null);  // place BEFORE showing so it rises in place
    peekOv.open();                       // show + shared scrim + focus the x button
    loadPeek(fragUrl, opts.anchorId || null);
  }
  function closePeek() { if (peekOv) peekOv.close(); }

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

  // ----- Ask-q doorways (Law 2, S9) -----
  // Any datum carrying data-ask-q OUTSIDE the Ask panel (a cockpit KPI chip, a
  // landing stat) is a doorway: a plain left click hands its relative-window
  // question to the Ask panel over the SAME stash-and-jump rail the palette
  // uses (goAsk). data-ask-q is the whole carrier — Ask has no URL for a query,
  // so these are <button>/<a> with data-ask-q and nothing else; there is no
  // middle-click destination to preserve. Phrase the question with period
  // COUNTS ("…, last 12 quarters"), never an ISO date range — the ViewSpec
  // compiler parses counts (nl_compile), and a date range compiles to nothing.
  //
  // Scope: EXCLUDES the Ask panel (data-panel="explore"). The Ask panel wires
  // its OWN data-ask-q example chips (submitAsk — see explore_panel); a
  // shell-level handler over them would double-fire (jump to #explore AND
  // resubmit). One owner per region.
  //
  // Precedence (coordinated with the fact_ref session, directive §6): a cell
  // may carry BOTH data-fact-ref (an exact PK series) and data-ask-q (a looser
  // relative window). data-fact-ref WINS — this handler bails on any element
  // that also carries data-fact-ref, leaving the exact series to the fact_ref
  // handler, so a richer anchor is never overridden by the relative question.
  document.addEventListener('click', function (ev) {
    if (ev.defaultPrevented || ev.button !== 0) return;
    if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
    if (!ev.target.closest) return;
    var a = ev.target.closest('[data-ask-q]');
    if (!a || a.hasAttribute('data-fact-ref')) return;   // fact_ref wins (exact series)
    if (a.closest('[data-panel="explore"]')) return;     // Ask owns its own chips
    ev.preventDefault();
    goAsk(a.getAttribute('data-ask-q'))();
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

  // The peek's x and the shared scrim click-out are wired by CCOverlay
  // (closeId 'cc-peek-close' + the one .k-scrim listener).

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

  // The hover card is a non-modal affordance (no scrim/trap/close button) —
  // register it as an Escape-only dismisser so CCOverlay's one keydown closes
  // it before any modal surface, without a second listener here.
  window.CCOverlay.addPopoverDismisser(function () {
    if (hovercard && !hovercard.hidden) { closeHover(); return true; }
    return false;
  });

  // Ctrl/Cmd+K, plus Ctrl+Space (UX9b) — ev.code so the spacebar binding is
  // keyboard-layout independent. (Escape + Tab/focus-trap are CCOverlay's now;
  // this listener never touches Escape, so the shell keeps exactly one.)
  document.addEventListener('keydown', function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 'k' || ev.key === 'K' || ev.code === 'Space')) {
      ev.preventDefault();
      if (palOv && !palOv.isOpen()) openPalette(); else closePalette();
    }
  });
  // Drawer sections ship collapsed; each remembers its own open/closed state
  // across reloads (store key drawer:<endpoint>; the pre-S14
  // cc-drawer-sec:<endpoint> keys migrate on first read). The toggle
  // handler only fetches while the drawer is VISIBLE — the boot-time
  // restore below also fires toggle events, and openDrawer() already loads
  // whatever is open.
  function drawerSecKey(sec) {
    return 'drawer:' + (sec.getAttribute('data-endpoint') || '');
  }
  if (drawer) {
    var allSecs = drawer.querySelectorAll('.cc-drawer-sec');
    for (var di = 0; di < allSecs.length; di++) {
      allSecs[di].addEventListener('toggle', function (ev) {
        window.CCState.set(drawerSecKey(ev.target), ev.target.open ? '1' : '0');
        if (ev.target.open && !drawer.hidden) loadDrawerSection(ev.target);
      });
      allSecs[di].open = window.CCState.get(drawerSecKey(allSecs[di])) === '1';
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
  // Boot restore (S14 PR2): a hash-less load returns to where you were —
  // same tab-session only (the store's tab/ticker are session-scoped, so a
  // fresh tab still lands on Overview; a mid-work reload doesn't).
  // location.replace keeps the detour out of Back history; the synchronous
  // onHashChange below reads the already-updated hash, and the async
  // hashchange event it also fires re-activates idempotently.
  if (!location.hash) {
    var bootTab = window.CCState.get('tab');
    var bootPanel = bootTab ? panelById(bootTab) : null;
    if (bootPanel && bootTab !== 'overview') {
      var bootTicker = bootPanel.getAttribute('data-picker') === '1'
        ? (window.CCState.get('ticker') || '') : '';
      location.replace('#' + bootTab + (bootTicker ? '=' + encodeURIComponent(bootTicker) : ''));
    }
  }
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
<a class="cc-skip" href="#cc-main">Skip to content</a>
<div id="cc-live" class="cc-sr-only" aria-live="polite" aria-atomic="true"></div>
""".replace("{css}", SHELL_CSS)
)

_DOC_FOOT = "</body></html>"
