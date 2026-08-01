from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance.latest_governed_state import (
    GovernedCurrentMaterializer,
    LatestGovernedRefreshRequest,
    LatestGovernedRefreshResult,
    LatestGovernedReprojectionRequest,
    LatestGovernedStateError,
    build_latest_governed_fact_search_query,
    refresh_latest_governed_state,
    reproject_latest_governed_state,
    search_latest_governed_facts,
    search_latest_governed_narrative,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = datetime(2026, 7, 30, 10, tzinfo=UTC)


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE v_population_cutover_current (
          population_run_id TEXT, receipt_set_sha256 TEXT,
          knowledge_cutoff TEXT, observed_through TEXT
        );
        CREATE TABLE v_ask_retrieval_scope_current (
          promotion_id TEXT, scope_key TEXT, status TEXT,
          research_snapshot_id TEXT, fact_generation_id TEXT,
          fact_projection_seal_sha256 TEXT, source_inventory_set_json TEXT,
          narrative_bundles_json TEXT, cutoff_at TEXT, population_run_id TEXT,
          population_receipt_set_sha256 TEXT, population_observed_through TEXT,
          issuer_id TEXT, reporting_entity_id TEXT,
          source_scope_key TEXT, source_scope_revision_id TEXT
        );
        CREATE TABLE v_issuer_reporting_scope_current (
          scope_revision_id TEXT, scope_key TEXT, issuer_id TEXT,
          inclusion_state TEXT
        );
        CREATE TABLE source_fact_publication_stream (
          publication_sequence INTEGER, sealed_at TEXT, assigned_at TEXT
        );
        CREATE TABLE canonical_fact_projection_generations (
          generation_id TEXT PRIMARY KEY, generation_kind TEXT,
          parent_generation_id TEXT
        );
        CREATE TABLE canonical_fact_projection_seals (
          generation_id TEXT PRIMARY KEY, projection_seal_sha256 TEXT
        );
        CREATE TABLE reporting_entities (
          reporting_entity_id TEXT PRIMARY KEY, issuer_id TEXT
        );
        CREATE TABLE research_snapshot_universe_commitments (
          research_snapshot_id TEXT PRIMARY KEY, issuer_id TEXT,
          reporting_entity_ids_json TEXT
        );
        CREATE TABLE source_inventory_snapshots (
          snapshot_id TEXT PRIMARY KEY, issuer_id TEXT, outcome TEXT
        );
        CREATE TABLE source_inventory_snapshot_seals (
          snapshot_id TEXT PRIMARY KEY, completion_status TEXT
        );
        CREATE TABLE canonical_metric_cells (
          canonical_metric_cell_id TEXT PRIMARY KEY, reporting_entity_id TEXT
        );
        CREATE TABLE canonical_fact_projection_entries (
          generation_id TEXT, entry_ordinal INTEGER, change_kind TEXT,
          canonical_metric_cell_id TEXT, canonical_resolution_revision_id TEXT,
          selected_observation_id TEXT, canonical_metric_name TEXT,
          period_kind TEXT, period_start TEXT, period_end TEXT, unit_key TEXT,
          currency TEXT, value_kind TEXT, canonical_value TEXT,
          canonical_search_text TEXT, entry_sha256 TEXT,
          evidence_document_version_id TEXT, evidence_node_id TEXT,
          evidence_locator_json TEXT, evidence_locator_sha256 TEXT,
          source_publication_id TEXT, source_publication_seal_id TEXT,
          source_publication_member_id TEXT, source_fact_cell_id TEXT,
          binding_revision_id TEXT, binding_commitment_sha256 TEXT,
          mapping_revision_id TEXT, mapping_commitment_sha256 TEXT,
          metric_definition_revision_id TEXT,
          metric_definition_commitment_sha256 TEXT
        );
        CREATE TABLE expected_documents (
          expected_document_id TEXT, expected_document_key TEXT,
          snapshot_id TEXT, source_kind TEXT, document_type TEXT,
          period_start TEXT, period_end TEXT
        );
        CREATE TABLE evidence_document_versions (
          document_version_id TEXT PRIMARY KEY, blob_sha256 TEXT
        );
        CREATE TABLE search_corpus_document_memberships (
          manifest_id TEXT, expected_document_key TEXT, document_version_id TEXT,
          membership_status TEXT, reason TEXT
        );
        CREATE TABLE evidence_extraction_runs (
          extraction_run_id TEXT PRIMARY KEY, document_version_id TEXT
        );
        CREATE TABLE evidence_nodes (
          node_id TEXT PRIMARY KEY, extraction_run_id TEXT
        );
        CREATE TABLE search_chunks (
          chunk_id TEXT PRIMARY KEY, manifest_id TEXT, evidence_node_id TEXT,
          chunk_key TEXT, text TEXT, content_sha256 TEXT,
          chunker_config_sha256 TEXT
        );
        CREATE TABLE search_embedding_artifacts (
          embedding_artifact_id TEXT, index_run_id TEXT, chunk_id TEXT,
          outcome TEXT
        );

        CREATE TABLE latest_governed_refresh_runs (
          refresh_run_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
          scope_key TEXT, status TEXT, baseline_population_run_id TEXT,
          baseline_population_receipt_sha256 TEXT, baseline_promotion_id TEXT,
          baseline_fact_generation_id TEXT, input_head_sha256 TEXT,
          policy_config_sha256 TEXT, knowledge_cutoff TEXT,
          observed_through TEXT, resume_cursor_json TEXT,
          resume_cursor_sha256 TEXT, staged_change_count INTEGER,
          applied_change_count INTEGER, planned_at TEXT, updated_at TEXT
        );
        CREATE TABLE latest_governed_refresh_stage (
          refresh_run_id TEXT, stage_ordinal INTEGER, entity_kind TEXT,
          change_kind TEXT, coordinate_key TEXT, digest_bucket INTEGER,
          prior_commitment_sha256 TEXT, current_commitment_sha256 TEXT,
          canonical_payload_json TEXT, payload_sha256 TEXT, stage_status TEXT,
          staged_at TEXT, applied_at TEXT,
          PRIMARY KEY(refresh_run_id,stage_ordinal),
          UNIQUE(refresh_run_id,entity_kind,coordinate_key)
        );
        CREATE TABLE latest_governed_refresh_receipts (
          receipt_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
          refresh_run_id TEXT UNIQUE, scope_key TEXT, prior_receipt_id TEXT,
          baseline_population_run_id TEXT,
          baseline_population_receipt_sha256 TEXT, baseline_promotion_id TEXT,
          fact_generation_id TEXT, input_head_sha256 TEXT,
          prior_state_sha256 TEXT, current_state_sha256 TEXT,
          fact_root_sha256 TEXT, document_root_sha256 TEXT,
          narrative_root_sha256 TEXT, change_count INTEGER,
          fact_change_count INTEGER, document_change_count INTEGER,
          narrative_change_count INTEGER, canonical_change_set_json TEXT,
          change_set_sha256 TEXT, canonical_receipt_json TEXT,
          receipt_sha256 TEXT UNIQUE, knowledge_cutoff TEXT,
          observed_through TEXT, sealed_at TEXT
        );
        CREATE TABLE latest_governed_refresh_changes (
          change_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
          receipt_id TEXT, change_ordinal INTEGER, entity_kind TEXT,
          change_kind TEXT, coordinate_key TEXT, digest_bucket INTEGER,
          prior_commitment_sha256 TEXT, current_commitment_sha256 TEXT,
          selection_reason TEXT, source_evidence_json TEXT,
          source_evidence_sha256 TEXT, canonical_change_json TEXT,
          change_sha256 TEXT, knowledge_cutoff TEXT, observed_through TEXT,
          recorded_at TEXT
        );
        CREATE TABLE latest_governed_scope_heads (
          scope_key TEXT PRIMARY KEY, refresh_receipt_id TEXT UNIQUE,
          population_run_id TEXT, promotion_id TEXT, fact_generation_id TEXT,
          source_heads_json TEXT, source_heads_sha256 TEXT, state_sha256 TEXT,
          fact_root_sha256 TEXT, document_root_sha256 TEXT,
          narrative_root_sha256 TEXT, fact_count INTEGER,
          document_count INTEGER, narrative_count INTEGER,
          knowledge_cutoff TEXT, observed_through TEXT, updated_at TEXT
        );
        CREATE TABLE latest_governed_fact_entries (
          scope_key TEXT, canonical_metric_cell_id TEXT, digest_bucket INTEGER,
          refresh_receipt_id TEXT, fact_generation_id TEXT,
          canonical_resolution_revision_id TEXT, selected_observation_id TEXT,
          canonical_metric_name TEXT, period_kind TEXT, period_start TEXT,
          period_end TEXT, unit_key TEXT, currency TEXT, value_kind TEXT,
          canonical_value TEXT, canonical_search_text TEXT,
          selection_reason TEXT, source_evidence_json TEXT,
          source_evidence_sha256 TEXT, prior_commitment_sha256 TEXT,
          current_commitment_sha256 TEXT, knowledge_cutoff TEXT,
          observed_through TEXT, updated_at TEXT,
          PRIMARY KEY(scope_key,canonical_metric_cell_id)
        );
        CREATE INDEX ix_latest_governed_fact_search
          ON latest_governed_fact_entries(
            scope_key,canonical_metric_name,period_end DESC,canonical_metric_cell_id
          );
        CREATE TABLE latest_governed_document_entries (
          scope_key TEXT, expected_document_key TEXT, digest_bucket INTEGER,
          refresh_receipt_id TEXT, expected_document_id TEXT,
          document_version_id TEXT, source_kind TEXT, document_type TEXT,
          period_start TEXT, period_end TEXT, selection_reason TEXT,
          source_evidence_json TEXT, source_evidence_sha256 TEXT,
          prior_commitment_sha256 TEXT, current_commitment_sha256 TEXT,
          knowledge_cutoff TEXT, observed_through TEXT, updated_at TEXT,
          PRIMARY KEY(scope_key,expected_document_key)
        );
        CREATE TABLE latest_governed_narrative_entries (
          scope_key TEXT, expected_document_key TEXT, chunk_key TEXT,
          digest_bucket INTEGER, refresh_receipt_id TEXT,
          document_version_id TEXT, evidence_node_id TEXT,
          source_chunk_id TEXT, embedding_artifact_id TEXT, text TEXT,
          content_sha256 TEXT, chunker_config_sha256 TEXT,
          selection_reason TEXT, prior_commitment_sha256 TEXT,
          current_commitment_sha256 TEXT, knowledge_cutoff TEXT,
          observed_through TEXT, updated_at TEXT,
          PRIMARY KEY(scope_key,expected_document_key,chunk_key)
        );
        CREATE VIRTUAL TABLE latest_governed_narrative_fts USING fts5(
          scope_key UNINDEXED, expected_document_key UNINDEXED,
          chunk_key UNINDEXED, text,
          content='latest_governed_narrative_entries', content_rowid='rowid'
        );
        CREATE TRIGGER latest_narrative_ai
        AFTER INSERT ON latest_governed_narrative_entries BEGIN
          INSERT INTO latest_governed_narrative_fts(
            rowid,scope_key,expected_document_key,chunk_key,text
          ) VALUES(
            new.rowid,new.scope_key,new.expected_document_key,new.chunk_key,new.text
          );
        END;
        CREATE TRIGGER latest_narrative_ad
        AFTER DELETE ON latest_governed_narrative_entries BEGIN
          INSERT INTO latest_governed_narrative_fts(
            latest_governed_narrative_fts,rowid,scope_key,
            expected_document_key,chunk_key,text
          ) VALUES(
            'delete',old.rowid,old.scope_key,old.expected_document_key,
            old.chunk_key,old.text
          );
        END;
        CREATE TRIGGER latest_narrative_au
        AFTER UPDATE ON latest_governed_narrative_entries BEGIN
          INSERT INTO latest_governed_narrative_fts(
            latest_governed_narrative_fts,rowid,scope_key,
            expected_document_key,chunk_key,text
          ) VALUES(
            'delete',old.rowid,old.scope_key,old.expected_document_key,
            old.chunk_key,old.text
          );
          INSERT INTO latest_governed_narrative_fts(
            rowid,scope_key,expected_document_key,chunk_key,text
          ) VALUES(
            new.rowid,new.scope_key,new.expected_document_key,new.chunk_key,new.text
          );
        END;
        """
    )
    _seed_frontier(conn)
    return conn


def _seed_frontier(conn: sqlite3.Connection) -> None:
    clock = T0.isoformat()
    bundles = json.dumps(
        [
            {
                "corpus_manifest_id": "manifest-1",
                "lexical_index_run_id": "lexical-1",
                "vector_index_run_id": "vector-1",
                "embedding_promotion_id": "embedding-promotion-1",
            }
        ],
        sort_keys=True,
    )
    conn.execute(
        "INSERT INTO v_population_cutover_current VALUES (?,?,?,?)",
        ("population-1", SHA_A, clock, clock),
    )
    conn.execute(
        "INSERT INTO v_issuer_reporting_scope_current VALUES (?,?,?,?)",
        ("scope-revision-1", "investor-research", "issuer-1", "core"),
    )
    conn.execute(
        "INSERT INTO v_ask_retrieval_scope_current VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "promotion-1",
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "promoted",
            "research-1",
            "generation-1",
            SHA_B,
            '["inventory-1"]',
            bundles,
            clock,
            "population-1",
            SHA_A,
            clock,
            "issuer-1",
            "reporting-1",
            "investor-research",
            "scope-revision-1",
        ),
    )
    conn.execute(
        "INSERT INTO source_fact_publication_stream VALUES (?,?,?)",
        (1, clock, clock),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
        ("generation-1", "checkpoint", None),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
        ("generation-1", SHA_B),
    )
    _seed_universe(conn, "research-1", "issuer-1", ("reporting-1",))
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES (?,?,?)",
        ("inventory-1", "issuer-1", "succeeded"),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshot_seals VALUES (?,?)",
        ("inventory-1", "complete"),
    )
    _fact_entry(conn, "generation-1", "cell-1", SHA_C, "100")
    conn.execute(
        "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?)",
        ("expected-1", "10-q:2026-q2", "inventory-1", "sec_filing", "10-Q", None, clock),
    )
    conn.execute("INSERT INTO evidence_document_versions VALUES (?,?)", ("doc-1", SHA_A))
    conn.execute(
        "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?)",
        ("manifest-1", "10-q:2026-q2", "doc-1", "included", "current"),
    )
    conn.execute("INSERT INTO evidence_extraction_runs VALUES (?,?)", ("extract-1", "doc-1"))
    conn.execute("INSERT INTO evidence_nodes VALUES (?,?)", ("node-1", "extract-1"))
    conn.execute(
        "INSERT INTO search_chunks VALUES (?,?,?,?,?,?,?)",
        ("chunk-1", "manifest-1", "node-1", "chunk:q2", "demand accelerated", SHA_C, SHA_D),
    )
    conn.execute(
        "INSERT INTO search_embedding_artifacts VALUES (?,?,?,?)",
        ("embedding-1", "vector-1", "chunk-1", "succeeded"),
    )
    conn.commit()


def _seed_universe(
    conn: sqlite3.Connection,
    research_snapshot_id: str,
    issuer_id: str,
    reporting_entity_ids: tuple[str, ...],
) -> None:
    for reporting_entity_id in reporting_entity_ids:
        conn.execute(
            "INSERT OR IGNORE INTO reporting_entities VALUES (?,?)",
            (reporting_entity_id, issuer_id),
        )
    conn.execute(
        "INSERT OR IGNORE INTO research_snapshot_universe_commitments VALUES (?,?,?)",
        (
            research_snapshot_id,
            issuer_id,
            json.dumps(list(reporting_entity_ids), separators=(",", ":")),
        ),
    )


def _fact_entry(
    conn: sqlite3.Connection,
    generation_id: str,
    cell_id: str,
    commitment: str,
    value: str | None,
    *,
    change_kind: str = "upsert",
    reporting_entity_id: str = "reporting-1",
    entry_ordinal: int = 0,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO canonical_metric_cells VALUES (?,?)",
        (cell_id, reporting_entity_id),
    )
    conn.execute(
        """
        INSERT INTO canonical_fact_projection_entries (
          generation_id,entry_ordinal,change_kind,canonical_metric_cell_id,
          canonical_resolution_revision_id,selected_observation_id,
          canonical_metric_name,period_kind,period_end,unit_key,currency,
          value_kind,canonical_value,canonical_search_text,entry_sha256,
          evidence_locator_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            generation_id,
            entry_ordinal,
            change_kind,
            cell_id,
            "resolution-" + cell_id,
            "observation-" + cell_id,
            "revenue",
            "duration",
            "2026-06-30",
            "USD",
            "USD",
            "numeric",
            value,
            "revenue 2026 issuer",
            commitment,
            "{}",
        ),
    )


def _request(**changes: object) -> LatestGovernedRefreshRequest:
    values: dict[str, object] = {
        "scope_id": "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
        "operation_recorded_at": T0 + timedelta(minutes=1),
        "apply": True,
        "max_batch_rows": 100,
    }
    values.update(changes)
    return LatestGovernedRefreshRequest.model_validate(values)


def _advance_delta(
    conn: sqlite3.Connection,
    *,
    generation: str,
    commitment: str | None,
    change_kind: str = "upsert",
) -> None:
    new_time = (T0 + timedelta(hours=1)).isoformat()
    conn.execute("DELETE FROM v_population_cutover_current")
    conn.execute(
        "INSERT INTO v_population_cutover_current VALUES (?,?,?,?)",
        ("population-2", SHA_D, new_time, new_time),
    )
    conn.execute("DELETE FROM v_ask_retrieval_scope_current")
    bundle = json.dumps(
        [
            {
                "corpus_manifest_id": "manifest-1",
                "lexical_index_run_id": "lexical-1",
                "vector_index_run_id": "vector-1",
                "embedding_promotion_id": "embedding-promotion-1",
            }
        ]
    )
    conn.execute(
        "INSERT INTO v_ask_retrieval_scope_current VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "promotion-2",
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "promoted",
            "research-2",
            generation,
            SHA_D,
            '["inventory-1"]',
            bundle,
            new_time,
            "population-2",
            SHA_D,
            new_time,
            "issuer-1",
            "reporting-1",
            "investor-research",
            "scope-revision-1",
        ),
    )
    _seed_universe(conn, "research-2", "issuer-1", ("reporting-1",))
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
        (generation, "delta", "generation-1"),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
        (generation, SHA_D),
    )
    _fact_entry(
        conn,
        generation,
        "cell-1",
        SHA_D if commitment is None else commitment,
        "110" if change_kind == "upsert" else None,
        change_kind=change_kind,
    )
    conn.commit()


def _seed_large_retained_document_corpus(
    conn: sqlite3.Connection,
    *,
    retained_document_count: int = 200,
    chunks_per_document: int = 8,
) -> None:
    expected_rows: list[tuple[object, ...]] = []
    version_rows: list[tuple[object, ...]] = []
    membership_rows: list[tuple[object, ...]] = []
    extraction_rows: list[tuple[object, ...]] = []
    node_rows: list[tuple[object, ...]] = []
    chunk_rows: list[tuple[object, ...]] = []
    for ordinal in range(retained_document_count):
        key = f"retained:{ordinal:04d}"
        version_id = f"retained-version-{ordinal:04d}"
        node_id = f"retained-node-{ordinal:04d}"
        expected_rows.append(
            (
                f"retained-expected-{ordinal:04d}",
                key,
                "inventory-1",
                "sec_filing",
                "10-Q",
                None,
                T0.isoformat(),
            )
        )
        version_rows.append((version_id, SHA_A))
        membership_rows.append(("manifest-1", key, version_id, "included", "current"))
        extraction_rows.append((f"retained-extract-{ordinal:04d}", version_id))
        node_rows.append((node_id, f"retained-extract-{ordinal:04d}"))
        for chunk_ordinal in range(chunks_per_document):
            chunk_rows.append(
                (
                    f"retained-chunk-{ordinal:04d}-{chunk_ordinal:02d}",
                    "manifest-1",
                    node_id,
                    f"chunk:{chunk_ordinal:02d}",
                    f"retained narrative {ordinal} {chunk_ordinal}",
                    SHA_C,
                    SHA_D,
                )
            )
    for chunk_ordinal in range(1, chunks_per_document):
        chunk_rows.append(
            (
                f"chunk-q2-{chunk_ordinal:02d}",
                "manifest-1",
                "node-1",
                f"chunk:q2:{chunk_ordinal:02d}",
                f"demand accelerated section {chunk_ordinal}",
                SHA_C,
                SHA_D,
            )
        )
    conn.executemany(
        "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?)",
        expected_rows,
    )
    conn.executemany(
        "INSERT INTO evidence_document_versions VALUES (?,?)",
        version_rows,
    )
    conn.executemany(
        "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?)",
        membership_rows,
    )
    conn.executemany(
        "INSERT INTO evidence_extraction_runs VALUES (?,?)",
        extraction_rows,
    )
    conn.executemany(
        "INSERT INTO evidence_nodes VALUES (?,?)",
        node_rows,
    )
    conn.executemany(
        "INSERT INTO search_chunks VALUES (?,?,?,?,?,?,?)",
        chunk_rows,
    )
    conn.commit()


def _promote_one_document_delta(
    conn: sqlite3.Connection,
    *,
    chunks_per_document: int = 8,
) -> None:
    clock = (T0 + timedelta(hours=1)).isoformat()
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES (?,?)",
        ("doc-2", SHA_B),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?)",
        ("extract-2", "doc-2"),
    )
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?)",
        ("node-2", "extract-2"),
    )
    conn.execute(
        """
        INSERT INTO search_corpus_document_memberships
        SELECT 'manifest-2',expected_document_key,
               CASE WHEN expected_document_key='10-q:2026-q2'
                    THEN 'doc-2' ELSE document_version_id END,
               membership_status,'new immutable manifest'
        FROM search_corpus_document_memberships
        WHERE manifest_id='manifest-1'
        """
    )
    conn.execute(
        """
        INSERT INTO search_chunks
        SELECT 'manifest-2:' || chunk_id,'manifest-2',evidence_node_id,
               chunk_key,text,content_sha256,chunker_config_sha256
        FROM search_chunks
        WHERE manifest_id='manifest-1'
          AND evidence_node_id<>'node-1'
        """
    )
    target_chunks = [
        (
            f"manifest-2:chunk-q2-{ordinal:02d}",
            "manifest-2",
            "node-2",
            "chunk:q2" if ordinal == 0 else f"chunk:q2:{ordinal:02d}",
            f"changed governed narrative {ordinal}",
            SHA_B,
            SHA_D,
        )
        for ordinal in range(chunks_per_document)
    ]
    conn.executemany(
        "INSERT INTO search_chunks VALUES (?,?,?,?,?,?,?)",
        target_chunks,
    )
    conn.execute("DELETE FROM v_population_cutover_current")
    conn.execute(
        "INSERT INTO v_population_cutover_current VALUES (?,?,?,?)",
        ("population-2", SHA_D, clock, clock),
    )
    conn.execute("DELETE FROM v_ask_retrieval_scope_current")
    bundle = json.dumps(
        [
            {
                "corpus_manifest_id": "manifest-2",
                "lexical_index_run_id": "lexical-2",
                "vector_index_run_id": "vector-2",
                "embedding_promotion_id": "embedding-promotion-2",
            }
        ]
    )
    conn.execute(
        "INSERT INTO v_ask_retrieval_scope_current VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "promotion-2",
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "promoted",
            "research-2",
            "generation-2",
            SHA_D,
            '["inventory-1"]',
            bundle,
            clock,
            "population-2",
            SHA_D,
            clock,
            "issuer-1",
            "reporting-1",
            "investor-research",
            "scope-revision-1",
        ),
    )
    _seed_universe(conn, "research-2", "issuer-1", ("reporting-1",))
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
        ("generation-2", "delta", "generation-1"),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
        ("generation-2", SHA_D),
    )
    conn.commit()


def _seed_second_inventory_document(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?)",
        (
            "expected-retained",
            "10-k:2025",
            "inventory-1",
            "sec_filing",
            "10-K",
            None,
            T0.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES (?,?)",
        ("doc-retained", SHA_B),
    )
    conn.execute(
        "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?)",
        ("manifest-1", "10-k:2025", "doc-retained", "included", "current"),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?)",
        ("extract-retained", "doc-retained"),
    )
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?)",
        ("node-retained", "extract-retained"),
    )
    conn.execute(
        "INSERT INTO search_chunks VALUES (?,?,?,?,?,?,?)",
        (
            "chunk-retained",
            "manifest-1",
            "node-retained",
            "chunk:retained",
            "retained narrative",
            SHA_B,
            SHA_D,
        ),
    )
    conn.execute(
        "INSERT INTO search_embedding_artifacts VALUES (?,?,?,?)",
        ("embedding-retained", "vector-1", "chunk-retained", "succeeded"),
    )
    conn.commit()


def _rollover_source_inventory(
    conn: sqlite3.Connection,
    *,
    retain_second: bool,
    duplicate_current_q2: bool = False,
) -> None:
    clock = (T0 + timedelta(hours=1)).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO source_inventory_snapshots VALUES (?,?,?)",
        ("inventory-2", "issuer-1", "succeeded"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO source_inventory_snapshot_seals VALUES (?,?)",
        ("inventory-2", "complete"),
    )
    conn.execute(
        "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?)",
        (
            "expected-2",
            "10-q:2026-q2",
            "inventory-2",
            "sec_filing",
            "10-Q",
            None,
            T0.isoformat(),
        ),
    )
    if duplicate_current_q2:
        conn.execute(
            "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?)",
            (
                "expected-2-conflict",
                "10-q:2026-q2",
                "inventory-2",
                "sec_filing",
                "10-Q",
                None,
                T0.isoformat(),
            ),
        )
    if retain_second:
        conn.execute(
            "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?)",
            (
                "expected-retained",
                "10-k:2025",
                "inventory-2",
                "sec_filing",
                "10-K",
                None,
                T0.isoformat(),
            ),
        )
    if not retain_second:
        conn.execute(
            "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?)",
            ("manifest-2", "10-q:2026-q2", "doc-1", "included", "current"),
        )
    conn.execute("DELETE FROM v_population_cutover_current")
    conn.execute(
        "INSERT INTO v_population_cutover_current VALUES (?,?,?,?)",
        ("population-2", SHA_D, clock, clock),
    )
    conn.execute("DELETE FROM v_ask_retrieval_scope_current")
    manifest_id = "manifest-1" if retain_second else "manifest-2"
    bundles = json.dumps(
        [
            {
                "corpus_manifest_id": manifest_id,
                "lexical_index_run_id": "lexical-1" if retain_second else "lexical-2",
                "vector_index_run_id": "vector-1" if retain_second else "vector-2",
                "embedding_promotion_id": (
                    "embedding-promotion-1" if retain_second else "embedding-promotion-2"
                ),
            }
        ],
        sort_keys=True,
    )
    conn.execute(
        "INSERT INTO v_ask_retrieval_scope_current VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "promotion-2",
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "promoted",
            "research-2",
            "generation-1",
            SHA_B,
            '["inventory-2"]',
            bundles,
            clock,
            "population-2",
            SHA_D,
            clock,
            "issuer-1",
            "reporting-1",
            "investor-research",
            "scope-revision-1",
        ),
    )
    _seed_universe(conn, "research-2", "issuer-1", ("reporting-1",))
    conn.commit()


def test_initial_refresh_latest_reads_and_exact_noop_replay() -> None:
    conn = _database()
    conn.executemany(
        "INSERT INTO source_fact_publication_stream VALUES (?,?,?)",
        [
            (2, T0.isoformat(), T0.isoformat()),
            (3, T0.isoformat(), T0.isoformat()),
        ],
    )
    conn.commit()
    first = refresh_latest_governed_state(conn, _request())
    assert first.outcome == "changed"
    assert first.source_event_count == 3
    assert first.fact_change_count == 1
    assert (first.fact_change_count, first.document_change_count) == (1, 1)
    assert first.narrative_change_count == 1
    assert first.current_write_count == 3
    assert (
        search_latest_governed_facts(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "revenue",
            5,
        )[0].canonical_value
        == "100"
    )
    narrative = search_latest_governed_narrative(
        conn,
        "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
        "accelerated",
        5,
    )
    assert narrative[0].embedding_artifact_id == "embedding-1"

    noop = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(minutes=2)),
    )
    assert noop.outcome == "no_op"
    assert noop.current_write_count == 0
    assert noop.receipt_write_count == 1
    receipt_count = conn.execute(
        "SELECT COUNT(*) FROM latest_governed_refresh_receipts"
    ).fetchone()[0]
    replay = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(minutes=3)),
    )
    assert replay == noop
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_stage").fetchone() == (0,)
    assert (
        conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_receipts").fetchone()[0]
        == receipt_count
    )


def test_latest_refresh_never_falls_back_to_ambiguous_raw_scope_key() -> None:
    conn = _database()

    with pytest.raises(
        LatestGovernedStateError,
        match="one current promoted Ask retrieval scope is required",
    ):
        refresh_latest_governed_state(
            conn,
            _request(scope_id="investor-research"),
        )

    assert conn.execute("SELECT COUNT(*) FROM latest_governed_scope_heads").fetchone() == (0,)


def test_small_direct_delta_and_tombstone_never_select_conflict() -> None:
    conn = _database()
    refresh_latest_governed_state(conn, _request())
    _advance_delta(conn, generation="generation-2", commitment=SHA_D)
    delta = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
    )
    assert delta.fact_change_count == 1
    assert delta.document_change_count == 0
    assert delta.narrative_change_count == 0
    assert delta.current_write_count == 1
    assert (
        search_latest_governed_facts(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "revenue",
            5,
        )[0].canonical_value
        == "110"
    )

    # A governed unresolved/conflict outcome appears downstream as a tombstone.
    later = T0 + timedelta(hours=2)
    conn.execute("DELETE FROM v_population_cutover_current")
    conn.execute(
        "INSERT INTO v_population_cutover_current VALUES (?,?,?,?)",
        ("population-3", SHA_C, later.isoformat(), later.isoformat()),
    )
    conn.execute("DELETE FROM v_ask_retrieval_scope_current")
    promotion = list(
        conn.execute("SELECT * FROM v_ask_retrieval_scope_current WHERE 0").description or ()
    )
    assert promotion  # preserve the faithful schema assertion
    bundles = json.dumps(
        [
            {
                "corpus_manifest_id": "manifest-1",
                "lexical_index_run_id": "lexical-1",
                "vector_index_run_id": "vector-1",
                "embedding_promotion_id": "embedding-promotion-1",
            }
        ]
    )
    conn.execute(
        "INSERT INTO v_ask_retrieval_scope_current VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "promotion-3",
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "promoted",
            "research-3",
            "generation-3",
            SHA_C,
            '["inventory-1"]',
            bundles,
            later.isoformat(),
            "population-3",
            SHA_C,
            later.isoformat(),
            "issuer-1",
            "reporting-1",
            "investor-research",
            "scope-revision-1",
        ),
    )
    _seed_universe(conn, "research-3", "issuer-1", ("reporting-1",))
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
        ("generation-3", "delta", "generation-2"),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
        ("generation-3", SHA_C),
    )
    _fact_entry(conn, "generation-3", "cell-1", SHA_C, None, change_kind="delete")
    conn.commit()
    tombstone = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=later + timedelta(minutes=1)),
    )
    assert tombstone.fact_change_count == 1
    assert (
        search_latest_governed_facts(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "revenue",
            5,
        )
        == ()
    )


def test_direct_delta_reads_only_changed_current_fact_coordinates() -> None:
    conn = _database()
    refresh_latest_governed_state(conn, _request())
    template = conn.execute(
        "SELECT * FROM latest_governed_fact_entries "
        "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' AND canonical_metric_cell_id='cell-1'"
    ).fetchone()
    assert template is not None
    columns = [
        str(row[1]) for row in conn.execute("PRAGMA table_info(latest_governed_fact_entries)")
    ]
    for ordinal in range(200):
        values = list(template)
        values[columns.index("canonical_metric_cell_id")] = f"retained-{ordinal:04d}"
        values[columns.index("canonical_metric_name")] = "retained history"
        values[columns.index("current_commitment_sha256")] = SHA_A
        conn.execute(
            "INSERT INTO latest_governed_fact_entries VALUES ("
            + ",".join("?" for _ in values)
            + ")",
            values,
        )
    conn.commit()
    _advance_delta(conn, generation="generation-2", commitment=SHA_D)
    result = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
    )
    assert result.fact_change_count == 1
    assert result.source_read_count == 1
    assert result.current_read_count == 1
    plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT canonical_metric_cell_id,"
            "current_commitment_sha256 FROM latest_governed_fact_entries "
            "WHERE scope_key=? AND canonical_metric_cell_id IN (?)",
            (
                "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
                "cell-1",
            ),
        )
    )
    assert "sqlite_autoindex_latest_governed_fact_entries_1" in plan


def test_shared_generation_isolated_by_promoted_reporting_entity_for_upserts_and_deletes() -> None:
    conn = _database()
    clock = T0.isoformat()
    bundles = str(
        conn.execute(
            "SELECT narrative_bundles_json FROM v_ask_retrieval_scope_current "
            "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3'"
        ).fetchone()[0]
    )
    _seed_universe(conn, "research-other", "issuer-2", ("reporting-2",))
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES (?,?,?)",
        ("inventory-other", "issuer-2", "succeeded"),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshot_seals VALUES (?,?)",
        ("inventory-other", "complete"),
    )
    _fact_entry(
        conn,
        "generation-1",
        "cell-2",
        SHA_D,
        "200",
        reporting_entity_id="reporting-2",
        entry_ordinal=1,
    )
    conn.execute(
        "INSERT INTO v_ask_retrieval_scope_current VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "promotion-other-1",
            "ask-scope:v1:0a03af0ac0dccd62d74966db96111cc02d144581edba5f3d9e16730c12b71ac0",
            "promoted",
            "research-other",
            "generation-1",
            SHA_B,
            '["inventory-other"]',
            bundles,
            clock,
            "population-1",
            SHA_A,
            clock,
            "issuer-2",
            "reporting-2",
            "investor-research",
            "scope-revision-2",
        ),
    )
    conn.execute(
        "INSERT INTO v_issuer_reporting_scope_current VALUES (?,?,?,?)",
        ("scope-revision-2", "investor-research", "issuer-2", "core"),
    )
    conn.commit()

    refresh_latest_governed_state(conn, _request())
    refresh_latest_governed_state(
        conn,
        _request(
            scope_id="ask-scope:v1:0a03af0ac0dccd62d74966db96111cc02d144581edba5f3d9e16730c12b71ac0"
        ),
    )
    assert conn.execute(
        "SELECT scope_key,canonical_metric_cell_id,canonical_value "
        "FROM latest_governed_fact_entries "
        "ORDER BY scope_key,canonical_metric_cell_id"
    ).fetchall() == [
        (
            "ask-scope:v1:0a03af0ac0dccd62d74966db96111cc02d144581edba5f3d9e16730c12b71ac0",
            "cell-2",
            "200",
        ),
        (
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "cell-1",
            "100",
        ),
    ]

    next_clock = (T0 + timedelta(hours=1)).isoformat()
    conn.execute("DELETE FROM v_population_cutover_current")
    conn.execute(
        "INSERT INTO v_population_cutover_current VALUES (?,?,?,?)",
        ("population-2", SHA_D, next_clock, next_clock),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
        ("generation-2", "delta", "generation-1"),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
        ("generation-2", SHA_D),
    )
    _fact_entry(
        conn,
        "generation-2",
        "cell-1",
        SHA_C,
        None,
        change_kind="delete",
    )
    _fact_entry(
        conn,
        "generation-2",
        "cell-2",
        SHA_A,
        "210",
        reporting_entity_id="reporting-2",
        entry_ordinal=1,
    )
    conn.execute(
        "UPDATE v_ask_retrieval_scope_current SET "
        "promotion_id='promotion-2-' || scope_key,"
        "fact_generation_id='generation-2',"
        "fact_projection_seal_sha256=?,cutoff_at=?,"
        "population_run_id='population-2',"
        "population_receipt_set_sha256=?,population_observed_through=?",
        (SHA_D, next_clock, SHA_D, next_clock),
    )
    conn.commit()

    first_delta = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
    )
    second_delta = refresh_latest_governed_state(
        conn,
        _request(
            scope_id=(
                "ask-scope:v1:0a03af0ac0dccd62d74966db96111cc02d144581edba5f3d9e16730c12b71ac0"
            ),
            operation_recorded_at=T0 + timedelta(hours=1, minutes=1),
        ),
    )
    assert (first_delta.fact_change_count, second_delta.fact_change_count) == (1, 1)
    assert conn.execute(
        "SELECT scope_key,canonical_metric_cell_id,canonical_value "
        "FROM latest_governed_fact_entries "
        "ORDER BY scope_key,canonical_metric_cell_id"
    ).fetchall() == [
        (
            "ask-scope:v1:0a03af0ac0dccd62d74966db96111cc02d144581edba5f3d9e16730c12b71ac0",
            "cell-2",
            "210",
        )
    ]


def test_reporting_entity_binding_rollover_forces_full_scope_replacement() -> None:
    conn = _database()
    initial = refresh_latest_governed_state(conn, _request())
    next_clock = (T0 + timedelta(hours=1)).isoformat()
    _seed_universe(conn, "research-rollover", "issuer-1", ("reporting-2",))
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES (?,?,?)",
        ("inventory-2", "issuer-1", "succeeded"),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshot_seals VALUES (?,?)",
        ("inventory-2", "complete"),
    )
    conn.execute(
        "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?)",
        ("expected-2", "10-q:2026-q3", "inventory-2", "sec_filing", "10-Q", None, next_clock),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES (?,?)",
        ("doc-2", SHA_B),
    )
    conn.execute(
        "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?)",
        ("manifest-2", "10-q:2026-q3", "doc-2", "included", "current"),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?)",
        ("extract-2", "doc-2"),
    )
    conn.execute("INSERT INTO evidence_nodes VALUES (?,?)", ("node-2", "extract-2"))
    conn.execute(
        "INSERT INTO search_chunks VALUES (?,?,?,?,?,?,?)",
        ("chunk-2", "manifest-2", "node-2", "chunk:q3", "new issuer", SHA_B, SHA_D),
    )
    conn.execute(
        "INSERT INTO search_embedding_artifacts VALUES (?,?,?,?)",
        ("embedding-2", "vector-2", "chunk-2", "succeeded"),
    )
    conn.execute("DELETE FROM v_population_cutover_current")
    conn.execute(
        "INSERT INTO v_population_cutover_current VALUES (?,?,?,?)",
        ("population-2", SHA_D, next_clock, next_clock),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
        ("generation-2", "delta", "generation-1"),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
        ("generation-2", SHA_D),
    )
    _fact_entry(
        conn,
        "generation-2",
        "cell-2",
        SHA_D,
        "200",
        reporting_entity_id="reporting-2",
    )
    bundles = json.dumps(
        [
            {
                "corpus_manifest_id": "manifest-2",
                "lexical_index_run_id": "lexical-2",
                "vector_index_run_id": "vector-2",
                "embedding_promotion_id": "embedding-promotion-2",
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    conn.execute(
        "UPDATE v_ask_retrieval_scope_current SET "
        "promotion_id='promotion-rollover',"
        "research_snapshot_id='research-rollover',"
        "fact_generation_id='generation-2',"
        "fact_projection_seal_sha256=?,cutoff_at=?,"
        "population_run_id='population-2',"
        "population_receipt_set_sha256=?,population_observed_through=?,"
        "issuer_id='issuer-1',reporting_entity_id='reporting-2',"
        "source_inventory_set_json='[\"inventory-2\"]',"
        "narrative_bundles_json=?",
        (SHA_D, next_clock, SHA_D, next_clock, bundles),
    )
    conn.commit()

    rollover = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
    )
    assert (
        rollover.fact_change_count,
        rollover.document_change_count,
        rollover.narrative_change_count,
        rollover.current_write_count,
    ) == (2, 2, 2, 6)
    assert conn.execute(
        "SELECT canonical_metric_cell_id,canonical_value "
        "FROM latest_governed_fact_entries WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3'"
    ).fetchall() == [("cell-2", "200")]
    assert initial.head_id != rollover.head_id
    assert conn.execute(
        "SELECT expected_document_key,document_version_id "
        "FROM latest_governed_document_entries WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3'"
    ).fetchall() == [("10-q:2026-q3", "doc-2")]
    assert conn.execute(
        "SELECT expected_document_key,chunk_key,text "
        "FROM latest_governed_narrative_entries WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3'"
    ).fetchall() == [("10-q:2026-q3", "chunk:q3", "new issuer")]


def test_reporting_entity_rollover_rejects_stale_cross_issuer_inventory() -> None:
    conn = _database()
    refresh_latest_governed_state(conn, _request())
    next_clock = (T0 + timedelta(hours=1)).isoformat()
    _seed_universe(conn, "research-rollover", "issuer-2", ("reporting-2",))
    conn.execute("DELETE FROM v_population_cutover_current")
    conn.execute(
        "INSERT INTO v_population_cutover_current VALUES (?,?,?,?)",
        ("population-2", SHA_D, next_clock, next_clock),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
        ("generation-2", "delta", "generation-1"),
    )
    conn.execute(
        "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
        ("generation-2", SHA_D),
    )
    conn.execute(
        "UPDATE v_ask_retrieval_scope_current SET "
        "promotion_id='promotion-rollover',"
        "research_snapshot_id='research-rollover',"
        "fact_generation_id='generation-2',"
        "fact_projection_seal_sha256=?,cutoff_at=?,"
        "population_run_id='population-2',"
        "population_receipt_set_sha256=?,population_observed_through=?,"
        "issuer_id='issuer-2',reporting_entity_id='reporting-2'",
        (SHA_D, next_clock, SHA_D, next_clock),
    )
    conn.commit()

    with pytest.raises(
        LatestGovernedStateError,
        match="scope ID does not match its source composite identity",
    ):
        refresh_latest_governed_state(
            conn,
            _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
        )


def test_reporting_universe_and_entity_binding_fail_closed() -> None:
    conn = _database()
    conn.execute(
        "DELETE FROM research_snapshot_universe_commitments WHERE research_snapshot_id='research-1'"
    )
    with pytest.raises(LatestGovernedStateError, match="universe binding"):
        refresh_latest_governed_state(conn, _request())

    conn = _database()
    conn.execute(
        "UPDATE research_snapshot_universe_commitments "
        "SET reporting_entity_ids_json='[\"reporting-2\"]' "
        "WHERE research_snapshot_id='research-1'"
    )
    with pytest.raises(LatestGovernedStateError, match="outside its research universe"):
        refresh_latest_governed_state(conn, _request())

    conn = _database()
    conn.execute(
        "UPDATE reporting_entities SET issuer_id='issuer-2' WHERE reporting_entity_id='reporting-1'"
    )
    with pytest.raises(LatestGovernedStateError, match="does not bind"):
        refresh_latest_governed_state(conn, _request())


def test_document_delta_reads_and_writes_only_changed_document_and_chunks() -> None:
    conn = _database()
    _seed_large_retained_document_corpus(conn)
    initial = refresh_latest_governed_state(conn, _request())
    assert initial.document_change_count == 201
    assert initial.narrative_change_count == 1_608
    retained_before = conn.execute(
        "SELECT refresh_receipt_id,source_evidence_json "
        "FROM latest_governed_document_entries "
        "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' "
        "AND expected_document_key='retained:0000'"
    ).fetchone()
    assert retained_before is not None

    _promote_one_document_delta(conn)
    delta = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
    )

    assert delta.source_event_count == 0
    assert delta.fact_change_count == 0
    assert delta.document_change_count == 1
    assert delta.narrative_change_count == 8
    assert delta.source_read_count == 9
    assert delta.current_read_count == 9
    assert delta.current_write_count == 9
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_document_entries WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3'"
    ).fetchone() == (201,)
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_narrative_entries WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3'"
    ).fetchone() == (1_608,)
    retained_after = conn.execute(
        "SELECT refresh_receipt_id,source_evidence_json "
        "FROM latest_governed_document_entries "
        "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' "
        "AND expected_document_key='retained:0000'"
    ).fetchone()
    assert retained_after == retained_before
    assert json.loads(str(retained_after[1]))["corpus_manifest_id"] == "manifest-1"


def test_staging_resume_and_idempotency_conflict() -> None:
    conn = _database()
    interrupted = refresh_latest_governed_state(
        conn,
        _request(max_batch_rows=1, interrupt_after_batches=1),
    )
    assert interrupted.outcome == "staged"
    assert interrupted.resume_cursor == "1"
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_refresh_stage WHERE refresh_run_id=?",
        (interrupted.refresh_id,),
    ).fetchone() == (1,)
    resumed = refresh_latest_governed_state(
        conn,
        _request(
            max_batch_rows=1,
            resume_refresh_id=interrupted.refresh_id,
            operation_recorded_at=T0 + timedelta(minutes=2),
        ),
    )
    assert resumed.outcome == "changed"
    assert resumed.replayed_count == 1
    assert resumed.current_write_count == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_refresh_stage WHERE refresh_run_id=?",
        (interrupted.refresh_id,),
    ).fetchone() == (0,)

    conn2 = _database()
    staged = refresh_latest_governed_state(
        conn2,
        _request(max_batch_rows=1, interrupt_after_batches=1),
    )
    conn2.execute(
        "UPDATE latest_governed_refresh_stage SET payload_sha256=? WHERE refresh_run_id=?",
        (SHA_A, staged.refresh_id),
    )
    conn2.commit()
    with pytest.raises(LatestGovernedStateError, match="idempotency conflict"):
        refresh_latest_governed_state(
            conn2,
            _request(max_batch_rows=1, resume_refresh_id=staged.refresh_id),
        )


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("change_kind", "delete"),
        ("digest_bucket", 4_095),
        ("prior_commitment_sha256", SHA_A),
        ("current_commitment_sha256", SHA_A),
        ("canonical_payload_json", '{"tampered":true}'),
        ("payload_sha256", SHA_A),
    ],
)
def test_finalization_rejects_every_tampered_stage_tuple_field_under_lock(
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    replacement: object,
) -> None:
    conn = _database()
    materializer = GovernedCurrentMaterializer(conn)

    def fail_before_head(_self: GovernedCurrentMaterializer) -> None:
        raise RuntimeError("leave complete deterministic stage for tamper test")

    monkeypatch.setattr(
        GovernedCurrentMaterializer,
        "_before_head_advance",
        fail_before_head,
    )
    with pytest.raises(RuntimeError, match="leave complete deterministic stage"):
        materializer.refresh(_request(max_batch_rows=1))
    refresh_id = str(
        conn.execute("SELECT refresh_run_id FROM latest_governed_refresh_runs").fetchone()[0]
    )
    assert conn.execute(
        "SELECT status,staged_change_count FROM latest_governed_refresh_runs "
        "WHERE refresh_run_id=?",
        (refresh_id,),
    ).fetchone() == ("ready", 3)
    conn.execute(
        f"UPDATE latest_governed_refresh_stage SET {column}=? "  # nosec B608 -- parametrized closed test set
        "WHERE refresh_run_id=?",
        (replacement, refresh_id),
    )
    conn.commit()

    def allow_head(_self: GovernedCurrentMaterializer) -> None:
        return None

    monkeypatch.setattr(
        GovernedCurrentMaterializer,
        "_before_head_advance",
        allow_head,
    )

    def preserve_tampered_stage(**_kwargs: object) -> tuple[int, int, int, int]:
        return 0, 0, 0, 3

    monkeypatch.setattr(materializer, "_stage_changes", preserve_tampered_stage)
    with pytest.raises(
        LatestGovernedStateError,
        match=r"canonical payload commitment|deterministic plan",
    ):
        materializer.refresh(
            _request(
                max_batch_rows=1,
                resume_refresh_id=refresh_id,
                operation_recorded_at=T0 + timedelta(minutes=2),
            )
        )
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_receipts").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_scope_heads").fetchone() == (0,)


def test_atomic_finalization_rolls_back_before_head_and_can_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _database()

    def fail(_self: GovernedCurrentMaterializer) -> None:
        raise RuntimeError("injected before head")

    monkeypatch.setattr(GovernedCurrentMaterializer, "_before_head_advance", fail)
    with pytest.raises(RuntimeError, match="injected before head"):
        refresh_latest_governed_state(conn, _request())
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_scope_heads").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_fact_entries").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_receipts").fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_refresh_stage WHERE stage_status='staged'"
    ).fetchone() == (3,)

    def succeed(_self: GovernedCurrentMaterializer) -> None:
        return None

    monkeypatch.setattr(GovernedCurrentMaterializer, "_before_head_advance", succeed)
    completed = refresh_latest_governed_state(conn, _request())
    assert completed.outcome == "changed"
    assert completed.replayed_count == 3
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_stage").fetchone() == (0,)


def test_forward_reprojection_restores_prior_state_without_mutating_history() -> None:
    conn = _database()
    initial = refresh_latest_governed_state(conn, _request())
    initial_head = conn.execute(
        "SELECT state_sha256,fact_root_sha256,document_root_sha256,"
        "narrative_root_sha256 FROM latest_governed_scope_heads "
        "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3'"
    ).fetchone()
    initial_rows = (
        conn.execute(
            "SELECT canonical_metric_cell_id,canonical_value,"
            "current_commitment_sha256 FROM latest_governed_fact_entries "
            "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' ORDER BY canonical_metric_cell_id"
        ).fetchall(),
        conn.execute(
            "SELECT expected_document_key,document_version_id,"
            "current_commitment_sha256 FROM latest_governed_document_entries "
            "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' ORDER BY expected_document_key"
        ).fetchall(),
        conn.execute(
            "SELECT expected_document_key,chunk_key,text,"
            "current_commitment_sha256 FROM latest_governed_narrative_entries "
            "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' ORDER BY expected_document_key,chunk_key"
        ).fetchall(),
    )
    _advance_delta(conn, generation="generation-2", commitment=SHA_D)
    changed = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
    )
    immutable_before = (
        conn.execute(
            "SELECT receipt_id,canonical_receipt_json,receipt_sha256 "
            "FROM latest_governed_refresh_receipts ORDER BY receipt_id"
        ).fetchall(),
        conn.execute(
            "SELECT change_id,canonical_change_json,change_sha256 "
            "FROM latest_governed_refresh_changes ORDER BY change_id"
        ).fetchall(),
    )

    restored = reproject_latest_governed_state(
        conn,
        LatestGovernedReprojectionRequest(
            scope_id="ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            target_receipt_id=initial.head_id or "",
            expected_current_receipt_id=changed.head_id or "",
            operation_recorded_at=T0 + timedelta(hours=1, minutes=2),
        ),
    )

    assert restored.head_id not in {initial.head_id, changed.head_id}
    assert (
        conn.execute(
            "SELECT state_sha256,fact_root_sha256,document_root_sha256,"
            "narrative_root_sha256 FROM latest_governed_scope_heads "
            "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3'"
        ).fetchone()
        == initial_head
    )
    assert (
        conn.execute(
            "SELECT canonical_metric_cell_id,canonical_value,"
            "current_commitment_sha256 FROM latest_governed_fact_entries "
            "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' ORDER BY canonical_metric_cell_id"
        ).fetchall()
        == initial_rows[0]
    )
    assert (
        conn.execute(
            "SELECT expected_document_key,document_version_id,"
            "current_commitment_sha256 FROM latest_governed_document_entries "
            "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' ORDER BY expected_document_key"
        ).fetchall()
        == initial_rows[1]
    )
    assert (
        conn.execute(
            "SELECT expected_document_key,chunk_key,text,"
            "current_commitment_sha256 FROM latest_governed_narrative_entries "
            "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' ORDER BY expected_document_key,chunk_key"
        ).fetchall()
        == initial_rows[2]
    )
    assert (
        conn.execute(
            "SELECT receipt_id,canonical_receipt_json,receipt_sha256 "
            "FROM latest_governed_refresh_receipts "
            "WHERE receipt_id IN (?,?) ORDER BY receipt_id",
            (initial.head_id, changed.head_id),
        ).fetchall()
        == immutable_before[0]
    )
    assert (
        conn.execute(
            "SELECT change_id,canonical_change_json,change_sha256 "
            "FROM latest_governed_refresh_changes "
            "WHERE receipt_id IN (?,?) ORDER BY change_id",
            (initial.head_id, changed.head_id),
        ).fetchall()
        == immutable_before[1]
    )
    assert conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_receipts").fetchone() == (3,)


def test_baseline_audit_is_bucket_compact_and_deltas_remain_detailed() -> None:
    conn = _database()
    initial = refresh_latest_governed_state(conn, _request())
    baseline = conn.execute(
        "SELECT change_count,canonical_change_set_json,canonical_receipt_json "
        "FROM latest_governed_refresh_receipts WHERE receipt_id=?",
        (initial.head_id,),
    ).fetchone()
    assert baseline is not None
    buckets = json.loads(str(baseline[1]))
    receipt_payload = json.loads(str(baseline[2]))
    assert receipt_payload["change_audit"] == {
        "bucket_count": len(buckets),
        "change_count": 3,
        "mode": "baseline_digest_buckets.v1",
    }
    assert 0 < len(buckets) <= 4_096
    assert [item["digest_bucket"] for item in buckets] == sorted(
        item["digest_bucket"] for item in buckets
    )
    assert sum(item["change_count"] for item in buckets) == int(baseline[0]) == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_refresh_changes WHERE receipt_id=?",
        (initial.head_id,),
    ).fetchone() == (0,)

    _advance_delta(conn, generation="generation-2", commitment=SHA_D)
    delta = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
    )
    changes = conn.execute(
        "SELECT canonical_change_json,source_evidence_json "
        "FROM latest_governed_refresh_changes WHERE receipt_id=? "
        "ORDER BY change_ordinal",
        (delta.head_id,),
    ).fetchall()
    assert len(changes) == delta.fact_change_count == 1
    delta_payload = json.loads(
        str(
            conn.execute(
                "SELECT canonical_receipt_json "
                "FROM latest_governed_refresh_receipts WHERE receipt_id=?",
                (delta.head_id,),
            ).fetchone()[0]
        )
    )
    assert delta_payload["change_audit"] == {
        "bucket_count": 0,
        "change_count": 1,
        "mode": "coordinate_changes.v1",
    }
    for change_json, evidence_json in changes:
        change = json.loads(str(change_json))
        evidence = json.loads(str(evidence_json))
        assert "canonical_payload" not in change
        assert len(str(change_json).encode("utf-8")) < 768
        assert {
            "change_kind",
            "coordinate_key",
            "current_commitment_sha256",
            "entity_kind",
            "prior_commitment_sha256",
            "selection_reason",
            "source_evidence_sha256",
        } == set(change)
        assert isinstance(evidence, dict)


def test_forward_reprojection_conflicts_fail_closed() -> None:
    conn = _database()
    initial = refresh_latest_governed_state(conn, _request())
    _advance_delta(conn, generation="generation-2", commitment=SHA_D)
    changed = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
    )
    with pytest.raises(LatestGovernedStateError, match="expected current receipt"):
        reproject_latest_governed_state(
            conn,
            LatestGovernedReprojectionRequest(
                scope_id="ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
                target_receipt_id=initial.head_id or "",
                expected_current_receipt_id=initial.head_id or "",
                operation_recorded_at=T0 + timedelta(hours=1, minutes=2),
            ),
        )
    with pytest.raises(LatestGovernedStateError, match="prior immutable receipt"):
        reproject_latest_governed_state(
            conn,
            LatestGovernedReprojectionRequest(
                scope_id="ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
                target_receipt_id=changed.head_id or "",
                expected_current_receipt_id=changed.head_id or "",
                operation_recorded_at=T0 + timedelta(hours=1, minutes=2),
            ),
        )
    assert conn.execute(
        "SELECT refresh_receipt_id FROM latest_governed_scope_heads"
    ).fetchone() == (changed.head_id,)


def test_concurrent_identical_refreshes_insert_or_load_one_complete_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "concurrent-latest.db"
    source = _database()
    target = sqlite3.connect(database_path)
    source.backup(target)
    source.close()
    target.close()
    barrier = threading.Barrier(2)

    def run() -> tuple[str | None, str]:
        connection = sqlite3.connect(database_path, timeout=10)
        try:
            barrier.wait(timeout=5)
            result = refresh_latest_governed_state(connection, _request())
            return result.head_id, result.terminal_commitment
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(run), pool.submit(run))
        results = tuple(future.result() for future in futures)

    assert results[0] == results[1]
    check = sqlite3.connect(database_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM latest_governed_refresh_runs").fetchone() == (1,)
        assert check.execute(
            "SELECT COUNT(*) FROM latest_governed_refresh_receipts"
        ).fetchone() == (1,)
        assert check.execute("SELECT COUNT(*) FROM latest_governed_scope_heads").fetchone() == (1,)
        assert check.execute("SELECT COUNT(*) FROM latest_governed_refresh_stage").fetchone() == (
            0,
        )
    finally:
        check.close()


def test_concurrent_identical_noops_return_one_immutable_receipt_and_actual_head(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "concurrent-noop.db"
    source = _database()
    initial = refresh_latest_governed_state(source, _request())
    target = sqlite3.connect(database_path)
    source.backup(target)
    source.close()
    target.close()
    barrier = threading.Barrier(2)
    noop_request = _request(operation_recorded_at=T0 + timedelta(minutes=2))

    def run() -> LatestGovernedRefreshResult:
        connection = sqlite3.connect(database_path, timeout=10)
        try:
            barrier.wait(timeout=5)
            return refresh_latest_governed_state(connection, noop_request)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(run), pool.submit(run))
        concurrent = tuple(future.result() for future in futures)
    replay_connection = sqlite3.connect(database_path)
    try:
        sequential = refresh_latest_governed_state(replay_connection, noop_request)
        assert concurrent[0] == concurrent[1] == sequential
        assert sequential.head_id == initial.head_id
        assert replay_connection.execute(
            "SELECT refresh_receipt_id FROM latest_governed_scope_heads"
        ).fetchone() == (initial.head_id,)
        assert replay_connection.execute(
            "SELECT COUNT(*) FROM latest_governed_refresh_receipts"
        ).fetchone() == (2,)
        assert replay_connection.execute(
            "SELECT COUNT(*) FROM latest_governed_refresh_receipts WHERE change_count=0"
        ).fetchone() == (1,)
    finally:
        replay_connection.close()


def test_cutover_and_promotion_must_be_exactly_current() -> None:
    conn = _database()
    conn.execute("DELETE FROM v_population_cutover_current")
    with pytest.raises(LatestGovernedStateError, match="sealed population cutover"):
        refresh_latest_governed_state(conn, _request())

    conn = _database()
    conn.execute(
        "UPDATE v_ask_retrieval_scope_current SET population_receipt_set_sha256=?",
        (SHA_D,),
    )
    with pytest.raises(LatestGovernedStateError, match="exactly bound"):
        refresh_latest_governed_state(conn, _request())


def test_document_checkpoint_reaches_inventory_rollover_with_changed_only_writes() -> None:
    conn = _database()
    _seed_second_inventory_document(conn)
    dry_default = refresh_latest_governed_state(conn, _request(apply=False))
    dry_checkpoint = refresh_latest_governed_state(
        conn,
        _request(apply=False, document_checkpoint=True),
    )
    assert dry_default.refresh_id != dry_checkpoint.refresh_id
    initial = refresh_latest_governed_state(conn, _request())
    retained_before = conn.execute(
        "SELECT refresh_receipt_id,current_commitment_sha256 "
        "FROM latest_governed_document_entries "
        "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' AND expected_document_key='10-k:2025'"
    ).fetchone()
    assert retained_before is not None
    _rollover_source_inventory(conn, retain_second=True)

    with pytest.raises(
        LatestGovernedStateError,
        match="explicit document checkpoint required",
    ):
        refresh_latest_governed_state(
            conn,
            _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
        )
    checkpoint = refresh_latest_governed_state(
        conn,
        _request(
            document_checkpoint=True,
            operation_recorded_at=T0 + timedelta(hours=1, minutes=1),
        ),
    )
    assert checkpoint.head_id != initial.head_id
    assert (
        checkpoint.fact_change_count,
        checkpoint.document_change_count,
        checkpoint.narrative_change_count,
        checkpoint.current_write_count,
    ) == (0, 1, 0, 1)
    assert (
        conn.execute(
            "SELECT refresh_receipt_id,current_commitment_sha256 "
            "FROM latest_governed_document_entries "
            "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' AND expected_document_key='10-k:2025'"
        ).fetchone()
        == retained_before
    )


def test_document_checkpoint_deletes_absent_coordinates_and_binds_resume_policy() -> None:
    conn = _database()
    _seed_second_inventory_document(conn)
    refresh_latest_governed_state(conn, _request())
    _rollover_source_inventory(conn, retain_second=False)
    staged = refresh_latest_governed_state(
        conn,
        _request(
            document_checkpoint=True,
            max_batch_rows=1,
            interrupt_after_batches=1,
            operation_recorded_at=T0 + timedelta(hours=1, minutes=1),
        ),
    )
    assert staged.outcome == "staged"
    with pytest.raises(
        LatestGovernedStateError,
        match="resume refresh does not match",
    ):
        refresh_latest_governed_state(
            conn,
            _request(
                max_batch_rows=1,
                resume_refresh_id=staged.refresh_id,
                operation_recorded_at=T0 + timedelta(hours=1, minutes=2),
            ),
        )
    completed = refresh_latest_governed_state(
        conn,
        _request(
            document_checkpoint=True,
            max_batch_rows=1,
            resume_refresh_id=staged.refresh_id,
            operation_recorded_at=T0 + timedelta(hours=1, minutes=2),
        ),
    )
    assert (
        completed.document_change_count,
        completed.narrative_change_count,
        completed.current_write_count,
    ) == (2, 2, 4)
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_document_entries "
        "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' AND expected_document_key='10-k:2025'"
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM latest_governed_narrative_entries "
        "WHERE scope_key='ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3' AND expected_document_key='10-k:2025'"
    ).fetchone() == (0,)


def test_document_checkpoint_rejects_ambiguous_active_current_inventory() -> None:
    conn = _database()
    initial = refresh_latest_governed_state(conn, _request())
    _rollover_source_inventory(
        conn,
        retain_second=False,
        duplicate_current_q2=True,
    )
    with pytest.raises(
        LatestGovernedStateError,
        match="missing or ambiguous",
    ):
        refresh_latest_governed_state(
            conn,
            _request(
                document_checkpoint=True,
                operation_recorded_at=T0 + timedelta(hours=1, minutes=1),
            ),
        )
    assert conn.execute(
        "SELECT refresh_receipt_id FROM latest_governed_scope_heads"
    ).fetchone() == (initial.head_id,)


def test_latest_reads_are_history_independent_and_reject_implicit_history() -> None:
    conn = _database()
    refresh_latest_governed_state(conn, _request())
    before = search_latest_governed_facts(
        conn,
        "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
        "revenue",
        5,
    )
    for ordinal in range(100):
        generation = f"historical-{ordinal}"
        conn.execute(
            "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
            (generation, "checkpoint", None),
        )
        conn.execute(
            "INSERT INTO canonical_fact_projection_entries "
            "(generation_id,entry_ordinal,change_kind,canonical_metric_cell_id,"
            "canonical_metric_name,entry_sha256) VALUES (?,?,?,?,?,?)",
            (generation, 0, "upsert", f"old-{ordinal}", "revenue", SHA_A),
        )
    conn.commit()
    assert (
        search_latest_governed_facts(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "revenue",
            5,
        )
        == before
    )
    with pytest.raises(LatestGovernedStateError, match="explicit historical"):
        search_latest_governed_facts(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "revenue",
            5,
            include_history=True,
        )
    with pytest.raises(LatestGovernedStateError, match="explicit historical"):
        search_latest_governed_narrative(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "demand",
            5,
            include_history=True,
        )

    fact_plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT canonical_metric_cell_id "
            "FROM latest_governed_fact_entries WHERE scope_key=? "
            "AND canonical_metric_name=? ORDER BY period_end DESC LIMIT 5",
            (
                "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
                "revenue",
            ),
        )
    )
    narrative_plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT entry.chunk_key "
            "FROM latest_governed_narrative_fts "
            "JOIN latest_governed_narrative_entries entry "
            "ON entry.rowid=latest_governed_narrative_fts.rowid "
            "WHERE latest_governed_narrative_fts MATCH ? "
            "AND entry.scope_key=? LIMIT 5",
            (
                "demand",
                "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            ),
        )
    )
    combined = (fact_plan + " " + narrative_plan).lower()
    assert "canonical_fact_projection_entries" not in combined
    assert "search_chunks" not in combined
    assert "recursive" not in combined


def test_fact_search_is_bounded_and_uses_the_public_indexed_sql() -> None:
    conn = _database()
    refresh_latest_governed_state(conn, _request())
    statement = build_latest_governed_fact_search_query(
        "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
        "Revenue revenue",
        5,
    )
    assert statement is not None
    sql, params = statement
    assert (
        search_latest_governed_facts(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "Revenue revenue",
            5,
        )[0].canonical_value
        == "100"
    )
    plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
    assert "ix_latest_governed_fact_search" in plan
    assert "USE TEMP B-TREE" not in plan
    assert "instr(" not in sql.casefold()
    with pytest.raises(ValueError, match="32 token"):
        search_latest_governed_facts(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            " ".join(f"metric-{ordinal}" for ordinal in range(33)),
            5,
        )
    with pytest.raises(ValueError, match="128 character"):
        search_latest_governed_facts(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "x" * 129,
            5,
        )
    with pytest.raises(ValueError, match="4096 character"):
        search_latest_governed_facts(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "x " * 2_049,
            5,
        )


def test_narrative_search_rejects_unbounded_queries() -> None:
    conn = _database()
    refresh_latest_governed_state(conn, _request())
    with pytest.raises(ValueError, match="32 token"):
        search_latest_governed_narrative(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            " ".join(f"word-{ordinal}" for ordinal in range(33)),
            5,
        )
    with pytest.raises(ValueError, match="128 character"):
        search_latest_governed_narrative(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "x" * 129,
            5,
        )
    with pytest.raises(ValueError, match="4096 character"):
        search_latest_governed_narrative(
            conn,
            "ask-scope:v1:3476a10310c9cbbf527a8277bff9db171809c30a0111949fd0b2619e3398fad3",
            "x " * 2_049,
            5,
        )


def test_changed_narrative_batch_stays_below_physical_write_ratchet() -> None:
    conn = _database()
    _seed_large_retained_document_corpus(conn)
    refresh_latest_governed_state(conn, _request())
    _promote_one_document_delta(conn)
    total_changes_before = conn.total_changes
    result = refresh_latest_governed_state(
        conn,
        _request(operation_recorded_at=T0 + timedelta(hours=1, minutes=1)),
    )
    logical_writes = (
        result.fact_change_count
        + result.document_change_count
        + result.narrative_change_count
        + result.receipt_write_count
    )
    physical_writes = conn.total_changes - total_changes_before
    assert (result.document_change_count, result.narrative_change_count) == (1, 8)
    assert physical_writes / logical_writes <= 8
