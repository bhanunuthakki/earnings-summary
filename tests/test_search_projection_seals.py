"""Exact search-projection commitment contracts."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from provenance.search_index_lineage import (
    SearchProjectionSeal,
    lexical_projection_commitment,
)

STAMP = datetime(2026, 7, 27, 18, 0)
SHA = "a" * 64


def _seal(**updates: object) -> SearchProjectionSeal:
    values: dict[str, object] = {
        "projection_seal_id": "seal",
        "idempotency_key": "seal",
        "index_run_id": "run",
        "manifest_id": "manifest",
        "index_kind": "lexical",
        "chunk_count": 0,
        "chunk_set_sha256": SHA,
        "projection_records_sha256": SHA,
        "config_sha256": SHA,
        "storage_uri": "sqlite-fts5://search_lexical_chunks",
        "sealed_at": STAMP,
    }
    values.update(updates)
    return SearchProjectionSeal.model_validate(values)


def test_empty_complete_lexical_projection_can_be_sealed_but_vector_cannot() -> None:
    assert _seal().chunk_count == 0
    with pytest.raises(ValueError, match="at least one chunk"):
        _seal(
            index_kind="vector",
            artifact_set_sha256=SHA,
            provider="fastembed",
            model="model",
            dimensions=384,
        )


def test_lexical_commitment_is_deterministic_and_exposes_duplicate_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE search_chunks (
            chunk_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE search_lexical_chunks USING fts5(
            chunk_id UNINDEXED, text
        );
        INSERT INTO search_chunks VALUES ('chunk-1', 'manifest');
        INSERT INTO search_lexical_chunks VALUES ('chunk-1', 'first');
        INSERT INTO search_lexical_chunks VALUES ('chunk-1', 'duplicate');
        """
    )
    try:
        first = lexical_projection_commitment(conn, manifest_id="manifest")
        second = lexical_projection_commitment(conn, manifest_id="manifest")
        assert first == second
        assert first[0] == 2
    finally:
        conn.close()
