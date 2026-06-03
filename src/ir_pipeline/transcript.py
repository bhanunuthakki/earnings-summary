"""Fetch the analyst-Q&A segment of an earnings call from the issuer's OWN
IR results-center — the timeliest source, since companies publish the official
transcript on their site days-to-weeks before the free aggregators index the
call (especially foreign issuers like NU/MELI/NVO).

This reuses the MZ/mziq discovery already built for the spreadsheet-KPI pipeline
(`ir_pipeline.discover.mz`): the results-center is a JS app whose download links
are `api.mziq.com/mzfilemanager` hashes that appear only after render, so we
drive headless Playwright, read the visible (current-quarter) anchors, and pick
the one whose advertised filename classifies as a transcript.

Scope: the results-center default view shows the LATEST quarter, which is exactly
what the post-earnings scan wants. We return a hit only when the discovered
transcript's filename quarter matches the requested (year, quarter); otherwise we
return None so the caller falls through to the aggregator chain (which has the
older quarters). The PDF is text-extracted, normalized, and trimmed to the Q&A
segment (prepared remarks are reproducible from the press release, so — like
`fetch_qa_transcript.py` — we keep only the unique Q&A content).

Playwright is the optional `ir` extra; it is imported lazily so this module
stays importable everywhere and a missing extra degrades to "no hit".
"""

from __future__ import annotations

import io
import re
import unicodedata
import urllib.error
from dataclasses import dataclass
from pathlib import Path

import requests
from pypdf import PdfReader

from ir_pipeline.config import get_config
from ir_pipeline.discover._docmeta import classify, filename_for_url

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_RENDER_TIMEOUT_MS = 60000
_DOWNLOAD_TIMEOUT_S = 60
# A genuine full call is tens of KB; below this the extract is a stub / failure.
_MIN_TRANSCRIPT_CHARS = 2000

# Runs in the page: visible (offsetParent != null) anchors → their hrefs. Mirrors
# ir_pipeline.discover.mz so the two discovery paths agree on "visible".
_VISIBLE_HREFS_JS = "els => els.filter(a => a.offsetParent !== null).map(a => a.href)"

# Start-of-Q&A markers, in the issuer's own transcript prose. The first match
# bounds the prepared-remarks/Q&A split; everything from it onward is the Q&A.
_QA_START_RX = re.compile(
    r"(?i)(?:"
    r"we will now (?:start|begin) the q\s*&\s*a"
    r"|question[- ]and[- ]answer session"
    r"|open the line for"
    r"|(?:first|next) question (?:comes|is|will)"
    r")"
)

# Quarter token in a transcript filename: "1Q26" / "Q1 2026" / "1T26" (PT
# "Trimestre"). Two-digit years are expanded to 20xx.
_QUARTER_RX = re.compile(
    r"(?:Q([1-4])\s*'?(\d{2,4})|([1-4])\s*[QT]\s*'?(\d{2,4}))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IrTranscriptHit:
    """A transcript located + extracted from an issuer's IR results-center."""

    page_url: str
    qa_text: str
    filename: str


def _normalize(raw: str) -> str:
    """Clean issuer-PDF text: fold ligatures/compat chars (NFKC turns "ﬁ"→"fi"),
    drop zero-width spaces some PDFs use as separators, collapse the runs of
    spaces PDF extraction emits, tighten "Operator :" → "Operator:", and flatten
    the hard line-wraps so the Q&A parser sees flowing turns."""
    text = unicodedata.normalize("NFKC", raw).replace("​", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" +([:?!,.])", r"\1", text)
    text = re.sub(r"\n+", " ", text)
    return re.sub(r" +", " ", text).strip()


def _quarter_of(filename: str) -> tuple[int, int] | None:
    """Parse (quarter, year) from a transcript filename, or None if absent."""
    m = _QUARTER_RX.search(filename)
    if m is None:
        return None
    if m.group(1) is not None:
        quarter, year = int(m.group(1)), int(m.group(2))
    else:
        quarter, year = int(m.group(3)), int(m.group(4))
    if year < 100:
        year += 2000
    return quarter, year


def _qa_segment(text: str) -> str:
    """Return the transcript from the first Q&A-start marker onward. If no marker
    is found, return the whole text (the roster parser finds turn boundaries in
    flowing text either way)."""
    m = _QA_START_RX.search(text)
    return text[m.start() :].strip() if m is not None else text


def _extract_pdf_text(content: bytes) -> str:
    """Extract all text from PDF bytes (pypdf, the repo's transcript extractor)."""
    reader = PdfReader(io.BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _discover_transcript_url(
    results_center_url: str, *, timeout_ms: int = _RENDER_TIMEOUT_MS
) -> tuple[str, str] | None:
    """Render the MZ results-center and return (url, filename) of the visible
    transcript document, or None. Playwright is imported lazily (`ir` extra)."""
    from playwright.sync_api import sync_playwright  # lazy import: optional `ir` extra

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=_UA)
            page.goto(results_center_url, wait_until="networkidle", timeout=timeout_ms)
            raw_hrefs = page.eval_on_selector_all("a[href*='mzfilemanager']", _VISIBLE_HREFS_JS)
        finally:
            browser.close()

    for href in dict.fromkeys(str(h) for h in raw_hrefs):
        try:
            filename = filename_for_url(href)
        except (urllib.error.URLError, OSError, ValueError):
            continue  # header probe failed for this link — skip it, try the next
        if classify(filename) == "transcript":
            return href, filename
    return None


def fetch_ir_transcript(
    ticker: str, year: int, quarter: int, *, repo_root: Path | None = None
) -> IrTranscriptHit | None:
    """Locate + download the issuer's own transcript for (year, quarter).

    Returns None — so the caller falls through to the aggregator chain — when the
    ticker has no MZ IR config, Playwright/render/download fails, or the
    results-center's current transcript is a different quarter than requested
    (only the latest quarter is on the default view).
    """
    cfg = get_config(ticker, repo_root)
    if cfg is None or cfg.platform != "mz" or not cfg.results_center_url:
        return None

    found = _discover_transcript_url(cfg.results_center_url)
    if found is None:
        return None
    url, filename = found

    file_quarter = _quarter_of(filename)
    if file_quarter is not None and file_quarter != (quarter, year):
        return None

    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=_DOWNLOAD_TIMEOUT_S)
    if resp.status_code != 200 or not resp.content:
        return None

    text = _normalize(_extract_pdf_text(resp.content))
    if len(text) < _MIN_TRANSCRIPT_CHARS:
        return None
    return IrTranscriptHit(page_url=url, qa_text=_qa_segment(text), filename=filename)
