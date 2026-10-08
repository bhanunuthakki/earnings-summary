"""Cross-process serialization for one SQLite database's pipeline write windows.

The daily brief worker overlaps independent tickers' chains on a bounded
process pool (``execution/daily_fetch_and_brief.py``). The driver and the
writer-capable step subprocesses it launches (thesis evaluator, commitment
matcher, DCF refresh, artifact build, and the DCF builders the refresh step
spawns) all mutate the same database file, and they run in DIFFERENT processes
— so an in-process lock cannot serialize them. Before this gate existed the
pool's single-writer guarantee held only inside the driver process: at the
shipped pool width three step subprocesses could each hold a writer connection
open on one file at once, relying on ``SQLITE_BUSY_TIMEOUT_MS`` to absorb the
contention.

This module is the process-safe serialization layer for those actual write
windows. A writer window is the span in which a chain participant holds an open
``SQLiteConnectionRole.WRITER`` connection (or an explicit ``BEGIN IMMEDIATE``)
against the database. Every chain participant opens its writer connection only
inside ``hold_writer_gate(db_path, ...)`` and closes it before releasing the
gate, so at most one writer window per resolved database path is in flight at
any instant — across processes, not just threads.

Deliberately NOT the production run lock:

* ``run_lock`` (``<db>.write.lock``) is the whole-job write-set lock for
  scheduled batch mutations (db_gc, the morning orchestrator). The daily chain
  runs *under* that doctrine, not as a peer of it: using the run lock as a
  per-write mutex would make a minutes-long chain step contend with — or
  starve — whole-job holders, and a step child could never acquire a run lock
  its own driver lineage needs to own. This gate uses its own lock file
  (``<db>.writer-gate.lock``) and its own advisory protocol.
* Short-transaction writers that are NOT chain participants (the cockpit,
  pollers, the LLM cost ledger) deliberately keep riding
  ``SQLITE_BUSY_TIMEOUT_MS`` exactly as before; the gate arbitrates only among
  the chain participants that take it, and it never changes behavior for anyone
  else.

Protocol (the run lock's hardened house style): the lock file sits next to the
database, is claimed atomically by ``os.link`` of a fully written temp file
(so a lock file is never observed half-written), carries a JSON payload
identifying the holder (pid, owner label, acquired_at, token), and a lock whose
recorded pid is no longer alive is stale and gets broken. Acquisition blocks
with a bounded timeout and then fails LOUD (``WriterGateHeldError``) — a chain
step surfaces as a failed stage rather than silently extending its budget.

Re-entrancy: acquiring the same resolved database path again on the SAME thread
is a no-op (counted, no second file claim), so a participant can wrap a wide
window (e.g. the DCF refresh's ``BEGIN IMMEDIATE`` block) while a shared helper
inside it also holds the gate without self-deadlock.

Diagnostics: setting ``EARNINGS_SUMMARY_WRITER_GATE_TRACE`` to a file path
appends one JSON line per window enter/exit from every participating process —
the instrument that proves "maximum simultaneous writer count per database
path is 1" with real child subprocesses. Unset (production default) costs one
environment lookup per window.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from clock import now_naive_utc
from run_lock import pid_is_alive

WRITER_GATE_SUFFIX = ".writer-gate.lock"
#: Generous default wait: the chain's longest legit window (the DCF refresh's
#: promote-and-swap transaction) is seconds-scale; a wait beyond this means a
#: live holder is stuck, which must fail loud instead of hanging the chain.
WRITER_GATE_TIMEOUT_S = 120.0
#: Fast handoff between the chain's short windows; still cheap to poll.
WRITER_GATE_POLL_S = 0.02

_TRACE_ENV = "EARNINGS_SUMMARY_WRITER_GATE_TRACE"

_LOCAL = threading.local()


class WriterGateHeldError(RuntimeError):
    """The per-database writer gate was not free within its bounded wait."""

    def __init__(self, lock_path: Path, holder: dict[str, object] | None) -> None:
        self.lock_path = lock_path
        self.holder = holder or {}
        who = self.holder.get("owner", "unknown")
        pid = self.holder.get("pid", "?")
        since = self.holder.get("acquired_at", "?")
        super().__init__(f"writer gate {lock_path} held by owner={who!r} pid={pid} since {since}")


def writer_gate_path_for(db_path: str | os.PathLike[str]) -> Path:
    """The gate lock file serializing ``db_path``'s chain write windows."""
    raw = os.fspath(db_path)
    if raw == ":memory:":
        raise ValueError("in-memory databases have no writer gate file")
    resolved = Path(raw).expanduser().resolve(strict=False)
    return resolved.with_name(resolved.name + WRITER_GATE_SUFFIX)


def _gate_key(db_path: str | os.PathLike[str]) -> str | None:
    """Resolve the gate key: one key per database file; ``None`` for :memory:."""
    raw = os.fspath(db_path)
    if raw == ":memory:":
        return None
    return str(Path(raw).expanduser().resolve(strict=False))


def _read_holder(lock_path: Path) -> dict[str, object] | None:
    try:
        parsed: object = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else None


@dataclass(slots=True)
class _HeldGate:
    """A claimed writer-gate lock file. Release exactly once."""

    lock_path: Path
    pid: int

    def release(self) -> None:
        holder = _read_holder(self.lock_path)
        # Only remove a lock file this process still owns — never clobber a
        # successor that legitimately broke our lock while we were suspended.
        if holder is not None and holder.get("pid") != self.pid:
            return
        with suppress(OSError):
            self.lock_path.unlink()


def _trace_event(event: str, *, owner: str, db_key: str) -> None:
    """Append one instrumentation line when the trace env var is configured."""
    path = os.environ.get(_TRACE_ENV, "").strip()
    if not path:
        return
    line = json.dumps(
        {
            "event": event,
            "owner": owner,
            "pid": os.getpid(),
            "db": db_key,
            "t": time.time(),
        },
        sort_keys=True,
    )
    with suppress(OSError), open(path, "a", encoding="utf-8") as trace:
        trace.write(line + "\n")


def _acquire_writer_gate(
    lock_path: Path,
    *,
    owner: str,
    timeout_s: float,
    poll_s: float,
) -> _HeldGate:
    """Claim ``lock_path`` or raise ``WriterGateHeldError`` within ``timeout_s``."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "owner": owner,
            "acquired_at": now_naive_utc().isoformat(),
            "argv": sys.argv,
            "token": uuid4().hex,
        }
    )
    deadline = time.monotonic() + max(0.0, timeout_s)
    # The lock file must become visible ALREADY CARRYING its payload: write the
    # payload to a private temp file, then claim the lock name atomically —
    # os.link fails with FileExistsError if the name is taken, giving
    # O_EXCL semantics on a fully-written file (run_lock's hardening).
    tmp_path = lock_path.with_name(f"{lock_path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    try:
        while True:
            try:
                os.link(tmp_path, lock_path)
            except FileExistsError:
                holder = _read_holder(lock_path)
                holder_pid = holder.get("pid") if holder else None
                if not (isinstance(holder_pid, int) and pid_is_alive(holder_pid)):
                    # Stale: dead holder, or an unreadable payload — which,
                    # because creation is atomic-with-payload, can only be
                    # real corruption, never a half-written live lock.
                    with suppress(OSError):
                        lock_path.unlink()
                    continue
                if time.monotonic() >= deadline:
                    raise WriterGateHeldError(lock_path, holder) from None
                time.sleep(poll_s)
                continue
            return _HeldGate(lock_path=lock_path, pid=os.getpid())
    finally:
        with suppress(OSError):
            tmp_path.unlink()


def _thread_depths() -> dict[str, int]:
    depths: dict[str, int] | None = getattr(_LOCAL, "depths", None)
    if depths is None:
        depths = {}
        _LOCAL.depths = depths
    return depths


@contextmanager
def hold_writer_gate(
    db_path: str | os.PathLike[str],
    *,
    owner: str,
    timeout_s: float = WRITER_GATE_TIMEOUT_S,
    poll_s: float = WRITER_GATE_POLL_S,
) -> Generator[None]:
    """Serialize one database's chain write windows across processes.

    ``owner`` labels the holder in the lock payload and the trace (use the
    chain step + ticker). Re-entrant per thread: a nested hold of the same
    database path from the same thread is counted and takes no second claim.
    In-memory databases have no file to guard and pass through ungated.
    """
    key = _gate_key(db_path)
    if key is None:
        yield
        return
    depths = _thread_depths()
    current = depths.get(key, 0)
    if current > 0:
        depths[key] = current + 1
        try:
            yield
        finally:
            depths[key] = depths.get(key, 1) - 1
        return
    lock_path = Path(key).with_name(Path(key).name + WRITER_GATE_SUFFIX)
    held = _acquire_writer_gate(lock_path, owner=owner, timeout_s=timeout_s, poll_s=poll_s)
    depths[key] = 1
    _trace_event("enter", owner=owner, db_key=key)
    try:
        yield
    finally:
        _trace_event("exit", owner=owner, db_key=key)
        held.release()
        depths[key] = 0


__all__ = [
    "WRITER_GATE_POLL_S",
    "WRITER_GATE_TIMEOUT_S",
    "WriterGateHeldError",
    "hold_writer_gate",
    "writer_gate_path_for",
]
