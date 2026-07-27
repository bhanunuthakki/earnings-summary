"""MZ/mziq discovery — headless-render the results-center, return document URLs.

Nubank-style IR sites are WordPress/MZ JS apps whose download links point at
``api.mziq.com/mzfilemanager`` hash URLs that rotate each quarter and only appear
after JavaScript runs (they are absent from the raw HTML). Playwright renders the
page, reads the *visible* (current-quarter) mzfilemanager anchors, and each is
classified by the filename its host advertises.

The historical-data spreadsheet is cumulative — one current file holds every
quarter — so resolving just the latest quarter's spreadsheet is sufficient for an
8-quarter (or full-history) refresh.
"""

from __future__ import annotations

from ir_pipeline._net import (
    PLAYWRIGHT_NETWORK_LOCKDOWN_ARG,
    PLAYWRIGHT_NO_PROXY_ARG,
    ensure_safe_public_url,
    install_public_only_playwright_routing,
)
from ir_pipeline.config import IrConfig
from ir_pipeline.discover._docmeta import classify, filename_for_url

# Runs in the page: visible (offsetParent != null) anchors → their hrefs.
_VISIBLE_HREFS_JS = "els => els.filter(a => a.offsetParent !== null).map(a => a.href)"


def _visible_filemanager_hrefs(url: str, timeout_ms: int = 60000) -> list[str]:
    from playwright.sync_api import sync_playwright  # lazy: optional `ir` extra

    ensure_safe_public_url(url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[PLAYWRIGHT_NETWORK_LOCKDOWN_ARG, PLAYWRIGHT_NO_PROXY_ARG],
        )
        try:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                service_workers="block",
            )
            install_public_only_playwright_routing(context, timeout_s=timeout_ms / 1000)
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            raw = page.eval_on_selector_all("a[href*='mzfilemanager']", _VISIBLE_HREFS_JS)
        finally:
            browser.close()

    seen: set[str] = set()
    hrefs: list[str] = []
    for h in raw:
        h = str(h)
        if h not in seen:
            seen.add(h)
            hrefs.append(h)
    return hrefs


def discover_documents(config: IrConfig) -> dict[str, str]:
    """Return {doc_type: url} for the latest quarter on `config`'s results-center."""
    docs: dict[str, str] = {}
    for url in _visible_filemanager_hrefs(config.results_center_url):
        doc_type = classify(filename_for_url(url))
        if doc_type and doc_type not in docs:
            docs[doc_type] = url
    return docs
