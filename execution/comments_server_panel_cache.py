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


# ---------------------------------------------------------------------------
# Mutation-route → cache-family registry (W6)
#
# Cache keys are request paths (e.g. ``/api/panel/operations?ticker=NU`` or
# ``/api/work-os/briefs?ticker=NU`` — the query-stripped ``full_path``). A
# *family* is a tuple of key prefixes; invalidating a family evicts every
# cached entry whose key starts with one of those prefixes. The registry maps
# each state-changing Flask rule string (``request.url_rule.rule``, e.g.
# ``/comments/<comment_id>``) to the families a success on that route can
# staleness. It is the single authority that replaces the old blanket
# ``panel_cache.clear()`` on every mutation request.
#
# Fail-safe contract: the map is TOTAL over the app's non-GET rule table
# (enforced by a test that walks ``app.url_map``). A rule that is absent
# resolves to ``None`` and the server must evict the ENTIRE cache, so a new
# mutation route can never silently serve a stale fragment.
# ---------------------------------------------------------------------------

# Work OS hydration GETs share the response cache with the panel fragments.
WORK_OS_DESK_KIND = "/api/work-os/companies/"  # every company desk (any ticker)
WORK_OS_BRIEFS_KIND = "/api/work-os/briefs"
WORK_OS_EVALUATION_KIND = "/api/work-os/evaluation"
WORK_OS_PORTFOLIO_KIND = "/api/work-os/portfolio"

FAMILY_WORK_OS_DESK = (WORK_OS_DESK_KIND,)
FAMILY_WORK_OS_BRIEFS = (WORK_OS_BRIEFS_KIND,)
FAMILY_WORK_OS_EVALUATION = (WORK_OS_EVALUATION_KIND,)
FAMILY_WORK_OS_PORTFOLIO = (WORK_OS_PORTFOLIO_KIND,)

# Portfolio-family panels: tracker-derived analytics, the target book, the
# decision/sizing audit trail, and the advisor memo record.
FAMILY_PANEL_PORTFOLIO = (
    "/api/panel/portfolio",
    "/api/panel/performance_risk",
    "/api/panel/portfolio_synthesis",
    "/api/panel/positioning",
    "/api/panel/portfolio_risk",
    "/api/panel/portfolio_health",
    "/api/panel/portfolio_allocation",
    "/api/panel/portfolio_record",
    "/api/panel/red_team",
)

# Decision-ledger panels: the allocation-decisions record, thesis ledger,
# decision journal, and advisor memos.
FAMILY_PANEL_DECISIONS = (
    "/api/panel/decisions_record",
    "/api/panel/thesis_ledger",
    "/api/panel/ledger_decisions",
    "/api/panel/advisor_memos",
)

# Journal/notes panels: analyst notes, the Ledger console, triage, and the
# shared notes drawer (which also surfaces a ticker's recent alerts).
FAMILY_PANEL_JOURNAL = (
    "/api/panel/notes_drawer",
    "/api/panel/journal",
    "/api/panel/triage",
    "/api/panel/musings",
)

# Settings-family panels: editable DCF globals/coverage and ticker settings.
FAMILY_PANEL_SETTINGS = (
    "/api/panel/dcf_globals",
    "/api/panel/ticker_settings",
    "/api/panel/dcf_coverage",
)

# Cockpit/Today overview: open loops reads decisions, decision drafts, tenets,
# reconciliation state, research proposals, and coach-ping queues.
FAMILY_PANEL_OVERVIEW = ("/api/panel/overview",)

# Diet family: the alerts→diet split lane, fed by alert lifecycle mutations.
FAMILY_PANEL_DIET = ("/api/panel/diet",)

# Operations/governance panels: pipeline health, provenance/validation,
# research-exploration queues, and cost/cache instrumentation.
FAMILY_PANEL_OPERATIONS = (
    "/api/panel/operations",
    "/api/panel/cron_health",
    "/api/panel/actions",
    "/api/panel/source_calls",
    "/api/panel/validation",
    "/api/panel/provenance",
    "/api/panel/overrides",
    "/api/panel/credibility",
    "/api/panel/section_coverage",
    "/api/panel/restatements",
    "/api/panel/ir_coverage",
    "/api/panel/evals",
    "/api/panel/model_eval",
    "/api/panel/explore",
    "/api/panel/discovery",
    "/api/panel/data_policy_settings",
)


class _CacheClearAll:
    """Sentinel family: a mutation mapped here evicts the ENTIRE response
    cache. Used deliberately for broad pipeline/ops actions whose writes can
    reach nearly every cached surface."""


CLEAR_ALL = _CacheClearAll()

#: Registry value type: a tuple of cache-key prefixes to invalidate (empty for
#: a declared no-op — the mutation touches no cached surface), or CLEAR_ALL.
CacheFamily = tuple[str, ...] | _CacheClearAll

#: Sentinel family value for mutations that provably touch no cached surface
#: (read-only compute POSTs, telemetry, retired tombstones, and routes that
#: invalidate at their own, more precise moment).
NO_CACHE_FAMILY: tuple[str, ...] = ()

MUTATION_ROUTE_CACHE_REGISTRY: dict[str, CacheFamily] = {
    # ---- Comments (report inline comments → Work OS desk + brief library) ----
    "/comments": (WORK_OS_DESK_KIND, WORK_OS_BRIEFS_KIND),
    "/comments/<comment_id>": (WORK_OS_DESK_KIND, WORK_OS_BRIEFS_KIND),
    # Applying comment resolutions edits the thesis + rebuilds artifacts.
    "/api/comments/process": CLEAR_ALL,
    # Dry-run preview writes nothing.
    "/api/thesis/<ticker>/preview": NO_CACHE_FAMILY,
    # Journalling a triaged note can re-stamp a comment (route action).
    "/api/notes/<int:note_id>/<action>": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_DESK_KIND,
        WORK_OS_BRIEFS_KIND,
    ),
    # ---- Decisions (→ Work OS evaluation/desk + decision-ledger panels) ----
    "/api/decisions/pass": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/decisions/<int:decision_id>/process-quality": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/reconcile/falsifier/<int:decision_id>": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/decision-drafts/<int:draft_id>/confirm": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
        *FAMILY_PANEL_JOURNAL,
    ),
    "/api/decision-drafts/<int:draft_id>/correct": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
        *FAMILY_PANEL_JOURNAL,
    ),
    "/api/decision-drafts/<int:draft_id>/dismiss": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
        *FAMILY_PANEL_JOURNAL,
    ),
    "/api/decision-draft-groups/<int:draft_id>/confirm": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
        *FAMILY_PANEL_JOURNAL,
    ),
    "/api/decision-draft-groups/<int:draft_id>/correct": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
        *FAMILY_PANEL_JOURNAL,
    ),
    "/api/decision-draft-groups/<int:draft_id>/dismiss": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
        *FAMILY_PANEL_JOURNAL,
    ),
    "/api/research/card/<ticker>/refresh": (
        WORK_OS_EVALUATION_KIND,
        WORK_OS_BRIEFS_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/research/card/<int:artifact_id>/<verb>": (
        WORK_OS_EVALUATION_KIND,
        WORK_OS_BRIEFS_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/research/proposal/<int:proposal_id>/<verb>": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        *FAMILY_PANEL_DECISIONS,
        *FAMILY_PANEL_JOURNAL,
    ),
    "/api/research/proposals/<int:proposal_id>/decision": (
        *FAMILY_PANEL_OVERVIEW,
        WORK_OS_EVALUATION_KIND,
        *FAMILY_PANEL_DECISIONS,
        *FAMILY_PANEL_JOURNAL,
    ),
    "/api/research/task/<int:task_id>/run": (
        WORK_OS_EVALUATION_KIND,
        *FAMILY_PANEL_JOURNAL,
    ),
    "/api/research/task/<int:task_id>/reject": (*FAMILY_PANEL_JOURNAL,),
    "/api/research/investment-profile/<ticker>/labels/<label>/<action>": (WORK_OS_EVALUATION_KIND,),
    # ---- Journal / capture / notes ----
    "/api/capture/text": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/onmymind/<int:note_id>/reply": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
    ),
    "/api/onmymind/<int:note_id>/<verb>": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
    ),
    "/api/reconcile/<kind>/<int:item_id>/<verdict>": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_EVALUATION_KIND,
    ),
    "/api/notes": (*FAMILY_PANEL_OVERVIEW, *FAMILY_PANEL_JOURNAL),
    "/api/work-os/question-proposals": (*FAMILY_PANEL_OVERVIEW, *FAMILY_PANEL_JOURNAL),
    "/api/work-os/question-proposals/<int:proposal_id>/approve": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_DESK_KIND,
        WORK_OS_EVALUATION_KIND,
    ),
    "/api/tenets": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_EVALUATION_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/tenets/<int:tenet_id>/<action>": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_EVALUATION_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/tenets/distill": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_EVALUATION_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/profile/fact/<int:fact_id>/affirm": (WORK_OS_EVALUATION_KIND,),
    "/api/profile/fact/<int:fact_id>/reaffirm": (WORK_OS_EVALUATION_KIND,),
    "/api/profile/fact/<int:fact_id>/reject": (WORK_OS_EVALUATION_KIND,),
    "/api/profile/fact/<int:fact_id>/retire": (WORK_OS_EVALUATION_KIND,),
    "/api/profile/fact/<int:fact_id>/update": (WORK_OS_EVALUATION_KIND,),
    # ---- Settings ----
    "/api/dcf-globals": (*FAMILY_PANEL_SETTINGS,),
    "/api/dcf/save": (
        *FAMILY_PANEL_SETTINGS,
        WORK_OS_EVALUATION_KIND,
        *FAMILY_PANEL_PORTFOLIO,
    ),
    "/api/dcf/recompute": NO_CACHE_FAMILY,  # read-only recompute projection
    "/api/ticker-settings/<ticker>": (*FAMILY_PANEL_SETTINGS, *FAMILY_PANEL_DIET),
    "/api/llm-budgets/<purpose>": (*FAMILY_PANEL_OPERATIONS,),
    # ---- Alerts / governed lifecycle ----
    "/api/alerts/<int:alert_id>/dismiss": (*FAMILY_PANEL_JOURNAL, *FAMILY_PANEL_DIET),
    "/api/actions/<int:action_id>/uncancel": (*FAMILY_PANEL_JOURNAL, *FAMILY_PANEL_DIET),
    "/api/thesis-episodes/<episode_id>/acknowledge": (*FAMILY_PANEL_OPERATIONS,),
    "/api/governed-alerts/<int:alert_id>/actions": (
        *FAMILY_PANEL_OPERATIONS,
        *FAMILY_PANEL_JOURNAL,
    ),
    # Queued alert actions can reach nearly any data surface when applied.
    "/approve": CLEAR_ALL,
    # ---- IR approval ----
    "/api/ir-approval/candidates/<candidate_id>/<action>": (
        *FAMILY_PANEL_OPERATIONS,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_BRIEFS_KIND,
    ),
    # ---- Tracker writes / positioning / sizing ----
    "/api/portfolio/policy": (
        *FAMILY_PANEL_PORTFOLIO,
        WORK_OS_PORTFOLIO_KIND,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
    ),
    "/api/position-entries/<int:entry_id>": (
        *FAMILY_PANEL_PORTFOLIO,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
    ),
    "/api/positioning/approve": (*FAMILY_PANEL_PORTFOLIO, WORK_OS_EVALUATION_KIND),
    "/api/positioning/coach": (*FAMILY_PANEL_PORTFOLIO, WORK_OS_EVALUATION_KIND),
    "/api/positioning/confirm-posture": (*FAMILY_PANEL_PORTFOLIO, WORK_OS_EVALUATION_KIND),
    "/api/positioning/propose": (*FAMILY_PANEL_PORTFOLIO,),
    "/api/positioning/simulate": NO_CACHE_FAMILY,  # read-only pre-trade projection
    "/api/sizing-intents": (
        *FAMILY_PANEL_DECISIONS,
        *FAMILY_PANEL_PORTFOLIO,
        WORK_OS_PORTFOLIO_KIND,
        WORK_OS_EVALUATION_KIND,
    ),
    "/api/sizing-intents/<ticker>/checkpoint": (
        *FAMILY_PANEL_DECISIONS,
        *FAMILY_PANEL_PORTFOLIO,
        WORK_OS_PORTFOLIO_KIND,
        WORK_OS_EVALUATION_KIND,
    ),
    "/actions/start-tracker": (
        *FAMILY_PANEL_PORTFOLIO,
        WORK_OS_PORTFOLIO_KIND,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
    ),
    # ---- Discover / explore / views ----
    "/api/discovery/candidates/<int:cand_id>/status": (
        *FAMILY_PANEL_OPERATIONS,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/discovery/candidates/<int:cand_id>/watch": (
        *FAMILY_PANEL_OPERATIONS,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        WORK_OS_BRIEFS_KIND,
    ),
    "/api/discovery/sources/<source_key>/weight": (*FAMILY_PANEL_OPERATIONS,),
    "/api/views": (*FAMILY_PANEL_OPERATIONS,),
    "/api/views/<int:view_id>": (*FAMILY_PANEL_OPERATIONS,),
    "/api/viewspec/run": NO_CACHE_FAMILY,  # executes an existing view
    "/api/viewspec/compile": (*FAMILY_PANEL_OPERATIONS,),  # records LLM calls
    # ---- Research / Red Team / earnings readouts ----
    "/api/earnings-readout/generate": (
        WORK_OS_DESK_KIND,
        WORK_OS_BRIEFS_KIND,
        WORK_OS_PORTFOLIO_KIND,
        WORK_OS_EVALUATION_KIND,
    ),
    "/api/red_team/<int:item_id>/respond": (*FAMILY_PANEL_PORTFOLIO, *FAMILY_PANEL_DECISIONS),
    "/api/socratic/memo": (
        *FAMILY_PANEL_DECISIONS,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
    ),
    "/api/coach/unmute": (WORK_OS_EVALUATION_KIND, *FAMILY_PANEL_DECISIONS),
    "/api/coach/attest-change": (*FAMILY_PANEL_DECISIONS, *FAMILY_PANEL_PORTFOLIO),
    # ---- Ask / sessions (write the session store, render no cached surface) ----
    "/api/ask/sessions/<session_id>": NO_CACHE_FAMILY,
    "/api/ask/stream": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    "/api/ask/sessions/<session_id>/distill": (
        *FAMILY_PANEL_OVERVIEW,
        *FAMILY_PANEL_JOURNAL,
        WORK_OS_EVALUATION_KIND,
        *FAMILY_PANEL_DECISIONS,
    ),
    # ---- Operations attention (invalidates precisely at its OWN success
    # moment inside the route; the global hook stays a no-op for it) ----
    "/api/operations/attention/<action_name>": NO_CACHE_FAMILY,
    # ---- Telemetry: observational, must never evict ----
    "/api/metrics/panel": NO_CACHE_FAMILY,
    # ---- Retired tombstones: never write ----
    "/chat/<ticker>": NO_CACHE_FAMILY,
    "/chat/<ticker>/apply": NO_CACHE_FAMILY,
    # ---- Broad pipeline / ops actions (fail-safe clear-all) ----
    "/actions/refresh": CLEAR_ALL,
    "/actions/maintenance": CLEAR_ALL,
    "/actions/rebuild-dcfs": CLEAR_ALL,
    "/actions/refresh-ir": (
        *FAMILY_PANEL_OPERATIONS,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        WORK_OS_BRIEFS_KIND,
    ),
    "/actions/resolve-issue": (*FAMILY_PANEL_OPERATIONS,),
    "/actions/run-eval": (*FAMILY_PANEL_OPERATIONS,),
    "/actions/run-scenario": (*FAMILY_PANEL_PORTFOLIO,),
    "/actions/socratic-questions": (*FAMILY_PANEL_DECISIONS,),
    "/actions/advisor-memo": (*FAMILY_PANEL_DECISIONS, *FAMILY_PANEL_PORTFOLIO),
    "/actions/position-review": (*FAMILY_PANEL_DECISIONS, *FAMILY_PANEL_PORTFOLIO),
    "/actions/discovery-run": (*FAMILY_PANEL_OPERATIONS,),
    "/actions/discovery-build": (
        *FAMILY_PANEL_OPERATIONS,
        WORK_OS_EVALUATION_KIND,
        WORK_OS_DESK_KIND,
        WORK_OS_BRIEFS_KIND,
    ),
    "/actions/dcf-export": (*FAMILY_PANEL_SETTINGS, WORK_OS_DESK_KIND),
    "/actions/dcf-import": (
        *FAMILY_PANEL_SETTINGS,
        WORK_OS_EVALUATION_KIND,
        *FAMILY_PANEL_PORTFOLIO,
    ),
    "/actions/readme-update": (*FAMILY_PANEL_OPERATIONS,),
}


def resolve_mutation_families(rule: str | None) -> tuple[str, ...] | None:
    """Resolve the cache-key prefixes a mutation on ``rule`` must invalidate.

    Returns a tuple of key prefixes (empty for a declared no-op — the
    mutation touches no cached surface) or ``None`` when the rule is unknown
    OR explicitly registered as CLEAR_ALL. Both mean "evict the entire
    cache", the fail-safe: a new mutation route can never silently serve a
    pre-mutation fragment.
    """
    if rule is None:
        return None
    families = MUTATION_ROUTE_CACHE_REGISTRY.get(rule)
    if families is None or isinstance(families, _CacheClearAll):
        return None
    return families
