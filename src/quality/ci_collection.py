"""Typed identities and trusted population helpers for CI collection."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CollectionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["ci-paired-collection/v1"] = "ci-paired-collection/v1"
    cohort: Literal["full-suite", "ci-shard"]
    source_shard: int | None = None
    source_shards: int = Field(default=8, ge=1)
    split_count: int = Field(default=1, ge=1)
    split_part: int = Field(default=0, ge=0)
    baseline_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    current_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    measured_repeats: int = Field(default=7, ge=7, le=21)
    max_measured_repeats: int = Field(default=21, ge=7, le=21)
    warmups: int = Field(default=1, ge=1, le=1)
    cache_proof_method: Literal["runner-isolated", "unavailable"] = "unavailable"
    network_proof_method: Literal["runner-isolated", "unavailable"] = "unavailable"
    # Only the repository-owned hermetic fixture is permitted for tests. The
    # production default is deliberately the real capture entrypoint.
    capture_mode: Literal["production", "hermetic"] = "production"
    timing_profile: Literal["stable", "noisy", "max-unstable"] = "stable"
    # Production collection is pinned to these files in the collector
    # checkout. They are optional only for the hermetic unit-test fixture.
    trusted_harness_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trusted_selector_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trusted_plugin_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_test_files: tuple[str, ...] = ()
    expected_node_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def proof_methods_are_honest(self) -> CollectionManifest:
        if self.cache_proof_method != "unavailable" or self.network_proof_method != "unavailable":
            raise ValueError("runner proof acquisition is not implemented by this collector")
        if self.cohort == "full-suite" and self.source_shard is not None:
            raise ValueError("full-suite manifest cannot carry shard coordinates")
        if self.cohort == "ci-shard" and self.source_shard is None:
            raise ValueError("ci-shard manifest requires source_shard")
        if (
            self.cohort == "ci-shard"
            and self.source_shard is not None
            and not 1 <= self.source_shard <= self.source_shards
        ):
            raise ValueError("source_shard is outside source_shards")
        if not 0 <= self.split_part < self.split_count:
            raise ValueError("split_part is outside split_count")
        if len(set(self.expected_test_files)) != len(self.expected_test_files):
            raise ValueError("expected_test_files must be unique")
        if tuple(sorted(self.expected_test_files)) != self.expected_test_files:
            raise ValueError("expected_test_files must be sorted")
        if len(set(self.expected_node_ids)) != len(self.expected_node_ids):
            raise ValueError("expected_node_ids must be unique")
        if tuple(sorted(self.expected_node_ids)) != self.expected_node_ids:
            raise ValueError("expected_node_ids must be sorted")
        expected_files = set(self.expected_test_files)
        if expected_files and any(
            node.split("::", 1)[0] not in expected_files for node in self.expected_node_ids
        ):
            raise ValueError("expected_node_ids must belong to expected_test_files")
        return self


class CollectionState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["ci-paired-state/v1"] = "ci-paired-state/v1"
    manifest_sha256: str
    baseline_completed: int = 0
    current_completed: int = 0
    baseline_warmups_completed: int = 0
    current_warmups_completed: int = 0
    completed_units: tuple[str, ...] = ()
    status: Literal["pending", "interrupted", "complete"] = "pending"
    pairing_status: Literal["pending", "HOLD", "INVALID", "PASS"] = "pending"
    measured_pairs: int = 0
    stop_reason: Literal["pending", "stable", "max-exhausted"] = "pending"
    receipt_paths: tuple[str, ...] = ()
    population_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trusted_harness_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trusted_selector_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trusted_plugin_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    capture_python_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def manifest_digest(manifest: CollectionManifest) -> str:
    return hashlib.sha256(manifest.model_dump_json().encode()).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_python_sha256(value: str) -> str:
    """Attest the capture interpreter before it can run revision code.

    The collector itself is the runtime authority. Allowing a caller to
    substitute another executable would make the receipt's runtime declaration
    meaningless, so the requested path must resolve to the same interpreter
    binary and report the same identity. The digest is then carried through
    state and each raw receipt to detect drift during a resumed collection.
    """
    requested = Path(value).expanduser()
    if not requested.is_absolute():
        located = shutil.which(value)
        if located is None:
            raise SystemExit("capture Python executable is not found")
        requested = Path(located)
    try:
        requested = requested.resolve(strict=True)
        collector = Path(sys.executable).resolve(strict=True)
        requested_digest = file_sha256(requested)
        collector_digest = file_sha256(collector)
    except OSError as exc:
        raise SystemExit("capture Python executable is not readable") from exc
    if not requested.is_file() or requested_digest != collector_digest:
        raise SystemExit("capture Python executable identity is not the collector runtime")
    probe = subprocess.run(
        [
            str(requested),
            "-I",
            "-c",
            (
                "import json, platform, sys; "
                "print(json.dumps({'implementation': platform.python_implementation(), "
                "'version': sys.version.split()[0], 'platform': platform.platform(), "
                "'machine': platform.machine()}))"
            ),
        ],
        capture_output=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
    )
    if probe.returncode != 0:
        raise SystemExit("capture Python runtime identity probe failed")
    try:
        payload = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit("capture Python runtime identity probe was invalid") from exc
    expected = {
        "implementation": platform.python_implementation(),
        "version": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    if payload != expected:
        raise SystemExit("capture Python runtime identity differs from collector runtime")
    return requested_digest


def trusted_paths() -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parents[2]
    return (
        root / "execution" / "capture_test_ci_performance.py",
        root / ".github" / "scripts" / "ci_gate.py",
        root / "src" / "quality" / "pytest_performance_plugin.py",
    )


def revision_test_files(root: Path, revision: str) -> tuple[str, ...]:
    output = subprocess.check_output(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", revision, "--", "tests"],
        text=True,
    )
    return tuple(
        sorted(
            line.strip()
            for line in output.splitlines()
            if line.strip().startswith("tests/")
            and Path(line.strip()).name.startswith("test_")
            and line.strip().endswith(".py")
            and line.strip() != "tests/test_design_computed_canary.py"
        )
    )


def trusted_selected_files(
    files: tuple[str, ...], manifest: CollectionManifest, selector: Path
) -> tuple[str, ...]:
    if manifest.cohort == "full-suite":
        return files
    if manifest.source_shard is None:
        raise ValueError("ci-shard manifest requires source_shard")
    selected = subprocess.run(
        [
            sys.executable,
            str(selector),
            "select-tests",
            "--source-shard",
            str(manifest.source_shard),
            "--source-shards",
            str(manifest.source_shards),
            "--split-count",
            str(manifest.split_count),
            "--split-part",
            str(manifest.split_part),
        ],
        input=("\n".join(files) + "\n").encode(),
        capture_output=True,
        check=False,
        cwd=selector.parents[2],
    )
    if selected.returncode != 0:
        raise SystemExit("trusted CI shard selection failed")
    return tuple(line for line in selected.stdout.decode().splitlines() if line)


def population(
    root: Path, manifest: CollectionManifest, selector: Path
) -> tuple[dict[str, object], str]:
    baseline_files = revision_test_files(root, manifest.baseline_revision)
    current_files = revision_test_files(root, manifest.current_revision)
    if baseline_files != current_files:
        raise SystemExit("baseline/current test-file populations differ")
    selected = trusted_selected_files(baseline_files, manifest, selector)
    if manifest.expected_test_files and tuple(manifest.expected_test_files) != selected:
        raise SystemExit("manifest expected test-file population differs from trusted selector")
    expected_nodes = tuple(manifest.expected_node_ids)
    if manifest.capture_mode == "production" and not expected_nodes:
        raise SystemExit("production manifest requires an exact expected node population")
    if any(node.split("::", 1)[0] not in set(selected) for node in expected_nodes):
        raise SystemExit("manifest expected node population differs from trusted selector")
    payload: dict[str, object] = {
        "schema_version": "ci-population/v1",
        "test_files": list(selected),
        "node_ids": list(expected_nodes),
        "baseline_revision": manifest.baseline_revision,
        "current_revision": manifest.current_revision,
        "selector_sha256": file_sha256(selector),
    }
    if manifest.cohort == "ci-shard":
        if manifest.source_shard is None:
            raise SystemExit("ci-shard manifest requires source_shard")
        payload.update(
            {
                "source_shard": manifest.source_shard,
                "source_shards": manifest.source_shards,
                "split_count": manifest.split_count,
                "split_part": manifest.split_part,
            }
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return payload, hashlib.sha256(encoded).hexdigest()
