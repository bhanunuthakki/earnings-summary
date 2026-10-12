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

The module also owns the one piece of sensor *mechanics* that several sensors
share verbatim: the optional 1-2 sentence LLM context line that enriches a
deterministic memo (see ``LlmContextSite`` / ``maybe_trigger_llm_context``).
It still never imports a CRUD row type.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from llm.anchors import compose_anchor_block
from llm_artifact_store import (
    UpsertRequest,
    artifact_is_reusable,
    compute_input_sha256,
    read_current,
    upsert,
)
from runtime.deterministic import sha256_text

log = logging.getLogger(__name__)


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

    def scan(self, ticker: str, db: sqlite3.Connection) -> list[TriggerCandidate]: ...

    def should_fire(
        self,
        candidate: TriggerCandidate,
        user_state: UserStateContext,
    ) -> bool: ...

    def signature_key_evidence(self, candidate: TriggerCandidate) -> Mapping[str, object]:
        """Return the subset of ``candidate.evidence`` that keys the
        re-fire dedup hash.

        The morning driver computes ``compute_signature_sha(kind, ticker,
        signature_key_evidence(candidate))`` BEFORE running ``build_alert``
        so it can short-circuit when a prior alert with the same signature
        already exists — saving the LLM call on duplicates. The value
        returned here MUST match the key_evidence the sensor's own
        ``build_alert`` will pass to ``compute_signature_sha`` when it
        constructs the AlertDraft; otherwise the driver's dedup index
        and the persisted ``alerts.signature_sha`` will diverge.

        Convention: each sensor picks the *smallest* subset of evidence
        that uniquely identifies a firing event — e.g. ``earnings_tone``
        keys on ``{"transcript_id": <id>}`` alone, ignoring fiscal-period
        labels that derive from the same transcript.
        """
        ...

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


@runtime_checkable
class ScanSweepTrigger(Protocol):
    """Opt-in for the morning driver's bounded-concurrency scan sweep.

    A trigger declares this marker when its per-ticker ``scan`` is the entire
    deliverable: it emits NO candidates (the product is what ``scan`` persists
    itself), its scan-time LLM spend is attributable per ticker through the
    shared ``llm_calls`` ledger, and it tolerates concurrent scans — its own
    writes are short writer transactions against the single SQLite writer
    lock and its artifacts are keyed per (ticker, purpose). The driver then
    runs these scans in waves of at most ``--scan-workers`` simultaneous
    tickers, each scan under its own per-ticker wall-clock budget
    (``--scan-timeout-s``), and admits each wave only while the ledger total
    since run start is below the run's cost cap — so scan-time spend is
    cap-accounted exactly like candidate spend. A ticker the cap or a
    per-ticker timeout leaves unscanned is explicitly deferred, never
    silently skipped. Stateful triggers (which own one transaction across
    their whole pass) must not opt in.
    """

    kind: ClassVar[str]
    concurrent_scan: ClassVar[bool]

    def scan(self, ticker: str, db: sqlite3.Connection) -> list[TriggerCandidate]: ...


@dataclass(frozen=True)
class StatefulTriggerResult:
    """Outcome of an atomic, persistent sensor pass.

    Stateful sensors own a transaction because their hysteresis state and an
    alert must advance together.  The ordinary ``Trigger`` contract remains
    deliberately read-only during ``scan``.
    """

    alerts_fired: int = 0
    dedup_skips: int = 0
    no_candidate: bool = False
    dry_run_alerts: int = 0


@runtime_checkable
class StatefulTrigger(Protocol):
    """Optional driver seam for sensors whose state transition is atomic."""

    kind: ClassVar[str]
    cadence: ClassVar[Cadence]

    def run(
        self,
        *,
        ticker: str,
        db_path: Path,
        user_id: str,
        dry_run: bool,
        as_of: datetime | None = None,
    ) -> StatefulTriggerResult: ...


# ---------------------------------------------------------------------------
# Optional LLM context line — shared sensor mechanics
# ---------------------------------------------------------------------------
#
# Several sensors follow one contract: ``build_alert`` composes a deterministic
# memo and *then* optionally enriches it with a 1-2 sentence LLM line. The
# deterministic memo is the contract, so the enrichment degrades silently to
# ``None`` on any failure (LLM down, budget cap, empty response, store error)
# and caches through the artifact store. Only the prompt text, the cache period
# label and the log event names differ between sensors, so the mechanics live
# here and each sensor declares an ``LlmContextSite``.
#
# The host sensor passes its own module-level ``call_llm`` and anchor loaders so
# its established test seams — ``monkeypatch.setattr("triggers.<sensor>.call_llm",
# ...)`` and the same for an anchor loader — keep working unchanged.


AnchorLoader = Callable[[Path, str], str]


@dataclass(frozen=True)
class LlmContextSite:
    """Per-sensor wiring for the optional LLM context line.

    ``period_label`` names the evidence field the cache inputs carry (for
    example ``period_end`` or ``period_target``); it is part of the
    ``input_sha256`` contract, so it must not change for a live sensor.
    """

    purpose: str
    prompt_version: str
    event_prefix: str
    period_label: str
    build_prompt: Callable[[str, str], str]


def _read_cached_or_call_context(
    *,
    site: LlmContextSite,
    ticker: str,
    kpi_name: str,
    fiscal_period: str,
    factual_core: str,
    anchor_block: str,
    cache_inputs: list[bytes | str],
    db_path: Path,
    call_llm: Callable[..., str],
) -> str | None:
    """Return the context line, hitting the artifact-store cache when possible.

    Per-entity artifacts share the ``(ticker, purpose)`` slot, so the caller
    keys ``fiscal_period`` by the entity that owns the line; ``input_sha256``
    still guards correctness.
    """
    existing = read_current(
        ticker=ticker,
        purpose=site.purpose,
        fiscal_period=fiscal_period,
        db_path=db_path,
    )
    new_sha = compute_input_sha256(prompt_version=site.prompt_version, cache_inputs=cache_inputs)
    if (
        existing is not None
        and artifact_is_reusable(existing, input_sha256=new_sha)
        and isinstance(existing.content_md, str)
        and existing.content_md.strip()
    ):
        log.info(
            {
                "event": f"{site.event_prefix}_context_cache_hit",
                "ticker": ticker,
                "kpi": kpi_name,
                "artifact_id": existing.id,
            }
        )
        return existing.content_md

    prompt = site.build_prompt(factual_core, anchor_block)
    raw = call_llm(prompt, purpose=site.purpose, ticker=ticker)
    context = raw.strip()
    if not context:
        return None

    _ = upsert(
        UpsertRequest(
            ticker=ticker,
            purpose=site.purpose,
            fiscal_period=fiscal_period,
            content_md=context,
            cache_inputs=cache_inputs,
            prompt_version=site.prompt_version,
        ),
        db_path=db_path,
    )
    return context


def maybe_trigger_llm_context(
    *,
    site: LlmContextSite,
    ticker: str,
    kpi_name: str,
    period_value: str,
    fiscal_period: str,
    factual_core: str,
    repo_root: Path,
    db_path: Path,
    anchor_loaders: tuple[AnchorLoader, AnchorLoader, AnchorLoader, AnchorLoader],
    call_llm: Callable[..., str],
) -> str | None:
    """Best-effort 1-2 sentence context line, or ``None``.

    Degrades silently to ``None`` on ANY failure so ``build_alert`` always
    emits the deterministic memo. Cache-aware via the artifact store.
    """
    thesis_loader, bear_loader, ir_loader, priors_loader = anchor_loaders
    try:
        # Compose the full 4-block anchor (thesis + bear + IR + analyst priors)
        # so the context line can weigh the event against the bear case,
        # management's IR framing and the analyst's own open watch-items;
        # compose_anchor_block omits whichever blocks are absent.
        anchor_block = compose_anchor_block(
            thesis_loader(repo_root, ticker),
            bear_loader(repo_root, ticker),
            ir_loader(repo_root, ticker),
            priors_loader(repo_root, ticker),
        )
    except Exception as exc:  # anchor is optional; never block the memo
        log.debug(
            {
                "event": f"{site.event_prefix}_anchor_load_failed",
                "ticker": ticker,
                "error": str(exc),
            }
        )
        anchor_block = ""

    # Deterministic cache-input list for ``compute_input_sha256``. Order and
    # the period label are part of the contract — changing either invalidates
    # every cached context artifact.
    cache_inputs: list[bytes | str] = [
        f"ticker={ticker}",
        f"kpi={kpi_name}",
        f"{site.period_label}={period_value}",
        "factual_sha=" + sha256_text(factual_core),
        "anchor_sha=" + sha256_text(anchor_block),
    ]
    try:
        return _read_cached_or_call_context(
            site=site,
            ticker=ticker,
            kpi_name=kpi_name,
            fiscal_period=fiscal_period,
            factual_core=factual_core,
            anchor_block=anchor_block,
            cache_inputs=cache_inputs,
            db_path=db_path,
            call_llm=call_llm,
        )
    except Exception as exc:
        # The deterministic memo is the contract — an LLM/budget/parse failure
        # here must NOT sink the alert. Log and proceed without the line.
        log.warning(
            {
                "event": f"{site.event_prefix}_context_failed",
                "ticker": ticker,
                "kpi": kpi_name,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return None
