"""Canonical identity contract for issuer-specific retrieval and latest scopes."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scope_identity import (
    RETRIEVAL_SCOPE_ID_PREFIX,
    derive_retrieval_scope_id,
    validate_retrieval_scope_identity,
)


def validate_source_scope_revision_id(value: str) -> str:
    """Require the exact nonblank append-only source revision identifier."""

    if not value.strip() or value != value.strip():
        raise ValueError("source scope revision ID must be nonblank and exact")
    return value


class RetrievalScope(BaseModel):
    """One exact production scope derived from the composite source identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_id: str = Field(min_length=1, max_length=256)
    source_scope_key: str = Field(min_length=1, max_length=128)
    source_scope_revision_id: str = Field(min_length=1, max_length=128)
    ticker: str = Field(min_length=1, max_length=32)
    issuer_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)

    _source_revision = field_validator("source_scope_revision_id")(
        validate_source_scope_revision_id
    )

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def _canonical_scope_identity(self) -> Self:
        validate_retrieval_scope_identity(
            scope_id=self.scope_id,
            source_scope_key=self.source_scope_key,
            issuer_id=self.issuer_id,
        )
        return self


__all__ = [
    "RETRIEVAL_SCOPE_ID_PREFIX",
    "RetrievalScope",
    "derive_retrieval_scope_id",
    "validate_retrieval_scope_identity",
    "validate_source_scope_revision_id",
]
