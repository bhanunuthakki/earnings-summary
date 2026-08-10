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

import json
import re
import urllib.request
from typing import cast

from pydantic import BaseModel, ValidationError

from ir_pipeline._net import (
    PLAYWRIGHT_NETWORK_LOCKDOWN_ARG,
    PLAYWRIGHT_NO_PROXY_ARG,
    build_public_opener,
    ensure_safe_public_url,
    install_public_only_playwright_routing,
)
from ir_pipeline.config import IrConfig
from ir_pipeline.discover._docmeta import classify, filename_for_url

# Runs in the page: visible (offsetParent != null) anchors → their hrefs.
_VISIBLE_HREFS_JS = "els => els.filter(a => a.offsetParent !== null).map(a => a.href)"
_MAX_CATALOG_BYTES = 2 * 1024 * 1024
_CATALOG_DOC_TYPES = {
    "apresentacao_resultados": "deck",
    "release_resultados": "press_release",
    "planilha_resultados": "spreadsheet",
    "script": "transcript",
}


class _YearsEnvelope(BaseModel):
    success: bool
    data: list[int]


class _DocumentMeta(BaseModel):
    internal_name: str
    file_title: str
    link_url: str | None = None
    permalink: str | None = None


class _DocumentData(BaseModel):
    document_metas: list[_DocumentMeta]


class _DocumentsEnvelope(BaseModel):
    success: bool
    data: _DocumentData


def _read_bounded(response: object) -> bytes:
    read = getattr(response, "read", None)
    if not callable(read):
        raise ValueError("MZ response has no readable body")
    body = cast(bytes, read(_MAX_CATALOG_BYTES + 1))
    if len(body) > _MAX_CATALOG_BYTES:
        raise ValueError("MZ response exceeds the 2 MiB limit")
    return body


def _get_text(url: str, timeout: int = 30) -> str:
    ensure_safe_public_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with build_public_opener().open(request, timeout=timeout) as response:
        return _read_bounded(response).decode("utf-8", errors="strict")


def _post_json(url: str, payload: dict[str, object], timeout: int = 30) -> object:
    ensure_safe_public_url(url)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    with build_public_opener().open(request, timeout=timeout) as response:
        return cast(object, json.loads(_read_bounded(response)))


def _catalog_config(page_html: str) -> tuple[str, str, list[str]]:
    issuer_match = re.search(r"const\s+fmId\s*=\s*['\"]([^'\"]+)", page_html)
    base_match = re.search(r"const\s+fmBase\s*=\s*['\"]([^'\"]+)", page_html)
    categories = list(
        dict.fromkeys(
            value.strip() for value in re.findall(r"internal_name\s*:\s*['\"]([^'\"]+)", page_html)
        )
    )
    if issuer_match is None or base_match is None or not categories:
        raise ValueError("MZ catalog configuration is absent from the results page")
    return base_match.group(1).rstrip("/"), issuer_match.group(1), categories


def _catalog_documents(results_center_url: str) -> dict[str, str]:
    base, issuer_id, categories = _catalog_config(_get_text(results_center_url))
    years_url = f"{base}/company/{issuer_id}/categoryInternalName/document/language/years"
    years = _YearsEnvelope.model_validate(
        _post_json(
            years_url,
            {"categoryInternalNames": categories, "language_code": "en_US"},
        )
    )
    if not years.success or not years.data:
        return {}

    documents_url = f"{base}/company/{issuer_id}/filter/categories/year/meta"
    documents = _DocumentsEnvelope.model_validate(
        _post_json(
            documents_url,
            {
                "year": str(max(years.data)),
                "categories": categories,
                "language": "en_US",
                "published": True,
            },
        )
    )
    if not documents.success:
        return {}

    found: dict[str, str] = {}
    for document in documents.data.document_metas:
        internal_name = document.internal_name.strip()
        doc_type = classify(document.file_title) or _CATALOG_DOC_TYPES.get(internal_name)
        target = document.link_url or document.permalink
        if doc_type is None or target is None or doc_type in found:
            continue
        found[doc_type] = ensure_safe_public_url(target)
    return found


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
    try:
        catalog = _catalog_documents(config.results_center_url)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, ValidationError):
        catalog = {}
    if catalog:
        return catalog

    docs: dict[str, str] = {}
    for url in _visible_filemanager_hrefs(config.results_center_url):
        doc_type = classify(filename_for_url(url))
        if doc_type and doc_type not in docs:
            docs[doc_type] = url
    return docs
