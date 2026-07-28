"""Verify that one extraction is represented by a complete, sealed search index.

Lexical and vector indexes deliberately use different physical representations:
SQLite FTS5 is populated by a trigger from ``search_chunks`` while vector
indexes record explicit ``search_index_memberships``.  Coverage may claim an
index only after the representation appropriate to the run kind is verified.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from search.corpus_builder import lexical_index_config_sha256

IndexKind = Literal["lexical", "vector"]


class SearchProjectionSeal(BaseModel):
    """Immutable commitment to one exact physical search projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_seal_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    index_run_id: str = Field(min_length=1, max_length=128)
    manifest_id: str = Field(min_length=1, max_length=128)
    index_kind: IndexKind
    chunk_count: int = Field(ge=0)
    chunk_set_sha256: str
    projection_records_sha256: str
    artifact_set_sha256: str | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    dimensions: int | None = Field(default=None, gt=0)
    runtime_artifact_sha256: str | None = None
    config_sha256: str
    storage_uri: str = Field(min_length=1)
    sealed_at: datetime

    @field_validator(
        "chunk_set_sha256",
        "projection_records_sha256",
        "artifact_set_sha256",
        "config_sha256",
        "runtime_artifact_sha256",
    )
    @classmethod
    def _sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("projection seal hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _backend_contract(self) -> SearchProjectionSeal:
        vector_fields = (
            self.artifact_set_sha256,
            self.provider,
            self.model,
            self.dimensions,
        )
        if self.index_kind == "vector" and any(value is None for value in vector_fields):
            raise ValueError("vector projection seals require artifact and model identity")
        if self.index_kind == "vector" and self.chunk_count == 0:
            raise ValueError("vector projection seals require at least one chunk")
        if self.index_kind == "lexical" and (
            any(value is not None for value in vector_fields)
            or self.runtime_artifact_sha256 is not None
        ):
            raise ValueError("lexical projection seals cannot claim vector model identity")
        return self


class SealedIndexLineage(BaseModel):
    """Auditable successful-index identity for one extracted document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_id: str = Field(min_length=1, max_length=128)
    index_run_id: str = Field(min_length=1, max_length=128)
    index_kind: IndexKind
    config_sha256: str = Field(min_length=64, max_length=64)
    code_version: str = Field(min_length=1, max_length=255)
    completed_at: datetime


def manifest_chunk_commitment(conn: sqlite3.Connection, *, manifest_id: str) -> tuple[int, str]:
    """Commit to the exact ordered chunk identity and content set."""

    rows = conn.execute(
        "SELECT chunk_id, content_sha256 FROM search_chunks "
        "WHERE manifest_id = ? ORDER BY chunk_id",
        (manifest_id,),
    ).fetchall()
    return len(rows), _digest_rows(rows)


def vector_artifact_commitment(conn: sqlite3.Connection, *, index_run_id: str) -> tuple[int, str]:
    """Commit to every successful embedding artifact and its immutable metadata."""

    rows = conn.execute(
        "SELECT chunk_id, purpose, provider, model, dimensions, vector_sha256, "
        "storage_uri, input_sha256, request_config_sha256, runtime_artifact_sha256 "
        "FROM search_embedding_artifacts "
        "WHERE index_run_id = ? AND outcome = 'succeeded' ORDER BY chunk_id",
        (index_run_id,),
    ).fetchall()
    bound = [row[-1] is not None for row in rows]
    if any(bound) and not all(bound):
        raise RuntimeError("vector artifact set mixes runtime-bound and legacy rows")
    if rows and not any(bound):
        rows = [row[:-1] for row in rows]
    return len(rows), _digest_rows(rows)


def lexical_projection_commitment(conn: sqlite3.Connection, *, manifest_id: str) -> tuple[int, str]:
    """Commit to the exact persisted FTS rows, including indexed text."""

    rows = conn.execute(
        "SELECT lexical.chunk_id, lexical.text FROM search_lexical_chunks AS lexical "
        "JOIN search_chunks AS chunk ON chunk.chunk_id = lexical.chunk_id "
        "WHERE chunk.manifest_id = ? ORDER BY lexical.chunk_id, lexical.rowid",
        (manifest_id,),
    ).fetchall()
    return len(rows), _digest_rows(rows)


def load_projection_seal(
    conn: sqlite3.Connection, *, index_run_id: str
) -> SearchProjectionSeal | None:
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_projection_seals'"
        ).fetchone()
        is None
    ):
        return None
    row = conn.execute(
        "SELECT projection_seal_id, idempotency_key, index_run_id, manifest_id, "
        "index_kind, chunk_count, chunk_set_sha256, projection_records_sha256, "
        "artifact_set_sha256, provider, model, dimensions, config_sha256, "
        "storage_uri, sealed_at, runtime_artifact_sha256 "
        "FROM search_projection_seals WHERE index_run_id = ?",
        (index_run_id,),
    ).fetchone()
    if row is None:
        return None
    return SearchProjectionSeal(
        projection_seal_id=_text(row[0], "projection_seal_id"),
        idempotency_key=_text(row[1], "idempotency_key"),
        index_run_id=_text(row[2], "index_run_id"),
        manifest_id=_text(row[3], "manifest_id"),
        index_kind=_index_kind(row[4]),
        chunk_count=int(row[5]),
        chunk_set_sha256=_text(row[6], "chunk_set_sha256"),
        projection_records_sha256=_text(row[7], "projection_records_sha256"),
        artifact_set_sha256=None if row[8] is None else str(row[8]),
        provider=None if row[9] is None else str(row[9]),
        model=None if row[10] is None else str(row[10]),
        dimensions=None if row[11] is None else int(row[11]),
        config_sha256=_text(row[12], "config_sha256"),
        storage_uri=_text(row[13], "storage_uri"),
        sealed_at=_datetime(row[14], "sealed_at"),
        runtime_artifact_sha256=None if row[15] is None else str(row[15]),
    )


def persist_projection_seal(conn: sqlite3.Connection, seal: SearchProjectionSeal) -> bool:
    """Insert an append-only seal, or prove an idempotent replay."""

    columns = tuple(SearchProjectionSeal.model_fields)
    values = tuple(getattr(seal, column) for column in columns)
    existing = conn.execute(
        f"SELECT {', '.join(columns)} FROM search_projection_seals WHERE idempotency_key = ?",
        (seal.idempotency_key,),
    ).fetchone()
    if existing is not None:
        if not _same_sql(tuple(existing), values):
            raise ValueError("immutable search projection seal conflicts with existing data")
        return False
    conn.execute(
        f"INSERT INTO search_projection_seals ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        values,
    )
    return True


def verify_ledger_projection_seal(conn: sqlite3.Connection, seal: SearchProjectionSeal) -> None:
    """Recompute all SQL-side commitments and fail closed on drift."""

    run = conn.execute(
        "SELECT manifest_id, index_kind, config_sha256, outcome "
        "FROM search_index_runs WHERE index_run_id = ?",
        (seal.index_run_id,),
    ).fetchone()
    if run is None or tuple(str(value) for value in run) != (
        seal.manifest_id,
        seal.index_kind,
        seal.config_sha256,
        "succeeded",
    ):
        raise RuntimeError("projection seal no longer matches its successful index run")
    chunk_count, chunk_digest = manifest_chunk_commitment(conn, manifest_id=seal.manifest_id)
    if (chunk_count, chunk_digest) != (seal.chunk_count, seal.chunk_set_sha256):
        raise RuntimeError("projection seal chunk commitment no longer matches manifest")
    if seal.index_kind == "vector":
        artifact_count, artifact_digest = vector_artifact_commitment(
            conn, index_run_id=seal.index_run_id
        )
        if (artifact_count, artifact_digest) != (
            seal.chunk_count,
            seal.artifact_set_sha256,
        ):
            raise RuntimeError("projection seal artifact commitment no longer matches ledger")
        included = conn.execute(
            "SELECT COUNT(*) FROM search_index_memberships "
            "WHERE index_run_id = ? AND membership_status = 'included'",
            (seal.index_run_id,),
        ).fetchone()
        if included is None or int(included[0]) != seal.chunk_count:
            raise RuntimeError("projection seal membership coverage is incomplete")
        runtime_rows = conn.execute(
            "SELECT DISTINCT runtime_artifact_sha256 FROM search_embedding_artifacts "
            "WHERE index_run_id = ? AND outcome = 'succeeded'",
            (seal.index_run_id,),
        ).fetchall()
        runtime_digests = {None if row[0] is None else str(row[0]) for row in runtime_rows}
        if runtime_digests != {seal.runtime_artifact_sha256}:
            raise RuntimeError("projection seal runtime artifact differs from vector rows")
    else:
        lexical_count, lexical_digest = lexical_projection_commitment(
            conn, manifest_id=seal.manifest_id
        )
        if (lexical_count, lexical_digest) != (
            seal.chunk_count,
            seal.projection_records_sha256,
        ):
            raise RuntimeError("lexical projection seal no longer matches FTS rows")


def sealed_index_lineage(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    extraction_run_id: str,
    index_kinds: tuple[IndexKind, ...] = ("lexical", "vector"),
) -> SealedIndexLineage | None:
    """Return the newest representation-complete index, if one exists."""

    if not index_kinds:
        raise ValueError("at least one index kind is required")
    if len(index_kinds) != len(set(index_kinds)):
        raise ValueError("index kinds must be unique")
    placeholders = ", ".join("?" for _ in index_kinds)
    rows = conn.execute(
        "SELECT run.manifest_id, run.index_run_id, run.index_kind, "
        "run.config_sha256, run.code_version, run.completed_at "
        "FROM search_corpus_document_memberships AS membership "
        "JOIN search_corpus_manifest_seals AS seal "
        "ON seal.manifest_id = membership.manifest_id "
        "JOIN v_search_index_successful AS run "
        "ON run.manifest_id = membership.manifest_id "
        "WHERE membership.document_version_id = ? "
        "AND membership.membership_status = 'included' "
        "AND seal.completion_status = 'complete' "
        f"AND run.index_kind IN ({placeholders}) "
        "AND EXISTS (SELECT 1 FROM search_chunks AS chunk "
        "JOIN evidence_nodes AS node ON node.node_id = chunk.evidence_node_id "
        "WHERE chunk.manifest_id = run.manifest_id "
        "AND node.extraction_run_id = ?) "
        "ORDER BY run.completed_at DESC, run.index_run_id DESC",
        (document_version_id, *index_kinds, extraction_run_id),
    ).fetchall()
    for row in rows:
        lineage = SealedIndexLineage(
            manifest_id=_text(row[0], "manifest_id"),
            index_run_id=_text(row[1], "index_run_id"),
            index_kind=_index_kind(row[2]),
            config_sha256=_text(row[3], "config_sha256"),
            code_version=_text(row[4], "code_version"),
            completed_at=_datetime(row[5], "completed_at"),
        )
        projection_seal = load_projection_seal(conn, index_run_id=lineage.index_run_id)
        if projection_seal is None:
            continue
        verify_ledger_projection_seal(conn, projection_seal)
        if lineage.index_kind == "lexical":
            if _lexical_representation_complete(
                conn,
                manifest_id=lineage.manifest_id,
                extraction_run_id=extraction_run_id,
            ):
                expected_config = lexical_index_config_sha256(
                    conn,
                    manifest_id=lineage.manifest_id,
                )
                if lineage.config_sha256 != expected_config:
                    raise RuntimeError(
                        "successful lexical index config does not commit to its "
                        f"sealed chunk set: {lineage.index_run_id}"
                    )
                return lineage
        elif _vector_representation_complete(
            conn,
            manifest_id=lineage.manifest_id,
            index_run_id=lineage.index_run_id,
            extraction_run_id=extraction_run_id,
        ):
            return lineage
    return None


def _lexical_representation_complete(
    conn: sqlite3.Connection,
    *,
    manifest_id: str,
    extraction_run_id: str,
) -> bool:
    missing = conn.execute(
        "SELECT 1 FROM search_chunks AS chunk "
        "JOIN evidence_nodes AS node ON node.node_id = chunk.evidence_node_id "
        "WHERE chunk.manifest_id = ? AND node.extraction_run_id = ? "
        "AND ((SELECT COUNT(*) FROM search_lexical_chunks AS lexical "
        "WHERE lexical.chunk_id = chunk.chunk_id) <> 1 "
        "OR NOT EXISTS (SELECT 1 FROM search_lexical_chunks AS lexical "
        "WHERE lexical.chunk_id = chunk.chunk_id AND lexical.text = chunk.text)) "
        "LIMIT 1",
        (manifest_id, extraction_run_id),
    ).fetchone()
    return missing is None


def _vector_representation_complete(
    conn: sqlite3.Connection,
    *,
    manifest_id: str,
    index_run_id: str,
    extraction_run_id: str,
) -> bool:
    missing = conn.execute(
        "SELECT 1 FROM search_chunks AS chunk "
        "JOIN evidence_nodes AS node ON node.node_id = chunk.evidence_node_id "
        "LEFT JOIN search_index_memberships AS membership "
        "ON membership.index_run_id = ? AND membership.chunk_id = chunk.chunk_id "
        "WHERE chunk.manifest_id = ? AND node.extraction_run_id = ? "
        "AND (membership.chunk_id IS NULL OR membership.membership_status <> 'included') "
        "LIMIT 1",
        (index_run_id, manifest_id, extraction_run_id),
    ).fetchone()
    return missing is None


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} must be non-empty text")
    return value


def _index_kind(value: object) -> IndexKind:
    if value == "lexical":
        return "lexical"
    if value == "vector":
        return "vector"
    raise RuntimeError("index_kind must be lexical or vector")


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError(f"{name} must be ISO-8601") from error
    raise RuntimeError(f"{name} must be a datetime")


def _digest_rows(rows: list[tuple[object, ...]]) -> str:
    canonical = [
        [value.isoformat() if isinstance(value, datetime) else value for value in row]
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _same_sql(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    def normalized(value: object) -> object:
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value

    return tuple(normalized(value) for value in left) == tuple(normalized(value) for value in right)
