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
inside ``hold_writer_gate(db_path, ...)`` (or the connection seam below) and
closes it before releasing the gate, so at most one writer window per resolved
database path is in flight at any instant — across processes, not just threads.

Serialization scope — who is actually gated (scrutiny 2026-10-08 correction):

* Chain participants are the daily-brief driver process AND every descendant
  process it launches while the run is active. The driver marks the run by
  exporting ``EARNINGS_SUMMARY_DAILY_CHAIN_DB`` (the resolved chain database
  path) for the duration of ``main()``; every child inherits the environment,
  so the mark propagates to all descendants automatically.
* Inside a chain participant, EVERY ``SQLiteConnectionRole.WRITER`` open on
  the marked database is serialized at the connection seam
  (``sqlite_runtime.connect_sqlite``), for the connection's whole lifetime
  (open through close). This covers the explicit write windows AND the
  auxiliary best-effort writers that open their own writer connections outside
  those windows: ``sources.registry.log_call`` / ``log_calls_batch`` /
  ``log_http_measurement`` (``source_calls`` / source-attempt telemetry),
  and — under the production ``--enable-llm`` flag — the ``llm_calls`` cost
  ledger, ``llm_artifacts`` upserts, ``llm_budget_alerts``, and the
  ``llm_circuit_breakers`` quota-breaker persistence. No chain writer open can
  escape the gate by opening a connection the explicit windows forgot.
* Exclusions, deliberately: processes that are NOT chain participants (the
  cockpit, pollers, other crons, a standalone ``refresh_dcf`` invoked by hand)
  keep exactly their current behavior — their writer connections ride
  ``SQLITE_BUSY_TIMEOUT_MS`` and never touch the gate, even while a chain gate
  interval is open. A chain participant opening a writer connection on a
  DIFFERENT database file (e.g. a section helper's own resolved path) is also
  ungated: the invariant is per resolved database path. ``refresh_dcf
  apply_edits`` (the cockpit's ``/api/dcf/save`` request path) is not a chain
  child of the daily pool and stays on the busy timeout.

Deliberately NOT the production run lock:

* ``run_lock`` (``<db>.write.lock``) is the whole-job write-set lock for
  scheduled batch mutations (db_gc, the morning orchestrator). The daily chain
  runs *under* that doctrine, not as a peer of it: using the run lock as a
  per-write mutex would make a minutes-long chain step contend with — or
  starve — whole-job holders, and a step child could never acquire a run lock
  its own driver lineage needs to own. This gate uses its own lock file
  (``<db>.writer-gate.lock``) and its own advisory protocol.

Protocol (the run lock's hardened house style): the lock file sits next to the
database, is claimed atomically by ``os.link`` of a fully written temp file
(so a lock file is never observed half-written), carries a JSON payload
identifying the holder (pid, owner label, acquired_at, token), and a lock whose
recorded pid is no longer alive is stale and gets broken. A persistent native
OS guard serializes every claim, stale identity/liveness recheck, replacement,
and token-checked release. Its descriptor is closed after each short transition;
the guard file stays in place so contenders always lock the same inode. Acquisition blocks
with a bounded timeout and then fails LOUD (``WriterGateHeldError``) — a chain
step surfaces as a failed stage rather than silently extending its budget.
The deadline bounds acquisition, not the holder's connection lifetime. The
gate is not FIFO and cannot promise that a waiter succeeds under continuous
contention. A timed-out caller must release its own resources and surface the
blocked stage; it may retry after the live holder closes. Never evict a live
holder to make the queue advance.

Best-effort telemetry writers that must never raise (``log_call``, the LLM
cost ledger, artifact upserts, budget alerts) catch that error, count the
dropped write (``telemetry_health.record_dropped_write``), and WARN — a gate
timeout is observable, never a silently lost row.

Re-entrancy: acquiring the same resolved database path again on the SAME thread
is a no-op (counted, no second file claim), so a participant can wrap a wide
window (e.g. the DCF refresh's ``BEGIN IMMEDIATE`` block) while a shared helper
inside it also holds the gate — or opens its own gated connection — without
self-deadlock. Threads of one process queue on an in-process lock first, so a
second thread waits for the first to release instead of fighting its own
process over the lock file; a release triggered by garbage collection may run
on any thread (depths are keyed by the opening thread).

Diagnostics: setting ``EARNINGS_SUMMARY_WRITER_GATE_TRACE`` to a file path
appends one JSON line per event from every participating process — gate
intervals (``enter``/``exit``) and, from the connection seam, actual
writer-connection lifetimes (``writer_open``/``writer_close``, each carrying
``pid``, ``conn``, ``chain`` and ``file_claim`` — the last says whether that
open took the lock file itself or rode an enclosing claim). Together they prove
"maximum simultaneous WRITER-role opens per database path is 1 across pids" and
"every chain writer open held the gate" with real child processes. Unset
(production default) costs one environment lookup per window.
"""

from __future__ import annotations

import errno
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
#: Marks THIS process (and, via environment inheritance, every descendant) as a
#: daily-chain participant whose WRITER-role connections on the named resolved
#: database path must take the writer gate. The daily-brief driver sets it for
#: the duration of its run (``chain_writer_scope``); nothing else sets it.
CHAIN_WRITER_DB_ENV = "EARNINGS_SUMMARY_DAILY_CHAIN_DB"


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
    """Missing/malformed is explicit stale evidence; unreadable stays unknown."""
    try:
        parsed: object = json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else None


@dataclass(slots=True)
class _HeldGate:
    """A claimed writer-gate lock file. Release exactly once."""

    lock_path: Path
    pid: int
    token: str

    def release(self) -> None:
        with _ownership_transition_guard(
            self.lock_path, deadline=time.monotonic() + WRITER_GATE_TIMEOUT_S
        ):
            holder = _read_holder(self.lock_path)
            # Compare the exact claim under the same guard used by takeover.
            # A successor in this process may have the same PID, but not token.
            if holder is None or holder.get("pid") != self.pid or holder.get("token") != self.token:
                return
            with suppress(FileNotFoundError):
                self.lock_path.unlink()


# ---------------------------------------------------------------------------
# Chain marker: who counts as a daily-chain participant
# ---------------------------------------------------------------------------


def chain_writer_db() -> str | None:
    """This process's marked chain database key, or ``None`` (not a chain)."""
    raw = os.environ.get(CHAIN_WRITER_DB_ENV, "").strip()
    if not raw:
        return None
    return _gate_key(raw)


def chain_gate_required(db_path: str | os.PathLike[str]) -> bool:
    """Whether a WRITER open on ``db_path`` must take the chain writer gate.

    True only inside a marked chain process AND only for the marked database
    (by resolved path). Everything else — unmarked processes, other database
    files — keeps its existing behavior.
    """
    chain_key = chain_writer_db()
    if chain_key is None:
        return False
    return _gate_key(db_path) == chain_key


@contextmanager
def chain_writer_scope(db_path: str | os.PathLike[str]) -> Generator[None]:
    """Mark this process (and its future children) as a daily-chain run.

    The daily-brief driver wraps its run in this scope: every WRITER-role
    connection it or a descendant opens on the resolved chain database is
    serialized by the gate for the connection's lifetime, while the process is
    inside the scope. Exit restores the prior environment exactly, so the
    driver process stops being a chain participant the moment the run ends and
    nothing leaks into callers that embed ``main()``.
    """
    raw = os.fspath(db_path)
    key = _gate_key(raw)
    if key is None:
        yield
        return
    previous = os.environ.get(CHAIN_WRITER_DB_ENV)
    os.environ[CHAIN_WRITER_DB_ENV] = key
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(CHAIN_WRITER_DB_ENV, None)
        else:
            os.environ[CHAIN_WRITER_DB_ENV] = previous


# ---------------------------------------------------------------------------
# Per-path gate state: one in-process lock + per-thread re-entrancy depths
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PathGateState:
    """In-process coordination for one resolved database path.

    ``inprocess`` is held from the first depth claim by ANY thread of this
    process until the last depth release, so sibling threads queue locally
    instead of polling a lock file their own pid owns. ``depths`` counts
    re-entrancy per OPENING thread (keyed by ``threading.get_ident()``) so a
    release triggered by garbage collection on a different thread still
    decrements the right counter.
    """

    inprocess: threading.Lock
    depths: dict[int, int]
    held: _HeldGate | None


_STATE_LOCK = threading.Lock()
_PATH_STATES: dict[str, _PathGateState] = {}


def _path_state(key: str) -> _PathGateState:
    with _STATE_LOCK:
        state = _PATH_STATES.get(key)
        if state is None:
            state = _PathGateState(inprocess=threading.Lock(), depths={}, held=None)
            _PATH_STATES[key] = state
        return state


def _fail_loud(key: str) -> None:
    lock_path = Path(key).with_name(Path(key).name + WRITER_GATE_SUFFIX)
    raise WriterGateHeldError(lock_path, _read_holder(lock_path)) from None


def _acquire_path_gate(
    key: str,
    *,
    owner: str,
    timeout_s: float,
    poll_s: float,
) -> bool:
    """Claim ``key``'s gate for the CURRENT thread (re-entrant, bounded).

    Fast path: the opening thread already holds a depth on this path — count
    it and return ``False`` (nested windows never self-deadlock and never
    re-claim the file). Slow path: queue on the in-process lock (so only one
    thread of this process polls the lock file), then claim the lock file
    itself; both waits share the one bounded ``timeout_s`` budget. Returns
    ``True`` when this call took the process's single claim.
    """
    state = _path_state(key)
    tid = threading.get_ident()
    with _STATE_LOCK:
        current = state.depths.get(tid, 0)
    if current > 0:
        with _STATE_LOCK:
            state.depths[tid] = current + 1
        return False
    deadline = time.monotonic() + max(0.0, timeout_s)
    # A failed acquire must NEVER release: the lock is held by a sibling
    # thread that is still inside its window.
    if not state.inprocess.acquire(timeout=max(0.0, deadline - time.monotonic())):
        _fail_loud(key)
    lock_path = Path(key).with_name(Path(key).name + WRITER_GATE_SUFFIX)
    try:
        held = _acquire_writer_gate(
            lock_path, owner=owner, timeout_s=max(0.0, deadline - time.monotonic()), poll_s=poll_s
        )
    except BaseException:
        state.inprocess.release()
        raise
    with _STATE_LOCK:
        state.depths[tid] = 1
        state.held = held
    return True


def _release_path_gate(key: str, *, owner_tid: int) -> None:
    """Release one depth claimed by ``owner_tid`` (any releasing thread).

    Hitting zero releases the lock file and the in-process lock. Double
    releases (explicit close plus the GC backstop) are tolerated.
    """
    state = _path_state(key)
    with _STATE_LOCK:
        current = state.depths.get(owner_tid, 0)
        if current == 0:
            return
        if current > 1:
            state.depths[owner_tid] = current - 1
            return
        state.depths.pop(owner_tid, None)
        held = state.held
        state.held = None
    try:
        if held is not None:
            held.release()
    finally:
        state.inprocess.release()


# ---------------------------------------------------------------------------
# Trace instrumentation (opt-in)
# ---------------------------------------------------------------------------


def writer_connection_trace_enabled() -> bool:
    """Whether the connection seam should record writer-lifetime trace lines."""
    return bool(os.environ.get(_TRACE_ENV, "").strip())


def _trace_event(
    event: str,
    *,
    owner: str,
    db_key: str,
    extra: dict[str, object] | None = None,
) -> None:
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
            **(extra or {}),
        },
        sort_keys=True,
    )
    with suppress(OSError), open(path, "a", encoding="utf-8") as trace:
        trace.write(line + "\n")


def trace_writer_connection(
    event: str,
    *,
    db_path: str | os.PathLike[str],
    owner: str,
    conn_id: str,
    chain: bool,
    file_claim: bool,
) -> None:
    """Record one ``writer_open``/``writer_close`` at the connection seam.

    ``chain`` is True for every connection the chain seam serialized;
    ``file_claim`` additionally says THIS open took the lock file itself
    (False for a nested open riding an enclosing claim, which is inside the
    same held gate).
    """
    key = _gate_key(db_path)
    if key is None:
        return
    _trace_event(
        event,
        owner=owner,
        db_key=key,
        extra={"conn": conn_id, "chain": chain, "file_claim": file_claim},
    )


# ---------------------------------------------------------------------------
# Lock-file protocol (atomic claim, stale breaking, bounded wait)
# ---------------------------------------------------------------------------


@contextmanager
def _ownership_transition_guard(lock_path: Path, *, deadline: float) -> Generator[None]:
    """Serialize claim/recheck/unlink with one persistent, crash-released OS lock.

    Never delete the guard inode: a replacement would permit two independent
    native locks. All acquisition waits share the writer's existing deadline.
    """
    guard_path = lock_path.with_name(f"{lock_path.name}.guard")
    descriptor = os.open(guard_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WriterGateHeldError(lock_path, _read_holder(lock_path)) from None
                time.sleep(min(WRITER_GATE_POLL_S, remaining))
            else:
                break
        yield
    finally:
        # Closing releases the byte-range lock/flock even after an exception.
        os.close(descriptor)


def _acquire_writer_gate(
    lock_path: Path,
    *,
    owner: str,
    timeout_s: float,
    poll_s: float,
) -> _HeldGate:
    """Claim ``lock_path`` or raise ``WriterGateHeldError`` within ``timeout_s``."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "owner": owner,
            "acquired_at": now_naive_utc().isoformat(),
            "argv": sys.argv,
            "token": token,
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
            with _ownership_transition_guard(lock_path, deadline=deadline):
                try:
                    os.link(tmp_path, lock_path)
                except FileExistsError:
                    # Read identity and liveness only while ownership changes
                    # are excluded. A pre-guard observation is never authority.
                    holder = _read_holder(lock_path)
                    holder_pid = holder.get("pid") if holder else None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WriterGateHeldError(lock_path, holder) from None
                    if not (isinstance(holder_pid, int) and pid_is_alive(holder_pid)):
                        try:
                            lock_path.unlink()
                        except FileNotFoundError:
                            pass
                        except OSError:
                            pass
                        else:
                            # Retain the guard through replacement: a second
                            # stale contender must observe this new live claim.
                            os.link(tmp_path, lock_path)
                            return _HeldGate(lock_path, os.getpid(), token)
                else:
                    return _HeldGate(lock_path, os.getpid(), token)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WriterGateHeldError(lock_path, _read_holder(lock_path)) from None
            time.sleep(min(poll_s, remaining))
    finally:
        with suppress(OSError):
            tmp_path.unlink()


# ---------------------------------------------------------------------------
# Public gate windows
# ---------------------------------------------------------------------------


@contextmanager
def hold_writer_gate(
    db_path: str | os.PathLike[str],
    *,
    owner: str,
    timeout_s: float | None = None,
    poll_s: float = WRITER_GATE_POLL_S,
) -> Generator[None]:
    """Serialize one database's chain write windows across processes.

    ``owner`` labels the holder in the lock payload and the trace (use the
    chain step + ticker). Re-entrant per thread: a nested hold of the same
    database path from the same thread is counted and takes no second claim.
    Sibling threads of one process queue on the in-process lock first. A held
    gate fails LOUD (``WriterGateHeldError``) after ``timeout_s`` (default
    ``WRITER_GATE_TIMEOUT_S``, read at call time). In-memory databases have
    no file to guard and pass through ungated.
    """
    key = _gate_key(db_path)
    if key is None:
        yield
        return
    wait = WRITER_GATE_TIMEOUT_S if timeout_s is None else timeout_s
    claimed = _acquire_path_gate(key, owner=owner, timeout_s=wait, poll_s=poll_s)
    entered = False
    primary_error: BaseException | None = None
    try:
        if claimed:
            _trace_event("enter", owner=owner, db_key=key)
            entered = True
        yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            try:
                if entered:
                    _trace_event("exit", owner=owner, db_key=key)
            finally:
                _release_path_gate(key, owner_tid=threading.get_ident())
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                f"writer gate cleanup also failed: {type(cleanup_error).__name__}"
            )


@dataclass(slots=True)
class WriterConnectionClaim:
    """One chain-gated WRITER connection's hold on the gate.

    ``release()`` is idempotent and thread-agnostic (the GC backstop may fire
    on any thread); the connection seam calls it when the connection closes.
    ``claimed`` records whether THIS claim took the process's single file
    claim (vs a nested fast-path hold inside an already-open interval).
    """

    key: str
    owner: str
    owner_tid: int
    claimed: bool
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            if self.claimed:
                _trace_event("exit", owner=self.owner, db_key=self.key)
        finally:
            _release_path_gate(self.key, owner_tid=self.owner_tid)


def claim_chain_writer_connection(
    db_path: str | os.PathLike[str],
    *,
    owner: str,
    timeout_s: float | None = None,
) -> WriterConnectionClaim | None:
    """Claim the chain writer gate for one WRITER connection's lifetime.

    The connection seam (``sqlite_runtime.connect_sqlite``) calls this before
    opening a ``SQLiteConnectionRole.WRITER`` connection. Returns ``None``
    when ``db_path`` is not this process's chain-marked database — non-chain
    callers (and other database files) keep their existing behavior and never
    touch the gate. Inside a chain participant the claim blocks behind any
    open writer window on the same path and raises ``WriterGateHeldError``
    when the bounded wait expires.
    """
    key = _gate_key(db_path)
    if key is None or not chain_gate_required(db_path):
        return None
    wait = WRITER_GATE_TIMEOUT_S if timeout_s is None else timeout_s
    claimed = _acquire_path_gate(key, owner=owner, timeout_s=wait, poll_s=WRITER_GATE_POLL_S)
    claim = WriterConnectionClaim(
        key=key, owner=owner, owner_tid=threading.get_ident(), claimed=claimed
    )
    try:
        if claimed:
            _trace_event("enter", owner=owner, db_key=key)
    except BaseException as error:
        try:
            claim.release()
        except BaseException as cleanup_error:
            error.add_note(f"writer gate cleanup also failed: {type(cleanup_error).__name__}")
        raise
    return claim


__all__ = [
    "CHAIN_WRITER_DB_ENV",
    "WRITER_GATE_POLL_S",
    "WRITER_GATE_TIMEOUT_S",
    "WriterConnectionClaim",
    "WriterGateHeldError",
    "chain_gate_required",
    "chain_writer_db",
    "chain_writer_scope",
    "claim_chain_writer_connection",
    "hold_writer_gate",
    "trace_writer_connection",
    "writer_connection_trace_enabled",
    "writer_gate_path_for",
]
