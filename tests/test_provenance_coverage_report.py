"""Tests for execution/provenance_coverage_report.py (section 4.3's
measurable-retrofit-progress CLI)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from provenance_coverage_report import audit_table, format_report  # noqa: E402


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE financial_facts (id INTEGER PRIMARY KEY, ticker TEXT, "
        "extracted_by TEXT, locator TEXT);"
        "CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, ticker TEXT, "
        "extracted_by TEXT, locator TEXT);"
    )
    rows = [
        ("NU", "fmp", None),
        ("NU", "fmp", '{"json_path":"[0].revenue"}'),  # v1-only: no locator_version/kind
        (
            "NU",
            "capture_xbrl_v1",
            '{"kind":"fmp_json_table","locator_version":2,"table_cell":{"row_label":"x"}}',
        ),
        (
            "MELI",
            "capture_xbrl_v1",
            '{"kind":"fmp_json_table","locator_version":2,"table_cell":{"row_label":"y"}}',
        ),
    ]
    conn.executemany(
        "INSERT INTO financial_facts (ticker, extracted_by, locator) VALUES (?, ?, ?)", rows
    )
    conn.commit()
    conn.close()


def test_audit_table_classifies_null_v1_v2(tmp_path: Path) -> None:
    db = tmp_path / "prov.db"
    _seed(db)
    conn = sqlite3.connect(db)
    try:
        cov = audit_table(conn, "financial_facts")
        assert cov is not None
        assert cov.rows == 4
        assert cov.locator_null == 1
        assert cov.v1_only == 1
        assert cov.v2_renderable == 2
        by_name = {e.extracted_by: e for e in cov.by_extractor}
        assert by_name["fmp"].rows == 2
        assert by_name["fmp"].v2_renderable == 0
        assert by_name["capture_xbrl_v1"].rows == 2
        assert by_name["capture_xbrl_v1"].v2_renderable == 2

        empty = audit_table(conn, "kpi_facts")
        assert empty is not None
        assert empty.rows == 0

        assert audit_table(conn, "no_such_table") is None
    finally:
        conn.close()


def test_audit_table_ticker_filter(tmp_path: Path) -> None:
    db = tmp_path / "prov.db"
    _seed(db)
    conn = sqlite3.connect(db)
    try:
        cov = audit_table(conn, "financial_facts", ticker="nu")
        assert cov is not None
        assert cov.rows == 3
        assert cov.v2_renderable == 1
    finally:
        conn.close()


def test_format_report_renders_percentages(tmp_path: Path) -> None:
    db = tmp_path / "prov.db"
    _seed(db)
    conn = sqlite3.connect(db)
    try:
        cov = audit_table(conn, "financial_facts")
        assert cov is not None
        out = format_report([cov])
        assert "financial_facts" in out
        assert "v2 renderable: 2 (50.0%)" in out
        assert "capture_xbrl_v1" in out
    finally:
        conn.close()
