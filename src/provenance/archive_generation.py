"""Fail-closed commitments for quiesced immutable SQLite archive generations.

The archive database is authoritative evidence.  Its manifest is a portable
sidecar commitment: it binds the exact SQLite file, schema, every ordinary
table's primary-key-ordered contents, publication bounds, predecessor, and
the separately verified cross-generation reference set.  This module never
writes to the archive database.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.immutable_artifact import require_no_reparse_points
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_SCHEMA_VERSION = "archive-generation-manifest/v1"
_HEX = frozenset("0123456789abcdef")
_EMPTY_SET_SHA256 = hashlib.sha256(b"[]").hexdigest()
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_HASH_CHUNK_BYTES = 1024 * 1024
_ROW_FETCH_SIZE = 2_000


class ArchiveGenerationError(RuntimeError):
    """An archive candidate failed a non-negotiable sealing invariant."""


class _Digest(Protocol):
    def update(self, value: bytes, /) -> None: ...


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArchiveGenerationRequest(_FrozenModel):
    generation_id: str = Field(min_length=1, max_length=128)
    archive_file: str = Field(min_length=1, max_length=255)
    publication_sequence_start: int = Field(ge=0)
    publication_sequence_end: int = Field(ge=0)
    recorded_at_start: datetime
    recorded_at_end: datetime
    predecessor_manifest_sha256: str | None = None
    external_reference_count: int = Field(ge=0)
    external_reference_set_sha256: str
    sealed_at: datetime

    @field_validator(
        "predecessor_manifest_sha256",
        "external_reference_set_sha256",
    )
    @classmethod
    def _valid_sha256(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in _HEX for character in value)
        ):
            raise ValueError("SHA-256 commitment is malformed")
        return value

    @field_validator("recorded_at_start", "recorded_at_end", "sealed_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("archive timestamps must include a timezone")
        return value

    @field_validator("archive_file")
    @classmethod
    def _portable_file_name(cls, value: str) -> str:
        if Path(value).name != value or "/" in value or "\\" in value:
            raise ValueError("archive_file must be one portable file name")
        return value

    @model_validator(mode="after")
    def _valid_bounds(self) -> ArchiveGenerationRequest:
        if self.publication_sequence_start > self.publication_sequence_end:
            raise ValueError("archive publication sequence range is reversed")
        if self.recorded_at_start > self.recorded_at_end:
            raise ValueError("archive recorded-at range is reversed")
        if (
            self.external_reference_count == 0
            and self.external_reference_set_sha256 != _EMPTY_SET_SHA256
        ):
            raise ValueError("empty external-reference set has the wrong commitment")
        return self


class ArchiveTableCommitment(_FrozenModel):
    table_name: str = Field(min_length=1)
    columns: tuple[str, ...] = Field(min_length=1)
    primary_key_columns: tuple[str, ...] = Field(min_length=1)
    row_count: int = Field(ge=0)
    content_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class ArchiveGenerationManifest(_FrozenModel):
    schema_version: Literal["archive-generation-manifest/v1"] = _SCHEMA_VERSION
    generation_id: str
    archive_file: str
    publication_sequence_start: int = Field(ge=0)
    publication_sequence_end: int = Field(ge=0)
    recorded_at_start: datetime
    recorded_at_end: datetime
    predecessor_manifest_sha256: str | None
    external_reference_count: int = Field(ge=0)
    external_reference_set_sha256: str
    sealed_at: datetime
    database_size_bytes: int = Field(ge=0)
    database_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    quick_check: Literal["ok"]
    integrity_check: Literal["ok"]
    foreign_key_violation_count: Literal[0]
    tables: tuple[ArchiveTableCommitment, ...] = Field(min_length=1)
    manifest_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _valid_generation_contract(self) -> ArchiveGenerationManifest:
        ArchiveGenerationRequest(
            generation_id=self.generation_id,
            archive_file=self.archive_file,
            publication_sequence_start=self.publication_sequence_start,
            publication_sequence_end=self.publication_sequence_end,
            recorded_at_start=self.recorded_at_start,
            recorded_at_end=self.recorded_at_end,
            predecessor_manifest_sha256=self.predecessor_manifest_sha256,
            external_reference_count=self.external_reference_count,
            external_reference_set_sha256=self.external_reference_set_sha256,
            sealed_at=self.sealed_at,
        )
        return self


class _ArchiveInspection(_FrozenModel):
    database_size_bytes: int
    database_sha256: str
    schema_sha256: str
    quick_check: Literal["ok"]
    integrity_check: Literal["ok"]
    foreign_key_violation_count: Literal[0]
    tables: tuple[ArchiveTableCommitment, ...]


class _FileSnapshot(_FrozenModel):
    device: int
    inode: int
    size_bytes: int
    modified_time_ns: int
    changed_time_ns: int
    sha256: str


def build_archive_generation_manifest(
    database: Path,
    request: ArchiveGenerationRequest,
) -> ArchiveGenerationManifest:
    """Inspect one quiesced archive read-only and bind it to a typed manifest."""

    candidate = _lexical_absolute(database)
    if candidate.name != request.archive_file:
        raise ArchiveGenerationError("archive file name does not match the request")
    inspection = _inspect_archive(candidate)
    core = request.model_dump(mode="json") | inspection.model_dump(mode="json")
    core["schema_version"] = _SCHEMA_VERSION
    return ArchiveGenerationManifest.model_validate(
        core | {"manifest_sha256": _canonical_sha256(core)}
    )


def verify_archive_generation_manifest(
    database: Path,
    manifest: ArchiveGenerationManifest,
) -> None:
    """Recompute all database commitments and reject any manifest divergence."""

    core = manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    if _canonical_sha256(core) != manifest.manifest_sha256:
        raise ArchiveGenerationError("archive manifest commitment is invalid")
    candidate = _lexical_absolute(database)
    if candidate.name != manifest.archive_file:
        raise ArchiveGenerationError("archive file name does not match its manifest")
    actual = _inspect_archive(candidate)
    expected = _ArchiveInspection(
        database_size_bytes=manifest.database_size_bytes,
        database_sha256=manifest.database_sha256,
        schema_sha256=manifest.schema_sha256,
        quick_check=manifest.quick_check,
        integrity_check=manifest.integrity_check,
        foreign_key_violation_count=manifest.foreign_key_violation_count,
        tables=manifest.tables,
    )
    if actual != expected:
        raise ArchiveGenerationError("archive database does not match its manifest")


def _inspect_archive(database: Path) -> _ArchiveInspection:
    require_no_reparse_points(database)
    if not database.is_file():
        raise ArchiveGenerationError("archive database is missing")
    _require_no_sidecars(database)
    before = _stable_file_snapshot(database)
    try:
        conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
        try:
            conn.execute("BEGIN")
            quick_check = _single_ok_check(conn, "quick_check")
            integrity_check = _single_ok_check(conn, "integrity_check")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ArchiveGenerationError("archive database has foreign-key violations")
            schema_rows = tuple(
                tuple(row)
                for row in conn.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                )
            )
            virtual_tables = tuple(
                str(row[1])
                for row in schema_rows
                if row[0] == "table"
                and isinstance(row[3], str)
                and row[3].lstrip().upper().startswith("CREATE VIRTUAL TABLE")
            )
            if virtual_tables:
                raise ArchiveGenerationError(
                    "immutable archive generations cannot contain virtual tables"
                )
            table_names = tuple(str(row[1]) for row in schema_rows if row[0] == "table")
            if not table_names:
                raise ArchiveGenerationError("archive database has no ordinary tables")
            tables = tuple(_table_commitment(conn, table) for table in table_names)
            conn.execute("COMMIT")
        finally:
            conn.close()
    except ArchiveGenerationError:
        raise
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise ArchiveGenerationError("archive database inspection failed") from exc
    _require_no_sidecars(database)
    after = _stable_file_snapshot(database)
    if after != before:
        raise ArchiveGenerationError("archive database changed while it was inspected")
    return _ArchiveInspection(
        database_size_bytes=after.size_bytes,
        database_sha256=after.sha256,
        schema_sha256=_canonical_sha256(schema_rows),
        quick_check=quick_check,
        integrity_check=integrity_check,
        foreign_key_violation_count=0,
        tables=tables,
    )


def _single_ok_check(conn: sqlite3.Connection, pragma: str) -> Literal["ok"]:
    rows = tuple(str(row[0]) for row in conn.execute(f"PRAGMA {pragma}"))  # nosec B608 -- fixed internal allowlist
    if rows != ("ok",):
        raise ArchiveGenerationError(f"archive database {pragma} failed")
    return "ok"


def _table_commitment(
    conn: sqlite3.Connection,
    table_name: str,
) -> ArchiveTableCommitment:
    quoted_table = _quote_identifier(table_name)
    info = tuple(conn.execute(f"PRAGMA table_info({quoted_table})"))  # nosec B608 -- schema-derived identifier is quoted
    columns = tuple(str(row[1]) for row in info)
    primary_key_rows = sorted(
        (row for row in info if int(row[5]) > 0),
        key=lambda row: int(row[5]),
    )
    primary_key_columns = tuple(str(row[1]) for row in primary_key_rows)
    if not columns or not primary_key_columns:
        raise ArchiveGenerationError(f"archive table {table_name!r} lacks a stable primary key")
    null_predicate = " OR ".join(
        f"{_quote_identifier(column)} IS NULL" for column in primary_key_columns
    )
    if (
        conn.execute(
            f"SELECT 1 FROM {quoted_table} WHERE {null_predicate} LIMIT 1"  # nosec B608 -- schema-derived identifiers are quoted
        ).fetchone()
        is not None
    ):
        raise ArchiveGenerationError(
            f"archive table {table_name!r} has a null primary-key component"
        )
    order = ",".join(_quote_identifier(column) for column in primary_key_columns)
    cursor = conn.execute(
        f"SELECT * FROM {quoted_table} ORDER BY {order}"  # nosec B608 -- schema-derived identifiers are quoted
    )
    digest = hashlib.sha256()
    _update_framed(digest, _canonical_bytes({"columns": columns}))
    row_count = 0
    while rows := cursor.fetchmany(_ROW_FETCH_SIZE):
        for row in rows:
            _update_framed(digest, _canonical_row(tuple(row)))
            row_count += 1
    return ArchiveTableCommitment(
        table_name=table_name,
        columns=columns,
        primary_key_columns=primary_key_columns,
        row_count=row_count,
        content_sha256=digest.hexdigest(),
    )


def _canonical_row(row: tuple[object, ...]) -> bytes:
    values: list[dict[str, object]] = []
    for value in row:
        if value is None:
            values.append({"type": "null"})
        elif isinstance(value, bool):
            values.append({"type": "integer", "value": int(value)})
        elif isinstance(value, int):
            values.append({"type": "integer", "value": value})
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ArchiveGenerationError("archive contains a non-finite numeric value")
            values.append({"type": "real", "value": value.hex()})
        elif isinstance(value, str):
            values.append({"type": "text", "value": value})
        elif isinstance(value, bytes):
            values.append({"type": "blob", "value": value.hex()})
        else:
            raise ArchiveGenerationError(
                f"archive contains unsupported SQLite value type {type(value).__name__}"
            )
    return _canonical_bytes(values)


def _update_framed(digest: _Digest, payload: bytes) -> None:
    digest.update(len(payload).to_bytes(8, byteorder="big", signed=False))
    digest.update(payload)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _stable_file_snapshot(path: Path) -> _FileSnapshot:
    require_no_reparse_points(path)
    lexical_before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ArchiveGenerationError("archive database is not a regular file")
        if _stat_identity(lexical_before)[:2] != _stat_identity(before)[:2]:
            raise ArchiveGenerationError("archive path changed before its handle was pinned")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    lexical_after = path.lstat()
    if _stat_identity(before) != _stat_identity(after):
        raise ArchiveGenerationError("archive database changed while it was hashed")
    if _stat_identity(lexical_after)[:2] != _stat_identity(after)[:2]:
        raise ArchiveGenerationError("archive path changed while it was hashed")
    return _FileSnapshot(
        device=int(after.st_dev),
        inode=int(after.st_ino),
        size_bytes=int(after.st_size),
        modified_time_ns=int(after.st_mtime_ns),
        changed_time_ns=int(after.st_ctime_ns),
        sha256=digest.hexdigest(),
    )


def _require_no_sidecars(database: Path) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = Path(f"{database}{suffix}")
        require_no_reparse_points(sidecar)
        if sidecar.exists():
            raise ArchiveGenerationError(f"archive database has SQLite sidecar {sidecar.name!r}")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )
