"""The open-loops band — ritual debt, rendered where the day starts.

The 2026-07-02 UX audit's top finding: every learning-circuit queue
(seed Reconcile, proposed Tenets, pending research proposals, owner decision
stubs missing their words, the coach's digest lane) waited 2+ clicks deep
while Home rendered market data only — the "fortress with no inhabitants"
entry point, unchanged. This band puts one dense line per non-empty queue
above the cockpit, each line a doorway into the panel that drains it; when
nothing waits it says so explicitly (an empty circuit that renders as blank
space can never pull the owner toward feeding it).

Every count is a cheap read over tables that already exist. Each query is
independently guarded so a pre-migration DB (or a stub fixture) can never
break Home — a queue whose table is missing simply doesn't render.

Pending Decision Draft confirmations and un-dispositioned Investment
Decision Cards (2026-07-25 PRD closeout postmortem) get their own lines,
queried directly here rather than through the Senior Partner Brief: 78
tracker-sourced drafts piled up unconfirmed because their only doorway was a
chip inside ``senior_partner_brief_panel.render_brief_today_card``, which
renders "" until a ``senior_partner_brief`` artifact exists at all — so the
confirmation queue was invisible for as long as the brief had never run.
This band already renders unconditionally (falling back to "Ritual clear"),
so these two lines surface the same instant a draft/card actually needs the
owner, with no dependency on any LLM artifact ever having been generated.

Home-band consolidation (navigation_ia.md D1, wave3b — ~2-viewport budget):
two more bands folded into this one rather than living beside it.

* The retired ``coach_strip`` band's ONLY count these existing lines didn't
  already cover was "sent today" (Telegram already pushed it; the digest and
  routed-to-brief queues above already count everything else the coach can
  hold) — one line, same doorway convention as its siblings.
* The Sunday packet (``pipeline.weekly_packet``) gets a state line reading
  the SAME ``weekly_packet_runs``/``weekly_packet_items`` rows the Telegram
  packet itself uses (never a parallel count that could disagree with what
  Telegram already showed) — dark when the packet is clear/complete or no
  run exists yet this ISO week (the ratified "dark-when-clear" contract;
  never a zero-count line). This band's render function stays read-only —
  it never calls ``weekly_packet.ensure_run`` (which INSERTs); it reads the
  current week's row directly, or finds none and renders nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from pathlib import Path

from user_state._db import open_conn

# Doorways are PANEL hashes — the shell router treats unknown hashes as panel
# ids, so these must stay panel ids ('musings' = the Ledger tab, where the
# Reconcile / Worldview / Research sections live; 'decisions_record' = the
# Portfolio > Decisions panel). The decisions id is interpolated rather than
# written as one '#decisions…' literal: the token guard's hex scan would read
# '#dec' as a raw color (tests/test_ui_controls.py scans every value literal
# in a CSS-emitting module).
_DECISIONS_PANEL = "decisions_record"
_LEDGER_HASH = "/#musings"
_DECISIONS_HASH = f"/#{_DECISIONS_PANEL}"
_RED_TEAM_HASH = "/#red_team"
# Not a shell panel hash — the mobile Inbox is its own route
# (execution/comments_server.py), the same doorway
# senior_partner_brief_panel.render_brief_today_card already links to.
_MOBILE_INBOX_HREF = "/mobile/inbox"

STYLE = """<style>
/* Promoted 2026-07-18 (UX audit): this is the page's one "what needs you
   today" line — it was rendering at --fs-caption with no fill, the same
   visual weight as table-header/timestamp metadata, so the intended entry
   point read as a footnote. Now a .k-well block (kit) at --fs-body, with
   each count a .k-pill-warn (kit) instead of plain mono text. */
.cc-open-loops { display: flex; flex-wrap: wrap; align-items: baseline;
  gap: 6px 18px; margin: 0 0 10px; font-size: var(--fs-body); }
.cc-ol-head { color: var(--fg); font-weight: 600; }
.cc-ol-line { color: var(--fg); text-decoration: none; display: inline-flex;
  align-items: baseline; gap: 6px; }
.cc-ol-line:hover { color: var(--accent); }
.cc-ol-line:hover .cc-ol-count { color: var(--accent); }
.cc-ol-clear { color: var(--muted); }
.cc-ol-escalation { margin: 0 0 8px; }
.cc-ol-escalation a { color: inherit; text-decoration: underline; }
</style>"""


def _age_suffix(oldest_iso: object) -> str:
    """'· oldest 12d' from a stored naive-UTC ISO stamp; '' when unparseable."""
    try:
        stamp = datetime.fromisoformat(str(oldest_iso))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(UTC).replace(tzinfo=None)
        days = (datetime.now(UTC).replace(tzinfo=None) - stamp).days
        return f" · oldest {days}d" if days > 0 else ""
    except Exception:
        return ""


def _reconcile_count(db_path: Path | str | None) -> int:
    from synthesis.reconcile import list_unreconciled

    return len(list_unreconciled(db_path))


def _proposed_tenet_count(db_path: Path | str | None) -> int:
    from pipeline.worldview_panel import worldview_enabled
    from synthesis.tenets import list_tenets

    if not worldview_enabled():
        return 0
    return len(list_tenets(status="proposed", db_path=db_path))


def _pending_proposal_count(db_path: Path | str | None) -> int:
    from research.proposals import list_proposals

    return len(list_proposals(status="pending", db_path=db_path))


def _decision_stub_debt(db_path: Path | str | None) -> tuple[int, str]:
    """Owner decisions still ungradeable — NULL conviction or falsifier. The
    same debt the governor's retro_annotation ping chases, counted without its
    channel/age filters (any incomplete stub starves Brier + the tripwires)."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*), MIN(created_at) FROM decisions
            WHERE decided_by = 'owner' AND outcome_label = 'pending'
              AND (conviction IS NULL OR falsifier IS NULL)
            """
        ).fetchone()
        return int(row[0] or 0), _age_suffix(row[1]) if row[0] else ""
    finally:
        conn.close()


def _digest_ping_debt(db_path: Path | str | None) -> tuple[int, str]:
    """Coach pings parked in the digest lane (over-cap or send-failed) — the
    write-only queue the audit found no surface ever rendered."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), MIN(created_at) FROM coach_pings WHERE status = 'digest'"
        ).fetchone()
        return int(row[0] or 0), _age_suffix(row[1]) if row[0] else ""
    finally:
        conn.close()


def _routed_to_brief_debt(db_path: Path | str | None) -> tuple[int, str]:
    """P2.2 (personal_investment_partner_prd.md §9.1): coach pings the
    governor routed to the Senior Partner Brief (calibration_finding /
    capacity_breach / life_event_checkpoint / profile_drift —
    ``research.governor.BRIEF_ROUTED_CLASSES``) and no brief has drained yet
    (``status = 'routed_to_brief'``). Without this line these four classes'
    items would be invisible on Home between the moment the governor routes
    them and the next weekly brief — the digest-debt line above only ever
    counted ``status = 'digest'``, which these rows never reach."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), MIN(created_at) FROM coach_pings WHERE status = 'routed_to_brief'"
        ).fetchone()
        return int(row[0] or 0), _age_suffix(row[1]) if row[0] else ""
    finally:
        conn.close()


def _pending_draft_confirmation_debt(db_path: Path | str | None) -> tuple[int, str]:
    """Decision drafts (tracker/Telegram/web capture — any ``source_channel``)
    still ``awaiting_confirmation`` — the same ``decision_drafts`` read
    ``pipeline.mobile_inbox_panel._drafts_section`` uses, so this line and
    the mobile Inbox never drift."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), MIN(created_at) FROM decision_drafts "
            "WHERE status = 'awaiting_confirmation'"
        ).fetchone()
        return int(row[0] or 0), _age_suffix(row[1]) if row[0] else ""
    finally:
        conn.close()


def _undispositioned_card_debt(db_path: Path | str | None) -> int:
    """Evaluation-list Investment Decision Cards with no pass/watch/promote
    disposition recorded yet — the same read
    ``pipeline.mobile_inbox_panel._card_dispositions_section`` uses."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM tracked_companies tc
            JOIN llm_artifacts la
              ON la.purpose = 'investment_decision_card'
             AND UPPER(la.ticker) = UPPER(tc.ticker)
             AND la.superseded_by_id IS NULL
            WHERE tc.list_type = 'evaluation'
              AND NOT EXISTS (
                SELECT 1 FROM decisions d
                WHERE d.advice_artifact_id = la.id
                  AND d.recommendation_kind IN ('pass', 'watch', 'promote')
              )
            """
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _coach_sent_today_debt(db_path: Path | str | None, *, now: datetime | None = None) -> int:
    """Coach pings actually pushed to Telegram today — the ONE count the
    retired ``coach_strip`` band carried that ``_digest_ping_debt`` /
    ``_routed_to_brief_debt`` above don't already cover (those two count the
    'digest' and 'routed_to_brief' queues; 'sent' is the third, disjoint
    status the strip used to render up to 4 rows of). Folded to a single
    count line — the strip's per-item detail (class label + body text) lived
    on Telegram already; this line's job is just parity ("Home shows what
    Telegram showed"), not a duplicate reading surface."""
    stamp = now or datetime.now(UTC).replace(tzinfo=None)
    day_start = stamp.date().isoformat()
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM coach_pings WHERE status = 'sent' AND created_at >= ?",
            (day_start,),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _packet_state(
    db_path: Path | str | None, *, now: datetime | None = None
) -> tuple[int, int] | None:
    """(answered, total) for the CURRENT ISO week's Sunday packet run, or
    ``None`` when there is nothing to show — no run yet this week, or the run
    is already dark (status 'clear'/'complete', or zero items). Reads
    ``weekly_packet_runs``/``weekly_packet_items`` directly (never
    ``pipeline.weekly_packet.ensure_run``, which INSERTs a row — this render
    path must stay read-only) so the number can never disagree with what the
    Sunday packet itself already showed on Telegram."""
    stamp = now or datetime.now(UTC).replace(tzinfo=None)
    iso_year, iso_week, _ = stamp.isocalendar()
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, status, total_items FROM weekly_packet_runs "
            "WHERE iso_year = ? AND iso_week = ?",
            (iso_year, iso_week),
        ).fetchone()
        if row is None:
            return None
        run_id, status, total_items = int(row[0]), str(row[1]), int(row[2] or 0)
        if status != "open" or not total_items:
            return None
        pending_row = conn.execute(
            "SELECT COUNT(*) FROM weekly_packet_items WHERE run_id = ? AND verdict IS NULL",
            (run_id,),
        ).fetchone()
        pending = int(pending_row[0] or 0)
        return total_items - pending, total_items
    finally:
        conn.close()


def render_weekly_packet_peek(db_path: Path | str | None, *, now: datetime | None = None) -> str:
    """The read-only quick-look behind the Sunday-packet line's
    ``data-peek-url`` — one row per item in the CURRENT week's run, verdict
    (or 'pending'), and a reminder that verdicts are given on Telegram (this
    page has no reply mechanism of its own — study §4/Task 3's "peeks the
    packet state read-only" instruction). Never raises; an empty/absent run
    renders a quiet note rather than a blank popover. ``now`` overrides the
    wall clock (tests pin it to dodge the UTC-midnight ISO-week flake)."""
    try:
        stamp = now or datetime.now(UTC).replace(tzinfo=None)
        iso_year, iso_week, _ = stamp.isocalendar()
        conn = open_conn(db_path)
        try:
            run = conn.execute(
                "SELECT id, status, total_items FROM weekly_packet_runs "
                "WHERE iso_year = ? AND iso_week = ?",
                (iso_year, iso_week),
            ).fetchone()
            if run is None:
                return '<div class="k-well muted">No Sunday packet run yet this week.</div>'
            run_id, status, total_items = int(run[0]), str(run[1]), int(run[2] or 0)
            items = conn.execute(
                "SELECT item_kind, ticker, title, verdict FROM weekly_packet_items "
                "WHERE run_id = ? ORDER BY order_index",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return '<div class="k-well muted">Packet state unavailable.</div>'
    if not items:
        return f'<div class="k-well muted">Packet clear this week (status: {escape(status)}).</div>'
    rows = "".join(
        '<div class="cc-ol-line">'
        f'<span class="k-chip k-chip-mono">{escape(str(kind))}</span> '
        f'{escape((str(ticker) + " — ") if ticker else "")}{escape(str(title)[:160])} '
        f'<span class="muted">{escape(str(verdict) if verdict else "pending")}</span></div>'
        for kind, ticker, title, verdict in items
    )
    return (
        f'<div class="k-well"><span class="k-chip k-chip-mono">Sunday packet</span> '
        f'<span class="muted">{total_items} items — verdicts are given on Telegram</span>'
        f"{rows}</div>"
    )


def _packet_line(db_path: Path | str | None, *, now: datetime | None = None) -> str:
    """Task 3: "Sunday packet · N of M answered · finish ▸" — dark (returns
    "") when the packet is clear/complete or no run exists this week, per the
    ratified "dark-when-clear" contract (never a zero-count line). The doorway
    falls back to a full-page destination (the Ledger, where the packet's
    substrate queues live) for a plain click/middle-click, and opens the
    read-only quick-look in place via ``data-peek-url`` (no web reply surface
    exists — verdicts are given on Telegram, per Task 3's spec)."""
    state = _packet_state(db_path, now=now)
    if state is None:
        return ""
    answered, total = state
    return (
        f'<a class="cc-ol-line" href="{_LEDGER_HASH}" data-peek-url="/api/peek/weekly-packet" '
        f'data-peek-title="Sunday packet">Sunday packet · {answered} of {total} answered · '
        "finish ▸</a>"
    )


def _line(href: str, label: str, count: int, suffix: str = "") -> str:
    return (
        f'<a class="cc-ol-line" href="{href}">{label}: '
        f'<span class="k-pill k-pill-warn">{count}</span>{suffix}</a>'
    )


def _red_team_escalated_count(db_path: Path | str | None) -> int:
    """Items that used their one allowed DEFER and are still unanswered
    (PR6, monthly_red_team.md Phase 2 — "a SECOND defer ... escalates to a
    persistent Home-band banner"). Degrades to 0 on a pre-migration DB (the
    caller's own try/except already covers it; this helper just isolates
    the redteam import so a missing package can't break the whole band)."""
    from redteam.gate import escalated_items

    return len(escalated_items(db_path=db_path))


def _escalation_banner(db_path: Path | str | None) -> str:
    """The persistent 'Red Team: N items escalated' banner — a k-well -bad
    block (never the quiet ritual-debt line style; this is a forced-response
    failure, not an inert queue count) rendered ABOVE the open-loops line.
    Empty string when nothing is escalated."""
    try:
        n = _red_team_escalated_count(db_path)
    except Exception:
        return ""
    if not n:
        return ""
    plural = "item" if n == 1 else "items"
    return (
        '<div class="k-well k-well-bad cc-ol-escalation">'
        f'<a href="{_RED_TEAM_HASH}">Red Team: {n} {plural} escalated — '
        "respond to close the month</a></div>"
    )


def render_open_loops_band(
    db_path: Path | str | None = None, *, now: datetime | None = None
) -> str:
    """The persistent Red Team escalation banner (if any) followed by one
    dense line per non-empty ritual queue, each a doorway; an explicit
    'Ritual clear' line when nothing waits. Never raises. ``now`` overrides
    the wall clock for the two date-boundary-sensitive lines (coach
    sent-today, the Sunday packet's ISO week) — tests pin it to dodge the
    repo's known UTC-midnight flake class; production leaves it unset."""
    banner = _escalation_banner(db_path)
    lines: list[str] = []

    try:
        n, age = _pending_draft_confirmation_debt(db_path)
        if n:
            lines.append(_line(_MOBILE_INBOX_HREF, "Pending confirmations", n, age))
    except Exception:
        pass
    try:
        n = _undispositioned_card_debt(db_path)
        if n:
            lines.append(_line(_MOBILE_INBOX_HREF, "Cards awaiting disposition", n))
    except Exception:
        pass
    try:
        n = _reconcile_count(db_path)
        if n:
            lines.append(_line(_LEDGER_HASH, "Reconcile", n))
    except Exception:
        pass
    try:
        n = _proposed_tenet_count(db_path)
        if n:
            lines.append(_line(_LEDGER_HASH, "Tenets proposed", n))
    except Exception:
        pass
    try:
        n = _pending_proposal_count(db_path)
        if n:
            lines.append(_line(_LEDGER_HASH, "Research proposals", n))
    except Exception:
        pass
    try:
        n, age = _decision_stub_debt(db_path)
        if n:
            lines.append(_line(_DECISIONS_HASH, "Decisions missing conviction/falsifier", n, age))
    except Exception:
        pass
    try:
        n, age = _digest_ping_debt(db_path)
        if n:
            lines.append(_line(_LEDGER_HASH, "Coach digest", n, age))
    except Exception:
        pass
    try:
        n, age = _routed_to_brief_debt(db_path)
        if n:
            lines.append(_line(_LEDGER_HASH, "Routed to weekly brief", n, age))
    except Exception:
        pass
    try:
        n = _coach_sent_today_debt(db_path, now=now)
        if n:
            lines.append(_line(_LEDGER_HASH, "Coach sent today", n))
    except Exception:
        pass
    try:
        packet_line = _packet_line(db_path, now=now)
        if packet_line:
            lines.append(packet_line)
    except Exception:
        pass

    if not lines:
        return (
            STYLE + banner + '<div class="cc-open-loops k-well">'
            '<span class="cc-ol-clear">Ritual clear - nothing waiting on you.</span></div>'
        )
    return (
        STYLE
        + banner
        + '<div class="cc-open-loops k-well"><span class="cc-ol-head">Open loops</span>'
        + "".join(lines)
        + "</div>"
    )
