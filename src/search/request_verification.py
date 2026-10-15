"""In-request verification memo for the sealed Ask retrieval plane.

Audit findings F2/F3 (``.tmp/audit/search.md``): within one sealed turn the
same immutable coordinates were re-derived several times — the full vector
projection re-read/re-hash/verify pass ran at every exact-scan site, and the
promotion, population-cutover, research-snapshot, projection-generation, and
whole retrieval-trace verifications re-ran at every orchestration site
(readiness, execution, and the trace verifications).  This module owns the turn-scoped memo that
collapses those repeated derivations into one executed pass per immutable
coordinate per request.

Contract:

* One cache instance per request, bound to the single connection that serves
  the turn.  Using it through a different connection fails closed.
* Every entry is bounded by the connection's ``PRAGMA data_version`` token,
  which changes exactly when another connection commits a change this
  connection can observe.  The request's own writes (the retrieval trace it
  is building) never touch the sealed coordinates and so do not invalidate
  the memo; any external commit invalidates every entry, so a memoized
  "verified" can never outlive the database state it proved.  A verification
  that completes while the token moves is journaled but never memoized.
* ``enabled=False`` journals every executed verification but stores nothing:
  the un-deduplicated reference instrument for output-preservation proofs on
  the same fixtures.
* The journal is the instrument.  It records every verification that actually
  executed, grouped by kind and immutable coordinate, so a fixture can prove
  each required verification runs once per request.

Synthetic-fixture timings measured through this cache are non-representative
of production latency; its purpose is exactly-once verification work, not
speed.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlite_runtime import ManagedConnection, active_managed_trace_connection_count

_ROLLBACK_PATTERN = re.compile(r"(?is)^(?:\s*(?:/\*.*?\*/|--[^\n]*(?:\n|$)))*\s*rollback\b")

TraceObserver = Callable[[str], None]


def register_connection_trace_observer(
    conn: sqlite3.Connection, observer: TraceObserver
) -> Callable[[], None]:
    """Register a trace observer through the connection-owned dispatcher.

    Returns a callable that unregisters the observer when invoked.
    Fails closed if the connection is not a ManagedConnection, ensuring
    unmanaged connections never have their trace callbacks replaced or wiped.
    """
    if isinstance(conn, ManagedConnection):
        return conn.register_trace_observer(observer)
    raise VerificationCacheError(
        "RequestVerificationCache requires a ManagedConnection to guarantee trace callback "
        f"preservation without replacing caller state; received {type(conn).__name__}"
    )


def active_connection_trace_dispatcher_count() -> int:
    """Return the number of connections currently registered in the trace dispatcher."""
    return active_managed_trace_connection_count()


def active_connection_trace_observer_count(conn: sqlite3.Connection) -> int:
    """Return the number of active observers registered on the given connection."""
    if isinstance(conn, ManagedConnection):
        return conn.trace_observer_count
    return 0


# Kinds of verification work the sealed plane memoizes per request.
SEMANTIC_PROJECTION_READ = "semantic_projection_read_rehash_verify"
SEMANTIC_RUNTIME_COORDINATES = "semantic_runtime_coordinates"
SEMANTIC_RUNTIME_INSTANCE = "semantic_runtime_instance"
PROMOTION_COORDINATES = "promotion_coordinates"
POPULATION_CUTOVER_CURRENT = "population_cutover_current"
POPULATION_RECEIPT = "population_receipt"
RESEARCH_COORDINATES = "research_coordinates"
PROJECTION_GENERATION_ADMISSION = "projection_generation_admission"
RETRIEVAL_TRACE = "retrieval_trace"


class VerificationCacheError(RuntimeError):
    """The in-request verification memo was used outside its contract."""


@dataclass(frozen=True)
class VerificationEvent:
    """One verification that actually executed against the ledger."""

    kind: str
    coordinate: tuple[str, ...]
    change_token: int
    sequence: int


class _NotVerified:
    """Sentinel for a memo miss; distinct from any memoized value."""

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "NOT_VERIFIED"


NOT_VERIFIED = _NotVerified()


class RequestVerificationCache:
    """Turn-scoped memo over immutable verification coordinates.

    The memo is intentionally not a cross-request cache: a fresh instance is
    created per sealed request and dies with it, so a new turn always pays a
    full verification pass over the current state.
    """

    def __init__(self, conn: sqlite3.Connection, *, enabled: bool = True) -> None:
        if not isinstance(conn, ManagedConnection):
            raise VerificationCacheError(
                "RequestVerificationCache requires a ManagedConnection to guarantee trace callback "
                f"preservation without replacing caller state; received {type(conn).__name__}"
            )
        self._conn = conn
        self.enabled = enabled
        self.journal: list[VerificationEvent] = []
        self._entries: dict[tuple[str, tuple[str, ...]], tuple[tuple[int, int], object]] = {}
        self._token: int | None = None
        self._epoch: int = 0
        self._in_flight_tokens: dict[tuple[str, tuple[str, ...]], list[tuple[int, int]]] = {}
        self._expected_changes: int = conn.total_changes
        self._closed: bool = False
        self._unregister_trace: Callable[[], None] | None = register_connection_trace_observer(
            conn, self._on_trace_sql
        )

    @property
    def is_closed(self) -> bool:
        """Whether the cache has completed its lifecycle and closed."""
        return self._closed

    @property
    def entry_count(self) -> int:
        """Return the number of currently memoized entries."""
        return len(self._entries)

    @property
    def in_flight_count(self) -> int:
        """Return the number of active in-flight verification tokens."""
        return len(self._in_flight_tokens)

    def _assert_not_closed(self) -> None:
        if self._closed:
            raise VerificationCacheError(
                "RequestVerificationCache is closed and cannot perform verification operations"
            )

    def _on_trace_sql(self, statement: str) -> None:
        if _ROLLBACK_PATTERN.match(statement):
            self._entries.clear()
            self._epoch += 1
            self._expected_changes = self._conn.total_changes

    def close(self) -> None:
        """Release trace callback registration on the connection and release cached entries."""

        if self._closed:
            return
        self._closed = True
        if self._unregister_trace is not None:
            self._unregister_trace()
            self._unregister_trace = None
        self._entries.clear()
        self._in_flight_tokens.clear()

    def __enter__(self) -> RequestVerificationCache:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def ensure_bound(self, conn: sqlite3.Connection) -> None:
        """Fail closed when the memo is used through a different connection."""

        self._assert_not_closed()
        if conn is not self._conn:
            raise VerificationCacheError(
                "in-request verification cache is bound to a different connection; "
                "a sealed request must verify through the connection it opened"
            )

    @contextmanager
    def authorized_write(self) -> Generator[None, None, None]:
        """Acknowledge authorized same-connection trace writes without invalidating memo."""

        self._assert_not_closed()
        before = self._conn.total_changes
        try:
            yield
        finally:
            delta = self._conn.total_changes - before
            if delta > 0:
                self._expected_changes += delta

    @contextmanager
    def savepoint(self, name: str) -> Generator[None, None, None]:
        """Execute within a SQLite savepoint; roll back and invalidate memo on error."""

        self._assert_not_closed()
        self._conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self._conn.execute(f"RELEASE SAVEPOINT {name}")
            self.invalidate()
            raise
        self._conn.execute(f"RELEASE SAVEPOINT {name}")

    def notify_rollback(self) -> None:
        """Notify the cache that a rollback occurred, invalidating all memoized entries."""

        self._assert_not_closed()
        self.invalidate()

    def _check_same_connection_mutation(self) -> None:
        """Clear memoized entries and advance epoch when unacknowledged same-connection writes occur."""

        current_changes = self._conn.total_changes
        if current_changes != self._expected_changes:
            self._entries.clear()
            self._epoch += 1
            self._expected_changes = current_changes

    def invalidate(self) -> None:
        """Explicitly invalidate all memoized entries."""

        self._assert_not_closed()
        self._entries.clear()
        self._epoch += 1
        self._expected_changes = self._conn.total_changes

    def _state_token(self) -> tuple[int, int]:
        token = self._change_token()
        return (token, self._epoch)

    def verified(self, kind: str, coordinate: tuple[str, ...]) -> object:
        """Return the memoized verification value, or ``NOT_VERIFIED``."""

        self._assert_not_closed()
        state = self._state_token()
        entry = self._entries.get((kind, coordinate))
        if entry is not None and entry[0] == state:
            return entry[1]
        self._in_flight_tokens.setdefault((kind, coordinate), []).append(state)
        return NOT_VERIFIED

    def record(
        self,
        kind: str,
        coordinate: tuple[str, ...],
        value: object,
        *,
        start_token: tuple[int, int] | int | None = None,
    ) -> None:
        """Journal one executed verification and memoize it at the live token."""

        self._assert_not_closed()

        token = self._change_token()
        current_state = (token, self._epoch)
        self.journal.append(
            VerificationEvent(
                kind=kind,
                coordinate=coordinate,
                change_token=token,
                sequence=len(self.journal),
            )
        )
        if not self.enabled:
            return
        stack = self._in_flight_tokens.get((kind, coordinate))
        lookup_state = start_token if start_token is not None else (stack.pop() if stack else None)
        if lookup_state is not None:
            if isinstance(lookup_state, tuple) and lookup_state != current_state:
                # Database mutated (external commit, unacknowledged write, rollback,
                # or explicit invalidation) while verification was running; do not memoize.
                return
            if isinstance(lookup_state, int) and lookup_state != current_state[0]:
                return
        self._entries[(kind, coordinate)] = (current_state, value)

    def counts(self) -> dict[tuple[str, tuple[str, ...]], int]:
        """Executed-verification counters grouped by kind and coordinate."""

        counts: dict[tuple[str, tuple[str, ...]], int] = {}
        for event in self.journal:
            key = (event.kind, event.coordinate)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _change_token(self) -> int:
        self._check_same_connection_mutation()
        row = self._conn.execute("PRAGMA data_version").fetchone()
        token = int(row[0])
        if self._token != token:
            self._token = token
            self._entries.clear()
            self._epoch += 1
        return token
