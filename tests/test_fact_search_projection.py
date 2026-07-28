"""Typed, fail-closed structured fact-search projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import JsonValue, ValidationError

from alembic import command
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    FactDimensionV2,
    FactResolutionCandidateV2,
    FactResolutionRevisionV2,
    ReportedFactObservationV2,
)
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from search.fact_projection import (
    DerivationInput,
    DerivedFactLineage,
    DocumentHit,
    FactDimension,
    FactHit,
    FactProjectionSpec,
    FactSearchFilter,
    FactSearchProjectionStore,
    RankedGroundedHit,
    ReportedFactEvidence,
)

T0 = datetime(2026, 7, 27, 12, tzinfo=UTC)
RECORDED = T0 + timedelta(hours=1)
ROOT = Path(__file__).resolve().parents[1]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _alembic_config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture
def hardened_conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    path = tmp_path / "fact-search-projection.db"
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
    base_revision = "0213_decision_draft_provider_id"
    config = _alembic_config(path)
    command.stamp(config, base_revision)
    command.upgrade(config, "head")
    database = sqlite3.connect(path)
    database.execute("PRAGMA foreign_keys = ON")
    _seed_hardened_foundation(database)
    database.commit()
    try:
        yield database
    finally:
        database.close()


def _seed_hardened_foundation(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-1", "issuer-key-1", "operating_company", T0),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "reporting-1",
            "reporting-key-1",
            "issuer-1",
            "legal_registrant",
            "Issuer One",
            T0,
        ),
    )
    blob_sha = _sha("filing bytes")
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (blob_sha, 12, "application/json", "file:///filing.json", T0),
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
            T0,
            T0,
            T0,
            T0,
            T0,
            _sha("retrieval"),
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
            T0 - timedelta(days=365),
            T0,
            T0,
            "en",
            None,
            None,
            T0,
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
            T0,
            T0,
            T0,
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
            _sha("extractor-config"),
            "test-v1",
            _sha("output"),
            T0,
            T0,
            "succeeded",
        ),
    )
    locator_json = '{"path":"facts.us-gaap.Revenue.units.USD[0]"}'
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
            locator_json,
            _sha(locator_json),
            T0,
        ),
    )


def _publish_hardened_fact(conn: sqlite3.Connection) -> None:
    dimension = FactDimensionV2(
        dimension_id="dimension-1",
        idempotency_key="dimension-key-1",
        axis_namespace="https://example.com/dimensions",
        axis_name="ProductAxis",
        member_kind="explicit",
        explicit_member_namespace="https://example.com/members",
        explicit_member_name="CloudMember",
        recorded_at=T0,
    )
    cell = FactCellV2(
        fact_cell_id="cell-1",
        idempotency_key="cell-key-1",
        reporting_entity_id="reporting-1",
        concept_namespace="us-gaap",
        concept_name="Revenue",
        taxonomy_name="US GAAP",
        taxonomy_version="2026",
        accounting_basis="us_gaap",
        consolidation_scope="consolidated",
        period_kind="duration",
        period_start=T0 - timedelta(days=365),
        period_end=T0,
        fiscal_year=2026,
        fiscal_period="FY",
        dimensions=(dimension,),
        unit_key="USD",
        currency="USD",
        effective_at=T0,
        knowledge_at=T0,
        recorded_at=T0,
    )
    observation = ReportedFactObservationV2(
        observation_id="observation-1",
        idempotency_key="observation-key-1",
        fact_cell_id=cell.fact_cell_id,
        observation_kind="reported",
        value_kind="numeric",
        numeric_value="100",
        raw_lexical_value="100",
        method_name="sec-xbrl",
        method_version="v1",
        method_config_sha256=_sha("method-config"),
        revision_kind="initial",
        effective_at=T0,
        knowledge_at=T0,
        recorded_at=T0,
        document_version_id="document-1",
        evidence_node_id="node-1",
        source_locator=CanonicalJSONObject({"path": "facts.us-gaap.Revenue.units.USD[0]"}),
        source_entry_sha256=_sha("entry-1"),
        subject_binding_revision_id="binding-1",
        source_taxonomy_version="2026",
        source_context_id="context-1",
        source_unit_id="unit-1",
        decimals="-6",
    )
    candidate = FactResolutionCandidateV2(
        candidate_id="candidate-1",
        idempotency_key="candidate-key-1",
        candidate_set_id="candidate-set-1",
        fact_cell_id=cell.fact_cell_id,
        observation_id=observation.observation_id,
        candidate_ordinal=0,
        eligibility="eligible",
        reason_code="exact_report",
        reason_details=CanonicalJSONObject({}),
        recorded_at=T0,
    )
    resolution = FactResolutionRevisionV2.model_validate(
        {
            "resolution_revision_id": "resolution-1",
            "idempotency_key": "resolution-key-1",
            "fact_cell_id": cell.fact_cell_id,
            "revision": 1,
            "status": "resolved",
            "candidate_set_id": "candidate-set-1",
            "candidates": (candidate,),
            "selected_observation_id": observation.observation_id,
            "policy_name": "exact-evidence-first",
            "policy_version": "v1",
            "policy_config_sha256": _sha("policy"),
            "reason_code": "resolved",
            "reason_details": CanonicalJSONObject({}),
            "knowledge_cutoff": T0,
            "effective_at": T0,
            "recorded_at": T0,
        }
    )
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="publication-1",
            idempotency_key="publication-key-1",
            reported_facts=(ReportedSourceFact(cell=cell, observation=observation),),
            extraction_seals=(
                ExtractionRunCompletenessSealV2(
                    extraction_seal_id="extraction-seal-1",
                    idempotency_key="extraction-seal-key-1",
                    extraction_run_id="run-1",
                    expected_node_count=1,
                    completeness_policy_name="all-run-nodes",
                    completeness_policy_version="v1",
                    completeness_policy_sha256=_sha("completeness"),
                    knowledge_at=T0,
                    recorded_at=T0,
                ),
            ),
            resolutions=(resolution,),
        )
    )


def _seed_hardened_manifest(conn: sqlite3.Connection) -> FactProjectionSpec:
    conn.execute(
        "INSERT INTO search_corpus_manifests "
        "(manifest_id,idempotency_key,corpus_key,revision,"
        "selection_config_sha256,selector_code_version,knowledge_cutoff,"
        "supersedes_manifest_id,recorded_at) "
        "VALUES ('manifest-hardened','manifest-key','company-reports',1,"
        "?,'test-v1',?,NULL,?)",
        (_sha("selection"), T0, T0),
    )
    conn.execute(
        "INSERT INTO search_corpus_document_memberships "
        "(membership_id,manifest_id,expected_document_key,"
        "document_version_id,membership_status,reason,recorded_at) "
        "VALUES ('membership-1','manifest-hardened','document-key-1',"
        "'document-1','included','selected',?)",
        (T0,),
    )
    conn.execute(
        "INSERT INTO search_corpus_manifest_seals "
        "(manifest_id,expected_document_count,membership_digest_sha256,"
        "completion_status,sealed_at) VALUES ('manifest-hardened',1,?,"
        "'complete',?)",
        (_sha("manifest-members"), T0),
    )
    return FactProjectionSpec(
        projection_run_id="projection-hardened",
        idempotency_key="projection-hardened-key",
        projection_key="company-facts",
        revision=1,
        manifest_id="manifest-hardened",
        knowledge_cutoff=T0,
        config_sha256=_sha("projection-config"),
        code_version="test-v1",
        recorded_at=RECORDED,
    )


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE search_corpus_manifests (
            manifest_id TEXT PRIMARY KEY,
            knowledge_cutoff TEXT
        );
        CREATE TABLE search_corpus_manifest_seals (
            manifest_id TEXT PRIMARY KEY,
            completion_status TEXT
        );
        CREATE TABLE search_corpus_document_memberships (
            manifest_id TEXT,
            document_version_id TEXT,
            membership_status TEXT
        );
        CREATE TABLE search_chunks (
            chunk_id TEXT PRIMARY KEY,
            manifest_id TEXT,
            evidence_node_id TEXT,
            text TEXT
        );
        CREATE TABLE fact_cells_v2 (
            fact_cell_id TEXT PRIMARY KEY,
            reporting_entity_id TEXT,
            scope_security_id TEXT,
            semantic_key_sha256 TEXT,
            concept_namespace TEXT,
            concept_name TEXT,
            taxonomy_name TEXT,
            taxonomy_version TEXT,
            accounting_basis TEXT,
            consolidation_scope TEXT,
            period_kind TEXT,
            period_start TEXT,
            period_end TEXT,
            fiscal_year INTEGER,
            fiscal_period TEXT,
            canonical_dimensions_json TEXT,
            canonical_dimensions_sha256 TEXT,
            unit_key TEXT,
            currency TEXT,
            knowledge_at TEXT,
            recorded_at TEXT
        );
        CREATE TABLE fact_resolution_revisions_v2 (
            resolution_revision_id TEXT PRIMARY KEY,
            fact_cell_id TEXT,
            revision INTEGER,
            status TEXT,
            selected_observation_id TEXT,
            candidate_set_id TEXT,
            candidate_count INTEGER,
            candidate_set_digest_sha256 TEXT,
            policy_name TEXT,
            policy_version TEXT,
            knowledge_at TEXT,
            recorded_at TEXT
        );
        CREATE TABLE fact_resolution_candidates_v2 (
            candidate_id TEXT PRIMARY KEY,
            candidate_set_id TEXT,
            fact_cell_id TEXT,
            observation_id TEXT,
            candidate_ordinal INTEGER,
            eligibility TEXT,
            candidate_payload_sha256 TEXT
        );
        CREATE TABLE fact_observations_v2 (
            observation_id TEXT PRIMARY KEY,
            fact_cell_id TEXT,
            observation_kind TEXT,
            value_kind TEXT,
            numeric_value TEXT,
            text_value TEXT,
            is_nil INTEGER,
            raw_lexical_value TEXT,
            document_version_id TEXT,
            evidence_node_id TEXT,
            source_locator_json TEXT,
            source_locator_sha256 TEXT,
            source_entry_sha256 TEXT,
            source_context_id TEXT,
            source_unit_id TEXT,
            decimals TEXT,
            precision TEXT,
            legacy_match_revision_id TEXT,
            formula_id TEXT,
            formula_version TEXT,
            effective_at TEXT,
            knowledge_at TEXT,
            recorded_at TEXT
        );
        CREATE TABLE fact_observation_payload_commitments_v2 (
            observation_id TEXT PRIMARY KEY,
            observation_payload_sha256 TEXT
        );
        CREATE TABLE fact_derivation_seals_v2 (
            derivation_seal_id TEXT,
            output_observation_id TEXT,
            input_count INTEGER,
            canonical_input_digest_sha256 TEXT,
            formula_config_sha256 TEXT,
            knowledge_at TEXT,
            recorded_at TEXT
        );
        CREATE TABLE fact_derivation_input_edges_v2 (
            output_observation_id TEXT,
            input_observation_id TEXT,
            input_resolution_revision_id TEXT,
            input_role TEXT,
            input_ordinal INTEGER
        );
        CREATE TABLE evidence_extraction_runs (
            extraction_run_id TEXT,
            document_version_id TEXT
        );
        CREATE TABLE evidence_nodes (
            node_id TEXT,
            extraction_run_id TEXT
        );
        CREATE TABLE search_fact_projection_runs (
            projection_run_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            projection_key TEXT,
            revision INTEGER,
            manifest_id TEXT,
            knowledge_cutoff TEXT,
            config_sha256 TEXT,
            code_version TEXT,
            supersedes_projection_run_id TEXT,
            recorded_at TEXT
        );
        CREATE TABLE search_fact_projection_memberships (
            membership_id TEXT PRIMARY KEY,
            projection_run_id TEXT,
            fact_cell_id TEXT,
            disposition TEXT,
            resolution_revision_id TEXT,
            reason_code TEXT,
            reason_details_json TEXT,
            membership_bundle_sha256 TEXT,
            recorded_at TEXT
        );
        CREATE TABLE search_fact_projection_rows (
            fact_hit_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            projection_run_id TEXT,
            fact_cell_id TEXT,
            resolution_revision_id TEXT,
            observation_id TEXT,
            reporting_entity_id TEXT,
            scope_security_id TEXT,
            concept_namespace TEXT,
            concept_name TEXT,
            taxonomy_name TEXT,
            taxonomy_version TEXT,
            accounting_basis TEXT,
            consolidation_scope TEXT,
            period_kind TEXT,
            period_start TEXT,
            period_end TEXT,
            fiscal_year INTEGER,
            fiscal_period TEXT,
            canonical_dimensions_json TEXT,
            canonical_dimensions_sha256 TEXT,
            unit_key TEXT,
            currency TEXT,
            observation_kind TEXT,
            value_kind TEXT,
            numeric_value TEXT,
            text_value TEXT,
            is_nil INTEGER,
            raw_lexical_value TEXT,
            candidate_set_id TEXT,
            candidate_count INTEGER,
            candidate_set_digest_sha256 TEXT,
            document_version_id TEXT,
            evidence_node_id TEXT,
            source_locator_json TEXT,
            source_locator_sha256 TEXT,
            source_entry_sha256 TEXT,
            legacy_match_revision_id TEXT,
            derivation_seal_id TEXT,
            derivation_input_count INTEGER,
            derivation_input_digest_sha256 TEXT,
            cell_knowledge_at TEXT,
            observation_knowledge_at TEXT,
            resolution_knowledge_at TEXT,
            row_bundle_json TEXT,
            row_bundle_sha256 TEXT,
            recorded_at TEXT
        );
        CREATE TABLE search_fact_projection_seals (
            projection_seal_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            projection_run_id TEXT UNIQUE,
            manifest_id TEXT,
            eligible_fact_cell_count INTEGER,
            membership_count INTEGER,
            included_count INTEGER,
            unresolved_material_count INTEGER,
            missing_provenance_count INTEGER,
            quarantined_count INTEGER,
            row_count INTEGER,
            membership_set_sha256 TEXT,
            row_set_sha256 TEXT,
            config_sha256 TEXT,
            sealed_at TEXT
        );
        CREATE TABLE ask_retrieval_traces (trace_id TEXT PRIMARY KEY);
        CREATE TABLE ask_retrieval_trace_hits (
            trace_id TEXT,
            rank INTEGER,
            hit_kind TEXT,
            manifest_id TEXT,
            chunk_id TEXT,
            projection_run_id TEXT,
            fact_hit_id TEXT,
            score REAL,
            bundle_sha256 TEXT,
            recorded_at TEXT,
            PRIMARY KEY(trace_id, rank),
            CHECK (
                (hit_kind = 'document' AND manifest_id IS NOT NULL
                    AND chunk_id IS NOT NULL AND projection_run_id IS NULL
                    AND fact_hit_id IS NULL)
                OR
                (hit_kind = 'fact' AND manifest_id IS NULL
                    AND chunk_id IS NULL AND projection_run_id IS NOT NULL
                    AND fact_hit_id IS NOT NULL)
            )
        );
        """
    )


def _seed_projection_inputs(conn: sqlite3.Connection) -> FactProjectionSpec:
    conn.execute(
        "INSERT INTO search_corpus_manifests VALUES (?,?)",
        ("manifest-1", T0),
    )
    conn.execute(
        "INSERT INTO search_corpus_manifest_seals VALUES (?,?)",
        ("manifest-1", "complete"),
    )
    dimensions = [{"key": "geography", "value": "US"}]
    dimension_json = _canonical(dimensions)
    cell_values = (
        "entity-1",
        None,
        _sha("semantic"),
        "us-gaap",
        "Revenue",
        "US-GAAP",
        "2026",
        "us_gaap",
        "consolidated",
        "duration",
        T0 - timedelta(days=365),
        T0,
        2026,
        "FY",
        dimension_json,
        _sha(dimension_json),
        "USD",
        "USD",
        T0,
        T0,
    )
    for cell_id in ("cell-unresolved", "cell-unverifiable"):
        conn.execute(
            "INSERT INTO fact_cells_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cell_id, *cell_values),
        )
    conn.execute(
        "INSERT INTO fact_resolution_revisions_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "resolution-unresolved",
            "cell-unresolved",
            1,
            "unresolved",
            None,
            "set-unresolved",
            0,
            _sha("empty"),
            "policy",
            "v1",
            T0,
            T0,
        ),
    )
    candidate_payload_sha = _sha("caller-controlled")
    candidate_set_payload = [
        {
            "candidate_ordinal": 0,
            "candidate_payload_sha256": candidate_payload_sha,
            "eligibility": "eligible",
            "observation_id": "observation-1",
        }
    ]
    conn.execute(
        "INSERT INTO fact_resolution_candidates_v2 VALUES (?,?,?,?,?,?,?)",
        (
            "candidate-1",
            "set-1",
            "cell-unverifiable",
            "observation-1",
            0,
            "eligible",
            candidate_payload_sha,
        ),
    )
    conn.execute(
        "INSERT INTO fact_resolution_revisions_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "resolution-1",
            "cell-unverifiable",
            1,
            "resolved",
            "observation-1",
            "set-1",
            1,
            _sha(_canonical(candidate_set_payload)),
            "policy",
            "v1",
            T0,
            T0,
        ),
    )
    return FactProjectionSpec(
        projection_run_id="projection-1",
        idempotency_key="projection-key-1",
        projection_key="company-facts",
        revision=1,
        manifest_id="manifest-1",
        knowledge_cutoff=T0,
        config_sha256=_sha("config"),
        code_version="test-v1",
        recorded_at=RECORDED,
    )


def _reported_hit() -> FactHit:
    dimensions = (
        FactDimension(
            axis_namespace="https://example.com/dimensions",
            axis_name="GeographyAxis",
            member_kind="explicit",
            explicit_member_namespace="https://example.com/members",
            explicit_member_name="USMember",
        ),
    )
    dimensions_json = _canonical([dimension.canonical_member for dimension in dimensions])
    locator: dict[str, JsonValue] = {"path": "facts.us-gaap.Revenue.units.USD[0]"}
    return FactHit(
        fact_hit_id="fact-hit-direct",
        projection_run_id="projection-direct",
        fact_cell_id="cell-direct",
        reporting_entity_id="entity-1",
        semantic_key_sha256=_sha("semantic-direct"),
        semantic_key_version="fact_cell_semantic_key.v3",
        concept_namespace="us-gaap",
        concept_name="Revenue",
        taxonomy_name="US-GAAP",
        accounting_basis="us_gaap",
        consolidation_scope="consolidated",
        period_kind="duration",
        period_start=T0 - timedelta(days=365),
        period_end=T0,
        fiscal_year=2026,
        fiscal_period="FY",
        dimensions=dimensions,
        dimensions_sha256=_sha(dimensions_json),
        unit_key="USD",
        currency="USD",
        resolution_revision_id="resolution-direct",
        resolution_revision=1,
        candidate_set_id="candidate-set-direct",
        candidate_count=1,
        candidate_set_digest_sha256=_sha("candidate-set"),
        resolution_policy_name="policy",
        resolution_policy_version="v1",
        observation_id="observation-direct",
        observation_payload_sha256=_sha("observation-direct"),
        observation_kind="reported",
        value_kind="numeric",
        numeric_value=Decimal("1000000000.0000000001"),
        cell_knowledge_at=T0,
        observation_effective_at=T0,
        observation_knowledge_at=T0,
        observation_recorded_at=T0,
        resolution_knowledge_at=T0,
        knowledge_cutoff=T0,
        provenance=ReportedFactEvidence(
            provenance_kind="reported",
            document_version_id="document-1",
            evidence_node_id="node-1",
            source_locator=locator,
            source_locator_sha256=_sha(_canonical(locator)),
            source_entry_sha256=_sha("entry"),
            subject_binding_revision_id="binding-1",
            extraction_run_id="run-1",
            extraction_seal_id="extraction-seal-1",
            source_taxonomy_version="2026",
            anchor_payload_sha256=_sha("anchor"),
        ),
    )


def test_projection_accounts_for_all_cells_and_fails_closed() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    spec = _seed_projection_inputs(conn)
    result = FactSearchProjectionStore(conn).build_projection(spec)

    assert result.seal.eligible_fact_cell_count == 2
    assert result.seal.missing_provenance_count == 2
    assert result.seal.unresolved_material_count == 0
    assert result.seal.quarantined_count == 0
    assert result.seal.included_count == result.seal.row_count == 0
    assert {item.reason_code for item in result.memberships} == {
        "publication_ledger_unavailable",
    }
    replay = FactSearchProjectionStore(conn).build_projection(spec)
    assert replay.created is False
    assert replay.seal == result.seal


def test_repository_published_reported_fact_is_searchable(
    hardened_conn: sqlite3.Connection,
) -> None:
    _publish_hardened_fact(hardened_conn)
    spec = _seed_hardened_manifest(hardened_conn)

    result = FactSearchProjectionStore(hardened_conn).build_projection(spec)

    assert result.seal.included_count == result.seal.row_count == 1
    assert result.seal.missing_provenance_count == 0
    assert result.seal.quarantined_count == 0
    assert len(result.rows) == 1
    hit = result.rows[0]
    assert isinstance(hit, FactHit)
    assert hit.observation_id == "observation-1"
    assert hit.numeric_value == Decimal("100")
    assert hit.semantic_key_version == "fact_cell_semantic_key.v3"
    assert (
        hit.observation_payload_sha256
        == hardened_conn.execute(
            "SELECT observation_payload_sha256 "
            "FROM fact_observation_payload_commitments_v2 "
            "WHERE observation_id = 'observation-1'"
        ).fetchone()[0]
    )
    assert isinstance(hit.provenance, ReportedFactEvidence)
    assert hit.provenance.extraction_seal_id == "extraction-seal-1"
    assert FactSearchProjectionStore(hardened_conn).search(
        spec.projection_run_id,
        FactSearchFilter(concept_names=("Revenue",)),
    ) == (hit,)


@pytest.mark.parametrize(
    ("corruption", "expected_disposition", "expected_reason"),
    [
        (
            "unsealed",
            "missing_provenance",
            "record_not_in_sealed_publication",
        ),
        (
            "tampered",
            "quarantined",
            "publication_member_tampered",
        ),
    ],
)
def test_unsealed_or_tampered_publication_is_disposition_only(
    hardened_conn: sqlite3.Connection,
    corruption: str,
    expected_disposition: str,
    expected_reason: str,
) -> None:
    _publish_hardened_fact(hardened_conn)
    if corruption == "unsealed":
        hardened_conn.execute("DROP TRIGGER trg_source_fact_publication_stream_append_only")
        hardened_conn.execute("DROP TRIGGER trg_source_fact_publication_stream_append_only_delete")
        hardened_conn.execute(
            "DELETE FROM source_fact_publication_stream WHERE publication_id = 'publication-1'"
        )
        hardened_conn.execute("DROP TRIGGER trg_source_fact_publication_seals_append_only_delete")
        hardened_conn.execute(
            "DELETE FROM source_fact_publication_seals WHERE publication_id = 'publication-1'"
        )
    else:
        hardened_conn.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
        hardened_conn.execute(
            "UPDATE source_fact_publication_members "
            "SET record_commitment_sha256 = ? "
            "WHERE publication_id = 'publication-1' "
            "AND record_kind = 'extraction_seal'",
            (_sha("tampered-publication-member"),),
        )
    spec = _seed_hardened_manifest(hardened_conn)

    result = FactSearchProjectionStore(hardened_conn).build_projection(spec)

    assert result.rows == ()
    assert result.seal.included_count == 0
    assert result.memberships[0].disposition == expected_disposition
    assert result.memberships[0].reason_code == expected_reason


@pytest.mark.parametrize("corruption", ["missing", "mismatch"])
def test_missing_or_corrupt_payload_commitment_is_disposition_only(
    hardened_conn: sqlite3.Connection,
    corruption: str,
) -> None:
    _publish_hardened_fact(hardened_conn)
    if corruption == "missing":
        hardened_conn.execute(
            "DROP TRIGGER trg_fact_observation_payload_commitments_v2_append_only_delete"
        )
        hardened_conn.execute(
            "DELETE FROM fact_observation_payload_commitments_v2 "
            "WHERE observation_id = 'observation-1'"
        )
        expected_reason = "publication_member_record_missing"
    else:
        hardened_conn.execute(
            "DROP TRIGGER trg_fact_observation_payload_commitments_v2_append_only"
        )
        hardened_conn.execute(
            "UPDATE fact_observation_payload_commitments_v2 "
            "SET observation_payload_sha256 = ? "
            "WHERE observation_id = 'observation-1'",
            (_sha("corrupt-payload"),),
        )
        expected_reason = "publication_record_commitment_mismatch"
    spec = _seed_hardened_manifest(hardened_conn)

    result = FactSearchProjectionStore(hardened_conn).build_projection(spec)

    assert result.rows == ()
    assert result.seal.included_count == 0
    assert result.seal.quarantined_count == 1
    assert result.memberships[0].reason_code == expected_reason


def test_fact_hit_is_decimal_safe_closed_and_exactly_hashed() -> None:
    hit = _reported_hit()

    assert hit.numeric_value == Decimal("1000000000.0000000001")
    assert hit.row_sha256 == hit.canonical_row_sha256
    with pytest.raises(ValidationError):
        FactHit.model_validate(
            {
                **hit.model_dump(mode="json"),
                "numeric_value": "NaN",
            }
        )
    with pytest.raises(ValidationError):
        FactHit.model_validate(
            {
                **hit.model_dump(mode="json"),
                "chunk_id": "document-chunk-cannot-masquerade-as-fact",
            }
        )


def test_derived_hit_requires_exact_ordered_input_digest() -> None:
    output_id = "derived-observation"
    inputs = (
        DerivationInput(
            input_ordinal=0,
            input_observation_id="input-1",
            input_role="numerator",
        ),
        DerivationInput(
            input_ordinal=1,
            input_observation_id="input-2",
            input_role="denominator",
        ),
    )
    payload = [
        {
            "input_observation_id": item.input_observation_id,
            "input_ordinal": item.input_ordinal,
            "input_resolution_revision_id": item.input_resolution_revision_id,
            "input_role": item.input_role,
            "output_observation_id": output_id,
        }
        for item in inputs
    ]
    lineage = DerivedFactLineage(
        provenance_kind="derived",
        formula_id="gross-margin",
        formula_version="v1",
        derivation_seal_id="seal-1",
        formula_config_sha256=_sha("formula"),
        canonical_input_digest_sha256=_sha(_canonical(payload)),
        derivation_basis_sha256=_sha("basis"),
        input_basis="as_reported",
        formula_definition_sha256=_sha("formula-definition"),
        knowledge_cutoff=T0,
        recorded_at=T0,
        inputs=inputs,
    )
    base = _reported_hit().model_dump(mode="json")
    base.update(
        {
            "fact_hit_id": "fact-hit-derived",
            "observation_id": output_id,
            "observation_payload_sha256": _sha("derived-observation"),
            "observation_kind": "derived",
            "provenance": lineage.model_dump(mode="json"),
            "row_sha256": None,
        }
    )
    hit = FactHit.model_validate(base)
    assert isinstance(hit.provenance, DerivedFactLineage)

    bad = dict(base)
    bad["provenance"] = {
        **lineage.model_dump(mode="json"),
        "canonical_input_digest_sha256": _sha("wrong"),
    }
    with pytest.raises(ValidationError):
        FactHit.model_validate(bad)


def test_structured_search_uses_decimal_not_float_comparison() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    hit = _reported_hit()
    conn.execute(
        "INSERT INTO search_fact_projection_seals "
        "(projection_seal_id,idempotency_key,projection_run_id,manifest_id,"
        "eligible_fact_cell_count,membership_count,included_count,"
        "unresolved_material_count,missing_provenance_count,quarantined_count,"
        "row_count,membership_set_sha256,row_set_sha256,config_sha256,sealed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "seal",
            "seal-key",
            hit.projection_run_id,
            "manifest",
            1,
            1,
            1,
            0,
            0,
            0,
            1,
            _sha("members"),
            _sha("rows"),
            _sha("config"),
            T0,
        ),
    )
    conn.execute(
        "INSERT INTO search_fact_projection_rows "
        "(fact_hit_id,projection_run_id,fact_cell_id,reporting_entity_id,"
        "concept_namespace,concept_name,taxonomy_name,period_end,"
        "canonical_dimensions_json,unit_key,currency,value_kind,numeric_value,"
        "row_bundle_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            hit.fact_hit_id,
            hit.projection_run_id,
            hit.fact_cell_id,
            hit.reporting_entity_id,
            hit.concept_namespace,
            hit.concept_name,
            hit.taxonomy_name,
            hit.period_end,
            _canonical([dimension.model_dump(mode="json") for dimension in hit.dimensions]),
            hit.unit_key,
            hit.currency,
            hit.value_kind,
            str(hit.numeric_value),
            _canonical(hit.model_dump(mode="json")),
        ),
    )
    store = FactSearchProjectionStore(conn)
    included = store.search(
        hit.projection_run_id,
        FactSearchFilter(
            reporting_entity_ids=("entity-1",),
            dimensions=hit.dimensions,
            numeric_min=Decimal("1000000000.00000000009"),
            numeric_max=Decimal("1000000000.00000000011"),
        ),
    )
    excluded = store.search(
        hit.projection_run_id,
        FactSearchFilter(numeric_min=Decimal("1000000000.00000000011")),
    )
    assert included == (hit,)
    assert excluded == ()


def test_heterogeneous_trace_preserves_source_kinds() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    hit = _reported_hit()
    conn.execute("INSERT INTO ask_retrieval_traces VALUES ('trace-1')")
    conn.execute(
        "INSERT INTO search_corpus_manifest_seals VALUES (?,?)",
        ("manifest-1", "complete"),
    )
    conn.execute(
        "INSERT INTO search_chunks VALUES (?,?,?,?)",
        ("chunk-1", "manifest-1", "node-1", "Narrative evidence"),
    )
    conn.execute(
        "INSERT INTO search_fact_projection_seals "
        "(projection_seal_id,idempotency_key,projection_run_id,manifest_id,"
        "eligible_fact_cell_count,membership_count,included_count,"
        "unresolved_material_count,missing_provenance_count,quarantined_count,"
        "row_count,membership_set_sha256,row_set_sha256,config_sha256,sealed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "seal-direct",
            "seal-direct-key",
            hit.projection_run_id,
            "manifest-1",
            1,
            1,
            1,
            0,
            0,
            0,
            1,
            _sha("members"),
            _sha("rows"),
            _sha("config"),
            T0,
        ),
    )
    conn.execute(
        "INSERT INTO search_fact_projection_rows "
        "(fact_hit_id,projection_run_id,fact_cell_id,row_bundle_sha256) "
        "VALUES (?,?,?,?)",
        (
            hit.fact_hit_id,
            hit.projection_run_id,
            hit.fact_cell_id,
            hit.row_sha256,
        ),
    )
    document = DocumentHit(
        manifest_id="manifest-1",
        chunk_id="chunk-1",
        evidence_node_id="node-1",
        text="Narrative evidence",
        bundle_sha256=_sha("document-bundle"),
    )
    FactSearchProjectionStore(conn).persist_trace_hits(
        "trace-1",
        (
            RankedGroundedHit(rank=1, score=0.9, hit=document),
            RankedGroundedHit(rank=2, score=0.8, hit=hit),
        ),
        recorded_at=RECORDED,
    )
    assert conn.execute(
        "SELECT hit_kind,manifest_id,chunk_id,projection_run_id,fact_hit_id "
        "FROM ask_retrieval_trace_hits ORDER BY rank"
    ).fetchall() == [
        ("document", "manifest-1", "chunk-1", None, None),
        ("fact", None, None, "projection-direct", "fact-hit-direct"),
    ]
