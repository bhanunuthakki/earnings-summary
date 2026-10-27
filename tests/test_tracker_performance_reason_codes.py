"""Tracker performance reason codes stay within the supported contract."""

from __future__ import annotations

import pytest

from integrations import portfolio_tracker_client as ptc
from integrations.portfolio_tracker_client import PortfolioAnalytics, fetch_portfolio_analytics
from pipeline.portfolio_panel import render_portfolio_analytics_sections

_UNAVAILABLE_PERFORMANCE: dict[str, object] = {
    "methodology": "performance.modified_dietz",
    "methodology_version": "2",
    "calculation_status": "unavailable",
    "calculation_reason_codes": [
        "option_history_reconstruction_unsupported",
        "spy_benchmark_price_unavailable",
    ],
    "source_coverage": {
        "status": "partial",
        "is_complete": False,
        "requested_start_date": "2025-10-24",
        "requested_end_date": "2026-10-24",
        "required_start_date": "2025-10-24",
        "required_end_date": "2026-10-24",
        "accounts": [],
        "attestations": [],
    },
    "start_date": "2025-10-24",
    "end_date": "2026-10-24",
    "base_value": None,
    "points": [],
    "earliest_observed_date": None,
    "net_external_cashflow_in": None,
    "backfill_start_unreliable": False,
    "valuation_account_ids": [],
    "equation_receipt": None,
}


def test_option_history_reason_is_a_supported_unavailable_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tracker PR #68 (2026-10-03) added this code. Unknown to the client, it
    # turned the tracker's real reasons into a false contract rejection.
    monkeypatch.delenv("PORTFOLIO_TRACKER_V1_READS", raising=False)
    paths: list[str] = []

    def fake_get_obj(base: str, path: str, *, timeout: float) -> dict[str, object]:
        paths.append(path)
        return dict(_UNAVAILABLE_PERFORMANCE)

    monkeypatch.setattr(ptc, "_get_obj", fake_get_obj)

    analytics = fetch_portfolio_analytics(api_url="http://tracker.test", only={"performance"})
    performance = analytics.performance
    assert paths == ["/api/portfolio/performance"]
    assert performance is not None
    assert performance.calculation_status == "unavailable"
    assert performance.compatibility_issue is None
    assert performance.calculation_reason_codes == [
        "option_history_reconstruction_unsupported",
        "spy_benchmark_price_unavailable",
    ]

    html = render_portfolio_analytics_sections(
        PortfolioAnalytics(available=True, api_url="http://tracker.test", performance=performance)
    )
    assert "option position history cannot be reconstructed" in html
    assert "SPY benchmark history incomplete" in html
    assert "supported calculation contract" not in html
