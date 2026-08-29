"""Tests for src/db.py connection PRAGMAs — WAL durability hardening.

WAL keeps a single writer from blocking concurrent readers, which matters
because the portfolio DB is hit simultaneously by sibling-branch pipelines and
the scheduled cron jobs. Regression guard: every get_connection() must come up
in WAL + synchronous=NORMAL.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import db
import sqlite_runtime
from pipeline.queries import open_db
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _point_db_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "portfolio.db"
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE tracked_companies (id INTEGER PRIMARY KEY)")
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


def test_get_connection_refuses_missing_or_empty_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing Mac checkout DB must not be recreated as an empty authority."""
    target = tmp_path / "data" / "portfolio.db"
    monkeypatch.setattr(db, "DB_PATH", str(target))
    monkeypatch.setattr(db, "DATA_DIR", str(target.parent))

    with pytest.raises(FileNotFoundError, match="does not exist"):
        db.get_connection()
    assert not target.exists()

    target.parent.mkdir()
    with sqlite3.connect(target):
        pass
    with pytest.raises(RuntimeError, match="uninitialized"):
        db.get_connection()


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac checkout authority guard")
def test_get_connection_rejects_exact_mac_checkout_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The checkout guard cannot be bypassed by mutating PROJECT_ROOT."""
    forbidden = PROJECT_ROOT / "data" / "portfolio.db"
    monkeypatch.setattr(db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(forbidden))
    monkeypatch.setattr(db, "DATA_DIR", str(forbidden.parent))

    with pytest.raises(RuntimeError, match="Mac checkout database is prohibited"):
        db.get_connection()
    with pytest.raises(RuntimeError, match="Mac checkout database is prohibited"):
        db.init_db()
    assert not forbidden.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac checkout authority guard")
def test_mac_checkout_case_alias_is_rejected_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case-folded aliases cannot bypass any direct SQLite entry point."""
    forbidden = PROJECT_ROOT / "data" / "portfolio.db"
    alternate_parts = list(forbidden.parts)
    assert alternate_parts[1] == "Applications"
    alternate_parts[1] = "applications"
    alternate = Path(*alternate_parts)
    assert alternate != forbidden

    monkeypatch.setattr(db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(alternate))
    monkeypatch.setattr(db, "DATA_DIR", str(alternate.parent))

    with pytest.raises(RuntimeError, match="Mac checkout database is prohibited"):
        db.get_connection()
    with pytest.raises(RuntimeError, match="Mac checkout database is prohibited"):
        db.init_db()
    with pytest.raises(RuntimeError, match="Mac checkout database is prohibited"):
        connect_sqlite(alternate, role=SQLiteConnectionRole.WRITER, schema_preflight=False)

    assert not forbidden.exists()
    assert not alternate.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac checkout authority guard")
def test_mac_checkout_symlink_alias_is_rejected(tmp_path: Path) -> None:
    """A symlinked checkout path still identifies the forbidden database."""
    checkout_alias = tmp_path / "checkout-alias"
    checkout_alias.symlink_to(PROJECT_ROOT, target_is_directory=True)
    forbidden_alias = checkout_alias / "data" / "portfolio.db"

    with pytest.raises(RuntimeError, match="Mac checkout database is prohibited"):
        connect_sqlite(
            forbidden_alias,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=False,
        )
    assert not forbidden_alias.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac checkout authority guard")
def test_mac_guard_does_not_conflate_case_distinct_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A case-sensitive volume's distinct parent is not the checkout parent."""
    canonical_parent = tmp_path / "Canonical" / "data"
    candidate_parent = tmp_path / "canonical" / "data"
    canonical_parent.mkdir(parents=True)
    candidate_parent.mkdir(parents=True, exist_ok=True)
    try:
        same_parent = candidate_parent.samefile(canonical_parent)
    except OSError:
        pytest.skip("filesystem cannot compare temporary parent identity")
    if same_parent:
        pytest.skip("temporary volume is case-insensitive")

    monkeypatch.setattr(
        sqlite_runtime,
        "_FORBIDDEN_MAC_CHECKOUT_DB",
        canonical_parent / "portfolio.db",
    )
    sqlite_runtime.reject_forbidden_mac_checkout_database(candidate_parent / "PORTFOLIO.DB")


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac checkout authority guard")
def test_mac_guard_allows_explicit_temp_symlink_and_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Disposable temp databases, including symlinked ones, remain valid."""
    target = tmp_path / "portfolio.db"
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE tracked_companies (id INTEGER PRIMARY KEY)")
    alias = tmp_path / "portfolio-alias.db"
    alias.symlink_to(target)

    monkeypatch.setattr(db, "DB_PATH", str(alias))
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    conn = db.get_connection()
    conn.close()

    memory = connect_sqlite(":memory:", role=SQLiteConnectionRole.WRITER, schema_preflight=False)
    memory.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac checkout authority guard")
@pytest.mark.parametrize(
    "role",
    [
        SQLiteConnectionRole.READ_ONLY,
        SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
        SQLiteConnectionRole.WRITER,
        SQLiteConnectionRole.SNAPSHOT_DESTINATION,
    ],
)
def test_shared_sqlite_runtime_rejects_exact_mac_checkout_path(
    role: SQLiteConnectionRole,
) -> None:
    forbidden = PROJECT_ROOT / "data" / "portfolio.db"

    with pytest.raises(RuntimeError, match="Mac checkout database is prohibited"):
        connect_sqlite(forbidden, role=role, schema_preflight=False)
    assert not forbidden.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac checkout authority guard")
def test_pipeline_open_db_cannot_bypass_mac_checkout_guard() -> None:
    forbidden = PROJECT_ROOT / "data" / "portfolio.db"

    with pytest.raises(RuntimeError, match="Mac checkout database is prohibited"):
        open_db(forbidden)
    assert not forbidden.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac checkout authority guard")
def test_unqualified_alembic_refuses_to_create_mac_checkout_database() -> None:
    """Raw Alembic must not bypass the same no-local-authority invariant."""
    forbidden = PROJECT_ROOT / "data" / "portfolio.db"
    assert not forbidden.exists()

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "refusing the unqualified Mac checkout database" in result.stderr
    assert not forbidden.exists()
