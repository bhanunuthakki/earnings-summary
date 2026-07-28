"""Typed enumeration and local-byte resolution for evidence-native documents.

Evidence-native capture stores immutable bytes by digest and may never create a
row in the legacy ``documents`` table.  This reader therefore advances by the
append-only evidence table's SQLite rowid and derives format/source metadata
from the evidence ledger itself.  It performs no network access.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field


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
        "SELECT document.rowid AS evidence_rowid, document.document_version_id, "
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
            candidate = Path(storage_uri)

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
