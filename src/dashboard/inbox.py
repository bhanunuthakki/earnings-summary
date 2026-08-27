"""Unified inbox — ONE deduped, categorized, RANKED stream over alerts,
queued-action drafts, thesis-ledger entries, open journal items, and (while
fresh) the cross-portfolio synthesis memo's sections (UX redesign PR3 +
Inbox v2).

The same model renders two ways:

  * the Home rail (``compact=True`` — top N, clamped bodies, no drawers),
  * the feed page (the full stream with filters).

(The standalone morning-digest page was the third surface; it retired
2026-06-11 — the Home rail IS the morning view now.) Dedupe collapses
near-identical bodies — the old digest showed one NU thesis update three
times because consecutive ledger rows carried the same narrative.

Inbox v2 on top: every item carries a **category facet** (News / Earnings /
Press releases / Rating changes / Thesis changes / Drafts / Watch items /
Synthesis) rendered as client-side filter chips (``show_filters=True``), and
the stream is **transparently ranked** as one flat list — severity x recency
decay x position weight x thesis relevance, score-descending (newest first
on ties; each card's relative stamp carries the "when") — with the factor
breakdown as the kind-chip's "why ranked here" tooltip (see ``inbox_rank``).
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast

from alerts import (
    ACTION_STATUS_PENDING,
    ALERT_STATUS_APPROVED,
    ALERT_STATUS_PENDING,
    AlertRow,
    QueuedActionRow,
    list_alerts,
    list_pending_actions,
    list_queued_actions_for_alerts,
)
from dashboard._styles import INBOX_CSS
from dashboard.inbox_rank import (
    ADVISOR_MEMO_TITLE,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    CATEGORY_THESIS,
    SEMANTIC_ADVISOR_MEMO,
    annotate_and_rank,
    decisive_alert_reason,
    inbox_label,
    note_semantic_kind,
)
from identity import DEFAULT_USER_ID
from schema_compat import SchemaRevisionMismatch
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.prose import prose_card_text
from ui.time import stamp_html
from user_state.ledger import list_recent_entries
from user_state.notes import list_notes

__all__ = [
    "INBOX_CSS",
    "INBOX_JS",
    "InboxItem",
    "collect_inbox",
    "render_inbox_stream",
    "schema_drift_notice",
]

# Why every source below re-raises SchemaRevisionMismatch before its
# ``except sqlite3.Error``:
#
# ``SchemaRevisionMismatch`` subclasses ``sqlite3.OperationalError`` (see
# src/schema_compat.py), so a bare ``except sqlite3.Error`` catches it. These
# handlers exist to tolerate ONE thing — a table this database does not have
# yet — and they express that by returning []. That made "your schema is behind
# the code" render as an inbox with nothing in it: identical, pixel for pixel,
# to a genuinely quiet morning. During the 2026-08-02 prod drift the whole
# stream read empty while 21 pending alerts and 45 ledger entries sat in the
# tables, and nothing on the page said otherwise.
#
# Drift is not absence. It propagates, and the surfaces render
# ``schema_drift_notice`` so the degraded state is visibly distinct from the
# happy path (design_language D4 / the silent-degradation rule). This mirrors
# the cron fleet's exit-78 treatment of the same condition.
_DRIFT_IS_NOT_EMPTY = True

_LEDGER_KIND_LABELS: dict[str, str] = {
    "thesis_update": "Thesis update",
    "bear_append": "Bear-case append",
    "sizing_update": "Sizing change",
    "earnings_prep_append": "Open question",
    # The shared constant keeps the label in lockstep with inbox_rank's
    # advisor-memo → synthesis-category refinement.
    "advisor_memo": ADVISOR_MEMO_TITLE,
}

# 'disclosure' is deliberately ABSENT (2026-07-30 owner ruling). Filing/
# transcript disclosure drift is a RESEARCH substrate, not a decision feed:
# `disclosure_events` carries 36k rows of which 6,348 passed the old inbox gate
# (`materiality >= 0.8`), so it monopolized the ranked stream and buried every
# alert. The gate could not work, because `materiality` is not one quantity —
# each detector writes its own incommensurable scale into that column:
# `item_diff` writes 1.0 - text_similarity, `metric_lifecycle` writes a
# magnitude RATIO, and `section_similarity` writes a whole-book PERCENTILE
# (so ~20% of the book scores >= 0.8 by construction, forever). Comparing them
# against one float threshold is a category error, not a tuning problem.
# The ratified Disclosure Intelligence v1 PRD (docs/design/
# disclosure_intelligence_v1_prd.md, owner ruling 4) already placed this
# substrate on **Ask + the ticker workspace**, with feed chips reserved for
# high-materiality events only — a bar no per-detector scale currently meets.
# Re-adding this kind needs a real cross-detector materiality contract first.
_DEFAULT_KINDS: tuple[str, ...] = (
    "alert",
    "draft",
    "ledger",
    "note",
    "synthesis",
)

# Cross-kind dedupe survivor order: when near-identical bodies land in the
# stream under different kinds (the advisor's memory-everywhere write puts one
# memo line in the ledger AND the journal), keep the kind that carries the
# most context on its card.
_KIND_RICHNESS: dict[str, int] = {
    "alert": 5,
    "draft": 4,
    "synthesis": 2,
    "ledger": 1,
    "note": 0,
}

# Humanized alert trigger labels for the card's kind chip — the raw enum
# (earnings_tone, material_news, …) never reaches a user-facing label
# (design_language §11). The raw kind still rides the card's data-trigger attr,
# so client-side trigger filtering and the tests keep their handle on it.
_TRIGGER_LABELS: dict[str, str] = {
    "earnings_tone": "Earnings tone",
    "material_news": "News",
    "kpi_inflection": "KPI inflection",
    "thesis_drift": "Thesis drift",
    "saydo_due": "Say/do due",
    "decision_condition": "Condition met",
    "restatement": "Restatement",
    "owner_capacity_breach": "Capacity breach",
    "data_feed_stale": "Data feed stale",
    "risk_drift": "Risk drift",
    "model_pin_switch": "Model routing",
}


@dataclass(frozen=True)
class InboxItem:
    """One stream entry. ``kind`` ∈ alert | draft | ledger | note | synthesis —
    the inbox's own lane taxonomy (which card renders, which JS owns it).
    ``semantic_kind`` is the orthogonal *identity* discriminator (Law 1): WHAT
    the item is, stamped from the source row's provenance at collect time, so
    category/label/actions resolve from identity not source table (an advisor
    memo is ``SEMANTIC_ADVISOR_MEMO`` whether it arrived as a note or a ledger
    echo). ``None`` for items with no identity refinement.

    Alerts carry their row + nested queued actions; standalone drafts (pending
    actions whose alert fell outside the window) carry just the action;
    note-backed items carry ``note_id`` for their lifecycle actions.
    ``category`` / ``score`` / ``score_why`` are assigned by
    ``inbox_rank.annotate_and_rank`` (the filter chips + ranking tooltip)."""

    kind: str
    ticker: str | None
    when: datetime
    title: str
    body: str
    status: str | None = None
    alert: AlertRow | None = None
    actions: tuple[QueuedActionRow, ...] = field(default=())
    action: QueuedActionRow | None = None
    category: str = ""
    score: float = 0.0
    score_why: str = ""
    semantic_kind: str | None = None
    note_id: int | None = None


def _norm_body(text: str) -> str:
    """Dedupe key normalization: case/whitespace-insensitive head of the body."""
    return " ".join(text.lower().split())[:120]


# The per-kind decorations the memory-everywhere writes wrap around one
# narrative (advisor.memos.persist_memo): the journal echo leads with
# "[advisor memo #12 · swap_check] ", the ledger echo embeds "(memo #12)".
_LEADING_TAG_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_MEMO_REF_RE = re.compile(r"\s*\(memo #\d+\)")


def _fuzzy_norm(text: str) -> str:
    """Cross-kind dedupe key: ``_norm_body`` after stripping the per-kind
    decorations, so "Title (memo #12) — line" (ledger) and
    "[advisor memo #12 · kind] Title — line" (journal) collapse."""
    return _norm_body(_MEMO_REF_RE.sub(" ", _LEADING_TAG_RE.sub("", text)))


def _as_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def collect_inbox(
    db_path: Path | None,
    *,
    user_id: str = DEFAULT_USER_ID,
    since: datetime | None = None,
    until: datetime | None = None,
    ticker: str | None = None,
    status: str | None = None,
    trigger_kind: str | None = None,
    kinds: tuple[str, ...] = _DEFAULT_KINDS,
    limit: int = 80,
    now: datetime | None = None,
    position_weights: dict[str, float] | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[InboxItem]:
    """The capped stream — see :func:`collect_inbox_counted` for the semantics.

    Thin wrapper for callers that don't render an overflow line (the Home rail
    and the mobile panel show a deliberate top-N, so "N of M" is noise there).
    A caller that renders the WHOLE stream should use ``collect_inbox_counted``
    and surface the remainder — a silently truncated list reads as complete.
    """
    return collect_inbox_counted(
        db_path,
        user_id=user_id,
        since=since,
        until=until,
        ticker=ticker,
        status=status,
        trigger_kind=trigger_kind,
        kinds=kinds,
        limit=limit,
        now=now,
        position_weights=position_weights,
        conn=conn,
    )[0]


def collect_inbox_counted(
    db_path: Path | None,
    *,
    user_id: str = DEFAULT_USER_ID,
    since: datetime | None = None,
    until: datetime | None = None,
    ticker: str | None = None,
    status: str | None = None,
    trigger_kind: str | None = None,
    kinds: tuple[str, ...] = _DEFAULT_KINDS,
    limit: int = 80,
    now: datetime | None = None,
    position_weights: dict[str, float] | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[InboxItem], int]:
    """Build the stream — deduped, categorized, ranked.

    Returns ``(capped_items, total_eligible)``. ``total_eligible`` counts the
    ranked stream BEFORE ``limit`` is applied, so a caller can tell the owner
    what the cap hid instead of presenting a truncated list as the whole queue.

    ``since`` windows the EVENT kinds — resolved alerts, ledger entries, and
    synthesis sections. Drafts, notes, and PENDING alerts are STANDING items:
    a pending draft (or alert) from ten days ago is still waiting on you, so
    they ignore ``since`` and stay in the stream (sinking on recency decay)
    instead of vanishing. The default stream (``status=None``) carries pending
    + recently-approved alerts only — dismissed/expired rows are settled noise
    and stay out unless explicitly requested via ``status``. ``until``
    upper-bounds everything (a stream re-built for a historical date stays
    honest). ``kinds`` filters the sources; ``status`` / ``trigger_kind`` apply
    to alerts only. ``now`` anchors recency decay (defaults to UTC now; pass a
    historical date to rank as that morning would have). ``position_weights``
    is ticker → fraction-of-book for the ranking factor — ``None`` reads the
    morning-pipeline-materialized weight cache beside the DB (a disk read, never
    the live tracker; empty → equal weighting), ``{}`` forces equal weighting.
    Best-effort: a missing DB or table yields ``([], 0)``. The returned list
    is ordered score-descending (newest first on ties) and capped at
    ``limit``.
    """
    if db_path is None or not Path(db_path).exists():
        # ([], 0) — NOT []. This returns a tuple now; a bare [] unpacks with
        # "not enough values to unpack" at every caller doing
        # ``items, total = collect_inbox_counted(...)``, which is every caller.
        return [], 0

    now_dt = _as_naive_utc(now) if now is not None else datetime.now(UTC).replace(tzinfo=None)
    until_n = _as_naive_utc(until) if until is not None else None

    def _in_window(when: datetime, *, windowed: bool) -> bool:
        if until_n is not None and when >= until_n:
            return False
        return not (windowed and since is not None and when < _as_naive_utc(since))

    items: list[InboxItem] = []
    shown_alert_ids: set[int] = set()

    if "alert" in kinds:
        try:
            if status is not None:
                # Explicit status filter (the /feed ?status= views — including
                # dismissed history): the plain windowed fetch, unchanged.
                alerts = list_alerts(
                    user_id=user_id,
                    ticker=ticker,
                    status=status,
                    since=since,
                    limit=limit,
                    db_path=db_path,
                    conn=conn,
                )
            else:
                # Default stream. PENDING is the owner's queue — fetched WHOLE,
                # no recency window or limit: the old status-blind
                # ``limit``-newest slice (ordered fired_at DESC) let a burst of
                # dismissed rows push old-but-still-pending alerts out of the
                # stream entirely. Recently-approved rows ride along as
                # windowed history; dismissed/expired rows stay OUT of the
                # default stream (settled noise — reachable via
                # /feed?status=dismissed etc., never ranked above the queue).
                alerts = list_alerts(
                    user_id=user_id,
                    ticker=ticker,
                    status=ALERT_STATUS_PENDING,
                    limit=None,
                    db_path=db_path,
                    conn=conn,
                ) + list_alerts(
                    user_id=user_id,
                    ticker=ticker,
                    status=ALERT_STATUS_APPROVED,
                    since=since,
                    limit=limit,
                    db_path=db_path,
                    conn=conn,
                )
        except SchemaRevisionMismatch:
            raise  # drift is not an absent table — see _DRIFT_IS_NOT_EMPTY
        except sqlite3.Error:
            alerts = []
        if trigger_kind:
            alerts = [a for a in alerts if a.trigger_kind == trigger_kind]
        # Pending alerts in the default stream are STANDING (like drafts/notes:
        # a three-week-old pending alert is still waiting on you) — only
        # ``until`` bounds them. Everything else keeps the ``since`` window.
        alerts = [
            a
            for a in alerts
            if _in_window(
                _as_naive_utc(a.fired_at),
                windowed=not (status is None and a.status == ALERT_STATUS_PENDING),
            )
        ]
        # One batched IN-query for every alert's queued actions, not one
        # connection-open + query PER alert (the GET / boot N+1).
        try:
            actions_by_alert = list_queued_actions_for_alerts(
                [a.id for a in alerts], db_path=db_path, conn=conn
            )
        except SchemaRevisionMismatch:
            raise  # drift is not an absent table — see _DRIFT_IS_NOT_EMPTY
        except sqlite3.Error:
            actions_by_alert = {}
        for a in alerts:
            shown_alert_ids.add(a.id)
            items.append(
                InboxItem(
                    kind="alert",
                    ticker=a.ticker,
                    when=_as_naive_utc(a.fired_at),
                    title=a.trigger_kind,
                    body="",
                    status=a.status,
                    alert=a,
                    actions=tuple(actions_by_alert.get(a.id, ())),
                )
            )

    if "draft" in kinds:
        # Pending drafts whose parent alert is NOT already in the stream
        # (older than the window, or filtered out) — the replacement for the
        # digest's separate "Outstanding actions" section.
        try:
            pending = list_pending_actions(user_id=user_id, db_path=db_path, conn=conn)
        except SchemaRevisionMismatch:
            raise  # drift is not an absent table — see _DRIFT_IS_NOT_EMPTY
        except sqlite3.Error:
            pending = []
        ticker_by_alert: dict[int, str] = {}
        status_by_alert: dict[int, str] = {}
        if pending:
            try:
                for a in list_alerts(user_id=user_id, limit=500, db_path=db_path, conn=conn):
                    ticker_by_alert[a.id] = a.ticker
                    status_by_alert[a.id] = a.status
            except SchemaRevisionMismatch:
                raise  # drift is not an absent table — see _DRIFT_IS_NOT_EMPTY
            except sqlite3.Error:
                pass
        for qa in pending:
            if qa.alert_id in shown_alert_ids:
                continue
            # A pending action whose parent alert is settled (dismissed /
            # approved / expired) is an orphan — rendering it would put a live
            # approve button on a decision the owner already closed. Skip it;
            # execution/cleanup_condition_alerts.py --repair-orphans cancels
            # the rows themselves.
            parent_status = status_by_alert.get(qa.alert_id)
            if parent_status is not None and parent_status != ALERT_STATUS_PENDING:
                continue
            qa_ticker = ticker_by_alert.get(qa.alert_id)
            if ticker is not None and qa_ticker != ticker:
                continue
            when = _as_naive_utc(qa.created_at)
            if not _in_window(when, windowed=False):  # standing: until only
                continue
            body = qa.payload.get("body") or qa.payload.get("narrative") or ""
            items.append(
                InboxItem(
                    kind="draft",
                    ticker=qa_ticker,
                    when=when,
                    title=_LEDGER_KIND_LABELS.get(qa.action_kind, qa.action_kind),
                    body=str(body),
                    status=qa.status,
                    action=qa,
                )
            )

    if "ledger" in kinds:
        try:
            entries = list_recent_entries(user_id=user_id, limit=60, db_path=db_path, conn=conn)
        except SchemaRevisionMismatch:
            raise  # drift is not an absent table — see _DRIFT_IS_NOT_EMPTY
        except (sqlite3.Error, FileNotFoundError, RuntimeError):
            entries = []
        for e in entries:
            when = _as_naive_utc(e.created_at)
            if not _in_window(when, windowed=True):
                continue
            if ticker is not None and e.ticker != ticker:
                continue
            items.append(
                InboxItem(
                    kind="ledger",
                    ticker=e.ticker,
                    when=when,
                    title=_LEDGER_KIND_LABELS.get(e.entry_kind, e.entry_kind),
                    body=e.body,
                    semantic_kind=(
                        SEMANTIC_ADVISOR_MEMO if e.entry_kind == "advisor_memo" else None
                    ),
                )
            )

    if "note" in kinds:
        try:
            notes = list_notes(
                user_id=user_id,
                ticker=ticker,
                status="open",
                db_path=db_path,
                conn=conn,
            )
        except SchemaRevisionMismatch:
            raise  # drift is not an absent table — see _DRIFT_IS_NOT_EMPTY
        except sqlite3.Error:
            notes = []
        # S15: open notes whose linked decision graded / position exited are
        # awaiting reconciliation — surface that on the card (title + pending
        # badge; "pending" also carries the needs-the-owner status multiplier
        # in inbox_rank) instead of letting them sit as ordinary watch items.
        reconcile_why: dict[int, str] = {}
        if notes:
            try:
                from journal_links import pending_reconciliation_note_ids

                reconcile_why = pending_reconciliation_note_ids(
                    db_path=db_path, user_id=user_id, conn=conn
                )
            except SchemaRevisionMismatch:
                raise  # drift is not an absent table — see _DRIFT_IS_NOT_EMPTY
            except Exception:  # pre-0093 schema — plain notes
                reconcile_why = {}
        for n in notes:
            when = _as_naive_utc(n.created_at)
            if not _in_window(when, windowed=False):  # standing: until only
                continue
            conclusion = reconcile_why.get(n.id)
            # title carries the RAW note kind; inbox_label() resolves the human
            # caption (+ the "Reconcile · " prefix, derived from the pending
            # status) so the raw enum never reaches a card. semantic_kind lifts
            # advisor memos to machine-authored identity (source/source_ref/
            # context → note_semantic_kind).
            items.append(
                InboxItem(
                    kind="note",
                    ticker=n.ticker,
                    when=when,
                    title=n.kind,
                    body=f"{n.body} — {conclusion}" if conclusion else n.body,
                    status="pending" if conclusion else None,
                    semantic_kind=note_semantic_kind(n.source, n.source_ref, n.context),
                    note_id=n.id,
                )
            )

    # NOTE: there is no 'disclosure' source here by design — see _DEFAULT_KINDS.
    # Disclosure drift reaches the owner through Ask and the ticker workspace,
    # which is where the ratified PRD put it.

    if "synthesis" in kinds and ticker is None:
        # Portfolio-scope insight: the cross-portfolio synthesis memo's
        # structured sections, only while the lens output is fresh.
        for s in _synthesis_items(db_path, now=now_dt, conn=conn):
            if _in_window(s.when, windowed=True):
                items.append(s)

    # Dedupe: near-identical bodies collapse ACROSS kinds, not just within
    # one — an advisor memo's ledger entry and its journal-observation echo
    # carry the same line under different decorations (see ``_fuzzy_norm``),
    # and the old per-kind key rendered both. The survivor is the RICHEST
    # kind (alert > draft > synthesis > ledger > note), newest on ties — so
    # within one kind this still keeps the newest of texts that repeat across
    # consecutive runs. Bodyless items (alert cards — their narrative lives
    # in evidence_json) pass through: alerts dedupe upstream on
    # signature_sha already.
    items.sort(key=lambda it: it.when, reverse=True)
    richest: dict[tuple[str | None, str], InboxItem] = {}
    passthrough: list[InboxItem] = []
    for it in items:
        if not it.body:
            passthrough.append(it)
            continue
        key = (it.ticker, _fuzzy_norm(it.body))
        cur = richest.get(key)
        if cur is None or _KIND_RICHNESS.get(it.kind, 0) > _KIND_RICHNESS.get(cur.kind, 0):
            richest[key] = it
    deduped = passthrough + list(richest.values())
    # Categorize + score + order (flat score-desc, newest-first ties) — the
    # Inbox v2 ranking layer (inbox_rank).
    ranked = annotate_and_rank(
        deduped,
        db_path=Path(db_path),
        now=now_dt,
        position_weights=position_weights,
        conn=conn,
    )
    return ranked[:limit], len(ranked)


# ----------------------------------------------------------------------------
# Synthesis sections (cross_portfolio_synthesis lens → stream items)
# ----------------------------------------------------------------------------

_SYNTHESIS_FRESH_DAYS = 7  # the lens runs weekly; older memos are stale insight
_SYNTHESIS_BODY_CAP = 600

# Heading keyword → stream-item title, in memo order. "What I'd want to spend
# more time on" stays in the full memo (Portfolio tab) — the stream carries the
# three actionable sections.
_SYNTHESIS_HEADINGS: tuple[tuple[str, str], ...] = (
    ("most-look", "Most-look name"),
    ("convergence", "Convergence clusters"),
    ("allocation", "Allocation suggestions"),
)


def _parse_artifact_dt(raw: str) -> datetime | None:
    try:
        return _as_naive_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _plain_text(md: str) -> str:
    return " ".join(re.sub(r"[*_`#]+", "", md).split())[:_SYNTHESIS_BODY_CAP]


def _parse_synthesis_sections(content_md: str) -> list[tuple[str, str]]:
    """(title, plain-text body) for each recognized ``## `` section."""
    raw_sections: list[tuple[str, list[str]]] = []
    for line in content_md.splitlines():
        if line.lstrip().startswith("## "):
            raw_sections.append((line.lstrip()[3:].strip(), []))
        elif raw_sections:
            raw_sections[-1][1].append(line)
    out: list[tuple[str, str]] = []
    for heading, body_lines in raw_sections:
        body = _plain_text("\n".join(body_lines))
        if not body:
            continue
        low = heading.lower()
        for key, title in _SYNTHESIS_HEADINGS:
            if key in low:
                out.append((title, body))
                break
    return out


def _synthesis_items(
    db_path: Path,
    *,
    now: datetime,
    conn: sqlite3.Connection | None = None,
) -> list[InboxItem]:
    """Stream items from the latest cached cross-portfolio synthesis memo,
    only while it is fresh (≤ ``_SYNTHESIS_FRESH_DAYS`` old). Best-effort:
    a missing table / artifact / unparsable timestamp yields []."""
    try:
        db_conn = conn or connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except SchemaRevisionMismatch:
        raise  # drift is not an absent table — see _DRIFT_IS_NOT_EMPTY
    except sqlite3.Error:
        return []
    try:
        has_artifacts = (
            db_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
            ).fetchone()
            is not None
        )
        if not has_artifacts:
            return []
        row = db_conn.execute(
            """
            SELECT content_md, generated_at FROM llm_artifacts
            WHERE purpose = 'lens:cross_portfolio_synthesis'
              AND scope = 'portfolio'
              AND superseded_by_id IS NULL
            ORDER BY generated_at DESC LIMIT 1
            """
        ).fetchone()
    except SchemaRevisionMismatch:
        raise  # drift is not an absent table — see _DRIFT_IS_NOT_EMPTY
    except sqlite3.Error:
        return []
    finally:
        if conn is None:
            db_conn.close()
    if row is None or not row[0]:
        return []
    when = _parse_artifact_dt(str(row[1] or ""))
    if when is None or now - when > timedelta(days=_SYNTHESIS_FRESH_DAYS):
        return []
    return [
        InboxItem(kind="synthesis", ticker=None, when=when, title=title, body=body)
        for title, body in _parse_synthesis_sections(str(row[0]))
    ]


# ----------------------------------------------------------------------------
# Render
# ----------------------------------------------------------------------------


def schema_drift_notice(exc: SchemaRevisionMismatch) -> str:
    """The degraded block a surface renders when the stream cannot be built.

    Owner language leads, with the engineering receipt behind a hover — the D4
    rule that a degraded state must be legible AND visibly distinct from the
    happy path, never a raw diagnostic string and never an empty list.

    The distinction that matters: this says the inbox could not be READ. An
    empty stream says there is nothing to read. Before this existed both looked
    the same, so a drifted database presented as a quiet morning.
    """
    return (
        '<div class="ix-degraded" role="status">'
        "<strong>Inbox unavailable</strong> — the database is on a different schema "
        "version than this build, so the stream can't be read. Your alerts and notes "
        "are intact; nothing has been lost. "
        f'<span class="ix-degraded-why" title="{_esc(str(exc))}">details</span>'
        "</div>"
    )


def render_inbox_stream(
    items: list[InboxItem],
    *,
    db_path: Path | None = None,
    compact: bool = False,
    show_status_badge: bool = True,
    show_filters: bool = False,
    surface: str | None = None,
    hidden_count: int = 0,
    empty_text: str = "Nothing new — alerts, drafts, thesis changes, and watch items land here.",
) -> str:
    """The stream HTML: ONE flat list in the score order ``collect_inbox``
    returns — no recency-bucket headers; each card's top-right relative stamp
    carries the "when". ``compact`` is the Home-rail variant — clamped bodies,
    collapsed evidence, no nested action bodies, hover ✓/✕ quick actions on
    approvable cards. ``show_filters`` renders the category chips (client-side
    filtering via INBOX_JS). ``surface`` names the page ("home" | "feed") for
    the unread tracking: INBOX_JS keys its per-surface localStorage last-seen
    off the ``data-ix-surface`` attr. ``hidden_count`` is how many ranked items
    the cap dropped — when >0 the stream closes with a one-line receipt naming
    the remainder, because a silently truncated list reads as the whole queue
    (the "no silent caps" rule; the count comes from
    ``collect_inbox_counted``)."""
    if not items:
        return f'<div class="ix-empty">{_esc(empty_text)}</div>'
    out = StringIO()
    surface_attr = f' data-ix-surface="{_esc(surface)}"' if surface else ""
    out.write(f'<div class="ix-stream{" ix-compact" if compact else ""}"{surface_attr}>')
    if show_filters:
        _render_category_chips(out, items)
    for it in items:
        _render_item(out, it, db_path=db_path, compact=compact, show_status=show_status_badge)
    if hidden_count > 0:
        # Owner language, not a diagnostic: what is shown, what is not, and why
        # the rest is safe to not look at (they rank below everything above).
        noun = "item" if hidden_count == 1 else "items"
        out.write(
            f'<div class="ix-more">Showing the top {len(items)} of '
            f"{len(items) + hidden_count} — {hidden_count} lower-ranked {noun} not shown.</div>"
        )
    out.write("</div>")
    return out.getvalue()


def _render_category_chips(out: StringIO, items: list[InboxItem]) -> None:
    """The category filter row: All + one chip per category present, with
    counts. Filtering is client-side (INBOX_JS toggles ``.ix-hide``) — zero
    round trips, scoped to this stream."""
    counts: dict[str, int] = {}
    for it in items:
        if it.category:
            counts[it.category] = counts.get(it.category, 0) + 1
    if not counts:
        return
    out.write('<div class="ix-cats" role="toolbar" aria-label="Filter by category">')
    out.write(
        '<button type="button" class="ix-cat k-chip k-chip-btn is-on" data-cat="*">'
        f"All <span>{len(items)}</span></button>"
    )
    for slug in CATEGORY_ORDER:
        n = counts.get(slug, 0)
        if not n:
            continue
        out.write(
            f'<button type="button" class="ix-cat k-chip k-chip-btn" data-cat="{_esc(slug)}">'
            f"{_esc(CATEGORY_LABELS[slug])} <span>{n}</span></button>"
        )
    out.write("</div>")


def _render_item(
    out: StringIO,
    it: InboxItem,
    *,
    db_path: Path | None,
    compact: bool,
    show_status: bool,
) -> None:
    """One stream card — ONE shape for every kind (alert, draft, ledger, note,
    synthesis). The heavy alert detail (the evidence drawer, the queued-action
    history) lives in the in-shell peek + the holding rail, not inline here: the
    feed is a scan-and-act surface, so each card is the ticker, a humanized kind,
    the status, the body, and — only when there's something to do or somewhere to
    go — a compact footer (approve/dismiss, open the article, review)."""
    when_attr = it.when.isoformat(timespec="seconds")
    cat_attr = f' data-cat="{_esc(it.category)}"' if it.category else ""
    # The raw trigger_kind rides a data-attr (a machine hook, like data-cat /
    # data-kind) — never a visible label; the chip humanizes it (§11).
    trig_attr = f' data-trigger="{_esc(it.title)}"' if it.kind == "alert" else ""
    # Tier-1 severity (one definition with the ranking layer): a decisive
    # alert — owner falsifier breach, registered threshold crossing — carries
    # a bad-toned rail + chip so it reads apart from routine news at a glance.
    decisive = (
        decisive_alert_reason(it.title, it.alert.evidence_json)
        if it.kind == "alert" and it.alert is not None
        else None
    )
    card_cls = "ix-card ix-sev-bad" if decisive else "ix-card"
    out.write(
        f'<div class="{card_cls}" data-kind="{_esc(it.kind)}"{cat_attr}{trig_attr} '
        f'data-when="{when_attr}">'
    )
    out.write('<div class="ix-head">')
    if it.ticker:
        # The ticker is a doorway, not inert text: it opens that holding's full
        # context in the shell (where the alert's evidence drawer lives), tying
        # the feed into the rest of the dashboard. data-peek-ticker still drives
        # the hover mini-card when this stream is embedded in the shell.
        out.write(
            f'<a class="ix-ticker" href="/#holding={_esc(it.ticker)}" '
            f'data-peek-ticker="{_esc(it.ticker)}">{_esc(it.ticker)}</a>'
        )
    why_attr = f' title="ranked: {_esc(it.score_why)}"' if it.score_why else ""
    # The kind label is a kit chip (controls.py .k-chip), not bare text — four
    # visually-identical kinds (earnings tone / thesis update / prep-note /
    # KPI inflection) were unreadable at a glance without the chip boundary.
    # Tone stays restrained: only CATEGORY_THESIS gets -warn, matching the
    # warn semantics thesis-status pills already carry elsewhere; every other
    # category is the plain (untoned) chip — accent is reserved for
    # interactive/unread state (design_language §2), not a category palette.
    kind_tone = " k-chip-warn" if it.category == CATEGORY_THESIS else ""
    out.write(
        f'<span class="ix-kind ix-kind-{_esc(it.kind)} k-chip{kind_tone}"{why_attr}>'
        f"{_esc(_chip_label(it))}</span>"
    )
    if decisive:
        # The kit outline chip in bad tone — the same red the cockpit's
        # tier-1 pending pill carries, with the reason in the hover.
        out.write(f'<span class="k-chip k-chip-bad" title="{_esc(decisive)}">tier 1</span>')
    if show_status and it.status and it.status not in ("open",):
        # The filled status pill is the kit .k-pill (controls.py §3); .ix-status
        # stays only as the JS hook (INBOX_JS swaps the tone in place).
        pill_cls = f"ix-status k-pill {_status_pill_tone(it.status)}".strip()
        out.write(f'<span class="{pill_cls}">{_esc(it.status)}</span>')
    if compact:
        _render_quick_actions(out, it)
    out.write(stamp_html(it.when, css="ix-when"))
    out.write("</div>")
    body = _display_body(it)
    if body:
        # prose_card_text, not bare _esc: memo/synthesis bodies carry markdown,
        # and a bare escape leaks literal **/## into the clamped card (§9).
        out.write(f'<div class="ix-body">{prose_card_text(body)}</div>')
    _render_card_footer(out, it, compact=compact)
    out.write("</div>")


# Status → kit .k-pill tone. pending→warn, applied/approved→ok; anything else
# (cancelled, …) rides the neutral base .k-pill (--paper fill / --fg-soft ink).
# INBOX_JS carries the same map inline so a swapped-in-place status pill picks
# the same tone the server-rendered one would.
_STATUS_PILL_TONES: dict[str, str] = {
    "pending": "k-pill-warn",
    "applied": "k-pill-ok",
    "approved": "k-pill-ok",
}


def _status_pill_tone(status: str) -> str:
    return _STATUS_PILL_TONES.get(status, "")


def _chip_label(it: InboxItem) -> str:
    """The kind chip's text. The raw enum never reaches a label (design_language
    §11; it rides ``data-trigger`` instead):

    * ``material_news`` is the one trigger the categorizer SPLITS — into News /
      Rating changes / Press releases — so its chip shows that refined category,
      matching the filter bucket the card lives under (no "News" chip on a card
      filed under "Rating changes");
    * every other trigger maps 1:1 to a category, so it humanizes directly;
    * non-alert kinds keep their identity-resolved ``inbox_label``.
    """
    if it.kind == "alert":
        if it.title == "material_news":
            return CATEGORY_LABELS.get(it.category, "News")
        return _TRIGGER_LABELS.get(it.title, it.title.replace("_", " ").capitalize())
    return inbox_label(it)


def _render_card_footer(out: StringIO, it: InboxItem, *, compact: bool) -> None:
    """The card's one action/doorway row — emitted ONLY when the item carries
    something to do or somewhere to go, so a passive ledger / synthesis card
    stays a clean two-line entry instead of growing an empty action band.
    Advisor memos keep their own ``.k-chip`` affordances (open memo / dismiss)."""
    if it.semantic_kind == SEMANTIC_ADVISOR_MEMO:
        _render_memo_actions(out, it, return_to="/" if compact else "/feed")
        return

    parts: list[str] = []
    pending = _pending_action(it)

    if compact:
        # Rail: the hover ✓/✕ buttons (in the header) already approve in place,
        # so the footer is just the "review →" peek for a still-pending item.
        if it.kind in ("alert", "draft") and it.status == "pending":
            target = f"/feed?ticker={it.ticker}" if it.ticker else "/feed"
            alert_id = (
                it.alert.id
                if it.alert is not None
                else (it.action.alert_id if it.action is not None else None)
            )
            peek = (
                f' data-peek-url="/api/peek/alert/{alert_id}" data-peek-title="Review alert"'
                if alert_id is not None
                else ""
            )
            parts.append(f'<a class="ix-foot-link" href="{_esc(target)}"{peek}>review →</a>')
    elif pending is not None:
        # Feed: the no-JS approve path (GET /approve), absolute so it resolves
        # the same on / and /feed. No CLI hint, no inline evidence drawer — that
        # detail is one ticker-click away in the holding view.
        #
        # An ALERT card settles at the alert level, not the action level. The
        # per-action link cleared exactly one draft and left the alert 'pending'
        # — and since the inbox fetches pending alerts unbounded, the card never
        # moved while its fresh ledger entry rendered as a SECOND card. Alerts
        # routinely carry several drafts (prod: 9 on FCX 28, 17 on NU 1), so the
        # count rides the hover text: the owner should know one click is
        # settling nine things. Standalone drafts have no parent to settle and
        # keep the action-level target.
        n_open = sum(1 for qa in it.actions if qa.status == ACTION_STATUS_PENDING)
        if it.kind == "alert" and it.alert is not None:
            target, noun = f"alert_id={it.alert.id}", f"{n_open} queued action(s)"
        else:
            target, noun = f"action_id={pending.id}", "this draft"
        parts.append(
            f'<a class="ix-foot-act" href="/approve?{target}" '
            f'title="Apply {_esc(noun)} and clear this card">approve</a>'
        )
        parts.append(
            '<a class="ix-foot-act ix-foot-dismiss" '
            f'href="/approve?{target}&dismiss=1" '
            f'title="Cancel {_esc(noun)} and clear this card">dismiss</a>'
        )

    article = _article_url(it)
    if article:
        # The feed's tie into the broader news: open the actual story, not a
        # dead-end card. New tab + noopener (an untrusted external origin).
        parts.append(
            f'<a class="ix-foot-link" href="{_esc(article)}" target="_blank" '
            'rel="noopener noreferrer">article ↗</a>'
        )

    if parts:
        out.write('<div class="ix-foot">' + "".join(parts) + "</div>")


def _display_body(it: InboxItem) -> str:
    """The card body text. Legacy advisor-memo notes (prod rows written before
    the clean-body fix) carry the retired ``[advisor memo #N · kind]`` lead
    tag; strip it PERMANENTLY at render so no internal-format string reaches a
    user-facing body (design_language §Streams). Scoped to advisor-memo
    identity so an ordinary note that legitimately opens with a bracket is
    untouched."""
    body = it.body or _alert_memo(it)
    if not body and it.kind == "alert" and it.title == "decision_condition":
        # decision_condition evidence carries none of the generic memo fields
        # (its drafter memo rides the queued action, not the evidence), so the
        # card showed ONLY the "Condition met" chip — two conditions differing
        # by threshold were indistinguishable. Surface the condition itself +
        # the latest observed value as the one-line body.
        body = _decision_condition_body(it)
    if it.semantic_kind == SEMANTIC_ADVISOR_MEMO:
        # Defensive render-time pass (B8): legacy memo rows persisted before
        # the writer-side stripper may still open with the model's process
        # narration — never serve it as the card's summary line.
        from llm.postprocess import strip_llm_preamble

        return strip_llm_preamble(_LEADING_TAG_RE.sub("", body))
    return body


def _decision_condition_body(it: InboxItem) -> str:
    """One-line body for a ``decision_condition`` alert, from its evidence:
    ``condition_label`` (metric ≤/≥ threshold unit, the analyst's own tripwire
    wording) plus the latest observed value + period — the two facts that make
    the card readable and disambiguate same-metric conditions. Best-effort ""
    on malformed/missing evidence (the card then keeps its chip-only shape)."""
    if it.alert is None or not it.alert.evidence_json:
        return ""
    try:
        parsed = json.loads(it.alert.evidence_json)
    except (ValueError, TypeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    ev = cast("Mapping[str, object]", parsed)
    label = ev.get("condition_label")
    if not isinstance(label, str) or not label.strip():
        return ""
    latest: object = ev.get("latest_value")
    if not isinstance(latest, (int, float)):
        observed = ev.get("observed")
        if isinstance(observed, list) and observed:
            head: object = cast("list[object]", observed)[0]
            if isinstance(head, dict):
                latest = cast("Mapping[str, object]", head).get("value")
    parts = [label.strip()]
    if isinstance(latest, (int, float)) and not isinstance(latest, bool):
        unit = ev.get("unit")
        period = ev.get("period_end")
        obs = f"latest {latest:g}"
        if isinstance(unit, str) and unit.strip():
            obs += f" {unit.strip()}"
        if isinstance(period, str) and period.strip():
            obs += f" @ {period.strip()}"
        parts.append(obs)
    return " — ".join(parts)


def _render_memo_actions(out: StringIO, it: InboxItem, *, return_to: str) -> None:
    """Affordances for an advisor-memo card (Law-1 identity), built from the
    shared control kit (``.k-chip``) — not the bespoke ``.ix-act`` quick
    buttons that approve queued drafts:

    * **open memo** → the Memos surface (``/#advisor_memos``). Per directive
      §7, "record in journal / update thesis" route to the company/Memos
      surface, not net-new write paths; a portfolio-level memo (ticker=None)
      has no company to click into, so it opens the memo record instead.
    * **dismiss** → archives the note-backed memo via the existing
      ``POST /api/notes/<id>/archive`` endpoint (note-backed memos only; a
      ledger-echo survivor carries no ``note_id`` and gets open-memo alone).
    """
    out.write('<div class="ix-memo-acts">')
    out.write('<a class="k-chip k-chip-btn ix-memo-open" href="/#advisor_memos">open memo</a>')
    if it.note_id is not None:
        out.write(
            f'<form method="post" action="/api/notes/{it.note_id}/archive" class="ix-action-form">'
            f'<input type="hidden" name="return_to" value="{_esc(return_to)}">'
            '<button class="k-chip k-chip-btn ix-note-dismiss" type="submit" '
            'title="Archive this memo note">dismiss</button></form>'
        )
    out.write("</div>")


def _pending_action(it: InboxItem) -> QueuedActionRow | None:
    """The queued action still awaiting the owner on this card, or None. Drafts
    act on their own action; alert cards act on their first still-pending queued
    action (one action per alert is the drafter's norm).

    Safety gate (red-team wave A): an approvable action only surfaces while the
    PARENT ALERT is itself still pending. Prod carried pending queued_actions
    dangling under dismissed alerts — rendering their ✓/✕ put a working
    approve button on a decision the owner had already closed."""
    if it.kind == "draft" and it.action is not None and it.action.status == ACTION_STATUS_PENDING:
        return it.action
    if it.kind == "alert" and it.alert is not None and it.alert.status == ALERT_STATUS_PENDING:
        for qa in it.actions:
            if qa.status == ACTION_STATUS_PENDING:
                return qa
    return None


def _quick_action_id(it: InboxItem) -> int | None:
    """The queued-action id the rail's hover ✓/✕ operate on, or None when the
    card carries nothing approvable."""
    qa = _pending_action(it)
    return qa.id if qa is not None else None


def _action_form(action: str, fields: tuple[tuple[str, object], ...], button: str) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{_esc(name)}" value="{_esc(str(value))}">'
        for name, value in fields
    )
    return (
        f'<form method="post" action="{_esc(action)}" class="ix-action-form">'
        f'{hidden}<input type="hidden" name="return_to" value="/">{button}</form>'
    )


def _btn_approve(action_id: int) -> str:
    button = (
        '<button class="ix-act ix-act-approve k-btn k-btn-quiet k-btn-sm" type="submit" '
        f'data-action-id="{action_id}" aria-label="Approve and apply" '
        'title="Approve &amp; apply">&#10003;</button>'
    )
    return _action_form("/approve", (("action_id", action_id), ("confirm", 1)), button)


def _btn_dismiss_action(action_id: int) -> str:
    button = (
        '<button class="ix-act ix-act-dismiss k-btn k-btn-quiet k-btn-sm" type="submit" '
        f'data-action-id="{action_id}" data-dismiss="1" aria-label="Dismiss action" title="Dismiss" '
        ">&#10005;</button>"
    )
    return _action_form(
        "/approve",
        (("action_id", action_id), ("dismiss", 1), ("confirm", 1)),
        button,
    )


def _btn_dismiss_alert(alert_id: int) -> str:
    button = (
        '<button class="ix-act ix-act-dismiss k-btn k-btn-quiet k-btn-sm" type="submit" '
        f'data-alert-id="{alert_id}" aria-label="Dismiss alert" '
        'title="Dismiss alert">&#10005;</button>'
    )
    return _action_form(
        "/approve",
        (("alert_id", alert_id), ("dismiss", 1), ("confirm", 1)),
        button,
    )


def _btn_dismiss_note(note_id: int) -> str:
    button = (
        '<button class="ix-act ix-act-dismiss k-btn k-btn-quiet k-btn-sm" type="submit" '
        f'data-note-id="{note_id}" aria-label="Dismiss note" title="Dismiss">&#10005;</button>'
    )
    return _action_form(f"/api/notes/{note_id}/archive", (), button)


def quick_actions_span(buttons: list[str]) -> str:
    """Wrap the compact rail's ordinary POST forms."""
    return '<span class="ix-quick">' + "".join(buttons) + "</span>"


def _render_quick_actions(out: StringIO, it: InboxItem) -> None:
    """The compact rail's hover ✓/✕ affordances, written into the EXISTING
    header row before the timestamp — visibility-flipped on card hover / focus
    so the resting layout is byte-identical (the zero-height design). What
    appears is resolved from the card's identity, giving the rail the same
    actionability the full feed's cards carry (owner ask: chip parity + a
    dismiss on every actionable card):

      * a pending queued action (a draft, or an alert that drafted one) → a
        ✓ that approves it (ordinary POST /approve form);
      * an alert → a ✕ that dismisses the ALERT itself (POST
        /approve with alert_id), the alert-level counterpart to a draft's
        action-level dismiss; the route also cancels any pending draft so the
        dismissed alert can't leave one behind to resurface;
      * a standalone draft → its ✕ dismisses the ACTION (POST /approve
        &dismiss=1) — reversible, so its done-chip carries an Undo;
      * a plain analyst note → a ✕ that archives it (POST
        /api/notes/<id>/archive) — also reversible (Undo).

    Informational ledger / synthesis cards carry nothing — they age out on
    recency decay (owner choice: dismiss is for actionable items only).
    Advisor-memo notes keep their own always-visible ``.ix-memo-acts`` row
    (open-memo + dismiss), so they're skipped here to avoid a double affordance.
    """
    buttons: list[str] = []
    action_id = _quick_action_id(it)
    if action_id is not None:
        buttons.append(_btn_approve(action_id))
    if it.kind == "alert" and it.alert is not None and it.alert.status == ALERT_STATUS_PENDING:
        # The card-level dismiss: clear the whole alert (not just one drafted
        # action). Present whether or not a draft exists, so an alert that never
        # drafted one (e.g. earnings_tone) is still dismissable.
        buttons.append(_btn_dismiss_alert(it.alert.id))
    elif action_id is not None:
        # A standalone draft (no parent alert on this card) — dismiss the action.
        buttons.append(_btn_dismiss_action(action_id))
    elif it.kind == "note" and it.note_id is not None and it.semantic_kind != SEMANTIC_ADVISOR_MEMO:
        buttons.append(_btn_dismiss_note(it.note_id))
    if buttons:
        out.write(quick_actions_span(buttons))


def _article_url(it: InboxItem) -> str | None:
    """The source-article URL behind a news / press / rating alert
    (``material_news`` evidence carries ``url``), so the card can link out to the
    actual story instead of dead-ending. None for any other kind, malformed
    evidence, or a non-http payload."""
    if it.kind != "alert" or it.alert is None or not it.alert.evidence_json:
        return None
    try:
        parsed: object = json.loads(it.alert.evidence_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    evidence = cast("Mapping[str, object]", parsed)
    url = evidence.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    return None


def _alert_memo(it: InboxItem) -> str:
    if it.alert is None:
        return ""
    from dashboard._card import memo_text_from_evidence

    return memo_text_from_evidence(it.alert.evidence_json) or ""


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


# Behavior for the stream's two client-side features, embedded once per page
# that renders an inbox (shell Overview, /feed). Plain string — its braces
# must survive f-string assembly untouched.
#
# 1. UNREAD — per-surface "since you last looked": cards carry ``data-when``
#    (naive-UTC seconds, lexicographically comparable); anything newer than
#    the surface's localStorage high-water mark gets ``.ix-new`` and is
#    counted into the surface's ``[data-ix-badge]`` (the Home rail header).
#    The mark advances only once the stream is actually ON SCREEN
#    (IntersectionObserver — landing on another tab doesn't mark Home seen),
#    so accents persist while you read and clear on the next visit.
# Mutating inbox actions are ordinary server-rendered forms and therefore need
# no JavaScript transport. This script owns only unread tracking and local
# category filtering.
INBOX_JS = r"""
(function () {
  if (window.__ixWired) return;
  window.__ixWired = true;

  function nowStamp() { return new Date().toISOString().slice(0, 19); }

  function initStream(stream) {
    var surface = stream.getAttribute('data-ix-surface');
    if (!surface) return;
    var key = 'ix-last-seen:' + surface;
    var last = null;
    try { last = localStorage.getItem(key); } catch (e) { return; }
    var fresh = 0;
    var cards = stream.querySelectorAll('[data-when]');
    for (var i = 0; i < cards.length; i++) {
      var w = cards[i].getAttribute('data-when');
      if (last && w && w > last) { cards[i].classList.add('ix-new'); fresh++; }
    }
    var badge = document.querySelector('[data-ix-badge="' + surface + '"]');
    if (badge && fresh > 0) { badge.textContent = String(fresh); badge.hidden = false; }
    function markSeen() {
      try { localStorage.setItem(key, nowStamp()); } catch (e) { /* storage off */ }
    }
    if (typeof IntersectionObserver === 'undefined') { markSeen(); return; }
    var io = new IntersectionObserver(function (entries) {
      for (var j = 0; j < entries.length; j++) {
        if (entries[j].isIntersecting) { markSeen(); io.disconnect(); return; }
      }
    });
    io.observe(stream);
  }
  var streams = document.querySelectorAll('.ix-stream[data-ix-surface]');
  for (var i = 0; i < streams.length; i++) initStream(streams[i]);

  // Category chips (Inbox v2): toggle .ix-hide on the stream's cards —
  // client-side filtering, scoped to the chip's own stream.
  document.addEventListener('click', function (ev) {
    if (!ev.target || !ev.target.closest) return;
    var chip = ev.target.closest('.ix-cat');
    if (!chip) return;
    var stream = chip.closest('.ix-stream');
    if (!stream) return;
    var cat = chip.getAttribute('data-cat');
    var chips = stream.querySelectorAll('.ix-cat');
    for (var i = 0; i < chips.length; i++) chips[i].classList.toggle('is-on', chips[i] === chip);
    var cards = stream.querySelectorAll('.ix-card[data-cat], .alert-card[data-cat]');
    for (var j = 0; j < cards.length; j++) {
      cards[j].classList.toggle('ix-hide', cat !== '*' && cards[j].getAttribute('data-cat') !== cat);
    }
  });
})();
""".strip()
