"""Durable evidence that a best-effort write was DROPPED.

Some writers are deliberately unable to fail.  ``llm_call_ledger.record_call``
is the canonical one: the LLM call that produced the row has already been paid
for, so a telemetry miss must never escalate into a pipeline failure.  That is
the right call-path policy and the wrong observability policy — on 2026-08-02 a
lagging Alembic revision silently ate seven ``llm_calls`` cost rows, and the
only trace was a WARNING line inside a cron log that a human happened to read
days later.  A dropped write that leaves no counter is indistinguishable from a
quiet day.

So every drop also lands here: one JSON line per event, appended to a file
beside the DATABASE rather than inside a checkout.  The anchor matters — this
machine runs the dashboard and the cron fleet from two different checkouts
against one ``portfolio.db``, so a counter under ``<checkout>/.tmp`` would be
written by one and read by neither.  ``runtime.job_runtime`` anchors its
portfolio-db lock the same way, for the same reason.

Deliberately stdlib-only and never-raising: this runs inside the exception
handler of a writer that already failed.  A recorder that can itself throw
would turn a lost telemetry row into a lost pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)

#: Kind written by ``llm_call_ledger.record_call`` when an ``llm_calls`` row
#: could not be persisted (schema drift, a locked database, a missing table).
DROPPED_LLM_LEDGER_WRITE = "llm_ledger_write"

# One rotation, no archive. The question this file answers is "is this
# happening, how often, and what was the last error" — not "replay every drop
# since the beginning of time". Rotating keeps a runaway loop from filling the
# disk the way the 2026-08 backup leak did.
_MAX_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class DroppedWrites:
    """Aggregate of one kind's drops inside a window."""

    kind: str
    count: int
    first_at: datetime
    last_at: datetime
    last_error: str


def health_dir(db_path: str | Path) -> Path:
    """The counter directory for the database at *db_path*."""
    return Path(db_path).resolve().parent / ".health"


def _log_path(kind: str, db_path: str | Path) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "-" for ch in kind) or "unknown"
    return health_dir(db_path) / f"dropped_{safe}.jsonl"


def record_dropped_write(
    kind: str,
    *,
    db_path: str | Path,
    error: str,
    purpose: str | None = None,
    ticker: str | None = None,
) -> None:
    """Append one drop record. Never raises, never blocks the caller."""
    try:
        path = _log_path(kind, db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            # os.replace is atomic; a concurrent appender may lose the line it
            # was mid-write on, which is an acceptable trade at 512 KiB of
            # evidence that something is already very wrong.
            os.replace(path, path.with_suffix(".1.jsonl"))
        line = json.dumps(
            {
                "at": datetime.now(UTC).isoformat(),
                "error": error[:300],
                "purpose": purpose,
                "ticker": ticker,
                "pid": os.getpid(),
            },
            sort_keys=True,
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except (OSError, TypeError, ValueError) as exc:
        log.debug({"event": "dropped_write_record_failed", "kind": kind, "error": str(exc)})


def dropped_writes_since(
    kind: str,
    *,
    db_path: str | Path,
    since: datetime,
) -> DroppedWrites | None:
    """Summarize drops of *kind* at or after *since*, or ``None`` for a clean window.

    A naive *since* is read as UTC — the repo stores naive-UTC stamps in SQLite
    and comparing those against this file's aware stamps would otherwise raise.
    """
    cutoff = since.replace(tzinfo=UTC) if since.tzinfo is None else since
    path = _log_path(kind, db_path)
    count = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    last_error = ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            parsed: object = json.loads(line)
            if not isinstance(parsed, dict):
                continue
            entry = cast("dict[str, object]", parsed)
            at = datetime.fromisoformat(str(entry["at"]))
        except (ValueError, KeyError, TypeError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if at < cutoff:
            continue
        count += 1
        if first_at is None or at < first_at:
            first_at = at
        if last_at is None or at >= last_at:
            last_at = at
            last_error = str(entry.get("error") or "")
    if count == 0 or first_at is None or last_at is None:
        return None
    return DroppedWrites(
        kind=kind,
        count=count,
        first_at=first_at,
        last_at=last_at,
        last_error=last_error,
    )
