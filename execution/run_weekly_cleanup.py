"""Allowlist-only weekly cleanup for disposable local filesystem artifacts.

The command is deliberately dry-run by default.  It has no database or output
archive behavior: those lifecycle decisions have separate, provenance-aware
tools.  Only the policy roots named below are ever traversed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath
from typing import Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field

from src.operations.artifact_retention import (
    CATALOG_RELATIVE_PATH,
    Artifact,
    ArtifactCatalog,
    load_catalog,
    merge_operator_artifact,
    retained_scope_roots,
    run_retention,
)
from src.operations.operational_backup_retention import (
    OperationalBackupReport,
    discover_operational_backups,
)
from src.operations.temp_coverage import TempCoverage, inventory_temp_coverage
from src.operations.temp_run_retention import discover_temp_runs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POLICY_VERSION = "weekly-cleanup-v5"
DEFAULT_DISPOSABLE_RETENTION_DAYS = 30
DEFAULT_CACHE_RETENTION_DAYS = 7
Collector: TypeAlias = Callable[[Path, datetime, "_Counts"], list["Candidate"]]


class PolicySummary(BaseModel):
    """Counts for one fixed cleanup policy."""

    model_config = ConfigDict(extra="forbid")

    files_scanned: int = Field(ge=0, default=0)
    would_delete: int = Field(ge=0, default=0)
    deleted: int = Field(ge=0, default=0)
    bytes: int = Field(ge=0, default=0)
    skipped_invalid: int = Field(ge=0, default=0)
    skipped_unsafe: int = Field(ge=0, default=0)
    skipped_qa_unverified: int = Field(ge=0, default=0)
    skipped_error: int = Field(ge=0, default=0)


class CleanupSummary(BaseModel):
    """Schema-validated stdout contract for a cleanup run."""

    model_config = ConfigDict(extra="forbid")

    policy_version: Literal["weekly-cleanup-v5"]
    idempotency_key: str = Field(min_length=1)
    mode: Literal["dry_run", "apply"]
    files_scanned: int = Field(ge=0)
    would_delete: int = Field(ge=0)
    deleted: int = Field(ge=0)
    bytes: int = Field(ge=0)
    skipped_invalid: int = Field(ge=0)
    policies: dict[str, PolicySummary]
    coverage: TempCoverage = Field(default_factory=TempCoverage)
    operational_backups: list[OperationalBackupReport] = Field(
        default_factory=lambda: list[OperationalBackupReport]()
    )


@dataclass(frozen=True)
class Candidate:
    path: Path
    size: int
    inode: int
    device: int
    mtime_ns: int


@dataclass
class _Counts:
    files_scanned: int = 0
    would_delete: int = 0
    deleted: int = 0
    bytes: int = 0
    skipped_invalid: int = 0
    skipped_unsafe: int = 0
    skipped_qa_unverified: int = 0
    skipped_error: int = 0
    protected_roots: tuple[Path, ...] = ()
    _scope_source: tuple[Path, ...] | None = field(default=None, init=False, repr=False)
    _scope_set: frozenset[Path] = field(default=frozenset[Path](), init=False, repr=False)

    def protects(self, path: Path) -> bool:
        # Catalog refresh replaces the tuple. Rebuild only its containment index;
        # file, checkpoint and catalog validation still run before each unlink.
        if self._scope_source is not self.protected_roots:
            self._scope_set = frozenset(self.protected_roots)
            self._scope_source = self.protected_roots
        return _within_protected_scope(path, self._scope_set)

    def summary(self) -> PolicySummary:
        return PolicySummary.model_validate(
            {
                key: value
                for key, value in self.__dict__.items()
                if key not in {"protected_roots", "_scope_source", "_scope_set"}
            }
        )


def _within_protected_scope(path: PurePath, scopes: frozenset[PurePath]) -> bool:
    return path in scopes or any(parent in scopes for parent in path.parents)


def _event(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr)


def _is_reparse_or_symlink(path: Path) -> bool:
    """Do not follow or delete link-like filesystem objects, including Windows junctions."""
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(attributes & reparse_flag)
    except OSError:
        return True


def _is_protected_name(path: Path) -> bool:
    """Global denylist inside otherwise allowed roots for live locks."""
    return "job_locks" in path.parts


_PROTECTED_TMP_DIRECTORIES = frozenset(
    {
        "data",
        "output",
        "transcripts",
        "ir_documents",
        "src",
        "execution",
        "tests",
        "scripts",
        "cron",
        "alembic",
        "migrations",
        "directives",
        "secrets",
        ".secrets",
        "credentials",
        "tokens",
        "keys",
        "certificates",
        "certs",
        ".ssh",
    }
)
_PROTECTED_TMP_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".cmd",
        ".bat",
        ".c",
        ".h",
        ".cpp",
        ".rs",
        ".go",
        ".java",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".pem",
        ".key",
        ".crt",
        ".cer",
        ".p12",
        ".pfx",
        ".jks",
        ".keystore",
        ".der",
        ".csr",
        ".sql",
        ".dll",
        ".reg",
        ".patch",
        ".diff",
        ".csv",
        ".tsv",
        ".b64",
        ".html",
        ".htm",
        ".jsonl",
        ".css",
        ".md",
    }
)
_SENSITIVE_TMP_FILENAME = re.compile(
    r"(?:^|[._-])(?:credentials?|tokens?|secrets?|passwords?|api[_-]?key|private[_-]?key|client[_-]?secret)(?:$|[._-])",
    re.IGNORECASE,
)


def _is_protected_tmp_material(path: Path, tmp_root: Path | None) -> bool:
    """Apply source and credential exclusions only within the named temporary root."""
    if tmp_root is None:
        return False
    try:
        parts = path.relative_to(tmp_root).parts
    except ValueError:
        return False
    name = path.name.lower()
    return (
        any(part.lower() in _PROTECTED_TMP_DIRECTORIES for part in parts)
        or any(suffix.lower() in _PROTECTED_TMP_SUFFIXES for suffix in path.suffixes)
        or name == ".env"
        or name.startswith(".env.")
        or name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
        or _SENSITIVE_TMP_FILENAME.search(name) is not None
    )


def _within_tmp_source_checkout(directory: Path, tmp_root: Path | None) -> bool:
    """Hold temporary checkout ancestors without following their Git metadata."""
    if tmp_root is None:
        return False
    for parent in (directory, *directory.parents):
        if parent != tmp_root and tmp_root not in parent.parents:
            break
        try:
            (parent / ".git").lstat()
        except FileNotFoundError:
            continue
        except OSError:
            # Unknown ownership is not deletion permission.
            return True
        return True
    return False


def _iter_regular_files(
    root: Path, counts: _Counts, tmp_root: Path | None = None
) -> Iterator[Path]:
    """Yield real files below one known-safe root without traversing links."""
    if not root.is_dir():
        return
    if _is_reparse_or_symlink(root):
        counts.skipped_unsafe += 1
        return
    if (
        _is_protected_tmp_material(root, tmp_root)
        or counts.protects(root)
        or _within_tmp_source_checkout(root, tmp_root)
    ):
        return
    for base, dirs, names in os.walk(root, topdown=True, followlinks=False):
        base_path = Path(base)
        # Hold the whole checkout. A worktree's .git file is ownership metadata;
        # its target is never opened or followed by cleanup.
        if tmp_root is not None and any(name.casefold() == ".git" for name in (*dirs, *names)):
            dirs[:] = []
            continue
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            child = base_path / name
            if (
                _is_protected_tmp_material(child, tmp_root)
                or name in {".git", ".claude", "venv", ".venv", "node_modules", "job_locks"}
                or counts.protects(child)
            ):
                continue
            if _is_reparse_or_symlink(child):
                counts.skipped_unsafe += 1
                _event("cleanup_skipped", reason="unsafe_path", path=str(child))
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(names):
            path = base_path / name
            if _is_protected_name(path) or _is_protected_tmp_material(path, tmp_root):
                continue
            if _is_reparse_or_symlink(path):
                counts.skipped_unsafe += 1
                _event("cleanup_skipped", reason="unsafe_path", path=str(path))
                continue
            try:
                if path.is_file():
                    yield path
            except OSError:
                counts.skipped_error += 1
                _event("cleanup_skipped", reason="stat_error", path=str(path))


def _older_than(path: Path, cutoff: datetime) -> Candidate | None:
    try:
        file_stat = path.stat()
    except OSError:
        return None
    modified = datetime.fromtimestamp(file_stat.st_mtime, tz=UTC)
    if modified >= cutoff:
        return None
    return Candidate(
        path=path,
        size=file_stat.st_size,
        inode=file_stat.st_ino,
        device=file_stat.st_dev,
        mtime_ns=file_stat.st_mtime_ns,
    )


def _cached_at(path: Path) -> datetime | None:
    """Return a payload timestamp, never substituting the file mtime."""
    try:
        payload_raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload_raw, dict):
        return None
    payload = cast(dict[str, object], payload_raw)
    cached_at_raw: object = payload.get("cached_at")
    if not isinstance(cached_at_raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(cached_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _collect_by_age(root: Path, cutoff: datetime, counts: _Counts) -> list[Candidate]:
    candidates: list[Candidate] = []
    for path in _iter_regular_files(root, counts):
        counts.files_scanned += 1
        candidate = _older_than(path, cutoff)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _collect_news_cache(root: Path, cutoff: datetime, counts: _Counts) -> list[Candidate]:
    candidates: list[Candidate] = []
    for path in _iter_regular_files(root, counts, root.parent):
        if path.suffix.lower() != ".json":
            continue
        if _is_recovery_material(path, root.parent) or _checkpoint_is_active(
            path, root.parent, counts
        ):
            continue
        counts.files_scanned += 1
        cached_at = _cached_at(path)
        if cached_at is None:
            counts.skipped_invalid += 1
            _event("cleanup_skipped", reason="invalid_cached_at", path=str(path))
            continue
        if cached_at >= cutoff:
            continue
        try:
            file_stat = path.stat()
            candidates.append(
                Candidate(
                    path=path,
                    size=file_stat.st_size,
                    inode=file_stat.st_ino,
                    device=file_stat.st_dev,
                    mtime_ns=file_stat.st_mtime_ns,
                )
            )
        except OSError:
            counts.skipped_error += 1
    return candidates


_MAIN_CODE_ROOTS = ("src", "execution", "tests", "cron", "scripts", "alembic")
_MAIN_CACHE_ROOTS = (".pytest_cache", ".ruff_cache")


def _is_main_cache_file(path: Path, repo_root: Path) -> bool:
    """Match cache components relative to the checkout, never its ancestors."""
    try:
        parts = path.relative_to(repo_root).parts
    except ValueError:
        return False
    return path.suffix.lower() == ".pyc" or any(
        part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in parts[:-1]
    )


def _main_cache_search_roots(repo_root: Path) -> list[Path]:
    """Fixed main-checkout roots; virtualenvs and nested worktrees are out of scope."""
    names = (*_MAIN_CODE_ROOTS, *_MAIN_CACHE_ROOTS)
    return [repo_root / name for name in names if (repo_root / name).is_dir()]


def _collect_main_caches(repo_root: Path, cutoff: datetime, counts: _Counts) -> list[Candidate]:
    """Scan cache artifacts in fixed source roots, never arbitrary repo subtrees."""
    candidates: list[Candidate] = []
    for root in _main_cache_search_roots(repo_root):
        for path in _iter_regular_files(root, counts):
            if _is_protected_name(path) or not _is_main_cache_file(path, repo_root):
                continue
            counts.files_scanned += 1
            candidate = _older_than(path, cutoff)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


_OWNED_TMP_ROOTS = frozenset({"cron_logs", "cron_runs", "news_cache", "pdf_pages"})
_RECOVERY_SUFFIXES = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".bak",
        ".gz",
        ".enc",
        ".tar",
        ".zip",
        ".zst",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
    }
)
_RECOVERY_NAME_MARKERS = (
    "backup",
    "snapshot",
    "recovery",
    "restore",
    "precutover",
    "pre_gc",
    "rollback",
    "lease",
    "lock",
)


_COMPLETED_CHECKPOINT_STATUSES = frozenset(
    {"complete", "completed", "done", "success", "succeeded"}
)


def _checkpoint_is_active(path: Path, tmp_root: Path, counts: _Counts) -> bool:
    """Treat checkpoint state as active unless it explicitly says it completed.

    Current resumable pipelines use heterogeneous state schemas and remove
    ``state.json`` after success. Therefore absence of a recognized completed
    status is intentionally fail-closed. An explicitly completed checkpoint is
    ordinary disposable `.tmp` material and receives the policy's age window.
    """
    parent = path.parent
    while parent == tmp_root or tmp_root in parent.parents:
        # A lifecycle receipt owns the entire run, including undeclared siblings.
        # Its entries can retire only through digest-bound explicit retention.
        manifest = parent / ".earnings-temp-run.json"
        if manifest.exists() or manifest.is_symlink():
            return True
        state_path = parent / "state.json"
        if state_path.exists() or state_path.is_symlink():
            if _is_reparse_or_symlink(state_path):
                counts.skipped_unsafe += 1
                return True
            try:
                payload_raw: object = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                counts.skipped_invalid += 1
                _event("cleanup_skipped", reason="invalid_checkpoint_state", path=str(state_path))
                return True
            if not isinstance(payload_raw, dict):
                counts.skipped_invalid += 1
                _event("cleanup_skipped", reason="invalid_checkpoint_state", path=str(state_path))
                return True
            payload = cast("dict[str, object]", payload_raw)
            status = payload.get("status")
            if not (
                isinstance(status, str) and status.strip().lower() in _COMPLETED_CHECKPOINT_STATUSES
            ):
                return True
        if parent == tmp_root:
            break
        parent = parent.parent
    return False


def _is_recovery_material(path: Path, root: Path | None = None) -> bool:
    lower_name = path.name.lower()
    relative = path.relative_to(root).parts if root is not None else (path.name,)
    return (
        any(suffix.lower() in _RECOVERY_SUFFIXES for suffix in path.suffixes)
        or lower_name.endswith((".db-wal", ".db-shm"))
        or any(
            marker in component.lower()
            for component in relative
            for marker in _RECOVERY_NAME_MARKERS
        )
    )


def _collect_tmp_unclassified(tmp_root: Path, cutoff: datetime, counts: _Counts) -> list[Candidate]:
    """Collect generic disposable files while preserving owned and recovery state."""
    candidates: list[Candidate] = []
    recovery_trees = {
        os.path.normcase(path.relative_to(tmp_root).parts[0])
        # Recovery discovery also sees protected data/source children. This is
        # read-only: excluding them here would hide a DB and expose its siblings.
        for path in _iter_regular_files(tmp_root, counts)
        if any(suffix.lower() in _RECOVERY_SUFFIXES for suffix in path.suffixes)
        and len(path.relative_to(tmp_root).parts) > 1
        and path.relative_to(tmp_root).parts[0] not in _OWNED_TMP_ROOTS
    }
    for path in _iter_regular_files(tmp_root, counts, tmp_root):
        relative = path.relative_to(tmp_root)
        if relative.parts and os.path.normcase(relative.parts[0]) in recovery_trees:
            continue
        if relative.parts and relative.parts[0] in _OWNED_TMP_ROOTS:
            continue
        if path.name.startswith("temp_audio_"):
            continue
        if _is_recovery_material(path, tmp_root) or _checkpoint_is_active(path, tmp_root, counts):
            continue
        counts.files_scanned += 1
        candidate = _older_than(path, cutoff)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _collect_tmp_owned_by_age(root: Path, cutoff: datetime, counts: _Counts) -> list[Candidate]:
    """Apply age retention to an owned `.tmp` root without breaking checkpoints."""
    candidates: list[Candidate] = []
    tmp_root = root.parent
    paths = list(_iter_regular_files(root, counts, tmp_root))
    latest_logs: dict[str, tuple[datetime, Path]] = {}
    if root.name == "cron_logs":
        for path in paths:
            identity = _timestamped_log_identity(path)
            if identity is not None:
                family, timestamp = identity
                previous = latest_logs.get(family)
                if previous is None or (timestamp, str(path)) > (previous[0], str(previous[1])):
                    latest_logs[family] = (timestamp, path)
    for path in paths:
        if path.name.startswith("temp_audio_"):
            continue
        recovery_path = (
            path.parent if root.name == "cron_logs" and path.suffix.lower() == ".log" else path
        )
        if _is_recovery_material(recovery_path, tmp_root) or _checkpoint_is_active(
            path, tmp_root, counts
        ):
            continue
        if root.name == "cron_logs":
            identity = _timestamped_log_identity(path)
            latest = latest_logs.get(identity[0]) if identity is not None else None
            if latest is not None and path == latest[1]:
                continue
            if _log_has_failure(path):
                continue
        counts.files_scanned += 1
        candidate = _older_than(path, cutoff)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


_TIMESTAMPED_LOG = re.compile(
    r"^(?P<family>.+?)[_-](?P<stamp>\d{8}(?:T\d{6}Z|_\d{6}))\.log$", re.IGNORECASE
)


def _timestamped_log_identity(path: Path) -> tuple[str, datetime] | None:
    match = _TIMESTAMPED_LOG.fullmatch(path.name)
    if match is None:
        return None
    stamp = match.group("stamp")
    try:
        timestamp = datetime.strptime(
            stamp, "%Y%m%dT%H%M%SZ" if "T" in stamp.upper() else "%Y%m%d_%H%M%S"
        )
    except ValueError:
        return None
    return match.group("family").casefold(), timestamp.replace(tzinfo=UTC)


_LOG_RESULT_FIELD = re.compile(
    r"""(?<!\w)["']?(?P<field>status|exit_code|skipped_error)["']?\s*[:=]\s*"""
    r"""(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^,}\s]*)""",
    re.IGNORECASE,
)
_SUCCESS_LOG_STATUSES = frozenset({"ok", "success", "succeeded", "complete", "completed", "done"})


def _log_has_failure(path: Path) -> bool:
    """Keep complete failure evidence, including invalid operational result fields."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                lowered = line.casefold()
                if any(
                    marker in lowered
                    for marker in ("traceback", "error:", "unlink_error", "cleanup_error", "failed")
                ):
                    return True
                matches = list(_LOG_RESULT_FIELD.finditer(line))
                if matches and line.lstrip().startswith("{"):
                    try:
                        json.loads(line)
                    except ValueError:
                        return True
                for match in matches:
                    value = match.group("value")
                    if match.group("field").casefold() == "status":
                        if value.strip("\"'").casefold() not in _SUCCESS_LOG_STATUSES:
                            return True
                    elif value != "0":
                        return True
        return False
    except OSError:
        return True


def _collect_temp_audio(root: Path, counts: _Counts) -> None:
    """Explicitly preserve audio unless qa_transcripts can prove matching QA is OK.

    This filesystem-only entrypoint intentionally has no database connection;
    qa_transcripts.py remains the narrow, DB-aware owner of that deletion.
    """
    if not root.is_dir() or _is_reparse_or_symlink(root):
        return
    for path in sorted(root.glob("temp_audio_*")):
        if _is_reparse_or_symlink(path):
            counts.skipped_unsafe += 1
            continue
        try:
            if path.is_file():
                counts.files_scanned += 1
                counts.skipped_qa_unverified += 1
                _event("cleanup_skipped", reason="qa_unverified", path=str(path))
        except OSError:
            counts.skipped_error += 1


def _delete_empty_dirs(
    root: Path, protected_roots: tuple[Path, ...] = (), tmp_root: Path | None = None
) -> None:
    """Remove empty directories only inside a policy root, never linked dirs."""
    scopes = frozenset(protected_roots)
    if (
        not root.is_dir()
        or _is_reparse_or_symlink(root)
        or _is_protected_tmp_material(root, tmp_root)
        or _within_protected_scope(root, scopes)
        or _within_tmp_source_checkout(root, tmp_root)
    ):
        return
    directories = [root]
    for base, dirs, names in os.walk(root, topdown=True, followlinks=False):
        base_path = Path(base)
        if tmp_root is not None and any(name.casefold() == ".git" for name in (*dirs, *names)):
            dirs[:] = []
            continue
        dirs[:] = [
            name
            for name in dirs
            if name not in {".git", ".claude", "venv", ".venv", "node_modules", "job_locks"}
            and not _is_protected_tmp_material(base_path / name, tmp_root)
            and not _is_reparse_or_symlink(base_path / name)
            and not _within_protected_scope(base_path / name, scopes)
        ]
        directories.extend(base_path / name for name in dirs)
    for directory in reversed(directories):
        if (
            _is_protected_tmp_material(directory, tmp_root)
            or _within_tmp_source_checkout(directory, tmp_root)
        ) or (
            tmp_root is not None
            and _checkpoint_is_active(directory / ".cleanup-directory", tmp_root, _Counts())
        ):
            continue
        with suppress(OSError):
            directory.rmdir()


def _delete_main_cache_dirs(repo_root: Path, protected_roots: tuple[Path, ...] = ()) -> None:
    """Prune empty cache directories beneath the same fixed main-checkout roots."""
    cache_dirs: list[Path] = []
    for root in _main_cache_search_roots(repo_root):
        if _is_reparse_or_symlink(root):
            continue
        for base, dirs, _ in os.walk(root, topdown=True, followlinks=False):
            base_path = Path(base)
            dirs[:] = [
                name
                for name in dirs
                if name not in {".git", ".claude", "venv", ".venv", "node_modules"}
                and not _is_reparse_or_symlink(base_path / name)
            ]
            if base_path.name in {"__pycache__", ".pytest_cache", ".ruff_cache"}:
                cache_dirs.append(base_path)
    for cache_dir in sorted(cache_dirs, key=lambda path: len(path.parts), reverse=True):
        _delete_empty_dirs(cache_dir, protected_roots)


def _candidate_still_disposable(candidate: Candidate, repo_root: Path, counts: _Counts) -> bool:
    path = candidate.path
    if any(_is_reparse_or_symlink(part) for part in (path, *path.parents)):
        return False
    if counts.protects(path):
        return False
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return False
        if getattr(metadata, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_READONLY", 0x1
        ):
            return False
        if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
            candidate.device,
            candidate.inode,
            candidate.size,
            candidate.mtime_ns,
        ):
            return False
    except OSError:
        return False
    tmp_root = repo_root / ".tmp"
    if tmp_root in path.parents:
        if _is_protected_tmp_material(path, tmp_root) or _within_tmp_source_checkout(
            path.parent, tmp_root
        ):
            return False
        recovery_path = (
            path.parent
            if (tmp_root / "cron_logs") in path.parents and path.suffix.lower() == ".log"
            else path
        )
        if _is_recovery_material(recovery_path, tmp_root) or _checkpoint_is_active(
            path, tmp_root, counts
        ):
            return False
        if (tmp_root / "cron_logs") in path.parents and _log_has_failure(path):
            return False
    return True


def _apply_candidates(
    policy: str,
    candidates: list[Candidate],
    counts: _Counts,
    apply: bool,
    repo_root: Path,
    *,
    catalog_roots: tuple[Path, ...] = (),
) -> None:
    catalog_signatures: dict[Path, tuple[int, int, int, int] | None] = {}
    for candidate in candidates:
        for catalog_root in catalog_roots or (repo_root,):
            catalog_path = catalog_root / CATALOG_RELATIVE_PATH
            try:
                catalog_stat = catalog_path.stat()
                current_signature = (
                    catalog_stat.st_dev,
                    catalog_stat.st_ino,
                    catalog_stat.st_size,
                    catalog_stat.st_mtime_ns,
                )
            except FileNotFoundError:
                current_signature = None
            if current_signature != catalog_signatures.get(catalog_root):
                registered = retained_scope_roots(load_catalog(catalog_root))
                counts.protected_roots = tuple(set(counts.protected_roots) | registered)
                catalog_signatures[catalog_root] = current_signature
        if not _candidate_still_disposable(candidate, repo_root, counts):
            counts.skipped_unsafe += 1
            _event(
                "cleanup_skipped",
                policy=policy,
                reason="candidate_changed_or_protected",
                path=str(candidate.path),
            )
            continue
        if not apply:
            counts.would_delete += 1
            counts.bytes += candidate.size
            _event(
                "cleanup_candidate", policy=policy, path=str(candidate.path), bytes=candidate.size
            )
            continue
        try:
            candidate.path.unlink()
        except OSError as exc:
            counts.skipped_error += 1
            _event(
                "cleanup_skipped",
                policy=policy,
                reason="unlink_error",
                path=str(candidate.path),
                error=str(exc),
            )
            continue
        counts.deleted += 1
        counts.bytes += candidate.size
        _event("cleanup_deleted", policy=policy, path=str(candidate.path), bytes=candidate.size)


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _lexical_alias_target(value: str, parent: Path) -> str:
    """Normalize link metadata without resolving or opening its descendants."""
    for prefix in ("\\\\?\\", "\\??\\"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    if not os.path.isabs(value):
        value = os.path.join(parent, value)
    return os.path.normcase(os.path.abspath(value))


def operator_backup_families(
    discovered: ArtifactCatalog, operators: list[ArtifactCatalog | None]
) -> list[Artifact]:
    """Extend exact operator family declarations without changing their holds.

    A legacy registration of the same verified bytes supplies the family name
    for future producer backups. Conflicting family names require review.
    """
    declared = {
        os.path.normcase(str(item.path)): item
        for catalog in operators
        if catalog is not None
        for item in catalog.artifacts
    }
    aliases: dict[str, set[str]] = {}
    for item in discovered.artifacts:
        operator = declared.get(os.path.normcase(str(item.path)))
        if (
            operator is not None
            and operator.kind == "backup"
            and operator.verified
            and operator.sha256 == item.sha256
            and operator.size == item.size
        ):
            aliases.setdefault(item.family, set()).add(operator.family)
    result: list[Artifact] = []
    reverse_aliases: dict[str, set[str]] = {}
    for producer_family, families in aliases.items():
        for family in families:
            reverse_aliases.setdefault(family, set()).add(producer_family)
    for item in discovered.artifacts:
        families = aliases.get(item.family, set())
        conflicting = len(families) > 1 or any(
            len(reverse_aliases[family]) > 1 for family in families
        )
        if conflicting:
            item = item.model_copy(
                update={"pins": [*item.pins, "ambiguous_operator_backup_family"]}
            )
        elif len(families) == 1:
            item = item.model_copy(update={"family": next(iter(families))})
        result.append(item)
    return result


def catalog_authority_roots(repo_root: Path, code_root: Path) -> tuple[Path, ...]:
    """Use state authority for the exact approved runtime data directory alias."""
    roots = tuple(dict.fromkeys((code_root, repo_root)))
    if code_root == repo_root:
        return roots
    alias = code_root / "data"
    try:
        metadata = alias.lstat()
    except FileNotFoundError:
        return roots
    except OSError as exc:
        raise ValueError("runtime data alias metadata is unavailable") from exc
    linked = stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if not linked:
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("runtime data alias boundary must be a directory")
        return roots
    expected = repo_root / "data"
    try:
        target = _lexical_alias_target(os.readlink(alias), alias.parent)
    except OSError as exc:
        raise ValueError("runtime data alias target is unavailable") from exc
    if target != os.path.normcase(os.path.abspath(expected)):
        raise ValueError("runtime data alias does not target configured state data")
    if any(_is_reparse_or_symlink(part) for part in (expected, *expected.parents)):
        raise ValueError("configured state data alias target must not traverse links")
    if not expected.is_dir():
        raise ValueError("configured state data alias target must exist")
    return (repo_root,)


def external_temp_coverage_roots() -> list[tuple[Path, str]]:
    """Count legacy tests and deployment backups without deletion authority."""
    if os.name != "nt":
        return []
    roots = [(Path("C:/tmp"), "unclassified")]
    deployments = Path("C:/ProgramData/BhanuOperations/deployments")
    try:
        if any(_is_reparse_or_symlink(part) for part in (deployments, *deployments.parents)):
            return [*roots, (deployments, "unclassified")]
        roots.extend((path, "unclassified") for path in deployments.glob("earnings-summary-*"))
    except FileNotFoundError:
        pass
    return roots


def run(argv: list[str] | None = None) -> CleanupSummary:
    parser = argparse.ArgumentParser(description="Dry-run-first allowlist-only weekly cleanup.")
    parser.add_argument(
        "--apply", action="store_true", help="Delete eligible files (default only reports)."
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None, help="Configured product state root to inspect."
    )
    parser.add_argument(
        "--code-root", type=Path, default=None, help="Runtime source root; also inspect its .tmp."
    )
    parser.add_argument(
        "--completed-test-retention-days",
        type=int,
        default=7,
        help="Age window for explicitly verified completed test files.",
    )
    parser.add_argument("--now", help="ISO-8601 timestamp, injectable for deterministic tests.")
    args = parser.parse_args(argv)
    root_arg = args.repo_root
    if root_arg is None:
        configured_root = os.environ.get("EARNINGS_SUMMARY_REPO_ROOT", "").strip()
        if configured_root:
            root_arg = Path(configured_root)
        else:
            from src.operations.paths import configured_product_state_root

            root_arg = configured_product_state_root(PROJECT_ROOT)
    repo_root = root_arg.resolve()
    if not repo_root.is_dir():
        raise ValueError(f"--repo-root must be an existing directory: {repo_root}")
    code_root = (
        args.code_root.resolve()
        if args.code_root is not None
        else repo_root
        if args.repo_root is not None
        else PROJECT_ROOT
    )
    if not code_root.is_dir():
        raise ValueError(f"--code-root must be an existing directory: {code_root}")
    now = _parse_now(args.now)
    mode: Literal["dry_run", "apply"] = "apply" if args.apply else "dry_run"

    catalog_roots = catalog_authority_roots(repo_root, code_root)
    legacy_catalogs = [load_catalog(root) for root in catalog_roots]
    discovery = discover_temp_runs(repo_root, code_root=code_root, now=now)
    backup_discovery = discover_operational_backups(repo_root)
    artifacts = {os.path.normcase(str(item.path)): item for item in discovery.catalog.artifacts}
    operational_artifacts = operator_backup_families(backup_discovery.catalog, legacy_catalogs)
    operational_by_path = {os.path.normcase(str(item.path)): item for item in operational_artifacts}
    for report in backup_discovery.reports:
        declared = operational_by_path.get(os.path.normcase(str(report.path)))
        if declared is not None:
            report.family = declared.family
            report.pins = declared.pins
            if "ambiguous_operator_backup_family" in declared.pins:
                report.status = "unclassified"
                report.reason = "ambiguous_operator_backup_family"
    for item in operational_artifacts:
        artifacts.setdefault(os.path.normcase(str(item.path)), item)
    # Legacy operator registrations have priority over producer registrations.
    # A second manifest must not remove a legacy recovery hold or pin.
    for legacy_catalog in legacy_catalogs:
        for item in legacy_catalog.artifacts if legacy_catalog is not None else []:
            name = os.path.normcase(str(item.path))
            artifacts[name] = merge_operator_artifact(item, artifacts.get(name))
    for report in backup_discovery.reports:
        name = os.path.normcase(str(report.path))
        if report.status not in {"failed", "unclassified"}:
            continue
        held_names = [name] if name in artifacts else []
        if report.path == report.root == repo_root / "data/backups":
            held_names = [
                key
                for key, item in artifacts.items()
                if item.kind == "backup" and report.root in item.path.parents
            ]
        for held_name in held_names:
            item = artifacts[held_name]
            artifacts[held_name] = item.model_copy(
                update={
                    "pins": [*item.pins, "producer_operation_unresolved"],
                    "status": "failed" if report.status == "failed" else "active",
                    "verified": False,
                }
            )
    catalog = ArtifactCatalog(schema_version=1, artifacts=list(artifacts.values()))
    lifecycle_roots = {
        Path(report.root)
        for report in discovery.reports
        if report.scope == "run"
        and (
            (Path(report.root) / ".earnings-temp-run.json").exists()
            or (Path(report.root) / ".earnings-temp-run.json").is_symlink()
        )
    }
    protected_scopes = tuple(retained_scope_roots(catalog) | lifecycle_roots)
    retirement = run_retention(
        repo_root,
        now=now,
        apply=args.apply,
        catalog=catalog,
        test_retention_days=args.completed_test_retention_days,
        catalog_roots=catalog_roots,
    )

    policies = {
        "cron_logs_30d": _Counts(),
        "cron_runs_30d": _Counts(),
        "news_cache_7d": _Counts(),
        "pdf_pages_30d": _Counts(),
        "main_python_caches_7d": _Counts(),
        "tmp_unclassified_30d": _Counts(),
        "temp_audio_qa_guard": _Counts(),
        "registered_artifact_retention": _Counts(
            files_scanned=len(retirement.decisions),
            would_delete=retirement.would_delete,
            deleted=retirement.deleted,
            bytes=retirement.bytes,
            skipped_error=retirement.errors,
        ),
    }
    policy_specs: list[tuple[str, Path, Collector, int]] = [
        (
            "cron_logs_30d",
            Path(".tmp/cron_logs"),
            _collect_tmp_owned_by_age,
            DEFAULT_DISPOSABLE_RETENTION_DAYS,
        ),
        (
            "cron_runs_30d",
            Path(".tmp/cron_runs"),
            _collect_tmp_owned_by_age,
            DEFAULT_DISPOSABLE_RETENTION_DAYS,
        ),
        (
            "news_cache_7d",
            Path(".tmp/news_cache"),
            _collect_news_cache,
            DEFAULT_CACHE_RETENTION_DAYS,
        ),
        (
            "pdf_pages_30d",
            Path(".tmp/pdf_pages"),
            _collect_tmp_owned_by_age,
            DEFAULT_DISPOSABLE_RETENTION_DAYS,
        ),
        ("main_python_caches_7d", Path("."), _collect_main_caches, DEFAULT_CACHE_RETENTION_DAYS),
        (
            "tmp_unclassified_30d",
            Path(".tmp"),
            _collect_tmp_unclassified,
            DEFAULT_DISPOSABLE_RETENTION_DAYS,
        ),
    ]
    targets: list[tuple[str, Path, Path, Collector, datetime]] = []
    owners = [("", repo_root)]
    if code_root != repo_root:
        owners.append(("runtime_", code_root))
    for prefix, owner in owners:
        for name, relative, collector, days in policy_specs:
            policy = prefix + name
            policies.setdefault(policy, _Counts())
            targets.append((policy, owner, owner / relative, collector, now - timedelta(days=days)))
        policies.setdefault(prefix + "temp_audio_qa_guard", _Counts())
    for counts in policies.values():
        counts.protected_roots = protected_scopes
    for decision in retirement.decisions:
        _event("artifact_retention", **decision.model_dump())
    roots_to_prune: list[tuple[Path, Path]] = []
    for name, owner, root, collector, cutoff in targets:
        _apply_candidates(
            name,
            collector(root, cutoff, policies[name]),
            policies[name],
            args.apply,
            owner,
            catalog_roots=catalog_roots,
        )
        if root not in (owner, owner / ".tmp"):
            roots_to_prune.append((root, owner / ".tmp"))
    for prefix, owner in owners:
        _collect_temp_audio(owner / ".tmp", policies[prefix + "temp_audio_qa_guard"])
    if args.apply:
        # A catalog added during collection protects the same roots during pruning.
        prune_scopes = tuple(
            set(protected_scopes).union(*(counts.protected_roots for counts in policies.values()))
        )
        for root, tmp_root in roots_to_prune:
            _delete_empty_dirs(root, prune_scopes, tmp_root)
        for _, owner in owners:
            _delete_main_cache_dirs(owner, prune_scopes)

    summaries = {name: counts.summary() for name, counts in policies.items()}
    coverage = inventory_temp_coverage(
        [
            (repo_root / ".tmp", "unclassified"),
            (code_root / ".tmp", "unclassified"),
            (repo_root / "data" / "operations", "recovery"),
            (repo_root / "data" / "backups", "recovery"),
            *external_temp_coverage_roots(),
            *((scope, "unclassified") for scope in protected_scopes),
            *(
                (Path(report.root), "unclassified")
                for report in discovery.reports
                if report.scope == "run"
            ),
        ],
        now=now,
        decisions=retirement.decisions,
        run_statuses={
            Path(report.root): report.status
            for report in discovery.reports
            if Path(report.root) in lifecycle_roots
        },
    )
    for report in discovery.reports:
        _event("temp_run_retention", **report.model_dump(mode="json"))
        if report.problems:
            coverage.status = "incomplete"
    for report in backup_discovery.reports:
        _event("operational_backup_retention", **report.model_dump(mode="json"))
        if report.status == "unclassified":
            coverage.status = "incomplete"
    _event("cleanup_coverage", **coverage.model_dump(mode="json"))
    return CleanupSummary(
        policy_version=POLICY_VERSION,
        idempotency_key=f"weekly_cleanup:{now.strftime('%G-W%V')}:{POLICY_VERSION}",
        mode=mode,
        files_scanned=sum(item.files_scanned for item in summaries.values()),
        would_delete=sum(item.would_delete for item in summaries.values()),
        deleted=sum(item.deleted for item in summaries.values()),
        bytes=sum(item.bytes for item in summaries.values()),
        skipped_invalid=sum(item.skipped_invalid for item in summaries.values()),
        policies=summaries,
        coverage=coverage,
        operational_backups=backup_discovery.reports,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        summary = run(argv)
    except (OSError, ValueError) as exc:
        _event("cleanup_error", error=str(exc))
        return 1
    print(summary.model_dump_json())
    return 1 if any(policy.skipped_error for policy in summary.policies.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
