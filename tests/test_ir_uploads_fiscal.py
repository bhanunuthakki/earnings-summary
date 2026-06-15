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
    # April-30 FYE maps to the generic apr30 calendar (NOT "rubrik" — Rubrik is
    # a January-31 issuer; the old "rubrik" id mislabeled it as April-30).
    assert calendar_id_from_fye("04-30") == "apr30"
    assert calendar_id_from_fye("09-30") == "visa"
    assert calendar_id_from_fye("05-31") == "oracle"
    assert calendar_id_from_fye(None) == "calendar"
    assert calendar_id_from_fye("99-99") == "calendar"


@pytest.mark.parametrize(
    ("fiscal_label", "want_period_end", "want_label"),
    [
        # Rubrik has a January-31 FYE (same as Veeva): FY26 Q1 ends Apr-30-2025.
        # Regression for the +1-quarter shift the old April-30 "rubrik" calendar
        # produced (e.g. FY26 Q1 was mis-stored as 2025-07-31).
        ("First Quarter Fiscal 2026", date(2025, 4, 30), "FY26 Q1"),
        ("Second Quarter Fiscal 2026", date(2025, 7, 31), "FY26 Q2"),
        ("Third Quarter Fiscal 2026", date(2025, 10, 31), "FY26 Q3"),
        ("Fourth Quarter Fiscal 2026", date(2026, 1, 31), "FY26 Q4"),
        ("First Quarter Fiscal 2025", date(2024, 4, 30), "FY25 Q1"),
    ],
)
def test_classify_rbrk_uses_january_fye(
    tmp_path: Path, fiscal_label: str, want_period_end: date, want_label: str
) -> None:
    p = tmp_path / "rbrk.xlsx"
    _write_xlsx(p, "Rubrik", f"{fiscal_label} Supplemental Data")
    res = classify_ir_file(p)
    assert isinstance(res, CategorizationResult)
    assert res.ticker == "RBRK"
    assert res.period_end == want_period_end
    assert res.period_label == want_label


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
    ("issuer", "phrase", "want_period_end"),
    [
        # SEC covers carry an EXPLICIT period-end date — it IS the period end.
        # For a non-December FYE issuer the month must map to the issuer's FISCAL
        # quarter, not the calendar quarter (the bug that mis-shifted RBRK 10-Qs:
        # "ended October 31, 2025" was stored as 2025-04-30).
        ("Rubrik", "For the quarterly period ended October 31, 2025", date(2025, 10, 31)),
        ("Rubrik", "For the quarterly period ended April 30, 2024", date(2024, 4, 30)),
        ("Veeva Systems", "For the quarterly period ended July 31, 2025", date(2025, 7, 31)),
        ("Visa Inc.", "For the fiscal year ended: September 30, 2025", date(2025, 9, 30)),
        # Calendar issuer is unchanged — the explicit date round-trips identically.
        ("Amazon.com", "For the quarterly period ended September 30, 2025", date(2025, 9, 30)),
    ],
)
def test_sec_cover_explicit_date_uses_fiscal_quarter(
    tmp_path: Path, issuer: str, phrase: str, want_period_end: date
) -> None:
    p = tmp_path / "sec.xlsx"
    _write_xlsx(p, issuer, phrase)
    res = classify_ir_file(p)
    assert isinstance(res, CategorizationResult)
    assert res.period_end == want_period_end


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
