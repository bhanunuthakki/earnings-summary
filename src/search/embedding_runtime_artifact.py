"""Path-free commitments to every local embedding runtime input.

The manifest intentionally records logical names, content digests, versions,
and execution settings only. Physical paths are caller-owned transient inputs
and never cross the persistence boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ArtifactRole = Literal["model", "tokenizer", "config", "runtime"]
ConfigValue = str | int | bool

_CHUNK_BYTES = 1024 * 1024
_MAX_FILES = 10_000
_MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024 * 1024
_SENSITIVE_NAMES = ("secret", "token", "password", "credential", "api_key")


def _sha256(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


class RuntimeArtifactFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_name: str = Field(min_length=1, max_length=512)
    role: ArtifactRole
    size_bytes: int = Field(ge=0, le=_MAX_FILE_BYTES)
    sha256: str

    @field_validator("logical_name")
    @classmethod
    def _logical_name(cls, value: str) -> str:
        if value != value.strip() or "\\" in value or value.startswith("/"):
            raise ValueError("logical_name must be a normalized relative logical identifier")
        parts = value.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("logical_name contains an invalid segment")
        return value

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, "file sha256")


class RuntimeComponentVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    version: str = Field(min_length=1, max_length=128)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\r\n"):
            raise ValueError("component version must be normalized single-line text")
        return value


class RuntimeExecutionSetting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    value: ConfigValue

    @field_validator("name")
    @classmethod
    def _not_sensitive(cls, value: str) -> str:
        lowered = value.lower()
        if any(marker in lowered for marker in _SENSITIVE_NAMES):
            raise ValueError("execution setting names cannot describe credentials")
        return value

    @field_validator("value")
    @classmethod
    def _bounded_value(cls, value: ConfigValue) -> ConfigValue:
        if isinstance(value, str):
            if len(value) > 512 or any(character in value for character in "\r\n"):
                raise ValueError("execution setting text must be bounded single-line data")
            lowered = value.lower()
            if "://" in value or lowered.startswith(("c:\\", "/", "\\\\")):
                raise ValueError("execution settings cannot persist paths or URIs")
        return value


class EmbeddingRuntimeArtifact(BaseModel):
    """Canonical, path-free identity for one executable embedding coordinate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    dimensions: int = Field(gt=0)
    execution_provider: str = Field(min_length=1, max_length=128)
    execution_settings: tuple[RuntimeExecutionSetting, ...]
    component_versions: tuple[RuntimeComponentVersion, ...]
    files: tuple[RuntimeArtifactFile, ...] = Field(min_length=1, max_length=_MAX_FILES)

    @model_validator(mode="after")
    def _canonical_collections(self) -> Self:
        file_names = [item.logical_name for item in self.files]
        components = [item.component for item in self.component_versions]
        settings = [item.name for item in self.execution_settings]
        if file_names != sorted(file_names) or len(file_names) != len(set(file_names)):
            raise ValueError("runtime artifact files must have unique sorted logical names")
        if components != sorted(components) or len(components) != len(set(components)):
            raise ValueError("component versions must have unique sorted names")
        if settings != sorted(settings) or len(settings) != len(set(settings)):
            raise ValueError("execution settings must have unique sorted names")
        if not components:
            raise ValueError("runtime artifact requires explicit component versions")
        if sum(item.size_bytes for item in self.files) > _MAX_TOTAL_BYTES:
            raise ValueError("runtime artifact exceeds the bounded total byte limit")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class RuntimeArtifactSource(BaseModel):
    """Transient physical input; this model must never be persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    logical_name: str
    role: ArtifactRole
    relative_path: Path


def build_runtime_artifact(
    root: Path,
    sources: Sequence[RuntimeArtifactSource],
    *,
    provider: str,
    model: str,
    dimensions: int,
    execution_provider: str,
    execution_settings: Mapping[str, ConfigValue],
    component_versions: Mapping[str, str],
) -> EmbeddingRuntimeArtifact:
    """Hash a bounded local tree without persisting or disclosing physical paths."""

    if not sources or len(sources) > _MAX_FILES:
        raise ValueError("runtime artifact source count is outside the bounded limit")
    try:
        root_resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("runtime artifact root is unavailable") from exc
    if not root_resolved.is_dir():
        raise ValueError("runtime artifact root must be a directory")

    logical_names: set[str] = set()
    resolved_files: set[Path] = set()
    files: list[RuntimeArtifactFile] = []
    total_bytes = 0
    for source in sorted(sources, key=lambda item: item.logical_name):
        # Validate before touching the physical source so errors never echo paths.
        logical = RuntimeArtifactFile(
            logical_name=source.logical_name,
            role=source.role,
            size_bytes=0,
            sha256="0" * 64,
        )
        if logical.logical_name in logical_names:
            raise ValueError("runtime artifact contains duplicate logical names")
        logical_names.add(logical.logical_name)
        if source.relative_path.is_absolute() or ".." in source.relative_path.parts:
            raise ValueError(f"runtime source {logical.logical_name!r} is not relative")
        try:
            candidate = root_resolved.joinpath(source.relative_path)
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root_resolved)
            metadata = resolved.stat()
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"runtime source {logical.logical_name!r} is unavailable or escapes its root"
            ) from exc
        if resolved in resolved_files:
            raise ValueError("runtime artifact aliases one physical file more than once")
        resolved_files.add(resolved)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"runtime source {logical.logical_name!r} is not a regular file")
        if metadata.st_size > _MAX_FILE_BYTES:
            raise ValueError(f"runtime source {logical.logical_name!r} exceeds its byte limit")
        total_bytes += metadata.st_size
        if total_bytes > _MAX_TOTAL_BYTES:
            raise ValueError("runtime artifact exceeds the bounded total byte limit")
        files.append(
            RuntimeArtifactFile(
                logical_name=logical.logical_name,
                role=source.role,
                size_bytes=metadata.st_size,
                sha256=_hash_regular_file(resolved, expected_stat=metadata),
            )
        )
    return EmbeddingRuntimeArtifact(
        provider=provider,
        model=model,
        dimensions=dimensions,
        execution_provider=execution_provider,
        execution_settings=tuple(
            RuntimeExecutionSetting(name=name, value=value)
            for name, value in sorted(execution_settings.items())
        ),
        component_versions=tuple(
            RuntimeComponentVersion(component=name, version=version)
            for name, version in sorted(component_versions.items())
        ),
        files=tuple(files),
    )


def verify_runtime_artifact(
    artifact: EmbeddingRuntimeArtifact,
    root: Path,
    sources: Sequence[RuntimeArtifactSource],
) -> None:
    """Rebuild with the sealed settings and fail closed on any byte or identity drift."""

    rebuilt = build_runtime_artifact(
        root,
        sources,
        provider=artifact.provider,
        model=artifact.model,
        dimensions=artifact.dimensions,
        execution_provider=artifact.execution_provider,
        execution_settings={item.name: item.value for item in artifact.execution_settings},
        component_versions={item.component: item.version for item in artifact.component_versions},
    )
    if rebuilt != artifact:
        raise ValueError("embedding runtime artifact no longer matches local bytes")


def parse_runtime_artifact(raw_json: str, expected_sha256: str) -> EmbeddingRuntimeArtifact:
    """Parse only canonical JSON whose digest and ordering are exact."""

    _sha256(expected_sha256, "runtime artifact sha256")
    artifact = EmbeddingRuntimeArtifact.model_validate_json(raw_json)
    if raw_json != artifact.canonical_json() or artifact.sha256() != expected_sha256:
        raise ValueError("embedding runtime artifact JSON or digest is non-canonical")
    return artifact


def load_runtime_artifact(path: Path) -> EmbeddingRuntimeArtifact:
    """Load a canonical descriptor without exposing its physical location."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise ValueError("embedding runtime artifact file is unavailable") from None
    artifact = EmbeddingRuntimeArtifact.model_validate_json(raw)
    if raw != artifact.canonical_json():
        raise ValueError("embedding runtime artifact file is not canonical JSON")
    return artifact


def _hash_regular_file(path: Path, *, expected_stat: os.stat_result) -> str:
    digest = hashlib.sha256()
    bytes_read = 0
    try:
        with path.open("rb") as handle:
            while block := handle.read(_CHUNK_BYTES):
                bytes_read += len(block)
                if bytes_read > _MAX_FILE_BYTES:
                    raise ValueError("runtime file changed beyond its byte limit while hashing")
                digest.update(block)
        final_stat = path.stat()
    except OSError as exc:
        raise ValueError("runtime file became unavailable while hashing") from exc
    stable_identity = (
        expected_stat.st_size,
        expected_stat.st_mtime_ns,
        getattr(expected_stat, "st_ino", None),
    )
    final_identity = (
        final_stat.st_size,
        final_stat.st_mtime_ns,
        getattr(final_stat, "st_ino", None),
    )
    if bytes_read != expected_stat.st_size or stable_identity != final_identity:
        raise ValueError("runtime file changed while hashing")
    return digest.hexdigest()
