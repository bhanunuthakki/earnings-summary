"""BHA-147 docs-only assembler (Phase B)."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from quality.admission_policy import (
    RUNTIME_POLICY_SHA256 as _RUNTIME_POLICY_SHA256,
)
from quality.admission_policy import (
    SOURCE_PATHS as _SOURCE_PATHS,
)
from quality.admission_policy import (
    ParsedSource as _ParsedSource,
)
from quality.admission_policy import (
    SourceName as _SourceName,
)
from quality.admission_policy import (
    evaluate_slot as _evaluate_slot,
)
from quality.admission_policy import (
    parse_source as _parse_source,
)
from quality.admission_policy import (
    required_paths as _required_paths,
)
from quality.admission_policy import (
    verify_registry as _verify_registry,
)
from quality.evidence_bundle_io import (
    atomic_write as _atomic_write,
)
from quality.evidence_bundle_io import (
    generator_blob_hash as _generator_blob_hash,
)
from quality.evidence_bundle_io import (
    git_staging_problems as _git_staging_problems,
)
from quality.evidence_bundle_io import (
    live_head_tree as _live_head_tree,
)
from quality.evidence_bundle_io import (
    manifest_integrity as _manifest_integrity,
)
from quality.evidence_bundle_io import (
    read_staged_secure as _read_staged_secure,
)
from quality.evidence_bundle_io import (
    reject_output_alias as _reject_output_alias,
)
from quality.evidence_bundle_io import (
    rollback_outputs as _rollback_outputs,
)
from quality.evidence_bundle_io import (
    snapshot_prior as _snapshot_prior,
)
from quality.evidence_bundle_io import (
    snapshot_subject as _snapshot_subject,
)
from quality.evidence_bundle_io import (
    status_path_set as _status_path_set,
)
from quality.evidence_bundle_io import (
    verify_staged_bytes,
)
from quality.evidence_bundle_models import (
    ALLOWED_SOURCE_PATHS,
    AdmissionState,
    ArtifactRecord,
    BundleAssembly,
    CollectionManifest,
    admission_path_for,
    allowed_bundle_paths,
    bound_violations,
    non_architecture_blocks,
)
from quality.evidence_bundle_models import (
    HEX40_RE as _HEX40_RE,
)
from quality.evidence_bundle_models import (
    is_canonical_generator_path as _is_canonical_generator_path,
)
from quality.evidence_bundle_models import (
    is_canonical_receipt_path as _is_canonical_receipt_path,
)
from quality.evidence_path_policy import FREEZE_PATH
from quality.roadmap_freeze_bundle import validate_freeze_index
from quality.scoring import (
    ADMISSION_GENERATOR_PATH,
    HARD_GATES,
    AdmissionReceipt,
    AdmissionSource,
)

__all__ = ("assemble_bundle",)


def assemble_bundle(
    repo_root: Path,
    manifest: CollectionManifest,
    staging_dir: Path,
    output_dir: Path,
    generator_path: str = ADMISSION_GENERATOR_PATH,
) -> BundleAssembly:
    root = repo_root.resolve()
    staging = staging_dir.resolve() if staging_dir.is_absolute() else (root / staging_dir).resolve()
    outdir = output_dir.resolve() if output_dir.is_absolute() else (root / output_dir).resolve()
    violations: list[str] = [*manifest.violations]
    if _manifest_integrity(manifest) != manifest.manifest_hash:
        violations.append("collection manifest integrity mismatch")
    try:
        rel_staging = staging.relative_to(root).as_posix()
    except ValueError:
        violations.append("staging escapes repository")
        rel_staging = ""
    if rel_staging.startswith(".tmp/"):
        violations.extend(
            _git_staging_problems(root, rel_staging, [a.staging_file for a in manifest.artifacts])
        )
    else:
        violations.append("staging escapes repository")
    if manifest.status != "COMPLETE":
        violations.append("collection is not COMPLETE")
    if not manifest.clean_before or not manifest.clean_after:
        violations.append("subject was not clean")
    if manifest.head_before != manifest.head_after or manifest.tree_before != manifest.tree_after:
        violations.append("subject moved during collection")
    staged_problems = verify_staged_bytes(manifest, staging)
    violations.extend(staged_problems)
    if generator_path != ADMISSION_GENERATOR_PATH:
        violations.append("admission generator is not the registered oracle")
    subject = manifest.subject_commit.lower()
    if _HEX40_RE.fullmatch(subject) is None:
        violations.append("subject commit is not exact 40-hex")
        subject = manifest.subject_commit.lower()
    gen_hash: str | None = None
    if _HEX40_RE.fullmatch(subject) is not None:
        gen_hash = _generator_blob_hash(root, subject, generator_path)
    if gen_hash is None:
        violations.append("generator is missing at subject")
    pre_snapshot = _snapshot_subject(root)
    if pre_snapshot is None:
        violations.append("git identity is unavailable before assembly")
    else:
        if pre_snapshot.commit != subject:
            violations.append("observed HEAD differs from manifest subject_commit")
        if pre_snapshot.tree != manifest.subject_tree.lower():
            violations.append("observed HEAD tree differs from manifest subject_tree")
        if not pre_snapshot.clean:
            violations.append("worktree is dirty before assembly")
        live_gen: str | None = None
        if _is_canonical_generator_path(generator_path):
            live_gen = _generator_blob_hash(root, pre_snapshot.commit, generator_path)
        if live_gen is None or gen_hash is None or live_gen.lower() != gen_hash.lower():
            violations.append("registered generator blob hash mismatch at observed subject")
        for rec in manifest.artifacts:
            if rec.generator_path is None:
                continue
            if not _is_canonical_generator_path(rec.generator_path):
                violations.append(f"generator path changed at observed subject: {rec.artifact_id}")
                continue
            h = _generator_blob_hash(root, pre_snapshot.commit, rec.generator_path)
            if h is None:
                violations.append(f"generator is missing at observed subject: {rec.artifact_id}")
            elif rec.generator_sha256 is None:
                violations.append(
                    f"generator hash is missing at observed subject: {rec.artifact_id}"
                )
            elif h.lower() != rec.generator_sha256.lower():
                violations.append(f"generator hash changed at observed subject: {rec.artifact_id}")
    try:
        out_rel = outdir.relative_to(root).as_posix()
    except ValueError:
        violations.append("output escapes repository")
        out_rel = ""
    if out_rel != "docs/quality":
        violations.append("output must be docs/quality")
    if not _is_canonical_generator_path(generator_path):
        violations.append("noncanonical generator path")
    by_id: dict[str, ArtifactRecord] = {a.artifact_id: a for a in manifest.artifacts}
    if len(by_id) != len(manifest.artifacts):
        violations.append("duplicate artifacts")
    staged_bytes: dict[str, bytes] = {}
    collected_freeze: bytes | None = None
    freeze_record: ArtifactRecord | None = None
    seen_inodes: set[tuple[int, int]] = set()
    try:
        staging_resolved = staging.resolve()
    except OSError:
        staging_resolved = staging
    for record in manifest.artifacts:
        if record.canonical_path == FREEZE_PATH:
            if record.collection_status != "collected":
                violations.append("freeze index is not collected")
                continue
            data, err = _read_staged_secure(staging, staging_resolved, record, seen_inodes)
            if err is not None:
                violations.append(f"freeze index bytes unstable: {err}")
                continue
            assert data is not None
            if hashlib.sha256(data).hexdigest() != record.sha256:
                violations.append("freeze index bytes unstable")
                continue
            if len(data) != record.byte_length:
                violations.append("freeze index length mismatch")
                continue
            collected_freeze = data
            freeze_record = record
            continue
        if record.canonical_path not in ALLOWED_SOURCE_PATHS:
            violations.append(f"source is not allowlisted: {record.canonical_path}")
            continue
        data, err = _read_staged_secure(staging, staging_resolved, record, seen_inodes)
        if err is not None:
            violations.append(f"source bytes unstable: {record.canonical_path}: {err}")
            continue
        assert data is not None
        if hashlib.sha256(data).hexdigest() != record.sha256:
            violations.append(f"source bytes unstable: {record.canonical_path}")
            continue
        if len(data) != record.byte_length:
            violations.append(f"source bytes unstable: {record.canonical_path}")
            continue
        staged_bytes[record.canonical_path] = data
    if not staged_bytes:
        violations.append("no admissible sources")
    _path_to_source: dict[str, _SourceName] = {v: k for k, v in _SOURCE_PATHS.items()}
    parsed: dict[_SourceName, _ParsedSource] = {}
    typed_sources: dict[str, AdmissionSource] = {}
    for rel in sorted(staged_bytes):
        raw = staged_bytes[rel]
        record = next(a for a in manifest.artifacts if a.canonical_path == rel)
        if record.collection_status != "collected":
            violations.append(f"source not collected: {rel}")
            continue
        name = _path_to_source.get(rel)
        if name is None:
            violations.append(f"source is not registered: {rel}")
            continue
        try:
            item = _parse_source(name, raw, manifest.subject_commit)
        except ValueError:
            violations.append(f"source is not registered: {rel}")
            continue
        if not item.typed_valid:
            violations.append(f"source is typed-invalid: {rel}")
            continue
        if item.path != rel or item.sha256 != hashlib.sha256(raw).hexdigest():
            violations.append(f"source bytes unstable: {rel}")
            continue
        parsed[name] = item
        typed_sources[rel] = AdmissionSource(
            path=item.path, sha256=item.sha256, schema_version=item.schema_version
        )
    if not _verify_registry():
        violations.append("admission registry mismatch")
    if not typed_sources:
        violations.append("admission sources are empty")
    pending_freeze: bytes | None = None
    freeze_to_write = collected_freeze
    if freeze_to_write is not None:
        try:
            validate_freeze_index(root, freeze_to_write, subject, staged_bytes)
        except (ValueError, RuntimeError) as exc:
            violations.append(f"freeze index is invalid: {type(exc).__name__}")
        else:
            pending_freeze = freeze_to_write
    written: list[str] = []
    pending_writes: dict[str, bytes] = {}
    if pending_freeze is not None:
        if freeze_record is None:
            violations.append("freeze index record is missing")
        else:
            freeze_src = staging / PurePosixPath(freeze_record.staging_file).name
            freeze_dst = root / FREEZE_PATH
            alias = _reject_output_alias(root, freeze_src, freeze_dst)
            if alias is not None:
                violations.append(f"output alias: {FREEZE_PATH}: {alias}")
            else:
                pending_writes[FREEZE_PATH] = pending_freeze
    for rel, raw in staged_bytes.items():
        if rel not in typed_sources:
            continue
        dst = root / rel
        src = (
            staging
            / PurePosixPath(
                next(a.staging_file for a in manifest.artifacts if a.canonical_path == rel)
            ).name
        )
        alias = _reject_output_alias(root, src, dst)
        if alias is not None:
            violations.append(f"output alias: {rel}: {alias}")
            continue
        pending_writes[rel] = raw
    _runtime_ok = gen_hash is not None and _RUNTIME_POLICY_SHA256.lower() == gen_hash.lower()
    if gen_hash is not None and not _runtime_ok:
        violations.append("runtime admission policy differs from subject generator")
    if _runtime_ok:
        assert gen_hash is not None
        for key in non_architecture_blocks():
            out_path = admission_path_for("block", key)
            if out_path in staged_bytes or out_path in pending_writes:
                violations.append(f"output collision: {out_path}")
                continue
            req_paths = _required_paths(key)
            slot_entries = tuple(typed_sources[p] for p in req_paths if p in typed_sources)
            receipt_state: AdmissionState = _evaluate_slot(key, parsed)
            try:
                receipt = AdmissionReceipt(
                    schema_version="quality-score-admission/v1",
                    kind="block",
                    key=key,
                    state=receipt_state,
                    subject_commit=subject,
                    generator_path=generator_path,
                    generator_sha256=gen_hash,
                    sources=tuple(sorted(slot_entries, key=lambda s: s.path)),
                )
            except ValueError:
                violations.append(f"admission is invalid: {key}")
                continue
            if any(s.path == out_path for s in receipt.sources):
                violations.append(f"self-reference: {key}")
                continue
            pending_writes[out_path] = (receipt.model_dump_json(indent=2) + "\n").encode("utf-8")
        for key in HARD_GATES:
            out_path = admission_path_for("hard_gate", key)
            if out_path in staged_bytes or out_path in pending_writes:
                violations.append(f"output collision: {out_path}")
                continue
            req_paths2 = _required_paths(key)
            slot_entries2 = tuple(typed_sources[p] for p in req_paths2 if p in typed_sources)
            receipt_state2: AdmissionState = _evaluate_slot(key, parsed)
            try:
                receipt = AdmissionReceipt(
                    schema_version="quality-score-admission/v1",
                    kind="hard_gate",
                    key=key,
                    state=receipt_state2,
                    subject_commit=subject,
                    generator_path=generator_path,
                    generator_sha256=gen_hash,
                    sources=tuple(sorted(slot_entries2, key=lambda s: s.path)),
                )
            except ValueError:
                violations.append(f"admission is invalid: {key}")
                continue
            if any(s.path == out_path for s in receipt.sources):
                violations.append(f"self-reference: {key}")
                continue
            pending_writes[out_path] = (receipt.model_dump_json(indent=2) + "\n").encode("utf-8")
    bounded = bound_violations(sorted(set(violations)))
    status_value = "COMPLETE" if not bounded else "HOLD"
    if status_value == "COMPLETE" and pre_snapshot is None:
        bounded = bound_violations([*bounded, "git identity is unavailable before assembly"])
        status_value = "HOLD"
    if status_value == "COMPLETE" and (pre_snapshot is None or not pre_snapshot.clean):
        bounded = bound_violations([*bounded, "worktree is dirty before assembly"])
        status_value = "HOLD"
    if status_value == "COMPLETE":
        assert pre_snapshot is not None
        root_res = root.resolve()
        ordered = sorted(pending_writes)
        allowed_set = set(allowed_bundle_paths())
        for rel in ordered:
            dst = root / rel
            if not _is_canonical_receipt_path(rel) or rel not in allowed_set:
                bounded = bound_violations([*bounded, f"output is not allowlisted: {rel}"])
                status_value = "HOLD"
                break
            alias = _reject_output_alias(root, staging / "manifest.json", dst)
            if alias is not None:
                bounded = bound_violations([*bounded, f"output alias: {rel}"])
                status_value = "HOLD"
                break
        priors: dict[str, tuple[bool, bytes | None, int | None]] = {}
        if status_value == "COMPLETE":
            seen_inodes: set[tuple[int, int]] = set()
            for rel in ordered:
                existed, data, mode, err = _snapshot_prior(root, root_res, rel)
                if err is not None:
                    bounded = bound_violations([*bounded, f"output unsafe: {rel}: {err}"])
                    status_value = "HOLD"
                    break
                if existed:
                    try:
                        lst = os.lstat(root / rel)
                    except OSError:
                        bounded = bound_violations([*bounded, f"output unsafe: {rel}"])
                        status_value = "HOLD"
                        break
                    key = (lst.st_dev, lst.st_ino)
                    if key in seen_inodes:
                        bounded = bound_violations([*bounded, f"output hard-link alias: {rel}"])
                        status_value = "HOLD"
                        break
                    seen_inodes.add(key)
                priors[rel] = (existed, data, mode)
            if status_value == "COMPLETE":
                try:
                    stage_inodes: set[tuple[int, int]] = set()
                    for rec in manifest.artifacts:
                        try:
                            sst = os.lstat(staging / PurePosixPath(rec.staging_file).name)
                        except OSError:
                            continue
                        stage_inodes.add((sst.st_dev, sst.st_ino))
                    for rel in ordered:
                        if priors[rel][0]:
                            try:
                                ost = os.lstat(root / rel)
                            except OSError:
                                continue
                            if (ost.st_dev, ost.st_ino) in stage_inodes:
                                bounded = bound_violations([*bounded, f"output alias: {rel}"])
                                status_value = "HOLD"
                                break
                except OSError:
                    bounded = bound_violations([*bounded, "unable to inspect source/output paths"])
                    status_value = "HOLD"
        if status_value == "COMPLETE":
            attempted: list[str] = []
            failed_rel: str | None = None
            for rel in ordered:
                existed, _, prior_mode = priors[rel]
                try:
                    _atomic_write(root / rel, pending_writes[rel])
                    if existed and prior_mode is not None:
                        os.chmod(root / rel, prior_mode)
                except OSError:
                    failed_rel = rel
                    break
                attempted.append(rel)
            if failed_rel is not None:
                rollback_problems = _rollback_outputs(root, priors, [*attempted, failed_rel])
                bounded = bound_violations(
                    [*bounded, f"unable to write output: {failed_rel}", *rollback_problems]
                )
                status_value = "HOLD"
            else:
                post_problems: list[str] = []
                live_commit, live_tree = _live_head_tree(root)
                if live_commit is None or live_tree is None:
                    post_problems.append("git identity is unavailable after assembly")
                elif live_commit != pre_snapshot.commit or live_tree != pre_snapshot.tree:
                    post_problems.append("subject moved during assembly")
                status_set = _status_path_set(root)
                if status_set is None:
                    post_problems.append("post-assembly status is unverifiable")
                else:
                    pending_set = set(ordered)
                    for p in status_set:
                        if p not in pending_set:
                            post_problems.append(f"unexpected dirty path after assembly: {p}")
                            break
                if not post_problems:
                    for rel in ordered:
                        dst = root / rel
                        try:
                            lst = os.lstat(dst)
                        except OSError:
                            post_problems.append(f"output missing after assembly: {rel}")
                            break
                        if (
                            stat.S_ISLNK(lst.st_mode)
                            or not stat.S_ISREG(lst.st_mode)
                            or lst.st_nlink != 1
                        ):
                            post_problems.append(f"output unsafe after assembly: {rel}")
                            break
                        try:
                            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                            fd = os.open(dst, flags)
                        except OSError:
                            post_problems.append(f"output unsafe after assembly: {rel}")
                            break
                        try:
                            chunks2: list[bytes] = []
                            while True:
                                ch = os.read(fd, 65536)
                                if not ch:
                                    break
                                chunks2.append(ch)
                            cur_data = b"".join(chunks2)
                        finally:
                            with contextlib.suppress(OSError):
                                os.close(fd)
                        if cur_data != pending_writes[rel]:
                            post_problems.append(f"output changed during assembly: {rel}")
                            break
                if post_problems:
                    _rollback_outputs(root, priors, attempted)
                    bounded = bound_violations([*bounded, *post_problems])
                    status_value = "HOLD"
                else:
                    written.extend(ordered)
    written_tuple: tuple[str, ...] = tuple(sorted(written)) if status_value == "COMPLETE" else ()
    return BundleAssembly(
        schema_version="quality-bundle-assembly-v1",
        status=status_value,
        subject_commit=subject,
        generator_path=generator_path,
        generator_sha256=gen_hash if gen_hash is not None else "0" * 64,
        written=written_tuple,
        violations=bounded,
    )
