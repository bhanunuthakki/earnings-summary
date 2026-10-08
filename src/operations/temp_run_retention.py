"""Record explicit disposable run ownership without inferring legacy ownership."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.operations.artifact_retention import Artifact, ArtifactCatalog

MANIFEST_NAME = ".earnings-temp-run.json"
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
    pins: list[str] = Field(default_factory=list)
    files: list[TempRunFile] = Field(default_factory=lambda: list[TempRunFile]())
    held_paths: list[TempRunHold] = Field(default_factory=lambda: list[TempRunHold]())

    @field_validator("started_at", "completed_at")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("run timestamps must include a timezone")
        return value.astimezone(UTC) if value is not None else None


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


class TempRunDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog: ArtifactCatalog
    reports: list[TempRunReport]


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
) -> TempRunManifest:
    """Claim only a new or empty exact caller-owned temporary directory."""
    root = _owned_root(run_root, repo_root, code_root)
    stamp = _now(now)
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
    try:
        if (root / MANIFEST_NAME).exists():
            raise ValueError("run root already has an ownership manifest")
        manifest = TempRunManifest(
            run_id=str(uuid4()), run_root=root, started_at=stamp, status="active"
        )
        _write_manifest(root, manifest)
    finally:
        claim.unlink()
    return manifest


def _seal_file(
    root: Path, declared: Path, names: set[str], live: Path | None
) -> TempRunFile | None:
    path = _absolute(declared if declared.is_absolute() else root / declared)
    if root not in path.parents:
        raise _FileHoldError("scope_escape")
    relative = path.relative_to(root)
    name = os.path.normcase(str(relative))
    if relative.name == MANIFEST_NAME:
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
    if any(part.casefold() in _SOURCE_PARTS for part in relative.parts):
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
        configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
        live = _absolute(Path(configured)) if configured else None
        for declared in disposable_paths:
            try:
                item = _seal_file(root, declared, names, live)
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
    return sealed


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
    _now(now)
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
            report.held_paths = manifest.held_paths
            if manifest.held_paths:
                report.problems.append("partial_completion")
            if manifest.status == "completed" and manifest.completed_at is not None:
                for item in manifest.files:
                    path = root / item.relative_path
                    artifacts.append(
                        Artifact(
                            path=path,
                            allowed_root=root,
                            family=f"temp-run:{manifest.run_id}",
                            created_at=manifest.completed_at,
                            sha256=item.sha256,
                            size=item.size,
                            kind="disposable_test",
                            status="completed",
                            verified=True,
                            pins=manifest.pins,
                            lifecycle_manifest=root / MANIFEST_NAME,
                            lifecycle_manifest_sha256=manifest_digest,
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
