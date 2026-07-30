from __future__ import annotations

import hashlib
import inspect
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    DerivationInputV2,
    DerivationSealV2,
    DerivedFactObservationV2,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    FactDimensionV2,
    FactResolutionCandidateV2,
    FactResolutionRevisionV2,
    ObservationRelationV2,
    ReportedFactObservationV2,
)
from provenance.source_fact_publication import (
    PublicationRecordMissingError,
    PublicationVerificationError,
    record_commitment,
    record_coordinates,
    record_idempotency_key,
    verify_source_fact_publication,
)
from provenance.source_fact_repository import (
    DerivedSourceFact,
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
BASE_REVISION = "0213_decision_draft_provider_id"


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    path = tmp_path / "source-fact-repository.db"
    database = sqlite3.connect(path)
    database.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        """
    )
    database.commit()
    database.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, "head")
    database = sqlite3.connect(path)
    database.execute("PRAGMA foreign_keys = ON")
    _seed_foundation(database)
    database.commit()
    try:
        yield database
    finally:
        database.close()


def _seed_foundation(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-1", "issuer-key-1", "operating_company", STAMP),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "reporting-1",
            "reporting-key-1",
            "issuer-1",
            "legal_registrant",
            "Issuer One",
            STAMP,
        ),
    )
    blob_sha = sha256("filing bytes")
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (blob_sha, 12, "application/json", "file:///filing.json", STAMP),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,"
        "source_published_at,filing_at,accepted_at,observed_at,retrieved_at,"
        "retrieval_config_sha256,collector_code_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "source-1",
            "source-key-1",
            "sec_companyfacts",
            "https://data.sec.gov/example.json",
            blob_sha,
            STAMP,
            STAMP,
            STAMP,
            STAMP,
            STAMP,
            sha256("retrieval"),
            "test-v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id,document_key,version_sequence,observation_id,"
        "blob_sha256,issuer_id,ticker,document_type,form_type,accession_number,"
        "exhibit_id,period_start,period_end,as_of_at,language,"
        "replaces_document_version_id,legacy_document_id,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "document-1",
            "document-key-1",
            1,
            "source-1",
            blob_sha,
            "issuer-1",
            None,
            "regulatory_filing",
            "10-K",
            "0000000001-26-000001",
            None,
            STAMP - timedelta(days=365),
            STAMP,
            STAMP,
            "en",
            None,
            None,
            STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "binding-1",
            "binding-key-1",
            "issuer-1",
            1,
            "issuer-1",
            "reporting-1",
            None,
            "selected",
            "deterministic",
            "exact_subject",
            "{}",
            0,
            STAMP,
            STAMP,
            STAMP,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run-1",
            "run-key-1",
            "document-1",
            blob_sha,
            "test-extractor",
            sha256("extractor-config"),
            "test-v1",
            sha256("output"),
            STAMP,
            STAMP,
            "succeeded",
        ),
    )
    locator = '{"path":"facts.us-gaap.Revenues.units.USD[0]"}'
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "node-1",
            "node-key-1",
            1,
            "run-1",
            None,
            None,
            "table_cell",
            "100",
            locator,
            sha256(locator),
            STAMP,
        ),
    )


def make_cell(suffix: str = "1", *, period_end: datetime = STAMP) -> FactCellV2:
    dimension = FactDimensionV2(
        dimension_id=f"dimension-{suffix}",
        idempotency_key=f"dimension-key-{suffix}",
        axis_namespace="https://example.com/dimensions",
        axis_name="ProductAxis",
        member_kind="explicit",
        explicit_member_namespace="https://example.com/members",
        explicit_member_name="CloudMember",
        recorded_at=STAMP,
    )
    return FactCellV2(
        fact_cell_id=f"cell-{suffix}",
        idempotency_key=f"cell-key-{suffix}",
        reporting_entity_id="reporting-1",
        concept_namespace="us-gaap",
        concept_name="Revenue",
        taxonomy_name="US GAAP",
        taxonomy_version="2026",
        accounting_basis="us_gaap",
        consolidation_scope="consolidated",
        period_kind="duration",
        period_start=period_end - timedelta(days=365),
        period_end=period_end,
        fiscal_year=period_end.year,
        fiscal_period="FY",
        dimensions=(dimension,),
        unit_key="USD",
        currency="USD",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )


def make_report(
    cell: FactCellV2,
    suffix: str = "1",
    *,
    numeric_value: str = "100",
    at: datetime = STAMP,
) -> ReportedFactObservationV2:
    return ReportedFactObservationV2(
        observation_id=f"observation-{suffix}",
        idempotency_key=f"observation-key-{suffix}",
        fact_cell_id=cell.fact_cell_id,
        observation_kind="reported",
        value_kind="numeric",
        numeric_value=numeric_value,
        raw_lexical_value=numeric_value,
        method_name="sec-xbrl",
        method_version="v1",
        method_config_sha256=sha256("method-config"),
        revision_kind="initial",
        effective_at=at,
        knowledge_at=at,
        recorded_at=at,
        document_version_id="document-1",
        evidence_node_id="node-1",
        source_locator=CanonicalJSONObject({"path": "facts.us-gaap.Revenues.units.USD[0]"}),
        source_entry_sha256=sha256(f"entry-{suffix}"),
        subject_binding_revision_id="binding-1",
        source_taxonomy_version="2026",
        source_context_id="context-1",
        source_unit_id="unit-1",
        decimals="-6",
    )


def make_resolution(
    cell: FactCellV2,
    report: ReportedFactObservationV2,
    suffix: str = "1",
    *,
    status: str = "resolved",
) -> FactResolutionRevisionV2:
    candidate = FactResolutionCandidateV2(
        candidate_id=f"candidate-{suffix}",
        idempotency_key=f"candidate-key-{suffix}",
        candidate_set_id=f"candidate-set-{suffix}",
        fact_cell_id=cell.fact_cell_id,
        observation_id=report.observation_id,
        candidate_ordinal=0,
        eligibility="eligible",
        reason_code="exact_report",
        reason_details=CanonicalJSONObject({}),
        recorded_at=report.recorded_at,
    )
    return FactResolutionRevisionV2.model_validate(
        {
            "resolution_revision_id": f"resolution-{suffix}",
            "idempotency_key": f"resolution-key-{suffix}",
            "fact_cell_id": cell.fact_cell_id,
            "revision": 1,
            "status": status,
            "candidate_set_id": f"candidate-set-{suffix}",
            "candidates": (candidate,),
            "selected_observation_id": (report.observation_id if status == "resolved" else None),
            "policy_name": "exact-evidence-first",
            "policy_version": "v1",
            "policy_config_sha256": sha256("policy"),
            "reason_code": status,
            "reason_details": CanonicalJSONObject({}),
            "knowledge_cutoff": report.knowledge_at,
            "effective_at": report.effective_at,
            "recorded_at": report.recorded_at,
        }
    )


def make_publication(
    *,
    status: str = "resolved",
) -> SourceFactPublication:
    cell = make_cell()
    report = make_report(cell)
    return SourceFactPublication(
        publication_id="publication-1",
        idempotency_key="publication-key-1",
        reported_facts=(ReportedSourceFact(cell=cell, observation=report),),
        extraction_seals=(
            ExtractionRunCompletenessSealV2(
                extraction_seal_id="extraction-seal-1",
                idempotency_key="extraction-seal-key-1",
                extraction_run_id="run-1",
                expected_node_count=1,
                completeness_policy_name="all-run-nodes",
                completeness_policy_version="v1",
                completeness_policy_sha256=sha256("completeness"),
                knowledge_at=STAMP,
                recorded_at=STAMP,
            ),
        ),
        resolutions=(make_resolution(cell, report, status=status),),
    )


def test_publish_is_exact_replay_and_v2_only(
    conn: sqlite3.Connection,
) -> None:
    repository = SourceFactRepository(conn)
    publication = make_publication()
    first = repository.publish(publication)
    second = repository.publish(publication)

    assert not first.exact_replay
    assert second.exact_replay
    assert second.created_record_ids == ()
    assert first.publication_payload_sha256 == second.publication_payload_sha256
    assert first.publication_seal_id == second.publication_seal_id
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publications").fetchone() == (1,)
    assert conn.execute(
        "SELECT record_kind FROM source_fact_publication_members ORDER BY member_ordinal"
    ).fetchall() == [
        ("fact_cell",),
        ("fact_observation",),
        ("extraction_seal",),
        ("resolution_revision",),
    ]
    payload = conn.execute(
        "SELECT canonical_publication_payload_json FROM source_fact_publications"
    ).fetchone()
    assert payload is not None
    assert sha256(str(payload[0])) == first.publication_payload_sha256
    assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone() == (0,)
    with pytest.raises(PublicationRecordMissingError) as captured:
        record_coordinates(
            conn,
            "fact_cell",
            ("missing-cell-first", publication.reported_facts[0].cell.fact_cell_id),
        )
    assert captured.value.record_kind == "fact_cell"
    assert captured.value.record_id == "missing-cell-first"


def test_public_verifier_recomputes_complete_publication_at_explicit_cutoff(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    receipt = SourceFactRepository(conn).publish(publication)

    verified = verify_source_fact_publication(
        conn,
        publication_id=publication.publication_id,
        cutoff=STAMP,
    )

    assert verified.publication_seal_id == receipt.publication_seal_id
    assert verified.publication_payload_sha256 == receipt.publication_payload_sha256
    assert verified.member_count == 4
    assert tuple(member.record_kind for member in verified.members) == (
        "fact_cell",
        "fact_observation",
        "extraction_seal",
        "resolution_revision",
    )


def test_public_verifier_rejects_publication_after_cutoff_as_missing(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)

    with pytest.raises(PublicationVerificationError) as captured:
        verify_source_fact_publication(
            conn,
            publication_id=publication.publication_id,
            cutoff=STAMP - timedelta(microseconds=1),
        )

    assert captured.value.reason_code == "publication_graph_after_cutoff"
    assert captured.value.disposition == "missing_provenance"


def test_repository_replays_and_verifies_distinct_knowledge_and_observation_clocks(
    conn: sqlite3.Connection,
) -> None:
    observed = STAMP + timedelta(hours=2)
    base = make_publication()
    source_fact = base.reported_facts[0]
    cell = source_fact.cell.model_copy(
        update={
            "recorded_at": observed,
            "dimensions": tuple(
                dimension.model_copy(update={"recorded_at": observed})
                for dimension in source_fact.cell.dimensions
            ),
        }
    )
    report = source_fact.observation.model_copy(update={"recorded_at": observed})
    resolution = base.resolutions[0].model_copy(
        update={
            "recorded_at": observed,
            "candidates": tuple(
                candidate.model_copy(update={"recorded_at": observed})
                for candidate in base.resolutions[0].candidates
            ),
        }
    )
    publication = base.model_copy(
        update={
            "created_at": STAMP,
            "recorded_at": observed,
            "reported_facts": (ReportedSourceFact(cell=cell, observation=report),),
            "extraction_seals": tuple(
                seal.model_copy(update={"recorded_at": observed}) for seal in base.extraction_seals
            ),
            "resolutions": (resolution,),
        }
    )
    repository = SourceFactRepository(conn)

    first = repository.publish(publication)
    replay = repository.publish(publication)
    verified = verify_source_fact_publication(
        conn,
        publication_id=publication.publication_id,
        cutoff=STAMP,
        observed_through=observed,
    )

    assert not first.exact_replay
    assert replay.exact_replay
    assert verified.cutoff == STAMP
    assert verified.observed_through == observed
    assert verified.created_at == STAMP
    assert verified.recorded_at == observed
    with pytest.raises(PublicationVerificationError) as early_observation:
        verify_source_fact_publication(
            conn,
            publication_id=publication.publication_id,
            cutoff=STAMP,
            observed_through=STAMP,
        )
    assert early_observation.value.reason_code == "publication_graph_after_cutoff"
    with pytest.raises(PublicationVerificationError) as future_knowledge:
        verify_source_fact_publication(
            conn,
            publication_id=publication.publication_id,
            cutoff=STAMP - timedelta(microseconds=1),
            observed_through=observed,
        )
    assert future_knowledge.value.reason_code == "publication_graph_after_cutoff"


def test_exact_replay_uses_public_verifier_and_rejects_member_tamper(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    repository = SourceFactRepository(conn)
    repository.publish(publication)
    conn.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
    conn.execute(
        "UPDATE source_fact_publication_members "
        "SET canonical_member_sha256 = ? WHERE member_ordinal = 0",
        (sha256("tampered-member"),),
    )

    with pytest.raises(PublicationVerificationError) as captured:
        repository.publish(publication)

    assert captured.value.reason_code == "publication_member_tampered"
    assert captured.value.disposition == "quarantined"


def test_public_verifier_rejects_live_record_commitment_tamper(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    conn.execute("DROP TRIGGER trg_fact_observation_payload_commitments_v2_append_only")
    conn.execute(
        "UPDATE fact_observation_payload_commitments_v2 "
        "SET observation_payload_sha256 = ? WHERE observation_id = ?",
        (
            sha256("tampered-live-record"),
            publication.reported_facts[0].observation.observation_id,
        ),
    )

    with pytest.raises(PublicationVerificationError) as captured:
        verify_source_fact_publication(
            conn,
            publication_id=publication.publication_id,
            cutoff=STAMP,
        )

    assert captured.value.reason_code == "publication_record_commitment_mismatch"
    assert captured.value.disposition == "quarantined"


def test_public_verifier_attributes_missing_batched_member_record(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    missing_observation_id = publication.reported_facts[0].observation.observation_id
    conn.execute("DROP TRIGGER trg_fact_observation_payload_commitments_v2_append_only")
    conn.execute("DROP TRIGGER trg_fact_observation_payload_commitments_v2_append_only_delete")
    conn.execute(
        "DELETE FROM fact_observation_payload_commitments_v2 WHERE observation_id = ?",
        (missing_observation_id,),
    )

    with pytest.raises(PublicationVerificationError) as captured:
        verify_source_fact_publication(
            conn,
            publication_id=publication.publication_id,
            cutoff=STAMP,
        )

    assert captured.value.reason_code == "publication_member_record_missing"
    assert captured.value.disposition == "quarantined"
    assert captured.value.record_kind == "fact_observation"
    assert captured.value.record_id == missing_observation_id


def test_missing_extraction_seal_rolls_back_entire_publication(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication().model_copy(update={"extraction_seals": ()})
    with pytest.raises(ValueError, match="complete extraction seal"):
        SourceFactRepository(conn).publish(publication)
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publications").fetchone() == (0,)


def test_repository_has_no_legacy_compatibility_write_surface() -> None:
    assert tuple(inspect.signature(SourceFactRepository).parameters) == ("conn",)
    assert tuple(inspect.signature(SourceFactRepository.publish).parameters) == (
        "self",
        "publication",
    )


def test_conflicting_replay_fails_without_partial_writes(
    conn: sqlite3.Connection,
) -> None:
    repository = SourceFactRepository(conn)
    publication = make_publication()
    repository.publish(publication)
    original = publication.reported_facts[0]
    conflict = original.model_copy(
        update={
            "observation": original.observation.model_copy(
                update={"numeric_value": "999", "raw_lexical_value": "999"}
            )
        }
    )
    with pytest.raises(ValueError, match="conflicts"):
        repository.publish(publication.model_copy(update={"reported_facts": (conflict,)}))
    assert conn.execute("SELECT numeric_value FROM fact_observations_v2").fetchone() == ("100",)


def test_publication_identity_conflict_is_atomic(
    conn: sqlite3.Connection,
) -> None:
    repository = SourceFactRepository(conn)
    publication = make_publication()
    repository.publish(publication)

    with pytest.raises(ValueError, match="publication identity conflicts"):
        repository.publish(
            publication.model_copy(update={"idempotency_key": "publication-key-conflict"})
        )

    assert conn.execute(
        "SELECT publication_id,idempotency_key FROM source_fact_publications"
    ).fetchall() == [("publication-1", "publication-key-1")]
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publication_members").fetchone() == (4,)
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publication_seals").fetchone() == (1,)


def test_publication_members_deduplicate_shared_cells(
    conn: sqlite3.Connection,
) -> None:
    cell = make_cell()
    first = make_report(cell)
    second = make_report(cell, "2")
    publication = make_publication().model_copy(
        update={
            "reported_facts": (
                ReportedSourceFact(cell=cell, observation=first),
                ReportedSourceFact(cell=cell, observation=second),
            ),
            "resolutions": (make_resolution(cell, first),),
        }
    )
    SourceFactRepository(conn).publish(publication)

    kinds = conn.execute(
        "SELECT record_kind,COUNT(*) "
        "FROM source_fact_publication_members "
        "GROUP BY record_kind ORDER BY record_kind"
    ).fetchall()
    assert kinds == [
        ("extraction_seal", 1),
        ("fact_cell", 1),
        ("fact_observation", 2),
        ("resolution_revision", 1),
    ]


def test_publication_ledger_is_append_only(
    conn: sqlite3.Connection,
) -> None:
    SourceFactRepository(conn).publish(make_publication())

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE source_fact_publication_members SET member_ordinal = 10")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM source_fact_publication_seals")


def test_later_source_reuses_exact_semantic_cell_envelope(
    conn: sqlite3.Connection,
) -> None:
    later = STAMP + timedelta(days=90)
    blob_sha = sha256("later filing bytes")
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (blob_sha, 18, "application/json", "file:///later.json", later),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,"
        "source_published_at,filing_at,accepted_at,observed_at,retrieved_at,"
        "retrieval_config_sha256,collector_code_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "source-2",
            "source-key-2",
            "sec_companyfacts",
            "https://data.sec.gov/later.json",
            blob_sha,
            later,
            later,
            later,
            later,
            later,
            sha256("retrieval"),
            "test-v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id,document_key,version_sequence,observation_id,"
        "blob_sha256,issuer_id,ticker,document_type,form_type,accession_number,"
        "exhibit_id,period_start,period_end,as_of_at,language,"
        "replaces_document_version_id,legacy_document_id,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "document-2",
            "document-key-2",
            1,
            "source-2",
            blob_sha,
            "issuer-1",
            None,
            "regulatory_filing",
            "10-K",
            "0000000001-26-000002",
            None,
            STAMP - timedelta(days=365),
            STAMP,
            later,
            "en",
            None,
            None,
            later,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run-2",
            "run-key-2",
            "document-2",
            blob_sha,
            "test-extractor",
            sha256("extractor-config"),
            "test-v1",
            sha256("output-2"),
            later,
            later,
            "succeeded",
        ),
    )
    locator = '{"path":"facts.us-gaap.Revenues.units.USD[0]"}'
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "node-2",
            "node-key-2",
            1,
            "run-2",
            None,
            None,
            "table_cell",
            "125",
            locator,
            sha256(locator),
            later,
        ),
    )
    repository = SourceFactRepository(conn)
    first_publication = make_publication()
    repository.publish(first_publication)

    first_cell = first_publication.reported_facts[0].cell
    later_cell = first_cell.model_copy(
        update={
            "taxonomy_version": "2026-q2",
            "effective_at": later,
            "knowledge_at": later,
            "recorded_at": later,
        }
    )
    later_observation = make_report(
        later_cell,
        "later",
        numeric_value="125",
        at=later,
    ).model_copy(
        update={
            "document_version_id": "document-2",
            "evidence_node_id": "node-2",
            "source_taxonomy_version": "2026-q2",
        }
    )
    second_publication = SourceFactPublication(
        publication_id="publication-2",
        idempotency_key="publication-key-2",
        reported_facts=(
            ReportedSourceFact(
                cell=later_cell,
                observation=later_observation,
            ),
        ),
        extraction_seals=(
            ExtractionRunCompletenessSealV2(
                extraction_seal_id="extraction-seal-2",
                idempotency_key="extraction-seal-key-2",
                extraction_run_id="run-2",
                expected_node_count=1,
                completeness_policy_name="all-run-nodes",
                completeness_policy_version="v1",
                completeness_policy_sha256=sha256("completeness"),
                knowledge_at=later,
                recorded_at=later,
            ),
        ),
    )
    receipt = repository.publish(second_publication)
    replay = repository.publish(second_publication)

    assert not receipt.exact_replay
    assert replay.exact_replay
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone() == (2,)
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publications").fetchone() == (2,)


def test_derived_publication_sequences_seal_before_resolution(
    conn: sqlite3.Connection,
) -> None:
    source_publication = make_publication()
    source_fact = source_publication.reported_facts[0]
    recast = make_report(
        source_fact.cell,
        "source-recast",
        numeric_value="101",
    )
    relation = ObservationRelationV2(
        relation_id="relation-source-recast",
        idempotency_key="relation-key-source-recast",
        subject_observation_id=recast.observation_id,
        object_observation_id=source_fact.observation.observation_id,
        relation_kind="presentation_recast_of",
        reason_code="presentation_recast",
        reason_details=CanonicalJSONObject({}),
        policy_name="test-relation",
        policy_version="v1",
        policy_config_sha256=sha256("relation-policy"),
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    source_publication = source_publication.model_copy(
        update={
            "reported_facts": (
                source_fact,
                ReportedSourceFact(cell=source_fact.cell, observation=recast),
            ),
            "relations": (relation,),
        }
    )
    repository = SourceFactRepository(conn)
    source_receipt = repository.publish(source_publication)
    source = source_publication.reported_facts[0].observation
    derived_cell = FactCellV2.model_validate(
        {
            **make_cell("derived").model_dump(),
            "concept_name": "DoubleRevenue",
            "semantic_key_sha256": None,
        }
    )
    derived = DerivedFactObservationV2(
        observation_id="observation-derived",
        idempotency_key="observation-key-derived",
        fact_cell_id=derived_cell.fact_cell_id,
        observation_kind="derived",
        value_kind="numeric",
        numeric_value="200",
        method_name="formula-engine",
        method_version="v1",
        method_config_sha256=sha256("formula-method"),
        revision_kind="initial",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
        formula_id="double",
        formula_version="v1",
    )
    edge = DerivationInputV2(
        edge_id="edge-derived",
        idempotency_key="edge-key-derived",
        derived_observation_id=derived.observation_id,
        input_position=0,
        input_observation_id=source.observation_id,
        input_role="base",
        recorded_at=STAMP,
    )
    derivation = DerivationSealV2(
        derivation_seal_id="derivation-seal-1",
        idempotency_key="derivation-seal-key-1",
        derived_observation_id=derived.observation_id,
        ordered_inputs=(edge,),
        input_basis="as_reported",
        formula_definition_sha256=sha256("formula-definition"),
        formula_config_sha256=sha256("formula-config"),
        seal_method="canonical-json",
        seal_method_version="v1",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    candidate = FactResolutionCandidateV2(
        candidate_id="candidate-derived",
        idempotency_key="candidate-key-derived",
        candidate_set_id="candidate-set-derived",
        fact_cell_id=derived_cell.fact_cell_id,
        observation_id=derived.observation_id,
        candidate_ordinal=0,
        eligibility="eligible",
        reason_code="sealed_formula",
        reason_details=CanonicalJSONObject({}),
        recorded_at=STAMP,
    )
    resolution = FactResolutionRevisionV2.model_validate(
        {
            "resolution_revision_id": "resolution-derived",
            "idempotency_key": "resolution-key-derived",
            "fact_cell_id": derived_cell.fact_cell_id,
            "revision": 1,
            "status": "resolved",
            "candidate_set_id": "candidate-set-derived",
            "candidates": (candidate,),
            "selected_observation_id": derived.observation_id,
            "policy_name": "sealed-derived-only",
            "policy_version": "v1",
            "policy_config_sha256": sha256("derived-policy"),
            "reason_code": "resolved",
            "reason_details": CanonicalJSONObject({}),
            "knowledge_cutoff": STAMP,
            "effective_at": STAMP,
            "recorded_at": STAMP,
        }
    )
    derived_publication = SourceFactPublication(
        publication_id="publication-derived",
        idempotency_key="publication-key-derived",
        derived_facts=(DerivedSourceFact(cell=derived_cell, observation=derived),),
        derivations=(derivation,),
        resolutions=(resolution,),
    )
    receipt = repository.publish(derived_publication)
    replay = repository.publish(derived_publication)
    assert receipt.derivation_seal_ids == ("derivation-seal-1",)
    assert source_receipt.relation_ids == ("relation-source-recast",)
    assert receipt.resolution_revision_ids == ("resolution-derived",)
    assert replay.exact_replay
    assert conn.execute(
        "SELECT COUNT(*) FROM fact_observation_payload_commitments_v2 "
        "WHERE observation_id = 'observation-derived'"
    ).fetchone() == (1,)

    member_rows = conn.execute(
        "SELECT record_kind,record_id FROM source_fact_publication_members "
        "ORDER BY publication_id,member_ordinal"
    ).fetchall()
    for record_kind in (
        "fact_cell",
        "fact_observation",
        "observation_relation",
        "derivation_seal",
        "extraction_seal",
        "resolution_revision",
    ):
        record_ids = tuple(str(record_id) for kind, record_id in member_rows if kind == record_kind)
        coordinates = record_coordinates(conn, record_kind, record_ids)
        assert tuple(coordinates) == tuple(dict.fromkeys(record_ids))
        assert coordinates == {
            record_id: (
                record_idempotency_key(conn, record_kind, record_id),
                record_commitment(conn, record_kind, record_id),
            )
            for record_id in dict.fromkeys(record_ids)
        }


def test_publication_member_coordinate_reads_scale_by_bounded_batch_count(
    conn: sqlite3.Connection,
) -> None:
    member_count = 401
    facts = tuple(
        ReportedSourceFact(
            cell=(cell := make_cell(f"batch-{index}", period_end=STAMP - timedelta(days=index))),
            observation=make_report(cell, f"batch-{index}"),
        )
        for index in range(member_count)
    )
    publication = SourceFactPublication(
        publication_id="publication-batched-coordinates",
        idempotency_key="publication-key-batched-coordinates",
        reported_facts=facts,
        extraction_seals=(
            ExtractionRunCompletenessSealV2(
                extraction_seal_id="extraction-seal-batched-coordinates",
                idempotency_key="extraction-seal-key-batched-coordinates",
                extraction_run_id="run-1",
                expected_node_count=1,
                completeness_policy_name="all-run-nodes",
                completeness_policy_version="v1",
                completeness_policy_sha256=sha256("completeness"),
                knowledge_at=STAMP,
                recorded_at=STAMP,
            ),
        ),
    )
    coordinate_selects: list[str] = []
    completeness_selects: list[str] = []
    per_record_selects: list[str] = []
    extraction_run_selects: list[str] = []
    nested_observation_savepoints: list[str] = []

    def capture_coordinate_select(statement: str) -> None:
        if (
            "WHERE cell.fact_cell_id IN (" in statement
            or "WHERE observation.observation_id IN (" in statement
        ):
            coordinate_selects.append(statement)
        if "WHERE anchor.observation_id IN (" in statement:
            completeness_selects.append(statement)
        if (
            "WHERE seal.semantic_key_sha256 =" in statement
            or "WHERE anchor.observation_id =" in statement
            or "SELECT * FROM fact_observations_v2 WHERE idempotency_key =" in statement
        ):
            per_record_selects.append(statement)
        if "WHERE node.node_id IN (" in statement:
            extraction_run_selects.append(statement)
        if statement == "SAVEPOINT persist_fact_observation_v2":
            nested_observation_savepoints.append(statement)

    conn.set_trace_callback(capture_coordinate_select)
    try:
        receipt = SourceFactRepository(conn).publish(publication)
    finally:
        conn.set_trace_callback(None)

    cell_selects = tuple(
        statement for statement in coordinate_selects if "WHERE cell.fact_cell_id IN (" in statement
    )
    observation_selects = tuple(
        statement
        for statement in coordinate_selects
        if "WHERE observation.observation_id IN (" in statement
    )
    # SQLite's trace hook may repeat a top-level statement while triggers run;
    # unique rendered SQL still proves exactly two 400-record batches per kind.
    assert len(set(cell_selects)) == 2
    assert len(set(observation_selects)) == 2
    assert len(set(completeness_selects)) == 2
    # All 401 observations share one evidence node, so run metadata is fetched once.
    assert len(set(extraction_run_selects)) == 1
    assert per_record_selects == []
    assert nested_observation_savepoints == []
    assert len(receipt.cell_ids) == member_count
    assert len(receipt.observation_ids) == member_count
    assert conn.execute(
        "SELECT record_kind,COUNT(*) "
        "FROM source_fact_publication_members "
        "WHERE publication_id = ? GROUP BY record_kind ORDER BY record_kind",
        (publication.publication_id,),
    ).fetchall() == [
        ("extraction_seal", 1),
        ("fact_cell", member_count),
        ("fact_observation", member_count),
    ]
    boundary_members = conn.execute(
        "SELECT member_ordinal,record_kind,record_id "
        "FROM source_fact_publication_members "
        "WHERE publication_id = ? AND member_ordinal IN (0,400,401,801,802) "
        "ORDER BY member_ordinal",
        (publication.publication_id,),
    ).fetchall()
    assert boundary_members == [
        (0, "fact_cell", "cell-batch-0"),
        (400, "fact_cell", "cell-batch-400"),
        (401, "fact_observation", "observation-batch-0"),
        (801, "fact_observation", "observation-batch-400"),
        (802, "extraction_seal", "extraction-seal-batched-coordinates"),
    ]
