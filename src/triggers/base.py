"""Trigger framework — Protocol contract and shared draft shapes.

The Personal CIO alerting layer (alembic 0060–0064) fans into five
trigger kinds: KPI inflection, earnings-tone shift, SayDo verdict
coming due, thesis-anchor drift, material news. Each sensor implements
the same lifecycle:

    scan(ticker, db)        -> list[TriggerCandidate]      # cheap query
    should_fire(c, state)   -> bool                        # gating
    build_alert(c, anchor)  -> AlertDraft                  # construct event
    draft_actions(a, c)     -> list[QueuedActionDraft]     # propose mutations

This module owns the contract; per-trigger modules (``kpi_inflection``,
...) own the logic. The CRUD layer that persists these drafts into
``alerts`` / ``queued_actions`` lives in a follow-on PR and does not
exist yet, which is why this module deliberately speaks only in
*pre-persistence draft* shapes — it never imports an ``AlertRow`` or
``QueuedActionRow`` type from a CRUD module.

``Cadence`` governs *when* the morning driver invokes a sensor:

  DAILY            — every morning, regardless of new data
  ON_EARNINGS      — after a new transcript / earnings release lands
  ON_NEWS          — after the news ingest writes a new story
  CALENDAR_DRIVEN  — when a calendar event (SayDo period_target due,
                     earnings T-N reminder) fires
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable


class Cadence(StrEnum):
    """When the morning driver should invoke a sensor.

    String values are stable identifiers (used in run logs, future
    routing tables); do not rename without a migration of any
    persisted scheduling row that references them.
    """

    DAILY = "daily"
    ON_EARNINGS = "on_earnings"
    ON_NEWS = "on_news"
    CALENDAR_DRIVEN = "calendar_driven"


@dataclass(frozen=True)
class TriggerCandidate:
    """One observation a sensor flagged as potentially alert-worthy.

    ``key`` uniquely identifies *this specific candidate firing event*
    within its ``(ticker, kind)`` namespace — e.g. a kpi_inflection
    candidate's key might be the (kpi_name, inflection_period) tuple
    serialized as a string. The sensor's ``build_alert`` derives the
    AlertDraft's ``signature_sha`` from ``(kind, ticker, key)`` so the
    re-fire dedup index on ``alerts.signature_sha`` can suppress
    duplicates across refreshes.

    ``evidence`` carries the per-kind numeric / textual evidence the
    sensor needs to (a) decide should_fire, (b) compose the memo, and
    (c) draft downstream queued_actions. Schema is owned per
    ``kind`` — this dataclass intentionally takes ``dict[str, Any]``
    so each sensor can declare its own evidence shape.
    """

    ticker: str
    kind: str
    key: str
    evidence: dict[str, Any]
    computed_at: datetime


@dataclass(frozen=True)
class AlertDraft:
    """Pre-persistence shape of an ``alerts`` row.

    Sensors return this from ``build_alert``; the CRUD layer (separate
    PR) converts it into an ``alerts`` INSERT. Notably distinct from
    the eventual ``AlertRow`` — the row carries an ``id``, a status,
    and timestamp columns the DB owns. The draft has only the fields
    the sensor itself decides.

    ``evidence_json`` is the JSON-serialized snapshot that lands in
    ``alerts.evidence_json``; the sensor pre-serializes so the CRUD
    layer never has to re-serialize and so signature_sha can be
    computed off the same bytes the row will store.

    ``memo_text`` is ``None`` when the sensor defers memo composition
    to a follow-on LLM pass (the common case). When the sensor has
    the memo body in hand at build time (deterministic templated
    memos for some trigger kinds), it sets the field and the CRUD
    layer routes it into the llm_artifacts pointer column.
    """

    trigger_kind: str
    ticker: str
    fired_at: datetime
    evidence_json: str
    signature_sha: str
    memo_text: str | None


@dataclass(frozen=True)
class QueuedActionDraft:
    """Pre-persistence shape of a ``queued_actions`` row.

    ``action_kind`` must be one of ``thesis_update`` | ``bear_append``
    | ``sizing_update`` | ``earnings_prep_append`` — kept as ``str``
    here because the canonical ``ActionKind`` enum lives in the CRUD
    layer (separate PR). ``payload`` schema is owned per
    ``action_kind`` and lands as JSON in ``queued_actions.payload_json``.
    """

    action_kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class UserStateContext:
    """Read-only snapshot of the user's stated thesis state.

    Composed by the morning driver before fanning out to sensors so
    every ``should_fire`` call sees a consistent view. The three lists
    mirror the substrate tables but use plain dicts to avoid coupling
    this module to a not-yet-written CRUD shape:

      ``registered_kpis``           — rows of ``user_kpi_registry`` for
                                       the ticker (used to gate KPI
                                       inflections against thresholds /
                                       thesis-breaker flags)
      ``sizing_intents``            — rows of ``position_sizing_intent``
                                       for the ticker (used by trim/add
                                       drafters to know target / max %)
      ``recent_dismissed_signatures`` — set of ``signature_sha`` values
                                       the user has dismissed in the
                                       lookback window; sensors check
                                       membership before firing so a
                                       dismissed alert doesn't re-fire
                                       on the next refresh
    """

    registered_kpis: list[dict[str, Any]]
    sizing_intents: list[dict[str, Any]]
    recent_dismissed_signatures: set[str]


@dataclass(frozen=True)
class ThesisAnchor:
    """Structured view of the analyst's stated thesis for a ticker.

    The companion module ``llm.anchors`` builds a *markdown* anchor
    block from the same holdings JSON; this dataclass is the
    *structured* twin so trigger sensors can address fields directly
    (e.g. attach the matched ``tier_1_kpi`` entry to an alert's
    evidence) rather than re-parse markdown.

    Sourced from ``micro_thesis/holdings/<TICKER>.json``. Composing
    the anchor is the morning driver's job; sensors only consume.
    """

    ticker: str
    thesis_statement: str | None
    key_driver: str | None
    tier_1_kpis: list[dict[str, Any]]
    business_model_rules: list[dict[str, Any]]


@runtime_checkable
class Trigger(Protocol):
    """Sensor contract — every trigger kind implements these four methods.

    ``kind`` is the persistence-stable identifier written to
    ``alerts.trigger_kind`` (also the value the dashboard's per-kind
    filter keys on). ``cadence`` selects which morning-driver phase
    invokes the sensor; see :class:`Cadence`.

    Implementations should be stateless across calls — any required
    state is read from ``db`` (current observations) or
    ``UserStateContext`` (user-side gating). This keeps sensors
    parallelizable across tickers and reproducible from the audit
    trail (``alerts.evidence_json`` + the ``as_of_date`` view of the
    DB).
    """

    kind: ClassVar[str]
    cadence: ClassVar[Cadence]

    def scan(
        self, ticker: str, db: sqlite3.Connection
    ) -> list[TriggerCandidate]: ...

    def should_fire(
        self,
        candidate: TriggerCandidate,
        user_state: UserStateContext,
    ) -> bool: ...

    def build_alert(
        self,
        candidate: TriggerCandidate,
        anchor: ThesisAnchor | None,
    ) -> AlertDraft: ...

    def draft_actions(
        self,
        alert: AlertDraft,
        candidate: TriggerCandidate,
    ) -> list[QueuedActionDraft]: ...
