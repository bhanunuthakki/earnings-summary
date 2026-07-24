"""Deterministic revenue-exposure rollup for the held book (C2, 2026-07-19
program plan — Workstream C).

Answers "what is the book actually exposed to?" from DISCLOSED revenue mixes,
not tickers: per holding, the latest revenue mix by geography (or product /
business_unit) from the segment junction tables; the book vector is tracker
weights × per-name mix. Pure code — zero LLM, zero network, no new tables.
The C3 business-factor taxonomy and the C7 risk-aware /review both consume
this rollup; the honest-degrade discipline here (unattributed weight is
REPORTED, never renormalized away) is what keeps those surfaces truthful.

Data basis (C1, completed 2026-07-23): MELI/NU/NVO carry QUARTERLY junction
rows (FMP segmentation + crosstab paths) so their mix is a trailing-4-quarter
sum; the remaining holdings carry ANNUAL rows from 10-K/20-F/40-F crosstab
extraction (FMP's quarterly segmentation endpoints are subscription-gated),
so their mix is the latest fiscal year — the ``basis`` field says which, and
freshness is judged against ``period_end``, never the wall clock alone.

Overrides: ``fact_overrides`` are applied at the INGEST gate
(``compute.segment_cache.apply_overrides`` →
``provenance.overrides.apply_segment_overrides``), so junction rows read here
are already post-override — this module deliberately has no re-application
logic (a second application would double-correct).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from user_state._db import open_conn

log = logging.getLogger(__name__)

DIM_TYPES: tuple[str, ...] = ("geography", "product", "business_unit")

# Trailing window inside which 4 quarterly periods count as a TTM. 15 months
# tolerates one late filing without letting a stale year masquerade as TTM.
_TTM_WINDOW_DAYS = 460

# Pre-normalization share total must land within this band of 1.0 — outside it
# the disclosure is partial (or the extraction grabbed a subtotal) and a
# normalized mix would be confidently wrong. Reported as a reason instead.
_SHARE_TOTAL_TOLERANCE = 0.15

# Revenue-metric selection: exact 'revenue' first; otherwise *_revenue
# variants (NVO's geography rows carry glp1_revenue / insulin_revenue etc.).
# Never these: they are not revenue even though they live in the same dims.
_NON_REVENUE_METRICS = frozenset({"non_current_assets", "operating_profit", "operating_income"})

# Disclosed geography label → ISO currency, conservative and label-exact after
# canonicalization (lowercase, stripped). Labels not here collapse to
# UNMAPPED — an honest bucket the caller can see, never a guess. Regions with
# no single currency (EMEA, APAC, "rest of world") are deliberately UNMAPPED:
# mapping them to anything would manufacture FX precision the filing never
# disclosed.
GEOGRAPHY_CURRENCY: dict[str, str] = {
    "brazil": "BRL",
    "mexico": "MXN",
    "argentina": "ARS",
    "colombia": "COP",
    "united states": "USD",
    "u.s.": "USD",
    "us": "USD",
    "usa": "USD",
    "united states of america": "USD",
    "north america": "USD",
    "north america operations": "USD",
    "united states and canada": "USD",
    "canada": "CAD",
    "israel": "ILS",
    "denmark": "DKK",
    "china": "CNY",
    "japan": "JPY",
    "united kingdom": "GBP",
    "germany": "EUR",
    "france": "EUR",
    "netherlands": "EUR",
    "ireland": "EUR",
    "europe": "EUR",
}

UNMAPPED = "UNMAPPED"


@dataclass(frozen=True, slots=True)
class NameMix:
    """One holding's latest disclosed revenue mix along one dimension."""

    ticker: str
    dim_type: str
    basis: str  # "ttm_quarterly" | "annual"
    period_end: str  # ISO date of the latest period the mix covers
    currency: str | None
    shares: dict[str, float]  # dim_name -> fraction of the name's revenue
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BookExposure:
    """The book's aggregated exposure along one dimension.

    ``unattributed`` carries the weight the rollup could NOT place (no mix,
    garbage disclosure, non-junction names like ETFs) with a reason per name —
    surfaced, never silently renormalized into the placed share.
    """

    dim_type: str
    shares: dict[str, float]  # dim_name -> fraction of the BOOK
    by_name: dict[str, NameMix] = field(default_factory=dict)
    unattributed: dict[str, str] = field(default_factory=dict)  # ticker -> why
    unattributed_weight: float = 0.0


def _canon_label(raw: str) -> str:
    text = " ".join(raw.strip().lower().split())
    # Crosstab extractions suffix countries with " Segment" ("Brazil Segment",
    # "Argentina Segment" — prod labels, 2026-07). Strip it for the currency
    # lookup ONLY; NameMix.shares keeps the disclosed label verbatim.
    return text.removesuffix(" segment").strip()


def _revenue_rows(
    conn: sqlite3.Connection, ticker: str, dim_type: str
) -> list[tuple[str, str, str, str | None, float, str]]:
    """(period_end, fiscal_period_type, period_basis, currency, value,
    dim_name) for every revenue-like dimension row of one ticker/dim."""
    rows = conn.execute(
        """
        SELECT p.period_end, COALESCE(p.fiscal_period_type, ''),
               COALESCE(p.period_basis, ''), p.currency, d.value, d.dim_name,
               d.metric
        FROM segment_dimensions d
        JOIN segment_periods p ON p.id = d.period_id
        WHERE p.ticker = ? AND d.dim_type = ? AND d.value IS NOT NULL
        """,
        (ticker, dim_type),
    ).fetchall()
    out: list[tuple[str, str, str, str | None, float, str]] = []
    for period_end, fpt, basis, currency, value, dim_name, metric in rows:
        m = str(metric or "").lower()
        if m in _NON_REVENUE_METRICS:
            continue
        if not (m == "revenue" or m.endswith("_revenue") or m.endswith("revenues")):
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        out.append((str(period_end), str(fpt), str(basis), currency, v, str(dim_name)))
    return out


def _period_date(period_end: str) -> date | None:
    try:
        return datetime.fromisoformat(period_end.replace(" ", "T")).date()
    except ValueError:
        return None


def _is_quarterly(fiscal_period_type: str, period_basis: str) -> bool:
    text = f"{fiscal_period_type} {period_basis}".lower()
    return "q" in fiscal_period_type.lower() or "quarter" in text or "3m" in text


def latest_revenue_mix(
    ticker: str,
    *,
    dim_type: str = "geography",
    db_path: Path | str | None = None,
    now: date | None = None,
) -> NameMix | None:
    """The latest disclosed revenue mix for one name, or ``None`` with a
    logged reason. Quarterly-TTM (last 4 quarters summed) when >=4 quarterly
    periods exist inside the trailing window; else the latest annual period.
    Never raises — a broken disclosure degrades to None, and the book rollup
    reports the name as unattributed."""
    if dim_type not in DIM_TYPES:
        raise ValueError(f"dim_type must be one of {DIM_TYPES}")
    try:
        conn = open_conn(db_path)
    except Exception:
        log.warning({"event": "exposure_db_unavailable", "ticker": ticker})
        return None
    try:
        rows = _revenue_rows(conn, ticker.upper(), dim_type)
    except sqlite3.Error:
        log.warning({"event": "exposure_read_failed", "ticker": ticker}, exc_info=True)
        return None
    finally:
        conn.close()
    if not rows:
        return None

    today = now or date.today()
    window_floor = today - timedelta(days=_TTM_WINDOW_DAYS)

    quarterly_periods: dict[str, list[tuple[str, float, str | None]]] = {}
    annual_periods: dict[str, list[tuple[str, float, str | None]]] = {}
    for period_end, fpt, basis, currency, value, dim_name in rows:
        bucket = quarterly_periods if _is_quarterly(fpt, basis) else annual_periods
        bucket.setdefault(period_end, []).append((dim_name, value, currency))

    recent_quarters = sorted(
        (p for p in quarterly_periods if (d := _period_date(p)) and d >= window_floor),
        reverse=True,
    )[:4]

    if len(recent_quarters) >= 4:
        basis_label = "ttm_quarterly"
        chosen = recent_quarters
        source = quarterly_periods
    elif annual_periods:
        basis_label = "annual"
        chosen = [max(annual_periods)]
        source = annual_periods
    elif quarterly_periods:
        # Fewer than 4 usable quarters and no annual row: a partial-year mix
        # would misrepresent seasonality — degrade honestly.
        log.info({"event": "exposure_insufficient_periods", "ticker": ticker})
        return None
    else:
        return None

    totals: dict[str, float] = {}
    currencies: set[str] = set()
    for period in chosen:
        for dim_name, value, currency in source[period]:
            totals[dim_name] = totals.get(dim_name, 0.0) + value
            if currency:
                currencies.add(str(currency))

    grand = sum(totals.values())
    if grand <= 0:
        log.info({"event": "exposure_nonpositive_total", "ticker": ticker})
        return None
    shares = {name: v / grand for name, v in totals.items() if v > 0}
    # The pre-normalization sanity is on the RAW share sum: negatives or a
    # dominant excluded bucket show up as a total far from the sum of kept
    # rows. Kept simple: if dropping negatives removed >tolerance of revenue,
    # the disclosure is too partial to trust.
    kept = sum(shares.values())
    if abs(1.0 - kept) > _SHARE_TOTAL_TOLERANCE:
        log.info({"event": "exposure_partial_disclosure", "ticker": ticker, "kept": round(kept, 3)})
        return None
    shares = {name: v / kept for name, v in shares.items()}

    notes: list[str] = []
    if len(currencies) > 1:
        notes.append(f"mixed reporting currencies: {sorted(currencies)}")
    return NameMix(
        ticker=ticker.upper(),
        dim_type=dim_type,
        basis=basis_label,
        period_end=max(chosen),
        currency=sorted(currencies)[0] if len(currencies) == 1 else None,
        shares=shares,
        notes=tuple(notes),
    )


def currency_exposure(mix: NameMix) -> dict[str, float]:
    """Collapse a GEOGRAPHY mix through the label→currency map. Regions
    without a single currency (and any label outside the map) aggregate under
    ``UNMAPPED`` — visible, never guessed."""
    out: dict[str, float] = {}
    for label, share in mix.shares.items():
        ccy = GEOGRAPHY_CURRENCY.get(_canon_label(label), UNMAPPED)
        out[ccy] = out.get(ccy, 0.0) + share
    return out


def book_exposure(
    *,
    dim_type: str = "geography",
    db_path: Path | str | None = None,
    repo_root: Path | str | None = None,
    now: date | None = None,
) -> BookExposure:
    """Tracker weights × per-name mixes → the book's exposure vector.

    Weight whose name has no usable mix lands in ``unattributed`` with a
    reason — the placed shares therefore sum to (1 - unattributed_weight),
    NOT to 1: renormalizing would claim knowledge the disclosures don't
    support. Never raises."""
    from portfolio_weights import read_materialized_weights

    try:
        weights = read_materialized_weights(Path(repo_root)) if repo_root else {}
    except Exception:
        log.warning({"event": "exposure_weights_unavailable"}, exc_info=True)
        weights = {}

    shares: dict[str, float] = {}
    by_name: dict[str, NameMix] = {}
    unattributed: dict[str, str] = {}
    missing_weight = 0.0
    for ticker, weight in sorted(weights.items()):
        if weight <= 0:
            continue
        try:
            mix = latest_revenue_mix(ticker, dim_type=dim_type, db_path=db_path, now=now)
        except Exception:
            log.warning({"event": "exposure_name_failed", "ticker": ticker}, exc_info=True)
            mix = None
        if mix is None:
            unattributed[ticker] = "no usable revenue mix (no junction rows or partial disclosure)"
            missing_weight += weight
            continue
        by_name[ticker] = mix
        for dim_name, share in mix.shares.items():
            shares[dim_name] = shares.get(dim_name, 0.0) + weight * share

    return BookExposure(
        dim_type=dim_type,
        shares=shares,
        by_name=by_name,
        unattributed=unattributed,
        unattributed_weight=missing_weight,
    )


__all__ = [
    "DIM_TYPES",
    "GEOGRAPHY_CURRENCY",
    "UNMAPPED",
    "BookExposure",
    "NameMix",
    "book_exposure",
    "currency_exposure",
    "latest_revenue_mix",
]
