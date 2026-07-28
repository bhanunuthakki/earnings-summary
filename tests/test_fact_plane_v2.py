"""Typed evidence-first fact-plane persistence and replay."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    DerivationInputV2,
    DerivationSealV2,
    DerivedFactObservationV2,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    FactDimensionV2,
    FactPlaneV2,
    FactResolutionCandidateV2,
    FactResolutionRevisionV2,
    ReportedFactObservationV2,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
BASE_REVISION = "0213_decision_draft_provider_id"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    path = tmp_path / "fact-plane-v2.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
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
    legacy.commit()
    legacy.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, "head")

    database = sqlite3.connect(path)
    database.execute("PRAGMA foreign_keys = ON")
    _seed_evidence_foundation(database)
    database.commit()
    try:
        yield database
    finally:
        database.close()


def _seed_evidence_foundation(conn: sqlite3.Connection) -> None:
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
    blob_sha = _sha("filing bytes")
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (blob_sha, 12, "application/json", "file:///isolated/filing.json", STAMP),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,"
        "source_published_at,filing_at,accepted_at,observed_at,retrieved_at,"
        "retrieval_config_sha256,collector_code_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "source-observation-1",
            "source-observation-key-1",
            "sec_companyfacts",
            "https://data.sec.gov/example.json",
            blob_sha,
            STAMP,
            STAMP,
            STAMP,
            STAMP,
            STAMP,
            _sha("retrieval-config"),
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
            "source-observation-1",
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
            "subject-binding-1",
            "subject-binding-key-1",
            "issuer-1",
            1,
            "issuer-1",
            "reporting-1",
            None,
            "selected",
            "deterministic",
            "exact_test_subject",
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
            _sha("extractor-config"),
            "test-v1",
            _sha("extracted-output"),
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
            _sha(locator),
            STAMP,
        ),
    )


def _cell(
    suffix: str = "1",
    *,
    fiscal_year: int = 2026,
    fiscal_period: str = "FY",
    dimensions: tuple[FactDimensionV2, ...] = (
        FactDimensionV2(
            dimension_id="dimension-product",
            idempotency_key="dimension-product-key",
            axis_namespace="https://example.com/dimensions",
            axis_name="ProductAxis",
            member_kind="explicit",
            explicit_member_namespace="https://example.com/members",
            explicit_member_name="CloudMember",
            recorded_at=STAMP,
        ),
        FactDimensionV2(
            dimension_id="dimension-geography",
            idempotency_key="dimension-geography-key",
            axis_namespace="https://example.com/dimensions",
            axis_name="GeographyAxis",
            member_kind="explicit",
            explicit_member_namespace="https://example.com/members",
            explicit_member_name="UnitedStatesMember",
            recorded_at=STAMP,
        ),
    ),
) -> FactCellV2:
    cell_dimensions = tuple(
        dimension.model_copy(
            update={
                "dimension_id": f"{dimension.dimension_id}-{suffix}",
                "idempotency_key": f"{dimension.idempotency_key}-{suffix}",
            }
        )
        for dimension in dimensions
    )
    return FactCellV2.model_validate(
        {
            "fact_cell_id": f"cell-{suffix}",
            "idempotency_key": f"cell-key-{suffix}",
            "reporting_entity_id": "reporting-1",
            "scope_security_id": None,
            "concept_namespace": "us-gaap",
            "concept_name": f"Revenue{'' if suffix in {'1', 'labels'} else suffix}",
            "taxonomy_name": "US GAAP",
            "taxonomy_version": "2026",
            "accounting_basis": "us_gaap",
            "consolidation_scope": "consolidated",
            "period_kind": "duration",
            "period_start": STAMP - timedelta(days=365),
            "period_end": STAMP,
            "fiscal_year": fiscal_year,
            "fiscal_period": fiscal_period,
            "dimensions": cell_dimensions,
            "unit_key": "USD",
            "currency": "usd",
            "effective_at": STAMP,
            "knowledge_at": STAMP,
            "recorded_at": STAMP,
        }
    )


def _reported(
    suffix: str,
    cell_id: str,
    *,
    knowledge_at: datetime = STAMP,
    numeric_value: str = "100",
) -> ReportedFactObservationV2:
    return ReportedFactObservationV2(
        observation_id=f"observation-{suffix}",
        idempotency_key=f"observation-key-{suffix}",
        fact_cell_id=cell_id,
        observation_kind="reported",
        value_kind="numeric",
        numeric_value=numeric_value,
        raw_lexical_value=numeric_value,
        method_name="sec-xbrl",
        method_version="v2",
        method_config_sha256=_sha("method-config"),
        revision_kind="initial",
        effective_at=knowledge_at,
        knowledge_at=knowledge_at,
        recorded_at=knowledge_at,
        document_version_id="document-1",
        evidence_node_id="node-1",
        source_locator=CanonicalJSONObject({"path": "facts.us-gaap.Revenues.units.USD[0]"}),
        source_entry_sha256=_sha(f"source-entry-{suffix}"),
        subject_binding_revision_id="subject-binding-1",
        source_taxonomy_version="2026",
        source_context_id="context-1",
        source_unit_id="unit-1",
        decimals="-6",
        precision=None,
    )


def _candidate(
    suffix: str,
    cell_id: str,
    observation_id: str,
    *,
    candidate_set_id: str,
    ordinal: int,
    recorded_at: datetime = STAMP,
) -> FactResolutionCandidateV2:
    return FactResolutionCandidateV2(
        candidate_id=f"candidate-{suffix}",
        idempotency_key=f"candidate-key-{suffix}",
        candidate_set_id=candidate_set_id,
        fact_cell_id=cell_id,
        observation_id=observation_id,
        candidate_ordinal=ordinal,
        eligibility="eligible",
        reason_code="eligible_report",
        reason_details=CanonicalJSONObject({}),
        recorded_at=recorded_at,
    )


def _resolution(
    suffix: str,
    cell_id: str,
    candidates: tuple[FactResolutionCandidateV2, ...],
    *,
    status: str,
    selected: str | None,
    revision: int = 1,
    knowledge_at: datetime = STAMP,
    supersedes: str | None = None,
) -> FactResolutionRevisionV2:
    return FactResolutionRevisionV2.model_validate(
        {
            "resolution_revision_id": f"resolution-{suffix}",
            "idempotency_key": f"resolution-key-{suffix}",
            "fact_cell_id": cell_id,
            "revision": revision,
            "status": status,
            "candidate_set_id": f"candidate-set-{suffix}",
            "candidates": candidates,
            "selected_observation_id": selected,
            "policy_name": "exact-evidence-first",
            "policy_version": "v1",
            "policy_config_sha256": _sha("resolution-policy"),
            "reason_code": f"{status}_test",
            "reason_details": CanonicalJSONObject({}),
            "knowledge_cutoff": knowledge_at,
            "effective_at": knowledge_at,
            "recorded_at": knowledge_at,
            "supersedes_resolution_revision_id": supersedes,
        }
    )


def test_semantic_key_is_dimension_ordered_and_ignores_fiscal_labels(
    conn: sqlite3.Connection,
) -> None:
    first = _cell()
    reordered = _cell(
        "labels",
        fiscal_year=2025,
        fiscal_period="Q4",
        dimensions=tuple(reversed(first.dimensions)),
    )
    assert tuple(item.canonical_member for item in first.dimensions) == tuple(
        item.canonical_member for item in reordered.dimensions
    )
    assert first.semantic_key_sha256 == reordered.semantic_key_sha256

    plane = FactPlaneV2(conn)
    assert plane.persist_cell(first).created
    with pytest.raises(ValueError, match="conflicts"):
        plane.persist_cell(reordered)
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (1,)


def test_reported_contract_requires_evidence_and_round_trips_source_context(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    cell = _cell()
    plane.persist_cell(cell)
    report = _reported("1", cell.fact_cell_id)
    assert plane.persist_observation(report).created
    loaded = plane.as_reported(cell.fact_cell_id)
    assert loaded.observations == (report,)
    assert loaded.observations[0].source_context_id == "context-1"
    assert loaded.observations[0].decimals == "-6"

    payload = report.model_dump()
    payload.pop("evidence_node_id")
    with pytest.raises(ValidationError):
        ReportedFactObservationV2.model_validate(payload)
    with pytest.raises(ValidationError):
        FactCellV2.model_validate({**cell.model_dump(), "ticker": "TEST"})


def test_derivation_inputs_are_atomically_sealed_with_exact_digest(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    input_cell = _cell()
    output_cell = _cell("derived")
    plane.persist_cell(input_cell)
    plane.persist_cell(output_cell)
    source = _reported("input", input_cell.fact_cell_id)
    plane.persist_observation(source)
    derived = DerivedFactObservationV2(
        observation_id="observation-derived",
        idempotency_key="observation-key-derived",
        fact_cell_id=output_cell.fact_cell_id,
        observation_kind="derived",
        value_kind="numeric",
        numeric_value="200",
        raw_lexical_value=None,
        method_name="formula-engine",
        method_version="v1",
        method_config_sha256=_sha("formula-method"),
        revision_kind="initial",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
        formula_id="double-input",
        formula_version="v1",
    )
    plane.persist_observation(derived)
    assert conn.execute(
        "SELECT COUNT(*) FROM fact_observation_payload_commitments_v2 WHERE observation_id = ?",
        (derived.observation_id,),
    ).fetchone() == (0,)
    edge = DerivationInputV2(
        edge_id="edge-1",
        idempotency_key="edge-key-1",
        derived_observation_id=derived.observation_id,
        input_position=0,
        input_observation_id=source.observation_id,
        input_role="base_value",
        recorded_at=STAMP,
    )
    seal = DerivationSealV2(
        derivation_seal_id="seal-1",
        idempotency_key="seal-key-1",
        derived_observation_id=derived.observation_id,
        ordered_inputs=(edge,),
        input_basis="as_reported",
        formula_definition_sha256=_sha("double-input-formula"),
        formula_config_sha256=_sha("formula-config"),
        seal_method="canonical-json",
        seal_method_version="v1",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    assert plane.finalize_derivation(seal).created
    assert not plane.finalize_derivation(seal).created
    stored = conn.execute(
        "SELECT canonical_input_digest_sha256 FROM fact_derivation_seals_v2"
    ).fetchone()
    assert stored == (seal.canonical_inputs_sha256,)
    payload_json = conn.execute(
        "SELECT canonical_payload_json "
        "FROM fact_observation_payload_commitments_v2 "
        "WHERE observation_id = ?",
        (derived.observation_id,),
    ).fetchone()[0]
    assert json.loads(str(payload_json))["provenance"] == {
        "canonical_input_digest_sha256": seal.canonical_inputs_sha256,
        "derivation_basis_sha256": conn.execute(
            "SELECT canonical_basis_sha256 "
            "FROM fact_derivation_basis_commitments_v2 "
            "WHERE derivation_seal_id = ?",
            (seal.derivation_seal_id,),
        ).fetchone()[0],
        "derivation_seal_id": seal.derivation_seal_id,
        "formula_id": derived.formula_id,
        "formula_version": derived.formula_version,
    }
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        conn.execute(
            "INSERT INTO fact_derivation_input_edges_v2 VALUES (?,?,?,?,?,?,?,?)",
            (
                "edge-late",
                "edge-key-late",
                derived.observation_id,
                source.observation_id,
                None,
                "late",
                1,
                STAMP,
            ),
        )


def test_resolution_rejects_cross_cell_candidate_atomically(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    first = _cell()
    second = _cell("2")
    plane.persist_cell(first)
    plane.persist_cell(second)
    other_report = _reported("other", second.fact_cell_id)
    plane.persist_observation(other_report)
    candidate = _candidate(
        "cross",
        first.fact_cell_id,
        other_report.observation_id,
        candidate_set_id="candidate-set-cross",
        ordinal=0,
    )
    resolution = _resolution(
        "cross",
        first.fact_cell_id,
        (candidate,),
        status="resolved",
        selected=other_report.observation_id,
    )
    with pytest.raises(sqlite3.IntegrityError, match="belong to fact cell"):
        plane.persist_resolution(resolution)
    assert conn.execute("SELECT COUNT(*) FROM fact_resolution_candidates_v2").fetchone() == (0,)


def test_unresolved_as_known_has_candidates_but_no_canonical_value(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    cell = _cell()
    plane.persist_cell(cell)
    report = _reported("unresolved", cell.fact_cell_id)
    plane.persist_observation(report)
    candidate = _candidate(
        "unresolved",
        cell.fact_cell_id,
        report.observation_id,
        candidate_set_id="candidate-set-unresolved",
        ordinal=0,
    )
    resolution = _resolution(
        "unresolved",
        cell.fact_cell_id,
        (candidate,),
        status="unresolved",
        selected=None,
    )
    plane.persist_resolution(resolution)
    result = plane.as_known(cell.fact_cell_id, STAMP + timedelta(minutes=1))
    assert result.resolution is not None
    assert result.resolution.resolution_revision_id == resolution.resolution_revision_id
    assert result.resolution.candidates[0].candidate_payload_sha256 is not None
    assert result.candidates == (report,)
    assert result.canonical_observation is None


def test_as_known_rejects_cell_that_did_not_exist_at_cutoff(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    cell = _cell()
    plane.persist_cell(cell)
    with pytest.raises(ValueError, match="was not known"):
        plane.as_known(cell.fact_cell_id, STAMP - timedelta(seconds=1))


def test_as_known_fails_closed_instead_of_dropping_future_candidate(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    cell = _cell()
    plane.persist_cell(cell)
    future = _reported(
        "future-integrity",
        cell.fact_cell_id,
        knowledge_at=STAMP + timedelta(days=1),
    )
    plane.persist_observation(future)
    candidate = _candidate(
        "future-integrity",
        cell.fact_cell_id,
        future.observation_id,
        candidate_set_id="candidate-set-future-integrity",
        ordinal=0,
    )
    resolution = _resolution(
        "future-integrity",
        cell.fact_cell_id,
        (candidate,),
        status="unresolved",
        selected=None,
    )
    # Simulate a corrupt/imported database that bypassed the cross-clock guard.
    conn.execute("DROP TRIGGER trg_fact_resolution_revisions_v2_candidates")
    plane.persist_resolution(resolution)
    with pytest.raises(ValueError, match="candidate set is incomplete"):
        plane.as_known(cell.fact_cell_id, STAMP)


def test_as_known_replays_resolution_revision_at_cutoff(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    cell = _cell()
    plane.persist_cell(cell)
    first = _reported("early", cell.fact_cell_id)
    later_time = STAMP + timedelta(days=1)
    second = _reported(
        "later",
        cell.fact_cell_id,
        knowledge_at=later_time,
        numeric_value="125",
    )
    plane.persist_observation(first)
    plane.persist_observation(second)
    first_candidate = _candidate(
        "early",
        cell.fact_cell_id,
        first.observation_id,
        candidate_set_id="candidate-set-early",
        ordinal=0,
    )
    first_resolution = _resolution(
        "early",
        cell.fact_cell_id,
        (first_candidate,),
        status="resolved",
        selected=first.observation_id,
    )
    plane.persist_resolution(first_resolution)
    later_candidate = _candidate(
        "later",
        cell.fact_cell_id,
        second.observation_id,
        candidate_set_id="candidate-set-later",
        ordinal=0,
        recorded_at=later_time,
    )
    later_resolution = _resolution(
        "later",
        cell.fact_cell_id,
        (later_candidate,),
        status="resolved",
        selected=second.observation_id,
        revision=2,
        knowledge_at=later_time,
        supersedes=first_resolution.resolution_revision_id,
    )
    plane.persist_resolution(later_resolution)

    early_view = plane.as_known(cell.fact_cell_id, STAMP + timedelta(hours=1))
    late_view = plane.as_known(cell.fact_cell_id, later_time)
    assert early_view.canonical_observation == first
    assert late_view.canonical_observation == second


def test_exact_replay_or_conflict_and_append_only_guards(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    cell = _cell()
    assert plane.persist_cell(cell).created
    assert not plane.persist_cell(cell).created
    conflicting = FactCellV2.model_validate(
        {
            **cell.model_dump(),
            "concept_name": "DifferentConcept",
            "semantic_key_sha256": None,
        }
    )
    with pytest.raises(ValueError, match="conflicts"):
        plane.persist_cell(conflicting)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE fact_cells_v2 SET fiscal_year = 2025 WHERE fact_cell_id = ?",
            (cell.fact_cell_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM fact_cells_v2 WHERE fact_cell_id = ?",
            (cell.fact_cell_id,),
        )


def test_semantic_key_v3_excludes_source_taxonomy_version(
    conn: sqlite3.Connection,
) -> None:
    first = _cell("taxonomy-a")
    second = FactCellV2.model_validate(
        {
            **first.model_dump(),
            "fact_cell_id": "cell-taxonomy-b",
            "idempotency_key": "cell-key-taxonomy-b",
            "taxonomy_version": "2027",
            "semantic_key_sha256": None,
            "dimensions": tuple(
                dimension.model_copy(
                    update={
                        "dimension_id": f"{dimension.dimension_id}-b",
                        "idempotency_key": (f"{dimension.idempotency_key}-b"),
                    }
                )
                for dimension in first.dimensions
            ),
        }
    )
    assert first.semantic_key_version == "fact_cell_semantic_key.v3"
    assert first.semantic_key_sha256 == second.semantic_key_sha256

    plane = FactPlaneV2(conn)
    plane.persist_cell(first)
    with pytest.raises(ValueError, match="conflicts"):
        plane.persist_cell(second)


def test_typed_dimension_is_normalized_and_identity_sealed(
    conn: sqlite3.Connection,
) -> None:
    typed = FactDimensionV2(
        dimension_id="dimension-typed",
        idempotency_key="dimension-typed-key",
        axis_namespace="https://example.com/dimensions",
        axis_name="ScenarioAxis",
        member_kind="typed",
        typed_member_value=CanonicalJSONObject({"scenario": "downside", "version": 2}),
        recorded_at=STAMP,
    )
    cell = _cell("typed", dimensions=(typed,))
    plane = FactPlaneV2(conn)
    plane.persist_cell(cell)
    assert typed.typed_member_value is not None

    row = conn.execute(
        "SELECT member_kind,typed_member_value_json,"
        "typed_member_value_sha256 FROM fact_dimensions_normalized_v2"
    ).fetchone()
    assert row == (
        "typed",
        typed.typed_member_value.canonical_json,
        typed.typed_member_value.canonical_sha256,
    )
    assert plane.as_reported(cell.fact_cell_id).cell == cell


def test_reported_fact_rejects_nonmatching_selected_subject_binding(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "reporting-2",
            "reporting-key-2",
            "issuer-1",
            "legal_registrant",
            "Issuer One Other Registrant",
            STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "subject-binding-2",
            "subject-binding-key-2",
            "issuer-1",
            2,
            "issuer-1",
            "reporting-2",
            None,
            "selected",
            "deterministic",
            "wrong_reporting_subject",
            "{}",
            0,
            STAMP,
            STAMP,
            STAMP,
            "subject-binding-1",
        ),
    )
    plane = FactPlaneV2(conn)
    cell = _cell("wrong-subject")
    plane.persist_cell(cell)
    report = _reported("wrong-subject", cell.fact_cell_id).model_copy(
        update={"subject_binding_revision_id": "subject-binding-2"}
    )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="exact selected subject",
    ):
        plane.persist_observation(report)
    assert conn.execute(
        "SELECT COUNT(*) FROM fact_observations_v2 WHERE observation_id = ?",
        (report.observation_id,),
    ).fetchone() == (0,)


def test_reported_fact_accepts_exact_alias_to_distinct_canonical_issuer(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-canonical", "issuer-key-canonical", "operating_company", STAMP),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "reporting-canonical",
            "reporting-key-canonical",
            "issuer-canonical",
            "legal_registrant",
            "Canonical Issuer Registrant",
            STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "subject-binding-alias",
            "subject-binding-key-alias",
            "issuer-1",
            2,
            "issuer-canonical",
            "reporting-canonical",
            None,
            "selected",
            "deterministic",
            "recorded_subject_alias",
            "{}",
            0,
            STAMP,
            STAMP,
            STAMP,
            "subject-binding-1",
        ),
    )
    cell = FactCellV2.model_validate(
        {
            **_cell("alias").model_dump(),
            "reporting_entity_id": "reporting-canonical",
            "semantic_key_sha256": None,
        }
    )
    plane = FactPlaneV2(conn)
    plane.persist_cell(cell)
    report = _reported("alias", cell.fact_cell_id).model_copy(
        update={"subject_binding_revision_id": "subject-binding-alias"}
    )
    assert plane.persist_observation(report).created
    assert conn.execute(
        "SELECT recorded_issuer_id,bound_issuer_id,"
        "bound_reporting_entity_id "
        "FROM v_fact_reported_anchors_selected_v2 "
        "WHERE observation_id = ?",
        (report.observation_id,),
    ).fetchone() == (
        "issuer-1",
        "issuer-canonical",
        "reporting-canonical",
    )


def test_reported_fact_rejects_subject_binding_from_the_future(
    conn: sqlite3.Connection,
) -> None:
    future = STAMP + timedelta(days=1)
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "subject-binding-future",
            "subject-binding-key-future",
            "issuer-1",
            2,
            "issuer-1",
            "reporting-1",
            None,
            "selected",
            "deterministic",
            "future_binding",
            "{}",
            0,
            future,
            future,
            future,
            "subject-binding-1",
        ),
    )
    plane = FactPlaneV2(conn)
    cell = _cell("future-binding")
    plane.persist_cell(cell)
    report = _reported("future-binding", cell.fact_cell_id).model_copy(
        update={"subject_binding_revision_id": "subject-binding-future"}
    )
    with pytest.raises(sqlite3.IntegrityError, match="exact selected subject"):
        plane.persist_observation(report)


def test_candidate_payload_is_internal_and_database_enforced(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    cell = _cell("payload")
    plane.persist_cell(cell)
    report = _reported("payload", cell.fact_cell_id)
    plane.persist_observation(report)
    committed_sha = conn.execute(
        "SELECT observation_payload_sha256 "
        "FROM fact_observation_payload_commitments_v2 "
        "WHERE observation_id = ?",
        (report.observation_id,),
    ).fetchone()[0]

    malicious = _candidate(
        "payload-bad",
        cell.fact_cell_id,
        report.observation_id,
        candidate_set_id="candidate-set-payload-bad",
        ordinal=0,
    ).model_copy(update={"candidate_payload_sha256": _sha("forged")})
    resolution = _resolution(
        "payload-bad",
        cell.fact_cell_id,
        (malicious,),
        status="resolved",
        selected=report.observation_id,
    )
    with pytest.raises(ValueError, match="internal commitment"):
        plane.persist_resolution(resolution)

    good = _candidate(
        "payload-good",
        cell.fact_cell_id,
        report.observation_id,
        candidate_set_id="candidate-set-payload-good",
        ordinal=0,
    )
    plane.persist_resolution(
        _resolution(
            "payload-good",
            cell.fact_cell_id,
            (good,),
            status="resolved",
            selected=report.observation_id,
        )
    )
    assert conn.execute(
        "SELECT candidate_payload_sha256 "
        "FROM fact_resolution_candidates_v2 "
        "WHERE candidate_id = 'candidate-payload-good'"
    ).fetchone() == (committed_sha,)


def test_extraction_completeness_seal_freezes_exact_run_sets(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    cell = _cell("extraction-seal")
    plane.persist_cell(cell)
    report = _reported("extraction-seal", cell.fact_cell_id)
    plane.persist_observation(report)
    seal = ExtractionRunCompletenessSealV2(
        extraction_seal_id="extraction-seal-1",
        idempotency_key="extraction-seal-key-1",
        extraction_run_id="run-1",
        expected_node_count=1,
        completeness_policy_name="all-succeeded-run-nodes",
        completeness_policy_version="v1",
        completeness_policy_sha256=_sha("completeness-policy"),
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    assert plane.seal_extraction_run(seal).created
    assert not plane.seal_extraction_run(seal).created
    assert conn.execute(
        "SELECT observed_node_count,reported_fact_count "
        "FROM fact_extraction_run_completeness_seals_v2"
    ).fetchone() == (1, 1)

    with pytest.raises(sqlite3.IntegrityError, match="run is sealed"):
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "node-late",
                "node-key-late",
                1,
                "run-1",
                None,
                None,
                "table_cell",
                "late",
                '{"path":"late"}',
                _sha('{"path":"late"}'),
                STAMP,
            ),
        )


def test_derivation_basis_rejects_effective_date_lookahead(
    conn: sqlite3.Connection,
) -> None:
    plane = FactPlaneV2(conn)
    input_cell = _cell("future-input")
    output_cell = _cell("early-output")
    plane.persist_cell(input_cell)
    plane.persist_cell(output_cell)
    future = _reported(
        "future-input",
        input_cell.fact_cell_id,
        knowledge_at=STAMP + timedelta(days=1),
    )
    plane.persist_observation(future)
    derived = DerivedFactObservationV2(
        observation_id="observation-early-derived",
        idempotency_key="observation-key-early-derived",
        fact_cell_id=output_cell.fact_cell_id,
        observation_kind="derived",
        value_kind="numeric",
        numeric_value="200",
        raw_lexical_value=None,
        method_name="formula-engine",
        method_version="v1",
        method_config_sha256=_sha("formula-method"),
        revision_kind="initial",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
        formula_id="bad-lookahead",
        formula_version="v1",
    )
    plane.persist_observation(derived)
    later = STAMP + timedelta(days=1)
    edge = DerivationInputV2(
        edge_id="edge-lookahead",
        idempotency_key="edge-key-lookahead",
        derived_observation_id=derived.observation_id,
        input_position=0,
        input_observation_id=future.observation_id,
        input_role="future_value",
        recorded_at=later,
    )
    seal = DerivationSealV2(
        derivation_seal_id="seal-lookahead",
        idempotency_key="seal-key-lookahead",
        derived_observation_id=derived.observation_id,
        ordered_inputs=(edge,),
        input_basis="as_reported",
        formula_definition_sha256=_sha("bad-lookahead-formula"),
        formula_config_sha256=_sha("bad-lookahead-config"),
        seal_method="canonical-json",
        seal_method_version="v1",
        effective_at=later,
        knowledge_at=later,
        recorded_at=later,
    )
    with pytest.raises(sqlite3.IntegrityError, match="no-look-ahead"):
        plane.finalize_derivation(seal)
    assert conn.execute("SELECT COUNT(*) FROM fact_derivation_seals_v2").fetchone() == (0,)
