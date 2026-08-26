"""Tests for the operation-weighted Damodaran country risk premium
(``src/dcf/country_risk.py``): the label→country mapping, revenue weighting with
renormalisation over the attributable share, and the FMP geo-cache loader.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import country_risk  # noqa: E402


# --------------------------------------------------------------------------- #
# Label -> country mapping
# --------------------------------------------------------------------------- #
def test_crp_for_country_exact_and_substring() -> None:
    assert country_risk.crp_for_country("Brazil") == country_risk.COUNTRY_CRP["Brazil"]
    # Reported-segment labels carry a "Segment" suffix — substring still maps.
    assert country_risk.crp_for_country("Brazil Segment") == country_risk.COUNTRY_CRP["Brazil"]
    assert country_risk.crp_for_country("ARGENTINA") == country_risk.COUNTRY_CRP["Argentina"]


def test_crp_for_country_aliases() -> None:
    assert country_risk.crp_for_country("USA") == 0.0
    assert country_risk.crp_for_country("U.S.") == 0.0
    assert country_risk.crp_for_country("UK") == country_risk.COUNTRY_CRP["United Kingdom"]


def test_crp_for_mature_country_is_zero_not_none() -> None:
    """A mapped mature country returns 0.0 (risk already in the base ERP) — which
    is distinct from an unmappable label returning None."""
    assert country_risk.crp_for_country("United States") == 0.0
    assert country_risk.crp_for_country("Germany") == 0.0


def test_crp_for_unattributable_label_is_none() -> None:
    assert country_risk.crp_for_country("Other Countries Segment") is None
    assert country_risk.crp_for_country("Rest of World") is None
    assert country_risk.crp_for_country("") is None
    assert country_risk.crp_for_country("Atlantis") is None


# --------------------------------------------------------------------------- #
# Revenue weighting
# --------------------------------------------------------------------------- #
def test_weighted_crp_renormalises_over_attributable_revenue() -> None:
    """MELI's geo mix: the un-nameable 'Other Countries' bucket is dropped and the
    Brazil/Mexico/Argentina weights renormalise over the attributable revenue."""
    geo = {
        "Brazil Segment": 15201.0,
        "Mexico Segment": 6475.0,
        "Argentina Segment": 5962.0,
        "Other Countries Segment": 1255.0,  # dropped (renormalised away)
    }
    attributable = 15201.0 + 6475.0 + 5962.0
    expected = (
        15201.0 * country_risk.COUNTRY_CRP["Brazil"]
        + 6475.0 * country_risk.COUNTRY_CRP["Mexico"]
        + 5962.0 * country_risk.COUNTRY_CRP["Argentina"]
    ) / attributable
    assert country_risk.weighted_crp(geo) == pytest.approx(expected)
    # A LatAm-heavy name lands well above zero; Argentina is the dominant lever.
    assert country_risk.weighted_crp(geo) > 0.025


def test_weighted_crp_us_only_is_zero() -> None:
    assert country_risk.weighted_crp({"United States": 1000.0}) == 0.0


def test_weighted_crp_empty_or_unattributable_is_zero() -> None:
    assert country_risk.weighted_crp({}) == 0.0
    assert country_risk.weighted_crp({"Other": 500.0, "Rest of World": 250.0}) == 0.0


def test_weighted_crp_ignores_nonpositive_revenue() -> None:
    geo = {"Brazil": 1000.0, "Argentina": 0.0, "Mexico": -50.0}
    assert country_risk.weighted_crp(geo) == pytest.approx(country_risk.COUNTRY_CRP["Brazil"])


# --------------------------------------------------------------------------- #
# FMP geo-cache loader + top-level entry point
# --------------------------------------------------------------------------- #
def _write_geo(repo: Path, ticker: str, records: list[dict[str, object]], *, annual: bool) -> None:
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    suffix = "annual" if annual else "quarterly"
    (fmp / f"{ticker}_geo_segments_{suffix}.json").write_text(json.dumps(records), encoding="utf-8")


def test_country_risk_premium_reads_annual_geo_cache(tmp_path: Path) -> None:
    _write_geo(
        tmp_path,
        "MELI",
        [
            {"fiscalYear": 2024, "period": "FY", "data": {"Brazil Segment": 10.0}},
            {
                "fiscalYear": 2025,
                "period": "FY",
                "data": {
                    "Brazil Segment": 15201.0,
                    "Mexico Segment": 6475.0,
                    "Argentina Segment": 5962.0,
                    "Other Countries Segment": 1255.0,
                },
            },
        ],
        annual=True,
    )
    crp = country_risk.country_risk_premium(tmp_path, "MELI")
    # Uses the latest FY (2025), not the stale 2024 single-country row.
    assert crp == pytest.approx(
        country_risk.weighted_crp(
            {
                "Brazil Segment": 15201.0,
                "Mexico Segment": 6475.0,
                "Argentina Segment": 5962.0,
                "Other Countries Segment": 1255.0,
            }
        )
    )
    assert crp > 0.025
    observation = country_risk.country_risk_observation(tmp_path, "MELI")
    assert observation.source_record is not None
    assert observation.source_record["path"] == (
        "data/historical/fmp/MELI_geo_segments_annual.json"
    )
    assert observation.source_record["selection"] == "annual_latest_fiscal_year"
    assert observation.source_record["influences_calculation"] is True


def test_country_risk_premium_no_cache_is_zero(tmp_path: Path) -> None:
    assert country_risk.country_risk_premium(tmp_path, "NOFILE") == 0.0


def test_country_risk_premium_falls_back_to_quarterly_ttm(tmp_path: Path) -> None:
    _write_geo(
        tmp_path,
        "QTR",
        [{"fiscalYear": 2025, "period": "FY", "data": {}}],
        annual=True,
    )
    _write_geo(
        tmp_path,
        "QTR",
        [
            {"fiscalYear": 2025, "period": q, "data": {"Brazil Segment": 100.0}}
            for q in ("Q1", "Q2", "Q3", "Q4")
        ],
        annual=False,
    )
    assert country_risk.country_risk_premium(tmp_path, "QTR") == pytest.approx(
        country_risk.COUNTRY_CRP["Brazil"]
    )
    observation = country_risk.country_risk_observation(tmp_path, "QTR")
    assert observation.source_record is not None
    assert observation.source_record["path"] == (
        "data/historical/fmp/QTR_geo_segments_quarterly.json"
    )
    assert observation.source_record["selection"] == "quarterly_latest_four"


def test_country_risk_premium_survives_malformed_cache(tmp_path: Path) -> None:
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / "BAD_geo_segments_annual.json").write_text("{not json", encoding="utf-8")
    assert country_risk.country_risk_premium(tmp_path, "BAD") == 0.0
