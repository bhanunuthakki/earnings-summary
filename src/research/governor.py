"""The nag governor — governed coach initiation (W2, 2026-07-02 grill decision #1).

The grill rescinded master_build's "pull-only / never initiates": the coach MAY
speak first, for exactly three deterministic moment classes, under a governor
that operationalizes the owner's own kill bar ("kill it if it reads as
nagging"):

- ``falsifier_breach`` — a ``decision_condition`` alert fired on a
  ``decided_by='owner'`` decision: *your own falsifier tripped*. The single
  highest-value initiation the deep dive found (NU's 15-90d NPL >5% 2Q clause
  sits at the line).
- ``retro_annotation`` — an owner decision stub (pledge or retro-net) has sat
  >24h without its conviction/falsifier: the ledger is accruing unusable
  history until answered.
- ``intent_followup`` — a standing ``kind='intent'`` note is still open after
  a fortnight: "still live?" (re-asked at most once per fortnight via a
  bucketed key).

Governor rules, all deterministic, ZERO LLM:

- **freshness gate** (the owner's staleness callout): a ping fires only after
  the underlying item is verified still-open — falsifier still present and
  ratified, ticker still held, stub still unannotated, intent still open.
  Can't verify → don't ping (recorded ``skipped_stale``).
- **frequency caps**: ≤ ``DAILY_CAP`` sends/day, ≤ ``WEEKLY_CAP``/week across
  all classes; overflow lands ``digest`` (surfaced quietly, never pushed).
- **auto-mute**: ``MUTE_AFTER`` consecutive dismissals of a class mutes it
  until explicitly cleared — dismissals train the coach; it never argues.
- one row per moment forever (UNIQUE class+key) — a moment is never re-pushed.

The Telegram send is injected (``send_fn``) so the core is pure; the dismiss
button routes back through ``research_notify`` (``cp:dismiss:<id>``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from user_state._db import now_naive_utc, open_conn

CLASSES: tuple[str, ...] = ("falsifier_breach", "retro_annotation", "intent_followup")
DAILY_CAP = 1
WEEKLY_CAP = 3
MUTE_AFTER = 3
ANNOTATION_AGE_HOURS = 24
INTENT_FOLLOWUP_DAYS = 14


@dataclass(frozen=True, slots=True)
class Moment:
    class_: str
    key: str
    ticker: str | None
    body: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class PingRow:
    """A ``coach_pings`` row, as read back for the ``cp:review`` callback (the
    Answer button on a falsifier_breach ping) — just enough to route the
    reply."""

    id: int
    class_: str
    ticker: str | None
    status: str


def _now(now: datetime | None) -> datetime:
    return (now or now_naive_utc()).replace(tzinfo=None)


def collect_moments(
    db_path: Path | str | None = None, *, now: datetime | None = None
) -> list[Moment]:
    """Deterministic scan for initiation-worthy moments. Read-only."""
    stamp = _now(now)
    moments: list[Moment] = []
    conn = open_conn(db_path)
    try:
        # falsifier_breach — an owner decision's own condition fired an alert
        try:
            rows = conn.execute(
                """
                SELECT a.id AS alert_id, d.id AS decision_id, d.ticker, d.falsifier
                FROM alerts a
                JOIN decisions d
                  ON d.id = CAST(json_extract(a.evidence_json, '$.decision_id') AS INTEGER)
                WHERE a.trigger_kind = 'decision_condition'
                  AND a.dismissed_at IS NULL
                  AND d.decided_by = 'owner'
                ORDER BY a.id
                """
            ).fetchall()
            for r in rows:
                falsifier = str(r[3] or "")[:200]
                moments.append(
                    Moment(
                        class_="falsifier_breach",
                        key=f"alert:{int(r[0])}",
                        ticker=str(r[2] or "") or None,
                        body=(
                            f'Your falsifier on {r[2]} tripped. You wrote: "{falsifier}". '
                            f"Keep the verdict with the framework, not the feeling — "
                            f"what's the call? (/review {r[2]})"
                        ),
                        source_ref=f"decision:{int(r[1])}",
                    )
                )
        except Exception:
            pass  # pre-0063/0086 schema — no alerts to scan

        # retro_annotation — pledge / retro-net stubs still missing their words
        cutoff = (stamp - timedelta(hours=ANNOTATION_AGE_HOURS)).isoformat()
        rows = conn.execute(
            """
            SELECT id, ticker, recommendation_kind, size_usd FROM decisions
            WHERE decided_by = 'owner'
              AND (conviction IS NULL OR falsifier IS NULL)
              AND (user_notes LIKE '%pledge:note:%' OR user_notes LIKE '%retro-net:%')
              AND created_at <= ?
            ORDER BY id
            """,
            (cutoff,),
        ).fetchall()
        for r in rows:
            size = f" (~${float(r[3]):,.0f})" if r[3] else ""
            moments.append(
                Moment(
                    class_="retro_annotation",
                    key=f"annot:{int(r[0])}",
                    ticker=str(r[1] or "") or None,
                    body=(
                        f"Decision #{int(r[0])} — {str(r[2]).upper()} {r[1]}{size} — is "
                        "still missing your conviction + falsifier. One line back "
                        "completes the record (it's write-once after that)."
                    ),
                    source_ref=f"decision:{int(r[0])}",
                )
            )

        # intent_followup — standing intents still open after a fortnight
        bucket = int(stamp.timestamp()) // (INTENT_FOLLOWUP_DAYS * 86400)
        opened_before = (stamp - timedelta(days=INTENT_FOLLOWUP_DAYS)).isoformat()
        rows = conn.execute(
            """
            SELECT id, ticker, body FROM analyst_notes
            WHERE kind = 'intent' AND status = 'open' AND created_at <= ?
            ORDER BY id
            """,
            (opened_before,),
        ).fetchall()
        for r in rows:
            moments.append(
                Moment(
                    class_="intent_followup",
                    key=f"intent:{int(r[0])}:{bucket}",
                    ticker=str(r[1] or "") or None,
                    body=(
                        f'Standing intent still open: "{str(r[2])[:140]}" — still live? '
                        "Reply here with a close reason, or resolve it from the Ledger tab."
                    ),
                    source_ref=f"note:{int(r[0])}",
                )
            )
    finally:
        conn.close()
    return moments


def freshness_ok(moment: Moment, db_path: Path | str | None = None) -> bool:
    """The staleness callout as a gate: verify the moment's subject is still
    live RIGHT NOW. Can't verify → False (don't ping)."""
    conn = open_conn(db_path)
    try:
        _kind, _, ref_id = moment.source_ref.partition(":")
        if moment.class_ == "falsifier_breach":
            row = conn.execute(
                """
                SELECT d.falsifier FROM decisions d
                WHERE d.id = ? AND d.decided_by = 'owner' AND d.falsifier IS NOT NULL
                  AND d.falsifier NOT LIKE '%(inferred)%'
                  AND EXISTS (SELECT 1 FROM tracked_companies t
                              WHERE t.ticker = d.ticker AND t.list_type = 'portfolio')
                """,
                (int(ref_id),),
            ).fetchone()
            return row is not None
        if moment.class_ == "retro_annotation":
            row = conn.execute(
                "SELECT 1 FROM decisions WHERE id = ? AND decided_by = 'owner' "
                "AND (conviction IS NULL OR falsifier IS NULL)",
                (int(ref_id),),
            ).fetchone()
            return row is not None
        if moment.class_ == "intent_followup":
            row = conn.execute(
                "SELECT 1 FROM analyst_notes WHERE id = ? AND kind = 'intent' AND status = 'open'",
                (int(ref_id),),
            ).fetchone()
            return row is not None
        return False
    except Exception:
        return False
    finally:
        conn.close()


def _sent_counts(conn: sqlite3.Connection, stamp: datetime) -> tuple[int, int]:
    day_cut = (stamp - timedelta(days=1)).isoformat()
    week_cut = (stamp - timedelta(days=7)).isoformat()
    day = conn.execute(
        "SELECT COUNT(*) FROM coach_pings WHERE status = 'sent' AND created_at >= ?",
        (day_cut,),
    ).fetchone()[0]
    week = conn.execute(
        "SELECT COUNT(*) FROM coach_pings WHERE status = 'sent' AND created_at >= ?",
        (week_cut,),
    ).fetchone()[0]
    return int(day), int(week)


def run_governor(
    db_path: Path | str | None = None,
    *,
    send_fn: Callable[[int, Moment], bool] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """One governed pass: collect → freshness-gate → cap → send/digest.

    ``send_fn(ping_id, moment) -> bool`` performs the push (Telegram); False
    (or absence) downgrades the ping to digest. Deterministic, zero LLM."""
    stamp = _now(now)
    tally = {"sent": 0, "digest": 0, "skipped_muted": 0, "skipped_stale": 0, "seen": 0}
    moments = collect_moments(db_path, now=stamp)
    conn = open_conn(db_path)
    try:
        muted = {str(r[0]) for r in conn.execute("SELECT class_ FROM coach_mutes").fetchall()}
        for moment in moments:
            exists = conn.execute(
                "SELECT 1 FROM coach_pings WHERE class_ = ? AND key = ?",
                (moment.class_, moment.key),
            ).fetchone()
            if exists:
                continue  # a moment is considered exactly once
            tally["seen"] += 1
            iso = stamp.isoformat()
            if moment.class_ in muted:
                status = "skipped_muted"
            elif not freshness_ok(moment, db_path):
                status = "skipped_stale"
            else:
                day, week = _sent_counts(conn, stamp)
                status = "digest" if (day >= DAILY_CAP or week >= WEEKLY_CAP) else "sent"
            cur = conn.execute(
                """
                INSERT INTO coach_pings
                    (class_, key, ticker, body, status, source_ref, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    moment.class_,
                    moment.key,
                    moment.ticker,
                    moment.body,
                    status,
                    moment.source_ref,
                    iso,
                    iso,
                ),
            )
            conn.commit()
            if status == "sent":
                ping_id = int(cur.lastrowid or 0)
                delivered = bool(send_fn(ping_id, moment)) if send_fn else False
                if not delivered:
                    conn.execute(
                        "UPDATE coach_pings SET status = 'digest', updated_at = ? WHERE id = ?",
                        (iso, ping_id),
                    )
                    conn.commit()
                    status = "digest"
            tally[status] += 1
    finally:
        conn.close()
    return tally


def record_dismissal(ping_id: int, *, db_path: Path | str | None = None) -> tuple[bool, str | None]:
    """Owner dismissed a ping. Returns (recorded, muted_class|None): MUTE_AFTER
    consecutive dismissals of a class silence it until cleared."""
    stamp = now_naive_utc().isoformat()
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT class_ FROM coach_pings WHERE id = ? AND status IN ('sent','digest')",
            (ping_id,),
        ).fetchone()
        if row is None:
            return False, None
        class_ = str(row[0])
        conn.execute(
            "UPDATE coach_pings SET status = 'dismissed', updated_at = ? WHERE id = ?",
            (stamp, ping_id),
        )
        recent = conn.execute(
            """
            SELECT status FROM coach_pings
            WHERE class_ = ? AND status IN ('dismissed', 'acted')
            ORDER BY updated_at DESC, id DESC LIMIT ?
            """,
            (class_, MUTE_AFTER),
        ).fetchall()
        muted: str | None = None
        if len(recent) >= MUTE_AFTER and all(str(r[0]) == "dismissed" for r in recent):
            conn.execute(
                "INSERT OR IGNORE INTO coach_mutes (class_, muted_at, reason) VALUES (?, ?, ?)",
                (class_, stamp, f"{MUTE_AFTER} consecutive dismissals"),
            )
            muted = class_
        conn.commit()
        return True, muted
    finally:
        conn.close()


def unmute(class_: str, *, db_path: Path | str | None = None) -> bool:
    conn = open_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM coach_mutes WHERE class_ = ?", (class_,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_ping(ping_id: int, db_path: Path | str | None = None) -> PingRow | None:
    """One ``coach_pings`` row by id, or None. Backs the ``cp:review`` callback
    (the Answer button on a falsifier_breach ping) — it needs the ticker to
    route the reply, without pulling in the full ``Moment`` collection path."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, class_, ticker, status FROM coach_pings WHERE id = ?",
            (ping_id,),
        ).fetchone()
        if row is None:
            return None
        return PingRow(id=int(row[0]), class_=str(row[1]), ticker=row[2], status=str(row[3]))
    finally:
        conn.close()


def digest_pings(
    db_path: Path | str | None = None, *, limit: int = 10
) -> list[tuple[int, str, str]]:
    """(id, class, body) rows waiting in the quiet digest — the overflow lane."""
    conn = open_conn(db_path)
    try:
        return [
            (int(r[0]), str(r[1]), str(r[2]))
            for r in conn.execute(
                "SELECT id, class_, body FROM coach_pings WHERE status = 'digest' "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        ]
    finally:
        conn.close()
