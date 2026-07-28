from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from alembic.config import Config

from alembic import command
from src.provenance.fact_identity_policy import admit_fact_identity
from src.provenance.metric_ontology import (
    BindingRevision,
    CanonicalAxis,
    CanonicalDimension,
    CanonicalMember,
    CanonicalMetric,
    CanonicalMetricCell,
    CanonicalMetricDefinitionRevision,
    MappingRevision,
    MetricOntology,
    OntologySnapshot,
    SourceDimensionMappingRevision,
    SourceObservationTaxonomyAssertion,
    SourceTaxonomyComponent,
)

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
T1 = datetime(2026, 7, 27, tzinfo=UTC)
T2 = T1 + timedelta(days=1)
CELL_SEMANTIC_SHA = "1" * 64
ANCHOR_SHA = "2" * 64
OBSERVATION_SHA = "3" * 64
EXTRACTION_OUTPUT_SHA = "4" * 64
RAW_ENTRY_SHA = "5" * 64
OBSERVATION_SET_SHA = "6" * 64


def _ensure_reporting_entity(conn: sqlite3.Connection) -> None:
    at = T1.replace(tzinfo=None).isoformat(sep=" ")
    conn.execute(
        "INSERT INTO issuer_entities "
        "(issuer_id,idempotency_key,entity_kind,created_at) VALUES (?,?,?,?)",
        ("issuer-1", "issuer-1", "operating_company", at),
    )
    conn.execute(
        "INSERT INTO reporting_entities "
        "(reporting_entity_id,idempotency_key,issuer_id,reporting_entity_kind,"
        "display_name,created_at) VALUES (?,?,?,?,?,?)",
        (
            "entity-1",
            "entity-1",
            "issuer-1",
            "legal_registrant",
            "Entity 1",
            at,
        ),
    )


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture(scope="module")
def ontology_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("ontology-template") / "template.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY, source_doc_id INTEGER NOT NULL
        );
        """
    )
    conn.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, "0243_metric_ontology")
    return path


@pytest.fixture
def ontology(
    ontology_template: Path, tmp_path: Path
) -> Generator[tuple[sqlite3.Connection, MetricOntology], None, None]:
    path = tmp_path / "ontology.db"
    shutil.copy2(ontology_template, path)
    conn = sqlite3.connect(path)
    repository = MetricOntology(conn)
    try:
        yield conn, repository
    finally:
        conn.close()


def _metric() -> CanonicalMetric:
    return CanonicalMetric(
        metric_id="revenue",
        idempotency_key="metric:revenue",
        canonical_name="Revenue",
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )


def _definition(
    revision: int = 1,
    *,
    at: datetime = T1,
) -> CanonicalMetricDefinitionRevision:
    return CanonicalMetricDefinitionRevision(
        metric_definition_revision_id=f"metric-definition:revenue:{revision}",
        idempotency_key=f"metric-definition:revenue:{revision}",
        metric_id="revenue",
        revision=revision,
        supersedes_metric_definition_revision_id=(
            None if revision == 1 else f"metric-definition:revenue:{revision - 1}"
        ),
        lifecycle="active",
        definition_text=f"Revenue definition revision {revision}.",
        aliases=("sales",),
        value_kind="numeric",
        period_kind="duration",
        unit_family="currency",
        accounting_basis="us_gaap",
        scope_constraints={},
        effective_at=at,
        knowledge_at=at,
        recorded_at=at,
    )


def _component(
    local_name: str = "Revenue",
    *,
    kind: Literal["concept", "axis", "member"] = "concept",
    version: str = "2025",
    extension: bool = False,
) -> SourceTaxonomyComponent:
    return SourceTaxonomyComponent(
        component_id=f"{kind}:{local_name}:{version}",
        idempotency_key=f"{kind}:{local_name}:{version}",
        component_kind=kind,
        taxonomy_namespace="http://fasb.org/us-gaap",
        local_name=local_name,
        taxonomy_name="us-gaap",
        taxonomy_version=version,
        reporting_entity_id="entity-1" if extension else None,
        is_extension=extension,
        data_type="monetaryItemType" if kind == "concept" else None,
        period_type="duration" if kind == "concept" else None,
        balance="credit" if kind == "concept" else None,
        is_abstract=False if kind == "concept" else None,
        standard_label=local_name,
        definition_text="Exact source definition.",
        references=(),
        evidence_locator={"url": "https://example.test/taxonomy"},
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )


def _mapping(
    component: SourceTaxonomyComponent,
    revision: int = 1,
    *,
    at: datetime = T1,
    disposition: Literal[
        "exact",
        "equivalent",
        "derived",
        "ambiguous",
        "not_applicable",
        "quarantined",
    ] = "equivalent",
) -> MappingRevision:
    return MappingRevision(
        mapping_revision_id=f"mapping:{component.component_id}:{revision}",
        idempotency_key=f"mapping:{component.component_id}:{revision}",
        source_component_id=component.component_id,
        revision=revision,
        supersedes_mapping_revision_id=(
            None if revision == 1 else f"mapping:{component.component_id}:{revision - 1}"
        ),
        metric_id="revenue" if disposition in {"exact", "equivalent", "derived"} else None,
        disposition=disposition,
        policy_name="reviewed_mapping",
        policy_version="v1",
        policy_config_sha256="a" * 64,
        method_name="human_review",
        method_version="v1",
        constraints={},
        evidence={"memo": "reviewed"},
        reviewer_identity="analyst@example.test",
        audited_policy_path=None,
        effective_at=at,
        knowledge_at=at,
        recorded_at=at,
    )


def test_null_scoped_source_identity_is_exactly_unique(
    ontology: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    _, repository = ontology
    first = _component()
    repository.persist_source_component(first)
    duplicate = first.model_copy(
        update={
            "component_id": "concept:duplicate",
            "idempotency_key": "concept:duplicate",
        }
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        repository.persist_source_component(duplicate)


def test_stable_metric_registry_and_cutoff_replay(
    ontology: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    _, repository = ontology
    repository.persist_metric(_metric())
    with pytest.raises(ValueError, match="idempotency conflict"):
        repository.persist_metric(
            _metric().model_copy(update={"canonical_name": "Mutated Revenue"})
        )
    repository.persist_metric_definition(_definition())
    repository.persist_metric_definition(_definition(2, at=T2))

    at_t1 = repository.metric_definition_as_known("revenue", T1)
    at_t2 = repository.metric_definition_as_known("revenue", T2)
    assert at_t1 is not None and at_t1.revision == 1
    assert at_t2 is not None and at_t2.revision == 2

    unknown = CanonicalMetricCell(
        canonical_metric_cell_id="cell:unknown",
        idempotency_key="cell:unknown",
        metric_id="unknown",
        reporting_entity_id="entity-1",
        period_kind="instant",
        period_end=T1,
        dimensions=(),
        unit_family="currency",
        accounting_basis="us_gaap",
        consolidation_scope="consolidated",
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )
    with pytest.raises(ValueError, match="registered metric"):
        repository.persist_canonical_metric_cell(unknown)


def test_mapping_as_known_is_revision_stable_and_extension_is_reviewed(
    ontology: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    conn, repository = ontology
    _ensure_reporting_entity(conn)
    repository.persist_metric(_metric())
    component = _component()
    repository.persist_source_component(component)
    first = _mapping(component)
    second = _mapping(component, 2, at=T2, disposition="quarantined")
    repository.persist_mapping(first)
    repository.persist_mapping(second)
    assert repository.mapping_as_known(component.component_id, T1) == first
    assert repository.mapping_as_known(component.component_id, T2) == second

    extension = _component("IssuerRevenue", extension=True)
    repository.persist_source_component(extension)
    unsafe = _mapping(extension).model_copy(update={"reviewer_identity": None})
    with pytest.raises(ValueError, match="extension"):
        repository.persist_mapping(unsafe)
    with pytest.raises(ValueError, match="extension"):
        admit_fact_identity(extension, unsafe)


def test_metric_mapping_cannot_predate_source_or_metric_registry(
    ontology: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    _, repository = ontology
    repository.persist_metric(_metric())
    future_source = _component("FutureSource").model_copy(
        update={"effective_at": T2, "knowledge_at": T2, "recorded_at": T2}
    )
    repository.persist_source_component(future_source)
    with pytest.raises(sqlite3.IntegrityError, match="predates"):
        repository.persist_mapping(_mapping(future_source))

    source = _component("CurrentSource")
    repository.persist_source_component(source)
    repository.persist_metric(
        CanonicalMetric(
            metric_id="future-metric",
            idempotency_key="metric:future",
            canonical_name="Future Metric",
            effective_at=T2,
            knowledge_at=T2,
            recorded_at=T2,
        )
    )
    early_mapping = _mapping(source).model_copy(
        update={
            "mapping_revision_id": "mapping:future-metric:1",
            "idempotency_key": "mapping:future-metric:1",
            "metric_id": "future-metric",
        }
    )
    with pytest.raises(sqlite3.IntegrityError, match="predates"):
        repository.persist_mapping(early_mapping)


def test_extension_source_component_requires_reporting_entity_scope() -> None:
    extension = _component("IssuerRevenue", extension=True)
    with pytest.raises(ValueError, match="reporting entity scope"):
        SourceTaxonomyComponent.model_validate(
            {**extension.model_dump(), "reporting_entity_id": None}
        )


def test_registry_children_cannot_predate_their_parents(
    ontology: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    conn, repository = ontology
    _ensure_reporting_entity(conn)
    extension = _component("IssuerRevenue", extension=True).model_copy(
        update={
            "effective_at": T1 - timedelta(days=1),
            "knowledge_at": T1 - timedelta(days=1),
            "recorded_at": T1 - timedelta(days=1),
        }
    )
    with pytest.raises(sqlite3.IntegrityError, match="predates"):
        repository.persist_source_component(extension)

    future_axis = CanonicalAxis(
        axis_id="future-axis",
        idempotency_key="future-axis",
        canonical_name="Future Axis",
        effective_at=T2,
        knowledge_at=T2,
        recorded_at=T2,
    )
    repository.persist_axis(future_axis)
    early_member = CanonicalMember(
        member_id="early-member",
        idempotency_key="early-member",
        axis_id=future_axis.axis_id,
        canonical_name="Early Member",
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )
    with pytest.raises(sqlite3.IntegrityError, match="predates"):
        repository.persist_member(early_member)

    source_axis = _component("SourceAxis", kind="axis")
    repository.persist_source_component(source_axis)
    early_dimension_mapping = SourceDimensionMappingRevision(
        dimension_mapping_revision_id="dimension-map:future-axis:1",
        idempotency_key="dimension-map:future-axis:1",
        source_component_id=source_axis.component_id,
        revision=1,
        disposition="equivalent",
        canonical_axis_id=future_axis.axis_id,
        canonical_member_id=None,
        policy_name="reviewed_mapping",
        policy_version="v1",
        policy_config_sha256="b" * 64,
        evidence={"memo": "clock test"},
        reviewer_identity="analyst@example.test",
        audited_policy_path=None,
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )
    with pytest.raises(sqlite3.IntegrityError, match="predates"):
        repository.persist_dimension_mapping(early_dimension_mapping)


def test_canonical_dimensions_require_registries_and_reviewed_source_admission(
    ontology: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    conn, repository = ontology
    _ensure_reporting_entity(conn)
    repository.persist_metric(_metric())
    axis = CanonicalAxis(
        axis_id="geography",
        idempotency_key="axis:geography",
        canonical_name="Geography",
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )
    member = CanonicalMember(
        member_id="united_states",
        idempotency_key="member:united_states",
        axis_id=axis.axis_id,
        canonical_name="United States",
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )
    repository.persist_axis(axis)
    repository.persist_member(member)
    source_axis = _component("GeographyAxis", kind="axis", extension=True)
    repository.persist_source_component(source_axis)
    unsafe = SourceDimensionMappingRevision(
        dimension_mapping_revision_id="dimension-map:axis:1",
        idempotency_key="dimension-map:axis:1",
        source_component_id=source_axis.component_id,
        revision=1,
        disposition="equivalent",
        canonical_axis_id=axis.axis_id,
        canonical_member_id=None,
        policy_name="reviewed_mapping",
        policy_version="v1",
        policy_config_sha256="b" * 64,
        evidence={"memo": "unreviewed extension"},
        reviewer_identity=None,
        audited_policy_path=None,
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )
    with pytest.raises(ValueError, match="extension dimension"):
        repository.persist_dimension_mapping(unsafe)

    cell = CanonicalMetricCell(
        canonical_metric_cell_id="cell:revenue",
        idempotency_key="cell:revenue",
        metric_id="revenue",
        reporting_entity_id="entity-1",
        period_kind="duration",
        period_start=T1 - timedelta(days=90),
        period_end=T1,
        dimensions=(
            CanonicalDimension(
                axis_id="unknown-axis",
                member_id="unknown-member",
            ),
        ),
        unit_family="currency",
        accounting_basis="us_gaap",
        consolidation_scope="consolidated",
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )
    with pytest.raises(ValueError, match="unknown axis/member"):
        repository.persist_canonical_metric_cell(cell)


def test_snapshot_is_active_at_cutoff_and_final(
    ontology: tuple[sqlite3.Connection, MetricOntology],
) -> None:
    conn, repository = ontology
    repository.persist_metric(_metric())
    repository.persist_metric_definition(_definition())
    repository.persist_metric_definition(_definition(2, at=T2))
    snapshot = OntologySnapshot(
        ontology_snapshot_id="snapshot:t1",
        idempotency_key="snapshot:t1",
        cutoff_at=T1,
        recorded_at=T2,
    )
    repository.seal_snapshot(snapshot)
    member_ids = {
        str(row[0])
        for row in conn.execute(
            "SELECT member_id FROM ontology_snapshot_members WHERE ontology_snapshot_id=?",
            (snapshot.ontology_snapshot_id,),
        )
    }
    assert "metric-definition:revenue:1" in member_ids
    assert "metric-definition:revenue:2" not in member_ids
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        conn.execute(
            "INSERT INTO ontology_snapshot_members VALUES (?,?,?,?,?)",
            (snapshot.ontology_snapshot_id, 99, "extra", "extra", "a" * 64),
        )


def test_binding_revision_requires_explicit_parent_and_cutoff() -> None:
    first = BindingRevision(
        binding_revision_id="binding:1",
        idempotency_key="binding:1",
        fact_cell_id="fact-cell",
        source_observation_id="observation",
        revision=1,
        canonical_metric_cell_id="canonical-cell",
        mapping_revision_id="mapping",
        source_component_id="concept",
        effective_at=T1,
        knowledge_at=T1,
        recorded_at=T1,
    )
    assert first.supersedes_binding_revision_id is None
    with pytest.raises(ValueError, match="later revisions require"):
        BindingRevision.model_validate(
            {
                **first.model_dump(),
                "binding_revision_id": "binding:2",
                "idempotency_key": "binding:2",
                "revision": 2,
            }
        )


def _taxonomy_assertion(
    observation_id: str = "observation:1",
    *,
    taxonomy_name: str = "test-taxonomy",
    taxonomy_version: str = "2025",
    extraction_run_id: str = "run-1",
    fact_cell_semantic_key_sha256: str = CELL_SEMANTIC_SHA,
    anchor_payload_sha256: str = ANCHOR_SHA,
    observation_payload_sha256: str = OBSERVATION_SHA,
    extraction_output_sha256: str = EXTRACTION_OUTPUT_SHA,
    raw_entry_sha256: str = RAW_ENTRY_SHA,
    observation_set_sha256: str = OBSERVATION_SET_SHA,
    at: datetime = T1,
) -> SourceObservationTaxonomyAssertion:
    return SourceObservationTaxonomyAssertion(
        observation_id=observation_id,
        idempotency_key=f"taxonomy:{observation_id}:{taxonomy_name}:{taxonomy_version}:"
        f"{extraction_run_id}:{at.isoformat()}",
        extraction_run_id=extraction_run_id,
        taxonomy_name=taxonomy_name,
        taxonomy_version=taxonomy_version,
        fact_cell_semantic_key_sha256=fact_cell_semantic_key_sha256,
        anchor_payload_sha256=anchor_payload_sha256,
        observation_payload_sha256=observation_payload_sha256,
        extraction_output_sha256=extraction_output_sha256,
        raw_entry_sha256=raw_entry_sha256,
        observation_set_sha256=observation_set_sha256,
        knowledge_at=at,
        recorded_at=at,
    )


def _binding_probe(*, persist_assertions: bool = True) -> tuple[sqlite3.Connection, MetricOntology]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE fact_cells_v2 (
            fact_cell_id TEXT PRIMARY KEY,
            concept_namespace TEXT NOT NULL,
            concept_name TEXT NOT NULL,
            taxonomy_name TEXT NOT NULL,
            reporting_entity_id TEXT NOT NULL,
            scope_security_id TEXT,
            period_kind TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT NOT NULL,
            accounting_basis TEXT NOT NULL,
            consolidation_scope TEXT NOT NULL,
            unit_key TEXT NOT NULL,
            currency TEXT,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE fact_observations_v2 (
            observation_id TEXT PRIMARY KEY,
            fact_cell_id TEXT NOT NULL,
            observation_kind TEXT NOT NULL,
            source_entry_sha256 TEXT,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE fact_reported_observation_anchors_v2 (
            observation_id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL,
            source_taxonomy_version TEXT NOT NULL,
            extraction_output_sha256 TEXT NOT NULL,
            raw_entry_sha256 TEXT NOT NULL,
            anchor_payload_sha256 TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE fact_cell_identity_seals_v2 (
            fact_cell_id TEXT PRIMARY KEY,
            semantic_key_sha256 TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE fact_observation_payload_commitments_v2 (
            observation_id TEXT PRIMARY KEY,
            observation_payload_sha256 TEXT NOT NULL,
            committed_at TEXT NOT NULL
        );
        CREATE TABLE evidence_extraction_runs (
            extraction_run_id TEXT PRIMARY KEY,
            output_sha256 TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            outcome TEXT NOT NULL
        );
        CREATE TABLE fact_extraction_run_completeness_seals_v2 (
            extraction_run_id TEXT PRIMARY KEY,
            extraction_output_sha256 TEXT NOT NULL,
            observation_set_json TEXT NOT NULL,
            observation_set_sha256 TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE fact_dimensions_normalized_v2 (
            fact_cell_id TEXT NOT NULL,
            dimension_ordinal INTEGER NOT NULL,
            axis_namespace TEXT NOT NULL,
            axis_name TEXT NOT NULL,
            member_kind TEXT NOT NULL,
            explicit_member_namespace TEXT,
            explicit_member_name TEXT
        );
        CREATE TABLE source_taxonomy_components (
            component_id TEXT PRIMARY KEY,
            component_kind TEXT NOT NULL,
            taxonomy_namespace TEXT NOT NULL,
            local_name TEXT NOT NULL,
            taxonomy_name TEXT NOT NULL,
            taxonomy_version TEXT NOT NULL,
            reporting_entity_scope_key TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE source_observation_taxonomy_assertions (
            observation_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            extraction_run_id TEXT NOT NULL,
            taxonomy_name TEXT NOT NULL,
            taxonomy_version TEXT NOT NULL,
            fact_cell_semantic_key_sha256 TEXT NOT NULL,
            anchor_payload_sha256 TEXT NOT NULL,
            observation_payload_sha256 TEXT NOT NULL,
            extraction_output_sha256 TEXT NOT NULL,
            raw_entry_sha256 TEXT NOT NULL,
            observation_set_sha256 TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            commitment_json TEXT NOT NULL,
            commitment_sha256 TEXT NOT NULL
        );
        CREATE TABLE metric_mapping_revisions (
            mapping_revision_id TEXT PRIMARY KEY,
            source_component_id TEXT NOT NULL,
            metric_id TEXT,
            disposition TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE source_dimension_mapping_revisions (
            source_component_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            disposition TEXT NOT NULL,
            canonical_axis_id TEXT,
            canonical_member_id TEXT,
            policy_name TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            policy_config_sha256 TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            reviewer_identity TEXT,
            audited_policy_path TEXT,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE canonical_metrics (
            metric_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            canonical_name TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            commitment_json TEXT NOT NULL,
            commitment_sha256 TEXT NOT NULL
        );
        CREATE TABLE canonical_metric_cells (
            canonical_metric_cell_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            metric_id TEXT NOT NULL,
            reporting_entity_id TEXT NOT NULL,
            scope_security_id TEXT,
            period_kind TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT NOT NULL,
            dimension_count INTEGER NOT NULL,
            unit_family TEXT NOT NULL,
            accounting_basis TEXT NOT NULL,
            consolidation_scope TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE canonical_metric_cell_seals (
            canonical_metric_cell_id TEXT PRIMARY KEY,
            dimension_set_json TEXT NOT NULL,
            dimension_set_sha256 TEXT NOT NULL,
            semantic_identity_json TEXT NOT NULL,
            semantic_key_sha256 TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE canonical_metric_cell_dimensions (
            canonical_metric_cell_id TEXT NOT NULL,
            dimension_ordinal INTEGER NOT NULL,
            axis_id TEXT NOT NULL,
            member_id TEXT NOT NULL
        );
        CREATE TABLE fact_cell_canonical_binding_revisions (
            binding_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            fact_cell_id TEXT NOT NULL,
            source_observation_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            supersedes_binding_revision_id TEXT,
            canonical_metric_cell_id TEXT,
            mapping_revision_id TEXT,
            source_component_id TEXT,
            binding_status TEXT NOT NULL,
            reason_code TEXT,
            reason_details_json TEXT,
            reason_details_sha256 TEXT,
            commitment_json TEXT NOT NULL,
            commitment_sha256 TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        """
    )
    at = T1.replace(tzinfo=None).isoformat(sep=" ")
    start = (T1 - timedelta(days=90)).replace(tzinfo=None).isoformat(sep=" ")
    conn.execute(
        "INSERT INTO fact_cells_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "fact-cell",
            "urn:test",
            "Revenue",
            "test-taxonomy",
            "entity-1",
            None,
            "duration",
            start,
            at,
            "us_gaap",
            "consolidated",
            "iso4217:USD",
            "USD",
            at,
            at,
            at,
        ),
    )
    conn.executemany(
        "INSERT INTO fact_observations_v2 VALUES (?,?,?,?,?)",
        (
            ("observation:1", "fact-cell", "reported", RAW_ENTRY_SHA, at),
            ("observation:2", "fact-cell", "reported", RAW_ENTRY_SHA, at),
            ("observation:derived", "fact-cell", "derived", None, at),
        ),
    )
    conn.executemany(
        "INSERT INTO fact_reported_observation_anchors_v2 VALUES (?,?,?,?,?,?,?)",
        (
            (
                "observation:1",
                "run-1",
                "2025",
                EXTRACTION_OUTPUT_SHA,
                RAW_ENTRY_SHA,
                ANCHOR_SHA,
                at,
            ),
            (
                "observation:2",
                "run-1",
                "2025",
                EXTRACTION_OUTPUT_SHA,
                RAW_ENTRY_SHA,
                ANCHOR_SHA,
                at,
            ),
        ),
    )
    conn.execute(
        "INSERT INTO fact_cell_identity_seals_v2 VALUES (?,?,?)",
        ("fact-cell", CELL_SEMANTIC_SHA, at),
    )
    conn.executemany(
        "INSERT INTO fact_observation_payload_commitments_v2 VALUES (?,?,?)",
        (
            ("observation:1", OBSERVATION_SHA, at),
            ("observation:2", OBSERVATION_SHA, at),
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?)",
        ("run-1", EXTRACTION_OUTPUT_SHA, at, "succeeded"),
    )
    conn.execute(
        "INSERT INTO fact_extraction_run_completeness_seals_v2 VALUES (?,?,?,?,?,?)",
        (
            "run-1",
            EXTRACTION_OUTPUT_SHA,
            '["observation:1","observation:2"]',
            OBSERVATION_SET_SHA,
            at,
            at,
        ),
    )
    for component_id, local_name, taxonomy_name in (
        ("concept:revenue", "Revenue", "test-taxonomy"),
        ("concept:wrong", "WrongQName", "test-taxonomy"),
        ("concept:wrong-taxonomy", "Revenue", "other-taxonomy"),
    ):
        conn.execute(
            "INSERT INTO source_taxonomy_components VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                component_id,
                "concept",
                "urn:test",
                local_name,
                taxonomy_name,
                "2025",
                "__global__",
                at,
                at,
                at,
            ),
        )
        conn.execute(
            "INSERT INTO metric_mapping_revisions VALUES (?,?,?,?,?,?,?)",
            (
                f"mapping:{component_id}",
                component_id,
                "revenue",
                "equivalent",
                at,
                at,
                at,
            ),
        )
    repository = MetricOntology(conn)
    if persist_assertions:
        for observation_id in ("observation:1", "observation:2"):
            repository.persist_observation_taxonomy_assertion(_taxonomy_assertion(observation_id))
    repository.persist_metric(_metric())
    for cell_id, period_end in (
        ("canonical-cell", T1),
        ("canonical-cell:wrong-period", T1 - timedelta(days=1)),
    ):
        repository.persist_canonical_metric_cell(
            CanonicalMetricCell(
                canonical_metric_cell_id=cell_id,
                idempotency_key=cell_id,
                metric_id="revenue",
                reporting_entity_id="entity-1",
                period_kind="duration",
                period_start=T1 - timedelta(days=90),
                period_end=period_end,
                dimensions=(),
                unit_family="currency",
                accounting_basis="us_gaap",
                consolidation_scope="consolidated",
                effective_at=T1,
                knowledge_at=T1,
                recorded_at=T1,
            )
        )
    return conn, repository


@pytest.mark.parametrize(
    "assertion",
    (
        _taxonomy_assertion(taxonomy_name="invented-taxonomy"),
        _taxonomy_assertion(extraction_run_id="invented-run"),
        _taxonomy_assertion(extraction_output_sha256="f" * 64),
        _taxonomy_assertion(taxonomy_version="invented-version"),
        _taxonomy_assertion(at=T1 - timedelta(seconds=1)),
    ),
    ids=(
        "invented-taxonomy-name",
        "wrong-run",
        "wrong-digest",
        "wrong-version",
        "assertion-before-evidence",
    ),
)
def test_taxonomy_assertion_requires_exact_prior_committed_evidence(
    assertion: SourceObservationTaxonomyAssertion,
) -> None:
    conn, repository = _binding_probe(persist_assertions=False)
    try:
        with pytest.raises(ValueError, match="exact committed fact evidence"):
            repository.persist_observation_taxonomy_assertion(assertion)
        repository.persist_observation_taxonomy_assertion(
            _taxonomy_assertion(assertion.observation_id)
        )
    finally:
        conn.close()


def _binding(
    *,
    binding_id: str = "binding:1",
    revision: int = 1,
    observation_id: str = "observation:1",
    supersedes_binding_id: str | None = None,
    cell_id: str | None = "canonical-cell",
    component_id: str | None = "concept:revenue",
    status: Literal["bound", "quarantined", "retired"] = "bound",
    reason_code: str | None = None,
    reason_details: dict[str, object] | None = None,
    at: datetime = T1,
) -> BindingRevision:
    return BindingRevision(
        binding_revision_id=binding_id,
        idempotency_key=binding_id,
        fact_cell_id="fact-cell",
        source_observation_id=observation_id,
        revision=revision,
        supersedes_binding_revision_id=(
            None if revision == 1 else supersedes_binding_id or "binding:1"
        ),
        canonical_metric_cell_id=cell_id,
        mapping_revision_id=None if component_id is None else f"mapping:{component_id}",
        source_component_id=component_id,
        binding_status=status,
        reason_code=reason_code,
        reason_details=reason_details,
        effective_at=at,
        knowledge_at=at,
        recorded_at=at,
    )


def test_binding_proves_qname_period_and_cutoff_compatibility() -> None:
    conn, repository = _binding_probe()
    try:
        first = _binding()
        repository.persist_binding(first)
        with pytest.raises(ValueError, match="incompatible"):
            repository.persist_binding(
                _binding(
                    binding_id="binding:wrong-period",
                    cell_id="canonical-cell:wrong-period",
                )
            )
        with pytest.raises(ValueError, match="incompatible"):
            repository.persist_binding(
                _binding(
                    binding_id="binding:wrong-qname",
                    component_id="concept:wrong",
                )
            )
        with pytest.raises(ValueError, match="incompatible"):
            repository.persist_binding(
                _binding(
                    binding_id="binding:wrong-taxonomy",
                    component_id="concept:wrong-taxonomy",
                )
            )
        second = _binding(
            binding_id="binding:2",
            revision=2,
            status="retired",
            at=T2,
        )
        repository.persist_binding(second)
        assert repository.binding_as_known("observation:1", T1) == first
        assert repository.binding_as_known("observation:1", T2) == second
    finally:
        conn.close()


def test_two_observations_share_one_cell_and_revise_independently() -> None:
    conn, repository = _binding_probe()
    try:
        first = _binding()
        second = _binding(
            binding_id="binding:observation:2:1",
            observation_id="observation:2",
        )
        repository.persist_binding(first)
        repository.persist_binding(second)
        assert repository.bindings_for_fact_cell_as_known("fact-cell", T1) == (
            first,
            second,
        )

        retired = _binding(
            binding_id="binding:2",
            revision=2,
            status="retired",
            at=T2,
        )
        repository.persist_binding(retired)
        assert repository.bindings_for_fact_cell_as_known("fact-cell", T1) == (
            first,
            second,
        )
        assert repository.bindings_for_fact_cell_as_known("fact-cell", T2) == (
            retired,
            second,
        )
    finally:
        conn.close()


def test_derived_observation_is_terminally_quarantined_not_bound() -> None:
    conn, repository = _binding_probe()
    try:
        with pytest.raises(ValueError, match="committed source"):
            repository.persist_binding(
                _binding(
                    binding_id="binding:derived:unsafe",
                    observation_id="observation:derived",
                )
            )
        quarantined = _binding(
            binding_id="binding:derived:quarantine",
            observation_id="observation:derived",
            cell_id=None,
            component_id=None,
            status="quarantined",
            reason_code="derived_requires_explicit_canonical_basis",
            reason_details={"policy": "reported-anchor-only-v1"},
        )
        repository.persist_binding(quarantined)
        assert repository.binding_as_known("observation:derived", T1) == quarantined
        with pytest.raises(ValueError, match="terminal"):
            repository.persist_binding(
                _binding(
                    binding_id="binding:derived:2",
                    observation_id="observation:derived",
                    revision=2,
                    supersedes_binding_id=quarantined.binding_revision_id,
                    status="retired",
                    at=T2,
                )
            )
    finally:
        conn.close()
