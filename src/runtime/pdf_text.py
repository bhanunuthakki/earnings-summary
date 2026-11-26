"""Shared PyMuPDF-backed PDF text extraction helpers."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Protocol, cast


class _PdfPage(Protocol):
    def get_text(self) -> object: ...


class _PdfDocument(Protocol):
    page_count: int

    def load_page(self, page_id: int) -> _PdfPage: ...

    def close(self) -> None: ...


def _open_pdf(source: str | Path | bytes | BinaryIO) -> _PdfDocument:
    import pymupdf

    if isinstance(source, (bytes, bytearray)):
        return cast("_PdfDocument", pymupdf.open(stream=bytes(source), filetype="pdf"))
    if isinstance(source, (str, Path)):
        return cast("_PdfDocument", pymupdf.open(str(source)))
    return cast("_PdfDocument", pymupdf.open(stream=source.read(), filetype="pdf"))


def extract_pdf_pages(
    source: str | Path | bytes | BinaryIO,
    *,
    max_pages: int | None = None,
) -> list[str]:
    """Return per-page extracted text from a PDF path, bytes, or binary stream."""
    doc = _open_pdf(source)
    try:
        count = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
        pages: list[str] = []
        for idx in range(count):
            text = doc.load_page(idx).get_text()
            pages.append(text if isinstance(text, str) else "")
        return pages
    finally:
        doc.close()


def extract_pdf_text(
    source: str | Path | bytes | BinaryIO,
    *,
    max_pages: int | None = None,
    separator: str = "\n",
) -> str:
    """Return joined PDF text across up to ``max_pages`` pages."""
    return separator.join(extract_pdf_pages(source, max_pages=max_pages))
