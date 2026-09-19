"""One disposable process owns each untrusted PDF parsing operation.

Invoked by pdf_render with JSON on stdin. Native-library diagnostics stay in
this child; only bounded JSON and a staged PNG cross the process boundary.
The source is opened for reading and is never a save destination.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Protocol, cast

MAX_PDF_RENDER_PIXELS = 16_000_000
MAX_PDF_RENDER_DIMENSION = 8192
MAX_PDF_TEXT_CHARACTERS = 4_000_000


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


def perform(
    operation: str,
    pdf_path: Path,
    *,
    page: int = 1,
    dpi: int = 150,
    output_path: Path | None = None,
    quote: str = "",
) -> object:
    """Execute one read-only source operation; the caller enforces the deadline."""
    import pymupdf

    doc = cast("_PdfDocument", pymupdf.open(str(pdf_path)))
    try:
        if operation == "count":
            return doc.page_count
        if operation == "texts":
            texts: list[str] = []
            count = 0
            for index in range(doc.page_count):
                text = doc.load_page(index).get_text() or ""
                count += len(text)
                if count > MAX_PDF_TEXT_CHARACTERS:
                    return None
                texts.append(text)
            return texts
        if not 1 <= page <= doc.page_count:
            return None
        pdf_page = doc.load_page(page - 1)
        if operation == "dimensions":
            return [float(pdf_page.rect.width), float(pdf_page.rect.height)]
        if operation == "render":
            width = pdf_page.rect.width * dpi / 72
            height = pdf_page.rect.height * dpi / 72
            if (
                output_path is None
                or not all(math.isfinite(value) and value > 0 for value in (width, height))
                or max(width, height) > MAX_PDF_RENDER_DIMENSION
                or math.ceil(width) * math.ceil(height) > MAX_PDF_RENDER_PIXELS
            ):
                return None
            pdf_page.get_pixmap(dpi=dpi).save(str(output_path))
            return True
        if operation == "bbox":
            candidates = [quote.strip()]
            head = " ".join(quote.split()[:6])
            if head and head != candidates[0]:
                candidates.append(head)
            for needle in candidates:
                rects = pdf_page.search_for(needle)
                if rects:
                    rect = rects[0]
                    return [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)]
            return None
        raise ValueError("unsupported PDF operation")
    finally:
        doc.close()


def main() -> int:
    # The parent constructs this protocol; validate it independently of the
    # source document. No path, input contents, or exception text is logged.
    raw: object = json.load(sys.stdin)
    if not isinstance(raw, dict):
        return 2
    request = cast("dict[str, object]", raw)
    operation = request.get("operation")
    source = request.get("pdf_path")
    result = request.get("result_path")
    page = request.get("page", 1)
    dpi = request.get("dpi", 150)
    quote = request.get("quote", "")
    output = request.get("output_path")
    if (
        not isinstance(operation, str)
        or not isinstance(source, str)
        or not isinstance(result, str)
        or not isinstance(page, int)
        or not isinstance(dpi, int)
        or not isinstance(quote, str)
        or (output is not None and not isinstance(output, str))
    ):
        return 2
    try:
        value = perform(
            operation,
            Path(source),
            page=page,
            dpi=dpi,
            quote=quote,
            output_path=Path(output) if output is not None else None,
        )
        Path(result).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
