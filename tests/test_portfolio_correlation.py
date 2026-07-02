"""Holdings pairwise correlation + crowding clusters — the pure math, the
disk assembler, and the Risk-tab section markup."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from integrations.portfolio_tracker_client import PortfolioAnalytics
from pipeline.portfolio_panel import compose_risk_page
from portfolio_correlation import (
    CorrelationRead,
    CrowdingCluster,
    build_holdings_correlation_from_disk,
    correlation_matrix,
    find_crowding_clusters,
)

# ---------------------------------------------------------------------------
# Deterministic synthetic series (no randomness).
# ---------------------------------------------------------------------------


def _trading_days(n: int, start: date = date(2025, 1, 6)) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _wave(days: list[date], freq: float, scale: float = 0.01) -> dict[date, float]:
    return {d: scale * math.sin(i * freq) for i, d in enumerate(days)}


def test_correlation_matrix_known_structure() -> None:
    days = _trading_days(200)
    a = _wave(days, 0.7)
    b = {d: -v for d, v in a.items()}  # perfectly anti-correlated
    c = _wave(days, 1.9)  # a different frequency — low corr to a
    matrix = np.array([[a[d], b[d], c[d]] for d in days])
    corr = correlation_matrix(matrix)
    assert corr is not None
    assert abs(corr[0, 0] - 1.0) < 1e-12
    assert abs(corr[0, 1] - (-1.0)) < 1e-9
    assert abs(corr[0, 2]) < 0.35  # distinct frequencies decorrelate


def test_correlation_matrix_rejects_degenerate_input() -> None:
    days = _trading_days(50)
    flat = np.array([[0.0, _wave(days, 0.7)[d]] for d in days])
    assert correlation_matrix(flat) is None  # zero-variance column
    assert correlation_matrix(np.zeros((2, 2))) is None  # too few rows


def test_find_crowding_clusters_single_linkage_and_ranking() -> None:
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    # AAA-BBB 0.9, BBB-CCC 0.75 (chain joins CCC), AAA-CCC only 0.5 —
    # single-linkage still groups all three; DDD stays alone.
    corr = np.array(
        [
            [1.0, 0.9, 0.5, 0.1],
            [0.9, 1.0, 0.75, 0.1],
            [0.5, 0.75, 1.0, 0.1],
            [0.1, 0.1, 0.1, 1.0],
        ]
    )
    clusters = find_crowding_clusters(tickers, corr, {"AAA": 0.2, "BBB": 0.1, "CCC": 0.05})
    assert len(clusters) == 1
    (cluster,) = clusters
    assert cluster.tickers == ["AAA", "BBB", "CCC"]
    assert abs(cluster.combined_weight_pct - 35.0) < 1e-9
    assert abs(cluster.avg_corr - (0.9 + 0.75 + 0.5) / 3) < 1e-12
    assert abs(cluster.min_corr - 0.5) < 1e-12  # the weakest link is reported


def test_find_crowding_clusters_none_below_threshold() -> None:
    corr = np.array([[1.0, 0.4], [0.4, 1.0]])
    assert find_crowding_clusters(["AAA", "BBB"], corr, {}) == []


# ---------------------------------------------------------------------------
# Disk assembler over FMP price-chart fixtures.
# ---------------------------------------------------------------------------


def _write_price_chart(repo_root: Path, ticker: str, rets: dict[date, float]) -> None:
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    level = 100.0
    rows: list[dict[str, object]] = []
    for d in sorted(rets):
        level *= math.exp(rets[d])
        rows.append({"date": d.isoformat(), "adjClose": level})
    (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(json.dumps(rows), encoding="utf-8")


def test_build_holdings_correlation_from_disk(tmp_path: Path) -> None:
    days = _trading_days(300)
    base = _wave(days, 0.7)
    other = _wave(days, 1.9)
    _write_price_chart(tmp_path, "AAA", base)
    _write_price_chart(tmp_path, "BBB", {d: v * 1.2 for d, v in base.items()})  # ~same bet
    _write_price_chart(tmp_path, "CCC", other)
    read = build_holdings_correlation_from_disk(
        tmp_path, ["AAA", "BBB", "CCC", "ZZZ"], {"AAA": 0.3, "BBB": 0.2, "CCC": 0.1}
    )
    assert read is not None
    assert read.tickers == ["AAA", "BBB", "CCC"]
    assert read.dropped == {"ZZZ": "no daily price history on file"}
    assert read.n_obs > 120
    assert read.prices_through == days[-1]
    i, j = read.tickers.index("AAA"), read.tickers.index("BBB")
    assert read.matrix[i][j] > 0.99
    assert len(read.clusters) == 1
    assert read.clusters[0].tickers == ["AAA", "BBB"]
    assert abs(read.clusters[0].combined_weight_pct - 50.0) < 1e-9
    assert read.avg_pairwise_corr is not None


def test_build_holdings_correlation_needs_two_names(tmp_path: Path) -> None:
    days = _trading_days(300)
    _write_price_chart(tmp_path, "AAA", _wave(days, 0.7))
    assert build_holdings_correlation_from_disk(tmp_path, ["AAA", "ZZZ"], {"AAA": 1.0}) is None
    assert build_holdings_correlation_from_disk(tmp_path, [], None) is None


# ---------------------------------------------------------------------------
# The Risk-tab section markup (via the page composer — both branches).
# ---------------------------------------------------------------------------


def _read() -> CorrelationRead:
    return CorrelationRead(
        tickers=["MELI", "NU"],
        matrix=[[1.0, 0.82], [0.82, 1.0]],
        clusters=[
            CrowdingCluster(
                tickers=["MELI", "NU"], combined_weight_pct=31.0, avg_corr=0.82, min_corr=0.82
            )
        ],
        n_obs=248,
        prices_through=date(2026, 7, 1),
        avg_pairwise_corr=0.82,
        dropped={"FLKR": "only 40 daily returns on file (need 120)"},
    )


def _page(analytics: PortfolioAnalytics, correlation: CorrelationRead | None) -> str:
    return compose_risk_page(
        analytics,
        drawdown=None,
        factor=None,
        scenarios=[],
        digest="",
        correlation=correlation,
    )


def test_correlation_section_matrix_clusters_and_provenance() -> None:
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), _read())
    assert "Holdings correlation &amp; crowding" in html
    assert "+0.82" in html and "pfc-c3" in html  # the hot cell tone
    assert "31% of book moves as one bet" in html
    assert "avg corr 0.82" in html
    assert "248 common trading days" in html
    assert "prices through 2026-07-01" in html
    assert "FLKR" in html and "only 40 daily returns" in html
    assert "Largest cluster" in html


def test_correlation_section_empty_state_and_offline_branch() -> None:
    html_none = _page(PortfolioAnalytics(available=True, api_url="http://x"), None)
    assert "Holdings correlation &amp; crowding" in html_none
    assert "Not enough daily price history" in html_none

    offline = PortfolioAnalytics(
        available=False, api_url="http://x", errors={"performance": "refused"}
    )
    html_down = _page(offline, _read())
    assert "live portfolio tracker" in html_down  # the offline note leads
    assert "Holdings correlation &amp; crowding" in html_down and "+0.82" in html_down
