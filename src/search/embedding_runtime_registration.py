"""Append-only registration of reproducible embedding runtimes.

A registration authorizes local bytes to produce evaluation candidates.  It is
deliberately not a routing decision: only an evaluated, owner-approved
``EmbeddingPromotion`` may activate a runtime.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from search.embedding_runtime_artifact import EmbeddingRuntimeArtifact

PURPOSE = "evidence_vector_retrieval"


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("runtime registration hashes must be lowercase SHA-256")
    return value


class EmbeddingRuntimeRegistration(BaseModel):
    """An inert, content-addressed candidate-runtime registration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_registration_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    purpose: str = Field(default=PURPOSE, min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    dimensions: int = Field(gt=0)
    runtime_artifact_json: str = Field(min_length=2)
    runtime_artifact_sha256: str
    registered_at: datetime

    _runtime_sha = field_validator("runtime_artifact_sha256")(_sha256)

    @model_validator(mode="after")
    def _artifact_contract(self) -> EmbeddingRuntimeRegistration:
        artifact = EmbeddingRuntimeArtifact.model_validate_json(self.runtime_artifact_json)
        if artifact.canonical_json() != self.runtime_artifact_json:
            raise ValueError("runtime registration artifact JSON is not canonical")
        if artifact.sha256() != self.runtime_artifact_sha256:
            raise ValueError("runtime registration artifact digest differs")
        if (artifact.provider, artifact.model, artifact.dimensions) != (
            self.provider,
            self.model,
            self.dimensions,
        ):
            raise ValueError("runtime registration coordinate differs from artifact")
        expected = runtime_registration_identity(
            purpose=self.purpose,
            runtime_artifact_sha256=self.runtime_artifact_sha256,
        )
        if self.runtime_registration_id != expected or self.idempotency_key != expected:
            raise ValueError("runtime registration identity is not content-derived")
        return self


def runtime_registration_identity(*, purpose: str, runtime_artifact_sha256: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "purpose": purpose,
                "runtime_artifact_sha256": _sha256(runtime_artifact_sha256),
                "version": "embedding-runtime-registration.v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"embedding-runtime:{digest}"


def registration_from_artifact(
    artifact: EmbeddingRuntimeArtifact,
    *,
    registered_at: datetime,
    purpose: str = PURPOSE,
) -> EmbeddingRuntimeRegistration:
    artifact_json = artifact.canonical_json()
    artifact_sha = artifact.sha256()
    identity = runtime_registration_identity(
        purpose=purpose,
        runtime_artifact_sha256=artifact_sha,
    )
    return EmbeddingRuntimeRegistration(
        runtime_registration_id=identity,
        idempotency_key=identity,
        purpose=purpose,
        provider=artifact.provider,
        model=artifact.model,
        dimensions=artifact.dimensions,
        runtime_artifact_json=artifact_json,
        runtime_artifact_sha256=artifact_sha,
        registered_at=registered_at,
    )


def persist_runtime_registration(
    conn: sqlite3.Connection,
    registration: EmbeddingRuntimeRegistration,
) -> bool:
    """Insert a registration or prove that an exact replay already exists."""

    register_embedding_governance_functions(conn)
    columns = tuple(EmbeddingRuntimeRegistration.model_fields)
    values = tuple(getattr(registration, column) for column in columns)
    existing = conn.execute(
        f"SELECT {', '.join(columns)} FROM search_embedding_runtime_registrations "  # nosec B608 -- fixed typed columns; values remain bound
        "WHERE runtime_registration_id=? OR idempotency_key=?",
        (registration.runtime_registration_id, registration.idempotency_key),
    ).fetchall()
    if existing:
        if len(existing) != 1 or not _same_sql(tuple(existing[0]), values):
            raise ValueError("immutable embedding runtime registration replay conflict")
        return False
    conn.execute(
        f"INSERT INTO search_embedding_runtime_registrations ({', '.join(columns)}) "  # nosec B608 -- fixed typed columns; values remain bound
        f"VALUES ({', '.join('?' for _ in columns)})",
        values,
    )
    return True


def load_runtime_registration(
    conn: sqlite3.Connection,
    runtime_registration_id: str,
) -> EmbeddingRuntimeRegistration | None:
    columns = tuple(EmbeddingRuntimeRegistration.model_fields)
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM search_embedding_runtime_registrations "  # nosec B608 -- fixed typed columns; value remains bound
        "WHERE runtime_registration_id=?",
        (runtime_registration_id,),
    ).fetchone()
    if row is None:
        return None
    return EmbeddingRuntimeRegistration(**dict(zip(columns, tuple(row), strict=True)))


def register_embedding_governance_functions(conn: sqlite3.Connection) -> None:
    """Install the deterministic digest used by embedding-governance triggers."""

    conn.create_function(
        "fact_sha256",
        1,
        _sql_sha256,
        deterministic=True,
    )


def _sql_sha256(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _same_sql(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    def normalized(value: object) -> object:
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value

    return tuple(normalized(value) for value in left) == tuple(normalized(value) for value in right)


__all__ = [
    "EmbeddingRuntimeRegistration",
    "load_runtime_registration",
    "persist_runtime_registration",
    "register_embedding_governance_functions",
    "registration_from_artifact",
    "runtime_registration_identity",
]
