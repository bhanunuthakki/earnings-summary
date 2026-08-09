"""Operational catalog and read-only gateway for sealed archive generations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from execution import register_archive_generation as registration_cli
from provenance import archive_catalog as catalog_module
from provenance.archive_catalog import (
    ArchiveCatalogError,
    ArchiveRegistrationRequest,
    open_archive_generation,
    register_archive_generation,
    select_archive_generations,
)
from provenance.archive_generation import (
    ArchiveGenerationRequest,
    build_archive_generation_manifest,
)
from provenance.immutable_artifact import publish_text_no_clobber

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 8, 2, 22, 0, tzinfo=UTC)
EMPTY_REFERENCE_ROOT = sha256(b"[]").hexdigest()


@pytest.fixture()
def ops_database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ops.db", stamp="archived-0271")


def _open_ops_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _archive(path: Path, *, sequence: int, value: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE publications (
                publication_id TEXT PRIMARY KEY,
                sequence_number INTEGER NOT NULL UNIQUE,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE facts (
                fact_id INTEGER PRIMARY KEY,
                publication_id TEXT NOT NULL REFERENCES publications(publication_id),
                payload TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO publications VALUES (?, ?, ?)",
            (f"pub-{sequence}", sequence, STAMP.isoformat()),
        )
        conn.execute(
            "INSERT INTO facts VALUES (?, ?, ?)",
            (sequence, f"pub-{sequence}", value),
        )
        conn.commit()
    finally:
        conn.close()


def _registration(
    archive_root: Path,
    *,
    generation_id: str,
    sequence: int,
    predecessor_manifest_sha256: str | None = None,
) -> ArchiveRegistrationRequest:
    database = archive_root / f"{generation_id}.db"
    manifest_path = archive_root / f"{generation_id}.manifest.json"
    _archive(database, sequence=sequence, value=generation_id)
    manifest = build_archive_generation_manifest(
        database,
        ArchiveGenerationRequest(
            generation_id=generation_id,
            archive_file=database.name,
            publication_sequence_start=sequence,
            publication_sequence_end=sequence,
            recorded_at_start=STAMP,
            recorded_at_end=STAMP,
            predecessor_manifest_sha256=predecessor_manifest_sha256,
            external_reference_count=0,
            external_reference_set_sha256=EMPTY_REFERENCE_ROOT,
            sealed_at=STAMP,
        ),
    )
    publish_text_no_clobber(manifest_path, manifest.model_dump_json())
    return ArchiveRegistrationRequest(
        manifest=manifest,
        archive_uri=database.name,
        manifest_uri=manifest_path.name,
        registered_at=STAMP,
    )


def test_current_head_contains_append_only_verified_catalog(
    ops_database: Path,
) -> None:
    conn = _open_ops_database(ops_database)
    try:
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "archive_generations",
            "archive_generation_table_commitments",
            "archive_generation_registration_receipts",
        } <= tables
        view = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' "
            "AND name='v_archive_generations_verified'"
        ).fetchone()
        assert view is not None
    finally:
        conn.close()



def test_registration_is_atomic_append_only_and_exactly_idempotent(
    tmp_path: Path,
    ops_database: Path,
) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    conn = _open_ops_database(ops_database)
    request = _registration(archive_root, generation_id="generation-1", sequence=1)
    try:
        first = register_archive_generation(conn, request)
        conn.commit()
        assert first.created
        assert not register_archive_generation(conn, request).created
        conn.commit()
        assert conn.execute(
            "SELECT generation_id FROM v_archive_generations_verified"
        ).fetchall() == [("generation-1",)]
        assert conn.execute(
            "SELECT COUNT(*) FROM archive_generation_table_commitments"
        ).fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE archive_generations SET archive_uri='other.db' "
                "WHERE generation_id='generation-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            conn.execute(
                "INSERT INTO archive_generation_table_commitments "
                "(generation_id,table_name,columns_json,primary_key_columns_json,"
                "row_count,content_sha256) VALUES (?,?,?,?,?,?)",
                ("generation-1", "late", "[]", "[]", 0, "a" * 64),
            )
    finally:
        conn.close()


def test_registration_rejects_archive_root_as_an_artifact_uri(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    request = _registration(archive_root, generation_id="generation-1", sequence=1)
    with pytest.raises(ValidationError, match="normalized relative paths"):
        ArchiveRegistrationRequest(
            manifest=request.manifest,
            archive_uri=".",
            manifest_uri=request.manifest_uri,
            registered_at=STAMP,
        )


def test_registration_requires_one_contiguous_verified_chain(
    tmp_path: Path,
    ops_database: Path,
) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    conn = _open_ops_database(ops_database)
    first = _registration(archive_root, generation_id="generation-1", sequence=1)
    try:
        register_archive_generation(conn, first)
        conn.commit()
        successor = _registration(
            archive_root,
            generation_id="generation-2",
            sequence=2,
            predecessor_manifest_sha256=first.manifest.manifest_sha256,
        )
        register_archive_generation(conn, successor)
        conn.commit()
        selected = select_archive_generations(conn, sequence_start=1, sequence_end=2)
        assert [item.generation_id for item in selected] == [
            "generation-1",
            "generation-2",
        ]

        gap = _registration(
            archive_root,
            generation_id="generation-4",
            sequence=4,
            predecessor_manifest_sha256=successor.manifest.manifest_sha256,
        )
        with pytest.raises(ArchiveCatalogError, match="contiguous"):
            register_archive_generation(conn, gap)
    finally:
        conn.close()


def test_gateway_reverifies_manifest_and_opens_archive_read_only(
    tmp_path: Path,
    ops_database: Path,
) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    conn = _open_ops_database(ops_database)
    request = _registration(archive_root, generation_id="generation-1", sequence=1)
    try:
        register_archive_generation(conn, request)
        conn.commit()
        with open_archive_generation(
            conn,
            archive_root=archive_root,
            generation_id="generation-1",
        ) as archive:
            assert archive.execute("SELECT payload FROM facts").fetchone()[0] == ("generation-1")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                archive.execute("INSERT INTO facts VALUES (2, 'pub-1', 'mutate')")
    finally:
        conn.close()


def test_gateway_detects_archive_or_manifest_tampering(
    tmp_path: Path,
    ops_database: Path,
) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    conn = _open_ops_database(ops_database)
    request = _registration(archive_root, generation_id="generation-1", sequence=1)
    try:
        register_archive_generation(conn, request)
        conn.commit()
        database = archive_root / request.archive_uri
        raw = sqlite3.connect(database)
        try:
            raw.execute("INSERT INTO facts VALUES (2, 'pub-1', 'tampered')")
            raw.commit()
        finally:
            raw.close()
        with (
            pytest.raises(ArchiveCatalogError, match="verification failed"),
            open_archive_generation(
                conn,
                archive_root=archive_root,
                generation_id="generation-1",
            ),
        ):
            pass
    finally:
        conn.close()


def test_gateway_reverifies_even_when_the_consumer_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ops_database: Path,
) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    conn = _open_ops_database(ops_database)
    request = _registration(archive_root, generation_id="generation-1", sequence=1)
    calls = 0
    original = catalog_module.verify_archive_generation_manifest

    def count_verifications(
        database: Path,
        manifest: catalog_module.ArchiveGenerationManifest,
    ) -> None:
        nonlocal calls
        calls += 1
        original(database, manifest)

    monkeypatch.setattr(
        catalog_module,
        "verify_archive_generation_manifest",
        count_verifications,
    )
    try:
        register_archive_generation(conn, request)
        conn.commit()
        with (
            pytest.raises(RuntimeError, match="consumer failed"),
            open_archive_generation(
                conn,
                archive_root=archive_root,
                generation_id="generation-1",
            ),
        ):
            raise RuntimeError("consumer failed")
        assert calls == 2
    finally:
        conn.close()


def test_registration_cli_dry_run_then_applies_exactly_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ops_database: Path,
) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    request = _registration(archive_root, generation_id="generation-1", sequence=1)
    manifest_path = archive_root / request.manifest_uri
    arguments = [
        "--repo-root",
        str(tmp_path),
        "--ops-database",
        str(ops_database),
        "--archive-root",
        str(archive_root),
        "--manifest",
        str(manifest_path),
        "--registered-at",
        STAMP.isoformat(),
    ]

    assert registration_cli.main(arguments) == 0
    dry_run = capsys.readouterr()
    assert '"status": "ready"' in dry_run.out
    conn = sqlite3.connect(ops_database)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM archive_generation_registration_receipts"
        ).fetchone() == (0,)
    finally:
        conn.close()

    assert registration_cli.main([*arguments, "--apply"]) == 0
    applied = capsys.readouterr()
    assert '"status": "registered"' in applied.out
    assert registration_cli.main([*arguments, "--apply"]) == 0
    replay = capsys.readouterr()
    assert '"status": "already_registered"' in replay.out
