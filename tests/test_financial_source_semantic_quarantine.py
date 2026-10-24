"""An explicit document quarantine blocks financial facts, not source access."""

from __future__ import annotations

import json
import runpy
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    DerivationInputV2,
    DerivationSealV2,
    DerivedFactObservationV2,
    FactResolutionCandidateV2,
    FactResolutionRevisionV2,
)
from provenance.fact_read_model import FactAdmissionError, FactReadModel
from provenance.financial_fact_resolution import (
    canonical_fact_relation,
    canonical_fact_row_ids_as_known,
    resolve_fact_row,
)
from provenance.semantic_disposition import (
    SemanticDisposition,
    SemanticDispositionStore,
    semantic_quarantine_available,
)
from provenance.source_fact_repository import (
    DerivedSourceFact,
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from tests.test_source_fact_repository import (
    STAMP,
    make_cell,
    make_publication,
    make_report,
    seed_foundation,
    sha256,
)

_CUTOVER = runpy.run_path(
    str(Path(__file__).with_name("test_financial_fact_resolution_cutover.py"))
)
_seed_document = cast(Callable[..., None], _CUTOVER["_seed_document"])
_insert_financial_fact = cast(Callable[..., int], _CUTOVER["_insert_financial_fact"])
_insert_kpi_fact = cast(Callable[..., int], _CUTOVER["_insert_kpi_fact"])


def _disposition(
    document_version_id: str,
    *,
    revision: int,
    status: str,
    knowledge_at: datetime,
    recorded_at: datetime,
) -> SemanticDisposition:
    return SemanticDisposition.model_validate(
        {
            "assessment_id": f"assessment-{document_version_id}-{revision}",
            "idempotency_key": f"assessment-key-{document_version_id}-{revision}",
            "document_version_id": document_version_id,
            "revision": revision,
            "semantic_status": status,
            "reason_code": "source_review",
            "reason_details": (("review", "source type"),),
            "decision_kind": "human",
            "reviewer_identity": "fixture-reviewer",
            "policy_name": "source-review",
            "policy_version": "1",
            "policy_config_sha256": "a" * 64,
            "effective_at": knowledge_at,
            "knowledge_at": knowledge_at,
            "recorded_at": recorded_at,
            "supersedes_assessment_id": (
                None if revision == 1 else f"assessment-{document_version_id}-{revision - 1}"
            ),
        }
    )


def test_legacy_financial_relation_and_history_exclude_only_visible_quarantine(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "semantic-financial.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        fact_id = _insert_financial_fact(conn, document_id=1, value="100")
        known = datetime.now(UTC) + timedelta(minutes=1)
        assert (
            resolve_fact_row(
                conn,
                fact_table="financial_facts",
                fact_row_id=fact_id,
                knowledge_cutoff=known,
            )
            is not None
        )

        def current_ids() -> tuple[int, ...]:
            relation = canonical_fact_relation(conn, "financial_facts")
            return tuple(int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}"))

        def historical_ids(cutoff: datetime) -> tuple[int, ...]:
            return canonical_fact_row_ids_as_known(
                conn,
                fact_table="financial_facts",
                effective_at=cutoff,
                known_at=cutoff,
            )

        assert current_ids() == (fact_id,)
        assert historical_ids(known) == (fact_id,)
        review_at = (known + timedelta(hours=1)).replace(microsecond=100_000)
        recorded_at = review_at + timedelta(hours=1)
        store = SemanticDispositionStore(conn)
        assert store.persist(
            _disposition(
                "document-version-1",
                revision=1,
                status="quarantined",
                knowledge_at=review_at,
                recorded_at=recorded_at,
            )
        )
        assert current_ids() == ()
        assert historical_ids(review_at - timedelta(seconds=1)) == (fact_id,)
        assert historical_ids(review_at + timedelta(minutes=30)) == (fact_id,)
        assert historical_ids(recorded_at - timedelta(microseconds=100_000)) == (fact_id,)
        assert historical_ids(recorded_at) == ()
        assert (
            conn.execute(
                "SELECT document_version_id FROM evidence_document_versions "
                "WHERE document_version_id='document-version-1'"
            ).fetchone()[0]
            == "document-version-1"
        )

        released_at = recorded_at + timedelta(microseconds=800_000)
        assert store.persist(
            _disposition(
                "document-version-1",
                revision=2,
                status="required",
                knowledge_at=released_at,
                recorded_at=released_at,
            )
        )
        assert current_ids() == (fact_id,)
        assert historical_ids(recorded_at) == ()
        assert historical_ids(recorded_at + timedelta(microseconds=400_000)) == ()
        assert historical_ids(released_at) == (fact_id,)
    finally:
        conn.close()


def test_native_read_model_blocks_quarantined_source_at_recorded_cutoff(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "semantic-native.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        seed_foundation(conn)
        publication = make_publication()
        SourceFactRepository(conn).publish(publication)
        reader = FactReadModel(conn)
        cell_id = publication.reported_facts[0].cell.fact_cell_id
        assert len(reader.as_reported(cell_id, cutoff=STAMP)) == 1
        review_at = STAMP + timedelta(hours=1, microseconds=100_000)
        recorded_at = review_at + timedelta(hours=1)
        store = SemanticDispositionStore(conn)
        store.persist(
            _disposition(
                "document-1",
                revision=1,
                status="quarantined",
                knowledge_at=review_at,
                recorded_at=recorded_at,
            )
        )
        assert len(reader.as_reported(cell_id, cutoff=review_at + timedelta(minutes=30))) == 1
        assert (
            len(reader.as_reported(cell_id, cutoff=recorded_at - timedelta(microseconds=100_000)))
            == 1
        )
        with pytest.raises(FactAdmissionError, match="source_document_semantically_quarantined"):
            reader.as_reported(cell_id, cutoff=recorded_at)
        assert (
            conn.execute(
                "SELECT document_version_id FROM evidence_document_versions "
                "WHERE document_version_id='document-1'"
            ).fetchone()[0]
            == "document-1"
        )
        released_at = recorded_at + timedelta(microseconds=800_000)
        store.persist(
            _disposition(
                "document-1",
                revision=2,
                status="required",
                knowledge_at=released_at,
                recorded_at=released_at,
            )
        )
        with pytest.raises(FactAdmissionError, match="source_document_semantically_quarantined"):
            reader.as_reported(cell_id, cutoff=recorded_at + timedelta(microseconds=400_000))
        assert len(reader.as_reported(cell_id, cutoff=released_at)) == 1
        conn.execute("DROP TRIGGER trg_document_semantic_disposition_revisions_append_only")
        conn.execute(
            "UPDATE document_semantic_disposition_revisions "
            "SET effective_at='bad',knowledge_at='bad',recorded_at='bad' "
            "WHERE document_version_id='document-1' AND revision=2"
        )
        with pytest.raises(FactAdmissionError, match="source_document_semantically_quarantined"):
            reader.as_reported(cell_id, cutoff=released_at)
    finally:
        conn.close()


def test_missing_installed_ledger_fails_closed_but_pre_disposition_schema_is_compatible(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    legacy = sqlite3.connect(":memory:")
    try:
        assert semantic_quarantine_available(legacy) is False
    finally:
        legacy.close()

    path = migrated_db(tmp_path / "missing-semantic-ledger.db")
    conn = sqlite3.connect(path)
    try:
        seed_foundation(conn)
        publication = make_publication()
        SourceFactRepository(conn).publish(publication)
        conn.execute("DROP TABLE document_semantic_disposition_revisions")
        cell_id = publication.reported_facts[0].cell.fact_cell_id
        with pytest.raises(RuntimeError, match="document semantic disposition ledger"):
            FactReadModel(conn).as_reported(cell_id, cutoff=STAMP)
        with pytest.raises(RuntimeError, match="document semantic disposition ledger"):
            canonical_fact_relation(conn, "financial_facts")
    finally:
        conn.close()


def test_quarantined_latest_resolution_does_not_resurrect_older_source(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "semantic-resolution-order.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP),
        )
        _seed_document(conn, document_id=1, source_tier="fmp_normalized", value_suffix="FMP")
        older_id = _insert_financial_fact(conn, document_id=1, value="100")
        first_at = datetime.now(UTC) + timedelta(minutes=1)
        assert (
            resolve_fact_row(
                conn,
                fact_table="financial_facts",
                fact_row_id=older_id,
                knowledge_cutoff=first_at,
            )
            is not None
        )
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="SEC")
        newer_id = _insert_financial_fact(conn, document_id=2, value="100")
        second_at = first_at + timedelta(minutes=1)
        assert (
            resolve_fact_row(
                conn,
                fact_table="financial_facts",
                fact_row_id=newer_id,
                knowledge_cutoff=second_at,
            )
            is not None
        )

        def historical_ids(cutoff: datetime) -> tuple[int, ...]:
            return canonical_fact_row_ids_as_known(
                conn,
                fact_table="financial_facts",
                effective_at=cutoff,
                known_at=cutoff,
            )

        assert historical_ids(first_at) == (older_id,)
        assert historical_ids(second_at) == (newer_id,)
        quarantine_at = second_at + timedelta(minutes=1)
        SemanticDispositionStore(conn).persist(
            _disposition(
                "document-version-2",
                revision=1,
                status="quarantined",
                knowledge_at=quarantine_at,
                recorded_at=quarantine_at,
            )
        )
        assert historical_ids(quarantine_at) == ()
        relation = canonical_fact_relation(conn, "financial_facts")
        assert conn.execute(f"SELECT id FROM {relation.sql}").fetchall() == []
    finally:
        conn.close()


def _publish_two_input_derivation(conn: sqlite3.Connection) -> tuple[str, str, str]:
    """Publish a derived value whose second source is a distinct document."""

    repository = SourceFactRepository(conn)
    original = make_publication()
    repository.publish(original)
    conn.execute(
        "INSERT INTO evidence_source_observations SELECT 'source-2','source-key-2',"
        "source_kind,'https://data.sec.gov/second.json',blob_sha256,source_published_at,"
        "filing_at,accepted_at,observed_at,retrieved_at,retrieval_config_sha256,"
        "collector_code_version FROM evidence_source_observations WHERE observation_id='source-1'"
    )
    conn.execute(
        "INSERT INTO evidence_document_versions SELECT 'document-2','document-key-2',"
        "version_sequence,'source-2',blob_sha256,issuer_id,ticker,document_type,form_type,"
        "'0000000001-26-000002',exhibit_id,period_start,period_end,as_of_at,language,"
        "replaces_document_version_id,legacy_document_id,recorded_at "
        "FROM evidence_document_versions WHERE document_version_id='document-1'"
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs SELECT 'run-2','run-key-2','document-2',"
        "input_sha256,extractor_name,extractor_config_sha256,extractor_code_version,"
        "output_sha256,started_at,completed_at,outcome FROM evidence_extraction_runs "
        "WHERE extraction_run_id='run-1'"
    )
    conn.execute(
        "INSERT INTO evidence_nodes SELECT 'node-2','node-key-2',revision,'run-2',"
        "parent_node_id,supersedes_node_id,node_kind,text,locator_json,locator_sha256,"
        "recorded_at FROM evidence_nodes WHERE node_id='node-1'"
    )
    second_cell = original.reported_facts[0].cell
    second = make_report(second_cell, "second").model_copy(
        update={"document_version_id": "document-2", "evidence_node_id": "node-2"}
    )
    second_seal = original.extraction_seals[0].model_copy(
        update={
            "extraction_seal_id": "extraction-seal-2",
            "idempotency_key": "extraction-seal-key-2",
            "extraction_run_id": "run-2",
        }
    )
    repository.publish(
        SourceFactPublication(
            publication_id="publication-second",
            idempotency_key="publication-key-second",
            reported_facts=(ReportedSourceFact(cell=second_cell, observation=second),),
            extraction_seals=(second_seal,),
        )
    )
    derived_cell = make_cell("derived").model_copy(
        update={"concept_name": "DerivedValue", "semantic_key_sha256": None}
    )
    derived = DerivedFactObservationV2(
        observation_id="observation-derived-two-inputs",
        idempotency_key="observation-key-derived-two-inputs",
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
    edges = tuple(
        DerivationInputV2(
            edge_id=f"edge-derived-{index}",
            idempotency_key=f"edge-key-derived-{index}",
            derived_observation_id=derived.observation_id,
            input_position=index,
            input_observation_id=source.observation_id,
            input_role=f"source-{index}",
            recorded_at=STAMP,
        )
        for index, source in enumerate((original.reported_facts[0].observation, second))
    )
    seal = DerivationSealV2(
        derivation_seal_id="derivation-seal-two-inputs",
        idempotency_key="derivation-seal-key-two-inputs",
        derived_observation_id=derived.observation_id,
        ordered_inputs=edges,
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
        candidate_id="candidate-derived-two-inputs",
        idempotency_key="candidate-key-derived-two-inputs",
        candidate_set_id="candidate-set-derived-two-inputs",
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
            "resolution_revision_id": "resolution-derived-two-inputs",
            "idempotency_key": "resolution-key-derived-two-inputs",
            "fact_cell_id": derived_cell.fact_cell_id,
            "revision": 1,
            "status": "resolved",
            "candidate_set_id": "candidate-set-derived-two-inputs",
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
    repository.publish(
        SourceFactPublication(
            publication_id="publication-derived-two-inputs",
            idempotency_key="publication-key-derived-two-inputs",
            derived_facts=(DerivedSourceFact(cell=derived_cell, observation=derived),),
            derivations=(seal,),
            resolutions=(resolution,),
        )
    )
    nested_cell = make_cell("nested").model_copy(
        update={"concept_name": "NestedDerivedValue", "semantic_key_sha256": None}
    )
    nested = derived.model_copy(
        update={
            "observation_id": "observation-nested-derived",
            "idempotency_key": "observation-key-nested-derived",
            "fact_cell_id": nested_cell.fact_cell_id,
        }
    )
    nested_edge = edges[0].model_copy(
        update={
            "edge_id": "edge-nested-derived",
            "idempotency_key": "edge-key-nested-derived",
            "derived_observation_id": nested.observation_id,
            "input_observation_id": derived.observation_id,
            "input_resolution_revision_id": resolution.resolution_revision_id,
        }
    )
    nested_seal = DerivationSealV2(
        derivation_seal_id="derivation-seal-nested",
        idempotency_key="derivation-seal-key-nested",
        derived_observation_id=nested.observation_id,
        ordered_inputs=(nested_edge,),
        input_basis="as_known",
        formula_definition_sha256=sha256("formula-definition"),
        formula_config_sha256=sha256("formula-config"),
        seal_method="canonical-json",
        seal_method_version="v1",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    repository.publish(
        SourceFactPublication(
            publication_id="publication-nested",
            idempotency_key="publication-key-nested",
            derived_facts=(DerivedSourceFact(cell=nested_cell, observation=nested),),
            derivations=(nested_seal,),
        )
    )
    return derived_cell.fact_cell_id, derived.observation_id, nested.observation_id


def test_native_derived_read_rechecks_non_anchor_input_at_request_cutoff(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "native-derived-quarantine.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        seed_foundation(conn)
        cell_id, observation_id, nested_id = _publish_two_input_derivation(conn)
        reader = FactReadModel(conn)
        assert reader.current_resolved(cell_id, cutoff=STAMP) is not None
        assert reader.provenance_bundle(observation_id, cutoff=STAMP).derivation is not None
        assert reader.provenance_bundle(nested_id, cutoff=STAMP).derivation is not None
        quarantine_at = STAMP + timedelta(hours=1, microseconds=900_000)
        SemanticDispositionStore(conn).persist(
            _disposition(
                "document-2",
                revision=1,
                status="quarantined",
                knowledge_at=quarantine_at,
                recorded_at=quarantine_at,
            )
        )
        assert (
            reader.current_resolved(cell_id, cutoff=quarantine_at - timedelta(microseconds=800_000))
            is not None
        )
        with pytest.raises(FactAdmissionError, match="source_document_semantically_quarantined"):
            reader.current_resolved(cell_id, cutoff=quarantine_at)
        with pytest.raises(FactAdmissionError, match="source_document_semantically_quarantined"):
            reader.provenance_bundle(observation_id, cutoff=quarantine_at)
        with pytest.raises(FactAdmissionError, match="source_document_semantically_quarantined"):
            reader.provenance_bundle(nested_id, cutoff=quarantine_at)
        released_at = quarantine_at + timedelta(microseconds=800_000)
        SemanticDispositionStore(conn).persist(
            _disposition(
                "document-2",
                revision=2,
                status="required",
                knowledge_at=released_at,
                recorded_at=released_at,
            )
        )
        with pytest.raises(FactAdmissionError, match="source_document_semantically_quarantined"):
            reader.current_resolved(cell_id, cutoff=released_at - timedelta(microseconds=100_000))
        assert reader.current_resolved(cell_id, cutoff=released_at) is not None
    finally:
        conn.close()


def test_legacy_derived_relation_checks_every_declared_input_document(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "compat-derived-quarantine.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        _seed_document(conn, document_id=2, source_tier="sec_official", value_suffix="SEC")
        fact_id = _insert_kpi_fact(conn, document_id=1, value="50", unit="percent")
        conn.execute(
            "UPDATE kpi_facts SET extracted_by='metrics_engine',computed_from=? WHERE id=?",
            (
                json.dumps(
                    {
                        "display": "a / b",
                        "inputs": [
                            {
                                "ref": "financial_fact",
                                "item": "a",
                                "period_end": "2026-06-30",
                                "doc_id": 1,
                            },
                            {
                                "ref": "financial_fact",
                                "item": "b",
                                "period_end": "2026-06-30",
                                "doc_id": 2,
                            },
                        ],
                    }
                ),
                fact_id,
            ),
        )
        known = datetime.now(UTC) + timedelta(minutes=1)
        assert (
            resolve_fact_row(
                conn, fact_table="kpi_facts", fact_row_id=fact_id, knowledge_cutoff=known
            )
            is not None
        )

        def current_ids() -> tuple[int, ...]:
            relation = canonical_fact_relation(conn, "kpi_facts")
            return tuple(int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}"))

        def historical_ids(cutoff: datetime) -> tuple[int, ...]:
            return canonical_fact_row_ids_as_known(
                conn, fact_table="kpi_facts", effective_at=cutoff, known_at=cutoff
            )

        assert current_ids() == (fact_id,)
        assert historical_ids(known) == (fact_id,)
        quarantine_at = known + timedelta(hours=1, microseconds=900_000)
        SemanticDispositionStore(conn).persist(
            _disposition(
                "document-version-2",
                revision=1,
                status="quarantined",
                knowledge_at=quarantine_at,
                recorded_at=quarantine_at,
            )
        )
        assert current_ids() == ()
        assert historical_ids(quarantine_at - timedelta(microseconds=800_000)) == (fact_id,)
        assert historical_ids(quarantine_at) == ()
        released_at = quarantine_at + timedelta(microseconds=800_000)
        SemanticDispositionStore(conn).persist(
            _disposition(
                "document-version-2",
                revision=2,
                status="required",
                knowledge_at=released_at,
                recorded_at=released_at,
            )
        )
        assert historical_ids(released_at - timedelta(microseconds=100_000)) == ()
        assert historical_ids(released_at) == (fact_id,)
        assert current_ids() == (fact_id,)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("ref", "item", "admitted"),
    (
        ("vendor_field", "price", True),
        ("vendor_field", "revenue", False),
        ("kpi_fact", "prior_ratio", False),
    ),
)
def test_legacy_unproven_derived_inputs_fail_closed(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    ref: str,
    item: str,
    admitted: bool,
) -> None:
    path = migrated_db(tmp_path / f"compat-vendor-{item}.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-acme", "issuer:acme", "operating_company", STAMP),
        )
        _seed_document(conn, document_id=1, source_tier="sec_official", value_suffix="SEC")
        fact_id = _insert_kpi_fact(conn, document_id=1, value="50", unit="percent")
        conn.execute(
            "UPDATE kpi_facts SET extracted_by='metrics_engine',computed_from=? WHERE id=?",
            (
                json.dumps(
                    {
                        "inputs": [
                            {
                                "ref": ref,
                                "item": item,
                                "period_end": "2026-06-30",
                                "doc_id": 1 if ref == "kpi_fact" else None,
                            }
                        ]
                    }
                ),
                fact_id,
            ),
        )
        known = datetime.now(UTC) + timedelta(minutes=1)
        assert (
            resolve_fact_row(
                conn, fact_table="kpi_facts", fact_row_id=fact_id, knowledge_cutoff=known
            )
            is not None
        )
        relation = canonical_fact_relation(conn, "kpi_facts")
        current = tuple(int(row[0]) for row in conn.execute(f"SELECT id FROM {relation.sql}"))
        historical = canonical_fact_row_ids_as_known(
            conn, fact_table="kpi_facts", effective_at=known, known_at=known
        )
        expected = (fact_id,) if admitted else ()
        assert current == expected
        assert historical == expected
    finally:
        conn.close()
