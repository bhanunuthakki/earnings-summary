"""Look-through overlap — what an ETF actually holds vs what the book owns.

The diversification factor correlates RETURNS; it can be fooled by an ETF
that literally holds the book's names (high overlap reads as "correlated"
without saying WHY, and a short common window can even hide it). This module
answers the ownership question directly from the published holdings
(``etf_holdings`` — N-PORT spine / issuer overlay, directives/etf_data.md):

  * **direct overlap** — the fraction of the ETF's weight sitting in
    constituents the book already holds;
  * **top overlaps** — the largest shared names (ETF weight vs book weight);
  * **sector / country rollups** — where the ETF's weight actually lives
    (country from N-PORT's per-constituent ``invCountry`` — real geography,
    not a profile-level proxy).

Consumed two ways: the ETF-only ``overlap`` fit factor
(``allocation.candidate_fit``) and the workup fragment's overlap table.
``None`` when no holdings snapshot exists (both sources degraded) — the
factor is then omitted, never guessed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from instrument_store import get_etf_holdings

#: Below this weight a constituent row is dust — skipped in the top table
#: (but still counted in the overlap fraction).
_TOP_TABLE_CAP = 8

#: Countries commonly classified emerging — for the sleeve read (coarse on
#: purpose: the sleeve factor only needs "does this fund serve an EM/intl
#: sleeve", not an index vendor's exact taxonomy).
EM_COUNTRIES: frozenset[str] = frozenset(
    {
        "AR",
        "BR",
        "CL",
        "CN",
        "CO",
        "CZ",
        "EG",
        "GR",
        "HU",
        "ID",
        "IN",
        "KR",
        "KW",
        "MX",
        "MY",
        "PE",
        "PH",
        "PL",
        "QA",
        "SA",
        "TH",
        "TR",
        "TW",
        "ZA",
    }
)


@dataclass(frozen=True, slots=True)
class OverlapRow:
    """One shared constituent."""

    constituent: str
    name: str | None
    etf_weight: float  # fraction of the ETF
    book_weight: float  # fraction of the book


@dataclass(frozen=True, slots=True)
class EtfOverlap:
    """The look-through read for one ETF against the held book."""

    ticker: str
    as_of: date
    source: str  # 'nport' | 'issuer:<name>' | 'fmp' (the snapshot's writer)
    holdings_count: int
    direct_overlap_pct: float  # Σ etf-weight over constituents the book holds
    top_overlaps: list[OverlapRow] = field(default_factory=list[OverlapRow])
    sector_weights: dict[str, float] = field(default_factory=dict[str, float])
    country_weights: dict[str, float] = field(default_factory=dict[str, float])

    @property
    def intl_weight(self) -> float:
        """Non-US weight (unknown-country rows count as neither)."""
        return sum(w for c, w in self.country_weights.items() if c and c != "US")

    @property
    def em_weight(self) -> float:
        return sum(w for c, w in self.country_weights.items() if c in EM_COUNTRIES)


def compute_lookthrough_overlap(
    conn: sqlite3.Connection,
    etf_ticker: str,
    book_weights: Mapping[str, float],
) -> EtfOverlap | None:
    """The latest holdings snapshot joined against the held book.

    ``None`` when no holdings rows exist (N-PORT unresolved AND the issuer
    overlay failed) — the caller's factor degrades instead of guessing.
    Weights are the ETF's own fractions; rows without a weight contribute 0.
    """
    t = etf_ticker.strip().upper()
    # instrument_store reads rows by name; not every caller's connection
    # carries a Row factory (the Stage 0f script's doesn't) — set it here,
    # the positioning-store precedent.
    conn.row_factory = sqlite3.Row
    try:
        holdings = get_etf_holdings(conn, t)
    except sqlite3.Error:
        return None
    if not holdings:
        return None

    book = {sym.upper() for sym, w in book_weights.items() if w > 0}
    overlap = 0.0
    rows: list[OverlapRow] = []
    sectors: dict[str, float] = {}
    countries: dict[str, float] = {}
    for h in holdings:
        w = h.weight_pct or 0.0
        if h.sector:
            sectors[h.sector] = sectors.get(h.sector, 0.0) + w
        if h.country:
            c = h.country.strip().upper()
            countries[c] = countries.get(c, 0.0) + w
        sym = (h.constituent_ticker or "").upper()
        if sym and sym in book:
            overlap += w
            rows.append(
                OverlapRow(
                    constituent=sym,
                    name=h.name,
                    etf_weight=w,
                    book_weight=float(book_weights.get(sym, 0.0)),
                )
            )
    rows.sort(key=lambda r: r.etf_weight, reverse=True)
    return EtfOverlap(
        ticker=t,
        as_of=holdings[0].as_of_date,
        source=holdings[0].source,
        holdings_count=len(holdings),
        direct_overlap_pct=overlap,
        top_overlaps=rows[:_TOP_TABLE_CAP],
        sector_weights=sectors,
        country_weights=countries,
    )


def overlap_detail(overlap: EtfOverlap) -> str:
    """The fit factor's human reading — 'X% of TICKER overlaps the book (top:
    …)', with the snapshot's honesty stamp."""
    top = ", ".join(
        f"{r.constituent} {r.etf_weight * 100.0:.1f}%" for r in overlap.top_overlaps[:3]
    )
    top_part = f" (top: {top})" if top else ""
    return (
        f"{overlap.direct_overlap_pct * 100.0:.0f}% of {overlap.ticker} overlaps the book"
        f"{top_part} · {overlap.source} as of {overlap.as_of.isoformat()}"
    )


def sleeves_served(
    overlap: EtfOverlap | None, *, size_beta: float | None = None
) -> tuple[str, ...]:
    """Which explicit sleeve targets this fund plausibly serves, from its own
    published exposure: 'intl' / 'em' from the country rollup (majority
    weight), 'small_cap' from the size-spread loading. Empty when the data
    can't say — the sleeve factor is then omitted, never guessed."""
    served: list[str] = []
    if overlap is not None and overlap.country_weights:
        if overlap.intl_weight >= 0.5:
            served.append("intl")
        if overlap.em_weight >= 0.5:
            served.append("em")
    if size_beta is not None and size_beta >= 0.3:
        served.append("small_cap")
    return tuple(served)
