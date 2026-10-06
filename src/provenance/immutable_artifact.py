"""No-clobber publication for immutable operational evidence artifacts."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from compute.evidence_snapshot import (
    open_windows_evidence_handle,
    windows_evidence_handle_path,
)


class ImmutableArtifactConflictError(RuntimeError):
    """An immutable destination already contains different bytes."""


@dataclass(frozen=True)
class ImmutableArtifactSnapshot:
    path: Path
    device: int
    inode: int
    size_bytes: int
    modified_time_ns: int
    changed_time_ns: int
    file_sha256: str


def read_stable_artifact(
    path: Path, *, max_bytes: int | None = None, allowed_root: Path | None = None
) -> tuple[ImmutableArtifactSnapshot, bytes]:
    """Read one regular file with stable identity and an optional finite byte cap.

    An explicit source root pins its directory chain. It grants no authority for
    other roots. Byte limits do not establish an I/O deadline.
    """
    if max_bytes is not None:
        max_bytes = _positive_byte_limit(max_bytes)
    artifact = _lexical_absolute(path)
    root = _lexical_absolute(allowed_root) if allowed_root is not None else None
    if root is not None:
        try:
            artifact.relative_to(root)
        except ValueError as exc:
            raise ImmutableArtifactConflictError(
                "immutable artifact escapes its source root"
            ) from exc
    if (
        root is not None
        and os.name != "nt"
        and any(not getattr(os, name, 0) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
    ):
        raise ImmutableArtifactConflictError("bounded no-follow source opening is unavailable")
    require_no_reparse_points(artifact)
    lexical_before = artifact.lstat()
    if not stat.S_ISREG(lexical_before.st_mode):
        raise ImmutableArtifactConflictError("immutable artifact is not a regular file")
    with ExitStack() as handles:
        parents: list[tuple[Path, int]] = []
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        parent_fd: int | None = None
        if root is not None:
            for parent in reversed(artifact.parent.parents):
                parents.append((parent, _open_parent(parent, parent_fd, flags, handles)))
                parent_fd = parents[-1][1]
            parents.append(
                (artifact.parent, _open_parent(artifact.parent, parent_fd, flags, handles))
            )
            parent_fd = parents[-1][1]
        if os.name == "nt" and root is not None:
            descriptor = handles.enter_context(open_windows_evidence_handle(artifact))
            _require_windows_path(descriptor, artifact)
        else:
            descriptor = (
                os.open(artifact, flags)
                if parent_fd is None
                else os.open(artifact.name, flags, dir_fd=parent_fd)
            )
            handles.callback(os.close, descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ImmutableArtifactConflictError("immutable artifact is not a regular file")
        if not _path_handle_identity_matches(lexical_before, before):
            raise ImmutableArtifactConflictError(
                "immutable artifact changed before its handle was pinned"
            )
        if max_bytes is not None and before.st_size > max_bytes:
            raise ImmutableArtifactConflictError("immutable artifact exceeds its byte limit")
        parts: list[bytes] = []
        size = 0
        while max_bytes is None or size < max_bytes:
            amount = 1024 * 1024 if max_bytes is None else min(1024 * 1024, max_bytes - size)
            chunk = os.read(descriptor, amount)
            if not chunk:
                break
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise ImmutableArtifactConflictError("immutable artifact exceeds its byte limit")
            parts.append(chunk)
        after = os.fstat(descriptor)
        if max_bytes is not None and after.st_size > max_bytes:
            raise ImmutableArtifactConflictError("immutable artifact exceeds its byte limit")
        require_no_reparse_points(artifact)
        lexical_after = artifact.lstat()
        if _stat_identity(before) != _stat_identity(after) or size != after.st_size:
            raise ImmutableArtifactConflictError("immutable artifact changed while it was read")
        if _stat_identity(lexical_before) != _stat_identity(
            lexical_after
        ) or not _path_handle_identity_matches(lexical_after, after):
            raise ImmutableArtifactConflictError(
                "immutable artifact path changed while it was read"
            )
        for parent, parent_handle in parents:
            if _stat_identity(parent.lstat())[:2] != _stat_identity(os.fstat(parent_handle))[:2]:
                raise ImmutableArtifactConflictError(
                    "immutable artifact directory changed while it was read"
                )
            if os.name == "nt":
                _require_windows_path(parent_handle, parent)
        if os.name == "nt" and root is not None:
            _require_windows_path(descriptor, artifact)
        payload = b"".join(parts)
    return (
        ImmutableArtifactSnapshot(
            path=artifact,
            device=int(after.st_dev),
            inode=int(after.st_ino),
            size_bytes=int(after.st_size),
            modified_time_ns=int(after.st_mtime_ns),
            changed_time_ns=int(after.st_ctime_ns),
            file_sha256=sha256(payload).hexdigest(),
        ),
        payload,
    )


def _positive_byte_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("max_bytes must be a positive integer")
    return value


def _open_parent(path: Path, parent_fd: int | None, flags: int, handles: ExitStack) -> int:
    if os.name == "nt":
        descriptor = handles.enter_context(open_windows_evidence_handle(path, directory=True))
        _require_windows_path(descriptor, path)
    else:
        descriptor = (
            os.open(path, flags | os.O_DIRECTORY)
            if parent_fd is None
            else os.open(path.name, flags | os.O_DIRECTORY, dir_fd=parent_fd)
        )
        handles.callback(os.close, descriptor)
    if _stat_identity(path.lstat())[:2] != _stat_identity(os.fstat(descriptor))[:2]:
        raise ImmutableArtifactConflictError(
            "immutable artifact directory changed before its handle was pinned"
        )
    return descriptor


def _require_windows_path(descriptor: int, path: Path) -> None:
    if os.path.normcase(str(windows_evidence_handle_path(descriptor))) != os.path.normcase(
        str(path)
    ):
        raise ImmutableArtifactConflictError(
            "immutable artifact handle escapes its lexical source path"
        )


def canonical_text_artifact_sha256(payload: str) -> str:
    """Hash the exact UTF-8 bytes emitted by ``publish_text_no_clobber``."""

    return sha256((payload + "\n").encode()).hexdigest()


def require_canonical_text_artifact(
    snapshot: ImmutableArtifactSnapshot,
    canonical_payload: str,
) -> None:
    """Reject alternate serializations that cannot be resolved from a ledger model."""

    if snapshot.file_sha256 != canonical_text_artifact_sha256(canonical_payload):
        raise ImmutableArtifactConflictError("immutable artifact is not canonically serialized")


def assert_artifact_unchanged(snapshot: ImmutableArtifactSnapshot) -> None:
    """Fail when an admitted artifact's identity or content has changed."""

    current, _payload = read_stable_artifact(snapshot.path)
    if current != snapshot:
        raise ImmutableArtifactConflictError("immutable artifact changed after admission")


def publish_text_no_clobber(path: Path, payload: str) -> bool:
    """Publish UTF-8 text through a unique same-directory file without overwrite."""

    return publish_bytes_no_clobber(path, (payload + "\n").encode())


def publish_bytes_no_clobber(path: Path, payload: bytes) -> bool:
    """Publish exact source bytes without altering or replacing an existing artifact."""

    destination = _lexical_absolute(path)
    require_no_reparse_points(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    require_no_reparse_points(destination)
    parent_before = os.stat(destination.parent, follow_symlinks=False)
    encoded = payload
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="xb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            staged = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        published = False
        exact_replay = False
        try:
            os.link(staged, destination)
            published = True
        except FileExistsError:
            if _file_equals_bytes(destination, encoded):
                exact_replay = True
            else:
                raise ImmutableArtifactConflictError(
                    "immutable artifact already exists with different content"
                ) from None
        parent_after = os.stat(destination.parent, follow_symlinks=False)
        if _stat_identity(parent_before)[:2] != _stat_identity(parent_after)[:2]:
            if published and destination.exists() and os.path.samefile(staged, destination):
                destination.unlink()
            raise ImmutableArtifactConflictError(
                "immutable artifact parent changed during publication"
            )
        if not _file_equals_bytes(destination, encoded):
            if published and destination.exists() and os.path.samefile(staged, destination):
                destination.unlink()
            raise ImmutableArtifactConflictError("immutable artifact changed during publication")
        return not exact_replay
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def path_aliases_any(path: Path, protected: set[Path]) -> bool:
    """Detect lexical and existing hardlink aliases without reading file bodies."""

    require_no_reparse_points(path)
    candidate = _lexical_absolute(path)
    for item in protected:
        require_no_reparse_points(item)
        resolved = _lexical_absolute(item)
        if candidate == resolved:
            return True
        try:
            if candidate.exists() and resolved.exists() and os.path.samefile(candidate, resolved):
                return True
        except OSError as exc:
            raise ImmutableArtifactConflictError("artifact alias check failed") from exc
    return False


def population_database_lock_resources(
    database: Path,
    portfolio_database: Path,
) -> tuple[str, ...]:
    """Reserve both the target and canonical writer namespaces before path admission."""

    candidate = _lexical_absolute(database)
    portfolio = _lexical_absolute(portfolio_database)
    require_no_reparse_points(candidate)
    require_no_reparse_points(portfolio)
    return (f"sqlite:{candidate}", "portfolio-db")


def validate_population_database_target(
    database: Path,
    portfolio_database: Path,
) -> Path:
    """Validate one population target after the canonical writer lock is held."""

    candidate = _lexical_absolute(database)
    portfolio = _lexical_absolute(portfolio_database)
    require_no_reparse_points(candidate)
    require_no_reparse_points(portfolio)
    if candidate != portfolio and path_aliases_any(candidate, {portfolio}):
        raise ValueError("population database aliases the portfolio database")
    return candidate


def require_no_reparse_points(path: Path) -> None:
    """Reject symlink/junction substitution in every existing path component."""

    current = _lexical_absolute(path)
    while True:
        is_junction = getattr(current, "is_junction", lambda: False)
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            metadata = None
        attributes = 0 if metadata is None else int(getattr(metadata, "st_file_attributes", 0))
        reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if metadata is not None and (
            current.is_symlink() or bool(is_junction()) or bool(attributes & reparse_attribute)
        ):
            raise ImmutableArtifactConflictError("artifact path contains a reparse point")
        if current.parent == current:
            return
        current = current.parent


def _file_equals_bytes(path: Path, expected: bytes) -> bool:
    try:
        snapshot, payload = read_stable_artifact(path)
        if snapshot.size_bytes != len(expected) or payload != expected:
            return False
        assert_artifact_unchanged(snapshot)
        return True
    except (ImmutableArtifactConflictError, OSError):
        return False


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_handle_identity_matches(path_stat: os.stat_result, handle_stat: os.stat_result) -> bool:
    # Windows path stat can expose creation time while fstat exposes change time.
    # Same-route pre/post comparisons retain both clocks independently.
    fields = 4 if os.name == "nt" else 5
    return _stat_identity(path_stat)[:fields] == _stat_identity(handle_stat)[:fields]


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )
