"""Record explicit disposable run ownership without inferring legacy ownership."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import socket
import stat
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.operations.artifact_retention import (
    Artifact,
    ArtifactCatalog,
    RetirementProof,
    RetirementResult,
    fixture_git_hold,
    run_retention,
)
from src.operations.storage_reservation import live_lease_roots, release_peak, reserve_peak

MANIFEST_NAME = ".earnings-temp-run.json"
RESOLUTION_NAME = ".earnings-temp-run-resolution.json"
# Session roots the pytest harness creates in the system temp directory. The
# shared ``earnings-summary-pytest-secrets*`` folders deliberately do not match.
_TEST_RUN_NAME = re.compile(r"earnings-summary-pytest-(?:controller|gw\d+)-[A-Za-z0-9_]+\Z")
# A completed run's payload is already retired or disposable; a failed run keeps
# its default seven-day review window (``begin_temp_run`` sets ``review_after``).
TEST_RUN_SKELETON_AGE = timedelta(days=1)
TEST_RUN_FAILED_AGE = timedelta(days=7)
# A failed or crashed run keeps its fixture payload this long, then only its
# review evidence (manifest, logs, JUnit XML, JSONL, retirement receipts) stays
# until TEST_RUN_FAILED_AGE: a failed full suite otherwise held tens of GB.
TEST_RUN_PAYLOAD_GRACE = timedelta(hours=1)
_EVIDENCE_SUFFIXES = frozenset({".log", ".xml", ".jsonl"})
_RETIREMENT_DIR = ".earnings-temp-retirement"
# Links and multiply linked files stay in place, unfollowed and never retired, so
# they do not make a passed rerun unfit to prove a failed attempt's recovery.
_RETAINED_SHAPE_HOLDS = frozenset({"linked_fixture", "not_unique_regular_file"})
_SOURCE_PARTS = {
    ".git",
    ".claude",
    "src",
    "execution",
    "directives",
    "ir_documents",
    "transcripts",
    "historical",
    "node_modules",
}


class TempRunFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    mtime_ns: int
    inode: int
    device: int

    @field_validator("relative_path")
    @classmethod
    def relative_file(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts or not value.parts:
            raise ValueError("disposable paths must be relative files without traversal")
        if value.name == MANIFEST_NAME:
            raise ValueError("the ownership manifest is retained evidence")
        return value


class TempRunHold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    reason: str = Field(min_length=1)


class _FileHoldError(ValueError):
    """A fixed diagnostic without file contents or provider exception text."""


class TempRunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="earnings-temp-run/v1", pattern=r"^earnings-temp-run/v1$")
    owner: str = Field(default="earnings-summary", pattern=r"^earnings-summary$")
    run_id: str = Field(min_length=1)
    run_root: Path
    started_at: datetime
    completed_at: datetime | None = None
    status: Literal["active", "failed", "completed"]
    disposable_class: Literal["declared_files", "isolated_test_fixture"] = "declared_files"
    purpose: str = Field(default="legacy", min_length=1)
    byte_budget: int | None = Field(default=None, ge=0)
    review_after: datetime | None = None
    producer_host: str | None = None
    reservation_token: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    retirement_proofs: tuple[RetirementProof, ...] = ()
    pins: list[str] = Field(default_factory=list)
    files: list[TempRunFile] = Field(default_factory=lambda: list[TempRunFile]())
    held_paths: list[TempRunHold] = Field(default_factory=lambda: list[TempRunHold]())

    @field_validator("started_at", "completed_at", "review_after")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("run timestamps must include a timezone")
        return value.astimezone(UTC) if value is not None else None


class TempRunResolution(BaseModel):
    """A reviewed closure links history instead of changing a failed attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["earnings-temp-run-resolution/v1"] = "earnings-temp-run-resolution/v1"
    run_root: Path
    failed_run_id: str
    failed_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    resolved_at: datetime
    reason: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    successful_manifest: RetirementProof
    files: list[TempRunFile]


class ReviewedTestFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    relative_path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)


class TempRunReview(BaseModel):
    """Exact operator declaration for existing test files; never an age inference."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["earnings-temp-run-review/v1"] = "earnings-temp-run-review/v1"
    run_root: Path
    completed_at: datetime
    purpose: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    disposable_class: Literal["declared_files", "isolated_test_fixture"] = "declared_files"
    fixture_origin: Literal["synthetic_generated"] | None = None
    files: list[ReviewedTestFile] = Field(min_length=1)


class TempRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path
    scope: Literal["run", "boundary"] = "run"
    status: Literal["active", "failed", "completed", "unknown"]
    files: int = 0
    bytes: int = 0
    registered_files: int = 0
    registered_bytes: int = 0
    held_files: int = 0
    held_bytes: int = 0
    problems: list[str] = Field(default_factory=list)
    held_paths: list[TempRunHold] = Field(default_factory=lambda: list[TempRunHold]())
    purpose: str | None = None
    byte_budget: int | None = None
    review_after: datetime | None = None
    review_due: bool = False
    budget_exceeded: bool = False
    resolution_status: Literal["unresolved", "resolved"] = "unresolved"


class TempRunDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog: ArtifactCatalog
    reports: list[TempRunReport]


class TempRunPruneResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    removed: tuple[Path, ...] = ()
    trimmed: tuple[Path, ...] = ()
    trimmed_bytes: int = 0
    kept: dict[str, int] = Field(default_factory=dict)


def _now(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise ValueError("now must include a timezone")
    return result.astimezone(UTC)


def _linked(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_path(path: Path) -> None:
    for ancestor in (path, *path.parents):
        if (ancestor.exists() or ancestor.is_symlink()) and _linked(ancestor):
            raise ValueError("temporary runs may not traverse links or reparse points")


def _absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError("temporary roots may not contain parent traversal")
    absolute = Path(os.path.abspath(path))
    temporary = Path(os.path.abspath(tempfile.gettempdir()))
    # macOS exposes its system temporary directory through the trusted /var
    # alias. Normalize only that configured boundary; never resolve run children.
    if absolute == temporary or temporary in absolute.parents:
        return temporary.resolve() / absolute.relative_to(temporary)
    return absolute


def _search_roots(repo_root: Path, code_root: Path | None) -> list[tuple[Path, bool]]:
    roots = [(_absolute(repo_root) / ".tmp", False)]
    if code_root is not None:
        roots.append((_absolute(code_root) / ".tmp", False))
    roots.append((_absolute(Path(tempfile.gettempdir())), True))
    if os.name == "nt":
        roots.append((Path("C:/tmp"), True))
    return list(dict.fromkeys(roots))


def _owned_root(run_root: Path, repo_root: Path, code_root: Path | None) -> Path:
    root = _absolute(run_root)
    for boundary, prefixed in _search_roots(repo_root, code_root):
        if boundary in root.parents:
            first = root.relative_to(boundary).parts[0]
            if not prefixed or first.startswith("earnings-summary-"):
                _safe_path(root)
                return root
    raise ValueError("run root is outside approved temporary ownership scopes")


def _write_manifest(root: Path, manifest: TempRunManifest) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".earnings-run-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(manifest.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / MANIFEST_NAME)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_manifest(root: Path) -> TempRunManifest:
    path = root / MANIFEST_NAME
    _safe_path(path)
    manifest = TempRunManifest.model_validate_json(path.read_bytes())
    if manifest.run_root != root:
        raise ValueError("ownership manifest does not match its exact run root")
    names = [os.path.normcase(str(item.relative_path)) for item in manifest.files]
    if len(names) != len(set(names)):
        raise ValueError("ownership manifest has duplicate disposable paths")
    if manifest.status == "completed" and (
        manifest.completed_at is None or manifest.completed_at < manifest.started_at
    ):
        raise ValueError("completed run requires a valid completion timestamp")
    if manifest.status != "completed" and manifest.files:
        raise ValueError("unfinished run may not declare verified disposable files")
    return manifest


def begin_temp_run(
    run_root: Path,
    *,
    repo_root: Path,
    code_root: Path | None = None,
    now: datetime | None = None,
    disposable_class: Literal["declared_files", "isolated_test_fixture"] = "declared_files",
    purpose: str = "legacy",
    byte_budget: int | None = None,
    review_after: datetime | None = None,
    expected_bytes: int = 0,
    min_free_bytes: int = 50 * 1024 * 1024 * 1024,
) -> TempRunManifest:
    """Claim only a new or empty exact caller-owned temporary directory."""
    root = _owned_root(run_root, repo_root, code_root)
    stamp = _now(now)
    if expected_bytes < 0 or min_free_bytes < 0 or (byte_budget is not None and byte_budget < 0):
        raise ValueError("disk admission values must be nonnegative")
    if byte_budget is not None and expected_bytes > byte_budget:
        raise ValueError("expected_bytes_exceeds_budget")
    if disposable_class == "isolated_test_fixture" and (purpose == "legacy" or not purpose.strip()):
        raise ValueError("isolated fixtures require an explicit purpose")
    existing = next(parent for parent in (root, *root.parents) if parent.exists())
    if shutil.disk_usage(existing).free < expected_bytes + min_free_bytes:
        raise ValueError("insufficient_disk_space")
    manifest = TempRunManifest(
        run_id=str(uuid4()),
        run_root=root,
        started_at=stamp,
        status="active",
        disposable_class=disposable_class,
        purpose=purpose,
        byte_budget=byte_budget,
        review_after=_now(review_after) if review_after else stamp + timedelta(days=7),
        producer_host=socket.gethostname(),
    )
    root.mkdir(parents=True, exist_ok=True)
    _safe_path(root)
    if any(root.iterdir()):
        raise ValueError("run ownership requires a new empty root")
    claim = root / ".earnings-temp-run.claim"
    try:
        descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("run root already has an ownership claimant") from exc
    os.close(descriptor)
    token: str | None = None
    try:
        if (root / MANIFEST_NAME).exists():
            raise ValueError("run root already has an ownership manifest")
        peak = expected_bytes or byte_budget or 0
        if peak:
            token = reserve_peak(root, peak, reserve_bytes=min_free_bytes)
            manifest = manifest.model_copy(update={"reservation_token": token})
        _write_manifest(root, manifest)
    except (OSError, ValueError):
        if token is not None and not (root / MANIFEST_NAME).exists():
            release_peak(root, token)
        raise
    finally:
        claim.unlink()
    return manifest


def _seal_file(
    root: Path,
    declared: Path,
    names: set[str],
    live: Path | None,
    *,
    isolated_fixture: bool = False,
) -> TempRunFile | None:
    path = _absolute(declared if declared.is_absolute() else root / declared)
    if root not in path.parents:
        raise _FileHoldError("scope_escape")
    relative = path.relative_to(root)
    name = os.path.normcase(str(relative))
    if relative.name in {MANIFEST_NAME, RESOLUTION_NAME}:
        return None
    if name in names:
        raise _FileHoldError("duplicate_path")
    names.add(name)
    try:
        _safe_path(path)
    except ValueError as exc:
        raise _FileHoldError("linked_path") from exc
    before = path.stat()
    if stat.S_ISDIR(before.st_mode):
        return None
    if not isolated_fixture and any(part.casefold() in _SOURCE_PARTS for part in relative.parts):
        raise _FileHoldError("protected_source")
    if path.name.casefold() == "portfolio_gc_archive.db" or (
        live is not None
        and os.path.normcase(str(path))
        in {os.path.normcase(str(live) + suffix) for suffix in ("", "-wal", "-shm", "-journal")}
    ):
        raise _FileHoldError("live_or_archive_database")
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _FileHoldError("not_unique_regular_file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    try:
        _safe_path(path)
    except ValueError as exc:
        raise _FileHoldError("linked_path") from exc
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise _FileHoldError("file_changed")
    return TempRunFile(
        relative_path=relative,
        sha256=digest.hexdigest(),
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        inode=after.st_ino,
        device=after.st_dev,
    )


def finish_temp_run(
    run_root: Path,
    *,
    repo_root: Path,
    code_root: Path | None = None,
    success: bool,
    disposable_paths: Iterable[Path] = (),
    now: datetime | None = None,
    allow_partial: bool = False,
) -> TempRunManifest:
    """Seal declared files after caller shutdown; explicitly report partial holds."""
    root = _owned_root(run_root, repo_root, code_root)
    manifest = _read_manifest(root)
    if manifest.status != "active":
        raise ValueError("only an active owned run can be finalized")
    stamp = _now(now)
    if stamp < manifest.started_at:
        raise ValueError("completion may not precede run start")
    files: list[TempRunFile] = []
    held: list[TempRunHold] = []
    names: set[str] = set()
    if success:
        isolated_fixture = manifest.disposable_class == "isolated_test_fixture"
        fixture_holds = _fixture_tree_holds(root) if isolated_fixture else []
        if fixture_holds and not allow_partial:
            raise ValueError(fixture_holds[0].reason)
        held.extend(fixture_holds)
        held_paths = frozenset(item.path for item in fixture_holds)
        configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
        live = _absolute(Path(configured)) if configured else None
        for declared in disposable_paths:
            try:
                if _under_hold(root, declared, held_paths):
                    continue
                item = _seal_file(root, declared, names, live, isolated_fixture=isolated_fixture)
                if item is not None:
                    files.append(item)
            except (OSError, ValueError) as exc:
                if not allow_partial:
                    raise
                reason = (
                    str(exc) if isinstance(exc, _FileHoldError) else "unavailable_or_invalid_file"
                )
                held.append(TempRunHold(path=declared, reason=reason))
                try:
                    held_path = _absolute(declared if declared.is_absolute() else root / declared)
                except ValueError:
                    continue
                files = [item for item in files if root / item.relative_path != held_path]
    sealed = manifest.model_copy(
        update={
            "status": "completed" if success else "failed",
            "completed_at": stamp,
            "files": files,
            "held_paths": held,
        }
    )
    _write_manifest(root, sealed)
    # The peak lease covers a live producer only. A failed seal keeps its manifest
    # and payload protected for review; the bytes remain in free-space admission.
    if sealed.reservation_token is not None:
        release_peak(root, sealed.reservation_token)
    return sealed


def retire_completed_temp_run(
    run_root: Path,
    *,
    repo_root: Path,
    code_root: Path | None = None,
    apply: bool = False,
    now: datetime | None = None,
    live_db: Path | None = None,
) -> RetirementResult:
    """Retire only this completed producer's sealed files, without discovery."""
    root = _owned_root(run_root, repo_root, code_root)
    path = root / MANIFEST_NAME
    _safe_path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = _read_manifest(root)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("ownership manifest changed during exact run closure")
    if (
        manifest.status != "completed"
        or manifest.completed_at is None
        or manifest.disposable_class != "isolated_test_fixture"
    ):
        raise ValueError("immediate retirement requires a completed isolated fixture run")
    artifacts = [
        Artifact(
            path=root / item.relative_path,
            allowed_root=root,
            family=f"temp-run:{manifest.run_id}",
            created_at=manifest.completed_at,
            sha256=item.sha256,
            size=item.size,
            kind="disposable_test",
            status="completed",
            verified=True,
            disposable_class=manifest.disposable_class,
            purpose=manifest.purpose,
            byte_budget=manifest.byte_budget,
            review_after=manifest.review_after,
            file_identity=(item.device, item.inode, item.mtime_ns),
            pins=manifest.pins,
            lifecycle_manifest=path,
            lifecycle_manifest_sha256=digest,
            retirement_proofs=manifest.retirement_proofs,
        )
        for item in manifest.files
    ]
    return run_retention(
        repo_root,
        now=_now(now),
        apply=apply,
        catalog=ArtifactCatalog(schema_version=1, artifacts=artifacts),
        catalog_roots=tuple(dict.fromkeys((repo_root, code_root or repo_root))),
        test_retention_days=0,
        exact_run_root=root,
        live_db=live_db,
    )


def _fixture_tree_holds(root: Path) -> list[TempRunHold]:
    """Only newly claimed isolated fixtures can contain disposable source names."""
    held: list[TempRunHold] = []
    for base, directories, files in os.walk(root, followlinks=False):
        directory = Path(base)
        _safe_path(directory)
        if reason := fixture_git_hold(directory, root):
            held.append(TempRunHold(path=directory, reason=reason))
            directories[:] = []
            continue
        for name in (*directories, *files):
            child = directory / name
            if _linked(child):
                held.append(TempRunHold(path=child, reason="linked_fixture"))
                if name in directories:
                    directories.remove(name)
    return held


def _blocking_holds(holds: Iterable[TempRunHold]) -> list[TempRunHold]:
    return [hold for hold in holds if hold.reason not in _RETAINED_SHAPE_HOLDS]


def _under_hold(root: Path, declared: Path, held_paths: frozenset[Path]) -> bool:
    absolute = _absolute(declared if declared.is_absolute() else root / declared)
    return absolute in held_paths or any(parent in held_paths for parent in absolute.parents)


def _write_once(path: Path, payload: BaseModel) -> None:
    """Publish a new receipt without overwriting earlier closure or review."""
    _safe_path(path)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload.model_dump_json())
        handle.flush()
        os.fsync(handle.fileno())


def _newest_change(root: Path, now: datetime) -> datetime:
    # Some tests stamp fixtures years ahead; a future time is not a recent change.
    ceiling = now.timestamp()
    newest = min(root.lstat().st_mtime, ceiling)
    for directory, children, names in os.walk(root, followlinks=False):
        for name in (*children, *names):
            try:
                changed = (Path(directory) / name).lstat().st_mtime
            except FileNotFoundError:
                continue
            if changed <= ceiling:
                newest = max(newest, changed)
    return datetime.fromtimestamp(newest, UTC)


def _trim_payload(root: Path, *, apply: bool) -> int:
    """Remove a run's payload, keeping review evidence; links are removed, never followed."""
    freed = 0
    for directory, children, names in os.walk(root, topdown=False, followlinks=False):
        here = Path(directory)
        relative = here.relative_to(root)
        if relative.parts[:1] == (_RETIREMENT_DIR,):
            continue
        for name in names + [child for child in children if (here / child).is_symlink()]:
            path = here / name
            if relative == Path(".") and name in {MANIFEST_NAME, RESOLUTION_NAME}:
                continue
            if not path.is_symlink() and path.suffix.lower() in _EVIDENCE_SUFFIXES:
                continue
            try:
                freed += path.lstat().st_size
                if apply:
                    path.unlink()
            except FileNotFoundError:
                continue
        if apply and here != root:
            with contextlib.suppress(OSError):
                here.rmdir()
    return freed


def prune_abandoned_test_runs(
    *,
    temp_root: Path | None = None,
    store: Path | None = None,
    now: datetime | None = None,
    apply: bool = True,
) -> TempRunPruneResult:
    """Remove earlier pytest session roots whose owner is gone.

    Only direct children named like harness session roots are considered, and a
    root whose lease holder may still run is always kept. Completed runs and
    folders without a manifest go after a day; failed runs, and runs still
    marked active whose holder is gone, keep only their review evidence after
    an hour and go after the seven-day review window. Links and unreadable
    manifests are kept for review.
    """
    base = _absolute(temp_root or Path(tempfile.gettempdir()))
    _safe_path(base)
    stamp = _now(now)
    live = {os.path.normcase(str(root)) for root in live_lease_roots(base, store=store)}
    removed: list[Path] = []
    trimmed: list[Path] = []
    trimmed_bytes = 0
    kept: dict[str, int] = {}

    def keep(reason: str) -> None:
        kept[reason] = kept.get(reason, 0) + 1

    for root in sorted(base.iterdir()):
        if not _TEST_RUN_NAME.match(root.name):
            continue
        if _linked(root) or not root.is_dir():
            keep("linked_or_not_directory")
            continue
        if os.path.normcase(str(root)) in live:
            keep("live_lease")
            continue
        if (root / MANIFEST_NAME).exists() or (root / MANIFEST_NAME).is_symlink():
            try:
                manifest = _read_manifest(root)
            except (OSError, ValueError):
                keep("unreadable_manifest")
                continue
            if manifest.status == "completed":
                ended, window = manifest.completed_at or manifest.started_at, TEST_RUN_SKELETON_AGE
            elif manifest.status == "failed":
                ended, window = manifest.completed_at or manifest.started_at, TEST_RUN_FAILED_AGE
            else:
                ended, window = manifest.started_at, TEST_RUN_FAILED_AGE
        else:
            ended, window = _newest_change(root, stamp), TEST_RUN_SKELETON_AGE
        if stamp - ended < window:
            if window == TEST_RUN_FAILED_AGE and stamp - ended >= TEST_RUN_PAYLOAD_GRACE:
                try:
                    freed = _trim_payload(root, apply=apply)
                except OSError:
                    keep("trim_failed")
                    continue
                if freed:
                    trimmed.append(root)
                    trimmed_bytes += freed
            keep("within_window")
            continue
        if apply:
            try:
                shutil.rmtree(root)
            except FileNotFoundError:
                pass
            except OSError:
                keep("removal_failed")
                continue
        removed.append(root)
    return TempRunPruneResult(
        removed=tuple(removed), trimmed=tuple(trimmed), trimmed_bytes=trimmed_bytes, kept=kept
    )


def resolve_failed_temp_run(
    run_root: Path,
    *,
    successful_root: Path,
    repo_root: Path,
    disposable_paths: Iterable[Path],
    reason: str,
    reviewed_by: str,
    code_root: Path | None = None,
    now: datetime | None = None,
) -> TempRunResolution:
    """Close reviewed failure recovery with exact files and a bound successful rerun."""
    root = _owned_root(run_root, repo_root, code_root)
    success_root = _owned_root(successful_root, repo_root, code_root)
    original = _read_manifest(root)
    success = _read_manifest(success_root)
    stamp = _now(now)
    if (
        original.status != "failed"
        or original.pins
        or success.status != "completed"
        or success.pins
        or _blocking_holds(success.held_paths)
        or original.purpose == "legacy"
        or original.purpose != success.purpose
        or original.disposable_class != success.disposable_class
        or original.completed_at is None
        or success.completed_at is None
        or success.completed_at < original.completed_at
        or stamp < success.completed_at
    ):
        raise ValueError("unverified_failure_resolution")
    holds: list[TempRunHold] = []
    if original.disposable_class == "isolated_test_fixture":
        holds = _fixture_tree_holds(root)
        if blocking := _blocking_holds(holds):
            raise ValueError(blocking[0].reason)
    held_paths = frozenset(item.path for item in holds)
    configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    live = _absolute(Path(configured)) if configured else None
    names: set[str] = set()
    files = [
        item
        for declared in disposable_paths
        if not _under_hold(root, declared, held_paths)
        and (
            item := _seal_file(
                root,
                declared,
                names,
                live,
                isolated_fixture=original.disposable_class == "isolated_test_fixture",
            )
        )
        is not None
    ]
    resolution = TempRunResolution(
        run_root=root,
        failed_run_id=original.run_id,
        failed_manifest_sha256=hashlib.sha256((root / MANIFEST_NAME).read_bytes()).hexdigest(),
        resolved_at=stamp,
        reason=reason,
        reviewed_by=reviewed_by,
        successful_manifest=RetirementProof(
            path=success_root / MANIFEST_NAME,
            sha256=hashlib.sha256((success_root / MANIFEST_NAME).read_bytes()).hexdigest(),
        ),
        files=files,
    )
    _write_once(root / RESOLUTION_NAME, resolution)
    return resolution


def register_reviewed_test_run(
    review_path: Path,
    *,
    repo_root: Path,
    code_root: Path | None = None,
) -> TempRunManifest:
    """Register only exact reviewed legacy test files; do not adopt source trees."""
    review_path = _absolute(review_path)
    _safe_path(review_path)
    raw = review_path.read_bytes()
    review = TempRunReview.model_validate_json(raw)
    root = _owned_root(review.run_root, repo_root, code_root)
    if (root / MANIFEST_NAME).exists() or (root / MANIFEST_NAME).is_symlink():
        raise ValueError("existing_run_ownership")
    stamp = _now(review.completed_at)
    isolated = review.disposable_class == "isolated_test_fixture"
    if isolated and review.fixture_origin != "synthetic_generated":
        raise ValueError("reviewed_fixture_origin_required")
    fixture_holds = _fixture_tree_holds(root) if isolated else []
    held_paths = frozenset(item.path for item in fixture_holds)
    configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
    live = _absolute(Path(configured)) if configured else None
    names: set[str] = set()
    files: list[TempRunFile] = []
    for declared in review.files:
        if _under_hold(root, declared.relative_path, held_paths):
            raise ValueError("reviewed_fixture_storage_unsafe")
        item = _seal_file(root, declared.relative_path, names, live, isolated_fixture=isolated)
        if item is None or item.sha256 != declared.sha256 or item.size != declared.size:
            raise ValueError("reviewed_file_changed")
        files.append(item)
    if review_path.read_bytes() != raw:
        raise ValueError("review_changed")
    manifest = TempRunManifest(
        run_root=root,
        run_id=str(uuid4()),
        started_at=stamp,
        completed_at=stamp,
        status="completed",
        disposable_class=review.disposable_class,
        purpose=review.purpose,
        files=files,
        review_after=stamp + timedelta(days=7),
        producer_host=socket.gethostname(),
        retirement_proofs=(
            RetirementProof(path=review_path, sha256=hashlib.sha256(raw).hexdigest()),
        ),
    )
    _write_once(root / MANIFEST_NAME, manifest)
    return manifest


def _walk(
    root: Path,
    problems: list[str],
    *,
    manifests_only: bool = False,
    skip_nested_runs: bool = False,
) -> Iterable[Path]:
    try:
        _safe_path(root)
        if not root.is_dir():
            return
        for base, dirs, names in os.walk(
            root, followlinks=False, onerror=lambda _: problems.append("unreadable_directory")
        ):
            parent = Path(base)
            if manifests_only and MANIFEST_NAME in names:
                dirs[:] = []
                names = [MANIFEST_NAME]
            for name in list(dirs):
                if manifests_only and name in {".git", ".claude", "venv", ".venv", "node_modules"}:
                    dirs.remove(name)
                    continue
                try:
                    if _linked(parent / name):
                        dirs.remove(name)
                        problems.append("linked_directory")
                    elif skip_nested_runs and (parent / name / MANIFEST_NAME).exists():
                        dirs.remove(name)
                except OSError:
                    dirs.remove(name)
                    problems.append("unreadable_directory")
            for name in names:
                if manifests_only and name != MANIFEST_NAME:
                    continue
                path = parent / name
                try:
                    if _linked(path):
                        problems.append("linked_file")
                    elif path.is_file():
                        yield path
                except OSError:
                    problems.append("unreadable_file")
    except (OSError, ValueError):
        problems.append("unavailable_or_linked_root")


def discover_temp_runs(
    repo_root: Path,
    code_root: Path | None = None,
    now: datetime | None = None,
) -> TempRunDiscovery:
    """Read manifests in bounded roots; unknown state never creates candidates."""
    observed = _now(now)
    found: set[Path] = set()
    external_unknown: set[Path] = set()
    reports: list[TempRunReport] = []
    artifacts: list[Artifact] = []
    for boundary, prefixed in _search_roots(repo_root, code_root):
        problems: list[str] = []
        roots = [boundary]
        if prefixed:
            roots = []
            try:
                _safe_path(boundary)
                if boundary.is_dir():
                    roots = [
                        child
                        for child in boundary.iterdir()
                        if child.name.startswith("earnings-summary-")
                    ]
            except (OSError, ValueError):
                problems.append("unavailable_or_linked_boundary")
        for search in roots:
            if prefixed and search.is_dir() and not (search / MANIFEST_NAME).exists():
                external_unknown.add(search)
            for path in _walk(search, problems, manifests_only=True):
                if path.name == MANIFEST_NAME:
                    found.add(path.parent)
        if problems:
            reports.append(
                TempRunReport(
                    root=boundary,
                    scope="boundary",
                    status="unknown",
                    problems=sorted(set(problems)),
                )
            )
    for root in sorted(found | external_unknown):
        report = TempRunReport(root=root, status="unknown")
        reports.append(report)
        manifest_digest = ""
        try:
            _owned_root(root, repo_root, code_root)
            _safe_path(root / MANIFEST_NAME)
            manifest_digest = hashlib.sha256((root / MANIFEST_NAME).read_bytes()).hexdigest()
            manifest = _read_manifest(root)
            if hashlib.sha256((root / MANIFEST_NAME).read_bytes()).hexdigest() != manifest_digest:
                raise ValueError("ownership manifest changed during discovery")
        except (OSError, ValueError):
            report.problems.append(
                "invalid_manifest" if (root / MANIFEST_NAME).exists() else "missing_manifest"
            )
            manifest = None
        for path in _walk(root, report.problems, skip_nested_runs=True):
            if path.name != MANIFEST_NAME:
                try:
                    size = path.stat().st_size
                    report.files += 1
                    report.bytes += size
                except OSError:
                    report.problems.append("unreadable_file")
        if manifest is not None:
            report.status = manifest.status
            report.purpose = manifest.purpose
            report.byte_budget = manifest.byte_budget
            report.review_after = manifest.review_after
            report.review_due = (
                manifest.review_after is not None and manifest.review_after <= observed
            )
            report.budget_exceeded = (
                manifest.byte_budget is not None and report.bytes > manifest.byte_budget
            )
            proofs = manifest.retirement_proofs
            closure_files = manifest.files
            closure_at = manifest.completed_at
            closed = manifest.status == "completed"
            if manifest.status == "failed" and (root / RESOLUTION_NAME).exists():
                try:
                    _safe_path(root / RESOLUTION_NAME)
                    resolution_raw = (root / RESOLUTION_NAME).read_bytes()
                    resolution = TempRunResolution.model_validate_json(resolution_raw)
                    _safe_path(resolution.successful_manifest.path)
                    success_root = _owned_root(
                        resolution.successful_manifest.path.parent, repo_root, code_root
                    )
                    success = _read_manifest(success_root)
                    if (
                        resolution.run_root != root
                        or resolution.failed_run_id != manifest.run_id
                        or resolution.failed_manifest_sha256 != manifest_digest
                        or manifest.pins
                        or success.status != "completed"
                        or success.pins
                        or _blocking_holds(success.held_paths)
                        or success.purpose != manifest.purpose
                        or manifest.purpose == "legacy"
                        or resolution.successful_manifest.path != success_root / MANIFEST_NAME
                        or resolution.successful_manifest.sha256
                        != hashlib.sha256((success_root / MANIFEST_NAME).read_bytes()).hexdigest()
                        or success.completed_at is None
                        or manifest.completed_at is None
                        or success.completed_at < manifest.completed_at
                        or _now(resolution.resolved_at) < success.completed_at
                    ):
                        raise ValueError("invalid_resolution")
                    closure_files, closure_at, closed = (
                        resolution.files,
                        resolution.resolved_at,
                        True,
                    )
                    proofs = (
                        *proofs,
                        resolution.successful_manifest,
                        RetirementProof(
                            path=root / RESOLUTION_NAME,
                            sha256=hashlib.sha256(resolution_raw).hexdigest(),
                        ),
                    )
                    report.resolution_status = "resolved"
                except (OSError, ValueError):
                    report.problems.append("invalid_resolution")
            report.held_paths = manifest.held_paths
            if manifest.held_paths:
                report.problems.append("partial_completion")
            if closed and closure_at is not None:
                for item in closure_files:
                    path = root / item.relative_path
                    artifacts.append(
                        Artifact(
                            path=path,
                            allowed_root=root,
                            family=f"temp-run:{manifest.run_id}",
                            created_at=closure_at,
                            sha256=item.sha256,
                            size=item.size,
                            kind="disposable_test",
                            status="completed",
                            verified=True,
                            disposable_class=manifest.disposable_class,
                            purpose=manifest.purpose,
                            byte_budget=manifest.byte_budget,
                            review_after=manifest.review_after,
                            file_identity=(item.device, item.inode, item.mtime_ns),
                            pins=manifest.pins,
                            lifecycle_manifest=root / MANIFEST_NAME,
                            lifecycle_manifest_sha256=manifest_digest,
                            retirement_proofs=proofs,
                        )
                    )
                    try:
                        _safe_path(path)
                        metadata = path.stat()
                        if (
                            metadata.st_dev,
                            metadata.st_ino,
                            metadata.st_size,
                            metadata.st_mtime_ns,
                        ) != (item.device, item.inode, item.size, item.mtime_ns):
                            report.problems.append("registered_file_changed")
                        else:
                            report.registered_files += 1
                            report.registered_bytes += item.size
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError):
                        report.problems.append("registered_file_unavailable")
        report.held_files = max(0, report.files - report.registered_files)
        report.held_bytes = max(0, report.bytes - report.registered_bytes)
        if manifest is not None and manifest.pins:
            report.held_files, report.held_bytes = report.files, report.bytes
        report.problems = sorted(set(report.problems))
    return TempRunDiscovery(
        catalog=ArtifactCatalog(schema_version=1, artifacts=artifacts), reports=reports
    )
