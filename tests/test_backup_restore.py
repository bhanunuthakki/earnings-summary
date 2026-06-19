"""Tested backup-restore drill (sre-3, 2026-06-18 hardening refresh).

A backup you have never restored is not a backup. Exercises cron/restore_db.py
directly (round-trip, refuse-overwrite, force, corrupt-snapshot rejection) and
the full backup_db -> restore_db path end-to-end.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "cron"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import backup_db  # noqa: E402
import restore_db  # noqa: E402


def _make_db(path: Path, value: str) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (k TEXT, v TEXT)")
        conn.execute("INSERT INTO t VALUES ('key', ?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _read_value(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT v FROM t WHERE k = 'key'").fetchone()
        return str(row[0])
    finally:
        conn.close()


def _gzip_file(src: Path, dst: Path) -> None:
    with open(src, "rb") as raw, gzip.open(dst, "wb") as gz:
        shutil.copyfileobj(raw, gz)


def test_restore_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_db(src, "hello")
    snap = tmp_path / "portfolio.db.20260101_000000.gz"
    _gzip_file(src, snap)
    target = tmp_path / "restored.db"
    restore_db.restore_snapshot(snap, target, force=False)
    assert target.exists()
    assert restore_db.integrity_ok(target)
    assert _read_value(target) == "hello"


def test_restore_refuses_overwrite_without_force(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_db(src, "x")
    snap = tmp_path / "s.gz"
    _gzip_file(src, snap)
    target = tmp_path / "exists.db"
    target.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        restore_db.restore_snapshot(snap, target, force=False)
    assert target.read_bytes() == b"keep"


def test_restore_force_overwrites(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_db(src, "new")
    snap = tmp_path / "s.gz"
    _gzip_file(src, snap)
    target = tmp_path / "exists.db"
    target.write_bytes(b"old")
    restore_db.restore_snapshot(snap, target, force=True)
    assert _read_value(target) == "new"


def test_restore_rejects_corrupt_snapshot(tmp_path: Path) -> None:
    snap = tmp_path / "bad.gz"
    with gzip.open(snap, "wb") as gz:
        gz.write(b"this is not a sqlite database")
    target = tmp_path / "out.db"
    with pytest.raises(RuntimeError):
        restore_db.restore_snapshot(snap, target, force=False)
    assert not target.exists()  # the corrupt restore is discarded, target untouched


def test_backup_then_restore_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = tmp_path / "live.db"
    _make_db(live, "e2e")
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup_db, "SRC_DB", live)
    monkeypatch.setenv("ES_DB_BACKUP_DIR", str(backup_dir))
    assert backup_db.main() == 0

    snaps = restore_db.list_snapshots(backup_dir)
    assert snaps, "backup_db wrote no snapshot"
    target = tmp_path / "recovered.db"
    rc = restore_db.main(["--latest", "--backup-dir", str(backup_dir), "--to", str(target)])
    assert rc == 0
    assert _read_value(target) == "e2e"
