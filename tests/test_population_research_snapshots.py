# pyright: reportPrivateUsage=false
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import provenance.population_research_snapshots as population
from provenance.population_completeness import PopulationTemporalScope
from provenance.population_research_snapshots import (
    ResearchSnapshotPlanError,
    ResearchSnapshotPopulationRequest,
    select_exact_corpus_coordinate,
    select_retrieval_coordinates,
    verify_research_snapshots,
)
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    ResearchSnapshotRequest,
    ResearchUniverse,
    canonical_json,
)

_CUTOFF = datetime(2026, 7, 29, 0, 0, tzinfo=UTC)
_SHA = "a" * 64


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _terminal_request(observed: datetime) -> ResearchSnapshotRequest:
    return ResearchSnapshotRequest(
        research_snapshot_id="snapshot",
        idempotency_key="snapshot",
        research_universe=ResearchUniverse(
            issuer_id="issuer",
            reporting_entity_ids=("entity",),
            document_version_ids=("document",),
            source_obligation_revision_ids=("obligation",),
        ),
        processing_snapshot_ids=("processing",),
        corpus_bundles=(
            CorpusProjectionBundle(
                corpus_manifest_id="manifest",
                lexical_index_run_id="lexical",
            ),
        ),
        source_fact_publication_ids=(),
        ontology_snapshot_id="ontology",
        canonical_fact_resolution_snapshot_id="resolution",
        canonical_fact_projection_run_id="projection",
        cutoff_at=_CUTOFF,
        recorded_at=observed,
    )


def test_research_verifier_ignores_snapshot_recorded_after_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()
    observed = datetime(2026, 7, 29, 1, 0, tzinfo=UTC)
    request = _terminal_request(observed)
    request_json = canonical_json(request)
    universe_json = canonical_json(
        {
            "document_version_ids": ["document"],
            "issuer_id": "issuer",
            "reporting_entity_ids": ["entity"],
            "source_obligation_revision_ids": ["obligation"],
        }
    )
    conn.executescript(
        """
        CREATE TABLE source_obligation_revisions (
            obligation_revision_id TEXT,obligation_key TEXT,revision INTEGER,
            issuer_id TEXT,reporting_entity_id TEXT,document_family TEXT,
            obligation_state TEXT,active_from TEXT,active_to TEXT,
            knowledge_at TEXT,recorded_at TEXT
        );
        CREATE TABLE research_snapshot_headers (
            research_snapshot_id TEXT,request_json TEXT,request_sha256 TEXT,
            cutoff_at TEXT,recorded_at TEXT
        );
        CREATE TABLE research_snapshot_seals (
            research_snapshot_id TEXT,member_set_sha256 TEXT,sealed_at TEXT
        );
        CREATE TABLE research_snapshot_universe_commitments (
            research_snapshot_id TEXT,issuer_id TEXT,canonical_universe_json TEXT,
            universe_sha256 TEXT,
            cutoff_at TEXT,recorded_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "obligation",
            "obligation",
            1,
            "issuer",
            "entity",
            "operating_company_periodic",
            "required",
            _CUTOFF.isoformat(),
            None,
            _CUTOFF.isoformat(),
            _CUTOFF.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?)",
        (
            "snapshot",
            request_json,
            population.digest_text(request_json),
            _CUTOFF.isoformat(),
            observed.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO research_snapshot_seals VALUES (?,?,?)",
        ("snapshot", _SHA, observed.isoformat()),
    )
    conn.execute(
        "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?,?,?,?)",
        (
            "snapshot",
            "issuer",
            universe_json,
            population.digest_text(universe_json),
            _CUTOFF.isoformat(),
            observed.isoformat(),
        ),
    )
    monkeypatch.setattr(
        population,
        "assemble_research_snapshot_request",
        lambda *_args, **_kwargs: request,
    )
    monkeypatch.setattr(population, "verify_research_snapshot", lambda *_args: None)

    before = verify_research_snapshots(
        conn,
        PopulationTemporalScope(
            knowledge_cutoff=_CUTOFF,
            observed_through=_CUTOFF,
        ),
    )
    after = verify_research_snapshots(
        conn,
        PopulationTemporalScope(
            knowledge_cutoff=_CUTOFF,
            observed_through=observed,
        ),
    )

    assert before.failed_count == 1
    assert after.materialized_count == 1


def test_research_verifier_rejects_o1_artifact_after_o2_late_input() -> None:
    conn = _connection()
    observed_o1 = _CUTOFF + timedelta(hours=1)
    observed_o2 = _CUTOFF + timedelta(hours=2)
    conn.executescript(
        """
        CREATE TABLE source_obligation_revisions (
            obligation_revision_id TEXT,obligation_key TEXT,revision INTEGER,
            issuer_id TEXT,reporting_entity_id TEXT,document_family TEXT,
            obligation_state TEXT,active_from TEXT,active_to TEXT,
            knowledge_at TEXT,recorded_at TEXT
        );
        CREATE TABLE research_snapshot_headers (
            research_snapshot_id TEXT,request_json TEXT,request_sha256 TEXT,
            cutoff_at TEXT,recorded_at TEXT
        );
        CREATE TABLE research_snapshot_seals (
            research_snapshot_id TEXT,member_set_sha256 TEXT,sealed_at TEXT
        );
        CREATE TABLE research_snapshot_universe_commitments (
            research_snapshot_id TEXT,issuer_id TEXT,canonical_universe_json TEXT,
            universe_sha256 TEXT,cutoff_at TEXT,recorded_at TEXT
        );
        """
    )
    obligations = [
        (
            "obligation-1",
            "obligation-1",
            1,
            "issuer-1",
            "entity-1",
            "operating_company_periodic",
            "required",
            _CUTOFF.isoformat(),
            None,
            _CUTOFF.isoformat(),
            _CUTOFF.isoformat(),
        ),
        (
            "obligation-2",
            "obligation-2",
            1,
            "issuer-2",
            "entity-2",
            "operating_company_periodic",
            "required",
            _CUTOFF.isoformat(),
            None,
            _CUTOFF.isoformat(),
            observed_o2.isoformat(),
        ),
    ]
    conn.executemany(
        "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        obligations,
    )
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?)",
        ("snapshot-o1", "{}", _SHA, _CUTOFF.isoformat(), observed_o1.isoformat()),
    )
    conn.execute(
        "INSERT INTO research_snapshot_seals VALUES (?,?,?)",
        ("snapshot-o1", _SHA, observed_o1.isoformat()),
    )
    conn.execute(
        "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?,?,?,?)",
        (
            "snapshot-o1",
            "issuer-1",
            "{}",
            _SHA,
            _CUTOFF.isoformat(),
            observed_o1.isoformat(),
        ),
    )

    result = verify_research_snapshots(
        conn,
        PopulationTemporalScope(
            knowledge_cutoff=_CUTOFF,
            observed_through=observed_o2,
        ),
    )

    assert result.expected_count == 2
    assert result.materialized_count == 0
    assert result.failed_count == 2


def test_research_verifier_reassembles_terminal_request_at_o2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _connection()
    observed_o2 = _CUTOFF + timedelta(hours=2)
    current_request = _terminal_request(observed_o2).model_copy(
        update={"research_snapshot_id": "snapshot-o2", "idempotency_key": "snapshot-o2"}
    )
    conn.executescript(
        """
        CREATE TABLE source_obligation_revisions (
            obligation_revision_id TEXT,obligation_key TEXT,revision INTEGER,
            issuer_id TEXT,reporting_entity_id TEXT,document_family TEXT,
            obligation_state TEXT,active_from TEXT,active_to TEXT,
            knowledge_at TEXT,recorded_at TEXT
        );
        CREATE TABLE research_snapshot_headers (
            research_snapshot_id TEXT,request_json TEXT,request_sha256 TEXT,
            cutoff_at TEXT,recorded_at TEXT
        );
        CREATE TABLE research_snapshot_seals (
            research_snapshot_id TEXT,member_set_sha256 TEXT,sealed_at TEXT
        );
        CREATE TABLE research_snapshot_universe_commitments (
            research_snapshot_id TEXT,issuer_id TEXT,canonical_universe_json TEXT,
            universe_sha256 TEXT,cutoff_at TEXT,recorded_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "obligation",
            "obligation",
            1,
            "issuer",
            "entity",
            "operating_company_periodic",
            "required",
            _CUTOFF.isoformat(),
            None,
            _CUTOFF.isoformat(),
            _CUTOFF.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?)",
        ("snapshot-o1", "{}", _SHA, _CUTOFF.isoformat(), observed_o2.isoformat()),
    )
    conn.execute(
        "INSERT INTO research_snapshot_seals VALUES (?,?,?)",
        ("snapshot-o1", _SHA, observed_o2.isoformat()),
    )
    conn.execute(
        "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?,?,?,?)",
        (
            "snapshot-o1",
            "issuer",
            "{}",
            _SHA,
            _CUTOFF.isoformat(),
            observed_o2.isoformat(),
        ),
    )
    monkeypatch.setattr(
        population,
        "assemble_research_snapshot_request",
        lambda *_args, **_kwargs: current_request,
    )

    with pytest.raises(ValueError, match="assembled K,O request"):
        verify_research_snapshots(
            conn,
            PopulationTemporalScope(
                knowledge_cutoff=_CUTOFF,
                observed_through=observed_o2,
            ),
        )


def test_corpus_coordinate_requires_exact_document_set() -> None:
    conn = _connection()
    conn.executescript(
        """
        CREATE TABLE search_corpus_manifests (
            manifest_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            knowledge_cutoff TEXT,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE search_corpus_manifest_seals (
            manifest_id TEXT PRIMARY KEY,
            completion_status TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE search_corpus_document_memberships (
            manifest_id TEXT NOT NULL,
            document_version_id TEXT,
            membership_status TEXT NOT NULL
        );
        """
    )
    for manifest_id, revision in (("superset", 2), ("exact", 1)):
        conn.execute(
            "INSERT INTO search_corpus_manifests VALUES (?,?,?,?)",
            (
                manifest_id,
                revision,
                "2026-07-29T00:00:00+00:00",
                "2026-07-28T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO search_corpus_manifest_seals VALUES (?,?,?)",
            (manifest_id, "complete", "2026-07-28T00:00:00+00:00"),
        )
    conn.executemany(
        "INSERT INTO search_corpus_document_memberships VALUES (?,?,?)",
        [
            ("superset", "document-a", "included"),
            ("superset", "document-b", "included"),
            ("superset", "supporting-asset", "included"),
            ("exact", "document-a", "included"),
            ("exact", "document-b", "included"),
            ("exact", None, "excluded"),
        ],
    )

    assert (
        select_exact_corpus_coordinate(
            conn,
            ("document-a", "document-b"),
            _CUTOFF,
        )
        == "exact"
    )


def test_retrieval_coordinate_requires_vector_projection() -> None:
    conn = _connection()
    conn.executescript(
        """
        CREATE TABLE search_projection_seals (
            index_run_id TEXT PRIMARY KEY,
            manifest_id TEXT NOT NULL,
            index_kind TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            dimensions INTEGER,
            runtime_artifact_sha256 TEXT,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE search_embedding_model_promotions (
            promotion_id TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            revision INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            runtime_artifact_sha256 TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        INSERT INTO search_projection_seals VALUES (
            'lexical-1','manifest-1','lexical',NULL,NULL,NULL,NULL,
            '2026-07-28T00:00:00+00:00'
        );
        """
    )

    with pytest.raises(ResearchSnapshotPlanError, match="vector_projection_seal_missing"):
        select_retrieval_coordinates(conn, "manifest-1", _CUTOFF)


def test_retrieval_coordinate_binds_exact_promoted_runtime_artifact() -> None:
    conn = _connection()
    conn.executescript(
        """
        CREATE TABLE search_projection_seals (
            index_run_id TEXT PRIMARY KEY,
            manifest_id TEXT NOT NULL,
            index_kind TEXT NOT NULL,
            provider TEXT,
            model TEXT,
            dimensions INTEGER,
            runtime_artifact_sha256 TEXT,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE search_embedding_model_promotions (
            promotion_id TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            revision INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            runtime_artifact_sha256 TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        INSERT INTO search_projection_seals VALUES (
            'lexical-1','manifest-1','lexical',NULL,NULL,NULL,NULL,
            '2026-07-28T00:00:00+00:00'
        );
        INSERT INTO search_projection_seals VALUES (
            'vector-1','manifest-1','vector','local','model-a',768,
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            '2026-07-28T00:00:00+00:00'
        );
        INSERT INTO search_embedding_model_promotions VALUES (
            'promotion-1','evidence_vector_retrieval',1,'local','model-a',768,
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            '2026-07-27T00:00:00+00:00','2026-07-27T00:00:00+00:00',
            '2026-07-27T00:00:00+00:00'
        );
        """
    )

    assert select_retrieval_coordinates(conn, "manifest-1", _CUTOFF) == (
        "lexical-1",
        "vector-1",
        "promotion-1",
    )


def test_expected_issuer_universe_comes_from_active_reporting_obligations() -> None:
    conn = _connection()
    conn.executescript(
        """
        CREATE TABLE source_obligation_revisions (
            obligation_revision_id TEXT PRIMARY KEY,
            obligation_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            issuer_id TEXT NOT NULL,
            reporting_entity_id TEXT,
            document_family TEXT NOT NULL,
            obligation_state TEXT NOT NULL,
            active_from TEXT NOT NULL,
            active_to TEXT,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        INSERT INTO source_obligation_revisions VALUES (
            'obligation-a-1','obligation-a',1,'issuer-without-processing',
            'entity-a','issuer_financial_statements','required',
            '2026-01-01T00:00:00+00:00',NULL,
            '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00'
        );
        """
    )

    assert population._issuer_ids(conn, _CUTOFF) == ("issuer-without-processing",)


def test_stale_generation_coordinates_are_not_admitted() -> None:
    conn = _connection()
    conn.executescript(
        """
        CREATE TABLE search_corpus_manifests (
            manifest_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            knowledge_cutoff TEXT,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE search_corpus_manifest_seals (
            manifest_id TEXT PRIMARY KEY,
            completion_status TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE search_corpus_document_memberships (
            manifest_id TEXT NOT NULL,
            document_version_id TEXT,
            membership_status TEXT NOT NULL
        );
        INSERT INTO search_corpus_manifests VALUES (
            'stale-manifest',1,'2026-07-28T00:00:00+00:00',
            '2026-07-28T00:00:00+00:00'
        );
        INSERT INTO search_corpus_manifest_seals VALUES (
            'stale-manifest','complete','2026-07-28T00:00:00+00:00'
        );
        INSERT INTO search_corpus_document_memberships VALUES (
            'stale-manifest','document-a','included'
        );

        CREATE TABLE ontology_snapshot_headers (
            ontology_snapshot_id TEXT PRIMARY KEY,
            cutoff_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE ontology_snapshot_seals (
            ontology_snapshot_id TEXT PRIMARY KEY,
            sealed_at TEXT NOT NULL
        );
        INSERT INTO ontology_snapshot_headers VALUES (
            'stale-ontology','2026-07-28T00:00:00+00:00',
            '2026-07-28T00:00:00+00:00'
        );
        INSERT INTO ontology_snapshot_seals VALUES (
            'stale-ontology','2026-07-28T00:00:00+00:00'
        );

        CREATE TABLE canonical_fact_resolution_snapshot_scope_headers (
            resolution_snapshot_id TEXT PRIMARY KEY,
            issuer_id TEXT NOT NULL,
            cutoff_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE canonical_fact_resolution_snapshot_scope_seals (
            resolution_snapshot_id TEXT PRIMARY KEY,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE canonical_fact_resolution_snapshot_seals (
            resolution_snapshot_id TEXT PRIMARY KEY,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE canonical_fact_resolution_snapshot_scope_members (
            resolution_snapshot_id TEXT NOT NULL,
            reporting_entity_id TEXT NOT NULL
        );
        INSERT INTO canonical_fact_resolution_snapshot_scope_headers VALUES (
            'stale-resolution','issuer-a','2026-07-28T00:00:00+00:00',
            '2026-07-28T00:00:00+00:00'
        );
        INSERT INTO canonical_fact_resolution_snapshot_scope_seals VALUES (
            'stale-resolution','2026-07-28T00:00:00+00:00'
        );
        INSERT INTO canonical_fact_resolution_snapshot_seals VALUES (
            'stale-resolution','2026-07-28T00:00:00+00:00'
        );
        INSERT INTO canonical_fact_resolution_snapshot_scope_members VALUES (
            'stale-resolution','entity-a'
        );

        CREATE TABLE canonical_fact_projection_generations (
            generation_id TEXT PRIMARY KEY,
            resolution_snapshot_id TEXT NOT NULL,
            ontology_snapshot_id TEXT NOT NULL,
            cutoff_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE canonical_fact_projection_seals (
            generation_id TEXT PRIMARY KEY,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE canonical_fact_projection_audit_receipts (
            generation_id TEXT PRIMARY KEY,
            audited_at TEXT NOT NULL
        );
        CREATE TABLE canonical_fact_projection_scope_bindings (
            generation_id TEXT PRIMARY KEY,
            resolution_snapshot_id TEXT NOT NULL
        );
        INSERT INTO canonical_fact_projection_generations VALUES (
            'stale-projection','stale-resolution','stale-ontology',
            '2026-07-28T00:00:00+00:00','2026-07-28T00:00:00+00:00'
        );
        INSERT INTO canonical_fact_projection_seals VALUES (
            'stale-projection','2026-07-28T00:00:00+00:00'
        );
        INSERT INTO canonical_fact_projection_audit_receipts VALUES (
            'stale-projection','2026-07-28T00:00:00+00:00'
        );
        INSERT INTO canonical_fact_projection_scope_bindings VALUES (
            'stale-projection','stale-resolution'
        );
        """
    )

    with pytest.raises(ResearchSnapshotPlanError, match="exact_search_corpus_missing"):
        select_exact_corpus_coordinate(conn, ("document-a",), _CUTOFF)
    with pytest.raises(ResearchSnapshotPlanError, match="ontology_snapshot_missing"):
        population._ontology_coordinate(conn, _CUTOFF)
    with pytest.raises(
        ResearchSnapshotPlanError,
        match="issuer_scoped_canonical_resolution_missing",
    ):
        population._resolution_coordinate(conn, "issuer-a", ("entity-a",), _CUTOFF)
    with pytest.raises(
        ResearchSnapshotPlanError,
        match="audited_canonical_projection_missing",
    ):
        population._canonical_projection_coordinate(
            conn,
            "stale-resolution",
            "stale-ontology",
            _CUTOFF,
        )


def test_input_commitment_changes_with_any_upstream_digest() -> None:
    baseline: list[dict[str, object]] = [
        {
            "issuer_id": "issuer-a",
            "request": {"research_snapshot_id": "snapshot-a"},
            "upstream": {"search_corpus_manifest_seals": [{"membership_digest_sha256": "a" * 64}]},
        }
    ]
    changed: list[dict[str, object]] = [
        {
            "issuer_id": "issuer-a",
            "request": {"research_snapshot_id": "snapshot-a"},
            "upstream": {"search_corpus_manifest_seals": [{"membership_digest_sha256": "b" * 64}]},
        }
    ]

    assert population._population_input_commitment(
        _CUTOFF,
        ("issuer-a",),
        ("issuer-a",),
        baseline,
    ) != population._population_input_commitment(
        _CUTOFF,
        ("issuer-a",),
        ("issuer-a",),
        changed,
    )


def test_subset_apply_requires_dry_run_commitments() -> None:
    with pytest.raises(ValidationError, match="commitments"):
        ResearchSnapshotPopulationRequest(
            cutoff_at=_CUTOFF,
            operation_recorded_at=_CUTOFF,
            issuer_ids=("issuer-a",),
            apply=True,
        )
