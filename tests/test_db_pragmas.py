"""Tests for src/db.py connection PRAGMAs — WAL durability hardening.

WAL keeps a single writer from blocking concurrent readers, which matters
because the portfolio DB is hit simultaneously by sibling-branch pipelines and
the scheduled cron jobs. Regression guard: every get_connection() must come up
in WAL + synchronous=NORMAL.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import db


def _point_db_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "portfolio.db"
    monkeypatch.setattr(db, "DB_PATH", str(target))
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))


def test_get_connection_enables_wal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_db_at(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_get_connection_sets_synchronous_normal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_db_at(monkeypatch, tmp_path)
    conn = db.get_connection()
    try:
        # PRAGMA synchronous: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA.
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        assert sync == 1
    finally:
        conn.close()


def test_wal_mode_persists_across_connections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """journal_mode=WAL is written to the DB header by the first connection;
    a fresh connection to the same file must still report WAL."""
    _point_db_at(monkeypatch, tmp_path)
    first = db.get_connection()
    first.execute("CREATE TABLE t (x INTEGER)")
    first.commit()
    first.close()

    second = db.get_connection()
    try:
        mode = second.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        second.close()
