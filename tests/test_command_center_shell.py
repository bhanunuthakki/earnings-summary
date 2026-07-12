"""Unit tests for the unified command-center shell renderer.

The shell is a pure function over a pre-built Overview HTML string + the
five-section table (UX redesign PR2); these lock the structural contract
the lazy-loader JS relies on (top-bar section nav + per-section sub-tab
rows, per-panel endpoints, cc-picker on the dropdown-driven Holding tab,
legacy-hash + section-alias redirects, the Ctrl+K palette chrome).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pipeline.cc_state import CC_STATE_JS
from pipeline.command_center_shell import (
    _LEGACY_PANEL_REDIRECTS,  # pyright: ignore[reportPrivateUsage]  # keep-in-sync contract under test
    _SETTINGS_DRAWER_HTML,  # pyright: ignore[reportPrivateUsage]  # collapsed-by-default contract
    _SKELETON_KINDS,  # pyright: ignore[reportPrivateUsage]  # skeleton coverage contract
    _THEMES,  # pyright: ignore[reportPrivateUsage]  # skeleton coverage contract
    SHELL_CSS,
    SHELL_JS,
    render_overview_panel,
    render_shell,
    system_status_summary,
)


def test_overview_panel_is_the_research_cockpit() -> None:
    """Overview = the cockpit + the freshness strip. The IR-KPI + maintenance
    action blocks moved to the settings drawer (P3.4) and must NOT inline."""
    html = render_overview_panel({"portfolio": [], "evaluation": []}, coverage={})
    assert "Portfolio" in html
    assert "Evaluation" in html
    assert "No portfolio tickers." in html
    assert "cockpit-section" in html
    assert 'id="refresh-ir-form"' not in html
    assert "/actions/maintenance" not in html


def test_overview_rail_carries_unread_badge_and_inbox_js() -> None:
    """Inbox v2: the rail header carries the per-surface unread badge and the
    aside embeds INBOX_JS (unread accents; the hover ✓/✕ actions are HTMX now,
    Wave 3b)."""
    html = render_overview_panel(
        {"portfolio": [], "evaluation": []},
        coverage={},
        inbox_html='<div class="ix-stream ix-compact" data-ix-surface="home"></div>',
    )
    assert 'data-ix-badge="home"' in html
    assert "ix-last-seen:" in html  # INBOX_JS inlined with the rail
    assert "IntersectionObserver" in html  # the unread-accent observer rides along
    # No rail → no badge, no script (the panel renders bare).
    bare = render_overview_panel({"portfolio": [], "evaluation": []}, coverage={})
    assert "data-ix-badge" not in bare


def test_overview_main_column_hoists_upcoming_strip_above_cockpit() -> None:
    """PR3 (navigation_ia §4): the retired /digest page's one surviving
    feature — the compact upcoming-earnings strip — is HOISTED from the rail
    into the main column, after the since-last/continue placeholder and
    before the cockpit; the rail keeps only the Inbox."""
    html = render_overview_panel(
        {"portfolio": [], "evaluation": []},
        coverage={},
        inbox_html='<div class="ix-stream ix-compact" data-ix-surface="home"></div>',
        upcoming_html='<div class="up-strip">UP-MARKER</div>',
    )
    assert "UP-MARKER" in html
    assert html.index('id="cc-today-bands"') < html.index('class="up-strip"')
    assert html.index('class="up-strip"') < html.index("cc-cockpit-live")
    # It's in the MAIN column, not the rail.
    main_start = html.index('class="cc-home-main"')
    rail_start = html.index('class="cc-home-rail"')
    assert main_start < html.index('class="up-strip"') < rail_start
    # The rail links to the feed only — the digest link is gone.
    assert 'href="/feed"' in html
    assert "/digest" not in html
    # No strip → no strip markup, rail otherwise intact.
    no_strip = render_overview_panel(
        {"portfolio": [], "evaluation": []},
        coverage={},
        inbox_html='<div class="ix-stream"></div>',
    )
    assert "up-strip" not in no_strip
    assert 'data-ix-badge="home"' in no_strip
    # The strip's CSS ships with the shell stylesheet.
    assert ".up-strip {" in SHELL_CSS


def test_overview_main_column_has_today_bands_placeholder() -> None:
    """The empty ``#cc-today-bands`` placeholder (PR3) always renders in the
    main column, right after open_loops — SHELL_JS fills it client-side
    (since-last fetch + the continue-line) on Overview activation; a
    JS-disabled or pre-migration load costs nothing and reserves no space.
    Present with or without a rail (inbox_html)."""
    html = render_overview_panel(
        {"portfolio": [], "evaluation": []},
        coverage={},
        open_loops_html='<div class="cc-open-loops">LOOP-MARKER</div>',
    )
    assert html.index("LOOP-MARKER") < html.index('id="cc-today-bands"')
    assert html.index('id="cc-today-bands"') < html.index("cc-cockpit-live")
    assert '<div id="cc-today-bands"></div>' in html


def test_render_shell_five_section_structure() -> None:
    html = render_shell(
        overview_html="<div id='ov-marker'>OVERVIEW</div>",
        generated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</body></html>")
    assert "<title>Portfolio · command center</title>" in html
    # Every section keeps its activation contract — four in the primary nav
    # (Today/Companies/Portfolio/Review), System as the utility icon (UX9b) —
    # and every one keeps a sub-tab row. Ask is fully hidden (navigation_ia):
    # no button anywhere (so no data-theme-target), but its sub-row + panel
    # survive, which is what keeps #explore / goAsk / the dock ⇗ working.
    for section in ("home", "companies", "portfolio", "review", "system"):
        assert f'data-theme-target="{section}"' in html
        assert f'data-cc-theme="{section}"' in html  # its sub-tab row exists
    assert 'data-theme-target="ask"' not in html
    assert 'data-cc-theme="ask"' in html
    # The old three-theme ids are gone from the nav.
    for dead in ("research", "governance"):
        assert f'data-theme-target="{dead}"' not in html
    # Surviving sub-tabs, each tagged with its section. System collapsed its
    # 8-tab diagnostics strip into the single "provenance" console (S10).
    for target in (
        "overview",
        "holding",
        "discovery",
        "journal",
        "explore",
        "portfolio",
        "portfolio_synthesis",
        "decisions_record",
        "advisor_memos",
        "holdings",
        "provenance",
    ):
        assert f'data-tab-target="{target}"' in html
    # The collapsed System diagnostics ids are no longer sub-tabs (they alias to
    # the provenance console via _LEGACY_PANEL_REDIRECTS).
    for collapsed in ("section_coverage", "ir_coverage", "source_calls", "validation"):
        assert f'data-tab-target="{collapsed}"' not in html
    for drawered in ("budget", "actions"):
        assert f'data-tab-target="{drawered}"' not in html
    # Overview is inlined verbatim and marked loaded.
    assert "OVERVIEW" in html
    assert 'data-panel="overview" data-loaded="1"' in html
    # Topbar links to the live feed; the retired /digest page is gone from the
    # entire shell document (topbar, rail, palette).
    assert 'href="/feed"' in html
    assert "/digest" not in html


def test_system_demoted_to_utility_icon() -> None:
    """navigation_ia over UX9b: the primary nav carries Today · Companies ·
    Portfolio · Review only; System rides as a top-right icon button that
    keeps the cc-theme-tab / data-theme-target activation contract (its
    sub-tabs render as before once opened) plus a data-pal-label so its
    palette row stays readable; Ask has NO top-bar button at all."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    topnav = html.split('class="cc-topnav"', 1)[1].split("</nav>", 1)[0]
    for primary in ("home", "companies", "portfolio", "review"):
        assert f'data-theme-target="{primary}"' in topnav
    assert 'data-theme-target="system"' not in topnav
    # Ask: hidden from the top bar (_HIDDEN_NAV_SECTIONS) but the section
    # machinery survives — the explore panel + sub-row render, so #explore /
    # goAsk / the dock ⇗ pop-out all still land on the full page.
    assert 'data-theme-target="ask"' not in topnav
    assert 'data-tab-target="explore"' in html
    assert 'data-panel="explore"' in html
    # Labels: Home reads "Today"; the Review section is present.
    assert ">Today</button>" in topnav
    assert ">Review</button>" in topnav
    # The icon button: theme contract intact (cc-theme-tab + cc-system-btn
    # activation hooks preserved), now composing the kit quiet button.
    assert 'class="cc-theme-tab cc-system-btn k-btn k-btn-quiet"' in html
    assert 'data-theme-target="system" data-pal-label="System"' in html
    # The palette JS prefers the readable label over the glyph text.
    assert "data-pal-label" in SHELL_JS
    # System's sub-tab row still exists for when the icon activates it.
    assert 'data-cc-theme="system"' in html


def test_review_section_reparents_the_ritual_surfaces() -> None:
    """navigation_ia §2.1: Ledger + Triage + Journal move out of Companies into
    the Review section — panel ids UNCHANGED (every #musings/#journal/#triage/
    #ledger deep link survives), the Ledger lands the section (first sub-tab,
    deliberately unsplit), and Companies keeps only its company-shaped tabs."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    for pid in ("musings", "triage", "journal"):
        # Each sub-tab renders exactly once, tagged with its new section.
        assert f'data-tab-target="{pid}" data-cc-theme="review"' in html
        assert html.count(f'data-tab-target="{pid}"') == 1
    # Ledger (musings) is the landing sub-tab: first of the review row.
    musings = html.index('data-tab-target="musings"')
    triage = html.index('data-tab-target="triage"')
    journal = html.index('data-tab-target="journal"')
    assert musings < triage < journal
    # Companies keeps exactly its company-shaped sub-tabs.
    for pid in ("holding", "discovery", "diet"):
        assert f'data-tab-target="{pid}" data-cc-theme="companies"' in html


# ---------------------------------------------------------------------------
# System status dot (PR9) — derived from the cheap daily-chain status
# artifact, never the cron_health_panel's heavy multi-day ingestion_runs scan.
# ---------------------------------------------------------------------------


def _write_status(tmp_path: Path, payload: dict[str, object]) -> None:
    status_dir = tmp_path / ".tmp"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "daily_chain_status.json").write_text(json.dumps(payload), encoding="utf-8")


def test_system_status_summary_none_without_repo_root() -> None:
    assert system_status_summary(None) is None


def test_system_status_summary_none_when_artifact_missing(tmp_path: Path) -> None:
    assert system_status_summary(tmp_path) is None


def test_system_status_summary_ok(tmp_path: Path) -> None:
    _write_status(tmp_path, {"verdict": "ok", "runs_today": 1})
    tone, summary = system_status_summary(tmp_path) or (None, None)
    assert tone == "ok"
    assert summary


def test_system_status_summary_missing_verdict_is_bad(tmp_path: Path) -> None:
    _write_status(tmp_path, {"verdict": "missing", "runs_today": 0})
    tone, _summary = system_status_summary(tmp_path) or (None, None)
    assert tone == "bad"


def test_system_status_summary_failed_verdict_is_bad(tmp_path: Path) -> None:
    _write_status(tmp_path, {"verdict": "failed", "error_summary": "kaboom"})
    tone, summary = system_status_summary(tmp_path) or (None, None)
    assert tone == "bad"
    assert "kaboom" in (summary or "")


def test_system_status_summary_db_error_is_warn(tmp_path: Path) -> None:
    _write_status(tmp_path, {"verdict": "db_error", "error": "locked"})
    tone, _summary = system_status_summary(tmp_path) or (None, None)
    assert tone == "warn"


def test_system_status_summary_stale_ok_artifact_is_bad(tmp_path: Path) -> None:
    """A dead verify_daily_chain task must not leave yesterday's green dot up
    forever: an 'ok' artifact whose latest_started_at is older than ~26h reads
    bad, not ok."""
    _write_status(
        tmp_path,
        {"verdict": "ok", "runs_today": 1, "latest_started_at": "2026-06-01 04:00:00"},
    )
    tone, summary = system_status_summary(tmp_path, now=datetime(2026, 6, 3, 8, 0, 0)) or (
        None,
        None,
    )
    assert tone == "bad"
    assert "stale" in (summary or "").lower()


def test_system_status_summary_fresh_ok_artifact_stays_ok(tmp_path: Path) -> None:
    _write_status(
        tmp_path,
        {"verdict": "ok", "runs_today": 1, "latest_started_at": "2026-06-03 04:00:00"},
    )
    tone, _summary = system_status_summary(tmp_path, now=datetime(2026, 6, 3, 8, 0, 0)) or (
        None,
        None,
    )
    assert tone == "ok"


def test_system_status_summary_unparseable_stamp_degrades_to_verdict(tmp_path: Path) -> None:
    """An unreadable stamp can't prove staleness — the verdict alone decides
    (hand-made and pre-stamp artifacts keep their pre-check behavior)."""
    _write_status(tmp_path, {"verdict": "ok", "latest_started_at": "not a date"})
    tone, _summary = system_status_summary(tmp_path, now=datetime(2026, 6, 3, 8, 0, 0)) or (
        None,
        None,
    )
    assert tone == "ok"


def test_system_status_summary_corrupt_json_degrades_to_none(tmp_path: Path) -> None:
    status_dir = tmp_path / ".tmp"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "daily_chain_status.json").write_text("not json", encoding="utf-8")
    assert system_status_summary(tmp_path) is None


def test_system_button_renders_dot_when_status_present() -> None:
    html = render_shell(
        overview_html="x",
        generated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    # No repo_root passed -> no dot ELEMENT (pre-PR9 look preserved; the
    # .cc-system-dot CSS rules are unconditionally in the stylesheet).
    assert '<span class="cc-system-dot' not in html


def test_system_button_dot_wired_through_repo_root(tmp_path: Path) -> None:
    _write_status(tmp_path, {"verdict": "ok", "runs_today": 1})
    html = render_shell(
        overview_html="x",
        generated_at=datetime(2026, 6, 1, tzinfo=UTC),
        repo_root=tmp_path,
    )
    assert 'class="cc-system-dot cc-system-dot-ok"' in html
    assert "Morning pipeline OK" in html


def test_single_tab_sections_suppress_their_sub_row() -> None:
    """Home and Ask have one sub-tab each — the row stays in the DOM (the JS
    derives the active section from the sub-tab's data-cc-theme) but is marked
    data-single so CSS suppresses it. Multi-tab sections must NOT be marked."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-cc-theme="home" data-single="1"' in html
    assert 'data-cc-theme="ask" data-single="1"' in html
    # System collapsed to one Provenance sub-tab (S10) → its row is now single
    # and suppressed too, exactly like Home / Ask.
    assert 'data-cc-theme="system" data-single="1"' in html
    assert 'data-cc-theme="companies" data-single="1"' not in html
    assert 'data-cc-theme="portfolio" data-single="1"' not in html
    # The CSS that suppresses single-tab rows + the [hidden] restatement that
    # keeps inactive rows from stacking (display:flex used to beat [hidden]).
    assert '.cc-subtabs[data-single="1"] { display: none; }' in SHELL_CSS
    assert ".cc-tabs[hidden] { display: none; }" in SHELL_CSS


def test_killed_surfaces_are_out_of_nav_but_redirected() -> None:
    """Pre-reads / Insiders / Predictions / Decisions died as nav surfaces
    (P1.1), budget/actions became drawer sections (P3.4), thesis_ledger folded
    into the Decisions record (P2.2), and the four section names alias to
    their landing panels (PR2); every one must remap client-side."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    for killed in (
        "prereads",
        "insiders",
        "predictions",
        "decisions",
        "thesis_ledger",
        "budget",
        "actions",
    ):
        assert f'data-tab-target="{killed}"' not in html
        assert f'data-panel="{killed}"' not in html
        # The JS REDIRECTS map carries every killed panel.
        assert f"{killed}:" in SHELL_JS.replace("'", "")
    # Python-side map mirrors the JS one (keep-in-sync contract). S10 added the
    # 8 collapsed System diagnostics ids — all aliasing to the provenance console.
    assert set(_LEGACY_PANEL_REDIRECTS) == {
        "prereads",
        "insiders",
        "predictions",
        "decisions",
        "thesis_ledger",
        "budget",
        "actions",
        "home",
        "companies",
        "ask",
        "system",
        "section_coverage",
        "ir_coverage",
        "source_calls",
        "cron_health",
        "dcf_coverage",
        "evals",
        "validation",
        "restatements",
        "model_eval",
        "ledger",
        "triggers",
        "health",
        "review",
    }
    # PR9 — readable ritual-vocabulary aliases (the palette keeps canonical ids).
    assert _LEGACY_PANEL_REDIRECTS["ledger"] == "musings"
    assert _LEGACY_PANEL_REDIRECTS["triggers"] == "holdings"
    assert _LEGACY_PANEL_REDIRECTS["health"] == "provenance"
    assert "ledger:" in SHELL_JS.replace("'", "")
    assert "triggers:" in SHELL_JS.replace("'", "")
    assert "health:" in SHELL_JS.replace("'", "")
    # The collapsed diagnostics ids alias to the one console.
    for collapsed in (
        "section_coverage",
        "ir_coverage",
        "source_calls",
        "cron_health",
        "dcf_coverage",
        "evals",
        "validation",
        "restatements",
    ):
        assert _LEGACY_PANEL_REDIRECTS[collapsed] == "provenance"
        assert f"{collapsed}:" in SHELL_JS.replace("'", "")
    for alias in ("home", "companies", "ask", "system"):
        assert f"{alias}:" in SHELL_JS.replace("'", "")
    for new_home in _LEGACY_PANEL_REDIRECTS.values():
        assert f'data-tab-target="{new_home}"' in html
    # The old decisions/ledger deep-links land on the allocation record.
    assert _LEGACY_PANEL_REDIRECTS["decisions"] == "decisions_record"
    assert _LEGACY_PANEL_REDIRECTS["thesis_ledger"] == "decisions_record"
    # Section aliases land on each section's first panel.
    assert _LEGACY_PANEL_REDIRECTS["home"] == "overview"
    assert _LEGACY_PANEL_REDIRECTS["companies"] == "holding"
    assert _LEGACY_PANEL_REDIRECTS["ask"] == "explore"
    assert _LEGACY_PANEL_REDIRECTS["system"] == "provenance"
    # navigation_ia — the Review section name lands on its Ledger landing.
    assert _LEGACY_PANEL_REDIRECTS["review"] == "musings"
    assert "review:" in SHELL_JS.replace("'", "")
    # The drawer-section legacy ids also auto-open the drawer on arrival.
    assert "DRAWER_OPENERS = { budget: 1, actions: 1 }" in SHELL_JS


def test_render_shell_lazy_endpoints_and_pickers() -> None:
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    # Lazy panels carry their fetch endpoint and start unloaded.
    assert 'data-endpoint="/api/panel/holdings"' in html
    assert 'data-endpoint="/api/panel/provenance"' in html
    assert 'data-loaded="0"' in html
    # The Holding drill-down is ticker-scoped (data-picker routes the hash ticker
    # to loadBody), but the picker UI is now a search combobox INSIDE the holding
    # fragment (UX9c) — there is no longer a cc-picker <select> in shell chrome.
    assert 'data-panel="holding" data-endpoint="/api/panel/holding" data-picker="1"' in html
    assert 'class="cc-picker"' not in html
    assert "data-picker-required" not in html
    # The shell JS + CSS are inlined.
    assert SHELL_JS[:30] in html
    assert SHELL_CSS[:30] in html


def test_holding_open_suppresses_companies_subrow() -> None:
    """UX9c: while a specific holding is open (Holding panel + a ticker), the
    Companies sub-row hides for a clean reading view; it returns on the
    no-ticker state and on Discovery/Journal. The combobox switches holdings."""
    # The activation JS gates the companies sub-row on (holdingOpen && theme).
    assert "panelId === 'holding' && !!ticker" in SHELL_JS
    assert "holdingOpen && theme === 'companies'" in SHELL_JS


def test_settings_drawer_structure() -> None:
    """P3.4: admin is a drawer, not a tab — the topbar toggle, the drawer
    chrome, and the three lazy sections (budget / ticker settings /
    maintenance) reusing the existing panel endpoints."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'id="cc-settings-toggle"' in html
    assert 'id="cc-drawer"' in html
    assert 'id="cc-drawer-close"' in html
    # Drawer sections lazy-load the SAME fragments the old tabs served.
    assert 'class="cc-drawer-sec" data-endpoint="/api/panel/budget"' in html
    assert 'data-endpoint="/api/panel/ticker_settings"' in html
    assert 'data-endpoint="/api/panel/actions"' in html
    # The drawer endpoints are sections, not nav panels.
    assert '<section class="cc-panel" data-panel="budget"' not in html
    assert '<section class="cc-panel" data-panel="actions"' not in html


def test_settings_drawer_sections_collapsed_with_remembered_state() -> None:
    """UI polish: every drawer section ships collapsed (no `open` attribute
    in the drawer markup); SHELL_JS restores and persists each section's
    state per-endpoint through the store (drawer:<endpoint>; the legacy
    cc-drawer-sec:<endpoint> localStorage keys migrate inside cc_state)."""
    assert "<details" in _SETTINGS_DRAWER_HTML
    assert " open " not in _SETTINGS_DRAWER_HTML
    assert "'drawer:' + (sec.getAttribute('data-endpoint')" in SHELL_JS
    assert "window.CCState.set(drawerSecKey(" in SHELL_JS
    assert "window.CCState.get(drawerSecKey(" in SHELL_JS
    assert "cc-drawer-sec:" in CC_STATE_JS  # the legacy name lives in the store


def test_type_scale_tokens_reach_the_shell() -> None:
    """The semantic scale is the shell's only type vocabulary: the headline
    tiers reference tokens and the old hardcoded h1/h2 sizes are gone."""
    assert "--fs-display: 22px" in SHELL_CSS
    assert "h1 { font-size: var(--fs-display)" in SHELL_CSS
    assert "h2 { font-size: var(--fs-title)" in SHELL_CSS
    assert "font-size: 24px" not in SHELL_CSS
    assert "font-size: 18px" not in SHELL_CSS


def test_command_palette_chrome() -> None:
    """PR2: the Ctrl/Cmd+K palette — overlay + input + list in the DOM, the
    topbar trigger button, and the JS wiring constants."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'id="cc-palette"' in html
    assert 'id="cc-palette-input"' in html
    assert 'id="cc-palette-list"' in html
    assert 'id="cc-palette-open"' in html
    assert "openPalette" in SHELL_JS
    assert "metaKey" in SHELL_JS  # Cmd+K works on mac keyboards too


def test_command_palette_hands_query_to_ask() -> None:
    """Ask v2: anything typed into the palette is also offered as an Ask
    question — the entry stashes the query (sessionStorage 'cc-ask-q'),
    jumps to #explore, and pokes a loaded panel via the 'cc-ask-q' event
    (the explore panel consumes the stash at wire-up / on the event)."""
    assert "goAsk" in SHELL_JS
    assert "'cc-ask-q'" in SHELL_JS
    assert "'#explore'" in SHELL_JS
    assert "Ask: " in SHELL_JS  # the dynamic palette entry label


def test_command_palette_ctrl_space_and_grown_corpus() -> None:
    """UX9b: Ctrl+Space joins Ctrl+K as a binding, and the palette corpus grows
    to open journal notes (→ #journal) and saved views (→ the Ask/explore
    builder via the same sessionStorage handoff as goAsk)."""
    # Ctrl+Space — ev.code so the binding is keyboard-layout independent.
    assert "ev.code === 'Space'" in SHELL_JS
    # Corpus growth: open notes + saved views fetched on open.
    assert "'/api/notes?status=open'" in SHELL_JS
    assert "'/api/views'" in SHELL_JS
    assert "goHash('journal')" in SHELL_JS
    # Saved-view pick hands off like goAsk: stash the id, jump, poke the panel.
    assert "goView" in SHELL_JS
    assert "'cc-view-id'" in SHELL_JS


def test_notes_drawer_chrome() -> None:
    """UX9b: the shared ✎ Notes drawer — topbar trigger + drawer chrome in the
    DOM, and the JS that re-fetches /api/panel/notes_drawer on every open with
    the Holding tab's ticker as scope. The quick-add fragment refreshes the
    list through the window.ccReloadNotesDrawer hook."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'id="cc-notes-toggle"' in html
    assert 'id="cc-notes-drawer"' in html
    assert 'id="cc-notes-drawer-body"' in html
    assert 'id="cc-notes-close"' in html
    assert "'/api/panel/notes_drawer'" in SHELL_JS
    assert "ccReloadNotesDrawer" in SHELL_JS
    assert "data-current-ticker" in SHELL_JS  # holding selection scopes the drawer
    # Fragment-side ✎ buttons (e.g. the Holding header's) open the SAME drawer.
    assert "data-cc-notes-open" in SHELL_JS


def test_capture_tray_chrome() -> None:
    """PR3: the Ctrl/Cmd+. global capture tray — overlay markup in the DOM,
    registered with CCOverlay (PALETTE priority, group 'cc-primary' so it
    closes the palette/drawers), and the keybinding extends the ONE existing
    global keydown listener rather than adding a second one."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'id="cc-capture-tray"' in html
    assert 'id="cc-capture-tray-body"' in html
    assert 'id="cc-capture-tray-save"' in html
    assert 'id="cc-capture-tray-close"' in html
    assert 'id="cc-capture-tray-ticker"' in html
    # Registered like the palette: PALETTE priority, shared 'cc-primary' group,
    # both inside the same register() call as the closeId (one contiguous
    # options object, not just present somewhere in the file).
    assert "closeId: 'cc-capture-tray-close'" in SHELL_JS
    assert "window.CCOverlay.PRIORITY.PALETTE" in SHELL_JS
    reg_start = SHELL_JS.index("window.CCOverlay.register(trayEl,")
    tray_reg = SHELL_JS[reg_start : reg_start + 400]
    assert "group: 'cc-primary'" in tray_reg
    assert "closeId: 'cc-capture-tray-close'" in tray_reg
    assert "priority: window.CCOverlay.PRIORITY.PALETTE" in tray_reg
    # POSTs straight to the capture spine — never /api/notes.
    assert "'/api/capture/text'" in SHELL_JS
    # Exactly one document-level keydown listener carries both bindings.
    assert SHELL_JS.count("document.addEventListener('keydown'") == 1
    assert "ev.key === '.'" in SHELL_JS
    # holdingTicker() idiom reused for the ticker prefill — no duplicate scope
    # lookup, and no server-side ticker param on the capture POST body.
    assert SHELL_JS.count("function holdingTicker()") == 1
    assert "trayPrefillTicker" in SHELL_JS


def test_capture_tray_coach_mount_and_renderer() -> None:
    """W2: the tray's entry-coaching mount (pledge_challenge card /
    annotated_decision_id receipt) — a DUPLICATED renderer (not shared) from
    pipeline/ledger_panel.py's _CAPTURE_JS renderCoach, per the repo's
    duplicate-simple-shared-logic preference. A challenge or receipt keeps the
    tray open (only closeTray() on the plain/no-coaching path)."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'id="cc-capture-tray-coach"' in html
    assert "res.pledge_challenge" in SHELL_JS
    assert "res.annotated_decision_id" in SHELL_JS
    assert "function trayRenderCoach" in SHELL_JS
    # Escaped before injection (challenge text carries ** and tickers) and
    # newlines become <br>, not raw markdown.
    assert "escHtml(res.pledge_challenge)" in SHELL_JS
    assert "<br>" in SHELL_JS
    # The annotation tap re-POSTs the SAME capture endpoint.
    assert SHELL_JS.count("'/api/capture/text'") >= 2
    # The receipt doorway is interpolated, never a literal '#dec...' (hex-scan
    # guard — see _TRAY_DECISIONS_HASH).
    assert "/#decisions_record" in SHELL_JS
    assert "__TRAY_DECISIONS_HASH__" not in SHELL_JS
    # Dismiss is plain element removal, not a second CCOverlay-registered
    # surface (it's inline content inside the tray, not a transient one).
    assert "data-tray-coach-dismiss" in SHELL_JS
    # trayCapture() only auto-closes when trayRenderCoach() reports nothing
    # to show — it must gate closeTray() behind the renderer's return value.
    capture_start = SHELL_JS.index("function trayCapture()")
    capture_body = SHELL_JS[capture_start : capture_start + 1200]
    assert "if (trayRenderCoach(res.j)) return;" in capture_body
    assert "closeTray();" in capture_body


def test_capture_tray_never_handles_escape_itself() -> None:
    """Constraint: Escape is CCOverlay's alone. The tray's own JS block must
    not add a local Escape branch (dismissal happens via CCOverlay.register,
    like every other modal surface)."""
    tray_start = SHELL_JS.index("cc-capture-tray")
    tray_block = SHELL_JS[tray_start : tray_start + 3000]
    assert "Escape" not in tray_block


def test_render_shell_overview_not_a_lazy_endpoint() -> None:
    """Overview must be inlined, never assigned a /api/panel/ endpoint."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-endpoint="/api/panel/overview"' not in html


def test_sub_tab_buttons_carry_their_section() -> None:
    """The JS derives the active section from the active sub-tab's
    data-cc-theme — every sub-tab button must carry one."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-tab-target="overview" data-cc-theme="home"' in html
    assert 'data-tab-target="holding" data-cc-theme="companies"' in html
    assert 'data-tab-target="explore" data-cc-theme="ask"' in html
    assert 'data-tab-target="portfolio_synthesis" data-cc-theme="portfolio"' in html
    assert 'data-tab-target="decisions_record" data-cc-theme="portfolio"' in html
    assert 'data-tab-target="provenance" data-cc-theme="system"' in html


def test_synthesis_lands_the_portfolio_section() -> None:
    """navigation_ia §2.1: Synthesis (thesis health + allocation) is the
    Portfolio section's FIRST sub-tab — the landing view. Performance is an
    outcome, not the front door; it sits second, ahead of the record tabs."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-endpoint="/api/panel/portfolio_synthesis"' in html
    # The trailing quote keeps "portfolio" from matching "portfolio_synthesis".
    perf = html.index('data-tab-target="portfolio"')
    synth = html.index('data-tab-target="portfolio_synthesis"')
    decisions = html.index('data-tab-target="decisions_record"')
    assert synth < perf < decisions


def test_risk_subtab_sits_right_after_performance() -> None:
    """L5: the whole-book Risk cockpit is its own lazy sub-tab, grouped with
    Performance (same pillar) — right after it, both behind the Synthesis
    landing tab (navigation_ia §2.1)."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert 'data-endpoint="/api/panel/portfolio_risk"' in html
    assert 'data-tab-target="portfolio_risk" data-cc-theme="portfolio"' in html
    perf = html.index('data-tab-target="portfolio"')
    risk = html.index('data-tab-target="portfolio_risk"')
    synth = html.index('data-tab-target="portfolio_synthesis"')
    assert synth < perf < risk


def test_content_width_is_wide() -> None:
    """PR2: the 1280px cap left ~300px dead gutters per side at 1920 — the
    shell now flows to 1600."""
    assert "max-width: 1600px" in SHELL_CSS
    assert "max-width: 1280px" not in SHELL_CSS


def test_lazy_panels_ship_structured_skeletons() -> None:
    """S14: every lazy panel's placeholder is shaped like its content — the
    kind map covers every lazy sub-tab, the placeholders render (aria-hidden)
    in the initial document, and the generic Loading… stub is gone from
    panel bodies (drawers/peek keep it — small surfaces)."""
    lazy_ids = {t[0] for _tid, _lbl, subs in _THEMES for t in subs if t[0] != "overview"}
    assert set(_SKELETON_KINDS) == lazy_ids
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    # One skeleton per lazy panel, none inside the inlined Overview.
    assert html.count('class="cc-skel"') == len(lazy_ids)
    assert 'aria-hidden="true"' in html
    # Shaped placeholders: the Holding embed, KPI-strip panels, table panels,
    # the Ask builder, and card surfaces each get their own anatomy.
    for kind in ("band", "kpis", "table", "form", "cards"):
        assert f'data-skel="{kind}"' in html
    # No generic loading stub left in any panel body.
    assert '<div class="cc-panel-body"><div class="cc-loading">' not in html
    # Skeleton CSS rides the shell sheet, honoring reduced-motion.
    assert ".cc-skel-row" in SHELL_CSS
    assert "prefers-reduced-motion" in SHELL_CSS


def test_shell_js_swr_cache_and_revalidation() -> None:
    """S14: the loader paints cached fragments instantly and revalidates in
    the background — cc:v1:panel:* entries addressed through CCState.panel
    (PR2 moved the cache into the store), conditional refetch via
    If-None-Match, quiet swap that preserves scroll and never fires while
    the user is typing inside the panel."""
    assert "window.CCState.panel.key" in SHELL_JS
    assert "window.CCState.panel.get" in SHELL_JS
    assert "window.CCState.panel.set" in SHELL_JS
    assert "'If-None-Match'" in SHELL_JS
    assert "304" in SHELL_JS
    # The boot pass captures the server-rendered skeletons for cold re-loads.
    assert "SKEL[p.getAttribute('data-panel')]" in SHELL_JS
    # Quiet-swap guards: active typing + scroll restoration.
    assert "document.activeElement" in SHELL_JS
    assert "window.scrollY" in SHELL_JS


def test_state_store_loads_before_every_other_script() -> None:
    """S14 PR2: window.CCState is shell infrastructure — render_shell inlines
    CC_STATE_JS exactly once, BEFORE the ask dock's IIFE and SHELL_JS (and
    therefore before any lazily-injected fragment script can run)."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert html.count("window.CCState = {") == 1
    store_at = html.index("window.CCState = {")
    assert store_at < html.index('id="ask-dock"')
    assert store_at < html.index(SHELL_JS[:40])


def test_tablist_roving_tabindex_contract(
    generated_at: datetime | None = None,
) -> None:
    """L13: every role="tab" button ships with tabindex="-1" so JS can manage
    the roving tabindex (one tab focusable at a time); activate() sets the
    active button to 0 + arrow-key handlers drive focus. The JS helper names
    and their wiring must be present."""
    from datetime import UTC, datetime

    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    # All tab buttons in the tablist navs must start with tabindex="-1".
    # (The active one is flipped to 0 by activate() on first paint.)
    import re

    tab_buttons = re.findall(r'<button[^>]+role="tab"[^>]*>', html)
    assert tab_buttons, "no role=tab buttons found"
    for btn in tab_buttons:
        assert 'tabindex="-1"' in btn, f"missing tabindex=-1 on: {btn!r}"
    # JS wiring: helper defined, called for both nav containers.
    assert "setupTablistNav" in SHELL_JS
    assert "ArrowRight" in SHELL_JS and "ArrowLeft" in SHELL_JS
    assert "Home" in SHELL_JS and "End" in SHELL_JS
    # activate() updates tabindex as well as aria-selected.
    assert "tabindex" in SHELL_JS


def test_offline_banner_and_retry_button_contract() -> None:
    """L13: an offline banner element exists in the shell and JS wires
    navigator.onLine + online/offline events; every fetch error state
    emits a Retry button that re-invokes the loader."""
    from datetime import UTC, datetime

    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    # Offline banner element present and starts hidden.
    assert 'id="cc-offline-banner"' in html
    assert "hidden" in html.split('id="cc-offline-banner"')[1].split(">")[0]
    # CSS ships the banner style.
    assert ".cc-offline-banner" in SHELL_CSS
    # JS wires the online/offline events and syncs the banner.
    assert "navigator.onLine" in SHELL_JS
    assert "'online'" in SHELL_JS and "'offline'" in SHELL_JS
    # Retry button in error states: class + onclick closure.
    assert "cc-retry-btn" in SHELL_JS
    assert ".cc-retry-btn" in SHELL_CSS


def test_theme_toggle_contract() -> None:
    """L13 PR2: a theme-toggle button exists in the shell chrome; SHELL_CSS
    uses palette_css("paper") so both light and dark tokens are defined
    (light :root default + [data-theme="dark"] override); SHELL_JS reads
    CCState on boot and toggles between 'dark' and 'paper'."""
    from datetime import UTC, datetime

    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    # Toggle button present in the topbar chrome.
    assert 'id="cc-theme-toggle"' in html
    assert ".cc-theme-toggle" in SHELL_CSS
    # CSS ships light :root + dark override (enables actual theme switching).
    assert ':root[data-theme="dark"]' in SHELL_CSS
    # The light tokens are also present (not just dark-only :root).
    assert "color-scheme: light" in SHELL_CSS
    # JS reads stored theme on boot and persists on click.
    assert "CCState.get('theme')" in SHELL_JS
    assert "CCState.set('theme'" in SHELL_JS
    assert "applyTheme" in SHELL_JS
    # Toggle cycles between dark and paper only.
    assert "THEMES_CYCLE" in SHELL_JS
    assert "'dark'" in SHELL_JS and "'paper'" in SHELL_JS


def test_shell_routes_state_through_the_store() -> None:
    """S14 PR2: the shell's own state writes go through CCState — palette
    handoffs, section/tab/ticker tracking — and no raw storage calls remain
    in SHELL_JS outside the store itself."""
    assert "window.CCState.set('askQ', q)" in SHELL_JS
    assert "window.CCState.set('askViewId', String(id))" in SHELL_JS
    assert "window.CCState.set('section', activeTheme)" in SHELL_JS
    assert "window.CCState.set('tab', panelId)" in SHELL_JS
    assert "window.CCState.set('ticker', ticker)" in SHELL_JS
    assert "sessionStorage" not in SHELL_JS
    assert "localStorage" not in SHELL_JS


def test_hashless_boot_restores_last_tab_within_the_session() -> None:
    """S14 PR2: a reload with no hash returns to the stored tab (+ ticker for
    picker panels) via location.replace — sessionStorage scoping means a
    fresh tab still lands on Overview."""
    assert "if (!location.hash)" in SHELL_JS
    assert "window.CCState.get('tab')" in SHELL_JS
    assert "window.CCState.get('ticker')" in SHELL_JS
    assert "location.replace('#' + bootTab" in SHELL_JS
    # Overview is the no-state default, never a forced redirect target.
    assert "bootTab !== 'overview'" in SHELL_JS


def test_shell_js_prefetch_and_warm_start() -> None:
    """S14: hover on a section/sub-tab button warms its landing panel; after
    first paint an idle pass warms Portfolio's landing (Synthesis — the
    tracker round-trip) and Ask. In-flight fetches are shared so activation
    never double-fetches."""
    assert "function prefetchPanel" in SHELL_JS
    assert "'.cc-theme-tab, .cc-tab[data-tab-target]'" in SHELL_JS
    assert "WARM_PANELS = ['portfolio_synthesis', 'explore']" in SHELL_JS
    assert "requestIdleCallback" in SHELL_JS
    assert "INFLIGHT" in SHELL_JS


def test_shell_js_instruments_panel_timings() -> None:
    """S14: each activation/refresh records which path served it + the
    fetch/render split, ringed on window.CCPerf and POSTed fire-and-forget
    to /api/metrics/panel (read back in System → Data Cache)."""
    assert "window.CCPerf" in SHELL_JS
    assert "'/api/metrics/panel'" in SHELL_JS
    assert "keepalive: true" in SHELL_JS
    for mode in ("'cold'", "'swr'", "'prefetch'", "'revalidate'"):
        assert mode in SHELL_JS


def test_ask_dock_is_persistent_shell_chrome() -> None:
    """Ask v5: the dock is shell chrome — rendered once OUTSIDE .cc-panels so
    it survives panel swaps — with the explicit state controls, the split-view
    reflow hook, and the mode/thread persistence keys wired."""
    html = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert html.count('id="ask-dock"') == 1
    # Outside the panel-swap container: the dock markup sits after </main>.
    assert html.index('id="ask-dock"') > html.index("</main>")
    # Header controls: ▁ minimize, ◫ split, ⇗ pop-out to the Ask tab.
    for control in ('id="ask-dock-min"', 'id="ask-dock-split"', 'id="ask-dock-pop"'):
        assert control in html
    # Split view reflows the panels beside the dock column.
    assert 'body[data-ask-split="1"] .cc-panels' in html
    # Mode persists in localStorage; the thread tail in sessionStorage.
    assert "askDockMode" in html
    assert "cc-ask-dock-tail" in html


def test_today_bands_js_wiring() -> None:
    """PR3 (navigation_ia §4): SHELL_JS's single ``activate()`` choke-point
    records "Continue where you left off" (every non-Overview activation
    writes ``lastContext`` through CCState — never raw storage, preserving
    the "SHELL_JS never touches localStorage/sessionStorage directly"
    invariant); TODAY_BANDS_JS (inlined with the Overview fragment, since its
    DOM persists across tab switches and a one-shot script would never
    re-check) owns the ``ix-last-seen:overview`` stamp + the since-last
    fetch, re-triggered on every reveal via IntersectionObserver — the same
    idiom INBOX_JS uses for its own per-surface unread mark."""
    assert "window.CCState.setJSON('lastContext'" in SHELL_JS
    assert "if (panelId !== 'overview') {" in SHELL_JS
    assert "localStorage" not in SHELL_JS  # the invariant this PR must preserve

    from pipeline.command_center_shell import TODAY_BANDS_JS

    assert "'ix-last-seen:overview'" in TODAY_BANDS_JS
    assert "'/api/panel/since_last?since='" in TODAY_BANDS_JS
    assert "IntersectionObserver" in TODAY_BANDS_JS
    assert "getElementById('cc-today-bands')" in TODAY_BANDS_JS
    assert "window.CCState.getJSON('lastContext')" in TODAY_BANDS_JS
    html = render_overview_panel({"portfolio": [], "evaluation": []}, coverage={})
    assert TODAY_BANDS_JS in html
    assert html.index('id="cc-today-bands"') < html.index(TODAY_BANDS_JS[:40])
    # lastContext is a namespaced, LOCAL (cross-session) CCState key — the
    # KEYS contract, not a raw ad-hoc localStorage call.
    assert "lastContext:  { store: 'local' }" in CC_STATE_JS
