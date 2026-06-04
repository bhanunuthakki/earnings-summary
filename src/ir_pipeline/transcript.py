"""Fetch the analyst-Q&A segment of an earnings call from the issuer's OWN
IR results-center — the timeliest source, since companies publish the official
transcript on their site days-to-weeks before the free aggregators index the
call (especially foreign issuers like NU/MELI/NVO).

The transcript URL is located through the shared IR-document discovery built for
the headless-fetch pipeline (`ir_pipeline.discover` + the persisted
`.tmp/ir_url_manifest/<T>_urls.json`), so a single crawl of the results-center
serves both this Q&A path and the document-corpus fetcher. We read the manifest
first — zero browser when a prior crawl (e.g. the weekly discovery cron) already
indexed the transcript — and fall back to a single live
`discover_history_hybrid` crawl only when the manifest has no transcript for the
requested quarter. Each candidate's authoritative doc-type + period come from the
filename its file host advertises (`_docmeta.classify` / `filename_for_url`), not
the opaque hash URL.

Scope: discovery returns whatever quarters the results-center exposes — an mz
JS-widget site shows only the current quarter; the generic crawler can reach a
few more via one-hop history links. We return a hit only for the requested
(year, quarter); any other quarter falls through to the aggregator chain (which
has the older quarters). The PDF is text-extracted, normalized, and trimmed to
the Q&A segment (prepared remarks are reproducible from the press release, so —
like `fetch_qa_transcript.py` — we keep only the unique Q&A content).

Discovery's headless browser is the optional `ir` extra, imported lazily deep in
`ir_pipeline.discover`; this module stays importable everywhere and a missing
extra (or any render/download failure) degrades to "no hit".
"""

from __future__ import annotations

import io
import re
import unicodedata
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests
from pypdf import PdfReader

from ir_pipeline.config import IrConfig, get_config
from ir_pipeline.discover import discover_history_hybrid
from ir_pipeline.discover._docmeta import classify, filename_for_url
from ir_pipeline.manifest import load_manifest

# src/ir_pipeline/transcript.py → repo root is parents[2] (mirrors config.py).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_RENDER_TIMEOUT_MS = 60000
_DOWNLOAD_TIMEOUT_S = 60
# A genuine full call is tens of KB; below this the extract is a stub / failure.
_MIN_TRANSCRIPT_CHARS = 2000

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


def _match_transcript(
    urls: list[str], year: int, quarter: int, fetch_filename: Callable[[str], str]
) -> tuple[str, str] | None:
    """First (url, filename) among `urls` whose host-advertised filename is a
    transcript for the requested (year, quarter).

    `fetch_filename` resolves the Content-Disposition name (injected so the
    selection logic is unit-testable without the network). A link whose filename
    can't be probed is skipped — we try the next candidate.
    """
    for url in urls:
        try:
            filename = fetch_filename(url)
        except (urllib.error.URLError, OSError, ValueError):
            continue  # header probe failed for this link — skip it, try the next
        if classify(filename) == "transcript" and _quarter_of(filename) == (quarter, year):
            return url, filename
    return None


def _locate_transcript(
    cfg: IrConfig, ticker: str, year: int, quarter: int, repo_root: Path
) -> tuple[str, str] | None:
    """Locate the requested quarter's transcript via the shared IR discovery.

    Manifest-first — reuses a prior crawl's URLs (the weekly discovery cron or an
    on-demand `discover_ir_documents` run), so a cached transcript launches no
    browser. Only when the manifest has no matching transcript do we run a single
    live `discover_history_hybrid` crawl (mz current-quarter fast-path + generic
    one-hop history), the same crawl the document-corpus fetcher uses.
    """
    manifest_urls = [e.url for e in load_manifest(repo_root, ticker) if e.doc_type == "transcript"]
    hit = _match_transcript(manifest_urls, year, quarter, filename_for_url)
    if hit is not None:
        return hit

    docs = discover_history_hybrid(
        ir_url=cfg.results_center_url, config=cfg, timeout_ms=_RENDER_TIMEOUT_MS
    )
    hybrid_urls = [d.url for d in docs if d.doc_type_guess == "transcript"]
    return _match_transcript(hybrid_urls, year, quarter, filename_for_url)


def fetch_ir_transcript(
    ticker: str, year: int, quarter: int, *, repo_root: Path | None = None
) -> IrTranscriptHit | None:
    """Locate + download the issuer's own transcript for (year, quarter).

    Returns None — so the caller falls through to the aggregator chain — when the
    ticker has no MZ IR config, the shared discovery surfaces no transcript for
    the requested quarter, or Playwright/render/download fails.
    """
    cfg = get_config(ticker, repo_root)
    if cfg is None or cfg.platform != "mz" or not cfg.results_center_url:
        return None

    found = _locate_transcript(cfg, ticker, year, quarter, repo_root or _PROJECT_ROOT)
    if found is None:
        return None
    url, filename = found

    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=_DOWNLOAD_TIMEOUT_S)
    if resp.status_code != 200 or not resp.content:
        return None

    text = _normalize(_extract_pdf_text(resp.content))
    if len(text) < _MIN_TRANSCRIPT_CHARS:
        return None
    return IrTranscriptHit(page_url=url, qa_text=_qa_segment(text), filename=filename)
