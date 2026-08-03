from __future__ import annotations

import json
import os
import sqlite3
import threading
import tracemalloc
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import pytest

import provenance.latest_state_benchmark as benchmark_module
from provenance.latest_governed_state import LatestGovernedStateError
from provenance.latest_state_benchmark import (
    AdapterRefresh,
    BudgetResult,
    FixtureCounts,
    HistoryIndependenceEvidence,
    LatestStateBenchmarkBudgets,
    LatestStateBenchmarkConfig,
    LatestStateSqliteAdapter,
    QueryPlanProof,
    ReadMeasurement,
    RefreshMeasurement,
    RefusedBenchmarkPathError,
    StorageEvidence,
    benchmark_scope_id,
    evaluate_benchmark_budgets,
    production_benchmark_budgets,
    production_benchmark_config,
    run_latest_state_benchmark,
    verify_production_benchmark_report,
    verify_report_sha256,
    write_report_atomic,
)

ROOT = Path(__file__).resolve().parents[1]
HEX = "a" * 64


class _DeterministicAdapter:
    """Contract fixture; production API integration is tested by its own suite."""

    def create_fixture(
        self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
    ) -> FixtureCounts:
        conn.execute("CREATE TABLE benchmark_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        conn.execute(
            "CREATE TABLE benchmark_scope_meta ("
            "scope_key TEXT PRIMARY KEY, reporting_entity_id TEXT, "
            "pending_facts INTEGER, pending_documents INTEGER, "
            "pending_narrative INTEGER, revision INTEGER)"
        )
        conn.execute(
            "CREATE TABLE v_ask_retrieval_scope_current ("
            "scope_key TEXT PRIMARY KEY, issuer_id TEXT, reporting_entity_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE canonical_metric_cells ("
            "canonical_metric_cell_id TEXT PRIMARY KEY, reporting_entity_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE canonical_fact_projection_entries ("
            "generation_id TEXT, canonical_metric_cell_id TEXT, "
            "value TEXT)"
        )
        conn.execute(
            "CREATE INDEX ix_canonical_fact_projection_entry_keyset "
            "ON canonical_fact_projection_entries("
            "generation_id,canonical_metric_cell_id)"
        )
        conn.execute(
            "CREATE TABLE latest_governed_fact_entries ("
            "scope_key TEXT, canonical_metric_cell_id TEXT, refresh_receipt_id TEXT, "
            "current_commitment_sha256 TEXT, "
            "PRIMARY KEY(scope_key,canonical_metric_cell_id))"
        )
        conn.execute("CREATE TABLE latest_governed_document_entries (scope_key TEXT, value TEXT)")
        conn.execute("CREATE TABLE latest_governed_narrative_entries (scope_key TEXT, value TEXT)")
        conn.execute("CREATE TABLE search_corpus_document_memberships (value TEXT)")
        conn.execute("CREATE TABLE search_chunks (value TEXT)")
        conn.execute(
            "CREATE TABLE latest_governed_scope_heads ("
            "scope_key TEXT PRIMARY KEY, refresh_receipt_id TEXT, state_sha256 TEXT)"
        )
        conn.execute(
            "CREATE TABLE latest_governed_refresh_stage "
            "(refresh_run_id TEXT, stage_ordinal INTEGER, entity_kind TEXT, "
            "change_kind TEXT, coordinate_key TEXT, prior_commitment_sha256 TEXT, "
            "current_commitment_sha256 TEXT, canonical_payload_json TEXT, "
            "payload_sha256 TEXT)"
        )
        conn.execute("CREATE TABLE source_fact_publication_stream (publication_sequence INTEGER)")
        conn.execute(
            "CREATE TABLE latest_governed_refresh_receipts ("
            "receipt_id TEXT, refresh_run_id TEXT, change_count INTEGER, "
            "canonical_change_set_json TEXT, canonical_receipt_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE latest_governed_refresh_changes "
            "(receipt_id TEXT, entity_kind TEXT, coordinate_key TEXT)"
        )
        conn.executemany(
            "INSERT INTO v_ask_retrieval_scope_current VALUES (?,?,?)",
            (
                (
                    benchmark_scope_id(scope_index),
                    f"issuer:{scope_index:04d}",
                    f"reporting:{scope_index:04d}",
                )
                for scope_index in range(config.scope_count)
            ),
        )
        conn.executemany(
            "INSERT INTO benchmark_scope_meta VALUES (?,?,?,?,?,?)",
            (
                (
                    benchmark_scope_id(scope_index),
                    f"reporting:{scope_index:04d}",
                    config.cell_count // config.scope_count
                    + (1 if scope_index < config.cell_count % config.scope_count else 0),
                    config.document_count if scope_index == 0 else 0,
                    config.chunk_count if scope_index == 0 else 0,
                    0,
                )
                for scope_index in range(config.scope_count)
            ),
        )
        conn.executemany(
            "INSERT INTO canonical_metric_cells VALUES (?,?)",
            (
                (
                    f"cell-{index:09d}",
                    f"reporting:{index % config.scope_count:04d}",
                )
                for index in range(config.cell_count)
            ),
        )
        conn.executemany(
            "INSERT INTO canonical_fact_projection_entries VALUES (?,?,?)",
            (
                (
                    "generation-0",
                    f"cell-{index:09d}",
                    f"canonical_fact_projection_entries:{index}",
                )
                for index in range(config.cell_count)
            ),
        )
        for table, count in (
            ("search_corpus_document_memberships", config.document_count),
            ("search_chunks", config.chunk_count),
        ):
            conn.executemany(
                f"INSERT INTO {table} VALUES (?)",  # nosec B608
                ((f"{table}:{index}",) for index in range(count)),
            )
        values = {
            "pending_publications": config.publication_count,
            "refresh_sequence": 0,
        }
        conn.executemany("INSERT INTO benchmark_meta VALUES (?,?)", values.items())
        conn.executemany(
            "INSERT INTO source_fact_publication_stream VALUES (?)",
            ((index + 1,) for index in range(config.publication_count)),
        )
        conn.commit()
        return FixtureCounts(
            publications=config.publication_count,
            cells=config.cell_count,
            documents=config.document_count,
            chunks=config.chunk_count,
            scopes=config.scope_count,
        )

    def create_reporting_entity_index(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE INDEX ix_canonical_metric_cells_reporting_entity "
            "ON canonical_metric_cells("
            "reporting_entity_id,canonical_metric_cell_id)"
        )
        conn.commit()

    def clone_fixture(
        self,
        source: sqlite3.Connection,
        target: sqlite3.Connection,
        *,
        history_multiplier: int,
    ) -> None:
        source.backup(target)
        if history_multiplier > 1:
            for table in ("search_corpus_document_memberships", "search_chunks"):
                rows = target.execute(f"SELECT value FROM {table}").fetchall()  # nosec B608
                for multiplier in range(1, history_multiplier):
                    target.executemany(
                        f"INSERT INTO {table} VALUES (?)",  # nosec B608
                        ((f"history-{multiplier}:{row[0]}",) for row in rows),
                    )
            rows = target.execute(
                "SELECT canonical_metric_cell_id,value FROM canonical_fact_projection_entries"
            ).fetchall()
            for multiplier in range(1, history_multiplier):
                target.executemany(
                    "INSERT INTO canonical_fact_projection_entries VALUES (?,?,?)",
                    (
                        (
                            f"history-{multiplier}",
                            f"history-{multiplier}:{row[0]}",
                            f"history-{multiplier}:{row[1]}",
                        )
                        for row in rows
                    ),
                )
            target.commit()

    def apply_small_delta(
        self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
    ) -> None:
        conn.execute(
            "UPDATE benchmark_meta SET value=? WHERE key='pending_publications'",
            (config.delta_publication_count,),
        )
        conn.execute(
            "UPDATE benchmark_scope_meta SET pending_facts=?,pending_documents=?,"
            "pending_narrative=? WHERE scope_key=?",
            (
                config.delta_cell_count,
                config.delta_document_count,
                config.delta_chunk_count,
                benchmark_scope_id(0),
            ),
        )
        maximum = int(
            conn.execute(
                "SELECT COALESCE(MAX(publication_sequence),0) FROM source_fact_publication_stream"
            ).fetchone()[0]
        )
        conn.executemany(
            "INSERT INTO source_fact_publication_stream VALUES (?)",
            ((maximum + index + 1,) for index in range(config.delta_publication_count)),
        )
        conn.commit()

    def refresh(
        self,
        conn: sqlite3.Connection,
        *,
        scope_id: str,
        config: LatestStateBenchmarkConfig,
        operation_recorded_at: datetime,
        resume_refresh_id: str | None = None,
        interrupt_after_batches: int | None = None,
    ) -> AdapterRefresh:
        del operation_recorded_at
        meta = {
            str(row[0]): int(row[1]) for row in conn.execute("SELECT key,value FROM benchmark_meta")
        }
        scope_meta = conn.execute(
            "SELECT reporting_entity_id,pending_facts,pending_documents,"
            "pending_narrative,revision FROM benchmark_scope_meta WHERE scope_key=?",
            (scope_id,),
        ).fetchone()
        if scope_meta is None:
            raise AssertionError("deterministic benchmark scope is missing")
        scope_index = int(str(scope_meta[0]).rsplit(":", 1)[1])
        sequence = meta["refresh_sequence"] + (0 if resume_refresh_id else 1)
        refresh_id = resume_refresh_id or f"refresh-{sequence}"
        facts = int(scope_meta[1])
        documents = int(scope_meta[2])
        narrative = int(scope_meta[3])
        changes = facts + documents + narrative
        stage_rows: list[tuple[object, ...]] = []
        ordinal = 0
        for entity_kind, count in (
            ("fact", facts),
            ("document", documents),
            ("narrative", narrative),
        ):
            for entity_index in range(count):
                coordinate = (
                    f"cell-{entity_index * config.scope_count + scope_index:09d}"
                    if entity_kind == "fact"
                    else f"{entity_kind}:{entity_index}"
                )
                current = f"{ordinal + 1:064x}"
                payload_json = json.dumps(
                    {"coordinate_key": coordinate, "entity_kind": entity_kind},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                stage_rows.append(
                    (
                        refresh_id,
                        ordinal,
                        entity_kind,
                        "upsert",
                        coordinate,
                        None,
                        current,
                        payload_json,
                        current,
                    )
                )
                ordinal += 1
        if interrupt_after_batches is not None:
            staged = min(changes, interrupt_after_batches * config.max_batch_rows)
            conn.executemany(
                "INSERT INTO latest_governed_refresh_stage VALUES (?,?,?,?,?,?,?,?,?)",
                stage_rows[:staged],
            )
            conn.commit()
            return AdapterRefresh(
                outcome="staged",
                refresh_id=refresh_id,
                terminal_commitment=HEX,
                source_events=meta["pending_publications"],
                fact_changes=facts,
                document_changes=documents,
                narrative_changes=narrative,
                source_reads=changes,
                current_reads=changes,
                current_writes=0,
                receipt_writes=0,
                created_count=staged,
                replayed_count=0,
                resume_cursor=str(staged),
            )
        current_revision = int(scope_meta[4])
        prestaged = int(
            conn.execute(
                "SELECT COUNT(*) FROM latest_governed_refresh_stage WHERE refresh_run_id=?",
                (refresh_id,),
            ).fetchone()[0]
        )
        if resume_refresh_id is not None and prestaged < changes:
            conn.executemany(
                "INSERT INTO latest_governed_refresh_stage VALUES (?,?,?,?,?,?,?,?,?)",
                stage_rows[prestaged:],
            )
        if changes:
            receipt_id = "receipt-" + refresh_id
            conn.executemany(
                "INSERT OR REPLACE INTO latest_governed_fact_entries VALUES (?,?,?,?)",
                (
                    (
                        scope_id,
                        str(stage_rows[index][4]),
                        receipt_id,
                        str(stage_rows[index][6]),
                    )
                    for index in range(facts)
                ),
            )
            conn.executemany(
                "INSERT INTO latest_governed_document_entries VALUES (?,?)",
                ((scope_id, f"{refresh_id}:{index}") for index in range(documents)),
            )
            conn.executemany(
                "INSERT INTO latest_governed_narrative_entries VALUES (?,?)",
                ((scope_id, f"{refresh_id}:{index}") for index in range(narrative)),
            )
            if current_revision > 0:
                conn.executemany(
                    "INSERT INTO latest_governed_refresh_changes VALUES (?,?,?)",
                    ((receipt_id, str(row[2]), str(row[4])) for row in stage_rows),
                )
            current_revision += 1
        receipt_id = "receipt-" + refresh_id
        is_baseline = changes > 0 and current_revision == 1
        if is_baseline:
            bucket_count = min(changes, 4)
            change_set: list[object] = [
                {
                    "digest_bucket": bucket,
                    "change_count": (
                        changes // bucket_count + (1 if bucket < changes % bucket_count else 0)
                    ),
                    "commitment_sha256": f"{bucket + 1:064x}",
                }
                for bucket in range(bucket_count)
            ]
            audit_mode = "baseline_digest_buckets.v1"
        else:
            change_set = [f"{index + 1:064x}" for index in range(changes)]
            bucket_count = 0
            audit_mode = "coordinate_changes.v1"
        conn.execute(
            "INSERT INTO latest_governed_refresh_receipts VALUES (?,?,?,?,?)",
            (
                receipt_id,
                refresh_id,
                changes,
                json.dumps(change_set, separators=(",", ":"), sort_keys=True),
                json.dumps(
                    {
                        "change_audit": {
                            "bucket_count": bucket_count,
                            "change_count": changes,
                            "mode": audit_mode,
                        }
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
        conn.execute(
            "DELETE FROM latest_governed_refresh_stage WHERE refresh_run_id=?",
            (refresh_id,),
        )
        if meta["pending_publications"] > 0:
            conn.execute("UPDATE benchmark_meta SET value=0 WHERE key='pending_publications'")
        conn.execute(
            "UPDATE benchmark_meta SET value=? WHERE key='refresh_sequence'",
            (sequence,),
        )
        if changes:
            conn.execute(
                "UPDATE benchmark_scope_meta SET pending_facts=0,"
                "pending_documents=0,pending_narrative=0,revision=? "
                "WHERE scope_key=?",
                (current_revision, scope_id),
            )
        commitment = f"{scope_index + current_revision * config.scope_count:064x}"
        conn.execute(
            "INSERT OR REPLACE INTO latest_governed_scope_heads VALUES (?,?,?)",
            (scope_id, receipt_id, commitment),
        )
        conn.commit()
        return AdapterRefresh(
            outcome="changed" if changes else "no_op",
            refresh_id=refresh_id,
            terminal_commitment=commitment,
            source_events=meta["pending_publications"],
            fact_changes=facts,
            document_changes=documents,
            narrative_changes=narrative,
            source_reads=changes,
            current_reads=changes if changes else 1,
            current_writes=changes,
            receipt_writes=1,
            created_count=changes + 1,
            replayed_count=prestaged,
        )

    def search_facts(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> list[object]:
        del conn, scope_id, query
        return [object()] * min(3, limit)

    def search_narrative(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> list[object]:
        del conn, scope_id, query
        return [object()] * min(4, limit)

    def fact_query_plan(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> QueryPlanProof:
        del conn, scope_id, query, limit
        return QueryPlanProof(
            sql=(
                "SELECT * FROM latest_governed_fact_entries WHERE scope_key=? "
                "AND canonical_metric_name IN (?) LIMIT ?"
            ),
            params=(benchmark_scope_id(0), "revenue", 5),
            details=(
                "SEARCH latest_governed_fact_entries USING INDEX "
                "ix_latest_governed_fact_search "
                "(scope_key=? AND canonical_metric_name=?)",
            ),
        )

    def narrative_query_plan(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> QueryPlanProof:
        del conn, scope_id, query, limit
        return QueryPlanProof(
            sql="SELECT * FROM latest_governed_narrative_fts WHERE text MATCH ?",
            params=("demand",),
            details=("SCAN latest_governed_narrative_fts VIRTUAL TABLE INDEX",),
        )


def _config() -> LatestStateBenchmarkConfig:
    return LatestStateBenchmarkConfig(
        publication_count=8,
        cell_count=40,
        document_count=8,
        chunk_count=32,
        scope_count=2,
        delta_publication_count=1,
        delta_cell_count=2,
        delta_document_count=1,
        delta_chunk_count=3,
        max_batch_rows=2,
        read_samples=3,
        read_limit=5,
        interrupt_after_batches=1,
    )


def _run(tmp_path: Path):
    return run_latest_state_benchmark(
        config=_config(),
        budgets=LatestStateBenchmarkBudgets(),
        database_path=tmp_path / "latest-state.db",
        adapter=_DeterministicAdapter(),
    )


def test_exact_work_read_and_resume_ratchets_pass(tmp_path: Path) -> None:
    report = _run(tmp_path)

    assert report.no_op.work.receipt_writes == 1
    assert report.no_op.work.current_writes == 0
    assert report.no_op.work.sqlite_vm_step_proxy > 0
    assert report.small_delta.work.independent_source_publications == 1
    assert report.small_delta.work.sqlite_vm_step_proxy > 0
    assert report.small_delta.work.fact_changes == 2
    assert report.small_delta.work.document_changes == 1
    assert report.small_delta.work.narrative_changes == 3
    assert report.fact_read.maximum_rows_fetched <= report.fact_read.limit
    assert report.narrative_read.maximum_rows_fetched <= report.narrative_read.limit
    assert report.resume.equivalent
    assert report.resume.ordered_stage_identity_payloads_equal
    assert (
        report.resume.staged_identity_payload_sha256
        == report.resume.finalized_identity_payload_prefix_sha256
    )
    assert report.resume.staged_rows_rewritten == 0
    assert report.history_independence.equivalent
    assert report.history_independence.sqlite_vm_step_ratio <= 1.10
    assert report.change_audit.baseline_mode == "baseline_digest_buckets.v1"
    assert report.change_audit.baseline_logical_changes == 60
    assert 0 < report.change_audit.baseline_digest_bucket_commitments <= 4_096
    assert report.change_audit.baseline_digest_buckets_non_empty
    assert report.change_audit.baseline_digest_buckets_ordered
    assert report.change_audit.baseline_detailed_change_rows == 0
    assert report.change_audit.delta_mode == "coordinate_changes.v1"
    assert report.change_audit.delta_logical_changes == 6
    assert report.change_audit.delta_detailed_change_rows == 6
    assert report.rows.refresh_changes == 6
    assert report.cross_scope.authoritative_scope_count == 2
    assert report.cross_scope.canonical_metric_cell_scope_index_columns == (
        "reporting_entity_id",
        "canonical_metric_cell_id",
    )
    assert report.cross_scope.canonical_projection_keyset_index_columns == (
        "generation_id",
        "canonical_metric_cell_id",
    )
    assert report.cross_scope.source_scope_query_uses_projection_keyset_index
    assert report.cross_scope.source_scope_query_avoids_projection_scan
    assert any(
        "ix_canonical_fact_projection_entry_keyset" in detail
        for detail in report.cross_scope.source_scope_query_plan
    )
    assert report.cross_scope.authoritative_issuer_count == 2
    assert report.cross_scope.authoritative_reporting_entity_count == 2
    assert report.cross_scope.source_fact_rows == 40
    assert report.cross_scope.materialized_fact_rows_before_delta == 40
    assert report.cross_scope.materialized_fact_rows_after_delta == 40
    assert report.cross_scope.minimum_source_facts_per_scope == 20
    assert report.cross_scope.maximum_source_facts_per_scope == 20
    assert report.cross_scope.cross_scope_fact_mismatches_before_delta == 0
    assert report.cross_scope.cross_scope_fact_mismatches_after_delta == 0
    assert report.cross_scope.non_target_heads_unchanged
    assert report.cross_scope.non_target_current_rows_bound_to_heads
    assert report.fixture_prep_wall_seconds > 0
    assert report.hot_path_wall_seconds > 0
    assert report.command_wall_seconds >= (
        report.fixture_prep_wall_seconds + report.hot_path_wall_seconds
    )
    assert report.python_memory_measurement_scope == "post_fixture_hot_path"
    assert report.cold_baseline_process_memory.sample_count >= 2
    assert report.cold_baseline_process_memory.peak_bytes >= max(
        report.cold_baseline_process_memory.before_bytes,
        report.cold_baseline_process_memory.after_bytes,
    )
    assert report.storage.total_allocated_pages == (
        report.storage.source_fixture_allocated_pages
        + report.storage.latest_state_incremental_allocated_pages
    )
    assert report.storage.reporting_entity_index_allocated_pages > 0
    assert report.storage.latest_state_incremental_allocated_pages == (
        report.storage.reporting_entity_index_allocated_pages
        + report.storage.latest_state_materialization_allocated_pages
    )
    assert report.storage.total_database_bytes == (
        report.storage.source_fixture_database_bytes
        + report.storage.latest_state_incremental_database_bytes
    )
    assert report.storage.latest_state_incremental_database_bytes == (
        report.storage.reporting_entity_index_database_bytes
        + report.storage.latest_state_materialization_database_bytes
    )
    assert {result.name for result in report.budget_results} >= {
        "hot_path_seconds",
        "hot_path_peak_python_memory_bytes",
        "latest_state_incremental_allocated_pages",
    }
    assert all(result.passed for result in report.ratchets)
    assert {
        "initial_checkpoint_bounded_change_audit",
        "multi_scope_exact_initial_materialization",
        "projection_keyset_index_bounds_cross_scope_evidence",
        "reporting_entity_index_storage_is_incremental",
        "small_delta_cross_scope_isolation",
        "small_delta_exact_change_audit",
    } <= {result.name for result in report.ratchets}
    implementation_files = {
        item.project_relative_path: item.sha256 for item in report.implementation_provenance.files
    }
    assert set(implementation_files) == {
        "alembic/versions/0261_latest_governed_state.py",
        "alembic/versions/0263_ask_scope_identity.py",
        "execution/benchmark_latest_state.py",
        "src/scope_identity.py",
        "src/provenance/scope_identity.py",
        "src/provenance/latest_governed_state.py",
        "src/provenance/latest_state_benchmark.py",
    }
    for relative_path, file_sha256 in implementation_files.items():
        assert file_sha256 == sha256((ROOT / relative_path).read_bytes()).hexdigest()
    assert verify_report_sha256(report)
    assert not verify_production_benchmark_report(report)
    tampered_file = report.implementation_provenance.files[0].model_copy(
        update={"sha256": "0" * 64}
    )
    tampered_provenance = report.implementation_provenance.model_copy(
        update={
            "files": (
                tampered_file,
                *report.implementation_provenance.files[1:],
            )
        }
    )
    assert not verify_report_sha256(
        report.model_copy(update={"implementation_provenance": tampered_provenance})
    )


def test_production_admission_budget_boundaries_use_fixed_measurements() -> None:
    """Admission is a deterministic <= decision, not a machine-speed test.

    Minimal typed models carry only the measurements this decision reads. The
    live production-scale measurement remains in execution/benchmark_latest_state.py,
    outside the CI test suite.
    """
    budgets = production_benchmark_budgets()

    def results_with(
        name: str | None = None, actual: float | None = None
    ) -> tuple[BudgetResult, ...]:
        values = {
            "hot_path_seconds": float(budgets.max_hot_path_seconds),
            "hot_path_peak_python_memory_bytes": float(budgets.max_peak_python_memory_bytes),
            "latest_state_incremental_allocated_pages": float(budgets.max_allocated_sqlite_pages),
            "no_op_milliseconds": budgets.max_noop_milliseconds,
            "small_delta_milliseconds": budgets.max_small_delta_milliseconds,
            "fact_read_p95_milliseconds": budgets.max_fact_read_p95_milliseconds,
            "narrative_read_p95_milliseconds": budgets.max_narrative_read_p95_milliseconds,
            "history_latency_ratio": budgets.max_history_latency_ratio,
        }
        if name is not None and actual is not None:
            values[name] = actual
        return evaluate_benchmark_budgets(
            budgets,
            hot_path_wall_seconds=values["hot_path_seconds"],
            peak_memory=int(values["hot_path_peak_python_memory_bytes"]),
            no_op=RefreshMeasurement.model_construct(
                wall_milliseconds=values["no_op_milliseconds"]
            ),
            delta=RefreshMeasurement.model_construct(
                wall_milliseconds=values["small_delta_milliseconds"]
            ),
            storage=StorageEvidence.model_construct(
                latest_state_incremental_allocated_pages=int(
                    values["latest_state_incremental_allocated_pages"]
                )
            ),
            fact_read=ReadMeasurement.model_construct(
                p95_milliseconds=values["fact_read_p95_milliseconds"]
            ),
            narrative_read=ReadMeasurement.model_construct(
                p95_milliseconds=values["narrative_read_p95_milliseconds"]
            ),
            history=HistoryIndependenceEvidence.model_construct(
                latency_ratio=values["history_latency_ratio"]
            ),
        )

    at_boundary = results_with()
    assert all(result.passed for result in at_boundary)

    for boundary in at_boundary:
        increment = 1.0 if boundary.maximum >= 1.0 else 0.01
        above = results_with(boundary.name, boundary.maximum + increment)
        assert [result.name for result in above if not result.passed] == [boundary.name]


def test_report_is_schema_validated_canonical_and_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run(tmp_path / "run")
    output = tmp_path / "report.json"
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def record_replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", record_replace)
    write_report_atomic(report, output)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["report_version"] == "latest_state_benchmark.v1"
    assert payload["fixture"] == {
        "publications": 8,
        "cells": 40,
        "documents": 8,
        "chunks": 32,
        "scopes": 2,
    }
    assert payload["report_sha256"] == report.report_sha256
    assert payload["python_memory_measurement_scope"] == "post_fixture_hot_path"
    assert payload["cold_baseline_process_memory"]["sample_count"] >= 2
    assert replacements[-1][1] == output
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))


def test_existing_and_live_database_paths_are_refused_before_open(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"keep")
    with pytest.raises(RefusedBenchmarkPathError, match="must not already exist"):
        run_latest_state_benchmark(
            config=_config(),
            budgets=LatestStateBenchmarkBudgets(),
            database_path=existing,
            adapter=_DeterministicAdapter(),
        )
    assert existing.read_bytes() == b"keep"

    live = ROOT / "data" / "portfolio.db"
    before = live.stat() if live.exists() else None
    with pytest.raises(RefusedBenchmarkPathError, match="live portfolio database"):
        run_latest_state_benchmark(
            config=_config(),
            budgets=LatestStateBenchmarkBudgets(),
            database_path=live,
            adapter=_DeterministicAdapter(),
        )
    if before is not None:
        after = live.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_config_requires_a_real_resume_boundary() -> None:
    with pytest.raises(ValueError, match="more changes than interrupted batches"):
        LatestStateBenchmarkConfig(
            publication_count=1,
            cell_count=1,
            document_count=1,
            chunk_count=1,
            scope_count=1,
            delta_publication_count=1,
            delta_cell_count=1,
            delta_document_count=1,
            delta_chunk_count=1,
            max_batch_rows=3,
            interrupt_after_batches=3,
        )


def test_query_plan_ratchet_rejects_historical_tables(tmp_path: Path) -> None:
    class HistoricalPlanAdapter(_DeterministicAdapter):
        def fact_query_plan(
            self,
            conn: sqlite3.Connection,
            *,
            scope_id: str,
            query: str,
            limit: int,
        ) -> QueryPlanProof:
            del conn, scope_id, query, limit
            return QueryPlanProof(
                sql="SELECT * FROM canonical_fact_projection_entries",
                params=(),
                details=("SCAN canonical_fact_projection_entries",),
            )

    report = run_latest_state_benchmark(
        config=_config(),
        budgets=LatestStateBenchmarkBudgets(),
        database_path=tmp_path / "historical-plan.db",
        adapter=HistoricalPlanAdapter(),
    )
    ratchets = {item.name: item for item in report.ratchets}
    assert not ratchets["default_fact_read_current_only"].passed
    assert not report.overall_pass


def test_query_plan_ratchet_rejects_full_current_scope_scan(tmp_path: Path) -> None:
    class FullCurrentScopeScanAdapter(_DeterministicAdapter):
        def fact_query_plan(
            self,
            conn: sqlite3.Connection,
            *,
            scope_id: str,
            query: str,
            limit: int,
        ) -> QueryPlanProof:
            del conn, scope_id, query, limit
            return QueryPlanProof(
                sql=(
                    "SELECT * FROM latest_governed_fact_entries "
                    "WHERE scope_key=? AND instr(canonical_search_text,?)>0"
                ),
                params=(benchmark_scope_id(0), "revenue"),
                details=(
                    "SEARCH latest_governed_fact_entries USING INDEX "
                    "sqlite_autoindex_latest_governed_fact_entries_1 (scope_key=?)",
                ),
            )

    report = run_latest_state_benchmark(
        config=_config(),
        budgets=LatestStateBenchmarkBudgets(),
        database_path=tmp_path / "full-current-scope.db",
        adapter=FullCurrentScopeScanAdapter(),
    )

    ratchets = {item.name: item for item in report.ratchets}
    assert not report.fact_read.avoids_full_current_scope_scan
    assert not ratchets["default_fact_read_current_only"].passed


def test_query_plan_ratchet_rejects_temporary_ordering_btree(
    tmp_path: Path,
) -> None:
    class TemporarySortAdapter(_DeterministicAdapter):
        def fact_query_plan(
            self,
            conn: sqlite3.Connection,
            *,
            scope_id: str,
            query: str,
            limit: int,
        ) -> QueryPlanProof:
            proof = super().fact_query_plan(conn, scope_id=scope_id, query=query, limit=limit)
            return QueryPlanProof(
                sql=proof.sql,
                params=proof.params,
                details=(*proof.details, "USE TEMP B-TREE FOR ORDER BY"),
            )

    report = run_latest_state_benchmark(
        config=_config(),
        budgets=LatestStateBenchmarkBudgets(),
        database_path=tmp_path / "temporary-sort.db",
        adapter=TemporarySortAdapter(),
    )

    ratchets = {item.name: item for item in report.ratchets}
    assert not report.fact_read.avoids_temporary_sort
    assert not ratchets["default_fact_read_current_only"].passed


def test_independent_publication_ratchet_rejects_counter_only_delta(
    tmp_path: Path,
) -> None:
    class CounterOnlyPublicationAdapter(_DeterministicAdapter):
        def apply_small_delta(
            self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
        ) -> None:
            before = int(
                conn.execute("SELECT COUNT(*) FROM source_fact_publication_stream").fetchone()[0]
            )
            super().apply_small_delta(conn, config)
            conn.execute(
                "DELETE FROM source_fact_publication_stream WHERE publication_sequence>?",
                (before,),
            )
            conn.commit()

    report = run_latest_state_benchmark(
        config=_config(),
        budgets=LatestStateBenchmarkBudgets(),
        database_path=tmp_path / "counter-only-publications.db",
        adapter=CounterOnlyPublicationAdapter(),
    )

    ratchet = next(item for item in report.ratchets if item.name == "small_delta_exact_work")
    assert report.small_delta.work.source_events == 1
    assert report.small_delta.work.independent_source_publications == 0
    assert not ratchet.passed


def test_cross_scope_ratchet_rejects_non_target_delta_write(tmp_path: Path) -> None:
    class CrossScopeWriteAdapter(_DeterministicAdapter):
        corrupted = False

        def refresh(
            self,
            conn: sqlite3.Connection,
            *,
            scope_id: str,
            config: LatestStateBenchmarkConfig,
            operation_recorded_at: datetime,
            resume_refresh_id: str | None = None,
            interrupt_after_batches: int | None = None,
        ) -> AdapterRefresh:
            result = super().refresh(
                conn,
                scope_id=scope_id,
                config=config,
                operation_recorded_at=operation_recorded_at,
                resume_refresh_id=resume_refresh_id,
                interrupt_after_batches=interrupt_after_batches,
            )
            if (
                not self.corrupted
                and scope_id == benchmark_scope_id(0)
                and result.fact_changes == config.delta_cell_count
            ):
                conn.execute(
                    "UPDATE latest_governed_fact_entries SET refresh_receipt_id=? "
                    "WHERE scope_key=? "
                    "AND canonical_metric_cell_id=("
                    "SELECT MIN(canonical_metric_cell_id) "
                    "FROM latest_governed_fact_entries "
                    "WHERE scope_key=?)",
                    (
                        "receipt-" + result.refresh_id,
                        benchmark_scope_id(1),
                        benchmark_scope_id(1),
                    ),
                )
                conn.commit()
                self.corrupted = True
            return result

    report = run_latest_state_benchmark(
        config=_config(),
        budgets=LatestStateBenchmarkBudgets(),
        database_path=tmp_path / "cross-scope-write.db",
        adapter=CrossScopeWriteAdapter(),
    )

    ratchets = {item.name: item for item in report.ratchets}
    assert ratchets["multi_scope_exact_initial_materialization"].passed
    assert not ratchets["small_delta_cross_scope_isolation"].passed
    assert not report.cross_scope.non_target_current_rows_bound_to_heads


def test_cross_scope_snapshot_uses_production_projection_keyset_index(
    tmp_path: Path,
) -> None:
    report = _run(tmp_path / "projection-keyset")
    ratchet = next(
        result
        for result in report.ratchets
        if result.name == "projection_keyset_index_bounds_cross_scope_evidence"
    )

    assert report.cross_scope.canonical_projection_keyset_index_columns == (
        "generation_id",
        "canonical_metric_cell_id",
    )
    assert report.cross_scope.source_scope_query_uses_projection_keyset_index
    assert report.cross_scope.source_scope_query_avoids_projection_scan
    assert ratchet.passed


def test_real_adapter_rejects_cross_issuer_source_inventory(
    tmp_path: Path,
) -> None:
    class CrossIssuerInventoryAdapter(LatestStateSqliteAdapter):
        def create_fixture(
            self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
        ) -> FixtureCounts:
            fixture = super().create_fixture(conn, config)
            conn.execute(
                "UPDATE source_inventory_snapshots SET issuer_id='issuer:0000' "
                "WHERE snapshot_id='inventory-0001'"
            )
            conn.commit()
            return fixture

    with pytest.raises(
        LatestGovernedStateError,
        match="source inventory does not bind to its issuer",
    ):
        run_latest_state_benchmark(
            config=_config(),
            budgets=LatestStateBenchmarkBudgets(),
            database_path=tmp_path / "cross-issuer-inventory.db",
            adapter=CrossIssuerInventoryAdapter(),
        )


def test_real_sqlite_adapter_calls_public_refresh_search_and_resume(
    tmp_path: Path,
) -> None:
    config = LatestStateBenchmarkConfig(
        publication_count=6,
        cell_count=24,
        document_count=4,
        chunk_count=8,
        scope_count=3,
        delta_publication_count=1,
        delta_cell_count=1,
        delta_document_count=1,
        delta_chunk_count=2,
        max_batch_rows=2,
        read_samples=2,
        read_limit=3,
        interrupt_after_batches=1,
    )
    report = run_latest_state_benchmark(
        config=config,
        budgets=LatestStateBenchmarkBudgets(),
        database_path=tmp_path / "real-adapter.db",
        adapter=LatestStateSqliteAdapter(),
    )

    assert report.fixture == FixtureCounts(
        publications=6,
        cells=24,
        documents=4,
        chunks=8,
        scopes=3,
    )
    assert report.no_op.work.source_reads == 0
    assert report.no_op.work.current_reads == 1
    assert report.no_op.work.current_writes == 0
    assert report.no_op.work.receipt_writes == 1
    assert report.small_delta.work.fact_changes == 1
    assert report.small_delta.work.document_changes == 1
    assert report.small_delta.work.narrative_changes == 2
    assert report.small_delta.work.independent_source_publications == 1
    assert report.small_delta.work.sqlite_vm_step_proxy > 0
    assert report.change_audit.baseline_logical_changes == 20
    assert report.change_audit.baseline_detailed_change_rows == 0
    assert report.change_audit.delta_logical_changes == 4
    assert report.change_audit.delta_detailed_change_rows == 4
    assert report.rows.refresh_changes == 4
    assert report.cross_scope.source_fact_rows == 24
    assert report.cross_scope.materialized_fact_rows_before_delta == 24
    assert report.cross_scope.materialized_fact_rows_after_delta == 24
    assert report.cross_scope.minimum_source_facts_per_scope == 8
    assert report.cross_scope.maximum_source_facts_per_scope == 8
    assert report.cross_scope.non_target_heads_unchanged
    assert report.fact_read.maximum_rows_fetched <= 3
    assert report.narrative_read.maximum_rows_fetched <= 3
    assert report.fact_read.uses_current_projection_only
    assert report.fact_read.avoids_full_current_scope_scan
    assert "canonical_metric_name IN (?)" in report.fact_read.query_sql
    assert any("ix_latest_governed_fact_search" in detail for detail in report.fact_read.query_plan)
    assert not any("TEMP B-TREE" in detail for detail in report.fact_read.query_plan)
    assert report.fact_read.avoids_temporary_sort
    assert report.narrative_read.uses_current_projection_only
    assert report.resume.equivalent
    assert report.resume.ordered_stage_identity_payloads_equal
    assert report.history_independence.equivalent, report.history_independence.model_dump()
    assert verify_report_sha256(report)


def test_evidence_clones_are_file_backed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = sqlite3.connect
    opened: list[str] = []

    def record_connect(
        database: str | Path,
        *,
        timeout: float = 5.0,
    ) -> sqlite3.Connection:
        opened.append(str(database))
        return real_connect(database, timeout=timeout)

    monkeypatch.setattr(sqlite3, "connect", record_connect)
    _run(tmp_path / "file-backed")

    assert len(opened) == 5
    assert ":memory:" not in opened
    assert all(Path(database).suffix == ".db" for database in opened)


def test_python_memory_gate_starts_after_cold_baseline(
    tmp_path: Path,
) -> None:
    class BoundaryAdapter(_DeterministicAdapter):
        refresh_calls = 0

        def create_fixture(
            self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
        ) -> FixtureCounts:
            assert not tracemalloc.is_tracing()
            return super().create_fixture(conn, config)

        def refresh(
            self,
            conn: sqlite3.Connection,
            *,
            scope_id: str,
            config: LatestStateBenchmarkConfig,
            operation_recorded_at: datetime,
            resume_refresh_id: str | None = None,
            interrupt_after_batches: int | None = None,
        ) -> AdapterRefresh:
            if self.refresh_calls < config.scope_count:
                assert not tracemalloc.is_tracing()
            else:
                assert tracemalloc.is_tracing()
            self.refresh_calls += 1
            return super().refresh(
                conn,
                scope_id=scope_id,
                config=config,
                operation_recorded_at=operation_recorded_at,
                resume_refresh_id=resume_refresh_id,
                interrupt_after_batches=interrupt_after_batches,
            )

    report = run_latest_state_benchmark(
        config=_config(),
        budgets=LatestStateBenchmarkBudgets(),
        database_path=tmp_path / "measurement-boundary.db",
        adapter=BoundaryAdapter(),
    )

    assert report.python_memory_measurement_scope == "post_fixture_hot_path"
    assert report.peak_python_memory_bytes > 0
    assert report.cold_baseline_process_memory.sample_count >= 2


def test_hot_path_wall_budget_excludes_ungated_fixture_prep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    clock = {"seconds": 0.0}

    class SlowFixtureAdapter(_DeterministicAdapter):
        refresh_calls = 0

        def create_fixture(
            self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
        ) -> FixtureCounts:
            fixture = super().create_fixture(conn, config)
            clock["seconds"] += 1_000.0
            return fixture

        def refresh(
            self,
            conn: sqlite3.Connection,
            *,
            scope_id: str,
            config: LatestStateBenchmarkConfig,
            operation_recorded_at: datetime,
            resume_refresh_id: str | None = None,
            interrupt_after_batches: int | None = None,
        ) -> AdapterRefresh:
            result = super().refresh(
                conn,
                scope_id=scope_id,
                config=config,
                operation_recorded_at=operation_recorded_at,
                resume_refresh_id=resume_refresh_id,
                interrupt_after_batches=interrupt_after_batches,
            )
            if self.refresh_calls >= config.scope_count:
                clock["seconds"] += 0.01
            self.refresh_calls += 1
            return result

    monkeypatch.setattr(
        benchmark_module.time,
        "perf_counter",
        lambda: clock["seconds"],
    )
    report = run_latest_state_benchmark(
        config=_config(),
        budgets=LatestStateBenchmarkBudgets(max_hot_path_seconds=900.0),
        database_path=tmp_path / "slow-fixture.db",
        adapter=SlowFixtureAdapter(),
    )

    hot_path_budget = next(
        result for result in report.budget_results if result.name == "hot_path_seconds"
    )
    assert report.fixture_prep_wall_seconds == pytest.approx(1_000.0)
    assert report.command_wall_seconds > 900.0
    assert report.hot_path_wall_seconds < 900.0
    assert hot_path_budget.actual == report.hot_path_wall_seconds
    assert hot_path_budget.maximum == 900.0
    assert hot_path_budget.passed
    assert report.overall_pass
    assert "total_seconds" not in {result.name for result in report.budget_results}


def test_sqlite_clone_fixture_multiplies_history_with_insert_select(
    tmp_path: Path,
) -> None:
    adapter = LatestStateSqliteAdapter()
    source = sqlite3.connect(tmp_path / "source.db")
    target = sqlite3.connect(tmp_path / "target.db")
    statements: list[str] = []
    target.set_trace_callback(statements.append)
    try:
        config = _config()
        adapter.create_fixture(source, config)
        adapter.clone_fixture(source, target, history_multiplier=4)

        assert target.execute("SELECT COUNT(*) FROM canonical_fact_projection_entries").fetchone()[
            0
        ] == (config.cell_count * 4)
        assert target.execute("SELECT COUNT(*) FROM search_corpus_document_memberships").fetchone()[
            0
        ] == (config.document_count * 4)
        assert target.execute("SELECT COUNT(*) FROM search_chunks").fetchone()[0] == (
            config.chunk_count * 4
        )
        normalized = tuple(statement.strip().upper() for statement in statements)
        assert any(
            statement.startswith("INSERT INTO CANONICAL_FACT_PROJECTION_ENTRIES")
            and " SELECT " in statement
            for statement in normalized
        )
        assert not any(
            statement.startswith("SELECT * FROM CANONICAL_FACT_PROJECTION_ENTRIES")
            for statement in normalized
        )
    finally:
        target.close()
        source.close()


def test_fixture_construction_has_bounded_python_collection_memory(
    tmp_path: Path,
) -> None:
    def fixture_peak(cell_count: int) -> int:
        conn = sqlite3.connect(tmp_path / f"fixture-{cell_count}.db")
        config = _config().model_copy(update={"cell_count": cell_count, "delta_cell_count": 1})
        tracemalloc.start()
        try:
            LatestStateSqliteAdapter().create_fixture(conn, config)
            assert (
                conn.execute("SELECT COUNT(*) FROM canonical_fact_projection_entries").fetchone()[0]
                == cell_count
            )
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            conn.close()

    small_peak = fixture_peak(1_000)
    large_peak = fixture_peak(50_000)

    assert large_peak < 16 * 1024 * 1024
    assert large_peak < small_peak * 5


def test_storage_budget_excludes_pre_materialization_source_fixture(
    tmp_path: Path,
) -> None:
    class LargeSourceFixtureAdapter(_DeterministicAdapter):
        def create_fixture(
            self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
        ) -> FixtureCounts:
            fixture = super().create_fixture(conn, config)
            conn.execute("CREATE TABLE benchmark_source_padding (payload BLOB)")
            conn.executemany(
                "INSERT INTO benchmark_source_padding VALUES (zeroblob(?))",
                ((4096,) for _ in range(256)),
            )
            conn.commit()
            return fixture

    report = run_latest_state_benchmark(
        config=_config(),
        budgets=LatestStateBenchmarkBudgets(max_allocated_sqlite_pages=100),
        database_path=tmp_path / "large-source-fixture.db",
        adapter=LargeSourceFixtureAdapter(),
    )

    storage_budget = next(
        result
        for result in report.budget_results
        if result.name == "latest_state_incremental_allocated_pages"
    )
    assert report.storage.source_fixture_allocated_pages > storage_budget.maximum
    assert storage_budget.actual == report.storage.latest_state_incremental_allocated_pages
    assert storage_budget.passed


def test_reporting_entity_index_storage_is_included_in_incremental_budget(
    tmp_path: Path,
) -> None:
    report = _run(tmp_path / "index-cost")
    storage_budget = next(
        result
        for result in report.budget_results
        if result.name == "latest_state_incremental_allocated_pages"
    )
    storage_ratchet = next(
        result
        for result in report.ratchets
        if result.name == "reporting_entity_index_storage_is_incremental"
    )

    assert report.storage.reporting_entity_index_allocated_pages > 0
    assert storage_budget.actual == (
        report.storage.reporting_entity_index_allocated_pages
        + report.storage.latest_state_materialization_allocated_pages
    )
    assert storage_ratchet.passed


def test_production_profile_records_measured_independent_dimensions() -> None:
    config = production_benchmark_config()
    budgets = production_benchmark_budgets()

    assert config.profile == "production"
    assert config.publication_count == 1_284
    assert config.cell_count == 831_471
    assert config.scope_count == 87
    assert config.document_count == 24
    assert config.chunk_count == 192
    assert divmod(config.cell_count, config.scope_count) == (9_557, 12)
    assert config.delta_cell_count + config.delta_document_count + config.delta_chunk_count == 26
    assert LatestStateBenchmarkBudgets().max_hot_path_seconds == 120.0
    assert budgets.max_hot_path_seconds == 900.0


def test_production_profile_requires_confirmation_and_reports_fixed_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution.benchmark_latest_state import (
        PRODUCTION_PROFILE_COMMAND,
        PRODUCTION_PROFILE_REPORT,
        main,
    )

    assert main(["--profile", "production"]) == 1
    event = json.loads(capsys.readouterr().err)
    assert event["event"] == "latest_state_benchmark_refused"
    assert event["command"] == PRODUCTION_PROFILE_COMMAND
    assert event["report"] == str(PRODUCTION_PROFILE_REPORT)

    assert (
        main(
            [
                "--profile",
                "production",
                "--confirm-production-profile",
                "--max-hot-path-seconds",
                "901",
            ]
        )
        == 1
    )
    budget_event = json.loads(capsys.readouterr().err)
    assert budget_event["event"] == "latest_state_benchmark_refused"
    assert "cannot exceed 900 seconds" in budget_event["reason"]


def test_cli_hard_runtime_ceiling_is_fixed_and_separate_from_benchmark_budget() -> None:
    from execution.benchmark_latest_state import (
        HARD_RUNTIME_CEILING_SECONDS,
        HardRuntimeCeiling,
    )

    fired = threading.Event()
    ceiling = HardRuntimeCeiling(seconds=0.01, on_timeout=fired.set)
    ceiling.start()
    try:
        assert fired.wait(timeout=1.0)
    finally:
        ceiling.cancel()

    assert HARD_RUNTIME_CEILING_SECONDS == 1_500.0


def test_cli_real_adapter_writes_report_and_refuses_existing_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution.benchmark_latest_state import main

    database = tmp_path / "cli.db"
    output = tmp_path / "cli.json"
    arguments = [
        "--database",
        str(database),
        "--output",
        str(output),
        "--publication-count",
        "4",
        "--cell-count",
        "12",
        "--document-count",
        "3",
        "--chunk-count",
        "6",
        "--scope-count",
        "2",
        "--delta-publication-count",
        "1",
        "--delta-cell-count",
        "1",
        "--delta-document-count",
        "1",
        "--delta-chunk-count",
        "2",
        "--max-batch-rows",
        "2",
        "--read-samples",
        "2",
        "--read-limit",
        "2",
        "--interrupt-after-batches",
        "1",
    ]
    exit_code = main(arguments)
    captured = capsys.readouterr()

    assert exit_code in (0, 2)
    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["fixture"]["cells"] == 12
    stderr_events = [json.loads(line)["event"] for line in captured.err.splitlines()]
    assert stderr_events == [
        "latest_state_benchmark_started",
        "latest_state_benchmark_finished",
    ]
    assert set(json.loads(captured.out)) == {
        "output",
        "overall_pass",
        "report_sha256",
    }

    refused = main(arguments)
    refused_output = capsys.readouterr()
    assert refused == 1
    assert json.loads(refused_output.err.splitlines()[-1])["event"] == (
        "latest_state_benchmark_refused"
    )
