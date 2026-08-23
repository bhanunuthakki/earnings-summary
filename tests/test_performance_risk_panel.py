"""Read-only unified Performance & Risk composition."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

import pipeline.performance_risk_panel as panel
from integrations.portfolio_allocation import (
    PortfolioAllocationBucket,
    PortfolioAllocationBuckets,
    PortfolioAllocationProjection,
    PortfolioAllocationReconciliation,
)


def _allocation(
    *, state: Literal["available", "incomplete", "unavailable"] = "available"
) -> PortfolioAllocationProjection:
    buckets = PortfolioAllocationBuckets(
        us_equity=PortfolioAllocationBucket(value=Decimal("41"), weight_pct=Decimal("41")),
        international_equity=PortfolioAllocationBucket(
            value=Decimal("20"), weight_pct=Decimal("20")
        ),
        us_etf=PortfolioAllocationBucket(value=Decimal("18"), weight_pct=Decimal("18")),
        international_etf=PortfolioAllocationBucket(value=Decimal("7"), weight_pct=Decimal("7")),
        cash=PortfolioAllocationBucket(value=Decimal("14"), weight_pct=Decimal("14")),
        unclassified=PortfolioAllocationBucket(value=Decimal("0"), weight_pct=Decimal("0")),
    )
    return PortfolioAllocationProjection(
        state=state,
        source_identity="portfolio_tracker_api_v1",
        currency="USD",
        buckets=buckets,
        reconciliation=PortfolioAllocationReconciliation(
            position_total=Decimal("100"),
            bucket_total=Decimal("100"),
            difference=Decimal("0"),
            is_reconciled=True,
        ),
        reason_codes=("portfolio_allocation_incomplete",) if state == "incomplete" else (),
    )


def test_unified_panel_composes_read_only_sections_and_correlation_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def render_benchmark(**_kwargs: object) -> str:
        return '<section data-testid="benchmark">Index Benchmarking</section>'

    def render_posture(*_args: object, **_kwargs: object) -> str:
        return '<section data-testid="posture">Portfolio Posture</section>'

    def fetch_allocation() -> PortfolioAllocationProjection:
        return _allocation()

    monkeypatch.setattr(
        panel,
        "render_portfolio_panel",
        render_benchmark,
    )
    monkeypatch.setattr(
        panel,
        "render_portfolio_posture_section",
        render_posture,
    )
    monkeypatch.setattr(panel, "fetch_portfolio_allocation", fetch_allocation)

    html = panel.render_performance_risk_panel(tmp_path / "portfolio.db", tmp_path)

    assert 'data-testid="benchmark"' in html
    assert 'data-testid="posture"' in html
    assert "Portfolio Allocation" in html
    assert "US ETF" in html and "Intl Equity" in html
    assert "Cash" in html and "Unclassified" in html
    assert "Next dollar" not in html and "Risk Budget" not in html
    assert 'data-pr-tab="correlation" aria-selected="true"' in html
    assert 'data-src="/api/panel/performance_risk?fragment=correlation"' in html
    assert "Policy mix remains read-only" in html


def test_unified_panel_keeps_window_refreshes_and_posture_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured_performance: dict[str, object] = {}
    captured_posture: dict[str, object] = {}

    def performance_renderer(**kwargs: object) -> str:
        captured_performance.update(kwargs)
        return '<section data-testid="benchmark">Index Benchmarking</section>'

    def posture_renderer(*_args: object, **kwargs: object) -> str:
        captured_posture.update(kwargs)
        return '<section data-testid="posture">Portfolio Posture</section>'

    monkeypatch.setattr(panel, "render_portfolio_panel", performance_renderer)
    monkeypatch.setattr(panel, "render_portfolio_posture_section", posture_renderer)

    panel.render_performance_risk_panel(
        tmp_path / "portfolio.db",
        tmp_path,
        allocation=_allocation(),
    )

    assert captured_performance["refresh_endpoint"] == "/api/panel/performance_risk"
    assert captured_performance["refresh_target_selector"] == "#workOsPerformanceMount"
    assert captured_posture["include_actions"] is False


def test_allocation_card_is_truthful_when_source_is_unavailable() -> None:
    html = panel.render_allocation_card(
        PortfolioAllocationProjection.model_construct(
            state="unavailable",
            source_identity="portfolio_tracker_api_v1",
            buckets=PortfolioAllocationBuckets.model_construct(),
            reconciliation=PortfolioAllocationReconciliation.model_construct(is_reconciled=False),
            reason_codes=("securities_unavailable",),
        )
    )

    assert "Allocation unavailable" in html
    assert "securities_unavailable" in html
    assert "0.0%" not in html
