"""Reported role review must fail before mutation without exact closed evidence."""

import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from dcf.input_evidence import FactBinding
from dcf.onon_inputs import requirements_for
from dcf.reviewed_input_roles import (
    InputRoleManifest,
    InputRoleReview,
    ReviewedInputRoleError,
    describe_input_role_source,
    review_input_roles,
)
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionSnapshotScope,
)
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    ReportedFactObservationV2,
)
from provenance.fact_read_model import FactReadModel
from provenance.metric_ontology import MetricOntology, OntologySnapshot, canonical_json
from provenance.population_metric_ontology import (
    ExactSourceAdmissionRequest,
    ExactSourceObservationReview,
    admit_exact_source_observations,
)
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from tests.test_source_fact_repository import STAMP, make_cell, make_report, seed_foundation, sha256

NOW = datetime(2026, 10, 4, tzinfo=UTC)


@pytest.mark.parametrize("period_end", [date(2026, 6, 30), date(2025, 12, 31)])
def test_incomplete_analyst_role_manifest_cannot_revise_definitions(period_end: date) -> None:
    manifest = InputRoleManifest(
        issuer_id="issuer",
        financial_period_end=period_end,
        ontology_snapshot_id="ontology",
        canonical_resolution_snapshot_id="resolution",
        source_cutoff_at=NOW - timedelta(seconds=1),
        reviewed_at=NOW,
        recorded_at=NOW,
        reviewer="analyst",
        inputs={},
    )
    with pytest.raises(ReviewedInputRoleError, match="population"):
        review_input_roles(
            sqlite3.connect(":memory:"), manifest.model_dump_json().encode(), as_of=NOW
        )


SOURCE = datetime(2026, 10, 3, 20, tzinfo=UTC)
REVIEW = SOURCE + timedelta(seconds=1)


@pytest.fixture
def reviewed_source(
    tmp_path: Path, migrated_db: Callable[..., Path], request: pytest.FixtureRequest
) -> Generator[tuple[sqlite3.Connection, InputRoleManifest], None, None]:
    path = tmp_path / "reviewed-input-roles.db"
    migrated_db(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    seed_foundation(conn)
    fixture_param: object = getattr(request, "param", None)
    period_end = fixture_param if isinstance(fixture_param, date) else date(2026, 6, 30)
    requirements = requirements_for(period_end)
    reported: list[ReportedSourceFact] = []
    for req in requirements:
        end = datetime.combine(req.period_end or period_end, datetime.min.time(), UTC)
        start = (
            None
            if req.period_start is None
            else datetime.combine(req.period_start, datetime.min.time(), UTC)
        )
        rawcell = make_cell(req.key).model_dump()
        rawcell.update(
            semantic_key_sha256=None,
            concept_namespace="synthetic-onon",
            concept_name=req.role,
            taxonomy_name="Synthetic issuer source",
            accounting_basis=req.accounting_basis,
            period_kind=req.period_kind,
            period_start=start,
            period_end=end,
            fiscal_year=end.year,
            fiscal_period=None,
            dimensions=(),
            unit_key=req.unit_key,
            currency=req.currency,
        )
        cell = FactCellV2.model_validate(rawcell)
        locator = canonical_json({"path": req.key})
        node = f"node-{req.key}"
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,1,'run-1',NULL,NULL,'table_cell',?,?,?,?)",
            (node, node, "100", locator, sha256(locator), STAMP),
        )
        value = "-100" if req.definition_constraints.get("reported_sign") else "100"
        if fixture_param == "positive_expense" and req.key == "sbc_fy":
            value = "100"
        rawreport = make_report(cell, req.key, numeric_value=value).model_dump()
        rawreport.update(
            evidence_node_id=node,
            source_locator=CanonicalJSONObject({"path": req.key}),
            source_locator_sha256=None,
        )
        report = ReportedFactObservationV2.model_validate(rawreport)
        reported.append(ReportedSourceFact(cell=cell, observation=report))
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="roles-source",
            idempotency_key="roles-source",
            reported_facts=tuple(reported),
            extraction_seals=(
                ExtractionRunCompletenessSealV2(
                    extraction_seal_id="roles-extraction",
                    idempotency_key="roles-extraction",
                    extraction_run_id="run-1",
                    expected_node_count=len(requirements) + 1,
                    completeness_policy_name="synthetic-all-source-nodes",
                    completeness_policy_version="1",
                    completeness_policy_sha256="a" * 64,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                ),
            ),
        )
    )
    reader = FactReadModel(conn)
    source_reviews: list[ExactSourceObservationReview] = []
    for fact in reported:
        bundle = reader.provenance_bundle(fact.observation.observation_id, cutoff=SOURCE)
        assert bundle.evidence is not None
        source_reviews.append(
            ExactSourceObservationReview(
                observation_id=fact.observation.observation_id,
                document_version_id=bundle.evidence.document_version_id,
                subject_binding_revision_id=bundle.evidence.subject_binding_revision_id,
                observation_payload_sha256=bundle.observation_payload_sha256,
                source_locator_sha256=bundle.evidence.source_locator_sha256,
                fact_cell_semantic_key_sha256=str(fact.cell.semantic_key_sha256),
            )
        )
    admitted = admit_exact_source_observations(
        conn,
        ExactSourceAdmissionRequest(
            observations=tuple(source_reviews),
            reviewer_identity="synthetic source analyst",
            review_evidence={"source": "explicit fixture review"},
            knowledge_cutoff=SOURCE,
            operation_recorded_at=SOURCE,
        ),
    )
    ontology, resolver = MetricOntology(conn), CanonicalFactResolutionEngine(conn)
    ontology.seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="roles-ontology",
            idempotency_key="roles-ontology",
            cutoff_at=SOURCE,
            recorded_at=SOURCE,
        )
    )
    resolver.seal_snapshot(
        "roles-resolution",
        SOURCE,
        SOURCE,
        ResolutionSnapshotScope(issuer_id="issuer-1", reporting_entity_ids=("reporting-1",)),
    )
    inputs: dict[str, InputRoleReview] = {}
    for req, fact in zip(requirements, reported, strict=True):
        index = admitted.observation_ids.index(fact.observation.observation_id)
        cell_id = admitted.canonical_metric_cell_ids[index]
        metric_id = str(
            conn.execute(
                "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
                (cell_id,),
            ).fetchone()[0]
        )
        definition = ontology.metric_definition_as_known(metric_id, SOURCE)
        assert definition is not None
        bundle = reader.provenance_bundle(fact.observation.observation_id, cutoff=SOURCE)
        reference = FactBinding(
            canonical_metric_cell_id=cell_id,
            metric_id=metric_id,
            metric_definition_revision_id=definition.metric_definition_revision_id,
            canonical_resolution_revision_id=admitted.canonical_resolution_revision_ids[index],
            observation_id=fact.observation.observation_id,
            observation_payload_sha256=bundle.observation_payload_sha256,
        )
        inputs[req.key] = describe_input_role_source(
            conn,
            requirement=req,
            reference=reference,
            cutoff=SOURCE,
            rationale=f"Analyst selects this exact synthetic source as {req.role}; source role and economic scope were explicitly reviewed.",
        )
    manifest = InputRoleManifest(
        issuer_id="issuer-1",
        financial_period_end=period_end,
        ontology_snapshot_id="roles-ontology",
        canonical_resolution_snapshot_id="roles-resolution",
        source_cutoff_at=SOURCE,
        reviewer="synthetic analyst",
        reviewed_at=REVIEW,
        recorded_at=REVIEW,
        inputs=inputs,
    )
    conn.commit()
    try:
        yield conn, manifest
    finally:
        conn.close()


@pytest.mark.parametrize("reviewed_source", [date(2026, 6, 30), date(2025, 12, 31)], indirect=True)
def test_complete_role_review_appends_only_metadata_and_preserves_old_snapshot(
    reviewed_source: tuple[sqlite3.Connection, InputRoleManifest],
) -> None:
    conn, manifest = reviewed_source
    body = manifest.model_dump_json().encode()
    before = conn.execute("SELECT count(*) FROM canonical_metric_definition_revisions").fetchone()[
        0
    ]
    plan = review_input_roles(conn, body, as_of=REVIEW)
    assert plan.mode == "dry_run" and plan.definitions_created == 0
    expected = requirements_for(manifest.financial_period_end)
    assert len(expected) == (23 if manifest.financial_period_end.month == 12 else 45)
    assert set(plan.updated_facts) == {req.key for req in expected}
    assert len(plan.observation_ids) == len(expected)
    assert (
        conn.execute("SELECT count(*) FROM canonical_metric_definition_revisions").fetchone()[0]
        == before
    )
    assert not conn.in_transaction
    conn.execute("BEGIN")
    applied = review_input_roles(
        conn, body, as_of=REVIEW, apply=True, expected_plan_sha256=plan.plan_sha256
    )
    assert applied.definitions_created == len(plan.definitions) > 0
    assert applied.plan_sha256 == plan.plan_sha256 and applied.definitions == plan.definitions
    assert conn.in_transaction
    ontology = MetricOntology(conn)
    ontology.verify_snapshot("roles-ontology")
    for key, revised in applied.updated_facts.items():
        prior = manifest.inputs[key].reference
        assert revised.model_dump(exclude={"metric_definition_revision_id"}) == prior.model_dump(
            exclude={"metric_definition_revision_id"}
        )
        definition = ontology.metric_definition_as_known(revised.metric_id, REVIEW)
        assert definition is not None
        old = ontology.metric_definition_as_known(revised.metric_id, SOURCE)
        assert old is not None
        assert (
            definition.definition_text == old.definition_text and definition.aliases == old.aliases
        )
        assert (
            definition.accounting_basis == old.accounting_basis
            and definition.unit_family == old.unit_family
        )
        assert (
            definition.supersedes_metric_definition_revision_id
            == prior.metric_definition_revision_id
        )
        review_metadata = definition.scope_constraints["valuation_role_review"]
        assert isinstance(review_metadata, dict)
        typed_metadata = cast(dict[str, object], review_metadata)
        assert typed_metadata["attribution"] == "analyst"
    conn.rollback()
    assert (
        conn.execute("SELECT count(*) FROM canonical_metric_definition_revisions").fetchone()[0]
        == before
    )
    conn.execute("BEGIN")
    review_input_roles(conn, body, as_of=REVIEW, apply=True, expected_plan_sha256=plan.plan_sha256)
    conn.commit()
    conn.execute("BEGIN")
    replay = review_input_roles(
        conn, body, as_of=REVIEW, apply=True, expected_plan_sha256=plan.plan_sha256
    )
    assert replay.exact_replay and replay.definitions_created == 0
    conn.rollback()


@pytest.mark.parametrize(
    "field,message",
    [
        ("source_document_sha256", "source_document_changed"),
        ("canonical_cell_sha256", "outside_sealed_semantic_population"),
        ("prior_definition_sha256", "semantic_or_resolution_commitment_changed"),
        ("binding_sha256", "semantic_or_resolution_commitment_changed"),
        ("mapping_sha256", "mapping_commitment_changed"),
    ],
)
def test_exact_source_commitment_drift_fails_without_partial_revision(
    reviewed_source: tuple[sqlite3.Connection, InputRoleManifest], field: str, message: str
) -> None:
    conn, manifest = reviewed_source
    key = next(iter(manifest.inputs))
    inputs = dict(manifest.inputs)
    inputs[key] = inputs[key].model_copy(update={field: "f" * 64})
    changed = manifest.model_copy(update={"inputs": inputs})
    before = conn.execute("SELECT count(*) FROM canonical_metric_definition_revisions").fetchone()[
        0
    ]
    conn.execute("BEGIN")
    with pytest.raises(ReviewedInputRoleError, match=message):
        review_input_roles(
            conn,
            changed.model_dump_json().encode(),
            as_of=REVIEW,
            apply=True,
            expected_plan_sha256="f" * 64,
        )
    assert conn.in_transaction
    assert (
        conn.execute("SELECT count(*) FROM canonical_metric_definition_revisions").fetchone()[0]
        == before
    )
    conn.rollback()


def test_conflicting_same_metric_roles_fail_closed(
    reviewed_source: tuple[sqlite3.Connection, InputRoleManifest],
) -> None:
    conn, manifest = reviewed_source
    a, b = [
        key
        for key, item in manifest.inputs.items()
        if item.requirement.period_kind == "instant"
        and item.requirement.currency == "CHF"
        and not item.requirement.definition_constraints.get("source_scope")
    ][:2]
    inputs = dict(manifest.inputs)
    inputs[b] = describe_input_role_source(
        conn,
        requirement=inputs[b].requirement,
        reference=inputs[a].reference,
        cutoff=SOURCE,
        rationale="Synthetic analyst deliberately supplies a conflicting second role for the identical metric and dimension set.",
    )
    with pytest.raises(ReviewedInputRoleError, match="same_metric_dimension_conflict"):
        review_input_roles(
            conn,
            manifest.model_copy(update={"inputs": inputs}).model_dump_json().encode(),
            as_of=REVIEW,
        )


def test_apply_requires_exact_plan_and_caller_transaction(
    reviewed_source: tuple[sqlite3.Connection, InputRoleManifest],
) -> None:
    conn, manifest = reviewed_source
    body = manifest.model_dump_json().encode()
    with pytest.raises(ReviewedInputRoleError, match="caller_transaction"):
        review_input_roles(conn, body, as_of=REVIEW, apply=True)
    conn.execute("BEGIN")
    with pytest.raises(ReviewedInputRoleError, match="plan_commitment_mismatch"):
        review_input_roles(conn, body, as_of=REVIEW, apply=True, expected_plan_sha256="f" * 64)
    assert conn.in_transaction
    conn.rollback()


def test_expected_definition_head_drift_does_not_overwrite_other_review(
    reviewed_source: tuple[sqlite3.Connection, InputRoleManifest],
) -> None:
    conn, manifest = reviewed_source
    body = manifest.model_dump_json().encode()
    plan = review_input_roles(conn, body, as_of=REVIEW)
    prior_ref = next(iter(manifest.inputs.values())).reference
    ontology = MetricOntology(conn)
    prior = ontology.metric_definition_as_known(prior_ref.metric_id, SOURCE)
    assert prior is not None
    unrelated = prior.model_copy(
        update={
            "metric_definition_revision_id": "other-reviewed-definition",
            "idempotency_key": "other-reviewed-definition",
            "revision": prior.revision + 1,
            "supersedes_metric_definition_revision_id": prior.metric_definition_revision_id,
            "effective_at": REVIEW,
            "knowledge_at": REVIEW,
            "recorded_at": REVIEW,
            "scope_constraints": {
                **prior.scope_constraints,
                "other_review": "different source commitment",
            },
        }
    )
    conn.execute("BEGIN")
    ontology.persist_metric_definition(unrelated)
    with pytest.raises(ReviewedInputRoleError, match="expected_prior_definition_drift"):
        review_input_roles(
            conn, body, as_of=REVIEW, apply=True, expected_plan_sha256=plan.plan_sha256
        )
    assert ontology.metric_definition_as_known(prior.metric_id, REVIEW) == unrelated
    assert (
        conn.execute("SELECT count(*) FROM canonical_metric_definition_revisions").fetchone()[0]
        == len(plan.definitions) + 1
    )
    conn.rollback()


def test_later_binding_selection_drift_blocks_fixed_source_review(
    reviewed_source: tuple[sqlite3.Connection, InputRoleManifest],
) -> None:
    conn, manifest = reviewed_source
    ontology = MetricOntology(conn)
    selected = next(iter(manifest.inputs.values())).reference
    binding = ontology.binding_as_known(selected.observation_id, SOURCE)
    assert binding is not None
    changed = binding.model_copy(
        update={
            "binding_revision_id": "later-role-binding",
            "idempotency_key": "later-role-binding",
            "revision": binding.revision + 1,
            "supersedes_binding_revision_id": binding.binding_revision_id,
            "effective_at": REVIEW,
            "knowledge_at": REVIEW,
            "recorded_at": REVIEW,
        }
    )
    conn.execute("BEGIN")
    ontology.persist_binding(changed)
    with pytest.raises(ReviewedInputRoleError, match="current_source_selection_drift"):
        review_input_roles(conn, manifest.model_dump_json().encode(), as_of=REVIEW)
    conn.rollback()


@pytest.mark.parametrize("reviewed_source", ["positive_expense"], indirect=True)
def test_reported_expense_sign_cannot_be_overridden_by_analyst_role_metadata(
    reviewed_source: tuple[sqlite3.Connection, InputRoleManifest],
) -> None:
    conn, manifest = reviewed_source
    with pytest.raises(ReviewedInputRoleError, match="source_sign_conflict"):
        review_input_roles(conn, manifest.model_dump_json().encode(), as_of=REVIEW)
