"""Shared fail-loud validation for provider rows that may drift in batches."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import TypeAdapter, ValidationError

log = logging.getLogger(__name__)
T = TypeVar("T")
DEFAULT_REJECTION_DIR = Path(__file__).resolve().parents[2] / ".tmp" / "schema_rejections"


class RowValidationDriftError(RuntimeError):
    """A provider batch rejected enough rows to indicate schema drift."""


def _write_rejection(
    *,
    source: str,
    index: int,
    raw: object,
    error: ValidationError,
    rejection_dir: Path,
) -> None:
    rejection_dir.mkdir(parents=True, exist_ok=True)
    safe_source = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in source)
    destination = rejection_dir / f"{safe_source}.jsonl"
    record = {
        "observed_at": datetime.now(UTC).isoformat(),
        "source": source,
        "index": index,
        "errors": error.errors(include_url=False, include_input=False),
        "raw": raw,
    }
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")


def validate_provider_rows(
    rows: Iterable[object],
    adapter: TypeAdapter[T],
    *,
    source: str,
    context: Mapping[str, object] | None = None,
    max_drop_rate: float = 0.10,
    min_samples: int = 20,
    rejection_dir: Path = DEFAULT_REJECTION_DIR,
) -> list[T]:
    """Validate rows, retain isolated rejects, and halt on batch-level drift.

    A small number of malformed rows degrades with an auditable JSONL sample.
    Once a batch is large enough to distinguish an isolated bad record from a
    provider contract change, a drop rate above ``max_drop_rate`` raises.
    """
    if not 0 <= max_drop_rate <= 1:
        raise ValueError("max_drop_rate must be between 0 and 1")
    if min_samples < 1:
        raise ValueError("min_samples must be at least 1")

    accepted: list[T] = []
    attempted = 0
    dropped = 0
    for index, raw in enumerate(rows):
        attempted += 1
        try:
            accepted.append(adapter.validate_python(raw))
        except ValidationError as exc:
            dropped += 1
            _write_rejection(
                source=source,
                index=index,
                raw=raw,
                error=exc,
                rejection_dir=rejection_dir,
            )

    if dropped:
        drop_rate = dropped / attempted
        event = {
            "event": "provider_rows_rejected",
            "source": source,
            "attempted": attempted,
            "dropped": dropped,
            "drop_rate": round(drop_rate, 6),
            **dict(context or {}),
        }
        log.warning(event)
        if attempted >= min_samples and drop_rate > max_drop_rate:
            raise RowValidationDriftError(
                f"{source} rejected {dropped}/{attempted} rows "
                f"({drop_rate:.1%}) above {max_drop_rate:.1%}"
            )
    return accepted


__all__ = [
    "DEFAULT_REJECTION_DIR",
    "RowValidationDriftError",
    "validate_provider_rows",
]
