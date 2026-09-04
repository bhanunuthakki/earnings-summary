"""Raw, fail-closed evidence for pytest/CI performance runs.

This module deliberately does not score performance. It records an attributable,
single-run observation for a later paired evaluator. A raw receipt can therefore
never claim complete evidence by itself.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ExecutionOutcome = Literal["passed", "failed", "cancelled", "not_run"]
EvidenceStatus = Literal["hold", "invalid"]
CacheState = Literal["cold", "warm", "unknown"]
CohortKind = Literal["full-suite", "ci-shard"]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def node_identity(node_ids: tuple[str, ...]) -> str:
    return _sha256("\n".join(sorted(node_ids)).encode())


class FrozenTestCohort(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: CohortKind
    config_version: str = "test-ci-performance/v2"
    source_shard: int | None = None
    source_shards: int | None = None
    split_count: int | None = None
    split_part: int | None = None
    test_files: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> FrozenTestCohort:
        if len(set(self.test_files)) != len(self.test_files):
            raise ValueError("test_files must be unique")
        if tuple(sorted(self.test_files)) != self.test_files:
            raise ValueError("test_files must be sorted")
        shard_fields = (self.source_shard, self.source_shards, self.split_count, self.split_part)
        if self.kind == "full-suite":
            if any(value is not None for value in shard_fields):
                raise ValueError("full-suite cohorts cannot carry shard coordinates")
            return self
        if any(value is None for value in shard_fields):
            raise ValueError("ci-shard cohorts require all shard coordinates")
        assert self.source_shard is not None
        assert self.source_shards is not None
        assert self.split_count is not None
        assert self.split_part is not None
        if self.source_shards < 1 or not 1 <= self.source_shard <= self.source_shards:
            raise ValueError("source_shard must be within source_shards")
        if self.split_count < 1 or not 0 <= self.split_part < self.split_count:
            raise ValueError("split_part must be within split_count")
        return self


class TestCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    skipped: int = Field(ge=0)
    xfailed: int = Field(ge=0)
    xpassed: int = Field(ge=0)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped + self.xfailed + self.xpassed


class PhaseTimings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    collection_seconds: float = Field(ge=0)
    setup_seconds: float = Field(ge=0)
    call_seconds: float = Field(ge=0)
    teardown_seconds: float = Field(ge=0)
    migrated_db_template_build_seconds: float = Field(default=0, ge=0)
    migrated_db_template_copy_seconds: float = Field(default=0, ge=0)
    migrated_db_template_builds: int = Field(default=0, ge=0)
    migrated_db_template_copies: int = Field(default=0, ge=0)


class WorkerEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "test-ci-performance-worker/v2"
    worker_id: str = Field(min_length=1)
    node_ids: tuple[str, ...]
    node_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    counts: TestCounts
    timings: PhaseTimings
    elapsed_seconds: float = Field(ge=0)
    peak_rss_bytes: int | None = Field(default=None, ge=0)
    cache_state: CacheState = "unknown"

    @model_validator(mode="after")
    def validate_nodes(self) -> WorkerEvidence:
        if not self.node_ids:
            raise ValueError("worker evidence must own at least one executed node")
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("worker node_ids must be unique")
        if tuple(sorted(self.node_ids)) != self.node_ids:
            raise ValueError("worker node_ids must be sorted")
        if self.node_id_sha256 != node_identity(self.node_ids):
            raise ValueError("node_id_sha256 does not match node_ids")
        if self.counts.total != len(self.node_ids):
            raise ValueError("outcome counts must equal executed node count")
        return self


class ArtifactIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    python_version: str
    python_implementation: str
    platform: str
    machine: str
    cpu_count: int | None = Field(default=None, ge=1)
    pytest_version: str
    xdist_version: str
    worker_count: int = Field(ge=1)


class TestCIPerformanceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "test-ci-performance/v2"
    attempt_id: str = Field(min_length=1)
    cohort: FrozenTestCohort
    source_sha256: str | None
    config_sha256: str | None
    configuration: tuple[ArtifactIdentity, ...]
    runtime: RuntimeIdentity
    cohort_sha256: str
    command_sha256: str | None = None
    execution_outcome: ExecutionOutcome
    evidence_status: EvidenceStatus
    hold_reasons: tuple[str, ...]
    workers: tuple[WorkerEvidence, ...]
    cache_state: CacheState
    paired: Literal[False] = False
    network_isolation: Literal["requested-not-proven"] = "requested-not-proven"
    cache_evidence: Literal["declared-only"] = "declared-only"
    output_sha256: str | None = None
    process_wall_seconds: float | None = Field(default=None, ge=0)
    process_peak_rss_bytes: int | None = Field(default=None, ge=0)


def cohort_identity(cohort: FrozenTestCohort) -> str:
    return _sha256(cohort.model_dump_json().encode())


def source_identity(repo_root: Path) -> str | None:
    """Hash tracked and untracked source inputs, excluding ignored artifacts."""
    try:
        names = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "src",
                "execution",
                "tests",
            ],
        ).split(b"\0")
        parts: list[bytes] = []
        for raw in sorted(name for name in names if name):
            path = repo_root / os.fsdecode(raw)
            if path.is_file():
                parts.append(raw + b"\0" + _sha256(path.read_bytes()).encode() + b"\n")
        return _sha256(b"".join(parts)) if parts else None
    except (OSError, subprocess.SubprocessError):
        return None


_CONFIG_PATHS = (
    "pyproject.toml",
    "requirements.lock",
    "tests/conftest.py",
    ".github/workflows/ci.yml",
)


def configuration_identities(repo_root: Path) -> tuple[ArtifactIdentity, ...]:
    identities: list[ArtifactIdentity] = []
    for name in _CONFIG_PATHS:
        path = repo_root / name
        try:
            identities.append(ArtifactIdentity(path=name, sha256=_sha256(path.read_bytes())))
        except OSError:
            continue
    return tuple(identities)


def config_identity(configuration: tuple[ArtifactIdentity, ...]) -> str | None:
    if not configuration:
        return None
    payload = b"".join(
        item.path.encode() + b"\0" + item.sha256.encode() + b"\n" for item in configuration
    )
    return _sha256(payload)


def runtime_identity(*, worker_count: int) -> RuntimeIdentity:
    def version(distribution: str) -> str:
        try:
            return importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            return "unavailable"

    return RuntimeIdentity(
        python_version=sys.version.split()[0],
        python_implementation=platform.python_implementation(),
        platform=platform.platform(),
        machine=platform.machine(),
        cpu_count=os.cpu_count(),
        pytest_version=version("pytest"),
        xdist_version=version("pytest-xdist"),
        worker_count=worker_count,
    )


def receipt_from_fragments(
    repo_root: str | Path,
    cohort: FrozenTestCohort,
    fragments: list[WorkerEvidence],
    *,
    attempt_id: str,
    execution_outcome: ExecutionOutcome,
    cache_state: CacheState = "unknown",
    fragment_errors: tuple[str, ...] = (),
    worker_count: int = 2,
) -> TestCIPerformanceReceipt:
    reasons = ["single raw run is unpaired; paired evidence is required"]
    invalid = bool(fragment_errors)
    reasons.extend(fragment_errors)
    if not fragments:
        reasons.append("no valid worker evidence fragments were collected")
        invalid = True
    if execution_outcome != "passed":
        reasons.append(f"execution outcome is {execution_outcome}")
    worker_ids = [worker.worker_id for worker in fragments]
    if len(set(worker_ids)) != len(worker_ids):
        reasons.append("worker identifiers are not unique")
        invalid = True
    if len(fragments) != worker_count:
        reasons.append("worker fragment count does not match declared runtime worker count")
        invalid = True
    seen_nodes: set[str] = set()
    overlap: set[str] = set()
    selected_files = set(cohort.test_files)
    unexpected_files: set[str] = set()
    for worker in fragments:
        worker_nodes = set(worker.node_ids)
        overlap.update(seen_nodes.intersection(worker_nodes))
        seen_nodes.update(worker_nodes)
        for node_id in worker.node_ids:
            node_file = node_id.split("::", 1)[0]
            if node_file not in selected_files:
                unexpected_files.add(node_file)
    if overlap:
        reasons.append("worker node ownership overlaps")
        invalid = True
    if unexpected_files:
        reasons.append("worker evidence contains nodes outside the frozen cohort")
        invalid = True
    executed_files = {node_id.split("::", 1)[0] for node_id in seen_nodes}
    if execution_outcome == "passed" and executed_files != selected_files:
        reasons.append("passed run does not cover every file in the frozen cohort")
        invalid = True
    if not cohort.test_files:
        reasons.append("frozen cohort contains no test files")
        invalid = True
    if any(worker.cache_state != cache_state for worker in fragments):
        reasons.append("worker cache_state disagrees with declared run cache_state")
        invalid = True
    if cache_state == "unknown":
        reasons.append("cache_state is unknown")
    configuration = configuration_identities(Path(repo_root).resolve())
    present_config = {item.path for item in configuration}
    missing_config = set(_CONFIG_PATHS).difference(present_config)
    if missing_config:
        reasons.append("required configuration identities are missing")
        invalid = True
    source_sha256 = source_identity(Path(repo_root).resolve())
    if source_sha256 is None:
        reasons.append("source identity is unavailable")
        invalid = True
    return TestCIPerformanceReceipt(
        attempt_id=attempt_id,
        cohort=cohort,
        source_sha256=source_sha256,
        config_sha256=config_identity(configuration),
        configuration=configuration,
        runtime=runtime_identity(worker_count=worker_count),
        cohort_sha256=cohort_identity(cohort),
        execution_outcome=execution_outcome,
        evidence_status="invalid" if invalid else "hold",
        hold_reasons=tuple(reasons),
        workers=tuple(fragments),
        cache_state=cache_state,
    )


def write_receipt(receipt: TestCIPerformanceReceipt, destination: str | Path) -> None:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
