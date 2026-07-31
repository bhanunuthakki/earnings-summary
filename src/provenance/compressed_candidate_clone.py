"""Receipted NTFS-compressed clones for large governed SQLite rehearsals."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.immutable_artifact import require_no_reparse_points
from provenance.latest_state_activation import (
    CandidateFileIdentity,
    GovernedCandidateAudit,
    GovernedCandidateCoverageAudit,
    LatestStateActivationError,
    require_checkpointed_sidecars,
    verify_candidate_audit_receipt,
    verify_candidate_coverage_receipt,
)

_FILE_ATTRIBUTE_COMPRESSED = 0x800
_COPY_BLOCK_BYTES = 8 * 1024 * 1024
_HEADROOM_CHECK_BYTES = 128 * 1024 * 1024
MINIMUM_SAFE_FREE_BYTES = 5 * 1024 * 1024 * 1024


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompressedCloneRequest(_FrozenModel):
    source_database: Path
    candidate_audit_receipt: Path
    candidate_coverage_receipt: Path
    destination_database: Path
    operation_recorded_at: datetime
    minimum_free_bytes: int = Field(ge=MINIMUM_SAFE_FREE_BYTES)

    @field_validator("operation_recorded_at")
    @classmethod
    def _aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operation_recorded_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _distinct_paths(self) -> CompressedCloneRequest:
        if self.source_database.resolve() == self.destination_database.resolve():
            raise ValueError("source and destination database paths must differ")
        return self


class CompressedCloneReceipt(_FrozenModel):
    schema_version: str = "latest-governed-compressed-clone/v1"
    source_database: str
    source_database_sha256: str
    source_identity_before: CandidateFileIdentity
    source_identity_after: CandidateFileIdentity
    candidate_audit_receipt: str
    candidate_audit_report_sha256: str
    candidate_audit_file_sha256: str
    candidate_audit_identity_before: CandidateFileIdentity
    candidate_audit_identity_after: CandidateFileIdentity
    candidate_coverage_receipt: str
    candidate_coverage_report_sha256: str
    candidate_coverage_file_sha256: str
    candidate_coverage_identity_before: CandidateFileIdentity
    candidate_coverage_identity_after: CandidateFileIdentity
    destination_database: str
    destination_database_sha256: str
    logical_size_bytes: int = Field(ge=0)
    compressed_size_bytes: int = Field(ge=0)
    free_bytes_before: int = Field(ge=0)
    free_bytes_after: int = Field(ge=0)
    minimum_free_bytes: int = Field(ge=MINIMUM_SAFE_FREE_BYTES)
    operation_recorded_at: datetime
    receipt_sha256: str

    @field_validator("operation_recorded_at")
    @classmethod
    def _aware_receipt_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operation_recorded_at must include a timezone")
        return value

    @field_validator("receipt_sha256")
    @classmethod
    def _receipt_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("receipt SHA-256 is malformed")
        return value


def verify_compressed_clone_receipt(receipt: CompressedCloneReceipt) -> bool:
    payload = receipt.model_dump(mode="json")
    stored = str(payload.pop("receipt_sha256"))
    return stored == _digest(payload)


def prepare_compressed_clone(request: CompressedCloneRequest) -> CompressedCloneReceipt:
    """Copy one audited quiesced database with bounded local-disk headroom."""

    if os.name != "nt":
        raise LatestStateActivationError("compressed candidate clones require Windows NTFS")
    source = request.source_database.resolve()
    audit_path = request.candidate_audit_receipt.resolve()
    coverage_path = request.candidate_coverage_receipt.resolve()
    destination = request.destination_database.resolve()
    require_no_reparse_points(source)
    require_no_reparse_points(audit_path)
    require_no_reparse_points(coverage_path)
    require_no_reparse_points(destination)
    if not source.is_file() or not audit_path.is_file() or not coverage_path.is_file():
        raise LatestStateActivationError("clone source or admission receipt is missing")
    if destination.exists() or any(
        Path(f"{destination}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise LatestStateActivationError("clone destination or sidecar already exists")
    audit_identity_before = _file_identity(audit_path)
    coverage_identity_before = _file_identity(coverage_path)
    try:
        audit_bytes = audit_path.read_bytes()
        audit = GovernedCandidateAudit.model_validate_json(audit_bytes)
    except (OSError, ValueError) as exc:
        raise LatestStateActivationError("candidate audit receipt is malformed") from exc
    audit_file_sha256 = hashlib.sha256(audit_bytes).hexdigest()
    if _file_identity(audit_path) != audit_identity_before:
        raise LatestStateActivationError("candidate audit receipt changed during admission")
    if not verify_candidate_audit_receipt(audit):
        raise LatestStateActivationError("candidate audit receipt commitment is invalid")
    if Path(audit.database_path).resolve() != source:
        raise LatestStateActivationError("candidate audit receipt names a different source")
    try:
        coverage_bytes = coverage_path.read_bytes()
        coverage = GovernedCandidateCoverageAudit.model_validate_json(coverage_bytes)
    except (OSError, ValueError) as exc:
        raise LatestStateActivationError("candidate coverage receipt is malformed") from exc
    coverage_file_sha256 = hashlib.sha256(coverage_bytes).hexdigest()
    if _file_identity(coverage_path) != coverage_identity_before:
        raise LatestStateActivationError("candidate coverage receipt changed during admission")
    if not verify_candidate_coverage_receipt(coverage):
        raise LatestStateActivationError("candidate coverage receipt commitment is invalid")
    if Path(coverage.database_path).resolve() != source:
        raise LatestStateActivationError("candidate coverage receipt names a different source")
    if (
        Path(coverage.candidate_audit_receipt).resolve() != audit_path
        or coverage.candidate_audit_report_sha256 != audit.report_sha256
        or coverage.candidate_audit_file_sha256 != audit_file_sha256
        or coverage.database_sha256 != audit.database_sha256
        or coverage.database_identity_after != audit.database_identity_after
    ):
        raise LatestStateActivationError("candidate coverage is not bound to the audit receipt")
    identity_before = _file_identity(source)
    if identity_before != audit.database_identity_after:
        raise LatestStateActivationError("source identity differs from candidate audit receipt")
    require_checkpointed_sidecars(source)

    if destination.parent.exists():
        raise LatestStateActivationError("clone destination directory must be new")
    if not destination.parent.parent.is_dir():
        raise LatestStateActivationError("clone destination directory parent is missing")
    destination.parent.mkdir(parents=False, exist_ok=False)
    created_parent = True
    try:
        _enable_directory_compression(destination.parent)
        if not _directory_is_compressed(destination.parent):
            raise LatestStateActivationError("clone directory did not retain NTFS compression")
        free_before = _free_bytes(destination.parent)
        if free_before <= request.minimum_free_bytes:
            raise LatestStateActivationError("insufficient disk headroom before clone")
    except (LatestStateActivationError, OSError):
        destination.parent.rmdir()
        raise

    digest = hashlib.sha256()
    copied_since_check = 0
    staged: Path | None = None
    published = False
    linked = False
    try:
        require_checkpointed_sidecars(source)
        with tempfile.NamedTemporaryFile(
            mode="xb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as target:
            staged = Path(target.name)
            with source.open("rb") as origin:
                while block := origin.read(_COPY_BLOCK_BYTES):
                    target.write(block)
                    digest.update(block)
                    copied_since_check += len(block)
                    if copied_since_check >= _HEADROOM_CHECK_BYTES:
                        _require_headroom(destination.parent, request.minimum_free_bytes)
                        copied_since_check = 0
            target.flush()
            os.fsync(target.fileno())
        _require_headroom(destination.parent, request.minimum_free_bytes)
        if digest.hexdigest() != audit.database_sha256:
            raise LatestStateActivationError("compressed clone SHA-256 differs from source seal")
        identity_after = _file_identity(source)
        if identity_after != identity_before:
            raise LatestStateActivationError("source identity changed during compressed clone")
        if not _file_is_compressed(staged):
            raise LatestStateActivationError("clone staging file is not NTFS-compressed")
        require_checkpointed_sidecars(source)
        identity_at_publication = _file_identity(source)
        if identity_at_publication != identity_before:
            raise LatestStateActivationError(
                "source identity changed before compressed clone publication"
            )
        audit_identity_after = _file_identity(audit_path)
        coverage_identity_after = _file_identity(coverage_path)
        if (
            audit_identity_after != audit_identity_before
            or coverage_identity_after != coverage_identity_before
            or _sha256(audit_path) != audit_file_sha256
            or _sha256(coverage_path) != coverage_file_sha256
        ):
            raise LatestStateActivationError("candidate admission receipt changed during clone")
        compressed_size = _compressed_size(staged)
        free_after = _free_bytes(destination.parent)
        if _sha256(staged) != digest.hexdigest():
            raise LatestStateActivationError("clone staging bytes changed before publication")
        receipt_core = {
            "schema_version": "latest-governed-compressed-clone/v1",
            "source_database": str(source),
            "source_database_sha256": audit.database_sha256,
            "source_identity_before": identity_before,
            "source_identity_after": identity_at_publication,
            "candidate_audit_receipt": str(audit_path),
            "candidate_audit_report_sha256": audit.report_sha256,
            "candidate_audit_file_sha256": audit_file_sha256,
            "candidate_audit_identity_before": audit_identity_before,
            "candidate_audit_identity_after": audit_identity_after,
            "candidate_coverage_receipt": str(coverage_path),
            "candidate_coverage_report_sha256": coverage.report_sha256,
            "candidate_coverage_file_sha256": coverage_file_sha256,
            "candidate_coverage_identity_before": coverage_identity_before,
            "candidate_coverage_identity_after": coverage_identity_after,
            "destination_database": str(destination),
            "destination_database_sha256": digest.hexdigest(),
            "logical_size_bytes": staged.stat().st_size,
            "compressed_size_bytes": compressed_size,
            "free_bytes_before": free_before,
            "free_bytes_after": free_after,
            "minimum_free_bytes": request.minimum_free_bytes,
            "operation_recorded_at": request.operation_recorded_at,
            "receipt_sha256": "0" * 64,
        }
        draft = CompressedCloneReceipt.model_validate(receipt_core)
        receipt_payload = draft.model_dump(mode="json")
        receipt_payload.pop("receipt_sha256")
        receipt = draft.model_copy(update={"receipt_sha256": _digest(receipt_payload)})
        try:
            os.link(staged, destination)
        except FileExistsError:
            raise LatestStateActivationError(
                "clone destination appeared during publication"
            ) from None
        linked = True
        if not os.path.samefile(staged, destination):
            raise LatestStateActivationError("published clone identity differs from staging")
        published = True
        return receipt
    except OSError as exc:
        raise LatestStateActivationError("compressed clone filesystem operation failed") from exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        if linked and not published:
            destination.unlink(missing_ok=True)
        if created_parent and not published:
            with suppress(OSError):
                destination.parent.rmdir()


def _enable_directory_compression(path: Path) -> None:
    compact = Path(r"C:\Windows\System32\compact.exe")
    if not compact.is_file():
        raise LatestStateActivationError("System32 compact.exe is unavailable")
    try:
        completed = subprocess.run(
            [str(compact), "/C", "/I", "/Q", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise LatestStateActivationError("NTFS compression command timed out") from exc
    if completed.returncode != 0:
        raise LatestStateActivationError("failed to enable NTFS directory compression")


def _attributes(path: Path) -> int:
    attributes = int(ctypes.windll.kernel32.GetFileAttributesW(str(path)))
    if attributes == 0xFFFFFFFF:
        raise LatestStateActivationError("could not read Windows file attributes")
    return attributes


def _directory_is_compressed(path: Path) -> bool:
    return bool(_attributes(path) & _FILE_ATTRIBUTE_COMPRESSED)


def _file_is_compressed(path: Path) -> bool:
    return bool(_attributes(path) & _FILE_ATTRIBUTE_COMPRESSED)


def _compressed_size(path: Path) -> int:
    high = ctypes.c_ulong(0)
    low = int(ctypes.windll.kernel32.GetCompressedFileSizeW(str(path), ctypes.byref(high)))
    if low == 0xFFFFFFFF and ctypes.windll.kernel32.GetLastError() != 0:
        raise LatestStateActivationError("could not read compressed file size")
    return (int(high.value) << 32) | low


def _free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _require_headroom(path: Path, minimum_free_bytes: int) -> None:
    if _free_bytes(path) <= minimum_free_bytes:
        raise LatestStateActivationError("compressed clone crossed the disk headroom floor")


def _file_identity(path: Path) -> CandidateFileIdentity:
    stat = path.stat()
    return CandidateFileIdentity(
        device=int(stat.st_dev),
        inode=int(stat.st_ino),
        size_bytes=stat.st_size,
        modified_time_ns=stat.st_mtime_ns,
        changed_time_ns=stat.st_ctime_ns,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_COPY_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
