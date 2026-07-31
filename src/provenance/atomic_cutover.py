"""Hash-bound, recoverable activation of one verified SQLite cutover candidate."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.job_runtime import JobLock, portfolio_db_path
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class AtomicCutoverError(RuntimeError):
    """The requested activation cannot proceed without weakening safety."""


class ActivationRolledBackError(AtomicCutoverError):
    """Activation failed after the first rename and the old live DB was restored."""

    def __init__(self, receipt: ActivationReceipt) -> None:
        super().__init__(receipt.failure or "activation failed and was rolled back")
        self.receipt: ActivationReceipt = receipt


class ActivationMode(StrEnum):
    DRY_RUN = "dry-run"
    APPLY = "apply"


class TaskObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1)
    state: Literal["Disabled"]
    enabled: Literal[False]


class ServiceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    state: Literal["Stopped"]


class ListenerObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    listening: Literal[False]
    pid: None

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


class QuiescenceReceipt(BaseModel):
    """Reviewed attestation that every named external writer has stopped."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
    captured_at: datetime
    valid_until: datetime
    live_database: str = Field(min_length=1)
    live_database_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_task_paths: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[TaskObservation, ...] = Field(min_length=1)
    expected_service_names: tuple[str, ...] = Field(min_length=1)
    services: tuple[ServiceObservation, ...] = Field(min_length=1)
    expected_listener_endpoints: tuple[str, ...] = Field(min_length=1)
    listeners: tuple[ListenerObservation, ...] = Field(min_length=1)
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _time_shape(self) -> Self:
        if self.captured_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("quiescence timestamps must include an explicit timezone")
        if self.valid_until <= self.captured_at:
            raise ValueError("quiescence valid_until must be after captured_at")
        return self


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repo_root: Path
    live_database: Path
    candidate_database: Path
    rollback_database: Path
    failed_candidate_database: Path
    receipt_path: Path
    quiescence_receipt_path: Path
    expected_quiescence_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_live_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_alembic_head: str = Field(min_length=1)
    mode: ActivationMode = ActivationMode.DRY_RUN


class DatabaseVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    database: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    alembic_revision: str
    quick_check: tuple[str, ...]
    integrity_check: tuple[str, ...]
    foreign_key_violations: int = Field(ge=0)


class ActivationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
    mode: ActivationMode
    status: Literal["ready", "activated", "rolled_back", "rollback_failed"]
    activation_mechanism: Literal["windows_replace_file", "portable_rename_pair"]
    repo_root: str
    live_database: str
    candidate_database: str
    rollback_database: str
    failed_candidate_database: str | None
    receipt_path: str
    expected_alembic_head: str
    quiescence_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    live_sha256_before: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    active_sha256_after: str | None
    rollback_sha256: str | None
    candidate_precheck: DatabaseVerification
    active_postcheck: DatabaseVerification | None
    rollback_restored: bool
    failure: str | None
    started_at: datetime
    completed_at: datetime
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)


class _Preflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repo_root: Path
    live_database: Path
    candidate_database: Path
    rollback_database: Path
    failed_candidate_database: Path
    receipt_path: Path
    quiescence_receipt_path: Path
    quiescence_receipt: QuiescenceReceipt
    candidate_verification: DatabaseVerification


def quiescence_payload_sha256(receipt: QuiescenceReceipt) -> str:
    """Seal the semantic receipt payload independently of JSON whitespace."""
    return _canonical_sha(receipt.model_dump(mode="json", exclude={"receipt_sha256"}))


def canonical_quiescence_json(receipt: QuiescenceReceipt) -> str:
    return _canonical_json(receipt.model_dump(mode="json")) + "\n"


def canonical_activation_json(receipt: ActivationReceipt) -> str:
    return _canonical_json(receipt.model_dump(mode="json")) + "\n"


def activation_payload_sha256(receipt: ActivationReceipt) -> str:
    """Verify the receipt seal over its canonical semantic payload."""
    return _canonical_sha(receipt.model_dump(mode="json", exclude={"receipt_sha256"}))


def activate_data_cutover(request: ActivationRequest) -> ActivationReceipt:
    """Validate and optionally activate the exact committed candidate."""
    started_at = _utc_now()
    preflight = _preflight(request)
    if request.mode is ActivationMode.DRY_RUN:
        return _seal_receipt(
            mode=request.mode,
            status="ready",
            preflight=preflight,
            expected_alembic_head=request.expected_alembic_head,
            live_sha256_before=request.expected_live_sha256,
            candidate_sha256=request.expected_candidate_sha256,
            started_at=started_at,
        )

    lock_database = portfolio_db_path(preflight.repo_root)
    if lock_database != preflight.live_database:
        raise AtomicCutoverError(
            "portfolio-db JobLock resolves to a different database: "
            f"{lock_database} != {preflight.live_database}"
        )

    with JobLock(preflight.repo_root, "activate-data-cutover", ["portfolio-db"]):
        # Re-hash under the lock without repeating the already hash-bound full
        # integrity scan and extending the production pause unnecessarily.
        _revalidate_locked(request, preflight)
        return _activate_locked(request, preflight, started_at=started_at)


def _activate_locked(
    request: ActivationRequest,
    preflight: _Preflight,
    *,
    started_at: datetime,
) -> ActivationReceipt:
    try:
        _replace_live_with_candidate(
            live=preflight.live_database,
            candidate=preflight.candidate_database,
            rollback=preflight.rollback_database,
        )
        active = _verify_database(
            preflight.live_database,
            expected_head=request.expected_alembic_head,
        )
        _require_no_sidecars(preflight.live_database, preflight.rollback_database)
        if active.sha256 != request.expected_candidate_sha256:
            raise AtomicCutoverError("installed candidate SHA-256 differs from commitment")
        rollback_sha = _sha256(preflight.rollback_database)
        if rollback_sha != request.expected_live_sha256:
            raise AtomicCutoverError(
                "rollback database SHA-256 differs from original live commitment"
            )
        receipt = _seal_receipt(
            mode=request.mode,
            status="activated",
            preflight=preflight,
            expected_alembic_head=request.expected_alembic_head,
            live_sha256_before=request.expected_live_sha256,
            candidate_sha256=request.expected_candidate_sha256,
            active_sha256_after=active.sha256,
            rollback_sha256=rollback_sha,
            candidate_precheck=preflight.candidate_verification,
            active_postcheck=active,
            started_at=started_at,
        )
        _write_receipt(preflight.receipt_path, receipt)
        return receipt
    except Exception as exc:
        if not preflight.rollback_database.exists():
            raise
        raise _restore_after_failure(request, preflight, exc, started_at=started_at) from exc


def _restore_after_failure(
    request: ActivationRequest,
    preflight: _Preflight,
    activation_error: Exception,
    *,
    started_at: datetime,
) -> ActivationRolledBackError | AtomicCutoverError:
    failed_evidence: Path | None = None
    rollback_error: Exception | None = None
    try:
        if not preflight.rollback_database.exists():
            raise AtomicCutoverError("rollback database disappeared after live rename")
        if _sha256(preflight.rollback_database) != request.expected_live_sha256:
            raise AtomicCutoverError("rollback database no longer matches original live SHA-256")
        if preflight.live_database.exists():
            if _sha256(preflight.live_database) != request.expected_candidate_sha256:
                raise AtomicCutoverError(
                    "unexpected database occupies live path; refusing destructive rollback"
                )
            preflight.live_database.rename(preflight.failed_candidate_database)
            failed_evidence = preflight.failed_candidate_database
        elif preflight.candidate_database.exists():
            if _sha256(preflight.candidate_database) != request.expected_candidate_sha256:
                raise AtomicCutoverError("candidate evidence changed during failed activation")
            failed_evidence = preflight.candidate_database
        preflight.rollback_database.rename(preflight.live_database)
        restored_sha = _sha256(preflight.live_database)
        if restored_sha != request.expected_live_sha256:
            raise AtomicCutoverError("restored live database differs from original SHA-256")
    except Exception as exc:
        rollback_error = exc

    if rollback_error is None:
        receipt = _seal_receipt(
            mode=request.mode,
            status="rolled_back",
            preflight=preflight,
            expected_alembic_head=request.expected_alembic_head,
            live_sha256_before=request.expected_live_sha256,
            candidate_sha256=request.expected_candidate_sha256,
            active_sha256_after=request.expected_live_sha256,
            rollback_sha256=request.expected_live_sha256,
            candidate_precheck=preflight.candidate_verification,
            rollback_restored=True,
            failed_candidate_database=failed_evidence,
            failure=_safe_failure(activation_error),
            started_at=started_at,
        )
        _write_failure_receipt(preflight.receipt_path, receipt)
        return ActivationRolledBackError(receipt)

    receipt = _seal_receipt(
        mode=request.mode,
        status="rollback_failed",
        preflight=preflight,
        expected_alembic_head=request.expected_alembic_head,
        live_sha256_before=request.expected_live_sha256,
        candidate_sha256=request.expected_candidate_sha256,
        candidate_precheck=preflight.candidate_verification,
        rollback_restored=False,
        failed_candidate_database=failed_evidence,
        failure=(
            f"activation failed: {_safe_failure(activation_error)}; "
            f"rollback failed: {_safe_failure(rollback_error)}"
        ),
        started_at=started_at,
    )
    _write_failure_receipt(preflight.receipt_path, receipt)
    return AtomicCutoverError(receipt.failure or "activation and rollback failed")


def _preflight(request: ActivationRequest) -> _Preflight:
    repo_root = _resolve_directory(request.repo_root, label="repo root")
    live = _resolve_existing_file(request.live_database, label="live database")
    candidate = _resolve_existing_file(request.candidate_database, label="candidate database")
    rollback = _resolve_output(request.rollback_database, label="rollback destination")
    failed_candidate = _resolve_output(
        request.failed_candidate_database,
        label="failed-candidate destination",
    )
    receipt_path = _resolve_output(request.receipt_path, label="activation receipt")
    quiescence_path = _resolve_existing_file(
        request.quiescence_receipt_path,
        label="quiescence receipt",
    )
    _require_distinct_paths(
        live,
        candidate,
        rollback,
        failed_candidate,
        receipt_path,
        quiescence_path,
    )
    if os.path.samefile(live, candidate):
        raise AtomicCutoverError("live and candidate databases must be distinct files")
    _require_single_link(live, candidate)
    _require_same_volume(live, candidate, rollback, failed_candidate)
    _require_no_sidecars(live, candidate)

    live_sha = _sha256(live)
    if live_sha != request.expected_live_sha256:
        raise AtomicCutoverError("live database SHA-256 differs from commitment")
    candidate_sha = _sha256(candidate)
    if candidate_sha != request.expected_candidate_sha256:
        raise AtomicCutoverError("candidate database SHA-256 differs from commitment")

    quiescence = _load_quiescence_receipt(quiescence_path)
    _require_quiescence(
        quiescence,
        expected_receipt_sha256=request.expected_quiescence_receipt_sha256,
        live_database=live,
        live_sha256=live_sha,
    )
    candidate_verification = _verify_database(
        candidate,
        expected_head=request.expected_alembic_head,
    )
    _require_no_sidecars(live, candidate)
    if candidate_verification.sha256 != candidate_sha:
        raise AtomicCutoverError("candidate changed while integrity checks were running")
    return _Preflight(
        repo_root=repo_root,
        live_database=live,
        candidate_database=candidate,
        rollback_database=rollback,
        failed_candidate_database=failed_candidate,
        receipt_path=receipt_path,
        quiescence_receipt_path=quiescence_path,
        quiescence_receipt=quiescence,
        candidate_verification=candidate_verification,
    )


def _revalidate_locked(request: ActivationRequest, preflight: _Preflight) -> None:
    for path, label in (
        (preflight.rollback_database, "rollback destination"),
        (preflight.failed_candidate_database, "failed-candidate destination"),
        (preflight.receipt_path, "activation receipt"),
    ):
        if path.exists():
            raise AtomicCutoverError(f"{label} appeared after preflight: {path}")
    _require_single_link(preflight.live_database, preflight.candidate_database)
    _require_same_volume(
        preflight.live_database,
        preflight.candidate_database,
        preflight.rollback_database,
        preflight.failed_candidate_database,
    )
    _require_no_sidecars(preflight.live_database, preflight.candidate_database)
    live_sha = _sha256(preflight.live_database)
    if live_sha != request.expected_live_sha256:
        raise AtomicCutoverError("live database changed before locked activation")
    candidate_sha = _sha256(preflight.candidate_database)
    if candidate_sha != request.expected_candidate_sha256:
        raise AtomicCutoverError("candidate database changed before locked activation")
    quiescence = _load_quiescence_receipt(preflight.quiescence_receipt_path)
    _require_quiescence(
        quiescence,
        expected_receipt_sha256=request.expected_quiescence_receipt_sha256,
        live_database=preflight.live_database,
        live_sha256=live_sha,
    )


def _verify_database(path: Path, *, expected_head: str) -> DatabaseVerification:
    # Both candidate and installed live files are hash-bound and quiesced for
    # this scan. Immutable mode prevents SQLite from creating WAL/SHM files
    # merely to inspect a clean WAL-mode database at the rename boundary.
    connection = connect_sqlite(
        path,
        role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY,
    )
    try:
        revision_rows = connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
        quick_rows = connection.execute("PRAGMA quick_check").fetchall()
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    except Exception as exc:
        raise AtomicCutoverError(
            f"candidate database verification failed: {_safe_failure(exc)}"
        ) from exc
    finally:
        connection.close()
    if len(revision_rows) != 1 or str(revision_rows[0][0]) != expected_head:
        observed = tuple(str(row[0]) for row in revision_rows)
        raise AtomicCutoverError(
            f"candidate Alembic head mismatch: expected {expected_head}, observed {observed}"
        )
    quick = tuple(str(row[0]) for row in quick_rows)
    integrity = tuple(str(row[0]) for row in integrity_rows)
    if quick != ("ok",):
        raise AtomicCutoverError(f"candidate quick_check failed: {quick}")
    if integrity != ("ok",):
        raise AtomicCutoverError(f"candidate integrity_check failed: {integrity}")
    if foreign_key_rows:
        raise AtomicCutoverError(
            f"candidate foreign-key check failed with {len(foreign_key_rows)} violation(s)"
        )
    return DatabaseVerification(
        database=str(path),
        sha256=_sha256(path),
        alembic_revision=expected_head,
        quick_check=quick,
        integrity_check=integrity,
        foreign_key_violations=0,
    )


def _load_quiescence_receipt(path: Path) -> QuiescenceReceipt:
    try:
        return QuiescenceReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AtomicCutoverError(f"invalid quiescence receipt: {_safe_failure(exc)}") from exc


def _require_quiescence(
    receipt: QuiescenceReceipt,
    *,
    expected_receipt_sha256: str,
    live_database: Path,
    live_sha256: str,
) -> None:
    computed_sha = quiescence_payload_sha256(receipt)
    if receipt.receipt_sha256 != computed_sha:
        raise AtomicCutoverError("quiescence receipt self-seal is invalid")
    if computed_sha != expected_receipt_sha256:
        raise AtomicCutoverError("quiescence receipt differs from reviewed commitment")
    if Path(receipt.live_database).resolve() != live_database:
        raise AtomicCutoverError("quiescence receipt names a different live database")
    if receipt.live_database_sha256 != live_sha256:
        raise AtomicCutoverError("quiescence receipt is bound to a different live database SHA-256")
    now = _utc_now()
    if now < receipt.captured_at.astimezone(UTC) or now > receipt.valid_until.astimezone(UTC):
        raise AtomicCutoverError("quiescence receipt is not currently valid")
    _require_exact_inventory(
        "task",
        receipt.expected_task_paths,
        tuple(task.path for task in receipt.tasks),
    )
    _require_exact_inventory(
        "service",
        receipt.expected_service_names,
        tuple(service.name for service in receipt.services),
    )
    _require_exact_inventory(
        "listener",
        receipt.expected_listener_endpoints,
        tuple(listener.endpoint for listener in receipt.listeners),
    )


def _require_exact_inventory(
    label: str,
    expected: tuple[str, ...],
    observed: tuple[str, ...],
) -> None:
    expected_keys = tuple(item.casefold() for item in expected)
    observed_keys = tuple(item.casefold() for item in observed)
    if len(set(expected_keys)) != len(expected_keys):
        raise AtomicCutoverError(f"{label} inventory commitment contains duplicates")
    if len(set(observed_keys)) != len(observed_keys):
        raise AtomicCutoverError(f"{label} inventory observation contains duplicates")
    if set(expected_keys) != set(observed_keys):
        raise AtomicCutoverError(
            f"{label} inventory does not exactly match its reviewed commitment"
        )


def _resolve_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AtomicCutoverError(f"{label} does not exist: {path}") from exc
    if not resolved.is_dir():
        raise AtomicCutoverError(f"{label} is not a directory: {resolved}")
    return resolved


def _resolve_existing_file(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AtomicCutoverError(f"{label} does not exist: {path}") from exc
    if not resolved.is_file():
        raise AtomicCutoverError(f"{label} is not a file: {resolved}")
    return resolved


def _resolve_output(path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    if resolved.exists():
        if label == "rollback destination":
            raise AtomicCutoverError(f"rollback destination already exists: {resolved}")
        raise AtomicCutoverError(f"{label} already exists: {resolved}")
    if not resolved.parent.is_dir():
        raise AtomicCutoverError(f"{label} parent directory does not exist: {resolved.parent}")
    return resolved


def _require_distinct_paths(*paths: Path) -> None:
    normalized = [os.path.normcase(str(path.resolve())) for path in paths]
    if len(set(normalized)) != len(normalized):
        raise AtomicCutoverError("all cutover input and output paths must be distinct")


def _volume_identity(path: Path) -> str:
    anchor = path if path.exists() else path.parent
    resolved = anchor.resolve()
    return f"{resolved.anchor.casefold()}:{resolved.stat().st_dev}"


def _require_same_volume(
    live: Path,
    candidate: Path,
    rollback: Path,
    failed_candidate: Path,
) -> None:
    identities = {
        _volume_identity(live),
        _volume_identity(candidate),
        _volume_identity(rollback),
        _volume_identity(failed_candidate),
    }
    if len(identities) != 1:
        raise AtomicCutoverError(
            "live, candidate, rollback, and failed-candidate paths must share one "
            "same filesystem volume"
        )


def _require_no_sidecars(*databases: Path) -> None:
    found = [
        str(Path(f"{database}{suffix}"))
        for database in databases
        for suffix in _SIDECAR_SUFFIXES
        if Path(f"{database}{suffix}").exists()
    ]
    if found:
        raise AtomicCutoverError(f"SQLite sidecar files must be absent: {found}")


def _require_single_link(*databases: Path) -> None:
    linked = [str(database) for database in databases if database.stat().st_nlink != 1]
    if linked:
        raise AtomicCutoverError(
            f"SQLite databases must not have multiple filesystem links: {linked}"
        )


def _replace_live_with_candidate(
    *,
    live: Path,
    candidate: Path,
    rollback: Path,
) -> None:
    """Replace live and create its backup in one Windows filesystem operation."""
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
        if not replace_file(
            str(live),
            str(candidate),
            str(rollback),
            0,
            None,
            None,
        ):
            error = ctypes.get_last_error()
            raise OSError(error, f"ReplaceFileW failed: {ctypes.FormatError(error)}")
        return

    # Non-Windows exists for deterministic CI and recovery rehearsals. The
    # production deployment is Windows and uses ReplaceFileW above.
    live.rename(rollback)
    candidate.rename(live)


def _seal_receipt(
    *,
    mode: ActivationMode,
    status: Literal["ready", "activated", "rolled_back", "rollback_failed"],
    preflight: _Preflight,
    expected_alembic_head: str,
    live_sha256_before: str,
    candidate_sha256: str,
    started_at: datetime,
    active_sha256_after: str | None = None,
    rollback_sha256: str | None = None,
    candidate_precheck: DatabaseVerification | None = None,
    active_postcheck: DatabaseVerification | None = None,
    rollback_restored: bool = False,
    failed_candidate_database: Path | None = None,
    failure: str | None = None,
) -> ActivationReceipt:
    fields: dict[str, object] = {
        "schema_version": "1",
        "mode": mode,
        "status": status,
        "activation_mechanism": (
            "windows_replace_file" if os.name == "nt" else "portable_rename_pair"
        ),
        "repo_root": str(preflight.repo_root),
        "live_database": str(preflight.live_database),
        "candidate_database": str(preflight.candidate_database),
        "rollback_database": str(preflight.rollback_database),
        "failed_candidate_database": (
            str(failed_candidate_database) if failed_candidate_database is not None else None
        ),
        "receipt_path": str(preflight.receipt_path),
        "expected_alembic_head": expected_alembic_head,
        "quiescence_receipt_sha256": preflight.quiescence_receipt.receipt_sha256,
        "live_sha256_before": live_sha256_before,
        "candidate_sha256": candidate_sha256,
        "active_sha256_after": active_sha256_after,
        "rollback_sha256": rollback_sha256,
        "candidate_precheck": candidate_precheck or preflight.candidate_verification,
        "active_postcheck": active_postcheck,
        "rollback_restored": rollback_restored,
        "failure": failure,
        "started_at": started_at,
        "completed_at": _utc_now(),
    }
    unsealed = ActivationReceipt.model_validate({**fields, "receipt_sha256": "0" * 64})
    receipt_sha256 = activation_payload_sha256(unsealed)
    return unsealed.model_copy(update={"receipt_sha256": receipt_sha256})


def _write_receipt(path: Path, receipt: ActivationReceipt) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_activation_json(receipt))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise AtomicCutoverError(f"activation receipt already exists: {path}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_failure_receipt(path: Path, receipt: ActivationReceipt) -> None:
    try:
        _write_receipt(path, receipt)
    except Exception as exc:
        raise AtomicCutoverError(
            f"{receipt.failure}; additionally failed to persist receipt: {_safe_failure(exc)}"
        ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_failure(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _utc_now() -> datetime:
    return datetime.now(UTC)
