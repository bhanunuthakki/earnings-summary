"""The Risk tab's style-factor section — markup contract + both page branches
(tracker up AND down; the substrate is local disk, so the section renders in
each)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from integrations.portfolio_tracker_client import LivePortfolio, PortfolioAnalytics
from pipeline.portfolio_panel import compose_risk_page, compose_synthesis_page
from portfolio_risk import CrowdedName
from portfolio_style_factors import StyleFactorLeg, StyleFactorRollup


def _page(analytics: PortfolioAnalytics, style: StyleFactorRollup | None) -> str:
    return compose_risk_page(
        analytics, drawdown=None, factor=None, scenarios=[], digest="", style=style
    )


def _rollup() -> StyleFactorRollup:
    return StyleFactorRollup(
        legs=[
            StyleFactorLeg(
                key="value",
                label="Value",
                spread_label="VTV - VUG",
                book_beta=0.42,
                names_priced=9,
                top=[CrowdedName(ticker="BN", weight_pct=12.0, loading=1.1)],
            ),
            StyleFactorLeg(
                key="size",
                label="Size (small)",
                spread_label="IWM - SPY",
                book_beta=-0.15,
                names_priced=9,
                top=[],
            ),
            StyleFactorLeg(
                key="momentum",
                label="Momentum",
                spread_label="MTUM - SPY",
                book_beta=None,  # no estimate — card hidden, leg still listed
                names_priced=0,
                top=[],
            ),
        ],
        names_total=11,
        lookback_obs=252,
        proxies_through=date(2026, 7, 1),
        missing_proxies=["MTUM"],
    )


def test_style_section_cards_coverage_and_missing_proxy_hint() -> None:
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), _rollup())
    assert "Style factor loadings" in html
    assert "+0.42" in html and "VTV - VUG" in html
    assert "-0.15" in html and "IWM - SPY" in html
    assert "9 of 11 names priced" in html
    assert "proxies through 2026-07-01" in html
    assert "252d window" in html
    assert "Missing proxy series: MTUM" in html
    assert "Largest value tilt" in html and "BN" in html
    # The estimate-less momentum leg contributes no KPI card.
    assert "Momentum β" not in html


def test_style_section_empty_state_names_the_refresh_command() -> None:
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), None)
    assert "Style factor loadings" in html
    assert "fetch_factor_proxies.py" in html


def test_compose_risk_page_renders_style_in_both_branches() -> None:
    html_up = _page(PortfolioAnalytics(available=True, api_url="http://x"), _rollup())
    assert "Style factor loadings" in html_up and "+0.42" in html_up

    offline = PortfolioAnalytics(
        available=False, api_url="http://x", errors={"performance": "refused"}
    )
    html_down = _page(offline, _rollup())
    # The offline note leads, but the local-substrate style read still renders.
    assert "live portfolio tracker" in html_down
    assert "Style factor loadings" in html_down and "+0.42" in html_down


def test_synthesis_insights_use_registry_large_card_track(tmp_path: Path) -> None:
    html = compose_synthesis_page(
        tmp_path / "missing.db",
        LivePortfolio(available=False, api_url="http://x", error="offline"),
        "",
    )
    compact = " ".join(html.split())
    assert "grid-template-columns:repeat(auto-fit,minmax(var(--grid-card-lg),1fr))" in compact
