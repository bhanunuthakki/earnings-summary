"""Typed conversion-record registry for admitted test-database conversions.

A converted test file no longer replays the Alembic chain, so it stops counting
toward the replay-reduction numerator. The registry is where that claim is
recorded and where it is checked: each record binds the file to the exact
post-conversion content it was verified against and to a PASS parity receipt for
the ``migrated_db`` invocation that replaced the chain builder.

Every failure mode is a violation rather than a silent pass. A record whose
recorded content hash no longer matches the file on disk is stale: the file
changed after the parity evidence was produced, so the conversion claim is
withdrawn and the file counts as replaying again.
"""

from __future__ import annotations

import json
import posixpath
from datetime import datetime
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from quality.test_db_invocations import is_canonical_issue, is_canonical_reason
from quality.test_db_models import ConversionRecord, ConversionRegistry

CONVERSION_REGISTRY_PATH = "docs/quality/test-db-conversions.json"
CONVERSION_REGISTRY_SCHEMA = "test-db-conversions/v1"

__all__ = [
    "CONVERSION_REGISTRY_PATH",
    "CONVERSION_REGISTRY_SCHEMA",
    "ConversionRecord",
    "ConversionRegistry",
    "RegistryVerdict",
    "evaluate_registry",
    "load_registry",
]


class RegistryVerdict(BaseModel):
    """Which converted paths the registry admits, and why the rest failed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    converted_paths: tuple[str, ...] = Field(default_factory=tuple)
    violations: tuple[str, ...] = Field(default_factory=tuple)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _is_canonical_path(value: str) -> bool:
    if len(value) == 0 or len(value) > 256:
        return False
    if value != value.strip():
        return False
    if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    if value.startswith("/") or posixpath.isabs(value):
        return False
    if ".." in PurePosixPath(value).parts:
        return False
    if posixpath.normpath(value) != value:
        return False
    return not ("//" in value or value.startswith("./") or value.endswith("/"))


def load_registry(raw: bytes) -> ConversionRegistry:
    """Decode registry bytes, rejecting duplicate JSON keys and unknown fields.

    The duplicate-key scan runs first because ``json.loads`` keeps the last
    value for a repeated key, so a typed parse alone would silently accept a
    registry that reads two different ways.
    """
    text = raw.decode("utf-8")
    payload: object = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(payload, dict):
        raise ValueError("conversion registry must be a JSON object")
    return ConversionRegistry.model_validate_json(text)


def _record_violation(record: ConversionRecord, kind: str) -> str:
    return f"{kind}:{record.path}"


def _admitted(
    record: ConversionRecord,
    *,
    file_sha256: dict[str, str],
    migrated_invocations: dict[str, frozenset[str]],
    now: datetime,
) -> str | None:
    """Return the violation kind for a record, or ``None`` when it is admitted."""
    if not _is_canonical_path(record.path):
        return "conversion-record-invalid"
    if record.path not in file_sha256:
        return "conversion-record-unknown-path"
    if not is_canonical_issue(record.owner_issue):
        return "conversion-record-invalid"
    if not is_canonical_reason(record.reason):
        return "conversion-record-invalid"
    if record.expires_at.tzinfo is None or record.expires_at.utcoffset() is None:
        return "conversion-record-invalid"
    if record.expires_at <= now:
        return "conversion-record-expired"
    receipt = record.parity_receipt
    if receipt.path != record.path or receipt.source_sha256 != record.source_sha256:
        return "conversion-record-invalid"
    if file_sha256[record.path] != record.source_sha256:
        return "conversion-record-stale"
    # The receipt names the invocation that replaced the chain builder. Without
    # this check a record could admit a file that never adopted the template.
    if receipt.invocation_id not in migrated_invocations.get(record.path, frozenset()):
        return "conversion-record-unproven"
    return None


def evaluate_registry(
    registry: ConversionRegistry,
    *,
    file_sha256: dict[str, str],
    migrated_invocations: dict[str, frozenset[str]],
    now: datetime,
) -> RegistryVerdict:
    """Admit the records that still hold, and report every other record.

    ``file_sha256`` maps each in-scope test path to the content hash the scan
    actually read. ``migrated_invocations`` maps each path to the invocation ids
    the scan classified as ``migrated_db`` calls.
    """
    counts: dict[str, int] = {}
    for record in registry.records:
        counts[record.path] = counts.get(record.path, 0) + 1
    violations: set[str] = set()
    admitted: set[str] = set()
    for record in registry.records:
        if counts[record.path] > 1:
            # A repeated path has no single authoritative record, so neither
            # occurrence may admit the file.
            violations.add(_record_violation(record, "conversion-record-duplicate"))
            continue
        kind = _admitted(
            record,
            file_sha256=file_sha256,
            migrated_invocations=migrated_invocations,
            now=now,
        )
        if kind is None:
            admitted.add(record.path)
        else:
            violations.add(_record_violation(record, kind))
    return RegistryVerdict(
        converted_paths=tuple(sorted(admitted)),
        violations=tuple(sorted(violations)),
    )
