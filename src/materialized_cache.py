"""Small, shared contract for render-path JSON materializations.

Readers perform one bounded file read and no network, database, or LLM work.
New payloads are schema/version stamped and expire from ``computed_at``;
legacy payloads remain readable during migration but expire from file mtime.
Stale or incompatible payloads fail closed without deleting the last-good file,
so the next scheduled materializer can replace it atomically.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

MATERIALIZED_CACHE_VERSION = 1
MATERIALIZED_CACHE_TTL = timedelta(hours=48)
_MAX_FUTURE_SKEW = timedelta(minutes=5)


def cache_metadata(schema: str, *, now: datetime | None = None) -> dict[str, object]:
    """Return the required metadata for a newly materialized payload."""
    stamp = now or datetime.now(UTC)
    return {
        "cache_schema": schema,
        "cache_version": MATERIALIZED_CACHE_VERSION,
        "computed_at": stamp.astimezone(UTC).replace(tzinfo=None).isoformat(),
    }


def write_payload_atomically(path: Path, payload: dict[str, object], *, prefix: str) -> None:
    """Publish complete JSON with a same-directory atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def read_fresh_payload(
    path: Path,
    *,
    schema: str,
    ttl: timedelta = MATERIALIZED_CACHE_TTL,
    now: datetime | None = None,
) -> dict[str, object]:
    """Read one compatible, fresh payload or return ``{}``.

    Payloads written before this contract lacked cache metadata. For those only,
    file mtime is the freshness clock. This explicit compatibility branch keeps
    an in-place upgrade from blanking the UI before the next morning refresh.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        decoded = json.loads(raw)
    except (OSError, ValueError, TypeError, OverflowError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    payload = cast("dict[str, object]", decoded)
    observed_schema = payload.get("cache_schema")
    observed_version = payload.get("cache_version")
    if observed_schema is None and observed_version is None:
        generated = modified
    else:
        if observed_schema != schema or observed_version != MATERIALIZED_CACHE_VERSION:
            return {}
        computed_at = payload.get("computed_at")
        if not isinstance(computed_at, str):
            return {}
        try:
            generated = datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
        except ValueError:
            return {}
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=UTC)
        else:
            generated = generated.astimezone(UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    else:
        reference = reference.astimezone(UTC)
    age = reference - generated
    if age > ttl or age < -_MAX_FUTURE_SKEW:
        return {}
    return payload


__all__ = [
    "MATERIALIZED_CACHE_TTL",
    "MATERIALIZED_CACHE_VERSION",
    "cache_metadata",
    "read_fresh_payload",
    "write_payload_atomically",
]
