"""The Risk tab's Monte Carlo section — markup contract + both page branches
(tracker up AND down; the substrate is local disk/DB, so the section renders
in each, same as tail-stress/correlation/style)."""

from __future__ import annotations

from datetime import date

from integrations.portfolio_tracker_client import PortfolioAnalytics
from pipeline.portfolio_panel import compose_risk_page
from portfolio_montecarlo import DistributionRead, EventLeg, EventStressResult, MonteCarloRead


def _page(
    analytics: PortfolioAnalytics,
    mc: MonteCarloRead | None,
    latam: EventStressResult | None = None,
) -> str:
    return compose_risk_page(
        analytics,
        drawdown=None,
        factor=None,
        scenarios=[],
        digest="",
        monte_carlo=mc,
        joint_latam=latam,
    )


def _mc() -> MonteCarloRead:
    normal = DistributionRead(
        method="normal",
        mean_pct=15.0,
        vol_pct=27.5,
        pct_5th=-18.0,
        pct_1st=-25.0,
        prob_below={"-20%": 0.02, "-30%": 0.008, "-40%": 0.001, "-50%": 0.0},
        detail="normal detail",
    )
    student_t = DistributionRead(
        method="student_t",
        mean_pct=16.0,
        vol_pct=55.0,
        pct_5th=-30.0,
        pct_1st=-45.0,
        prob_below={"-20%": 0.06, "-30%": 0.033, "-40%": 0.015, "-50%": 0.006},
        detail="t detail",
    )
    return MonteCarloRead(
        tickers=["MELI", "NOW", "NU"],
        n_obs=248,
        prices_through=date(2026, 7, 1),
        dropped={"FLKR": "only 40 daily returns on file (need 120)"},
        risky_weight_pct=85.0,
        cash_like_weight_pct=8.0,
        modeled_weight_pct=93.0,
        unmodeled_weight_pct=7.0,
        n_paths=50_000,
        seed=42,
        t_df=4,
        drift_source="sample mean of the aligned daily log returns, annualized",
        normal=normal,
        student_t=student_t,
        analytic_vol_pct=26.0,
    )


def _latam() -> EventStressResult:
    return EventStressResult(
        scenario_id="joint_latam",
        title="Joint LatAm credit/macro shock",
        description="MELI and NU both hit their persisted bear-case DCF fair value.",
        book_return_pct=-22.4,
        modeled_weight_pct=100.0,
        legs=[
            EventLeg(ticker="MELI", weight_pct=14.0, return_pct=-38.0, label="fallback -38%"),
            EventLeg(ticker="NU", weight_pct=11.0, return_pct=-52.0, label="fallback -52%"),
            EventLeg(ticker="NOW", weight_pct=9.0, return_pct=-15.0, label="other equities"),
        ],
        notes=["fallback constants used (no live bear scenario persisted) for: MELI, NU"],
    )


def test_monte_carlo_section_headline_stats_and_coverage() -> None:
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), _mc(), _latam())
    assert "Tail risk (Monte Carlo)" in html
    assert "-45%" in html  # t-dist 1st percentile headline
    assert "3.3%" in html  # P(book &lt; -30%) headline (t-dist)
    assert "27.5%" in html  # book vol (normal/covariance basis)
    assert "93%" in html  # coverage
    assert "16%" in html  # wealthplan CMA assumed vol
    assert "FLKR" in html and "only 40 daily returns" in html


def test_monte_carlo_section_joint_latam_block() -> None:
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), _mc(), _latam())
    assert "Joint LatAm credit/macro shock" in html
    assert "-22.4% book" in html
    assert "MELI" in html and "NU" in html
    assert "fallback constants used" in html


def test_monte_carlo_section_empty_state_and_offline_branch() -> None:
    html_none = _page(PortfolioAnalytics(available=True, api_url="http://x"), None, None)
    assert "Tail risk (Monte Carlo)" in html_none
    assert "Not enough daily price history" in html_none

    offline = PortfolioAnalytics(
        available=False, api_url="http://x", errors={"performance": "refused"}
    )
    html_down = _page(offline, _mc(), _latam())
    assert "live portfolio tracker" in html_down  # the offline note leads
    assert "Tail risk (Monte Carlo)" in html_down and "-45%" in html_down


def test_monte_carlo_section_missing_latam_shows_placeholder() -> None:
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), _mc(), None)
    assert "Joint-LatAm stress" in html
    assert "not enough weighted holdings to stress" in html


# --------------------------------------------------------------------------- #
# Wave 3 (surface_density_jit_redesign.md #3): the implicit-bets statement —
# the risk page's organizing frame ("what am I positioned for").
# --------------------------------------------------------------------------- #


def _bets_snapshot(**kw: object):
    from portfolio_risk_snapshot_store import RiskSnapshot

    defaults: dict[str, object] = {
        "captured_at": "2026-07-24T09:00:00",
        "beta": 1.44,
        "r_squared": 0.10,
        "growth_tilt": -0.47,
        "rate_beta_10y": 0.02,
    }
    defaults.update(kw)
    return RiskSnapshot(**defaults)  # type: ignore[arg-type]


def test_implicit_bets_rank_and_state_numbers() -> None:
    from pipeline.portfolio_panel import _implicit_bets_section

    weights = {"VTI": 0.201, "MELI": 0.147, "NU": 0.123, "NOW": 0.085}
    html = _implicit_bets_section(_bets_snapshot(), weights, None)

    assert "What am I positioned for?" in html
    # Concentration bet leads (top weight 20.1% x salience 4 = highest).
    assert "<strong>Single-name execution at VTI</strong>" in html
    assert "20.1% of the book" in html
    # LatAm cycle bet with the joint weight (MELI+NU = 27.0%).
    assert "LatAm credit/FX cycle" in html
    assert "27.0% of the book" in html
    # Style bet states direction + the tilt number.
    assert "value leadership over growth" in html
    assert "-0.47" in html
    # Market bet reconciles beta with the low R².
    assert "β 1.44" in html
    assert "mostly stock-specific" in html
    # Ordering: concentration before the style bet.
    assert html.index("Single-name execution") < html.index("value leadership")


def test_implicit_bets_degrade_to_unlock_line() -> None:
    from pipeline.portfolio_panel import _implicit_bets_section

    html = _implicit_bets_section(None, {}, None)
    assert "What am I positioned for?" in html
    assert "Not derivable yet" in html
    assert "<ol>" not in html


def test_implicit_bets_lead_the_risk_page_in_both_branches() -> None:
    from integrations.portfolio_tracker_client import PortfolioAnalytics
    from pipeline.portfolio_panel import compose_risk_page

    bets = '<section class="panel pfr-bets"><h2>What am I positioned for?</h2></section>'
    offline = compose_risk_page(
        PortfolioAnalytics(available=False, api_url="http://x", errors={}),
        drawdown=None,
        factor=None,
        scenarios=[],
        digest="",
        bets=bets,
    )
    assert "What am I positioned for?" in offline
    # It LEADS: before any other section of the page body.
    assert offline.index("pfr-bets") < offline.index("pfr-root") + len(offline)
    root = offline.index('id="pfr-root"')
    assert offline.index("pfr-bets", root) < offline.index("</section>", root) + 1
