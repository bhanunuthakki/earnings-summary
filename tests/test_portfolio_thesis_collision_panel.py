"""The Risk tab's thesis-collision section — markup contract + both page
branches (tracker up AND down; the substrate is the cached DB artifact, so
the section renders in each)."""

from __future__ import annotations

from integrations.portfolio_tracker_client import PortfolioAnalytics
from pipeline.portfolio_panel import compose_risk_page
from thesis_collision import CachedReport, CollisionReport, SharedDriverCluster, ThesisContradiction


def _report() -> CollisionReport:
    return CollisionReport(
        clusters=(
            SharedDriverCluster(
                tickers=("MELI", "NU"),
                driver="Brazilian consumer credit cycle",
                rationale="Both theses lean on LatAm credit normalizing.",
            ),
        ),
        contradictions=(
            ThesisContradiction(
                tickers=("META", "GOOGL"),
                contradiction="Opposite bets on ad-spend share shift",
                rationale="META's thesis assumes share gain at GOOGL's expense.",
            ),
        ),
        tickers_analyzed=("GOOGL", "META", "MELI", "NU"),
    )


def _cached() -> CachedReport:
    return CachedReport(report=_report(), generated_at="2026-07-02T10:00:00")


def _page(analytics: PortfolioAnalytics, cached: CachedReport | None) -> str:
    return compose_risk_page(
        analytics, drawdown=None, factor=None, scenarios=[], digest="", collision=cached
    )


def test_thesis_collision_section_renders_findings() -> None:
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), _cached())
    assert "Thesis collisions" in html
    assert "MELI" in html and "NU" in html
    assert "Brazilian consumer credit cycle" in html
    assert "Both theses lean on LatAm credit normalizing." in html
    assert "META" in html and "GOOGL" in html
    assert "Opposite bets on ad-spend share shift" in html
    assert "4 names analyzed" in html
    assert "1 shared-driver" in html
    assert "1 contradictions" in html


def test_thesis_collision_section_empty_state_names_the_refresh_command() -> None:
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), None)
    assert "Thesis collisions" in html
    assert "run_thesis_collision.py" in html


def test_thesis_collision_section_clean_book_reads_no_findings() -> None:
    cached = CachedReport(
        report=CollisionReport(tickers_analyzed=("AAA", "BBB")),
        generated_at="2026-07-02T10:00:00",
    )
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), cached)
    assert "No shared-driver clusters or contradictions found across 2 names" in html


def test_thesis_collision_section_renders_offline() -> None:
    offline = PortfolioAnalytics(
        available=False, api_url="http://x", errors={"performance": "refused"}
    )
    html = _page(offline, _cached())
    assert "live portfolio tracker" in html  # the offline note still leads
    assert "Thesis collisions" in html and "Brazilian consumer credit cycle" in html
