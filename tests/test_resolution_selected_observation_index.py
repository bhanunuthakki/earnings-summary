"""Selected-observation lookups stay bounded without changing ledger selection."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.observation_resolution import (
    ObservationResolutionLedger,
    ReportedObservation,
    ResolutionRevision,
)

PARENT = "0048_metric_computation_output_observation"
REVISION = "0049_resolution_selected_observation_index"
INDEX = "ix_observation_resolution_selected_observation"
STAMP = datetime(2026, 10, 3)


def seed_resolution_ledger(conn: sqlite3.Connection) -> None:
    evidence = EvidenceLedger(conn)
    evidence.persist(
        ContentBlob(
            sha256="a" * 64,
            byte_size=10,
            media_type="text/plain",
            storage_uri="fixture:reported",
            recorded_at=STAMP,
        )
    )
    evidence.persist(
        SourceObservation(
            observation_id="source",
            idempotency_key="source",
            source_kind="sec_filing",
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            source_url="https://example.invalid/filing",
            blob_sha256="a" * 64,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="b" * 64,
            collector_code_version="fixture",
        )
    )
    evidence.persist(
        DocumentVersion(
            document_version_id="document",
            document_key="TST:filing",
            version_sequence=1,
            observation_id="source",
            blob_sha256="a" * 64,
            issuer_id="fixture-issuer",
            ticker="TST",
            document_type="10-Q",
            form_type="10-Q",
            accession_number=None,
            exhibit_id=None,
            period_start=STAMP,
            period_end=STAMP,
            as_of_at=None,
            replaces_document_version_id=None,
            legacy_document_id=None,
            language="en",
            recorded_at=STAMP,
        )
    )
    evidence.persist(
        ExtractionRun(
            extraction_run_id="run",
            idempotency_key="run",
            document_version_id="document",
            input_sha256="a" * 64,
            extractor_name="fixture",
            extractor_config_sha256="b" * 64,
            extractor_code_version="fixture",
            output_sha256="c" * 64,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    evidence.persist(
        EvidenceNode(
            node_id="node",
            evidence_key="fixture:revenue",
            revision=1,
            extraction_run_id="run",
            node_kind="table_cell",
            parent_node_id=None,
            supersedes_node_id=None,
            locator=None,
            text="Revenue 10",
            recorded_at=STAMP,
        )
    )
    ledger = ObservationResolutionLedger(conn)
    for number in (1, 2):
        ledger.persist_observation(
            ReportedObservation(
                observation_id=f"observation-{number}",
                idempotency_key=f"observation-{number}",
                issuer_id="fixture-issuer",
                ticker="TST",
                concept_key="revenue",
                period_start=STAMP,
                period_end=STAMP,
                fiscal_period_type="quarter",
                dimensions=(),
                text_value=None,
                currency="USD",
                unit="currency",
                scale=0,
                legacy_table=None,
                legacy_row_id=None,
                numeric_value=str(number * 10),
                observation_status="reported",
                evidence_node_id="node",
                available_at=STAMP,
                recorded_at=STAMP,
                method="fixture",
                method_version="1",
                confidence=1,
            )
        )
    for number in (1, 2):
        ledger.persist_resolution(
            ResolutionRevision(
                resolution_id=f"resolution-{number}",
                idempotency_key=f"resolution-{number}",
                logical_key="TST:revenue",
                revision=number,
                selected_observation_id=f"observation-{number}",
                candidate_observation_ids=("observation-1", "observation-2"),
                resolver_kind="deterministic_policy",
                policy_version="fixture",
                reason="fixture correction",
                knowledge_cutoff=STAMP,
                effective_at=STAMP,
                recorded_at=STAMP,
                material_dissent=False,
                supersedes_resolution_id="resolution-1" if number == 2 else None,
            )
        )
    conn.commit()


def resolution_indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[1]) for row in conn.execute("PRAGMA index_list(observation_resolution_revisions)")
    }


def test_current_schema_indexes_selected_observations(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "current.db")
    with sqlite3.connect(path) as conn:
        assert INDEX in resolution_indexes(conn)
        assert [str(row[2]) for row in conn.execute(f"PRAGMA index_info({INDEX})")] == [
            "selected_observation_id"
        ]
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)


def test_selected_lookup_instruction_work_is_bounded_by_matches(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "lookup.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        seed_resolution_ledger(conn)
        # Distinct logical keys model unrelated issuer/metric families. The
        # exact selected membership and append-only constraints remain active.
        conn.executemany(
            "INSERT INTO observation_resolution_candidates VALUES (?,?)",
            [(f"other-{i}", "observation-2") for i in range(5000)],
        )
        conn.executemany(
            "INSERT INTO observation_resolution_revisions (resolution_id,idempotency_key,logical_key,revision,selected_observation_id,resolver_kind,policy_version,reason,knowledge_cutoff,effective_at,material_dissent,recorded_at) VALUES (?,?,?,1,'observation-2','fixture','1','fixture',?,?,0,?)",
            [
                (f"other-{i}", f"other-{i}", f"other-logical-{i}", STAMP, STAMP, STAMP)
                for i in range(5000)
            ],
        )
        conn.commit()
        query = "SELECT resolution_id FROM observation_resolution_revisions WHERE selected_observation_id=?"

        def measured() -> tuple[list[tuple[object, ...]], int]:
            steps = 0

            def progress() -> int:
                nonlocal steps
                steps += 1
                return 0

            conn.set_progress_handler(progress, 1)
            try:
                return conn.execute(query, ("observation-1",)).fetchall(), steps
            finally:
                conn.set_progress_handler(None, 0)

        indexed_rows, indexed_steps = measured()
        plan = [
            str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + query, ("observation-1",))
        ]
        assert any(INDEX in detail and "SEARCH" in detail for detail in plan)
        conn.execute(f"DROP INDEX {INDEX}")
        unindexed_rows, unindexed_steps = measured()
        assert indexed_rows == unindexed_rows == [("resolution-1",)]
        assert indexed_steps < 100
        assert unindexed_steps > indexed_steps * 100
    finally:
        conn.close()
