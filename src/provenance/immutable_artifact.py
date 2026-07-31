"""No-clobber publication for immutable operational evidence artifacts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class ImmutableArtifactConflictError(RuntimeError):
    """An immutable destination already contains different bytes."""


def publish_text_no_clobber(path: Path, payload: str) -> bool:
    """Publish UTF-8 text through a unique same-directory file without overwrite."""

    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (payload + "\n").encode()
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
        try:
            os.link(staged, destination)
        except FileExistsError:
            if _file_equals_bytes(destination, encoded):
                return False
            raise ImmutableArtifactConflictError(
                "immutable artifact already exists with different content"
            ) from None
        return True
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def path_aliases_any(path: Path, protected: set[Path]) -> bool:
    """Detect lexical and existing hardlink aliases without reading file bodies."""

    candidate = path.resolve()
    for item in protected:
        resolved = item.resolve()
        if candidate == resolved:
            return True
        try:
            if candidate.exists() and resolved.exists() and os.path.samefile(candidate, resolved):
                return True
        except OSError:
            continue
    return False


def require_no_reparse_points(path: Path) -> None:
    """Reject symlink/junction substitution in every existing path component."""

    current = path.absolute()
    while True:
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.exists() and (current.is_symlink() or bool(is_junction())):
            raise ImmutableArtifactConflictError("artifact path contains a reparse point")
        if current.parent == current:
            return
        current = current.parent


def _file_equals_bytes(path: Path, expected: bytes) -> bool:
    try:
        if path.stat().st_size != len(expected):
            return False
        with path.open("rb") as handle:
            offset = 0
            while block := handle.read(64 * 1024):
                if block != expected[offset : offset + len(block)]:
                    return False
                offset += len(block)
        return offset == len(expected)
    except OSError:
        return False
