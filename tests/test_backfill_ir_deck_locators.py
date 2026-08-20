"""Tests for the Phase B IR-deck locator retrofit
(execution/backfill_ir_deck_locators.py) and the --apply manifest locator
upgrade in execution/extract_kpis_from_ir.py."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

fitz = pytest.importorskip("fitz", reason="PyMuPDF (fitz) not installed")

import backfill_ir_deck_locators as bfl  # noqa: E402
import extract_kpis_from_ir as ekfi  # noqa: E402

from models.facts import (  # noqa: E402
    Currency,
    FactLocator,
    FiscalPeriodType,
    LegacyEscapeHatch,
    LocatorKind,
    Unit,
)
from pipeline.kpi_persistence import KpiExtractionManifest, KpiValue  # noqa: E402

_QUOTE = "Revenue was $1.2 billion"

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP,
    fetch_status TEXT,
    raw_bytes_size INTEGER DEFAULT 0
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
    source_excerpt TEXT
);
"""


def _make_pdf(path: Path) -> None:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Cover")
    page2 = doc.new_page()
    page2.insert_text((72, 100), _QUOTE)
    page2.insert_text((72, 140), "NIM of 17.8%")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


def _seed(repo: Path) -> sqlite3.Connection:
    """Doc #1 = a real deck PDF; four kpi_facts rows covering the retrofit
    cases: NULL+excerpt-found, v1 pdf_page, NULL+no-excerpt, NULL+not-found."""
    _make_pdf(repo / "ir_documents" / "NU" / "2026-03-31" / "deck.pdf")
    db = repo / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256) "
        "VALUES (1, 'NU', 'ir_doc', 'ir_presentation', "
        "'ir_documents/NU/2026-03-31/deck.pdf', 'f00d')"
    )
    conn.execute("INSERT INTO kpi_definitions (id, ticker, name) VALUES (1, 'NU', 'Revenue')")
    rows = [
        (1, None, _QUOTE),  # NULL locator, excerpt IS in the PDF (page 2)
        (2, '{"pdf_page": 2}', "NIM of 17.8%"),  # v1 bare page + excerpt
        (3, None, None),  # nothing to locate -> stays legacy
        (4, None, "text that is not in the deck"),  # unlocatable -> stays legacy
    ]
    for fact_id, locator, excerpt in rows:
        conn.execute(
            "INSERT INTO kpi_facts (id, ticker, kpi_definition_id, source_doc_id, "
            "locator, source_excerpt) VALUES (?, 'NU', 1, 1, ?, ?)",
            (fact_id, locator, excerpt),
        )
    conn.commit()
    return conn


def _locator(conn: sqlite3.Connection, fact_id: int) -> FactLocator | None:
    raw = conn.execute("SELECT locator FROM kpi_facts WHERE id = ?", (fact_id,)).fetchone()[0]
    return FactLocator.from_json(raw) if raw else None


def test_backfill_enriches_and_leaves_honest_legacy(tmp_path: Path) -> None:
    conn = _seed(tmp_path)
    result = bfl.backfill_ticker(conn, tmp_path, "NU")
    assert result.examined == 4
    assert result.backfilled_from_excerpt == 1
    assert result.upgraded_v1_to_v2 == 1
    assert result.bbox_added == 2
    assert result.legacy_no_excerpt == 1
    assert result.legacy_excerpt_not_found == 1

    # NULL + findable excerpt -> full v2 with page + bbox.
    loc1 = _locator(conn, 1)
    assert loc1 is not None
    assert loc1.kind == LocatorKind.PDF_SLIDE
    assert loc1.pdf_page == 2
    assert loc1.pdf_bbox is not None
    assert loc1.verbatim_snippet == _QUOTE

    # v1 pdf_page -> promoted to v2 on the cited page, bbox derived.
    loc2 = _locator(conn, 2)
    assert loc2 is not None
    assert loc2.kind == LocatorKind.PDF_SLIDE
    assert loc2.pdf_page == 2
    assert loc2.pdf_bbox is not None

    # No excerpt / unlocatable excerpt stay legacy — never fabricated.
    assert _locator(conn, 3) is None
    assert _locator(conn, 4) is None
    conn.close()


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    conn = _seed(tmp_path)
    bfl.backfill_ticker(conn, tmp_path, "NU")
    before = {int(r["id"]): r["locator"] for r in conn.execute("SELECT id, locator FROM kpi_facts")}
    second = bfl.backfill_ticker(conn, tmp_path, "NU")
    assert second.backfilled_from_excerpt == 0
    assert second.upgraded_v1_to_v2 == 0
    assert second.already_v2 == 2
    after = {int(r["id"]): r["locator"] for r in conn.execute("SELECT id, locator FROM kpi_facts")}
    assert after == before
    conn.close()


def test_backfill_dry_run_writes_nothing(tmp_path: Path) -> None:
    conn = _seed(tmp_path)
    result = bfl.backfill_ticker(conn, tmp_path, "NU", dry_run=True)
    assert result.backfilled_from_excerpt == 1  # counted...
    assert _locator(conn, 1) is None  # ...but not written
    conn.close()


def test_backfill_counts_missing_pdf(tmp_path: Path) -> None:
    conn = _seed(tmp_path)
    (tmp_path / "ir_documents" / "NU" / "2026-03-31" / "deck.pdf").unlink()
    result = bfl.backfill_ticker(conn, tmp_path, "NU")
    # Rows 1, 2 and 4 need the PDF (row 3 short-circuits on no-excerpt before
    # touching the file only when a locator page exists; NULL-locator rows
    # resolve the PDF first). All existing rows keep their original locator.
    assert result.missing_pdf >= 2
    assert _locator(conn, 1) is None
    conn.close()


# ----------------------------------------------------------------------------
# --apply manifest locator upgrade (_upgrade_pdf_locators)
# ----------------------------------------------------------------------------


def _manifest(values: list[KpiValue]) -> KpiExtractionManifest:
    return KpiExtractionManifest(
        ticker="NU",
        period_end=datetime(2026, 3, 31),
        fiscal_period_type=FiscalPeriodType.Q1,
        source_doc_id=1,
        values=values,
    )


def test_upgrade_promotes_v1_pdf_page_with_bbox(tmp_path: Path) -> None:
    conn = _seed(tmp_path)
    manifest = _manifest(
        [
            KpiValue(
                name="Revenue",
                value=Decimal("1200000000"),
                unit=Unit.ACTUAL,
                currency=Currency.USD,
                source_excerpt=_QUOTE,
                locator=FactLocator(pdf_page=2),
            )
        ]
    )
    upgraded = ekfi.upgrade_pdf_locators(conn, manifest, repo_root=tmp_path)
    loc = upgraded.values[0].locator
    assert isinstance(loc, FactLocator)
    assert loc.locator_version == 2
    assert loc.kind == LocatorKind.PDF_SLIDE
    assert loc.pdf_page == 2
    assert loc.pdf_bbox is not None
    assert loc.verbatim_snippet == _QUOTE
    conn.close()


def test_upgrade_requires_excerpt_for_pdf_page(tmp_path: Path) -> None:
    conn = _seed(tmp_path)
    manifest = _manifest(
        [
            KpiValue(
                name="Revenue",
                value=Decimal("1200000000"),
                unit=Unit.ACTUAL,
                currency=Currency.USD,
                locator=FactLocator(pdf_page=2),
            )
        ]
    )
    with pytest.raises(ValueError, match="source_excerpt"):
        ekfi.upgrade_pdf_locators(conn, manifest, repo_root=tmp_path)
    conn.close()


def test_upgrade_leaves_escape_hatch_and_v2_untouched(tmp_path: Path) -> None:
    conn = _seed(tmp_path)
    hatch = KpiValue(
        name="Other",
        value=Decimal("1"),
        unit=Unit.COUNT,
        locator=LegacyEscapeHatch(reason="fixture value not under locator test"),
    )
    already = KpiValue(
        name="NIM",
        value=Decimal("17.8"),
        unit=Unit.PERCENT,
        source_excerpt="NIM of 17.8%",
        locator=FactLocator(
            pdf_page=2,
            locator_version=2,
            kind=LocatorKind.PDF_SLIDE,
            pdf_bbox=(1.0, 2.0, 3.0, 4.0),
            verbatim_snippet="NIM of 17.8%",
        ),
    )
    manifest = _manifest([hatch, already])
    upgraded = ekfi.upgrade_pdf_locators(conn, manifest, repo_root=tmp_path)
    assert upgraded is manifest  # nothing to change -> same object, no churn
    conn.close()
