"""Discover verified KPI rollback backups from immutable operation evidence.

Discovery reads bounded metadata only. It does not open a SQLite connection,
write a catalog, or infer operation completion from snapshot publication.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable
from itertools import islice
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from execution.backup_restore_readiness_receipt import (
    BackupRestoreReadinessReceipt,
    evidence_id_is_valid,
)
from src.operations.artifact_retention import (
    Artifact,
    ArtifactCatalog,
    RetirementEvidenceSet,
    RetirementProof,
    evidence_set_digest,
)
from src.operations.kpi_repair_receipts import (
    KpiDispositionAttemptReceipt,
    KpiRepairAttemptReceipt,
)
from src.sqlite_snapshot import SnapshotManifest

MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_METADATA_FILES = 4096
_Attempt = KpiRepairAttemptReceipt | KpiDispositionAttemptReceipt


class OperationalBackupReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path
    path: Path
    status: Literal["missing", "unclassified", "failed", "ready"]
    reason: str
    bytes: int = Field(default=0, ge=0)
    source: str | None = None
    purpose: str | None = None
    family: str | None = None
    pins: list[str] = Field(default_factory=list)


class OperationalBackupDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog: ArtifactCatalog
    reports: list[OperationalBackupReport]


class _HeldError(ValueError):
    """Fixed diagnostics contain no receipt contents or private exception text."""


def _absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise _HeldError("nonabsolute_or_traversing_path")
    return Path(os.path.abspath(path))


def _identity(path: str | Path) -> str:
    return os.path.normcase(str(_absolute(Path(path))))


def _regular(path: Path) -> os.stat_result:
    for part in (path, *path.parents):
        metadata = part.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise _HeldError("linked_evidence_path")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise _HeldError("nonunique_evidence_file")
    return metadata


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise _HeldError("duplicate_json_key")
        result[key] = value
    return result


def _metadata(path: Path) -> tuple[dict[str, object], RetirementProof]:
    before = _regular(path)
    if before.st_size > MAX_METADATA_BYTES:
        raise _HeldError("metadata_size_limit")
    with path.open("rb") as handle:
        raw = handle.read(MAX_METADATA_BYTES + 1)
    after = _regular(path)
    if len(raw) > MAX_METADATA_BYTES or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise _HeldError("metadata_changed")
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    value: object = json.loads(raw.decode(encoding), object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise _HeldError("metadata_not_object")
    return cast("dict[str, object]", value), RetirementProof(
        path=path, sha256=hashlib.sha256(raw).hexdigest()
    )


def _bounded(paths: Iterable[Path]) -> list[Path]:
    result = list(islice(paths, MAX_METADATA_FILES + 1))
    if len(result) > MAX_METADATA_FILES:
        raise _HeldError("metadata_count_limit")
    return sorted(result)


def _compatible(attempt: _Attempt) -> tuple[str, str, str, str]:
    return (
        attempt.logical_idempotency_key_sha256,
        attempt.manifest_sha256,
        attempt.review_bundle_sha256,
        attempt.executor_code_sha256,
    )


def _closed(attempt: _Attempt) -> bool:
    return (
        attempt.mode == "apply"
        and attempt.state in {"applied", "replayed"}
        and not attempt.blocker_codes
        and attempt.completed_at >= attempt.started_at
    )


def _migration_backup(path: Path) -> bool:
    # This excludes an unrelated known producer from KPI eligibility. It never
    # establishes migration completion or gives permission to delete its files.
    return path.name.startswith(("portfolio_pre_0032_", "portfolio_upgrade_0032_"))


def discover_operational_backups(repo_root: Path) -> OperationalBackupDiscovery:
    """Join snapshots to exact verified readiness and completed KPI attempts.

    An unknown backup or invalid operation record pins the latest last-good
    backup in each family. Every invocation rebuilds the joins, so later compatible completion
    can resolve a failed attempt without rewriting its historical receipt.
    """
    root = _absolute(repo_root)
    backup_root = root / "data/backups"
    reports: list[OperationalBackupReport] = []
    artifacts: list[Artifact] = []
    result = OperationalBackupDiscovery(
        catalog=ArtifactCatalog(schema_version=1, artifacts=[]), reports=reports
    )
    try:
        backups = _bounded(backup_root.glob("*.db"))
        manifests = _bounded(backup_root.glob("*.db.manifest.json"))
    except (OSError, _HeldError):
        result.reports.append(
            OperationalBackupReport(
                root=backup_root,
                path=backup_root,
                status="unclassified",
                reason="discovery_boundary_or_count_limit",
            )
        )
        return result
    if not backups and not manifests:
        return result
    configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    try:
        source = _identity(configured)
    except _HeldError:
        result.reports.append(
            OperationalBackupReport(
                root=backup_root,
                path=backup_root,
                status="unclassified",
                reason="configured_source_missing_or_invalid",
            )
        )
        return result

    readiness: dict[str, tuple[BackupRestoreReadinessReceipt, RetirementProof]] = {}
    attempts: dict[str, list[tuple[str, _Attempt, RetirementProof]]] = {}
    global_hold = False
    operation_roots = tuple(
        root / "data/operations" / directory for directory in ("kpi_repairs", "kpi_dispositions")
    )
    evidence_directories = tuple(
        path for directory in operation_roots for path in (directory, directory / "attempts")
    )
    try:
        evidence_sets = tuple(
            RetirementEvidenceSet(path=path, sha256=evidence_set_digest(path))
            for path in evidence_directories
        )
    except (OSError, ValueError):
        result.reports.append(
            OperationalBackupReport(
                root=backup_root,
                path=backup_root,
                status="unclassified",
                reason="operation_evidence_set_unavailable",
            )
        )
        return result
    try:
        metadata_paths: list[tuple[str, Path, bool]] = []
        for directory, purpose in (
            ("kpi_repairs", "kpi-source-repair"),
            ("kpi_dispositions", "kpi-semantic-disposition"),
        ):
            operation_root = root / "data/operations" / directory
            metadata_paths.extend(
                (purpose, path, False) for path in _bounded(operation_root.glob("*.json"))
            )
            metadata_paths.extend(
                (purpose, path, True)
                for path in _bounded((operation_root / "attempts").glob("*.json"))
            )
        if len(metadata_paths) > MAX_METADATA_FILES:
            raise _HeldError("metadata_count_limit")
    except (OSError, _HeldError):
        result.reports.append(
            OperationalBackupReport(
                root=backup_root,
                path=backup_root,
                status="unclassified",
                reason="discovery_boundary_or_count_limit",
            )
        )
        return result

    for purpose, path, immutable in metadata_paths:
        try:
            value, proof = _metadata(path)
            schema = value.get("schema_version")
            if schema == "backup-restore-readiness/v1":
                receipt = BackupRestoreReadinessReceipt.model_validate(value)
                if not evidence_id_is_valid(receipt) or value.get("verified") is not True:
                    raise _HeldError("invalid_readiness_receipt")
                if not receipt.verified or receipt.blocking_reasons:
                    raise _HeldError("failed_readiness_receipt")
                if (
                    _identity(receipt.source_db_resolved_path) != source
                    or _identity(receipt.source_db_requested_path) != source
                ):
                    reports.append(
                        OperationalBackupReport(
                            root=path.parent,
                            path=path,
                            status="unclassified",
                            reason="different_source",
                        )
                    )
                    continue
                if receipt.observed_at.tzinfo is None:
                    raise _HeldError("naive_readiness_time")
                previous = readiness.get(receipt.evidence_id)
                if previous is not None and previous[0] != receipt:
                    raise _HeldError("ambiguous_readiness_identity")
                readiness[receipt.evidence_id] = receipt, proof
            elif schema in {
                "kpi_repair_attempt.v2",
                "kpi_repair_attempt.v3",
                "kpi_disposition_attempt.v1",
            }:
                attempt: _Attempt = (
                    KpiRepairAttemptReceipt.model_validate(value)
                    if purpose == "kpi-source-repair"
                    and schema in {"kpi_repair_attempt.v2", "kpi_repair_attempt.v3"}
                    else KpiDispositionAttemptReceipt.model_validate(value)
                )
                if immutable:
                    if (
                        path.name != f"{attempt.attempt_id}.json"
                        or attempt.completed_at < attempt.started_at
                    ):
                        raise _HeldError("invalid_immutable_attempt")
                    attempts.setdefault(attempt.backup_restore_evidence_id, []).append(
                        (purpose, attempt, proof)
                    )
                elif path.name == "latest.json":
                    immutable_path = path.parent / "attempts" / f"{attempt.attempt_id}.json"
                    immutable_value, _ = _metadata(immutable_path)
                    if immutable_value != value:
                        raise _HeldError("latest_without_exact_immutable_attempt")
        except (OSError, ValueError, ValidationError):
            global_hold = True
            reports.append(
                OperationalBackupReport(
                    root=path.parent,
                    path=path,
                    status="unclassified",
                    reason="invalid_or_unbound_operation_metadata",
                )
            )

    manifested: set[Path] = set()
    for manifest_path in manifests:
        path = manifest_path.with_name(manifest_path.name.removesuffix(".manifest.json"))
        manifested.add(path)
        if _migration_backup(path):
            reports.append(
                OperationalBackupReport(
                    root=backup_root,
                    path=path,
                    status="unclassified",
                    reason="migration_closure_not_supported",
                )
            )
            continue
        size = 0
        try:
            value, manifest_proof = _metadata(manifest_path)
            manifest = SnapshotManifest.model_validate(value)
            if (
                manifest.schema_version != "sqlite-reader-snapshot/v1"
                or manifest.code_config_version != "sqlite-reader-snapshot/v1"
            ):
                raise _HeldError("unsupported_snapshot_contract")
            if (
                _identity(manifest.snapshot.path) != _identity(path)
                or _identity(manifest.source.path) != source
            ):
                raise _HeldError("snapshot_path_or_source_mismatch")
            if (
                manifest.verification.integrity_check != ("ok",)
                or manifest.verification.foreign_key_check
                or manifest.created_at.tzinfo is None
            ):
                raise _HeldError("snapshot_not_verified")
            metadata = _regular(path)
            size = metadata.st_size
            if size != manifest.snapshot.byte_size:
                raise _HeldError("snapshot_size_changed")
            matches = [
                item
                for item in readiness.values()
                if _identity(item[0].snapshot_resolved_path) == _identity(path)
            ]
            if len(matches) != 1:
                raise _HeldError("missing_or_ambiguous_readiness")
            receipt, readiness_proof = matches[0]
            if (
                _identity(receipt.snapshot_requested_path) != _identity(path)
                or _identity(receipt.snapshot_manifest_resolved_path) != _identity(manifest_path)
                or receipt.snapshot_sha256 != manifest.snapshot.sha256
                or receipt.snapshot_byte_size != size
                or receipt.integrity_check != ("ok",)
                or receipt.foreign_key_violation_count != 0
                or receipt.restored_db_revision != manifest.source.alembic_revision
                or receipt.source_db_revision != manifest.source.alembic_revision
                or receipt.observed_at < manifest.created_at
            ):
                raise _HeldError("readiness_snapshot_binding_mismatch")
            bound = attempts.get(receipt.evidence_id, [])
            successful = [
                item
                for item in bound
                if _closed(item[1]) and item[1].started_at >= receipt.observed_at
            ]
            if not successful:
                failed = any(item[1].state in {"failed", "blocked"} for item in bound)
                reports.append(
                    OperationalBackupReport(
                        root=backup_root,
                        path=path,
                        status="failed" if failed else "unclassified",
                        reason="operation_failed" if failed else "operation_not_closed",
                        bytes=size,
                    )
                )
                global_hold = True
                continue
            purpose, closed, closure_proof = max(successful, key=lambda item: item[1].completed_at)
            unresolved = [
                item
                for item in bound
                if item[1].state in {"failed", "blocked"}
                and (
                    item[0] != purpose
                    or _compatible(item[1]) != _compatible(closed)
                    or item[1].completed_at > closed.completed_at
                )
            ]
            if unresolved or len({item[0] for item in bound}) != 1:
                reports.append(
                    OperationalBackupReport(
                        root=backup_root,
                        path=path,
                        status="failed",
                        reason="unresolved_operation_attempt",
                        bytes=size,
                    )
                )
                global_hold = True
                continue
            family = f"{purpose}:{hashlib.sha256(source.encode()).hexdigest()}"
            artifacts.append(
                Artifact(
                    path=path,
                    allowed_root=backup_root,
                    family=family,
                    created_at=manifest.created_at,
                    sha256=manifest.snapshot.sha256,
                    size=size,
                    kind="backup",
                    status="completed",
                    verified=True,
                    retirement_proofs=tuple(
                        dict.fromkeys(
                            (
                                manifest_proof,
                                readiness_proof,
                                closure_proof,
                                *(item[2] for item in bound),
                            )
                        )
                    ),
                    retirement_evidence_sets=evidence_sets,
                )
            )
            reports.append(
                OperationalBackupReport(
                    root=backup_root,
                    path=path,
                    status="ready",
                    reason="verified_snapshot_and_closed_operation",
                    bytes=size,
                    source=source,
                    purpose=purpose,
                    family=family,
                )
            )
        except FileNotFoundError:
            reports.append(
                OperationalBackupReport(
                    root=backup_root, path=path, status="missing", reason="snapshot_missing"
                )
            )
        except (OSError, ValueError, ValidationError):
            global_hold = True
            reports.append(
                OperationalBackupReport(
                    root=backup_root,
                    path=path,
                    status="unclassified",
                    reason="invalid_or_unbound_backup_evidence",
                    bytes=size,
                )
            )

    for path in backups:
        if path not in manifested:
            if not _migration_backup(path):
                global_hold = True
            reports.append(
                OperationalBackupReport(
                    root=backup_root,
                    path=path,
                    status="unclassified",
                    reason="migration_closure_not_supported"
                    if _migration_backup(path)
                    else "snapshot_manifest_missing",
                )
            )
    try:
        if any(evidence_set_digest(item.path) != item.sha256 for item in evidence_sets):
            raise _HeldError("operation_evidence_set_changed")
    except (OSError, ValueError):
        for report in reports:
            if report.status == "ready":
                report.status = "unclassified"
                report.reason = "operation_evidence_set_changed"
                report.pins = ["operation_evidence_set_changed"]
        reports.append(
            OperationalBackupReport(
                root=backup_root,
                path=backup_root,
                status="unclassified",
                reason="operation_evidence_set_unavailable_or_changed",
            )
        )
        result.reports = reports
        return result
    if global_hold:
        latest: dict[str, Artifact] = {}
        for item in artifacts:
            prior = latest.get(item.family)
            if prior is None or (item.created_at, str(item.path)) > (
                prior.created_at,
                str(prior.path),
            ):
                latest[item.family] = item
        held = {item.path for item in latest.values()}
        artifacts = [
            item.model_copy(update={"pins": ["unclassified_or_unresolved_operational_backup"]})
            if item.path in held
            else item
            for item in artifacts
        ]
        for report in reports:
            if report.status == "ready" and report.path in held:
                report.pins = ["unclassified_or_unresolved_operational_backup"]
                report.reason = "last_good_held_by_unclassified_or_unresolved_backup"
    result.catalog = ArtifactCatalog(schema_version=1, artifacts=artifacts)
    result.reports = reports
    return result
