"""Shared data-level memo for ``build_cockpit_rows`` (M2, audit F5).

The three paint-cycle routes — ``/api/panel/overview``,
``/api/work-os/portfolio`` and ``/api/work-os/evaluation`` — each built the
same cockpit rows per response, so a cold paint paid the full roster build two
to three times within the same second while the response cache only
deduplicated per request path. This module memoizes the *data* (the rows
dict), keyed on DATA identity — ``(repo_root, resolved database path, schema
revision)`` — never on the request path, so the three routes share one build
inside the TTL window while each keeps its own response contract (an HTML
fragment with an ETag versus JSON hydrations with ``no-store``).

Staleness contract (validation contract VAL-SERVE-020): the memo TTL matches
the panel cache's 30s, and every entry carries its ``data_as_of`` monotonic
stamp plus the derived ``expires_at`` deadline so a response-cache entry fed
from either a memo hit or a new build cannot extend that deadline. Cached
snapshots expire 30s after the build reservation, including build time. A
slow direct result can still be returned to its caller, but an expired result
is not published to either cache.

Fail-closed degradation: a build failure never publishes (the exception
propagates exactly as before, the reservation is abandoned), and a caller that
cannot reserve or wait (``CockpitDataBusy``) builds directly without
publishing, so the memo can only ever make a response faster, never wrong.

The entry count is bounded with deterministic oldest-first eviction, and the
single-flight reservation pattern mirrors
``execution/comments_server_panel_cache.py`` so concurrent cold requests for
the same data coalesce into one build.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.research_cockpit import CockpitRow
from schema_compat import expected_head

__all__ = [
    "CockpitDataBusy",
    "CockpitDataEntry",
    "CockpitDataHit",
    "CockpitDataMemo",
    "CockpitDataReservation",
    "cockpit_data_memo_key",
]


@dataclass(frozen=True, slots=True)
class CockpitDataEntry:
    """One memoized cockpit-rows snapshot, independent of its insert time.

    ``rows`` stores immutable tuples per role so handing the same snapshot to
    several routes can never let one route's consumer reorder another's
    (``CockpitRow`` itself is a frozen dataclass)."""

    rows: dict[str, tuple[CockpitRow, ...]]
    data_as_of: float  # monotonic clock before the builder starts reading
    expires_at: float  # data_as_of + TTL, in the same monotonic clock


@dataclass(frozen=True, slots=True)
class CockpitDataHit:
    """A fresh snapshot returned without invoking the builder."""

    entry: CockpitDataEntry


@dataclass(frozen=True, slots=True)
class CockpitDataReservation:
    """Exclusive permission to build one memo key."""

    key: str
    generation: int
    data_as_of: float
    ready: threading.Event = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CockpitDataBusy:
    """The wait for an in-flight build expired or capacity is occupied.

    Not an error: the caller degrades to a direct, unpublished build."""


def cockpit_data_memo_key(repo_root: Path, db_path: Path) -> str:
    """The DATA-identity key for one app's cockpit rows.

    Keyed on the repo root (the on-disk caches the builder reads), the
    resolved runtime database path and the checkout's schema revision — never
    on the request path or query string. Two apps in one process bound to two
    different databases therefore never share rows (VAL-SERVE-021), and a
    schema migration or a different database file changes the key outright.
    """
    return "\x1f".join((str(repo_root.resolve()), str(Path(db_path).resolve()), expected_head()))


class CockpitDataMemo:
    """TTL data memo with per-key single-flight builds and a bounded entry set.

    Mirrors the panel response cache's reservation protocol: a miss reserves
    its key, later callers for that key wait for the reservation to be
    published or abandoned, and unrelated keys never share a build lock. A
    waiter whose bounded wait expires gets ``CockpitDataBusy`` and must build
    directly (fail-closed), never a 503.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        max_entries: int = 4,
        wait_seconds: float = 5.0,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if wait_seconds <= 0:
            raise ValueError("wait_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._wait_seconds = wait_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, CockpitDataEntry] = {}
        self._in_flight: dict[str, _InFlight] = {}
        self._generation = 0

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def entry_count(self) -> int:
        """Bounded introspection for the entry-bound contract (VAL-SERVE-023)."""
        with self._lock:
            return len(self._entries)

    def get_or_reserve(self, key: str) -> CockpitDataHit | CockpitDataReservation | CockpitDataBusy:
        """Return a fresh hit or reserve ``key``, waiting only on that key."""
        deadline = time.monotonic() + self._wait_seconds
        while True:
            now = time.monotonic()
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None:
                    if now <= entry.expires_at:
                        return CockpitDataHit(entry)
                    self._entries.pop(key, None)
                in_flight = self._in_flight.get(key)
                if in_flight is None:
                    if len(self._in_flight) >= self._max_entries:
                        return CockpitDataBusy()
                    ready = threading.Event()
                    generation = self._generation
                    self._in_flight[key] = _InFlight(generation, ready)
                    return CockpitDataReservation(key, generation, now, ready)
                ready = in_flight.ready
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not ready.wait(timeout=remaining):
                return CockpitDataBusy()

    def publish(
        self, reservation: CockpitDataReservation, rows: dict[str, list[CockpitRow]]
    ) -> CockpitDataEntry:
        """Store a successful build and release same-key waiters.

        Age starts when the build is reserved, before any database or cache
        read. A slow build consumes its TTL instead of receiving a fresh TTL
        when it finishes. Expired results release waiters without publication.
        """
        data_as_of = reservation.data_as_of
        entry = CockpitDataEntry(
            rows={role: tuple(role_rows) for role, role_rows in rows.items()},
            data_as_of=data_as_of,
            expires_at=data_as_of + self._ttl_seconds,
        )
        with self._lock:
            in_flight = self._in_flight.get(reservation.key)
            if in_flight is None or in_flight.ready is not reservation.ready:
                return entry
            ready = in_flight.ready
            self._in_flight.pop(reservation.key, None)
            if reservation.generation == self._generation and time.monotonic() < entry.expires_at:
                if len(self._entries) >= self._max_entries:
                    oldest_key = min(self._entries, key=lambda k: self._entries[k].data_as_of)
                    self._entries.pop(oldest_key, None)
                self._entries[reservation.key] = entry
        ready.set()
        return entry

    def abandon(self, reservation: CockpitDataReservation) -> None:
        """Release a failed build so one waiter can retry it."""
        with self._lock:
            in_flight = self._in_flight.get(reservation.key)
            if in_flight is None or in_flight.ready is not reservation.ready:
                return
            ready = in_flight.ready
            self._in_flight.pop(reservation.key, None)
        ready.set()

    def clear(self) -> None:
        """Invalidate memoized entries and any result from an active old build.

        Called by the mutation-route hook for every registry family that feeds
        the cockpit rows, and fail-safe for unknown/CLEAR_ALL mutations, so the
        memo's invalidation set is never narrower than the response cache's
        (VAL-SERVE-019)."""
        with self._lock:
            self._generation += 1
            self._entries.clear()
            ready_events = [entry.ready for entry in self._in_flight.values()]
            self._in_flight.clear()
        for ready in ready_events:
            ready.set()


@dataclass(slots=True)
class _InFlight:
    generation: int
    ready: threading.Event
