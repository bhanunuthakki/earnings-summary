from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from upgrade_database import ACTIVE_HEAD, UpgradeDatabaseError, upgrade_database

ROOT = Path(__file__).resolve().parents[1]


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


def test_upgrade_database_cli_emits_valid_json_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.db"
    result = subprocess.run(
        [
            sys.executable,
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
