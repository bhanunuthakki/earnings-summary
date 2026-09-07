"""BHA-147 evidence-bundle diff validation and score-evidence recording."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

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
    exact_hex40 as _exact_hex40,
)
from quality.evidence_bundle_io import (
    extract_schema as _extract_schema,
)
from quality.evidence_bundle_io import (
    generator_blob_hash as _generator_blob_hash,
)
from quality.evidence_bundle_io import (
    git as _git,
)
from quality.evidence_bundle_io import (
    has_parent_symlink as _has_parent_symlink,
)
from quality.evidence_bundle_io import (
    manifest_integrity as _manifest_integrity,
)
from quality.evidence_bundle_io import (
    reject_duplicate_keys as _reject_duplicate_keys,
)
from quality.evidence_bundle_io import (
    verify_staged_bytes,
)
from quality.evidence_bundle_models import (
    HEX40_RE as _HEX40_RE,
)
from quality.evidence_bundle_models import (
    ArtifactRecord,
    CollectionManifest,
    admission_path_for,
    allowed_bundle_paths,
    bound_violations,
    non_architecture_blocks,
)
from quality.evidence_bundle_models import (
    is_canonical_receipt_path as _is_canonical_receipt_path,
)
from quality.scoring import (
    ADMISSION_GENERATOR_PATH,
    HARD_GATES,
    AdmissionReceipt,
    EvidenceEntry,
    ScoreEvidence,
)

__all__ = (
    "record_score_evidence",
    "validate_bundle_diff",
)


@dataclass(frozen=True, slots=True)
class _SourceContext:
    blobs: dict[str, bytes]
    parsed: dict[_SourceName, _ParsedSource]
    typed_valid_paths: frozenset[str]


def _resolve_exact_commit(repo_root: Path, value: str) -> str | None:
    proc = _git(repo_root, "rev-parse", "--verify", f"{value}^{{commit}}")
    if proc.returncode != 0:
        return None
    resolved = _exact_hex40(proc.stdout)
    if resolved is None:
        return None
    if resolved != value.lower():
        return None
    return resolved


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    return _git(repo_root, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def _evidence_only_diff(repo_root: Path, subject: str, bundle: str) -> tuple[str, ...] | None:
    proc = _git(
        repo_root, "diff", "--name-only", "--no-renames", "-z", f"{subject}..{bundle}", "--"
    )
    if proc.returncode != 0:
        return None
    if not proc.stdout:
        return ()
    if not proc.stdout.endswith(b"\0"):
        return None
    try:
        paths = tuple(p.decode("utf-8") for p in proc.stdout[:-1].split(b"\0"))
    except UnicodeDecodeError:
        return None
    if any(not p for p in paths) or len(set(paths)) != len(paths):
        return None
    return paths


def _show_blob(root: Path, commit: str, rel: str) -> bytes | None:
    proc = _git(root, "show", f"{commit}:{rel}")
    if proc.returncode != 0:
        return None
    return proc.stdout


def _expected_admission_set() -> set[str]:
    paths: set[str] = set()
    for key in non_architecture_blocks():
        paths.add(admission_path_for("block", key))
    for key in HARD_GATES:
        paths.add(admission_path_for("hard_gate", key))
    return paths


def _admission_key_for_path(rel: str) -> tuple[str, str] | None:
    for key in non_architecture_blocks():
        if admission_path_for("block", key) == rel:
            return ("block", key)
    for key in HARD_GATES:
        if admission_path_for("hard_gate", key) == rel:
            return ("hard_gate", key)
    return None


def _check_external_output(root: Path, root_res: Path, rel_out: str, out: Path) -> str | None:
    if not rel_out.startswith(".tmp/"):
        return "external manifest must live under ignored .tmp/"
    if rel_out != str(PurePosixPath(rel_out)):
        return "external manifest escapes repository"
    if "\\" in rel_out:
        return "external manifest escapes repository"
    if ".." in PurePosixPath(rel_out).parts:
        return "external manifest escapes repository"
    if _has_parent_symlink(root_res, rel_out):
        return "external manifest parent symlink"
    try:
        st = os.lstat(out)
    except FileNotFoundError:
        pass
    except OSError:
        return "unable to inspect external manifest"
    else:
        if stat.S_ISLNK(st.st_mode):
            return "external manifest alias"
        if not stat.S_ISREG(st.st_mode):
            return "external manifest non-regular"
        if st.st_nlink != 1:
            return "external manifest hard-link"
    if _git(root, "check-ignore", "-q", "--", rel_out).returncode != 0:
        return "external manifest is not ignored"
    if _git(root, "ls-files", "--error-unmatch", "--", rel_out).returncode == 0:
        return "external manifest is tracked"
    return None


def _verify_admission_blob(
    root: Path,
    exact_subject: str,
    exact_bundle: str,
    rel: str,
    oracle: str | None,
    rec_by_path: dict[str, ArtifactRecord] | None,
    problems: list[str],
    ctx: _SourceContext,
) -> None:
    bundle_bytes = _show_blob(root, exact_bundle, rel)
    if bundle_bytes is None:
        problems.append(f"bundle admission bytes absent: {rel}")
        return
    subject_bytes = _show_blob(root, exact_subject, rel)
    if subject_bytes is not None and subject_bytes == bundle_bytes:
        problems.append(f"bundle admission unchanged from subject: {rel}")
        return
    try:
        text = bundle_bytes.decode("utf-8")
    except UnicodeDecodeError:
        problems.append(f"bundle admission is not utf-8: {rel}")
        return
    try:
        payload: object = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError:
        problems.append(f"bundle admission is not valid JSON: {rel}")
        return
    if not isinstance(payload, dict):
        problems.append(f"bundle admission has invalid schema: {rel}")
        return
    payload = cast("dict[str, object]", payload)
    try:
        adm = AdmissionReceipt.model_validate(payload)
    except ValueError:
        problems.append(f"admission receipt is invalid: {rel}")
        return
    slot = _admission_key_for_path(rel)
    if slot is None:
        problems.append(f"bundle admission is not registered: {rel}")
        return
    exp_kind, exp_key = slot
    if adm.kind != exp_kind or adm.key != exp_key:
        problems.append(f"admission key mismatch: {rel}")
    if adm.subject_commit.lower() != exact_subject.lower():
        problems.append(f"admission subject mismatch: {rel}")
    if adm.generator_path != ADMISSION_GENERATOR_PATH:
        problems.append(f"admission generator mismatch: {rel}")
    if oracle is None:
        problems.append(f"registered generator is missing at subject: {rel}")
    elif adm.generator_sha256.lower() != oracle.lower():
        problems.append(f"admission generator hash mismatch: {rel}")
    if adm.schema_version != "quality-score-admission/v1":
        problems.append(f"admission schema mismatch: {rel}")
    if adm.state not in ("pass", "fail"):
        problems.append(f"admission state is invalid: {rel}")
    seen: set[str] = set()
    ordered: list[str] = []
    for src in adm.sources:
        if src.path in seen:
            problems.append(f"admission duplicate source: {rel}")
            break
        seen.add(src.path)
        ordered.append(src.path)
    if any(p == rel for p in ordered):
        problems.append(f"admission self-reference: {rel}")
    if tuple(ordered) != tuple(sorted(ordered)):
        problems.append(f"admission sources are unsorted: {rel}")
    _path_to_source: dict[str, _SourceName] = {v: k for k, v in _SOURCE_PATHS.items()}
    parsed = ctx.parsed
    typed_valid_paths = ctx.typed_valid_paths
    _exp_kind, _exp_key = slot
    try:
        _req = _required_paths(_exp_key)
    except ValueError:
        problems.append(f"bundle admission is not registered: {rel}")
        return
    expected_sources = tuple(sorted(set(_req) & set(typed_valid_paths)))
    if tuple(ordered) != expected_sources:
        problems.append(f"admission sources are forged: {rel}")
        return
    try:
        expected_state = _evaluate_slot(_exp_key, parsed)
    except ValueError:
        problems.append(f"admission slot is invalid: {rel}")
        return
    if adm.state != expected_state:
        problems.append(f"admission state is forged: {rel}")
    for _src in adm.sources:
        _rn = _path_to_source.get(_src.path)
        if _rn is None:
            continue
        _ri = parsed.get(_rn)
        if _ri is None:
            continue
        if _ri.sha256 != _src.sha256.lower():
            problems.append(f"admission source hash mismatch: {rel}: {_src.path}")
        if _ri.schema_version != _src.schema_version:
            problems.append(f"admission source schema mismatch: {rel}: {_src.path}")
    if rec_by_path is not None:
        for src in adm.sources:
            rec = rec_by_path.get(src.path)
            if rec is None:
                continue
            if src.sha256.lower() != rec.sha256.lower():
                problems.append(f"admission source hash mismatch: {rel}: {src.path}")
            if rec.schema_version is not None and src.schema_version != rec.schema_version:
                problems.append(f"admission source schema mismatch: {rel}: {src.path}")


def validate_bundle_diff(
    repo_root: Path,
    subject: str,
    bundle: str,
    manifest: CollectionManifest | None = None,
) -> tuple[str, ...]:
    root = repo_root.resolve()
    problems: list[str] = []
    if not _verify_registry():
        problems.append("admission registry mismatch")
    exact_subject = _resolve_exact_commit(root, subject.lower())
    exact_bundle = _resolve_exact_commit(root, bundle.lower())
    if exact_subject is None:
        problems.append("subject commit does not resolve exactly")
        return bound_violations(problems)
    if exact_bundle is None:
        problems.append("bundle commit does not resolve exactly")
        return bound_violations(problems)
    if not _is_ancestor(root, exact_subject, exact_bundle):
        problems.append("subject is not an ancestor of bundle")
    origin = _git(root, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
    if origin.returncode != 0:
        problems.append("trusted origin/main is missing")
    else:
        origin_hex = _exact_hex40(origin.stdout)
        if origin_hex is None:
            problems.append("trusted origin/main is unreadable")
        elif not _is_ancestor(root, exact_bundle, origin_hex):
            problems.append("bundle is not an ancestor of origin/main")
    changed = _evidence_only_diff(root, exact_subject, exact_bundle)
    if changed is None:
        problems.append("evidence-only diff is unverifiable")
        return bound_violations(problems)
    allowed = set(allowed_bundle_paths())
    for path in changed:
        if path not in allowed:
            problems.append(f"bundle contains non-evidence file: {path}")
        if not path.endswith(".json") or not _is_canonical_receipt_path(path):
            problems.append(f"bundle contains non-JSON evidence: {path}")
    for path in changed:
        show = _git(root, "show", f"{exact_bundle}:{path}")
        if show.returncode != 0:
            problems.append(f"bundle receipt is missing: {path}")
            continue
        try:
            text = show.stdout.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"bundle receipt is not utf-8: {path}")
            continue
        if exact_bundle in text.lower():
            problems.append(f"bundle embeds its own commit: {path}")
        try:
            payload: object = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except ValueError:
            problems.append(f"bundle receipt is not valid JSON: {path}")
            continue
        if not isinstance(payload, dict):
            problems.append(f"bundle receipt has invalid schema: {path}")
            continue
        payload = cast("dict[str, object]", payload)
    expected_adm = _expected_admission_set()
    if len(expected_adm) != 24 or len(non_architecture_blocks()) != 14 or len(HARD_GATES) != 10:
        problems.append("admission registry is incomplete")
    changed_set = set(changed)
    for rel in sorted(expected_adm):
        if rel not in changed_set:
            problems.append(f"bundle is missing admission: {rel}")
    rec_by_path: dict[str, ArtifactRecord] | None = None
    if manifest is not None:
        if _manifest_integrity(manifest) != manifest.manifest_hash:
            problems.append("collection manifest integrity mismatch")
        tmp: dict[str, ArtifactRecord] = {}
        for rec in manifest.artifacts:
            if rec.canonical_path in tmp:
                problems.append(f"duplicate manifest source: {rec.canonical_path}")
            tmp[rec.canonical_path] = rec
            if rec.canonical_path not in allowed:
                problems.append(f"manifest source is not allowlisted: {rec.canonical_path}")
        rec_by_path = tmp
        for rec_path in sorted(tmp):
            if rec_path not in changed_set:
                problems.append(f"bundle is missing raw source: {rec_path}")
        for path in changed:
            if path not in expected_adm and path not in tmp:
                problems.append(f"bundle contains unexpected evidence: {path}")
        for rec_path in sorted(tmp):
            rec = tmp[rec_path]
            bbytes = _show_blob(root, exact_bundle, rec_path)
            if bbytes is None:
                problems.append(f"bundle source bytes absent: {rec_path}")
                continue
            if hashlib.sha256(bbytes).hexdigest() != rec.sha256.lower():
                problems.append(f"bundle source bytes altered: {rec_path}")
            if len(bbytes) != rec.byte_length:
                problems.append(f"bundle source length mismatch: {rec_path}")
            schema = _extract_schema(bbytes)
            if schema is None:
                problems.append(f"bundle source schema is missing: {rec_path}")
            elif rec.schema_version is not None and schema != rec.schema_version:
                problems.append(f"bundle source schema mismatch: {rec_path}")
            sbytes = _show_blob(root, exact_subject, rec_path)
            if sbytes is not None and sbytes == bbytes:
                problems.append(f"bundle source unchanged from subject: {rec_path}")
    oracle: str | None = None
    if _HEX40_RE.fullmatch(exact_subject) is not None:
        oracle = _generator_blob_hash(root, exact_subject, ADMISSION_GENERATOR_PATH)
    if manifest is not None and oracle is None:
        problems.append("registered generator is missing at subject")
    if oracle is not None and _RUNTIME_POLICY_SHA256.lower() != oracle.lower():
        problems.append("runtime admission policy differs from subject generator")
        return bound_violations(problems)
    _path_to_source: dict[str, _SourceName] = {v: k for k, v in _SOURCE_PATHS.items()}
    _source_paths: set[str] = set(_SOURCE_PATHS.values())
    if rec_by_path is not None:
        available: set[str] = set(rec_by_path.keys())
    else:
        available = {p for p in changed_set if p in _source_paths}
    blobs: dict[str, bytes] = {}
    for _p in sorted(available):
        _blob = _show_blob(root, exact_bundle, _p)
        if _blob is None:
            problems.append(f"bundle source bytes absent: {_p}")
            continue
        blobs[_p] = _blob
    parsed: dict[_SourceName, _ParsedSource] = {}
    typed_valid_paths: set[str] = set()
    for _p in sorted(blobs):
        _name = _path_to_source.get(_p)
        if _name is None:
            continue
        try:
            _item = _parse_source(_name, blobs[_p], exact_subject)
        except ValueError:
            problems.append(f"bundle source is not registered: {_p}")
            continue
        if not _item.typed_valid:
            problems.append(f"bundle source is typed-invalid: {_p}")
            continue
        parsed[_name] = _item
        typed_valid_paths.add(_p)
    ctx = _SourceContext(blobs=blobs, parsed=parsed, typed_valid_paths=frozenset(typed_valid_paths))
    for rel in sorted(expected_adm):
        if rel not in changed_set:
            continue
        _verify_admission_blob(
            root,
            exact_subject,
            exact_bundle,
            rel,
            oracle,
            rec_by_path,
            problems,
            ctx,
        )
    return bound_violations(problems)


def record_score_evidence(
    repo_root: Path,
    bundle_commit: str,
    manifest: CollectionManifest,
    staging_dir: Path,
    output_path: Path,
) -> ScoreEvidence:
    root = repo_root.resolve()
    root_res = root.resolve()
    raw_out = output_path if output_path.is_absolute() else (root / output_path)
    try:
        rel_out = raw_out.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("external manifest escapes repository") from exc
    out = raw_out.resolve(strict=False) if raw_out.is_absolute() else (root / rel_out)
    err = _check_external_output(root, root_res, rel_out, raw_out)
    if err is not None:
        raise ValueError(err)
    exact_bundle = _resolve_exact_commit(root, bundle_commit.lower())
    if exact_bundle is None:
        raise ValueError("bundle commit does not resolve exactly")
    if _manifest_integrity(manifest) != manifest.manifest_hash:
        raise ValueError("collection manifest integrity mismatch")
    if manifest.status != "COMPLETE":
        raise ValueError("collection is not COMPLETE")
    if not manifest.clean_before or not manifest.clean_after:
        raise ValueError("collection worktree was not clean")
    if (
        manifest.head_before.lower() != manifest.subject_commit.lower()
        or manifest.head_after.lower() != manifest.subject_commit.lower()
    ):
        raise ValueError("collection head does not match subject commit")
    if (
        manifest.tree_before.lower() != manifest.subject_tree.lower()
        or manifest.tree_after.lower() != manifest.subject_tree.lower()
    ):
        raise ValueError("collection tree does not match subject tree")
    staging = staging_dir.resolve() if staging_dir.is_absolute() else (root / staging_dir).resolve()
    try:
        rel_staging = staging.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("staging escapes repository") from exc
    if not rel_staging.startswith(".tmp/"):
        raise ValueError("staging escapes repository")
    if _git(root, "check-ignore", "-q", "--", f"{rel_staging}/manifest.json").returncode != 0:
        raise ValueError("staging directory is not Git-ignored")
    staged_problems = verify_staged_bytes(manifest, staging)
    if staged_problems:
        raise ValueError("staged bytes are not valid: " + "; ".join(staged_problems))
    problems = validate_bundle_diff(root, manifest.subject_commit, exact_bundle, manifest)
    if problems:
        raise ValueError("bundle diff is not valid: " + "; ".join(problems))
    blocks: dict[str, EvidenceEntry] = {}
    gates: dict[str, EvidenceEntry] = {}
    allowed = set(allowed_bundle_paths())
    changed = _evidence_only_diff(root, manifest.subject_commit.lower(), exact_bundle)
    if changed is None:
        raise ValueError("evidence-only diff is unverifiable")
    for rel in changed:
        if rel not in allowed:
            raise ValueError(f"bundle contains non-evidence file: {rel}")
        show = _git(root, "show", f"{exact_bundle}:{rel}")
        if show.returncode != 0:
            raise ValueError(f"bundle receipt is missing: {rel}")
        digest = hashlib.sha256(show.stdout).hexdigest()
        try:
            payload: object = json.loads(
                show.stdout.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
            )
        except ValueError as exc:
            raise ValueError(f"bundle receipt is not valid JSON: {rel}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"bundle receipt has invalid schema: {rel}")
        payload = cast("dict[str, object]", payload)
        if payload.get("schema_version") == "quality-score-admission/v1":
            try:
                admission = AdmissionReceipt.model_validate(payload)
            except ValueError as exc:
                raise ValueError(f"admission receipt is invalid: {rel}") from exc
            entry = EvidenceEntry(receipt_path=rel, sha256=digest, bundle_commit=exact_bundle)
            if admission.subject_commit.lower() != manifest.subject_commit.lower():
                raise ValueError(f"admission subject mismatch: {rel}")
            if admission.kind == "block" and admission.key in blocks:
                raise ValueError(f"duplicate admission key: {admission.key}")
            if admission.kind != "block" and admission.key in gates:
                raise ValueError(f"duplicate admission key: {admission.key}")
            if admission.kind == "block":
                blocks[admission.key] = entry
            else:
                gates[admission.key] = entry
    if set(blocks) != set(non_architecture_blocks()):
        raise ValueError("admission blocks are incomplete")
    if set(gates) != set(HARD_GATES):
        raise ValueError("admission hard gates are incomplete")
    evidence = ScoreEvidence(
        schema_version="quality-score-evidence-v1",
        scoped_commit=manifest.subject_commit.lower(),
        blocks=blocks,
        hard_gates=gates,
    )
    err2 = _check_external_output(root, root_res, rel_out, raw_out)
    if err2 is not None:
        raise ValueError(err2)
    _atomic_write(out, (evidence.model_dump_json(indent=2) + "\n").encode("utf-8"))
    return evidence
