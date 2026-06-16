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
    ALERT_STATUS_PENDING,
    AlertRow,
    QueuedActionRow,
    list_alerts,
    list_pending_actions,
    list_queued_actions_for_alerts,
)
from dashboard.inbox_rank import (
    ADVISOR_MEMO_TITLE,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    SEMANTIC_ADVISOR_MEMO,
    annotate_and_rank,
    inbox_label,
    note_semantic_kind,
)
from identity import DEFAULT_USER_ID
from ui.time import stamp_html
from user_state.ledger import list_recent_entries
from user_state.notes import list_notes

__all__ = ["INBOX_CSS", "INBOX_JS", "InboxItem", "collect_inbox", "render_inbox_stream"]

_LEDGER_KIND_LABELS: dict[str, str] = {
    "thesis_update": "Thesis update",
    "bear_append": "Bear-case append",
    "sizing_update": "Sizing change",
    "earnings_prep_append": "Earnings-prep note",
    # The shared constant keeps the label in lockstep with inbox_rank's
    # advisor-memo → synthesis-category refinement.
    "advisor_memo": ADVISOR_MEMO_TITLE,
}

_DEFAULT_KINDS: tuple[str, ...] = ("alert", "draft", "ledger", "note", "synthesis")

# Cross-kind dedupe survivor order: when near-identical bodies land in the
# stream under different kinds (the advisor's memory-everywhere write puts one
# memo line in the ledger AND the journal), keep the kind that carries the
# most context on its card.
_KIND_RICHNESS: dict[str, int] = {"alert": 4, "draft": 3, "synthesis": 2, "ledger": 1, "note": 0}

# Humanized alert trigger labels for the card's kind chip — the raw enum
# (earnings_tone, material_news, …) never reaches a user-facing label
# (design_language §8). The raw kind still rides the card's data-trigger attr,
# so client-side trigger filtering and the tests keep their handle on it.
_TRIGGER_LABELS: dict[str, str] = {
    "earnings_tone": "Earnings tone",
    "material_news": "News",
    "kpi_inflection": "KPI inflection",
    "thesis_drift": "Thesis drift",
    "saydo_due": "Say/do due",
    "decision_condition": "Condition met",
    "restatement": "Restatement",
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
) -> list[InboxItem]:
    """Build the stream — deduped, categorized, ranked.

    ``since`` windows the EVENT kinds — alerts, ledger entries, and synthesis
    sections. Drafts and notes are STANDING items: a pending draft from ten
    days ago is still waiting on you, so they ignore ``since`` and stay in
    the stream (sinking on recency decay) instead of vanishing. ``until``
    upper-bounds everything (a stream re-built for a historical date stays
    honest). ``kinds`` filters the sources; ``status`` / ``trigger_kind`` apply
    to alerts only. ``now`` anchors recency decay (defaults to UTC now; pass a
    historical date to rank as that morning would have). ``position_weights``
    is ticker → fraction-of-book for the ranking factor — ``None`` reads the
    morning-pipeline-materialized weight cache beside the DB (a disk read, never
    the live tracker; empty → equal weighting), ``{}`` forces equal weighting.
    Best-effort: a missing DB or table yields []. The returned list
    is ordered score-descending (newest first on ties) and capped at
    ``limit``.
    """
    if db_path is None or not Path(db_path).exists():
        return []

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
            alerts = list_alerts(
                user_id=user_id,
                ticker=ticker,
                status=status,
                since=since,
                limit=limit,
                db_path=db_path,
            )
        except sqlite3.Error:
            alerts = []
        if trigger_kind:
            alerts = [a for a in alerts if a.trigger_kind == trigger_kind]
        alerts = [a for a in alerts if _in_window(_as_naive_utc(a.fired_at), windowed=True)]
        # One batched IN-query for every alert's queued actions, not one
        # connection-open + query PER alert (the GET / boot N+1).
        try:
            actions_by_alert = list_queued_actions_for_alerts(
                [a.id for a in alerts], db_path=db_path
            )
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
            pending = list_pending_actions(user_id=user_id, db_path=db_path)
        except sqlite3.Error:
            pending = []
        ticker_by_alert: dict[int, str] = {}
        if pending:
            try:
                for a in list_alerts(user_id=user_id, limit=500, db_path=db_path):
                    ticker_by_alert[a.id] = a.ticker
            except sqlite3.Error:
                pass
        for qa in pending:
            if qa.alert_id in shown_alert_ids:
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
            entries = list_recent_entries(user_id=user_id, limit=60, db_path=db_path)
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
            notes = list_notes(user_id=user_id, ticker=ticker, status="open", db_path=db_path)
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

                reconcile_why = pending_reconciliation_note_ids(db_path=db_path, user_id=user_id)
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

    if "synthesis" in kinds and ticker is None:
        # Portfolio-scope insight: the cross-portfolio synthesis memo's
        # structured sections, only while the lens output is fresh.
        for s in _synthesis_items(db_path, now=now_dt):
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
        deduped, db_path=Path(db_path), now=now_dt, position_weights=position_weights
    )
    return ranked[:limit]


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


def _synthesis_items(db_path: Path, *, now: datetime) -> list[InboxItem]:
    """Stream items from the latest cached cross-portfolio synthesis memo,
    only while it is fresh (≤ ``_SYNTHESIS_FRESH_DAYS`` old). Best-effort:
    a missing table / artifact / unparsable timestamp yields []."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        has_artifacts = (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
            ).fetchone()
            is not None
        )
        if not has_artifacts:
            return []
        row = conn.execute(
            """
            SELECT content_md, generated_at FROM llm_artifacts
            WHERE purpose = 'lens:cross_portfolio_synthesis'
              AND scope = 'portfolio'
              AND superseded_by_id IS NULL
            ORDER BY generated_at DESC LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
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


def render_inbox_stream(
    items: list[InboxItem],
    *,
    db_path: Path | None = None,
    compact: bool = False,
    show_status_badge: bool = True,
    show_filters: bool = False,
    surface: str | None = None,
    empty_text: str = "Nothing new — alerts, drafts, thesis changes, and watch items land here.",
) -> str:
    """The stream HTML: ONE flat list in the score order ``collect_inbox``
    returns — no recency-bucket headers; each card's top-right relative stamp
    carries the "when". ``compact`` is the Home-rail variant — clamped bodies,
    collapsed evidence, no nested action bodies, hover ✓/✕ quick actions on
    approvable cards. ``show_filters`` renders the category chips (client-side
    filtering via INBOX_JS). ``surface`` names the page ("home" | "feed") for
    the unread tracking: INBOX_JS keys its per-surface localStorage last-seen
    off the ``data-ix-surface`` attr."""
    if not items:
        return f'<div class="ix-empty">{_esc(empty_text)}</div>'
    out = StringIO()
    surface_attr = f' data-ix-surface="{_esc(surface)}"' if surface else ""
    out.write(f'<div class="ix-stream{" ix-compact" if compact else ""}"{surface_attr}>')
    if show_filters:
        _render_category_chips(out, items)
    for it in items:
        _render_item(out, it, db_path=db_path, compact=compact, show_status=show_status_badge)
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
        '<button type="button" class="ix-cat k-chip-btn is-on" data-cat="*">'
        f"All <span>{len(items)}</span></button>"
    )
    for slug in CATEGORY_ORDER:
        n = counts.get(slug, 0)
        if not n:
            continue
        out.write(
            f'<button type="button" class="ix-cat k-chip-btn" data-cat="{_esc(slug)}">'
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
    # data-kind) — never a visible label; the chip humanizes it (§8).
    trig_attr = f' data-trigger="{_esc(it.title)}"' if it.kind == "alert" else ""
    out.write(
        f'<div class="ix-card" data-kind="{_esc(it.kind)}"{cat_attr}{trig_attr} '
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
    out.write(
        f'<span class="ix-kind ix-kind-{_esc(it.kind)}"{why_attr}>{_esc(_chip_label(it))}</span>'
    )
    if show_status and it.status and it.status not in ("open",):
        out.write(f'<span class="ix-status ix-status-{_esc(it.status)}">{_esc(it.status)}</span>')
    if compact:
        _render_quick_actions(out, it)
    out.write(stamp_html(it.when, css="ix-when"))
    out.write("</div>")
    body = _display_body(it)
    if body:
        out.write(f'<div class="ix-body">{_esc(body)}</div>')
    _render_card_footer(out, it, compact=compact)
    out.write("</div>")


def _chip_label(it: InboxItem) -> str:
    """The kind chip's text. The raw enum never reaches a label (design_language
    §8; it rides ``data-trigger`` instead):

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
        _render_memo_actions(out, it)
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
        parts.append(f'<a class="ix-foot-act" href="/approve?action_id={pending.id}">approve</a>')
        parts.append(
            '<a class="ix-foot-act ix-foot-dismiss" '
            f'href="/approve?action_id={pending.id}&dismiss=1">dismiss</a>'
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
    if it.semantic_kind == SEMANTIC_ADVISOR_MEMO:
        return _LEADING_TAG_RE.sub("", body)
    return body


def _render_memo_actions(out: StringIO, it: InboxItem) -> None:
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
            '<button type="button" class="k-chip k-chip-btn ix-note-dismiss" '
            f'data-note-id="{it.note_id}" title="Archive this memo note">dismiss</button>'
        )
    out.write("</div>")


def _pending_action(it: InboxItem) -> QueuedActionRow | None:
    """The queued action still awaiting the owner on this card, or None. Drafts
    act on their own action; alert cards act on their first still-pending queued
    action (one action per alert is the drafter's norm)."""
    if it.kind == "draft" and it.action is not None and it.action.status == ACTION_STATUS_PENDING:
        return it.action
    if it.kind == "alert":
        for qa in it.actions:
            if qa.status == ACTION_STATUS_PENDING:
                return qa
    return None


def _quick_action_id(it: InboxItem) -> int | None:
    """The queued-action id the rail's hover ✓/✕ operate on, or None when the
    card carries nothing approvable."""
    qa = _pending_action(it)
    return qa.id if qa is not None else None


def _render_quick_actions(out: StringIO, it: InboxItem) -> None:
    """The compact rail's hover ✓/✕ affordances, written into the EXISTING
    header row before the timestamp — visibility-flipped on card hover / focus
    so the resting layout is byte-identical (the zero-height design). What
    appears is resolved from the card's identity, giving the rail the same
    actionability the full feed's cards carry (owner ask: chip parity + a
    dismiss on every actionable card):

      * a pending queued action (a draft, or an alert that drafted one) → a
        ✓ that approves it (``data-action-id`` → POST /approve);
      * an alert → a ✕ that dismisses the ALERT itself (``data-alert-id`` →
        POST /api/alerts/<id>/dismiss), the alert-level counterpart to a
        draft's action-level dismiss; the route also cancels any pending draft
        so the dismissed alert can't leave one behind to resurface;
      * a standalone draft → its ✕ dismisses the ACTION (``data-action-id`` +
        ``data-dismiss``), unchanged;
      * a plain analyst note → a ✕ that archives it (``data-note-id`` → POST
        /api/notes/<id>/archive).

    Informational ledger / synthesis cards carry nothing — they age out on
    recency decay (owner choice: dismiss is for actionable items only).
    Advisor-memo notes keep their own always-visible ``.ix-memo-acts`` row
    (open-memo + dismiss), so they're skipped here to avoid a double affordance.
    """
    buttons: list[str] = []
    action_id = _quick_action_id(it)
    if action_id is not None:
        buttons.append(
            f'<button class="ix-act ix-act-approve k-btn k-btn-quiet k-btn-sm" type="button" '
            f'data-action-id="{action_id}" title="Approve &amp; apply">&#10003;</button>'
        )
    if it.kind == "alert" and it.alert is not None and it.alert.status == ALERT_STATUS_PENDING:
        # The card-level dismiss: clear the whole alert from the inbox (not
        # just one drafted action). Present whether or not a draft exists, so
        # an alert that never drafted an action (e.g. earnings_tone) is still
        # dismissable from the rail.
        buttons.append(
            f'<button class="ix-act ix-act-dismiss k-btn k-btn-quiet k-btn-sm" type="button" '
            f'data-alert-id="{it.alert.id}" title="Dismiss alert">&#10005;</button>'
        )
    elif action_id is not None:
        # A standalone draft (no parent alert on this card) — dismiss the
        # action itself, the original behavior.
        buttons.append(
            f'<button class="ix-act ix-act-dismiss k-btn k-btn-quiet k-btn-sm" type="button" '
            f'data-action-id="{action_id}" data-dismiss="1" title="Dismiss">&#10005;</button>'
        )
    elif it.kind == "note" and it.note_id is not None and it.semantic_kind != SEMANTIC_ADVISOR_MEMO:
        buttons.append(
            f'<button class="ix-act ix-act-dismiss k-btn k-btn-quiet k-btn-sm" type="button" '
            f'data-note-id="{it.note_id}" title="Dismiss">&#10005;</button>'
        )
    if buttons:
        out.write('<span class="ix-quick">' + "".join(buttons) + "</span>")


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
    from dashboard._card import _memo_text_from_evidence

    return _memo_text_from_evidence(it.alert.evidence_json) or ""


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


INBOX_CSS = """
.ix-stream { display: flex; flex-direction: column; gap: var(--sp-2); }
.ix-card { border-radius: var(--radius);
  background: var(--surface); padding: 9px 12px; }
.ix-head { display: flex; align-items: baseline; gap: 8px; }
.ix-ticker { font-family: var(--mono); font-weight: 600; font-size: var(--fs-caption);
  color: var(--fg); text-decoration: none; }
.ix-ticker:hover { color: var(--accent); }
.ix-kind { font-size: var(--fs-micro); font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--muted); }
.ix-status { font-size: var(--fs-micro); font-weight: 600; border: 1px solid var(--border);
  border-radius: var(--radius); padding: 0 5px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.05em; }
.ix-status-pending { color: var(--warn); border-color: var(--warn); }
.ix-status-applied, .ix-status-approved { color: var(--ok); }
.ix-when { margin-left: auto; color: var(--muted); font-size: var(--fs-micro);
  font-family: var(--mono); white-space: nowrap; }
.ix-body { margin-top: 5px; font-size: var(--fs-body); line-height: 1.45; color: var(--fg);
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.ix-compact .ix-body { -webkit-line-clamp: 2; }
.ix-card:hover .ix-body { -webkit-line-clamp: unset; }
/* The card's one action/doorway row (approve · dismiss · article · review) —
   the SAME footer shape for every kind, replacing the old per-kind .ix-actions
   block and bespoke .alert-card queued-action panel. */
.ix-foot { display: flex; flex-wrap: wrap; align-items: baseline; gap: 14px;
  margin-top: 6px; }
.ix-foot a { font-size: var(--fs-caption); text-decoration: none; }
.ix-foot-act { color: var(--accent); }
.ix-foot-act:hover { text-decoration: underline; }
.ix-foot-dismiss { color: var(--muted); }
.ix-foot-dismiss:hover { color: var(--bad); text-decoration: none; }
.ix-foot-link { color: var(--muted); }
.ix-foot-link:hover { color: var(--accent); }
/* Advisor-memo affordances — the shared .k-chip kit (controls.py), so the
   memo card carries dismiss + open-memo without a bespoke button system. */
.ix-memo-acts { display: flex; gap: 6px; margin-top: 7px; }
.ix-memo-open { text-decoration: none; }
.ix-note-dismiss:hover { color: var(--bad); border-color: var(--bad); }
.ix-empty { color: var(--muted); font-size: var(--fs-body); padding: 14px 4px; }
/* Quick approve/dismiss (compact rail cards) — zero-height: the buttons sit
   in the existing header row and flip visibility (layout stays reserved, so
   nothing shifts) on card hover / keyboard focus. */
.ix-quick { margin-left: auto; display: inline-flex; gap: 3px; visibility: hidden; }
.ix-card:hover .ix-quick, .ix-quick:focus-within { visibility: visible; }
.ix-quick ~ .ix-when, .ix-acted ~ .ix-when { margin-left: 0; }
/* Quick approve/dismiss = .k-btn .k-btn-quiet .k-btn-sm (kit); only the
   semantic hover/fail tones are local — a success/danger affordance the
   quiet button has no intent for. */
.ix-act-approve:hover { color: var(--ok); border-color: var(--ok); }
.ix-act-dismiss:hover { color: var(--bad); border-color: var(--bad); }
.ix-act[disabled] { opacity: 0.5; cursor: default; }
.ix-act-fail { color: var(--bad); border-color: var(--bad); }
.ix-acted { margin-left: auto; font-size: var(--fs-micro); font-weight: 600;
  white-space: nowrap; color: var(--muted); }
.ix-acted-applied { color: var(--ok); }
.ix-status-cancelled { color: var(--muted); }
.ix-dismissed { opacity: 0.55; transition: opacity var(--transition); }
/* Unread ("since you last looked") — inset accent bar: no border-width
   change, zero layout shift. Accent is sanctioned here: unread marks are
   actionable state, the one non-link accent this surface carries. */
.ix-new { box-shadow: inset 2px 0 0 var(--accent); }
.ix-badge { display: inline-block; min-width: 14px; text-align: center;
  margin-left: 6px; padding: 1px 5px; border-radius: var(--radius-full);
  background: var(--accent); color: var(--accent-contrast);
  font-family: var(--mono); font-size: var(--fs-micro); font-weight: 600;
  line-height: 1.4; vertical-align: 2px; }
.ix-badge[hidden] { display: none; }
/* Category filter chips (Inbox v2) — client-side, scoped per stream. */
.ix-cats { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; }
/* Category filter chip = .k-chip-btn (kit); .is-on is the kit's active state.
   Only the count span's de-emphasis is local. */
.ix-cat span { opacity: 0.7; margin-left: 2px; }
.ix-hide { display: none !important; }
/* "Why ranked here" — the factor breakdown rides the kind chip's title. */
.ix-kind[title] { cursor: help; }
.ix-kind-synthesis { color: var(--accent); }
""".strip()

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
# 2. QUICK ACTIONS — the rail's hover ✓/✕, routed by which id the button
#    carries so the rail can act on the same surface the full feed does:
#      * data-action-id  → POST /approve  (approve / dismiss a queued action),
#      * data-alert-id   → POST /api/alerts/<id>/dismiss  (dismiss the alert),
#      * data-note-id    → POST /api/notes/<id>/archive   (dismiss a note).
#    Each updates the card in place: an approve swaps a draft's status chip
#    (applied/cancelled); a dismissal (action / alert / note) fades the card
#    and leaves a small "✓ applied" / "✕ dismissed" confirmation where the
#    buttons sat. Alert/note dismissals clear the card from the inbox.
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

  document.addEventListener('click', function (ev) {
    if (!ev.target || !ev.target.closest) return;
    var btn = ev.target.closest('.ix-act');
    if (!btn || btn.disabled) return;
    var actionId = btn.getAttribute('data-action-id');
    var alertId = btn.getAttribute('data-alert-id');
    var noteId = btn.getAttribute('data-note-id');
    // alert / note ✕ are dismissals by construction; the action ✓/✕ pair keys
    // off data-dismiss. A dismissal clears the card; an approve swaps a pill.
    var dismiss = btn.getAttribute('data-dismiss') === '1' || alertId !== null || noteId !== null;
    var clears = alertId !== null || noteId !== null;
    var card = btn.closest('.ix-card');
    var quick = btn.closest('.ix-quick');
    var btns = quick ? quick.querySelectorAll('.ix-act') : [btn];
    for (var i = 0; i < btns.length; i++) btns[i].disabled = true;
    var req;
    if (alertId !== null) {
      req = fetch('/api/alerts/' + encodeURIComponent(alertId) + '/dismiss', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      });
    } else if (noteId !== null) {
      req = fetch('/api/notes/' + encodeURIComponent(noteId) + '/archive', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      });
    } else {
      req = fetch('/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'action_id=' + encodeURIComponent(actionId) + (dismiss ? '&dismiss=1' : '')
      });
    }
    req.then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (payload) {
        if (!resp.ok) throw new Error(payload.error || ('HTTP ' + resp.status));
        return payload;
      });
    }).then(function (payload) {
      if (!card) return;
      var status = payload.status || (dismiss ? 'cancelled' : 'applied');
      if (quick) {
        var done = document.createElement('span');
        done.className = 'ix-acted ix-acted-' + status;
        done.textContent = dismiss ? '✕ dismissed' : '✓ applied';
        quick.parentNode.replaceChild(done, quick);
      }
      if (clears) {
        // A dismissed alert / archived note leaves the inbox entirely.
        var open = card.querySelector('.ix-open');
        if (open) open.remove();
        card.classList.add('ix-dismissed');
      } else if (card.getAttribute('data-kind') === 'draft') {
        var chip = card.querySelector('.ix-status');
        if (!chip) {
          chip = document.createElement('span');
          var head = card.querySelector('.ix-head');
          var anchor = card.querySelector('.ix-acted') || card.querySelector('.ix-when');
          if (head) head.insertBefore(chip, anchor);
        }
        chip.className = 'ix-status ix-status-' + status;
        chip.textContent = status;
        var dOpen = card.querySelector('.ix-open');
        if (dOpen) dOpen.remove();
        if (dismiss) card.classList.add('ix-dismissed');
      }
    }).catch(function (err) {
      for (var k = 0; k < btns.length; k++) btns[k].disabled = false;
      btn.classList.add('ix-act-fail');
      btn.title = String((err && err.message) || err);
    });
  });

  // Advisor-memo dismiss chip (.ix-note-dismiss): archive the note-backed memo
  // via the REST endpoint the journal already uses, then fade the card in
  // place. Distinct from the .ix-act quick buttons above (those POST /approve
  // on queued drafts) — this is the memo card's own affordance.
  document.addEventListener('click', function (ev) {
    if (!ev.target || !ev.target.closest) return;
    var chip = ev.target.closest('.ix-note-dismiss');
    if (!chip || chip.disabled) return;
    var noteId = chip.getAttribute('data-note-id');
    var card = chip.closest('.ix-card');
    chip.disabled = true;
    fetch('/api/notes/' + encodeURIComponent(noteId) + '/archive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    }).then(function (resp) {
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      if (!card) return;
      card.classList.add('ix-dismissed');
      var acts = card.querySelector('.ix-memo-acts');
      if (acts) acts.innerHTML = '<span class="ix-acted ix-acted-applied">✓ dismissed</span>';
    }).catch(function (err) {
      chip.disabled = false;
      chip.classList.add('ix-act-fail');
      chip.title = String((err && err.message) || err);
    });
  });
})();
""".strip()
