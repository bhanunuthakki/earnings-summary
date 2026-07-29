"""Safety contracts for isolated, clone-only data cutover preparation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import provenance.cutover_preflight as cutover
from execution.prepare_data_cutover import main
from provenance.cutover_preflight import (
    CutoverMode,
    CutoverPreflightError,
    CutoverRequest,
    canonical_manifest_json,
    prepare_cutover,
    verify_manifest_sha256,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


def _fake_repo(path: Path) -> Path:
    repo = path / "checkout"
    repo.mkdir()
    _write(repo / ".gitignore", ".tmp/\ndata/\n__pycache__/\n")
    _write(
        repo / "alembic" / "env.py",
        """
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if context.is_offline_mode():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
""".lstrip(),
    )
    _write(
        repo / "alembic" / "script.py.mako",
        '"""${message}"""\n',
    )
    _write(
        repo / "alembic" / "versions" / "0001_initial.py",
        """
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("payload")

def downgrade():
    op.drop_table("payload")
""".lstrip(),
    )
    _write(
        repo / "alembic" / "versions" / "0002_cutover_marker.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "cutover_marker",
        sa.Column("id", sa.Integer(), primary_key=True),
    )

def downgrade():
    op.drop_table("cutover_marker")
""".lstrip(),
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "cutover-test@example.invalid")
    _git(repo, "config", "user.name", "Cutover Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "test migration checkout")
    return repo


def _source_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            INSERT INTO alembic_version VALUES ('0001');
            CREATE TABLE payload (
                id INTEGER PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO payload (value) VALUES ('source remains immutable');
            """
        )
        conn.commit()
    finally:
        conn.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dry_run_is_read_only_and_seals_committed_migration_plan(
    tmp_path: Path,
) -> None:
    repo = _fake_repo(tmp_path)
    source = tmp_path / "source.db"
    destination = tmp_path / "isolated" / "clone.db"
    _source_database(source)
    source_before = source.read_bytes()

    manifest = prepare_cutover(
        CutoverRequest(
            repo_root=repo,
            source_path=source,
            destination_path=destination,
            minimum_space_reserve_bytes=1,
        )
    )

    assert manifest.mode is CutoverMode.DRY_RUN
    assert manifest.status == "ready"
    assert manifest.source_unchanged
    assert source.read_bytes() == source_before
    assert not destination.exists()
    assert not destination.parent.exists()
    assert manifest.manifest_path is None
    assert manifest.destination_artifact is None
    assert manifest.migration_plan.expected_alembic_head == "0002"
    assert [
        migration.revision for migration in manifest.migration_plan.ordered_migration_files
    ] == ["0001", "0002"]
    assert all(
        len(migration.sha256) == 64 for migration in manifest.migration_plan.ordered_migration_files
    )
    assert verify_manifest_sha256(manifest)
    assert json.loads(canonical_manifest_json(manifest))["manifest_sha256"] == (
        manifest.manifest_sha256
    )
    assert not _git(repo, "status", "--porcelain=v1", "--untracked-files=all").strip()


def test_refuses_live_database_as_destination(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    source = tmp_path / "source.db"
    live = repo / "data" / "portfolio.db"
    _source_database(source)

    with pytest.raises(ValueError, match="live database"):
        CutoverRequest(
            repo_root=repo,
            source_path=source,
            destination_path=live,
            live_database_path=live,
        )


def test_cli_defaults_to_dry_run_and_emits_canonical_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _fake_repo(tmp_path)
    source = tmp_path / "source.db"
    destination = tmp_path / "clone.db"
    _source_database(source)

    assert (
        main(
            [
                "--repo-root",
                str(repo),
                "--source-path",
                str(source),
                "--destination-path",
                str(destination),
                "--minimum-space-reserve-bytes",
                "1",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["mode"] == "dry-run"
    assert payload["status"] == "ready"
    assert not destination.exists()
    events = [json.loads(line)["event"] for line in captured.err.splitlines()]
    assert events == ["data_cutover_preflight_ready"]


def test_apply_requires_clean_committed_checkout_before_creating_clone(
    tmp_path: Path,
) -> None:
    repo = _fake_repo(tmp_path)
    source = tmp_path / "source.db"
    destination = tmp_path / "clone.db"
    _source_database(source)
    migration = repo / "alembic" / "versions" / "0002_cutover_marker.py"
    migration.write_text(
        migration.read_text(encoding="utf-8") + "\n# uncommitted\n",
        encoding="utf-8",
    )

    with pytest.raises(CutoverPreflightError, match="clean committed checkout"):
        prepare_cutover(
            CutoverRequest(
                repo_root=repo,
                source_path=source,
                destination_path=destination,
                mode=CutoverMode.APPLY,
                minimum_space_reserve_bytes=1,
            )
        )

    assert not destination.exists()


def test_apply_snapshots_and_upgrades_only_new_isolated_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _fake_repo(tmp_path)
    source = tmp_path / "source.db"
    destination = tmp_path / "clone.db"
    live = repo / "data" / "portfolio.db"
    _source_database(source)
    live.parent.mkdir(parents=True)
    _source_database(live)
    source_before = _sha256(source)
    live_before = _sha256(live)
    observed_write_sets: list[list[str]] = []
    job_lock = cutover.JobLock

    def recording_job_lock(
        repo_root: Path,
        job_name: str,
        write_sets: list[str],
    ) -> cutover.JobLock:
        observed_write_sets.append(write_sets)
        return job_lock(repo_root, job_name, write_sets)

    monkeypatch.setattr(cutover, "JobLock", recording_job_lock)

    manifest = prepare_cutover(
        CutoverRequest(
            repo_root=repo,
            source_path=source,
            destination_path=destination,
            live_database_path=live,
            mode=CutoverMode.APPLY,
            minimum_space_reserve_bytes=1,
        )
    )

    assert manifest.status == "applied"
    assert len(observed_write_sets) == 1
    assert len(observed_write_sets[0]) == 1
    assert manifest.clone_before is not None
    assert manifest.clone_after is not None
    assert manifest.clone_before.sqlite.clean
    assert manifest.clone_after.sqlite.clean
    assert manifest.destination_artifact is not None
    assert manifest.destination_artifact.alembic_revision == "0002"
    assert _sha256(source) == source_before
    assert _sha256(live) == live_before
    source_conn = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(f"{destination.as_uri()}?mode=ro", uri=True)
    try:
        assert source_conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0001",
        )
        assert destination_conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0002",
        )
        assert destination_conn.execute(
            "SELECT name FROM sqlite_master WHERE name='cutover_marker'"
        ).fetchone() == ("cutover_marker",)
    finally:
        source_conn.close()
        destination_conn.close()
    manifest_path = Path(manifest.manifest_path or "")
    assert manifest_path.is_file()
    assert manifest_path.read_text(encoding="utf-8").rstrip("\n") == (
        canonical_manifest_json(manifest)
    )
    assert verify_manifest_sha256(manifest)


def test_insufficient_free_space_stops_before_destination_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _fake_repo(tmp_path)
    source = tmp_path / "source.db"
    destination = tmp_path / "clone.db"
    _source_database(source)

    def insufficient_disk_usage(
        _path: str | os.PathLike[str],
    ) -> SimpleNamespace:
        return SimpleNamespace(total=10, used=9, free=1)

    monkeypatch.setattr(
        cutover.shutil,
        "disk_usage",
        insufficient_disk_usage,
    )

    with pytest.raises(CutoverPreflightError, match="insufficient destination free space"):
        prepare_cutover(
            CutoverRequest(
                repo_root=repo,
                source_path=source,
                destination_path=destination,
                mode=CutoverMode.APPLY,
                minimum_space_reserve_bytes=2,
            )
        )

    assert not destination.exists()
