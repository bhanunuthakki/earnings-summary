"""Tests for execution/restore_drill.py — the monthly DB restore drill.

The drill restores the newest backup snapshot to a throwaway temp file and
verifies it (integrity via restore_db + core-table row counts + a soft schema
match). These tests build synthetic .gz snapshots in tmp_path and never touch a
real DB; ingestion_runs bookkeeping is skipped by pointing --db at a path that
does not exist.
"""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

from execution import restore_drill


def _make_snapshot(
    backup_dir: Path,
    *,
    populated: bool = True,
    version: str = "0113_x",
    name: str = "portfolio.db.20260715T090000Z.gz",
) -> Path:
    """Write a gzipped SQLite snapshot restore_db.list_snapshots will discover."""
    raw = backup_dir / "_raw.db"
    conn = sqlite3.connect(str(raw))
    conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
    conn.execute("INSERT INTO alembic_version VALUES (?)", (version,))
    conn.execute("CREATE TABLE tracked_companies (ticker TEXT)")
    conn.execute("CREATE TABLE financial_facts (id INTEGER)")
    if populated:
        conn.execute("INSERT INTO tracked_companies VALUES ('NU')")
        conn.execute("INSERT INTO financial_facts VALUES (1)")
    conn.commit()
    conn.close()
    gz = backup_dir / name
    with open(raw, "rb") as f, gzip.open(gz, "wb") as g:
        g.write(f.read())
    raw.unlink()
    return gz


def test_drill_passes_on_healthy_snapshot(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()
    _make_snapshot(bdir, populated=True)

    ok, summary = restore_drill.run_drill(bdir, tmp_path / "absent.db")

    assert ok is True
    assert summary["status"] == "ok"
    assert summary["row_counts"] == {"tracked_companies": 1, "financial_facts": 1}
    assert summary["schema_match"] is True  # live absent → soft check passes


def test_drill_fails_on_empty_core_tables(tmp_path: Path) -> None:
    """A snapshot that passes integrity_check but has empty core tables (e.g. a
    truncated/half-written backup) must be caught."""
    bdir = tmp_path / "backups"
    bdir.mkdir()
    _make_snapshot(bdir, populated=False)

    ok, summary = restore_drill.run_drill(bdir, tmp_path / "absent.db")

    assert ok is False
    assert summary["status"] == "empty_core_tables"


def test_no_snapshot_reports_status(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()

    ok, summary = restore_drill.run_drill(bdir, tmp_path / "absent.db")

    assert ok is False
    assert summary["status"] == "no_snapshot"


def test_corrupt_snapshot_fails(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "portfolio.db.20260715T090000Z.gz").write_bytes(b"this is not gzip")

    ok, summary = restore_drill.run_drill(bdir, tmp_path / "absent.db")

    assert ok is False
    assert summary["status"] == "restore_failed"


def test_schema_mismatch_warns_but_passes(tmp_path: Path) -> None:
    """A snapshot whose schema differs from the live DB warns (a migration after
    the last backup is legitimate) but does not fail the drill."""
    bdir = tmp_path / "backups"
    bdir.mkdir()
    _make_snapshot(bdir, version="0100_old")

    live = tmp_path / "live.db"
    conn = sqlite3.connect(str(live))
    conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
    conn.execute("INSERT INTO alembic_version VALUES ('0200_new')")
    conn.commit()
    conn.close()

    ok, summary = restore_drill.run_drill(bdir, live)

    assert ok is True
    assert summary["schema_match"] is False
    assert "warning" in summary


def test_temp_restore_is_cleaned_up(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()
    _make_snapshot(bdir)
    before = set(Path(tmp_path).rglob("drill.db"))
    restore_drill.run_drill(bdir, tmp_path / "absent.db")
    after = set(Path(tmp_path).rglob("drill.db"))
    assert before == after == set()  # no leftover temp restore


def test_main_exit_codes(tmp_path: Path) -> None:
    bdir = tmp_path / "backups"
    bdir.mkdir()
    absent = tmp_path / "absent.db"

    # no snapshot → exit 2
    assert restore_drill.main(["--backup-dir", str(bdir), "--db", str(absent)]) == 2

    # healthy snapshot → exit 0
    _make_snapshot(bdir)
    assert restore_drill.main(["--backup-dir", str(bdir), "--db", str(absent)]) == 0
