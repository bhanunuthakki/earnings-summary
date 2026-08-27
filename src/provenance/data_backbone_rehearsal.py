"""Governed, source-preserving rehearsal primitives for the data backbone."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REPARSE_POINT = 0x400

# These tables contain owner state, accepted research judgments, or portfolio
# configuration. Derived FMP tables are intentionally excluded because corpus
# replay is expected to add recoverable observations to them.
PRESERVATION_CRITICAL_TABLES: tuple[str, ...] = (
    "advisor_memos",
    "analyst_notes",
    "capture_audit_log",
    "decision_drafts",
    "decision_nudges",
    "decisions",
    "insight_notes",
    "investor_calibration",
    "owner_profile_facts",
    "position_entries",
    "position_sizing_intent",
    "positioning_intents",
    "raw_capture_sessions",
    "thesis_evaluations",
    "thesis_ledger_entries",
    "thesis_state",
    "tracked_companies",
    "user_kpi_registry",
)


class RehearsalError(RuntimeError):
    """A rehearsal invariant failed; no production activation is authorized."""


class DatabaseReadMode(StrEnum):
    """Whether a read targets a mutable candidate or a closed source snapshot."""

    CANDIDATE = "candidate"
    CLOSED_IMMUTABLE_SOURCE = "closed_immutable_source"


class SwapRehearsalRolledBackError(RehearsalError):
    """A forced post-swap fault was recovered on throwaway paths."""

    def __init__(self, message: str, evidence: SwapRollbackEvidence) -> None:
        super().__init__(message)
        self.evidence = evidence


class CorpusEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    modified_at_ns: int = Field(ge=0)


class CorpusManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    entries: tuple[CorpusEntry, ...]
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.file_count != len(self.entries):
            raise ValueError("corpus file_count does not match entries")
        if self.total_bytes != sum(entry.size_bytes for entry in self.entries):
            raise ValueError("corpus total_bytes does not match entries")
        if self.manifest_sha256 != _manifest_sha(self.entries):
            raise ValueError("corpus manifest seal is invalid")
        return self


class TableCommitment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table_name: str = Field(min_length=1)
    present: bool
    row_count: int = Field(ge=0)
    logical_sha256: str = Field(pattern=_SHA256_PATTERN)


class TablePreservationEvidence(BaseModel):
    """Proof that every source row survived across a schema-changing migration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table_name: str = Field(min_length=1)
    source_present: bool
    candidate_present: bool
    source_row_count: int = Field(ge=0)
    candidate_row_count: int = Field(ge=0)
    primary_key_columns: tuple[str, ...]
    compared_columns: tuple[str, ...]
    dropped_source_columns: tuple[str, ...]
    added_candidate_columns: tuple[str, ...]
    source_projection_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_projection_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _preserved(self) -> Self:
        if self.source_present and not self.candidate_present:
            raise ValueError("source preservation table is absent from candidate")
        if self.candidate_row_count < self.source_row_count:
            raise ValueError("candidate has fewer rows than the source preservation table")
        if self.source_projection_sha256 != self.candidate_projection_sha256:
            raise ValueError("source row projection differs from candidate")
        if self.source_present and not self.primary_key_columns:
            raise ValueError("source preservation proof requires a primary key")
        return self


class DatabaseVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    alembic_revision: str = Field(min_length=1)
    quick_check: tuple[str, ...]
    integrity_check: tuple[str, ...]
    foreign_key_violations: int = Field(ge=0)


class DatabaseStorageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    suffix: Literal["", "-wal", "-shm", "-journal"]
    size_bytes: int = Field(ge=0)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    modified_at_ns: int = Field(ge=0)
    change_at_ns: int = Field(ge=0)
    inode: int = Field(ge=0)
    link_count: int = Field(ge=1)


class DatabaseStorageIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    entries: tuple[DatabaseStorageEntry, ...] = Field(min_length=1)
    aggregate_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _sealed(self) -> Self:
        if self.entries[0].suffix != "":
            raise ValueError("database storage identity must begin with the main file")
        expected = hashlib.sha256(
            _canonical_json([entry.model_dump(mode="json") for entry in self.entries]).encode(
                "utf-8"
            )
        ).hexdigest()
        if self.aggregate_sha256 != expected:
            raise ValueError("database storage identity seal is invalid")
        return self


class ClosedDatabaseStorageAttestation(BaseModel):
    """A sealed storage identity plus explicit proof every SQLite sidecar is absent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1)
    storage: DatabaseStorageIdentity
    wal_absent: Literal[True]
    shm_absent: Literal[True]
    journal_absent: Literal[True]

    @model_validator(mode="after")
    def _contains_only_the_closed_main_file(self) -> Self:
        if tuple(entry.suffix for entry in self.storage.entries) != ("",):
            raise ValueError("closed storage attestation cannot contain SQLite sidecars")
        return self


class OfflineTerminalReceipt(BaseModel):
    """The subset of refresh_cache's closed receipt needed by the gate."""

    model_config = ConfigDict(extra="allow", frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    status: Literal["DEGRADED_CORPUS"]
    discovered_file_count: int = Field(ge=0)
    selected_count: int = Field(gt=0)
    admitted_count: int = Field(ge=0)
    admitted_new_count: int = Field(ge=0)
    already_applied_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    corpus_count: int = Field(ge=0)
    failed_count: Literal[0]
    deferred_count: Literal[0]
    excluded_by_tier_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    network_calls: Literal[0]
    manifest_before_sha256: str = Field(pattern=_SHA256_PATTERN)
    manifest_after_sha256: str = Field(pattern=_SHA256_PATTERN)
    manifest_unchanged: Literal[True]
    mode: Literal["offline_corpus_only"]
    exit_code: Literal[2]

    @model_validator(mode="after")
    def _closed_replay_arithmetic(self) -> Self:
        if self.admitted_count != self.admitted_new_count + self.already_applied_count:
            raise ValueError("admitted count split is inconsistent")
        if self.admitted_count != self.corpus_count:
            raise ValueError("corpus count must equal admitted count")
        if self.eligible_count != self.selected_count:
            raise ValueError("eligible count must equal selected count")
        if (
            self.discovered_file_count
            != self.selected_count + self.excluded_by_tier_count + self.skipped_count
        ):
            raise ValueError("discovered corpus arithmetic is inconsistent")
        if self.selected_count != self.admitted_count + self.failed_count + self.deferred_count:
            raise ValueError("selected work arithmetic is inconsistent")
        if self.pending_count != self.selected_count:
            raise ValueError("pending count must equal selected corpus obligations")
        if self.manifest_sha256 != self.manifest_before_sha256:
            raise ValueError("manifest_sha256 must identify the before manifest")
        return self


class SwapRollbackEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mechanism: Literal["windows_replace_file", "portable_rename_pair"]
    live_path: str = Field(min_length=1)
    candidate_path: str = Field(min_length=1)
    rollback_path: str = Field(min_length=1)
    failed_candidate_path: str = Field(min_length=1)
    live_sha256_before: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    installed_sha256: str = Field(pattern=_SHA256_PATTERN)
    restored_live_sha256: str = Field(pattern=_SHA256_PATTERN)
    failed_candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    live_storage_before: ClosedDatabaseStorageAttestation
    candidate_storage_before: ClosedDatabaseStorageAttestation
    installed_live_storage: ClosedDatabaseStorageAttestation
    restored_live_storage: ClosedDatabaseStorageAttestation
    failed_candidate_storage: ClosedDatabaseStorageAttestation
    rollback_restored: Literal[True]

    @model_validator(mode="after")
    def _storage_commitments_match_file_commitments(self) -> Self:
        checks = (
            (self.live_storage_before, self.live_sha256_before),
            (self.candidate_storage_before, self.candidate_sha256),
            (self.installed_live_storage, self.installed_sha256),
            (self.restored_live_storage, self.restored_live_sha256),
            (self.failed_candidate_storage, self.failed_candidate_sha256),
        )
        if any(attestation.storage.entries[0].content_sha256 != sha for attestation, sha in checks):
            raise ValueError("rollback storage closure does not match file commitments")
        if self.installed_sha256 != self.candidate_sha256:
            raise ValueError("installed live identity does not match the candidate")
        if self.failed_candidate_sha256 != self.candidate_sha256:
            raise ValueError("failed candidate identity does not match the candidate")
        if self.restored_live_sha256 != self.live_sha256_before:
            raise ValueError("restored live identity does not match original live")
        if self.live_storage_before.path != self.live_path:
            raise ValueError("live storage path does not match rollback path")
        if self.candidate_storage_before.path != self.candidate_path:
            raise ValueError("candidate storage path does not match rollback path")
        if self.installed_live_storage.path != self.live_path:
            raise ValueError("installed live storage path does not match rollback path")
        if self.restored_live_storage.path != self.live_path:
            raise ValueError("restored live storage path does not match rollback path")
        if self.failed_candidate_storage.path != self.failed_candidate_path:
            raise ValueError("failed candidate storage path does not match rollback path")
        return self


class RuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    python_version: str = Field(min_length=1)
    python_executable: str = Field(min_length=1)
    python_executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    sqlite_version: str = Field(min_length=1)
    pydantic_version: str = Field(min_length=1)
    alembic_version: str = Field(min_length=1)
    platform: str = Field(min_length=1)


class CodeManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)


class CodeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    worktree_clean: bool
    porcelain_sha256: str = Field(pattern=_SHA256_PATTERN)
    entries: tuple[CodeManifestEntry, ...] = Field(min_length=1)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _sealed(self) -> Self:
        expected = hashlib.sha256(
            _canonical_json([entry.model_dump(mode="json") for entry in self.entries]).encode(
                "utf-8"
            )
        ).hexdigest()
        if self.manifest_sha256 != expected:
            raise ValueError("code manifest seal is invalid")
        return self


class UpgradeTerminalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["created", "upgraded", "bridged", "already_current"]
    db_path: str = Field(min_length=1)
    from_revision: str | None
    to_revision: str = Field(min_length=1)
    backup_path: str | None
    completed_at: str = Field(min_length=1)


class RehearsalReceipt(BaseModel):
    """Frozen, self-sealed terminal evidence for one source-only rehearsal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["data-backbone-rehearsal/v3"]
    mode: Literal["plan", "apply_rehearsal"]
    status: Literal["planned", "passed"]
    main_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    code_identity: CodeIdentity
    runtime: RuntimeIdentity
    repo_root: str = Field(min_length=1)
    source_database: str = Field(min_length=1)
    source_corpus: str = Field(min_length=1)
    work_directory: str = Field(min_length=1)
    receipt_path: str = Field(min_length=1)
    source_schema_before: str = Field(min_length=1)
    expected_schema_after: str = Field(min_length=1)
    source_database_before: DatabaseVerification
    source_storage_before: ClosedDatabaseStorageAttestation
    source_database_after_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_storage_after: ClosedDatabaseStorageAttestation | None = None
    candidate_database_before_upgrade: DatabaseVerification | None = None
    candidate_storage_before_upgrade: ClosedDatabaseStorageAttestation | None = None
    candidate_database_after: DatabaseVerification | None = None
    candidate_storage_after: ClosedDatabaseStorageAttestation | None = None
    source_corpus_before: CorpusManifest
    source_corpus_after: CorpusManifest | None = None
    copied_corpus_after: CorpusManifest | None = None
    preservation_before: tuple[TableCommitment, ...]
    preservation_after_upgrade: tuple[TableCommitment, ...] | None = None
    preservation_after: tuple[TableCommitment, ...] | None = None
    source_row_preservation: tuple[TablePreservationEvidence, ...] | None = None
    upgrade: UpgradeTerminalReceipt | None = None
    offline_replay: OfflineTerminalReceipt | None = None
    swap_rollback: SwapRollbackEvidence | None = None
    forced_failure_rollback: SwapRollbackEvidence | None = None
    required_disk_bytes: int = Field(ge=0)
    free_disk_bytes_at_preflight: int = Field(ge=0)
    cleanup_resume_policy: Literal[
        "retain_failure_evidence_and_resume_only_with_a_new_empty_work_directory"
    ]
    started_at: datetime
    completed_at: datetime
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _terminal_shape(self, info: ValidationInfo) -> Self:
        terminal = (
            self.source_database_after_sha256,
            self.source_storage_after,
            self.candidate_database_before_upgrade,
            self.candidate_storage_before_upgrade,
            self.candidate_database_after,
            self.candidate_storage_after,
            self.source_corpus_after,
            self.copied_corpus_after,
            self.preservation_after_upgrade,
            self.preservation_after,
            self.source_row_preservation,
            self.upgrade,
            self.offline_replay,
            self.swap_rollback,
            self.forced_failure_rollback,
        )
        if self.status == "planned" and any(item is not None for item in terminal):
            raise ValueError("planned receipt cannot claim apply evidence")
        if self.status == "passed" and any(item is None for item in terminal):
            raise ValueError("passed receipt requires complete apply evidence")
        if self.source_storage_before.path != self.source_database:
            raise ValueError("source storage-before path does not match source database")
        if self.source_database_before.content_sha256 != (
            self.source_storage_before.storage.entries[0].content_sha256
        ):
            raise ValueError("source verification-before does not match closed storage")
        if self.status == "passed":
            source_storage_after = self.source_storage_after
            source_database_after_sha256 = self.source_database_after_sha256
            candidate_database_before_upgrade = self.candidate_database_before_upgrade
            candidate_storage_before_upgrade = self.candidate_storage_before_upgrade
            candidate_database_after = self.candidate_database_after
            candidate_storage_after = self.candidate_storage_after
            if (
                source_storage_after is None
                or source_database_after_sha256 is None
                or candidate_database_before_upgrade is None
                or candidate_storage_before_upgrade is None
                or candidate_database_after is None
                or candidate_storage_after is None
            ):
                raise ValueError("passed receipt requires complete closed-storage evidence")
            if source_storage_after.path != self.source_database:
                raise ValueError("source storage-after path does not match source database")
            if (
                source_database_after_sha256
                != source_storage_after.storage.entries[0].content_sha256
            ):
                raise ValueError("source verification-after does not match closed storage")
            if (
                candidate_database_before_upgrade.path != candidate_storage_before_upgrade.path
                or candidate_database_before_upgrade.content_sha256
                != candidate_storage_before_upgrade.storage.entries[0].content_sha256
            ):
                raise ValueError("candidate verification-before does not match closed storage")
            if (
                candidate_database_after.path != candidate_storage_after.path
                or candidate_database_after.content_sha256
                != candidate_storage_after.storage.entries[0].content_sha256
            ):
                raise ValueError("candidate verification-after does not match closed storage")
        provisional = bool(info.context and info.context.get("allow_internal_provisional"))
        if not (provisional and self.receipt_sha256 == "0" * 64) and (
            self.receipt_sha256 != rehearsal_receipt_payload_sha256(self)
        ):
            raise ValueError("rehearsal receipt seal is invalid")
        return self


class RehearsalFailureReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["data-backbone-rehearsal-failure/v1"]
    status: Literal["failed"]
    requested_receipt_path: str = Field(min_length=1)
    durable_receipt_path: str = Field(min_length=1)
    failure_type: str = Field(min_length=1)
    failure_detail: str = Field(min_length=1)
    cleanup_resume_policy: Literal[
        "retain_failure_evidence_and_resume_only_with_a_new_empty_work_directory"
    ]
    completed_at: datetime
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _sealed(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"receipt_sha256"})
        expected = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
        if self.receipt_sha256 != expected:
            raise ValueError("failure receipt seal is invalid")
        return self


def rehearsal_receipt_payload_sha256(receipt: RehearsalReceipt) -> str:
    payload = receipt.model_dump(mode="json", exclude={"receipt_sha256"})
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def seal_rehearsal_receipt(**fields: object) -> RehearsalReceipt:
    provisional = RehearsalReceipt.model_validate(
        {"receipt_sha256": "0" * 64, **fields},
        strict=True,
        context={"allow_internal_provisional": True},
    )
    payload = provisional.model_dump(mode="python", exclude={"receipt_sha256"})
    return RehearsalReceipt.model_validate(
        {**payload, "receipt_sha256": rehearsal_receipt_payload_sha256(provisional)},
        strict=True,
    )


def seal_failure_receipt(
    *,
    requested_receipt_path: Path,
    durable_receipt_path: Path,
    failure_type: str,
    failure_detail: str,
    cleanup_resume_policy: str,
) -> RehearsalFailureReceipt:
    completed_at = datetime.now(UTC)
    provisional = RehearsalFailureReceipt.model_construct(
        schema_version="data-backbone-rehearsal-failure/v1",
        status="failed",
        requested_receipt_path=str(requested_receipt_path),
        durable_receipt_path=str(durable_receipt_path),
        failure_type=failure_type,
        failure_detail=failure_detail,
        cleanup_resume_policy=cleanup_resume_policy,
        completed_at=completed_at,
        receipt_sha256="0" * 64,
    )
    payload = provisional.model_dump(mode="json", exclude={"receipt_sha256"})
    seal = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return RehearsalFailureReceipt.model_validate(
        {
            **provisional.model_dump(mode="python", exclude={"receipt_sha256"}),
            "receipt_sha256": seal,
        },
        strict=True,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_storage_identity(path: Path) -> DatabaseStorageIdentity:
    entries: list[DatabaseStorageEntry] = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        storage_path = path if not suffix else Path(f"{path}{suffix}")
        if not storage_path.exists():
            continue
        storage_stat = storage_path.stat()
        entries.append(
            DatabaseStorageEntry(
                suffix=suffix,
                size_bytes=storage_stat.st_size,
                content_sha256=sha256_file(storage_path),
                modified_at_ns=storage_stat.st_mtime_ns,
                change_at_ns=storage_stat.st_ctime_ns,
                inode=storage_stat.st_ino,
                link_count=storage_stat.st_nlink,
            )
        )
    frozen = tuple(entries)
    if not frozen:
        raise RehearsalError(f"database does not exist: {path}")
    aggregate = hashlib.sha256(
        _canonical_json([entry.model_dump(mode="json") for entry in frozen]).encode("utf-8")
    ).hexdigest()
    return DatabaseStorageIdentity(entries=frozen, aggregate_sha256=aggregate)


def require_sidecar_free_database(path: Path) -> None:
    """Refuse active or incompletely closed SQLite storage before any open."""
    found = tuple(
        str(Path(f"{path}{suffix}"))
        for suffix in ("-wal", "-shm", "-journal")
        if Path(f"{path}{suffix}").exists()
    )
    if found:
        raise RehearsalError(
            "source database must be a closed restored snapshot with no SQLite sidecars: "
            + ", ".join(found)
        )


def checkpoint_and_close_candidate_database(path: Path) -> tuple[int, int, int]:
    """Checkpoint an isolated migrated candidate and prove its storage is closed."""
    connection = connect_sqlite(path, role=SQLiteConnectionRole.WRITER)
    try:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or len(row) != 3:
            raise RehearsalError("candidate WAL checkpoint returned an invalid result")
        checkpoint = (int(row[0]), int(row[1]), int(row[2]))
    finally:
        connection.close()
    busy, log_frames, checkpointed_frames = checkpoint
    if busy != 0 or log_frames != checkpointed_frames:
        raise RehearsalError(
            "candidate WAL checkpoint did not close cleanly: "
            f"busy={busy} log_frames={log_frames} checkpointed_frames={checkpointed_frames}"
        )
    remaining = tuple(
        str(Path(f"{path}{suffix}"))
        for suffix in ("-wal", "-shm", "-journal")
        if Path(f"{path}{suffix}").exists()
    )
    if remaining:
        raise RehearsalError(
            "candidate database remained open after WAL checkpoint: " + ", ".join(remaining)
        )
    return checkpoint


def attest_closed_database_storage(path: Path) -> ClosedDatabaseStorageAttestation:
    """Seal a database identity while making every closed-storage predicate explicit."""
    resolved = path.resolve(strict=True)
    require_sidecar_free_database(resolved)
    storage = database_storage_identity(resolved)
    require_sidecar_free_database(resolved)
    return ClosedDatabaseStorageAttestation(
        path=str(resolved),
        storage=storage,
        wal_absent=True,
        shm_absent=True,
        journal_absent=True,
    )


@contextmanager
def _open_database_read(
    database: Path,
    *,
    read_mode: DatabaseReadMode,
) -> Generator[sqlite3.Connection]:
    """Open one database read and enforce closed-source immutability around it."""
    resolved = database.resolve(strict=True)
    source_before: DatabaseStorageIdentity | None = None
    role = SQLiteConnectionRole.READ_ONLY
    if read_mode is DatabaseReadMode.CLOSED_IMMUTABLE_SOURCE:
        require_sidecar_free_database(resolved)
        source_before = database_storage_identity(resolved)
        role = SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY
    connection = connect_sqlite(resolved, role=role)
    try:
        yield connection
    finally:
        connection.close()
        if source_before is not None:
            require_sidecar_free_database(resolved)
            source_after = database_storage_identity(resolved)
            if source_after != source_before:
                raise RehearsalError("source database changed during immutable read")


def is_reparse_point(stat_result: os.stat_result) -> bool:
    return bool(int(getattr(stat_result, "st_file_attributes", 0)) & _REPARSE_POINT)


def _require_safe_root(root: Path) -> Path:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise RehearsalError(f"corpus root is unavailable: {root}") from exc
    if root.is_symlink() or is_reparse_point(root_stat) or not root.is_dir():
        raise RehearsalError(f"unsafe corpus entry: {root}")
    return root.resolve(strict=True)


def _safe_corpus_files(root: Path) -> tuple[Path, ...]:
    resolved_root = _require_safe_root(root)
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise RehearsalError(f"unable to enumerate corpus: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RehearsalError(f"unable to inspect corpus entry: {path}") from exc
            if entry.is_symlink() or is_reparse_point(entry_stat):
                raise RehearsalError(f"unsafe corpus entry: {path}")
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise RehearsalError(f"unsafe corpus entry: {path}")
            try:
                path.resolve(strict=True).relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                raise RehearsalError(f"corpus entry resolves outside root: {path}") from exc
            files.append(path)
    return tuple(sorted(files, key=lambda item: item.relative_to(root).as_posix()))


def build_corpus_manifest(root: Path) -> CorpusManifest:
    # Keep the lexical root until after lstat so a symlink/reparse root cannot
    # disappear behind Path.resolve() before the safety check.
    root = root.expanduser().absolute()
    entries: list[CorpusEntry] = []
    for path in _safe_corpus_files(root):
        before = path.lstat()
        content_sha = sha256_file(path)
        after = path.lstat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ino != after.st_ino
            or path.is_symlink()
            or is_reparse_point(after)
        ):
            raise RehearsalError(f"corpus file changed while hashing: {path}")
        entries.append(
            CorpusEntry(
                relative_path=path.relative_to(root).as_posix(),
                size_bytes=after.st_size,
                content_sha256=content_sha,
                modified_at_ns=after.st_mtime_ns,
            )
        )
    frozen = tuple(entries)
    return CorpusManifest(
        entries=frozen,
        file_count=len(frozen),
        total_bytes=sum(entry.size_bytes for entry in frozen),
        manifest_sha256=_manifest_sha(frozen),
    )


def _manifest_sha(entries: Iterable[CorpusEntry]) -> str:
    payload = [entry.model_dump(mode="json") for entry in entries]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _copy_regular_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        source_stat = source.stat()
        os.utime(temporary, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def copy_corpus_verified(
    source: Path,
    destination: Path,
    *,
    expected_source: CorpusManifest | None = None,
    copy_file: Callable[[Path, Path], None] = _copy_regular_file,
) -> tuple[CorpusManifest, CorpusManifest]:
    """Copy one immutable corpus, rejecting links, mutation, and byte drift."""
    if destination.exists():
        raise RehearsalError(f"corpus destination already exists: {destination}")
    before = expected_source or build_corpus_manifest(source)
    destination.mkdir(parents=True)
    for entry in before.entries:
        source_file = source / Path(entry.relative_path)
        destination_file = destination / Path(entry.relative_path)
        copy_file(source_file, destination_file)
    source_after = build_corpus_manifest(source)
    copied = build_corpus_manifest(destination)
    if source_after != before:
        raise RehearsalError("source corpus changed during verified copy")
    if copied != before:
        raise RehearsalError("copied corpus differs from source manifest")
    return source_after, copied


def online_backup_read_only(source: Path, destination: Path) -> None:
    """Back up one closed lab snapshot through an immutable read-only URI."""
    source = source.resolve(strict=True)
    require_sidecar_free_database(source)
    if destination.exists():
        raise RehearsalError(f"database destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with _open_database_read(
            source,
            read_mode=DatabaseReadMode.CLOSED_IMMUTABLE_SOURCE,
        ) as source_connection:
            destination_connection = connect_sqlite(
                temporary,
                role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
                schema_preflight=False,
            )
            try:
                source_connection.backup(destination_connection, pages=256)
            finally:
                destination_connection.close()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _canonical_cell(value: object) -> object:
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bytes):
        return {"bytes_base64": base64.b64encode(value).decode("ascii")}
    raise RehearsalError(f"unsupported SQLite value type: {type(value).__name__}")


def build_table_commitments(
    database: Path,
    *,
    tables: Iterable[str] = PRESERVATION_CRITICAL_TABLES,
    read_mode: DatabaseReadMode = DatabaseReadMode.CANDIDATE,
) -> tuple[TableCommitment, ...]:
    commitments: list[TableCommitment] = []
    with _open_database_read(database, read_mode=read_mode) as connection:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        for table in sorted(set(tables)):
            if table not in existing:
                commitments.append(
                    TableCommitment(
                        table_name=table,
                        present=False,
                        row_count=0,
                        logical_sha256=hashlib.sha256(
                            _canonical_json({"absent": table}).encode("utf-8")
                        ).hexdigest(),
                    )
                )
                continue
            identifier = _quote_identifier(table)
            columns = tuple(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({identifier})")
            )
            if not columns:
                raise RehearsalError(f"unable to inspect preservation table: {table}")
            order = ",".join(_quote_identifier(column) for column in columns)
            digest = hashlib.sha256()
            count = 0
            # Both identifiers are double-quoted names obtained from sqlite_master.
            query = f"SELECT * FROM {identifier} ORDER BY {order}"  # nosec B608
            for row in connection.execute(query):
                encoded = _canonical_json([_canonical_cell(value) for value in row])
                digest.update(encoded.encode("utf-8"))
                digest.update(b"\n")
                count += 1
            schema_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            digest.update(_canonical_json({"schema": schema_row[0]}).encode("utf-8"))
            commitments.append(
                TableCommitment(
                    table_name=table,
                    present=True,
                    row_count=count,
                    logical_sha256=digest.hexdigest(),
                )
            )
    return tuple(commitments)


def require_equal_commitments(
    expected: tuple[TableCommitment, ...],
    observed: tuple[TableCommitment, ...],
) -> None:
    if expected != observed:
        raise RehearsalError("preservation commitment mismatch")


def _projection_sha256(columns: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> str:
    digest = hashlib.sha256()
    digest.update(_canonical_json({"columns": columns}).encode("utf-8"))
    digest.update(b"\n")
    for row in rows:
        digest.update(_canonical_json([_canonical_cell(value) for value in row]).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def verify_source_rows_preserved(
    source: Path,
    candidate: Path,
    *,
    tables: Iterable[str] = PRESERVATION_CRITICAL_TABLES,
    source_read_mode: DatabaseReadMode = DatabaseReadMode.CLOSED_IMMUTABLE_SOURCE,
    candidate_read_mode: DatabaseReadMode = DatabaseReadMode.CANDIDATE,
) -> tuple[TablePreservationEvidence, ...]:
    """Prove source owner rows survive while allowing explicit migration deltas.

    Schema migrations may add owner-state rows or intentionally retire a column.
    The proof therefore projects every pre-migration row onto columns retained by
    the candidate, keys it by the source primary key, and requires that projection
    to exist unchanged after migration. Candidate-only rows and schema columns are
    recorded in the evidence; they are not allowed to hide source-row loss.
    """

    evidence: list[TablePreservationEvidence] = []
    with (
        _open_database_read(source, read_mode=source_read_mode) as source_connection,
        _open_database_read(candidate, read_mode=candidate_read_mode) as candidate_connection,
    ):
        source_tables = {
            str(row[0])
            for row in source_connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        candidate_tables = {
            str(row[0])
            for row in candidate_connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        for table in sorted(set(tables)):
            source_present = table in source_tables
            candidate_present = table in candidate_tables
            absent_sha = hashlib.sha256(
                _canonical_json({"absent_source_rows": table}).encode("utf-8")
            ).hexdigest()
            if not source_present:
                candidate_columns = (
                    tuple(
                        str(row[1])
                        for row in candidate_connection.execute(
                            f"PRAGMA table_info({_quote_identifier(table)})"
                        )
                    )
                    if candidate_present
                    else ()
                )
                candidate_count = (
                    int(
                        candidate_connection.execute(
                            f"SELECT COUNT(*) FROM {_quote_identifier(table)}"  # nosec B608
                        ).fetchone()[0]
                    )
                    if candidate_present
                    else 0
                )
                evidence.append(
                    TablePreservationEvidence(
                        table_name=table,
                        source_present=False,
                        candidate_present=candidate_present,
                        source_row_count=0,
                        candidate_row_count=candidate_count,
                        primary_key_columns=(),
                        compared_columns=(),
                        dropped_source_columns=(),
                        added_candidate_columns=candidate_columns,
                        source_projection_sha256=absent_sha,
                        candidate_projection_sha256=absent_sha,
                    )
                )
                continue
            if not candidate_present:
                raise RehearsalError(f"source preservation table is absent from candidate: {table}")

            source_info = tuple(
                source_connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
            )
            candidate_info = tuple(
                candidate_connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
            )
            source_columns = tuple(str(row[1]) for row in source_info)
            candidate_columns = tuple(str(row[1]) for row in candidate_info)
            primary_key_columns = tuple(
                str(row[1])
                for row in sorted(
                    (row for row in source_info if int(row[5]) > 0), key=lambda row: int(row[5])
                )
            )
            if not primary_key_columns:
                raise RehearsalError(f"source preservation table has no primary key: {table}")
            if any(column not in candidate_columns for column in primary_key_columns):
                raise RehearsalError(f"candidate dropped a preservation primary key: {table}")
            compared_columns = tuple(
                column for column in source_columns if column in candidate_columns
            )
            dropped_columns = tuple(
                column for column in source_columns if column not in candidate_columns
            )
            added_columns = tuple(
                column for column in candidate_columns if column not in source_columns
            )
            selected = ",".join(_quote_identifier(column) for column in compared_columns)
            ordered = ",".join(_quote_identifier(column) for column in primary_key_columns)
            source_rows = tuple(
                source_connection.execute(
                    f"SELECT {selected} FROM {_quote_identifier(table)} ORDER BY {ordered}"  # nosec B608
                )
            )
            candidate_rows = tuple(
                candidate_connection.execute(
                    f"SELECT {selected} FROM {_quote_identifier(table)} ORDER BY {ordered}"  # nosec B608
                )
            )
            key_indexes = tuple(compared_columns.index(column) for column in primary_key_columns)
            candidate_by_key = {
                tuple(row[index] for index in key_indexes): tuple(row) for row in candidate_rows
            }
            source_keys = tuple(tuple(row[index] for index in key_indexes) for row in source_rows)
            missing = tuple(key for key in source_keys if key not in candidate_by_key)
            if missing:
                raise RehearsalError(
                    f"candidate is missing {len(missing)} source row(s) from preservation table: {table}"
                )
            candidate_projection = tuple(candidate_by_key[key] for key in source_keys)
            source_projection = tuple(tuple(row) for row in source_rows)
            source_sha = _projection_sha256(compared_columns, source_projection)
            candidate_sha = _projection_sha256(compared_columns, candidate_projection)
            if source_sha != candidate_sha:
                raise RehearsalError(f"source rows changed in preservation table: {table}")
            evidence.append(
                TablePreservationEvidence(
                    table_name=table,
                    source_present=True,
                    candidate_present=True,
                    source_row_count=len(source_rows),
                    candidate_row_count=len(candidate_rows),
                    primary_key_columns=primary_key_columns,
                    compared_columns=compared_columns,
                    dropped_source_columns=dropped_columns,
                    added_candidate_columns=added_columns,
                    source_projection_sha256=source_sha,
                    candidate_projection_sha256=candidate_sha,
                )
            )
    return tuple(evidence)


def database_revision(
    database: Path,
    *,
    read_mode: DatabaseReadMode = DatabaseReadMode.CANDIDATE,
) -> str:
    with _open_database_read(database, read_mode=read_mode) as connection:
        rows = connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        raise RehearsalError("database must contain exactly one Alembic revision")
    return str(rows[0][0])


def verify_database(
    database: Path,
    *,
    expected_head: str,
    read_mode: DatabaseReadMode = DatabaseReadMode.CANDIDATE,
) -> DatabaseVerification:
    with _open_database_read(database, read_mode=read_mode) as connection:
        revisions = connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
        quick = tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check"))
        integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
        foreign_keys = tuple(connection.execute("PRAGMA foreign_key_check"))
    if len(revisions) != 1 or str(revisions[0][0]) != expected_head:
        raise RehearsalError(
            f"Alembic revision mismatch: expected={expected_head} observed={revisions!r}"
        )
    if quick != ("ok",):
        raise RehearsalError(f"SQLite quick_check failed: {quick!r}")
    if integrity != ("ok",):
        raise RehearsalError(f"SQLite integrity_check failed: {integrity!r}")
    if foreign_keys:
        raise RehearsalError(
            f"SQLite foreign-key check failed with {len(foreign_keys)} violation(s)"
        )
    return DatabaseVerification(
        path=str(database.resolve()),
        size_bytes=database.stat().st_size,
        content_sha256=sha256_file(database),
        alembic_revision=expected_head,
        quick_check=quick,
        integrity_check=integrity,
        foreign_key_violations=0,
    )


def validate_offline_receipt(
    stdout: str,
    *,
    return_code: int,
    copied_manifest_sha: str,
) -> OfflineTerminalReceipt:
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RehearsalError("offline replay did not emit one valid JSON receipt") from exc
    if not isinstance(payload, Mapping):
        raise RehearsalError("offline replay receipt must be a JSON object")
    try:
        receipt = OfflineTerminalReceipt.model_validate(payload)
    except ValueError as exc:
        raise RehearsalError(f"offline replay receipt is invalid: {exc}") from exc
    if return_code != receipt.exit_code:
        raise RehearsalError("offline replay terminal code differs from its receipt")
    if receipt.manifest_before_sha256 != copied_manifest_sha:
        raise RehearsalError("offline replay manifest differs from copied corpus commitment")
    if receipt.manifest_after_sha256 != copied_manifest_sha:
        raise RehearsalError("offline replay changed the copied corpus manifest")
    return receipt


def exercise_swap_and_rollback(
    rehearsal_root: Path,
    live: Path,
    candidate: Path,
    *,
    force_post_swap_failure: bool = False,
) -> SwapRollbackEvidence:
    """Install then restore throwaway files, preserving the failed candidate."""
    rehearsal_root = rehearsal_root.resolve(strict=True)
    live = live.resolve(strict=True)
    candidate = candidate.resolve(strict=True)
    rollback = live.with_name("rehearsal-rollback.db")
    failed = live.with_name("rehearsal-failed-candidate.db")
    for path in (live, candidate, rollback, failed):
        try:
            path.resolve(strict=False).relative_to(rehearsal_root)
        except ValueError as exc:
            raise RehearsalError("throwaway swap path escapes explicit rehearsal root") from exc
    if rollback.exists() or failed.exists():
        raise RehearsalError("throwaway rollback destinations must not already exist")
    live_storage_before = attest_closed_database_storage(live)
    candidate_storage_before = attest_closed_database_storage(candidate)
    live_sha = live_storage_before.storage.entries[0].content_sha256
    candidate_sha = candidate_storage_before.storage.entries[0].content_sha256
    mechanism: Literal["windows_replace_file", "portable_rename_pair"] = (
        "windows_replace_file" if os.name == "nt" else "portable_rename_pair"
    )
    _replace_throwaway_live(live=live, candidate=candidate, rollback=rollback)
    installed_live_storage = attest_closed_database_storage(live)
    installed_sha = installed_live_storage.storage.entries[0].content_sha256
    if installed_sha != candidate_sha:
        raise RehearsalError("throwaway swap installed bytes differ from candidate")
    try:
        if force_post_swap_failure:
            raise RehearsalError("forced post-swap failure")
    except Exception as exc:
        live.rename(failed)
        rollback.rename(live)
        restored_live_storage = attest_closed_database_storage(live)
        failed_candidate_storage = attest_closed_database_storage(failed)
        restored_sha = restored_live_storage.storage.entries[0].content_sha256
        failed_sha = failed_candidate_storage.storage.entries[0].content_sha256
        if restored_sha != live_sha:
            raise RehearsalError("throwaway rollback did not restore original live bytes") from exc
        evidence = SwapRollbackEvidence(
            mechanism=mechanism,
            live_path=str(live),
            candidate_path=str(candidate),
            rollback_path=str(rollback),
            failed_candidate_path=str(failed),
            live_sha256_before=live_sha,
            candidate_sha256=candidate_sha,
            installed_sha256=installed_sha,
            restored_live_sha256=restored_sha,
            failed_candidate_sha256=failed_sha,
            live_storage_before=live_storage_before,
            candidate_storage_before=candidate_storage_before,
            installed_live_storage=installed_live_storage,
            restored_live_storage=restored_live_storage,
            failed_candidate_storage=failed_candidate_storage,
            rollback_restored=True,
        )
        raise SwapRehearsalRolledBackError(f"{exc}; throwaway rollback restored", evidence) from exc

    live.rename(failed)
    rollback.rename(live)
    restored_live_storage = attest_closed_database_storage(live)
    failed_candidate_storage = attest_closed_database_storage(failed)
    restored_sha = restored_live_storage.storage.entries[0].content_sha256
    failed_sha = failed_candidate_storage.storage.entries[0].content_sha256
    if restored_sha != live_sha or failed_sha != candidate_sha:
        raise RehearsalError("throwaway rollback evidence differs from commitments")
    return SwapRollbackEvidence(
        mechanism=mechanism,
        live_path=str(live),
        candidate_path=str(candidate),
        rollback_path=str(rollback),
        failed_candidate_path=str(failed),
        live_sha256_before=live_sha,
        candidate_sha256=candidate_sha,
        installed_sha256=installed_sha,
        restored_live_sha256=restored_sha,
        failed_candidate_sha256=failed_sha,
        live_storage_before=live_storage_before,
        candidate_storage_before=candidate_storage_before,
        installed_live_storage=installed_live_storage,
        restored_live_storage=restored_live_storage,
        failed_candidate_storage=failed_candidate_storage,
        rollback_restored=True,
    )


def _replace_throwaway_live(*, live: Path, candidate: Path, rollback: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        replace_file = kernel32.ReplaceFileW
        replace_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        replace_file.restype = wintypes.BOOL
        if not replace_file(str(live), str(candidate), str(rollback), 0, None, None):
            error = ctypes.get_last_error()
            raise OSError(error, f"ReplaceFileW failed: {ctypes.FormatError(error)}")
        return
    live.rename(rollback)
    candidate.rename(live)


def require_disk_space(work_dir: Path, *, database_bytes: int, corpus_bytes: int) -> int:
    """Require conservative peak space: corpus plus four database generations."""
    parent = work_dir if work_dir.exists() else work_dir.parent
    required = corpus_bytes + (database_bytes * 4) + max(256 * 1024 * 1024, database_bytes // 20)
    free = shutil.disk_usage(parent).free
    if free < required:
        raise RehearsalError(f"insufficient disk space: required={required} free={free}")
    return required


def write_json_atomically(path: Path, payload: BaseModel | Mapping[str, object]) -> None:
    if path.exists():
        raise RehearsalError(f"refusing to overwrite receipt: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, BaseModel):
        raw: object = payload.model_dump(mode="json")
    else:
        raw = dict(payload)
    content = _canonical_json(raw) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
