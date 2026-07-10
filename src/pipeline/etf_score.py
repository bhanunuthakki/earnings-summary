"""ETF next-dollar attractiveness — the fund sibling of the equity score.

The equity score (DCF upside × Rev growth × FCF margin × PEG) is meaningless
for a fund: no income statement, no DCF, no PEG. This module scores the same
QUESTION — "is this an attractive next dollar, standalone?" — with
fund-appropriate factors, in the exact band-multiplier idiom
(``research_cockpit.attractiveness_breakdown``): each factor a small
best-first band table, the score their product, the math shipped verbatim as
the chip's ``why``. It reuses ``AttractivenessFactor`` /
``AttractivenessBreakdown`` / ``attractiveness_tone`` so the chip and peek
renderers work unchanged, and ETF and equity rows sort on one scale.

Factors (inputs from the published-data layer, directives/etf_data.md):

  ret   — realized risk-adjusted return edge vs SPY: ΔSR = SR_etf - SR_SPY,
          both rf=0 Sharpe over the latest ≤504 common trading days (≥120).
          The factor premium either shows up in delivered risk-adjusted
          return or it doesn't.
  er    — expense drag (etf_profile.expense_ratio, decimal). The one return
          number that is guaranteed, forever, and negative.
  prem  — factor distinctiveness: Σ|β| over the three style spreads
          (Value VTV-VUG, Size IWM-SPY, Momentum MTUM-SPY) keeping legs with
          r² ≥ 0.10. A "factor" fund indistinguishable from its benchmark is
          a closet index at a worse fee.
  val   — basket valuation from issuer characteristics: P/B for value-tilted
          funds (P/B is the sort key those funds actually use), else P/E.

Missing-input policy matches the equity idiom exactly: ×0.85 + partial, so a
sparse ETF sinks below full-data rows in the SHARED evaluation sort instead
of floating on unknowns. (For Avantis names the val/er inputs arrive via the
issuer adapter or FMP enrichment; until then they read 0.85 — honest, not
neutral.)

Pure scorer (:func:`score_etf`) / I/O gatherer (:func:`compute_etf_score`)
split, same as ``candidate_fit``. The gatherer is price-history-heavy —
morning Stage 0f materializes it (``etf_score_cache``); the render path only
ever reads the cache.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from allocation.candidate_fit import ANNUALIZE, TRADING_DAYS, mean_var
from allocation.price_history import daily_log_returns, load_daily_closes
from instrument_store import get_etf_profile
from models.instruments import EtfProfile
from pipeline.research_cockpit import AttractivenessBreakdown, AttractivenessFactor

_MISSING_FACTOR = 0.85

#: Sharpe window: up to two years of common days, at least the fit floor.
RET_LOOKBACK_OBS = 504
RET_MIN_OBS = 120

# ΔSR = SR_etf - SR_SPY (rf=0 both sides), best-first (>= threshold).
_RET_BANDS: tuple[tuple[float, float], ...] = (
    (0.30, 1.40),
    (0.10, 1.20),
    (-0.10, 1.00),
    (-0.30, 0.80),
)
_RET_FLOOR = 0.65

# Expense ratio (decimal), lower-better (<= threshold, best-first).
_ER_BANDS: tuple[tuple[float, float], ...] = (
    (0.0010, 1.15),
    (0.0025, 1.05),
    (0.0050, 1.00),
    (0.0075, 0.90),
)
_ER_FLOOR = 0.80

# Factor distinctiveness D = Σ|β| over qualifying style legs (>= threshold).
_PREM_BANDS: tuple[tuple[float, float], ...] = (
    (1.00, 1.20),
    (0.50, 1.10),
    (0.20, 1.00),
)
_PREM_FLOOR = 0.95  # closet benchmark
PREM_MIN_R2 = 0.10

# Basket valuation, lower-better (<= threshold, best-first).
_PE_BANDS: tuple[tuple[float, float], ...] = (
    (14.0, 1.15),
    (20.0, 1.05),
    (27.0, 1.00),
    (35.0, 0.90),
)
_PB_BANDS: tuple[tuple[float, float], ...] = ((1.2, 1.15), (2.0, 1.05), (3.0, 1.00), (4.5, 0.90))
_VAL_FLOOR = 0.80

_FACTOR_LABELS: dict[str, str] = {
    "ret": "Risk-adj return",
    "er": "Expense drag",
    "prem": "Factor premium",
    "val": "Basket valuation",
}

_BENCH = "SPY"


@dataclass(frozen=True, slots=True)
class StyleLoadingRead:
    """One style leg's OLS read, carried into the cache for the workup."""

    key: str  # 'value' | 'size' | 'momentum'
    beta: float
    r_squared: float
    n_obs: int


@dataclass(frozen=True, slots=True)
class EtfScoreInputs:
    """Everything the pure scorer needs — the gatherer's output."""

    ticker: str
    delta_sharpe_vs_bench: float | None = None  # SR_etf - SR_SPY, rf=0
    ret_obs: int | None = None
    expense_ratio: float | None = None  # decimal
    distinctiveness: float | None = None  # Σ|β| over qualifying legs
    loadings: tuple[StyleLoadingRead, ...] = ()
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    value_tilted: bool = False  # P/B is the valuation lens for value funds


def _band_ge(value: float, bands: tuple[tuple[float, float], ...], floor: float) -> float:
    for threshold, mult in bands:
        if value >= threshold:
            return mult
    return floor


def _band_le(value: float, bands: tuple[tuple[float, float], ...], floor: float) -> float:
    for threshold, mult in bands:
        if value <= threshold:
            return mult
    return floor


def score_etf(inputs: EtfScoreInputs) -> AttractivenessBreakdown:
    """Pure scorer: band tables → multipliers → product → why. Same output
    type as the equity breakdown so chips/peeks render it unchanged."""
    raw: list[tuple[str, float, str]] = []

    if inputs.delta_sharpe_vs_bench is None:
        raw.append(("ret", _MISSING_FACTOR, "n/a"))
    else:
        obs = f", n={inputs.ret_obs}" if inputs.ret_obs else ""
        raw.append(
            (
                "ret",
                _band_ge(inputs.delta_sharpe_vs_bench, _RET_BANDS, _RET_FLOOR),
                f"dSR {inputs.delta_sharpe_vs_bench:+.2f} vs {_BENCH}{obs}",
            )
        )

    if inputs.expense_ratio is None:
        raw.append(("er", _MISSING_FACTOR, "n/a"))
    else:
        raw.append(
            (
                "er",
                _band_le(inputs.expense_ratio, _ER_BANDS, _ER_FLOOR),
                f"{inputs.expense_ratio * 1e4:.0f} bps/yr",
            )
        )

    if inputs.distinctiveness is None:
        raw.append(("prem", _MISSING_FACTOR, "n/a"))
    else:
        legs = " ".join(f"{ld.key} {ld.beta:+.2f}" for ld in inputs.loadings)
        detail = f"D={inputs.distinctiveness:.2f}" + (f" ({legs})" if legs else "")
        raw.append(("prem", _band_ge(inputs.distinctiveness, _PREM_BANDS, _PREM_FLOOR), detail))

    if inputs.value_tilted and inputs.pb_ratio is not None and inputs.pb_ratio > 0:
        raw.append(
            ("val", _band_le(inputs.pb_ratio, _PB_BANDS, _VAL_FLOOR), f"P/B {inputs.pb_ratio:.1f}")
        )
    elif inputs.pe_ratio is not None and inputs.pe_ratio > 0:
        raw.append(
            ("val", _band_le(inputs.pe_ratio, _PE_BANDS, _VAL_FLOOR), f"P/E {inputs.pe_ratio:.1f}")
        )
    elif inputs.pb_ratio is not None and inputs.pb_ratio > 0:
        raw.append(
            ("val", _band_le(inputs.pb_ratio, _PB_BANDS, _VAL_FLOOR), f"P/B {inputs.pb_ratio:.1f}")
        )
    else:
        raw.append(("val", _MISSING_FACTOR, "n/a"))

    score = 1.0
    for _, mult, _ in raw:
        score *= mult
    partial = any(detail == "n/a" for _, _, detail in raw)
    why = (
        " x ".join(f"{name} {mult:.2f} ({detail})" for name, mult, detail in raw)
        + f" = {score:.2f}"
    )
    factors = [
        AttractivenessFactor(
            key=name,
            label=_FACTOR_LABELS[name],
            multiplier=mult,
            detail=detail,
            missing=detail == "n/a",
        )
        for name, mult, detail in raw
    ]
    return AttractivenessBreakdown(factors=factors, score=score, why=why, partial=partial)


# --------------------------------------------------------------------------- #
# Gatherer (price history + etf_profile I/O) — Stage 0f, never the render path
# --------------------------------------------------------------------------- #


def _rf0_sharpe(returns: dict[date, float], dates: list[date]) -> float | None:
    xs = [returns[d] for d in dates]
    mean, var = mean_var(xs)
    sd = math.sqrt(var)
    if sd <= 0.0:
        return None
    return (mean * TRADING_DAYS) / (sd * ANNUALIZE)


def _delta_sharpe(repo_root: Path, ticker: str) -> tuple[float | None, int | None]:
    """SR_etf - SR_SPY over the latest ≤RET_LOOKBACK_OBS common days (rf=0 —
    it cancels in the difference up to the vol ratio, and keeps the read free
    of the tracker's rf)."""
    etf = daily_log_returns(load_daily_closes(ticker, repo_root))
    spy = daily_log_returns(load_daily_closes(_BENCH, repo_root))
    common = sorted(set(etf) & set(spy))[-RET_LOOKBACK_OBS:]
    if len(common) < RET_MIN_OBS:
        return None, None
    sr_etf = _rf0_sharpe(etf, common)
    sr_spy = _rf0_sharpe(spy, common)
    if sr_etf is None or sr_spy is None:
        return None, None
    return sr_etf - sr_spy, len(common)


def _style_loadings(repo_root: Path, ticker: str) -> tuple[StyleLoadingRead, ...]:
    """OLS loadings on the three style spreads (the exact legs the Risk tab
    rolls up), keeping legs with r² ≥ PREM_MIN_R2."""
    from factor_proxies import load_proxy_returns
    from portfolio_style_factors import STYLE_FACTORS, factor_spread_returns, regress_loading

    etf = daily_log_returns(load_daily_closes(ticker, repo_root))
    if not etf:
        return ()
    proxies = load_proxy_returns(repo_root)
    out: list[StyleLoadingRead] = []
    for factor in STYLE_FACTORS:
        spread = factor_spread_returns(proxies, factor)
        if not spread:
            continue
        loading = regress_loading(etf, spread)
        if loading is None or loading.r_squared < PREM_MIN_R2:
            continue
        out.append(
            StyleLoadingRead(
                key=factor.key,
                beta=loading.beta,
                r_squared=loading.r_squared,
                n_obs=loading.n_obs,
            )
        )
    return tuple(out)


def _is_value_tilted(profile: EtfProfile | None) -> bool:
    """P/B is the valuation lens for value funds — read the tilt off the fund's
    own name/benchmark (the issuer's stated strategy, not a guess)."""
    if profile is None:
        return False
    blob = " ".join(filter(None, (profile.name, profile.benchmark_index))).lower()
    return "value" in blob


def gather_etf_score_inputs(
    conn: sqlite3.Connection, repo_root: Path, ticker: str
) -> EtfScoreInputs:
    """Everything the scorer needs, from the price caches + etf_profile."""
    t = ticker.strip().upper()
    delta, obs = _delta_sharpe(repo_root, t)
    loadings = _style_loadings(repo_root, t)
    distinctiveness = sum(abs(ld.beta) for ld in loadings) if loadings else None
    try:
        profile = get_etf_profile(conn, t)
    except sqlite3.Error:
        profile = None
    return EtfScoreInputs(
        ticker=t,
        delta_sharpe_vs_bench=delta,
        ret_obs=obs,
        expense_ratio=profile.expense_ratio if profile else None,
        distinctiveness=distinctiveness,
        loadings=loadings,
        pe_ratio=profile.pe_ratio if profile else None,
        pb_ratio=profile.pb_ratio if profile else None,
        value_tilted=_is_value_tilted(profile),
    )


def compute_etf_score(
    conn: sqlite3.Connection, repo_root: Path, ticker: str
) -> AttractivenessBreakdown:
    """Gather + score one ETF (Stage 0f's per-name unit of work)."""
    return score_etf(gather_etf_score_inputs(conn, repo_root, ticker))
