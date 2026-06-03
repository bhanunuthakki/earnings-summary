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

# Ticker -> IR quarterly-results / results-center URL.
# Keep in sync with the table in directives/fetch_ir_documents.md.
IR_URL_OVERRIDES: dict[str, str] = {
    "AMZN": "https://ir.aboutamazon.com/quarterly-results/default.aspx",
    "GOOG": "https://abc.xyz/investor/",
    "GOOGL": "https://abc.xyz/investor/",
    "META": "https://investor.atmeta.com/investor-news/",
    "MELI": "https://investor.mercadolibre.com/financial-information/quarterly-results",
    "NU": "https://ir.nu.com.br/en/financial-information/quarterly-results/",
    "NVO": "https://investor.novonordisk.com/financial-reports",
    "NOW": "https://investors.servicenow.com/financial-information/quarterly-results",
    "WIX": "https://investors.wix.com/financial-information/quarterly-results",
    "RBRK": "https://ir.rubrik.com/financial-information/quarterly-results",
    "VEEV": "https://ir.veeva.com/",
    "BN": "https://bam.brookfield.com/investors",
    "LLY": "https://investor.lilly.com/financial-information",
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
