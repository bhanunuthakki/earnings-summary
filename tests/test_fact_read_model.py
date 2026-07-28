from __future__ import annotations

import inspect
import runpy
import sqlite3
from collections.abc import Callable, Generator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    DerivedFactObservationV2,
    FactCellV2,
    FactPlaneV2,
    FactResolutionCandidateV2,
    FactResolutionRevisionV2,
    ObservationRelationV2,
    ReportedFactObservationV2,
)
from provenance.fact_read_model import (
    FactAdmissionError,
    FactReadModel,
    FactSelector,
)
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)

_HELPERS = runpy.run_path(str(Path(__file__).with_name("test_source_fact_repository.py")))
STAMP = cast(datetime, _HELPERS["STAMP"])
sha256 = cast(Callable[[str], str], _HELPERS["sha256"])
make_cell = cast(Callable[..., FactCellV2], _HELPERS["make_cell"])
make_report = cast(
    Callable[..., ReportedFactObservationV2],
    _HELPERS["make_report"],
)
make_resolution = cast(
    Callable[..., FactResolutionRevisionV2],
    _HELPERS["make_resolution"],
)
make_publication = cast(
    Callable[..., SourceFactPublication],
    _HELPERS["make_publication"],
)
_conn_factory = cast(
    Callable[[Path], Generator[sqlite3.Connection, None, None]],
    getattr(_HELPERS["conn"], "__wrapped__"),
)


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    yield from _conn_factory(tmp_path)


def test_as_reported_and_provenance_bundle_are_exact_and_decimal(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    read = FactReadModel(conn)
    cell_id = publication.reported_facts[0].cell.fact_cell_id
    observation_id = publication.reported_facts[0].observation.observation_id

    reported = read.as_reported(cell_id, cutoff=STAMP)
    assert len(reported) == 1
    assert reported[0].decimal_value == Decimal("100")
    assert reported[0].currency == "USD"
    assert reported[0].unit_key == "USD"
    assert reported[0].evidence is not None
    assert reported[0].evidence.subject_binding_revision_id == "binding-1"
    assert reported[0].evidence.extraction_seal_id == "extraction-seal-1"

    bundle = read.provenance_bundle(observation_id, cutoff=STAMP)
    assert bundle.observation_payload_sha256 == (reported[0].observation_payload_sha256)
    assert bundle.evidence is not None
    assert bundle.evidence.document_version_id == "document-1"
    assert bundle.evidence.evidence_node_id == "node-1"
    locator_path = bundle.evidence.source_locator.root["path"]
    assert isinstance(locator_path, str)
    assert locator_path.startswith("facts.")
    provenance = bundle.canonical_payload.root["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["anchor_payload_sha256"] == bundle.evidence.anchor_payload_sha256


def test_unresolved_resolution_never_returns_a_canonical_value(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication(status="unresolved")
    SourceFactRepository(conn).publish(publication)
    read = FactReadModel(conn)
    snapshot = read.as_known(
        publication.reported_facts[0].cell.fact_cell_id,
        STAMP,
    )

    assert snapshot.resolution is not None
    assert snapshot.resolution.status == "unresolved"
    assert snapshot.canonical_value is None
    assert len(snapshot.resolution.candidates) == 1
    assert snapshot.resolution.dissent == snapshot.resolution.candidates
    assert (
        read.current_resolved(
            snapshot.cell.fact_cell_id,
            cutoff=STAMP,
        )
        is None
    )


def test_as_known_does_not_leak_a_later_resolution(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    repository = SourceFactRepository(conn)
    repository.publish(publication)
    cell = publication.reported_facts[0].cell
    report = publication.reported_facts[0].observation
    later = STAMP + timedelta(days=1)
    candidate = FactResolutionCandidateV2(
        candidate_id="candidate-later",
        idempotency_key="candidate-key-later",
        candidate_set_id="candidate-set-later",
        fact_cell_id=cell.fact_cell_id,
        observation_id=report.observation_id,
        candidate_ordinal=0,
        eligibility="eligible",
        reason_code="later_review",
        reason_details=CanonicalJSONObject({}),
        recorded_at=later,
    )
    later_resolution = FactResolutionRevisionV2.model_validate(
        {
            "resolution_revision_id": "resolution-later",
            "idempotency_key": "resolution-key-later",
            "fact_cell_id": cell.fact_cell_id,
            "revision": 2,
            "status": "unresolved",
            "candidate_set_id": "candidate-set-later",
            "candidates": (candidate,),
            "selected_observation_id": None,
            "policy_name": "exact-evidence-first",
            "policy_version": "v2",
            "policy_config_sha256": sha256("policy-v2"),
            "reason_code": "material_dissent",
            "reason_details": CanonicalJSONObject({"issue": "scope"}),
            "knowledge_cutoff": later,
            "effective_at": later,
            "recorded_at": later,
            "supersedes_resolution_revision_id": "resolution-1",
        }
    )
    repository.publish(
        SourceFactPublication(
            publication_id="publication-later",
            idempotency_key="publication-key-later",
            resolutions=(later_resolution,),
        )
    )
    read = FactReadModel(conn)

    early = read.as_known(cell.fact_cell_id, STAMP)
    late = read.as_known(cell.fact_cell_id, later)
    assert early.canonical_value is not None
    assert early.resolution is not None
    assert early.resolution.resolution_revision_id == "resolution-1"
    assert late.canonical_value is None
    assert late.resolution is not None
    assert late.resolution.resolution_revision_id == "resolution-later"


def test_series_latest_catalog_coverage_raw_and_relations(
    conn: sqlite3.Connection,
) -> None:
    recent_cell = make_cell("recent")
    old_cell = make_cell("old", period_end=STAMP - timedelta(days=365))
    recent = make_report(recent_cell, "recent", numeric_value="120")
    recent_dissent = make_report(
        recent_cell,
        "recent-dissent",
        numeric_value="119",
    )
    old = make_report(old_cell, "old", numeric_value="80")
    relation = ObservationRelationV2(
        relation_id="relation-series",
        idempotency_key="relation-key-series",
        subject_observation_id=recent.observation_id,
        object_observation_id=recent_dissent.observation_id,
        relation_kind="conflicts_with",
        reason_code="source_disagreement",
        reason_details=CanonicalJSONObject({}),
        policy_name="explicit-recast",
        policy_version="v1",
        policy_config_sha256=sha256("recast-policy"),
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    base = make_publication()
    publication = base.model_copy(
        update={
            "publication_id": "publication-series",
            "idempotency_key": "publication-key-series",
            "reported_facts": (
                ReportedSourceFact(cell=recent_cell, observation=recent),
                ReportedSourceFact(
                    cell=recent_cell,
                    observation=recent_dissent,
                ),
                ReportedSourceFact(cell=old_cell, observation=old),
            ),
            "relations": (relation,),
            "resolutions": (
                make_resolution(recent_cell, recent, "recent"),
                make_resolution(old_cell, old, "old"),
            ),
        }
    )
    SourceFactRepository(conn).publish(publication)
    read = FactReadModel(conn)
    selector = FactSelector(
        reporting_entity_id="reporting-1",
        concept_namespace="us-gaap",
        concept_name="Revenue",
        currency="USD",
    )

    series = read.series(selector, cutoff=STAMP)
    assert tuple(
        item.canonical_value.decimal_value for item in series if item.canonical_value is not None
    ) == (Decimal("80"), Decimal("120"))
    latest = read.latest(selector, cutoff=STAMP)
    assert latest is not None
    assert latest.cell.fact_cell_id == recent_cell.fact_cell_id
    assert len(read.catalog("reporting-1", cutoff=STAMP)) == 2
    coverage = read.coverage("reporting-1", cutoff=STAMP)
    assert coverage.total_cells == 2
    assert coverage.resolved_cells == 2
    assert coverage.complete_reported_observations == 3
    assert len(read.raw_observations(recent_cell.fact_cell_id, cutoff=STAMP)) == 2
    assert read.relations(recent.observation_id, cutoff=STAMP) == (
        read.provenance_bundle(
            recent.observation_id,
            cutoff=STAMP,
        ).relations[0],
    )


def test_direct_fact_plane_write_is_not_admitted(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    plane = FactPlaneV2(conn)
    fact = publication.reported_facts[0]
    plane.persist_cell(fact.cell)
    plane.persist_observation(fact.observation)
    plane.seal_extraction_run(publication.extraction_seals[0])
    plane.persist_resolution(publication.resolutions[0])

    with pytest.raises(FactAdmissionError) as captured:
        FactReadModel(conn).as_known(fact.cell.fact_cell_id, STAMP)

    assert captured.value.reason_code == "record_not_in_sealed_publication"
    assert captured.value.record_kind == "fact_cell"
    assert captured.value.disposition == "missing_provenance"


def test_coverage_fails_closed_on_unpublished_observation(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    cell = publication.reported_facts[0].cell
    FactPlaneV2(conn).persist_observation(
        DerivedFactObservationV2(
            observation_id="observation-unpublished-derived",
            idempotency_key="observation-key-unpublished-derived",
            fact_cell_id=cell.fact_cell_id,
            observation_kind="derived",
            value_kind="numeric",
            numeric_value="101",
            raw_lexical_value="101",
            method_name="deterministic-formula",
            method_version="v1",
            method_config_sha256=sha256("derived-method-config"),
            revision_kind="initial",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            formula_id="revenue-adjustment",
            formula_version="v1",
        )
    )

    with pytest.raises(FactAdmissionError) as captured:
        FactReadModel(conn).coverage("reporting-1", cutoff=STAMP)

    assert captured.value.reason_code == "record_not_in_sealed_publication"
    assert captured.value.record_kind == "fact_observation"
    assert captured.value.record_id == "observation-unpublished-derived"


def test_member_commitment_tampering_quarantines_entire_publication(
    conn: sqlite3.Connection,
) -> None:
    publication = make_publication()
    SourceFactRepository(conn).publish(publication)
    conn.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
    conn.execute(
        "UPDATE source_fact_publication_members "
        "SET record_commitment_sha256 = ? "
        "WHERE record_kind = 'extraction_seal'",
        (sha256("tampered"),),
    )

    with pytest.raises(FactAdmissionError) as captured:
        FactReadModel(conn).as_known(
            publication.reported_facts[0].cell.fact_cell_id,
            STAMP,
        )

    assert captured.value.reason_code == "publication_member_tampered"
    assert captured.value.disposition == "quarantined"


def test_unpublished_relation_makes_bounded_provenance_incomplete(
    conn: sqlite3.Connection,
) -> None:
    cell = make_cell()
    first = make_report(cell, "first")
    second = make_report(cell, "second")
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
    relation = ObservationRelationV2(
        relation_id="relation-unpublished",
        idempotency_key="relation-key-unpublished",
        subject_observation_id=first.observation_id,
        object_observation_id=second.observation_id,
        relation_kind="conflicts_with",
        reason_code="source_disagreement",
        reason_details=CanonicalJSONObject({}),
        policy_name="explicit-recast",
        policy_version="v1",
        policy_config_sha256=sha256("recast-policy"),
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    FactPlaneV2(conn).persist_relation(relation)

    with pytest.raises(FactAdmissionError) as captured:
        FactReadModel(conn).provenance_bundle(
            first.observation_id,
            cutoff=STAMP,
        )

    assert captured.value.reason_code == "record_not_in_sealed_publication"
    assert captured.value.record_kind == "observation_relation"
    assert captured.value.disposition == "missing_provenance"


def test_relation_admission_respects_historical_cutoff(
    conn: sqlite3.Connection,
) -> None:
    cell = make_cell()
    first = make_report(cell, "historical-first")
    second = make_report(cell, "historical-second")
    publication = make_publication().model_copy(
        update={
            "reported_facts": (
                ReportedSourceFact(cell=cell, observation=first),
                ReportedSourceFact(cell=cell, observation=second),
            ),
            "resolutions": (make_resolution(cell, first),),
        }
    )
    repository = SourceFactRepository(conn)
    repository.publish(publication)
    later = STAMP + timedelta(days=1)
    relation = ObservationRelationV2(
        relation_id="relation-later",
        idempotency_key="relation-key-later",
        subject_observation_id=first.observation_id,
        object_observation_id=second.observation_id,
        relation_kind="conflicts_with",
        reason_code="later_review",
        reason_details=CanonicalJSONObject({}),
        policy_name="explicit-recast",
        policy_version="v1",
        policy_config_sha256=sha256("later-recast-policy"),
        effective_at=later,
        knowledge_at=later,
        recorded_at=later,
    )
    repository.publish(
        SourceFactPublication(
            publication_id="publication-later-relation",
            idempotency_key="publication-key-later-relation",
            relations=(relation,),
        )
    )
    read = FactReadModel(conn)

    assert (
        read.provenance_bundle(
            first.observation_id,
            cutoff=STAMP,
        ).relations
        == ()
    )
    assert tuple(
        item.relation_id
        for item in read.provenance_bundle(
            first.observation_id,
            cutoff=later,
        ).relations
    ) == ("relation-later",)


def test_read_model_has_no_legacy_fallback(
    conn: sqlite3.Connection,
) -> None:
    read = FactReadModel(conn)
    selector = FactSelector(
        reporting_entity_id="reporting-1",
        concept_name="Revenue",
    )
    assert read.series(selector, cutoff=STAMP) == ()
    assert read.latest(selector, cutoff=STAMP) is None
    assert read.catalog("reporting-1", cutoff=STAMP) == ()
    source = inspect.getsource(FactReadModel)
    assert "financial_facts" not in source
    assert "kpi_facts" not in source


def test_every_public_read_requires_an_explicit_cutoff() -> None:
    for method_name in (
        "cell",
        "as_reported",
        "as_known",
        "current_resolved",
        "series",
        "latest",
        "catalog",
        "coverage",
        "raw_observations",
        "relations",
        "provenance_bundle",
    ):
        cutoff = inspect.signature(getattr(FactReadModel, method_name)).parameters["cutoff"]
        assert cutoff.default is inspect.Parameter.empty
