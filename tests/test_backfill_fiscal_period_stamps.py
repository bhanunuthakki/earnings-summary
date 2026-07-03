"""Tests for execution/backfill_fiscal_period_stamps.py — dry-run reporting,
--apply writes to both documents and dependent kpi_facts, idempotence, and
ticker scoping.

Uses the same by-file-path import pattern as
test_backfill_llm_extracted_parents.py (execution/ scripts aren't on the
package path).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    src = PROJECT_ROOT / "execution" / "backfill_fiscal_period_stamps.py"
    spec = importlib.util.spec_from_file_location("backfill_fiscal_period_stamps", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_fiscal_period_stamps"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def backfill_mod() -> Any:
    return _load_module()


def _seed_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT, source_type TEXT, doc_type TEXT, period_end TIMESTAMP,
            file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP, fetch_status TEXT,
            raw_bytes_size INTEGER, parent_document_id INTEGER
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT, period_end TIMESTAMP, fiscal_period_type TEXT,
            kpi_definition_id INTEGER, value NUMERIC, unit TEXT,
            source_doc_id INTEGER
        );
        """
    )
    rows = [
        # AMAT Q4 2025 (Oct FYE): mis-stamped calendar-quarter-end 2025-12-31
        # instead of the true fiscal Q4 end 2025-10-31 — the bug this script fixes.
        (
            "AMAT",
            "llm_extracted",
            "llm_summary",
            "2025-12-31 00:00:00",
            ".tmp/AMAT_Q4_2025_summary.txt",
            "sha-amat-q4",
            "2026-06-17 05:38:12",
            "ok",
            1,
            None,
        ),
        # VEEV Q1 2025 (Jan FYE): already correctly stamped by the existing
        # RBRK/VEEV override — must be left untouched.
        (
            "VEEV",
            "llm_extracted",
            "llm_summary",
            "2025-04-30 00:00:00",
            ".tmp/VEEV_Q1_2025_summary.txt",
            "sha-veev-q1",
            "2026-05-01 00:00:00",
            "ok",
            1,
            None,
        ),
    ]
    conn.executemany(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size, parent_document_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()

    amat_doc_id = conn.execute("SELECT id FROM documents WHERE sha256 = 'sha-amat-q4'").fetchone()[
        0
    ]
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, "
        "kpi_definition_id, value, unit, source_doc_id) "
        "VALUES ('AMAT', '2025-12-31 00:00:00', 'Q4', 4733, 12.5, 'percent', ?)",
        (amat_doc_id,),
    )
    conn.commit()
    conn.close()


def _doc_id(db_path: Path, sha256: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        conn.close()


def test_dry_run_reports_without_writing(tmp_path: Path, backfill_mod: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)

    result = backfill_mod.backfill(db_path, dry_run=True, log=False)

    assert result.scanned == 2
    assert result.corrected == 1  # only the AMAT row is wrong
    assert result.unchanged == 1  # VEEV's existing correct stamp
    assert result.kpi_facts_updated == 1  # reported even in dry-run

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT period_end FROM documents WHERE sha256 = 'sha-amat-q4'").fetchone()
    assert row[0] == "2025-12-31 00:00:00"  # unchanged by dry run
    conn.close()


def test_apply_corrects_document_and_dependent_kpi_facts(tmp_path: Path, backfill_mod: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    amat_doc_id = _doc_id(db_path, "sha-amat-q4")

    result = backfill_mod.backfill(db_path, dry_run=False, log=False)
    assert result.corrected == 1
    assert result.kpi_facts_updated == 1

    conn = sqlite3.connect(db_path)
    doc_row = conn.execute(
        "SELECT period_end FROM documents WHERE id = ?", (amat_doc_id,)
    ).fetchone()
    assert doc_row[0][:10] == "2025-10-31"

    fact_row = conn.execute(
        "SELECT period_end, fiscal_period_type FROM kpi_facts WHERE source_doc_id = ?",
        (amat_doc_id,),
    ).fetchone()
    assert fact_row[0][:10] == "2025-10-31"
    assert fact_row[1] == "Q4"  # fiscal_period_type is untouched — only the date changes

    # VEEV's already-correct row must be untouched.
    veev_row = conn.execute(
        "SELECT period_end FROM documents WHERE sha256 = 'sha-veev-q1'"
    ).fetchone()
    assert veev_row[0] == "2025-04-30 00:00:00"
    conn.close()


def test_apply_is_idempotent(tmp_path: Path, backfill_mod: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)

    backfill_mod.backfill(db_path, dry_run=False, log=False)
    second = backfill_mod.backfill(db_path, dry_run=False, log=False)

    assert second.corrected == 0
    assert second.unchanged == 2


def test_ticker_filter_scopes_the_scan(tmp_path: Path, backfill_mod: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)

    result = backfill_mod.backfill(db_path, only_ticker="amat", dry_run=True, log=False)
    assert result.scanned == 1
    assert result.corrected == 1
