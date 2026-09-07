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
            command=(py, "execution/reconcile_quality_baseline.py", "--repo-root", "."),
            native_scope="WORKTREE",
            output_flag="--output",
            accepted_exit_codes=(0, 2),
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
    )


def _producer_output_path(staging: Path, artifact_id: str) -> Path:
    safe = artifact_id.replace("/", "-").replace("\\", "-")
    return staging / f"{safe}.out"


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
    staged: list[tuple[ArtifactSpec, bytes, ArtifactCollectionStatus, int | None]] = []
    per_status: dict[str, ArtifactCollectionStatus] = {}
    for spec in ordered:
        if any(per_status.get(dep) != "collected" for dep in spec.depends_on):
            violations.append(f"unsatisfied dependency: {spec.artifact_id}")
            hold_dep: ArtifactCollectionStatus = "hold"
            per_status[spec.artifact_id] = hold_dep
            staged.append((spec, b"", hold_dep, None))
            continue
        if spec.output_flag is not None:
            out_path = _producer_output_path(staging, spec.artifact_id)
            problem = _preflight_output(out_path)
            if problem is not None:
                violations.append(f"{problem}: {spec.artifact_id}")
                status: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = status
                staged.append((spec, b"", status, None))
                continue
            argv = (*spec.command, spec.output_flag, str(out_path))
            try:
                result = run(argv, root)
            except (OSError, RuntimeError, ValueError) as exc:
                violations.append(f"producer failed: {spec.artifact_id}: {type(exc).__name__}")
                failed: ArtifactCollectionStatus = "failed"
                per_status[spec.artifact_id] = failed
                staged.append((spec, b"", failed, None))
                continue
            code = int(result.returncode)
            if code not in spec.accepted_exit_codes:
                violations.append(f"producer exit {result.returncode}: {spec.artifact_id}")
                hold: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = hold
                staged.append((spec, b"", hold, code))
                continue
            data, err = _read_producer_output(out_path, staging_resolved)
            if err is not None:
                violations.append(f"{err}: {spec.artifact_id}")
                hold_err: ArtifactCollectionStatus = "hold"
                per_status[spec.artifact_id] = hold_err
                staged.append((spec, b"", hold_err, code))
                continue
            assert data is not None
            if spec.handoff_path is not None:
                try:
                    _install_handoff(root, spec.handoff_path, data)
                except (OSError, RuntimeError, ValueError) as exc:
                    violations.append(f"handoff failed: {spec.artifact_id}: {type(exc).__name__}")
                    hold_hand: ArtifactCollectionStatus = "hold"
                    per_status[spec.artifact_id] = hold_hand
                    staged.append((spec, data, hold_hand, code))
                    continue
            collected: ArtifactCollectionStatus = "collected"
            per_status[spec.artifact_id] = collected
            staged.append((spec, data, collected, code))
        else:
            try:
                result = run(spec.command, root)
                stdout = result.stdout
                code = int(result.returncode)
                if not isinstance(stdout, bytes):
                    violations.append(f"producer returned non-bytes: {spec.artifact_id}")
                    failed_stdout: ArtifactCollectionStatus = "failed"
                    per_status[spec.artifact_id] = failed_stdout
                    staged.append((spec, b"", failed_stdout, code))
                elif code not in spec.accepted_exit_codes:
                    violations.append(f"producer exit {result.returncode}: {spec.artifact_id}")
                    hold_stdout: ArtifactCollectionStatus = "hold"
                    per_status[spec.artifact_id] = hold_stdout
                    staged.append((spec, b"", hold_stdout, code))
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
                            staged.append((spec, stdout, hold_hand_stdout, code))
                            continue
                    collected_stdout: ArtifactCollectionStatus = "collected"
                    per_status[spec.artifact_id] = collected_stdout
                    staged.append((spec, stdout, collected_stdout, code))
            except (OSError, RuntimeError, ValueError) as exc:
                violations.append(f"producer failed: {spec.artifact_id}: {type(exc).__name__}")
                failed_exc: ArtifactCollectionStatus = "failed"
                per_status[spec.artifact_id] = failed_exc
                staged.append((spec, b"", failed_exc, None))
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
    for spec, raw, status, code in sorted(staged, key=lambda t: t[0].artifact_id):
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
            command=spec.command,
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
            _atomic_write(dest, raw)
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
