"""The Home "Coach" strip (B9, 2026-07-19 program overhaul) — the brief and
Telegram must show the SAME 1-2 governed items.

The governor's pushes land on Telegram and its overflow lands in the digest
lane; before B9 the brief showed only a digest COUNT (``open_loops``'s
ritual-debt line). The owner's stated reading surface is the morning brief
(ruling #1), so the actual items — today's sent pings plus the queued digest —
render here as one compact band. Read-only over ``coach_pings``; the actions
(dismiss / review / free-text reply) stay on Telegram where the buttons and
the reply loop (``capture.coach_reply``) already live.

Composes existing kit/shell classes only (``k-well``/``k-chip``/``cc-ol-line``)
— no CSS of its own, so the lexical guard's surface census is unaffected.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from user_state._db import now_naive_utc, open_conn

# One line per item, at most this many — the strip is a glance, not a console.
_MAX_ROWS = 4

# class_ -> short human label (falls back to the raw class name).
_CLASS_LABELS: dict[str, str] = {
    "falsifier_breach": "falsifier",
    "capacity_breach": "capacity",
    "life_event_checkpoint": "life event",
    "post_mortem": "post-mortem",
    "calibration_finding": "calibration",
    "tenet_challenge": "tenet",
    "retro_annotation": "annotate",
    "intent_followup": "intent",
    "profile_drift": "profile",
}


def _rows(db_path: Path | str | None, *, now: datetime | None = None) -> list[tuple[str, str, str]]:
    """(state, class label, body) for today's sent pings + the digest queue +
    the brief-routed queue, newest first within each state, capped at
    ``_MAX_ROWS``. Sent-today first: those are the items Telegram pushed this
    morning — the strip's whole contract is showing the same thing. P2.2: a
    calibration_finding/capacity_breach/life_event_checkpoint/profile_drift
    moment never reaches 'sent' or 'digest' anymore (governor.
    BRIEF_ROUTED_CLASSES) — without surfacing 'routed_to_brief' here too,
    those four classes would silently vanish from this band the moment the
    P2.2 governor adapter landed, even though the owner used to see them."""
    stamp = now or now_naive_utc()
    day_start = stamp.date().isoformat()
    conn = open_conn(db_path)
    try:
        sent = conn.execute(
            "SELECT class_, body FROM coach_pings "
            "WHERE status = 'sent' AND created_at >= ? ORDER BY id DESC LIMIT ?",
            (day_start, _MAX_ROWS),
        ).fetchall()
        room = _MAX_ROWS - len(sent)
        digest = (
            conn.execute(
                "SELECT class_, body FROM coach_pings "
                "WHERE status = 'digest' ORDER BY id DESC LIMIT ?",
                (room,),
            ).fetchall()
            if room > 0
            else []
        )
        room -= len(digest)
        routed = (
            conn.execute(
                "SELECT class_, body FROM coach_pings "
                "WHERE status = 'routed_to_brief' ORDER BY id DESC LIMIT ?",
                (room,),
            ).fetchall()
            if room > 0
            else []
        )
    finally:
        conn.close()
    out: list[tuple[str, str, str]] = []
    for r in sent:
        out.append(("sent", _CLASS_LABELS.get(str(r[0]), str(r[0])), str(r[1])))
    for r in digest:
        out.append(("digest", _CLASS_LABELS.get(str(r[0]), str(r[0])), str(r[1])))
    for r in routed:
        out.append(("routed_to_brief", _CLASS_LABELS.get(str(r[0]), str(r[0])), str(r[1])))
    return out


def render_coach_strip(db_path: Path | str | None = None, *, now: datetime | None = None) -> str:
    """The band, or ``""`` when the coach has nothing live — an empty strip
    would just be chrome. Never raises (Home must render through any DB
    state, including pre-0131 fixtures with no coach_pings table)."""
    try:
        rows = _rows(db_path, now=now)
    except Exception:
        return ""
    if not rows:
        return ""
    lines: list[str] = []
    for state, label, body in rows:
        tone = "" if state == "sent" else " k-chip-warn"
        if state == "sent":
            state_note = ""
        elif state == "routed_to_brief":
            state_note = " (in weekly brief)"
        else:
            state_note = " (queued)"
        lines.append(
            '<div class="cc-ol-line">'
            f'<span class="k-chip{tone}">{escape(label)}</span> '
            f"{escape(body[:160])}{state_note}</div>"
        )
    return (
        '<div class="k-well cc-coach-strip">'
        '<span class="k-chip k-chip-mono">Coach</span> '
        "<!-- same items Telegram pushed; actions live there -->"
        f"{''.join(lines)}</div>"
    )
