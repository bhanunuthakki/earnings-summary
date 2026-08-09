from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
import upgrade_database as upgrade_database_module
from upgrade_database import ACTIVE_HEAD, UpgradeDatabaseError, upgrade_database

ROOT = Path(__file__).resolve().parents[1]


def test_upgrade_requires_safe_sqlite_before_touching_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "must-not-exist.db"

    def unsafe_runtime() -> None:
        raise RuntimeError("unsafe SQLite test sentinel")

    monkeypatch.setattr(
        upgrade_database_module,
        "require_safe_sqlite_writer_runtime",
        unsafe_runtime,
    )
    with pytest.raises(RuntimeError, match="unsafe SQLite test sentinel"):
        upgrade_database(db_path, repo_root=ROOT)
    assert not db_path.exists()


def test_upgrade_classifies_database_only_after_lock_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "locked-classification.db"
    db_path.touch()
    lock_held = False

    @contextmanager
    def fake_lock(*_args: object, **_kwargs: object) -> Generator[None]:
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def revisions_only_under_lock(_db_path: Path) -> tuple[str, ...]:
        assert lock_held
        return (ACTIVE_HEAD,)

    monkeypatch.setattr(upgrade_database_module, "hold_run_lock", fake_lock)
    monkeypatch.setattr(upgrade_database_module, "_read_revisions", revisions_only_under_lock)

    def accept_integrity(_path: Path) -> None:
        return None

    monkeypatch.setattr(upgrade_database_module, "_integrity_check", accept_integrity)

    receipt = upgrade_database(db_path, repo_root=ROOT)

    assert receipt.status == "already_current"


def _revision(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def test_upgrade_database_creates_fresh_db_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"

    created = upgrade_database(db_path, repo_root=ROOT)
    repeated = upgrade_database(db_path, repo_root=ROOT)

    assert created.status == "created"
    assert repeated.status == "already_current"
    assert _revision(db_path) == ACTIVE_HEAD


def test_upgrade_database_bridges_archived_revision_with_verified_backup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    upgrade_database(db_path, repo_root=ROOT)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE alembic_version SET version_num='0273_post_earnings_readout_budget'")
        conn.commit()
    finally:
        conn.close()
    backup_path = tmp_path / "before.db"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "execution" / "sqlite_bootstrap.py"),
            str(ROOT / "execution" / "upgrade_database.py"),
            "--db-path",
            str(db_path),
            "--repo-root",
            str(ROOT),
            "--backup-path",
            str(backup_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)

    assert receipt["status"] == "bridged"
    assert receipt["backup_path"] == str(backup_path.resolve())
    assert _revision(db_path) == ACTIVE_HEAD
    assert _revision(backup_path) == "0273_post_earnings_readout_budget"


def test_upgrade_database_rejects_nonempty_unversioned_db(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE operator_data(id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(UpgradeDatabaseError, match="refusing to guess"):
        upgrade_database(db_path, repo_root=ROOT)


def test_upgrade_database_rejects_foreign_key_corruption(tmp_path: Path) -> None:
    db_path = tmp_path / "foreign-key-corrupt.db"
    upgrade_database(db_path, repo_root=ROOT)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            INSERT INTO alerts
                (user_id, ticker, trigger_kind, fired_at, evidence_json, signature_sha)
            VALUES ('missing-tenant', 'TEST', 'material_news',
                    '2026-08-08T00:00:00', '{}', 'fk-corruption-test')
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(UpgradeDatabaseError, match="foreign_key_check failed"):
        upgrade_database(db_path, repo_root=ROOT)


def test_upgrade_database_cli_emits_valid_json_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.db"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "execution" / "sqlite_bootstrap.py"),
            str(ROOT / "execution" / "upgrade_database.py"),
            "--db-path",
            str(db_path),
            "--repo-root",
            str(ROOT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "created"
    assert payload["to_revision"] == ACTIVE_HEAD
