"""Immutable replicas and source-retrieval links for canonical evidence.

``evidence_content_blobs`` names bytes, not a single storage location.  This
module records independently verified locations as revision chains and lets a
logical document version retain every retrieval observation that returned its
exact bytes.  It deliberately does not update the 0213 primary fields: those
remain a backward-compatible first observation while this ledger provides the
complete history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LocationKind: TypeAlias = Literal["local", "object", "archive", "mirror"]
AvailabilityState: TypeAlias = Literal["present", "missing", "quarantined"]
DocumentObservationLinkKind: TypeAlias = Literal["primary", "retrieval", "mirror"]
_SHA256_LENGTH = 64


def _validate_sha256(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != _SHA256_LENGTH or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


def _validate_optional_sha256(value: str | None) -> str | None:
    return None if value is None else _validate_sha256(value)


class _LinkRecord(BaseModel):
    """Closed, immutable records accepted by this persistence boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BlobLocationObservation(_LinkRecord):
    """One immutable verification of one replica location for known bytes."""

    location_observation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    blob_sha256: str
    storage_uri: str = Field(min_length=1)
    location_kind: LocationKind
    availability_state: AvailabilityState
    location_sequence: int = Field(gt=0)
    verified_at: datetime
    verified_byte_size: int | None = Field(default=None, ge=0)
    verified_sha256: str | None = None
    supersedes_location_observation_id: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    recorded_at: datetime

    _blob_sha256 = field_validator("blob_sha256")(_validate_sha256)
    _verified_sha256 = field_validator("verified_sha256")(_validate_optional_sha256)

    @model_validator(mode="after")
    def _validate_revision_and_clocks(self) -> Self:
        if self.location_sequence == 1 and self.supersedes_location_observation_id is not None:
            raise ValueError("first location revision cannot supersede another observation")
        if self.location_sequence > 1 and self.supersedes_location_observation_id is None:
            raise ValueError("later location revisions must supersede the prior observation")
        if self.verified_sha256 is not None and self.verified_sha256 != self.blob_sha256:
            raise ValueError("verified_sha256 must match blob_sha256")
        if self.recorded_at < self.verified_at:
            raise ValueError("recorded_at must not precede verified_at")
        return self


class DocumentObservationLink(_LinkRecord):
    """One append-only association of a document version to a retrieval."""

    link_id: str = Field(min_length=1, max_length=128)
    document_version_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    link_kind: DocumentObservationLinkKind
    linked_at: datetime


@dataclass(frozen=True, slots=True)
class PersistResult:
    """Identity and creation status returned by one append-only write."""

    record_id: str
    created: bool


def _matches_stored_values(existing: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    """Compare SQLite values with its timestamp serialization normalized."""
    if len(existing) != len(expected):
        return False
    for stored, supplied in zip(existing, expected, strict=True):
        if isinstance(supplied, datetime):
            try:
                stored_time = datetime.fromisoformat(str(stored)).replace(tzinfo=None)
            except ValueError:
                return False
            if stored_time != supplied.replace(tzinfo=None):
                return False
        elif stored != supplied:
            return False
    return True


class EvidenceLinkLedger:
    """Typed, exact-replay-only writer for replica and retrieval-link history."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist_location(self, location: BlobLocationObservation) -> PersistResult:
        """Append a verified location state or accept an exact replay."""
        # ``model_copy`` intentionally skips Pydantic validation.  Revalidate
        # at the persistence boundary so even a copied immutable record cannot
        # bypass the closed revision/hash contract.
        location = BlobLocationObservation.model_validate(location.model_dump())
        return self._persist_one(
            table="evidence_blob_location_observations",
            columns=(
                "location_observation_id",
                "idempotency_key",
                "blob_sha256",
                "storage_uri",
                "location_kind",
                "availability_state",
                "location_sequence",
                "verified_at",
                "verified_byte_size",
                "verified_sha256",
                "supersedes_location_observation_id",
                "recorded_at",
            ),
            values=(
                location.location_observation_id,
                location.idempotency_key,
                location.blob_sha256,
                location.storage_uri,
                location.location_kind,
                location.availability_state,
                location.location_sequence,
                location.verified_at,
                location.verified_byte_size,
                location.verified_sha256,
                location.supersedes_location_observation_id,
                location.recorded_at,
            ),
            identity_column="idempotency_key",
            identity_value=location.idempotency_key,
            record_id=location.location_observation_id,
        )

    def persist_link(self, link: DocumentObservationLink) -> PersistResult:
        """Append a source observation link after database byte-identity checks."""
        return self._persist_one(
            table="evidence_document_observation_links",
            columns=("link_id", "document_version_id", "observation_id", "link_kind", "linked_at"),
            values=(
                link.link_id,
                link.document_version_id,
                link.observation_id,
                link.link_kind,
                link.linked_at,
            ),
            identity_column="link_id",
            identity_value=link.link_id,
            record_id=link.link_id,
        )

    def _persist_one(
        self,
        *,
        table: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        identity_column: str,
        identity_value: str,
        record_id: str,
    ) -> PersistResult:
        placeholders = ", ".join("?" for _ in columns)
        cursor = self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",  # nosec B608 -- trusted internal SQL shape; values remain bound
            values,
        )
        if cursor.rowcount == 1:
            return PersistResult(record_id=record_id, created=True)
        existing = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {identity_column} = ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
            (identity_value,),
        ).fetchone()
        if existing is None or not _matches_stored_values(tuple(existing), values):
            raise ValueError(
                f"immutable {table} identity {identity_value!r} conflicts with existing data"
            )
        return PersistResult(record_id=record_id, created=False)
