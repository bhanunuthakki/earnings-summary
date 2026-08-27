"""Allowlist-only weekly cleanup for disposable local filesystem artifacts.

The command is deliberately dry-run by default.  It has no database or output
archive behavior: those lifecycle decisions have separate, provenance-aware
tools.  Only the policy roots named below are ever traversed.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POLICY_VERSION = "weekly-cleanup-v2"
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

    policy_version: Literal["weekly-cleanup-v2"]
    idempotency_key: str = Field(min_length=1)
    mode: Literal["dry_run", "apply"]
    files_scanned: int = Field(ge=0)
    would_delete: int = Field(ge=0)
    deleted: int = Field(ge=0)
    bytes: int = Field(ge=0)
    skipped_invalid: int = Field(ge=0)
    policies: dict[str, PolicySummary]


@dataclass(frozen=True)
class Candidate:
    path: Path
    size: int


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

    def summary(self) -> PolicySummary:
        return PolicySummary.model_validate(self.__dict__)


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


def _iter_regular_files(root: Path, counts: _Counts) -> Iterator[Path]:
    """Yield real files below one known-safe root without traversing links."""
    if not root.is_dir():
        return
    if _is_reparse_or_symlink(root):
        counts.skipped_unsafe += 1
        return
    for base, dirs, names in os.walk(root, topdown=True, followlinks=False):
        base_path = Path(base)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            child = base_path / name
            if _is_reparse_or_symlink(child):
                counts.skipped_unsafe += 1
                _event("cleanup_skipped", reason="unsafe_path", path=str(child))
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(names):
            path = base_path / name
            if _is_protected_name(path):
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
    return Candidate(path=path, size=file_stat.st_size)


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
    for path in _iter_regular_files(root, counts):
        if path.suffix.lower() != ".json":
            continue
        if _is_recovery_material(path) or _checkpoint_is_active(path, root.parent, counts):
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
            candidates.append(Candidate(path=path, size=path.stat().st_size))
        except OSError:
            counts.skipped_error += 1
    return candidates


_MAIN_CODE_ROOTS = ("src", "execution", "tests", "cron", "scripts", "alembic")
_MAIN_CACHE_ROOTS = (".pytest_cache", ".ruff_cache")


def _is_main_cache_file(path: Path) -> bool:
    parts = path.parts
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
            if _is_protected_name(path) or not _is_main_cache_file(path):
                continue
            counts.files_scanned += 1
            candidate = _older_than(path, cutoff)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


_OWNED_TMP_ROOTS = frozenset({"cron_logs", "cron_runs", "news_cache", "pdf_pages"})
_RECOVERY_SUFFIXES = frozenset({".db", ".bak", ".gz", ".enc"})
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
        state_path = parent / "state.json"
        if state_path.is_file():
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
            return not (
                isinstance(status, str) and status.strip().lower() in _COMPLETED_CHECKPOINT_STATUSES
            )
        if parent == tmp_root:
            break
        parent = parent.parent
    return False


def _is_recovery_material(path: Path) -> bool:
    lower_name = path.name.lower()
    return (
        path.suffix.lower() in _RECOVERY_SUFFIXES
        or lower_name.endswith((".db-wal", ".db-shm"))
        or any(marker in lower_name for marker in _RECOVERY_NAME_MARKERS)
    )


def _collect_tmp_unclassified(tmp_root: Path, cutoff: datetime, counts: _Counts) -> list[Candidate]:
    """Collect generic disposable files while preserving owned and recovery state."""
    candidates: list[Candidate] = []
    for path in _iter_regular_files(tmp_root, counts):
        relative = path.relative_to(tmp_root)
        if relative.parts and relative.parts[0] in _OWNED_TMP_ROOTS:
            continue
        if path.name.startswith("temp_audio_"):
            continue
        if _is_recovery_material(path) or _checkpoint_is_active(path, tmp_root, counts):
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
    for path in _iter_regular_files(root, counts):
        if path.name.startswith("temp_audio_"):
            continue
        if _is_recovery_material(path) or _checkpoint_is_active(path, tmp_root, counts):
            continue
        counts.files_scanned += 1
        candidate = _older_than(path, cutoff)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


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


def _delete_empty_dirs(root: Path) -> None:
    """Remove empty directories only inside a policy root, never linked dirs."""
    if not root.is_dir() or _is_reparse_or_symlink(root):
        return
    for base, dirs, _ in os.walk(root, topdown=False, followlinks=False):
        for name in sorted(dirs):
            path = Path(base) / name
            if _is_reparse_or_symlink(path):
                continue
            with suppress(OSError):
                path.rmdir()
    with suppress(OSError):
        root.rmdir()


def _delete_main_cache_dirs(repo_root: Path) -> None:
    """Prune empty cache directories beneath the same fixed main-checkout roots."""
    cache_dirs: list[Path] = []
    for root in _main_cache_search_roots(repo_root):
        if _is_reparse_or_symlink(root):
            continue
        for base, dirs, _ in os.walk(root, topdown=True, followlinks=False):
            base_path = Path(base)
            dirs[:] = [name for name in dirs if not _is_reparse_or_symlink(base_path / name)]
            if base_path.name in {"__pycache__", ".pytest_cache", ".ruff_cache"}:
                cache_dirs.append(base_path)
    for cache_dir in sorted(cache_dirs, key=lambda path: len(path.parts), reverse=True):
        _delete_empty_dirs(cache_dir)


def _apply_candidates(
    policy: str, candidates: list[Candidate], counts: _Counts, apply: bool
) -> None:
    for candidate in candidates:
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


def run(argv: list[str] | None = None) -> CleanupSummary:
    parser = argparse.ArgumentParser(description="Dry-run-first allowlist-only weekly cleanup.")
    parser.add_argument(
        "--apply", action="store_true", help="Delete eligible files (default only reports)."
    )
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Repository root to inspect."
    )
    parser.add_argument("--now", help="ISO-8601 timestamp, injectable for deterministic tests.")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    if not repo_root.is_dir():
        raise ValueError(f"--repo-root must be an existing directory: {repo_root}")
    now = _parse_now(args.now)
    mode: Literal["dry_run", "apply"] = "apply" if args.apply else "dry_run"

    policies = {
        "cron_logs_30d": _Counts(),
        "cron_runs_30d": _Counts(),
        "news_cache_7d": _Counts(),
        "pdf_pages_30d": _Counts(),
        "main_python_caches_7d": _Counts(),
        "tmp_unclassified_30d": _Counts(),
        "temp_audio_qa_guard": _Counts(),
    }
    targets: list[tuple[str, Path, Collector, datetime]] = [
        (
            "cron_logs_30d",
            repo_root / ".tmp" / "cron_logs",
            _collect_tmp_owned_by_age,
            now - timedelta(days=30),
        ),
        (
            "cron_runs_30d",
            repo_root / ".tmp" / "cron_runs",
            _collect_tmp_owned_by_age,
            now - timedelta(days=30),
        ),
        (
            "news_cache_7d",
            repo_root / ".tmp" / "news_cache",
            _collect_news_cache,
            now - timedelta(days=7),
        ),
        (
            "pdf_pages_30d",
            repo_root / ".tmp" / "pdf_pages",
            _collect_tmp_owned_by_age,
            now - timedelta(days=30),
        ),
        (
            "main_python_caches_7d",
            repo_root,
            _collect_main_caches,
            now - timedelta(days=7),
        ),
        (
            "tmp_unclassified_30d",
            repo_root / ".tmp",
            _collect_tmp_unclassified,
            now - timedelta(days=30),
        ),
    ]
    roots_to_prune: list[Path] = []
    for name, root, collector, cutoff in targets:
        _apply_candidates(name, collector(root, cutoff, policies[name]), policies[name], args.apply)
        if root not in (repo_root, repo_root / ".tmp"):
            roots_to_prune.append(root)
    _collect_temp_audio(repo_root / ".tmp", policies["temp_audio_qa_guard"])
    if args.apply:
        for root in roots_to_prune:
            _delete_empty_dirs(root)
        _delete_main_cache_dirs(repo_root)

    summaries = {name: counts.summary() for name, counts in policies.items()}
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
