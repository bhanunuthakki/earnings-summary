"""Red-team wave B (B4/B5): composite-console latency + memo duplication.

B4a — the Portfolio composites defer their heavy tail: Health's Risk +
Red Team and Allocation's Performance render as on-reveal HTMX placeholders
(the per-builder ``/api/panel/<id>`` routes were deliberately kept live), so
the landing section paints first. The ``csec-*`` anchor wrappers must survive
for the jump chips.

B4b — ONE cheap liveness probe gates the tracker data walk: when the probe
says the host is down, the serial data GETs are skipped entirely and the
existing offline banner renders immediately.

B5 — Health's Synthesis section shows headline + a "full memo →" doorway
(``#advisor_memos`` → Record's Memos section via the shell ANCHORS mechanism)
instead of duplicating the full cross-portfolio memo.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import pipeline.analytical_dashboard as ad
import pipeline.portfolio_panel as pp
from integrations.portfolio_tracker_client import LivePortfolio, PortfolioAnalytics
from pipeline.analytical_dashboard import AnalyticalDashboard
from pipeline.portfolio_console_panel import (
    render_portfolio_allocation_panel,
    render_portfolio_health_panel,
)

_DOWN = (False, "http://127.0.0.1:8000")


@pytest.fixture
def probe_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "probe_tracker", lambda api_url=None: _DOWN)


# --------------------------------------------------------------------------- #
# B4a — lazy composite sections
# --------------------------------------------------------------------------- #


def test_health_console_defers_risk_and_red_team(tmp_path: Path, probe_down: None) -> None:
    html = render_portfolio_health_panel(tmp_path / "missing.db")
    # The anchor wrappers stay (jump chips target csec-*; the placeholder swap
    # is outerHTML on the INNER div only).
    assert 'id="csec-risk"' in html and 'id="csec-red_team"' in html
    assert 'data-console-jump="csec-risk"' in html
    # Heavy builders are on-reveal placeholders, not inline sections.
    assert 'hx-get="/api/panel/portfolio_risk" hx-trigger="revealed" hx-swap="outerHTML"' in html
    assert 'hx-get="/api/panel/red_team" hx-trigger="revealed" hx-swap="outerHTML"' in html
    assert html.count('class="cc-loading"') == 2
    # The risk builder itself never ran inline (its signature content is absent).
    assert "Whole-book macro stress" not in html


def test_allocation_console_defers_performance(tmp_path: Path, probe_down: None) -> None:
    html = render_portfolio_allocation_panel(tmp_path / "missing.db")
    assert 'id="csec-positioning"' in html and 'id="csec-performance"' in html
    assert 'hx-get="/api/panel/portfolio" hx-trigger="revealed" hx-swap="outerHTML"' in html
    assert html.count('class="cc-loading"') == 1


# --------------------------------------------------------------------------- #
# B4b — the liveness probe gates the data walk
# --------------------------------------------------------------------------- #


def _boom(*args: object, **kwargs: object) -> object:
    raise AssertionError("tracker data fetcher called despite a down probe")


def test_probe_down_performance_banners_without_calling_fetchers(
    monkeypatch: pytest.MonkeyPatch, probe_down: None
) -> None:
    monkeypatch.setattr(pp, "fetch_portfolio_analytics", _boom)
    monkeypatch.setattr(pp, "fetch_live_portfolio", _boom)
    html = pp.render_portfolio_panel(db_path=None)
    assert "pf-live-offline" in html  # the existing offline banner leads
    assert "liveness probe failed" in html


def test_probe_down_synthesis_banners_without_calling_live_fetch(
    monkeypatch: pytest.MonkeyPatch, probe_down: None, tmp_path: Path
) -> None:
    monkeypatch.setattr(pp, "fetch_live_portfolio", _boom)
    html = pp.render_portfolio_synthesis_panel(tmp_path / "missing.db")
    assert "pf-live-offline" in html
    assert 'data-refresh-endpoint="/api/panel/portfolio_synthesis"' in html


def test_probe_down_risk_panel_degrades_without_calling_analytics(
    monkeypatch: pytest.MonkeyPatch, probe_down: None
) -> None:
    monkeypatch.setattr(pp, "fetch_portfolio_analytics", _boom)
    html = pp.render_portfolio_risk_panel(db_path=None)
    # The tracker-fed sections degrade to the offline note; macro stress stays.
    assert "Whole-book macro stress" in html


def test_probe_up_still_uses_the_fetchers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "probe_tracker", lambda api_url=None: (True, "http://x"))
    calls: list[str] = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    def _mark_active(name: str) -> None:
        nonlocal active, max_active
        with lock:
            calls.append(name)
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.04)
        with lock:
            active -= 1

    def _fake_analytics(**kwargs: object) -> PortfolioAnalytics:
        _mark_active("analytics")
        return PortfolioAnalytics(available=False, api_url="http://x", errors={"performance": "e"})

    def _fake_live(**kwargs: object) -> LivePortfolio:
        _mark_active("live")
        return LivePortfolio(available=False, api_url="http://x", error="e")

    monkeypatch.setattr(pp, "fetch_portfolio_analytics", _fake_analytics)
    monkeypatch.setattr(pp, "fetch_live_portfolio", _fake_live)
    pp.render_portfolio_panel(db_path=None)
    # Performance is now a compact benchmark surface. The probe supplies its
    # availability gate; holdings/transactions belong to Positioning and are
    # not fetched again here.
    assert calls == ["analytics"]


# --------------------------------------------------------------------------- #
# B5 — the Health synthesis memo becomes headline + doorway
# --------------------------------------------------------------------------- #

_MEMO_MD = (
    "## Cross-portfolio synthesis\n"
    "The book is overweight LatAm fintech against one rate regime.\n"
    "\n"
    "Second paragraph carrying the full argument that must NOT render in Health.\n"
)


def test_health_synthesis_shows_headline_and_doorway_not_full_memo(
    monkeypatch: pytest.MonkeyPatch, probe_down: None, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        ad,
        "build_analytical_dashboard",
        lambda db_path, sections=None, **kw: AnalyticalDashboard(portfolio_synthesis_md=_MEMO_MD),
    )
    html = pp.render_portfolio_synthesis_panel(tmp_path / "missing.db")
    # Headline (first substantive prose line) + the doorway to Record → Memos.
    assert "The book is overweight LatAm fintech against one rate regime." in html
    assert 'href="#advisor_memos"' in html
    assert "full memo →" in html
    # The full body never renders here — it lives in Record's Memos section.
    assert "Second paragraph carrying the full argument" not in html


def test_health_synthesis_without_cached_memo_keeps_the_run_hint(
    monkeypatch: pytest.MonkeyPatch, probe_down: None, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        ad,
        "build_analytical_dashboard",
        lambda db_path, sections=None, **kw: AnalyticalDashboard(),
    )
    html = pp.render_portfolio_synthesis_panel(tmp_path / "missing.db")
    assert "No cross-portfolio synthesis cached" in html
    assert "full memo →" not in html


# --------------------------------------------------------------------------- #
# Wave 1 (surface_density_jit_redesign.md D1): the consoles are a brief + one
# dense tile grid, and the What-if signpost section is gone.
# --------------------------------------------------------------------------- #


def test_allocation_console_is_brief_plus_grid(tmp_path: Path, probe_down: None) -> None:
    html = render_portfolio_allocation_panel(tmp_path / "missing.db")
    # Band 1: the read leads, then ONE grid wraps the tiles.
    assert 'class="console-grid"' in html
    assert 'id="csec-brief"' in html
    assert html.index('id="csec-brief"') < html.index('id="csec-risk_budget"')
    # Wide spans: brief + landing recommendation; Risk Budget / Posture /
    # Positioning are tiles (no csec-wide on their wrappers).
    assert 'class="console-sec csec-wide" id="csec-brief"' in html
    assert 'class="console-sec csec-wide" id="csec-allocation_recommendation"' in html
    assert 'class="console-sec" id="csec-risk_budget"' in html
    assert 'class="console-sec" id="csec-positioning"' in html
    # The benchmark answer leads before the recommendation and target-setting
    # detail instead of sitting below the fold.
    assert html.index('id="csec-performance"') < html.index('id="csec-allocation_recommendation"')


def test_allocation_console_whatif_signpost_is_gone(tmp_path: Path, probe_down: None) -> None:
    """D1 Band-3 rule: a section may never exist solely to say where its
    functionality lives. The signpost is dead; the brief carries a What-if
    chip that jumps to the Next dollar tile (which owns the actions)."""
    html = render_portfolio_allocation_panel(tmp_path / "missing.db")
    assert "csec-whatif_pointer" not in html
    assert "Simulate a weight change or compare candidates" not in html
    assert 'data-console-jump="csec-allocation_recommendation">What-if / Compare</button>' in html


def test_record_console_is_brief_plus_grid(tmp_path: Path, probe_down: None) -> None:
    from pipeline.portfolio_console_panel import render_portfolio_record_panel

    html = render_portfolio_record_panel(tmp_path / "missing.db")
    assert 'class="console-grid"' in html
    assert 'class="console-sec csec-wide" id="csec-brief"' in html
    assert 'class="console-sec csec-wide" id="csec-decisions"' in html
    # Memos + Triggers sit side-by-side as tiles.
    assert 'class="console-sec" id="csec-memos"' in html
    assert 'class="console-sec" id="csec-triggers"' in html


def test_briefs_degrade_to_quiet_line_on_missing_db(tmp_path: Path, probe_down: None) -> None:
    """D4: an empty read states itself in one line — it never blanks the
    console or leaks a traceback."""
    from pipeline.portfolio_console_panel import render_portfolio_record_panel

    # Allocation still has one true fact on an empty DB — no next-dollar
    # artifact exists — so its read states that (with the doorway chip)
    # rather than the generic quiet line.
    alloc = render_portfolio_allocation_panel(tmp_path / "missing.db")
    assert 'class="panel console-brief"' in alloc
    assert "No current next-dollar recommendation" in alloc
    assert "Traceback" not in alloc
    # Record has nothing to say → the one-line quiet state, never a blank.
    record = render_portfolio_record_panel(tmp_path / "missing.db")
    assert 'class="panel console-brief"' in record
    assert "Not enough live data for a read yet" in record
    assert "Traceback" not in record


def test_health_console_unchanged_by_grid_mode(tmp_path: Path, probe_down: None) -> None:
    """Health keeps the stacked layout until the Wave-1 pattern is reviewed —
    grid mode is opt-in per console, not a scaffold-wide flip."""
    html = render_portfolio_health_panel(tmp_path / "missing.db")
    assert 'class="console-grid"' not in html


# --------------------------------------------------------------------------- #
# Wave 4: the Band-1 read propagates to Health (grid stays a documented
# exception — its children are composite full-width surfaces).
# --------------------------------------------------------------------------- #


def test_health_console_gets_the_brief_but_not_the_grid(tmp_path: Path, probe_down: None) -> None:
    html = render_portfolio_health_panel(tmp_path / "missing.db")
    assert 'id="csec-brief"' in html
    assert 'class="panel console-brief"' in html
    assert html.index('id="csec-brief"') < html.index('id="csec-synthesis"')
    # Documented exception: no tile grid on Health.
    assert 'class="console-grid"' not in html
    # Lazy tail untouched.
    assert html.count('class="cc-loading"') == 2


def test_health_brief_counts_real_theses_only(tmp_path: Path, probe_down: None) -> None:
    """The thesis-health line uses the non-stub predicate — bulk-onboarded
    STUB rows must not read as phantom breaches."""
    import sqlite3 as _sq

    db = tmp_path / "health.db"
    conn = _sq.connect(str(db))
    conn.executescript(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT);"
        "CREATE TABLE thesis_state (ticker TEXT, thesis TEXT, breach_status TEXT);"
    )
    rows = [
        ("NU", "portfolio", "Real thesis.", "ok"),
        ("MELI", "portfolio", "Real thesis two.", "breach"),
        ("STB", "evaluation", "STUB: needs user-authored thesis", "breach"),
        ("WCH", "watchlist", "Watchlist thesis (out of scope).", "breach"),
    ]
    for t, lt, thesis, status in rows:
        conn.execute("INSERT INTO tracked_companies (ticker, list_type) VALUES (?, ?)", (t, lt))
        conn.execute(
            "INSERT INTO thesis_state (ticker, thesis, breach_status) VALUES (?, ?, ?)",
            (t, thesis, status),
        )
    conn.commit()
    conn.close()

    html = render_portfolio_health_panel(db)
    assert "Thesis health needs eyes:" in html
    assert "1 breach" in html  # MELI only — STB (stub) and WCH (watchlist) excluded
    assert "1 ok" in html
