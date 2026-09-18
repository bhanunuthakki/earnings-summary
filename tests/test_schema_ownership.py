from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from quality import schema_ownership
from quality.git_env import clean_local_git_env
from quality.schema_ownership import SchemaOwnershipInventory, inventory_database


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path, *, include_orphan: bool = False) -> tuple[Path, Path]:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=clean_local_git_env())
    _write(
        tmp_path,
        "alembic/versions/0001_base.py",
        """revision = "0001"
down_revision = None
def upgrade():
    op.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY)")
    op.execute("CREATE VIEW fact_ids AS SELECT id FROM facts")
""",
    )
    _write(
        tmp_path,
        "src/store.py",
        'READ = "SELECT id FROM facts"\nWRITE = "INSERT INTO facts(id) VALUES (?)"\n',
    )
    database = tmp_path / "fixture.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        connection.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE VIEW fact_ids AS SELECT id FROM facts")
        if include_orphan:
            connection.execute("CREATE TABLE orphaned (id INTEGER)")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, env=clean_local_git_env())
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
        env=clean_local_git_env(),
    )
    return tmp_path, database


def test_records_schema_revision_readers_writers_and_recovery_owner(tmp_path: Path) -> None:
    root, database = _repo(tmp_path)
    inventory = inventory_database(root, database, schema_revision="0001")
    entries = {entry.name: entry for entry in inventory.entries}
    facts = entries["facts"]
    assert inventory.status == "PASS"
    assert inventory.deletion_authority is False
    assert inventory.schema_revision == "0001"
    assert facts.schema_revision == "0001"
    assert facts.readers == ("src/store.py:1",)
    assert facts.writers == ("src/store.py:2",)
    assert facts.historical_evidence == ("alembic/versions/0001_base.py:4",)
    assert facts.recovery_owner == "alembic/versions/0001_base.py:4"
    assert entries["fact_ids"].kind == "view"
    assert entries["alembic_version"].recovery_owner == "alembic:version-table"


def test_missing_recovery_owner_holds_without_granting_deletion(tmp_path: Path) -> None:
    root, database = _repo(tmp_path, include_orphan=True)
    inventory = inventory_database(root, database, schema_revision="0001")
    orphaned = next(entry for entry in inventory.entries if entry.name == "orphaned")
    assert inventory.status == "HOLD"
    assert inventory.deletion_authority is False
    assert orphaned.ownership == "unowned"
    assert orphaned.recovery_owner is None
    assert inventory.violations == ("table orphaned: no active migration recovery owner",)


def test_inventory_is_deterministic(tmp_path: Path) -> None:
    root, database = _repo(tmp_path)
    first = inventory_database(root, database, schema_revision="0001")
    second = inventory_database(root, database, schema_revision="0001")
    assert json.loads(first.model_dump_json()) == json.loads(second.model_dump_json())


def test_comments_and_downgrade_sql_do_not_manufacture_ownership(tmp_path: Path) -> None:
    root, database = _repo(tmp_path, include_orphan=True)
    migration = root / "alembic/versions/0001_base.py"
    migration.write_text(
        migration.read_text(encoding="utf-8")
        + """
# CREATE TABLE orphaned (id INTEGER)
def downgrade():
    op.execute("CREATE TABLE orphaned (id INTEGER)")
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, env=clean_local_git_env())
    subprocess.run(
        ["git", "commit", "-qm", "misleading ddl"],
        cwd=root,
        check=True,
        env=clean_local_git_env(),
    )

    inventory = inventory_database(root, database, schema_revision="0001")
    orphaned = next(entry for entry in inventory.entries if entry.name == "orphaned")
    assert inventory.status == "HOLD"
    assert orphaned.ownership == "unowned"
    assert orphaned.recovery_owner is None


def test_sqlite_prefix_filter_does_not_hide_similarly_named_user_table(
    tmp_path: Path,
) -> None:
    root, database = _repo(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sqlitefacts (id INTEGER)")

    inventory = inventory_database(root, database, schema_revision="0001")
    entry = next(item for item in inventory.entries if item.name == "sqlitefacts")
    assert inventory.status == "HOLD"
    assert entry.ownership == "unowned"


def test_output_must_be_non_aliasing_path_under_repository_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = _repo(tmp_path)
    inventory = inventory_database(root, database, schema_revision="0001")

    def fake_build_inventory(_root: Path) -> SchemaOwnershipInventory:
        return inventory

    monkeypatch.setattr(schema_ownership, "build_inventory", fake_build_inventory)
    source = tmp_path / "src/store.py"
    (tmp_path / ".tmp").mkdir()

    before = source.read_bytes()
    assert schema_ownership.main(["--repo-root", str(root), "--output", "src/store.py"]) == 1
    assert source.read_bytes() == before

    symlink = tmp_path / ".tmp/link.json"
    symlink.symlink_to(source)
    assert schema_ownership.main(["--repo-root", str(root), "--output", ".tmp/link.json"]) == 1
    assert source.read_bytes() == before

    hardlink = tmp_path / ".tmp/hardlink.json"
    os.link(source, hardlink)
    assert schema_ownership.main(["--repo-root", str(root), "--output", ".tmp/hardlink.json"]) == 1
    assert source.read_bytes() == before
