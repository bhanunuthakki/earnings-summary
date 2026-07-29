"""Deterministic material-input fingerprints for pipeline idempotency keys."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path

from pipeline.run_accounting import JsonValue

_CHUNK_SIZE = 1024 * 1024


def _display_path(path: Path, *, root: Path | None) -> str:
    resolved = path.resolve()
    if root is not None:
        with suppress(ValueError):
            resolved = resolved.relative_to(root.resolve())
    return resolved.as_posix()


def file_fingerprint(path: Path, *, root: Path | None = None) -> dict[str, JsonValue]:
    """Return stable identity and bytes hash for one material file input."""
    display_path = _display_path(path, root=root)
    if not path.is_file():
        return {"path": display_path, "exists": False}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return {
        "path": display_path,
        "exists": True,
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def files_fingerprint(paths: Iterable[Path], *, root: Path | None = None) -> list[JsonValue]:
    """Fingerprint a path set in deterministic display-path order."""
    unique = {path.resolve(): path for path in paths}
    return [
        file_fingerprint(path, root=root)
        for path in sorted(unique.values(), key=lambda item: _display_path(item, root=root))
    ]


def payload_sha256(value: Mapping[str, JsonValue] | list[JsonValue]) -> str:
    """Hash a JSON-compatible material payload using canonical serialization."""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
