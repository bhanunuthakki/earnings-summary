# pyright: reportPrivateUsage=false
"""Tests for execution/gc_restore.py — classify-then-apply archive restore.

Fixture DBs deliberately omit alembic_version so schema_compat's write
preflight no-ops (its documented fixture contract)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "execution"))

import db_gc  # noqa: E402
import gc_restore  # noqa: E402


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT,
            fiscal_period_type TEXT, line_item TEXT, value NUMERIC,
            extracted_by TEXT, supersedes_id INTEGER REFERENCES financial_facts(id)
        );
        CREATE INDEX ix_0270_financial_facts_supersedes_id
            ON financial_facts(supersedes_id);
        CREATE TABLE ingestion_runs (run_id TEXT, started_at TEXT);
        """
    )
    conn.commit()
    conn.close()
    return path


def _attached(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    db_gc.attach_archive(conn, path.parent / "archive" / db_gc.ARCHIVE_NAME)
    return conn


def _seed_fact(conn: sqlite3.Connection, fid: int, ticker: str, value: int) -> None:
    conn.execute(
        "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
        " line_item, value, extracted_by) VALUES (?, ?, '2020-12-31', 'Q4', 'revenue', ?, 'fmp')",
        (fid, ticker, value),
    )


def _archive_facts(conn: sqlite3.Connection, run_at: str, ids: list[int]) -> int:
    db_gc._reset_doomed(conn)
    conn.executemany("INSERT INTO _gc_doomed (id) VALUES (?)", [(i,) for i in ids])
    return db_gc._archive_doomed(conn, table="financial_facts", run_at=run_at, policy="facts-depth")


def _pruned_db(path: Path) -> None:
    """Archive facts 1-3 under run r1 and delete them from main (a normal prune)."""
    conn = _attached(path)
    for fid in (1, 2, 3):
        _seed_fact(conn, fid, "EVAL", fid * 10)
    assert _archive_facts(conn, "r1", [1, 2, 3]) == 3
    conn.execute("DELETE FROM financial_facts WHERE id IN (1, 2, 3)")
    conn.commit()
    conn.close()


class TestClassify:
    def test_dry_run_classifies_missing_identical_conflict(self, db: Path) -> None:
        _pruned_db(db)
        conn = _attached(db)
        # id 2 came back identical; id 3 came back with a DIFFERENT payload.
        _seed_fact(conn, 2, "EVAL", 20)
        _seed_fact(conn, 3, "OTHER", 999)
        conn.commit()
        conn.close()

        report = gc_restore.run_restore(db, mode="dry-run")
        (t,) = [x for x in report.tables if x.table == "financial_facts"]
        assert (t.candidates, t.restorable, t.identical, t.conflicts) == (3, 1, 1, 1)
        assert t.conflict_key_samples == ["3"]
        # Dry run wrote nothing.
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 2

    def test_legacy_archive_aborts_dry_run_with_instruction(self, db: Path) -> None:
        conn = _attached(db)
        _seed_fact(conn, 1, "EVAL", 10)
        conn.execute('CREATE TABLE gcarc."financial_facts" AS SELECT * FROM main."financial_facts"')
        conn.commit()
        conn.close()
        with pytest.raises(db_gc.GcAbortedError, match="predates run-keying"):
            gc_restore.run_restore(db, mode="dry-run")


class TestApply:
    def test_apply_restores_missing_skips_identical_reports_conflict(self, db: Path) -> None:
        _pruned_db(db)
        conn = _attached(db)
        _seed_fact(conn, 2, "EVAL", 20)  # identical
        _seed_fact(conn, 3, "OTHER", 999)  # conflict
        conn.commit()
        conn.close()

        report = gc_restore.run_restore(db, mode="apply", lock_timeout_s=0.0)
        (t,) = [x for x in report.tables if x.table == "financial_facts"]
        assert (t.restorable, t.restored, t.identical, t.conflicts) == (1, 1, 1, 1)

        conn = _attached(db)
        # Row 1 back verbatim; conflict row 3 untouched.
        assert conn.execute(
            "SELECT ticker, value FROM financial_facts WHERE id = 1"
        ).fetchone() == ("EVAL", 10)
        assert conn.execute(
            "SELECT ticker, value FROM financial_facts WHERE id = 3"
        ).fetchone() == ("OTHER", 999)
        # Audit trail in the manifest, under the restore policy.
        assert conn.execute(
            "SELECT source_table, rows_archived FROM gc_manifest WHERE policy = 'restore'"
        ).fetchall() == [("financial_facts", 1)]

    def test_run_filter_restores_exactly_that_run(self, db: Path) -> None:
        conn = _attached(db)
        _seed_fact(conn, 1, "A", 1)
        assert _archive_facts(conn, "r1", [1]) == 1
        conn.execute("DELETE FROM financial_facts WHERE id = 1")
        _seed_fact(conn, 2, "B", 2)
        assert _archive_facts(conn, "r2", [2]) == 1
        conn.execute("DELETE FROM financial_facts WHERE id = 2")
        conn.commit()
        conn.close()

        report = gc_restore.run_restore(db, mode="apply", run_filter="r1", lock_timeout_s=0.0)
        (t,) = [x for x in report.tables if x.table == "financial_facts"]
        assert (t.candidates, t.restored) == (1, 1)
        conn = sqlite3.connect(db)
        assert [r[0] for r in conn.execute("SELECT id FROM financial_facts ORDER BY id")] == [1]

    def test_supersedes_chain_restores_under_fk_enforcement(self, db: Path) -> None:
        conn = _attached(db)
        _seed_fact(conn, 10, "EVAL", 1)
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, fiscal_period_type,"
            " line_item, value, extracted_by, supersedes_id) VALUES"
            " (11, 'EVAL', '2020-12-31', 'Q4', 'revenue', 2, 'sec_xbrl', 10)"
        )
        assert _archive_facts(conn, "r1", [10, 11]) == 2
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM financial_facts WHERE id IN (10, 11)")
        conn.commit()
        conn.close()

        report = gc_restore.run_restore(db, mode="apply", lock_timeout_s=0.0)
        (t,) = [x for x in report.tables if x.table == "financial_facts"]
        assert t.restored == 2
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys = ON")
        assert conn.execute("PRAGMA foreign_key_check(financial_facts)").fetchall() == []
        assert conn.execute(
            "SELECT supersedes_id FROM financial_facts WHERE id = 11"
        ).fetchone() == (10,)

    def test_apply_upgrades_legacy_archive_in_place(self, db: Path) -> None:
        conn = _attached(db)
        _seed_fact(conn, 1, "EVAL", 10)
        conn.execute('CREATE TABLE gcarc."financial_facts" AS SELECT * FROM main."financial_facts"')
        conn.execute("DELETE FROM financial_facts WHERE id = 1")
        conn.commit()
        conn.close()

        report = gc_restore.run_restore(db, mode="apply", lock_timeout_s=0.0)
        (t,) = [x for x in report.tables if x.table == "financial_facts"]
        assert t.restored == 1
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT ticker FROM financial_facts WHERE id = 1").fetchone() == (
            "EVAL",
        )


class TestDrill:
    def test_drill_verifies_without_touching_main(self, db: Path) -> None:
        _pruned_db(db)
        before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0]

        report = gc_restore.run_restore(db, mode="drill")
        (t,) = [x for x in report.tables if x.table == "financial_facts"]
        assert t.drill_verified is True
        assert t.candidates == 3

        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == before

    def test_cli_exit_codes(self, db: Path) -> None:
        _pruned_db(db)
        conn = _attached(db)
        _seed_fact(conn, 3, "OTHER", 999)  # conflict
        conn.commit()
        conn.close()
        # Dry-run with a conflict present exits 4 — action needed.
        assert gc_restore.main(["--db", str(db), "--ignore-protected-window"]) == 4
        # Drill on the same state verifies and exits 0 (no conflicts consulted).
        conn = _attached(db)
        conn.execute("DELETE FROM financial_facts WHERE id = 3")
        conn.commit()
        conn.close()
        assert gc_restore.main(["--db", str(db), "--drill", "--ignore-protected-window"]) == 0
