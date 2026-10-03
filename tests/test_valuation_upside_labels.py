"""Actual report and portfolio renderers distinguish present-value gaps from returns."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from importlib import import_module
from io import StringIO
from pathlib import Path
from typing import cast

import pytest

from pipeline import portfolio_panel
from pipeline.peeks import render_what_if_peek
from portfolio_risk_snapshot_store import RiskSnapshot
from report.models import SectionStatus, SnapshotSection, ValuationSnapshot
from risk_reward import RiskRewardGap, RiskRewardGapRow

card_html = cast(
    Callable[[StringIO, SnapshotSection], None],
    getattr(
        import_module("report.renderers.workspace_sections.thesis_risk"), "_valuation_summary_panel"
    ),
)
card_md = cast(
    Callable[[StringIO, ValuationSnapshot], None],
    getattr(import_module("report.renderers.markdown"), "_valuation_card_md"),
)
gap_html = cast(
    Callable[[RiskRewardGap], str],
    getattr(portfolio_panel, "_risk_reward_gap_section"),
)


@pytest.mark.parametrize("source", ["global", "owner", "llm"])
@pytest.mark.parametrize("partial", [False, True])
def test_report_cards_label_present_fair_value_gap_and_unaccepted_prior(
    source: str, partial: bool
) -> None:
    valuation = ValuationSnapshot(
        consolidated_npv_per_share=100,
        current_price=100,
        bull_npv_per_share=150,
        bear_npv_per_share=None if partial else 50,
        valuation_date=date(2026, 10, 1),
        live_price_at=datetime(2026, 10, 2, 20, tzinfo=UTC),
        scenario_expected_return=0.25,
        scenario_skew=0.25,
        scenario_weights={"bull": 0.25, "base": 0.5, "bear": 0.25},
        scenario_set_by=source,
    )
    assert valuation.scenario_valuation_upside == 0.25
    body = StringIO()
    card_html(body, SnapshotSection(status=SectionStatus.OK, ticker="TEST", valuation=valuation))
    md = StringIO()
    card_md(md, valuation)
    for text in (body.getvalue(), md.getvalue()):
        assert "Scenario-weighted upside to present fair value" in text
        assert "+25%" in text
        assert "E[V]" not in text and "Expected value" not in text
        assert "2026-10-01" in text and "2026-10-02" in text
        assert "unaccepted" in text
        assert ("default prior" if source == "global" else "per-name prior") in text
        if partial:
            assert "partial scenarios; weights renormalized" in text


def test_portfolio_unavailable_valuation_retains_reason_and_conviction_only() -> None:
    row = RiskRewardGapRow(
        ticker="TEST",
        weight_pct=60,
        risk_share_pct=70,
        marginal_vol_ann_pct=30,
        expected_return_pct=None,
        reward_share_pct=None,
        gap_pct=None,
        conviction=2,
        has_scenarios=False,
        low_confidence=True,
        confidence_reason="scenario_acceptance_unverified",
        reward_detail=None,
        mismatch_score=2,
        mismatch_reasons=["conviction 2/5 but 70% of book risk"],
    )
    gap = RiskRewardGap(
        rows=[row],
        portfolio_vol_ann=0.3,
        weights_source="tracker",
        prices_through=date(2026, 10, 2),
        cov_obs=252,
        shrinkage=0.1,
        valued_names=0,
    )
    text = gap_html(gap)
    assert "Modeled upside share" in text and "Valuation upside" in text
    assert "Exp. return" not in text and "expected reward" not in text
    assert "unavailable" in text and "scenario_acceptance_unverified" in text
    assert "conviction 2/5" in text
    assert "0/1" in text and "readiness-qualified" in text


def test_cached_historical_sharpe_retains_window_and_missing_basis() -> None:
    cached_html = cast(
        Callable[[RiskSnapshot], str], getattr(portfolio_panel, "_cached_risk_section")
    )
    text = cached_html(
        RiskSnapshot(
            captured_at="2026-10-02", window_start="2025-10-01", window_end="2026-10-01", sharpe=0.8
        )
    )
    assert "Historical Sharpe" in text
    assert "2025-10-01" in text and "2026-10-01" in text
    assert "return basis and risk-free treatment unavailable" in text


@pytest.mark.parametrize("risk_free", [0.04, None])
def test_historical_what_if_uses_price_window_basis_and_risk_free_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, risk_free: float | None
) -> None:
    import candidate_fit_cache
    import portfolio_weights
    from allocation.what_if import clear_caches

    def weights(_repo_root: Path) -> dict[str, float]:
        return {"AAA": 0.6, "BBB": 0.4}

    def meta(_repo_root: Path) -> dict[str, object]:
        return {"book": {"risk_free_annual": risk_free}}

    monkeypatch.setattr(portfolio_weights, "read_materialized_weights", weights)
    monkeypatch.setattr(candidate_fit_cache, "read_materialized_fit_meta", meta)
    price_dir = tmp_path / "data" / "historical" / "fmp"
    price_dir.mkdir(parents=True)
    for ticker, phase in (("AAA", 0), ("BBB", 1), ("TEST", 2)):
        price = 100.0
        prices: list[dict[str, object]] = []
        for i in range(201):
            price *= math.exp(0.008 * math.sin(i / 5 + phase) + 0.0004)
            prices.append(
                {"date": (date(2025, 6, 1) + timedelta(days=i)).isoformat(), "adjClose": price}
            )
        (price_dir / f"{ticker}_price_chart_10y_div_adj.json").write_text(json.dumps(prices))
    clear_caches()
    try:
        text = render_what_if_peek(tmp_path, "TEST", 0.03)
        assert text is not None
        assert "Historical Sharpe" in text
        assert "Historical aligned daily log returns" in text
        assert "dividend-adjusted close; close fallback" in text
        assert "200 common days" in text and "2025-12-18" in text
        if risk_free is None:
            assert "risk-free treatment unavailable" in text
        else:
            assert "risk-free 4.0%/yr" in text
            assert "Historical what-if &Delta;Sharpe" in text
    finally:
        clear_caches()
