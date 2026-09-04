"""Materialized portfolio weights — the render path reads the last-computed
fraction-of-book per ticker from disk, never the live tracker.

Why this exists (directive interaction_paradigm_2026_06 §4 S12, "move
``live_position_weights`` off the render path"): the inbox ranking factor needs
each ticker's weight, but the companion tracker is a NETWORK call (~0.5s of
connect-refusal when it is offline — measured on the GET / boot path). A render
must never block on it. The morning-pipeline reconciliation rung (stage 0c,
``position_lifecycle.sync_position_lifecycle``) already fetches the live
portfolio to reconcile ``position_entries`` — so it materializes the weights
here in the SAME pass, and the inbox render reads the cached value.

Last-good semantics: only an AVAILABLE snapshot overwrites the cache
(``materialize_weights`` is a no-op when the tracker is offline), so a transient
outage leaves the weights the render depends on intact. A missing cache (never
reconciled with an available tracker) degrades to ``{}`` → equal weighting —
exactly what the old in-render live fallback did when the tracker was down.

This is a single small JSON cache (the same disk-cache pattern
``research_cockpit`` already reads on the render path), NOT a universal signals
spine.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from integrations.portfolio_tracker_client import LivePortfolio

__all__ = [
    "MaterializedWeightSnapshot",
    "materialize_weights",
    "read_materialized_weight_snapshot",
    "read_materialized_weights",
    "read_materialized_weights_as_of",
    "weights_from_portfolio",
]

# data/portfolio_weights.json, repo-root relative (the data/ disk-cache home).
_CACHE_REL: tuple[str, ...] = ("data", "portfolio_weights.json")


@dataclass(frozen=True, slots=True)
class MaterializedWeightSnapshot:
    computed_at: datetime
    weights: dict[str, float]


def _cache_path(repo_root: Path) -> Path:
    return repo_root.joinpath(*_CACHE_REL)


def weights_from_portfolio(portfolio: LivePortfolio) -> dict[str, float]:
    """``ticker`` (upper-cased) → fraction-of-book from a live snapshot.

    Mirrors the retired in-render derivation exactly: ``percent_of_portfolio``
    is in percent units, so divide by 100; clamp negatives to 0; skip positions
    missing either a ticker or a percent."""
    out: dict[str, float] = {}
    for p in portfolio.positions:
        if p.ticker and p.percent_of_portfolio is not None:
            out[p.ticker.upper()] = max(p.percent_of_portfolio, 0.0) / 100.0
    return out


def materialize_weights(repo_root: Path, portfolio: LivePortfolio) -> int:
    """Write the weights cache from an AVAILABLE portfolio snapshot.

    No-op returning 0 when the tracker was offline (``available=False``), so the
    last-good cache survives an outage. Otherwise writes
    ``{"computed_at": iso, "weights": {ticker: fraction}}`` atomically (temp file
    + ``os.replace``) and returns the number of weighted tickers."""
    if not portfolio.available:
        return 0
    weights = weights_from_portfolio(portfolio)
    payload = {
        "computed_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "weights": weights,
    }
    path = _cache_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix="portfolio_weights.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return len(weights)


def read_materialized_weight_snapshot(repo_root: Path) -> MaterializedWeightSnapshot | None:
    """Read one complete, validated snapshot for decision-support consumers.

    The rendering helpers below intentionally degrade malformed caches to an
    empty mapping. Economic simulations must instead distinguish a legitimate
    empty portfolio from an unreadable or internally invalid snapshot.
    """
    try:
        payload = json.loads(_cache_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    typed_payload = cast("dict[str, object]", payload)
    stamp = typed_payload.get("computed_at")
    raw_weights = typed_payload.get("weights")
    if not isinstance(stamp, str) or not isinstance(raw_weights, dict):
        return None
    try:
        computed_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=UTC)
    else:
        computed_at = computed_at.astimezone(UTC)

    weights: dict[str, float] = {}
    for ticker, value in cast("dict[str, object]", raw_weights).items():
        if (
            not ticker.strip()
            or ticker != ticker.strip()
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
        ):
            return None
        weight = float(value)
        if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
            return None
        weights[ticker.upper()] = weight
    return MaterializedWeightSnapshot(computed_at=computed_at, weights=weights)


def read_materialized_weights(repo_root: Path) -> dict[str, float]:
    """``ticker`` → fraction-of-book from the cache; ``{}`` when the cache is
    absent or unreadable. A pure disk read — never the network. This is the
    render path's only weight source."""
    path = _cache_path(repo_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    weights = cast("dict[str, object]", payload).get("weights")
    if not isinstance(weights, dict):
        return {}
    out: dict[str, float] = {}
    # JSON object keys are strings (the cast asserts it); only the values need
    # a runtime number check (and bool is an int subclass — reject it).
    for k, v in cast("dict[str, object]", weights).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k.upper()] = float(v)
    return out


def read_materialized_weights_as_of(repo_root: Path) -> str | None:
    """The materialized weights cache's ``computed_at`` stamp (naive-UTC ISO),
    or ``None`` when the cache is absent/unreadable. Separate from
    ``read_materialized_weights`` (which only needs the weights) because
    ``execution/export_book_cma.py`` uses this stamp as its idempotency key
    (``weights_as_of`` unchanged since the last export -> skip, matching the
    directive's "before executing, check whether the deliverable for that key
    already exists" rule) without every other caller paying for it."""
    path = _cache_path(repo_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    stamp = cast("dict[str, object]", payload).get("computed_at")
    return stamp if isinstance(stamp, str) else None
