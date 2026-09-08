"""Fail-closed loading for roadmap-freeze source artifacts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, TypeVar, cast

from pydantic import BaseModel, ValidationError

from quality.architecture import ArchitectureReceipt, build_architecture_receipt
from quality.duplicates import DuplicateInventory
from quality.duplicates import build_inventory as build_duplicate_inventory
from quality.git_env import clean_local_git_env
from quality.lifecycle_models import LifecycleInventory
from quality.performance_models import PerformanceReceipt
from quality.reachability import ReachabilityGraph
from quality.roadmap_freeze_models import EvidenceKey, OwnerSnapshot
from quality.roadmap_reconciliation import ReconciliationReceipt
from quality.scoring import HARD_GATES, SCORE_BLOCKS
from quality.static_quality import StaticQualityInventory
from quality.test_db_models import TestDbAudit
from quality.test_db_patterns import audit_test_db_patterns

MAX_INPUT_BYTES = 32 * 1024 * 1024
EVIDENCE_KEYS: tuple[EvidenceKey, ...] = (
    "architecture",
    "duplicates",
    "static",
    "test_db",
    "lifecycle",
    "reachability",
    "performance",
    "reconciliation",
)
JsonValue: TypeAlias = bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
NativeEvidence: TypeAlias = (
    ArchitectureReceipt
    | DuplicateInventory
    | StaticQualityInventory
    | TestDbAudit
    | LifecycleInventory
    | ReachabilityGraph
    | PerformanceReceipt
    | ReconciliationReceipt
    | OwnerSnapshot
)


class FreezeInputError(ValueError):
    """An input cannot safely or unambiguously support a freeze."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    links: int


@dataclass(frozen=True, kw_only=True)
class LoadedJson:
    path: Path
    relative_path: str
    raw: bytes
    sha256: str
    identity: FileIdentity | None = None


@dataclass(frozen=True, kw_only=True)
class LoadedInput(LoadedJson):
    value: NativeEvidence
    oracle_status: Literal["VERIFIED", "HOLD"]
    oracle_reasons: tuple[str, ...]
    companions: tuple[LoadedJson, ...] = ()


T = TypeVar("T", bound=BaseModel)


def _run_git(
    root: Path, *args: str, text: bool = True
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=text,
        env=clean_local_git_env(),
    )


def _git(root: Path, *args: str) -> str:
    result = _run_git(root, *args)
    if result.returncode != 0:
        raise FreezeInputError(f"git command failed: {' '.join(args)}")
    return cast(str, result.stdout).strip()


def exact_subject(root: Path) -> tuple[str, str]:
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise FreezeInputError("worktree must be clean except ignored staging")
    return commit, tree


def _pairs_no_duplicates(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise FreezeInputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(raw: bytes) -> JsonValue:
    try:
        return cast(
            JsonValue,
            json.loads(
                raw,
                object_pairs_hook=_pairs_no_duplicates,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    FreezeInputError(f"non-finite JSON number: {value}")
                ),
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreezeInputError("input is not strict UTF-8 JSON") from exc


def _is_tracked(root: Path, relative: str) -> bool:
    return _run_git(root, "ls-files", "--error-unmatch", "--", relative).returncode == 0


def _is_ignored(root: Path, relative: str) -> bool:
    return _run_git(root, "check-ignore", "-q", "--", relative).returncode == 0


def _safe_path(root: Path, path: Path, *, allow_staging: bool) -> tuple[Path, str]:
    root = root.resolve(strict=True)
    candidate = path if path.is_absolute() else root / path
    if ".." in candidate.parts:
        raise FreezeInputError("input path contains a parent traversal")
    try:
        relative = candidate.absolute().relative_to(root).as_posix()
    except ValueError as exc:
        raise FreezeInputError("input path is outside repository") from exc
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise FreezeInputError(f"symlink input is forbidden: {relative}")
    resolved = candidate.resolve(strict=True)
    if candidate.absolute() != resolved:
        raise FreezeInputError("input path is not canonical")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FreezeInputError("resolved input path is outside repository") from exc
    stat = resolved.stat()
    if not resolved.is_file() or stat.st_nlink != 1:
        raise FreezeInputError(f"input must be a single-link regular file: {relative}")
    if not _is_tracked(root, relative) and not (
        allow_staging and relative.startswith(".tmp/") and _is_ignored(root, relative)
    ):
        raise FreezeInputError(
            f"input is neither tracked canonical nor ignored staging: {relative}"
        )
    return resolved, relative


def _identity(path: Path) -> FileIdentity:
    stat = path.lstat()
    return FileIdentity(
        stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_nlink
    )


def _read(path: Path) -> tuple[bytes, FileIdentity]:
    before = path.stat()
    if before.st_size > MAX_INPUT_BYTES:
        raise FreezeInputError("input exceeds byte limit")
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or len(raw) != before.st_size:
        raise FreezeInputError("input changed while being read")
    return raw, _identity(path)


def _model(model: type[T], raw: bytes) -> T:
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise FreezeInputError(f"invalid {model.__name__}") from exc


def parse_evidence_bytes(key: EvidenceKey, raw: bytes, subject: str) -> NativeEvidence:
    strict_json(raw)
    model_by_key: dict[EvidenceKey, type[BaseModel]] = {
        "architecture": ArchitectureReceipt,
        "duplicates": DuplicateInventory,
        "static": StaticQualityInventory,
        "test_db": TestDbAudit,
        "lifecycle": LifecycleInventory,
        "reachability": ReachabilityGraph,
        "performance": PerformanceReceipt,
        "reconciliation": ReconciliationReceipt,
    }
    parsed = cast(NativeEvidence, _model(model_by_key[key], raw))
    identity_by_key = {
        "architecture": getattr(parsed, "scoped_commit", None),
        "duplicates": getattr(parsed, "commit_hash", None),
        "static": getattr(parsed, "scoped_commit", None),
        "test_db": getattr(parsed, "scoped_commit", None),
        "lifecycle": getattr(parsed, "revision", None),
        "reachability": getattr(parsed, "subject_commit", None),
        "performance": getattr(parsed, "revision", None),
        "reconciliation": getattr(parsed, "subject_commit", None),
    }
    if identity_by_key[key] != subject:
        raise FreezeInputError(f"{key} subject mismatch")
    if isinstance(parsed, (LifecycleInventory, ReconciliationReceipt)) and parsed.worktree_dirty:
        raise FreezeInputError(f"{key} subject is dirty")
    if isinstance(parsed, PerformanceReceipt) and parsed.source_identity != "clean_head":
        raise FreezeInputError("performance source is not clean HEAD")
    return parsed


def evidence_oracle_disposition(
    key: EvidenceKey,
) -> tuple[Literal["VERIFIED", "HOLD"], tuple[str, ...]]:
    if key in {"architecture", "duplicates", "test_db"}:
        return "VERIFIED", ()
    if key == "performance":
        return "HOLD", ("raw performance evidence cannot prove the paired 510-second gate",)
    return "HOLD", ("native membership oracle unavailable",)


def load_evidence(root: Path, key: EvidenceKey, path: Path, subject: str) -> LoadedInput:
    safe, relative = _safe_path(root, path, allow_staging=True)
    raw, identity = _read(safe)
    parsed = parse_evidence_bytes(key, raw, subject)
    status, reasons = evidence_oracle_disposition(key)
    if key == "architecture":
        if parsed != build_architecture_receipt(root, "WORKTREE"):
            raise FreezeInputError("architecture receipt differs from native exact-subject oracle")
    elif key == "duplicates":
        if parsed != build_duplicate_inventory(root, "WORKTREE"):
            raise FreezeInputError("duplicate receipt differs from native exact-subject oracle")
    elif key == "test_db" and parsed != audit_test_db_patterns(root):
        raise FreezeInputError("test-db receipt differs from native exact-subject oracle")
    return LoadedInput(
        path=safe,
        relative_path=relative,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        identity=identity,
        value=parsed,
        oracle_status=status,
        oracle_reasons=reasons,
    )


def load_owner_snapshot(root: Path, path: Path, subject: str) -> LoadedInput:
    safe, relative = _safe_path(root, path, allow_staging=False)
    raw, identity = _read(safe)
    strict_json(raw)
    parsed = _model(OwnerSnapshot, raw)
    owner_ids = [item.issue_id for item in parsed.owners]
    populations = [item.population for item in parsed.population_routes]
    route_keys = [(item.kind, item.key) for item in parsed.admission_routes]
    if len(owner_ids) != len(set(owner_ids)):
        raise FreezeInputError("owner snapshot contains duplicate issues")
    if len(populations) != len(set(populations)) or len(populations) != 8:
        raise FreezeInputError("owner snapshot must route eight unique populations")
    expected_routes = {
        *(("block", key) for key, _label, _points in SCORE_BLOCKS),
        *(("hard_gate", key) for key in HARD_GATES),
    }
    if len(route_keys) != len(set(route_keys)) or set(route_keys) != expected_routes:
        raise FreezeInputError("owner admission keys differ from native scoring registry")
    known = set(owner_ids)
    if any(not set(route.owner_issues).issubset(known) for route in parsed.population_routes):
        raise FreezeInputError("population route names an unknown issue")
    if any(
        route.issue_id not in known or not set(route.delivery_issues).issubset(known)
        for route in parsed.admission_routes
    ):
        raise FreezeInputError("admission route names an unknown issue")
    source_safe, _ = _safe_path(root, root / parsed.source_document_path, allow_staging=False)
    source_raw, source_identity = _read(source_safe)
    if hashlib.sha256(source_raw).hexdigest() != parsed.source_sha256:
        raise FreezeInputError("owner snapshot source hash mismatch")
    for relative_path, expected in ((relative, raw), (parsed.source_document_path, source_raw)):
        blob = _run_git(root, "show", f"{subject}:{relative_path}", text=False)
        if blob.returncode != 0 or blob.stdout != expected:
            raise FreezeInputError(f"tracked source differs from subject: {relative_path}")
    companion = LoadedJson(
        path=source_safe,
        relative_path=parsed.source_document_path,
        raw=source_raw,
        sha256=hashlib.sha256(source_raw).hexdigest(),
        identity=source_identity,
    )
    return LoadedInput(
        path=safe,
        relative_path=relative,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        identity=identity,
        value=parsed,
        oracle_status="HOLD",
        oracle_reasons=("typed owner-approval binding unavailable",),
        companions=(companion,),
    )


def load_plan(root: Path, path: Path) -> LoadedJson:
    safe, relative = _safe_path(root, path, allow_staging=True)
    raw, identity = _read(safe)
    strict_json(raw)
    return LoadedJson(
        path=safe,
        relative_path=relative,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        identity=identity,
    )


def load_json_input(root: Path, path: Path, *, allow_staging: bool = True) -> LoadedJson:
    safe, relative = _safe_path(root, path, allow_staging=allow_staging)
    raw, identity = _read(safe)
    strict_json(raw)
    return LoadedJson(
        path=safe,
        relative_path=relative,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        identity=identity,
    )


def assert_unchanged(item: LoadedInput | LoadedJson) -> None:
    if item.path.is_symlink():
        raise FreezeInputError(f"input replaced by symlink: {item.relative_path}")
    raw, identity = _read(item.path)
    if item.identity is None or raw != item.raw or identity != item.identity:
        raise FreezeInputError(f"input changed during freeze: {item.relative_path}")
    if isinstance(item, LoadedInput):
        for companion in item.companions:
            assert_unchanged(companion)


def reject_aliases(inputs: list[LoadedInput | LoadedJson]) -> None:
    paths = [item.path for item in inputs]
    inodes = [(path.stat().st_dev, path.stat().st_ino) for path in paths]
    if len(paths) != len(set(paths)) or len(inodes) != len(set(inodes)):
        raise FreezeInputError("input artifacts alias the same file")


__all__ = [
    "EVIDENCE_KEYS",
    "FreezeInputError",
    "LoadedInput",
    "LoadedJson",
    "assert_unchanged",
    "evidence_oracle_disposition",
    "exact_subject",
    "load_evidence",
    "load_json_input",
    "load_owner_snapshot",
    "load_plan",
    "parse_evidence_bytes",
    "reject_aliases",
    "strict_json",
]
