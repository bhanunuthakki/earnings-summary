from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import execution.evaluate_embedding_models as evaluation_cli
from execution.evaluate_embedding_models import _verified_projection_seal, _write_atomic
from execution.promote_embedding_model import _approved_at
from provenance.search_index_lineage import SearchProjectionSeal
from search.local_vector import LanceVectorIndex, vector_records_digest, vector_sha256


def test_evaluation_artifact_write_is_exact_and_replaces_atomically(
    tmp_path: Path,
) -> None:
    output = tmp_path / "nested" / "evaluation.json"

    _write_atomic(output, '{"revision":1}')
    assert output.read_bytes() == b'{"revision":1}\n'

    _write_atomic(output, '{"revision":2}')
    assert output.read_bytes() == b'{"revision":2}\n'
    assert list(output.parent.glob(".*.tmp")) == []


def test_owner_approval_timestamp_is_explicit_and_normalized_to_utc() -> None:
    assert _approved_at("2026-07-28T01:02:03-07:00") == datetime(2026, 7, 28, 8, 2, 3, tzinfo=UTC)
    assert _approved_at("2026-07-28T08:02:03Z") == datetime(2026, 7, 28, 8, 2, 3, tzinfo=UTC)

    with pytest.raises(argparse.ArgumentTypeError, match="timezone"):
        _approved_at("2026-07-28T08:02:03")


def test_evaluation_rejects_external_projection_bytes_mutated_after_seal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[dict[str, object]] = [
        {
            "chunk_id": "chunk-1",
            "vector": [1.0, 2.0],
            "vector_sha256": vector_sha256([1.0, 2.0], dimensions=2),
            "dimensions": 2,
            "input_sha256": "a" * 64,
            "manifest_id": "manifest-1",
            "ticker": "ACME",
        }
    ]
    seal = SearchProjectionSeal(
        projection_seal_id="seal-1",
        idempotency_key="seal-1",
        index_run_id="run-1",
        manifest_id="manifest-1",
        index_kind="vector",
        chunk_count=1,
        chunk_set_sha256="b" * 64,
        projection_records_sha256=vector_records_digest(records),
        artifact_set_sha256="c" * 64,
        provider="fastembed",
        model="BAAI/bge-small-en-v1.5",
        dimensions=2,
        runtime_artifact_sha256="d" * 64,
        config_sha256="e" * 64,
        storage_uri="lance://sealed/run-1#evidence_chunks",
        sealed_at=datetime(2026, 7, 29, 10, 0, tzinfo=UTC),
    )

    class ProjectionIndex:
        def __init__(self) -> None:
            self.storage_uri = seal.storage_uri

        def published_storage_uri(self, index_run_id: str) -> str:
            assert index_run_id == "run-1"
            return self.storage_uri

        def read_projection(
            self,
            index_run_id: str,
            *,
            expected_count: int,
        ) -> list[dict[str, object]]:
            assert index_run_id == "run-1"
            assert expected_count == 1
            return records

    verified: list[SearchProjectionSeal] = []
    monkeypatch.setattr(
        evaluation_cli,
        "load_projection_seal",
        lambda _conn, *, index_run_id: seal if index_run_id == "run-1" else None,
    )
    monkeypatch.setattr(
        evaluation_cli,
        "verify_ledger_projection_seal",
        lambda _conn, candidate_seal: verified.append(candidate_seal),
    )
    index = ProjectionIndex()
    conn = sqlite3.connect(":memory:")
    try:
        assert (
            _verified_projection_seal(
                conn,
                cast(LanceVectorIndex, index),
                model=seal.model or "",
                index_run_id="run-1",
                manifest_id="manifest-1",
                dimensions=2,
                runtime_artifact_sha256="d" * 64,
            )
            == seal
        )
        index.storage_uri = "lance://wrong-root/run-1#evidence_chunks"
        with pytest.raises(ValueError, match="index root"):
            _verified_projection_seal(
                conn,
                cast(LanceVectorIndex, index),
                model=seal.model or "",
                index_run_id="run-1",
                manifest_id="manifest-1",
                dimensions=2,
                runtime_artifact_sha256="d" * 64,
            )
        index.storage_uri = seal.storage_uri
        records[0]["ticker"] = "MUTATED"
        with pytest.raises(ValueError, match="external vector projection"):
            _verified_projection_seal(
                conn,
                cast(LanceVectorIndex, index),
                model=seal.model or "",
                index_run_id="run-1",
                manifest_id="manifest-1",
                dimensions=2,
                runtime_artifact_sha256="d" * 64,
            )
    finally:
        conn.close()
    assert verified == [seal, seal, seal]
