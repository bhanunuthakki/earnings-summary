"""Tests for the fiscal-calendar additions in src/ir_uploads.py (PR1 foundation).

The headless auto-fetch path attributes IR-document periods via ir_uploads'
fiscal-calendar math. Visa (Sep-30 FYE) and Oracle (May-31 FYE) were added for
the evaluation list. These exercise the math + registration through the PUBLIC
``classify_ir_file`` surface (a synthetic 1-cell xlsx, like test_ir_pipeline.py),
so no private symbols are touched.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import openpyxl
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_uploads import (  # noqa: E402
    CategorizationResult,
    calendar_id_from_fye,
    classify_ir_file,
)
from models.documents import DocType  # noqa: E402


def _write_xlsx(path: Path, *cells: str) -> None:
    """One value per row in column A — enough for the content fingerprint."""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    for i, val in enumerate(cells, start=1):
        ws.cell(i, 1, val)
    wb.save(str(path))


def test_calendar_id_from_fye_known_and_default() -> None:
    assert calendar_id_from_fye("12-31") == "calendar"
    assert calendar_id_from_fye("01-31") == "veeva"
    assert calendar_id_from_fye("04-30") == "rubrik"
    assert calendar_id_from_fye("09-30") == "visa"
    assert calendar_id_from_fye("05-31") == "oracle"
    assert calendar_id_from_fye(None) == "calendar"
    assert calendar_id_from_fye("99-99") == "calendar"


def test_classify_xlsx_visa_q1_crosses_calendar_year(tmp_path: Path) -> None:
    # Visa FY2025 Q1 ends Dec-31-*2024* — the year-offset is the easy bug.
    p = tmp_path / "v.xlsx"
    _write_xlsx(p, "Visa Inc.", "Q1 FY2025 Supplemental Data")
    res = classify_ir_file(p)
    assert isinstance(res, CategorizationResult)
    assert res.ticker == "V"
    assert res.doc_type == DocType.IR_SUPPLEMENT
    assert res.period_end == date(2024, 12, 31)
    assert res.period_label == "FY25 Q1"


def test_classify_xlsx_oracle_q4(tmp_path: Path) -> None:
    p = tmp_path / "o.xlsx"
    _write_xlsx(p, "Oracle Corporation", "Q4 FY2025 Supplemental")
    res = classify_ir_file(p)
    assert isinstance(res, CategorizationResult)
    assert res.ticker == "ORCL"
    assert res.period_end == date(2025, 5, 31)
    assert res.period_label == "FY25 Q4"


@pytest.mark.parametrize(
    ("issuer", "ticker"),
    [
        ("CoreWeave, Inc.", "CRWV"),
        ("Tempus AI, Inc.", "TEM"),
        ("Nebius Group", "NBIS"),
    ],
)
def test_classify_detects_calendar_eval_names(tmp_path: Path, issuer: str, ticker: str) -> None:
    # Content-only detection (no ticker_hint) proves the registry entry exists;
    # Q3 2025 on a calendar-year name → Sep-30.
    p = tmp_path / "d.xlsx"
    _write_xlsx(p, issuer, "Q3 2025 Supplemental")
    res = classify_ir_file(p)
    assert isinstance(res, CategorizationResult)
    assert res.ticker == ticker
    assert res.period_end == date(2025, 9, 30)
    assert res.period_label == "Q3 2025"
