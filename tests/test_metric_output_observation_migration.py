"""0048 preserves sealed legacy derivation commitments byte for byte."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    DerivationInputV2,
    DerivationSealV2,
    DerivedFactObservationV2,
    FactCellV2,
    FactResolutionCandidateV2,
    FactResolutionRevisionV2,
)
from provenance.fact_read_model import FactReadModel
from provenance.source_fact_publication import verify_source_fact_publication
from provenance.source_fact_repository import (
    DerivedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from tests import test_source_fact_repository as foundation


def _publish_legacy_derived(conn: sqlite3.Connection) -> str:
    source_publication = foundation.make_publication()
    SourceFactRepository(conn).publish(source_publication)
    source = source_publication.reported_facts[0].observation
    cell = FactCellV2.model_validate(
        {
            **foundation.make_cell("migration-derived").model_dump(),
            "concept_name": "LegacyDerivedRevenue",
            "semantic_key_sha256": None,
        }
    )
    output = DerivedFactObservationV2(
        observation_id="migration-derived-output",
        idempotency_key="migration-derived-output",
        fact_cell_id=cell.fact_cell_id,
        observation_kind="derived",
        value_kind="numeric",
        numeric_value="200",
        method_name="formula-engine",
        method_version="v1",
        method_config_sha256=foundation.sha256("migration-formula-method"),
        revision_kind="initial",
        effective_at=foundation.STAMP,
        knowledge_at=foundation.STAMP,
        recorded_at=foundation.STAMP,
        formula_id="migration-double",
        formula_version="v1",
    )
    edge = DerivationInputV2(
        edge_id="migration-derived-edge",
        idempotency_key="migration-derived-edge",
        derived_observation_id=output.observation_id,
        input_position=0,
        input_observation_id=source.observation_id,
        input_role="base",
        recorded_at=foundation.STAMP,
    )
    derivation = DerivationSealV2(
        derivation_seal_id="migration-derived-seal",
        idempotency_key="migration-derived-seal",
        derived_observation_id=output.observation_id,
        ordered_inputs=(edge,),
        input_basis="as_reported",
        formula_definition_sha256=foundation.sha256("migration-formula-definition"),
        formula_config_sha256=foundation.sha256("migration-formula-config"),
        seal_method="canonical-json",
        seal_method_version="v1",
        effective_at=foundation.STAMP,
        knowledge_at=foundation.STAMP,
        recorded_at=foundation.STAMP,
    )
    candidate = FactResolutionCandidateV2(
        candidate_id="migration-derived-candidate",
        idempotency_key="migration-derived-candidate",
        candidate_set_id="migration-derived-candidate-set",
        fact_cell_id=cell.fact_cell_id,
        observation_id=output.observation_id,
        candidate_ordinal=0,
        eligibility="eligible",
        reason_code="sealed_formula",
        reason_details=CanonicalJSONObject({}),
        recorded_at=foundation.STAMP,
    )
    resolution = FactResolutionRevisionV2.model_validate(
        {
            "resolution_revision_id": "migration-derived-resolution",
            "idempotency_key": "migration-derived-resolution",
            "fact_cell_id": cell.fact_cell_id,
            "revision": 1,
            "status": "resolved",
            "candidate_set_id": candidate.candidate_set_id,
            "candidates": (candidate,),
            "selected_observation_id": output.observation_id,
            "policy_name": "sealed-derived-only",
            "policy_version": "v1",
            "policy_config_sha256": foundation.sha256("migration-derived-policy"),
            "reason_code": "resolved",
            "reason_details": CanonicalJSONObject({}),
            "knowledge_cutoff": foundation.STAMP,
            "effective_at": foundation.STAMP,
            "recorded_at": foundation.STAMP,
        }
    )
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="migration-derived-publication",
            idempotency_key="migration-derived-publication",
            derived_facts=(DerivedSourceFact(cell=cell, observation=output),),
            derivations=(derivation,),
            resolutions=(resolution,),
        )
    )
    return output.observation_id


def _commitments(conn: sqlite3.Connection) -> tuple[object, ...]:
    return tuple(
        tuple(
            conn.execute(
                "SELECT canonical_input_digest_sha256 FROM fact_derivation_seals_v2 "
                "WHERE derivation_seal_id='migration-derived-seal'"
            ).fetchone()
        )
        + tuple(
            conn.execute(
                "SELECT canonical_basis_json,canonical_basis_sha256 "
                "FROM fact_derivation_basis_commitments_v2 "
                "WHERE derivation_seal_id='migration-derived-seal'"
            ).fetchone()
        )
        + tuple(
            conn.execute(
                "SELECT canonical_payload_json,observation_payload_sha256 "
                "FROM fact_observation_payload_commitments_v2 "
                "WHERE observation_id='migration-derived-output'"
            ).fetchone()
        )
        + tuple(
            conn.execute(
                "SELECT canonical_publication_payload_json,member_set_sha256,"
                "publication_payload_sha256 FROM source_fact_publications "
                "WHERE publication_id='migration-derived-publication'"
            ).fetchone()
        )
        + tuple(
            conn.execute(
                "SELECT canonical_member_set_json,member_set_sha256,"
                "publication_payload_sha256 "
                "FROM source_fact_publication_seals "
                "WHERE publication_id='migration-derived-publication'"
            ).fetchone()
        )
        + tuple(
            conn.execute(
                "SELECT canonical_event_json,event_sha256 "
                "FROM source_fact_publication_stream "
                "WHERE publication_id='migration-derived-publication'"
            ).fetchone()
        )
    )


def test_0048_preserves_preexisting_legacy_derived_publication(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    before: dict[str, object] = {}

    def seed(path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        foundation.seed_foundation(conn)
        observation_id = _publish_legacy_derived(conn)
        before["commitments"] = _commitments(conn)
        before["derivation"] = (
            FactReadModel(conn)
            .provenance_bundle(observation_id, cutoff=foundation.STAMP)
            .derivation
        )
        verify_source_fact_publication(
            conn,
            publication_id="migration-derived-publication",
            cutoff=foundation.STAMP,
        )
        conn.commit()
        conn.close()

    path = migrated_db(
        tmp_path / "legacy-derived.db",
        upgrade_from="0047_source_regime_measurements",
        before_upgrade=seed,
    )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert _commitments(conn) == before["commitments"]
        edge = conn.execute(
            "SELECT input_canonical_resolution_revision_id "
            "FROM fact_derivation_input_edges_v2 "
            "WHERE edge_id='migration-derived-edge'"
        ).fetchone()
        assert edge is not None and edge[0] is None
        after = FactReadModel(conn).provenance_bundle(
            "migration-derived-output", cutoff=foundation.STAMP
        )
        assert after.derivation == before["derivation"]
        verify_source_fact_publication(
            conn,
            publication_id="migration-derived-publication",
            cutoff=foundation.STAMP,
        )
    finally:
        conn.close()
