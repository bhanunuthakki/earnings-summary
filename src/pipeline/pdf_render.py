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

import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Literal, cast

log = logging.getLogger(__name__)
DEFAULT_PDF_RENDER_DPI = 150
PDF_OPERATION_TIMEOUT_SECONDS = 15.0
PDF_WORKER_PATH = Path(__file__).with_name("pdf_worker.py")
MAX_PDF_RESULT_BYTES = 24_000_000
_WORKER_SLOTS = threading.BoundedSemaphore(4)
_PDF_PAGES_CACHE_DIR = Path(".tmp") / "pdf_pages"
_WS_RX = re.compile(r"\s+")
Operation = Literal["count", "dimensions", "render", "texts", "bbox"]


def _normalize(text: str) -> str:
    return _WS_RX.sub(" ", text).strip().casefold()


def _run_pdf(operation: Operation, pdf_path: Path, directory: Path, **arguments: object) -> object:
    result_path = directory / "result.json"
    request = {
        "operation": operation,
        "pdf_path": str(pdf_path.resolve()),
        "result_path": str(result_path),
        **arguments,
    }
    # The native parser needs interpreter/system paths, not application secrets.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "TMPDIR"}
    }
    if not _WORKER_SLOTS.acquire(blocking=False):
        log.warning({"event": "pdf_operation_busy", "operation": operation})
        return None
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(PDF_WORKER_PATH)],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=PDF_OPERATION_TIMEOUT_SECONDS,
            check=False,
        )
        # subprocess.run kills and waits for the child before raising on timeout.
        if completed.returncode != 0 or result_path.stat().st_size > MAX_PDF_RESULT_BYTES:
            return None
        return json.loads(result_path.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired:
        log.warning({"event": "pdf_operation_timeout", "operation": operation})
    except (OSError, ValueError):
        log.warning({"event": "pdf_operation_failed", "operation": operation})
    finally:
        _WORKER_SLOTS.release()
    return None


def _read_pdf(operation: Operation, pdf_path: Path, **arguments: object) -> object:
    try:
        if not pdf_path.is_file():
            return None
        with tempfile.TemporaryDirectory(prefix=".pdf-worker-") as temporary:
            return _run_pdf(operation, pdf_path, Path(temporary), **arguments)
    except OSError:
        log.warning({"event": "pdf_operation_failed", "operation": operation})
        return None


def rendered_page_path(
    repo_root: Path, *, sha256: str, page: int, dpi: int = DEFAULT_PDF_RENDER_DPI
) -> Path:
    """Content-addressed preview cache; a cache hit never launches a process."""
    return repo_root / _PDF_PAGES_CACHE_DIR / sha256[:16] / f"p{page}_dpi{dpi}.png"


def page_count(pdf_path: Path) -> int | None:
    value = _read_pdf("count", pdf_path)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _coordinates(value: object, count: int) -> list[float] | None:
    if not isinstance(value, list):
        return None
    values = cast("list[object]", value)
    if len(values) != count or any(not isinstance(item, (int, float)) for item in values):
        return None
    coordinates = [float(item) for item in values if isinstance(item, (int, float))]
    return coordinates if all(math.isfinite(item) for item in coordinates) else None


def page_dimensions(pdf_path: Path, page: int) -> tuple[float, float] | None:
    """Width/height in PDF points; None on parser failure or deadline."""
    value = _coordinates(_read_pdf("dimensions", pdf_path, page=page), 2)
    return (value[0], value[1]) if value is not None else None


def render_page_image(
    repo_root: Path,
    *,
    pdf_path: Path,
    sha256: str,
    page: int,
    dpi: int = DEFAULT_PDF_RENDER_DPI,
) -> Path | None:
    """Rasterize within a deadline and atomically publish only a complete PNG."""
    out_path = rendered_page_path(repo_root, sha256=sha256, page=page, dpi=dpi)
    if out_path.is_file():
        return out_path
    if not pdf_path.is_file():
        return None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".pdf-worker-", dir=out_path.parent) as temporary:
            directory = Path(temporary)
            staged = directory / "page.png"
            result = _run_pdf(
                "render", pdf_path, directory, page=page, dpi=dpi, output_path=str(staged.resolve())
            )
            if result is not True or not staged.is_file():
                return None
            with staged.open("rb") as image:
                if image.read(8) != b"\x89PNG\r\n\x1a\n":
                    return None
            staged.replace(out_path)
        return out_path
    except OSError:
        log.warning({"event": "pdf_render_page_failed", "page": page, "dpi": dpi})
        return None


def extract_page_texts(pdf_path: Path) -> list[str] | None:
    """Extract page text within one document-wide deadline and output limit."""
    value = _read_pdf("texts", pdf_path)
    if not isinstance(value, list):
        return None
    values = cast("list[object]", value)
    return (
        [item for item in values if isinstance(item, str)]
        if all(isinstance(item, str) for item in values)
        else None
    )


def find_quote_bbox(
    pdf_path: Path, page: int, quote: str
) -> tuple[float, float, float, float] | None:
    """Locate a verbatim quote; a timeout never creates a fabricated anchor."""
    if not quote.strip():
        return None
    value = _coordinates(_read_pdf("bbox", pdf_path, page=page, quote=quote), 4)
    return (value[0], value[1], value[2], value[3]) if value is not None else None


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
