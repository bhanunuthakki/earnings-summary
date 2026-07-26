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
"""

from __future__ import annotations

from datetime import UTC, datetime
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


def render_open_loops_band(db_path: Path | str | None = None) -> str:
    """The persistent Red Team escalation banner (if any) followed by one
    dense line per non-empty ritual queue, each a doorway; an explicit
    'Ritual clear' line when nothing waits. Never raises."""
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
