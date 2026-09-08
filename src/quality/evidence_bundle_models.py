"""BHA-147 evidence-bundle public data models and path policies."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quality.admission_policy import SOURCE_PATHS as _ADMISSION_SOURCE_PATHS
from quality.evidence_path_policy import (
    FREEZE_PATH,
    admission_path_for,
    is_canonical_generator_path,
    is_canonical_receipt_path,
)
from quality.scoring import HARD_GATES, SCORE_BLOCKS

COLLECTION_SCHEMA = "quality-evidence-collection-v1"
VIOLATION_CAP = 20
VIOLATION_TEXT_CAP = 200
_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_HEX40_RE = re.compile(_HEX40)
HEX40_RE = _HEX40_RE

AdmissionKind = Literal["block", "hard_gate"]
AdmissionState = Literal["pass", "fail"]
ArtifactCollectionStatus = Literal["collected", "hold", "failed"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_is_canonical_receipt_path = is_canonical_receipt_path


_is_canonical_generator_path = is_canonical_generator_path


def bound_violations(items: Sequence[str]) -> tuple[str, ...]:
    out = [v[:VIOLATION_TEXT_CAP] for v in items[:VIOLATION_CAP]]
    if len(items) > VIOLATION_CAP:
        out[-1] = f"{len(items) - VIOLATION_CAP + 1} additional violations omitted"
    return tuple(out)


def non_architecture_blocks() -> tuple[str, ...]:
    return tuple(key for key, _label, _points in SCORE_BLOCKS if not key.startswith("elegance."))


ALLOWED_SOURCE_PATHS: tuple[str, ...] = tuple(_ADMISSION_SOURCE_PATHS.values())


def _check_accepted_exit_codes(codes: tuple[int, ...]) -> tuple[int, ...]:
    if len(codes) == 0 or len(codes) > 256:
        raise ValueError("accepted_exit_codes out of bounds")
    for code in codes:
        if isinstance(code, bool):
            raise ValueError("accepted_exit_codes must not contain bools")
        if code < 0 or code > 255:
            raise ValueError("accepted_exit_codes out of range")
    if tuple(sorted(set(codes))) != tuple(codes):
        raise ValueError("accepted_exit_codes must be unique sorted")
    return codes


def allowed_bundle_paths() -> tuple[str, ...]:
    paths: list[str] = [*ALLOWED_SOURCE_PATHS, FREEZE_PATH]
    for key in non_architecture_blocks():
        paths.append(admission_path_for("block", key))
    for key in HARD_GATES:
        paths.append(admission_path_for("hard_gate", key))
    return tuple(sorted(set(paths)))


def _is_canonical_handoff_path(value: str) -> bool:
    if not 1 <= len(value) <= 200:
        return False
    if "\\" in value:
        return False
    rel = PurePosixPath(value)
    if rel.is_absolute():
        return False
    if value != rel.as_posix():
        return False
    if rel.parts[:1] != (".tmp",):
        return False
    if ".." in rel.parts or "." in rel.parts:
        return False
    if "" in rel.parts:
        return False
    return not value.endswith("/")


def _check_depends_on(artifact_id: str, depends_on: tuple[str, ...]) -> tuple[str, ...]:
    if len(depends_on) > 64:
        raise ValueError("depends_on out of bounds")
    for dep in depends_on:
        if not 1 <= len(dep) <= 100:
            raise ValueError("depends_on entry out of bounds")
        if re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$", dep) is None:
            raise ValueError("depends_on entry is not a canonical id")
    if tuple(sorted(set(depends_on))) != tuple(depends_on):
        raise ValueError("depends_on must be unique sorted")
    if artifact_id in depends_on:
        raise ValueError("depends_on must not contain self")
    return depends_on


class ArtifactSpec(StrictModel):
    artifact_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
    canonical_path: str
    generator_path: str | None = None
    generator_version: str | None = Field(default=None, max_length=100)
    command: tuple[str, ...] = Field(min_length=1, max_length=32)
    native_scope: str = Field(min_length=1, max_length=100)
    output_flag: str | None = Field(
        default=None, max_length=32, pattern=r"^--[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
    accepted_exit_codes: tuple[int, ...] = Field(default=(0,))
    depends_on: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    handoff_path: str | None = Field(default=None, min_length=1, max_length=200)
    input_manifest_flag: str | None = Field(
        default=None, max_length=32, pattern=r"^--[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
    roadmap_context_path: Literal["docs/quality/quality-9plus-roadmap.md"] | None = None

    @model_validator(mode="after")
    def _check(self) -> ArtifactSpec:
        if not _is_canonical_receipt_path(self.canonical_path):
            raise ValueError("noncanonical canonical_path")
        if self.generator_path is not None and not _is_canonical_generator_path(
            self.generator_path
        ):
            raise ValueError("noncanonical generator_path")
        if any(len(c) == 0 or len(c) > 500 for c in self.command):
            raise ValueError("command argv entry out of bounds")
        if self.native_scope.strip() != self.native_scope or not self.native_scope.strip():
            raise ValueError("native_scope must be a nonempty trimmed label")
        _check_accepted_exit_codes(self.accepted_exit_codes)
        _check_depends_on(self.artifact_id, self.depends_on)
        if self.handoff_path is not None and not _is_canonical_handoff_path(self.handoff_path):
            raise ValueError("noncanonical handoff_path")
        if self.roadmap_context_path is not None and self.input_manifest_flag is None:
            raise ValueError("roadmap_context_path requires input_manifest_flag")
        return self


class ArtifactRecord(StrictModel):
    artifact_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
    canonical_path: str
    generator_path: str | None = None
    generator_sha256: str | None = Field(default=None, pattern=_HEX64)
    generator_version: str | None = Field(default=None, max_length=100)
    command: tuple[str, ...] = Field(min_length=1)
    native_scope: str = Field(min_length=1, max_length=100)
    output_flag: str | None = Field(
        default=None, max_length=32, pattern=r"^--[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
    embedded_subject: str | None = Field(default=None, pattern=_HEX40)
    schema_version: str | None = Field(default=None, max_length=200)
    sha256: str = Field(pattern=_HEX64)
    byte_length: int = Field(ge=0)
    collection_status: ArtifactCollectionStatus
    staging_file: str = Field(
        min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_\-]*\.raw$"
    )
    accepted_exit_codes: tuple[int, ...] = Field(default=(0,))
    return_code: int | None = Field(default=None, ge=-255, le=255)
    depends_on: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    handoff_path: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _check(self) -> ArtifactRecord:
        if self.staging_file != f"{self.artifact_id}.raw":
            raise ValueError("staging_file must equal '<artifact_id>.raw'")
        if not _is_canonical_receipt_path(self.canonical_path):
            raise ValueError("noncanonical canonical_path")
        if self.generator_path is not None and not _is_canonical_generator_path(
            self.generator_path
        ):
            raise ValueError("noncanonical generator_path")
        object.__setattr__(self, "sha256", self.sha256.lower())
        _check_accepted_exit_codes(self.accepted_exit_codes)
        if self.generator_sha256 is not None:
            object.__setattr__(self, "generator_sha256", self.generator_sha256.lower())
        if self.embedded_subject is not None:
            object.__setattr__(self, "embedded_subject", self.embedded_subject.lower())
        _check_depends_on(self.artifact_id, self.depends_on)
        if self.handoff_path is not None and not _is_canonical_handoff_path(self.handoff_path):
            raise ValueError("noncanonical handoff_path")
        return self


class SubjectSnapshot(StrictModel):
    commit: str = Field(pattern=_HEX40)
    tree: str = Field(pattern=_HEX40)
    clean: bool


class CollectionManifest(StrictModel):
    schema_version: str = Field(pattern=r"^quality-evidence-collection-v1$")
    subject_commit: str = Field(pattern=_HEX40)
    subject_tree: str = Field(pattern=_HEX40)
    head_before: str = Field(pattern=_HEX40)
    head_after: str = Field(pattern=_HEX40)
    tree_before: str = Field(pattern=_HEX40)
    tree_after: str = Field(pattern=_HEX40)
    clean_before: bool
    clean_after: bool
    artifacts: tuple[ArtifactRecord, ...] = Field(max_length=64)
    violations: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    status: str = Field(pattern=r"^(COMPLETE|HOLD)$")
    manifest_hash: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _check(self) -> CollectionManifest:
        ids = [a.artifact_id for a in self.artifacts]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate artifact_id")
        paths = [a.canonical_path for a in self.artifacts]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate canonical_path")
        if tuple(sorted(ids)) != tuple(ids):
            raise ValueError("artifacts must be sorted by artifact_id")
        for field in (
            "subject_commit",
            "subject_tree",
            "head_before",
            "head_after",
            "tree_before",
            "tree_after",
        ):
            object.__setattr__(self, field, getattr(self, field).lower())
        object.__setattr__(self, "manifest_hash", self.manifest_hash.lower())
        if self.status == "COMPLETE":
            if len(self.violations) != 0:
                raise ValueError("COMPLETE requires empty violations")
            if any(a.collection_status != "collected" for a in self.artifacts):
                raise ValueError("COMPLETE requires all artifacts collected")
            if not (self.clean_before and self.clean_after):
                raise ValueError("COMPLETE requires clean worktree")
            if not (self.head_before == self.head_after == self.subject_commit):
                raise ValueError("COMPLETE requires exact-subject head bracket")
            if not (self.tree_before == self.tree_after == self.subject_tree):
                raise ValueError("COMPLETE requires exact-subject tree bracket")
        return self


class BundleAssembly(StrictModel):
    schema_version: str = Field(pattern=r"^quality-bundle-assembly-v1$")
    status: str = Field(pattern=r"^(COMPLETE|HOLD)$")
    subject_commit: str = Field(pattern=_HEX40)
    generator_path: str
    generator_sha256: str = Field(pattern=_HEX64)
    written: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    violations: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
