"""Operational publication boundary and read gateway for archive generations.

Archive files are durable before this catalog is changed.  Registration adds
one immutable catalog row, its table commitments, and one receipt in a small
operational transaction.  Readers see only receipted generations and verify
the manifest and archive again before and after each read-only session.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from provenance.archive_generation import (
    ArchiveGenerationError,
    ArchiveGenerationManifest,
    verify_archive_generation_manifest,
)
from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    assert_artifact_unchanged,
    read_stable_artifact,
    require_no_reparse_points,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_REGISTRATION_SCHEMA_VERSION = "archive-generation-registration/v1"


class ArchiveCatalogError(RuntimeError):
    """A catalog write or verified archive read failed closed."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArchiveRegistrationRequest(_FrozenModel):
    manifest: ArchiveGenerationManifest
    archive_uri: str = Field(min_length=1, max_length=1024)
    manifest_uri: str = Field(min_length=1, max_length=1024)
    registered_at: datetime

    @field_validator("archive_uri", "manifest_uri")
    @classmethod
    def _safe_relative_uri(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.parts or path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise ValueError("archive catalog URIs must be normalized relative paths")
        if any(":" in part or "\\" in part for part in path.parts):
            raise ValueError("archive catalog URIs must use portable path components")
        return path.as_posix()

    @field_validator("registered_at")
    @classmethod
    def _aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("registered_at must include a timezone")
        return value


class ArchiveRegistrationResult(_FrozenModel):
    generation_id: str
    receipt_id: str
    receipt_sha256: str
    created: bool


class ArchiveCatalogRecord(_FrozenModel):
    generation_id: str
    predecessor_generation_id: str | None
    archive_uri: str
    manifest_uri: str
    manifest_sha256: str
    database_sha256: str
    publication_sequence_start: int
    publication_sequence_end: int
    recorded_at_start: datetime
    recorded_at_end: datetime
    receipt_sha256: str


def register_archive_generation(
    conn: sqlite3.Connection,
    request: ArchiveRegistrationRequest,
) -> ArchiveRegistrationResult:
    """Atomically register one already sealed generation, or prove exact replay."""

    manifest = request.manifest
    request_core = {
        "archive_uri": request.archive_uri,
        "generation_id": manifest.generation_id,
        "manifest_sha256": manifest.manifest_sha256,
        "manifest_uri": request.manifest_uri,
        "registered_at": request.registered_at,
        "schema_version": _REGISTRATION_SCHEMA_VERSION,
    }
    request_sha256 = _digest(request_core)
    result_core = {
        "database_sha256": manifest.database_sha256,
        "external_reference_set_sha256": manifest.external_reference_set_sha256,
        "generation_id": manifest.generation_id,
        "manifest_sha256": manifest.manifest_sha256,
        "schema_sha256": manifest.schema_sha256,
        "table_count": len(manifest.tables),
    }
    result_sha256 = _digest(result_core)
    receipt_core = {
        **request_core,
        "request_sha256": request_sha256,
        "result_sha256": result_sha256,
    }
    receipt_sha256 = _digest(receipt_core)
    receipt_id = f"archive-registration:{receipt_sha256}"

    existing = conn.execute(
        "SELECT receipt_id,request_sha256,result_sha256,receipt_sha256 "
        "FROM archive_generation_registration_receipts WHERE generation_id=?",
        (manifest.generation_id,),
    ).fetchone()
    if existing is not None:
        if tuple(str(value) for value in existing) != (
            receipt_id,
            request_sha256,
            result_sha256,
            receipt_sha256,
        ):
            raise ArchiveCatalogError(
                "archive generation identity conflicts with its existing registration"
            )
        return ArchiveRegistrationResult(
            generation_id=manifest.generation_id,
            receipt_id=receipt_id,
            receipt_sha256=receipt_sha256,
            created=False,
        )

    predecessor_generation_id = _resolve_predecessor(conn, manifest)
    conn.execute("SAVEPOINT archive_generation_registration")
    try:
        conn.execute(
            "INSERT INTO archive_generations ("
            "generation_id,predecessor_generation_id,predecessor_manifest_sha256,"
            "archive_uri,manifest_uri,manifest_sha256,database_sha256,schema_sha256,"
            "schema_version,publication_sequence_start,publication_sequence_end,"
            "recorded_at_start,recorded_at_end,external_reference_count,"
            "external_reference_set_sha256,database_size_bytes,table_count,sealed_at,"
            "registered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                manifest.generation_id,
                predecessor_generation_id,
                manifest.predecessor_manifest_sha256,
                request.archive_uri,
                request.manifest_uri,
                manifest.manifest_sha256,
                manifest.database_sha256,
                manifest.schema_sha256,
                manifest.schema_version,
                manifest.publication_sequence_start,
                manifest.publication_sequence_end,
                manifest.recorded_at_start.isoformat(),
                manifest.recorded_at_end.isoformat(),
                manifest.external_reference_count,
                manifest.external_reference_set_sha256,
                manifest.database_size_bytes,
                len(manifest.tables),
                manifest.sealed_at.isoformat(),
                request.registered_at.isoformat(),
            ),
        )
        conn.executemany(
            "INSERT INTO archive_generation_table_commitments "
            "(generation_id,table_name,columns_json,primary_key_columns_json,"
            "row_count,content_sha256) VALUES (?,?,?,?,?,?)",
            (
                (
                    manifest.generation_id,
                    table.table_name,
                    _canonical_json(table.columns),
                    _canonical_json(table.primary_key_columns),
                    table.row_count,
                    table.content_sha256,
                )
                for table in manifest.tables
            ),
        )
        conn.execute(
            "INSERT INTO archive_generation_registration_receipts "
            "(receipt_id,generation_id,request_sha256,result_sha256,receipt_sha256,"
            "receipt_json,recorded_at) VALUES (?,?,?,?,?,?,?)",
            (
                receipt_id,
                manifest.generation_id,
                request_sha256,
                result_sha256,
                receipt_sha256,
                _canonical_json(receipt_core),
                request.registered_at.isoformat(),
            ),
        )
        conn.execute("RELEASE archive_generation_registration")
    except Exception:
        conn.execute("ROLLBACK TO archive_generation_registration")
        conn.execute("RELEASE archive_generation_registration")
        raise
    return ArchiveRegistrationResult(
        generation_id=manifest.generation_id,
        receipt_id=receipt_id,
        receipt_sha256=receipt_sha256,
        created=True,
    )


def select_archive_generations(
    conn: sqlite3.Connection,
    *,
    sequence_start: int,
    sequence_end: int,
) -> tuple[ArchiveCatalogRecord, ...]:
    """Return the verified generations overlapping one publication range."""

    if sequence_start < 0 or sequence_end < sequence_start:
        raise ValueError("archive selection sequence range is invalid")
    rows = conn.execute(
        "SELECT generation_id,predecessor_generation_id,archive_uri,manifest_uri,"
        "manifest_sha256,database_sha256,publication_sequence_start,"
        "publication_sequence_end,recorded_at_start,recorded_at_end,receipt_sha256 "
        "FROM v_archive_generations_verified "
        "WHERE publication_sequence_end>=? AND publication_sequence_start<=? "
        "ORDER BY publication_sequence_start,generation_id",
        (sequence_start, sequence_end),
    ).fetchall()
    return tuple(
        ArchiveCatalogRecord(
            generation_id=str(row[0]),
            predecessor_generation_id=None if row[1] is None else str(row[1]),
            archive_uri=str(row[2]),
            manifest_uri=str(row[3]),
            manifest_sha256=str(row[4]),
            database_sha256=str(row[5]),
            publication_sequence_start=int(row[6]),
            publication_sequence_end=int(row[7]),
            recorded_at_start=row[8],
            recorded_at_end=row[9],
            receipt_sha256=str(row[10]),
        )
        for row in rows
    )


@contextmanager
def open_archive_generation(
    ops_conn: sqlite3.Connection,
    *,
    archive_root: Path,
    generation_id: str,
) -> Generator[sqlite3.Connection]:
    """Verify and open one cataloged generation read-only for a bounded session."""

    row = ops_conn.execute(
        "SELECT archive_uri,manifest_uri,manifest_sha256,database_sha256 "
        "FROM v_archive_generations_verified WHERE generation_id=?",
        (generation_id,),
    ).fetchone()
    if row is None:
        raise ArchiveCatalogError("archive generation is not registered and verified")
    require_no_reparse_points(archive_root)
    root = archive_root.resolve()
    database = _resolve_catalog_path(root, str(row[0]))
    manifest_path = _resolve_catalog_path(root, str(row[1]))
    try:
        manifest_snapshot, payload = read_stable_artifact(manifest_path)
        manifest = ArchiveGenerationManifest.model_validate_json(payload)
        if manifest.manifest_sha256 != str(row[2]):
            raise ArchiveCatalogError("catalog manifest commitment does not match artifact")
        if manifest.database_sha256 != str(row[3]):
            raise ArchiveCatalogError("catalog database commitment does not match manifest")
        verify_archive_generation_manifest(database, manifest)
        conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
        try:
            try:
                yield conn
            finally:
                conn.close()
        finally:
            verify_archive_generation_manifest(database, manifest)
            assert_artifact_unchanged(manifest_snapshot)
    except ArchiveCatalogError:
        raise
    except (
        ArchiveGenerationError,
        ImmutableArtifactConflictError,
        OSError,
        sqlite3.DatabaseError,
        ValueError,
    ) as exc:
        raise ArchiveCatalogError("archive generation verification failed") from exc


def _resolve_predecessor(
    conn: sqlite3.Connection,
    manifest: ArchiveGenerationManifest,
) -> str | None:
    if manifest.predecessor_manifest_sha256 is None:
        if conn.execute("SELECT 1 FROM v_archive_generations_verified LIMIT 1").fetchone():
            raise ArchiveCatalogError("archive generation chain already has a genesis")
        return None
    row = conn.execute(
        "SELECT generation_id,publication_sequence_end,recorded_at_end "
        "FROM v_archive_generations_verified WHERE manifest_sha256=?",
        (manifest.predecessor_manifest_sha256,),
    ).fetchone()
    if row is None:
        raise ArchiveCatalogError("archive predecessor is not registered and verified")
    if manifest.publication_sequence_start != int(row[1]) + 1:
        raise ArchiveCatalogError("archive publication sequence is not contiguous")
    predecessor_recorded_end = datetime.fromisoformat(str(row[2]))
    if manifest.recorded_at_start < predecessor_recorded_end:
        raise ArchiveCatalogError("archive recorded-at range overlaps its predecessor")
    return str(row[0])


def _resolve_catalog_path(root: Path, uri: str) -> Path:
    relative = PurePosixPath(uri)
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
    ):
        raise ArchiveCatalogError("archive catalog contains an unsafe URI")
    candidate = root.joinpath(*relative.parts)
    require_no_reparse_points(candidate)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ArchiveCatalogError("archive catalog URI escapes its root") from exc
    return candidate


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported archive receipt value {type(value).__name__}")
