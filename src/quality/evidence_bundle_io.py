"""BHA-147 low-level evidence-bundle IO helpers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from quality.evidence_bundle_models import (
    HEX40_RE as _HEX40_RE,
)
from quality.evidence_bundle_models import (
    ArtifactRecord,
    CollectionManifest,
    SubjectSnapshot,
    bound_violations,
)
from quality.evidence_bundle_models import (
    is_canonical_generator_path as _is_canonical_generator_path,
)
from quality.git_env import clean_local_git_env


class RunnerResult(Protocol):
    @property
    def returncode(self) -> int: ...
    @property
    def stdout(self) -> object: ...


Runner = Callable[[tuple[str, ...], Path], RunnerResult]


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        env=clean_local_git_env(),
    )


def _default_runner(argv: tuple[str, ...], repo_root: Path) -> subprocess.CompletedProcess[bytes]:
    env = clean_local_git_env()
    src = str(repo_root / "src")
    existing = env.get("PYTHONPATH")
    if existing:
        env["PYTHONPATH"] = src + os.pathsep + existing
    else:
        env["PYTHONPATH"] = src
    return subprocess.run(
        list(argv),
        cwd=repo_root,
        check=False,
        capture_output=True,
        env=env,
    )


def _exact_hex40(raw: bytes) -> str | None:
    try:
        text = raw.decode("ascii").strip().lower()
    except UnicodeDecodeError:
        return None
    if _HEX40_RE.fullmatch(text) is None:
        return None
    return text


def _snapshot_subject(repo_root: Path) -> SubjectSnapshot | None:
    head = _git(repo_root, "rev-parse", "--verify", "HEAD^{commit}")
    tree = _git(repo_root, "rev-parse", "--verify", "HEAD^{tree}")
    status = _git(repo_root, "status", "--porcelain", "-z", "--untracked-files=all", "--")
    if head.returncode != 0 or tree.returncode != 0 or status.returncode != 0:
        return None
    commit = _exact_hex40(head.stdout)
    tree_hex = _exact_hex40(tree.stdout)
    if commit is None or tree_hex is None:
        return None
    return SubjectSnapshot(commit=commit, tree=tree_hex, clean=status.stdout == b"")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate object key: {key}")
        seen[key] = value
    return seen


def _parse_json_object(raw: bytes) -> dict[str, object] | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        payload: object = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    raw_dict = cast(dict[object, object], payload)
    out: dict[str, object] = {}
    for key, value in raw_dict.items():
        if not isinstance(key, str):
            return None
        out[key] = value
    return out


def _extract_schema(raw: bytes) -> str | None:
    payload = _parse_json_object(raw)
    if payload is None:
        return None
    value = payload.get("schema_version")
    if isinstance(value, str) and 1 <= len(value) <= 200:
        return value
    return None


def _extract_embedded_subject(raw: bytes) -> str | None:
    payload = _parse_json_object(raw)
    if payload is None:
        return None
    for key in ("subject_commit", "scoped_commit", "commit_hash", "scoped_revision"):
        value = payload.get(key)
        if isinstance(value, str) and _HEX40_RE.fullmatch(value.lower()) is not None:
            return value.lower()
    return None


def _generator_blob_hash(repo_root: Path, subject: str, generator_path: str) -> str | None:
    if not _is_canonical_generator_path(generator_path):
        return None
    proc = _git(repo_root, "show", f"{subject}:{generator_path}")
    if proc.returncode != 0:
        return None
    return hashlib.sha256(proc.stdout).hexdigest()


def _staging_path(staging_dir: Path, artifact_id: str) -> Path:
    safe = artifact_id.replace("/", "-").replace("\\", "-")
    return staging_dir / f"{safe}.raw"


def _atomic_write(path: Path, data: bytes) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f".{path.name}.", suffix=".tmp")
    fd_open = True
    try:
        with os.fdopen(fd, "wb") as handle:
            fd_open = False
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        if fd_open:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _install_handoff(repo_root: Path, rel_path: str, data: bytes) -> None:
    root_res = repo_root.resolve()
    rel = PurePosixPath(rel_path)
    if rel.is_absolute():
        raise ValueError("handoff path must be repo-relative")
    if rel_path != rel.as_posix():
        raise ValueError("handoff path must be posix")
    if rel.parts[:1] != (".tmp",):
        raise ValueError("handoff path must live under .tmp/")
    if ".." in rel.parts or "." in rel.parts:
        raise ValueError("handoff path must not contain dot segments")
    target = root_res / rel_path
    cur = root_res
    for part in rel.parts[:-1]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OSError("unable to inspect handoff parent") from exc
        if stat.S_ISLNK(st.st_mode):
            raise ValueError("handoff parent is symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError("handoff parent is not a directory")
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise OSError("unable to resolve handoff path") from exc
    if resolved != root_res and root_res not in resolved.parents:
        raise ValueError("handoff escapes repository")
    if _git(root_res, "check-ignore", "-q", "--", rel_path).returncode != 0:
        raise ValueError("handoff path is not ignored")
    if _git(root_res, "ls-files", "--error-unmatch", "--", rel_path).returncode == 0:
        raise ValueError("handoff path is tracked")
    try:
        st_target = os.lstat(target)
    except FileNotFoundError:
        _atomic_write(target, data)
        return
    except OSError as exc:
        raise OSError("unable to inspect handoff target") from exc
    if stat.S_ISLNK(st_target.st_mode):
        raise ValueError("handoff target is symlink")
    if stat.S_ISDIR(st_target.st_mode):
        raise ValueError("handoff target is directory")
    if not stat.S_ISREG(st_target.st_mode):
        raise ValueError("handoff target is non-regular")
    if st_target.st_nlink != 1:
        raise ValueError("handoff target is hard-linked")
    _atomic_write(target, data)


def _git_staging_problems(root: Path, rel_staging: str, names: Sequence[str]) -> list[str]:
    out: list[str] = []
    probe = f"{rel_staging}/manifest.json"
    if _git(root, "check-ignore", "-q", "--", probe).returncode != 0:
        out.append("staging directory is not Git-ignored")
    for name in names:
        rel = f"{rel_staging}/{PurePosixPath(name).name}"
        if _git(root, "ls-files", "--error-unmatch", "--", rel).returncode == 0:
            out.append(f"staging artifact is tracked: {name}")
    if _git(root, "ls-files", "--error-unmatch", "--", probe).returncode == 0:
        out.append("staging manifest is tracked")
    return out


def _read_staged_secure(
    staging: Path,
    staging_resolved: Path,
    record: ArtifactRecord,
    seen_inodes: set[tuple[int, int]],
) -> tuple[bytes | None, str | None]:
    candidate = staging / PurePosixPath(record.staging_file).name
    try:
        st = os.lstat(candidate)
    except OSError:
        return None, f"missing staged artifact: {record.artifact_id}"
    if stat.S_ISLNK(st.st_mode):
        return None, f"staging symlink: {record.artifact_id}"
    try:
        if candidate.resolve().parent != staging_resolved:
            return None, f"staging escape: {record.artifact_id}"
    except OSError:
        return None, f"unable to resolve staging: {record.artifact_id}"
    if not stat.S_ISREG(st.st_mode):
        return None, f"staging non-regular: {record.artifact_id}"
    if st.st_nlink != 1:
        return None, f"staging hard-link: {record.artifact_id}"
    key = (st.st_dev, st.st_ino)
    if key in seen_inodes:
        return None, f"staging duplicate inode: {record.artifact_id}"
    seen_inodes.add(key)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd1 = os.open(candidate, flags)
    except OSError:
        return None, f"staging symlink: {record.artifact_id}"
    try:
        try:
            st1 = os.fstat(fd1)
        except OSError:
            return None, f"unable to resolve staging: {record.artifact_id}"
        if (st1.st_dev, st1.st_ino) != key or not stat.S_ISREG(st1.st_mode) or st1.st_nlink != 1:
            return None, f"staging bytes changed during read: {record.artifact_id}"
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd1, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        data1 = b"".join(chunks)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd1)
    try:
        fd2 = os.open(candidate, flags)
    except OSError:
        return None, f"staging bytes changed during read: {record.artifact_id}"
    try:
        try:
            st2 = os.fstat(fd2)
        except OSError:
            return None, f"unable to resolve staging: {record.artifact_id}"
        if (st2.st_dev, st2.st_ino) != key or not stat.S_ISREG(st2.st_mode) or st2.st_nlink != 1:
            return None, f"staging bytes changed during read: {record.artifact_id}"
        chunks2: list[bytes] = []
        while True:
            chunk = os.read(fd2, 65536)
            if not chunk:
                break
            chunks2.append(chunk)
        data2 = b"".join(chunks2)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd2)
    try:
        st_final = os.lstat(candidate)
    except OSError:
        return None, f"staging bytes changed during read: {record.artifact_id}"
    if (st_final.st_dev, st_final.st_ino) != key:
        return None, f"staging bytes changed during read: {record.artifact_id}"
    if not stat.S_ISREG(st_final.st_mode) or st_final.st_nlink != 1:
        return None, f"staging bytes changed during read: {record.artifact_id}"
    if data1 != data2:
        return None, f"staging bytes changed during read: {record.artifact_id}"
    return data1, None


def _relative_posix(repo_root: Path, path: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None
    return rel


def _reject_output_alias(repo_root: Path, src: Path, dst: Path) -> str | None:
    try:
        src_res = src.resolve()
        dst_res = dst.resolve(strict=False)
        root_res = repo_root.resolve()
    except OSError:
        return "unable to resolve source/output paths"
    if src_res == dst_res:
        return "source and output alias"
    try:
        if dst.exists() and src.exists() and os.path.samefile(src, dst):
            return "source and output alias"
    except OSError:
        pass
    try:
        if dst.is_symlink() or src.is_symlink():
            return "symlink source/output alias"
    except OSError:
        return "unable to inspect source/output paths"
    try:
        dst.relative_to(root_res)
    except ValueError:
        return "output escapes repository"
    if ".." in dst_res.relative_to(root_res).parts:
        return "output escapes repository"
    return None


def _live_head_tree(root: Path) -> tuple[str | None, str | None]:
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    tree = _git(root, "rev-parse", "--verify", "HEAD^{tree}")
    if head.returncode != 0 or tree.returncode != 0:
        return None, None
    return _exact_hex40(head.stdout), _exact_hex40(tree.stdout)


def _status_path_set(root: Path) -> set[str] | None:
    proc = _git(root, "status", "--porcelain", "-z", "--untracked-files=all", "--")
    if proc.returncode != 0:
        return None
    raw = proc.stdout
    if not raw:
        return set()
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    chunks = raw.split(b"\x00")
    paths: set[str] = set()
    expect_follow = False
    for chunk in chunks:
        if not chunk:
            return None
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if expect_follow:
            paths.add(text)
            expect_follow = False
            continue
        if len(text) < 4 or text[2] != " ":
            return None
        code = text[:2]
        part = text[3:]
        if not part:
            return None
        if "R" in code or "C" in code:
            paths.add(part)
            expect_follow = True
        elif " -> " in part:
            _, _, new = part.rpartition(" -> ")
            if not new.strip():
                return None
            paths.add(new.strip())
        else:
            paths.add(part)
    if expect_follow:
        return None
    return paths


def _has_parent_symlink(root_res: Path, rel: str) -> bool:
    parts = PurePosixPath(rel).parts[:-1]
    cur = root_res
    for part in parts:
        cur = cur / part
        try:
            if cur.is_symlink():
                return True
        except OSError:
            return True
    return False


def _snapshot_prior(
    root: Path, root_res: Path, rel: str
) -> tuple[bool, bytes | None, int | None, str | None]:
    dst = root / rel
    if _has_parent_symlink(root_res, rel):
        return True, None, None, "output parent symlink"
    try:
        st = os.lstat(dst)
    except FileNotFoundError:
        return False, None, None, None
    except OSError:
        return True, None, None, "unable to inspect output"
    if stat.S_ISLNK(st.st_mode):
        return True, None, None, "output symlink"
    if not stat.S_ISREG(st.st_mode):
        return True, None, None, "output non-regular"
    if st.st_nlink != 1:
        return True, None, None, "output hard-link"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(dst, flags)
    except OSError:
        return True, None, None, "output symlink"
    try:
        try:
            st2 = os.fstat(fd)
        except OSError:
            return True, None, None, "unable to read output"
        if not stat.S_ISREG(st2.st_mode) or st2.st_nlink != 1:
            return True, None, None, "output changed during preflight"
        if (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino):
            return True, None, None, "output changed during preflight"
        mode = stat.S_IMODE(st2.st_mode)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    try:
        st3 = os.lstat(dst)
    except OSError:
        return True, None, None, "output changed during preflight"
    if (st3.st_dev, st3.st_ino) != (st.st_dev, st.st_ino):
        return True, None, None, "output changed during preflight"
    return True, data, mode, None


def _rollback_outputs(
    root: Path, priors: dict[str, tuple[bool, bytes | None, int | None]], attempted: Sequence[str]
) -> list[str]:
    failures: list[str] = []
    for rel in attempted:
        info = priors.get(rel)
        if info is None:
            continue
        existed, data, mode = info
        dst = root / rel
        if not existed:
            try:
                dst.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                failures.append(f"rollback failed: {rel}")
                continue
        else:
            if data is None or mode is None:
                failures.append(f"rollback failed: {rel}")
                continue
            try:
                _atomic_write(dst, data)
            except OSError:
                failures.append(f"rollback failed: {rel}")
                continue
            try:
                os.chmod(dst, mode)
            except OSError:
                failures.append(f"rollback failed: {rel}")
    return failures


def _manifest_integrity(manifest: CollectionManifest) -> str:
    canonical = manifest.model_dump_json(exclude={"manifest_hash"}).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def verify_staged_bytes(manifest: CollectionManifest, staging_dir: Path) -> tuple[str, ...]:
    problems: list[str] = []
    seen: set[str] = set()
    seen_inodes: set[tuple[int, int]] = set()
    try:
        staging_resolved = staging_dir.resolve()
    except OSError:
        return bound_violations(["unable to resolve staging"])
    for record in manifest.artifacts:
        if record.staging_file in seen:
            problems.append(f"duplicate staging file: {record.staging_file}")
            continue
        seen.add(record.staging_file)
        data, err = _read_staged_secure(staging_dir, staging_resolved, record, seen_inodes)
        if err is not None:
            problems.append(err)
            continue
        assert data is not None
        if hashlib.sha256(data).hexdigest() != record.sha256:
            problems.append(f"tampered staged artifact: {record.artifact_id}")
        if len(data) != record.byte_length:
            problems.append(f"length mismatch: {record.artifact_id}")
    return bound_violations(sorted(set(problems)))


def staged_input_alias_paths(manifest: Path) -> tuple[Path, ...]:
    """Enumerate output-protection targets, including rejected input spellings.

    This is only an alias guard. Admission still validates the manifest and its
    canonical paths. Duplicate keys must not hide an earlier input from the guard.
    """
    candidates: list[str] = []

    def capture_paths(pairs: list[tuple[str, object]]) -> dict[str, object]:
        for key, value in pairs:
            if key == "path" and isinstance(value, str) and value:
                candidates.append(value)
        return dict(pairs)

    try:
        json.loads(manifest.read_bytes(), object_pairs_hook=capture_paths)
        base = manifest.parent.resolve()
    except (OSError, ValueError):
        return ()
    paths: set[Path] = set()
    for value in candidates:
        try:
            # Absolute, overlong and dot-segment spellings can still name a
            # retained file even though the input validator rejects them.
            paths.add((base / value).resolve())
        except (OSError, ValueError):
            continue
    return tuple(sorted(paths))


atomic_write = _atomic_write
default_runner = _default_runner
exact_hex40 = _exact_hex40
extract_embedded_subject = _extract_embedded_subject
extract_schema = _extract_schema
generator_blob_hash = _generator_blob_hash
git = _git
git_staging_problems = _git_staging_problems
install_handoff = _install_handoff
has_parent_symlink = _has_parent_symlink
live_head_tree = _live_head_tree
manifest_integrity = _manifest_integrity
read_staged_secure = _read_staged_secure
reject_duplicate_keys = _reject_duplicate_keys
reject_output_alias = _reject_output_alias
rollback_outputs = _rollback_outputs
snapshot_prior = _snapshot_prior
snapshot_subject = _snapshot_subject
staging_path = _staging_path
status_path_set = _status_path_set
