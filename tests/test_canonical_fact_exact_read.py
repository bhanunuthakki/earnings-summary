"""Exact reads retain the complete selected projection context."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest

import search.canonical_fact_projection as projection
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionPolicy,
    ResolutionSnapshotScope,
)
from provenance.filing_xbrl_extraction_ledger import FilingXbrlExtractionLedger
from provenance.metric_ontology import MetricOntology, OntologySnapshot
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
    populate_metric_ontology,
)
from provenance.source_fact_stream import bind_resolution_snapshot_watermark
from tests.test_filing_xbrl_extraction_ledger import (
    STAMP,
    filing_xbrl_entry,
    filing_xbrl_ledger_database,
    filing_xbrl_output,
)

SCOPE = ResolutionSnapshotScope(issuer_id="issuer-1", reporting_entity_ids=("reporting-1",))


@pytest.fixture
def projected(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Iterator[tuple[sqlite3.Connection, tuple[str, ...]]]:
    output = filing_xbrl_output((filing_xbrl_entry(0), filing_xbrl_entry(1, concept_name="Assets")))
    conn = filing_xbrl_ledger_database(tmp_path, output, migrated_db)
    conn.row_factory = sqlite3.Row
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        populated = populate_metric_ontology(
            conn,
            MetricOntologyPopulationRequest(
                knowledge_cutoff=STAMP, operation_recorded_at=STAMP, apply=True
            ),
        )
        assert populated.snapshot_id is not None
        cells = tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT canonical_metric_cell_id FROM fact_cell_canonical_binding_revisions "
                "WHERE binding_status='bound' ORDER BY canonical_metric_cell_id"
            )
        )
        assert len(cells) == 2
        resolver = CanonicalFactResolutionEngine(conn)
        for cell in cells:
            resolver.resolve(
                cell,
                STAMP,
                ResolutionPolicy(name="synthetic", version="1", config={}),
                recorded_at=STAMP,
            )
        resolver.seal_snapshot("resolution-base", STAMP, STAMP, SCOPE)
        bind_resolution_snapshot_watermark(
            conn, resolution_snapshot_id="resolution-base", cutoff_at=STAMP, recorded_at=STAMP
        )
        projection.build_canonical_projection_generation(
            conn,
            projection.ProjectionGenerationRequest(
                generation_id="base",
                idempotency_key="base",
                generation_kind="checkpoint",
                resolution_snapshot_id="resolution-base",
                ontology_snapshot_id=populated.snapshot_id,
                cutoff_at=STAMP,
                recorded_at=STAMP,
            ),
        )
        conn.commit()
        yield conn, cells
    finally:
        conn.close()


def test_exact_lookup_preserves_order_duplicates_absence_and_full_context(
    projected: tuple[sqlite3.Connection, tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, cells = projected

    def forbidden_aliases(*_args: object, **_kwargs: object) -> dict[str, tuple[str, ...]]:
        raise AssertionError("exact identity lookup must not use lexical aliases")

    monkeypatch.setattr(projection, "kpi_alias_expansions", forbidden_aliases)
    wanted = (cells[1], "absent", cells[0], cells[1])
    before = (conn.in_transaction, conn.total_changes, conn.row_factory)
    reads = projection.read_canonical_facts_by_id(
        conn, generation_id="base", canonical_metric_cell_ids=wanted
    )
    assert tuple(None if read is None else read.canonical_metric_cell_id for read in reads) == (
        cells[1],
        None,
        cells[0],
        cells[1],
    )
    assert reads[0] == reads[3]
    assert reads[0] is not None
    for key in (
        "metric_definition_revision_id",
        "metric_definition_commitment_sha256",
        "binding_revision_id",
        "binding_commitment_sha256",
        "mapping_revision_id",
        "mapping_commitment_sha256",
    ):
        assert reads[0].lineage[key]
    assert (conn.in_transaction, conn.total_changes, conn.row_factory) == before


def test_exact_lookup_has_no_lexical_candidate_limit(
    projected: tuple[sqlite3.Connection, tuple[str, ...]],
) -> None:
    conn, cells = projected
    wanted = (*(f"absent-{index}" for index in range(1_001)), cells[0], cells[0])
    reads = projection.read_canonical_facts_by_id(
        conn, generation_id="base", canonical_metric_cell_ids=wanted
    )
    assert len(reads) == len(wanted)
    assert all(read is None for read in reads[:1_001])
    assert reads[-1] is not None and reads[-1] == reads[-2]


@pytest.mark.parametrize("empty", [False, True])
def test_missing_admission_fails_even_for_empty_request(
    projected: tuple[sqlite3.Connection, tuple[str, ...]], empty: bool
) -> None:
    conn, cells = projected
    conn.execute("DROP TRIGGER trg_canonical_fact_projection_audit_receipts_append_only_delete")
    conn.execute("DELETE FROM canonical_fact_projection_audit_receipts WHERE generation_id='base'")
    with pytest.raises(
        projection.CanonicalFactProjectionError, match="strict_audit_receipt_missing"
    ):
        projection.read_canonical_facts_by_id(
            conn, generation_id="base", canonical_metric_cell_ids=() if empty else cells
        )


@pytest.mark.parametrize("forge_bucket", [False, True])
def test_deleted_entry_cannot_masquerade_as_absent(
    projected: tuple[sqlite3.Connection, tuple[str, ...]],
    forge_bucket: bool,
) -> None:
    conn, cells = projected
    conn.execute("DROP TRIGGER trg_canonical_fact_projection_entries_append_only_delete")
    conn.execute(
        "DELETE FROM canonical_fact_projection_entries "
        "WHERE generation_id='base' AND canonical_metric_cell_id=?",
        (cells[0],),
    )
    if forge_bucket:
        # A self-consistent replacement bucket still cannot replace its digest
        # inside the already audited generation seal.
        conn.execute("DROP TRIGGER trg_canonical_fact_projection_buckets_append_only")
        conn.execute(
            "UPDATE canonical_fact_projection_buckets "
            "SET entry_count=0,canonical_entry_set_json='[]',entry_set_sha256=? "
            "WHERE generation_id='base' AND digest_bucket=?",
            (projection.digest_text("[]"), projection.digest_bucket(cells[0])),
        )
    with pytest.raises(projection.CanonicalFactProjectionError, match="bucket"):
        projection.read_canonical_facts_by_id(
            conn, generation_id="base", canonical_metric_cell_ids=(cells[0],)
        )


def test_changed_selected_entry_fails_commitment_verification(
    projected: tuple[sqlite3.Connection, tuple[str, ...]],
) -> None:
    conn, cells = projected
    conn.execute("DROP TRIGGER trg_canonical_fact_projection_entries_append_only")
    conn.execute(
        "UPDATE canonical_fact_projection_entries SET canonical_value='999' "
        "WHERE generation_id='base' AND canonical_metric_cell_id=?",
        (cells[0],),
    )
    with pytest.raises(projection.CanonicalFactProjectionError, match="entry_commitment_tampered"):
        projection.read_canonical_facts_by_id(
            conn, generation_id="base", canonical_metric_cell_ids=(cells[0],)
        )


def test_renamed_entry_cannot_masquerade_as_absent(
    projected: tuple[sqlite3.Connection, tuple[str, ...]],
) -> None:
    conn, cells = projected
    # Simulate damaged storage beyond the normal append and foreign-key guards.
    conn.execute("PRAGMA foreign_keys=OFF")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    conn.execute("DROP TRIGGER trg_canonical_fact_projection_entries_append_only")
    conn.execute(
        "UPDATE canonical_fact_projection_entries SET canonical_metric_cell_id='forged-id' "
        "WHERE generation_id='base' AND canonical_metric_cell_id=?",
        (cells[0],),
    )
    with pytest.raises(projection.CanonicalFactProjectionError, match="entry_commitment"):
        projection.read_canonical_facts_by_id(
            conn, generation_id="base", canonical_metric_cell_ids=(cells[0],)
        )


@pytest.mark.parametrize("caller_transaction", [False, True])
def test_cancelled_exact_read_restores_transaction_boundary(
    projected: tuple[sqlite3.Connection, tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    caller_transaction: bool,
) -> None:
    conn, cells = projected

    class ReadCancelled(BaseException):
        pass

    failure = ReadCancelled("synthetic cancellation")

    def cancel(*_args: object, **_kwargs: object) -> None:
        raise failure

    if caller_transaction:
        conn.execute("BEGIN")
        conn.execute("CREATE TEMP TABLE caller_marker(value TEXT)")
        conn.execute("INSERT INTO caller_marker VALUES ('retained')")
    monkeypatch.setattr(projection, "_verify_projected_hit_for_read", cancel)
    with pytest.raises(ReadCancelled) as raised:
        projection.read_canonical_facts_by_id(
            conn, generation_id="base", canonical_metric_cell_ids=cells
        )
    assert raised.value is failure
    assert conn.in_transaction is caller_transaction
    with pytest.raises(sqlite3.OperationalError, match="no such savepoint"):
        conn.execute("RELEASE SAVEPOINT canonical_fact_exact_read")
    if caller_transaction:
        assert conn.execute("SELECT value FROM caller_marker").fetchone()[0] == "retained"
        conn.rollback()


def test_lexical_verification_checks_every_entry_in_a_verified_bucket(
    projected: tuple[sqlite3.Connection, tuple[str, ...]],
) -> None:
    conn, _ = projected
    rows = conn.execute(
        "SELECT canonical_metric_cell_id,digest_bucket,entry_json FROM canonical_fact_projection_entries "
        "WHERE generation_id='base' ORDER BY canonical_metric_name,period_end DESC,canonical_metric_cell_id"
    ).fetchall()
    first, second = rows
    payload = json.loads(str(second[2]))
    payload["canonical_value"] = "999"
    forged = projection.canonical_json(payload)
    conn.execute("DROP TRIGGER trg_canonical_fact_projection_entries_append_only")
    conn.execute(
        "UPDATE canonical_fact_projection_entries "
        "SET canonical_value='999',digest_bucket=?,entry_json=?,entry_sha256=? "
        "WHERE generation_id='base' AND canonical_metric_cell_id=?",
        (first[1], forged, projection.digest_text(forged), second[0]),
    )
    with pytest.raises(projection.CanonicalFactProjectionError, match="bucket"):
        projection.search_canonical_facts(
            conn, generation_id="base", query_text="2026", limit=10, aliases={}
        )


def test_delta_overrides_and_tombstones_do_not_fall_back_to_parent(
    projected: tuple[sqlite3.Connection, tuple[str, ...]],
) -> None:
    conn, cells = projected
    later = STAMP + timedelta(seconds=2)
    ontology = MetricOntology(conn)
    selected = conn.execute(
        "SELECT source_observation_id FROM fact_cell_canonical_binding_revisions "
        "WHERE canonical_metric_cell_id=?",
        (cells[0],),
    ).fetchone()
    prior = ontology.binding_as_known(str(selected[0]), STAMP)
    assert prior is not None
    ontology.persist_binding(
        prior.model_copy(
            update={
                "binding_revision_id": "later-retirement",
                "idempotency_key": "later-retirement",
                "revision": 2,
                "supersedes_binding_revision_id": prior.binding_revision_id,
                "binding_status": "retired",
                "knowledge_at": later,
                "recorded_at": later,
            }
        )
    )
    metric = str(
        conn.execute(
            "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
            (cells[1],),
        ).fetchone()[0]
    )
    definition = ontology.metric_definition_as_known(metric, STAMP)
    assert definition is not None
    ontology.persist_metric_definition(
        definition.model_copy(
            update={
                "metric_definition_revision_id": "later-definition",
                "idempotency_key": "later-definition",
                "revision": 2,
                "supersedes_metric_definition_revision_id": definition.metric_definition_revision_id,
                "definition_text": "Later reviewed definition",
                "knowledge_at": later,
                "recorded_at": later,
            }
        )
    )
    ontology.seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="later-ontology",
            idempotency_key="later-ontology",
            cutoff_at=later,
            recorded_at=later,
        )
    )
    resolver = CanonicalFactResolutionEngine(conn)
    resolver.resolve(
        cells[0],
        later,
        ResolutionPolicy(name="synthetic", version="1", config={}),
        recorded_at=later,
    )
    resolver.seal_snapshot("later-resolution", later, later, SCOPE)
    bind_resolution_snapshot_watermark(
        conn, resolution_snapshot_id="later-resolution", cutoff_at=later, recorded_at=later
    )
    projection.build_canonical_projection_generation(
        conn,
        projection.ProjectionGenerationRequest(
            generation_id="delta",
            idempotency_key="delta",
            generation_kind="delta",
            parent_generation_id="base",
            resolution_snapshot_id="later-resolution",
            ontology_snapshot_id="later-ontology",
            cutoff_at=later,
            recorded_at=later,
        ),
    )
    parent = projection.read_canonical_facts_by_id(
        conn, generation_id="base", canonical_metric_cell_ids=cells
    )
    reads = projection.read_canonical_facts_by_id(
        conn, generation_id="delta", canonical_metric_cell_ids=cells
    )
    assert parent[0] is not None and parent[1] is not None
    assert reads[0] is None and reads[1] is not None
    assert reads[1].lineage["metric_definition_revision_id"] == "later-definition"
    assert reads[1].entry_sha256 != parent[1].entry_sha256
