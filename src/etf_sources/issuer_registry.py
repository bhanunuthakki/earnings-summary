"""Issuer-published ETF data — the freshness/characteristics overlay.

Fund administrators publish the richest, freshest view of their own products:
daily/monthly full holdings and basket characteristics (expense ratio, P/E,
P/B, weighted market cap). Each issuer publishes differently, so each gets a
small adapter; this registry routes a ticker to its adapter and normalizes
the contract.

Contract: an adapter is ``fetch(ticker) -> IssuerData | None``. None means
"couldn't fetch / page changed shape" — the caller logs and falls back to
the N-PORT spine (etf_sources/nport.py), which is always sufficient for the
evaluation lane. Adapters therefore parse DEFENSIVELY and return None on
surprise rather than raising: an issuer redesigning their fund page must
never block an evaluation.

Adapter status (directives/etf_data.md tracks this):
  vanguard — LIVE (investor.vanguard.com JSON API: paginated full holdings
             + profile w/ expense ratio; no basket P/E-P/B published there)
  avantis  — NOT YET: avantisinvestors.com is JS-rendered with no static
             data URLs; needs a network-tab scout (see the directive's
             scouting note). Avantis names ride the N-PORT spine meanwhile.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from models.instruments import EtfHolding

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssuerCharacteristics:
    """Basket-level facts an issuer publishes about the fund itself."""

    source: str  # 'issuer:vanguard'
    as_of: date | None = None
    name: str | None = None
    issuer: str | None = None
    expense_ratio: float | None = None  # decimal; 0.0006 = 6 bps
    pe_ratio: float | None = None  # multiple-of-1x
    pb_ratio: float | None = None
    weighted_avg_mktcap_usd_m: float | None = None
    distribution_yield: float | None = None  # decimal


@dataclass(frozen=True, slots=True)
class IssuerData:
    """One adapter fetch: fresh holdings and/or characteristics."""

    source: str
    holdings: list[EtfHolding] = field(default_factory=list[EtfHolding])
    holdings_as_of: date | None = None
    characteristics: IssuerCharacteristics | None = None


AdapterFn = Callable[[str], "IssuerData | None"]


def _adapters() -> dict[str, AdapterFn]:
    # Imported lazily so a broken/missing adapter module degrades that issuer
    # only, and module import stays cheap for pure-parse test callers.
    from etf_sources import vanguard

    return {"vanguard": vanguard.fetch}


#: Ticker → issuer key. Extend when onboarding a new issuer's fund; a ticker
#: absent here simply gets no overlay (N-PORT spine only).
ISSUER_BY_TICKER: dict[str, str] = {
    "VWO": "vanguard",
    "VTV": "vanguard",
    "VUG": "vanguard",
    # Avantis (AVDV / AVUV / AVEM …) intentionally unmapped — no adapter yet.
}


def fetch_issuer_data(ticker: str) -> IssuerData | None:
    """Route to the ticker's issuer adapter. None when unmapped, adapter
    missing, or the fetch/parse failed — callers fall back to N-PORT."""
    upper = ticker.upper()
    issuer_key = ISSUER_BY_TICKER.get(upper)
    if issuer_key is None:
        log.info({"event": "issuer_overlay_unmapped", "ticker": upper})
        return None
    adapter = _adapters().get(issuer_key)
    if adapter is None:
        log.info({"event": "issuer_overlay_no_adapter", "ticker": upper, "issuer": issuer_key})
        return None
    try:
        return adapter(upper)
    except Exception as exc:  # defensive: an issuer page change must degrade, not block
        log.warning(
            {
                "event": "issuer_overlay_failed",
                "ticker": upper,
                "issuer": issuer_key,
                "error": str(exc)[:200],
            }
        )
        return None
