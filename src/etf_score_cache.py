"""Materialized ETF scores — the render path reads Stage 0f's last compute.

The exact ``candidate_fit_cache`` pattern for the ETF sibling: the gatherer
(``pipeline.etf_score``) is a price-history read per name (Sharpe window +
three style regressions), far too heavy for GET; the morning pipeline
materializes every evaluation-list ETF into ``data/etf_score.json`` (a tail
call inside Stage 0f's ``refresh_candidate_fit.py`` — same budget, no new
stage) and the cockpit/peek read it back.

The payload also carries each ETF's raw style loadings — the workup fragment
renders them without re-running the OLS.

Last-good semantics mirror the siblings: missing/unreadable file → ``{}``
(no Score chip), atomic temp-file writes, and a payload the reader decodes
defensively field-by-field.
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

from pipeline.etf_score import StyleLoadingRead, compute_etf_score, gather_etf_score_inputs
from pipeline.research_cockpit import AttractivenessBreakdown, AttractivenessFactor

__all__ = [
    "evaluation_etf_tickers",
    "materialize_etf_scores",
    "read_materialized_etf_loadings",
    "read_materialized_etf_scores",
]

_CACHE_REL: tuple[str, ...] = ("data", "etf_score.json")


def _cache_path(repo_root: Path) -> Path:
    return repo_root.joinpath(*_CACHE_REL)


def evaluation_etf_tickers(conn: sqlite3.Connection) -> list[str]:
    """Non-archived evaluation-list ETFs — the names this cache scores.
    Degrades to [] on a pre-0044 substrate (missing instrument_type)."""
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE list_type = 'evaluation' AND archived_at IS NULL "
            "AND LOWER(COALESCE(instrument_type, '')) = 'etf'"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(r[0]).upper() for r in rows if r[0]]


def materialize_etf_scores(conn: sqlite3.Connection, repo_root: Path) -> int:
    """Score every evaluation ETF and write the cache atomically; returns the
    number scored. Zero ETFs still writes (an empty cache is the valid 'no
    ETFs tracked' answer, and it clears stale entries after an archive)."""
    tickers = evaluation_etf_tickers(conn)
    scores: dict[str, dict[str, object]] = {}
    loadings: dict[str, list[dict[str, object]]] = {}
    for t in tickers:
        inputs = gather_etf_score_inputs(conn, repo_root, t)
        bd = compute_etf_score(conn, repo_root, t)
        scores[t] = _breakdown_to_json(bd)
        loadings[t] = [
            {"key": ld.key, "beta": ld.beta, "r_squared": ld.r_squared, "n_obs": ld.n_obs}
            for ld in inputs.loadings
        ]
    payload = {
        "computed_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "scores": scores,
        "loadings": loadings,
    }
    path = _cache_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix="etf_score.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return len(scores)


def read_materialized_etf_scores(repo_root: Path) -> dict[str, AttractivenessBreakdown]:
    """ticker → breakdown from the cache; ``{}`` when absent/unreadable. Pure
    disk read — the cockpit's and score peek's only ETF-score source."""
    payload = _read_payload(repo_root)
    raw_scores = payload.get("scores")
    if not isinstance(raw_scores, dict):
        return {}
    out: dict[str, AttractivenessBreakdown] = {}
    for ticker, blob in cast("dict[str, object]", raw_scores).items():
        bd = _breakdown_from_json(blob)
        if bd is not None:
            out[str(ticker).upper()] = bd
    return out


def read_materialized_etf_loadings(repo_root: Path) -> dict[str, list[StyleLoadingRead]]:
    """ticker → raw style loadings (for the workup fragment); ``{}`` on absence."""
    payload = _read_payload(repo_root)
    raw = payload.get("loadings")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[StyleLoadingRead]] = {}
    for ticker, rows in cast("dict[str, object]", raw).items():
        if not isinstance(rows, list):
            continue
        decoded: list[StyleLoadingRead] = []
        for row in cast("list[object]", rows):
            if not isinstance(row, dict):
                continue
            r = cast("dict[str, object]", row)
            beta, r2 = r.get("beta"), r.get("r_squared")
            if not isinstance(beta, (int, float)) or not isinstance(r2, (int, float)):
                continue
            n_obs = r.get("n_obs")
            decoded.append(
                StyleLoadingRead(
                    key=str(r.get("key", "")),
                    beta=float(beta),
                    r_squared=float(r2),
                    n_obs=int(n_obs) if isinstance(n_obs, (int, float)) else 0,
                )
            )
        out[str(ticker).upper()] = decoded
    return out


def _read_payload(repo_root: Path) -> dict[str, object]:
    try:
        raw = _cache_path(repo_root).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return cast("dict[str, object]", payload)


def _breakdown_to_json(bd: AttractivenessBreakdown) -> dict[str, object]:
    return {
        "score": bd.score,
        "why": bd.why,
        "partial": bd.partial,
        "factors": [
            {
                "key": f.key,
                "label": f.label,
                "multiplier": f.multiplier,
                "detail": f.detail,
                "missing": f.missing,
            }
            for f in bd.factors
        ],
    }


def _breakdown_from_json(blob: object) -> AttractivenessBreakdown | None:
    if not isinstance(blob, dict):
        return None
    rec = cast("dict[str, object]", blob)
    raw_factors = rec.get("factors")
    if not isinstance(raw_factors, list):
        return None
    factors: list[AttractivenessFactor] = []
    for raw in cast("list[object]", raw_factors):
        if not isinstance(raw, dict):
            continue
        fr = cast("dict[str, object]", raw)
        mult = fr.get("multiplier")
        factors.append(
            AttractivenessFactor(
                key=str(fr.get("key", "")),
                label=str(fr.get("label", "")),
                multiplier=float(mult) if isinstance(mult, (int, float)) else 1.0,
                detail=str(fr.get("detail", "")),
                missing=bool(fr.get("missing", False)),
            )
        )
    score_v = rec.get("score")
    return AttractivenessBreakdown(
        factors=factors,
        score=float(score_v) if isinstance(score_v, (int, float)) else 1.0,
        why=str(rec.get("why", "")),
        partial=bool(rec.get("partial", False)),
    )
