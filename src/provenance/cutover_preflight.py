"""Prepare an isolated SQLite clone for an explicitly approved data cutover.

Dry-run is the default and performs no filesystem writes.  Apply mode is
deliberately narrower than a deployment: it creates one new SQLite snapshot,
upgrades only that isolated artifact, verifies it, and emits a sealed manifest.
The source database and the repository's live database are always read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from alembic import command
from provenance.compressed_candidate_clone import (
    MINIMUM_SAFE_FREE_BYTES,
    CompressedCloneReceipt,
    compressed_file_metrics,
    verify_compressed_clone_receipt,
)
from provenance.immutable_artifact import (
    ImmutableArtifactSnapshot,
    assert_artifact_unchanged,
    path_aliases_any,
    publish_text_no_clobber,
    read_stable_artifact,
    require_no_reparse_points,
)
from provenance.integrity_audit import AuditOptions, IntegrityAuditSummary, audit_connection
from provenance.latest_state_activation import (
    CandidateFileIdentity,
    candidate_file_identity,
    require_checkpointed_sidecars,
)
from runtime.job_runtime import JobLock, portfolio_db_path
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from sqlite_snapshot import SnapshotRequest, SnapshotResult, create_snapshot

_SCHEMA_VERSION = "data-cutover-preflight/v1"
_SNAPSHOT_CONFIG_VERSION = "data-cutover-isolated-clone/v1"
_MINIMUM_SPACE_RESERVE_BYTES = 64 * 1024 * 1024
_SPACE_MULTIPLIER = 3

CutoverLogger = Callable[[str, dict[str, object]], None]


class CutoverPreflightError(RuntimeError):
    """A safety or integrity gate prevented isolated cutover preparation."""


class CutoverMode(StrEnum):
    """Whether the preflight may create and migrate the isolated clone."""

    DRY_RUN = "dry-run"
    APPLY = "apply"


class CutoverRequest(BaseModel):
    """Explicit paths and bounded audit parameters for one preflight."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path
    source_path: Path
    destination_path: Path
    live_database_path: Path | None = None
    mode: CutoverMode = CutoverMode.DRY_RUN
    audit_sample_limit: int = Field(default=20, ge=1, le=500)
    minimum_space_reserve_bytes: int = Field(
        default=_MINIMUM_SPACE_RESERVE_BYTES,
        ge=0,
    )
    space_multiplier: int = Field(default=_SPACE_MULTIPLIER, ge=2, le=10)

    @field_validator(
        "repo_root",
        "source_path",
        "destination_path",
        "live_database_path",
    )
    @classmethod
    def _absolute_path(cls, value: Path | None) -> Path | None:
        return None if value is None else value.expanduser().resolve()

    @model_validator(mode="after")
    def _safe_paths(self) -> CutoverRequest:
        live_path = self.resolved_live_database_path
        if self.source_path == self.destination_path:
            raise ValueError("destination_path must be distinct from source_path")
        if self.destination_path == live_path:
            raise ValueError("destination_path must not be the live database")
        return self

    @property
    def resolved_live_database_path(self) -> Path:
        """Return the explicit live database identity used by the refusal gate."""
        return (
            self.live_database_path
            if self.live_database_path is not None
            else (self.repo_root / "data" / "portfolio.db").resolve()
        )


class CheckoutIdentity(BaseModel):
    """Committed code identity that fixes the migration implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    clean: bool


class MigrationFileDigest(BaseModel):
    """One migration in graph order, tied to its committed file bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)
    revision: str = Field(min_length=1)
    down_revisions: tuple[str, ...]
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MigrationPlan(BaseModel):
    """Single-head Alembic plan sealed into the preflight manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_alembic_head: str = Field(min_length=1)
    ordered_migration_files: tuple[MigrationFileDigest, ...]


class DatabaseIdentity(BaseModel):
    """Stable file and schema identity for a SQLite database."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    byte_size: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alembic_revision: str = Field(min_length=1)


class SQLiteVerification(BaseModel):
    """The three SQLite checks required on each cutover boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quick_check: tuple[str, ...]
    foreign_key_check: tuple[tuple[str | int | float | None, ...], ...]
    integrity_check: tuple[str, ...]
    clean: bool


class VerificationStage(BaseModel):
    """SQLite and evidence-integrity results for one database state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sqlite: SQLiteVerification
    integrity_audit: IntegrityAuditSummary


class FreeSpaceEstimate(BaseModel):
    """Conservative capacity check for snapshot plus migration working space."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    probe_path: str = Field(min_length=1)
    source_byte_size: int = Field(ge=1)
    multiplier: int = Field(ge=2)
    minimum_reserve_bytes: int = Field(ge=0)
    required_free_bytes: int = Field(ge=1)
    available_free_bytes: int = Field(ge=0)
    sufficient: bool


class DestinationArtifact(BaseModel):
    """Final post-migration identity of the isolated destination."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    byte_size: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alembic_revision: str = Field(min_length=1)
    snapshot_manifest_path: str = Field(min_length=1)


class CutoverPreflightManifest(BaseModel):
    """Canonical, self-authenticating output contract for the preflight."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = _SCHEMA_VERSION
    generated_at: datetime
    mode: CutoverMode
    status: Literal["ready", "applied"]
    source: DatabaseIdentity
    source_unchanged: bool
    destination_path: str = Field(min_length=1)
    live_database_path: str = Field(min_length=1)
    checkout: CheckoutIdentity
    migration_plan: MigrationPlan
    free_space: FreeSpaceEstimate
    source_before: VerificationStage
    clone_before: VerificationStage | None = None
    clone_after: VerificationStage | None = None
    destination_artifact: DestinationArtifact | None = None
    manifest_path: str | None = None
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExistingCloneUpgradeRequest(BaseModel):
    """Upgrade one already-admitted isolated compressed clone in place."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo_root: Path
    database_path: Path
    compressed_clone_receipt: Path
    receipt_path: Path
    expected_source_revision: str = Field(min_length=1, max_length=128)
    expected_target_revision: str = Field(min_length=1, max_length=128)
    operation_recorded_at: datetime
    minimum_free_bytes: int = Field(ge=MINIMUM_SAFE_FREE_BYTES)

    @field_validator("repo_root", "database_path", "compressed_clone_receipt", "receipt_path")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("operation_recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operation_recorded_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _not_live(self) -> ExistingCloneUpgradeRequest:
        live = portfolio_db_path(self.repo_root).resolve()
        if self.database_path == live:
            raise ValueError("existing isolated clone must not be the live database")
        protected = {
            self.database_path,
            self.compressed_clone_receipt,
            live,
            *(Path(f"{self.database_path}{suffix}") for suffix in ("-wal", "-shm", "-journal")),
        }
        if path_aliases_any(self.receipt_path, protected):
            raise ValueError("upgrade receipt aliases a protected artifact")
        return self


class ExistingCloneUpgradeReceipt(BaseModel):
    """Self-authenticating migration evidence for one compressed clone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["existing-isolated-clone-upgrade/v1"] = (
        "existing-isolated-clone-upgrade/v1"
    )
    database_path: str
    compressed_clone_receipt_path: str
    compressed_clone_receipt_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compressed_clone_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upgrade_intent_path: str
    upgrade_intent_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upgrade_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkout: CheckoutIdentity
    migration_plan: MigrationPlan
    database_before: DatabaseIdentity
    database_after: DatabaseIdentity
    file_identity_before: CandidateFileIdentity
    file_identity_after: CandidateFileIdentity
    sqlite_before: SQLiteVerification
    sqlite_after: SQLiteVerification
    compressed_before: bool
    compressed_after: bool
    compressed_size_before: int = Field(ge=0)
    compressed_size_after: int = Field(ge=0)
    free_bytes_before: int = Field(ge=0)
    free_bytes_after: int = Field(ge=0)
    minimum_free_bytes: int = Field(ge=MINIMUM_SAFE_FREE_BYTES)
    operation_recorded_at: datetime
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExistingCloneUpgradeIntent(BaseModel):
    """Immutable recovery boundary written before the Alembic mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["existing-isolated-clone-upgrade-intent/v1"] = (
        "existing-isolated-clone-upgrade-intent/v1"
    )
    database_path: str
    receipt_path: str
    compressed_clone_receipt_path: str
    compressed_clone_receipt_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compressed_clone_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkout: CheckoutIdentity
    migration_plan: MigrationPlan
    database_before: DatabaseIdentity
    database_instance_id: str | None = Field(
        default=None,
        pattern=r"^database-instance:[0-9a-f]{32}$",
    )
    file_identity_before: CandidateFileIdentity
    sqlite_before: SQLiteVerification
    compressed_before: bool
    compressed_size_before: int = Field(ge=0)
    free_bytes_before: int = Field(ge=0)
    expected_source_revision: str
    expected_target_revision: str
    minimum_free_bytes: int = Field(ge=MINIMUM_SAFE_FREE_BYTES)
    operation_recorded_at: datetime
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def verify_existing_clone_upgrade_intent(intent: ExistingCloneUpgradeIntent) -> bool:
    payload = intent.model_dump(mode="json", exclude={"intent_sha256"})
    return intent.intent_sha256 == _digest_text(_canonical_json(payload))


def verify_existing_clone_upgrade_receipt(receipt: ExistingCloneUpgradeReceipt) -> bool:
    payload = receipt.model_dump(mode="json", exclude={"receipt_sha256"})
    return receipt.receipt_sha256 == _digest_text(_canonical_json(payload))


def prepare_cutover(
    request: CutoverRequest,
    *,
    logger: CutoverLogger | None = None,
) -> CutoverPreflightManifest:
    """Run a read-only preflight or prepare one new isolated destination clone."""
    _validate_static_inputs(request)
    checkout = _checkout_identity(request.repo_root)
    migration_plan = _migration_plan(request.repo_root)
    if request.mode is CutoverMode.APPLY:
        _require_apply_checkout(request.repo_root, checkout, migration_plan)
        _require_unoccupied_destination(request.destination_path)

    source_identity_before = _database_identity(request.source_path)
    source_before = _verification_stage(
        request.source_path,
        repo_root=request.repo_root,
        sample_limit=request.audit_sample_limit,
    )
    _require_clean_stage("source", source_before)
    free_space = _free_space_estimate(request, source_identity_before.byte_size)
    if not free_space.sufficient:
        raise CutoverPreflightError(
            "insufficient destination free space: "
            f"required={free_space.required_free_bytes}, "
            f"available={free_space.available_free_bytes}"
        )

    _emit(
        logger,
        "data_cutover_preflight_ready",
        mode=request.mode,
        source_path=request.source_path,
        destination_path=request.destination_path,
        expected_alembic_head=migration_plan.expected_alembic_head,
    )
    if request.mode is CutoverMode.DRY_RUN:
        source_identity_after = _database_identity(request.source_path)
        source_unchanged = source_identity_after == source_identity_before
        if not source_unchanged:
            raise CutoverPreflightError("source database changed during dry-run preflight")
        return _seal_manifest(
            generated_at=datetime.now(UTC),
            mode=request.mode,
            status="ready",
            source=source_identity_before,
            source_unchanged=True,
            request=request,
            checkout=checkout,
            migration_plan=migration_plan,
            free_space=free_space,
            source_before=source_before,
        )

    write_set = _destination_write_set(request.destination_path)
    with JobLock(request.repo_root, "prepare-data-cutover", [write_set]):
        _require_unoccupied_destination(request.destination_path)
        locked_checkout = _checkout_identity(request.repo_root)
        locked_migration_plan = _migration_plan(request.repo_root)
        _require_apply_checkout(
            request.repo_root,
            locked_checkout,
            locked_migration_plan,
        )
        if locked_checkout != checkout or locked_migration_plan != migration_plan:
            raise CutoverPreflightError(
                "committed checkout changed before isolated clone preparation"
            )
        if _database_identity(request.source_path) != source_identity_before:
            raise CutoverPreflightError("source database changed before isolated snapshot")
        snapshot = create_snapshot(
            SnapshotRequest(
                source_path=request.source_path,
                destination_path=request.destination_path,
                code_config_version=_SNAPSHOT_CONFIG_VERSION,
            ),
            logger=logger,
        )
        clone_before = _verification_stage(
            request.destination_path,
            repo_root=request.repo_root,
            sample_limit=request.audit_sample_limit,
        )
        _require_clean_stage("clone before migration", clone_before)
        _upgrade_clone(
            repo_root=request.repo_root,
            destination_path=request.destination_path,
            expected_head=migration_plan.expected_alembic_head,
        )
        clone_after = _verification_stage(
            request.destination_path,
            repo_root=request.repo_root,
            sample_limit=request.audit_sample_limit,
        )
        _require_clean_stage("clone after migration", clone_after)
        destination = _destination_artifact(
            request.destination_path,
            snapshot,
            migration_plan.expected_alembic_head,
        )
        final_checkout = _checkout_identity(request.repo_root)
        final_migration_plan = _migration_plan(request.repo_root)
        _require_apply_checkout(
            request.repo_root,
            final_checkout,
            final_migration_plan,
        )
        if final_checkout != checkout or final_migration_plan != migration_plan:
            raise CutoverPreflightError(
                "committed checkout changed during isolated clone preparation"
            )
        source_unchanged = _database_identity(request.source_path) == source_identity_before
        if not source_unchanged:
            raise CutoverPreflightError("source database changed during isolated clone preparation")
        manifest_path = _cutover_manifest_path(request.destination_path)
        manifest = _seal_manifest(
            generated_at=datetime.now(UTC),
            mode=request.mode,
            status="applied",
            source=source_identity_before,
            source_unchanged=True,
            request=request,
            checkout=checkout,
            migration_plan=migration_plan,
            free_space=free_space,
            source_before=source_before,
            clone_before=clone_before,
            clone_after=clone_after,
            destination_artifact=destination,
            manifest_path=str(manifest_path),
        )
        _write_manifest_atomically(manifest_path, manifest)

    _emit(
        logger,
        "data_cutover_clone_prepared",
        destination_path=request.destination_path,
        manifest_path=manifest.manifest_path,
        manifest_sha256=manifest.manifest_sha256,
    )
    return manifest


def canonical_manifest_json(manifest: CutoverPreflightManifest) -> str:
    """Return the unique compact JSON representation used by the SHA seal."""
    return json.dumps(
        manifest.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def verify_manifest_sha256(manifest: CutoverPreflightManifest) -> bool:
    """Verify the embedded digest over every field except the digest itself."""
    payload = manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    return manifest.manifest_sha256 == _digest_text(_canonical_json(payload))


def upgrade_existing_isolated_clone(
    request: ExistingCloneUpgradeRequest,
) -> ExistingCloneUpgradeReceipt:
    """Upgrade and receipt an admitted clone under one recoverable ownership boundary."""

    database = request.database_path
    clone_receipt_path = request.compressed_clone_receipt
    receipt_path = request.receipt_path
    intent_path = Path(f"{receipt_path}.intent.json")
    live = portfolio_db_path(request.repo_root).resolve()
    resources = [
        "portfolio-db",
        f"sqlite:{database}",
        f"artifact:{receipt_path}",
        f"artifact:{intent_path}",
    ]
    with JobLock(request.repo_root, "upgrade-existing-isolated-clone", resources):
        for path in (
            request.repo_root,
            database,
            clone_receipt_path,
            receipt_path,
            intent_path,
            live,
        ):
            require_no_reparse_points(path)
        if not database.is_file() or not clone_receipt_path.is_file():
            raise CutoverPreflightError("isolated clone or compressed-clone receipt is missing")
        if path_aliases_any(database, {live}):
            raise CutoverPreflightError("isolated clone aliases the configured live database")
        clone_snapshot, clone_payload = read_stable_artifact(clone_receipt_path)
        try:
            clone_receipt = CompressedCloneReceipt.model_validate_json(clone_payload)
        except ValueError as exc:
            raise CutoverPreflightError("compressed-clone receipt is malformed") from exc
        if not verify_compressed_clone_receipt(clone_receipt):
            raise CutoverPreflightError("compressed-clone receipt commitment is invalid")
        if Path(clone_receipt.destination_database).resolve() != database:
            raise CutoverPreflightError("compressed-clone receipt names a different database")
        checkout = _checkout_identity(request.repo_root)
        migration_plan = _migration_plan(request.repo_root)
        _require_apply_checkout(request.repo_root, checkout, migration_plan)
        if migration_plan.expected_alembic_head != request.expected_target_revision:
            raise CutoverPreflightError("migration head differs from the requested target revision")
        if receipt_path.exists():
            return _load_completed_existing_clone_upgrade(
                request=request,
                clone_snapshot=clone_snapshot,
                clone_receipt=clone_receipt,
                checkout=checkout,
                migration_plan=migration_plan,
            )

        require_checkpointed_sidecars(database)
        current = _database_identity(database)
        if intent_path.exists():
            intent_snapshot, intent_payload = read_stable_artifact(intent_path)
            try:
                intent = ExistingCloneUpgradeIntent.model_validate_json(intent_payload)
            except ValueError as exc:
                raise CutoverPreflightError("clone upgrade intent is malformed") from exc
            if not verify_existing_clone_upgrade_intent(intent):
                raise CutoverPreflightError("clone upgrade intent commitment is invalid")
            _verify_upgrade_intent(
                intent,
                request=request,
                clone_snapshot=clone_snapshot,
                clone_receipt=clone_receipt,
                checkout=checkout,
                migration_plan=migration_plan,
            )
        else:
            if (
                current.alembic_revision != request.expected_source_revision
                or current.sha256 != clone_receipt.destination_database_sha256
                or current.byte_size != clone_receipt.logical_size_bytes
            ):
                raise CutoverPreflightError(
                    "isolated clone differs from its compressed-clone receipt"
                )
            compressed_before, compressed_size_before = compressed_file_metrics(database)
            free_before = _available_free_bytes(database.parent)
            sqlite_before = _sqlite_verification(database)
            if not compressed_before or not sqlite_before.clean:
                raise CutoverPreflightError("isolated clone failed pre-upgrade admission")
            if free_before < request.minimum_free_bytes:
                raise CutoverPreflightError(
                    "isolated clone upgrade lacks required free-space headroom"
                )
            intent = _build_upgrade_intent(
                request=request,
                clone_snapshot=clone_snapshot,
                clone_receipt=clone_receipt,
                checkout=checkout,
                migration_plan=migration_plan,
                database_before=current,
                database_instance_id=_database_runtime_identity(database),
                file_identity_before=candidate_file_identity(database),
                sqlite_before=sqlite_before,
                compressed_before=compressed_before,
                compressed_size_before=compressed_size_before,
                free_bytes_before=free_before,
            )
            publish_text_no_clobber(intent_path, intent.model_dump_json())
            intent_snapshot, _intent_payload = read_stable_artifact(intent_path)

        assert_artifact_unchanged(clone_snapshot)
        assert_artifact_unchanged(intent_snapshot)
        require_no_reparse_points(database)
        if path_aliases_any(database, {live}):
            raise CutoverPreflightError("isolated clone became an alias of the live database")
        if current.alembic_revision == request.expected_source_revision:
            if (
                current != intent.database_before
                or candidate_file_identity(database) != intent.file_identity_before
            ):
                raise CutoverPreflightError(
                    "isolated clone changed after upgrade intent publication"
                )
            _upgrade_clone(
                repo_root=request.repo_root,
                destination_path=database,
                expected_head=request.expected_target_revision,
            )
        elif current.alembic_revision == request.expected_target_revision:
            recovered_identity = candidate_file_identity(database)
            recovered_instance = _database_runtime_identity(database)
            if (
                recovered_identity.device != intent.file_identity_before.device
                or recovered_identity.inode != intent.file_identity_before.inode
                or not _recovery_runtime_identity_is_valid(
                    source_identity=intent.database_instance_id,
                    recovered_identity=recovered_instance,
                    migration_plan=migration_plan,
                )
            ):
                raise CutoverPreflightError("upgrade recovery candidate is a replacement database")
        else:
            raise CutoverPreflightError(
                "upgrade stopped at an intermediate revision; restore the admitted clone"
            )

        require_checkpointed_sidecars(database)
        database_after = _database_identity(database)
        file_identity_after = candidate_file_identity(database)
        sqlite_after = _sqlite_verification(database)
        compressed_after, compressed_size_after = compressed_file_metrics(database)
        free_after = _available_free_bytes(database.parent)
        assert_artifact_unchanged(clone_snapshot)
        assert_artifact_unchanged(intent_snapshot)
        if (
            _checkout_identity(request.repo_root) != checkout
            or _migration_plan(request.repo_root) != migration_plan
        ):
            raise CutoverPreflightError("checkout or migration plan changed during clone upgrade")
        if database_after.alembic_revision != request.expected_target_revision:
            raise CutoverPreflightError("isolated clone did not reach the expected target revision")
        if not sqlite_after.clean or not compressed_after:
            raise CutoverPreflightError("upgraded clone failed SQLite or compression verification")
        if free_after < request.minimum_free_bytes:
            raise CutoverPreflightError("isolated clone upgrade consumed required headroom")
        fields: dict[str, object] = {
            "checkout": checkout,
            "compressed_after": compressed_after,
            "compressed_before": intent.compressed_before,
            "compressed_clone_receipt_file_sha256": clone_snapshot.file_sha256,
            "compressed_clone_receipt_path": str(clone_snapshot.path),
            "compressed_clone_receipt_sha256": clone_receipt.receipt_sha256,
            "compressed_size_after": compressed_size_after,
            "compressed_size_before": intent.compressed_size_before,
            "database_after": database_after,
            "database_before": intent.database_before,
            "database_path": str(database),
            "file_identity_after": file_identity_after,
            "file_identity_before": intent.file_identity_before,
            "free_bytes_after": free_after,
            "free_bytes_before": intent.free_bytes_before,
            "migration_plan": migration_plan,
            "minimum_free_bytes": request.minimum_free_bytes,
            "operation_recorded_at": request.operation_recorded_at,
            "schema_version": "existing-isolated-clone-upgrade/v1",
            "sqlite_after": sqlite_after,
            "sqlite_before": intent.sqlite_before,
            "upgrade_intent_file_sha256": intent_snapshot.file_sha256,
            "upgrade_intent_path": str(intent_snapshot.path),
            "upgrade_intent_sha256": intent.intent_sha256,
        }
        draft = ExistingCloneUpgradeReceipt.model_validate(fields | {"receipt_sha256": "0" * 64})
        payload = draft.model_dump(mode="json", exclude={"receipt_sha256"})
        receipt = draft.model_copy(
            update={"receipt_sha256": _digest_text(_canonical_json(payload))}
        )
        publish_text_no_clobber(receipt_path, receipt.model_dump_json())
        return receipt


def _build_upgrade_intent(
    *,
    request: ExistingCloneUpgradeRequest,
    clone_snapshot: ImmutableArtifactSnapshot,
    clone_receipt: CompressedCloneReceipt,
    checkout: CheckoutIdentity,
    migration_plan: MigrationPlan,
    database_before: DatabaseIdentity,
    database_instance_id: str | None,
    file_identity_before: CandidateFileIdentity,
    sqlite_before: SQLiteVerification,
    compressed_before: bool,
    compressed_size_before: int,
    free_bytes_before: int,
) -> ExistingCloneUpgradeIntent:
    fields: dict[str, object] = {
        "checkout": checkout,
        "compressed_before": compressed_before,
        "compressed_clone_receipt_file_sha256": clone_snapshot.file_sha256,
        "compressed_clone_receipt_path": str(clone_snapshot.path),
        "compressed_clone_receipt_sha256": clone_receipt.receipt_sha256,
        "compressed_size_before": compressed_size_before,
        "database_before": database_before,
        "database_instance_id": database_instance_id,
        "database_path": str(request.database_path),
        "expected_source_revision": request.expected_source_revision,
        "expected_target_revision": request.expected_target_revision,
        "file_identity_before": file_identity_before,
        "free_bytes_before": free_bytes_before,
        "migration_plan": migration_plan,
        "minimum_free_bytes": request.minimum_free_bytes,
        "operation_recorded_at": request.operation_recorded_at,
        "receipt_path": str(request.receipt_path),
        "schema_version": "existing-isolated-clone-upgrade-intent/v1",
        "sqlite_before": sqlite_before,
    }
    draft = ExistingCloneUpgradeIntent.model_validate(fields | {"intent_sha256": "0" * 64})
    payload = draft.model_dump(mode="json", exclude={"intent_sha256"})
    return draft.model_copy(update={"intent_sha256": _digest_text(_canonical_json(payload))})


def _verify_upgrade_intent(
    intent: ExistingCloneUpgradeIntent,
    *,
    request: ExistingCloneUpgradeRequest,
    clone_snapshot: ImmutableArtifactSnapshot,
    clone_receipt: CompressedCloneReceipt,
    checkout: CheckoutIdentity,
    migration_plan: MigrationPlan,
) -> None:
    if (
        intent.database_path != str(request.database_path)
        or intent.receipt_path != str(request.receipt_path)
        or intent.compressed_clone_receipt_path != str(clone_snapshot.path)
        or intent.compressed_clone_receipt_file_sha256 != clone_snapshot.file_sha256
        or intent.compressed_clone_receipt_sha256 != clone_receipt.receipt_sha256
        or intent.checkout != checkout
        or intent.migration_plan != migration_plan
        or intent.expected_source_revision != request.expected_source_revision
        or intent.expected_target_revision != request.expected_target_revision
        or intent.minimum_free_bytes != request.minimum_free_bytes
        or intent.operation_recorded_at != request.operation_recorded_at
        or intent.database_before.sha256 != clone_receipt.destination_database_sha256
        or intent.database_before.byte_size != clone_receipt.logical_size_bytes
        or intent.database_before.alembic_revision != request.expected_source_revision
    ):
        raise CutoverPreflightError("clone upgrade intent differs from the exact request")


def _recovery_runtime_identity_is_valid(
    *,
    source_identity: str | None,
    recovered_identity: str | None,
    migration_plan: MigrationPlan,
) -> bool:
    if source_identity is not None:
        return recovered_identity == source_identity
    introduces_identity = any(
        item.revision == "0264_document_processing_operation_ledger"
        for item in migration_plan.ordered_migration_files
    )
    if not introduces_identity:
        return False
    return recovered_identity is not None


def _load_completed_existing_clone_upgrade(
    *,
    request: ExistingCloneUpgradeRequest,
    clone_snapshot: ImmutableArtifactSnapshot,
    clone_receipt: CompressedCloneReceipt,
    checkout: CheckoutIdentity,
    migration_plan: MigrationPlan,
) -> ExistingCloneUpgradeReceipt:
    receipt_snapshot, payload = read_stable_artifact(request.receipt_path)
    try:
        receipt = ExistingCloneUpgradeReceipt.model_validate_json(payload)
    except ValueError as exc:
        raise CutoverPreflightError("existing clone upgrade receipt is malformed") from exc
    if not verify_existing_clone_upgrade_receipt(receipt):
        raise CutoverPreflightError("existing clone upgrade receipt commitment is invalid")
    intent_snapshot, intent_payload = read_stable_artifact(Path(receipt.upgrade_intent_path))
    try:
        intent = ExistingCloneUpgradeIntent.model_validate_json(intent_payload)
    except ValueError as exc:
        raise CutoverPreflightError("existing clone upgrade intent is malformed") from exc
    if not verify_existing_clone_upgrade_intent(intent):
        raise CutoverPreflightError("existing clone upgrade intent commitment is invalid")
    _verify_upgrade_intent(
        intent,
        request=request,
        clone_snapshot=clone_snapshot,
        clone_receipt=clone_receipt,
        checkout=checkout,
        migration_plan=migration_plan,
    )
    if (
        receipt_snapshot.path != request.receipt_path
        or receipt.database_path != str(request.database_path)
        or receipt.checkout != checkout
        or receipt.migration_plan != migration_plan
        or receipt.compressed_clone_receipt_file_sha256 != clone_snapshot.file_sha256
        or receipt.compressed_clone_receipt_sha256 != clone_receipt.receipt_sha256
        or receipt.upgrade_intent_file_sha256 != intent_snapshot.file_sha256
        or receipt.upgrade_intent_sha256 != intent.intent_sha256
        or receipt.operation_recorded_at != request.operation_recorded_at
        or receipt.minimum_free_bytes != request.minimum_free_bytes
        or _database_identity(request.database_path) != receipt.database_after
        or candidate_file_identity(request.database_path) != receipt.file_identity_after
    ):
        raise CutoverPreflightError("existing clone upgrade receipt differs from current state")
    assert_artifact_unchanged(receipt_snapshot)
    assert_artifact_unchanged(intent_snapshot)
    return receipt


def _seal_manifest(**fields: object) -> CutoverPreflightManifest:
    request = fields.pop("request", None)
    if not isinstance(request, CutoverRequest):
        raise TypeError("request is required to seal a cutover manifest")
    fields["destination_path"] = str(request.destination_path)
    fields["live_database_path"] = str(request.resolved_live_database_path)
    unsigned = CutoverPreflightManifest.model_validate({**fields, "manifest_sha256": "0" * 64})
    payload = unsigned.model_dump(mode="json", exclude={"manifest_sha256"})
    return unsigned.model_copy(update={"manifest_sha256": _digest_text(_canonical_json(payload))})


def _validate_static_inputs(request: CutoverRequest) -> None:
    if not request.repo_root.is_dir():
        raise FileNotFoundError(f"repository root does not exist: {request.repo_root}")
    if not request.source_path.is_file():
        raise FileNotFoundError(f"source database does not exist: {request.source_path}")
    if not (request.repo_root / "alembic").is_dir():
        raise CutoverPreflightError(f"Alembic directory does not exist: {request.repo_root}")
    if request.destination_path == request.resolved_live_database_path:
        raise CutoverPreflightError("destination_path must not be the live database")


def _checkout_identity(repo_root: Path) -> CheckoutIdentity:
    commit_sha = _git(repo_root, "rev-parse", "--verify", "HEAD").strip().lower()
    status = _git(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    return CheckoutIdentity(commit_sha=commit_sha, clean=not status.strip())


def _require_apply_checkout(
    repo_root: Path,
    checkout: CheckoutIdentity,
    migration_plan: MigrationPlan,
) -> None:
    if not checkout.clean:
        raise CutoverPreflightError("apply requires a clean committed checkout")
    tracked_output = _git(
        repo_root,
        "ls-tree",
        "-r",
        "--name-only",
        "HEAD",
        "--",
        "alembic/versions",
    )
    tracked = {line.strip().replace("\\", "/") for line in tracked_output.splitlines()}
    uncommitted_paths = [
        migration.relative_path
        for migration in migration_plan.ordered_migration_files
        if migration.relative_path.replace("\\", "/") not in tracked
    ]
    if uncommitted_paths:
        raise CutoverPreflightError(
            "apply requires every migration file to be committed: " + ", ".join(uncommitted_paths)
        )


def _git(repo_root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (
            exc.stderr.strip()
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
            else str(exc)
        )
        raise CutoverPreflightError(f"unable to inspect committed checkout: {detail}") from exc
    return completed.stdout


def _migration_plan(repo_root: Path) -> MigrationPlan:
    config = _alembic_config(repo_root, repo_root / ".tmp" / "unused-cutover-plan.db")
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
        if len(heads) != 1:
            raise CutoverPreflightError(f"cutover requires exactly one Alembic head; found {heads}")
        expected_head = heads[0]
        revisions = list(script.walk_revisions(base="base", head="heads"))
        revisions.reverse()
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
    migrations: list[MigrationFileDigest] = []
    for ordinal, revision in enumerate(revisions):
        raw_path = getattr(revision, "path", None)
        if not isinstance(raw_path, str):
            raise CutoverPreflightError(f"migration {revision.revision} has no file path")
        path = Path(raw_path).resolve()
        try:
            relative_path = path.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise CutoverPreflightError(
                f"migration file is outside the committed checkout: {path}"
            ) from exc
        migrations.append(
            MigrationFileDigest(
                ordinal=ordinal,
                revision=str(revision.revision),
                down_revisions=_down_revisions(revision.down_revision),
                relative_path=relative_path,
                sha256=_sha256(path),
            )
        )
    return MigrationPlan(
        expected_alembic_head=expected_head,
        ordered_migration_files=tuple(migrations),
    )


def _down_revisions(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, tuple):
        values = cast("tuple[object, ...]", value)
        if all(isinstance(item, str) for item in values):
            return cast("tuple[str, ...]", values)
    raise CutoverPreflightError(f"unsupported Alembic down_revision: {value!r}")


def _alembic_config(repo_root: Path, database_path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(repo_root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def _database_identity(path: Path) -> DatabaseIdentity:
    stat = path.stat()
    conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        revision = _alembic_revision(conn)
    finally:
        conn.close()
    return DatabaseIdentity(
        path=str(path),
        byte_size=stat.st_size,
        sha256=_sha256(path),
        alembic_revision=revision,
    )


def _alembic_revision(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT version_num FROM alembic_version ORDER BY version_num").fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        raise CutoverPreflightError("database must contain exactly one non-empty Alembic revision")
    return str(rows[0][0])


def _database_runtime_identity(path: Path) -> str | None:
    conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='database_runtime_identity'"
        ).fetchone()
        if table is None:
            return None
        rows = conn.execute(
            "SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1"
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise CutoverPreflightError("database runtime identity is missing or ambiguous")
    value = str(rows[0][0])
    suffix = value.removeprefix("database-instance:")
    if (
        len(value) != 50
        or len(suffix) != 32
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        raise CutoverPreflightError("database runtime identity is invalid")
    return value


def _verification_stage(
    path: Path,
    *,
    repo_root: Path,
    sample_limit: int,
) -> VerificationStage:
    sqlite_verification = _sqlite_verification(path)
    conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        conn.execute("PRAGMA query_only = ON")
        audit = audit_connection(
            conn,
            AuditOptions(
                sample_limit=sample_limit,
                deep_sqlite_checks=True,
                verify_bytes=False,
                repo_root=repo_root,
            ),
        )
    finally:
        conn.close()
    return VerificationStage(sqlite=sqlite_verification, integrity_audit=audit)


def _sqlite_verification(path: Path) -> SQLiteVerification:
    conn = connect_sqlite(
        path,
        role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
        schema_preflight=False,
    )
    try:
        quick_rows = conn.execute("PRAGMA quick_check").fetchall()
        foreign_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return SQLiteVerification(
        quick_check=tuple(str(row[0]) for row in quick_rows),
        foreign_key_check=tuple(tuple(value for value in row) for row in foreign_rows),
        integrity_check=tuple(str(row[0]) for row in integrity_rows),
        clean=(
            tuple(str(row[0]).lower() for row in quick_rows) == ("ok",)
            and not foreign_rows
            and tuple(str(row[0]).lower() for row in integrity_rows) == ("ok",)
        ),
    )


def _require_clean_stage(label: str, stage: VerificationStage) -> None:
    if not stage.sqlite.clean:
        raise CutoverPreflightError(f"{label} failed SQLite integrity checks")
    if stage.integrity_audit.has_blockers:
        codes = sorted(
            finding.code
            for finding in stage.integrity_audit.findings
            if finding.severity.value == "blocker"
        )
        raise CutoverPreflightError(f"{label} failed evidence integrity audit: {', '.join(codes)}")


def _free_space_estimate(
    request: CutoverRequest,
    source_byte_size: int,
) -> FreeSpaceEstimate:
    probe = _existing_ancestor(request.destination_path.parent)
    required = max(
        source_byte_size * request.space_multiplier,
        request.minimum_space_reserve_bytes,
    )
    available = shutil.disk_usage(probe).free
    return FreeSpaceEstimate(
        probe_path=str(probe),
        source_byte_size=source_byte_size,
        multiplier=request.space_multiplier,
        minimum_reserve_bytes=request.minimum_space_reserve_bytes,
        required_free_bytes=required,
        available_free_bytes=available,
        sufficient=available >= required,
    )


def _existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise CutoverPreflightError(f"unable to find an existing destination ancestor: {path}")
        candidate = parent
    return candidate


def _available_free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(_existing_ancestor(path)).free)


def _require_unoccupied_destination(destination_path: Path) -> None:
    occupied = [
        path
        for path in (
            destination_path,
            destination_path.with_suffix(destination_path.suffix + ".manifest.json"),
            _cutover_manifest_path(destination_path),
        )
        if path.exists()
    ]
    if occupied:
        raise CutoverPreflightError(
            "isolated destination must not already exist: "
            + ", ".join(str(path) for path in occupied)
        )


def _upgrade_clone(
    *,
    repo_root: Path,
    destination_path: Path,
    expected_head: str,
) -> None:
    live_path = portfolio_db_path(repo_root).resolve()
    for path in (destination_path, live_path):
        require_no_reparse_points(path)
    if path_aliases_any(destination_path, {live_path}):
        raise CutoverPreflightError("refusing to point Alembic at the live database")
    command.upgrade(
        _alembic_config(repo_root, destination_path),
        expected_head,
    )


def _destination_artifact(
    destination_path: Path,
    snapshot: SnapshotResult,
    expected_head: str,
) -> DestinationArtifact:
    identity = _database_identity(destination_path)
    if identity.alembic_revision != expected_head:
        raise CutoverPreflightError(
            "isolated destination revision does not match expected Alembic head: "
            f"actual={identity.alembic_revision}, expected={expected_head}"
        )
    return DestinationArtifact(
        path=identity.path,
        byte_size=identity.byte_size,
        sha256=identity.sha256,
        alembic_revision=identity.alembic_revision,
        snapshot_manifest_path=str(snapshot.manifest_path),
    )


def _destination_write_set(destination_path: Path) -> str:
    digest = _digest_text(str(destination_path).casefold())[:24]
    return f"isolated-data-cutover-{digest}"


def _cutover_manifest_path(destination_path: Path) -> Path:
    return destination_path.with_suffix(destination_path.suffix + ".cutover-preflight.json")


def _write_manifest_atomically(
    path: Path,
    manifest: CutoverPreflightManifest,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_manifest_json(manifest))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _emit(logger: CutoverLogger | None, event: str, **fields: object) -> None:
    if logger is not None:
        logger(event, fields)
