"""Damodaran country risk premiums, weighted by where a company earns revenue.

A single FCFF discount rate built on the US/mature equity risk premium
under-prices a company whose cash flows are earned in riskier economies. This
module supplies the missing layer the Damodaran way: an *operation-weighted*
country risk premium (CRP) that adds the sovereign-risk spread of each country
to the cost of equity, weighted by the share of revenue earned there.

    ke = risk_free + beta * mature_ERP + country_risk_premium

(the additive λ≈1 form the codebase already standardises on in
``global_assumptions.capm_ke``). The premium is computed from two inputs:

  * ``COUNTRY_CRP`` — Damodaran's published equity country risk premiums (the
    premium ABOVE a mature market like the US/Germany, which sit at 0). This is
    a periodically-refreshed reference snapshot, NOT a live feed — update it
    from https://pages.stern.nyu.edu/~adamodar/ when Damodaran posts his
    January/July revisions.
  * the company's reported geographic revenue mix (FMP geo segments), so a
    LatAm-heavy name like MELI carries a Brazil/Mexico/Argentina-weighted
    premium while a US-only name carries ~0.

It is deliberately systematic — every name runs through the same table and the
same revenue weighting, so there are no per-ticker hand-tuned premiums. A name
with no attributable foreign revenue (or no geo file at all) gets CRP 0 and is
left exactly where it was.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Damodaran equity country risk premiums (decimal), the premium ABOVE the
# mature-market ERP. Mature markets (US, Canada, Germany, UK, etc.) sit at 0.0
# because their risk is already in the base ERP. SNAPSHOT — refresh from
# Damodaran's country-risk-premium dataset (he revises it each January & July);
# the values below are a defensible early-2026 calibration, rounded.
COUNTRY_CRP_AS_OF = "2026-01"
COUNTRY_CRP_SOURCE = "Damodaran country risk premiums (pages.stern.nyu.edu/~adamodar)"

COUNTRY_CRP: dict[str, float] = {
    # Mature / Aaa-Aa — risk already in the base ERP.
    "United States": 0.0,
    "Canada": 0.0,
    "Germany": 0.0,
    "Switzerland": 0.0,
    "Netherlands": 0.0,
    "Sweden": 0.0,
    "Norway": 0.0,
    "Denmark": 0.0,
    "Australia": 0.0,
    "Singapore": 0.0,
    "United Kingdom": 0.006,
    "France": 0.009,
    "Japan": 0.006,
    "South Korea": 0.005,
    "Taiwan": 0.005,
    "China": 0.007,
    "Israel": 0.016,
    "Chile": 0.009,
    "Poland": 0.013,
    # Emerging — the layer that actually moves a discount rate.
    "Mexico": 0.020,
    "India": 0.025,
    "Indonesia": 0.020,
    "Philippines": 0.020,
    "Brazil": 0.030,
    "Colombia": 0.034,
    "South Africa": 0.035,
    "Peru": 0.020,
    "Uruguay": 0.018,
    "Turkey": 0.060,
    # Argentina is a hyperinflationary, high-spread sovereign; even after the
    # 2025-26 compression its CRP dwarfs the rest of LatAm. The dominant single
    # lever for any Argentina-exposed name — keep it visible and easy to revise.
    "Argentina": 0.070,
}

# Common reported-segment / geo labels normalised to a COUNTRY_CRP key. Matching
# is case-insensitive and substring-based (so "Brazil Segment", "BRAZIL", and
# "Brazil" all resolve), but this explicit map wins first for the awkward ones.
_LABEL_ALIASES: dict[str, str] = {
    "usa": "United States",
    "u.s.": "United States",
    "us": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "korea": "South Korea",
    "republic of korea": "South Korea",
}

# Foreign-but-unattributable buckets ("Other Countries", "International", "Rest
# of World") simply don't match any country name, so ``crp_for_country`` returns
# None for them and ``weighted_crp`` drops them and renormalises over the revenue
# it *can* attribute — rather than fabricating a premium for an unknown region.


def crp_for_country(label: str) -> float | None:
    """The CRP for a geo label, or ``None`` if it can't be mapped to a country.

    ``None`` means "don't attribute" (an aggregate/unknown bucket) — distinct
    from a mapped mature country, which returns ``0.0``.
    """
    key = label.strip()
    if not key:
        return None
    low = key.lower()
    alias = _LABEL_ALIASES.get(low)
    if alias is not None:
        return COUNTRY_CRP[alias]
    # Exact (case-insensitive) country name.
    for country, crp in COUNTRY_CRP.items():
        if low == country.lower():
            return crp
    # Substring: "Brazil Segment" -> Brazil. Longest country name first so
    # "United States" wins over a hypothetical "States" substring.
    for country in sorted(COUNTRY_CRP, key=len, reverse=True):
        if country.lower() in low:
            return COUNTRY_CRP[country]
    return None


def weighted_crp(geo_revenue: dict[str, float]) -> float:
    """Revenue-weighted CRP over the country-attributable share of revenue.

    Labels that map to a country contribute ``weight_i * CRP_i``; unattributable
    aggregates ("Other Countries") and unknown labels are excluded and the
    weights renormalised over the attributable revenue. Returns 0.0 when nothing
    is attributable (a US-only / no-geo name is left untouched).
    """
    attributable: list[tuple[float, float]] = []  # (revenue, crp)
    for label, rev in geo_revenue.items():
        if not isinstance(rev, (int, float)) or rev <= 0:
            continue
        crp = crp_for_country(label)
        if crp is None:
            continue
        attributable.append((float(rev), crp))
    total = sum(rev for rev, _ in attributable)
    if total <= 0:
        return 0.0
    return sum(rev * crp for rev, crp in attributable) / total


def _latest_geo_revenue(repo_root: Path, ticker: str) -> dict[str, float]:
    """Latest geographic revenue mix for a ticker from the FMP geo-segment cache.

    Prefers the annual file's most recent fiscal year; falls back to summing the
    latest four quarters of the quarterly file. Returns an empty dict when no
    usable geo cache exists (so the caller yields CRP 0).
    """
    fmp = repo_root / "data" / "historical" / "fmp"

    def _records(name: str) -> list[dict[str, object]]:
        p = fmp / name
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    annual = _records(f"{ticker}_geo_segments_annual.json")
    if annual:
        latest = max(annual, key=lambda r: str(r.get("fiscalYear", "")))
        data = latest.get("data")
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}

    quarterly = _records(f"{ticker}_geo_segments_quarterly.json")
    if quarterly:
        ordered = sorted(
            quarterly, key=lambda r: (str(r.get("fiscalYear", "")), str(r.get("period", "")))
        )
        agg: dict[str, float] = {}
        for rec in ordered[-4:]:
            data = rec.get("data")
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, (int, float)):
                        agg[str(k)] = agg.get(str(k), 0.0) + float(v)
        return agg
    return {}


def country_risk_premium(repo_root: Path, ticker: str) -> float:
    """The operation-weighted country risk premium for a ticker (decimal).

    Reads the cached geographic revenue mix and weights it through
    ``COUNTRY_CRP``. Best-effort: any read/parse failure degrades to 0.0 so a
    DCF build never fails because the geo cache was missing or malformed.
    """
    try:
        geo = _latest_geo_revenue(Path(repo_root), ticker)
    except Exception as exc:  # never let a CRP lookup break a build
        log.debug({"event": "country_risk_premium_failed", "ticker": ticker, "error": str(exc)})
        return 0.0
    return weighted_crp(geo)
