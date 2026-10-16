"""Retire explicitly catalogued artifacts, preserving verified recovery survivors.

Catalog registration is an operator decision. Age or a directory name alone
never grants permission to delete a database, backup, or test snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CATALOG_RELATIVE_PATH = Path("data/operations/artifact-retention.json")
RECEIPT_RELATIVE_ROOT = Path("data/operations/artifact-retention-receipts")


class RetirementProof(BaseModel):
    """Immutable operation-closure or verification evidence bound to an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("proof paths must be absolute without parent traversal")
        return value


class RetirementEvidenceSet(BaseModel):
    """A current operation directory, including absence and new publications."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("evidence directories must be absolute without parent traversal")
        return value


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    allowed_root: Path
    family: str = Field(min_length=1)
    created_at: datetime
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    kind: Literal["disposable_test", "backup"]
    status: Literal["completed", "failed", "active"]
    verified: bool
    pins: list[str] = Field(default_factory=list)
    lifecycle_manifest: Path | None = None
    lifecycle_manifest_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    retirement_proofs: tuple[RetirementProof, ...] = ()
    retirement_evidence_sets: tuple[RetirementEvidenceSet, ...] = ()

    @model_validator(mode="after")
    def lifecycle_binding(self) -> Artifact:
        if (self.lifecycle_manifest is None) != (self.lifecycle_manifest_sha256 is None):
            raise ValueError("lifecycle manifest and digest must be supplied together")
        if self.lifecycle_manifest is not None and self.lifecycle_manifest != (
            self.allowed_root / ".earnings-temp-run.json"
        ):
            raise ValueError("lifecycle manifest must belong to the exact allowed run root")
        return self

    @field_validator("created_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value.astimezone(UTC)

    @field_validator("path", "allowed_root")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("artifact paths must be absolute without parent traversal")
        return value


class ArtifactCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    artifacts: list[Artifact]

    @field_validator("artifacts")
    @classmethod
    def unique_paths(cls, value: list[Artifact]) -> list[Artifact]:
        names = [os.path.normcase(str(item.path)) for item in value]
        if len(set(names)) != len(names):
            raise ValueError("catalog paths must be unique")
        return value


class RetirementDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    family: str
    sha256: str
    size: int
    action: Literal["keep", "retire", "missing", "deleted", "error"]
    reason: str


class RetirementResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_sha256: str | None = None
    receipt_path: str | None = None
    decisions: list[RetirementDecision] = Field(default_factory=lambda: list[RetirementDecision]())
    would_delete: int = 0
    deleted: int = 0
    bytes: int = 0
    errors: int = 0


def _link_like(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_ancestors(path: Path) -> bool:
    """Check named ancestors before resolve can hide a junction or symlink."""
    return not any(_link_like(parent) for parent in (path, *path.parents))


def _catalog_source(repo_root: Path) -> tuple[ArtifactCatalog | None, str | None]:
    path = repo_root / CATALOG_RELATIVE_PATH
    # Check each existing component before a child lookup can traverse a link.
    # A linked parent is unsafe even when its catalog child does not exist.
    for component in reversed((path, *path.parents)):
        try:
            if _link_like(component):
                raise ValueError("artifact catalog may not traverse links")
        except FileNotFoundError:
            return None, None
    data = path.read_bytes()
    return ArtifactCatalog.model_validate_json(data), hashlib.sha256(data).hexdigest()


def load_catalog(repo_root: Path) -> ArtifactCatalog | None:
    return _catalog_source(repo_root)[0]


def retained_scope_roots(catalog: ArtifactCatalog | None) -> set[Path]:
    """Exclude whole registered scopes from the ordinary age-based cleaner."""
    return {item.allowed_root for item in catalog.artifacts} if catalog else set()


def merge_operator_artifact(operator: Artifact, producer: Artifact | None) -> Artifact:
    """Operator declarations retain authority; producer evidence can add holds."""
    if producer is None:
        return operator
    holds = list(dict.fromkeys([*operator.pins, *producer.pins]))
    if producer.status != "completed":
        holds.append("producer_operation_unfinished")
    if not producer.verified:
        holds.append("producer_artifact_unverified")
    return operator.model_copy(
        update={
            "status": producer.status if operator.status == "completed" else operator.status,
            "verified": operator.verified and producer.verified,
            "pins": list(dict.fromkeys(holds)),
            "retirement_proofs": tuple(
                dict.fromkeys((*operator.retirement_proofs, *producer.retirement_proofs))
            ),
            "retirement_evidence_sets": tuple(
                dict.fromkeys(
                    (*operator.retirement_evidence_sets, *producer.retirement_evidence_sets)
                )
            ),
        }
    )


def _approved_root(root: Path, repo_root: Path) -> bool:
    local_roots = (repo_root / ".tmp", repo_root / "data" / "backups")
    if any(root == scope or scope in root.parents for scope in local_roots):
        return True
    temporary = Path(tempfile.gettempdir()).resolve()
    if temporary in root.parents and root.relative_to(temporary).parts[0].startswith(
        "earnings-summary-"
    ):
        return True
    if os.name != "nt":
        return False
    # These are the existing Windows test/deployment locations, not drive-wide
    # permission. The catalog must name a narrower root for each run or family.
    windows_roots = (
        Path("C:/tmp"),
        Path("C:/ProgramData/BhanuOperations/deployments"),
        Path.home() / ".gemini/antigravity/runtime/earnings-summary/.tmp",
        Path.home() / ".gemini/antigravity/scratch/earnings-summary/.tmp",
    )
    return any(scope in root.parents for scope in windows_roots)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_set_digest(directory: Path) -> str | None:
    """Hash bounded direct JSON members without following links or opening a DB."""
    for component in reversed((directory, *directory.parents)):
        try:
            if _link_like(component):
                raise ValueError("linked operation evidence directory")
        except FileNotFoundError:
            return None
    if not directory.is_dir():
        raise ValueError("operation evidence boundary is not a directory")
    with os.scandir(directory) as entries:
        members: list[Path] = []
        for entry in entries:
            if entry.name.casefold().endswith(".json"):
                members.append(Path(entry.path))
                if len(members) > 4096:
                    raise ValueError("operation evidence count limit")
    digest = hashlib.sha256()
    total_bytes = 0
    for path in sorted(members):
        before = path.lstat()
        if (
            _link_like(path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > 2 * 1024 * 1024
        ):
            raise ValueError("unsafe operation evidence member")
        total_bytes += before.st_size
        if total_bytes > 64 * 1024 * 1024:
            raise ValueError("operation evidence byte limit")
        with path.open("rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        after = path.lstat()
        if len(raw) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("operation evidence changed during observation")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def _ancestor_lifecycle(path: Path, parent: Path) -> tuple[str | None, str | None]:
    """Check lifecycle ownership without importing the producer module."""
    if not _safe_ancestors(path):
        return "linked_lifecycle_manifest", None
    try:
        with path.open("rb") as handle:
            data = handle.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            return "unverified_lifecycle_manifest", None
        payload: object = json.loads(data)
        if not isinstance(payload, dict):
            return "unverified_lifecycle_manifest", None
        record = cast("dict[str, object]", payload)
        root = record.get("run_root")
        pins = record.get("pins", [])
        status = record.get("status")
        if (
            record.get("schema_version") != "earnings-temp-run/v1"
            or record.get("owner") != "earnings-summary"
            or not isinstance(root, str)
            or not Path(root).is_absolute()
            or ".." in Path(root).parts
            or os.path.normcase(os.path.abspath(root)) != os.path.normcase(str(parent))
            or not isinstance(pins, list)
            or not isinstance(status, str)
            or not isinstance(record.get("run_id"), str)
            or not record.get("run_id")
            or not isinstance(record.get("files"), list)
        ):
            return "unverified_lifecycle_manifest", None
        if status in {"active", "failed"}:
            return "unfinished_lifecycle_manifest", None
        if status != "completed":
            return "unverified_lifecycle_manifest", None
        started, completed = record.get("started_at"), record.get("completed_at")
        if not isinstance(started, str) or not isinstance(completed, str):
            return "unverified_lifecycle_manifest", None
        start, finish = datetime.fromisoformat(started), datetime.fromisoformat(completed)
        if start.tzinfo is None or finish.tzinfo is None or finish < start:
            return "unverified_lifecycle_manifest", None
        if pins:
            return "pinned_lifecycle_manifest", None
        return None, hashlib.sha256(data).hexdigest()
    except (OSError, UnicodeError, ValueError):
        return "unverified_lifecycle_manifest", None


def _validate_proofs(item: Artifact) -> str | None:
    for evidence in item.retirement_evidence_sets:
        try:
            if evidence_set_digest(evidence.path) != evidence.sha256:
                return "retirement_evidence_set_changed"
        except (OSError, ValueError):
            return "retirement_evidence_set_unavailable"
    for proof in item.retirement_proofs:
        try:
            if not _safe_ancestors(proof.path):
                return "linked_retirement_proof"
            metadata = proof.path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 2 * 1024 * 1024:
                return "invalid_retirement_proof"
            if _hash(proof.path) != proof.sha256:
                return "retirement_proof_changed"
        except OSError:
            return "retirement_proof_unavailable"
    return None


def _validate_file(item: Artifact, repo_root: Path, live_db: Path | None) -> str | None:
    """A preservation reason, or None when the named file still matches proof."""
    if not _approved_root(item.allowed_root, repo_root):
        return "unapproved_scope"
    if item.path == item.allowed_root or item.allowed_root not in item.path.parents:
        return "scope_escape"
    if item.path.name.casefold() == "portfolio_gc_archive.db":
        return "durable_archive"
    if live_db is not None and os.path.normcase(os.path.abspath(item.path)) in {
        os.path.normcase(os.path.abspath(str(live_db) + suffix))
        for suffix in ("", "-wal", "-shm", "-journal")
    }:
        return "live_database"
    relative_parts = item.path.relative_to(item.allowed_root).parts
    if any(
        component.casefold()
        in {
            ".git",
            ".claude",
            "node_modules",
            "ir_documents",
            "transcripts",
            "src",
            "execution",
            "directives",
            "historical",
        }
        for component in relative_parts
    ):
        return "protected_source"
    try:
        if not _safe_ancestors(item.path):
            return "linked_path"
        if reason := _validate_proofs(item):
            return reason
        if item.lifecycle_manifest is not None:
            if not _safe_ancestors(item.lifecycle_manifest):
                return "linked_lifecycle_manifest"
            if _hash(item.lifecycle_manifest) != item.lifecycle_manifest_sha256:
                return "lifecycle_manifest_changed"
        if live_db is not None and item.path.resolve() == live_db.resolve():
            return "live_database"
        parent = item.path.parent
        boundaries = [
            repo_root / ".tmp",
            repo_root / "data/backups",
            Path(tempfile.gettempdir()).resolve(),
        ]
        if os.name == "nt":
            boundaries += [
                Path("C:/tmp"),
                Path("C:/ProgramData/BhanuOperations/deployments"),
                Path.home() / ".gemini/antigravity/runtime/earnings-summary/.tmp",
                Path.home() / ".gemini/antigravity/scratch/earnings-summary/.tmp",
            ]
        boundary = next(
            scope
            for scope in boundaries
            if scope == item.allowed_root or scope in item.allowed_root.parents
        )
        lifecycle_guards: list[tuple[Path, str]] = []
        checkpoint_guards: list[tuple[Path, str | None]] = []
        while parent == boundary or boundary in parent.parents:
            lifecycle = parent / ".earnings-temp-run.json"
            if lifecycle != item.lifecycle_manifest and (
                lifecycle.exists() or lifecycle.is_symlink()
            ):
                reason, digest = _ancestor_lifecycle(lifecycle, parent)
                if reason is not None:
                    return reason
                if digest is not None:
                    lifecycle_guards.append((lifecycle, digest))
            state_file = parent / "state.json"
            if state_file.exists() or state_file.is_symlink():
                if not _safe_ancestors(state_file):
                    return "linked_checkpoint"
                try:
                    checkpoint_bytes = state_file.read_bytes()
                    state: object = json.loads(checkpoint_bytes)
                except (OSError, UnicodeError, ValueError):
                    return "unverified_checkpoint"
                if not isinstance(state, dict) or state.get("status") not in {
                    "completed",
                    "complete",
                    "done",
                    "success",
                    "succeeded",
                }:
                    return "unfinished_checkpoint"
                checkpoint_guards.append((state_file, hashlib.sha256(checkpoint_bytes).hexdigest()))
            else:
                checkpoint_guards.append((state_file, None))
            if parent == boundary:
                break
            parent = parent.parent
        metadata = item.path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return "not_unique_regular_file"
        if metadata.st_size != item.size:
            return "size_changed"
        if getattr(metadata, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_READONLY", 0x1
        ):
            return "readonly_attribute"
        if item.path.name.lower().endswith(("-wal", "-journal")) and metadata.st_size:
            return "nonzero_database_wal"
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(item.path) + suffix)
            if sidecar.exists() and (not _safe_ancestors(sidecar) or sidecar.stat().st_size):
                return "nonempty_database_sidecar"
        if _hash(item.path) != item.sha256:
            return "digest_changed"
        after = item.path.stat()
        if (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return "file_changed_during_validation"
        if item.lifecycle_manifest is not None and (
            not _safe_ancestors(item.lifecycle_manifest)
            or _hash(item.lifecycle_manifest) != item.lifecycle_manifest_sha256
        ):
            return "lifecycle_manifest_changed"
        if reason := _validate_proofs(item):
            return reason
        if any(
            not _safe_ancestors(path) or _hash(path) != digest for path, digest in lifecycle_guards
        ):
            return "lifecycle_manifest_changed"
        for path, digest in checkpoint_guards:
            if digest is None:
                if path.exists() or path.is_symlink():
                    return "checkpoint_changed"
            elif not _safe_ancestors(path) or _hash(path) != digest:
                return "checkpoint_changed"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable_file"
    return None


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _safe_ancestors(path.parent):
        raise ValueError("retirement receipts may not traverse links")
    descriptor, temporary = tempfile.mkstemp(prefix=".retention-", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        staged.unlink(missing_ok=True)


def run_retention(
    repo_root: Path,
    *,
    now: datetime,
    apply: bool = False,
    live_db: Path | None = None,
    catalog: ArtifactCatalog | None = None,
    catalog_roots: tuple[Path, ...] = (),
    test_retention_days: int = 7,
) -> RetirementResult:
    """Plan explicit retirement and save a durable receipt before apply effects."""
    if test_retention_days < 0:
        raise ValueError("test_retention_days must be nonnegative")
    # Load current operator declarations atop producer registrations. Later roots
    # have priority, so the caller supplies runtime first and canonical state last.
    sources = {root: _catalog_source(root) for root in dict.fromkeys(catalog_roots or (repo_root,))}
    artifacts = (
        {os.path.normcase(str(item.path)): item for item in catalog.artifacts} if catalog else {}
    )
    for current, _digest in sources.values():
        if current is not None:
            for item in current.artifacts:
                name = os.path.normcase(str(item.path))
                artifacts[name] = merge_operator_artifact(item, artifacts.get(name))
    if catalog is None and all(current is None for current, _digest in sources.values()):
        return RetirementResult()
    catalog = ArtifactCatalog(schema_version=1, artifacts=list(artifacts.values()))
    if live_db is None:
        configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
        if configured:
            live_db = Path(configured)
        elif any(item.path.name.casefold() == "portfolio.db" for item in catalog.artifacts):
            raise ValueError("EARNINGS_SUMMARY_DB_PATH is required for database retirement")
    catalog_sha = hashlib.sha256(catalog.model_dump_json().encode()).hexdigest()
    reasons = [_validate_file(item, repo_root, live_db) for item in catalog.artifacts]
    latest: dict[str, Artifact] = {}
    for item, unsafe in zip(catalog.artifacts, reasons, strict=True):
        if (
            item.kind == "backup"
            and item.status == "completed"
            and item.verified
            and unsafe is None
        ):
            previous = latest.get(item.family)
            if previous is None or (item.created_at, str(item.path)) > (
                previous.created_at,
                str(previous.path),
            ):
                latest[item.family] = item
    result = RetirementResult(catalog_sha256=catalog_sha)
    for item, unsafe in zip(catalog.artifacts, reasons, strict=True):
        reason = unsafe or (
            "pinned"
            if item.pins
            else "unfinished"
            if item.status != "completed"
            else "unverified"
            if not item.verified
            else "latest_verified"
            if latest.get(item.family) == item
            else "within_test_retention"
            if item.kind == "disposable_test"
            and item.created_at >= now - timedelta(days=test_retention_days)
            else "completed_test"
            if item.kind == "disposable_test"
            else "superseded_verified_backup"
        )
        action: Literal["keep", "retire", "missing", "deleted", "error"] = (
            "missing"
            if reason == "missing"
            else "retire"
            if reason in {"completed_test", "superseded_verified_backup"}
            else "keep"
        )
        result.decisions.append(
            RetirementDecision(
                path=str(item.path),
                family=item.family,
                sha256=item.sha256,
                size=item.size,
                action=action,
                reason=reason,
            )
        )
    candidates = [decision for decision in result.decisions if decision.action == "retire"]
    result.would_delete = len(candidates)
    result.bytes = sum(decision.size for decision in candidates)
    if not apply or not candidates:
        return result
    # Unique receipts preserve prior partial-failure evidence and bind the exact
    # catalog and plan. Never replace the only receipt for an earlier attempt.
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    receipt = repo_root / RECEIPT_RELATIVE_ROOT / f"{stamp}-{os.getpid()}-{catalog_sha[:16]}.json"
    if receipt.exists():
        raise ValueError("retirement receipt already exists; use a new invocation time")
    result.receipt_path = str(receipt)
    payload: dict[str, object] = {
        "schema_version": 1,
        "created_at": now.isoformat(),
        "state": "planned",
        "catalog_sources": [
            {"path": str(root / CATALOG_RELATIVE_PATH), "sha256": digest}
            for root, (_current, digest) in sources.items()
        ],
        "result": result.model_dump(mode="json"),
    }
    _write_receipt(receipt, payload)
    by_path = {str(item.path): item for item in catalog.artifacts}
    journal_path = receipt.with_suffix(".jsonl")
    payload["result_journal"] = str(journal_path)
    _write_receipt(receipt, payload)
    with journal_path.open("x", encoding="utf-8") as journal:
        for decision in candidates:
            item = by_path[decision.path]
            # Revalidate both target and last-good survivor immediately before each
            # deletion. A changed newest backup cannot justify retiring the old one.
            unsafe = _validate_file(item, repo_root, live_db)
            survivor = latest.get(item.family)
            if unsafe is None and item.kind == "backup" and survivor is not None:
                unsafe = _validate_file(survivor, repo_root, live_db)
            if unsafe is None:
                # A pin, status change, removal, new declaration or malformed
                # catalog invalidates this plan. A fresh attempt must replan.
                try:
                    if any(
                        _catalog_source(root)[1] != digest
                        for root, (_current, digest) in sources.items()
                    ):
                        unsafe = "catalog_changed"
                except (OSError, ValueError):
                    unsafe = "catalog_unavailable"
            if unsafe is not None:
                decision.action = "error"
                decision.reason = unsafe
                result.errors += 1
            else:
                try:
                    item.path.unlink()
                    decision.action = "deleted"
                    result.deleted += 1
                except OSError:
                    decision.action = "error"
                    decision.reason = "unlink_failed"
                    result.errors += 1
            journal.write(decision.model_dump_json() + "\n")
            journal.flush()
            os.fsync(journal.fileno())
    result.would_delete = 0
    result.bytes = sum(item.size for item in result.decisions if item.action == "deleted")
    payload["state"] = "failed" if result.errors else "completed"
    payload["result"] = result.model_dump(mode="json")
    _write_receipt(receipt, payload)
    return result
