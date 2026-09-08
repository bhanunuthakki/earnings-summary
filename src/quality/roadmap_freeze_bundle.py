"""Typed transport for the roadmap index; this module awards no score points."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from quality.admission_policy import SOURCE_PATHS, parse_source
from quality.evidence_bundle_io import git
from quality.roadmap_freeze import GENERATOR_PATHS, verify_index_contents
from quality.roadmap_freeze_inputs import (
    FreezeInputError,
    LoadedInput,
    LoadedJson,
    evidence_oracle_disposition,
    load_json_input,
    parse_evidence_bytes,
    strict_json,
)
from quality.roadmap_freeze_models import (
    EvidenceKey,
    FreezeReceipt,
    OwnerSnapshot,
    OwnerSnapshotIndex,
)


class DependencyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    path: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def canonical_relative_path(self) -> DependencyEntry:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or path.as_posix() != self.path
            or ".." in path.parts
            or "\\" in self.path
        ):
            raise ValueError("dependency path must be canonical and manifest-relative")
        return self


class DependencyManifest(RootModel[dict[EvidenceKey, DependencyEntry]]):
    pass


def load_dependency_inputs(
    root: Path, manifest_path: Path
) -> tuple[dict[str, Path], tuple[LoadedJson, ...]]:
    """Load exact sibling source bytes and retain snapshots for the caller's final check."""
    manifest = load_json_input(root, manifest_path)
    entries = DependencyManifest.model_validate_json(manifest.raw).root
    snapshots = [manifest]
    paths: dict[str, Path] = {}
    seen_paths = {manifest.path}
    for key, entry in entries.items():
        source = load_json_input(root, manifest.path.parent / entry.path)
        if source.path in seen_paths:
            raise FreezeInputError("dependency manifest aliases an input")
        if source.sha256 != entry.sha256:
            raise FreezeInputError(f"dependency hash mismatch: {key}")
        seen_paths.add(source.path)
        snapshots.append(source)
        paths[key] = source.path
    return paths, tuple(snapshots)


def validate_freeze_index(
    root: Path, raw: bytes, subject: str, sources: Mapping[str, bytes]
) -> FreezeReceipt:
    """Bind a serialized index to its exact subject and bundled native inputs."""
    strict_json(raw)
    receipt = FreezeReceipt.model_validate_json(raw)
    if receipt.subject_commit != subject:
        raise ValueError("freeze subject differs from bundle subject")
    tree = git(root, "rev-parse", "--verify", f"{subject}^{{tree}}")
    if tree.returncode != 0 or tree.stdout.decode().strip() != receipt.subject_tree:
        raise ValueError("freeze subject tree differs from Git")
    generator = hashlib.sha256()
    for path in GENERATOR_PATHS:
        blob = git(root, "show", f"{subject}:{path}")
        if blob.returncode != 0:
            raise ValueError("freeze generator is missing from subject")
        generator.update(path.encode() + b"\0" + blob.stdout + b"\0")
    if generator.hexdigest() != receipt.generator_sha256:
        raise ValueError("freeze generator hash differs from subject")
    available = {key for key, path in SOURCE_PATHS.items() if path in sources}
    if {entry.key for entry in receipt.evidence} != available:
        raise ValueError("freeze does not index the complete bundled source set")
    loaded: dict[EvidenceKey, LoadedInput] = {}
    for entry in receipt.evidence:
        source = sources[SOURCE_PATHS[entry.key]]
        parsed = parse_source(entry.key, source, subject)
        if (
            not parsed.typed_valid
            or entry.sha256 != hashlib.sha256(source).hexdigest()
            or entry.byte_length != len(source)
            or entry.schema_version != parsed.schema_version
            or entry.subject_commit != subject
        ):
            raise ValueError(f"freeze source binding differs from bundled bytes: {entry.key}")
        relative = _relative_input_path(entry.path)
        native = parse_evidence_bytes(entry.key, source, subject)
        status, reasons = evidence_oracle_disposition(entry.key)
        loaded[entry.key] = LoadedInput(
            path=root / relative,
            relative_path=relative,
            raw=source,
            sha256=hashlib.sha256(source).hexdigest(),
            identity=None,
            value=native,
            oracle_status=status,
            oracle_reasons=reasons,
        )
    owner = _bound_owner_snapshot(root, receipt, subject)
    if receipt.plan_path is not None:
        _relative_input_path(receipt.plan_path)
    verify_index_contents(root, receipt, loaded, owner)
    return receipt


def _relative_input_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or "\\" in value
    ):
        raise ValueError("freeze input locator is not canonical and relative")
    return value


def _bound_owner_snapshot(root: Path, receipt: FreezeReceipt, subject: str) -> OwnerSnapshot | None:
    index = receipt.owner_snapshot
    if index is None:
        return None
    relative = _relative_input_path(index.path)
    blob = git(root, "show", f"{subject}:{relative}")
    if blob.returncode != 0 or hashlib.sha256(blob.stdout).hexdigest() != index.sha256:
        raise ValueError("owner snapshot differs from subject bytes")
    strict_json(blob.stdout)
    owner = OwnerSnapshot.model_validate_json(blob.stdout)
    source = git(root, "show", f"{subject}:{owner.source_document_path}")
    if source.returncode != 0 or hashlib.sha256(source.stdout).hexdigest() != owner.source_sha256:
        raise ValueError("owner source document differs from subject bytes")
    expected = OwnerSnapshotIndex(
        path=relative,
        sha256=hashlib.sha256(blob.stdout).hexdigest(),
        source_document_id=owner.source_document_id,
        source_document_path=owner.source_document_path,
        source_sha256=owner.source_sha256,
        owners=owner.owners,
        population_routes=owner.population_routes,
        admission_routes=owner.admission_routes,
        oracle_status="HOLD",
        oracle_reasons=("typed owner-approval binding unavailable",),
    )
    if expected != index:
        raise ValueError("owner index differs from subject snapshot")
    return owner
