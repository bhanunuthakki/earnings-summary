"""Lightweight canonical scope-ID derivation shared by code and SQLite."""

from __future__ import annotations

import hashlib
import json

RETRIEVAL_SCOPE_ID_PREFIX = "ask-scope:v1:"


def derive_retrieval_scope_id(*, source_scope_key: str, issuer_id: str) -> str:
    """Derive one stable bounded ID from the authoritative 0227 composite key."""

    normalized_scope_key = source_scope_key.strip()
    normalized_issuer_id = issuer_id.strip()
    if not normalized_scope_key:
        raise ValueError("source scope key is required")
    if normalized_scope_key != source_scope_key:
        raise ValueError("source scope key must not contain surrounding whitespace")
    if not normalized_issuer_id:
        raise ValueError("issuer ID is required")
    if normalized_issuer_id != issuer_id:
        raise ValueError("issuer ID must not contain surrounding whitespace")
    composite = json.dumps(
        {
            "issuer_id": normalized_issuer_id,
            "source_scope_key": normalized_scope_key,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return RETRIEVAL_SCOPE_ID_PREFIX + hashlib.sha256(composite.encode("utf-8")).hexdigest()


def validate_retrieval_scope_identity(
    *,
    scope_id: str,
    source_scope_key: str,
    issuer_id: str,
) -> None:
    """Reject any identity that is not the canonical composite-derived value."""

    expected = derive_retrieval_scope_id(
        source_scope_key=source_scope_key,
        issuer_id=issuer_id,
    )
    if scope_id != expected:
        raise ValueError("retrieval scope ID does not match its source composite identity")


__all__ = [
    "RETRIEVAL_SCOPE_ID_PREFIX",
    "derive_retrieval_scope_id",
    "validate_retrieval_scope_identity",
]
