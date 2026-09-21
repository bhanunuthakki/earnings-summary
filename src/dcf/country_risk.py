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
same revenue weighting, so there are no per-ticker hand-tuned premiums. A known
mature-market mix may resolve to CRP 0. Missing or unattributable geography is
unavailable and cannot masquerade as mature-market evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CountryRiskObservation:
    """Computed premium plus the exact geo bytes that influenced it."""

    premium: float
    source_record: dict[str, object] | None
    geo_revenue: dict[str, float]


class CountryRiskUnavailableError(RuntimeError):
    """Typed failure for missing or unattributable geographic revenue evidence."""

    def __init__(
        self,
        reason: str,
        *,
        source_record: dict[str, object] | None = None,
        geo_revenue: dict[str, float] | None = None,
    ) -> None:
        self.reason = reason
        self.source_record = source_record
        self.geo_revenue = dict(geo_revenue or {})
        super().__init__(f"country risk unavailable: {reason}")


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


def weighted_crp(geo_revenue: dict[str, float]) -> float | None:
    """Revenue-weighted CRP over the country-attributable share of revenue.

    Labels that map to a country contribute ``weight_i * CRP_i``; unattributable
    aggregates ("Other Countries") and unknown labels are excluded and the
    weights renormalised over the attributable revenue. Returns ``None`` when
    nothing is attributable. A known mature-market mix remains an available
    numeric ``0.0``.
    """
    attributable: list[tuple[float, float]] = []  # (revenue, crp)
    for label, rev in geo_revenue.items():
        if not math.isfinite(rev):
            return None
        if rev <= 0:
            continue
        crp = crp_for_country(label)
        if crp is None:
            continue
        attributable.append((float(rev), crp))
    total = sum(rev for rev, _ in attributable)
    if total <= 0 or not math.isfinite(total):
        return None
    premium = sum(rev * crp for rev, crp in attributable) / total
    return premium if math.isfinite(premium) else None


def _geo_source_record(
    path: Path, *, repo_root: Path, selection: str
) -> tuple[list[dict[str, object]], dict[str, object]] | None:
    """Read and hash one candidate file from the same opened byte stream."""
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read()
            stat = os.fstat(handle.fileno())
        decoded: object = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(decoded, list):
        return None
    decoded_items = cast("list[object]", decoded)
    records = [cast("dict[str, object]", item) for item in decoded_items if isinstance(item, dict)]
    try:
        locator = str(path.relative_to(repo_root))
    except ValueError:
        locator = str(path)
    return records, {
        "role": "geographic_revenue",
        "path": locator.replace("\\", "/"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "observed_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        "influences_calculation": True,
        "selection": selection,
    }


def _geo_values(record: dict[str, object]) -> dict[str, float]:
    data = record.get("data")
    if not isinstance(data, dict):
        return {}
    return {
        str(key): float(value)
        for key, value in cast("dict[object, object]", data).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _latest_geo_observation(repo_root: Path, ticker: str) -> CountryRiskObservation:
    """Latest geographic revenue mix for a ticker from the FMP geo-segment cache.

    Prefers the annual file's most recent fiscal year; falls back to summing the
    latest four quarters of the quarterly file. Missing or unattributable
    geography raises :class:`CountryRiskUnavailableError`.
    """
    fmp = repo_root / "data" / "historical" / "fmp"

    annual = _geo_source_record(
        fmp / f"{ticker}_geo_segments_annual.json",
        repo_root=repo_root,
        selection="annual_latest_fiscal_year",
    )
    if annual is not None and annual[0]:
        latest = max(annual[0], key=lambda record: str(record.get("fiscalYear", "")))
        geo = _geo_values(latest)
        if geo:
            premium = weighted_crp(geo)
            if premium is None:
                raise CountryRiskUnavailableError(
                    "geographic_revenue_unattributable",
                    source_record=annual[1],
                    geo_revenue=geo,
                )
            return CountryRiskObservation(premium, annual[1], geo)

    quarterly = _geo_source_record(
        fmp / f"{ticker}_geo_segments_quarterly.json",
        repo_root=repo_root,
        selection="quarterly_latest_four",
    )
    if quarterly is not None and quarterly[0]:
        ordered = sorted(
            quarterly[0],
            key=lambda record: (
                str(record.get("fiscalYear", "")),
                str(record.get("period", "")),
            ),
        )
        agg: dict[str, float] = {}
        for rec in ordered[-4:]:
            for key, value in _geo_values(rec).items():
                agg[key] = agg.get(key, 0.0) + value
        if agg:
            premium = weighted_crp(agg)
            if premium is None:
                raise CountryRiskUnavailableError(
                    "geographic_revenue_unattributable",
                    source_record=quarterly[1],
                    geo_revenue=agg,
                )
            return CountryRiskObservation(premium, quarterly[1], agg)
    raise CountryRiskUnavailableError("geographic_revenue_unavailable")


def _latest_geo_revenue(repo_root: Path, ticker: str) -> dict[str, float]:
    """Compatibility helper for callers that need the selected revenue mix only."""
    return _latest_geo_observation(repo_root, ticker).geo_revenue


def country_risk_observation(repo_root: Path, ticker: str) -> CountryRiskObservation:
    """Return CRP and a same-byte-stream receipt for its selected geo source."""
    try:
        return _latest_geo_observation(Path(repo_root), ticker)
    except CountryRiskUnavailableError:
        raise
    except Exception as exc:
        log.debug({"event": "country_risk_premium_failed", "ticker": ticker, "error": str(exc)})
        raise CountryRiskUnavailableError("geographic_revenue_read_failed") from exc


def country_risk_premium(repo_root: Path, ticker: str) -> float:
    """The operation-weighted country risk premium for a ticker (decimal).

    Reads the cached geographic revenue mix and weights it through
    ``COUNTRY_CRP``. Missing, malformed, or wholly unattributable geography is
    unavailable rather than a synthetic mature-market zero.
    """
    return country_risk_observation(repo_root, ticker).premium
