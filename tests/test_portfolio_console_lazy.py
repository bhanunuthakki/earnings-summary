"""Red-team wave B (B4/B5): composite-console latency + memo duplication.

B4a — Health's Risk + Red Team panes defer until first activation. Allocation
is already an on-demand Work OS route, so its Performance subsection must be
fully rendered in the same request rather than depending on an absent HTMX
runtime. The ``csec-*`` anchor wrappers must survive for the jump chips.

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
from pipeline.console_scaffold import ConsoleSection
from pipeline.portfolio_console_panel import (
    render_portfolio_record_panel,
)

_DOWN = (False, "http://127.0.0.1:8000")


@pytest.fixture
def probe_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def probe(api_url: str | None = None) -> tuple[bool, str]:
        return _DOWN

    monkeypatch.setattr(pp, "probe_tracker", probe)


# --------------------------------------------------------------------------- #
# B4a — lazy composite sections
# --------------------------------------------------------------------------- #


def test_console_grid_uses_registry_large_card_track(tmp_path: Path, probe_down: None) -> None:
    html = render_portfolio_record_panel(tmp_path / "missing.db")
    compact = " ".join(html.split())
    assert "grid-template-columns:repeat(auto-fit,minmax(var(--grid-card-lg),1fr))" in compact


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
    def probe(api_url: str | None = None) -> tuple[bool, str]:
        return True, "http://x"

    monkeypatch.setattr(pp, "probe_tracker", probe)
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
    def dashboard(db_path: Path, sections: object = None, **kwargs: object) -> AnalyticalDashboard:
        return AnalyticalDashboard(portfolio_synthesis_md=_MEMO_MD)

    monkeypatch.setattr(
        ad,
        "build_analytical_dashboard",
        dashboard,
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
    def dashboard(db_path: Path, sections: object = None, **kwargs: object) -> AnalyticalDashboard:
        return AnalyticalDashboard()

    monkeypatch.setattr(
        ad,
        "build_analytical_dashboard",
        dashboard,
    )
    html = pp.render_portfolio_synthesis_panel(tmp_path / "missing.db")
    assert "No cross-portfolio synthesis cached" in html
    assert "full memo →" not in html


# --------------------------------------------------------------------------- #
# Wave 1 (surface_density_jit_redesign.md D1): the consoles are a brief + one
# dense tile grid, and the What-if signpost section is gone.
# --------------------------------------------------------------------------- #


def test_record_console_is_brief_plus_grid(tmp_path: Path, probe_down: None) -> None:
    from pipeline.portfolio_console_panel import render_portfolio_record_panel

    html = render_portfolio_record_panel(tmp_path / "missing.db")
    assert 'class="console-grid"' in html
    assert 'class="console-sec csec-wide k-card k-card-section" id="csec-brief"' in html
    assert 'class="console-sec csec-wide k-card k-card-section" id="csec-decisions"' in html
    assert 'class="console-sec k-card k-card-section" id="csec-triggers"' in html


def test_briefs_degrade_to_quiet_line_on_missing_db(tmp_path: Path, probe_down: None) -> None:
    """D4: an empty read states itself in one line — it never blanks the
    console or leaks a traceback."""
    from pipeline.portfolio_console_panel import render_portfolio_record_panel

    # Record has nothing to say → the one-line quiet state, never a blank.
    record = render_portfolio_record_panel(tmp_path / "missing.db")
    assert 'class="console-brief"' in record
    assert 'class="panel console-brief"' not in record
    assert "Not enough live data for a read yet" in record
    assert "Traceback" not in record


# --------------------------------------------------------------------------- #
# Health redesign (2026-07-30): the brief leads the two-card grid, and the
# chip fragments each render standalone (with their own CSS block).
# --------------------------------------------------------------------------- #


def test_health_fragment_thesis_quiet_plus_memo_doorway(
    monkeypatch: pytest.MonkeyPatch, probe_down: None, tmp_path: Path
) -> None:
    def _fake_dash(db_path: Path, sections: object = None, **kw: object) -> AnalyticalDashboard:
        return AnalyticalDashboard(portfolio_synthesis_md=_MEMO_MD)

    monkeypatch.setattr(ad, "build_analytical_dashboard", _fake_dash)
    html = pp.render_health_fragment(tmp_path / "missing.db", "thesis")
    assert "No evaluated theses yet." in html
    assert "The book is overweight LatAm fintech against one rate regime." in html
    assert 'href="#advisor_memos"' in html


def test_health_fragment_drawdown_degrades_without_tracker_or_snapshot(
    monkeypatch: pytest.MonkeyPatch, probe_down: None, tmp_path: Path
) -> None:
    monkeypatch.setattr(pp, "fetch_portfolio_analytics", _boom)
    html = pp.render_health_fragment(tmp_path / "missing.db", "drawdown")
    assert "Risk &amp; drawdown" in html  # the offline note, never a crash


def test_health_fragment_crowding_and_tail_empty_states(tmp_path: Path, probe_down: None) -> None:
    crowding = pp.render_health_fragment(tmp_path / "missing.db", "crowding")
    assert "Holdings correlation &amp; crowding" in crowding
    tail = pp.render_health_fragment(tmp_path / "missing.db", "tail")
    assert "Scenario-tail stress" in tail
    assert "Tail risk (Monte Carlo)" in tail
    # The macro-stress lens is NOT part of any Health fragment.
    assert "Whole-book macro stress" not in tail


def test_health_fragment_unknown_key_is_a_quiet_note(tmp_path: Path) -> None:
    html = pp.render_health_fragment(tmp_path / "missing.db", "nope")
    assert "Unknown Health fragment" in html
    assert "Traceback" not in html


def test_lazy_record_shell_defers_every_builder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import pipeline.portfolio_console_panel as console

    def blocked() -> str:
        raise AssertionError("The audit shell must not execute builders")

    def sections(_db: Path, _user: str) -> list[ConsoleSection]:
        return [("decisions", "Decisions", blocked), ("brief", "Read", blocked)]

    monkeypatch.setattr(
        console,
        "_record_sections",
        sections,
    )
    html = console.render_portfolio_record_panel(tmp_path / "unused.db", lazy=True)
    assert "fragment=decisions" in html
    assert "fragment=brief" in html
    assert "Loading Decisions" in html
