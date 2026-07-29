"""The next-dollar allocation model: blend three factors into a distribution.

Where should the next incremental dollar go, across the current portfolio
holdings? Three inspectable factors per holding (each visible in the panel's
waterfall, never a black-box composite):

  ret   — absolute expected return: the probability-weighted DCF upside over
          the latest ``dcf_runs`` row's bull/base/bear scenario range
          (``dcf.scenario_reward``), so the reward leg reflects the asymmetry of
          the range, not just its midpoint. Degrades to the base point estimate
          (npv_per_share / live_price − 1) when the run carries no scenarios.
          The base leg is recomputed from the row's own two fields, never read
          from ``over_under_pct`` (same convention rule as
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

from allocation.book_risk import build_book_risk
from dcf.scenario_reward import scenario_reward
from macro_store import Sensitivity, fetch_sensitivities, fetch_series
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# The blend, in plain sight. Keys are factor ids used throughout; values are
# the target weights when every factor is live. Renormalized (a) model-wide
# when a factor is hidden for lack of data, (b) per holding over the factors
# that holding carries.
#
# tenet-2 Phase 2 (owner decision 5, §7 of docs/design/tenet2_advisory_program.md):
# this hardcoded 50/30/20 is now the LABELED NO-PROFILE FALLBACK only. When an
# AFFIRMED appetite fact `next_dollar.blend_weights` exists in
# `owner_profile_facts`, `build_next_dollar_model` uses it instead and stamps
# `NextDollarModel.blend_weights_source = "owner_profile"`; otherwise this
# constant is used and the model stamps `"default_fallback"`. The module
# constant itself never changes shape — it stays the seed value the packet-walk
# ratification card offers the owner (see
# execution/import_owner_capacity.py --seed-appetite).
BLEND_WEIGHTS: dict[str, float] = {"ret": 0.50, "div": 0.30, "macro": 0.20}
FACTOR_LABELS: dict[str, str] = {
    "ret": "expected return",
    "div": "diversification",
    "macro": "macro tilt",
}

SOFTMAX_TEMPERATURE = 1.0  # z-scale scores; ~e^3 spread between best and worst
MACRO_BETA_LOOKBACK = 252  # preferred macro_sensitivities lookback_window_days
MACRO_MOMENTUM_CAL_DAYS = 90  # macro momentum = log change over this window
MACRO_STALE_DAYS = 45  # ignore a macro series whose latest point is older
# Beta-quality floor (2026-07-19 review): below these, a sensitivity is
# regression noise, not exposure — it must not tilt the blend. The n floor is
# 30, not 40: the standard 252-trading-day lookback yields ~36 aligned WEEKLY
# observations (50 weeks minus series-alignment gaps — verified on the first
# post-revival recompute), so 40 would floor out every standard-lookback fit
# and turn the quality gate into "macro permanently off". r² stays primary.
MACRO_MIN_R_SQUARED = 0.05
MACRO_MIN_N_OBS = 30
RET_CLAMP = 1.0  # winsorize DCF upside at ±100% before z-scoring


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
    # Cash-aware mode (tenet-2 Phase 2, §7 decision 5b): allocation_pct x the
    # caller's cash_to_deploy_usd, in dollars. None when no cash figure was
    # supplied to build_next_dollar_model — the model then reads exactly as
    # it did pre-Phase-2 (a distribution, not a dollar plan).
    cash_allocation_usd: float | None = None

    def reading(self, key: str) -> FactorReading | None:
        return {"ret": self.ret, "div": self.div, "macro": self.macro}[key]


@dataclass(slots=True)
class NextDollarModel:
    rows: list[HoldingScore]  # sorted by allocation desc
    blend: list[tuple[str, float]]  # (factor id, model-wide weight) after hiding
    hidden_factors: dict[str, str]  # factor id -> why it is absent model-wide
    weights_source: str  # "tracker" | "equal" — CURRENT-BOOK weighting method
    prices_through: date | None  # last common trading date in the covariance
    cov_obs: int | None  # rows of the return matrix
    shrinkage: float | None  # Ledoit-Wolf delta
    portfolio_vol_ann: float | None  # modeled book vol, annualized fraction
    excluded: dict[str, str]  # ticker -> reason it has no row at all
    # Where the BLEND (ret/div/macro target weights) came from — distinct
    # from `weights_source` above, which is about CURRENT holding weights.
    # "owner_profile" when an affirmed next_dollar.blend_weights fact drove
    # the blend; "default_fallback" for the hardcoded BLEND_WEIGHTS house view.
    blend_weights_source: str = "default_fallback"
    # Cash-aware mode: the caller's input dollar amount, echoed back so a
    # renderer doesn't need to thread it through separately. None in the
    # default (distribution-only) mode.
    cash_to_deploy_usd: float | None = None
    notes: list[str] = field(default_factory=list[str])


def _effective_blend_weights(db_path: Path) -> tuple[dict[str, float], str]:
    """Resolve the next-dollar blend weights: an AFFIRMED owner appetite fact
    (``owner_profile_facts`` category='appetite', key='next_dollar.blend_weights')
    when present and valid, else the hardcoded :data:`BLEND_WEIGHTS` house view
    (owner decision 5 — the hardcode is now the labeled no-profile fallback).

    Never raises: a missing DB / pre-0159 substrate / absent fact / a value
    that fails :class:`owner_profile.models.NextDollarBlendWeights` validation
    (e.g. doesn't sum to ~1.0) all degrade to the fallback, source-labeled
    ``"default_fallback"``.
    """
    if not db_path.exists():
        return dict(BLEND_WEIGHTS), "default_fallback"
    try:
        from owner_profile.models import NextDollarBlendWeights
        from owner_profile.store import get_current_profile

        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            grouped = get_current_profile(conn)
        finally:
            conn.close()
        for row in grouped.get("appetite", []):
            if row.key != "next_dollar.blend_weights":
                continue
            weights = NextDollarBlendWeights.model_validate(row.value)
            return {"ret": weights.ret, "div": weights.div, "macro": weights.macro}, (
                "owner_profile"
            )
    except Exception:
        pass  # any problem (missing table, bad shape, sqlite lock) — fall back
    return dict(BLEND_WEIGHTS), "default_fallback"


def build_next_dollar_model(
    db_path: Path,
    repo_root: Path,
    holdings: Sequence[str],
    live_values: Mapping[str, float | None] | None = None,
    *,
    cash_to_deploy_usd: float | None = None,
) -> NextDollarModel | None:
    """Score the holdings and softmax into a next-dollar distribution.

    ``live_values`` is the tracker's market value per ticker (any subset);
    None or empty falls back to equal weights, labelled via
    ``weights_source``. Returns None when no factor has enough data to score
    anything — the caller falls back to its narrative-only rendering.

    ``cash_to_deploy_usd`` opts into cash-aware mode (tenet-2 Phase 2, §7
    decision 5b): when supplied, each row's ``cash_allocation_usd`` is
    ``allocation_pct / 100 * cash_to_deploy_usd`` — a concrete dollar plan for
    THIS cash, not just a distribution. Omitted (the default), every row's
    ``cash_allocation_usd`` stays ``None`` and the model behaves exactly as
    before Phase 2.

    The blend (ret/div/macro target weights) is profile-driven when the owner
    has affirmed a ``next_dollar.blend_weights`` appetite fact
    (:func:`_effective_blend_weights`), else the hardcoded :data:`BLEND_WEIGHTS`
    — labeled via ``NextDollarModel.blend_weights_source``.
    """
    tickers = [t.upper() for t in dict.fromkeys(holdings) if t]
    if not tickers:
        return None

    weights, weights_source = _current_weights(tickers, live_values)
    blend_weights, blend_weights_source = _effective_blend_weights(db_path)
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
    active = [k for k in blend_weights if k not in hidden]
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
        weight_sum = sum(blend_weights[k] for k in have)
        readings: dict[str, FactorReading] = {}
        score = 0.0
        for k in have:
            w_eff = blend_weights[k] / weight_sum
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
        if cash_to_deploy_usd is not None:
            row.cash_allocation_usd = row.allocation_pct / 100.0 * cash_to_deploy_usd
    rows.sort(key=lambda r: (-r.allocation_pct, r.ticker))

    active_total = sum(blend_weights[k] for k in active)
    blend = [(k, blend_weights[k] / active_total) for k in active]
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
        blend_weights_source=blend_weights_source,
        cash_to_deploy_usd=cash_to_deploy_usd,
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
    """Latest DCF *expected* upside per ticker, asymmetry-aware.

    The reward leg is the probability-weighted return over the run's bull/base/
    bear scenario range (``dcf.scenario_reward``) — not the bare base point
    estimate — so a name whose bear case is far below price is rewarded less than
    a symmetric one with the same midpoint. Falls back to exactly the base point
    estimate (``npv_per_share / live_price − 1``) when the run has no scenario
    range. The base leg is recomputed from the row's own two fields (currency-
    consistent), never from ``over_under_pct`` — the bank/holdco builders stored
    that column in a different convention. Same base rule as
    ``research_cockpit.latest_dcf_runs``."""
    if not db_path.exists():
        return {}
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        # The scenario block lives in assumption_snapshot_json (migration 0024+);
        # select it only when the column exists so hand-rolled test schemas (and
        # very old data dirs) still score on the base leg.
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
        snap_sel = ", assumption_snapshot_json" if "assumption_snapshot_json" in cols else ""
        rows = conn.execute(
            f"SELECT ticker, valuation_date, npv_per_share, live_price{snap_sel} "
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
        snapshot = r["assumption_snapshot_json"] if snap_sel else None
        reward = scenario_reward(price=px, base_fv=fv, snapshot_json=snapshot)
        if reward is None:
            continue
        as_of = str(r["valuation_date"]) if r["valuation_date"] else "?"
        out[t] = (reward.expected_return, f"{reward.detail} ({as_of})")
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

    A thin view over ``allocation.book_risk.build_book_risk`` (the one place the
    book covariance is assembled — also the L7 risk-budget allocator's risk
    leg): the diversification factor is the *negative* marginal vol, so a name
    that lowers book vol at the margin scores high. Weights are renormalized over
    the names that made it into the matrix; the note says who fell out and why."""
    br = build_book_risk(repo_root, tickers, weights)
    if br.hidden_reason is not None:
        return _DivResult(hidden_reason=br.hidden_reason)
    result = _DivResult(
        prices_through=br.prices_through,
        cov_obs=br.cov_obs,
        shrinkage=br.shrinkage,
        portfolio_vol_ann=br.portfolio_vol_ann,
    )
    for t in br.tickers:
        mvol_ann = br.marginal_vol_ann[t]
        corr = br.corr_to_book[t]
        result.readings[t] = (
            -mvol_ann,
            f"corr to book {corr:+.2f} · marginal vol {mvol_ann * 100.0:+.1f}%/yr",
        )
    if br.dropped:
        result.notes.append(
            "outside the covariance: "
            + "; ".join(f"{t} ({reason})" for t, reason in sorted(br.dropped.items()))
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
            # Beta-quality floor (2026-07-19 review): a fit explaining <5% of
            # variance — or regressed on <40 observations when n is known — is
            # noise, not exposure; MELI/NU/NVO carried indistinguishable
            # usd_cad betas at r² 0.01-0.10 tilting 20% of the blend. Unknown
            # n (pre-0184 rows) passes on the r² gate alone until recompute.
            if s.r_squared is None or s.r_squared < MACRO_MIN_R_SQUARED:
                continue
            if s.n_obs is not None and s.n_obs < MACRO_MIN_N_OBS:
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
