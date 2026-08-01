"""Authoritative clone-only evidence generators for latest-state rehearsal.

The admitted rehearsal database is an immutable source for these operations.
Restore proof is performed only on one bounded disposable clone at a time and
the clone is removed after each proof phase.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.compressed_candidate_clone import (
    MINIMUM_SAFE_FREE_BYTES,
    CompressedCloneRequest,
    prepare_compressed_clone,
)
from provenance.immutable_artifact import (
    path_aliases_any,
    read_stable_artifact,
    require_no_reparse_points,
)
from provenance.latest_governed_population import LatestGovernedPopulationReceipt
from provenance.latest_state_benchmark import (
    LatestStateBenchmarkReport,
    LatestStateSqliteAdapter,
    verify_production_benchmark_report,
)
from provenance.latest_state_rehearsal import (
    ArtifactCommitment,
    CandidatePerformanceEvidence,
    CandidateScopePerformance,
    DatabaseFileState,
    RestoreRoundtripEvidence,
)
from runtime.job_runtime import portfolio_db_path
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RestoreRoundtripRequest(_FrozenModel):
    """Inputs for a disposable, byte-exact restore proof."""

    repo_root: Path
    source_database: Path
    candidate_audit_receipt: Path
    candidate_coverage_receipt: Path
    work_directory: Path
    operation_recorded_at: datetime
    minimum_free_bytes: int = Field(ge=MINIMUM_SAFE_FREE_BYTES)

    @field_validator("operation_recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operation_recorded_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _bounded_paths(self) -> Self:
        repo_root = self.repo_root.expanduser().resolve()
        source = self.source_database.expanduser().resolve()
        work = self.work_directory.expanduser().resolve()
        audit = self.candidate_audit_receipt.expanduser().resolve()
        coverage = self.candidate_coverage_receipt.expanduser().resolve()
        for path in (repo_root, source, work, audit, coverage):
            require_no_reparse_points(path)
        if repo_root != self.repo_root:
            raise ValueError("restore repo root must be absolute and canonical")
        live = portfolio_db_path(repo_root).resolve()
        live_storage = {
            live,
            *(Path(f"{live}{suffix}") for suffix in ("-wal", "-shm", "-journal")),
        }
        if path_aliases_any(source, live_storage):
            raise ValueError("restore proof source must not alias the configured live database")
        if work.parent != source.parent:
            raise ValueError("restore work directory must share the source database parent")
        protected = {
            source,
            audit,
            coverage,
            *(Path(f"{source}{suffix}") for suffix in ("-wal", "-shm", "-journal")),
        }
        work_database = work / "restored-candidate.db"
        if (
            work in protected
            or work_database in protected
            or path_aliases_any(work, live_storage)
            or path_aliases_any(work_database, live_storage)
            or path_aliases_any(audit, live_storage)
            or path_aliases_any(coverage, live_storage)
        ):
            raise ValueError("restore work paths alias an admitted input")
        return self


class CandidatePerformanceRequest(_FrozenModel):
    """Bounded thresholds and synthetic evidence for candidate read ratchets."""

    schema_version: Literal["latest-governed-candidate-performance-request/v1"] = (
        "latest-governed-candidate-performance-request/v1"
    )
    synthetic_benchmark_report: Path
    read_samples: int = Field(ge=3, le=100)
    read_limit: int = Field(ge=1, le=1_000)
    max_fact_read_p95_milliseconds: float = Field(gt=0)
    max_narrative_read_p95_milliseconds: float = Field(gt=0)
    max_history_scale_ratio: float = Field(gt=0)

    @model_validator(mode="after")
    def _canonical_report(self) -> Self:
        report = self.synthetic_benchmark_report.expanduser().resolve()
        require_no_reparse_points(report)
        if report != self.synthetic_benchmark_report or not report.is_file():
            raise ValueError("synthetic benchmark report must be an existing canonical path")
        return self


def _cleanup_owned_clone(work_directory: Path, database: Path) -> None:
    """Remove only the exact disposable clone paths owned by this operation."""

    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)
    database.unlink(missing_ok=True)
    if work_directory.exists():
        work_directory.rmdir()


def _mutate_disposable_clone(path: Path) -> None:
    """Commit a deterministic sentinel mutation to the disposable clone only."""

    conn = connect_sqlite(
        path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE __latest_state_restore_probe("
            "probe_id INTEGER PRIMARY KEY CHECK(probe_id=1),marker TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO __latest_state_restore_probe(probe_id,marker) VALUES (1,?)",
            ("latest-governed-restore-roundtrip/v1",),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _database_checks(path: Path) -> tuple[str, str, int]:
    conn = connect_sqlite(
        path,
        role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
        schema_preflight=False,
    )
    try:
        quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        return quick, integrity, foreign_keys
    finally:
        conn.close()


def _clone_once(request: RestoreRoundtripRequest, destination: Path) -> str:
    receipt = prepare_compressed_clone(
        CompressedCloneRequest(
            source_database=request.source_database,
            candidate_audit_receipt=request.candidate_audit_receipt,
            candidate_coverage_receipt=request.candidate_coverage_receipt,
            destination_database=destination,
            operation_recorded_at=request.operation_recorded_at,
            minimum_free_bytes=request.minimum_free_bytes,
        )
    )
    return str(receipt.destination_database_sha256)


def generate_restore_roundtrip_evidence(
    request: RestoreRoundtripRequest,
) -> RestoreRoundtripEvidence:
    """Prove mutation and exact restore without writing the admitted database."""

    source = request.source_database.expanduser().resolve()
    work = request.work_directory.expanduser().resolve()
    disposable = work / "restored-candidate.db"
    if work.exists():
        raise ValueError("restore work directory already exists; preserve it for inspection")
    source_before = DatabaseFileState.from_path(source)
    created = False
    try:
        initial_sha = _clone_once(request, disposable)
        created = True
        if initial_sha != source_before.file_sha256:
            raise ValueError("disposable rollback clone differs from the admitted database")
        _mutate_disposable_clone(disposable)
        mutated = DatabaseFileState.from_path(disposable).file_sha256
        if mutated == initial_sha:
            raise ValueError("restore drill mutation did not change disposable database bytes")
        _cleanup_owned_clone(work, disposable)
        created = False

        restored_sha = _clone_once(request, disposable)
        created = True
        if restored_sha != initial_sha:
            raise ValueError("restored disposable clone differs from its rollback commitment")
        quick, integrity, foreign_keys = _database_checks(disposable)
        restored_observed = DatabaseFileState.from_path(disposable).file_sha256
        if restored_observed != restored_sha:
            raise ValueError("restored disposable clone changed during verification")
        source_after = DatabaseFileState.from_path(source)
        if source_after != source_before:
            raise ValueError("admitted database changed during disposable restore proof")
        return RestoreRoundtripEvidence(
            rollback_database_sha256=initial_sha,
            mutated_database_sha256=mutated,
            restored_database_sha256=restored_observed,
            quick_check=quick,
            integrity_check=integrity,
            foreign_key_violation_count=foreign_keys,
            replay_equivalent=restored_observed == initial_sha,
        )
    finally:
        if created:
            _cleanup_owned_clone(work, disposable)


def _load_benchmark(
    path: Path,
) -> tuple[ArtifactCommitment, LatestStateBenchmarkReport]:
    snapshot, payload = read_stable_artifact(path)
    artifact = ArtifactCommitment(
        path=str(snapshot.path),
        device=snapshot.device,
        inode=snapshot.inode,
        size_bytes=snapshot.size_bytes,
        modified_time_ns=snapshot.modified_time_ns,
        changed_time_ns=snapshot.changed_time_ns,
        file_sha256=snapshot.file_sha256,
    )
    report = LatestStateBenchmarkReport.model_validate_json(payload)
    if not verify_production_benchmark_report(report):
        raise ValueError("synthetic benchmark report commitment is invalid")
    return artifact, report


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("candidate performance measurement is empty")
    rank = (len(ordered) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _measure_candidate_reads(
    *,
    database_path: Path,
    scope_ids: tuple[str, ...],
    read_samples: int,
    read_limit: int,
) -> tuple[CandidateScopePerformance, ...]:
    adapter = LatestStateSqliteAdapter()
    measurements: list[CandidateScopePerformance] = []
    conn = connect_sqlite(
        database_path,
        role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
        schema_preflight=False,
    )
    try:
        for scope_id in scope_ids:
            fact_timings: list[float] = []
            narrative_timings: list[float] = []
            fact_row = conn.execute(
                "SELECT canonical_metric_name FROM latest_governed_fact_entries "
                "WHERE scope_key=? ORDER BY canonical_metric_name LIMIT 1",
                (scope_id,),
            ).fetchone()
            narrative_row = conn.execute(
                "SELECT text FROM latest_governed_narrative_entries "
                "WHERE scope_key=? ORDER BY expected_document_key,chunk_key LIMIT 1",
                (scope_id,),
            ).fetchone()
            if fact_row is None or narrative_row is None:
                raise ValueError("candidate performance scope lacks fact or narrative rows")
            fact_query = str(fact_row[0])
            narrative_tokens = tuple(re.findall(r"[A-Za-z0-9_-]+", str(narrative_row[0])))
            if not narrative_tokens:
                raise ValueError("candidate performance narrative probe is empty")
            narrative_query = narrative_tokens[0][:128]
            fact_plan = adapter.fact_query_plan(
                conn,
                scope_id=scope_id,
                query=fact_query,
                limit=read_limit,
            )
            narrative_plan = adapter.narrative_query_plan(
                conn,
                scope_id=scope_id,
                query=narrative_query,
                limit=read_limit,
            )
            fact_plan_details = tuple(str(item) for item in fact_plan.details)
            narrative_plan_details = tuple(str(item) for item in narrative_plan.details)
            for _ in range(read_samples):
                started = time.perf_counter()
                fact_hits = adapter.search_facts(
                    conn,
                    scope_id=scope_id,
                    query=fact_query,
                    limit=read_limit,
                )
                fact_timings.append((time.perf_counter() - started) * 1_000.0)
                started = time.perf_counter()
                narrative_hits = adapter.search_narrative(
                    conn,
                    scope_id=scope_id,
                    query=narrative_query,
                    limit=read_limit,
                )
                narrative_timings.append((time.perf_counter() - started) * 1_000.0)
                if not fact_hits or not narrative_hits:
                    raise ValueError("candidate performance public read returned no governed rows")
            fact_plan_text = " ".join(fact_plan_details).casefold()
            narrative_plan_text = " ".join(narrative_plan_details).casefold()
            measurements.append(
                CandidateScopePerformance(
                    scope_id=scope_id,
                    sample_count=read_samples,
                    fact_read_p95_milliseconds=_percentile(fact_timings, 0.95),
                    narrative_read_p95_milliseconds=_percentile(narrative_timings, 0.95),
                    fact_query_uses_production_index=(
                        fact_plan_text.count("ix_latest_governed_fact_search") == 1
                        and "scan latest_governed_fact_entries" not in fact_plan_text
                    ),
                    narrative_query_uses_fts_index=(
                        "latest_governed_narrative_fts" in narrative_plan_text
                        and narrative_plan_text.count("virtual table index") == 1
                    ),
                    fact_query_plan=fact_plan_details,
                    narrative_query_plan=narrative_plan_details,
                )
            )
    finally:
        conn.close()
    return tuple(measurements)


def generate_candidate_performance_evidence(
    *,
    database_path: Path,
    request: CandidatePerformanceRequest,
    request_artifact: ArtifactCommitment,
    no_op_receipt_artifact: ArtifactCommitment,
    no_op_receipt: LatestGovernedPopulationReceipt,
    production_scope_ids: tuple[str, ...],
) -> CandidatePerformanceEvidence:
    """Measure current public reads and bind a full-cohort nonmutating no-op."""

    expected_scopes = tuple(sorted(set(production_scope_ids)))
    if not expected_scopes or expected_scopes != production_scope_ids:
        raise ValueError("candidate performance requires a nonempty exact production cohort")
    result = no_op_receipt.result
    if (
        result.mode != "dry_run"
        or result.outcome != "planned"
        or tuple(result.processed_scope_ids) != expected_scopes
        or result.remaining_scope_ids
        or tuple(item.scope_id for item in result.scope_results) != expected_scopes
    ):
        raise ValueError("candidate no-op receipt differs from the exact production cohort")
    if result.heads_before != result.heads_after:
        raise ValueError("candidate no-op receipt changed latest-state heads")
    no_op_writes = sum(item.dry_run.current_write_count for item in result.scope_results)
    if any(
        item.dry_run.outcome != "no_op"
        or item.dry_run.fact_change_count
        or item.dry_run.document_change_count
        or item.dry_run.narrative_change_count
        for item in result.scope_results
    ):
        raise ValueError("candidate performance dry run is not an exact no-op")
    if not no_op_receipt_artifact.verify():
        raise ValueError("candidate no-op receipt artifact changed")
    if not request_artifact.verify():
        raise ValueError("candidate performance request artifact changed")
    benchmark_artifact, benchmark = _load_benchmark(request.synthetic_benchmark_report)
    if not verify_production_benchmark_report(benchmark):
        raise ValueError("synthetic latest-state production benchmark did not pass admission")
    database = database_path.expanduser().resolve()
    before = DatabaseFileState.from_path(database)
    measurements = _measure_candidate_reads(
        database_path=database,
        scope_ids=expected_scopes,
        read_samples=request.read_samples,
        read_limit=request.read_limit,
    )
    after = DatabaseFileState.from_path(database)
    if after != before:
        raise ValueError("candidate performance measurement changed the database")
    if (
        not request_artifact.verify()
        or not no_op_receipt_artifact.verify()
        or not benchmark_artifact.verify()
    ):
        raise ValueError("candidate performance input artifact changed during measurement")
    fact_p95 = max(item.fact_read_p95_milliseconds for item in measurements)
    narrative_p95 = max(item.narrative_read_p95_milliseconds for item in measurements)
    fact_plan = tuple(detail for item in measurements for detail in item.fact_query_plan)
    narrative_plan = tuple(detail for item in measurements for detail in item.narrative_query_plan)
    fact_index_count = sum(item.fact_query_uses_production_index for item in measurements)
    narrative_index_count = sum(item.narrative_query_uses_fts_index for item in measurements)
    return CandidatePerformanceEvidence(
        database_sha256=after.file_sha256,
        performance_request=request_artifact,
        synthetic_benchmark_report=benchmark_artifact,
        candidate_no_op_receipt=no_op_receipt_artifact,
        synthetic_benchmark_profile="production",
        synthetic_benchmark_passed=benchmark.overall_pass,
        no_op_current_write_count=no_op_writes,
        measured_scope_ids=expected_scopes,
        scope_measurements=measurements,
        read_sample_count=sum(item.sample_count for item in measurements),
        fact_read_p95_milliseconds=fact_p95,
        narrative_read_p95_milliseconds=narrative_p95,
        max_fact_read_p95_milliseconds=request.max_fact_read_p95_milliseconds,
        max_narrative_read_p95_milliseconds=request.max_narrative_read_p95_milliseconds,
        fact_query_uses_production_index=fact_index_count == len(expected_scopes),
        narrative_query_uses_fts_index=narrative_index_count == len(expected_scopes),
        fact_query_index_use_count=fact_index_count,
        narrative_fts_index_use_count=narrative_index_count,
        fact_query_plan=fact_plan,
        narrative_query_plan=narrative_plan,
        history_scale_ratio=benchmark.history_independence.latency_ratio,
        max_history_scale_ratio=request.max_history_scale_ratio,
    )
