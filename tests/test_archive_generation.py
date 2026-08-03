"""Contracts for immutable, independently verifiable archive generations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from provenance.archive_generation import (
    ArchiveGenerationError,
    ArchiveGenerationManifest,
    ArchiveGenerationRequest,
    build_archive_generation_manifest,
    verify_archive_generation_manifest,
)

STAMP = datetime(2026, 8, 2, 21, 0, tzinfo=UTC)
EMPTY_REFERENCE_ROOT = sha256(b"[]").hexdigest()


def _archive(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
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
                metric TEXT NOT NULL,
                value REAL,
                evidence BLOB
            );
            CREATE INDEX ix_facts_publication ON facts(publication_id);
            CREATE TRIGGER facts_append_only
            BEFORE DELETE ON facts
            BEGIN
                SELECT RAISE(ABORT, 'facts are append-only');
            END;
            INSERT INTO publications VALUES ('pub-1', 10, '2026-08-01T00:00:00Z');
            INSERT INTO publications VALUES ('pub-2', 11, '2026-08-02T00:00:00Z');
            INSERT INTO facts VALUES (1, 'pub-1', 'revenue', 12.5, X'0001');
            INSERT INTO facts VALUES (2, 'pub-2', 'margin', NULL, X'FF');
            """
        )
        conn.commit()
    finally:
        conn.close()


def _request(path: Path) -> ArchiveGenerationRequest:
    return ArchiveGenerationRequest(
        generation_id="archive-0001",
        archive_file=path.name,
        publication_sequence_start=10,
        publication_sequence_end=11,
        recorded_at_start=datetime(2026, 8, 1, tzinfo=UTC),
        recorded_at_end=datetime(2026, 8, 2, tzinfo=UTC),
        predecessor_manifest_sha256=None,
        external_reference_count=0,
        external_reference_set_sha256=EMPTY_REFERENCE_ROOT,
        sealed_at=STAMP,
    )


def test_manifest_commits_file_schema_and_every_table_without_mutating_archive(
    tmp_path: Path,
) -> None:
    database = tmp_path / "archive-0001.db"
    _archive(database)
    before = database.stat()

    manifest = build_archive_generation_manifest(database, _request(database))

    after = database.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert manifest.archive_file == database.name
    assert manifest.database_size_bytes == database.stat().st_size
    assert len(manifest.database_sha256) == 64
    assert len(manifest.schema_sha256) == 64
    assert manifest.quick_check == "ok"
    assert manifest.integrity_check == "ok"
    assert manifest.foreign_key_violation_count == 0
    assert [(table.table_name, table.row_count) for table in manifest.tables] == [
        ("facts", 2),
        ("publications", 2),
    ]
    assert all(len(table.content_sha256) == 64 for table in manifest.tables)
    assert len(manifest.manifest_sha256) == 64
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert not Path(f"{database}-journal").exists()
    verify_archive_generation_manifest(database, manifest)


def test_table_content_commitment_is_insertion_order_independent(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _archive(first)
    _archive(second)
    conn = sqlite3.connect(second)
    try:
        conn.execute("DROP TRIGGER facts_append_only")
        rows = conn.execute("SELECT * FROM facts ORDER BY fact_id DESC").fetchall()
        conn.execute("DELETE FROM facts")
        conn.executemany("INSERT INTO facts VALUES (?, ?, ?, ?, ?)", rows)
        conn.executescript(
            """
            CREATE TRIGGER facts_append_only
            BEFORE DELETE ON facts
            BEGIN
                SELECT RAISE(ABORT, 'facts are append-only');
            END;
            """
        )
        conn.commit()
    finally:
        conn.close()

    first_manifest = build_archive_generation_manifest(first, _request(first))
    second_manifest = build_archive_generation_manifest(second, _request(second))

    first_roots = {table.table_name: table.content_sha256 for table in first_manifest.tables}
    second_roots = {table.table_name: table.content_sha256 for table in second_manifest.tables}
    assert first_roots == second_roots


def test_manifest_verification_detects_archive_mutation(tmp_path: Path) -> None:
    database = tmp_path / "archive-0001.db"
    _archive(database)
    manifest = build_archive_generation_manifest(database, _request(database))
    conn = sqlite3.connect(database)
    try:
        conn.execute("INSERT INTO publications VALUES ('pub-3', 12, '2026-08-03T00:00:00Z')")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ArchiveGenerationError, match="does not match its manifest"):
        verify_archive_generation_manifest(database, manifest)


def test_sealing_rejects_ctas_table_without_stable_primary_key(tmp_path: Path) -> None:
    database = tmp_path / "ctas.db"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(
            """
            CREATE TABLE source_rows (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            INSERT INTO source_rows VALUES (1, 'evidence');
            CREATE TABLE archive_rows AS SELECT * FROM source_rows;
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ArchiveGenerationError, match="stable primary key"):
        build_archive_generation_manifest(database, _request(database))


def test_sealing_rejects_fts_or_other_virtual_tables(tmp_path: Path) -> None:
    database = tmp_path / "fts.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute("CREATE VIRTUAL TABLE search_rows USING fts5(body)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ArchiveGenerationError, match="virtual tables"):
        build_archive_generation_manifest(database, _request(database))


def test_request_rejects_invalid_generation_bounds(tmp_path: Path) -> None:
    database = tmp_path / "archive-0001.db"
    _archive(database)
    payload = _request(database).model_dump()
    payload["publication_sequence_start"] = 12
    payload["publication_sequence_end"] = 11

    with pytest.raises(ValueError, match="sequence range"):
        ArchiveGenerationRequest.model_validate(payload)


def test_manifest_parser_reapplies_generation_bounds(tmp_path: Path) -> None:
    database = tmp_path / "archive-0001.db"
    _archive(database)
    manifest = build_archive_generation_manifest(database, _request(database))
    payload = manifest.model_dump()
    payload["recorded_at_start"] = datetime(2026, 8, 4, tzinfo=UTC)

    with pytest.raises(ValueError, match="recorded-at range"):
        ArchiveGenerationManifest.model_validate(payload)


def test_cli_seals_and_verifies_non_live_archive_without_clobber(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution.seal_archive_generation import main

    repo = tmp_path / "repo"
    database = tmp_path / "archive-0001.db"
    seal = tmp_path / "archive-0001.manifest.json"
    (repo / "data").mkdir(parents=True)
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    _archive(database)
    common = [
        "--repo-root",
        str(repo),
        "--database",
        str(database),
        "--manifest",
        str(seal),
    ]

    seal_args = [
        "seal",
        *common,
        "--generation-id",
        "archive-0001",
        "--publication-sequence-start",
        "10",
        "--publication-sequence-end",
        "11",
        "--recorded-at-start",
        "2026-08-01T00:00:00+00:00",
        "--recorded-at-end",
        "2026-08-02T00:00:00+00:00",
        "--external-reference-count",
        "0",
        "--external-reference-set-sha256",
        EMPTY_REFERENCE_ROOT,
        "--sealed-at",
        STAMP.isoformat(),
    ]

    assert main(seal_args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "sealed"
    assert seal.is_file()

    assert main(["verify", *common]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "verified"

    assert main(seal_args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["status"] == "already_sealed"


def test_cli_refuses_canonical_live_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution.seal_archive_generation import main

    repo = tmp_path / "repo"
    database = repo / "data" / "portfolio.db"
    database.parent.mkdir(parents=True)
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    _archive(database)

    assert (
        main(
            [
                "verify",
                "--repo-root",
                str(repo),
                "--database",
                str(database),
                "--manifest",
                str(tmp_path / "manifest.json"),
            ]
        )
        == 2
    )
