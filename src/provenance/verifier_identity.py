"""Path-free source-artifact identity for strict deterministic verifiers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

_MAX_FILES = 64
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024


def verifier_source_artifact_sha256(files: Mapping[str, Path]) -> str:
    """Hash an explicit verifier closure without persisting physical paths."""

    if not files or len(files) > _MAX_FILES:
        raise ValueError("verifier source artifact file count is outside its bound")
    members: list[dict[str, object]] = []
    total_bytes = 0
    for logical_name, path in sorted(files.items()):
        _validate_logical_name(logical_name)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"verifier source member {logical_name!r} is unavailable") from exc
        if len(payload) > _MAX_FILE_BYTES:
            raise ValueError(f"verifier source member {logical_name!r} exceeds its byte bound")
        total_bytes += len(payload)
        if total_bytes > _MAX_TOTAL_BYTES:
            raise ValueError("verifier source artifact exceeds its total byte bound")
        members.append(
            {
                "logical_name": logical_name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    canonical = json.dumps(
        {"schema_version": 1, "members": members},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_logical_name(value: str) -> None:
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("verifier source logical name is invalid")
