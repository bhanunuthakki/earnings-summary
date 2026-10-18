"""Historical document scopes cannot yet admit a financial Research Snapshot."""

from __future__ import annotations

import sqlite3

import pytest

from provenance import research_snapshot as snapshots
from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    ResearchSnapshotRequest,
    ResearchUniverse,
)
from tests.test_historical_analysis_scope import historical as historical


@pytest.mark.parametrize("verify_fact_subjects", [True, False])
def test_historical_financial_snapshot_requires_source_closure_before_references(
    historical: tuple[sqlite3.Connection, dict[str, object]], verify_fact_subjects: bool
) -> None:
    conn, payload = historical
    scope = build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))
    request = ResearchSnapshotRequest(
        research_snapshot_id="historical-financial-unavailable",
        idempotency_key="historical-financial-unavailable",
        research_universe=ResearchUniverse(
            issuer_id=scope.request.issuer_id,
            reporting_entity_ids=("not-read-reporting-entity",),
            document_version_ids=("original-financial",),
            source_obligation_revision_ids=("not-read-obligation",),
            analysis_scope=scope,
        ),
        processing_snapshot_ids=("not-read-processing",),
        corpus_bundles=(
            CorpusProjectionBundle(
                corpus_manifest_id="not-read-corpus", lexical_index_run_id="not-read-lexical"
            ),
        ),
        source_fact_publication_ids=(),
        ontology_snapshot_id="not-read-ontology",
        canonical_fact_resolution_snapshot_id="not-read-resolution",
        canonical_fact_projection_run_id="not-read-projection",
        cutoff_at=scope.request.cutoff_at,
        recorded_at=scope.request.observed_through,
    )
    # The historical document scope above is real synthetic native evidence.
    # Financial coordinates are deliberately unavailable: neither a default
    # nor a custom reference verifier may be reached for this unsupported mode.
    queries: list[str] = []
    before = conn.total_changes
    conn.set_trace_callback(queries.append)
    try:
        with pytest.raises(
            ValueError, match=r"^historical_research_fact_source_closure_unavailable$"
        ):
            getattr(snapshots, "_verify_research_universe")(
                conn, request, verify_fact_subjects=verify_fact_subjects
            )
        assert queries == []
        assert conn.total_changes == before
    finally:
        conn.set_trace_callback(None)
