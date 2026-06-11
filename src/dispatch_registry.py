"""In-memory job registry for dashboard-spawned subprocesses.

A single-flight registry: one running job per (ticker, kind) at a time,
plus a global cap to keep us from saturating the box if the user fires
a bulk action.

Each Job spawns a subprocess (the dispatcher CLI), captures its stdout
line-by-line on a background thread, and exposes a `stream_events()`
generator the SSE endpoint hands to the browser. Lines are buffered so a
late-joining client gets the full log from the start, not just the tail.

Out of scope (intentional): job persistence across server restarts. This
is a single-user localhost tool — losing in-flight jobs on Ctrl+C is
acceptable.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

MAX_CONCURRENT_DEFAULT = 3
POLL_INTERVAL_SEC = 0.1


@dataclass
class Job:
    """One subprocess invocation. Created via `Registry.start()` only."""

    job_id: str
    ticker: str
    kind: str  # e.g. "refresh-stale", "refresh-full" — registry slot key
    argv: list[str]
    started_at: datetime
    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None
    # Working directory for the subprocess (PR6: the tracker-server job runs
    # from the sibling checkout so it finds its own .env / data files). None →
    # inherit this server's cwd, the historical behavior for every other job.
    cwd: str | None = None
    _process: subprocess.Popen | None = field(default=None, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reader: threading.Thread | None = field(default=None, repr=False)

    def _start_subprocess(self) -> None:
        """Spawn the process and a background reader. Idempotent — second call is a no-op."""
        if self._process is not None:
            return
        self._process = subprocess.Popen(
            self.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.cwd,
        )
        self._reader = threading.Thread(target=self._consume_output, daemon=True)
        self._reader.start()

    def _consume_output(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                with self._lock:
                    self.lines.append(line.rstrip("\n"))
        finally:
            self._process.wait()
            self.exit_code = self._process.returncode
            self._done.set()

    @property
    def is_running(self) -> bool:
        return not self._done.is_set()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "ticker": self.ticker,
                "kind": self.kind,
                "started_at": self.started_at.isoformat(),
                "is_running": self.is_running,
                "exit_code": self.exit_code,
                "line_count": len(self.lines),
            }

    def stream_events(self) -> Iterator[str]:
        """Yield SSE-formatted lines from job start through exit.

        Each log line becomes one `event: log` SSE frame. When the
        subprocess exits, one final `event: done` frame carries the exit
        code. Designed for `text/event-stream`.
        """
        sent = 0
        yield _sse(
            {"event": "start", "job_id": self.job_id, "ticker": self.ticker, "kind": self.kind}
        )
        while True:
            with self._lock:
                new_lines = self.lines[sent:]
                sent = len(self.lines)
            for line in new_lines:
                yield _sse({"event": "log", "line": line})
            if self._done.is_set():
                with self._lock:
                    new_lines = self.lines[sent:]
                    sent = len(self.lines)
                for line in new_lines:
                    yield _sse({"event": "log", "line": line})
                yield _sse({"event": "done", "exit_code": self.exit_code})
                return
            time.sleep(POLL_INTERVAL_SEC)


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


class RegistryConflict(Exception):
    """Raised when start() can't accept the request — caller maps to HTTP 409."""


class Registry:
    """Single-flight slot map keyed by (ticker, kind)."""

    def __init__(self, *, max_concurrent: int = MAX_CONCURRENT_DEFAULT) -> None:
        self._max_concurrent = max_concurrent
        self._jobs: dict[str, Job] = {}  # job_id -> Job
        self._slots: dict[tuple[str, str], str] = {}  # (ticker, kind) -> job_id
        self._lock = threading.Lock()

    def start(
        self,
        *,
        ticker: str,
        kind: str,
        argv: list[str],
        spawn: bool = True,
        cwd: str | None = None,
    ) -> Job:
        """Reserve a slot and start the job. Raises RegistryConflict on collision.

        `spawn=False` skips subprocess.Popen for tests that mock the runner.
        `cwd` sets the subprocess working directory (default: inherit).
        """
        ticker = ticker.upper()
        with self._lock:
            slot = (ticker, kind)
            existing_job_id = self._slots.get(slot)
            if existing_job_id is not None:
                existing = self._jobs.get(existing_job_id)
                if existing is not None and existing.is_running:
                    raise RegistryConflict(
                        f"job already running for ticker={ticker} kind={kind}: {existing_job_id}"
                    )
                # Old completed job in the slot — clear it so this new one can take over.
                self._slots.pop(slot, None)

            running_count = sum(1 for j in self._jobs.values() if j.is_running)
            if running_count >= self._max_concurrent:
                raise RegistryConflict(
                    f"concurrent job cap reached ({running_count}/{self._max_concurrent})"
                )

            job_id = f"job_{uuid.uuid4().hex[:10]}"
            job = Job(
                job_id=job_id,
                ticker=ticker,
                kind=kind,
                argv=argv,
                started_at=datetime.now(UTC),
                cwd=cwd,
            )
            self._jobs[job_id] = job
            self._slots[slot] = job_id

        if spawn:
            job._start_subprocess()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict[str, object]]:
        with self._lock:
            return [j.snapshot() for j in self._jobs.values()]

    def gc_completed(self, older_than_sec: float = 3600.0) -> int:
        """Drop completed jobs older than the cutoff. Returns count removed."""
        now = datetime.now(UTC)
        removed = 0
        with self._lock:
            for job_id in list(self._jobs.keys()):
                job = self._jobs[job_id]
                if job.is_running:
                    continue
                age = (now - job.started_at).total_seconds()
                if age >= older_than_sec:
                    self._jobs.pop(job_id)
                    slot = (job.ticker, job.kind)
                    if self._slots.get(slot) == job_id:
                        self._slots.pop(slot)
                    removed += 1
        return removed
