"""BHA-147 exact-subject evidence collection (Phase A)."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import cast

from quality.evidence_bundle_io import (
    Runner,
    verify_staged_bytes,
)
from quality.evidence_bundle_io import (
    atomic_write as _atomic_write,
)
from quality.evidence_bundle_io import (
    default_runner as _default_runner,
)
from quality.evidence_bundle_io import (
    extract_embedded_subject as _extract_embedded_subject,
)
from quality.evidence_bundle_io import (
    extract_schema as _extract_schema,
)
from quality.evidence_bundle_io import (
    generator_blob_hash as _generator_blob_hash,
)
from quality.evidence_bundle_io import (
    git_staging_problems as _git_staging_problems,
)
from quality.evidence_bundle_io import (
    install_handoff as _install_handoff,
)
from quality.evidence_bundle_io import (
    manifest_integrity as _manifest_integrity,
)
from quality.evidence_bundle_io import (
    reject_duplicate_keys as _reject_duplicate_keys,
)
from quality.evidence_bundle_io import (
    snapshot_subject as _snapshot_subject,
)
from quality.evidence_bundle_io import (
    staging_path as _staging_path,
)
from quality.evidence_bundle_models import (
    COLLECTION_SCHEMA,
    ArtifactCollectionStatus,
    ArtifactRecord,
    ArtifactSpec,
    CollectionManifest,
    bound_violations,
)
from quality.evidence_bundle_models import (
    HEX40_RE as _HEX40_RE,
)
from quality.evidence_path_policy import FREEZE_PATH

__all__ = (
    "collect_evidence",
    "default_artifact_specs",
    "load_collection_manifest",
    "verify_staged_bytes",
)


def default_artifact_specs() -> tuple[ArtifactSpec, ...]:
    py = sys.executable
    return (
        ArtifactSpec(
            artifact_id="architecture",
            canonical_path="docs/quality/architecture-ratchet.json",
            generator_path="src/quality/architecture.py",
            generator_version="architecture-measurement-v1",
            command=(py, "src/quality/architecture.py", "--revision", "WORKTREE"),
            native_scope="WORKTREE",
            output_flag="--output",
            accepted_exit_codes=(0, 2),
        ),
        ArtifactSpec(
            artifact_id="duplicates",
            canonical_path="docs/quality/duplicates-ratchet.json",
            generator_path="src/quality/duplicates.py",
            generator_version="python-ast-normalized-v1",
            command=(py, "src/quality/duplicates.py", "--revision", "WORKTREE"),
            native_scope="WORKTREE",
            output_flag="--out",
            accepted_exit_codes=(0, 2),
        ),
        ArtifactSpec(
            artifact_id="lifecycle",
            canonical_path="docs/quality/lifecycle-inventory.json",
            generator_path="execution/classify_operational_lifecycle.py",
            generator_version="lifecycle-inventory-v1",
            command=(
                py,
                "execution/classify_operational_lifecycle.py",
                "--repo-root",
                ".",
            ),
            native_scope="WORKTREE",
            output_flag="--output",
            accepted_exit_codes=(0, 2),
            depends_on=("reachability",),
        ),
        ArtifactSpec(
            artifact_id="performance",
            canonical_path="docs/quality/performance-baseline.json",
            generator_path="execution/capture_performance_baseline.py",
            generator_version="performance-baseline-v1",
            command=(
                py,
                "execution/capture_performance_baseline.py",
                "--command",
                shlex.join((py, "-c", "print('ok')")),
            ),
            native_scope="WORKTREE",
            output_flag="--output",
            accepted_exit_codes=(0, 2),
        ),
        ArtifactSpec(
            artifact_id="reachability",
            canonical_path="docs/quality/reachability-check.json",
            generator_path="src/quality/reachability.py",
            generator_version="1.2.1",
            command=(py, "src/quality/reachability.py", "--repo-root", "."),
            native_scope="WORKTREE",
            output_flag="--output",
            accepted_exit_codes=(0, 2),
            handoff_path=".tmp/quality/reachability-check.json",
        ),
        ArtifactSpec(
            artifact_id="reconciliation",
            canonical_path="docs/quality/roadmap-reconciliation.json",
            generator_path="execution/reconcile_quality_baseline.py",
            generator_version="roadmap-reconciliation-v1",
            command=(py, "execution/reconcile_quality_baseline.py", "--subject-root", "."),
            native_scope="WORKTREE",
            output_flag="--output",
            accepted_exit_codes=(0, 2),
            depends_on=("architecture", "duplicates", "reachability", "static", "test_db"),
            input_manifest_flag="--staged-manifest",
            roadmap_context_path="docs/quality/quality-9plus-roadmap.md",
        ),
        ArtifactSpec(
            artifact_id="static",
            canonical_path="docs/quality/static-baseline.json",
            generator_path="src/quality/static_quality.py",
            generator_version="bha-120.v3",
            command=(py, "src/quality/static_quality.py", "--repo-root", "."),
            native_scope="WORKTREE",
            output_flag="--output",
            accepted_exit_codes=(0, 2),
        ),
        ArtifactSpec(
            artifact_id="test_db",
            canonical_path="docs/quality/test-db-patterns-baseline.json",
            generator_path="execution/audit_test_db_patterns.py",
            generator_version="test-db-patterns-v1",
            command=(py, "execution/audit_test_db_patterns.py", "--root", "."),
            native_scope="WORKTREE",
            output_flag="--output",
            accepted_exit_codes=(0, 2),
        ),
        ArtifactSpec(
            artifact_id="roadmap_freeze",
            canonical_path=FREEZE_PATH,
            generator_path="execution/freeze_quality_roadmap.py",
            generator_version="roadmap-freeze-index/v1",
            command=(py, "execution/freeze_quality_roadmap.py", "--repo-root", "."),
            native_scope="WORKTREE",
            output_flag="--output",
            input_manifest_flag="--input-manifest",
            accepted_exit_codes=(0, 2),
            depends_on=(
                "architecture",
                "duplicates",
                "lifecycle",
                "performance",
                "reachability",
                "reconciliation",
                "static",
                "test_db",
            ),
        ),
    )


def _producer_output_path(staging: Path, artifact_id: str) -> Path:
    safe = artifact_id.replace("/", "-").replace("\\", "-")
    return staging / f"{safe}.out"


def _input_manifest_path(staging: Path, artifact_id: str) -> Path:
    safe = artifact_id.replace("/", "-").replace("\\", "-")
    return staging / f"{safe}.inputs.json"


def _roadmap_context_handoff(staging: Path) -> Path:
    return staging / "roadmap.context.md"


def _roadmap_claims_handoff(staging: Path) -> Path:
    return staging / "roadmap.claims.json"


_ROADMAP_CLAIMS_PATH = "config/quality_roadmap_claims.json"


def _preflight_input_file(path: Path) -> str | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return "unable to inspect input handoff"
    if stat.S_ISLNK(st.st_mode):
        return "input handoff is symlink"
    if stat.S_ISDIR(st.st_mode):
        return "input handoff is directory"
    if not stat.S_ISREG(st.st_mode):
        return "input handoff is non-regular"
    if st.st_nlink != 1:
        return "input handoff is hard-linked"
    return None


def _input_manifest_bytes(
    root: Path,
    staging: Path,
    spec: ArtifactSpec,
    raw_by_id: dict[str, bytes],
    subject_commit: str,
) -> tuple[bytes, tuple[tuple[Path, tuple[int, int, int] | None], ...]] | None:
    if spec.input_manifest_flag is None:
        return None
    entries: dict[str, dict[str, str]] = {}
    for dep in spec.depends_on:
        data = raw_by_id.get(dep)
        if data is None:
            return None
        entries[dep] = {
            "path": f"{dep}.raw",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    snapshots: list[tuple[Path, tuple[int, int, int] | None]] = []
    if spec.roadmap_context_path is not None:
        context = root / spec.roadmap_context_path
        try:
            context_stat = os.lstat(context)
            context_resolved = context.resolve()
            if (
                stat.S_ISLNK(context_stat.st_mode)
                or not stat.S_ISREG(context_stat.st_mode)
                or context_stat.st_nlink != 1
                or context_resolved != context
            ):
                return None
            if (
                _default_runner(
                    ("git", "ls-files", "--error-unmatch", "--", spec.roadmap_context_path), root
                ).returncode
                != 0
            ):
                return None
            expected_context: bytes = _default_runner(
                ("git", "show", f"{subject_commit}:{spec.roadmap_context_path}"), root
            ).stdout
            if context.read_bytes() != expected_context:
                return None
            context_after = os.lstat(context)
            context_signature = (
                context_stat.st_dev,
                context_stat.st_ino,
                context_stat.st_ctime_ns,
            )
            if (context_after.st_dev, context_after.st_ino) != (
                context_stat.st_dev,
                context_stat.st_ino,
            ) or context.read_bytes() != expected_context:
                return None
            handoff = _roadmap_context_handoff(staging)
            _atomic_write(handoff, expected_context)
        except OSError:
            return None
        entries["roadmap"] = {
            "path": handoff.name,
            "sha256": hashlib.sha256(expected_context).hexdigest(),
        }
        snapshots.append((context, context_signature))
    if spec.roadmap_context_path is not None:
        claims = root / _ROADMAP_CLAIMS_PATH
        try:
            claims_result = _default_runner(
                ("git", "show", f"{subject_commit}:{_ROADMAP_CLAIMS_PATH}"), root
            )
            if claims_result.returncode == 0:
                claims_bytes = claims_result.stdout
                claims_stat = None
                try:
                    claims_stat = os.lstat(claims)
                    claims_resolved = claims.resolve()
                    if (
                        stat.S_ISLNK(claims_stat.st_mode)
                        or not stat.S_ISREG(claims_stat.st_mode)
                        or claims_stat.st_nlink != 1
                        or claims_resolved != claims
                        or claims.read_bytes() != claims_bytes
                    ):
                        return None
                except FileNotFoundError:
                    if claims.resolve() != claims:
                        return None
                except OSError:
                    return None
                _atomic_write(_roadmap_claims_handoff(staging), claims_bytes)
                entries["roadmap_claims"] = {
                    "path": _roadmap_claims_handoff(staging).name,
                    "sha256": hashlib.sha256(claims_bytes).hexdigest(),
                }
                if claims_stat is not None:
                    snapshots.append(
                        (
                            claims,
                            (claims_stat.st_dev, claims_stat.st_ino, claims_stat.st_ctime_ns),
                        )
                    )
        except OSError:
            return None
    return (
        (json.dumps(entries, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        tuple(snapshots),
    )


def _verify_input_manifest_inputs(
    staging: Path,
    manifest_path: Path,
    manifest_bytes: bytes,
    raw_by_id: dict[str, bytes],
    context_snapshots: tuple[tuple[Path, tuple[int, int, int] | None], ...] = (),
) -> str | None:
    try:
        manifest_stat = os.lstat(manifest_path)
        if (
            stat.S_ISLNK(manifest_stat.st_mode)
            or not stat.S_ISREG(manifest_stat.st_mode)
            or manifest_stat.st_nlink != 1
            or manifest_path.resolve().parent != staging.resolve()
            or manifest_path.read_bytes() != manifest_bytes
        ):
            return "input manifest changed during collection"
        for context_path, context_signature in context_snapshots:
            try:
                context_stat = os.lstat(context_path)
            except FileNotFoundError:
                return "roadmap context changed during collection"
            if (
                stat.S_ISLNK(context_stat.st_mode)
                or not stat.S_ISREG(context_stat.st_mode)
                or context_stat.st_nlink != 1
                or context_signature is None
                or (context_stat.st_dev, context_stat.st_ino, context_stat.st_ctime_ns)
                != context_signature
            ):
                return "roadmap context changed during collection"
        payload: object = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            return "input manifest changed during collection"
        seen: set[tuple[int, int]] = set()
        raw_payload = cast(dict[object, object], payload)
        for entry_value in raw_payload.values():
            entry: object = entry_value
            if not isinstance(entry, dict):
                return "input manifest changed during collection"
            raw_entry = cast(dict[object, object], entry)
            rel: object = raw_entry.get("path")
            digest: object = raw_entry.get("sha256")
            if not isinstance(rel, str) or not isinstance(digest, str):
                return "input manifest changed during collection"
            path = staging / rel
            artifact_id = Path(rel).stem
            expected = raw_by_id.get(artifact_id)
            if expected is None and rel in (
                _roadmap_context_handoff(staging).name,
                _roadmap_claims_handoff(staging).name,
            ):
                expected = path.read_bytes()
            st = os.lstat(path)
            if (
                stat.S_ISLNK(st.st_mode)
                or not stat.S_ISREG(st.st_mode)
                or st.st_nlink != 1
                or (st.st_dev, st.st_ino) in seen
                or expected is None
                or path.read_bytes() != expected
                or hashlib.sha256(expected).hexdigest() != digest
            ):
                return f"input dependency changed during collection: {rel}"
            seen.add((st.st_dev, st.st_ino))
    except OSError:
        return "input dependency unavailable during collection"
    return None


def _preflight_output(path: Path) -> str | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return "unable to inspect producer output"
    if stat.S_ISLNK(st.st_mode):
        return "producer output is symlink"
    if stat.S_ISDIR(st.st_mode):
        return "producer output is directory"
    if not stat.S_ISREG(st.st_mode):
        return "producer output is non-regular"
    if st.st_nlink != 1:
        return "producer output is hard-linked"
    try:
        os.unlink(path)
    except OSError:
        return "unable to remove producer output"
    return None


def _read_producer_output(path: Path, staging_resolved: Path) -> tuple[bytes | None, str | None]:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, "missing producer output"
    except OSError:
        return None, "unable to inspect producer output"
    if stat.S_ISLNK(st.st_mode):
        return None, "producer output is symlink"
    if stat.S_ISDIR(st.st_mode):
        return None, "producer output is directory"
    if not stat.S_ISREG(st.st_mode):
        return None, "producer output is non-regular"
    if st.st_nlink != 1:
        return None, "producer output is hard-linked"
    try:
        resolved = path.resolve()
        if resolved.parent != staging_resolved:
            return None, "producer output outside staging"
    except OSError:
        return None, "unable to resolve producer output"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd1 = os.open(path, flags)
    except OSError:
        return None, "producer output is symlink"
    try:
        try:
            st1 = os.fstat(fd1)
        except OSError:
            return None, "unable to read producer output"
        if not stat.S_ISREG(st1.st_mode) or st1.st_nlink != 1:
            return None, "unstable producer output"
        if (st1.st_dev, st1.st_ino) != (st.st_dev, st.st_ino):
            return None, "unstable producer output"
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd1, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        data1 = b"".join(chunks)
    finally:
        with suppress(OSError):
            os.close(fd1)
    try:
        fd2 = os.open(path, flags)
    except OSError:
        return None, "unstable producer output"
    try:
        try:
            st_fd2 = os.fstat(fd2)
        except OSError:
            return None, "unstable producer output"
        if not stat.S_ISREG(st_fd2.st_mode) or st_fd2.st_nlink != 1:
            return None, "unstable producer output"
        if (st_fd2.st_dev, st_fd2.st_ino) != (st.st_dev, st.st_ino):
            return None, "unstable producer output"
        chunks2: list[bytes] = []
        while True:
            chunk = os.read(fd2, 65536)
            if not chunk:
                break
            chunks2.append(chunk)
        data2 = b"".join(chunks2)
    except OSError:
        return None, "unstable producer output"
    finally:
        with suppress(OSError):
            os.close(fd2)
    try:
        st2 = os.lstat(path)
    except OSError:
        return None, "unstable producer output"
    if (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino):
        return None, "unstable producer output"
    if not stat.S_ISREG(st2.st_mode) or st2.st_nlink != 1:
        return None, "unstable producer output"
    if data1 != data2:
        return None, "unstable producer output"
    return data1, None


def _ordered_specs(specs: Sequence[ArtifactSpec]) -> list[ArtifactSpec]:
    by_id: dict[str, ArtifactSpec] = {s.artifact_id: s for s in specs}
    visited: dict[str, str] = {}
    ordered: list[ArtifactSpec] = []

    def visit(artifact_id: str) -> None:
        state = visited.get(artifact_id)
        if state == "done":
            return
        if state == "visiting":
            raise ValueError(f"dependency cycle: {artifact_id}")
        visited[artifact_id] = "visiting"
        spec = by_id[artifact_id]
        for dep in spec.depends_on:
            if dep not in by_id:
                raise ValueError(f"unknown dependency: {dep}")
            visit(dep)
        visited[artifact_id] = "done"
        ordered.append(spec)

    for artifact_id in sorted(by_id):
        visit(artifact_id)
    return ordered


def collect_evidence(
    repo_root: Path,
    staging_dir: Path,
    specs: Sequence[ArtifactSpec],
    runner: Runner | None = None,
) -> CollectionManifest:
    root = repo_root.resolve()
    staging = staging_dir.resolve() if staging_dir.is_absolute() else (root / staging_dir).resolve()
    violations: list[str] = []
    try:
        rel_staging = staging.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("staging escapes repository") from exc
    if not rel_staging.startswith(".tmp/"):
        raise ValueError("staging must live under ignored .tmp/")
    staging_names: list[str] = []
    for _spec in specs:
        staging_names.append(f"{_spec.artifact_id}.raw")
        if _spec.output_flag is not None:
            staging_names.append(f"{_spec.artifact_id}.out")
        if _spec.input_manifest_flag is not None:
            staging_names.append(f"{_spec.artifact_id}.inputs.json")
        if _spec.roadmap_context_path is not None:
            staging_names.append(_roadmap_context_handoff(staging).name)
            staging_names.append(_roadmap_claims_handoff(staging).name)
    _staging_problems = _git_staging_problems(root, rel_staging, staging_names)
    if _staging_problems:
        raise ValueError(f"unsafe staging configuration: {'; '.join(_staging_problems)}")
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError("unable to create staging") from exc
    if len(specs) == 0 or len(specs) > 64:
        raise ValueError("spec count out of bounds")
    ids = [s.artifact_id for s in specs]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate artifact_id")
    cpaths = [s.canonical_path for s in specs]
    if len(set(cpaths)) != len(cpaths):
        raise ValueError("duplicate canonical_path")
    handoffs = [s.handoff_path for s in specs if s.handoff_path is not None]
    if len(set(handoffs)) != len(handoffs):
        raise ValueError("duplicate handoff_path")
    ordered = _ordered_specs(specs)
    before = _snapshot_subject(root)
    if before is None:
        before_clean = False
        before_commit = "0" * 40
        before_tree = "0" * 40
        violations.append("git identity is unavailable before collection")
    else:
        before_clean = before.clean
        before_commit = before.commit
        before_tree = before.tree
        if not before.clean:
            violations.append("git worktree is dirty before collection")
    run = runner if runner is not None else _default_runner
    try:
        staging_resolved = staging.resolve()
    except OSError:
        staging_resolved = staging
    staged: list[
        tuple[ArtifactSpec, bytes, ArtifactCollectionStatus, int | None, tuple[str, ...]]
    ] = []
    per_status: dict[str, ArtifactCollectionStatus] = {}
    raw_by_id: dict[str, bytes] = {}
    input_manifests: dict[
        str,
        tuple[
            Path,
            bytes,
            dict[str, bytes],
            tuple[tuple[Path, tuple[int, int, int] | None], ...],
        ],
    ] = {}
    for spec in ordered:
        if any(per_status.get(dep) != "collected" for dep in spec.depends_on):
            violations.append(f"unsatisfied dependency: {spec.artifact_id}")
            hold_dep: ArtifactCollectionStatus = "hold"
            per_status[spec.artifact_id] = hold_dep
            staged.append((spec, b"", hold_dep, None, spec.command))
            continue
        actual_command = spec.command
        if spec.input_manifest_flag is not None:
            dependency_bytes = {dep: raw_by_id[dep] for dep in spec.depends_on if dep in raw_by_id}
            manifest_path = _input_manifest_path(staging, spec.artifact_id)
            input_file_problem = _preflight_input_file(manifest_path)
            if input_file_problem is not None:
                violations.append(f"{input_file_problem}: {spec.artifact_id}")
                hold_input_file: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = hold_input_file
                staged.append((spec, b"", hold_input_file, None, actual_command))
                continue
            if spec.roadmap_context_path is not None:
                context_problem = _preflight_input_file(_roadmap_context_handoff(staging))
                if context_problem is not None:
                    violations.append(f"{context_problem}: {spec.artifact_id}")
                    hold_context_file: ArtifactCollectionStatus = "hold"
                    per_status[spec.artifact_id] = hold_context_file
                    staged.append((spec, b"", hold_context_file, None, actual_command))
                    continue
                claims_problem = _preflight_input_file(_roadmap_claims_handoff(staging))
                if claims_problem is not None:
                    violations.append(f"{claims_problem}: {spec.artifact_id}")
                    hold_claims_file: ArtifactCollectionStatus = "hold"
                    per_status[spec.artifact_id] = hold_claims_file
                    staged.append((spec, b"", hold_claims_file, None, actual_command))
                    continue
            prepared_inputs = _input_manifest_bytes(root, staging, spec, raw_by_id, before_commit)
            if prepared_inputs is None or len(dependency_bytes) != len(spec.depends_on):
                violations.append(f"unable to prepare input manifest: {spec.artifact_id}")
                hold_manifest: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = hold_manifest
                staged.append((spec, b"", hold_manifest, None, actual_command))
                continue
            manifest_bytes, context_snapshots = prepared_inputs
            try:
                _atomic_write(manifest_path, manifest_bytes)
            except OSError:
                violations.append(f"unable to write input manifest: {spec.artifact_id}")
                hold_manifest_write: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = hold_manifest_write
                staged.append((spec, b"", hold_manifest_write, None, actual_command))
                continue
            input_manifests[spec.artifact_id] = (
                manifest_path,
                manifest_bytes,
                dependency_bytes,
                context_snapshots,
            )
            actual_command = (*actual_command, spec.input_manifest_flag, str(manifest_path))
        if spec.input_manifest_flag is not None:
            input_problem: str | None = None
            for (
                manifest_path,
                manifest_bytes,
                dependencies,
                context_snapshots,
            ) in input_manifests.values():
                input_problem = _verify_input_manifest_inputs(
                    staging,
                    manifest_path,
                    manifest_bytes,
                    dependencies,
                    context_snapshots,
                )
                if input_problem is not None:
                    break
            if input_problem is not None:
                violations.append(f"{input_problem}: {spec.artifact_id}")
                hold_input: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = hold_input
                staged.append((spec, b"", hold_input, None, actual_command))
                continue
        if spec.output_flag is not None:
            out_path = _producer_output_path(staging, spec.artifact_id)
            problem = _preflight_output(out_path)
            if problem is not None:
                violations.append(f"{problem}: {spec.artifact_id}")
                status: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = status
                staged.append((spec, b"", status, None, actual_command))
                continue
            argv = (*actual_command, spec.output_flag, str(out_path))
            actual_command = argv
            try:
                result = run(argv, root)
            except (OSError, RuntimeError, ValueError) as exc:
                violations.append(f"producer failed: {spec.artifact_id}: {type(exc).__name__}")
                failed: ArtifactCollectionStatus = "failed"
                per_status[spec.artifact_id] = failed
                staged.append((spec, b"", failed, None, actual_command))
                continue
            code = int(result.returncode)
            if code not in spec.accepted_exit_codes:
                violations.append(f"producer exit {result.returncode}: {spec.artifact_id}")
                hold: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = hold
                staged.append((spec, b"", hold, code, actual_command))
                continue
            data, err = _read_producer_output(out_path, staging_resolved)
            if err is not None:
                violations.append(f"{err}: {spec.artifact_id}")
                hold_err: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = hold_err
                staged.append((spec, b"", hold_err, code, actual_command))
                continue
            assert data is not None
            try:
                _atomic_write(_staging_path(staging, spec.artifact_id), data)
            except OSError:
                violations.append(f"unable to write staging: {spec.artifact_id}")
                status_write: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = status_write
                staged.append((spec, data, status_write, code, actual_command))
                continue
            if spec.handoff_path is not None:
                try:
                    _install_handoff(root, spec.handoff_path, data)
                except (OSError, RuntimeError, ValueError) as exc:
                    violations.append(f"handoff failed: {spec.artifact_id}: {type(exc).__name__}")
                    hold_hand: ArtifactCollectionStatus = "hold"
                    per_status[spec.artifact_id] = hold_hand
                    staged.append((spec, data, hold_hand, code, actual_command))
                    continue
            collected: ArtifactCollectionStatus = "collected"
            per_status[spec.artifact_id] = collected
            raw_by_id[spec.artifact_id] = data
            staged.append((spec, data, collected, code, actual_command))
        else:
            try:
                result = run(actual_command, root)
                stdout = result.stdout
                code = int(result.returncode)
                if not isinstance(stdout, bytes):
                    violations.append(f"producer returned non-bytes: {spec.artifact_id}")
                    failed_stdout: ArtifactCollectionStatus = "failed"
                    per_status[spec.artifact_id] = failed_stdout
                    staged.append((spec, b"", failed_stdout, code, actual_command))
                elif code not in spec.accepted_exit_codes:
                    violations.append(f"producer exit {result.returncode}: {spec.artifact_id}")
                    hold_stdout: ArtifactCollectionStatus = "hold"
                    per_status[spec.artifact_id] = hold_stdout
                    staged.append((spec, b"", hold_stdout, code, actual_command))
                else:
                    if spec.handoff_path is not None:
                        try:
                            _install_handoff(root, spec.handoff_path, stdout)
                        except (OSError, RuntimeError, ValueError) as exc:
                            violations.append(
                                f"handoff failed: {spec.artifact_id}: {type(exc).__name__}"
                            )
                            hold_hand_stdout: ArtifactCollectionStatus = "hold"
                            per_status[spec.artifact_id] = hold_hand_stdout
                            staged.append((spec, stdout, hold_hand_stdout, code, actual_command))
                            continue
                    collected_stdout: ArtifactCollectionStatus = "collected"
                    per_status[spec.artifact_id] = collected_stdout
                    try:
                        _atomic_write(_staging_path(staging, spec.artifact_id), stdout)
                    except OSError:
                        violations.append(f"unable to write staging: {spec.artifact_id}")
                        collected_stdout = "hold"
                        per_status[spec.artifact_id] = collected_stdout
                    if collected_stdout == "collected":
                        raw_by_id[spec.artifact_id] = stdout
                    staged.append((spec, stdout, collected_stdout, code, actual_command))
            except (OSError, RuntimeError, ValueError) as exc:
                violations.append(f"producer failed: {spec.artifact_id}: {type(exc).__name__}")
                failed_exc: ArtifactCollectionStatus = "failed"
                per_status[spec.artifact_id] = failed_exc
                staged.append((spec, b"", failed_exc, None, actual_command))
    for artifact_id, (
        manifest_path,
        manifest_bytes,
        dependencies,
        context_snapshots,
    ) in input_manifests.items():
        problem = _verify_input_manifest_inputs(
            staging,
            manifest_path,
            manifest_bytes,
            dependencies,
            context_snapshots,
        )
        if problem is not None:
            violations.append(f"{problem}: {artifact_id}")
    after = _snapshot_subject(root)
    if after is None:
        after_commit = before_commit
        after_tree = before_tree
        after_clean = False
        violations.append("git identity is unavailable after collection")
    else:
        after_commit = after.commit
        after_tree = after.tree
        after_clean = after.clean
        if not after.clean:
            violations.append("git worktree is dirty after collection")
    if before_commit != after_commit:
        violations.append("git HEAD changed during collection")
    if before_tree != after_tree:
        violations.append("git tree changed during collection")
    if before_clean != after_clean:
        violations.append("git worktree state changed during collection")
    subject_commit = (
        after_commit if _HEX40_RE.fullmatch(after_commit) is not None else before_commit
    )
    subject_tree = after_tree if _HEX40_RE.fullmatch(after_tree) is not None else before_tree
    records: list[ArtifactRecord] = []
    for spec, raw, status, code, actual_command in sorted(staged, key=lambda t: t[0].artifact_id):
        digest = hashlib.sha256(raw).hexdigest()
        schema = _extract_schema(raw)
        embedded = _extract_embedded_subject(raw)
        gen_hash: str | None = None
        if spec.generator_path is not None and _HEX40_RE.fullmatch(subject_commit) is not None:
            gen_hash = _generator_blob_hash(root, subject_commit, spec.generator_path)
            if gen_hash is None:
                violations.append(f"generator is missing at subject: {spec.artifact_id}")
                status = "hold" if status == "collected" else status
        if embedded is not None and embedded != subject_commit:
            violations.append(f"embedded subject mismatch: {spec.artifact_id}")
            status = "hold" if status == "collected" else status
        record = ArtifactRecord(
            artifact_id=spec.artifact_id,
            canonical_path=spec.canonical_path,
            generator_path=spec.generator_path,
            generator_sha256=gen_hash,
            generator_version=spec.generator_version,
            command=actual_command,
            native_scope=spec.native_scope,
            output_flag=spec.output_flag,
            embedded_subject=embedded,
            schema_version=schema,
            sha256=digest,
            byte_length=len(raw),
            collection_status=status,
            accepted_exit_codes=spec.accepted_exit_codes,
            return_code=code,
            depends_on=spec.depends_on,
            handoff_path=spec.handoff_path,
            staging_file=f"{spec.artifact_id}.raw",
        )
        records.append(record)
        dest = _staging_path(staging, spec.artifact_id)
        try:
            try:
                dest_stat = os.lstat(dest)
            except FileNotFoundError:
                dest_stat = None
            if dest_stat is None:
                _atomic_write(dest, raw)
            elif (
                stat.S_ISLNK(dest_stat.st_mode)
                or not stat.S_ISREG(dest_stat.st_mode)
                or dest_stat.st_nlink != 1
                or dest.read_bytes() != raw
            ):
                violations.append(f"staging bytes changed during collection: {spec.artifact_id}")
        except OSError:
            violations.append(f"unable to write staging: {spec.artifact_id}")
    bounded = bound_violations(sorted(set(violations)))
    status_value = (
        "COMPLETE"
        if not bounded and all(r.collection_status == "collected" for r in records)
        else "HOLD"
    )
    if status_value == "COMPLETE" and (not after_clean or not before_clean):
        status_value = "HOLD"
    draft = CollectionManifest(
        schema_version=COLLECTION_SCHEMA,
        subject_commit=subject_commit,
        subject_tree=subject_tree,
        head_before=before_commit,
        head_after=after_commit,
        tree_before=before_tree,
        tree_after=after_tree,
        clean_before=before_clean,
        clean_after=after_clean,
        artifacts=tuple(sorted(records, key=lambda r: r.artifact_id)),
        violations=bounded,
        status=status_value,
        manifest_hash="0" * 64,
    )
    integrity = _manifest_integrity(draft)
    manifest = CollectionManifest(
        schema_version=draft.schema_version,
        subject_commit=draft.subject_commit,
        subject_tree=draft.subject_tree,
        head_before=draft.head_before,
        head_after=draft.head_after,
        tree_before=draft.tree_before,
        tree_after=draft.tree_after,
        clean_before=draft.clean_before,
        clean_after=draft.clean_after,
        artifacts=draft.artifacts,
        violations=draft.violations,
        status=draft.status,
        manifest_hash=integrity,
    )
    _atomic_write(
        staging / "manifest.json", (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    )
    return manifest


def load_collection_manifest(path: Path) -> CollectionManifest:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("collection manifest is missing") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("collection manifest is not utf-8") from exc
    try:
        _: object = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        raise ValueError("collection manifest has duplicate keys or bad json") from exc
    try:
        manifest = CollectionManifest.model_validate_json(text)
    except ValueError as exc:
        raise ValueError("collection manifest schema is invalid") from exc
    if _manifest_integrity(manifest) != manifest.manifest_hash:
        raise ValueError("collection manifest integrity mismatch")
    return manifest
