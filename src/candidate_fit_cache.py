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
        api_url=api_url, timeout=_MORNING_ANALYTICS_TIMEOUT_SECONDS
    )
    snapshot = read_latest_snapshot(db_path=db_path)

    sharpe: float | None = None
    risk_free_annual: float | None = None
    if analytics.available and analytics.beta is not None:
        sharpe = analytics.beta.sharpe
        risk_free_annual = analytics.beta.risk_free_annual
    if sharpe is None and snapshot is not None:
        sharpe = snapshot.sharpe

    growth_tilt: float | None = None
    sector_weights: dict[str, float] = {}
    if analytics.available and analytics.positioning is not None:
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

    captured_at = snapshot.captured_at if snapshot is not None else None
    return BookContext(
        weights=weights,
        sharpe=sharpe,
        risk_free_annual=risk_free_annual,
        growth_tilt=growth_tilt,
        sector_weights=sector_weights,
        captured_at=captured_at or None,
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
    fits = compute_candidate_fit(repo_root, candidates, book, sectors=sectors)

    payload = {
        "computed_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "book": {
            "sharpe": book.sharpe,
            "growth_tilt": book.growth_tilt,
            "risk_free_annual": book.risk_free_annual,
            "captured_at": book.captured_at,
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


def _fit_to_json(fit: CandidateFit) -> dict[str, object]:
    return {
        "fit": fit.fit,
        "why": fit.why,
        "partial": fit.partial,
        "obs": fit.obs,
        "factors": [
            {
                "key": f.key,
                "label": f.label,
                "multiplier": f.multiplier,
                "detail": f.detail,
                "missing": f.missing,
            }
            for f in fit.factors
        ],
    }


def _fit_from_json(ticker: str, blob: object) -> CandidateFit | None:
    if not isinstance(blob, dict):
        return None
    rec = cast("dict[str, object]", blob)
    raw_factors = rec.get("factors")
    if not isinstance(raw_factors, list):
        return None
    factors: list[FitFactor] = []
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
    fit_v = rec.get("fit")
    obs_v = rec.get("obs")
    return CandidateFit(
        ticker=ticker,
        factors=factors,
        fit=float(fit_v) if isinstance(fit_v, (int, float)) else 1.0,
        why=str(rec.get("why", "")),
        partial=bool(rec.get("partial", False)),
        obs=int(obs_v) if isinstance(obs_v, (int, float)) else None,
    )
