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
    assert (
        'hx-get="/api/panel/portfolio_risk" hx-trigger="revealed" hx-swap="outerHTML"' in html
    )
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

    def _fake_analytics(**kwargs: object) -> PortfolioAnalytics:
        calls.append("analytics")
        return PortfolioAnalytics(available=False, api_url="http://x", errors={"performance": "e"})

    def _fake_live(**kwargs: object) -> LivePortfolio:
        calls.append("live")
        return LivePortfolio(available=False, api_url="http://x", error="e")

    monkeypatch.setattr(pp, "fetch_portfolio_analytics", _fake_analytics)
    monkeypatch.setattr(pp, "fetch_live_portfolio", _fake_live)
    pp.render_portfolio_panel(db_path=None)
    assert calls == ["analytics", "live"]


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
