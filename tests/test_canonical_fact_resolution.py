# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

from alembic import command
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionPolicy,
    ResolutionSnapshotScope,
)
from provenance.fact_plane_v2 import FactPlaneV2
from provenance.filing_xbrl_extraction_ledger import FilingXbrlExtractionLedger
from provenance.filing_xbrl_fact_adapter import (
    FilingXbrlFactAdapter,
    FilingXbrlNormalizedOutput,
)
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalMetric,
    CanonicalMetricCell,
    CanonicalMetricDefinitionRevision,
    MappingRevision,
    MetricOntology,
    PeriodKind,
    SourceObservationTaxonomyAssertion,
    SourceTaxonomyComponent,
)
from provenance.source_fact_repository import (
    SourceFactPublication,
    SourceFactRepository,
)
from tests.test_filing_xbrl_extraction_ledger import (
    _database,
    _entry,
    _insert_extraction_run,
    _output,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
SCOPE = ResolutionSnapshotScope(
    issuer_id="issuer-1",
    reporting_entity_ids=("reporting-1",),
)


class _DuplicateSnapshotEngine(CanonicalFactResolutionEngine):
    def _latest_resolution_members(
        self,
        cutoff: datetime,
        scope: ResolutionSnapshotScope,
        *,
        recorded_cutoff: datetime | None = None,
    ) -> list[dict[str, object]]:
        members = super()._latest_resolution_members(
            cutoff,
            scope,
            recorded_cutoff=recorded_cutoff,
        )
        return [*members, *members]


def _resolution_database(
    tmp_path: Path,
    output: FilingXbrlNormalizedOutput,
) -> sqlite3.Connection:
    real_upgrade = command.upgrade

    def _bounded_upgrade(config: Config, revision: str) -> None:
        real_upgrade(
            config,
            "0244_canonical_fact_resolution" if revision == "head" else revision,
        )

    command.upgrade = _bounded_upgrade
    try:
        conn = _database(tmp_path, output)
    finally:
        command.upgrade = real_upgrade
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    conn.execute(
        "CREATE TABLE llm_budgets ("
        "purpose TEXT PRIMARY KEY,"
        "monthly_cap_usd REAL NOT NULL,"
        "warn_threshold_pct REAL NOT NULL,"
        "hard_block INTEGER NOT NULL,"
        "on_exceed TEXT NOT NULL,"
        "created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL,"
        "notes TEXT)"
    )
    conn.commit()
    conn.close()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "0255_scoped_canonical_resolution_snapshots")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _component(name: str) -> SourceTaxonomyComponent:
    return SourceTaxonomyComponent(
        component_id=f"component:{name}",
        idempotency_key=f"component:{name}",
        component_kind="concept",
        taxonomy_namespace="https://fasb.org/us-gaap/2026",
        local_name=name,
        taxonomy_name="US GAAP",
        taxonomy_version="2026",
        is_extension=False,
        data_type="monetaryItemType",
        period_type="duration",
        balance="credit",
        is_abstract=False,
        standard_label=name,
        definition_text=name,
        references=(),
        reporting_entity_id="reporting-1",
        evidence_locator={"source": "test"},
        effective_at=NOW,
        knowledge_at=NOW,
        recorded_at=NOW,
    )


def _mapping(component: SourceTaxonomyComponent) -> MappingRevision:
    return MappingRevision(
        mapping_revision_id=f"mapping:{component.local_name}",
        idempotency_key=f"mapping:{component.local_name}",
        source_component_id=component.component_id,
        metric_id="revenue",
        revision=1,
        disposition="equivalent",
        policy_name="test",
        policy_version="v1",
        policy_config_sha256="a" * 64,
        method_name="review",
        method_version="v1",
        constraints={},
        evidence={"test": True},
        reviewer_identity="reviewer@example.test",
        effective_at=NOW,
        knowledge_at=NOW,
        recorded_at=NOW,
    )


def _bind_every_published_cell(conn: sqlite3.Connection) -> str:
    ontology = MetricOntology(conn)
    period_start, period_end = conn.execute(
        "SELECT period_start,period_end FROM fact_cells_v2 LIMIT 1"
    ).fetchone()
    ontology.persist_metric(
        CanonicalMetric(
            metric_id="revenue",
            idempotency_key="metric:revenue",
            canonical_name="Revenue",
            effective_at=NOW,
            knowledge_at=NOW,
            recorded_at=NOW,
        )
    )
    ontology.persist_metric_definition(
        CanonicalMetricDefinitionRevision(
            metric_definition_revision_id="metric:revenue:v1",
            idempotency_key="metric:revenue:v1",
            metric_id="revenue",
            revision=1,
            lifecycle="active",
            definition_text="Revenue",
            aliases=(),
            value_kind="numeric",
            period_kind="duration",
            unit_family="currency",
            accounting_basis="us_gaap",
            scope_constraints={},
            effective_at=NOW,
            knowledge_at=NOW,
            recorded_at=NOW,
        )
    )
    cell = CanonicalMetricCell(
        canonical_metric_cell_id="canonical:revenue",
        idempotency_key="canonical:revenue",
        metric_id="revenue",
        reporting_entity_id="reporting-1",
        scope_security_id=None,
        period_kind="duration",
        period_start=datetime.fromisoformat(str(period_start)),
        period_end=datetime.fromisoformat(str(period_end)),
        dimensions=(),
        unit_family="currency",
        accounting_basis="us_gaap",
        consolidation_scope="consolidated",
        effective_at=NOW,
        knowledge_at=NOW,
        recorded_at=NOW,
    )
    ontology.persist_canonical_metric_cell(cell)
    cells = conn.execute(
        "SELECT DISTINCT c.fact_cell_id,c.concept_name,o.observation_id FROM fact_cells_v2 c JOIN fact_observations_v2 o ON o.fact_cell_id=c.fact_cell_id JOIN filing_xbrl_extraction_dispositions d ON d.observation_id=o.observation_id WHERE d.disposition='published' ORDER BY c.fact_cell_id"
    ).fetchall()
    for ordinal, (fact_cell_id, concept_name, observation_id) in enumerate(cells):
        proof = conn.execute(
            "SELECT anchor.extraction_run_id,cell.taxonomy_name,"
            "anchor.source_taxonomy_version,cell_seal.semantic_key_sha256,"
            "anchor.anchor_payload_sha256,payload.observation_payload_sha256,"
            "run.output_sha256,anchor.raw_entry_sha256,"
            "completeness.observation_set_sha256 "
            "FROM fact_reported_observation_anchors_v2 anchor "
            "JOIN fact_cells_v2 cell ON cell.fact_cell_id=? "
            "JOIN fact_cell_identity_seals_v2 cell_seal "
            "ON cell_seal.fact_cell_id=cell.fact_cell_id "
            "JOIN fact_observation_payload_commitments_v2 payload "
            "ON payload.observation_id=anchor.observation_id "
            "JOIN evidence_extraction_runs run "
            "ON run.extraction_run_id=anchor.extraction_run_id "
            "JOIN fact_extraction_run_completeness_seals_v2 completeness "
            "ON completeness.extraction_run_id=anchor.extraction_run_id "
            "WHERE anchor.observation_id=?",
            (fact_cell_id, observation_id),
        ).fetchone()
        assert proof is not None
        ontology.persist_observation_taxonomy_assertion(
            SourceObservationTaxonomyAssertion(
                observation_id=str(observation_id),
                idempotency_key=f"taxonomy:{observation_id}",
                extraction_run_id=str(proof[0]),
                taxonomy_name=str(proof[1]),
                taxonomy_version=str(proof[2]),
                fact_cell_semantic_key_sha256=str(proof[3]),
                anchor_payload_sha256=str(proof[4]),
                observation_payload_sha256=str(proof[5]),
                extraction_output_sha256=str(proof[6]),
                raw_entry_sha256=str(proof[7]),
                observation_set_sha256=str(proof[8]),
                knowledge_at=NOW,
                recorded_at=NOW,
            )
        )
        component = _component(str(concept_name))
        mapping = _mapping(component)
        ontology.persist_source_component(component)
        ontology.persist_mapping(mapping)
        ontology.persist_binding(
            BindingRevision(
                binding_revision_id=f"binding:{ordinal}",
                idempotency_key=f"binding:{ordinal}",
                fact_cell_id=str(fact_cell_id),
                source_observation_id=str(observation_id),
                revision=1,
                canonical_metric_cell_id=cell.canonical_metric_cell_id,
                mapping_revision_id=mapping.mapping_revision_id,
                source_component_id=component.component_id,
                effective_at=NOW,
                knowledge_at=NOW,
                recorded_at=NOW,
            )
        )
    return cell.canonical_metric_cell_id


def _persist_taxonomy_assertion(
    conn: sqlite3.Connection,
    observation_id: str,
    fact_cell_id: str,
    *,
    idempotency_key: str,
) -> None:
    proof = conn.execute(
        "SELECT anchor.extraction_run_id,cell.taxonomy_name,"
        "anchor.source_taxonomy_version,cell_seal.semantic_key_sha256,"
        "anchor.anchor_payload_sha256,payload.observation_payload_sha256,"
        "run.output_sha256,anchor.raw_entry_sha256,"
        "completeness.observation_set_sha256 "
        "FROM fact_reported_observation_anchors_v2 anchor "
        "JOIN fact_cells_v2 cell ON cell.fact_cell_id=? "
        "JOIN fact_cell_identity_seals_v2 cell_seal "
        "ON cell_seal.fact_cell_id=cell.fact_cell_id "
        "JOIN fact_observation_payload_commitments_v2 payload "
        "ON payload.observation_id=anchor.observation_id "
        "JOIN evidence_extraction_runs run "
        "ON run.extraction_run_id=anchor.extraction_run_id "
        "JOIN fact_extraction_run_completeness_seals_v2 completeness "
        "ON completeness.extraction_run_id=anchor.extraction_run_id "
        "WHERE anchor.observation_id=?",
        (fact_cell_id, observation_id),
    ).fetchone()
    assert proof is not None
    MetricOntology(conn).persist_observation_taxonomy_assertion(
        SourceObservationTaxonomyAssertion(
            observation_id=observation_id,
            idempotency_key=idempotency_key,
            extraction_run_id=str(proof[0]),
            taxonomy_name=str(proof[1]),
            taxonomy_version=str(proof[2]),
            fact_cell_semantic_key_sha256=str(proof[3]),
            anchor_payload_sha256=str(proof[4]),
            observation_payload_sha256=str(proof[5]),
            extraction_output_sha256=str(proof[6]),
            raw_entry_sha256=str(proof[7]),
            observation_set_sha256=str(proof[8]),
            knowledge_at=NOW,
            recorded_at=NOW,
        )
    )


def test_cross_qname_candidates_are_exhaustive_and_conflicts_stay_unresolved(
    tmp_path: Path,
) -> None:
    output = _output(
        (
            _entry(
                0,
                concept_name="Revenue",
                numeric_value=Decimal("100"),
            ),
            _entry(1, concept_name="Sales", numeric_value=Decimal("200")),
        )
    )
    conn = _resolution_database(tmp_path, output)
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        cell = _bind_every_published_cell(conn)
        engine = CanonicalFactResolutionEngine(conn)
        receipt = engine.resolve(
            cell,
            NOW,
            ResolutionPolicy(name="deterministic", version="v1", config={}),
            recorded_at=NOW,
        )
        assert receipt.status == "unresolved"
        assert tuple(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_fact_candidate_dispositions WHERE candidate_universe_id=?",
                (receipt.candidate_universe_id,),
            ).fetchone()
        ) == (2,)
        # There is deliberately no public argument through which either source
        # QName's admitted assertion can be omitted.
        assert "candidates" not in CanonicalFactResolutionEngine.resolve.__annotations__
        assert tuple(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_fact_relation_assertions WHERE relation_set_id=? AND relation_kind='conflicts_with'",
                (receipt.relation_set_id,),
            ).fetchone()
        ) == (2,)
    finally:
        conn.close()


def test_duplicate_entry_is_related_without_inflating_the_candidate_universe(
    tmp_path: Path,
) -> None:
    first = _entry(0, source_entry_sha256="a" * 64)
    output = _output((first, first.model_copy(update={"ordinal": 1})))
    conn = _resolution_database(tmp_path, output)
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        cell = _bind_every_published_cell(conn)
        receipt = CanonicalFactResolutionEngine(conn).resolve(
            cell,
            NOW,
            ResolutionPolicy(name="deterministic", version="v1", config={}),
            recorded_at=NOW,
        )
        assert receipt.status == "resolved"
        assert tuple(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_fact_candidate_dispositions WHERE candidate_universe_id=?",
                (receipt.candidate_universe_id,),
            ).fetchone()
        ) == (1,)
        assert tuple(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_fact_relation_assertions WHERE relation_set_id=? AND relation_kind='exact_duplicate_of'",
                (receipt.relation_set_id,),
            ).fetchone()
        ) == (1,)
    finally:
        conn.close()


def test_non_filing_reported_publication_is_admitted_without_xbrl_fk(
    tmp_path: Path,
) -> None:
    output = _output((_entry(0),))
    conn = _resolution_database(tmp_path, output)
    try:
        xbrl = FilingXbrlFactAdapter().adapt(output)
        FilingXbrlExtractionLedger(conn).publish(output)
        alternate = SourceFactPublication(
            publication_id="zz-reported-publication",
            idempotency_key="zz-reported-publication",
            created_at=NOW,
            recorded_at=NOW,
            reported_facts=xbrl.publication.reported_facts,
        )
        SourceFactRepository(conn).publish(alternate)
        cell = _bind_every_published_cell(conn)
        receipt = CanonicalFactResolutionEngine(conn).resolve(
            cell,
            NOW,
            ResolutionPolicy(name="deterministic", version="v1", config={}),
            recorded_at=NOW,
        )
        row = conn.execute(
            "SELECT source_lane,filing_disposition_id,eligibility "
            "FROM canonical_fact_candidate_dispositions "
            "WHERE candidate_universe_id=?",
            (receipt.candidate_universe_id,),
        ).fetchone()
        assert tuple(row) == (
            "reported_source_publication",
            None,
            "eligible",
        )
    finally:
        conn.close()


def test_later_binding_retirement_creates_complete_resolution_supersession(
    tmp_path: Path,
) -> None:
    output = _output((_entry(0),))
    conn = _resolution_database(tmp_path, output)
    later = NOW + timedelta(hours=1)
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        cell = _bind_every_published_cell(conn)
        engine = CanonicalFactResolutionEngine(conn)
        first = engine.resolve(
            cell,
            NOW,
            ResolutionPolicy(name="deterministic", version="v1", config={}),
            recorded_at=NOW,
        )
        prior = conn.execute(
            "SELECT binding_revision_id,fact_cell_id,source_observation_id,"
            "mapping_revision_id,source_component_id "
            "FROM fact_cell_canonical_binding_revisions LIMIT 1"
        ).fetchone()
        MetricOntology(conn).persist_binding(
            BindingRevision(
                binding_revision_id="binding:retired",
                idempotency_key="binding:retired",
                fact_cell_id=str(prior[1]),
                source_observation_id=str(prior[2]),
                revision=2,
                supersedes_binding_revision_id=str(prior[0]),
                canonical_metric_cell_id=cell,
                mapping_revision_id=str(prior[3]),
                source_component_id=str(prior[4]),
                binding_status="retired",
                effective_at=later,
                knowledge_at=later,
                recorded_at=later,
            )
        )
        second = engine.resolve(
            cell,
            later,
            ResolutionPolicy(name="deterministic", version="v1", config={}),
            recorded_at=later,
        )
        assert second.status == "unresolved"
        revision = conn.execute(
            "SELECT revision,supersedes_resolution_revision_id "
            "FROM canonical_fact_resolution_revisions "
            "WHERE canonical_resolution_revision_id=?",
            (second.canonical_resolution_revision_id,),
        ).fetchone()
        assert tuple(revision) == (
            2,
            first.canonical_resolution_revision_id,
        )
        assert tuple(
            conn.execute(
                "SELECT member_count "
                "FROM canonical_fact_candidate_universe_seals "
                "WHERE candidate_universe_id=?",
                (second.candidate_universe_id,),
            ).fetchone()
        ) == (0,)
    finally:
        conn.close()


def test_candidate_cap_fails_before_any_resolution_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _output((_entry(0), _entry(1, concept_name="Sales")))
    conn = _resolution_database(tmp_path, output)
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        cell = _bind_every_published_cell(conn)
        monkeypatch.setattr(
            "provenance.canonical_fact_resolution.MAX_CANDIDATES_PER_CANONICAL_CELL",
            1,
        )
        with pytest.raises(ValueError, match="exceeds the bounded"):
            CanonicalFactResolutionEngine(conn).resolve(
                cell,
                NOW,
                ResolutionPolicy(
                    name="deterministic",
                    version="v1",
                    config={},
                ),
                recorded_at=NOW,
            )
        assert tuple(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_fact_candidate_universe_revisions"
            ).fetchone()
        ) == (0,)
    finally:
        conn.close()


def test_ineligible_binding_is_sealed_but_never_selected_or_compared(
    tmp_path: Path,
) -> None:
    output = _output((_entry(0, numeric_value=Decimal("100")),))
    conn = _resolution_database(tmp_path, output)
    later = NOW + timedelta(hours=1)
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        canonical_cell = _bind_every_published_cell(conn)
        eligible_binding = conn.execute(
            "SELECT binding_revision_id,fact_cell_id,source_observation_id,"
            "mapping_revision_id,source_component_id "
            "FROM fact_cell_canonical_binding_revisions LIMIT 1"
        ).fetchone()

        foreign_entry = _entry(
            0,
            numeric_value=Decimal("999"),
            source_entry_sha256="f" * 64,
        ).model_copy(
            update={
                "evidence_node_id": "node-unpublished",
                "source_context_id": "context-unpublished",
            }
        )
        foreign_output = _output(
            (foreign_entry,),
            extraction_run_id="run-unpublished",
            extractor_config_sha256="e" * 64,
        )
        _insert_extraction_run(conn, foreign_output)
        foreign = FilingXbrlFactAdapter().adapt(foreign_output)
        source_fact = foreign.publication.reported_facts[0]
        plane = FactPlaneV2(conn)
        plane.persist_cell(source_fact.cell)
        plane.persist_observation(source_fact.observation)
        plane.seal_extraction_run(foreign.publication.extraction_seals[0])
        _persist_taxonomy_assertion(
            conn,
            source_fact.observation.observation_id,
            source_fact.cell.fact_cell_id,
            idempotency_key="taxonomy:unpublished",
        )
        MetricOntology(conn).persist_binding(
            BindingRevision(
                binding_revision_id="binding:unpublished",
                idempotency_key="binding:unpublished",
                fact_cell_id=source_fact.cell.fact_cell_id,
                source_observation_id=source_fact.observation.observation_id,
                revision=1,
                canonical_metric_cell_id=canonical_cell,
                mapping_revision_id=str(eligible_binding[3]),
                source_component_id=str(eligible_binding[4]),
                effective_at=NOW,
                knowledge_at=NOW,
                recorded_at=NOW,
            )
        )
        engine = CanonicalFactResolutionEngine(conn)
        mixed = engine.resolve(
            canonical_cell,
            NOW,
            ResolutionPolicy(name="mixed", version="v1", config={}),
            recorded_at=NOW,
        )
        assert mixed.status == "resolved"
        assert mixed.selected_observation_id == str(eligible_binding[2])
        dispositions = conn.execute(
            "SELECT eligibility,reason_code "
            "FROM canonical_fact_candidate_dispositions "
            "WHERE candidate_universe_id=? ORDER BY candidate_ordinal",
            (mixed.candidate_universe_id,),
        ).fetchall()
        assert {tuple(row) for row in dispositions} == {
            ("ineligible", "missing_sealed_source_publication"),
            ("eligible", "sealed_filing_xbrl_admission"),
        }
        assert tuple(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_fact_relation_assertions "
                "WHERE relation_set_id=? AND relation_kind='conflicts_with'",
                (mixed.relation_set_id,),
            ).fetchone()
        ) == (0,)

        MetricOntology(conn).persist_binding(
            BindingRevision(
                binding_revision_id="binding:eligible-retired",
                idempotency_key="binding:eligible-retired",
                fact_cell_id=str(eligible_binding[1]),
                source_observation_id=str(eligible_binding[2]),
                revision=2,
                supersedes_binding_revision_id=str(eligible_binding[0]),
                canonical_metric_cell_id=canonical_cell,
                mapping_revision_id=str(eligible_binding[3]),
                source_component_id=str(eligible_binding[4]),
                binding_status="retired",
                effective_at=later,
                knowledge_at=later,
                recorded_at=later,
            )
        )
        lone = engine.resolve(
            canonical_cell,
            later,
            ResolutionPolicy(name="mixed", version="v1", config={}),
            recorded_at=later,
        )
        assert lone.status == "unresolved"
        assert lone.selected_observation_id is None
    finally:
        conn.close()


def test_final_seals_reject_omission_and_snapshot_binds_live_latest(
    tmp_path: Path,
) -> None:
    output = _output((_entry(0),))
    conn = _resolution_database(tmp_path, output)
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        cell = _bind_every_published_cell(conn)
        engine = CanonicalFactResolutionEngine(conn)
        receipt = engine.resolve(
            cell,
            NOW,
            ResolutionPolicy(name="deterministic", version="v1", config={}),
            recorded_at=NOW,
        )
        with pytest.raises(sqlite3.IntegrityError):
            _DuplicateSnapshotEngine(conn).seal_snapshot(
                "snapshot-atomic-failure",
                NOW,
                NOW,
                SCOPE,
            )
        assert tuple(
            conn.execute(
                "SELECT COUNT(*) "
                "FROM canonical_fact_resolution_snapshot_members "
                "WHERE resolution_snapshot_id='snapshot-atomic-failure'"
            ).fetchone()
        ) == (0,)
        engine.seal_snapshot("snapshot-1", NOW, NOW, SCOPE)
        engine.verify_snapshot("snapshot-1", NOW)
        engine.resolve(
            cell,
            NOW,
            ResolutionPolicy(
                name="deterministic",
                version="v2",
                config={"revision": 2},
            ),
            recorded_at=NOW,
        )
        with pytest.raises(
            ValueError,
            match="not exhaustive latest-as-known",
        ):
            engine.verify_snapshot("snapshot-1", NOW)
        conn.execute(
            "INSERT INTO canonical_fact_candidate_universe_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "omitted-universe",
                "omitted-universe",
                cell,
                NOW,
                1,
                "[]",
                hashlib.sha256(b"[]").hexdigest(),
                NOW,
                NOW,
                NOW,
            ),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="final seal mismatch",
        ):
            conn.execute(
                "INSERT INTO canonical_fact_candidate_universe_seals VALUES (?,?,?,?,?,?)",
                (
                    "omitted-universe",
                    "omitted-universe-seal",
                    1,
                    "[]",
                    hashlib.sha256(b"[]").hexdigest(),
                    NOW,
                ),
            )
        assert receipt.canonical_resolution_revision_id
    finally:
        conn.close()


def test_resolution_snapshots_are_exactly_issuer_scoped(tmp_path: Path) -> None:
    output = _output((_entry(0),))
    conn = _resolution_database(tmp_path, output)
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        first_cell_id = _bind_every_published_cell(conn)
        engine = CanonicalFactResolutionEngine(conn)
        engine.resolve(
            first_cell_id,
            NOW,
            ResolutionPolicy(name="scoped", version="v1", config={}),
            recorded_at=NOW,
        )
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-2", "issuer-2", "operating_company", NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
            (
                "reporting-2",
                "reporting-2",
                "issuer-2",
                "legal_registrant",
                "Issuer two",
                NOW.isoformat(),
            ),
        )
        first = conn.execute(
            "SELECT metric_id,period_kind,period_start,period_end,unit_family,"
            "accounting_basis,consolidation_scope,effective_at,knowledge_at,recorded_at "
            "FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
            (first_cell_id,),
        ).fetchone()
        assert first is not None
        second_cell_id = "canonical-cell:issuer-2"
        MetricOntology(conn).persist_canonical_metric_cell(
            CanonicalMetricCell(
                canonical_metric_cell_id=second_cell_id,
                idempotency_key=second_cell_id,
                metric_id=str(first[0]),
                reporting_entity_id="reporting-2",
                period_kind=cast(PeriodKind, str(first[1])),
                period_start=(None if first[2] is None else datetime.fromisoformat(str(first[2]))),
                period_end=datetime.fromisoformat(str(first[3])),
                unit_family=str(first[4]),
                accounting_basis=str(first[5]),
                consolidation_scope=str(first[6]),
                effective_at=datetime.fromisoformat(str(first[7])),
                knowledge_at=datetime.fromisoformat(str(first[8])),
                recorded_at=datetime.fromisoformat(str(first[9])),
            )
        )
        engine.resolve(
            second_cell_id,
            NOW,
            ResolutionPolicy(name="scoped", version="v1", config={}),
            recorded_at=NOW,
        )
        second_scope = ResolutionSnapshotScope(
            issuer_id="issuer-2",
            reporting_entity_ids=("reporting-2",),
        )
        first_receipt = engine.seal_snapshot("snapshot:issuer-1", NOW, NOW, SCOPE)
        second_receipt = engine.seal_snapshot("snapshot:issuer-2", NOW, NOW, second_scope)
        assert first_receipt.scope == SCOPE
        assert second_receipt.scope == second_scope
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT canonical_metric_cell_id "
                "FROM canonical_fact_resolution_snapshot_members "
                "WHERE resolution_snapshot_id='snapshot:issuer-1'"
            )
        } == {first_cell_id}
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT canonical_metric_cell_id "
                "FROM canonical_fact_resolution_snapshot_members "
                "WHERE resolution_snapshot_id='snapshot:issuer-2'"
            )
        } == {second_cell_id}
    finally:
        conn.close()


def test_snapshot_uses_separate_knowledge_and_system_clocks(tmp_path: Path) -> None:
    output = _output((_entry(0),))
    conn = _resolution_database(tmp_path, output)
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        cell_id = _bind_every_published_cell(conn)
        recorded_at = NOW + timedelta(hours=1)
        engine = CanonicalFactResolutionEngine(conn)
        engine.resolve(
            cell_id,
            NOW,
            ResolutionPolicy(name="dual-clock", version="v1", config={}),
            recorded_at=recorded_at,
        )
        members = engine._latest_resolution_members(
            NOW,
            SCOPE,
            recorded_cutoff=recorded_at,
        )

        assert [member["canonical_metric_cell_id"] for member in members] == [cell_id]
    finally:
        conn.close()
