"""Contracts for the read-only, verified SQLite snapshot seam."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import sqlite_snapshot
from execution.create_sqlite_snapshot import main
from sqlite_runtime import SQLiteConnectionRole
from sqlite_snapshot import (
    SnapshotConflictError,
    SnapshotRequest,
    create_snapshot,
    verify_snapshot_matches_source,
)


def _source_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
        INSERT INTO alembic_version (version_num) VALUES ('0219');
        CREATE TABLE reported_values (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO reported_values (value) VALUES ('consistent evidence');
        """
    )
    conn.commit()
    conn.close()


def test_creates_verified_consistent_snapshot_and_strict_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshots" / "source.snapshot.db"
    _source_database(source)

    manifest = create_snapshot(SnapshotRequest(source_path=source, destination_path=destination))

    assert destination.exists()
    copied = sqlite3.connect(f"{destination.as_uri()}?mode=ro", uri=True)
    try:
        assert copied.execute("SELECT value FROM reported_values").fetchone() == (
            "consistent evidence",
        )
    finally:
        copied.close()
    payload = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
    assert payload["source"]["alembic_revision"] == "0219"
    assert payload["snapshot"]["byte_size"] == destination.stat().st_size
    assert payload["verification"]["integrity_check"] == ["ok"]
    assert payload["verification"]["foreign_key_check"] == []
    assert set(payload) == {
        "code_config_version",
        "created_at",
        "schema_version",
        "snapshot",
        "source",
        "verification",
    }


def test_refuses_destination_conflict_but_accepts_exact_verified_replay(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    _source_database(source)
    request = SnapshotRequest(source_path=source, destination_path=destination)

    first = create_snapshot(request)
    replay = create_snapshot(request)

    assert replay.snapshot_sha256 == first.snapshot_sha256
    destination.with_suffix(destination.suffix + ".manifest.json").unlink()
    with pytest.raises(SnapshotConflictError, match="already exists"):
        create_snapshot(request)


def test_rejects_snapshot_when_source_has_foreign_key_violation(tmp_path: Path) -> None:
    source = tmp_path / "broken.db"
    destination = tmp_path / "snapshot.db"
    _source_database(source)
    conn = sqlite3.connect(source)
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES reported_values(id));
        INSERT INTO child (id, parent_id) VALUES (1, 999);
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="foreign_key_check"):
        create_snapshot(SnapshotRequest(source_path=source, destination_path=destination))

    assert not destination.exists()


def test_wal_mutation_with_unchanged_main_file_conflicts_instead_of_replaying(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    _source_database(source)
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute("INSERT INTO reported_values (value) VALUES ('first WAL row')")
        writer.commit()
        request = SnapshotRequest(source_path=source, destination_path=destination)
        create_snapshot(request)
        main_file_before = (source.stat().st_size, source.stat().st_mtime_ns)

        writer.execute("INSERT INTO reported_values (value) VALUES ('new WAL row')")
        writer.commit()

        assert (source.stat().st_size, source.stat().st_mtime_ns) == main_file_before
        with pytest.raises(SnapshotConflictError, match="different source content"):
            create_snapshot(request)
    finally:
        writer.close()


def test_replay_ignores_verifier_owned_transient_empty_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    _source_database(source)
    request = SnapshotRequest(source_path=source, destination_path=destination)
    expected = create_snapshot(request)
    real_connect = sqlite_snapshot.connect_sqlite
    real_optional_file_state = sqlite_snapshot._optional_file_state
    source_connection_open = False

    class SourceConnectionProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

        def close(self) -> None:
            nonlocal source_connection_open
            self._connection.close()
            source_connection_open = False

    def connect_with_transient_wal(
        path: str | Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection | SourceConnectionProxy:
        nonlocal source_connection_open
        connection = real_connect(path, role=role, schema_preflight=schema_preflight)
        if Path(path) == source and role is SQLiteConnectionRole.READ_ONLY:
            source_connection_open = True
            return SourceConnectionProxy(connection)
        return connection

    def transient_wal_state(path: Path) -> tuple[int, int] | None:
        if path == Path(f"{source}-wal") and source_connection_open:
            return (1, 0)
        return real_optional_file_state(path)

    monkeypatch.setattr(sqlite_snapshot, "connect_sqlite", connect_with_transient_wal)
    monkeypatch.setattr(sqlite_snapshot, "_optional_file_state", transient_wal_state)

    replay = verify_snapshot_matches_source(request)

    assert replay.replayed is True
    assert replay.snapshot_sha256 == expected.snapshot_sha256


def test_replay_rejects_post_close_commit_checkpointed_into_main_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    _source_database(source)
    request = SnapshotRequest(source_path=source, destination_path=destination)
    create_snapshot(request)
    real_optional_file_state = sqlite_snapshot._optional_file_state
    wal_observation_count = 0

    def commit_before_final_wal_observation(path: Path) -> tuple[int, int] | None:
        nonlocal wal_observation_count
        if path == Path(f"{source}-wal"):
            wal_observation_count += 1
            if wal_observation_count == 2:
                writer = sqlite3.connect(source)
                try:
                    assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
                    writer.execute(
                        "INSERT INTO reported_values (value) VALUES ('post-close commit')"
                    )
                    writer.commit()
                    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    writer.close()
                Path(f"{source}-wal").unlink(missing_ok=True)
                Path(f"{source}-shm").unlink(missing_ok=True)
        return real_optional_file_state(path)

    monkeypatch.setattr(
        sqlite_snapshot, "_optional_file_state", commit_before_final_wal_observation
    )

    with pytest.raises(RuntimeError, match="source WAL changed"):
        verify_snapshot_matches_source(request)

    live = sqlite3.connect(source)
    snapshot = sqlite3.connect(destination)
    try:
        assert live.execute("SELECT COUNT(*) FROM reported_values").fetchone() == (2,)
        assert snapshot.execute("SELECT COUNT(*) FROM reported_values").fetchone() == (1,)
    finally:
        live.close()
        snapshot.close()
    assert not Path(f"{source}-wal").exists()


def test_manifest_publish_failure_leaves_a_conflicting_partial_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    _source_database(source)
    request = SnapshotRequest(source_path=source, destination_path=destination)

    def fail_manifest_publish(path: Path, manifest: object) -> None:
        raise OSError("manifest storage unavailable")

    monkeypatch.setattr(sqlite_snapshot, "_write_manifest_atomically", fail_manifest_publish)
    with pytest.raises(OSError, match="manifest storage unavailable"):
        create_snapshot(request)

    assert destination.exists()
    with pytest.raises(SnapshotConflictError, match="without a complete manifest"):
        create_snapshot(request)


def test_cli_emits_one_json_result_and_structured_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    _source_database(source)

    assert main(["--source-path", str(source), "--destination-path", str(destination)]) == 0

    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["snapshot_path"] == str(destination.resolve())
    events = [json.loads(line)["event"] for line in captured.err.splitlines()]
    assert events[0] == "sqlite_snapshot_started"
    assert events[-1] == "sqlite_snapshot_finished"
