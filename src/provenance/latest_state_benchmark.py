"""Deterministic, provider-free benchmark for latest governed state.

The benchmark intentionally measures *work*, not only elapsed time.  Stable
semantic counters are hard ratchets; timings, SQLite pages, and Python peak
memory are secondary evidence.  The implementation calls the public
``provenance.latest_governed_state`` API through a narrow adapter so tests can
exercise report and refusal behavior without provider or wall-clock
dependencies.
"""

from __future__ import annotations

import ctypes
import gc
import hashlib
import json
import os
import platform
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self, cast

import pydantic
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scope_identity import derive_retrieval_scope_id
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

MAX_COUNT = 1_000_000
MAX_READ_LIMIT = 1_000
MAX_SMALL_DELTA_WRITE_AMPLIFICATION = 8.0
MAX_SMALL_DELTA_VM_STEPS_PER_CHANGE = 100_000
MAX_RETAINED_HISTORY_VM_STEP_RATIO = 1.10
MAX_INITIAL_CHECKPOINT_DIGEST_BUCKETS = 4_096
SQLITE_PROGRESS_INTERVAL = 1
REPORT_VERSION = "latest_state_benchmark.v1"
POLICY_VERSION = "latest-governed-state.v1"
BENCHMARK_STAMP = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
ProcessMemoryMetric = Literal["private_bytes", "rss_bytes", "unavailable"]
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_IMPLEMENTATION_RELATIVE_PATHS = (
    "alembic/versions/0261_latest_governed_state.py",
    "alembic/versions/0263_ask_scope_identity.py",
    "execution/benchmark_latest_state.py",
    "src/scope_identity.py",
    "src/provenance/scope_identity.py",
    "src/provenance/latest_governed_state.py",
    "src/provenance/latest_state_benchmark.py",
)
_FORBIDDEN_PLAN_TOKENS = (
    "with recursive",
    "canonical_fact_projection",
    "search_corpus_document_memberships",
    "search_chunks",
    "search_lexical_chunks",
)


def canonical_json(value: object) -> str:
    """Return the canonical JSON representation used by report commitments."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def benchmark_scope_id(scope_index: int) -> str:
    return derive_retrieval_scope_id(
        source_scope_key="investor-research",
        issuer_id=f"issuer:{scope_index:04d}",
    )


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LatestStateBenchmarkConfig(_FrozenModel):
    """Independent scale and delta dimensions for a benchmark run."""

    profile: Literal["smoke", "production"] = "smoke"
    publication_count: int = Field(default=128, ge=1, le=MAX_COUNT)
    cell_count: int = Field(default=10_000, ge=1, le=MAX_COUNT)
    document_count: int = Field(default=500, ge=1, le=MAX_COUNT)
    chunk_count: int = Field(default=4_000, ge=1, le=MAX_COUNT)
    scope_count: int = Field(default=12, ge=1, le=10_000)
    delta_publication_count: int = Field(default=1, ge=1, le=MAX_COUNT)
    delta_cell_count: int = Field(default=8, ge=1, le=MAX_COUNT)
    delta_document_count: int = Field(default=2, ge=1, le=MAX_COUNT)
    delta_chunk_count: int = Field(default=16, ge=1, le=MAX_COUNT)
    max_batch_rows: int = Field(default=1_000, ge=1, le=MAX_READ_LIMIT)
    read_samples: int = Field(default=40, ge=1, le=MAX_READ_LIMIT)
    read_limit: int = Field(default=20, ge=1, le=MAX_READ_LIMIT)
    history_multiplier: Literal[4] = 4
    interrupt_after_batches: int = Field(default=1, ge=1, le=MAX_COUNT)

    @model_validator(mode="after")
    def _validate_dimensions(self) -> Self:
        pairs = (
            ("delta_publication_count", self.delta_publication_count, self.publication_count),
            ("delta_cell_count", self.delta_cell_count, self.cell_count),
            ("delta_document_count", self.delta_document_count, self.document_count),
            ("delta_chunk_count", self.delta_chunk_count, self.chunk_count),
        )
        for name, delta, total in pairs:
            if delta > total:
                raise ValueError(f"{name} cannot exceed its base count")
        if self.scope_count > self.cell_count:
            raise ValueError("scope_count cannot exceed cell_count")
        target_scope_fact_count = (self.cell_count + self.scope_count - 1) // self.scope_count
        if self.delta_cell_count > target_scope_fact_count:
            raise ValueError("delta_cell_count cannot exceed the target scope fact count")
        delta_total = self.delta_cell_count + self.delta_document_count + self.delta_chunk_count
        if self.interrupt_after_batches >= delta_total:
            raise ValueError("resume fixture must contain more changes than interrupted batches")
        return self


class LatestStateBenchmarkBudgets(_FrozenModel):
    max_hot_path_seconds: float = Field(default=120.0, gt=0)
    max_peak_python_memory_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    max_allocated_sqlite_pages: int = Field(default=250_000, gt=0)
    max_noop_milliseconds: float = Field(default=1_000.0, gt=0)
    max_small_delta_milliseconds: float = Field(default=5_000.0, gt=0)
    max_fact_read_p95_milliseconds: float = Field(default=100.0, gt=0)
    max_narrative_read_p95_milliseconds: float = Field(default=100.0, gt=0)
    max_history_latency_ratio: float = Field(default=1.50, ge=1.0)


class FixtureCounts(_FrozenModel):
    publications: int = Field(ge=0)
    cells: int = Field(ge=0)
    documents: int = Field(ge=0)
    chunks: int = Field(ge=0)
    scopes: int = Field(ge=0)


class RefreshWorkVector(_FrozenModel):
    source_events: int = Field(ge=0)
    independent_source_publications: int = Field(ge=0)
    fact_changes: int = Field(ge=0)
    document_changes: int = Field(ge=0)
    narrative_changes: int = Field(ge=0)
    source_reads: int = Field(ge=0)
    current_reads: int = Field(ge=0)
    current_writes: int = Field(ge=0)
    receipt_writes: int = Field(ge=0)
    total_changes: int = Field(ge=0)
    sqlite_vm_step_proxy: int = Field(ge=0)
    allocated_pages_before: int = Field(ge=0)
    allocated_pages_after: int = Field(ge=0)


class RefreshMeasurement(_FrozenModel):
    outcome: str
    wall_milliseconds: float = Field(ge=0)
    terminal_commitment: str = Field(min_length=64, max_length=64)
    refresh_id: str
    work: RefreshWorkVector


class ReadMeasurement(_FrozenModel):
    sample_count: int = Field(ge=1)
    limit: int = Field(ge=1, le=MAX_READ_LIMIT)
    maximum_rows_fetched: int = Field(ge=0, le=MAX_READ_LIMIT)
    p50_milliseconds: float = Field(ge=0)
    p95_milliseconds: float = Field(ge=0)
    query_sql: str
    query_parameters_sha256: str = Field(min_length=64, max_length=64)
    query_plan: tuple[str, ...]
    uses_current_projection_only: bool
    avoids_full_current_scope_scan: bool
    avoids_temporary_sort: bool


class CurrentHistoricalRows(_FrozenModel):
    current_facts: int = Field(ge=0)
    current_documents: int = Field(ge=0)
    current_narrative_chunks: int = Field(ge=0)
    refresh_receipts: int = Field(ge=0)
    refresh_changes: int = Field(ge=0)
    staged_rows: int = Field(ge=0)
    retained_fact_rows: int = Field(ge=0)
    retained_document_rows: int = Field(ge=0)
    retained_narrative_rows: int = Field(ge=0)


class StorageEvidence(_FrozenModel):
    page_size_bytes: int = Field(gt=0)
    source_fixture_allocated_pages: int = Field(ge=0)
    reporting_entity_index_allocated_pages: int = Field(ge=0)
    latest_state_materialization_allocated_pages: int = Field(ge=0)
    latest_state_incremental_allocated_pages: int = Field(ge=0)
    total_allocated_pages: int = Field(ge=0)
    source_fixture_database_bytes: int = Field(ge=0)
    reporting_entity_index_database_bytes: int = Field(ge=0)
    latest_state_materialization_database_bytes: int = Field(ge=0)
    latest_state_incremental_database_bytes: int = Field(ge=0)
    total_database_bytes: int = Field(ge=0)


class ChangeAuditEvidence(_FrozenModel):
    baseline_refresh_id: str
    baseline_mode: Literal["baseline_digest_buckets.v1"]
    baseline_logical_changes: int = Field(ge=1)
    baseline_digest_bucket_limit: Literal[4096]
    baseline_declared_digest_bucket_commitments: int = Field(ge=1, le=4096)
    baseline_digest_bucket_commitments: int = Field(ge=1, le=4096)
    baseline_digest_buckets_non_empty: bool
    baseline_digest_buckets_ordered: bool
    baseline_detailed_change_rows: int = Field(ge=0)
    delta_refresh_id: str
    delta_mode: Literal["coordinate_changes.v1"]
    delta_logical_changes: int = Field(ge=1)
    delta_coordinate_change_commitments: int = Field(ge=1)
    delta_detailed_change_rows: int = Field(ge=0)


class CrossScopeIsolationEvidence(_FrozenModel):
    target_scope_id: str
    canonical_metric_cell_scope_index_columns: tuple[str, ...]
    canonical_projection_keyset_index_columns: tuple[str, ...]
    source_scope_query_plan: tuple[str, ...]
    source_scope_query_uses_projection_keyset_index: bool
    source_scope_query_avoids_projection_scan: bool
    authoritative_scope_count: int = Field(ge=1)
    authoritative_issuer_count: int = Field(ge=1)
    authoritative_reporting_entity_count: int = Field(ge=1)
    source_fact_rows: int = Field(ge=1)
    source_reporting_entity_mismatches: int = Field(ge=0)
    minimum_source_facts_per_scope: int = Field(ge=0)
    maximum_source_facts_per_scope: int = Field(ge=0)
    target_source_fact_rows: int = Field(ge=1)
    materialized_scope_count_before_delta: int = Field(ge=1)
    materialized_scope_count_after_delta: int = Field(ge=1)
    materialized_fact_rows_before_delta: int = Field(ge=1)
    materialized_fact_rows_after_delta: int = Field(ge=1)
    target_current_fact_rows_before_delta: int = Field(ge=1)
    target_current_fact_rows_after_delta: int = Field(ge=1)
    scope_fact_count_mismatches_before_delta: int = Field(ge=0)
    scope_fact_count_mismatches_after_delta: int = Field(ge=0)
    cross_scope_fact_mismatches_before_delta: int = Field(ge=0)
    cross_scope_fact_mismatches_after_delta: int = Field(ge=0)
    detailed_change_rows_before_delta: int = Field(ge=0)
    detailed_change_rows_after_delta: int = Field(ge=0)
    non_target_head_set_sha256_before_delta: str = Field(min_length=64, max_length=64)
    non_target_head_set_sha256_after_delta: str = Field(min_length=64, max_length=64)
    non_target_heads_unchanged: bool
    non_target_current_rows_bound_to_heads: bool


class ImplementationFileDigest(_FrozenModel):
    project_relative_path: str
    sha256: str = Field(min_length=64, max_length=64)


class ImplementationProvenance(_FrozenModel):
    digest_algorithm: Literal["sha256"]
    files: tuple[ImplementationFileDigest, ...]
    source_set_sha256: str = Field(min_length=64, max_length=64)


class WriteAmplificationEvidence(_FrozenModel):
    no_op_logical_writes: int = Field(ge=0)
    no_op_physical_total_changes: int = Field(ge=0)
    small_delta_logical_writes: int = Field(ge=1)
    small_delta_physical_total_changes: int = Field(ge=0)
    small_delta_amplification_ratio: float = Field(ge=0)
    small_delta_durable_logical_writes: int = Field(ge=1)
    small_delta_durable_amplification_ratio: float = Field(ge=0)
    small_delta_allocated_page_growth: int = Field(ge=0)


class ResumeEvidence(_FrozenModel):
    interrupted_refresh_id: str
    resume_cursor: str | None
    staged_rows_before_resume: int = Field(ge=0)
    staged_rows_rewritten: int = Field(ge=0)
    replayed_rows: int = Field(ge=0)
    duplicate_rows_after_resume: int = Field(ge=0)
    staged_identity_payload_bytes: int = Field(ge=0)
    finalized_identity_payload_prefix_bytes: int = Field(ge=0)
    staged_identity_payload_sha256: str = Field(min_length=64, max_length=64)
    finalized_identity_payload_prefix_sha256: str = Field(min_length=64, max_length=64)
    ordered_stage_identity_payloads_equal: bool
    final_commitment: str = Field(min_length=64, max_length=64)
    uninterrupted_commitment: str = Field(min_length=64, max_length=64)
    equivalent: bool


class HistoryIndependenceEvidence(_FrozenModel):
    one_x_commitment: str = Field(min_length=64, max_length=64)
    four_x_commitment: str = Field(min_length=64, max_length=64)
    one_x_work: RefreshWorkVector
    four_x_work: RefreshWorkVector
    one_x_wall_milliseconds: float = Field(ge=0)
    four_x_wall_milliseconds: float = Field(ge=0)
    latency_ratio: float = Field(ge=0)
    sqlite_vm_step_ratio: float = Field(ge=0)
    equivalent: bool


class EnvironmentVersions(_FrozenModel):
    python: str
    sqlite: str
    pydantic: str
    platform: str


class ProcessMemoryEvidence(_FrozenModel):
    metric: ProcessMemoryMetric
    before_bytes: int = Field(ge=0)
    after_bytes: int = Field(ge=0)
    peak_bytes: int = Field(ge=0)
    sample_count: int = Field(ge=2)


class RatchetResult(_FrozenModel):
    name: str
    passed: bool
    detail: str


class BudgetResult(_FrozenModel):
    name: str
    actual: float
    maximum: float
    passed: bool


class LatestStateBenchmarkReport(_FrozenModel):
    report_version: Literal["latest_state_benchmark.v1"]
    config: LatestStateBenchmarkConfig
    budgets: LatestStateBenchmarkBudgets
    config_sha256: str = Field(min_length=64, max_length=64)
    fixture: FixtureCounts
    no_op: RefreshMeasurement
    small_delta: RefreshMeasurement
    fact_read: ReadMeasurement
    narrative_read: ReadMeasurement
    rows: CurrentHistoricalRows
    storage: StorageEvidence
    change_audit: ChangeAuditEvidence
    cross_scope: CrossScopeIsolationEvidence
    implementation_provenance: ImplementationProvenance
    write_amplification: WriteAmplificationEvidence
    resume: ResumeEvidence
    history_independence: HistoryIndependenceEvidence
    peak_python_memory_bytes: int = Field(ge=0)
    python_memory_measurement_scope: Literal["post_fixture_hot_path"]
    cold_baseline_process_memory: ProcessMemoryEvidence
    fixture_prep_wall_seconds: float = Field(ge=0)
    hot_path_wall_seconds: float = Field(ge=0)
    command_wall_seconds: float = Field(ge=0)
    environment: EnvironmentVersions
    ratchets: tuple[RatchetResult, ...]
    budget_results: tuple[BudgetResult, ...]
    overall_pass: bool
    report_sha256: str = Field(min_length=64, max_length=64)


class RefusedBenchmarkPathError(ValueError):
    """Raised before opening a non-isolated benchmark path."""


class BenchmarkContractError(RuntimeError):
    """Raised when the public latest-state contract cannot be benchmarked."""


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    )


def _process_memory_sample(pid: int) -> tuple[ProcessMemoryMetric, int]:
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        open_process = kernel32["OpenProcess"]
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        close_handle = kernel32["CloseHandle"]
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        get_memory = psapi["GetProcessMemoryInfo"]
        get_memory.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCountersEx),
            wintypes.DWORD,
        )
        get_memory.restype = wintypes.BOOL
        handle = open_process(0x0400 | 0x0010, False, pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            counters = _ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(counters)
            if not get_memory(handle, ctypes.byref(counters), counters.cb):
                raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
            return "private_bytes", int(counters.PrivateUsage)
        finally:
            close_handle(handle)
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return "rss_bytes", int(line.split()[1]) * 1024
    return "unavailable", 0


class _ProcessMemorySampler:
    def __init__(self, pid: int) -> None:
        self._pid = pid
        metric, initial = _process_memory_sample(pid)
        self._metric: ProcessMemoryMetric = metric
        self._before = initial
        self._after = initial
        self._peak = initial
        self._sample_count = 1
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._sample_until_stopped,
            name="latest-state-benchmark-memory",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> ProcessMemoryEvidence:
        self._stop.set()
        self._thread.join()
        metric, value = _process_memory_sample(self._pid)
        if metric != self._metric:
            raise BenchmarkContractError("process memory metric changed during sampling")
        self._record(value)
        self._after = value
        return ProcessMemoryEvidence(
            metric=self._metric,
            before_bytes=self._before,
            after_bytes=self._after,
            peak_bytes=self._peak,
            sample_count=self._sample_count,
        )

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(0.05):
            metric, value = _process_memory_sample(self._pid)
            if metric == self._metric:
                self._record(value)

    def _record(self, value: int) -> None:
        self._peak = max(self._peak, value)
        self._sample_count += 1


@dataclass(frozen=True)
class QueryPlanProof:
    """Exact SQL, parameters, and SQLite explanation for a public read."""

    sql: str
    params: tuple[object, ...]
    details: tuple[str, ...]


@dataclass(frozen=True)
class AdapterRefresh:
    outcome: str
    refresh_id: str
    terminal_commitment: str
    source_events: int
    fact_changes: int
    document_changes: int
    narrative_changes: int
    source_reads: int
    current_reads: int
    current_writes: int
    receipt_writes: int
    created_count: int = 0
    replayed_count: int = 0
    resume_cursor: str | None = None


@dataclass(frozen=True)
class _RefreshChangeAuditSnapshot:
    refresh_id: str
    mode: str
    logical_changes: int
    declared_bucket_commitments: int
    change_set_entries: int
    digest_buckets_non_empty: bool
    digest_buckets_ordered: bool
    detailed_change_rows: int


@dataclass(frozen=True)
class _ScopeFactSnapshot:
    canonical_metric_cell_scope_index_columns: tuple[str, ...]
    canonical_projection_keyset_index_columns: tuple[str, ...]
    source_scope_query_plan: tuple[str, ...]
    source_scope_query_uses_projection_keyset_index: bool
    source_scope_query_avoids_projection_scan: bool
    authoritative_scope_count: int
    authoritative_issuer_count: int
    authoritative_reporting_entity_count: int
    source_fact_rows: int
    source_reporting_entity_mismatches: int
    minimum_source_facts_per_scope: int
    maximum_source_facts_per_scope: int
    target_source_fact_rows: int
    materialized_scope_count: int
    materialized_fact_rows: int
    target_current_fact_rows: int
    scope_fact_count_mismatches: int
    cross_scope_fact_mismatches: int
    detailed_change_rows: int
    non_target_head_set_sha256: str
    non_target_current_rows_not_bound_to_heads: int


class LatestStateBenchmarkAdapter(Protocol):
    """Narrow contract between the harness and the production implementation."""

    def create_fixture(
        self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
    ) -> FixtureCounts: ...

    def clone_fixture(
        self,
        source: sqlite3.Connection,
        target: sqlite3.Connection,
        *,
        history_multiplier: int,
    ) -> None: ...

    def create_reporting_entity_index(self, conn: sqlite3.Connection) -> None: ...

    def apply_small_delta(
        self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
    ) -> None: ...

    def refresh(
        self,
        conn: sqlite3.Connection,
        *,
        scope_id: str,
        config: LatestStateBenchmarkConfig,
        operation_recorded_at: datetime,
        resume_refresh_id: str | None = None,
        interrupt_after_batches: int | None = None,
    ) -> AdapterRefresh: ...

    def search_facts(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> Sequence[object]: ...

    def search_narrative(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> Sequence[object]: ...

    def fact_query_plan(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> QueryPlanProof: ...

    def narrative_query_plan(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> QueryPlanProof: ...


_BENCHMARK_SCHEMA = """
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
CREATE TABLE source_fact_publication_stream (
  publication_sequence INTEGER, sealed_at TEXT, assigned_at TEXT
);
CREATE TABLE canonical_fact_projection_generations (
  generation_id TEXT PRIMARY KEY, generation_kind TEXT, parent_generation_id TEXT
);
CREATE TABLE canonical_fact_projection_seals (
  generation_id TEXT PRIMARY KEY, projection_seal_sha256 TEXT
);
CREATE TABLE canonical_metric_cells (
  canonical_metric_cell_id TEXT PRIMARY KEY, reporting_entity_id TEXT
);
CREATE TABLE canonical_fact_projection_entries (
  generation_id TEXT, entry_ordinal INTEGER, change_kind TEXT,
  canonical_metric_cell_id TEXT, canonical_resolution_revision_id TEXT,
  selected_observation_id TEXT, canonical_metric_name TEXT, period_kind TEXT,
  period_start TEXT, period_end TEXT, unit_key TEXT, currency TEXT,
  value_kind TEXT, canonical_value TEXT, canonical_search_text TEXT,
  entry_sha256 TEXT, evidence_document_version_id TEXT, evidence_node_id TEXT,
  evidence_locator_json TEXT, evidence_locator_sha256 TEXT,
  source_publication_id TEXT, source_publication_seal_id TEXT,
  source_publication_member_id TEXT, source_fact_cell_id TEXT,
  binding_revision_id TEXT, binding_commitment_sha256 TEXT,
  mapping_revision_id TEXT, mapping_commitment_sha256 TEXT,
  metric_definition_revision_id TEXT, metric_definition_commitment_sha256 TEXT
);
CREATE INDEX ix_benchmark_projection_generation
  ON canonical_fact_projection_entries(generation_id,entry_ordinal);
CREATE INDEX ix_canonical_fact_projection_entry_keyset
  ON canonical_fact_projection_entries(
    generation_id,canonical_metric_cell_id
  );
CREATE TABLE expected_documents (
  expected_document_id TEXT, expected_document_key TEXT, snapshot_id TEXT,
  source_kind TEXT, document_type TEXT, period_start TEXT, period_end TEXT
);
CREATE TABLE evidence_document_versions (
  document_version_id TEXT PRIMARY KEY, blob_sha256 TEXT
);
CREATE TABLE search_corpus_document_memberships (
  manifest_id TEXT, expected_document_key TEXT, document_version_id TEXT,
  membership_status TEXT, reason TEXT
);
CREATE INDEX ix_benchmark_membership_manifest
  ON search_corpus_document_memberships(manifest_id,expected_document_key);
CREATE TABLE evidence_extraction_runs (
  extraction_run_id TEXT PRIMARY KEY, document_version_id TEXT
);
CREATE TABLE evidence_nodes (
  node_id TEXT PRIMARY KEY, extraction_run_id TEXT
);
CREATE TABLE search_chunks (
  chunk_id TEXT PRIMARY KEY, manifest_id TEXT, evidence_node_id TEXT,
  chunk_key TEXT, text TEXT, content_sha256 TEXT, chunker_config_sha256 TEXT
);
CREATE INDEX ix_benchmark_chunk_manifest ON search_chunks(manifest_id,chunk_key);
CREATE TABLE search_embedding_artifacts (
  embedding_artifact_id TEXT, index_run_id TEXT, chunk_id TEXT, outcome TEXT
);
CREATE TABLE latest_governed_refresh_runs (
  refresh_run_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, scope_key TEXT,
  status TEXT, baseline_population_run_id TEXT,
  baseline_population_receipt_sha256 TEXT, baseline_promotion_id TEXT,
  baseline_fact_generation_id TEXT, input_head_sha256 TEXT,
  policy_config_sha256 TEXT, knowledge_cutoff TEXT, observed_through TEXT,
  resume_cursor_json TEXT, resume_cursor_sha256 TEXT,
  staged_change_count INTEGER, applied_change_count INTEGER,
  planned_at TEXT, updated_at TEXT
);
CREATE TABLE latest_governed_refresh_stage (
  refresh_run_id TEXT, stage_ordinal INTEGER, entity_kind TEXT,
  change_kind TEXT, coordinate_key TEXT, digest_bucket INTEGER,
  prior_commitment_sha256 TEXT, current_commitment_sha256 TEXT,
  canonical_payload_json TEXT, payload_sha256 TEXT, stage_status TEXT,
  staged_at TEXT, applied_at TEXT, PRIMARY KEY(refresh_run_id,stage_ordinal),
  UNIQUE(refresh_run_id,entity_kind,coordinate_key)
);
CREATE INDEX ix_benchmark_stage_resume
  ON latest_governed_refresh_stage(refresh_run_id,stage_status,stage_ordinal);
CREATE TABLE latest_governed_refresh_receipts (
  receipt_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
  refresh_run_id TEXT UNIQUE, scope_key TEXT, prior_receipt_id TEXT,
  baseline_population_run_id TEXT, baseline_population_receipt_sha256 TEXT,
  baseline_promotion_id TEXT, fact_generation_id TEXT, input_head_sha256 TEXT,
  prior_state_sha256 TEXT, current_state_sha256 TEXT, fact_root_sha256 TEXT,
  document_root_sha256 TEXT, narrative_root_sha256 TEXT, change_count INTEGER,
  fact_change_count INTEGER, document_change_count INTEGER,
  narrative_change_count INTEGER, canonical_change_set_json TEXT,
  change_set_sha256 TEXT, canonical_receipt_json TEXT, receipt_sha256 TEXT UNIQUE,
  knowledge_cutoff TEXT, observed_through TEXT, sealed_at TEXT
);
CREATE TABLE latest_governed_refresh_changes (
  change_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, receipt_id TEXT,
  change_ordinal INTEGER, entity_kind TEXT, change_kind TEXT,
  coordinate_key TEXT, digest_bucket INTEGER, prior_commitment_sha256 TEXT,
  current_commitment_sha256 TEXT, selection_reason TEXT,
  source_evidence_json TEXT, source_evidence_sha256 TEXT,
  canonical_change_json TEXT, change_sha256 TEXT, knowledge_cutoff TEXT,
  observed_through TEXT, recorded_at TEXT
);
CREATE TABLE latest_governed_scope_heads (
  scope_key TEXT PRIMARY KEY, refresh_receipt_id TEXT UNIQUE,
  population_run_id TEXT, promotion_id TEXT, fact_generation_id TEXT,
  source_heads_json TEXT, source_heads_sha256 TEXT, state_sha256 TEXT,
  fact_root_sha256 TEXT, document_root_sha256 TEXT, narrative_root_sha256 TEXT,
  fact_count INTEGER, document_count INTEGER, narrative_count INTEGER,
  knowledge_cutoff TEXT, observed_through TEXT, updated_at TEXT
);
CREATE TABLE latest_governed_fact_entries (
  scope_key TEXT, canonical_metric_cell_id TEXT, digest_bucket INTEGER,
  refresh_receipt_id TEXT, fact_generation_id TEXT,
  canonical_resolution_revision_id TEXT, selected_observation_id TEXT,
  canonical_metric_name TEXT, period_kind TEXT, period_start TEXT,
  period_end TEXT, unit_key TEXT, currency TEXT, value_kind TEXT,
  canonical_value TEXT, canonical_search_text TEXT, selection_reason TEXT,
  source_evidence_json TEXT, source_evidence_sha256 TEXT,
  prior_commitment_sha256 TEXT, current_commitment_sha256 TEXT,
  knowledge_cutoff TEXT, observed_through TEXT, updated_at TEXT,
  PRIMARY KEY(scope_key,canonical_metric_cell_id)
);
CREATE INDEX ix_latest_governed_fact_search
  ON latest_governed_fact_entries(
    scope_key ASC,canonical_metric_name ASC,period_end DESC,
    canonical_metric_cell_id ASC
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
  digest_bucket INTEGER, refresh_receipt_id TEXT, document_version_id TEXT,
  evidence_node_id TEXT, source_chunk_id TEXT, embedding_artifact_id TEXT,
  text TEXT, content_sha256 TEXT, chunker_config_sha256 TEXT,
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
  ) VALUES(new.rowid,new.scope_key,new.expected_document_key,new.chunk_key,new.text);
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
  ) VALUES(new.rowid,new.scope_key,new.expected_document_key,new.chunk_key,new.text);
END;
"""


class LatestStateSqliteAdapter:
    """Provider-free adapter over the real latest-governed-state public API."""

    def create_fixture(
        self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
    ) -> FixtureCounts:
        conn.executescript(_BENCHMARK_SCHEMA)
        clock = BENCHMARK_STAMP.isoformat()
        baseline_sha = digest_text("benchmark-baseline")
        seal_sha = digest_text("benchmark-generation-0")
        bundle = self._bundle("manifest-0", "vector-0")
        conn.execute(
            "INSERT INTO v_population_cutover_current VALUES (?,?,?,?)",
            ("population-0", baseline_sha, clock, clock),
        )
        conn.executemany(
            "INSERT INTO v_issuer_reporting_scope_current VALUES (?,?,?,?)",
            (
                (
                    f"scope-revision-{scope_index:04d}",
                    "investor-research",
                    f"issuer:{scope_index:04d}",
                    "core",
                )
                for scope_index in range(config.scope_count)
            ),
        )
        conn.executemany(
            "INSERT INTO reporting_entities VALUES (?,?)",
            (
                (
                    f"reporting:{scope_index:04d}",
                    f"issuer:{scope_index:04d}",
                )
                for scope_index in range(config.scope_count)
            ),
        )
        conn.executemany(
            "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?)",
            (
                (
                    f"research-{scope_index:04d}",
                    f"issuer:{scope_index:04d}",
                    canonical_json([f"reporting:{scope_index:04d}"]),
                )
                for scope_index in range(config.scope_count)
            ),
        )
        conn.executemany(
            "INSERT INTO source_inventory_snapshots VALUES (?,?,?)",
            (
                (
                    "inventory-0" if scope_index == 0 else f"inventory-{scope_index:04d}",
                    f"issuer:{scope_index:04d}",
                    "succeeded",
                )
                for scope_index in range(config.scope_count)
            ),
        )
        conn.executemany(
            "INSERT INTO source_inventory_snapshot_seals VALUES (?,?)",
            (
                (
                    "inventory-0" if scope_index == 0 else f"inventory-{scope_index:04d}",
                    "complete",
                )
                for scope_index in range(config.scope_count)
            ),
        )
        conn.executemany(
            "INSERT INTO v_ask_retrieval_scope_current VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    f"promotion-{scope_index:04d}",
                    benchmark_scope_id(scope_index),
                    "promoted",
                    f"research-{scope_index:04d}",
                    "generation-0",
                    seal_sha,
                    canonical_json(
                        ["inventory-0" if scope_index == 0 else f"inventory-{scope_index:04d}"]
                    ),
                    bundle if scope_index == 0 else "[]",
                    clock,
                    "population-0",
                    baseline_sha,
                    clock,
                    f"issuer:{scope_index:04d}",
                    f"reporting:{scope_index:04d}",
                    "investor-research",
                    f"scope-revision-{scope_index:04d}",
                )
                for scope_index in range(config.scope_count)
            ),
        )
        conn.executemany(
            "INSERT INTO source_fact_publication_stream VALUES (?,?,?)",
            (
                (publication_index + 1, clock, clock)
                for publication_index in range(config.publication_count)
            ),
        )
        conn.execute(
            "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
            ("generation-0", "checkpoint", None),
        )
        conn.execute(
            "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
            ("generation-0", seal_sha),
        )
        self._insert_facts(
            conn,
            "generation-0",
            config.cell_count,
            revision=0,
            scope_count=config.scope_count,
        )
        self._insert_documents(
            conn,
            manifest_id="manifest-0",
            inventory_id="inventory-0",
            document_offset=0,
            document_count=config.document_count,
            chunk_count=config.chunk_count,
            revision=0,
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
        if history_multiplier == 1:
            return
        for copy_index in range(1, history_multiplier):
            generation = f"cold-history-{copy_index}"
            target.execute(
                "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
                (generation, "checkpoint", None),
            )
            target.execute(
                "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
                (generation, digest_text(generation)),
            )
            target.execute(
                "INSERT INTO canonical_fact_projection_entries "
                "SELECT ?,entry_ordinal,change_kind,canonical_metric_cell_id,"
                "canonical_resolution_revision_id,selected_observation_id,"
                "canonical_metric_name,period_kind,period_start,period_end,"
                "unit_key,currency,value_kind,canonical_value,canonical_search_text,"
                "entry_sha256,evidence_document_version_id,evidence_node_id,"
                "evidence_locator_json,evidence_locator_sha256,source_publication_id,"
                "source_publication_seal_id,source_publication_member_id,"
                "source_fact_cell_id,binding_revision_id,binding_commitment_sha256,"
                "mapping_revision_id,mapping_commitment_sha256,"
                "metric_definition_revision_id,metric_definition_commitment_sha256 "
                "FROM canonical_fact_projection_entries "
                "WHERE generation_id NOT LIKE 'cold-history-%'",
                (generation,),
            )
            target.execute(
                "INSERT INTO search_corpus_document_memberships "
                "SELECT ?,? || expected_document_key,document_version_id,"
                "membership_status,reason "
                "FROM search_corpus_document_memberships "
                "WHERE manifest_id NOT LIKE 'cold-manifest-%'",
                (f"cold-manifest-{copy_index}", f"cold-{copy_index}:"),
            )
            target.execute(
                "INSERT INTO search_chunks "
                "SELECT ? || chunk_id,?,evidence_node_id,? || chunk_key,text,"
                "content_sha256,chunker_config_sha256 FROM search_chunks "
                "WHERE manifest_id NOT LIKE 'cold-manifest-%'",
                (
                    f"cold-{copy_index}:",
                    f"cold-manifest-{copy_index}",
                    f"cold-{copy_index}:",
                ),
            )
        target.commit()

    def apply_small_delta(
        self, conn: sqlite3.Connection, config: LatestStateBenchmarkConfig
    ) -> None:
        revision = (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM canonical_fact_projection_generations "
                    "WHERE generation_id LIKE 'generation-delta-%'"
                ).fetchone()[0]
            )
            + 1
        )
        parent = str(
            conn.execute(
                "SELECT fact_generation_id FROM latest_governed_scope_heads WHERE scope_key=?",
                (benchmark_scope_id(0),),
            ).fetchone()[0]
        )
        generation = f"generation-delta-{revision}"
        seal_sha = digest_text(generation)
        clock = datetime(2026, 7, 30, 12 + revision, 0, tzinfo=UTC).isoformat()
        population = f"population-delta-{revision}"
        population_sha = digest_text(population)
        manifest = f"manifest-delta-{revision}"
        vector = f"vector-delta-{revision}"
        self._insert_changed_documents(
            conn,
            manifest_id=manifest,
            document_count=config.delta_document_count,
            baseline_document_count=config.document_count,
            baseline_chunk_count=config.chunk_count,
            changed_chunk_count=config.delta_chunk_count,
            revision=revision,
            vector_id=vector,
        )
        previous = conn.execute(
            "SELECT narrative_bundles_json,source_inventory_set_json "
            "FROM v_ask_retrieval_scope_current WHERE scope_key=?",
            (benchmark_scope_id(0),),
        ).fetchone()
        bundles = list(json.loads(self._bundle(manifest, vector)))
        inventories = list(json.loads(str(previous[1])))
        conn.execute(
            "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?)",
            (
                f"research-delta-{revision}",
                "issuer:0000",
                canonical_json(["reporting:0000"]),
            ),
        )
        conn.execute("DELETE FROM v_population_cutover_current")
        conn.execute(
            "INSERT INTO v_population_cutover_current VALUES (?,?,?,?)",
            (population, population_sha, clock, clock),
        )
        conn.execute(
            "UPDATE v_ask_retrieval_scope_current SET "
            "promotion_id=?,research_snapshot_id=?,fact_generation_id=?,"
            "fact_projection_seal_sha256=?,source_inventory_set_json=?,"
            "narrative_bundles_json=?,cutoff_at=?,population_run_id=?,"
            "population_receipt_set_sha256=?,population_observed_through=? "
            "WHERE scope_key=?",
            (
                f"promotion-delta-{revision}",
                f"research-delta-{revision}",
                generation,
                seal_sha,
                canonical_json(inventories),
                canonical_json(bundles),
                clock,
                population,
                population_sha,
                clock,
                benchmark_scope_id(0),
            ),
        )
        maximum_publication = int(
            conn.execute(
                "SELECT COALESCE(MAX(publication_sequence),0) FROM source_fact_publication_stream"
            ).fetchone()[0]
        )
        conn.executemany(
            "INSERT INTO source_fact_publication_stream VALUES (?,?,?)",
            (
                (maximum_publication + index + 1, clock, clock)
                for index in range(config.delta_publication_count)
            ),
        )
        conn.execute(
            "INSERT INTO canonical_fact_projection_generations VALUES (?,?,?)",
            (generation, "delta", parent),
        )
        conn.execute(
            "INSERT INTO canonical_fact_projection_seals VALUES (?,?)",
            (generation, seal_sha),
        )
        self._insert_facts(
            conn,
            generation,
            config.delta_cell_count,
            revision=revision,
            scope_count=config.scope_count,
            reporting_entity_index=0,
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
        from provenance.latest_governed_state import (
            LatestGovernedRefreshRequest,
            refresh_latest_governed_state,
        )

        frontier_time = datetime.fromisoformat(
            str(
                conn.execute(
                    "SELECT observed_through FROM v_population_cutover_current"
                ).fetchone()[0]
            )
        )
        recorded_at = max(operation_recorded_at, frontier_time)
        result = refresh_latest_governed_state(
            conn,
            LatestGovernedRefreshRequest(
                scope_id=scope_id,
                operation_recorded_at=recorded_at,
                policy_version=POLICY_VERSION,
                max_batch_rows=config.max_batch_rows,
                apply=True,
                resume_refresh_id=resume_refresh_id,
                interrupt_after_batches=interrupt_after_batches,
            ),
        )
        return AdapterRefresh(
            outcome=result.outcome,
            refresh_id=result.refresh_id,
            terminal_commitment=result.terminal_commitment,
            source_events=result.source_event_count,
            fact_changes=result.fact_change_count,
            document_changes=result.document_change_count,
            narrative_changes=result.narrative_change_count,
            source_reads=result.source_read_count,
            current_reads=result.current_read_count,
            current_writes=result.current_write_count,
            receipt_writes=result.receipt_write_count,
            created_count=result.created_count,
            replayed_count=result.replayed_count,
            resume_cursor=result.resume_cursor,
        )

    def search_facts(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> Sequence[object]:
        from provenance.latest_governed_state import search_latest_governed_facts

        return search_latest_governed_facts(conn, scope_id, query, limit)

    def search_narrative(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> Sequence[object]:
        from provenance.latest_governed_state import search_latest_governed_narrative

        return search_latest_governed_narrative(conn, scope_id, query, limit)

    def fact_query_plan(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> QueryPlanProof:
        from provenance.latest_governed_state import (
            build_latest_governed_fact_search_query,
        )

        statement = build_latest_governed_fact_search_query(scope_id, query, limit)
        if statement is None:
            raise BenchmarkContractError("fact query-plan probe produced no SQL")
        sql, params = statement
        details = tuple(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + sql,
                params,
            )
        )
        return QueryPlanProof(sql=sql, params=params, details=details)

    def narrative_query_plan(
        self, conn: sqlite3.Connection, *, scope_id: str, query: str, limit: int
    ) -> QueryPlanProof:
        expression = " OR ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in query.split() if token.strip()
        )
        sql = (
            "SELECT entry.*,"
            "bm25(latest_governed_narrative_fts) AS lexical_rank "
            "FROM latest_governed_narrative_fts "
            "JOIN latest_governed_narrative_entries entry "
            "ON entry.rowid=latest_governed_narrative_fts.rowid "
            "WHERE latest_governed_narrative_fts MATCH ? AND entry.scope_key=? "
            "ORDER BY lexical_rank,entry.expected_document_key,entry.chunk_key LIMIT ?"
        )
        params: tuple[object, ...] = (expression, scope_id, limit)
        details = tuple(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + sql,
                params,
            )
        )
        return QueryPlanProof(sql=sql, params=params, details=details)

    @staticmethod
    def _bundle(manifest_id: str, vector_id: str) -> str:
        return canonical_json(
            [
                {
                    "corpus_manifest_id": manifest_id,
                    "lexical_index_run_id": "lexical-" + manifest_id,
                    "vector_index_run_id": vector_id,
                    "embedding_promotion_id": "embedding-" + manifest_id,
                }
            ]
        )

    @staticmethod
    def _insert_facts(
        conn: sqlite3.Connection,
        generation_id: str,
        count: int,
        *,
        revision: int,
        scope_count: int,
        reporting_entity_index: int | None = None,
    ) -> None:
        def coordinates() -> Iterator[tuple[int, int, int]]:
            for entry_ordinal in range(count):
                coordinate_ordinal = (
                    entry_ordinal
                    if reporting_entity_index is None
                    else entry_ordinal * scope_count + reporting_entity_index
                )
                entity_index = (
                    entry_ordinal % scope_count
                    if reporting_entity_index is None
                    else reporting_entity_index
                )
                yield entry_ordinal, coordinate_ordinal, entity_index

        conn.executemany(
            "INSERT OR IGNORE INTO canonical_metric_cells VALUES (?,?)",
            (
                (
                    f"cell-{coordinate_ordinal:09d}",
                    f"reporting:{entity_index:04d}",
                )
                for _, coordinate_ordinal, entity_index in coordinates()
            ),
        )
        conn.executemany(
            "INSERT INTO canonical_fact_projection_entries ("
            "generation_id,entry_ordinal,change_kind,canonical_metric_cell_id,"
            "canonical_resolution_revision_id,selected_observation_id,"
            "canonical_metric_name,period_kind,period_end,unit_key,currency,"
            "value_kind,canonical_value,canonical_search_text,entry_sha256,"
            "evidence_locator_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    generation_id,
                    entry_ordinal,
                    "upsert",
                    f"cell-{coordinate_ordinal:09d}",
                    f"resolution-{revision}-{coordinate_ordinal:09d}",
                    f"observation-{revision}-{coordinate_ordinal:09d}",
                    "revenue",
                    "duration",
                    "2026-06-30",
                    "USD",
                    "USD",
                    "numeric",
                    str(1_000_000 + revision * 10_000 + coordinate_ordinal),
                    f"revenue issuer benchmark cell {coordinate_ordinal:09d}",
                    digest_text(f"fact:{revision}:{coordinate_ordinal}"),
                    "{}",
                )
                for entry_ordinal, coordinate_ordinal, _entity_index in coordinates()
            ),
        )

    @staticmethod
    def _insert_documents(
        conn: sqlite3.Connection,
        *,
        manifest_id: str,
        inventory_id: str,
        document_offset: int,
        document_count: int,
        chunk_count: int,
        revision: int,
    ) -> None:
        if document_count == 0:
            return
        conn.executemany(
            "INSERT INTO expected_documents VALUES (?,?,?,?,?,?,?)",
            (
                (
                    f"expected-{document_offset + ordinal:06d}",
                    f"10-q:{document_offset + ordinal:06d}",
                    inventory_id,
                    "sec_filing",
                    "10-Q",
                    "2026-04-01",
                    "2026-06-30",
                )
                for ordinal in range(document_count)
            ),
        )
        conn.executemany(
            "INSERT INTO evidence_document_versions VALUES (?,?)",
            (
                (
                    f"document-{revision}-{ordinal:06d}",
                    digest_text(f"document:{revision}:{ordinal}"),
                )
                for ordinal in range(document_count)
            ),
        )
        conn.executemany(
            "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?)",
            (
                (
                    manifest_id,
                    f"10-q:{document_offset + ordinal:06d}",
                    f"document-{revision}-{ordinal:06d}",
                    "included",
                    "current",
                )
                for ordinal in range(document_count)
            ),
        )
        conn.executemany(
            "INSERT INTO evidence_extraction_runs VALUES (?,?)",
            (
                (
                    f"extract-{revision}-{ordinal:06d}",
                    f"document-{revision}-{ordinal:06d}",
                )
                for ordinal in range(document_count)
            ),
        )
        conn.executemany(
            "INSERT INTO evidence_nodes VALUES (?,?)",
            (
                (
                    f"node-{revision}-{ordinal:09d}",
                    f"extract-{revision}-{ordinal % document_count:06d}",
                )
                for ordinal in range(chunk_count)
            ),
        )
        conn.executemany(
            "INSERT INTO search_chunks VALUES (?,?,?,?,?,?,?)",
            (
                (
                    f"chunk-{revision}-{ordinal:09d}",
                    manifest_id,
                    f"node-{revision}-{ordinal:09d}",
                    f"chunk:{ordinal:09d}",
                    f"benchmark demand evidence chunk {ordinal:09d}",
                    digest_text(f"chunk-content:{revision}:{ordinal}"),
                    digest_text("benchmark-chunker-v1"),
                )
                for ordinal in range(chunk_count)
            ),
        )
        conn.executemany(
            "INSERT INTO search_embedding_artifacts VALUES (?,?,?,?)",
            (
                (
                    f"embedding-{revision}-{ordinal:09d}",
                    f"vector-{revision}",
                    f"chunk-{revision}-{ordinal:09d}",
                    "succeeded",
                )
                for ordinal in range(chunk_count)
            ),
        )

    @staticmethod
    def _insert_changed_documents(
        conn: sqlite3.Connection,
        *,
        manifest_id: str,
        document_count: int,
        baseline_document_count: int,
        baseline_chunk_count: int,
        changed_chunk_count: int,
        revision: int,
        vector_id: str,
    ) -> None:
        changed_ordinals = [
            ordinal
            for ordinal in range(baseline_chunk_count)
            if ordinal % baseline_document_count < document_count
        ]
        if len(changed_ordinals) != changed_chunk_count:
            raise BenchmarkContractError(
                "delta chunk count must match the baseline chunks owned by changed documents"
            )
        conn.executemany(
            "INSERT INTO evidence_document_versions VALUES (?,?)",
            (
                (
                    f"document-{revision}-{ordinal:06d}",
                    digest_text(f"document:{revision}:{ordinal}"),
                )
                for ordinal in range(document_count)
            ),
        )
        conn.executemany(
            "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?)",
            (
                (
                    manifest_id,
                    f"10-q:{ordinal:06d}",
                    f"document-{revision}-{ordinal:06d}",
                    "included",
                    "changed-current",
                )
                for ordinal in range(document_count)
            ),
        )
        conn.executemany(
            "INSERT INTO search_corpus_document_memberships VALUES (?,?,?,?,?)",
            (
                (
                    manifest_id,
                    f"10-q:{ordinal:06d}",
                    f"document-0-{ordinal:06d}",
                    "included",
                    "unchanged-current",
                )
                for ordinal in range(document_count, baseline_document_count)
            ),
        )
        conn.executemany(
            "INSERT INTO evidence_extraction_runs VALUES (?,?)",
            (
                (
                    f"extract-{revision}-{ordinal:06d}",
                    f"document-{revision}-{ordinal:06d}",
                )
                for ordinal in range(document_count)
            ),
        )
        conn.executemany(
            "INSERT INTO evidence_nodes VALUES (?,?)",
            (
                (
                    f"node-{revision}-{chunk_ordinal:09d}",
                    f"extract-{revision}-{chunk_ordinal % baseline_document_count:06d}",
                )
                for chunk_ordinal in changed_ordinals
            ),
        )
        conn.executemany(
            "INSERT INTO search_chunks VALUES (?,?,?,?,?,?,?)",
            (
                (
                    f"chunk-{revision}-{chunk_ordinal:09d}",
                    manifest_id,
                    f"node-{revision}-{chunk_ordinal:09d}",
                    f"chunk:{chunk_ordinal:09d}",
                    f"benchmark changed demand evidence {chunk_ordinal:09d}",
                    digest_text(f"chunk-content:{revision}:{chunk_ordinal}"),
                    digest_text("benchmark-chunker-v1"),
                )
                for chunk_ordinal in changed_ordinals
            ),
        )
        conn.executemany(
            "INSERT INTO search_embedding_artifacts VALUES (?,?,?,?)",
            (
                (
                    f"embedding-{revision}-{chunk_ordinal:09d}",
                    vector_id,
                    f"chunk-{revision}-{chunk_ordinal:09d}",
                    "succeeded",
                )
                for chunk_ordinal in changed_ordinals
            ),
        )


def allocated_sqlite_pages(conn: sqlite3.Connection) -> int:
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return page_count - freelist_count


@dataclass(frozen=True)
class _StorageCheckpoint:
    page_size_bytes: int
    allocated_pages: int
    database_bytes: int


def _storage_checkpoint(conn: sqlite3.Connection, database_path: Path) -> _StorageCheckpoint:
    return _StorageCheckpoint(
        page_size_bytes=int(conn.execute("PRAGMA page_size").fetchone()[0]),
        allocated_pages=allocated_sqlite_pages(conn),
        database_bytes=database_path.stat().st_size,
    )


def _storage_evidence(
    source_fixture: _StorageCheckpoint,
    reporting_entity_index: _StorageCheckpoint,
    total: _StorageCheckpoint,
) -> StorageEvidence:
    if not (
        source_fixture.page_size_bytes
        == reporting_entity_index.page_size_bytes
        == total.page_size_bytes
    ):
        raise BenchmarkContractError("SQLite page size changed during benchmark")
    if not (
        source_fixture.allocated_pages
        <= reporting_entity_index.allocated_pages
        <= total.allocated_pages
    ):
        raise BenchmarkContractError("allocated SQLite pages regressed after fixture")
    if not (
        source_fixture.database_bytes
        <= reporting_entity_index.database_bytes
        <= total.database_bytes
    ):
        raise BenchmarkContractError("SQLite database bytes regressed after fixture")
    return StorageEvidence(
        page_size_bytes=total.page_size_bytes,
        source_fixture_allocated_pages=source_fixture.allocated_pages,
        reporting_entity_index_allocated_pages=(
            reporting_entity_index.allocated_pages - source_fixture.allocated_pages
        ),
        latest_state_materialization_allocated_pages=(
            total.allocated_pages - reporting_entity_index.allocated_pages
        ),
        latest_state_incremental_allocated_pages=(
            total.allocated_pages - source_fixture.allocated_pages
        ),
        total_allocated_pages=total.allocated_pages,
        source_fixture_database_bytes=source_fixture.database_bytes,
        reporting_entity_index_database_bytes=(
            reporting_entity_index.database_bytes - source_fixture.database_bytes
        ),
        latest_state_materialization_database_bytes=(
            total.database_bytes - reporting_entity_index.database_bytes
        ),
        latest_state_incremental_database_bytes=(
            total.database_bytes - source_fixture.database_bytes
        ),
        total_database_bytes=total.database_bytes,
    )


def percentile(samples: Sequence[float], percentile_value: float) -> float:
    if not samples:
        raise ValueError("percentile requires at least one sample")
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile_value) - 1))
    return ordered[index]


def _validate_new_path(path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    live = (Path(__file__).resolve().parents[2] / "data" / "portfolio.db").resolve()
    if resolved == live:
        raise RefusedBenchmarkPathError(f"{label} cannot be the live portfolio database")
    if resolved.exists():
        raise RefusedBenchmarkPathError(f"{label} must not already exist: {resolved}")
    if resolved.parent.exists() and not resolved.parent.is_dir():
        raise RefusedBenchmarkPathError(f"{label} parent is not a directory: {resolved.parent}")
    return resolved


def preflight_benchmark_paths(database_path: Path, report_path: Path) -> tuple[Path, Path]:
    """Refuse live, existing, or aliased benchmark paths before fixture work."""

    database = _validate_new_path(database_path, label="benchmark database")
    report = _validate_new_path(report_path, label="benchmark report")
    if database == report:
        raise RefusedBenchmarkPathError("benchmark database and report paths must differ")
    return database, report


def _current_historical_rows(conn: sqlite3.Connection) -> CurrentHistoricalRows:
    def count(table: str) -> int:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # nosec B608
        return int(row[0])

    return CurrentHistoricalRows(
        current_facts=count("latest_governed_fact_entries"),
        current_documents=count("latest_governed_document_entries"),
        current_narrative_chunks=count("latest_governed_narrative_entries"),
        refresh_receipts=count("latest_governed_refresh_receipts"),
        refresh_changes=count("latest_governed_refresh_changes"),
        staged_rows=count("latest_governed_refresh_stage"),
        retained_fact_rows=count("canonical_fact_projection_entries"),
        retained_document_rows=count("search_corpus_document_memberships"),
        retained_narrative_rows=count("search_chunks"),
    )


def _refresh_change_audit_snapshot(
    conn: sqlite3.Connection,
    *,
    refresh_id: str,
) -> _RefreshChangeAuditSnapshot:
    row = conn.execute(
        "SELECT receipt_id,change_count,canonical_change_set_json,"
        "canonical_receipt_json FROM latest_governed_refresh_receipts "
        "WHERE refresh_run_id=?",
        (refresh_id,),
    ).fetchone()
    if row is None:
        raise BenchmarkContractError("refresh audit receipt is missing")
    change_set_raw = json.loads(str(row[2]))
    receipt_raw = json.loads(str(row[3]))
    if not isinstance(change_set_raw, list) or not isinstance(receipt_raw, dict):
        raise BenchmarkContractError("refresh audit payload has an invalid JSON shape")
    change_set = cast(list[object], change_set_raw)
    receipt = cast(dict[str, object], receipt_raw)
    audit_raw = receipt.get("change_audit")
    if not isinstance(audit_raw, dict):
        raise BenchmarkContractError("refresh receipt change_audit metadata is missing")
    audit = cast(dict[str, object], audit_raw)
    mode = audit.get("mode")
    logical_changes = audit.get("change_count")
    declared_buckets = audit.get("bucket_count")
    if (
        not isinstance(mode, str)
        or not isinstance(logical_changes, int)
        or isinstance(logical_changes, bool)
        or not isinstance(declared_buckets, int)
        or isinstance(declared_buckets, bool)
    ):
        raise BenchmarkContractError("refresh receipt change_audit metadata is invalid")
    if int(row[1]) != logical_changes:
        raise BenchmarkContractError("refresh audit logical change count disagrees with receipt")

    buckets: list[int] = []
    non_empty = True
    if mode == "baseline_digest_buckets.v1":
        for raw_entry in change_set:
            if not isinstance(raw_entry, dict):
                raise BenchmarkContractError("baseline digest bucket entry is not an object")
            entry = cast(dict[str, object], raw_entry)
            bucket = entry.get("digest_bucket")
            count = entry.get("change_count")
            commitment = entry.get("commitment_sha256")
            if (
                not isinstance(bucket, int)
                or isinstance(bucket, bool)
                or not 0 <= bucket < MAX_INITIAL_CHECKPOINT_DIGEST_BUCKETS
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count <= 0
                or not isinstance(commitment, str)
                or len(commitment) != 64
            ):
                raise BenchmarkContractError("baseline digest bucket entry is invalid")
            buckets.append(bucket)
            non_empty = non_empty and count > 0
    elif mode == "coordinate_changes.v1":
        if not all(isinstance(item, str) and len(item) == 64 for item in change_set):
            raise BenchmarkContractError("coordinate change commitment is invalid")
    else:
        raise BenchmarkContractError("refresh receipt change_audit mode is unsupported")

    detailed_rows = int(
        conn.execute(
            "SELECT COUNT(*) FROM latest_governed_refresh_changes WHERE receipt_id=?",
            (str(row[0]),),
        ).fetchone()[0]
    )
    return _RefreshChangeAuditSnapshot(
        refresh_id=refresh_id,
        mode=mode,
        logical_changes=logical_changes,
        declared_bucket_commitments=declared_buckets,
        change_set_entries=len(change_set),
        digest_buckets_non_empty=non_empty,
        digest_buckets_ordered=buckets == sorted(set(buckets)),
        detailed_change_rows=detailed_rows,
    )


def _scope_fact_snapshot(
    conn: sqlite3.Connection,
    *,
    target_scope_id: str,
) -> _ScopeFactSnapshot:
    bindings = tuple(
        (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            "SELECT scope_key,issuer_id,reporting_entity_id "
            "FROM v_ask_retrieval_scope_current ORDER BY scope_key"
        )
    )
    source_scope_sql = (
        "SELECT scope.scope_key,COUNT(entry.canonical_metric_cell_id) "
        "FROM v_ask_retrieval_scope_current scope "
        "LEFT JOIN canonical_metric_cells cell "
        "ON cell.reporting_entity_id=scope.reporting_entity_id "
        "LEFT JOIN canonical_fact_projection_entries entry "
        "ON entry.generation_id='generation-0' "
        "AND entry.canonical_metric_cell_id=cell.canonical_metric_cell_id "
        "GROUP BY scope.scope_key ORDER BY scope.scope_key"
    )
    source_scope_query_plan = tuple(
        str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + source_scope_sql)
    )
    source_scope_query_plan_text = " ".join(source_scope_query_plan).lower()
    source_counts = {str(row[0]): int(row[1]) for row in conn.execute(source_scope_sql)}
    current_counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT scope.scope_key,COUNT(current.canonical_metric_cell_id) "
            "FROM v_ask_retrieval_scope_current scope "
            "LEFT JOIN latest_governed_fact_entries current "
            "ON current.scope_key=scope.scope_key "
            "GROUP BY scope.scope_key ORDER BY scope.scope_key"
        )
    }
    if not bindings or set(source_counts) != set(current_counts):
        raise BenchmarkContractError("scope fact evidence is incomplete")
    source_values = tuple(source_counts.values())
    source_fact_rows = sum(source_values)
    current_fact_rows = sum(current_counts.values())
    non_target_heads = tuple(
        (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            "SELECT scope_key,refresh_receipt_id,state_sha256 "
            "FROM latest_governed_scope_heads WHERE scope_key<>? ORDER BY scope_key",
            (target_scope_id,),
        )
    )
    source_reporting_mismatches = int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_fact_projection_entries entry "
            "LEFT JOIN canonical_metric_cells cell "
            "ON cell.canonical_metric_cell_id=entry.canonical_metric_cell_id "
            "LEFT JOIN v_ask_retrieval_scope_current scope "
            "ON scope.reporting_entity_id=cell.reporting_entity_id "
            "WHERE entry.generation_id='generation-0' "
            "AND (cell.canonical_metric_cell_id IS NULL "
            "OR scope.scope_key IS NULL)"
        ).fetchone()[0]
    )
    cross_scope_mismatches = int(
        conn.execute(
            "SELECT COUNT(*) FROM latest_governed_fact_entries current "
            "JOIN v_ask_retrieval_scope_current scope "
            "ON scope.scope_key=current.scope_key "
            "LEFT JOIN canonical_metric_cells cell "
            "ON cell.canonical_metric_cell_id=current.canonical_metric_cell_id "
            "WHERE cell.canonical_metric_cell_id IS NULL "
            "OR cell.reporting_entity_id IS NOT scope.reporting_entity_id"
        ).fetchone()[0]
    )
    unbound_non_target_rows = int(
        conn.execute(
            "SELECT COUNT(*) FROM latest_governed_fact_entries current "
            "JOIN latest_governed_scope_heads head "
            "ON head.scope_key=current.scope_key "
            "WHERE current.scope_key<>? "
            "AND current.refresh_receipt_id IS NOT head.refresh_receipt_id",
            (target_scope_id,),
        ).fetchone()[0]
    )
    return _ScopeFactSnapshot(
        canonical_metric_cell_scope_index_columns=tuple(
            str(row[2])
            for row in conn.execute(
                "PRAGMA index_info('ix_canonical_metric_cells_reporting_entity')"
            )
        ),
        canonical_projection_keyset_index_columns=tuple(
            str(row[2])
            for row in conn.execute(
                "PRAGMA index_info('ix_canonical_fact_projection_entry_keyset')"
            )
        ),
        source_scope_query_plan=source_scope_query_plan,
        source_scope_query_uses_projection_keyset_index=any(
            "ix_canonical_fact_projection_entry_keyset" in detail
            for detail in source_scope_query_plan
        ),
        source_scope_query_avoids_projection_scan=(
            "scan entry" not in source_scope_query_plan_text
            and "scan canonical_fact_projection_entries" not in source_scope_query_plan_text
        ),
        authoritative_scope_count=len({item[0] for item in bindings}),
        authoritative_issuer_count=len({item[1] for item in bindings}),
        authoritative_reporting_entity_count=len({item[2] for item in bindings}),
        source_fact_rows=source_fact_rows,
        source_reporting_entity_mismatches=source_reporting_mismatches,
        minimum_source_facts_per_scope=min(source_values),
        maximum_source_facts_per_scope=max(source_values),
        target_source_fact_rows=source_counts[target_scope_id],
        materialized_scope_count=sum(value > 0 for value in current_counts.values()),
        materialized_fact_rows=current_fact_rows,
        target_current_fact_rows=current_counts[target_scope_id],
        scope_fact_count_mismatches=sum(
            current_counts[scope_id] != source_count
            for scope_id, source_count in source_counts.items()
        ),
        cross_scope_fact_mismatches=cross_scope_mismatches,
        detailed_change_rows=int(
            conn.execute("SELECT COUNT(*) FROM latest_governed_refresh_changes").fetchone()[0]
        ),
        non_target_head_set_sha256=digest_text(canonical_json(non_target_heads)),
        non_target_current_rows_not_bound_to_heads=unbound_non_target_rows,
    )


def _source_publication_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM source_fact_publication_stream").fetchone()[0])


def _measure_refresh(
    conn: sqlite3.Connection,
    operation: Callable[[], AdapterRefresh],
    *,
    independent_source_publications: int,
) -> RefreshMeasurement:
    pages_before = allocated_sqlite_pages(conn)
    total_changes_before = conn.total_changes
    progress_callbacks = 0

    def count_progress() -> int:
        nonlocal progress_callbacks
        progress_callbacks += 1
        return 0

    conn.set_progress_handler(count_progress, SQLITE_PROGRESS_INTERVAL)
    started = time.perf_counter()
    try:
        result = operation()
        wall_ms = (time.perf_counter() - started) * 1_000.0
    finally:
        conn.set_progress_handler(None, 0)
    return RefreshMeasurement(
        outcome=result.outcome,
        wall_milliseconds=wall_ms,
        terminal_commitment=result.terminal_commitment,
        refresh_id=result.refresh_id,
        work=RefreshWorkVector(
            source_events=result.source_events,
            independent_source_publications=independent_source_publications,
            fact_changes=result.fact_changes,
            document_changes=result.document_changes,
            narrative_changes=result.narrative_changes,
            source_reads=result.source_reads,
            current_reads=result.current_reads,
            current_writes=result.current_writes,
            receipt_writes=result.receipt_writes,
            total_changes=conn.total_changes - total_changes_before,
            sqlite_vm_step_proxy=progress_callbacks * SQLITE_PROGRESS_INTERVAL,
            allocated_pages_before=pages_before,
            allocated_pages_after=allocated_sqlite_pages(conn),
        ),
    )


def _measure_reads(
    *,
    samples: int,
    limit: int,
    operation: Callable[[], Sequence[object]],
    plan: QueryPlanProof,
    require_bounded_fact_index: bool = False,
) -> ReadMeasurement:
    timings: list[float] = []
    maximum_rows = 0
    for _ in range(samples):
        started = time.perf_counter()
        rows = operation()
        timings.append((time.perf_counter() - started) * 1_000.0)
        maximum_rows = max(maximum_rows, len(rows))
    normalized_plan = tuple(str(item) for item in plan.details)
    plan_text = " ".join(normalized_plan).lower()
    current_only = not any(token in plan_text for token in _FORBIDDEN_PLAN_TOKENS)
    avoids_full_scope_scan = not require_bounded_fact_index or (
        "scan latest_governed_fact_entries" not in plan_text
        and "ix_latest_governed_fact_search" in plan_text
        and "canonical_metric_name" in plan_text
    )
    avoids_temporary_sort = "temp b-tree" not in plan_text
    return ReadMeasurement(
        sample_count=samples,
        limit=limit,
        maximum_rows_fetched=maximum_rows,
        p50_milliseconds=percentile(timings, 0.50),
        p95_milliseconds=percentile(timings, 0.95),
        query_sql=plan.sql,
        query_parameters_sha256=digest_text(canonical_json(plan.params)),
        query_plan=normalized_plan,
        uses_current_projection_only=current_only,
        avoids_full_current_scope_scan=avoids_full_scope_scan,
        avoids_temporary_sort=avoids_temporary_sort,
    )


def _ratchets(
    config: LatestStateBenchmarkConfig,
    no_op: RefreshMeasurement,
    delta: RefreshMeasurement,
    fact_read: ReadMeasurement,
    narrative_read: ReadMeasurement,
    rows: CurrentHistoricalRows,
    change_audit: ChangeAuditEvidence,
    cross_scope: CrossScopeIsolationEvidence,
    storage: StorageEvidence,
    amplification: WriteAmplificationEvidence,
    resume: ResumeEvidence,
    history: HistoryIndependenceEvidence,
) -> tuple[RatchetResult, ...]:
    expected_delta_changes = (
        config.delta_cell_count + config.delta_document_count + config.delta_chunk_count
    )
    checks = (
        (
            "no_op_exact_work",
            no_op.work.source_events == 0
            and no_op.work.independent_source_publications == 0
            and no_op.work.fact_changes == 0
            and no_op.work.document_changes == 0
            and no_op.work.narrative_changes == 0
            and no_op.work.source_reads == 0
            and no_op.work.current_reads == 1
            and no_op.work.current_writes == 0
            and no_op.work.receipt_writes == 1,
            canonical_json(no_op.work.model_dump(mode="json")),
        ),
        (
            "small_delta_exact_work",
            delta.work.independent_source_publications == config.delta_publication_count
            and delta.work.source_events == delta.work.independent_source_publications
            and delta.work.fact_changes == config.delta_cell_count
            and delta.work.document_changes == config.delta_document_count
            and delta.work.narrative_changes == config.delta_chunk_count
            and delta.work.source_reads == expected_delta_changes
            and delta.work.current_reads == expected_delta_changes
            and delta.work.current_writes == expected_delta_changes
            and delta.work.receipt_writes == 1,
            canonical_json(delta.work.model_dump(mode="json")),
        ),
        (
            "default_fact_read_current_only",
            fact_read.uses_current_projection_only
            and fact_read.avoids_full_current_scope_scan
            and fact_read.avoids_temporary_sort
            and fact_read.maximum_rows_fetched <= fact_read.limit,
            canonical_json(fact_read.model_dump(mode="json")),
        ),
        (
            "default_narrative_read_current_only",
            narrative_read.uses_current_projection_only
            and narrative_read.maximum_rows_fetched <= narrative_read.limit,
            canonical_json(narrative_read.model_dump(mode="json")),
        ),
        (
            "resume_equivalence_without_stage_rewrite",
            # Finalization may compact mutable stage rows.  Replayed counters
            # and duplicate checks prove reuse without mistaking deletion for
            # a rewrite.
            resume.equivalent
            and resume.ordered_stage_identity_payloads_equal
            and resume.staged_rows_rewritten == 0
            and resume.replayed_rows >= resume.staged_rows_before_resume
            and resume.duplicate_rows_after_resume == 0,
            canonical_json(resume.model_dump(mode="json")),
        ),
        (
            "finalized_stage_is_compacted",
            rows.staged_rows == 0,
            canonical_json(rows.model_dump(mode="json")),
        ),
        (
            "initial_checkpoint_bounded_change_audit",
            change_audit.baseline_logical_changes
            == (cross_scope.target_source_fact_rows + config.document_count + config.chunk_count)
            and change_audit.baseline_declared_digest_bucket_commitments
            == change_audit.baseline_digest_bucket_commitments
            and 0
            < change_audit.baseline_digest_bucket_commitments
            <= MAX_INITIAL_CHECKPOINT_DIGEST_BUCKETS
            and change_audit.baseline_digest_buckets_non_empty
            and change_audit.baseline_digest_buckets_ordered
            and change_audit.baseline_detailed_change_rows == 0,
            canonical_json(change_audit.model_dump(mode="json")),
        ),
        (
            "multi_scope_exact_initial_materialization",
            cross_scope.canonical_metric_cell_scope_index_columns
            == ("reporting_entity_id", "canonical_metric_cell_id")
            and cross_scope.authoritative_scope_count == config.scope_count
            and cross_scope.authoritative_issuer_count == config.scope_count
            and cross_scope.authoritative_reporting_entity_count == config.scope_count
            and cross_scope.source_fact_rows == config.cell_count
            and cross_scope.source_reporting_entity_mismatches == 0
            and cross_scope.maximum_source_facts_per_scope
            - cross_scope.minimum_source_facts_per_scope
            <= 1
            and cross_scope.materialized_scope_count_before_delta == config.scope_count
            and cross_scope.materialized_fact_rows_before_delta == config.cell_count
            and cross_scope.target_current_fact_rows_before_delta
            == cross_scope.target_source_fact_rows
            and cross_scope.scope_fact_count_mismatches_before_delta == 0
            and cross_scope.cross_scope_fact_mismatches_before_delta == 0
            and cross_scope.detailed_change_rows_before_delta == 0,
            canonical_json(cross_scope.model_dump(mode="json")),
        ),
        (
            "projection_keyset_index_bounds_cross_scope_evidence",
            cross_scope.canonical_projection_keyset_index_columns
            == ("generation_id", "canonical_metric_cell_id")
            and cross_scope.source_scope_query_uses_projection_keyset_index
            and cross_scope.source_scope_query_avoids_projection_scan,
            canonical_json(
                {
                    "index_columns": (cross_scope.canonical_projection_keyset_index_columns),
                    "query_plan": cross_scope.source_scope_query_plan,
                }
            ),
        ),
        (
            "small_delta_cross_scope_isolation",
            cross_scope.materialized_scope_count_after_delta == config.scope_count
            and cross_scope.materialized_fact_rows_after_delta == config.cell_count
            and rows.current_facts == config.cell_count
            and cross_scope.target_current_fact_rows_after_delta
            == cross_scope.target_source_fact_rows
            and cross_scope.scope_fact_count_mismatches_after_delta == 0
            and cross_scope.cross_scope_fact_mismatches_after_delta == 0
            and cross_scope.detailed_change_rows_after_delta == expected_delta_changes
            and cross_scope.non_target_heads_unchanged
            and cross_scope.non_target_current_rows_bound_to_heads,
            canonical_json(cross_scope.model_dump(mode="json")),
        ),
        (
            "reporting_entity_index_storage_is_incremental",
            storage.reporting_entity_index_allocated_pages > 0
            and storage.latest_state_incremental_allocated_pages
            == (
                storage.reporting_entity_index_allocated_pages
                + storage.latest_state_materialization_allocated_pages
            )
            and storage.latest_state_incremental_database_bytes
            == (
                storage.reporting_entity_index_database_bytes
                + storage.latest_state_materialization_database_bytes
            )
            and storage.total_allocated_pages
            == (
                storage.source_fixture_allocated_pages
                + storage.latest_state_incremental_allocated_pages
            )
            and storage.total_database_bytes
            == (
                storage.source_fixture_database_bytes
                + storage.latest_state_incremental_database_bytes
            ),
            canonical_json(storage.model_dump(mode="json")),
        ),
        (
            "small_delta_exact_change_audit",
            change_audit.delta_logical_changes == expected_delta_changes
            and change_audit.delta_coordinate_change_commitments == expected_delta_changes
            and change_audit.delta_detailed_change_rows == expected_delta_changes
            and rows.refresh_changes == expected_delta_changes,
            canonical_json(
                {
                    "change_audit": change_audit.model_dump(mode="json"),
                    "expected_delta_changes": expected_delta_changes,
                    "retained_detailed_change_rows": rows.refresh_changes,
                }
            ),
        ),
        (
            "bounded_write_amplification",
            amplification.no_op_physical_total_changes <= 3
            and amplification.small_delta_amplification_ratio
            <= MAX_SMALL_DELTA_WRITE_AMPLIFICATION,
            canonical_json(amplification.model_dump(mode="json")),
        ),
        (
            "small_delta_bounded_sqlite_vm_work",
            delta.work.sqlite_vm_step_proxy > 0
            and delta.work.sqlite_vm_step_proxy
            <= no_op.work.sqlite_vm_step_proxy
            + expected_delta_changes * MAX_SMALL_DELTA_VM_STEPS_PER_CHANGE,
            canonical_json(
                {
                    "no_op_sqlite_vm_step_proxy": no_op.work.sqlite_vm_step_proxy,
                    "small_delta_sqlite_vm_step_proxy": delta.work.sqlite_vm_step_proxy,
                    "maximum_increment_per_change": (MAX_SMALL_DELTA_VM_STEPS_PER_CHANGE),
                    "expected_delta_changes": expected_delta_changes,
                }
            ),
        ),
        (
            "history_independent_exact_work",
            history.equivalent
            and _semantic_work(history.one_x_work) == _semantic_work(history.four_x_work)
            and history.sqlite_vm_step_ratio <= MAX_RETAINED_HISTORY_VM_STEP_RATIO,
            canonical_json(history.model_dump(mode="json")),
        ),
    )
    return tuple(
        RatchetResult(name=name, passed=passed, detail=detail) for name, passed, detail in checks
    )


def _semantic_work(work: RefreshWorkVector) -> tuple[int, ...]:
    """Exclude physical page allocation while comparing logical SQL work."""

    return (
        work.source_events,
        work.independent_source_publications,
        work.fact_changes,
        work.document_changes,
        work.narrative_changes,
        work.source_reads,
        work.current_reads,
        work.current_writes,
        work.receipt_writes,
        work.total_changes,
    )


def _budget_results(
    budgets: LatestStateBenchmarkBudgets,
    *,
    hot_path_wall_seconds: float,
    peak_memory: int,
    no_op: RefreshMeasurement,
    delta: RefreshMeasurement,
    storage: StorageEvidence,
    fact_read: ReadMeasurement,
    narrative_read: ReadMeasurement,
    history: HistoryIndependenceEvidence,
) -> tuple[BudgetResult, ...]:
    values = (
        ("hot_path_seconds", hot_path_wall_seconds, budgets.max_hot_path_seconds),
        (
            "hot_path_peak_python_memory_bytes",
            float(peak_memory),
            float(budgets.max_peak_python_memory_bytes),
        ),
        (
            "latest_state_incremental_allocated_pages",
            float(storage.latest_state_incremental_allocated_pages),
            float(budgets.max_allocated_sqlite_pages),
        ),
        ("no_op_milliseconds", no_op.wall_milliseconds, budgets.max_noop_milliseconds),
        (
            "small_delta_milliseconds",
            delta.wall_milliseconds,
            budgets.max_small_delta_milliseconds,
        ),
        (
            "fact_read_p95_milliseconds",
            fact_read.p95_milliseconds,
            budgets.max_fact_read_p95_milliseconds,
        ),
        (
            "narrative_read_p95_milliseconds",
            narrative_read.p95_milliseconds,
            budgets.max_narrative_read_p95_milliseconds,
        ),
        (
            "history_latency_ratio",
            history.latency_ratio,
            budgets.max_history_latency_ratio,
        ),
    )
    return tuple(
        BudgetResult(name=name, actual=actual, maximum=maximum, passed=actual <= maximum)
        for name, actual, maximum in values
    )


def _report_sha256(report: LatestStateBenchmarkReport) -> str:
    payload = report.model_dump(mode="json")
    payload["report_sha256"] = "0" * 64
    return digest_text(canonical_json(payload))


def _implementation_provenance() -> ImplementationProvenance:
    files = tuple(
        ImplementationFileDigest(
            project_relative_path=relative_path,
            sha256=hashlib.sha256((_PROJECT_ROOT / relative_path).read_bytes()).hexdigest(),
        )
        for relative_path in _IMPLEMENTATION_RELATIVE_PATHS
    )
    return ImplementationProvenance(
        digest_algorithm="sha256",
        files=files,
        source_set_sha256=digest_text(
            canonical_json([item.model_dump(mode="json") for item in files])
        ),
    )


def verify_report_sha256(report: LatestStateBenchmarkReport) -> bool:
    return report.report_sha256 == _report_sha256(report)


def production_benchmark_config() -> LatestStateBenchmarkConfig:
    """Return the production-representative 2026-07-30 corpus profile.

    The fact/publication/scope dimensions match the measured production-derived
    inventory.  The currently admitted narrative blocker inventory contains 24
    documents; eight chunks per document is a documented conservative chunk
    factor until the governed corpus is populated.
    """

    return LatestStateBenchmarkConfig(
        profile="production",
        publication_count=1_284,
        cell_count=831_471,
        document_count=24,
        chunk_count=24 * 8,
        scope_count=87,
        delta_publication_count=1,
        delta_cell_count=8,
        delta_document_count=2,
        delta_chunk_count=16,
        max_batch_rows=1_000,
        read_samples=40,
        read_limit=20,
        interrupt_after_batches=1,
    )


def production_benchmark_budgets() -> LatestStateBenchmarkBudgets:
    """Return the explicit hot-path budget for the large production profile."""

    return LatestStateBenchmarkBudgets(max_hot_path_seconds=900.0)


def run_latest_state_benchmark(
    *,
    config: LatestStateBenchmarkConfig,
    budgets: LatestStateBenchmarkBudgets,
    database_path: Path,
    adapter: LatestStateBenchmarkAdapter,
) -> LatestStateBenchmarkReport:
    """Run the benchmark in new isolated SQLite files owned by the caller."""

    database = _validate_new_path(database_path, label="benchmark database")
    database.parent.mkdir(parents=True, exist_ok=True)
    command_started = time.perf_counter()
    conn = connect_sqlite(
        database,
        role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
    )
    tracing_started = False
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        fixture_started = time.perf_counter()
        baseline_memory_sampler = _ProcessMemorySampler(os.getpid())
        baseline_memory_sampler.start()
        try:
            fixture = adapter.create_fixture(conn, config)
            source_fixture_storage = _storage_checkpoint(conn, database)
            adapter.create_reporting_entity_index(conn)
            reporting_entity_index_storage = _storage_checkpoint(conn, database)
            scope_id = benchmark_scope_id(0)
            initial_checkpoint: AdapterRefresh | None = None
            for scope_index in range(config.scope_count):
                candidate = adapter.refresh(
                    conn,
                    scope_id=benchmark_scope_id(scope_index),
                    config=config,
                    operation_recorded_at=BENCHMARK_STAMP,
                )
                if scope_index == 0:
                    initial_checkpoint = candidate
            if initial_checkpoint is None:
                raise BenchmarkContractError("target initial checkpoint is missing")
            baseline_audit = _refresh_change_audit_snapshot(
                conn,
                refresh_id=initial_checkpoint.refresh_id,
            )
            cross_scope_before_delta = _scope_fact_snapshot(
                conn,
                target_scope_id=scope_id,
            )
            gc.collect()
        finally:
            cold_baseline_memory = baseline_memory_sampler.stop()
        fixture_prep_wall = time.perf_counter() - fixture_started
        tracemalloc.start()
        tracing_started = True
        hot_path_started = time.perf_counter()
        no_op = _measure_refresh(
            conn,
            lambda: adapter.refresh(
                conn,
                scope_id=scope_id,
                config=config,
                operation_recorded_at=BENCHMARK_STAMP,
            ),
            independent_source_publications=0,
        )
        publications_before_delta = _source_publication_count(conn)
        adapter.apply_small_delta(conn, config)
        independent_delta_publications = _source_publication_count(conn) - publications_before_delta
        delta = _measure_refresh(
            conn,
            lambda: adapter.refresh(
                conn,
                scope_id=scope_id,
                config=config,
                operation_recorded_at=BENCHMARK_STAMP,
            ),
            independent_source_publications=independent_delta_publications,
        )
        delta_audit = _refresh_change_audit_snapshot(
            conn,
            refresh_id=delta.refresh_id,
        )
        cross_scope_after_delta = _scope_fact_snapshot(
            conn,
            target_scope_id=scope_id,
        )
        storage = _storage_evidence(
            source_fixture_storage,
            reporting_entity_index_storage,
            _storage_checkpoint(conn, database),
        )
        fact_read = _measure_reads(
            samples=config.read_samples,
            limit=config.read_limit,
            operation=lambda: adapter.search_facts(
                conn, scope_id=scope_id, query="revenue", limit=config.read_limit
            ),
            plan=adapter.fact_query_plan(
                conn, scope_id=scope_id, query="revenue", limit=config.read_limit
            ),
            require_bounded_fact_index=True,
        )
        narrative_read = _measure_reads(
            samples=config.read_samples,
            limit=config.read_limit,
            operation=lambda: adapter.search_narrative(
                conn, scope_id=scope_id, query="demand", limit=config.read_limit
            ),
            plan=adapter.narrative_query_plan(
                conn, scope_id=scope_id, query="demand", limit=config.read_limit
            ),
        )
        rows = _current_historical_rows(conn)
        logical_delta_writes = (
            delta.work.fact_changes
            + delta.work.document_changes
            + delta.work.narrative_changes
            + delta.work.receipt_writes
        )
        durable_logical_delta_writes = (
            2
            * (delta.work.fact_changes + delta.work.document_changes + delta.work.narrative_changes)
            + 3
        )
        amplification = WriteAmplificationEvidence(
            no_op_logical_writes=no_op.work.receipt_writes,
            no_op_physical_total_changes=no_op.work.total_changes,
            small_delta_logical_writes=logical_delta_writes,
            small_delta_physical_total_changes=delta.work.total_changes,
            small_delta_amplification_ratio=(delta.work.total_changes / logical_delta_writes),
            small_delta_durable_logical_writes=durable_logical_delta_writes,
            small_delta_durable_amplification_ratio=(
                delta.work.total_changes / durable_logical_delta_writes
            ),
            small_delta_allocated_page_growth=max(
                0,
                delta.work.allocated_pages_after - delta.work.allocated_pages_before,
            ),
        )
        resume = _resume_evidence(conn, adapter, config, scope_id)
        history = _history_evidence(conn, adapter, config, scope_id)
        peak_memory = tracemalloc.get_traced_memory()[1]
        hot_path_wall = time.perf_counter() - hot_path_started
        command_wall = time.perf_counter() - command_started
    finally:
        if tracing_started:
            tracemalloc.stop()
        conn.close()
    change_audit = ChangeAuditEvidence.model_validate(
        {
            "baseline_refresh_id": baseline_audit.refresh_id,
            "baseline_mode": baseline_audit.mode,
            "baseline_logical_changes": baseline_audit.logical_changes,
            "baseline_digest_bucket_limit": MAX_INITIAL_CHECKPOINT_DIGEST_BUCKETS,
            "baseline_declared_digest_bucket_commitments": (
                baseline_audit.declared_bucket_commitments
            ),
            "baseline_digest_bucket_commitments": baseline_audit.change_set_entries,
            "baseline_digest_buckets_non_empty": (baseline_audit.digest_buckets_non_empty),
            "baseline_digest_buckets_ordered": baseline_audit.digest_buckets_ordered,
            "baseline_detailed_change_rows": baseline_audit.detailed_change_rows,
            "delta_refresh_id": delta_audit.refresh_id,
            "delta_mode": delta_audit.mode,
            "delta_logical_changes": delta_audit.logical_changes,
            "delta_coordinate_change_commitments": delta_audit.change_set_entries,
            "delta_detailed_change_rows": delta_audit.detailed_change_rows,
        }
    )
    cross_scope = CrossScopeIsolationEvidence(
        target_scope_id=scope_id,
        canonical_metric_cell_scope_index_columns=(
            cross_scope_before_delta.canonical_metric_cell_scope_index_columns
        ),
        canonical_projection_keyset_index_columns=(
            cross_scope_before_delta.canonical_projection_keyset_index_columns
        ),
        source_scope_query_plan=cross_scope_before_delta.source_scope_query_plan,
        source_scope_query_uses_projection_keyset_index=(
            cross_scope_before_delta.source_scope_query_uses_projection_keyset_index
        ),
        source_scope_query_avoids_projection_scan=(
            cross_scope_before_delta.source_scope_query_avoids_projection_scan
        ),
        authoritative_scope_count=(cross_scope_before_delta.authoritative_scope_count),
        authoritative_issuer_count=(cross_scope_before_delta.authoritative_issuer_count),
        authoritative_reporting_entity_count=(
            cross_scope_before_delta.authoritative_reporting_entity_count
        ),
        source_fact_rows=cross_scope_before_delta.source_fact_rows,
        source_reporting_entity_mismatches=(
            cross_scope_before_delta.source_reporting_entity_mismatches
        ),
        minimum_source_facts_per_scope=(cross_scope_before_delta.minimum_source_facts_per_scope),
        maximum_source_facts_per_scope=(cross_scope_before_delta.maximum_source_facts_per_scope),
        target_source_fact_rows=cross_scope_before_delta.target_source_fact_rows,
        materialized_scope_count_before_delta=(cross_scope_before_delta.materialized_scope_count),
        materialized_scope_count_after_delta=(cross_scope_after_delta.materialized_scope_count),
        materialized_fact_rows_before_delta=(cross_scope_before_delta.materialized_fact_rows),
        materialized_fact_rows_after_delta=(cross_scope_after_delta.materialized_fact_rows),
        target_current_fact_rows_before_delta=(cross_scope_before_delta.target_current_fact_rows),
        target_current_fact_rows_after_delta=(cross_scope_after_delta.target_current_fact_rows),
        scope_fact_count_mismatches_before_delta=(
            cross_scope_before_delta.scope_fact_count_mismatches
        ),
        scope_fact_count_mismatches_after_delta=(
            cross_scope_after_delta.scope_fact_count_mismatches
        ),
        cross_scope_fact_mismatches_before_delta=(
            cross_scope_before_delta.cross_scope_fact_mismatches
        ),
        cross_scope_fact_mismatches_after_delta=(
            cross_scope_after_delta.cross_scope_fact_mismatches
        ),
        detailed_change_rows_before_delta=(cross_scope_before_delta.detailed_change_rows),
        detailed_change_rows_after_delta=cross_scope_after_delta.detailed_change_rows,
        non_target_head_set_sha256_before_delta=(
            cross_scope_before_delta.non_target_head_set_sha256
        ),
        non_target_head_set_sha256_after_delta=(cross_scope_after_delta.non_target_head_set_sha256),
        non_target_heads_unchanged=(
            cross_scope_before_delta.non_target_head_set_sha256
            == cross_scope_after_delta.non_target_head_set_sha256
        ),
        non_target_current_rows_bound_to_heads=(
            cross_scope_after_delta.non_target_current_rows_not_bound_to_heads == 0
        ),
    )
    ratchets = _ratchets(
        config,
        no_op,
        delta,
        fact_read,
        narrative_read,
        rows,
        change_audit,
        cross_scope,
        storage,
        amplification,
        resume,
        history,
    )
    budget_results = _budget_results(
        budgets,
        hot_path_wall_seconds=hot_path_wall,
        peak_memory=peak_memory,
        no_op=no_op,
        delta=delta,
        storage=storage,
        fact_read=fact_read,
        narrative_read=narrative_read,
        history=history,
    )
    base = LatestStateBenchmarkReport(
        report_version=REPORT_VERSION,
        config=config,
        budgets=budgets,
        config_sha256=digest_text(canonical_json(config.model_dump(mode="json"))),
        fixture=fixture,
        no_op=no_op,
        small_delta=delta,
        fact_read=fact_read,
        narrative_read=narrative_read,
        rows=rows,
        storage=storage,
        change_audit=change_audit,
        cross_scope=cross_scope,
        implementation_provenance=_implementation_provenance(),
        write_amplification=amplification,
        resume=resume,
        history_independence=history,
        peak_python_memory_bytes=peak_memory,
        python_memory_measurement_scope="post_fixture_hot_path",
        cold_baseline_process_memory=cold_baseline_memory,
        fixture_prep_wall_seconds=fixture_prep_wall,
        hot_path_wall_seconds=hot_path_wall,
        command_wall_seconds=command_wall,
        environment=EnvironmentVersions(
            python=sys.version,
            sqlite=sqlite3.sqlite_version,
            pydantic=pydantic.__version__,
            platform=platform.platform(),
        ),
        ratchets=ratchets,
        budget_results=budget_results,
        overall_pass=all(item.passed for item in (*ratchets, *budget_results)),
        report_sha256="0" * 64,
    )
    return base.model_copy(update={"report_sha256": _report_sha256(base)})


_STAGE_IDENTITY_PAYLOAD_COLUMNS = (
    "stage_ordinal",
    "entity_kind",
    "change_kind",
    "coordinate_key",
    "prior_commitment_sha256",
    "current_commitment_sha256",
    "canonical_payload_json",
    "payload_sha256",
)


def _ordered_stage_identity_payload_bytes(
    conn: sqlite3.Connection,
    *,
    table: Literal["latest_governed_refresh_stage", "benchmark_stage_delete_audit"],
    refresh_id: str,
    limit: int | None = None,
) -> bytes:
    columns = ",".join(_STAGE_IDENTITY_PAYLOAD_COLUMNS)
    sql = (
        f"SELECT {columns} FROM {table} WHERE refresh_run_id=? "  # nosec B608
        "ORDER BY stage_ordinal"
    )
    params: tuple[object, ...]
    if limit is None:
        params = (refresh_id,)
    else:
        sql += " LIMIT ?"
        params = (refresh_id, limit)
    rows = conn.execute(sql, params).fetchall()
    payload = [
        {column: row[index] for index, column in enumerate(_STAGE_IDENTITY_PAYLOAD_COLUMNS)}
        for row in rows
    ]
    return canonical_json(payload).encode("utf-8")


def _install_stage_delete_audit(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TEMP TABLE benchmark_stage_delete_audit (
          refresh_run_id TEXT,
          stage_ordinal INTEGER,
          entity_kind TEXT,
          change_kind TEXT,
          coordinate_key TEXT,
          prior_commitment_sha256 TEXT,
          current_commitment_sha256 TEXT,
          canonical_payload_json TEXT,
          payload_sha256 TEXT
        );
        CREATE TEMP TRIGGER benchmark_stage_delete_audit_trigger
        BEFORE DELETE ON latest_governed_refresh_stage BEGIN
          INSERT INTO benchmark_stage_delete_audit VALUES (
            OLD.refresh_run_id,
            OLD.stage_ordinal,
            OLD.entity_kind,
            OLD.change_kind,
            OLD.coordinate_key,
            OLD.prior_commitment_sha256,
            OLD.current_commitment_sha256,
            OLD.canonical_payload_json,
            OLD.payload_sha256
          );
        END;
        """
    )


def _resume_evidence(
    conn: sqlite3.Connection,
    adapter: LatestStateBenchmarkAdapter,
    config: LatestStateBenchmarkConfig,
    scope_id: str,
) -> ResumeEvidence:
    delta_total = config.delta_cell_count + config.delta_document_count + config.delta_chunk_count
    resume_batch_rows = min(
        config.max_batch_rows,
        max(1, (delta_total - 1) // config.interrupt_after_batches),
    )
    resume_config = config.model_copy(update={"max_batch_rows": resume_batch_rows})
    with tempfile.TemporaryDirectory(prefix="latest-state-resume-") as directory:
        clone_root = Path(directory)
        uninterrupted = connect_sqlite(
            clone_root / "uninterrupted.db",
            role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
        )
        interrupted = connect_sqlite(
            clone_root / "interrupted.db",
            role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
        )
        try:
            adapter.clone_fixture(conn, uninterrupted, history_multiplier=1)
            adapter.clone_fixture(conn, interrupted, history_multiplier=1)
            adapter.apply_small_delta(uninterrupted, resume_config)
            adapter.apply_small_delta(interrupted, resume_config)
            whole = adapter.refresh(
                uninterrupted,
                scope_id=scope_id,
                config=resume_config,
                operation_recorded_at=BENCHMARK_STAMP,
            )
            partial = adapter.refresh(
                interrupted,
                scope_id=scope_id,
                config=resume_config,
                operation_recorded_at=BENCHMARK_STAMP,
                interrupt_after_batches=config.interrupt_after_batches,
            )
            staged_before = int(
                interrupted.execute(
                    "SELECT COUNT(*) FROM latest_governed_refresh_stage WHERE refresh_run_id=?",
                    (partial.refresh_id,),
                ).fetchone()[0]
            )
            staged_identity_payload = _ordered_stage_identity_payload_bytes(
                interrupted,
                table="latest_governed_refresh_stage",
                refresh_id=partial.refresh_id,
            )
            _install_stage_delete_audit(interrupted)
            resumed = adapter.refresh(
                interrupted,
                scope_id=scope_id,
                config=resume_config,
                operation_recorded_at=BENCHMARK_STAMP,
                resume_refresh_id=partial.refresh_id,
            )
            finalized_identity_payload_prefix = _ordered_stage_identity_payload_bytes(
                interrupted,
                table="benchmark_stage_delete_audit",
                refresh_id=partial.refresh_id,
                limit=staged_before,
            )
            ordered_stage_equal = staged_identity_payload == finalized_identity_payload_prefix
            duplicate_stage = int(
                interrupted.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT entity_kind,coordinate_key,COUNT(*) AS copies "
                    "FROM latest_governed_refresh_stage WHERE refresh_run_id=? "
                    "GROUP BY entity_kind,coordinate_key HAVING copies>1)",
                    (partial.refresh_id,),
                ).fetchone()[0]
            )
            duplicate_changes = int(
                interrupted.execute(
                    "SELECT COUNT(*) FROM ("
                    "SELECT change_row.entity_kind,change_row.coordinate_key,"
                    "COUNT(*) AS copies "
                    "FROM latest_governed_refresh_changes change_row "
                    "JOIN latest_governed_refresh_receipts receipt "
                    "ON receipt.receipt_id=change_row.receipt_id "
                    "WHERE receipt.refresh_run_id=? "
                    "GROUP BY entity_kind,coordinate_key HAVING copies>1)",
                    (resumed.refresh_id,),
                ).fetchone()[0]
            )
            return ResumeEvidence(
                interrupted_refresh_id=partial.refresh_id,
                resume_cursor=partial.resume_cursor,
                staged_rows_before_resume=staged_before,
                staged_rows_rewritten=0 if ordered_stage_equal else staged_before,
                replayed_rows=resumed.replayed_count,
                duplicate_rows_after_resume=duplicate_stage + duplicate_changes,
                staged_identity_payload_bytes=len(staged_identity_payload),
                finalized_identity_payload_prefix_bytes=len(finalized_identity_payload_prefix),
                staged_identity_payload_sha256=hashlib.sha256(staged_identity_payload).hexdigest(),
                finalized_identity_payload_prefix_sha256=hashlib.sha256(
                    finalized_identity_payload_prefix
                ).hexdigest(),
                ordered_stage_identity_payloads_equal=ordered_stage_equal,
                final_commitment=resumed.terminal_commitment,
                uninterrupted_commitment=whole.terminal_commitment,
                equivalent=(
                    resumed.terminal_commitment == whole.terminal_commitment and ordered_stage_equal
                ),
            )
        finally:
            uninterrupted.close()
            interrupted.close()


def _history_evidence(
    conn: sqlite3.Connection,
    adapter: LatestStateBenchmarkAdapter,
    config: LatestStateBenchmarkConfig,
    scope_id: str,
) -> HistoryIndependenceEvidence:
    with tempfile.TemporaryDirectory(prefix="latest-state-history-") as directory:
        clone_root = Path(directory)
        one_x = connect_sqlite(
            clone_root / "one-x.db",
            role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
        )
        four_x = connect_sqlite(
            clone_root / "four-x.db",
            role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
        )
        try:
            adapter.clone_fixture(conn, one_x, history_multiplier=1)
            adapter.clone_fixture(conn, four_x, history_multiplier=config.history_multiplier)
            one_publications_before = _source_publication_count(one_x)
            four_publications_before = _source_publication_count(four_x)
            adapter.apply_small_delta(one_x, config)
            adapter.apply_small_delta(four_x, config)
            one_publication_delta = _source_publication_count(one_x) - one_publications_before
            four_publication_delta = _source_publication_count(four_x) - four_publications_before
            one = _measure_refresh(
                one_x,
                lambda: adapter.refresh(
                    one_x,
                    scope_id=scope_id,
                    config=config,
                    operation_recorded_at=BENCHMARK_STAMP,
                ),
                independent_source_publications=one_publication_delta,
            )
            four = _measure_refresh(
                four_x,
                lambda: adapter.refresh(
                    four_x,
                    scope_id=scope_id,
                    config=config,
                    operation_recorded_at=BENCHMARK_STAMP,
                ),
                independent_source_publications=four_publication_delta,
            )
            ratio = four.wall_milliseconds / max(one.wall_milliseconds, 0.001)
            vm_step_ratio = four.work.sqlite_vm_step_proxy / max(
                one.work.sqlite_vm_step_proxy, SQLITE_PROGRESS_INTERVAL
            )
            exact_work = _semantic_work(one.work) == _semantic_work(four.work)
            same_commitment = one.terminal_commitment == four.terminal_commitment
            return HistoryIndependenceEvidence(
                one_x_commitment=one.terminal_commitment,
                four_x_commitment=four.terminal_commitment,
                one_x_work=one.work,
                four_x_work=four.work,
                one_x_wall_milliseconds=one.wall_milliseconds,
                four_x_wall_milliseconds=four.wall_milliseconds,
                latency_ratio=ratio,
                sqlite_vm_step_ratio=vm_step_ratio,
                equivalent=(
                    exact_work
                    and same_commitment
                    and vm_step_ratio <= MAX_RETAINED_HISTORY_VM_STEP_RATIO
                ),
            )
        finally:
            one_x.close()
            four_x.close()


def write_report_atomic(report: LatestStateBenchmarkReport, output_path: Path) -> None:
    """Write a validated canonical report using atomic replacement."""

    output = _validate_new_path(output_path, label="benchmark report")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not verify_report_sha256(report):
        raise BenchmarkContractError("report commitment is invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(report.model_dump(mode="json")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
