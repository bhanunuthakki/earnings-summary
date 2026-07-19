"""Tests for provenance click-through Phase B: PDF page rendering
(pipeline.pdf_render), the PDF page viewer + image route, the pdf_slide
provenance peek branch, and the PdfQuoteLocator extraction helper."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

fitz = pytest.importorskip("fitz", reason="PyMuPDF (fitz) not installed")

import comments_server  # noqa: E402

from models.facts import FactLocator, LocatorKind  # noqa: E402
from pipeline.locators import PdfQuoteLocator  # noqa: E402
from pipeline.pdf_render import (  # noqa: E402
    DEFAULT_PDF_RENDER_DPI,
    extract_page_texts,
    find_page_for_quote,
    find_quote_bbox,
    page_count,
    page_dimensions,
    render_page_image,
    rendered_page_path,
)
from pipeline.peeks import render_fact_provenance_peek  # noqa: E402
from pipeline.source_viewers import render_pdf_page_view  # noqa: E402

_SHA = "f" * 64

_QUOTE = "Revenue was $1.2 billion"


def _make_deck_pdf(path: Path) -> None:
    """A real 2-page deck: cover page + a metrics page carrying the quote."""
    doc = fitz.open()
    page1 = doc.new_page()
    page1.insert_text((72, 72), "Cover slide - FY2025 results")
    page2 = doc.new_page()
    page2.insert_text((72, 100), _QUOTE)
    page2.insert_text((72, 140), "NIM of 17.8%")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


@pytest.fixture
def deck_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "ir_documents" / "NU" / "2026-03-31" / "deck.pdf"
    _make_deck_pdf(pdf)
    return pdf


# ----------------------------------------------------------------------------
# pipeline.pdf_render primitives
# ----------------------------------------------------------------------------


def test_rendered_page_path_is_deterministic(tmp_path: Path) -> None:
    a = rendered_page_path(tmp_path, sha256=_SHA, page=3, dpi=150)
    b = rendered_page_path(tmp_path, sha256=_SHA, page=3, dpi=150)
    assert a == b
    assert a == tmp_path / ".tmp" / "pdf_pages" / _SHA[:16] / "p3_dpi150.png"
    # A different dpi / page / sha keys a different artifact.
    assert rendered_page_path(tmp_path, sha256=_SHA, page=3, dpi=96) != a
    assert rendered_page_path(tmp_path, sha256="0" * 64, page=3, dpi=150) != a


def test_render_page_image_renders_and_caches(tmp_path: Path, deck_pdf: Path) -> None:
    out = render_page_image(tmp_path, pdf_path=deck_pdf, sha256=_SHA, page=2)
    assert out is not None
    assert out.exists()
    assert out == rendered_page_path(tmp_path, sha256=_SHA, page=2, dpi=DEFAULT_PDF_RENDER_DPI)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # Idempotent: the second call is a cache hit, not a re-render.
    first_mtime = out.stat().st_mtime_ns
    again = render_page_image(tmp_path, pdf_path=deck_pdf, sha256=_SHA, page=2)
    assert again == out
    assert out.stat().st_mtime_ns == first_mtime


def test_render_page_image_degrades_to_none(tmp_path: Path, deck_pdf: Path) -> None:
    assert render_page_image(tmp_path, pdf_path=deck_pdf, sha256=_SHA, page=99) is None
    assert render_page_image(tmp_path, pdf_path=deck_pdf, sha256=_SHA, page=0) is None
    missing = tmp_path / "nope.pdf"
    assert render_page_image(tmp_path, pdf_path=missing, sha256=_SHA, page=1) is None
    # A non-PDF byte blob degrades, never raises.
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(b"not a pdf at all")
    assert render_page_image(tmp_path, pdf_path=garbage, sha256="a" * 64, page=1) is None


def test_page_count_and_dimensions(deck_pdf: Path, tmp_path: Path) -> None:
    assert page_count(deck_pdf) == 2
    dims = page_dimensions(deck_pdf, 1)
    assert dims is not None
    assert dims[0] > 0 and dims[1] > 0
    assert page_dimensions(deck_pdf, 99) is None
    assert page_count(tmp_path / "nope.pdf") is None


def test_find_page_and_bbox_for_quote(deck_pdf: Path) -> None:
    texts = extract_page_texts(deck_pdf)
    assert texts is not None and len(texts) == 2
    hit = find_page_for_quote(deck_pdf, _QUOTE)
    assert hit is not None
    page, bbox = hit
    assert page == 2
    assert bbox is not None
    x0, y0, x1, y1 = bbox
    assert x1 > x0 and y1 > y0
    # The whitespace/case-normalized match still attributes the page.
    fuzzy = find_page_for_quote(deck_pdf, "  revenue   was $1.2 BILLION ")
    assert fuzzy is not None and fuzzy[0] == 2
    # A quote that isn't in the document stays unlocated — never fabricated.
    assert find_page_for_quote(deck_pdf, "this text appears nowhere at all") is None
    assert find_quote_bbox(deck_pdf, 2, "NIM of 17.8%") is not None
    assert find_quote_bbox(deck_pdf, 1, "NIM of 17.8%") is None  # wrong page


def test_pdf_quote_locator_builds_v2_pdf_slide(deck_pdf: Path) -> None:
    locate = PdfQuoteLocator(deck_pdf)
    loc = locate(_QUOTE)
    assert loc is not None
    assert loc.locator_version == 2
    assert loc.kind == LocatorKind.PDF_SLIDE
    assert loc.pdf_page == 2
    assert loc.pdf_bbox is not None
    assert loc.verbatim_snippet == _QUOTE
    assert locate(None) is None
    assert locate("   ") is None
    assert locate("no such text in the deck") is None


def test_pdf_quote_locator_degrades_on_unreadable_pdf(tmp_path: Path) -> None:
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(b"%PDF-1.4\n% not really parseable")
    assert PdfQuoteLocator(garbage)(_QUOTE) is None


# ----------------------------------------------------------------------------
# viewer + routes + peek
# ----------------------------------------------------------------------------

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP,
    source_url TEXT,
    accession_number TEXT,
    filing_date TEXT
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL,
    source_doc_id INTEGER,
    locator TEXT,
    confidence REAL,
    extracted_by TEXT
);
"""


def _seed(repo: Path) -> Path:
    """A repo with the deck PDF registered as doc #1 and two kpi_facts rows:
    #1 carries a full v2 pdf_slide locator, #2 a bare v1 {"pdf_page": 2}."""
    _make_deck_pdf(repo / "ir_documents" / "NU" / "2026-03-31" / "deck.pdf")
    db = repo / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, doc_type, file_path, sha256, fetched_at) "
        "VALUES (1, 'NU', 'ir_presentation', 'ir_documents/NU/2026-03-31/deck.pdf', ?, ?)",
        (_SHA, datetime.now()),
    )
    conn.execute("INSERT INTO kpi_definitions (id, ticker, name) VALUES (1, 'NU', 'Revenue')")
    deck_path = repo / "ir_documents" / "NU" / "2026-03-31" / "deck.pdf"
    bbox = find_quote_bbox(deck_path, 2, _QUOTE)
    assert bbox is not None
    v2 = FactLocator(
        pdf_page=2,
        locator_version=2,
        kind=LocatorKind.PDF_SLIDE,
        pdf_bbox=bbox,
        verbatim_snippet=_QUOTE,
    )
    conn.execute(
        "INSERT INTO kpi_facts (id, ticker, kpi_definition_id, source_doc_id, locator, "
        "confidence, extracted_by) VALUES (1, 'NU', 1, 1, ?, 0.9, 'llm:test')",
        (v2.to_json(),),
    )
    conn.execute(
        "INSERT INTO kpi_facts (id, ticker, kpi_definition_id, source_doc_id, locator, "
        "confidence, extracted_by) VALUES (2, 'NU', 1, 1, '{\"pdf_page\": 2}', 0.9, 'llm:test')"
    )
    conn.commit()
    conn.close()
    return db


def test_render_pdf_page_view_full_and_fragment(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    html = render_pdf_page_view(tmp_path, db, 1, 2, bbox=(70.0, 90.0, 200.0, 110.0), snippet=_QUOTE)
    assert html is not None
    assert 'src="/source/1/page/2.png"' in html
    assert "sv-pdf-hit" in html  # bbox overlay
    assert "page 2 / 2" in html
    assert "p.1</a>" in html  # pager back-link
    assert _QUOTE in html
    assert "k-well" in html  # snippet composes the kit callout

    frag = render_pdf_page_view(tmp_path, db, 1, 2, snippet=_QUOTE, fragment=True)
    assert frag is not None
    assert frag.startswith('<div class="sv-frag">')
    assert "<!doctype" not in frag
    assert "sv-pdf-hit" not in frag  # no bbox passed → no overlay
    assert "cited value is on this page" in frag
    # Fragment pager links absolutely so it doesn't navigate the host shell.
    assert 'href="/source/1?page=1"' in frag


def test_render_pdf_page_view_clamps_page_and_rejects_non_pdf(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    html = render_pdf_page_view(tmp_path, db, 1, 99)
    assert html is not None
    assert "page 2 / 2" in html  # clamped to the last page
    assert render_pdf_page_view(tmp_path, db, 404) is None  # unknown doc


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    _seed(tmp_path)
    return comments_server.create_app(tmp_path).test_client()


def test_source_route_renders_pdf_page_view(client: FlaskClient) -> None:
    resp = client.get("/source/1?page=2&bbox=70,90,200,110")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'src="/source/1/page/2.png"' in body
    assert "sv-pdf-hit" in body


def test_page_image_route_serves_cached_png(client: FlaskClient, tmp_path: Path) -> None:
    resp = client.get("/source/1/page/2.png")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"
    cached = rendered_page_path(tmp_path, sha256=_SHA, page=2, dpi=DEFAULT_PDF_RENDER_DPI)
    assert cached.exists()
    assert client.get("/source/1/page/99.png").status_code == 404
    assert client.get("/source/404/page/1.png").status_code == 404


def test_pdf_slide_peek_renders_page_with_highlight(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    html = render_fact_provenance_peek(db, tmp_path, "kpi_facts:1")
    assert html is not None
    assert 'src="/source/1/page/2.png"' in html
    assert "sv-pdf-hit" in html
    assert _QUOTE in html
    assert "provenance: legacy" not in html


def test_v1_bare_pdf_page_row_renders_without_data_change(tmp_path: Path) -> None:
    """§5.2: existing bare-pdf_page locators became renderable the moment the
    render capability shipped — no re-extraction, no backfill required."""
    db = _seed(tmp_path)
    html = render_fact_provenance_peek(db, tmp_path, "kpi_facts:2")
    assert html is not None
    assert 'src="/source/1/page/2.png"' in html
    assert "provenance: legacy" not in html


def test_pdf_slide_peek_degrades_to_legacy_floor_when_pdf_missing(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    (tmp_path / "ir_documents" / "NU" / "2026-03-31" / "deck.pdf").unlink()
    html = render_fact_provenance_peek(db, tmp_path, "kpi_facts:1")
    assert html is not None
    assert "provenance: legacy" in html  # never a dead end
