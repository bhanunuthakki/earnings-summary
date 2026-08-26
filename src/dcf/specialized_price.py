"""One governed market-price observation for specialized DCF builders.

The generic FCFF refresher already reads :mod:`sources.price` once and threads
that exact observation through the workbook, reverse valuation, provenance and
``dcf_runs``.  Specialized builders historically re-read only the cached FMP
profile, which let a successful rebuild retain a weeks-old price timestamp.

This adapter gives every specialized archetype the same single-read contract
without fabricating freshness when all price sources are unavailable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sources.price import read_live_price


@dataclass(frozen=True, slots=True)
class SpecializedPriceObservation:
    """The effective price and the evidence clock/source that supports it."""

    price: float
    observed_at: datetime | None
    source_name: str
    currency: str | None = None
    source_path: str | None = None


def resolve_specialized_price(
    repo_root: Path,
    ticker: str,
    *,
    fallback_price: float,
    fallback_source_name: str = "model_seed",
    fallback_source_path: str | None = None,
) -> SpecializedPriceObservation:
    """Return one governed quote, or the existing seed without fake freshness."""

    live = read_live_price(repo_root, ticker)
    if live is not None and math.isfinite(live.price) and live.price > 0:
        return SpecializedPriceObservation(
            price=live.price,
            observed_at=live.fetched_at,
            source_name=live.source_name,
            currency=live.currency,
            source_path=(
                f"data/historical/fmp/{ticker.upper()}_profile.json"
                if live.source_name == "fmp_cache"
                else None
            ),
        )
    if not math.isfinite(fallback_price) or fallback_price <= 0:
        raise ValueError(f"{ticker.upper()} fallback price must be finite and positive")
    return SpecializedPriceObservation(
        price=fallback_price,
        observed_at=None,
        source_name=fallback_source_name,
        source_path=fallback_source_path,
    )


def price_seed_source_files(
    repo_root: Path,
    observation: SpecializedPriceObservation,
) -> tuple[tuple[Path, str], ...]:
    """Return the one local file that supplied a fallback price, if any.

    A network quote has no local seed input. A governed FMP-cache observation
    does: its profile remains a calculation source even though its mtime also
    supplies the observation clock. Superseded profiles carry no ``source_path``
    and therefore stay out of lineage.
    """

    if observation.source_path is None:
        return ()
    return ((repo_root / observation.source_path, "market_price_seed"),)


__all__ = [
    "SpecializedPriceObservation",
    "price_seed_source_files",
    "resolve_specialized_price",
]
