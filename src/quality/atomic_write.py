"""Atomic file writes for quality producer CLIs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["write_text_atomic"]


def write_text_atomic(output: Path, payload: str, *, encoding: str = "utf-8") -> None:
    """Write payload through a same-directory temporary file and atomic replace."""
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
