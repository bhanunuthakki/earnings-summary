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
import urllib.error
import urllib.request
import urllib.response
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
from ir_pipeline.discover import IrDiscoveryAuthenticationDeniedError
from ir_pipeline.discover._docmeta import classify, filename_for_url

# Runs in the page: visible (offsetParent != null) anchors → their hrefs.
_VISIBLE_HREFS_JS = "els => els.filter(a => a.offsetParent !== null).map(a => a.href)"
MZ_CATALOG_MAX_BYTES = 2 * 1024 * 1024
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


def _authentication_denial_status(status_code: object) -> int | None:
    if isinstance(status_code, int) and status_code in {401, 403}:
        return status_code
    return None


def _read_bounded(response: object) -> bytes:
    read = getattr(response, "read", None)
    if not callable(read):
        raise ValueError("MZ response has no readable body")
    body = cast(bytes, read(MZ_CATALOG_MAX_BYTES + 1))
    if len(body) > MZ_CATALOG_MAX_BYTES:
        raise ValueError("MZ response exceeds the 2 MiB limit")
    return body


def _get_text(url: str, timeout: int = 30) -> str:
    ensure_safe_public_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        response = cast(
            urllib.response.addinfourl,
            build_public_opener().open(request, timeout=timeout),
        )
        with response:
            return _read_bounded(response).decode("utf-8", errors="strict")
    except urllib.error.HTTPError as exc:
        status_code = _authentication_denial_status(exc.code)
        if status_code is not None:
            raise IrDiscoveryAuthenticationDeniedError(status_code) from None
        raise


def _post_json(url: str, payload: dict[str, object], timeout: int = 30) -> object:
    ensure_safe_public_url(url)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    try:
        response = cast(
            urllib.response.addinfourl,
            build_public_opener().open(request, timeout=timeout),
        )
        with response:
            return cast(object, json.loads(_read_bounded(response)))
    except urllib.error.HTTPError as exc:
        status_code = _authentication_denial_status(exc.code)
        if status_code is not None:
            raise IrDiscoveryAuthenticationDeniedError(status_code) from None
        raise


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


def catalog_config(page_html: str) -> tuple[str, str, list[str]]:
    """Read the publisher's MZ endpoint and category scope from retained HTML."""

    return _catalog_config(page_html)


def catalog_post_request(
    base: str,
    issuer_id: str,
    categories: tuple[str, ...],
    *,
    year: int | None,
) -> tuple[str, bytes]:
    """Return the exact endpoint and JSON bytes used by this adapter."""

    if year is None:
        url = f"{base}/company/{issuer_id}/categoryInternalName/document/language/years"
    else:
        url = f"{base}/company/{issuer_id}/filter/categories/year/meta"
    return url, catalog_post_body(categories, year=year)


def catalog_post_body(categories: tuple[str, ...], *, year: int | None) -> bytes:
    return json.dumps(catalog_post_payload(categories, year=year)).encode("utf-8")


def catalog_post_payload(categories: tuple[str, ...], *, year: int | None) -> dict[str, object]:
    if year is None:
        payload: dict[str, object] = {
            "categoryInternalNames": list(categories),
            "language_code": "en_US",
        }
    else:
        payload = {
            "year": str(year),
            "categories": list(categories),
            "language": "en_US",
            "published": True,
        }
    return payload


def catalog_years(body: bytes) -> tuple[int, ...]:
    envelope = _YearsEnvelope.model_validate_json(body, strict=True)
    if (
        not envelope.success
        or not envelope.data
        or len(envelope.data) > 30
        or len(envelope.data) != len(set(envelope.data))
        or any(year < 2000 or year > 2100 for year in envelope.data)
    ):
        raise ValueError("MZ catalog did not return a unique nonempty year set")
    return tuple(envelope.data)


def catalog_document_urls(body: bytes) -> tuple[str, ...]:
    envelope = _DocumentsEnvelope.model_validate_json(body, strict=True)
    if not envelope.success:
        raise ValueError("MZ catalog document response was not successful")
    urls: list[str] = []
    for document in envelope.data.document_metas:
        url = document.link_url or document.permalink
        if url is None:
            raise ValueError("MZ catalog document has no URL")
        urls.append(url)
    if len(urls) != len(set(urls)):
        raise ValueError("MZ catalog returned duplicate document URLs")
    return tuple(urls)


# Public, typed seams used by boundary tests and adapter callers.
get_text = _get_text
post_json = _post_json


def _catalog_documents(results_center_url: str) -> dict[str, str]:
    base, issuer_id, categories = _catalog_config(_get_text(results_center_url))
    category_names = tuple(categories)
    years_url, _years_body = catalog_post_request(base, issuer_id, category_names, year=None)
    years = _YearsEnvelope.model_validate(
        _post_json(
            years_url,
            catalog_post_payload(category_names, year=None),
        )
    )
    if not years.success or not years.data:
        return {}

    current_year = max(years.data)
    documents_url, _documents_body = catalog_post_request(
        base, issuer_id, category_names, year=current_year
    )
    documents = _DocumentsEnvelope.model_validate(
        _post_json(
            documents_url,
            catalog_post_payload(category_names, year=current_year),
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
            response = cast(
                object,
                page.goto(url, wait_until="networkidle", timeout=timeout_ms),
            )
            status_code = _authentication_denial_status(getattr(response, "status", None))
            if status_code is not None:
                raise IrDiscoveryAuthenticationDeniedError(status_code)
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
