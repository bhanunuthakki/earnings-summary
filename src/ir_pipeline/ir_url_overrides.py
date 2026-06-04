"""Curated IR results-center / quarterly-results URLs.

Transcribed from the table in ``directives/fetch_ir_documents.md`` — the
high-quality landing pages a human verified. They take precedence over
``tracked_companies.ir_url`` (which ``db._safe_find_ir_url`` resolves best-effort
on onboard and can be a generic homepage) and over a ticker's
``ir_config.results_center_url``. For any ticker not listed here, discovery falls
back to the config URL, then the DB-stored ``ir_url``.

Adding a name = adding one dict entry (same philosophy as the upload classifier's
``ISSUER_REGISTRY``). A ticker that never gets an entry still works — discovery
just relies on the DB ``ir_url`` and is best-effort.
"""

from __future__ import annotations

# Ticker -> IR landing / quarterly-results / results-center URL. The crawler
# follows "quarterly results / financial / earnings / investors" sub-links one+
# hop from here, so an IR landing page is sufficient (the exact quarterly URL is
# not required). Portfolio block is from directives/fetch_ir_documents.md; the
# evaluation block was researched + empirically validated 2026-06-03.
IR_URL_OVERRIDES: dict[str, str] = {
    # --- Portfolio (directive) ---
    "AMZN": "https://ir.aboutamazon.com/quarterly-results/default.aspx",
    "GOOG": "https://abc.xyz/investor/",
    "GOOGL": "https://abc.xyz/investor/",
    "META": "https://investor.atmeta.com/investor-news/",
    "MELI": "https://investor.mercadolibre.com/financial-information/quarterly-results",
    "NU": "https://www.investidores.nu/en/financials/results-center/",
    "NVO": "https://www.novonordisk.com/investors.html",
    "NOW": "https://www.servicenow.com/company/investor-relations.html",
    "WIX": "https://investors.wix.com/financials",
    "RBRK": "https://ir.rubrik.com/financial-information/quarterly-results",
    "VEEV": "https://ir.veeva.com/",
    "BN": "https://bam.brookfield.com/investors",
    # --- Evaluation list ---
    "LLY": "https://investor.lilly.com/financial-information",
    "V": "https://investor.visa.com/financial-information/quarterly-earnings/default.aspx",
    "ORCL": "https://investor.oracle.com/financials/default.aspx",
    "UBER": "https://investor.uber.com/news-events/default.aspx",
    "ABNB": "https://investors.airbnb.com/financials/quarterly-results/default.aspx",
    "BKNG": "https://ir.bookingholdings.com/financial-information/quarterly-results",
    "TMO": "https://ir.thermofisher.com/investors/news-events/financial-news/default.aspx",
    "FCX": "https://investors.fcx.com/investors/financial-information/default.aspx",
    "SOFI": "https://investors.sofi.com/financials/quarterly-results/default.aspx",
    "NTRA": "https://investor.natera.com/financial-information/quarterly-results",
    "NSP": "https://ir.insperity.com/investor-relations/quarterly-results",
    "DLO": "https://investor.dlocal.com/financials/",
    "NBIS": "https://nebius.com/investor-hub",
    "TEM": "https://investors.tempus.com/",
    "CRWV": "https://investors.coreweave.com/financials/quarterly-results/default.aspx",
    "WGS": "https://ir.genedx.com/financials-filings/quarterly-results/",
    "FRVO": "https://ir.fervoenergy.com/investor-relations",
    "FIGR": "https://investors.figure.com/",
    "CGEH": "https://ir.capstonegreenenergy.com/",  # Capstone Green Energy (DB name "Care.com" is stale)
    "BHP": "https://www.bhp.com/investors/financial-results",  # Australian — half-yearly
    "NTDOY": "https://www.nintendo.co.jp/ir/en/",  # Japanese ADR — semi-annual English IR
}


def resolve_ir_url(
    ticker: str,
    db_ir_url: str | None,
    config_url: str | None = None,
) -> str | None:
    """Best IR URL for ``ticker``: curated override → config URL → DB ``ir_url`` → None."""
    override = IR_URL_OVERRIDES.get(ticker.upper())
    if override:
        return override
    if config_url and config_url.strip():
        return config_url.strip()
    if db_ir_url and db_ir_url.strip():
        return db_ir_url.strip()
    return None
