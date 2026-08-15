"""Tests for src/ir_fetch_status.py — the IR auto-fetch status + coverage store.

Coverage is read live from ``documents`` (source_type='ir_doc'); crawl health
from ``ir_fetch_status``. Every reader/writer must tolerate a missing table.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import ir_fetch_status as ifs  # noqa: E402
from pipeline.queries import BRIEFED_LIST_TYPE_VALUES  # noqa: E402


def test_ir_coverage_uses_canonical_briefed_list_types() -> None:
    assert ifs.BRIEFED_LIST_TYPES is BRIEFED_LIST_TYPE_VALUES


def _make_db(db: Path, *, with_status: bool = True, with_docs: bool = True) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, "
        "archived_at TEXT, fiscal_year_end TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies (ticker, name, list_type, archived_at) VALUES (?,?,?,?)",
        [
            ("NU", "Nu Holdings", "portfolio", None),
            ("ORCL", "Oracle", "evaluation", None),
            ("NOW", "ServiceNow", "portfolio", None),
            ("XYZ", "Watch Co", "watchlist", None),  # excluded (not briefed)
            ("OLD", "Archived Co", "portfolio", "2026-01-01"),  # excluded (archived)
        ],
    )
    if with_docs:
        conn.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT, source_type TEXT, "
            "doc_type TEXT, period_end TEXT, fetched_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO documents (ticker, source_type, doc_type, period_end, fetched_at) "
            "VALUES (?,?,?,?,?)",
            [
                ("NU", "ir_doc", "press_release", "2026-03-31", "2026-06-01T10:00:00"),
                ("NU", "ir_doc", "presentation", "2026-03-31", "2026-06-02T10:00:00"),
                ("NU", "fmp", "10-K", "2025-12-31", "2026-05-01T10:00:00"),  # not ir_doc
                ("ORCL", "ir_doc", "transcript", "2025-11-30", "2026-05-20T10:00:00"),
                # NOW has zero ir_doc rows — the manual-pull gap.
            ],
        )
    if with_status:
        conn.execute(
            "CREATE TABLE ir_fetch_status (ticker TEXT PRIMARY KEY, last_attempt_at TEXT, "
            "last_status TEXT, discovered INTEGER, downloaded INTEGER, reason TEXT, updated_at TEXT)"
        )
    conn.commit()
    conn.close()


def test_record_attempt_inserts_then_upserts(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _make_db(db)
    assert ifs.record_attempt(
        db,
        "now",
        status="failed",
        discovered=0,
        downloaded=0,
        reason="403",
        now_iso="2026-06-04T01:00:00",
    )
    st = ifs.load_statuses(db)["NOW"]
    assert st.last_status == "failed"
    assert st.reason == "403"
    assert st.last_attempt_at == "2026-06-04T01:00:00"

    # Second attempt overwrites (PRIMARY KEY upsert), not duplicates.
    ifs.record_attempt(
        db,
        "NOW",
        status="ok",
        discovered=5,
        downloaded=2,
        reason=None,
        now_iso="2026-06-05T01:00:00",
    )
    statuses = ifs.load_statuses(db)
    assert len([t for t in statuses if t == "NOW"]) == 1
    assert statuses["NOW"].last_status == "ok"
    assert statuses["NOW"].downloaded == 2
    assert statuses["NOW"].reason is None


def test_record_attempt_tolerates_missing_table(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _make_db(db, with_status=False)
    # No ir_fetch_status table — best-effort no-op, never raises.
    assert ifs.record_attempt(db, "NU", status="ok", discovered=1, downloaded=1) is False
    assert ifs.load_statuses(db) == {}


def test_record_attempt_tolerates_missing_db(tmp_path: Path) -> None:
    assert (
        ifs.record_attempt(tmp_path / "nope.db", "NU", status="ok", discovered=0, downloaded=0)
        is False
    )


def test_ir_doc_coverage_counts_only_ir_docs(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _make_db(db)
    cov = ifs.ir_doc_coverage(db)
    # NU has 2 ir_doc rows (the fmp 10-K is excluded); latest period + newest fetch.
    assert cov["NU"][0] == 2
    assert cov["NU"][1] == "2026-03-31"
    assert cov["NU"][2] == "2026-06-02T10:00:00"
    assert cov["ORCL"][0] == 1
    assert "NOW" not in cov  # zero ir_doc rows


def test_briefed_roster_excludes_watchlist_and_archived(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _make_db(db)
    roster = ifs.briefed_roster(db)
    tickers = [t for t, _, _ in roster]
    assert tickers == ["NOW", "NU", "ORCL"]  # sorted; XYZ + OLD excluded
    assert ("ORCL", "evaluation", "Oracle") in roster


def test_coverage_rows_gaps_sort_first(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _make_db(db)
    ifs.record_attempt(db, "NOW", status="failed", discovered=0, downloaded=0, reason="HTTP 403")
    rows = ifs.coverage_rows(db, ifs.briefed_roster(db))
    # NOW (no docs) sorts before NU/ORCL (have docs).
    assert rows[0].ticker == "NOW"
    assert rows[0].has_docs is False
    assert rows[0].status is not None
    assert rows[0].status.reason == "HTTP 403"
    nu = next(r for r in rows if r.ticker == "NU")
    assert nu.has_docs is True
    assert nu.doc_count == 2
    assert nu.status is None  # never attempted via the batch in this test


def test_gap_tickers_are_zero_coverage_names(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _make_db(db)
    gaps = ifs.gap_tickers(db, ["NU", "ORCL", "NOW", "missing"])
    assert set(gaps) == {"NOW", "MISSING"}  # NU/ORCL have docs; case-normalized


def test_readers_tolerate_absent_documents_table(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _make_db(db, with_docs=False)
    assert ifs.ir_doc_coverage(db) == {}
    # Every briefed name becomes a gap when there is no documents table yet.
    assert set(ifs.gap_tickers(db, ["NU", "ORCL"])) == {"NU", "ORCL"}
