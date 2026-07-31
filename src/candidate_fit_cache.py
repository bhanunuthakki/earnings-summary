"""Materialized portfolio-fit per evaluation name — the render path reads the
last-computed :class:`~allocation.candidate_fit.CandidateFit` per candidate from
disk, never the price files or the tracker.

Why this exists (the same argument as ``cockpit_fundamentals`` and
``portfolio_weights``): the fit math is a covariance-grade price-history read per
candidate plus a tracker round-trip — far too heavy for the GET / render path.
The morning pipeline runs :func:`materialize_candidate_fit` once (Stage 0f, after
the weights cache and the DCF re-price are fresh) and writes
``data/candidate_fit.json``; the cockpit's Evaluation table reads it back with
:func:`read_materialized_candidate_fit`.

Book state assembly (:func:`assemble_book_context`) is where the cohesion with
the tracker lives: the held-book Sharpe + risk-free rate come from the tracker's
``/api/portfolio/beta``, the sector weights from ``/api/portfolio/positioning``,
and the growth tilt from the same per-name beta roll-up the Risk tab uses
(``portfolio_risk.factor_exposure_rollup``). The held weights come from the
already-materialized ``portfolio_weights.json`` (Stage 0c). When the tracker is
unreachable during the morning run, it falls back to the cached risk snapshot for
Sharpe + growth tilt (the risk-free rate and sector weights are tracker-only, so
those factors degrade to partial — never faked).

Last-good semantics mirror the sibling caches: a missing / unreadable file
degrades to ``{}`` (the Evaluation table simply shows no Fit chip), and the
atomic temp-file write means a crashed run never leaves a half-written cache.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from allocation.candidate_fit import (
    BookContext,
    CandidateFit,
    FitFactor,
    compute_candidate_fit,
)

__all__ = [
    "assemble_book_context",
    "materialize_candidate_fit",
    "read_materialized_candidate_fit",
    "read_materialized_fit_meta",
]

# data/candidate_fit.json, repo-root relative (the data/ disk-cache home).
_CACHE_REL: tuple[str, ...] = ("data", "candidate_fit.json")

# Analytics-fetch read timeout for the book-state assembly. The client's default
# (_ANALYTICS_TIMEOUT_SECONDS, 6s) is tuned for the render path, where a slow
# tracker must degrade fast rather than block a page. This assembly runs in the
# morning pipeline (Stage 0f), off the render path — so we wait longer, because
# /beta recomputes a year-long regression and can take ~10s on a cold box.
# Without this, the live fetch times out, Sharpe/rf/sector come back null, and
# every candidate's Marginal-Sharpe + Sector factors degrade to partial.
_MORNING_ANALYTICS_TIMEOUT_SECONDS = 15.0


def _cache_path(repo_root: Path) -> Path:
    return repo_root.joinpath(*_CACHE_REL)


# --------------------------------------------------------------------------- #
# Book-state assembly (tracker + caches → BookContext)
# --------------------------------------------------------------------------- #


def assemble_book_context(
    repo_root: Path, *, db_path: Path, api_url: str | None = None
) -> BookContext:
    """The held-book state the fit factors compare against.

    Held weights from the materialized cache (Stage 0c); book Sharpe + risk-free
    rate, sector weights, and growth tilt from a LIVE tracker fetch (this runs in
    the morning pipeline, so the network is fine), falling back to the cached risk
    snapshot for Sharpe + growth tilt when the tracker is down. The risk-free rate
    and sector weights are tracker-only — absent offline, which leaves the
    Marginal-Sharpe and Sector factors partial rather than guessed."""
    from integrations.portfolio_tracker_client import fetch_portfolio_analytics
    from portfolio_risk import factor_exposure_rollup
    from portfolio_risk_snapshot_store import read_latest_snapshot
    from portfolio_weights import read_materialized_weights

    weights = read_materialized_weights(repo_root)
    analytics = fetch_portfolio_analytics(
        api_url=api_url,
        timeout=_MORNING_ANALYTICS_TIMEOUT_SECONDS,
        only={"beta", "positioning"},
    )
    snapshot = read_latest_snapshot(db_path=db_path)

    # Loud degradation: every absent book input records a human-readable
    # reason. Factors still contribute a mathematical 1.0 (unknowable must not
    # read dilutive), but the chips/peeks can now SAY the read is degraded
    # instead of silently dimming.
    degraded: list[str] = []
    tracker_up = analytics.available

    sharpe: float | None = None
    risk_free_annual: float | None = None
    if tracker_up and analytics.beta is not None:
        sharpe = analytics.beta.sharpe
        risk_free_annual = analytics.beta.risk_free_annual
    if sharpe is None and snapshot is not None:
        sharpe = snapshot.sharpe
        if sharpe is not None:
            degraded.append(
                f"tracker offline — book Sharpe from the cached risk snapshot "
                f"(as of {snapshot.captured_at or 'unknown'})"
            )
    if sharpe is None:
        degraded.append("tracker offline and no risk snapshot — book Sharpe unknown")
    if risk_free_annual is None:
        degraded.append("risk-free rate is tracker-only — Marginal-Sharpe leg unscored")

    # Same source + same fallback pattern as book Sharpe above — the tracker's
    # /beta portfolio_volatility_annualized (the Risk tab's "Portfolio sigma"
    # card), falling back to the cached risk snapshot's own copy of that figure.
    vol_ann: float | None = None
    if tracker_up and analytics.beta is not None:
        vol_ann = analytics.beta.portfolio_volatility_annualized
    if vol_ann is None and snapshot is not None:
        vol_ann = snapshot.portfolio_volatility_annualized
        if vol_ann is not None:
            degraded.append(
                f"tracker offline — book vol from the cached risk snapshot "
                f"(as of {snapshot.captured_at or 'unknown'})"
            )
    if vol_ann is None:
        degraded.append("tracker offline and no risk snapshot — book vol unknown")

    growth_tilt: float | None = None
    sector_weights: dict[str, float] = {}
    if tracker_up and analytics.positioning is not None:
        rollup = factor_exposure_rollup(analytics.positioning.correlations)
        if rollup is not None:
            growth_tilt = rollup.growth_tilt
        # AllocationBucket.weight_pct is in percent; the fit bands are fractions.
        # NB the candidate sector (FMP profile) and these labels must share a
        # taxonomy — a mismatch reads as an unheld sector (a mild lift), never a
        # crash; refine the label map if the two sources drift.
        for bucket in analytics.positioning.by_sector:
            if bucket.label and bucket.weight_pct is not None:
                sector_weights[bucket.label] = bucket.weight_pct / 100.0
    if growth_tilt is None and snapshot is not None:
        growth_tilt = snapshot.growth_tilt
    if not sector_weights:
        degraded.append("sector weights are tracker-only — sector factors unscored")
    if growth_tilt is None:
        degraded.append("book growth tilt unknown (no tracker, no snapshot)")
    if not weights:
        degraded.append("weights cache empty — book series undefined")

    captured_at = snapshot.captured_at if snapshot is not None else None
    return BookContext(
        weights=weights,
        sharpe=sharpe,
        risk_free_annual=risk_free_annual,
        growth_tilt=growth_tilt,
        vol_ann=vol_ann,
        sector_weights=sector_weights,
        captured_at=captured_at or None,
        degraded=tuple(degraded),
    )


def _evaluation_tickers(conn: sqlite3.Connection) -> list[str]:
    """Tracked, non-archived evaluation-list names — the cockpit's Score/Fit
    table. Degrades to [] on a minimal/partial schema (missing column)."""
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE list_type = 'evaluation' AND archived_at IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(r[0]).upper() for r in rows if r[0]]


def _etf_tickers(conn: sqlite3.Connection, candidates: list[str]) -> set[str]:
    """The evaluation candidates that are ETFs (pre-0044 substrate → none)."""
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE LOWER(COALESCE(instrument_type, '')) = 'etf'"
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    etf = {str(r[0]).upper() for r in rows if r[0]}
    return etf & set(candidates)


def _etf_overlaps_and_sleeves(
    conn: sqlite3.Connection, repo_root: Path, etf_tickers: set[str], weights: dict[str, float]
) -> tuple[dict[str, tuple[float, str]], dict[str, tuple[str, ...]]]:
    """Per-ETF look-through overlap + served-sleeve reads for the fit factors.

    A handful of names at most, so the one extra size-spread OLS per ETF is
    cheap. Missing holdings/spreads degrade per name (the factors are then
    omitted, never guessed)."""
    from allocation.price_history import daily_log_returns, load_daily_closes
    from etf_overlap import compute_lookthrough_overlap, overlap_detail, sleeves_served
    from factor_proxies import load_proxy_returns
    from portfolio_style_factors import STYLE_FACTORS, factor_spread_returns, regress_loading

    overlaps: dict[str, tuple[float, str]] = {}
    sleeves: dict[str, tuple[str, ...]] = {}
    if not etf_tickers:
        return overlaps, sleeves
    proxies = load_proxy_returns(repo_root)
    size_def = next((f for f in STYLE_FACTORS if f.key == "size"), None)
    size_spread = factor_spread_returns(proxies, size_def) if size_def is not None else {}
    for t in sorted(etf_tickers):
        ov = compute_lookthrough_overlap(conn, t, weights)
        size_beta: float | None = None
        if size_spread:
            rets = daily_log_returns(load_daily_closes(t, repo_root))
            loading = regress_loading(rets, size_spread) if rets else None
            size_beta = loading.beta if loading is not None else None
        if ov is not None:
            overlaps[t] = (ov.direct_overlap_pct, overlap_detail(ov))
        served = sleeves_served(ov, size_beta=size_beta)
        if served:
            sleeves[t] = served
    return overlaps, sleeves


def _load_sectors(repo_root: Path, tickers: list[str]) -> dict[str, str]:
    """ticker → FMP-profile sector, for the candidates that have a profile cache.
    Mirrors ``evaluation_snapshot._load_profile_fields`` parsing (the endpoint
    returns a one-element list, occasionally a bare object)."""
    fmp = repo_root / "data" / "historical" / "fmp"
    out: dict[str, str] = {}
    for t in tickers:
        path = fmp / f"{t}_profile.json"
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rec: dict[str, object] | None = None
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            rec = cast("dict[str, object]", payload[0])
        elif isinstance(payload, dict):
            rec = cast("dict[str, object]", payload)
        if rec is None:
            continue
        sector = rec.get("sector")
        if isinstance(sector, str) and sector.strip():
            out[t] = sector.strip()
    return out


# --------------------------------------------------------------------------- #
# Materialize / read
# --------------------------------------------------------------------------- #


def materialize_candidate_fit(
    conn: sqlite3.Connection, repo_root: Path, *, db_path: Path, api_url: str | None = None
) -> int:
    """Compute and write the candidate-fit cache atomically; returns the number
    of evaluation names scored. The cache is only overwritten on a successful
    computation (a tracker outage still yields partial fits from the cached
    weights + price history — see :func:`assemble_book_context`)."""
    candidates = _evaluation_tickers(conn)
    book = assemble_book_context(repo_root, db_path=db_path, api_url=api_url)
    sectors = _load_sectors(repo_root, candidates)
    # ETF candidates: the profile 'sector' is meaningless for a fund (drop it —
    # the factor is then omitted-equivalent), and look-through overlap + sleeve
    # reads take its place (etf_overlap.py).
    etf_set = _etf_tickers(conn, candidates)
    for t in etf_set:
        sectors.pop(t, None)
    overlaps, sleeves = _etf_overlaps_and_sleeves(conn, repo_root, etf_set, book.weights)
    # The active positioning target (a pure local DB read — no new tracker
    # dependency; with no saved intent this is the book default and the fit
    # numbers are identical to v1).
    from positioning.target import resolve_target_context

    target = resolve_target_context(db_path, book)
    fits = compute_candidate_fit(
        repo_root,
        candidates,
        book,
        sectors=sectors,
        target=target,
        overlaps=overlaps,
        sleeves=sleeves,
    )

    payload = {
        "version": 2,
        "computed_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "book": {
            "sharpe": book.sharpe,
            "growth_tilt": book.growth_tilt,
            "risk_free_annual": book.risk_free_annual,
            "vol_ann": book.vol_ann,
            "captured_at": book.captured_at,
            "degraded": list(book.degraded),
        },
        "target": {
            "source": target.source,
            "intent_id": target.intent_id,
            "created_at": target.intent_created_at,
            "narrative": target.narrative,
            "growth_tilt": target.growth_tilt,
            "sector_weights": target.sector_weights,
            "sector_bands": target.sector_bands,
            "sleeves": target.sleeves,
        },
        "fits": {t: _fit_to_json(f) for t, f in fits.items()},
    }
    path = _cache_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix="candidate_fit.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return len(fits)


def read_materialized_candidate_fit(repo_root: Path) -> dict[str, CandidateFit]:
    """ticker → :class:`CandidateFit` from the cache; ``{}`` when absent,
    unreadable, or malformed. A pure disk read — never the tracker or the price
    files. This is the render path's only fit source."""
    try:
        raw = _cache_path(repo_root).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    fits = cast("dict[str, object]", payload).get("fits")
    if not isinstance(fits, dict):
        return {}
    out: dict[str, CandidateFit] = {}
    for ticker, blob in cast("dict[str, object]", fits).items():
        fit = _fit_from_json(str(ticker).upper(), blob)
        if fit is not None:
            out[str(ticker).upper()] = fit
    return out


def read_materialized_fit_meta(repo_root: Path) -> dict[str, object]:
    """The cache's ``book`` + ``target`` blocks (plus version/computed_at) for
    surfaces that need the context rather than per-ticker fits — the cockpit's
    degradation banner, the fit peek's target header, the what-if route's book
    inputs. ``{}`` when the cache is absent/unreadable; a v1 payload simply
    lacks the ``target``/``degraded`` keys."""
    try:
        raw = _cache_path(repo_root).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    rec = cast("dict[str, object]", payload)
    return {k: rec.get(k) for k in ("version", "computed_at", "book", "target") if k in rec}


def _factor_to_json(f: FitFactor) -> dict[str, object]:
    return {
        "key": f.key,
        "label": f.label,
        "multiplier": f.multiplier,
        "detail": f.detail,
        "missing": f.missing,
    }


def _fit_to_json(fit: CandidateFit) -> dict[str, object]:
    return {
        "fit": fit.fit,
        "why": fit.why,
        "partial": fit.partial,
        "obs": fit.obs,
        "factors": [_factor_to_json(f) for f in fit.factors],
        # ---- v2 fields (readers tolerate their absence — v1 payloads) ----
        "fit_target": fit.fit_target,
        "target_factors": [_factor_to_json(f) for f in fit.target_factors],
        "sharpe_delta_bps": fit.sharpe_delta_bps,
        "corr_trend": fit.corr_trend,
        "corr_recent": fit.corr_recent,
        "degraded": list(fit.degraded),
        "held_weight": fit.held_weight,
    }


def _factors_from_json(raw_factors: object) -> list[FitFactor]:
    factors: list[FitFactor] = []
    if not isinstance(raw_factors, list):
        return factors
    for raw in cast("list[object]", raw_factors):
        if not isinstance(raw, dict):
            continue
        fr = cast("dict[str, object]", raw)
        mult = fr.get("multiplier")
        factors.append(
            FitFactor(
                key=str(fr.get("key", "")),
                label=str(fr.get("label", "")),
                multiplier=float(mult) if isinstance(mult, (int, float)) else 1.0,
                detail=str(fr.get("detail", "")),
                missing=bool(fr.get("missing", False)),
            )
        )
    return factors


def _opt_float(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _fit_from_json(ticker: str, blob: object) -> CandidateFit | None:
    if not isinstance(blob, dict):
        return None
    rec = cast("dict[str, object]", blob)
    raw_factors = rec.get("factors")
    if not isinstance(raw_factors, list):
        return None
    factors = _factors_from_json(cast("list[object]", raw_factors))
    fit_v = rec.get("fit")
    obs_v = rec.get("obs")
    corr_trend = rec.get("corr_trend")
    raw_degraded = rec.get("degraded")
    degraded = (
        tuple(str(d) for d in cast("list[object]", raw_degraded))
        if isinstance(raw_degraded, list)
        else ()
    )
    return CandidateFit(
        ticker=ticker,
        factors=factors,
        fit=float(fit_v) if isinstance(fit_v, (int, float)) else 1.0,
        why=str(rec.get("why", "")),
        partial=bool(rec.get("partial", False)),
        obs=int(obs_v) if isinstance(obs_v, (int, float)) else None,
        # v2 fields — absent on a v1 payload, which decodes to the v1 shape.
        fit_target=_opt_float(rec.get("fit_target")),
        target_factors=_factors_from_json(rec.get("target_factors")),
        sharpe_delta_bps=_opt_float(rec.get("sharpe_delta_bps")),
        corr_trend=str(corr_trend) if isinstance(corr_trend, str) else None,
        corr_recent=_opt_float(rec.get("corr_recent")),
        degraded=degraded,
        held_weight=_opt_float(rec.get("held_weight")),
    )
