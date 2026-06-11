"""The next-dollar allocation model: blend three factors into a distribution.

Where should the next incremental dollar go, across the current portfolio
holdings? Three inspectable factors per holding (each visible in the panel's
waterfall, never a black-box composite):

  ret   — absolute expected return: DCF fair-value upside on price,
          (npv_per_share / live_price − 1) from the latest ``dcf_runs`` row.
          Recomputed from the row's own two fields, never read from
          ``over_under_pct`` (same convention rule as
          ``research_cockpit.latest_dcf_runs``).
  div   — diversification: −(∂σ_p/∂w_i), the marginal volatility the next
          dollar of the name adds to the modeled book, off a Ledoit–Wolf
          shrunk covariance of ~1y of daily log returns at current weights.
          This is the risk leg of marginal Sharpe; the return leg is ``ret``.
  macro — macro sentiment tilt: Σ_series β(ticker, series) × momentum(series),
          betas from ``macro_sensitivities`` (weekly-log-return OLS) and
          momentum as the trailing ~90-calendar-day log change of each
          ``macro_series`` — the same return space the betas were fit in.

Each factor is z-scored cross-sectionally over the holdings that carry it,
blended by visible weights (BLEND_WEIGHTS, renormalized per holding over the
factors it actually has), and the blended scores pass through a softmax into
an allocation distribution. A factor with fewer than two carriers is hidden
model-wide and the blend renormalizes — hide-don't-stub, with the reason
surfaced. Full model documentation: directives/next_dollar_model.md.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from allocation.covariance import ledoit_wolf, marginal_risk
from allocation.price_history import build_aligned_returns, daily_log_returns, load_daily_closes
from macro_store import Sensitivity, fetch_sensitivities, fetch_series

# The blend, in plain sight. Keys are factor ids used throughout; values are
# the target weights when every factor is live. Renormalized (a) model-wide
# when a factor is hidden for lack of data, (b) per holding over the factors
# that holding carries.
BLEND_WEIGHTS: dict[str, float] = {"ret": 0.50, "div": 0.30, "macro": 0.20}
FACTOR_LABELS: dict[str, str] = {
    "ret": "expected return",
    "div": "diversification",
    "macro": "macro tilt",
}

SOFTMAX_TEMPERATURE = 1.0  # z-scale scores; ~e^3 spread between best and worst
COV_LOOKBACK_OBS = 252  # ~1 trading year of daily returns in the covariance
MIN_OVERLAP_OBS = 120  # min common trading days for a name to enter the matrix
MACRO_BETA_LOOKBACK = 252  # preferred macro_sensitivities lookback_window_days
MACRO_MOMENTUM_CAL_DAYS = 90  # macro momentum = log change over this window
MACRO_STALE_DAYS = 45  # ignore a macro series whose latest point is older
RET_CLAMP = 1.0  # winsorize DCF upside at ±100% before z-scoring
ANNUALIZE = math.sqrt(252.0)


@dataclass(frozen=True, slots=True)
class FactorReading:
    """One factor's value for one holding — the waterfall's substrate."""

    z: float  # cross-sectional z-score (what gets blended)
    raw: float  # the underlying economic quantity (fraction, signed)
    weight: float  # effective blend weight applied for this holding
    contribution: float  # weight x z — what actually moved the score
    detail: str  # short human substrate, e.g. "fair $160.07 vs $113.87"


@dataclass(slots=True)
class HoldingScore:
    ticker: str
    current_weight_pct: float  # share of the modeled book at current weights
    score: float  # blended z (sum of weight x z over carried factors)
    allocation_pct: float  # softmax share of the next dollar
    ret: FactorReading | None
    div: FactorReading | None
    macro: FactorReading | None
    missing: tuple[str, ...]  # active factor ids this holding lacked

    def reading(self, key: str) -> FactorReading | None:
        return {"ret": self.ret, "div": self.div, "macro": self.macro}[key]


@dataclass(slots=True)
class NextDollarModel:
    rows: list[HoldingScore]  # sorted by allocation desc
    blend: list[tuple[str, float]]  # (factor id, model-wide weight) after hiding
    hidden_factors: dict[str, str]  # factor id -> why it is absent model-wide
    weights_source: str  # "tracker" | "equal"
    prices_through: date | None  # last common trading date in the covariance
    cov_obs: int | None  # rows of the return matrix
    shrinkage: float | None  # Ledoit-Wolf delta
    portfolio_vol_ann: float | None  # modeled book vol, annualized fraction
    excluded: dict[str, str]  # ticker -> reason it has no row at all
    notes: list[str] = field(default_factory=list[str])


def build_next_dollar_model(
    db_path: Path,
    repo_root: Path,
    holdings: Sequence[str],
    live_values: Mapping[str, float | None] | None = None,
) -> NextDollarModel | None:
    """Score the holdings and softmax into a next-dollar distribution.

    ``live_values`` is the tracker's market value per ticker (any subset);
    None or empty falls back to equal weights, labelled via
    ``weights_source``. Returns None when no factor has enough data to score
    anything — the caller falls back to its narrative-only rendering.
    """
    tickers = [t.upper() for t in dict.fromkeys(holdings) if t]
    if not tickers:
        return None

    weights, weights_source = _current_weights(tickers, live_values)
    ret_readings = _dcf_upside(db_path, tickers)
    div = _diversification(repo_root, tickers, weights)
    macro_readings = _macro_tilt(db_path, tickers)

    factor_raw: dict[str, dict[str, tuple[float, str]]] = {
        "ret": ret_readings,
        "div": div.readings,
        "macro": macro_readings,
    }
    hidden: dict[str, str] = {}
    for key, factor_readings in factor_raw.items():
        carriers = [t for t in tickers if t in factor_readings]
        if len(carriers) >= 2:
            continue
        if key == "div":
            hidden[key] = div.hidden_reason or "fewer than two holdings with price history"
        elif key == "ret":
            hidden[key] = "no DCF runs with fair value + price for these holdings"
        else:
            hidden[key] = (
                "no macro betas/series on file (run fetch_macro_series + "
                "compute_macro_sensitivities)"
            )
    active = [k for k in BLEND_WEIGHTS if k not in hidden]
    if not active:
        return None

    zscores = {
        key: _zscore(factor_raw[key], clamp=RET_CLAMP if key == "ret" else None) for key in active
    }

    rows: list[HoldingScore] = []
    excluded: dict[str, str] = {}
    for t in tickers:
        have = [k for k in active if t in zscores[k]]
        if not have:
            excluded[t] = "no factor data"
            continue
        weight_sum = sum(BLEND_WEIGHTS[k] for k in have)
        readings: dict[str, FactorReading] = {}
        score = 0.0
        for k in have:
            w_eff = BLEND_WEIGHTS[k] / weight_sum
            z = zscores[k][t]
            raw, detail = factor_raw[k][t]
            contribution = w_eff * z
            score += contribution
            readings[k] = FactorReading(
                z=z, raw=raw, weight=w_eff, contribution=contribution, detail=detail
            )
        rows.append(
            HoldingScore(
                ticker=t,
                current_weight_pct=weights[t] * 100.0,
                score=score,
                allocation_pct=0.0,
                ret=readings.get("ret"),
                div=readings.get("div"),
                macro=readings.get("macro"),
                missing=tuple(k for k in active if k not in have),
            )
        )
    if not rows:
        return None

    scores = np.array([r.score for r in rows], dtype=np.float64) / SOFTMAX_TEMPERATURE
    exp = np.exp(scores - float(scores.max()))
    shares = exp / float(exp.sum())
    for row, share in zip(rows, shares, strict=True):
        row.allocation_pct = float(share) * 100.0
    rows.sort(key=lambda r: (-r.allocation_pct, r.ticker))

    active_total = sum(BLEND_WEIGHTS[k] for k in active)
    blend = [(k, BLEND_WEIGHTS[k] / active_total) for k in active]
    return NextDollarModel(
        rows=rows,
        blend=blend,
        hidden_factors=hidden,
        weights_source=weights_source,
        prices_through=div.prices_through,
        cov_obs=div.cov_obs,
        shrinkage=div.shrinkage,
        portfolio_vol_ann=div.portfolio_vol_ann,
        excluded=excluded,
        notes=div.notes,
    )


# ---------------------------------------------------------------------------
# Factor inputs
# ---------------------------------------------------------------------------


def _current_weights(
    tickers: Sequence[str], live_values: Mapping[str, float | None] | None
) -> tuple[dict[str, float], str]:
    """Current modeled-book weights: tracker market values restricted to the
    holdings and renormalized, else equal weights. A tracked holding absent
    from the tracker simply carries weight 0 (it still gets scored — that is
    the point of a next-dollar model)."""
    values = {
        t.upper(): float(v)
        for t, v in (live_values or {}).items()
        if v is not None and float(v) > 0.0
    }
    in_book = {t: values.get(t, 0.0) for t in tickers}
    total = sum(in_book.values())
    if total > 0.0:
        return {t: v / total for t, v in in_book.items()}, "tracker"
    n = len(tickers)
    return dict.fromkeys(tickers, 1.0 / n), "equal"


def _dcf_upside(db_path: Path, tickers: Sequence[str]) -> dict[str, tuple[float, str]]:
    """Latest DCF upside per ticker: npv_per_share / live_price − 1.

    Recomputed from the row's own two fields (currency-consistent with each
    other), never from ``over_under_pct`` — the bank/holdco builders stored
    that column in a different convention. Same rule as
    ``research_cockpit.latest_dcf_runs``."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ticker, valuation_date, npv_per_share, live_price "
            "FROM dcf_runs ORDER BY ticker, created_at DESC, id DESC"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    want = set(tickers)
    out: dict[str, tuple[float, str]] = {}
    for r in rows:
        t = str(r["ticker"]).upper()
        if t not in want or t in out:
            continue
        fv = float(r["npv_per_share"]) if r["npv_per_share"] is not None else None
        px = float(r["live_price"]) if r["live_price"] is not None else None
        if fv is None or px is None or fv <= 0.0 or px <= 0.0:
            continue
        upside = fv / px - 1.0
        as_of = str(r["valuation_date"]) if r["valuation_date"] else "?"
        out[t] = (upside, f"fair ${fv:,.2f} vs ${px:,.2f} ({as_of})")
    return out


@dataclass(slots=True)
class _DivResult:
    readings: dict[str, tuple[float, str]] = field(default_factory=dict[str, tuple[float, str]])
    prices_through: date | None = None
    cov_obs: int | None = None
    shrinkage: float | None = None
    portfolio_vol_ann: float | None = None
    notes: list[str] = field(default_factory=list[str])
    hidden_reason: str | None = None


def _diversification(
    repo_root: Path, tickers: Sequence[str], weights: Mapping[str, float]
) -> _DivResult:
    """−(marginal annualized vol) per holding off the shrunk covariance.

    Weights are renormalized over the names that made it into the matrix (a
    dropped name's weight is small or zero in practice; the note says who
    fell out and why)."""
    returns_by_ticker: dict[str, dict[date, float]] = {}
    for t in tickers:
        rets = daily_log_returns(load_daily_closes(t, repo_root))
        if rets:
            returns_by_ticker[t] = rets
    no_file = [t for t in tickers if t not in returns_by_ticker]
    if len(returns_by_ticker) < 2:
        return _DivResult(hidden_reason="fewer than two holdings with price history on file")
    aligned = build_aligned_returns(
        returns_by_ticker, lookback_obs=COV_LOOKBACK_OBS, min_overlap_obs=MIN_OVERLAP_OBS
    )
    if aligned is None:
        return _DivResult(
            hidden_reason=(
                f"holdings share fewer than {MIN_OVERLAP_OBS} common trading days of history"
            )
        )
    cov = ledoit_wolf(aligned.matrix)
    w_arr = np.array([weights.get(t, 0.0) for t in aligned.tickers], dtype=np.float64)
    w_sum = float(w_arr.sum())
    if w_sum > 0.0:
        w_arr = w_arr / w_sum
    else:
        w_arr = np.full(len(aligned.tickers), 1.0 / len(aligned.tickers), dtype=np.float64)
    risk = marginal_risk(cov.sigma, w_arr)
    if risk is None:
        return _DivResult(hidden_reason="degenerate covariance (zero portfolio variance)")
    result = _DivResult(
        prices_through=aligned.dates[-1],
        cov_obs=cov.n_obs,
        shrinkage=cov.shrinkage,
        portfolio_vol_ann=risk.portfolio_vol * ANNUALIZE,
    )
    for i, t in enumerate(aligned.tickers):
        mvol_ann = float(risk.marginal_vol[i]) * ANNUALIZE
        corr = float(risk.corr_to_book[i])
        result.readings[t] = (
            -mvol_ann,
            f"corr to book {corr:+.2f} · marginal vol {mvol_ann * 100.0:+.1f}%/yr",
        )
    skipped = dict(aligned.dropped)
    for t in no_file:
        skipped[t] = "no daily price history on file"
    if skipped:
        result.notes.append(
            "outside the covariance: "
            + "; ".join(f"{t} ({reason})" for t, reason in sorted(skipped.items()))
        )
    return result


def _macro_tilt(db_path: Path, tickers: Sequence[str]) -> dict[str, tuple[float, str]]:
    """Σ β × momentum per holding, in cumulative-log-return units.

    Betas come from ``macro_sensitivities`` (preferring the standard
    252-day-lookback rows); momentum is the trailing ~90-calendar-day log
    change of each series — the cumulative version of the weekly log returns
    the betas were regressed on, so β × momentum is an expected ticker-return
    contribution. Series whose latest point is stale are skipped rather than
    silently tilting on dead data."""
    sens_by_ticker: dict[str, list[Sensitivity]] = {}
    needed: set[str] = set()
    for t in tickers:
        sens = fetch_sensitivities(ticker=t, db_path=db_path)
        if not sens:
            continue
        by_lookback: dict[int, list[Sensitivity]] = {}
        for s in sens:
            by_lookback.setdefault(s.lookback_window_days, []).append(s)
        pick = by_lookback.get(MACRO_BETA_LOOKBACK) or by_lookback[min(by_lookback)]
        sens_by_ticker[t] = pick
        needed.update(s.series_id for s in pick)
    if not sens_by_ticker:
        return {}
    momentum = {sid: _series_momentum(db_path, sid) for sid in sorted(needed)}
    out: dict[str, tuple[float, str]] = {}
    for t, sens in sens_by_ticker.items():
        contributions: list[tuple[str, float]] = []
        for s in sens:
            mom = momentum.get(s.series_id)
            if mom is None:
                continue
            contributions.append((s.series_id, s.beta * mom))
        if not contributions:
            continue
        tilt = sum(c for _, c in contributions)
        top = sorted(contributions, key=lambda kv: -abs(kv[1]))[:2]
        detail = " · ".join(f"{sid} {c * 100.0:+.1f}%" for sid, c in top)
        out[t] = (tilt, f"{detail} (beta x {MACRO_MOMENTUM_CAL_DAYS}d momentum)")
    return out


def _series_momentum(db_path: Path, series_id: str) -> float | None:
    """Trailing log change of one macro series over ~MACRO_MOMENTUM_CAL_DAYS."""
    points = fetch_series(
        series_id=series_id,
        lookback_days=MACRO_MOMENTUM_CAL_DAYS + 60,
        db_path=db_path,
    )
    if len(points) < 2:
        return None
    latest = points[0]  # fetch_series returns newest first
    if (date.today() - latest.rate_date).days > MACRO_STALE_DAYS:
        return None
    cutoff = latest.rate_date - timedelta(days=MACRO_MOMENTUM_CAL_DAYS)
    base = next((p for p in points if p.rate_date <= cutoff), None)
    if base is None or base.value <= 0.0 or latest.value <= 0.0:
        return None
    return math.log(latest.value / base.value)


# ---------------------------------------------------------------------------
# Standardization
# ---------------------------------------------------------------------------


def _zscore(
    readings: Mapping[str, tuple[float, str]], *, clamp: float | None = None
) -> dict[str, float]:
    """Cross-sectional z-scores of the raw values (population sd).

    ``clamp`` winsorizes the raws first — a 4× DCF mispricing signal is
    treated as no stronger than 2× (the raw stays visible in the waterfall).
    A degenerate cross-section (sd ≈ 0) scores everyone 0 rather than
    dividing by noise."""
    if not readings:
        return {}
    raws = {
        t: max(-clamp, min(clamp, raw)) if clamp is not None else raw
        for t, (raw, _detail) in readings.items()
    }
    arr = np.array(list(raws.values()), dtype=np.float64)
    sd = float(arr.std())
    if sd < 1e-12:
        return dict.fromkeys(raws, 0.0)
    mean = float(arr.mean())
    return {t: (v - mean) / sd for t, v in raws.items()}
