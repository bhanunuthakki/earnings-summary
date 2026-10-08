"""Versioned publication clocks preserve old immutable request commitments."""

from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import ValidationError

from provenance.research_snapshot import (
    ResearchSnapshotRequest,
    build_research_snapshot,
    canonical_json,
)
from tests.test_onon_inputs import NOW


def _snapshot() -> ResearchSnapshotRequest:
    return ResearchSnapshotRequest.model_validate(
        {
            "research_snapshot_id": "snapshot",
            "idempotency_key": "snapshot",
            "research_universe": {
                "issuer_id": "issuer-1",
                "reporting_entity_ids": ["entity"],
                "document_version_ids": ["doc"],
                "source_obligation_revision_ids": ["obligation"],
            },
            "processing_snapshot_ids": ["processing"],
            "corpus_bundles": [
                {"corpus_manifest_id": "manifest", "lexical_index_run_id": "lexical"}
            ],
            "source_fact_publication_ids": ["publication"],
            "ontology_snapshot_id": "ontology",
            "canonical_fact_resolution_snapshot_id": "resolution",
            "canonical_fact_projection_run_id": "projection",
            "cutoff_at": NOW,
            "recorded_at": NOW,
        }
    )


def test_legacy_request_serialization_is_exact_and_replayable() -> None:
    legacy = _snapshot()
    payload = legacy.model_dump(mode="json")
    assert "source_publication_reference_clock" not in payload
    raw = canonical_json(payload)
    replay = ResearchSnapshotRequest.model_validate_json(raw)
    assert canonical_json(replay) == raw
    explicit = {**payload, "source_publication_reference_clock": "cutoff_v1"}
    assert canonical_json(ResearchSnapshotRequest.model_validate(explicit)) == raw


def test_new_clock_policy_is_explicit_hash_bound_and_unknown_refuses() -> None:
    payload = _snapshot().model_dump(mode="json")
    current = ResearchSnapshotRequest.model_validate(
        {**payload, "source_publication_reference_clock": "publication_created_v2"}
    )
    assert (
        json.loads(canonical_json(current))["source_publication_reference_clock"]
        == "publication_created_v2"
    )
    assert canonical_json(current) != canonical_json(payload)
    assert ResearchSnapshotRequest.model_validate_json(canonical_json(current)) == current
    with pytest.raises(ValidationError):
        ResearchSnapshotRequest.model_validate(
            {**payload, "source_publication_reference_clock": "invented"}
        )


def test_unknown_mode_model_copy_refuses_before_database_access() -> None:
    request = _snapshot().model_copy(
        update={"source_publication_reference_clock": "unknown", "source_fact_publication_ids": ()}
    )
    with pytest.raises(ValueError):
        request.model_dump_json()
    queries: list[str] = []
    with sqlite3.connect(":memory:") as conn:
        conn.set_trace_callback(queries.append)
        with pytest.raises(ValueError, match="unsupported source publication reference clock"):
            build_research_snapshot(conn, request)
    assert queries == []
