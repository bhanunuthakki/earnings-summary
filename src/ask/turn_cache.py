"""In-process turn-level caches for the Ask conversational path (L14).

WHY THIS EXISTS — the architecture verification (close_the_loops L14, 2026-06-13).
The conversational transport is a COLD ``claude -p`` subprocess (``src/llm/cli.py``
+ ``chat_session.stream_llm_text``) that re-encodes the entire thread + evidence
into a fresh prompt every turn. We verified what caching that envelope supports:

  * There is NO CLI mechanism to inject Anthropic prompt-cache breakpoints
    (``cache_control: {type: "ephemeral"}``) onto a caller-supplied prompt
    prefix. ``claude --help`` exposes ``--json-schema`` / ``--system-prompt`` /
    budgets, but nothing to mark cache breakpoints on stdin. Claude Code manages
    ``cache_control`` internally, on ITS OWN system prompt + tool defs only.
  * Each cold ``-p`` subprocess is a fresh API conversation, so prompt caching
    gives ZERO cross-invocation hits: ``cache_read_input_tokens`` (already
    captured in the ledger) stays 0. The 5-minute ephemeral TTL never helps a
    separate process.
  * ``--resume`` / ``--session-id`` WOULD let an API cache read the unchanged
    prefix, but the transport runs from a neutral cwd to dodge MCP boot (sessions
    are cwd-bound), can't send "only the new turn", re-boots/re-reads per call,
    and the evidence block changes every turn so the cacheable prefix is just the
    small static header. And billing is the user's SUBSCRIPTION, not metered, so
    token-cost savings aren't even the goal — LATENCY is. ``--bare`` would cut
    startup cost but forces ANTHROPIC_API_KEY billing (OAuth/keychain never
    read), breaking the subscription path — rejected.

So native prompt caching is unreachable. The fallback (the directive's plan) is
to stop REDOING the stable, question-independent work every turn and to skip
redundant interactive LLM round-trips. Three process-local caches do that:

  corpus  — the heavy filing-JSON / transcript-file read+flatten+squash is
            question-INDEPENDENT and dominates retrieval wall-clock; memoized by
            (path, mtime, size). Only the cheap keyword re-scoring runs per
            question. Identical results, pure speedup. (``ask.grounding``)
  route   — the pack-router's fast-model call is memoized per normalized
            question (TTL), so a repeated question skips the round-trip.
            (``ask.router``)
  gather  — a whole ``gather_evidence`` result is memoized per (thread, scope,
            normalized question, db-mtime) so an exact-repeat turn skips the
            router + packs + every scan. (``ask.grounding``)

Every cache is bounded (LRU), freshness-validated (file/db mtime for the
content-keyed ones, a short TTL for the question-keyed ones), and reset by
``clear_all()`` — the autouse test fixture calls it so cached state can never
leak between tests (and the route cache can never hand one test another test's
monkeypatched router result).

Process-local + lightly locked: the only consumer is the single-user localhost
research server, which serves turns on worker threads. A ``threading.Lock``
around each cache's get/put is enough; there is no cross-process sharing to
worry about (and none is wanted — the win is intra-conversation reuse).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar, cast

_K = TypeVar("_K")
_V = TypeVar("_V")


class _LruTtlCache(Generic[_K, _V]):
    """A bounded LRU cache with an optional per-entry TTL.

    ``ttl_seconds <= 0`` means no expiry (freshness is carried by the key
    itself — e.g. the content caches fold the file mtime into the key, so a
    changed file is simply a different key and the stale entry ages out by LRU).
    Tracks ``hits`` / ``misses`` for the latency harness.
    """

    def __init__(self, *, max_entries: int, ttl_seconds: float) -> None:
        self._max = max(1, max_entries)
        self._ttl = ttl_seconds
        self._data: OrderedDict[_K, tuple[float, _V]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: _K) -> _V | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            ts, value = entry
            if self._ttl > 0 and (now - ts) > self._ttl:
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: _K, value: _V) -> None:
        now = time.monotonic()
        with self._lock:
            self._data[key] = (now, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0


# ---------------------------------------------------------------------------
# Cache instances. Sizes are generous for a single-user book (a few dozen
# tracked names, a handful of filings/transcripts each) yet bounded so a long
# server uptime can't grow them without limit.
# ---------------------------------------------------------------------------

# Content-keyed: the key carries the file's (path, mtime, size), so a changed
# file is a fresh key and the old parse ages out by LRU — no TTL needed.
_CORPUS_MAX = 512
_corpus: _LruTtlCache[tuple[str, int, int], object] = _LruTtlCache(
    max_entries=_CORPUS_MAX, ttl_seconds=0
)

# Question-keyed: the router maps a question to packs deterministically enough
# that reusing the decision for a few minutes is correct and free. Process-wide
# (the mapping doesn't depend on the session) with a short TTL for freshness.
_ROUTE_TTL_SECONDS = 300.0
_route: _LruTtlCache[str, tuple[str, ...]] = _LruTtlCache(
    max_entries=256, ttl_seconds=_ROUTE_TTL_SECONDS
)

# (thread, scope, question, db-mtime)-keyed full-retrieval memo. Short TTL so a
# mid-session file change self-heals quickly; the db mtime in the key already
# busts it on any DB write. Value is a list of frozen EvidenceItem (returned as
# a fresh copy so a caller can never mutate the cached list).
_GATHER_TTL_SECONDS = 60.0
_gather: _LruTtlCache[tuple[object, ...], list[object]] = _LruTtlCache(
    max_entries=256, ttl_seconds=_GATHER_TTL_SECONDS
)


# ---------------------------------------------------------------------------
# Corpus (content) cache — question-independent file parse reuse.
# ---------------------------------------------------------------------------


def cached_file_parse(path: Path, builder: Callable[[Path], _V | None]) -> _V | None:
    """Memoize an expensive parse of ``path`` keyed on (resolved path, mtime,
    size).

    ``builder(path)`` runs only on a miss; a ``None`` result (unreadable /
    unparseable file) is NOT cached, so the caller retries next turn exactly as
    the pre-cache code re-read the file each turn. A cached EMPTY result (``[]``)
    is a hit and returned as-is — an empty-but-valid file is not re-read.

    Returns ``None`` when the file can't be ``stat``'d (missing/raced) or when
    ``builder`` returns ``None``; callers treat that as "this file contributes
    nothing", matching the prior ``continue``-on-error behavior.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _corpus.get(key)
    if hit is not None:
        # The shared corpus cache stores heterogeneous parses as ``object``;
        # the builder fixes _V per call site, so the cached hit IS a _V.
        return cast("_V", hit)
    value = builder(path)
    if value is not None:
        _corpus.put(key, value)
    return value


# ---------------------------------------------------------------------------
# Route cache — skip the pack-router round-trip on a repeated question.
# ---------------------------------------------------------------------------


def normalize_question(question: str) -> str:
    """Case/whitespace-folded question — the key for the route + gather caches.

    Conservative on purpose: only case and runs of whitespace are folded, so a
    hit means the same question, not merely a similar one. Two genuinely
    different questions never collide; the win is real repeats (a retry, a
    refresh, the engine re-running the same turn)."""
    return " ".join(question.lower().split())


def get_route(norm_question: str) -> tuple[str, ...] | None:
    """A previously-resolved pack tuple for this normalized question, or None."""
    if not norm_question:
        return None
    return _route.get(norm_question)


def put_route(norm_question: str, packs: tuple[str, ...]) -> None:
    """Cache a SUCCESSFUL router resolution (only the ``ok`` path calls this —
    gated/skipped/error stay uncached so their state-dependent contract is
    re-evaluated every turn)."""
    if norm_question:
        _route.put(norm_question, packs)


# ---------------------------------------------------------------------------
# Gather memo — skip re-retrieval entirely on an exact-repeat turn.
# ---------------------------------------------------------------------------


def gather_key(
    *,
    cache_key: str,
    repo_root: Path,
    scope_tickers: list[str],
    norm_question: str,
    db_token: int,
) -> tuple[object, ...]:
    """The full-retrieval memo key. ``cache_key`` scopes it to one thread (a
    session id, or ticker:report_date) so two conversations never share a memo;
    ``db_token`` is the DB mtime so any write busts every entry."""
    scope = tuple(sorted({t.strip().upper() for t in scope_tickers if t.strip()}))
    return (cache_key, str(repo_root), scope, norm_question, db_token)


def get_gather(key: tuple[object, ...]) -> list[object] | None:
    """The cached evidence list for this key, as a FRESH copy (the items are
    frozen, so a shallow copy fully isolates the caller from the cache)."""
    hit = _gather.get(key)
    return list(hit) if hit is not None else None


def put_gather(key: tuple[object, ...], items: list[object]) -> None:
    _gather.put(key, list(items))


def db_mtime_token(db_path: Path) -> int:
    """A cheap freshness token for the DB — its mtime in ns, 0 when absent."""
    try:
        return db_path.stat().st_mtime_ns
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Test + observability surface
# ---------------------------------------------------------------------------


def clear_all() -> None:
    """Reset every cache. The autouse test fixture calls this so cached state
    never leaks across tests (most importantly the route cache, which would
    otherwise hand one test another test's monkeypatched router result)."""
    _corpus.clear()
    _route.clear()
    _gather.clear()


def stats() -> dict[str, dict[str, int]]:
    """hits/misses per cache — consumed by execution/ask_latency_baseline.py."""
    return {
        "corpus": {"hits": _corpus.hits, "misses": _corpus.misses},
        "route": {"hits": _route.hits, "misses": _route.misses},
        "gather": {"hits": _gather.hits, "misses": _gather.misses},
    }


__all__ = [
    "cached_file_parse",
    "clear_all",
    "db_mtime_token",
    "gather_key",
    "get_gather",
    "get_route",
    "normalize_question",
    "put_gather",
    "put_route",
    "stats",
]
