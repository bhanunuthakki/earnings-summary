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
from provenance.integrity_audit import AuditOptions, IntegrityAuditSummary, audit_connection
from runtime.job_runtime import JobLock
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


def _verification_stage(
    path: Path,
    *,
    repo_root: Path,
    sample_limit: int,
) -> VerificationStage:
    conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        conn.execute("PRAGMA query_only = ON")
        quick_rows = conn.execute("PRAGMA quick_check").fetchall()
        foreign_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        sqlite_verification = SQLiteVerification(
            quick_check=tuple(str(row[0]) for row in quick_rows),
            foreign_key_check=tuple(tuple(value for value in row) for row in foreign_rows),
            integrity_check=tuple(str(row[0]) for row in integrity_rows),
            clean=(
                tuple(str(row[0]).lower() for row in quick_rows) == ("ok",)
                and not foreign_rows
                and tuple(str(row[0]).lower() for row in integrity_rows) == ("ok",)
            ),
        )
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
    live_path = (repo_root / "data" / "portfolio.db").resolve()
    if destination_path.resolve() == live_path:
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
