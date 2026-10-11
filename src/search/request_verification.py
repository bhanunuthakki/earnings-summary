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

import sqlite3
from dataclasses import dataclass

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
        self._conn = conn
        self.enabled = enabled
        self.journal: list[VerificationEvent] = []
        self._entries: dict[tuple[str, tuple[str, ...]], tuple[int, object]] = {}
        self._token: int | None = None
        self._in_flight_tokens: dict[tuple[str, tuple[str, ...]], list[int]] = {}

    def ensure_bound(self, conn: sqlite3.Connection) -> None:
        """Fail closed when the memo is used through a different connection."""

        if conn is not self._conn:
            raise VerificationCacheError(
                "in-request verification cache is bound to a different connection; "
                "a sealed request must verify through the connection it opened"
            )

    def verified(self, kind: str, coordinate: tuple[str, ...]) -> object:
        """Return the memoized verification value, or ``NOT_VERIFIED``."""

        token = self._change_token()
        entry = self._entries.get((kind, coordinate))
        if entry is not None and entry[0] == token:
            return entry[1]
        self._in_flight_tokens.setdefault((kind, coordinate), []).append(token)
        return NOT_VERIFIED

    def record(
        self,
        kind: str,
        coordinate: tuple[str, ...],
        value: object,
        *,
        start_token: int | None = None,
    ) -> None:
        """Journal one executed verification and memoize it at the live token."""

        token = self._change_token()
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
        lookup_token = start_token if start_token is not None else (stack.pop() if stack else None)
        if lookup_token is not None and token != lookup_token:
            # An external commit landed while this verification was running;
            # its result cannot be attributed to a stable database view.
            return
        self._entries[(kind, coordinate)] = (token, value)

    def counts(self) -> dict[tuple[str, tuple[str, ...]], int]:
        """Executed-verification counters grouped by kind and coordinate."""

        counts: dict[tuple[str, tuple[str, ...]], int] = {}
        for event in self.journal:
            key = (event.kind, event.coordinate)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _change_token(self) -> int:
        row = self._conn.execute("PRAGMA data_version").fetchone()
        token = int(row[0])
        if self._token != token:
            self._token = token
            self._entries.clear()
        return token
