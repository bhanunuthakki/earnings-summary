"""Deferred-FMP-task log — a durable, idempotent backlog of work blocked on FMP.

Motivation
----------
Several data-quality fixes can't run until FMP access is restored / a feed is
pulled: back-adjusting split-contaminated per-share history with the authoritative
splits feed, re-pulling clean analyst consensus to overwrite the contaminated
cache, re-authing a rejected FMP key, etc. Previously these lived only in a human's
memory or a hand-maintained markdown backlog. This module gives them a structured,
self-populating store: the split-normalization guard AUTO-LOGS an entry whenever it
quarantines a series it can't reconcile, so the backlog fills itself instead of
relying on anyone remembering.

Store
-----
A newline-delimited JSON file (`data/deferred_fmp/deferred_fmp.jsonl`), one
`DeferredFmpTask` per line. JSONL (not SQLite) because the store is a small,
append-mostly, human-auditable backlog that wants to be diff-friendly and readable
without a DB client; it is git-tracked (see .gitignore negation) so the backlog
survives cache wipes.

Idempotency
-----------
`log_deferred` dedupes on the natural key ``(area, task, ticker)``: re-logging the
same blocked item updates its context/timestamp in place rather than appending a
duplicate. This makes it safe to call from the ingest loop on every run — a
ticker that quarantines every day produces exactly one open row, not one per day.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from clock import now_iso


class DeferredStatus(StrEnum):
    OPEN = "open"
    DONE = "done"


class DeferredFmpTask(BaseModel):
    """One unit of work blocked on FMP access / a feed being restored.

    The natural key is ``(area, task, ticker)`` — `log_deferred` dedupes on it.
    `ticker` is optional (some tasks are global, e.g. re-auth the key).
    """

    model_config = ConfigDict(extra="forbid")

    area: str  # coarse bucket, e.g. "split_normalization", "consensus_cache", "auth"
    task: str  # short imperative description of the blocked work
    blocked_on: str  # what must be restored, e.g. "fmp_splits_feed", "fmp_api_key"
    ticker: str | None = None
    context: str = ""  # free-form detail: root cause, dump paths, factors seen
    status: DeferredStatus = DeferredStatus.OPEN
    created_at: str = Field(default_factory=lambda: _now_iso())
    updated_at: str = Field(default_factory=lambda: _now_iso())

    def dedupe_key(self) -> tuple[str, str, str]:
        return (self.area, self.task, self.ticker or "")


def _now_iso() -> str:
    return now_iso()


_DEFAULT_REL = Path("data") / "deferred_fmp" / "deferred_fmp.jsonl"


def default_store_path(project_root: Path) -> Path:
    return project_root / _DEFAULT_REL


def _read_all(store_path: Path) -> list[DeferredFmpTask]:
    """Load every task from the JSONL store. A malformed line halts loudly rather
    than being silently skipped — a corrupt backlog is a bug to surface, not to
    paper over."""
    if not store_path.exists():
        return []
    tasks: list[DeferredFmpTask] = []
    for raw in store_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        tasks.append(DeferredFmpTask.model_validate_json(line))
    return tasks


def _write_all(store_path: Path, tasks: list[DeferredFmpTask]) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(t.model_dump_json() for t in tasks)
    store_path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def log_deferred(
    task: DeferredFmpTask,
    store_path: Path,
) -> tuple[DeferredFmpTask, bool]:
    """Append `task` to the store, or update the existing row with the same
    ``(area, task, ticker)`` key. Returns ``(stored_task, created)`` where
    `created` is True on first insert, False when an existing open row was
    refreshed. Idempotent: same inputs → same open row, updated_at bumped.

    A task whose key already exists as DONE is reopened only if the incoming
    status is OPEN (a fix that regressed); otherwise the DONE row is preserved."""
    tasks = _read_all(store_path)
    key = task.dedupe_key()
    for i, existing in enumerate(tasks):
        if existing.dedupe_key() == key:
            # Preserve a completed row unless we're explicitly reopening it.
            if existing.status is DeferredStatus.DONE and task.status is DeferredStatus.DONE:
                return (existing, False)
            updated = existing.model_copy(
                update={
                    "blocked_on": task.blocked_on,
                    "context": task.context or existing.context,
                    "status": task.status,
                    "updated_at": _now_iso(),
                }
            )
            tasks[i] = updated
            _write_all(store_path, tasks)
            return (updated, False)
    tasks.append(task)
    _write_all(store_path, tasks)
    return (task, True)


def list_tasks(
    store_path: Path,
    *,
    status: DeferredStatus | None = DeferredStatus.OPEN,
) -> list[DeferredFmpTask]:
    """Return tasks, filtered to `status` (default: open only; pass None for all),
    newest-first by created_at."""
    tasks = _read_all(store_path)
    if status is not None:
        tasks = [t for t in tasks if t.status is status]
    return sorted(tasks, key=lambda t: t.created_at, reverse=True)


def mark_done(
    area: str,
    task: str,
    ticker: str | None,
    store_path: Path,
) -> bool:
    """Flip the matching task to DONE. Returns True when a row was updated."""
    tasks = _read_all(store_path)
    key = (area, task, ticker or "")
    for i, existing in enumerate(tasks):
        if existing.dedupe_key() == key:
            tasks[i] = existing.model_copy(
                update={"status": DeferredStatus.DONE, "updated_at": _now_iso()}
            )
            _write_all(store_path, tasks)
            return True
    return False
