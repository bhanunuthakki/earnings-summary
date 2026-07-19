"""PDF page-image rendering + quote location for provenance click-throughs.

Phase B of docs/design/provenance_clickthrough.md (§2.3): the app never had a
PDF-rendering capability — a ``pdf_page`` locator (IR decks, supplements) had
nothing to land on. This module supplies the two primitives that close it:

  * :func:`render_page_image` — rasterize one PDF page to a cached PNG via
    PyMuPDF (``fitz``, already a soft dependency through
    ``ir_uploads._fingerprint_pdf_pymupdf``). Idempotent cache under
    ``.tmp/pdf_pages/<sha256[:16]>/p<page>_dpi<dpi>.png`` — content-addressed
    (documents are sha256-keyed and never mutated, so a changed source PDF is
    a NEW documents row → new sha → new cache dir; no invalidation logic).
    ``.tmp/`` because rendered previews are regenerable intermediates, never
    deliverables (repo file-organization contract).
  * :func:`find_page_for_quote` / :func:`find_quote_bbox` — locate a verbatim
    excerpt inside a PDF (page number, and where possible a bounding box via
    ``page.search_for``) so extractors and the retrofit CLI can mint
    ``pdf_slide`` locators for values whose page was never recorded.

Every function degrades to ``None`` when PyMuPDF is unavailable or the PDF is
unreadable — rendering is an enrichment layer; a missing renderer must never
break a write path or a peek (the peek's §2.7 legacy floor is the fallback).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Protocol, cast

log = logging.getLogger(__name__)

# 150 DPI: legible for a deck slide, small enough to cache (§2.3's default).
DEFAULT_PDF_RENDER_DPI = 150

# Cache directory for rendered page PNGs, relative to repo root.
_PDF_PAGES_CACHE_DIR = Path(".tmp") / "pdf_pages"

_WS_RX = re.compile(r"\s+")


# --- Minimal typed view over the untyped fitz API surface we use. The single
# cast in _open_pdf is the validated external-library boundary (repo typing
# convention) — everything downstream stays fully typed.


class _PdfRect(Protocol):
    x0: float
    y0: float
    x1: float
    y1: float
    width: float
    height: float


class _PdfPixmap(Protocol):
    def save(self, filename: str) -> None: ...


class _PdfPage(Protocol):
    @property
    def rect(self) -> _PdfRect: ...

    def get_text(self) -> str: ...

    def get_pixmap(self, *, dpi: int) -> _PdfPixmap: ...

    def search_for(self, needle: str) -> list[_PdfRect]: ...


class _PdfDocument(Protocol):
    page_count: int

    def load_page(self, page_id: int) -> _PdfPage: ...

    def close(self) -> None: ...


def _normalize(text: str) -> str:
    return _WS_RX.sub(" ", text).strip().casefold()


def _open_pdf(pdf_path: Path) -> _PdfDocument | None:
    """Open ``pdf_path`` with PyMuPDF; None when fitz is unavailable, the
    file is missing, or the document can't be parsed. Caller must close."""
    if not pdf_path.exists():
        return None
    try:
        import fitz  # PyMuPDF — soft dependency, same pattern as ir_uploads
    except ImportError:
        log.warning({"event": "pdf_render_fitz_unavailable", "path": str(pdf_path)})
        return None
    try:
        return cast("_PdfDocument", fitz.open(str(pdf_path)))
    except Exception:  # PyMuPDF's error tree is wide; degrade, never crash
        log.warning({"event": "pdf_render_open_failed", "path": str(pdf_path)})
        return None


def rendered_page_path(
    repo_root: Path, *, sha256: str, page: int, dpi: int = DEFAULT_PDF_RENDER_DPI
) -> Path:
    """Deterministic cache path for one rendered page — sha256 + page + dpi
    keyed, so re-requesting the same page is a filesystem check, not a
    re-render."""
    return repo_root / _PDF_PAGES_CACHE_DIR / sha256[:16] / f"p{page}_dpi{dpi}.png"


def page_count(pdf_path: Path) -> int | None:
    """Number of pages in the PDF, or None when unreadable/fitz-less."""
    doc = _open_pdf(pdf_path)
    if doc is None:
        return None
    try:
        return doc.page_count
    finally:
        doc.close()


def page_dimensions(pdf_path: Path, page: int) -> tuple[float, float] | None:
    """(width, height) of a 1-based ``page`` in PDF points — the coordinate
    space ``FactLocator.pdf_bbox`` is expressed in, needed to convert a bbox
    into percentage offsets over the rendered image."""
    doc = _open_pdf(pdf_path)
    if doc is None:
        return None
    try:
        if not 1 <= page <= doc.page_count:
            return None
        rect = doc.load_page(page - 1).rect
        return (float(rect.width), float(rect.height))
    except Exception:
        return None
    finally:
        doc.close()


def render_page_image(
    repo_root: Path,
    *,
    pdf_path: Path,
    sha256: str,
    page: int,
    dpi: int = DEFAULT_PDF_RENDER_DPI,
) -> Path | None:
    """Rasterize 1-based ``page`` of ``pdf_path`` to a cached PNG.

    Returns the cache path (rendering only on a cache miss), or None when the
    page can't be rendered (missing file, fitz unavailable, page out of
    range). The cache key is (sha256, page, dpi) so the same request never
    re-renders.
    """
    out_path = rendered_page_path(repo_root, sha256=sha256, page=page, dpi=dpi)
    if out_path.exists():
        return out_path
    doc = _open_pdf(pdf_path)
    if doc is None:
        return None
    try:
        if not 1 <= page <= doc.page_count:
            return None
        pixmap = doc.load_page(page - 1).get_pixmap(dpi=dpi)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Write via a temp name + replace so a concurrent request never reads
        # a half-written PNG (renders are idempotent, so last-write-wins).
        # The temp name keeps a .png suffix — pixmap.save infers the image
        # format from the filename extension.
        tmp_path = out_path.parent / f"{out_path.stem}.tmp.png"
        pixmap.save(str(tmp_path))
        tmp_path.replace(out_path)
        return out_path
    except Exception:
        log.warning(
            {"event": "pdf_render_page_failed", "path": str(pdf_path), "page": page, "dpi": dpi}
        )
        return None
    finally:
        doc.close()


def extract_page_texts(pdf_path: Path) -> list[str] | None:
    """Per-page plain text for the whole PDF (index 0 = page 1), or None when
    unreadable. The page-attribution substrate for :func:`find_page_for_quote`."""
    doc = _open_pdf(pdf_path)
    if doc is None:
        return None
    try:
        return [doc.load_page(i).get_text() or "" for i in range(doc.page_count)]
    except Exception:
        return None
    finally:
        doc.close()


def find_quote_bbox(
    pdf_path: Path, page: int, quote: str
) -> tuple[float, float, float, float] | None:
    """Bounding box (x0, y0, x1, y1, page coords) of ``quote`` on 1-based
    ``page`` via ``page.search_for`` — the §1.2 fallback path for extractors
    that don't get bboxes for free. A multi-line hit returns the first line's
    rect (a stable anchor beats a page-spanning union). None when not found."""
    if not quote.strip():
        return None
    doc = _open_pdf(pdf_path)
    if doc is None:
        return None
    try:
        if not 1 <= page <= doc.page_count:
            return None
        pg = doc.load_page(page - 1)
        # search_for is whitespace-tolerant but not case-folding; try the
        # verbatim quote first, then a shorter head (long quotes fail when the
        # extractor's whitespace normalization diverged from the PDF's).
        candidates = [quote.strip()]
        head = " ".join(quote.split()[:6])
        if head and head != candidates[0]:
            candidates.append(head)
        for needle in candidates:
            rects = pg.search_for(needle)
            if rects:
                r = rects[0]
                return (float(r.x0), float(r.y0), float(r.x1), float(r.y1))
        return None
    except Exception:
        return None
    finally:
        doc.close()


def find_page_for_quote(
    pdf_path: Path, quote: str, *, page_texts: list[str] | None = None
) -> tuple[int, tuple[float, float, float, float] | None] | None:
    """Locate a verbatim excerpt in the PDF: (1-based page, bbox-or-None).

    Page match is the whitespace/case-normalized substring check (the same
    honesty bar as ``pipeline.locators.verify_quote_in_source`` — no fuzzy
    matching; an excerpt that can't be found verbatim stays legacy rather
    than getting a fabricated anchor). ``page_texts`` lets a caller doing
    many lookups against one PDF build the index once via
    :func:`extract_page_texts`.
    """
    if not quote.strip():
        return None
    texts = page_texts if page_texts is not None else extract_page_texts(pdf_path)
    if not texts:
        return None
    needle = _normalize(quote)
    for i, text in enumerate(texts):
        if needle in _normalize(text):
            page = i + 1
            return (page, find_quote_bbox(pdf_path, page, quote))
    return None
