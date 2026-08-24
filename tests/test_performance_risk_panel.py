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
from integrations.portfolio_tracker_client import PolicyMix, PolicyWeight


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
    assert "Policy mix unavailable from the tracker" in html


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


def _policy(*, ready: bool = True, extra_weight: bool = False) -> PolicyMix:
    weights = [
        PolicyWeight(ticker="QQQ", weight_pct=35.0, notes=None),
        PolicyWeight(ticker="SGOV", weight_pct=10.0, notes=None),
        PolicyWeight(ticker="VTI", weight_pct=40.0, notes=None),
        PolicyWeight(ticker="VWO", weight_pct=15.0, notes=None),
    ]
    if extra_weight:
        weights.append(PolicyWeight(ticker="BIL", weight_pct=0.0, notes=None))
    return PolicyMix(
        total_pct=100.0,
        is_balanced=True,
        weights=weights,
        revision=8 if ready else None,
        source="earnings_summary" if ready else None,
        as_of="2026-08-23T12:00:00+00:00" if ready else None,
        recomputation_status="current" if ready else "required",
        recomputation_policy_revision=8 if ready else None,
        recomputation_reason=None if ready else "policy_weights_changed",
    )


def test_policy_editor_uses_only_confirmed_provider_weights_and_fresh_read_contract(
    tmp_path: Path,
) -> None:
    html = panel.render_performance_risk_panel(
        tmp_path / "portfolio.db",
        tmp_path,
        allocation=_allocation(),
        performance_renderer=lambda: (
            '<section data-testid="benchmark">Index Benchmarking</section>'
        ),
        policy=_policy(),
    )

    for ticker, weight in (("QQQ", "35.00"), ("SGOV", "10.00"), ("VTI", "40.00"), ("VWO", "15.00")):
        assert f'data-policy-ticker="{ticker}"' in html
        assert f'value="{weight}"' in html
    assert "Apply mix</button>" in html
    assert 'data-write-ready="true"' in html
    assert "/api/portfolio/policy" in html
    assert "X-Portfolio-Write-Intent" in html
    assert "Policy weights must total 100.00%" in html
    assert "checkReceipt" in html
    assert "bha79-policy-receipt" in html
    assert "body.policy.revision" in html
    assert "no newer confirmed revision" in html
    assert "/api/panel/performance_risk" in html
    assert "Math.random" in html


def test_policy_editor_fails_closed_for_pending_or_noncanonical_provider_mix(
    tmp_path: Path,
) -> None:
    pending = panel.render_performance_risk_panel(
        tmp_path / "portfolio.db",
        tmp_path,
        allocation=_allocation(),
        performance_renderer=lambda: "<section>benchmark</section>",
        policy=_policy(ready=False),
    )
    expanded = panel.render_performance_risk_panel(
        tmp_path / "portfolio.db",
        tmp_path,
        allocation=_allocation(),
        performance_renderer=lambda: "<section>benchmark</section>",
        policy=_policy(extra_weight=True),
    )

    assert 'data-write-ready="false"' in pending
    assert "metadata is not current" in pending
    assert 'type="submit" class="k-btn k-btn-primary" disabled' in pending
    assert 'data-write-ready="false"' in expanded
    assert "cannot be safely edited" in expanded
