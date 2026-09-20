"""Typed enumeration and local-byte resolution for evidence-native documents.

Evidence-native capture stores immutable bytes by digest and may never create a
row in the legacy ``documents`` table.  This reader therefore advances by the
append-only evidence table's SQLite rowid and derives format/source metadata
from the evidence ledger itself.  It performs no network access.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True, slots=True)
class VerifiedLocalEvidenceBytes:
    """Bytes read from a verified local source or immutable replica ledger row."""

    path: Path
    storage_uri: str
    raw_bytes: bytes
    location_observation_id: str | None
    used_replica: bool


class LocalEvidenceReadError(RuntimeError):
    """A stable degradation reason for a local evidence read."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class EvidenceNativeDocumentCandidate(BaseModel):
    """Closed read boundary for one immutable evidence document version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_rowid: int = Field(gt=0)
    document_version_id: str = Field(min_length=1, max_length=128)
    blob_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)
    storage_uri: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    recorded_at: datetime


def select_evidence_native_candidates(
    conn: sqlite3.Connection,
    *,
    after_rowid: int,
    batch_size: int,
    pdf_only: bool = False,
) -> list[EvidenceNativeDocumentCandidate]:
    """Return a bounded append-order batch with a deterministic local replica.

    Every legacy-free document remains enumerable even when no verified local
    replica exists.  In that case the blob's original storage URI is retained
    so extraction can quarantine it explicitly instead of silently shrinking
    coverage.
    """

    _require_schema(conn)
    rows = conn.execute(
        "SELECT document.rowid AS evidence_rowid, document.document_version_id, "
        "lower(document.blob_sha256) AS blob_sha256, blob.byte_size, blob.media_type, "
        "COALESCE(("
        "SELECT location.storage_uri FROM v_evidence_blob_locations_current AS location "
        "WHERE location.blob_sha256 = document.blob_sha256 "
        "AND location.location_kind = 'local' AND location.availability_state = 'present' "
        "AND location.verified_sha256 = document.blob_sha256 "
        "AND location.verified_byte_size = blob.byte_size "
        "ORDER BY location.verified_at DESC, location.storage_uri LIMIT 1"
        "), blob.storage_uri) AS storage_uri, observation.source_url AS source_ref, "
        "document.recorded_at "
        "FROM evidence_document_versions AS document "
        "JOIN evidence_content_blobs AS blob ON blob.sha256 = document.blob_sha256 "
        "JOIN evidence_source_observations AS observation "
        "ON observation.observation_id = document.observation_id "
        "WHERE document.legacy_document_id IS NULL AND document.rowid > ? "
        "AND (? = 0 OR lower(blob.media_type) = 'application/pdf' "
        "OR lower(observation.source_url) LIKE '%.pdf' "
        "OR lower(observation.source_url) LIKE '%.pdf?%' "
        "OR lower(observation.source_url) LIKE '%.pdf#%') "
        "ORDER BY document.rowid LIMIT ?",
        (after_rowid, pdf_only, batch_size),
    ).fetchall()
    return [
        EvidenceNativeDocumentCandidate(
            evidence_rowid=_integer(row["evidence_rowid"], "evidence document rowid"),
            document_version_id=_text(row["document_version_id"], "document_version_id"),
            blob_sha256=_text(row["blob_sha256"], "blob_sha256").lower(),
            byte_size=_nonnegative_integer(row["byte_size"], "byte_size"),
            media_type=_text(row["media_type"], "media_type"),
            storage_uri=_text(row["storage_uri"], "storage_uri"),
            source_ref=_text(row["source_ref"], "source_ref"),
            recorded_at=_datetime(row["recorded_at"], "recorded_at"),
        )
        for row in rows
    ]


def has_evidence_native_after(
    conn: sqlite3.Connection, after_rowid: int, *, pdf_only: bool = False
) -> bool:
    """Return whether another legacy-free append exists after the cursor."""

    return (
        conn.execute(
            "SELECT 1 FROM evidence_document_versions AS document "
            "JOIN evidence_content_blobs AS blob ON blob.sha256 = document.blob_sha256 "
            "JOIN evidence_source_observations AS observation "
            "ON observation.observation_id = document.observation_id "
            "WHERE document.legacy_document_id IS NULL AND document.rowid > ? "
            "AND (? = 0 OR lower(blob.media_type) = 'application/pdf' "
            "OR lower(observation.source_url) LIKE '%.pdf' "
            "OR lower(observation.source_url) LIKE '%.pdf?%' "
            "OR lower(observation.source_url) LIKE '%.pdf#%') LIMIT 1",
            (after_rowid, pdf_only),
        ).fetchone()
        is not None
    )


def select_evidence_native_candidates_by_id(
    conn: sqlite3.Connection,
    *,
    document_version_ids: tuple[str, ...],
) -> list[EvidenceNativeDocumentCandidate]:
    """Resolve an explicit, bounded set of evidence-native document versions."""

    _require_schema(conn)
    if not document_version_ids:
        return []
    if len(document_version_ids) != len(set(document_version_ids)):
        raise ValueError("document version IDs must be unique")
    placeholders = ", ".join("?" for _ in document_version_ids)
    rows = conn.execute(
        "SELECT document.rowid AS evidence_rowid, document.document_version_id, "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "lower(document.blob_sha256) AS blob_sha256, blob.byte_size, blob.media_type, "
        "COALESCE((SELECT location.storage_uri "
        "FROM v_evidence_blob_locations_current AS location "
        "WHERE location.blob_sha256 = document.blob_sha256 "
        "AND location.location_kind = 'local' "
        "AND location.availability_state = 'present' "
        "AND location.verified_sha256 = document.blob_sha256 "
        "AND location.verified_byte_size = blob.byte_size "
        "ORDER BY location.verified_at DESC, location.storage_uri LIMIT 1"
        "), blob.storage_uri) AS storage_uri, observation.source_url AS source_ref, "
        "document.recorded_at "
        "FROM evidence_document_versions AS document "
        "JOIN evidence_content_blobs AS blob ON blob.sha256 = document.blob_sha256 "
        "JOIN evidence_source_observations AS observation "
        "ON observation.observation_id = document.observation_id "
        f"WHERE document.legacy_document_id IS NULL "
        f"AND document.document_version_id IN ({placeholders}) "
        "ORDER BY document.rowid",
        document_version_ids,
    ).fetchall()
    candidates = [
        EvidenceNativeDocumentCandidate(
            evidence_rowid=_integer(row["evidence_rowid"], "evidence document rowid"),
            document_version_id=_text(row["document_version_id"], "document_version_id"),
            blob_sha256=_text(row["blob_sha256"], "blob_sha256").lower(),
            byte_size=_nonnegative_integer(row["byte_size"], "byte_size"),
            media_type=_text(row["media_type"], "media_type"),
            storage_uri=_text(row["storage_uri"], "storage_uri"),
            source_ref=_text(row["source_ref"], "source_ref"),
            recorded_at=_datetime(row["recorded_at"], "recorded_at"),
        )
        for row in rows
    ]
    found = {candidate.document_version_id for candidate in candidates}
    if missing := sorted(set(document_version_ids) - found):
        raise ValueError("evidence-native document versions not found: " + ", ".join(missing))
    return candidates


def resolve_local_storage_uri(storage_uri: str, *, allowed_roots: tuple[Path, ...]) -> Path | None:
    """Resolve a local path only when it stays inside an explicit allowed root."""

    if re.match(r"^[A-Za-z]:[\\/]", storage_uri):
        candidate = Path(storage_uri)
    else:
        parsed = urlparse(storage_uri)
        if parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"}:
                return None
            decoded = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:", decoded):
                decoded = decoded[1:]
            candidate = Path(decoded)
        elif parsed.scheme:
            return None
        else:
            candidate = Path(storage_uri.partition("#")[0])

    normalized_roots = tuple(root.resolve() for root in allowed_roots)
    if not normalized_roots:
        raise ValueError("at least one allowed content root is required")
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (normalized_roots[0] / candidate).resolve()
    for root in normalized_roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return resolved
    return None


def read_verified_local_evidence_bytes(
    conn: sqlite3.Connection,
    *,
    storage_uri: str,
    expected_sha256: str,
    expected_byte_size: int | None,
    allowed_roots: tuple[Path, ...],
    legacy_document_id: int | None = None,
    document_version_id: str | None = None,
) -> VerifiedLocalEvidenceBytes:
    """Read an exact local input, then an admitted exact replica if it changed.

    The initial URI is the document's historical physical alias and is usable
    only after this read verifies its bytes.  A fallback may only be a current
    ``present`` local location already registered for the same immutable
    document version and blob.  This deliberately does not scan the filesystem
    or promote a matching file into the evidence ledger.
    """

    if legacy_document_id is not None and document_version_id is not None:
        raise ValueError("provide at most one immutable document identity")
    if legacy_document_id is not None and legacy_document_id <= 0:
        raise ValueError("legacy_document_id must be positive")
    if document_version_id is not None and not document_version_id:
        raise ValueError("document_version_id must be non-empty")
    expected_sha256 = _sha256(expected_sha256, "expected_sha256")
    if expected_byte_size is not None and (
        isinstance(expected_byte_size, bool) or expected_byte_size < 0
    ):
        raise ValueError("expected_byte_size must be a non-negative integer")

    primary_error: LocalEvidenceReadError
    try:
        return _read_exact_local_bytes(
            storage_uri=storage_uri,
            expected_sha256=expected_sha256,
            expected_byte_size=expected_byte_size,
            allowed_roots=allowed_roots,
            location_observation_id=None,
            used_replica=False,
        )
    except LocalEvidenceReadError as error:
        primary_error = error

    if legacy_document_id is None and document_version_id is None:
        raise primary_error
    for location_id, replica_uri, replica_size in _verified_local_replicas(
        conn,
        expected_sha256=expected_sha256,
        expected_byte_size=expected_byte_size,
        legacy_document_id=legacy_document_id,
        document_version_id=document_version_id,
    ):
        try:
            return _read_exact_local_bytes(
                storage_uri=replica_uri,
                expected_sha256=expected_sha256,
                expected_byte_size=replica_size,
                allowed_roots=allowed_roots,
                location_observation_id=location_id,
                used_replica=True,
            )
        except LocalEvidenceReadError:
            # A historical verification observation is not a substitute for a
            # fresh byte read.  Continue only through bounded current rows.
            continue
    raise primary_error


def _read_exact_local_bytes(
    *,
    storage_uri: str,
    expected_sha256: str,
    expected_byte_size: int | None,
    allowed_roots: tuple[Path, ...],
    location_observation_id: str | None,
    used_replica: bool,
) -> VerifiedLocalEvidenceBytes:
    try:
        path = resolve_local_storage_uri(storage_uri, allowed_roots=allowed_roots)
        if path is None:
            raise LocalEvidenceReadError("storage_uri_not_allowed_local_file")
        if not path.is_file():
            raise LocalEvidenceReadError("content_missing")
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise LocalEvidenceReadError("content_unreadable") from error
    if hashlib.sha256(raw_bytes).hexdigest() != expected_sha256:
        raise LocalEvidenceReadError("sha256_mismatch")
    if expected_byte_size is not None and len(raw_bytes) != expected_byte_size:
        raise LocalEvidenceReadError("byte_size_mismatch")
    return VerifiedLocalEvidenceBytes(
        path=path,
        storage_uri=path.as_uri(),
        raw_bytes=raw_bytes,
        location_observation_id=location_observation_id,
        used_replica=used_replica,
    )


def _verified_local_replicas(
    conn: sqlite3.Connection,
    *,
    expected_sha256: str,
    expected_byte_size: int | None,
    legacy_document_id: int | None,
    document_version_id: str | None,
) -> list[tuple[str, str, int]]:
    identity_clause = (
        "document.legacy_document_id = ?"
        if legacy_document_id is not None
        else "document.document_version_id = ?"
    )
    identity_value: object = (
        legacy_document_id if legacy_document_id is not None else document_version_id
    )
    rows = conn.execute(
        "SELECT location.location_observation_id, location.storage_uri, blob.byte_size "
        "FROM evidence_document_versions AS document "
        "JOIN evidence_content_blobs AS blob ON blob.sha256 = document.blob_sha256 "
        "JOIN v_evidence_blob_locations_current AS location "
        "ON location.blob_sha256 = document.blob_sha256 "
        "WHERE " + identity_clause + " AND lower(document.blob_sha256) = ? "
        "AND lower(location.blob_sha256) = ? "
        "AND location.location_kind = 'local' "
        "AND location.availability_state = 'present' "
        "AND lower(location.verified_sha256) = ? "
        "AND location.verified_byte_size = blob.byte_size "
        "AND (? IS NULL OR blob.byte_size = ?) "
        "ORDER BY location.verified_at DESC, location.storage_uri",
        (
            identity_value,
            expected_sha256,
            expected_sha256,
            expected_sha256,
            expected_byte_size,
            expected_byte_size,
        ),
    ).fetchall()
    result: list[tuple[str, str, int]] = []
    for row in rows:
        location_id, replica_uri, replica_size = row
        if (
            not isinstance(location_id, str)
            or not location_id
            or not isinstance(replica_uri, str)
            or not replica_uri
            or isinstance(replica_size, bool)
            or not isinstance(replica_size, int)
            or replica_size < 0
        ):
            raise RuntimeError("verified local replica metadata is invalid")
        result.append((location_id, replica_uri, replica_size))
    return result


def _sha256(value: str, name: str) -> str:
    normalized = value.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return normalized


def _require_schema(conn: sqlite3.Connection) -> None:
    required = {
        "evidence_content_blobs",
        "evidence_source_observations",
        "evidence_document_versions",
    }
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if missing := sorted(required - tables):
        raise RuntimeError("Evidence-native extraction schema is incomplete: " + ", ".join(missing))
    view = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'view' AND name = 'v_evidence_blob_locations_current'"
    ).fetchone()
    if view is None:
        raise RuntimeError("Evidence-native extraction requires v_evidence_blob_locations_current")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} must be non-empty text")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{name} must be a non-negative integer")
    return value


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError(f"{name} must be an ISO-8601 datetime") from error
    raise RuntimeError(f"{name} must be a datetime")
