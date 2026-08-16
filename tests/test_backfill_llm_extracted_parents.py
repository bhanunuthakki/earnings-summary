"""Tests for execution/backfill_llm_extracted_parents.py — dry-run reporting,
--apply writes, idempotence, and the unresolved-row report.

Uses the same by-file-path import pattern as test_backfill_earnings_surprises.py
(execution/ scripts aren't on the package path).
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
    src = PROJECT_ROOT / "execution" / "backfill_llm_extracted_parents.py"
    spec = importlib.util.spec_from_file_location("backfill_llm_extracted_parents", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_llm_extracted_parents"] = mod
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
        # Resolvable: unique transcript_audio candidate.
        (
            "NU",
            "transcript_audio",
            "earnings_call_transcript",
            "2023-03-31 00:00:00",
            "transcripts/processed/NU_Q1_2023.txt",
            "sha-transcript",
            "2026-05-19 01:45:16",
            "ok",
            1,
            None,
        ),
        (
            "NU",
            "llm_extracted",
            "llm_summary",
            "2023-03-31 00:00:00",
            ".tmp/NU_Q1_2023_summary.txt",
            "sha-orphan-1",
            "2026-06-17 06:00:00",
            "ok",
            1,
            None,
        ),
        # Unresolvable: no candidate parent on file.
        (
            "AMAT",
            "llm_extracted",
            "llm_summary",
            "2025-12-31 00:00:00",
            ".tmp/AMAT_Q4_2025_summary.txt",
            "sha-orphan-2",
            "2026-06-17 06:00:00",
            "ok",
            1,
            None,
        ),
        # Already linked — must be left untouched and never appear in the scan.
        (
            "MELI",
            "ir_doc",
            "ir_press_release",
            "2024-06-30 00:00:00",
            "x",
            "sha-press",
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
    # A pre-linked llm_extracted row — must be excluded from the scan entirely.
    press_id = conn.execute("SELECT id FROM documents WHERE sha256 = 'sha-press'").fetchone()[0]
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size, parent_document_id) "
        "VALUES ('MELI', 'llm_extracted', 'ir_press_release_synthesized', '2024-06-30 00:00:00', "
        "'x', 'sha-orphan-linked', '2026-05-01 00:00:00', 'ok', 1, ?)",
        (press_id,),
    )
    conn.commit()
    conn.close()


def test_dry_run_reports_without_writing(tmp_path: Path, backfill_mod: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    before = db_path.read_bytes()

    result = backfill_mod.backfill(db_path, dry_run=True, log=False)

    assert result.scanned == 2  # the two NULL-parent llm_extracted rows only
    assert result.resolved == 1
    assert result.unresolved == 1
    assert result.unresolved_rows == [
        (
            _doc_id(db_path, "sha-orphan-2"),
            "AMAT",
            "llm_summary",
            "2025-12-31",
        )
    ]
    assert db_path.read_bytes() == before

    # Dry run must not write.
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT parent_document_id FROM documents WHERE sha256 = 'sha-orphan-1'"
    ).fetchone()
    assert row[0] is None
    conn.close()


def test_apply_writes_resolved_rows_only(tmp_path: Path, backfill_mod: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    transcript_id = _doc_id(db_path, "sha-transcript")

    result = backfill_mod.backfill(db_path, dry_run=False, log=False)
    assert result.resolved == 1
    assert result.unresolved == 1

    conn = sqlite3.connect(db_path)
    resolved_row = conn.execute(
        "SELECT parent_document_id FROM documents WHERE sha256 = 'sha-orphan-1'"
    ).fetchone()
    assert resolved_row[0] == transcript_id

    unresolved_row = conn.execute(
        "SELECT parent_document_id FROM documents WHERE sha256 = 'sha-orphan-2'"
    ).fetchone()
    assert unresolved_row[0] is None

    # The pre-linked row is untouched (still points at its original parent).
    linked_row = conn.execute(
        "SELECT parent_document_id FROM documents WHERE sha256 = 'sha-orphan-linked'"
    ).fetchone()
    assert linked_row[0] is not None
    conn.close()


def test_apply_is_idempotent(tmp_path: Path, backfill_mod: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)

    backfill_mod.backfill(db_path, dry_run=False, log=False)
    second = backfill_mod.backfill(db_path, dry_run=False, log=False)

    # Second pass: the resolved row is no longer an orphan (already linked), so
    # only the genuinely unresolvable row remains in scope.
    assert second.scanned == 1
    assert second.resolved == 0
    assert second.unresolved == 1


def test_ticker_filter_scopes_the_scan(tmp_path: Path, backfill_mod: Any) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)

    result = backfill_mod.backfill(db_path, only_ticker="amat", dry_run=True, log=False)
    assert result.scanned == 1
    assert result.unresolved == 1


def test_veev_orphan_without_exact_transcript_remains_unresolved(
    tmp_path: Path, backfill_mod: Any
) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size, parent_document_id) "
        "VALUES ('VEEV', 'llm_extracted', 'llm_summary', '2026-04-30', "
        "'.tmp/VEEV_Q1_2026_summary.txt', 'veev-orphan', '2026-05-01', 'ok', 1, NULL)"
    )
    conn.commit()
    conn.close()

    result = backfill_mod.backfill(db_path, only_ticker="VEEV", dry_run=False, log=False)
    assert result.resolved == 0
    assert result.unresolved == 1
    assert _doc_id(db_path, "veev-orphan") in {row[0] for row in result.unresolved_rows}


def test_apply_uses_parent_period_and_retimes_only_dependent_facts(
    tmp_path: Path, backfill_mod: Any
) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    conn = sqlite3.connect(db_path)
    parent_id = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size, parent_document_id) "
        "VALUES ('RBRK', 'transcript_audio', 'earnings_call_transcript', '2025-04-30', "
        "'transcripts/processed/RBRK_Q1_2026.txt', 'rbrk-parent', '2025-05-01', 'ok', 1, NULL)"
    ).lastrowid
    child_id = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size, parent_document_id) "
        "VALUES ('RBRK', 'llm_extracted', 'llm_summary', '2026-04-30', "
        "'.tmp/RBRK_Q1_2026_summary.txt', 'rbrk-summary', '2025-05-02', 'ok', 1, NULL)"
    ).lastrowid
    assert parent_id is not None and child_id is not None
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, value, "
        "unit, source_doc_id) VALUES ('RBRK', '2026-04-30', 'Q1', 1, 10, 'actual', ?)",
        (child_id,),
    )
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, value, "
        "unit, source_doc_id) VALUES ('RBRK', '2026-04-30', 'Q1', 2, 10, 'actual', ?)",
        (parent_id,),
    )
    conn.commit()
    conn.close()

    dry_result = backfill_mod.backfill(db_path, only_ticker="RBRK", dry_run=True, log=False)
    assert dry_result.resolved == 1
    assert dry_result.retimestamped == 1
    assert dry_result.kpi_facts_retimestamped == 1

    result = backfill_mod.backfill(db_path, only_ticker="RBRK", dry_run=False, log=False)
    assert result.resolved == 1
    assert result.retimestamped == 1
    assert result.kpi_facts_retimestamped == 1

    conn = sqlite3.connect(db_path)
    child = conn.execute(
        "SELECT parent_document_id, period_end FROM documents WHERE id = ?", (child_id,)
    ).fetchone()
    assert child == (parent_id, "2025-04-30 00:00:00")
    dependent = conn.execute(
        "SELECT period_end FROM kpi_facts WHERE source_doc_id = ?", (child_id,)
    ).fetchone()
    assert dependent == ("2025-04-30 00:00:00",)
    unrelated = conn.execute(
        "SELECT period_end FROM kpi_facts WHERE source_doc_id = ?", (parent_id,)
    ).fetchone()
    assert unrelated == ("2026-04-30",)
    conn.close()


def _doc_id(db_path: Path, sha256: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        conn.close()
