"""Tests for cron/snapshot_db.py — the consistent SQLite snapshot helper.

The monthly project backup must capture data/portfolio.db as a CONSISTENT copy
even while the daily refresh pipeline is mid-write. A raw file copy of a live
SQLite DB (with a -wal sidecar) can be torn; the online backup API cannot.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cron"))

import snapshot_db


def _make_db(path: Path, rows: int) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO t (id, v) VALUES (?, ?)",
            [(i, f"row-{i}") for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def test_snapshot_copies_all_rows(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    dst = tmp_path / "snap.db"
    _make_db(src, rows=500)

    snapshot_db.snapshot(src, dst)

    assert dst.exists()
    conn = sqlite3.connect(str(dst))
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM t").fetchone()
    finally:
        conn.close()
    assert count == 500


def test_snapshot_is_consistent_under_open_writer(tmp_path: Path) -> None:
    """A live connection with an uncommitted write must not corrupt the snapshot.

    The snapshot should reflect the last committed state (200 rows), not the
    uncommitted insert, and must be a fully valid, openable database.
    """
    src = tmp_path / "src.db"
    dst = tmp_path / "snap.db"
    _make_db(src, rows=200)

    writer = sqlite3.connect(str(src))
    try:
        # Begin an uncommitted write to simulate a mid-pipeline DB.
        writer.execute("INSERT INTO t (id, v) VALUES (9999, 'uncommitted')")
        snapshot_db.snapshot(src, dst)
    finally:
        writer.rollback()
        writer.close()

    conn = sqlite3.connect(str(dst))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        (count,) = conn.execute("SELECT COUNT(*) FROM t").fetchone()
    finally:
        conn.close()
    assert count == 200


def test_snapshot_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        snapshot_db.snapshot(tmp_path / "nope.db", tmp_path / "out.db")


def test_main_cli_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    dst = tmp_path / "snap.db"
    _make_db(src, rows=10)

    rc = snapshot_db.main([str(src), str(dst)])

    assert rc == 0
    assert dst.exists()
