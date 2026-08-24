"""Bounded response cache with per-key single-flight panel rendering."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PanelCacheEntry:
    """One rendered panel response, independent of its insertion time."""

    body: bytes
    content_type: str
    etag: str


@dataclass(frozen=True, slots=True)
class PanelCacheHit:
    """A fresh response returned without invoking the panel builder."""

    entry: PanelCacheEntry


@dataclass(frozen=True, slots=True)
class PanelCacheReservation:
    """Exclusive permission to build one cache key."""

    key: str
    generation: int
    ready: threading.Event = field(repr=False, compare=False)


@dataclass(slots=True)
class _InFlight:
    generation: int
    ready: threading.Event


class PanelResponseCache:
    """TTL response cache that coalesces only identical concurrent keys.

    A miss reserves its key. Later callers for that key wait for the reservation
    to be stored or abandoned, while callers for unrelated keys proceed without
    sharing a build lock.
    """

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, PanelCacheEntry]] = {}
        self._in_flight: dict[str, _InFlight] = {}
        self._generation = 0

    def get_or_reserve(self, key: str) -> PanelCacheHit | PanelCacheReservation:
        """Return a fresh hit or reserve ``key``, waiting only on that key."""
        while True:
            now = time.monotonic()
            with self._lock:
                cached = self._entries.get(key)
                if cached is not None:
                    inserted_at, entry = cached
                    if now - inserted_at <= self._ttl_seconds:
                        return PanelCacheHit(entry)
                    self._entries.pop(key, None)

                in_flight = self._in_flight.get(key)
                if in_flight is None:
                    ready = threading.Event()
                    generation = self._generation
                    self._in_flight[key] = _InFlight(generation, ready)
                    return PanelCacheReservation(key, generation, ready)
                ready = in_flight.ready
            ready.wait()

    def store(self, reservation: PanelCacheReservation, entry: PanelCacheEntry) -> None:
        """Publish a reserved build and release same-key waiters."""
        ready: threading.Event | None = None
        with self._lock:
            in_flight = self._in_flight.get(reservation.key)
            if in_flight is None or in_flight.ready is not reservation.ready:
                return
            ready = in_flight.ready
            self._in_flight.pop(reservation.key, None)
            if reservation.generation == self._generation:
                if len(self._entries) >= self._max_entries:
                    oldest_key = min(self._entries, key=lambda key: self._entries[key][0])
                    self._entries.pop(oldest_key, None)
                self._entries[reservation.key] = (time.monotonic(), entry)
        ready.set()

    def abandon(self, reservation: PanelCacheReservation) -> None:
        """Release a failed build so one waiter can retry it."""
        ready: threading.Event | None = None
        with self._lock:
            in_flight = self._in_flight.get(reservation.key)
            if in_flight is None or in_flight.ready is not reservation.ready:
                return
            ready = in_flight.ready
            self._in_flight.pop(reservation.key, None)
        ready.set()

    def clear(self) -> None:
        """Invalidate cached entries and any result from an active old build."""
        with self._lock:
            self._generation += 1
            self._entries.clear()

    def invalidate_prefix(self, prefix: str) -> None:
        """Invalidate one bounded panel family without evicting unrelated panels."""
        ready_events: list[threading.Event] = []
        with self._lock:
            for key in tuple(self._entries):
                if key.startswith(prefix):
                    self._entries.pop(key, None)
            for key, in_flight in tuple(self._in_flight.items()):
                if key.startswith(prefix):
                    self._in_flight.pop(key, None)
                    ready_events.append(in_flight.ready)
        for ready in ready_events:
            ready.set()
