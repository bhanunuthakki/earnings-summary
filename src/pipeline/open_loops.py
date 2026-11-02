"""Render owner queues that need attention on the home screen."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pipeline.analysis_styles import ANALYSIS_STYLE
from user_state._db import open_read_conn

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
# Not a shell panel hash — the mobile Inbox is its own route.
_MOBILE_INBOX_HREF = "/mobile/inbox"


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


def reconcile_count(db_path: Path | str | None, *, conn: sqlite3.Connection | None = None) -> int:
    from synthesis.reconcile import list_unreconciled

    return len(list_unreconciled(db_path, conn=conn))


def proposed_tenet_count(
    db_path: Path | str | None, *, conn: sqlite3.Connection | None = None
) -> int:
    from pipeline.worldview_panel import worldview_enabled
    from synthesis.tenets import list_tenets

    if not worldview_enabled():
        return 0
    return len(list_tenets(status="proposed", db_path=db_path, conn=conn))


def pending_proposal_count(
    db_path: Path | str | None, *, conn: sqlite3.Connection | None = None
) -> int:
    from research.proposals import list_proposals

    return len(list_proposals(status="pending", db_path=db_path, conn=conn))


def _pending_draft_confirmation_debt(
    db_path: Path | str | None, *, conn: sqlite3.Connection | None = None
) -> tuple[int, str]:
    """Decision drafts (tracker/Telegram/web capture — any ``source_channel``)
    still ``awaiting_confirmation`` in the canonical decision-draft store.
    The mobile Inbox link opens the responsive Cockpit action queue."""
    db_conn = conn or open_read_conn(db_path)
    try:
        row = db_conn.execute(
            "SELECT COUNT(*), MIN(created_at) FROM decision_drafts "
            "WHERE status = 'awaiting_confirmation'"
        ).fetchone()
        return int(row[0] or 0), _age_suffix(row[1]) if row[0] else ""
    finally:
        if conn is None:
            db_conn.close()


def _undispositioned_card_debt(
    db_path: Path | str | None, *, conn: sqlite3.Connection | None = None
) -> int:
    """Evaluation-list Investment Decision Cards with no pass/watch/promote
    disposition recorded yet. The mobile Inbox link opens the responsive Cockpit."""
    db_conn = conn or open_read_conn(db_path)
    try:
        row = db_conn.execute(
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
        if conn is None:
            db_conn.close()


def _line(href: str, label: str, count: int, suffix: str = "") -> str:
    return (
        f'<a class="cc-ol-line" href="{href}">{label}: '
        f'<span class="k-pill k-pill-warn">{count}</span>{suffix}</a>'
    )


def _red_team_escalated_count(
    db_path: Path | str | None, *, conn: sqlite3.Connection | None = None
) -> int:
    """Items that used their one allowed DEFER and are still unanswered
    (PR6, monthly_red_team.md Phase 2 — "a SECOND defer ... escalates to a
    persistent Home-band banner"). Degrades to 0 on a pre-migration DB (the
    caller's own try/except already covers it; this helper just isolates
    the redteam import so a missing package can't break the whole band)."""
    from redteam.gate import escalated_items

    return len(escalated_items(db_path=db_path, conn=conn))


def _escalation_banner(
    db_path: Path | str | None, *, conn: sqlite3.Connection | None = None
) -> str:
    """The persistent 'Red Team: N items escalated' banner — a k-well -bad
    block (never the quiet ritual-debt line style; this is a forced-response
    failure, not an inert queue count) rendered ABOVE the open-loops line.
    Empty string when nothing is escalated."""
    try:
        n = _red_team_escalated_count(db_path, conn=conn)
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
    db_path: Path | str | None = None,
    *,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Render the Red Team escalation banner and non-empty owner queues.

    An empty queue set renders an explicit clear state. Missing tables or
    unavailable optional queues do not break the home screen.
    """
    banner = _escalation_banner(db_path, conn=conn)
    lines: list[str] = []

    try:
        n, age = _pending_draft_confirmation_debt(db_path, conn=conn)
        if n:
            lines.append(_line(_MOBILE_INBOX_HREF, "Pending confirmations", n, age))
    except Exception:
        pass
    try:
        n = _undispositioned_card_debt(db_path, conn=conn)
        if n:
            lines.append(_line(_MOBILE_INBOX_HREF, "Cards awaiting disposition", n))
    except Exception:
        pass
    try:
        n = reconcile_count(db_path, conn=conn)
        if n:
            lines.append(_line(_LEDGER_HASH, "Reconcile", n))
    except Exception:
        pass
    try:
        n = proposed_tenet_count(db_path, conn=conn)
        if n:
            lines.append(_line(_LEDGER_HASH, "Tenets proposed", n))
    except Exception:
        pass
    try:
        n = pending_proposal_count(db_path, conn=conn)
        if n:
            lines.append(_line(_LEDGER_HASH, "Research proposals", n))
    except Exception:
        pass

    if not lines:
        return (
            ANALYSIS_STYLE + banner + '<div class="cc-open-loops k-well">'
            '<span class="cc-ol-clear">Ritual clear - nothing waiting on you.</span></div>'
        )
    return (
        ANALYSIS_STYLE
        + banner
        + '<div class="cc-open-loops k-well"><span class="cc-ol-head">Open loops</span>'
        + "".join(lines)
        + "</div>"
    )
