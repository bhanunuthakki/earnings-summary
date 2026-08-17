"""Typed review queue for official publisher IR homepages.

These entries are candidates until their live bytes pass the explicit identity
markers and are captured by ``home_authority``.  The old URL override mapping
is now only a compatibility projection of this richer registry.
"""

from __future__ import annotations

from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_IR_MARKERS = (
    "Investor",
    "Financial",
    "Quarterly",
    "Earnings",
    "Results",
    "Shareholder",
    "Reports & Filings",
    "IR Information",
)


class IRHomeAuthorityCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ticker: str = Field(min_length=1, max_length=16)
    ticker_aliases: tuple[str, ...] = ()
    requested_url: str = Field(min_length=1)
    required_marker_groups: tuple[tuple[str, ...], ...] = Field(min_length=1)
    origin: Literal["curated_override"] = "curated_override"
    verification_method: Literal["curated_url_live_identity_markers"] = (
        "curated_url_live_identity_markers"
    )

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("ticker_aliases")
    @classmethod
    def _ticker_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(alias.strip().upper() for alias in value)
        if any(not alias for alias in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("candidate ticker aliases must be non-empty and unique")
        return normalized

    @field_validator("requested_url")
    @classmethod
    def _url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("candidate URL must be uncredentialed HTTPS")
        return value

    @model_validator(mode="after")
    def _aliases_exclude_primary(self) -> Self:
        if self.ticker in self.ticker_aliases:
            raise ValueError("candidate ticker aliases cannot repeat the primary ticker")
        return self


def _candidate(
    ticker: str,
    requested_url: str,
    *identity_markers: str,
    ir_markers: tuple[str, ...] = _IR_MARKERS,
    ticker_aliases: tuple[str, ...] = (),
) -> IRHomeAuthorityCandidate:
    return IRHomeAuthorityCandidate(
        ticker=ticker,
        ticker_aliases=ticker_aliases,
        requested_url=requested_url,
        required_marker_groups=(identity_markers, ir_markers),
    )


IR_HOME_AUTHORITY_CANDIDATES = (
    _candidate(
        "AMZN",
        "https://ir.aboutamazon.com/quarterly-results/default.aspx",
        "Amazon",
    ),
    _candidate(
        "GOOG",
        "https://abc.xyz/investor/",
        "Alphabet",
        "Google",
        ticker_aliases=("GOOGL",),
    ),
    _candidate(
        "META",
        "https://investor.atmeta.com/investor-news/",
        "Meta",
    ),
    _candidate(
        "MELI",
        "https://investor.mercadolibre.com/sec-filings",
        "Mercado Libre",
        "MercadoLibre",
    ),
    _candidate(
        "NU",
        "https://www.investidores.nu/en/financials/results-center/",
        "Nubank",
        "Nu Holdings",
    ),
    _candidate(
        "NVO",
        "https://www.novonordisk.com/investors.html",
        "Novo Nordisk",
    ),
    _candidate(
        "NOW",
        "https://investor.servicenow.com/overview/default.aspx",
        "ServiceNow",
    ),
    _candidate("WIX", "https://investors.wix.com/financials", "Wix"),
    _candidate(
        "RBRK",
        "https://ir.rubrik.com/financials/quarterly-results/default.aspx",
        "Rubrik",
    ),
    _candidate("VEEV", "https://ir.veeva.com/", "Veeva"),
    _candidate(
        "BN",
        "https://bn.brookfield.com/reports-filings",
        "Brookfield Corporation",
        "Brookfield",
    ),
    _candidate(
        "LLY",
        "https://investor.lilly.com/financial-information/quarterly-results",
        "Eli Lilly",
        "Lilly",
    ),
    _candidate(
        "V",
        "https://investor.visa.com/financial-information/quarterly-earnings/default.aspx",
        "Visa",
    ),
    _candidate(
        "ORCL",
        "https://investor.oracle.com/financials/default.aspx",
        "Oracle",
    ),
    _candidate(
        "UBER",
        "https://investor.uber.com/news-events/default.aspx",
        "Uber",
    ),
    _candidate(
        "ABNB",
        "https://investors.airbnb.com/financials/quarterly-results/default.aspx",
        "Airbnb",
    ),
    _candidate(
        "BKNG",
        "https://ir.bookingholdings.com/financial-information/quarterly-results",
        "Booking Holdings",
        "Booking.com",
    ),
    _candidate(
        "TMO",
        "https://ir.thermofisher.com/investors/news-events/financial-news/default.aspx",
        "Thermo Fisher",
    ),
    _candidate(
        "FCX",
        "https://investors.fcx.com/investors/financial-information/default.aspx",
        "Freeport",
        "FCX",
    ),
    _candidate(
        "SOFI",
        "https://investors.sofi.com/financials/quarterly-results/default.aspx",
        "SoFi",
    ),
    _candidate(
        "NTRA",
        "https://investor.natera.com/financial-information/quarterly-results",
        "Natera",
    ),
    _candidate(
        "NSP",
        "https://ir.insperity.com/investor-relations/quarterly-results",
        "Insperity",
    ),
    _candidate("DLO", "https://investor.dlocal.com/financials/", "dLocal"),
    _candidate("NBIS", "https://nebius.com/investor-hub", "Nebius"),
    _candidate(
        "TEM",
        "https://investors.tempus.com/financials/financial-information",
        "Tempus",
    ),
    _candidate(
        "CRWV",
        "https://investors.coreweave.com/financials/quarterly-results/default.aspx",
        "CoreWeave",
    ),
    _candidate(
        "WGS",
        "https://ir.genedx.com/financials-filings/quarterly-results/",
        "GeneDx",
    ),
    _candidate(
        "FRVO",
        "https://ir.fervoenergy.com/investor-relations",
        "Fervo",
    ),
    _candidate(
        "FIGR",
        "https://investors.figure.com/financial-information/quarterly-results",
        "Figure",
    ),
    _candidate(
        "CGEH",
        "https://ir.capstonegreenenergy.com/",
        "Capstone Green Energy",
        "Capstone",
    ),
    _candidate(
        "BHP",
        "https://www.bhp.com/financial-results",
        "BHP",
    ),
    _candidate(
        "NTDOY",
        "https://www.nintendo.co.jp/ir/en/",
        "Nintendo",
    ),
    _candidate(
        "IFNNY",
        "https://www.infineon.com/cms/en/about-infineon/investor/",
        "Infineon",
    ),
    _candidate(
        "PCOR",
        "https://investors.procore.com/",
        "Procore",
    ),
    _candidate(
        "TOST",
        "https://investors.toasttab.com/",
        "Toast",
    ),
)

_BY_TICKER: dict[str, IRHomeAuthorityCandidate] = {}
for _candidate_entry in IR_HOME_AUTHORITY_CANDIDATES:
    for _ticker in (_candidate_entry.ticker, *_candidate_entry.ticker_aliases):
        if _ticker in _BY_TICKER:
            raise RuntimeError("IR home authority target tickers and aliases must be unique")
        _BY_TICKER[_ticker] = _candidate_entry


def candidate_for_ticker(ticker: str) -> IRHomeAuthorityCandidate | None:
    return _BY_TICKER.get(ticker.strip().upper())
