"""Whole-book risk derivations — pure transforms over tracker analytics (L5).

Two derivations the tracker doesn't expose as endpoints, computed here from
payloads it DOES return (so the sibling repo stays out of scope):

* :func:`compute_drawdown` — peak-to-trough drawdown, the underwater curve, and
  time-to-recovery, from the daily time-weighted-return series the tracker's
  ``/api/portfolio/performance`` already returns. (No drawdown endpoint exists;
  this is the sanctioned client-side derivation — a transform of the returned
  cumulative-return series, not a re-computation of returns.)
* :func:`factor_exposure_rollup` — a book value-weighted roll-up of the
  per-ticker correlation/beta rows the positioning endpoint returns (and that
  the client used to discard): market beta (SPY), growth tilt (QQQ beta and the
  QQQ-minus-SPY gap), market co-movement / crowding (SPY correlation), and an
  optional rate-sensitivity leg fed from the local ``macro_sensitivities``
  10Y betas. Value / size / momentum loadings need a factor-return series the
  tracker doesn't expose — deliberately omitted rather than faked.

Both are pure and dependency-free (stdlib + the client's dataclasses) so they
test without a network or a DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from integrations.portfolio_tracker_client import PerformancePoint, PositionCorrelationRow


@dataclass(slots=True)
class DrawdownPoint:
    """One day of the underwater curve: the percent below the running peak
    (``<= 0``; 0 means the book is at a fresh high)."""

    date: str
    drawdown_pct: float


@dataclass(slots=True)
class DrawdownStats:
    """Peak-to-trough drawdown over the window, derived from cumulative TWR.

    ``max_drawdown_pct`` is the deepest underwater reading (negative percent).
    ``recovery_date`` is the first day the book regained its pre-drawdown peak —
    ``None`` while still underwater, in which case ``days_to_recovery`` is None
    and ``recovered`` is False. ``current_drawdown_pct`` is the last point's
    distance below its running peak (0 when the book ends at a high)."""

    max_drawdown_pct: float
    peak_date: str | None
    trough_date: str | None
    recovery_date: str | None
    days_to_recovery: int | None
    current_drawdown_pct: float
    recovered: bool
    underwater: list[DrawdownPoint] = field(default_factory=list[DrawdownPoint])


def _parse_iso(d: str) -> date | None:
    try:
        return date.fromisoformat(d[:10])
    except (ValueError, IndexError):
        return None


def compute_drawdown(points: list[PerformancePoint]) -> DrawdownStats | None:
    """Drawdown stats from the portfolio's cumulative TWR series.

    Each ``portfolio_return_pct`` is the cumulative window return to that day, so
    the equity curve (indexed to 1.0 at the window start) is ``1 + pct/100``.
    Drawdown at day *i* is ``equity_i / running_peak_i - 1``. Points whose
    portfolio return is missing are skipped. Returns ``None`` when fewer than two
    usable points remain (no curve to measure).
    """
    curve: list[tuple[str, float]] = [
        (p.date, 1.0 + p.portfolio_return_pct / 100.0)
        for p in points
        if p.portfolio_return_pct is not None and p.date
    ]
    if len(curve) < 2:
        return None

    underwater: list[DrawdownPoint] = []
    peak_value = curve[0][1]
    peak_date = curve[0][0]
    max_dd = 0.0
    trough_date: str | None = None
    # The peak that PRECEDED the deepest trough — what the book must regain.
    max_dd_peak_value = peak_value
    max_dd_peak_date = peak_date
    for d, value in curve:
        if value > peak_value:
            peak_value = value
            peak_date = d
        dd = (value / peak_value - 1.0) * 100.0 if peak_value > 0 else 0.0
        underwater.append(DrawdownPoint(date=d, drawdown_pct=dd))
        if dd < max_dd:
            max_dd = dd
            trough_date = d
            max_dd_peak_value = peak_value
            max_dd_peak_date = peak_date

    # Time-to-recovery: first day at/after the trough that regains the prior peak.
    recovery_date: str | None = None
    if trough_date is not None:
        seen_trough = False
        for d, value in curve:
            if d == trough_date:
                seen_trough = True
                continue
            if seen_trough and value >= max_dd_peak_value:
                recovery_date = d
                break

    days_to_recovery: int | None = None
    if recovery_date is not None and trough_date is not None:
        t, r = _parse_iso(trough_date), _parse_iso(recovery_date)
        if t is not None and r is not None:
            days_to_recovery = (r - t).days

    current_dd = underwater[-1].drawdown_pct if underwater else 0.0
    return DrawdownStats(
        max_drawdown_pct=max_dd,
        peak_date=max_dd_peak_date if trough_date is not None else None,
        trough_date=trough_date,
        recovery_date=recovery_date,
        days_to_recovery=days_to_recovery,
        current_drawdown_pct=current_dd,
        recovered=recovery_date is not None,
        underwater=underwater,
    )


@dataclass(slots=True)
class CrowdedName:
    """One holding's contribution to a factor leg — its weight × loading, used
    to name where the book's exposure is concentrated."""

    ticker: str
    weight_pct: float
    loading: float


@dataclass(slots=True)
class FactorRollup:
    """Book value-weighted factor/style exposure over the per-ticker rows.

    Each ``*_beta`` / ``avg_correlation_spy`` is a weight-renormalized mean over
    the names that HAVE that estimate (so a few names lacking price history don't
    silently zero the book read). ``growth_tilt`` is ``qqq_beta - spy_beta`` —
    positive means the book leans growth/tech beyond its plain market beta.
    ``rate_beta_10y`` is fed from the local macro 10Y sensitivities and is None
    when none are available. ``names_priced`` / ``names_total`` make the coverage
    explicit; ``top_market`` / ``top_growth`` / ``top_crowding`` name the largest
    contributors to each leg."""

    spy_beta: float | None
    qqq_beta: float | None
    growth_tilt: float | None
    avg_correlation_spy: float | None
    rate_beta_10y: float | None
    names_total: int
    names_priced: int
    top_market: list[CrowdedName] = field(default_factory=list[CrowdedName])
    top_growth: list[CrowdedName] = field(default_factory=list[CrowdedName])
    top_crowding: list[CrowdedName] = field(default_factory=list[CrowdedName])


def _weighted_mean(pairs: list[tuple[float, float]]) -> float | None:
    """Weight-renormalized mean of ``(weight, value)`` pairs; None if no weight."""
    total_w = sum(w for w, _v in pairs if w > 0)
    if total_w <= 0:
        return None
    return sum(w * v for w, v in pairs if w > 0) / total_w


def _top_contributors(
    rows: list[tuple[str, float, float | None]], *, limit: int = 3
) -> list[CrowdedName]:
    """The ``limit`` names with the largest |weight × loading|, biggest first."""
    scored = [
        CrowdedName(ticker=t, weight_pct=w, loading=v)
        for t, w, v in rows
        if v is not None and w > 0
    ]
    scored.sort(key=lambda c: abs(c.weight_pct * c.loading), reverse=True)
    return scored[:limit]


def factor_exposure_rollup(
    rows: list[PositionCorrelationRow],
    rate_beta_by_ticker: dict[str, float] | None = None,
) -> FactorRollup | None:
    """Roll the per-ticker correlation/beta rows into a book factor read.

    Weights are each row's ``weight_pct`` (its share of book), renormalized per
    leg over the names that have that estimate. ``rate_beta_by_ticker`` (optional)
    supplies per-name 10Y betas from the local ``macro_sensitivities`` table for
    the rate-sensitivity leg. Returns ``None`` when there are no rows.
    """
    if not rows:
        return None
    rate_beta_by_ticker = rate_beta_by_ticker or {}

    spy_pairs: list[tuple[float, float]] = []
    qqq_pairs: list[tuple[float, float]] = []
    corr_pairs: list[tuple[float, float]] = []
    rate_pairs: list[tuple[float, float]] = []
    market_rows: list[tuple[str, float, float | None]] = []
    growth_rows: list[tuple[str, float, float | None]] = []
    crowd_rows: list[tuple[str, float, float | None]] = []
    names_priced = 0
    for r in rows:
        w = float(r.weight_pct or 0.0)
        ticker = r.ticker or "?"
        if r.sample_size and r.sample_size > 1:
            names_priced += 1
        if r.beta_spy is not None:
            spy_pairs.append((w, r.beta_spy))
        if r.beta_qqq is not None:
            qqq_pairs.append((w, r.beta_qqq))
        if r.correlation_spy is not None:
            corr_pairs.append((w, r.correlation_spy))
        rate = rate_beta_by_ticker.get(ticker.upper())
        if rate is not None:
            rate_pairs.append((w, rate))
        # Growth contribution is the name's QQQ-minus-SPY gap (its growth lean).
        growth_gap = (
            r.beta_qqq - r.beta_spy if r.beta_qqq is not None and r.beta_spy is not None else None
        )
        market_rows.append((ticker, w, r.beta_spy))
        growth_rows.append((ticker, w, growth_gap))
        crowd_rows.append((ticker, w, r.correlation_spy))

    spy_beta = _weighted_mean(spy_pairs)
    qqq_beta = _weighted_mean(qqq_pairs)
    growth_tilt = qqq_beta - spy_beta if spy_beta is not None and qqq_beta is not None else None
    return FactorRollup(
        spy_beta=spy_beta,
        qqq_beta=qqq_beta,
        growth_tilt=growth_tilt,
        avg_correlation_spy=_weighted_mean(corr_pairs),
        rate_beta_10y=_weighted_mean(rate_pairs),
        names_total=len(rows),
        names_priced=names_priced,
        top_market=_top_contributors(market_rows),
        top_growth=_top_contributors(growth_rows),
        top_crowding=_top_contributors(crowd_rows),
    )
