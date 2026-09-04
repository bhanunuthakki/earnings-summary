"""Shared deterministic payload digest for ``src/allocation/``.

Single source of truth for the byte-for-byte canonical JSON SHA-256 used by
the eligibility gate and the deterministic frontier ``input_sha`` fields.
"""

from __future__ import annotations

import hashlib
import json

__all__ = ["allocation_payload_sha"]


def allocation_payload_sha(payload: object) -> str:
    """SHA-256 hex of canonical JSON: sorted keys, compact separators, UTF-8."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
