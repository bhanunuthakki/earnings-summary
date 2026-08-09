"""Fail-closed, read-only recovery audit for interrupted facts-depth GC runs.

The historical GC archive has aggregate manifest rows but no per-row run
identity.  This module therefore never infers one run's cohort from archive
timestamps.  It binds a sealed pre-run baseline, the observed database, and
the archive by stable file identity and compares the complete affected planes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
from collections import Counter
from collections.abc import Generator, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import groupby
from pathlib import Path
from typing import Literal, NoReturn, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.atomic_cutover import (
    ActivationMode,
    ActivationReceipt,
    QuiescenceReceipt,
    activation_payload_sha256,
    quiescence_payload_sha256,
)
from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    ImmutableArtifactSnapshot,
    assert_artifact_unchanged,
    path_aliases_any,
    read_stable_artifact,
    require_canonical_text_artifact,
    require_no_reparse_points,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_DELETE_TRIGGER = "trg_financial_facts_observation_delete"
_CANONICAL_DELETE_TRIGGER_SQL = (
    "CREATE TRIGGER trg_financial_facts_observation_delete "
    "BEFORE DELETE ON financial_facts BEGIN SELECT RAISE(ABORT, "
    "'financial fact history is append-only after cutover'); END"
)
_ARCHIVED_TABLES = ("financial_facts", "metric_computation_attempts")
_PROVENANCE_PLANES = (
    ("fact_observation_revisions", "fact_table", "fact_row_id"),
    ("legacy_fact_evidence_match_revisions", "fact_table", "fact_row_id"),
    ("fact_selection_decisions", "target_table", "target_row_id"),
)
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = r"^[0-9a-f]{64}$"
_MAX_ADMISSION_TTL = timedelta(minutes=30)
_ARCHIVE_VARIANT_LIMIT = 64
# Archive-only metadata columns the run-keyed sidecar carries on top of the
# live table's mirror (execution/db_gc.py ARCHIVE_META_COLUMNS + the historic
# archive_run_id spelling _audit_manifest accepts).
_ARCHIVE_META_COLUMNS = frozenset({"gc_run_id", "archive_run_id", "gc_source_rowid"})
_CANONICAL_LISTENER = "127.0.0.1:7421"
_GC_ARCHIVE_NAME = "portfolio_gc_archive.db"
_CANONICAL_REPOSITORY = Path(__file__).resolve().parents[2]
_PUBLICATION_FRESHNESS_FLOOR = timedelta(seconds=30)
_INERT_INACCESSIBLE_WINDOWS_PROCESS_IMAGES = frozenset(
    {
        "memory compression",
        "registry",
        "secure system",
        "system",
        "system idle process",
    }
)


class GcRecoveryError(RuntimeError):
    """The evidence set cannot support a stable recovery audit."""


CanonicalRow = tuple[tuple[str, str | bytes], ...]


@dataclass(frozen=True)
class _RowGroup:
    key: tuple[object, ...]
    row_count: int
    rows: Counter[CanonicalRow]
    variant_overflow: bool = False


@dataclass(frozen=True)
class _EffectiveGcInvocation:
    script: Path
    database: Path
    archive: Path
    policies: tuple[str, ...]
    apply: bool
    include_portfolio: bool


class GcRecoveryOutcome(StrEnum):
    """What the three-way evidence proves about the affected planes."""

    ROLLED_BACK_OR_NOOP = "rolled_back_or_noop"
    COMMITTED_RECOVERABLE = "committed_recoverable"
    AMBIGUOUS = "ambiguous"


class ArtifactSnapshot(BaseModel):
    """Stable identity and digest for one sealed input artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    device: int
    inode: int
    link_count: int = Field(ge=1)
    size_bytes: int = Field(ge=0)
    modified_time_ns: int = Field(ge=0)
    changed_time_ns: int = Field(ge=0)
    sha256: str = Field(pattern=_SHA256)


class DatabaseVerification(BaseModel):
    """Read-only SQLite structural verification bound to an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str | None
    quick_check: tuple[str, ...]
    integrity_check: tuple[str, ...]
    foreign_key_violations: int = Field(ge=0)


class ArchivedTableRecovery(BaseModel):
    """Exhaustive baseline/current/archive parity for one archived table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str
    primary_key_columns: tuple[str, ...]
    baseline_count: int = Field(ge=0)
    current_count: int = Field(ge=0)
    archive_row_count: int = Field(ge=0)
    archive_unique_key_count: int = Field(ge=0)
    missing_from_current_count: int = Field(ge=0)
    current_extra_count: int = Field(ge=0)
    current_payload_changed_count: int = Field(ge=0)
    missing_exact_in_archive_count: int = Field(ge=0)
    missing_without_archive_count: int = Field(ge=0)
    missing_conflicting_archive_count: int = Field(ge=0)
    exact_archive_duplicate_row_count: int = Field(ge=0)
    archive_variant_overflow_key_count: int = Field(ge=0)
    current_archive_overlap_row_count: int = Field(ge=0)
    missing_key_samples: tuple[str, ...]


class ProvenancePlaneRecovery(BaseModel):
    """Rows tied to deleted facts that GC did not archive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str
    baseline_candidate_row_count: int = Field(ge=0)
    current_candidate_row_count: int = Field(ge=0)
    lost_row_count: int = Field(ge=0)
    unexpected_row_count: int = Field(ge=0)


class ArchiveManifestSummary(BaseModel):
    """Historical aggregate manifest evidence; not a row-level run binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    present: bool
    facts_depth_manifest_rows: int = Field(ge=0)
    source_rows_archived: tuple[tuple[str, int], ...]
    run_at_values: tuple[str, ...]
    row_level_run_identity_present: bool


class RecoveryBaselineAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gc-recovery-baseline-authority/v1"]
    baseline_database: str = Field(min_length=1)
    baseline_database_sha256: str = Field(pattern=_SHA256)
    baseline_revision: str = Field(min_length=1)
    baseline_captured_at: datetime
    baseline_capture_method: Literal[
        "sqlite-backup",
        "quiesced-file-copy",
        "activation-rollback-snapshot",
    ]
    baseline_quick_check: Literal["ok"]
    baseline_integrity_check: Literal["ok"]
    baseline_foreign_key_violations: Literal[0]
    activated_database_sha256: str = Field(pattern=_SHA256)
    activation_receipt_artifact: str = Field(min_length=1)
    activation_receipt_artifact_sha256: str = Field(pattern=_SHA256)
    activation_receipt_artifact_size_bytes: int = Field(ge=1)
    activation_quiescence_artifact: str = Field(min_length=1)
    activation_quiescence_artifact_sha256: str = Field(pattern=_SHA256)
    activation_quiescence_artifact_size_bytes: int = Field(ge=1)
    receipt_sha256: str = Field(pattern=_SHA256)

    def computed_receipt_sha256(self) -> str:
        return _canonical_sha(self.model_dump(mode="json", exclude={"receipt_sha256"}))


class QuiescedTaskObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1)
    state: Literal["Disabled"]
    enabled: Literal[False]


class QuiescedServiceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    state: Literal["Stopped"]


class QuiescedListenerObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    host: Literal["127.0.0.1"]
    port: Literal[7421]
    listening: Literal[False]
    pid: None

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


class RecoveryQuiescenceRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gc-recovery-quiescence/v1"]
    captured_at: datetime
    tasks: tuple[QuiescedTaskObservation, ...] = Field(min_length=1)
    services: tuple[QuiescedServiceObservation, ...] = Field(min_length=2, max_length=2)
    listeners: tuple[QuiescedListenerObservation, ...] = Field(min_length=1, max_length=1)


class ProcessCensusObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pid: int = Field(ge=0)
    parent_pid: int | None = Field(default=None, ge=0)
    image_name: str = Field(min_length=1)
    command_line_status: Literal["ok", "access_denied"]
    command_line: str | None
    working_directory: str | None

    @model_validator(mode="after")
    def _validate_observation(self) -> Self:
        if self.command_line_status == "ok" and not self.command_line:
            raise ValueError("accessible process census rows require a command line")
        if self.command_line_status == "access_denied" and self.command_line is not None:
            raise ValueError("access-denied process census rows cannot carry a command line")
        return self


class RecoveryProcessCensus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gc-recovery-process-census/v1"]
    captured_at: datetime
    scope: Literal["all-process-command-lines/v1"]
    command_sha256: str = Field(pattern=_SHA256)
    snapshot_complete: Literal[True]
    inventory_source: Literal["windows-all-process-command-lines/v1"]
    processes: tuple[ProcessCensusObservation, ...] = Field(min_length=1)


class LiveProcessCensus(BaseModel):
    """Process inventory executed by this auditor, not supplied by admission."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gc-recovery-live-process-census/v1"]
    captured_at: datetime
    collector: Literal[
        "powershell-get-ciminstance-win32-process/v1",
        "procfs-all-processes/v1",
    ]
    exit_code: Literal[0]
    processes: tuple[ProcessCensusObservation, ...] = Field(min_length=1)
    census_sha256: str = Field(pattern=_SHA256)

    def computed_census_sha256(self) -> str:
        return _canonical_sha(self.model_dump(mode="json", exclude={"census_sha256"}))


class RecoveryRuntimeAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gc-recovery-runtime-authority/v1"]
    repository: str = Field(min_length=1)
    git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    db_gc_artifact: str = Field(min_length=1)
    db_gc_sha256: str = Field(pattern=_SHA256)


class CanonicalTaskManifestEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    task_name: str = Field(min_length=1)


class CanonicalTaskManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1]
    namespace: Literal[r"\earnings-summary"]
    tasks: tuple[CanonicalTaskManifestEntry, ...] = Field(min_length=1)


class RecoveryTerminalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gc-recovery-terminal/v1"]
    captured_at: datetime
    working_directory: str = Field(min_length=1)
    command_argv_sha256: str = Field(pattern=_SHA256)
    stdout_sha256: str = Field(pattern=_SHA256)
    stderr_sha256: str = Field(pattern=_SHA256)
    status: Literal["complete", "failed", "unknown"]
    exit_code: int | None

    @model_validator(mode="after")
    def _validate_terminal(self) -> Self:
        if self.captured_at.tzinfo is None:
            raise ValueError("terminal evidence timestamp requires an explicit timezone")
        if self.status == "complete" and self.exit_code != 0:
            raise ValueError("complete terminal evidence requires exit code zero")
        if self.status == "failed" and (self.exit_code is None or self.exit_code == 0):
            raise ValueError("failed terminal evidence requires a nonzero exit code")
        if self.status == "unknown" and self.exit_code is not None:
            raise ValueError("unknown terminal evidence cannot carry an exit code")
        return self


class GcPolicyReportEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy: str
    applied: bool
    rows_deleted: dict[str, int]
    rows_updated: dict[str, int]
    detail: dict[str, int]


class FactsDepthPreflightEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gc-facts-depth-apply-preflight/v1"]
    foreign_keys_enabled: Literal[True]
    self_fk_target_table: Literal["financial_facts"]
    self_fk_from_column: Literal["supersedes_id"]
    self_fk_to_column: Literal["id"]
    lookup_index_name: Literal["ix_0270_financial_facts_supersedes_id"]
    lookup_index_columns: tuple[Literal["supersedes_id"], ...]
    lookup_index_unique: Literal[False]
    lookup_index_origin: Literal["c"]
    lookup_index_partial: Literal[False]
    sqlite_version: str = Field(min_length=1)
    lookup_query_plan: tuple[str, ...] = Field(min_length=1)


class GcRunReportEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_at: str = Field(min_length=1)
    db_path: str = Field(min_length=1)
    archive_path: str = Field(min_length=1)
    apply: bool
    policies: tuple[GcPolicyReportEvidence, ...]
    facts_depth_apply_preflight: FactsDepthPreflightEvidence | None


class GcEventEvidence(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, strict=True)

    event: str = Field(min_length=1)


class GcRecoveryAdmissionReceipt(BaseModel):
    """Reviewed precommitment for the baseline, operation, and quiescence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["gc-recovery-admission/v1"]
    captured_at: datetime
    valid_until: datetime
    current_database: str = Field(min_length=1)
    current_database_sha256: str = Field(pattern=_SHA256)
    current_revision: str = Field(min_length=1)
    baseline_database: str = Field(min_length=1)
    baseline_database_sha256: str = Field(pattern=_SHA256)
    baseline_revision: str = Field(min_length=1)
    baseline_captured_at: datetime
    baseline_checkpointed: bool
    baseline_sidecars_absent: bool
    baseline_capture_method: Literal[
        "sqlite-backup",
        "quiesced-file-copy",
        "activation-rollback-snapshot",
    ]
    baseline_quick_check: Literal["ok"]
    baseline_integrity_check: Literal["ok"]
    baseline_foreign_key_violations: Literal[0]
    activated_database_sha256: str = Field(pattern=_SHA256)
    baseline_authority_artifact: str = Field(min_length=1)
    baseline_authority_artifact_sha256: str = Field(pattern=_SHA256)
    archive_database: str = Field(min_length=1)
    archive_database_sha256: str = Field(pattern=_SHA256)
    operation_started_at: datetime
    operation_working_directory: str = Field(min_length=1)
    operation_command_argv: tuple[str, ...] = Field(min_length=1)
    operation_database: str = Field(min_length=1)
    operation_archive_database: str = Field(min_length=1)
    operation_policy: Literal["facts-depth"]
    operation_include_portfolio: Literal[True]
    runtime_git_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    runtime_db_gc_sha256: str | None = Field(default=None, pattern=_SHA256)
    runtime_repository: str = Field(min_length=1)
    runtime_db_gc_artifact: str = Field(min_length=1)
    runtime_db_gc_artifact_size_bytes: int = Field(ge=1)
    runtime_authority_artifact: str = Field(min_length=1)
    runtime_authority_artifact_sha256: str = Field(pattern=_SHA256)
    runtime_authority_artifact_size_bytes: int = Field(ge=1)
    event_log_artifact: str = Field(min_length=1)
    event_log_artifact_sha256: str = Field(pattern=_SHA256)
    event_log_size_bytes: int = Field(ge=0)
    report_artifact: str = Field(min_length=1)
    report_artifact_sha256: str = Field(pattern=_SHA256)
    report_size_bytes: int = Field(ge=0)
    terminal_status: Literal["complete", "failed", "unknown"]
    terminal_exit_code: int | None
    terminal_artifact: str = Field(min_length=1)
    terminal_artifact_sha256: str = Field(pattern=_SHA256)
    terminal_artifact_size_bytes: int = Field(ge=1)
    quiescence_registry_artifact: str = Field(min_length=1)
    quiescence_registry_artifact_sha256: str = Field(pattern=_SHA256)
    quiescence_registry_artifact_size_bytes: int = Field(ge=1)
    expected_task_paths: tuple[str, ...] = Field(min_length=1)
    disabled_task_paths: tuple[str, ...] = Field(min_length=1)
    expected_service_names: tuple[str, ...] = Field(min_length=2, max_length=2)
    stopped_service_names: tuple[str, ...] = Field(min_length=2, max_length=2)
    expected_listener_endpoints: tuple[str, ...] = Field(min_length=1)
    inactive_listener_endpoints: tuple[str, ...] = Field(min_length=1)
    process_census_scope: Literal["all-process-command-lines/v1"]
    process_census_command_sha256: str = Field(pattern=_SHA256)
    process_census_artifact: str = Field(min_length=1)
    process_census_artifact_sha256: str = Field(pattern=_SHA256)
    process_census_artifact_size_bytes: int = Field(ge=1)
    process_census_total_count: int = Field(ge=0)
    process_command_line_access_denied_count: int = Field(ge=0)
    database_writer_matches: tuple[str, ...]
    receipt_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        for value in (
            self.captured_at,
            self.valid_until,
            self.baseline_captured_at,
            self.operation_started_at,
        ):
            if value.tzinfo is None:
                raise ValueError("recovery admission timestamps require explicit timezones")
        if not (
            self.baseline_captured_at
            <= self.operation_started_at
            <= self.captured_at
            < self.valid_until
        ):
            raise ValueError("recovery admission clocks are out of order")
        if self.valid_until - self.captured_at > _MAX_ADMISSION_TTL:
            raise ValueError("recovery admission validity exceeds the governed maximum")
        _require_exact_casefold_set(
            self.expected_task_paths,
            self.disabled_task_paths,
            label="task",
        )
        _require_exact_casefold_set(
            self.expected_service_names,
            self.stopped_service_names,
            label="service",
        )
        if {name.casefold() for name in self.expected_service_names} != {
            "es-dashboard",
            "es-poller",
        }:
            raise ValueError("recovery admission must bind es-dashboard and es-poller")
        _require_exact_casefold_set(
            self.expected_listener_endpoints,
            self.inactive_listener_endpoints,
            label="listener",
        )
        if tuple(endpoint.casefold() for endpoint in self.expected_listener_endpoints) != (
            _CANONICAL_LISTENER,
        ):
            raise ValueError("recovery admission must bind the canonical dashboard listener")
        if any(
            not path.casefold().startswith("\\earnings-summary\\")
            for path in self.expected_task_paths
        ):
            raise ValueError("recovery admission tasks must use the canonical namespace")
        if self.terminal_status == "unknown" and self.terminal_exit_code is not None:
            raise ValueError("unknown terminal status cannot carry an exit code")
        if self.terminal_status == "complete" and self.terminal_exit_code != 0:
            raise ValueError("complete terminal status requires exit code zero")
        if self.terminal_status == "failed" and (
            self.terminal_exit_code is None or self.terminal_exit_code == 0
        ):
            raise ValueError("failed terminal status requires a nonzero exit code")
        return self

    def computed_receipt_sha256(self) -> str:
        return _canonical_sha(self.model_dump(mode="json", exclude={"receipt_sha256"}))


class AdmittedRecoveryEvidence(BaseModel):
    """Stable small-artifact evidence retained through the database scan."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    receipt: GcRecoveryAdmissionReceipt
    admission_artifact: ImmutableArtifactSnapshot
    baseline_authority_artifact: ImmutableArtifactSnapshot
    activation_receipt_artifact: ImmutableArtifactSnapshot
    activation_quiescence_artifact: ImmutableArtifactSnapshot
    runtime_db_gc_artifact: ImmutableArtifactSnapshot
    runtime_authority_artifact: ImmutableArtifactSnapshot
    event_log_artifact: ImmutableArtifactSnapshot
    report_artifact: ImmutableArtifactSnapshot
    terminal_artifact: ImmutableArtifactSnapshot
    quiescence_registry_artifact: ImmutableArtifactSnapshot
    process_census_artifact: ImmutableArtifactSnapshot
    process_census: RecoveryProcessCensus
    operation_report: GcRunReportEvidence | None


class GcRecoveryReceipt(BaseModel):
    """Self-sealed recovery decision over one immutable evidence triple."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["gc-recovery-audit/v1"]
    evidence_clock: datetime
    admission_receipt_path: str
    admission_receipt_file_sha256: str = Field(pattern=_SHA256)
    admission_receipt_sha256: str = Field(pattern=_SHA256)
    admission_valid_until: datetime
    operation_terminal_status: Literal["complete", "failed", "unknown"]
    evidence_fence_mode: Literal["windows-deny-write", "posix-advisory"]
    process_census_start: LiveProcessCensus
    process_census_final: LiveProcessCensus
    current_database: ArtifactSnapshot
    baseline_database: ArtifactSnapshot
    archive_database: ArtifactSnapshot
    current_verification: DatabaseVerification
    baseline_verification: DatabaseVerification
    archive_verification: DatabaseVerification
    delete_trigger_present: bool
    delete_trigger_matches_baseline: bool
    delete_trigger_matches_canonical: bool
    facts_depth_retry_fk_ready: bool
    facts_depth_retry_index_ready: bool
    facts_depth_retry_admission_ready: bool
    financial_facts: ArchivedTableRecovery
    metric_computation_attempts: ArchivedTableRecovery
    provenance_planes: tuple[ProvenancePlaneRecovery, ...]
    governed_linked_candidate_count: int = Field(ge=0)
    reported_financial_facts_deleted: int = Field(ge=0)
    reported_metric_computation_attempts_deleted: int = Field(ge=0)
    archive_manifest: ArchiveManifestSummary
    outcome: GcRecoveryOutcome
    recovery_ready: bool
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    report_sha256: str = Field(pattern=_SHA256)

    def computed_report_sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude={"report_sha256"})
        return _canonical_sha(payload)


def _audit_gc_recovery_core(
    current_database: Path,
    *,
    baseline_database: Path,
    archive_database: Path,
    admission_receipt_path: Path,
    expected_admission_receipt_sha256: str,
    expected_current_revision: str,
    expected_baseline_revision: str,
    evidence_fence_mode: Literal["windows-deny-write", "posix-advisory"],
    process_census_start: LiveProcessCensus,
) -> GcRecoveryReceipt:
    """Audit one quiesced evidence triple without creating SQLite sidecars."""

    current, baseline, archive = _admit_paths(
        current_database,
        baseline_database,
        archive_database,
    )
    admitted = _load_admission(
        admission_receipt_path,
        expected_file_sha256=expected_admission_receipt_sha256,
    )
    _require_admission_fresh(admitted.receipt)
    before = tuple(_snapshot_artifact(path) for path in (current, baseline, archive))
    _require_admission_artifact_bindings(
        admitted.receipt,
        current=before[0],
        baseline=before[1],
        archive=before[2],
        expected_current_revision=expected_current_revision,
        expected_baseline_revision=expected_baseline_revision,
    )
    try:
        with ExitStack() as stack:
            current_conn, baseline_conn, archive_conn = tuple(
                stack.enter_context(
                    closing(
                        connect_sqlite(
                            path,
                            role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
                        )
                    )
                )
                for path in (current, baseline, archive)
            )
            current_verification = _verify_database(current_conn, require_revision=True)
            baseline_verification = _verify_database(baseline_conn, require_revision=True)
            archive_verification = _verify_database(archive_conn, require_revision=False)
            trigger_current = _schema_sql(current_conn, "trigger", _DELETE_TRIGGER)
            trigger_baseline = _schema_sql(baseline_conn, "trigger", _DELETE_TRIGGER)
            retry_fk_ready = _facts_depth_retry_fk_ready(current_conn)
            retry_index_ready = _facts_depth_retry_index_ready(current_conn)
            (
                financial_facts,
                metric_attempts,
                provenance_planes,
                governed_linked_count,
            ) = _audit_tables(current_conn, baseline_conn, archive_conn)
            manifest = _audit_manifest(archive_conn)
            reported_facts, reported_attempts = _validate_report_against_audit(
                admitted.operation_report,
                financial_facts=financial_facts,
                metric_attempts=metric_attempts,
                provenance_planes=provenance_planes,
            )
    except (sqlite3.Error, ValueError) as exc:
        raise GcRecoveryError(
            f"recovery evidence query failed: {type(exc).__name__}: {exc}"
        ) from exc
    trigger_matches_canonical = trigger_current is not None and _normalized_sql(
        trigger_current
    ) == _normalized_sql(_CANONICAL_DELETE_TRIGGER_SQL)

    after = tuple(_snapshot_artifact(path) for path in (current, baseline, archive))
    _require_no_sidecars((current, baseline, archive))
    if after != before:
        raise GcRecoveryError("sealed database or archive changed during recovery audit")
    _assert_admission_unchanged(admitted)

    final = tuple(_snapshot_artifact(path) for path in (current, baseline, archive))
    _require_no_sidecars((current, baseline, archive))
    if final != before:
        raise GcRecoveryError("sealed database or archive changed before publication")
    _assert_admission_unchanged(admitted)
    process_census_final = _collect_live_process_census()
    _require_runtime_head_unchanged(admitted.receipt)
    _require_admission_fresh(admitted.receipt)

    blocker_set = set(
        _blockers(
            current_verification=current_verification,
            baseline_verification=baseline_verification,
            archive_verification=archive_verification,
            expected_current_revision=expected_current_revision,
            expected_baseline_revision=expected_baseline_revision,
            delete_trigger_present=trigger_current is not None,
            delete_trigger_matches_baseline=(
                trigger_current is not None
                and trigger_baseline is not None
                and _normalized_sql(trigger_current) == _normalized_sql(trigger_baseline)
            ),
            delete_trigger_matches_canonical=trigger_matches_canonical,
            archived_tables=(financial_facts, metric_attempts),
            provenance_planes=provenance_planes,
            governed_linked_count=governed_linked_count,
            manifest=manifest,
            retry_fk_ready=retry_fk_ready,
            retry_index_ready=retry_index_ready,
            admission=admitted.receipt,
            process_census=admitted.process_census,
        )
    )
    blocker_set.update(_live_process_blockers(process_census_start, current_database=current))
    blocker_set.update(_live_process_blockers(process_census_final, current_database=current))
    if evidence_fence_mode != "windows-deny-write":
        blocker_set.add("evidence_fence_is_not_write_deny")
    blockers = tuple(sorted(blocker_set))
    changed = any(
        table.missing_from_current_count
        or table.current_extra_count
        or table.current_payload_changed_count
        for table in (financial_facts, metric_attempts)
    )
    if blockers:
        outcome = GcRecoveryOutcome.AMBIGUOUS
    elif changed:
        outcome = GcRecoveryOutcome.COMMITTED_RECOVERABLE
    else:
        outcome = GcRecoveryOutcome.ROLLED_BACK_OR_NOOP

    evidence_clock = datetime.fromtimestamp(
        max(snapshot.modified_time_ns for snapshot in before) / 1_000_000_000,
        tz=UTC,
    )
    warnings = (
        () if manifest.row_level_run_identity_present else ("archive_rows_lack_run_identity",)
    )
    fields: dict[str, object] = {
        "schema_version": "gc-recovery-audit/v1",
        "evidence_clock": evidence_clock,
        "admission_receipt_path": str(admitted.admission_artifact.path),
        "admission_receipt_file_sha256": admitted.admission_artifact.file_sha256,
        "admission_receipt_sha256": admitted.receipt.receipt_sha256,
        "admission_valid_until": admitted.receipt.valid_until,
        "operation_terminal_status": admitted.receipt.terminal_status,
        "evidence_fence_mode": evidence_fence_mode,
        "process_census_start": process_census_start,
        "process_census_final": process_census_final,
        "current_database": before[0],
        "baseline_database": before[1],
        "archive_database": before[2],
        "current_verification": current_verification,
        "baseline_verification": baseline_verification,
        "archive_verification": archive_verification,
        "delete_trigger_present": trigger_current is not None,
        "delete_trigger_matches_baseline": (
            trigger_current is not None
            and trigger_baseline is not None
            and _normalized_sql(trigger_current) == _normalized_sql(trigger_baseline)
        ),
        "delete_trigger_matches_canonical": trigger_matches_canonical,
        "facts_depth_retry_fk_ready": retry_fk_ready,
        "facts_depth_retry_index_ready": retry_index_ready,
        "facts_depth_retry_admission_ready": retry_fk_ready and retry_index_ready,
        "financial_facts": financial_facts,
        "metric_computation_attempts": metric_attempts,
        "provenance_planes": provenance_planes,
        "governed_linked_candidate_count": governed_linked_count,
        "reported_financial_facts_deleted": reported_facts,
        "reported_metric_computation_attempts_deleted": reported_attempts,
        "archive_manifest": manifest,
        "outcome": outcome,
        "recovery_ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "report_sha256": "0" * 64,
    }
    unsealed = GcRecoveryReceipt.model_validate(fields)
    return unsealed.model_copy(update={"report_sha256": unsealed.computed_report_sha256()})


def audit_gc_recovery(
    current_database: Path,
    *,
    baseline_database: Path,
    archive_database: Path,
    admission_receipt_path: Path,
    expected_admission_receipt_sha256: str,
    expected_activation_receipt_sha256: str,
    expected_current_revision: str,
    expected_baseline_revision: str,
) -> GcRecoveryReceipt:
    """Audit one evidence triple while denying writes to every admitted artifact."""

    current, baseline, archive = _admit_paths(
        current_database,
        baseline_database,
        archive_database,
    )
    preliminary = _load_admission(
        admission_receipt_path,
        expected_file_sha256=expected_admission_receipt_sha256,
    )
    _require_expected_activation_receipt(
        preliminary,
        expected_activation_receipt_sha256,
    )
    paths = _fenced_evidence_paths(preliminary, current, baseline, archive)
    with _write_denial_fence(paths) as fence_mode:
        process_start = _collect_live_process_census()
        return _audit_gc_recovery_core(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission_receipt_path,
            expected_admission_receipt_sha256=expected_admission_receipt_sha256,
            expected_current_revision=expected_current_revision,
            expected_baseline_revision=expected_baseline_revision,
            evidence_fence_mode=cast(
                Literal["windows-deny-write", "posix-advisory"],
                fence_mode,
            ),
            process_census_start=process_start,
        )


def _validate_gc_recovery_replay_core(
    current_database: Path,
    *,
    baseline_database: Path,
    archive_database: Path,
    admission_receipt_path: Path,
    expected_admission_receipt_sha256: str,
    expected_current_revision: str,
    expected_baseline_revision: str,
    recovery_receipt_path: Path,
    expected_recovery_receipt_sha256: str,
) -> GcRecoveryReceipt:
    """Revalidate unchanged commitments without repeating the row census."""

    if not re.fullmatch(_SHA256, expected_recovery_receipt_sha256):
        raise GcRecoveryError("expected recovery receipt hash is invalid")
    current, baseline, archive = _admit_paths(
        current_database,
        baseline_database,
        archive_database,
    )
    admitted = _load_admission(
        admission_receipt_path,
        expected_file_sha256=expected_admission_receipt_sha256,
    )
    _require_admission_fresh(admitted.receipt)
    try:
        receipt_snapshot, payload = read_stable_artifact(recovery_receipt_path)
        if receipt_snapshot.file_sha256 != expected_recovery_receipt_sha256:
            raise GcRecoveryError("recovery receipt differs from the independent file commitment")
        receipt = GcRecoveryReceipt.model_validate_json(payload)
        if receipt.report_sha256 != receipt.computed_report_sha256():
            raise GcRecoveryError("recovery receipt self-seal is invalid")
        require_canonical_text_artifact(receipt_snapshot, receipt.model_dump_json())
    except GcRecoveryError:
        raise
    except (ImmutableArtifactConflictError, OSError, ValueError) as exc:
        raise GcRecoveryError(
            f"existing recovery receipt is invalid: {type(exc).__name__}: {exc}"
        ) from exc
    observed = tuple(_snapshot_artifact(path) for path in (current, baseline, archive))
    _require_admission_artifact_bindings(
        admitted.receipt,
        current=observed[0],
        baseline=observed[1],
        archive=observed[2],
        expected_current_revision=expected_current_revision,
        expected_baseline_revision=expected_baseline_revision,
    )
    if observed != (
        receipt.current_database,
        receipt.baseline_database,
        receipt.archive_database,
    ):
        raise GcRecoveryError("recovery evidence differs from existing receipt commitments")
    if Path(receipt.admission_receipt_path) != admitted.admission_artifact.path:
        raise GcRecoveryError("existing receipt names a different admission artifact")
    if (
        receipt.admission_receipt_file_sha256 != admitted.admission_artifact.file_sha256
        or receipt.admission_receipt_sha256 != admitted.receipt.receipt_sha256
    ):
        raise GcRecoveryError("existing receipt admission commitment differs")
    if receipt.operation_terminal_status != admitted.receipt.terminal_status:
        raise GcRecoveryError("existing receipt terminal outcome differs from admission")
    if (
        receipt.current_verification.revision != expected_current_revision
        or receipt.baseline_verification.revision != expected_baseline_revision
    ):
        raise GcRecoveryError("existing receipt revision contract differs")
    final = tuple(_snapshot_artifact(path) for path in (current, baseline, archive))
    _require_no_sidecars((current, baseline, archive))
    if final != observed:
        raise GcRecoveryError("recovery evidence changed before replay publication")
    _assert_admission_unchanged(admitted)
    assert_artifact_unchanged(receipt_snapshot)
    _require_runtime_head_unchanged(admitted.receipt)
    _require_admission_fresh(admitted.receipt)
    return receipt


def validate_gc_recovery_replay(
    current_database: Path,
    *,
    baseline_database: Path,
    archive_database: Path,
    admission_receipt_path: Path,
    expected_admission_receipt_sha256: str,
    expected_activation_receipt_sha256: str,
    expected_current_revision: str,
    expected_baseline_revision: str,
    recovery_receipt_path: Path,
    expected_recovery_receipt_sha256: str,
) -> GcRecoveryReceipt:
    current, baseline, archive = _admit_paths(
        current_database,
        baseline_database,
        archive_database,
    )
    preliminary = _load_admission(
        admission_receipt_path,
        expected_file_sha256=expected_admission_receipt_sha256,
    )
    _require_expected_activation_receipt(
        preliminary,
        expected_activation_receipt_sha256,
    )
    paths = (
        *_fenced_evidence_paths(preliminary, current, baseline, archive),
        recovery_receipt_path,
    )
    with _write_denial_fence(paths) as fence_mode:
        start = _collect_live_process_census()
        if _live_process_blockers(start, current_database=current):
            raise GcRecoveryError("live process census blocks exact recovery replay")
        receipt = _validate_gc_recovery_replay_core(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission_receipt_path,
            expected_admission_receipt_sha256=expected_admission_receipt_sha256,
            expected_current_revision=expected_current_revision,
            expected_baseline_revision=expected_baseline_revision,
            recovery_receipt_path=recovery_receipt_path,
            expected_recovery_receipt_sha256=expected_recovery_receipt_sha256,
        )
        final = _collect_live_process_census()
        if _live_process_blockers(final, current_database=current):
            raise GcRecoveryError("live process census changed before replay publication")
        if fence_mode != "windows-deny-write":
            raise GcRecoveryError("exact recovery replay lacks a write-denial fence")
        _require_runtime_head_unchanged(preliminary.receipt)
        require_gc_recovery_receipt_fresh(receipt)
        return receipt


def publish_gc_recovery_audit(
    current_database: Path,
    *,
    baseline_database: Path,
    archive_database: Path,
    admission_receipt_path: Path,
    expected_admission_receipt_sha256: str,
    expected_activation_receipt_sha256: str,
    expected_current_revision: str,
    expected_baseline_revision: str,
    output: Path,
    expected_recovery_receipt_sha256: str | None = None,
) -> tuple[GcRecoveryReceipt, bool]:
    """Audit or replay and no-clobber publish inside one write-denial fence."""

    current, baseline, archive = _admit_paths(
        current_database,
        baseline_database,
        archive_database,
    )
    preliminary = _load_admission(
        admission_receipt_path,
        expected_file_sha256=expected_admission_receipt_sha256,
    )
    _require_expected_activation_receipt(
        preliminary,
        expected_activation_receipt_sha256,
    )
    paths = _fenced_evidence_paths(preliminary, current, baseline, archive)
    output_existed_before_fence = output.exists()
    if output_existed_before_fence:
        paths = (*paths, output)
    with _write_denial_fence(paths) as fence_mode:
        process_start = _collect_live_process_census()
        output_exists_after_fence = output.exists()
        if output_exists_after_fence != output_existed_before_fence:
            raise GcRecoveryError("recovery receipt existence changed during fence acquisition")
        publish_requested = not output_exists_after_fence
        if not publish_requested:
            if expected_recovery_receipt_sha256 is None:
                raise GcRecoveryError(
                    "existing recovery receipt requires an independent file commitment"
                )
            if _live_process_blockers(process_start, current_database=current):
                raise GcRecoveryError("live process census blocks recovery replay")
            receipt = _validate_gc_recovery_replay_core(
                current,
                baseline_database=baseline,
                archive_database=archive,
                admission_receipt_path=admission_receipt_path,
                expected_admission_receipt_sha256=expected_admission_receipt_sha256,
                expected_current_revision=expected_current_revision,
                expected_baseline_revision=expected_baseline_revision,
                recovery_receipt_path=output,
                expected_recovery_receipt_sha256=expected_recovery_receipt_sha256,
            )
            published = False
        else:
            if expected_recovery_receipt_sha256 is not None:
                raise GcRecoveryError(
                    "new recovery publication cannot predeclare a receipt file commitment"
                )
            receipt = _audit_gc_recovery_core(
                current,
                baseline_database=baseline,
                archive_database=archive,
                admission_receipt_path=admission_receipt_path,
                expected_admission_receipt_sha256=expected_admission_receipt_sha256,
                expected_current_revision=expected_current_revision,
                expected_baseline_revision=expected_baseline_revision,
                evidence_fence_mode=cast(
                    Literal["windows-deny-write", "posix-advisory"],
                    fence_mode,
                ),
                process_census_start=process_start,
            )
            published = False
        if fence_mode != "windows-deny-write":
            raise GcRecoveryError("recovery publication lacks a write-denial fence")
        if publish_requested:
            require_gc_recovery_receipt_fresh(
                receipt,
                minimum_remaining=_PUBLICATION_FRESHNESS_FLOOR,
            )
            published = _publish_new_gc_recovery_receipt_fenced(
                output,
                receipt,
                current_database=current,
                expected_runtime_git_commit=cast(
                    str,
                    preliminary.receipt.runtime_git_commit,
                ),
            )
        else:
            publication_census = _collect_live_process_census()
            if _live_process_blockers(publication_census, current_database=current):
                raise GcRecoveryError("live process census changed at publication boundary")
            _require_runtime_head_unchanged(preliminary.receipt)
        require_gc_recovery_receipt_fresh(receipt)
        return receipt, published


def _publish_new_gc_recovery_receipt_fenced(
    output: Path,
    receipt: GcRecoveryReceipt,
    *,
    current_database: Path,
    expected_runtime_git_commit: str,
) -> bool:
    """Link and validate a new receipt while its inode denies writes and deletion."""

    destination = Path(os.path.abspath(os.fspath(output)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    require_no_reparse_points(destination)
    parent_before = os.stat(destination.parent, follow_symlinks=False)
    encoded = (receipt.model_dump_json() + "\n").encode()
    staged: Path | None = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="xb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            staged = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        with _write_denial_fence((staged,)) as output_fence:
            if output_fence != "windows-deny-write":
                raise GcRecoveryError("new recovery receipt lacks a write-denial fence")
            staged_snapshot, staged_payload = read_stable_artifact(staged)
            if staged_payload != encoded:
                raise GcRecoveryError("staged recovery receipt bytes are inconsistent")
            require_canonical_text_artifact(staged_snapshot, receipt.model_dump_json())
            try:
                os.link(staged, destination)
            except FileExistsError as exc:
                raise ImmutableArtifactConflictError(
                    "recovery receipt appeared during no-clobber publication"
                ) from exc
            published = True
            parent_after = os.stat(destination.parent, follow_symlinks=False)
            if (int(parent_before.st_dev), int(parent_before.st_ino)) != (
                int(parent_after.st_dev),
                int(parent_after.st_ino),
            ):
                raise ImmutableArtifactConflictError(
                    "recovery receipt parent changed during publication"
                )
            require_no_reparse_points(destination)
            if not os.path.samefile(staged, destination):
                raise ImmutableArtifactConflictError(
                    "recovery receipt does not name the fenced staged inode"
                )
            output_snapshot, output_payload = read_stable_artifact(destination)
            published_receipt = GcRecoveryReceipt.model_validate_json(output_payload)
            if (
                published_receipt != receipt
                or published_receipt.report_sha256 != published_receipt.computed_report_sha256()
            ):
                raise GcRecoveryError("published recovery receipt is not the audited receipt")
            require_canonical_text_artifact(output_snapshot, receipt.model_dump_json())
            publication_census = _collect_live_process_census()
            if _live_process_blockers(
                publication_census,
                current_database=current_database,
            ):
                raise GcRecoveryError("live process census changed at publication boundary")
            canonical_runtime_git_commit(expected_runtime_git_commit)
            require_gc_recovery_receipt_fresh(receipt)
            assert_artifact_unchanged(output_snapshot)
        return True
    except Exception:
        if (
            published
            and staged is not None
            and destination.exists()
            and os.path.samefile(staged, destination)
        ):
            destination.unlink()
        raise
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _fenced_evidence_paths(
    admitted: AdmittedRecoveryEvidence,
    current: Path,
    baseline: Path,
    archive: Path,
) -> tuple[Path, ...]:
    return (
        current,
        baseline,
        archive,
        admitted.admission_artifact.path,
        admitted.baseline_authority_artifact.path,
        admitted.activation_receipt_artifact.path,
        admitted.activation_quiescence_artifact.path,
        admitted.runtime_db_gc_artifact.path,
        admitted.runtime_authority_artifact.path,
        admitted.event_log_artifact.path,
        admitted.report_artifact.path,
        admitted.terminal_artifact.path,
        admitted.quiescence_registry_artifact.path,
        admitted.process_census_artifact.path,
    )


def _require_expected_activation_receipt(
    admitted: AdmittedRecoveryEvidence,
    expected_file_sha256: str,
) -> None:
    if not re.fullmatch(_SHA256, expected_file_sha256):
        raise GcRecoveryError("expected activation receipt hash is invalid")
    if admitted.activation_receipt_artifact.file_sha256 != expected_file_sha256:
        raise GcRecoveryError("activation receipt differs from independent file commitment")


def _admit_paths(current: Path, baseline: Path, archive: Path) -> tuple[Path, Path, Path]:
    paths = tuple(Path(os.path.abspath(os.fspath(path))) for path in (current, baseline, archive))
    for path in paths:
        require_no_reparse_points(path)
        if not path.is_file():
            raise GcRecoveryError(f"recovery evidence is not a file: {path}")
    _require_no_sidecars(paths)
    for index, path in enumerate(paths):
        try:
            if path_aliases_any(path, set(paths[:index] + paths[index + 1 :])):
                raise GcRecoveryError("recovery evidence paths must be distinct files")
        except ImmutableArtifactConflictError as exc:
            raise GcRecoveryError(str(exc)) from exc
    return paths[0], paths[1], paths[2]


def _load_admission(
    path: Path,
    *,
    expected_file_sha256: str,
) -> AdmittedRecoveryEvidence:
    try:
        admission_snapshot, payload = read_stable_artifact(path)
        if admission_snapshot.file_sha256 != expected_file_sha256:
            raise GcRecoveryError("admission receipt differs from reviewed file commitment")
        receipt = GcRecoveryAdmissionReceipt.model_validate_json(payload)
        if receipt.receipt_sha256 != receipt.computed_receipt_sha256():
            raise GcRecoveryError("admission receipt self-seal is invalid")
        require_canonical_text_artifact(admission_snapshot, receipt.model_dump_json())
        authority_snapshot, authority_payload = read_stable_artifact(
            Path(receipt.baseline_authority_artifact)
        )
        baseline_authority = RecoveryBaselineAuthority.model_validate_json(authority_payload)
        activation_snapshot, activation_payload = read_stable_artifact(
            Path(baseline_authority.activation_receipt_artifact)
        )
        activation_quiescence_snapshot, activation_quiescence_payload = read_stable_artifact(
            Path(baseline_authority.activation_quiescence_artifact)
        )
        runtime_snapshot, runtime_payload = read_stable_artifact(
            Path(receipt.runtime_db_gc_artifact)
        )
        runtime_authority_snapshot, runtime_authority_payload = read_stable_artifact(
            Path(receipt.runtime_authority_artifact)
        )
        event_snapshot, event_payload = read_stable_artifact(Path(receipt.event_log_artifact))
        report_snapshot, report_payload = read_stable_artifact(Path(receipt.report_artifact))
        terminal_snapshot, terminal_payload = read_stable_artifact(Path(receipt.terminal_artifact))
        quiescence_snapshot, quiescence_payload = read_stable_artifact(
            Path(receipt.quiescence_registry_artifact)
        )
        census_snapshot, census_payload = read_stable_artifact(
            Path(receipt.process_census_artifact)
        )
    except GcRecoveryError:
        raise
    except (ImmutableArtifactConflictError, OSError, ValueError) as exc:
        raise GcRecoveryError(
            f"recovery admission artifact is invalid: {type(exc).__name__}: {exc}"
        ) from exc
    for label, snapshot, expected_sha, expected_size in (
        (
            "baseline authority",
            authority_snapshot,
            receipt.baseline_authority_artifact_sha256,
            None,
        ),
        (
            "runtime db_gc source",
            runtime_snapshot,
            receipt.runtime_db_gc_sha256,
            receipt.runtime_db_gc_artifact_size_bytes,
        ),
        (
            "activation receipt",
            activation_snapshot,
            baseline_authority.activation_receipt_artifact_sha256,
            baseline_authority.activation_receipt_artifact_size_bytes,
        ),
        (
            "activation quiescence receipt",
            activation_quiescence_snapshot,
            baseline_authority.activation_quiescence_artifact_sha256,
            baseline_authority.activation_quiescence_artifact_size_bytes,
        ),
        (
            "runtime authority",
            runtime_authority_snapshot,
            receipt.runtime_authority_artifact_sha256,
            receipt.runtime_authority_artifact_size_bytes,
        ),
        (
            "operation event log",
            event_snapshot,
            receipt.event_log_artifact_sha256,
            receipt.event_log_size_bytes,
        ),
        (
            "operation report",
            report_snapshot,
            receipt.report_artifact_sha256,
            receipt.report_size_bytes,
        ),
        (
            "operation terminal evidence",
            terminal_snapshot,
            receipt.terminal_artifact_sha256,
            receipt.terminal_artifact_size_bytes,
        ),
        (
            "quiescence registry",
            quiescence_snapshot,
            receipt.quiescence_registry_artifact_sha256,
            receipt.quiescence_registry_artifact_size_bytes,
        ),
        (
            "process census",
            census_snapshot,
            receipt.process_census_artifact_sha256,
            receipt.process_census_artifact_size_bytes,
        ),
    ):
        if snapshot.file_sha256 != expected_sha:
            raise GcRecoveryError(f"{label} differs from admission commitment")
        if expected_size is not None and snapshot.size_bytes != expected_size:
            raise GcRecoveryError(f"{label} size differs from admission commitment")
    try:
        activation_receipt = ActivationReceipt.model_validate_json(activation_payload)
        activation_quiescence = QuiescenceReceipt.model_validate_json(activation_quiescence_payload)
        runtime_authority = RecoveryRuntimeAuthority.model_validate_json(runtime_authority_payload)
        terminal = RecoveryTerminalEvidence.model_validate_json(terminal_payload)
        quiescence = RecoveryQuiescenceRegistry.model_validate_json(quiescence_payload)
        census = RecoveryProcessCensus.model_validate_json(census_payload)
        if baseline_authority.receipt_sha256 != (baseline_authority.computed_receipt_sha256()):
            raise ValueError("baseline authority self-seal is invalid")
        if activation_receipt.receipt_sha256 != activation_payload_sha256(activation_receipt):
            raise ValueError("activation receipt self-seal is invalid")
        if activation_quiescence.receipt_sha256 != quiescence_payload_sha256(activation_quiescence):
            raise ValueError("activation quiescence receipt self-seal is invalid")
        require_canonical_text_artifact(
            authority_snapshot,
            baseline_authority.model_dump_json(),
        )
        require_canonical_text_artifact(
            activation_snapshot,
            _canonical_json(activation_receipt.model_dump(mode="json")),
        )
        require_canonical_text_artifact(
            activation_quiescence_snapshot,
            _canonical_json(activation_quiescence.model_dump(mode="json")),
        )
        require_canonical_text_artifact(
            runtime_authority_snapshot,
            runtime_authority.model_dump_json(),
        )
        require_canonical_text_artifact(terminal_snapshot, terminal.model_dump_json())
        require_canonical_text_artifact(quiescence_snapshot, quiescence.model_dump_json())
        require_canonical_text_artifact(census_snapshot, census.model_dump_json())
        operation_report = _validate_operation_artifacts(
            receipt,
            runtime_authority=runtime_authority,
            terminal=terminal,
            quiescence=quiescence,
            census=census,
            runtime_payload=runtime_payload,
            event_payload=event_payload,
            report_payload=report_payload,
        )
        _validate_baseline_authority(
            receipt,
            baseline_authority=baseline_authority,
            activation_receipt=activation_receipt,
            activation_receipt_path=activation_snapshot.path,
            activation_quiescence=activation_quiescence,
        )
        _require_distinct_support_artifacts(
            (
                admission_snapshot,
                authority_snapshot,
                activation_snapshot,
                activation_quiescence_snapshot,
                runtime_snapshot,
                runtime_authority_snapshot,
                event_snapshot,
                report_snapshot,
                terminal_snapshot,
                quiescence_snapshot,
                census_snapshot,
            ),
            receipt=receipt,
        )
    except GcRecoveryError:
        raise
    except (ImmutableArtifactConflictError, OSError, ValueError) as exc:
        raise GcRecoveryError(
            f"recovery support artifact is invalid: {type(exc).__name__}: {exc}"
        ) from exc
    return AdmittedRecoveryEvidence(
        receipt=receipt,
        admission_artifact=admission_snapshot,
        baseline_authority_artifact=authority_snapshot,
        activation_receipt_artifact=activation_snapshot,
        activation_quiescence_artifact=activation_quiescence_snapshot,
        runtime_db_gc_artifact=runtime_snapshot,
        runtime_authority_artifact=runtime_authority_snapshot,
        event_log_artifact=event_snapshot,
        report_artifact=report_snapshot,
        terminal_artifact=terminal_snapshot,
        quiescence_registry_artifact=quiescence_snapshot,
        process_census_artifact=census_snapshot,
        process_census=census,
        operation_report=operation_report,
    )


class _GcArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(f"invalid admitted db_gc argv: {message}")


def _effective_gc_invocation(
    argv: tuple[str, ...],
    *,
    working_directory: Path,
) -> _EffectiveGcInvocation:
    if len(argv) < 2:
        raise ValueError("operation argv must name the Python runtime and db_gc script")
    script = argv[1].replace("\\", "/").casefold()
    if not (script == "execution/db_gc.py" or script.endswith("/execution/db_gc.py")):
        raise ValueError("operation argv must execute execution/db_gc.py directly")
    arguments = argv[2:]
    for option in ("--apply", "--db-path", "--policies", "--include-portfolio"):
        if _option_occurrences(arguments, option) != 1:
            raise ValueError(f"operation argv must contain exactly one {option}")
    parser = _GcArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--policies", required=True)
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--keep-quarters", type=int, default=20)
    parser.add_argument("--keep-fy", type=int, default=12)
    parser.add_argument("--include-portfolio", action="store_true")
    parser.add_argument("--tickers")
    parser.add_argument("--vacuum", action="store_true")
    parser.add_argument("--batch-size", type=int, default=20_000)
    parser.add_argument("--max-runtime-min", type=float, default=120.0)
    parser.add_argument("--vacuum-timeout-min", type=float, default=30.0)
    parser.add_argument("--lock-timeout-s", type=float, default=60.0)
    parser.add_argument("--ignore-protected-window", action="store_true")
    values = cast(dict[str, object], vars(parser.parse_args(arguments)))
    raw_database = values["db_path"]
    raw_policies = values["policies"]
    if not isinstance(raw_database, Path) or not isinstance(raw_policies, str):
        raise ValueError("operation argv parser returned invalid typed values")
    policies = tuple(policy.strip() for policy in raw_policies.split(",") if policy.strip())
    if policies != ("facts-depth",):
        raise ValueError("operation argv effective policies must be exactly facts-depth")
    if values["tickers"] is not None:
        raise ValueError("recovery audit requires an unsharded facts-depth operation")
    if values["vacuum"] is not False:
        raise ValueError("recovery audit does not admit an inline VACUUM")
    if values["ignore_protected_window"] is not False:
        raise ValueError("recovery audit does not admit protected-window bypass")
    script_path = _resolve_operation_path(Path(argv[1]), working_directory)
    database = _resolve_operation_path(raw_database, working_directory)
    return _EffectiveGcInvocation(
        script=script_path,
        database=database,
        archive=database.parent / "archive" / _GC_ARCHIVE_NAME,
        policies=policies,
        apply=values["apply"] is True,
        include_portfolio=values["include_portfolio"] is True,
    )


def _resolve_operation_path(value: Path, working_directory: Path) -> Path:
    candidate = value if value.is_absolute() else working_directory / value
    return Path(os.path.abspath(os.fspath(candidate)))


def _option_occurrences(arguments: tuple[str, ...], option: str) -> int:
    return sum(1 for value in arguments if value == option or value.startswith(f"{option}="))


def _validate_operation_artifacts(
    receipt: GcRecoveryAdmissionReceipt,
    *,
    runtime_authority: RecoveryRuntimeAuthority,
    terminal: RecoveryTerminalEvidence,
    quiescence: RecoveryQuiescenceRegistry,
    census: RecoveryProcessCensus,
    runtime_payload: bytes,
    event_payload: bytes,
    report_payload: bytes,
) -> GcRunReportEvidence | None:
    working_directory = Path(os.path.abspath(receipt.operation_working_directory))
    invocation = _effective_gc_invocation(
        receipt.operation_command_argv,
        working_directory=working_directory,
    )
    current = Path(os.path.abspath(receipt.current_database))
    archive = Path(os.path.abspath(receipt.archive_database))
    if invocation.database != current or invocation.database != Path(
        os.path.abspath(receipt.operation_database)
    ):
        raise ValueError("effective db_gc database differs from admitted current database")
    if invocation.script != Path(os.path.abspath(receipt.runtime_db_gc_artifact)):
        raise ValueError("effective db_gc script differs from admitted runtime source")
    if invocation.archive != archive or invocation.archive != Path(
        os.path.abspath(receipt.operation_archive_database)
    ):
        raise ValueError("effective db_gc archive differs from admitted archive database")
    if not invocation.apply or not invocation.include_portfolio:
        raise ValueError("effective db_gc invocation must apply to portfolio facts")
    if runtime_authority.git_commit != receipt.runtime_git_commit:
        raise ValueError("runtime commit differs from authority artifact")
    if Path(os.path.abspath(runtime_authority.repository)) != Path(
        os.path.abspath(receipt.runtime_repository)
    ):
        raise ValueError("runtime repository differs from authority artifact")
    if Path(os.path.abspath(runtime_authority.db_gc_artifact)) != Path(
        os.path.abspath(receipt.runtime_db_gc_artifact)
    ):
        raise ValueError("runtime db_gc path differs from authority artifact")
    if runtime_authority.db_gc_sha256 != receipt.runtime_db_gc_sha256:
        raise ValueError("runtime db_gc hash differs from authority artifact")
    canonical_task_paths = _require_runtime_git_blob(
        runtime_authority,
        runtime_payload=runtime_payload,
    )
    if terminal.command_argv_sha256 != _canonical_sha(receipt.operation_command_argv):
        raise ValueError("terminal evidence differs from admitted operation argv")
    if Path(os.path.abspath(terminal.working_directory)) != working_directory:
        raise ValueError("terminal evidence differs from admitted working directory")
    if terminal.stdout_sha256 != receipt.report_artifact_sha256:
        raise ValueError("terminal stdout commitment differs from operation report")
    if terminal.stderr_sha256 != receipt.event_log_artifact_sha256:
        raise ValueError("terminal stderr commitment differs from operation event log")
    if terminal.status != receipt.terminal_status or terminal.exit_code != (
        receipt.terminal_exit_code
    ):
        raise ValueError("terminal evidence differs from admitted terminal outcome")
    if not receipt.operation_started_at <= terminal.captured_at <= receipt.captured_at:
        raise ValueError("terminal evidence timestamp is outside the admitted operation")
    if quiescence.captured_at != receipt.captured_at:
        raise ValueError("quiescence registry timestamp differs from admission")
    _require_exact_casefold_set(
        receipt.expected_task_paths,
        canonical_task_paths,
        label="committed canonical task",
    )
    _require_exact_casefold_set(
        receipt.expected_task_paths,
        tuple(task.path for task in quiescence.tasks),
        label="quiescence registry task",
    )
    _require_exact_casefold_set(
        receipt.disabled_task_paths,
        tuple(task.path for task in quiescence.tasks),
        label="disabled task",
    )
    _require_exact_casefold_set(
        receipt.expected_service_names,
        tuple(service.name for service in quiescence.services),
        label="quiescence registry service",
    )
    _require_exact_casefold_set(
        receipt.stopped_service_names,
        tuple(service.name for service in quiescence.services),
        label="stopped service",
    )
    _require_exact_casefold_set(
        receipt.expected_listener_endpoints,
        tuple(listener.endpoint for listener in quiescence.listeners),
        label="quiescence registry listener",
    )
    _require_exact_casefold_set(
        receipt.inactive_listener_endpoints,
        tuple(listener.endpoint for listener in quiescence.listeners),
        label="inactive listener",
    )
    if census.captured_at != receipt.captured_at:
        raise ValueError("process census timestamp differs from admission")
    if census.scope != receipt.process_census_scope:
        raise ValueError("process census scope differs from admission")
    if census.command_sha256 != receipt.process_census_command_sha256:
        raise ValueError("process census command differs from admission")
    for observation in census.processes:
        if observation.command_line_status == "ok":
            argv = _parse_windows_command_line(cast(str, observation.command_line))
            if not argv:
                raise ValueError("process census command line parsed to an empty argv")
    denied = sum(
        observation.command_line_status == "access_denied" for observation in census.processes
    )
    writers = tuple(
        f"{observation.pid}:{evidence}"
        for observation in sorted(census.processes, key=lambda item: item.pid)
        if (
            evidence := _process_writer_evidence(
                observation,
                current_database=current,
            )
        )
        is not None
    )
    if receipt.process_census_total_count != len(census.processes):
        raise ValueError("process census total differs from raw census artifact")
    if receipt.process_command_line_access_denied_count != denied:
        raise ValueError("process census access-denied count differs from raw artifact")
    if receipt.database_writer_matches != writers:
        raise ValueError("database writer matches differ from raw census artifact")
    events = _parse_event_log(event_payload)
    if receipt.terminal_status == "complete" and any(
        event.event in {"gc_aborted", "gc_runtime_budget_exceeded", "gc_protected_window_abort"}
        for event in events
    ):
        raise ValueError("successful operation contains an abort event")
    report: GcRunReportEvidence | None = None
    if report_payload.strip():
        report = GcRunReportEvidence.model_validate_json(report_payload)
        _validate_operation_report(
            report,
            current=current,
            archive=archive,
            events=events,
        )
    elif receipt.terminal_status == "complete":
        raise ValueError("successful operation requires a structured terminal report")
    return report


def _validate_baseline_authority(
    receipt: GcRecoveryAdmissionReceipt,
    *,
    baseline_authority: RecoveryBaselineAuthority,
    activation_receipt: ActivationReceipt,
    activation_receipt_path: Path,
    activation_quiescence: QuiescenceReceipt,
) -> None:
    if Path(os.path.abspath(baseline_authority.baseline_database)) != Path(
        os.path.abspath(receipt.baseline_database)
    ):
        raise ValueError("baseline authority names a different rollback database")
    if baseline_authority.baseline_database_sha256 != receipt.baseline_database_sha256:
        raise ValueError("baseline authority hash differs from rollback snapshot")
    if baseline_authority.baseline_revision != receipt.baseline_revision:
        raise ValueError("baseline authority revision differs from admission")
    if baseline_authority.baseline_captured_at != receipt.baseline_captured_at:
        raise ValueError("baseline authority capture time differs from admission")
    if baseline_authority.baseline_capture_method != receipt.baseline_capture_method:
        raise ValueError("baseline authority capture method differs from admission")
    if (
        baseline_authority.baseline_quick_check != receipt.baseline_quick_check
        or baseline_authority.baseline_integrity_check != receipt.baseline_integrity_check
        or baseline_authority.baseline_foreign_key_violations
        != receipt.baseline_foreign_key_violations
    ):
        raise ValueError("baseline authority verification differs from admission")
    if baseline_authority.activated_database_sha256 != receipt.activated_database_sha256:
        raise ValueError("baseline authority activated hash differs from admission")
    if activation_receipt.mode is not ActivationMode.APPLY:
        raise ValueError("activation receipt is not an applied cutover")
    if (
        activation_receipt.status != "activated"
        or activation_receipt.rollback_restored
        or activation_receipt.failure is not None
    ):
        raise ValueError("activation receipt does not attest a successful activation")
    if Path(os.path.abspath(activation_receipt.receipt_path)) != Path(
        os.path.abspath(activation_receipt_path)
    ):
        raise ValueError("activation receipt path is not self-consistent")
    if Path(os.path.abspath(activation_receipt.repo_root)) != Path(
        os.path.abspath(_CANONICAL_REPOSITORY)
    ):
        raise ValueError("activation receipt repository is not the canonical checkout")
    if Path(os.path.abspath(activation_receipt.live_database)) != Path(
        os.path.abspath(receipt.current_database)
    ):
        raise ValueError("activation receipt names a different live database")
    if activation_receipt.expected_alembic_head != receipt.current_revision:
        raise ValueError("activation receipt head differs from the admitted current revision")
    activation_candidate = Path(os.path.abspath(activation_receipt.candidate_database))
    if activation_candidate in {
        Path(os.path.abspath(receipt.current_database)),
        Path(os.path.abspath(receipt.baseline_database)),
    }:
        raise ValueError("activation candidate aliases live or rollback evidence")
    if Path(os.path.abspath(activation_receipt.rollback_database)) != Path(
        os.path.abspath(receipt.baseline_database)
    ):
        raise ValueError("activation receipt names a different rollback snapshot")
    if (
        activation_receipt.live_sha256_before != receipt.baseline_database_sha256
        or activation_receipt.rollback_sha256 != receipt.baseline_database_sha256
    ):
        raise ValueError("activation receipt rollback hash differs from baseline")
    if activation_receipt.quiescence_receipt_sha256 != activation_quiescence.receipt_sha256:
        raise ValueError("activation receipt quiescence commitment is unresolved")
    if Path(os.path.abspath(activation_quiescence.live_database)) != Path(
        os.path.abspath(receipt.current_database)
    ):
        raise ValueError("activation quiescence names a different live database")
    if activation_quiescence.live_database_sha256 != receipt.baseline_database_sha256:
        raise ValueError("activation quiescence live hash differs from the rollback baseline")
    if not (
        activation_quiescence.captured_at
        <= activation_receipt.started_at
        <= activation_quiescence.valid_until
    ):
        raise ValueError("activation started outside its quiescence validity window")
    if (
        tuple(item.path for item in activation_quiescence.tasks)
        != activation_quiescence.expected_task_paths
        or activation_quiescence.expected_task_paths != receipt.expected_task_paths
        or tuple(item.name for item in activation_quiescence.services)
        != activation_quiescence.expected_service_names
        or activation_quiescence.expected_service_names != receipt.expected_service_names
        or tuple(item.endpoint for item in activation_quiescence.listeners)
        != activation_quiescence.expected_listener_endpoints
        or activation_quiescence.expected_listener_endpoints != receipt.expected_listener_endpoints
    ):
        raise ValueError("activation quiescence inventory differs from recovery admission")
    if not (
        activation_receipt.started_at
        <= baseline_authority.baseline_captured_at
        <= activation_receipt.completed_at
        <= receipt.operation_started_at
    ):
        raise ValueError("activation and baseline capture clocks are inconsistent")
    if (
        activation_receipt.active_sha256_after is None
        or activation_receipt.active_postcheck is None
        or activation_receipt.candidate_sha256 != activation_receipt.active_sha256_after
        or activation_receipt.candidate_precheck.sha256 != activation_receipt.candidate_sha256
        or activation_receipt.active_postcheck.sha256 != activation_receipt.active_sha256_after
        or Path(os.path.abspath(activation_receipt.candidate_precheck.database))
        != Path(os.path.abspath(activation_receipt.candidate_database))
        or Path(os.path.abspath(activation_receipt.active_postcheck.database))
        != Path(os.path.abspath(receipt.current_database))
        or activation_receipt.candidate_precheck.alembic_revision
        != activation_receipt.expected_alembic_head
        or activation_receipt.active_postcheck.alembic_revision
        != activation_receipt.expected_alembic_head
    ):
        raise ValueError("activation receipt database lineage is inconsistent")
    if activation_receipt.active_sha256_after != receipt.activated_database_sha256:
        raise ValueError("activation receipt active hash differs from admission")
    for verification in (
        activation_receipt.candidate_precheck,
        activation_receipt.active_postcheck,
    ):
        if (
            verification.quick_check != ("ok",)
            or verification.integrity_check != ("ok",)
            or verification.foreign_key_violations
        ):
            raise ValueError("activation receipt database verification is not clean")


def _require_runtime_git_blob(
    authority: RecoveryRuntimeAuthority,
    *,
    runtime_payload: bytes,
) -> tuple[str, ...]:
    repository = Path(os.path.abspath(authority.repository))
    require_no_reparse_points(repository)
    if repository != Path(os.path.abspath(_CANONICAL_REPOSITORY)):
        raise ValueError("runtime authority repository is not the canonical checkout")
    if authority.git_commit != canonical_runtime_git_commit():
        raise ValueError("runtime authority commit is not canonical checkout HEAD")
    source = _git_show_blob(repository, authority.git_commit, "execution/db_gc.py")
    if _normalized_source_sha256(source) != _normalized_source_sha256(runtime_payload):
        raise ValueError("runtime db_gc source differs from committed git blob")
    manifest_payload = _git_show_blob(
        repository,
        authority.git_commit,
        "cron/task_manifest.json",
    )
    manifest = CanonicalTaskManifest.model_validate_json(manifest_payload)
    paths = tuple(task.task_name for task in manifest.tasks)
    if len({path.casefold() for path in paths}) != len(paths):
        raise ValueError("committed task manifest contains duplicate task paths")
    return paths


def canonical_runtime_git_commit(expected: str | None = None) -> str:
    result = subprocess.run(  # nosec B603 -- fixed git executable and canonical repository
        ["git", "-C", str(_CANONICAL_REPOSITORY), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise GcRecoveryError("canonical runtime Git HEAD is unavailable")
    if expected is not None and commit != expected:
        raise GcRecoveryError("canonical runtime Git HEAD differs from expected commit")
    return commit


def _require_runtime_head_unchanged(receipt: GcRecoveryAdmissionReceipt) -> None:
    if receipt.runtime_git_commit is None:
        raise GcRecoveryError("recovery admission lacks a runtime Git commitment")
    canonical_runtime_git_commit(receipt.runtime_git_commit)


def _collect_live_process_census() -> LiveProcessCensus:
    observations: list[ProcessCensusObservation]
    if os.name == "nt":
        powershell = (
            Path(
                os.environ.get(
                    "SYSTEMROOT",
                    r"C:\Windows",
                )
            )
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        script = (
            "$ErrorActionPreference='Stop';"
            "@(Get-CimInstance Win32_Process | Sort-Object ProcessId | ForEach-Object {"
            "[pscustomobject]@{pid=[int]$_.ProcessId;"
            "parent_pid=[int]$_.ParentProcessId;image_name=[string]$_.Name;"
            "command_line=$_.CommandLine}}) | ConvertTo-Json -Compress -Depth 3"
        )
        result = subprocess.run(  # nosec B603 -- fixed system PowerShell and fixed collector
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise GcRecoveryError("live all-process census collector failed")
        try:
            raw: object = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GcRecoveryError("live all-process census output is invalid") from exc
        rows: list[object] = cast(list[object], raw) if isinstance(raw, list) else [raw]
        observations = []
        for row_raw in rows:
            if not isinstance(row_raw, dict):
                raise GcRecoveryError("live process census row is not an object")
            row = cast(dict[str, object], row_raw)
            command_line = row.get("command_line")
            observations.append(
                ProcessCensusObservation(
                    pid=int(cast(int | str, row.get("pid"))),
                    parent_pid=int(cast(int | str, row.get("parent_pid"))),
                    image_name=str(row.get("image_name") or "unknown"),
                    command_line_status=(
                        "ok" if isinstance(command_line, str) and command_line else "access_denied"
                    ),
                    command_line=(
                        command_line if isinstance(command_line, str) and command_line else None
                    ),
                    working_directory=None,
                )
            )
        collector: Literal[
            "powershell-get-ciminstance-win32-process/v1",
            "procfs-all-processes/v1",
        ] = "powershell-get-ciminstance-win32-process/v1"
    else:
        observations = []
        proc = Path("/proc")
        for process_dir in sorted(
            (item for item in proc.iterdir() if item.name.isdigit()),
            key=lambda item: int(item.name),
        ):
            pid = int(process_dir.name)
            try:
                stat_fields = (process_dir / "stat").read_text(encoding="utf-8").split()
                parent_pid = int(stat_fields[3])
                image_name = (process_dir / "comm").read_text(encoding="utf-8").strip()
                argv = tuple(
                    value.decode("utf-8", errors="strict")
                    for value in (process_dir / "cmdline").read_bytes().split(b"\0")
                    if value
                )
                command_line = subprocess.list2cmdline(argv) if argv else None
                try:
                    working_directory = str((process_dir / "cwd").resolve(strict=True))
                except OSError:
                    working_directory = None
            except (OSError, UnicodeError, ValueError):
                parent_pid = None
                image_name = "unknown"
                command_line = None
                working_directory = None
            observations.append(
                ProcessCensusObservation(
                    pid=pid,
                    parent_pid=parent_pid,
                    image_name=image_name or "unknown",
                    command_line_status="ok" if command_line else "access_denied",
                    command_line=command_line,
                    working_directory=working_directory,
                )
            )
        collector = "procfs-all-processes/v1"
    unsealed = LiveProcessCensus(
        schema_version="gc-recovery-live-process-census/v1",
        captured_at=datetime.now(UTC),
        collector=collector,
        exit_code=0,
        processes=tuple(observations),
        census_sha256="0" * 64,
    )
    return unsealed.model_copy(update={"census_sha256": unsealed.computed_census_sha256()})


def _live_process_blockers(
    census: LiveProcessCensus,
    *,
    current_database: Path,
) -> tuple[str, ...]:
    blockers: set[str] = set()
    if census.census_sha256 != census.computed_census_sha256():
        blockers.add("live_process_census_self_seal_invalid")
    if any(
        row.command_line_status == "access_denied"
        and _inaccessible_process_can_host_database_writer(row)
        for row in census.processes
    ):
        blockers.add("live_process_census_incomplete")
    if any(
        _process_writer_evidence(row, current_database=current_database) is not None
        for row in census.processes
    ):
        blockers.add("live_database_writer_present")
    return tuple(sorted(blockers))


def _inaccessible_process_can_host_database_writer(
    observation: ProcessCensusObservation,
) -> bool:
    """Refuse every unresolved process except inert Windows kernel pseudo-processes.

    Windows intentionally withholds command lines for its kernel pseudo-processes.
    They cannot host application code and remain recorded in the exhaustive census.
    Every user-mode executable, service host, unknown image, interpreter, or SQLite
    client is unresolved writer capability when its command line is inaccessible.
    The write-denial fence separately excludes open writers during the audit; this
    predicate prevents an idle or unregistered writer from being ignored afterward.
    """

    image_name = Path(observation.image_name).name.casefold()
    return image_name not in _INERT_INACCESSIBLE_WINDOWS_PROCESS_IMAGES


@contextmanager
def _write_denial_fence(paths: tuple[Path, ...]) -> Generator[str, None, None]:
    unique = tuple(dict.fromkeys(Path(os.path.abspath(path)) for path in paths))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handles: list[int] = []
        try:
            for path in unique:
                require_no_reparse_points(path)
                handle = create_file(
                    str(path),
                    0x80000000,
                    0x00000001,
                    None,
                    3,
                    0x00000080,
                    None,
                )
                if handle == wintypes.HANDLE(-1).value:
                    error = ctypes.get_last_error()
                    raise GcRecoveryError(
                        f"cannot acquire write-denial evidence fence for {path}: {error}"
                    )
                handles.append(int(handle))
            yield "windows-deny-write"
        finally:
            for handle in reversed(handles):
                close_handle(wintypes.HANDLE(handle))
        return

    import fcntl

    descriptors: list[int] = []
    try:
        for path in unique:
            require_no_reparse_points(path)
            descriptor = os.open(path, os.O_RDONLY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BaseException:
                os.close(descriptor)
                raise
            descriptors.append(descriptor)
        yield "posix-advisory"
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _normalized_source_sha256(payload: bytes) -> str:
    normalized = payload.replace(b"\r\n", b"\n")
    if b"\r" in normalized:
        raise ValueError("runtime source contains unsupported carriage returns")
    return hashlib.sha256(normalized).hexdigest()


def _git_show_blob(repository: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(  # nosec B603 -- fixed git executable; commit is regex-constrained and passed without a shell
        [
            "git",
            "-C",
            str(repository),
            "show",
            f"{commit}:{path}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(f"runtime commit blob is unavailable: {path}")
    return result.stdout


def _process_writer_evidence(
    observation: ProcessCensusObservation,
    *,
    current_database: Path,
) -> str | None:
    if observation.command_line_status != "ok" or observation.command_line is None:
        return None
    argv = _parse_windows_command_line(observation.command_line)
    normalized = tuple(value.replace("\\", "/").casefold() for value in argv)
    script_indexes = [
        index
        for index, value in enumerate(normalized)
        if value.endswith("/execution/db_gc.py") or value == "execution/db_gc.py"
    ]
    module_indexes = [
        index
        for index, value in enumerate(normalized[:-1])
        if value == "-m" and normalized[index + 1] == "execution.db_gc"
    ]
    raw_lower = observation.command_line.casefold()
    apply_signaled = "--apply" in normalized or "--apply" in raw_lower
    if len(script_indexes) + len(module_indexes) > 1:
        return "derived:ambiguous_db_gc_apply" if apply_signaled else None
    if not script_indexes and not module_indexes:
        if apply_signaled and ("db_gc" in raw_lower or "db-gc" in raw_lower):
            return "derived:ambiguous_db_gc_apply"
        executable = Path(observation.image_name).name.casefold()
        if executable in {"sqlite3", "sqlite3.exe"}:
            for value in argv[1:]:
                if Path(os.path.abspath(value)) == current_database:
                    return "derived:sqlite_cli_current_database"
        return None
    if script_indexes:
        invocation_prefix = (
            argv[script_indexes[0] - 1] if script_indexes[0] else "python",
            argv[script_indexes[0]],
        )
        arguments = argv[script_indexes[0] + 1 :]
    else:
        module_index = module_indexes[0]
        invocation_prefix = (
            argv[module_index - 1] if module_index else "python",
            "execution/db_gc.py",
        )
        arguments = argv[module_index + 2 :]
    if _option_occurrences(arguments, "--apply") != 1:
        return None
    working_directory = (
        Path(os.path.abspath(observation.working_directory))
        if observation.working_directory is not None
        else None
    )
    if working_directory is None:
        return "derived:db_gc_apply_without_working_directory"
    try:
        invocation = _effective_gc_invocation(
            (*invocation_prefix, *arguments),
            working_directory=working_directory,
        )
    except (IndexError, ValueError):
        return "derived:ambiguous_db_gc_apply"
    if invocation.database == current_database:
        return "derived:db_gc_apply_current_database"
    return None


def _parse_windows_command_line(command_line: str) -> tuple[str, ...]:
    """Parse one raw Windows command line using the documented CRT quote rules."""

    arguments: list[str] = []
    index = 0
    length = len(command_line)
    while index < length:
        while index < length and command_line[index] in " \t":
            index += 1
        if index >= length:
            break
        value: list[str] = []
        quoted = False
        while index < length:
            if command_line[index] in " \t" and not quoted:
                break
            backslashes = 0
            while index < length and command_line[index] == "\\":
                backslashes += 1
                index += 1
            if index < length and command_line[index] == '"':
                value.extend("\\" * (backslashes // 2))
                if backslashes % 2:
                    value.append('"')
                    index += 1
                    continue
                if quoted and index + 1 < length and command_line[index + 1] == '"':
                    value.append('"')
                    index += 2
                    continue
                quoted = not quoted
                index += 1
                continue
            value.extend("\\" * backslashes)
            if index < length and not (command_line[index] in " \t" and not quoted):
                value.append(command_line[index])
                index += 1
        if quoted:
            raise ValueError("process command line contains an unmatched quote")
        arguments.append("".join(value))
        while index < length and command_line[index] in " \t":
            index += 1
    return tuple(arguments)


def _parse_event_log(payload: bytes) -> tuple[GcEventEvidence, ...]:
    events: list[GcEventEvidence] = []
    for ordinal, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            events.append(GcEventEvidence.model_validate_json(raw_line))
        except ValueError as exc:
            raise ValueError(f"operation event log line {ordinal} is invalid") from exc
    return tuple(events)


def _validate_operation_report(
    report: GcRunReportEvidence,
    *,
    current: Path,
    archive: Path,
    events: tuple[GcEventEvidence, ...],
) -> None:
    if not report.apply:
        raise ValueError("operation report does not attest an applied run")
    if Path(os.path.abspath(report.db_path)) != current:
        raise ValueError("operation report database differs from admission")
    if Path(os.path.abspath(report.archive_path)) != archive:
        raise ValueError("operation report archive differs from admission")
    if len(report.policies) != 1:
        raise ValueError("operation report must contain exactly one policy")
    policy = report.policies[0]
    if policy.policy != "facts-depth" or not policy.applied:
        raise ValueError("operation report does not attest an applied facts-depth policy")
    if report.facts_depth_apply_preflight is None:
        raise ValueError("operation report lacks facts-depth structural preflight evidence")
    deleted = policy.rows_deleted.get("financial_facts", 0)
    if deleted < 0:
        raise ValueError("operation report contains a negative deletion count")
    event_names = tuple(event.event for event in events)
    if deleted == 0 and event_names:
        raise ValueError("no-op facts-depth report must have an empty event stream")
    if deleted:
        allowed = {
            "gc_facts_depth_ticker",
            "gc_append_only_guard_window",
            "gc_batch_commit",
        }
        if not set(event_names).issubset(allowed):
            raise ValueError("facts-depth event stream contains an unexpected event")
        if "gc_append_only_guard_window" not in event_names:
            raise ValueError("facts-depth event stream lacks the guard-window event")
        if not event_names or event_names[-1] != "gc_batch_commit":
            raise ValueError("facts-depth event stream lacks a terminal batch commit")


def _validate_report_against_audit(
    report: GcRunReportEvidence | None,
    *,
    financial_facts: ArchivedTableRecovery,
    metric_attempts: ArchivedTableRecovery,
    provenance_planes: tuple[ProvenancePlaneRecovery, ...],
) -> tuple[int, int]:
    if report is None:
        return 0, 0
    policy = report.policies[0]
    expected = {
        "financial_facts": financial_facts.missing_from_current_count,
        "metric_computation_attempts": metric_attempts.missing_from_current_count,
        **{plane.table: plane.lost_row_count for plane in provenance_planes},
    }
    if any(value < 0 for value in policy.rows_deleted.values()):
        raise ValueError("operation report contains a negative deletion count")
    unknown = set(policy.rows_deleted).difference(expected)
    if unknown:
        raise ValueError("operation report contains an unaudited deletion table")
    for table, observed in expected.items():
        if policy.rows_deleted.get(table, 0) != observed:
            raise ValueError(f"operation report deletion count differs for {table}")
    return expected["financial_facts"], expected["metric_computation_attempts"]


def _require_distinct_support_artifacts(
    snapshots: tuple[ImmutableArtifactSnapshot, ...],
    *,
    receipt: GcRecoveryAdmissionReceipt,
) -> None:
    database_paths = {
        Path(os.path.abspath(receipt.current_database)),
        Path(os.path.abspath(receipt.baseline_database)),
        Path(os.path.abspath(receipt.archive_database)),
    }
    for index, snapshot in enumerate(snapshots):
        _require_support_single_link(snapshot)
        other_support = {item.path for item in snapshots[index + 1 :]}
        if path_aliases_any(snapshot.path, database_paths | other_support):
            raise GcRecoveryError("recovery support artifacts must be distinct files")


def _require_admission_artifact_bindings(
    receipt: GcRecoveryAdmissionReceipt,
    *,
    current: ArtifactSnapshot,
    baseline: ArtifactSnapshot,
    archive: ArtifactSnapshot,
    expected_current_revision: str,
    expected_baseline_revision: str,
) -> None:
    for label, observed, expected_path, expected_sha in (
        (
            "current database",
            current,
            receipt.current_database,
            receipt.current_database_sha256,
        ),
        (
            "baseline database",
            baseline,
            receipt.baseline_database,
            receipt.baseline_database_sha256,
        ),
        (
            "archive database",
            archive,
            receipt.archive_database,
            receipt.archive_database_sha256,
        ),
    ):
        if Path(observed.path) != Path(os.path.abspath(expected_path)):
            raise GcRecoveryError(f"{label} path differs from admission commitment")
        if observed.sha256 != expected_sha:
            raise GcRecoveryError(f"{label} hash differs from admission commitment")
    if receipt.baseline_revision != expected_baseline_revision:
        raise GcRecoveryError("baseline revision differs from admission commitment")
    if receipt.current_revision != expected_current_revision:
        raise GcRecoveryError("current revision differs from admission commitment")
    if Path(os.path.abspath(receipt.operation_database)) != Path(current.path):
        raise GcRecoveryError("operation database differs from admitted current database")
    if Path(os.path.abspath(receipt.operation_archive_database)) != Path(archive.path):
        raise GcRecoveryError("operation archive differs from admitted archive database")


def _assert_admission_unchanged(admitted: AdmittedRecoveryEvidence) -> None:
    try:
        for snapshot in (
            admitted.admission_artifact,
            admitted.baseline_authority_artifact,
            admitted.activation_receipt_artifact,
            admitted.activation_quiescence_artifact,
            admitted.runtime_db_gc_artifact,
            admitted.runtime_authority_artifact,
            admitted.event_log_artifact,
            admitted.report_artifact,
            admitted.terminal_artifact,
            admitted.quiescence_registry_artifact,
            admitted.process_census_artifact,
        ):
            _require_support_single_link(snapshot)
            assert_artifact_unchanged(snapshot)
    except (ImmutableArtifactConflictError, OSError) as exc:
        raise GcRecoveryError("admission evidence changed during recovery audit") from exc


def _require_support_single_link(snapshot: ImmutableArtifactSnapshot) -> None:
    observed = snapshot.path.stat(follow_symlinks=False)
    if (int(observed.st_dev), int(observed.st_ino)) != (
        snapshot.device,
        snapshot.inode,
    ):
        raise ImmutableArtifactConflictError("support artifact path identity changed")
    if int(observed.st_nlink) != 1:
        raise ImmutableArtifactConflictError("support artifact has an external hardlink")


def _require_admission_fresh(receipt: GcRecoveryAdmissionReceipt) -> None:
    now = datetime.now(UTC)
    if now < receipt.captured_at.astimezone(UTC):
        raise GcRecoveryError("recovery admission is not yet valid")
    if now > receipt.valid_until.astimezone(UTC):
        raise GcRecoveryError("recovery admission expired before publication")


def require_gc_recovery_receipt_fresh(
    receipt: GcRecoveryReceipt,
    *,
    minimum_remaining: timedelta = timedelta(0),
) -> None:
    if datetime.now(UTC) + minimum_remaining > receipt.admission_valid_until.astimezone(UTC):
        raise GcRecoveryError("recovery receipt admission is expired or too near expiry")


def _snapshot_artifact(path: Path) -> ArtifactSnapshot:
    require_no_reparse_points(path)
    lexical_before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GcRecoveryError("recovery evidence is not a regular file")
        if _file_identity(lexical_before)[:2] != _file_identity(before)[:2]:
            raise GcRecoveryError("recovery evidence changed before its handle was pinned")
        if int(before.st_nlink) != 1:
            raise GcRecoveryError("recovery evidence must have exactly one filesystem link")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    lexical_after = path.lstat()
    if _file_identity(before) != _file_identity(after):
        raise GcRecoveryError("recovery evidence changed while it was hashed")
    if _file_identity(lexical_after)[:2] != _file_identity(after)[:2]:
        raise GcRecoveryError("recovery evidence path changed while it was hashed")
    return ArtifactSnapshot(
        path=str(path),
        device=int(after.st_dev),
        inode=int(after.st_ino),
        link_count=int(after.st_nlink),
        size_bytes=int(after.st_size),
        modified_time_ns=int(after.st_mtime_ns),
        changed_time_ns=int(after.st_ctime_ns),
        sha256=digest.hexdigest(),
    )


def _verify_database(
    connection: sqlite3.Connection,
    *,
    require_revision: bool,
) -> DatabaseVerification:
    revision: str | None = None
    if require_revision:
        rows = connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(f"expected one Alembic revision, observed {len(rows)}")
        revision = str(rows[0][0])
    return DatabaseVerification(
        revision=revision,
        quick_check=tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check")),
        integrity_check=tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check")),
        foreign_key_violations=sum(1 for _row in connection.execute("PRAGMA foreign_key_check")),
    )


def _audit_tables(
    current: sqlite3.Connection,
    baseline: sqlite3.Connection,
    archive: sqlite3.Connection,
) -> tuple[
    ArchivedTableRecovery,
    ArchivedTableRecovery,
    tuple[ProvenancePlaneRecovery, ...],
    int,
]:
    summaries: list[ArchivedTableRecovery] = []
    for table in _ARCHIVED_TABLES:
        summary = _audit_archived_table(current, baseline, archive, table)
        summaries.append(summary)
        if table == "financial_facts" and summary.primary_key_columns != ("id",):
            raise ValueError("financial_facts recovery requires primary key (id)")
    planes: list[ProvenancePlaneRecovery] = []
    for table, table_column, row_column in _PROVENANCE_PLANES:
        plane = _audit_provenance_plane(
            current,
            baseline,
            table=table,
            table_column=table_column,
            row_column=row_column,
        )
        planes.append(plane)
    governed_count = _count_governed_missing_fact_ids(current, baseline)
    return summaries[0], summaries[1], tuple(planes), governed_count


def _audit_archived_table(
    current: sqlite3.Connection,
    baseline: sqlite3.Connection,
    archive: sqlite3.Connection,
    table: str,
) -> ArchivedTableRecovery:
    columns = _table_columns(baseline, table)
    if _table_columns(current, table) != columns:
        raise ValueError(f"{table} baseline/current columns differ")
    archive_present = _table_exists(archive, table)
    if archive_present:
        # The run-keyed archive (execution/db_gc.py, 2026-08-03) carries
        # gc_run_id — the very column _audit_manifest requires for
        # row_level_run_identity_present — plus gc_source_rowid on rowid-keyed
        # tables. Excuse exactly that meta set; any other divergence from the
        # baseline mirror is still a hard error.
        archive_mirror = tuple(
            column
            for column in _table_columns(archive, table)
            if column not in _ARCHIVE_META_COLUMNS
        )
        if archive_mirror != columns:
            raise ValueError(f"{table} archive columns differ from baseline")
    primary_key = _primary_key_columns(baseline, table)
    if primary_key != ("id",) or _primary_key_columns(current, table) != primary_key:
        raise ValueError(f"{table} baseline/current primary key differs")
    baseline_groups = _grouped_rows(baseline, table, columns, primary_key)
    current_groups = _grouped_rows(current, table, columns, primary_key)
    archive_groups = (
        _grouped_rows(
            archive,
            table,
            columns,
            primary_key,
            max_distinct_rows_per_key=_ARCHIVE_VARIANT_LIMIT,
        )
        if archive_present
        else iter(())
    )
    baseline_group = next(baseline_groups, None)
    current_group = next(current_groups, None)
    archive_group = next(archive_groups, None)
    baseline_count = 0
    current_count = 0
    archive_count = 0
    archive_unique_count = 0
    missing_count = 0
    extra_count = 0
    changed_count = 0
    exact_count = 0
    without_archive_count = 0
    conflicting_count = 0
    duplicate_exact_count = 0
    variant_overflow_count = 0
    overlap_count = 0
    samples: list[str] = []
    while any(group is not None for group in (baseline_group, current_group, archive_group)):
        key = _minimum_group_key(baseline_group, current_group, archive_group)
        baseline_at_key = baseline_group if baseline_group and baseline_group.key == key else None
        current_at_key = current_group if current_group and current_group.key == key else None
        archive_at_key = archive_group if archive_group and archive_group.key == key else None
        if baseline_at_key is not None:
            if baseline_at_key.row_count != 1:
                raise ValueError(f"{table} contains a duplicate primary key")
            baseline_count += 1
        if current_at_key is not None:
            if current_at_key.row_count != 1:
                raise ValueError(f"{table} contains a duplicate current primary key")
            current_count += 1
        if archive_at_key is not None:
            archive_count += archive_at_key.row_count
            archive_unique_count += 1
            if archive_at_key.variant_overflow:
                variant_overflow_count += 1
            if current_at_key is not None:
                overlap_count += archive_at_key.row_count
        if baseline_at_key is not None and current_at_key is None:
            missing_count += 1
            if len(samples) < 20:
                samples.append(_key_text(key))
            baseline_row = next(iter(baseline_at_key.rows))
            exact_hits = 0 if archive_at_key is None else archive_at_key.rows[baseline_row]
            conflicting_hits = (
                0 if archive_at_key is None else archive_at_key.row_count - exact_hits
            )
            if exact_hits and not conflicting_hits:
                exact_count += 1
                duplicate_exact_count += max(0, exact_hits - 1)
            elif conflicting_hits:
                conflicting_count += 1
            else:
                without_archive_count += 1
        elif baseline_at_key is None and current_at_key is not None:
            extra_count += 1
        elif baseline_at_key is not None and current_at_key is not None:
            if baseline_at_key.rows != current_at_key.rows:
                changed_count += 1
        if baseline_at_key is not None:
            baseline_group = next(baseline_groups, None)
        if current_at_key is not None:
            current_group = next(current_groups, None)
        if archive_at_key is not None:
            archive_group = next(archive_groups, None)
    return ArchivedTableRecovery(
        table=table,
        primary_key_columns=primary_key,
        baseline_count=baseline_count,
        current_count=current_count,
        archive_row_count=archive_count,
        archive_unique_key_count=archive_unique_count,
        missing_from_current_count=missing_count,
        current_extra_count=extra_count,
        current_payload_changed_count=changed_count,
        missing_exact_in_archive_count=exact_count,
        missing_without_archive_count=without_archive_count,
        missing_conflicting_archive_count=conflicting_count,
        exact_archive_duplicate_row_count=duplicate_exact_count,
        archive_variant_overflow_key_count=variant_overflow_count,
        current_archive_overlap_row_count=overlap_count,
        missing_key_samples=tuple(samples),
    )


def _audit_provenance_plane(
    current: sqlite3.Connection,
    baseline: sqlite3.Connection,
    *,
    table: str,
    table_column: str,
    row_column: str,
) -> ProvenancePlaneRecovery:
    columns = _table_columns(baseline, table)
    if _table_columns(current, table) != columns:
        raise ValueError(f"{table} baseline/current columns differ")
    columns.index(table_column)
    columns.index(row_column)
    primary_key = _primary_key_columns(baseline, table)
    if not primary_key or _primary_key_columns(current, table) != primary_key:
        raise ValueError(f"{table} baseline/current primary key differs")
    baseline_groups = _grouped_rows(
        baseline,
        table,
        columns,
        primary_key,
        where_column=table_column,
        where_value="financial_facts",
    )
    current_groups = _grouped_rows(
        current,
        table,
        columns,
        primary_key,
        where_column=table_column,
        where_value="financial_facts",
    )
    baseline_group = next(baseline_groups, None)
    current_group = next(current_groups, None)
    baseline_count = 0
    current_count = 0
    lost_count = 0
    unexpected_count = 0
    while baseline_group is not None or current_group is not None:
        key = _minimum_group_key(baseline_group, current_group)
        baseline_at_key = baseline_group if baseline_group and baseline_group.key == key else None
        current_at_key = current_group if current_group and current_group.key == key else None
        if baseline_at_key is not None:
            baseline_count += baseline_at_key.row_count
        if current_at_key is not None:
            current_count += current_at_key.row_count
        if baseline_at_key is not None and current_at_key is None:
            lost_count += baseline_at_key.row_count
        elif baseline_at_key is None and current_at_key is not None:
            unexpected_count += current_at_key.row_count
        elif baseline_at_key is not None and current_at_key is not None:
            lost_count += sum((baseline_at_key.rows - current_at_key.rows).values())
            unexpected_count += sum((current_at_key.rows - baseline_at_key.rows).values())
        if baseline_at_key is not None:
            baseline_group = next(baseline_groups, None)
        if current_at_key is not None:
            current_group = next(current_groups, None)
    return ProvenancePlaneRecovery(
        table=table,
        baseline_candidate_row_count=baseline_count,
        current_candidate_row_count=current_count,
        lost_row_count=lost_count,
        unexpected_row_count=unexpected_count,
    )


def _count_governed_missing_fact_ids(
    current: sqlite3.Connection,
    baseline: sqlite3.Connection,
) -> int:
    missing = _missing_primary_keys(
        current,
        baseline,
        table="financial_facts",
        primary_key=("id",),
    )
    governed = _distinct_scoped_row_ids(
        baseline,
        table="fact_observation_revisions",
        table_column="fact_table",
        row_column="fact_row_id",
        table_value="financial_facts",
    )
    missing_id = next(missing, None)
    governed_id = next(governed, None)
    matched = 0
    while missing_id is not None and governed_id is not None:
        comparison = _compare_sqlite_values(missing_id, governed_id)
        if comparison == 0:
            matched += 1
            missing_id = next(missing, None)
            governed_id = next(governed, None)
        elif comparison < 0:
            missing_id = next(missing, None)
        else:
            governed_id = next(governed, None)
    return matched


def _missing_primary_keys(
    current: sqlite3.Connection,
    baseline: sqlite3.Connection,
    *,
    table: str,
    primary_key: tuple[str, ...],
) -> Iterator[object]:
    columns = _table_columns(baseline, table)
    baseline_groups = _grouped_rows(baseline, table, columns, primary_key)
    current_groups = _grouped_rows(current, table, columns, primary_key)
    baseline_group = next(baseline_groups, None)
    current_group = next(current_groups, None)
    while baseline_group is not None:
        if current_group is None:
            yield baseline_group.key[0]
            baseline_group = next(baseline_groups, None)
            continue
        comparison = _compare_sqlite_keys(baseline_group.key, current_group.key)
        if comparison == 0:
            baseline_group = next(baseline_groups, None)
            current_group = next(current_groups, None)
        elif comparison < 0:
            yield baseline_group.key[0]
            baseline_group = next(baseline_groups, None)
        else:
            current_group = next(current_groups, None)


def _distinct_scoped_row_ids(
    connection: sqlite3.Connection,
    *,
    table: str,
    table_column: str,
    row_column: str,
    table_value: str,
) -> Iterator[object]:
    previous: object = object()
    for values in _raw_rows(
        connection,
        table,
        (row_column,),
        order_by=(row_column,),
        where_column=table_column,
        where_value=table_value,
    ):
        row_id = values[0]
        if type(row_id) is not type(previous) or row_id != previous:
            yield row_id
            previous = row_id


def _audit_manifest(connection: sqlite3.Connection) -> ArchiveManifestSummary:
    if not _table_exists(connection, "gc_manifest"):
        return ArchiveManifestSummary(
            present=False,
            facts_depth_manifest_rows=0,
            source_rows_archived=(),
            run_at_values=(),
            row_level_run_identity_present=False,
        )
    rows = connection.execute(
        "SELECT run_at, source_table, rows_archived FROM gc_manifest "
        "WHERE policy = 'facts-depth' ORDER BY run_at, source_table"
    ).fetchall()
    totals: Counter[str] = Counter()
    run_at_values: set[str] = set()
    for run_at, source_table, rows_archived in rows:
        run_at_values.add(str(run_at))
        totals[str(source_table)] += int(rows_archived)
    archive_columns: set[str] = (
        set(_table_columns(connection, "financial_facts"))
        if _table_exists(connection, "financial_facts")
        else set()
    )
    return ArchiveManifestSummary(
        present=True,
        facts_depth_manifest_rows=len(rows),
        source_rows_archived=tuple(sorted(totals.items())),
        run_at_values=tuple(sorted(run_at_values)),
        row_level_run_identity_present=bool(
            archive_columns.intersection({"gc_run_id", "archive_run_id"})
        ),
    )


def _blockers(
    *,
    current_verification: DatabaseVerification,
    baseline_verification: DatabaseVerification,
    archive_verification: DatabaseVerification,
    expected_current_revision: str,
    expected_baseline_revision: str,
    delete_trigger_present: bool,
    delete_trigger_matches_baseline: bool,
    delete_trigger_matches_canonical: bool,
    archived_tables: tuple[ArchivedTableRecovery, ArchivedTableRecovery],
    provenance_planes: tuple[ProvenancePlaneRecovery, ...],
    governed_linked_count: int,
    manifest: ArchiveManifestSummary,
    retry_fk_ready: bool,
    retry_index_ready: bool,
    admission: GcRecoveryAdmissionReceipt,
    process_census: RecoveryProcessCensus,
) -> tuple[str, ...]:
    blockers: set[str] = set()
    if current_verification.revision != expected_current_revision:
        blockers.add("current_revision_mismatch")
    if baseline_verification.revision != expected_baseline_revision:
        blockers.add("baseline_revision_mismatch")
    if expected_baseline_revision != expected_current_revision:
        blockers.add("baseline_current_revision_contract_mismatch")
    for label, verification in (
        ("current", current_verification),
        ("baseline", baseline_verification),
        ("archive", archive_verification),
    ):
        if verification.quick_check != ("ok",):
            blockers.add(f"{label}_quick_check_failed")
        if verification.integrity_check != ("ok",):
            blockers.add(f"{label}_integrity_check_failed")
        if verification.foreign_key_violations:
            blockers.add(f"{label}_foreign_key_violations")
    if not delete_trigger_present:
        blockers.add("delete_trigger_missing")
    if not delete_trigger_matches_baseline:
        blockers.add("delete_trigger_differs_from_baseline")
    if not delete_trigger_matches_canonical:
        blockers.add("delete_trigger_differs_from_canonical")
    if not retry_fk_ready:
        blockers.add("facts_depth_retry_self_fk_not_ready")
    if not retry_index_ready:
        blockers.add("facts_depth_retry_index_not_ready")
    now = datetime.now(UTC)
    if now < admission.captured_at.astimezone(UTC):
        blockers.add("quiescence_receipt_not_yet_valid")
    if now > admission.valid_until.astimezone(UTC):
        blockers.add("quiescence_receipt_expired")
    if not admission.baseline_checkpointed:
        blockers.add("baseline_not_checkpointed")
    if not admission.baseline_sidecars_absent:
        blockers.add("baseline_sidecars_were_present")
    if admission.runtime_git_commit is None or admission.runtime_db_gc_sha256 is None:
        blockers.add("operation_runtime_identity_missing")
    if admission.terminal_status == "unknown":
        blockers.add("operation_terminal_status_unknown")
    if admission.terminal_status == "failed":
        blockers.add("operation_terminal_status_failed")
    if admission.report_size_bytes == 0:
        blockers.add("operation_terminal_report_empty")
    if any(
        row.command_line_status == "access_denied"
        and _inaccessible_process_can_host_database_writer(row)
        for row in process_census.processes
    ):
        blockers.add("process_census_incomplete")
    if admission.database_writer_matches:
        blockers.add("database_writer_still_present")
    for table in archived_tables:
        if table.missing_without_archive_count:
            blockers.add("archive_rows_missing")
        if table.missing_conflicting_archive_count:
            blockers.add("archive_payload_conflict")
        if table.current_payload_changed_count:
            blockers.add("current_payload_changed_since_baseline")
        if table.current_extra_count:
            blockers.add("current_rows_added_since_baseline")
        if table.missing_from_current_count and table.exact_archive_duplicate_row_count:
            blockers.add("archive_exact_duplicates_unbound")
        if table.archive_variant_overflow_key_count:
            blockers.add("archive_variant_limit_exceeded")
    if any(plane.lost_row_count for plane in provenance_planes):
        blockers.add("nonarchived_provenance_rows_lost")
    if any(plane.unexpected_row_count for plane in provenance_planes):
        blockers.add("nonarchived_provenance_rows_changed")
    fact_candidates = archived_tables[0].missing_from_current_count
    if governed_linked_count != fact_candidates:
        blockers.add("deletion_candidates_not_governed_linked")
    manifest_counts = dict(manifest.source_rows_archived)
    for table in archived_tables:
        if table.missing_from_current_count and (
            not manifest.present
            or manifest_counts.get(table.table, 0) < table.missing_from_current_count
        ):
            blockers.add("archive_manifest_incomplete")
    if any(table.missing_from_current_count for table in archived_tables) and not (
        manifest.row_level_run_identity_present
    ):
        blockers.add("archive_rows_lack_run_identity")
    return tuple(sorted(blockers))


def _facts_depth_retry_index_ready(connection: sqlite3.Connection) -> bool:
    rows = connection.execute("PRAGMA index_list('financial_facts')").fetchall()
    expected = next(
        (row for row in rows if str(row[1]) == "ix_0270_financial_facts_supersedes_id"),
        None,
    )
    if expected is None or bool(expected[2]) or bool(expected[4]):
        return False
    columns = tuple(
        str(row[2])
        for row in connection.execute("PRAGMA index_xinfo('ix_0270_financial_facts_supersedes_id')")
        if int(row[5]) == 1
    )
    return columns == ("supersedes_id",)


def _facts_depth_retry_fk_ready(connection: sqlite3.Connection) -> bool:
    rows = connection.execute("PRAGMA foreign_key_list('financial_facts')").fetchall()
    groups: dict[int, list[tuple[object, ...]]] = {}
    for raw_row in rows:
        row = tuple(raw_row)
        if len(row) < 8 or not isinstance(row[0], int):
            return False
        groups.setdefault(row[0], []).append(row)
    candidates = [
        group for group in groups.values() if any(str(row[3]) == "supersedes_id" for row in group)
    ]
    if len(candidates) != 1 or len(candidates[0]) != 1:
        return False
    row = candidates[0][0]
    if not isinstance(row[1], int):
        return False
    return (
        row[1] == 0
        and str(row[2]) == "financial_facts"
        and str(row[3]) == "supersedes_id"
        and str(row[4]) == "id"
        and str(row[5]).upper() == "NO ACTION"
        and str(row[6]).upper() == "NO ACTION"
        and str(row[7]).upper() == "NONE"
    )


def _schema_sql(connection: sqlite3.Connection, kind: str, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (kind, name),
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _normalized_sql(value: str) -> str:
    return " ".join(value.split()).casefold()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    _quote_identifier(table)
    rows = connection.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    if not rows:
        raise ValueError(f"required table is absent: {table}")
    return tuple(str(row[1]) for row in rows)


def _primary_key_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    return tuple(
        str(row[1])
        for row in sorted((row for row in rows if int(row[5]) > 0), key=lambda row: int(row[5]))
    )


def _grouped_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    primary_key: tuple[str, ...],
    *,
    where_column: str | None = None,
    where_value: object | None = None,
    max_distinct_rows_per_key: int | None = None,
) -> Iterator[_RowGroup]:
    key_indexes = tuple(columns.index(column) for column in primary_key)
    rows = _raw_rows(
        connection,
        table,
        columns,
        order_by=primary_key,
        where_column=where_column,
        where_value=where_value,
    )
    for key, grouped in groupby(
        rows,
        key=lambda values: tuple(values[index] for index in key_indexes),
    ):
        rows_at_key: Counter[CanonicalRow] = Counter()
        row_count = 0
        variant_overflow = False
        for values in grouped:
            row_count += 1
            canonical = _canonical_row(values)
            if canonical in rows_at_key:
                rows_at_key[canonical] += 1
            elif max_distinct_rows_per_key is None or len(rows_at_key) < max_distinct_rows_per_key:
                rows_at_key[canonical] = 1
            else:
                variant_overflow = True
        yield _RowGroup(
            key=key,
            row_count=row_count,
            rows=rows_at_key,
            variant_overflow=variant_overflow,
        )


def _raw_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    *,
    order_by: tuple[str, ...] = (),
    where_column: str | None = None,
    where_value: object | None = None,
) -> Iterator[tuple[object, ...]]:
    select_list = ", ".join(_quote_identifier(column) for column in columns)
    order_clause = (
        " ORDER BY " + ", ".join(_quote_identifier(column) for column in order_by)
        if order_by
        else ""
    )
    if where_column is None:
        sql = f"SELECT {select_list} FROM {_quote_identifier(table)}{order_clause}"  # nosec B608 -- identifiers come from validated SQLite schema metadata
        parameters: tuple[object, ...] = ()
    else:
        sql = (
            f"SELECT {select_list} FROM {_quote_identifier(table)} "  # nosec B608 -- identifiers come from validated SQLite schema metadata
            f"WHERE {_quote_identifier(where_column)} = ?{order_clause}"
        )
        parameters = (where_value,)
    for row in connection.execute(sql, parameters):
        yield tuple(row)


def _canonical_row(values: tuple[object, ...]) -> CanonicalRow:
    return tuple(_canonical_row_value(value) for value in values)


def _canonical_row_value(value: object) -> tuple[str, str | bytes]:
    if value is None:
        return ("null", "")
    if isinstance(value, int):
        return ("integer", str(value))
    if isinstance(value, float):
        if math.isnan(value):
            return ("real", "nan")
        if math.isinf(value):
            return ("real", "+inf" if value > 0 else "-inf")
        return ("real", value.hex())
    if isinstance(value, str):
        return ("text", value)
    if isinstance(value, bytes):
        return ("blob", value)
    raise ValueError(f"unsupported SQLite value type: {type(value).__name__}")


def _canonical_sqlite_value(value: object) -> tuple[str, object]:
    if value is None:
        return ("null", "")
    if isinstance(value, int):
        return ("integer", str(value))
    if isinstance(value, float):
        if math.isnan(value):
            return ("real", "nan")
        if math.isinf(value):
            return ("real", "+inf" if value > 0 else "-inf")
        return ("real", value.hex())
    if isinstance(value, str):
        return ("text", value)
    if isinstance(value, bytes):
        return ("blob", base64.b64encode(value).decode("ascii"))
    raise ValueError(f"unsupported SQLite value type: {type(value).__name__}")


def _minimum_group_key(*groups: _RowGroup | None) -> tuple[object, ...]:
    keys = [group.key for group in groups if group is not None]
    if not keys:
        raise ValueError("cannot select a key from empty row groups")
    selected = keys[0]
    for candidate in keys[1:]:
        if _compare_sqlite_keys(candidate, selected) < 0:
            selected = candidate
    return selected


def _compare_sqlite_keys(
    left: tuple[object, ...],
    right: tuple[object, ...],
) -> int:
    if len(left) != len(right):
        raise ValueError("SQLite primary-key arity changed during merge")
    for left_value, right_value in zip(left, right, strict=True):
        comparison = _compare_sqlite_values(left_value, right_value)
        if comparison:
            return comparison
    return 0


def _compare_sqlite_values(left: object, right: object) -> int:
    if type(left) is not type(right):
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return -1 if left < right else 1 if left > right else 0
        raise ValueError("SQLite primary-key storage class differs across evidence")
    if left is None:
        return 0
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return -1 if left < right else 1 if left > right else 0
    if isinstance(left, str) and isinstance(right, str):
        return -1 if left < right else 1 if left > right else 0
    if isinstance(left, bytes) and isinstance(right, bytes):
        return -1 if left < right else 1 if left > right else 0
    raise ValueError("unsupported SQLite primary-key storage class")


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid SQLite identifier: {value!r}")
    return f'"{value}"'


def _key_text(key: tuple[object, ...]) -> str:
    return _canonical_json([_canonical_sqlite_value(value) for value in key])


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_nlink),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_no_sidecars(paths: tuple[Path, ...]) -> None:
    found = tuple(
        str(sidecar)
        for path in paths
        for suffix in _SIDECAR_SUFFIXES
        if (sidecar := Path(f"{path}{suffix}")).exists()
    )
    if found:
        raise GcRecoveryError(f"sealed SQLite evidence has sidecars: {found}")


def _require_exact_casefold_set(
    expected: tuple[str, ...],
    observed: tuple[str, ...],
    *,
    label: str,
) -> None:
    expected_keys = tuple(value.casefold() for value in expected)
    observed_keys = tuple(value.casefold() for value in observed)
    if len(set(expected_keys)) != len(expected_keys):
        raise ValueError(f"{label} commitment contains duplicates")
    if len(set(observed_keys)) != len(observed_keys):
        raise ValueError(f"{label} observation contains duplicates")
    if set(expected_keys) != set(observed_keys):
        raise ValueError(f"{label} observation differs from commitment")
