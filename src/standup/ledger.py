"""standup_messages CRUD — dedup, rate-limit reads, and the delivery record.

Thin SQLite over the 0106 table. Every function takes an open connection (the
run owns one read/write connection for the whole pass) and writes commit
immediately so the short-lived ``ask.store`` connections that persist the thread
between records never contend on a held write lock.

The three rate-limit reads back the orchestrator's per-signal gate:

  * ``recently_composed_signatures`` — the dedup set: a trip already composed
    (delivered OR eval-suppressed) inside the dedup window must not re-compose
    and re-pay the LLM. ``compose_failed`` rows are excluded so a transient
    composition failure is retried, not dedup-suppressed.
  * ``delivered_today_count`` — the per-day cap denominator.
  * ``last_delivered_at_for_ticker`` — the per-name cooldown.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from alerts.store import compute_signature_sha
from identity import DEFAULT_USER_ID
from standup.signals import StandupSignal

# Compose-attempt outcomes recorded on a row.
STATUS_DELIVERED = "delivered"
STATUS_SUPPRESSED_EVAL = "suppressed_eval"
STATUS_COMPOSE_FAILED = "compose_failed"

# Statuses that count as "we already looked at this trip" for dedup. A
# compose_failed is a transient miss, retried rather than suppressed.
_DEDUP_STATUSES = (STATUS_DELIVERED, STATUS_SUPPRESSED_EVAL)


def signature_sha(signal: StandupSignal) -> str:
    """The dedup hash for a signal — ``kind|scope|{"key": <trip-key>}`` through
    the shared ``alerts.store`` hasher so the convention matches the alert feed."""
    return compute_signature_sha(signal.kind, signal.scope_key, {"key": signal.signature})


def _now_iso(now: datetime) -> str:
    return now.replace(tzinfo=None).isoformat()


def recently_composed_signatures(
    conn: sqlite3.Connection,
    *,
    user_id: str = DEFAULT_USER_ID,
    now: datetime,
    within_days: int,
) -> set[str]:
    """Signatures composed (delivered or eval-suppressed) since the dedup
    cutoff — the orchestrator skips composing any of these again."""
    cutoff = _now_iso(now - timedelta(days=within_days))
    placeholders = ",".join("?" * len(_DEDUP_STATUSES))
    try:
        rows = conn.execute(
            f"SELECT DISTINCT signature_sha FROM standup_messages "
            f"WHERE user_id = ? AND created_at >= ? AND status IN ({placeholders})",
            (user_id, cutoff, *_DEDUP_STATUSES),
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(r[0]) for r in rows}


def delivered_today_count(
    conn: sqlite3.Connection, *, user_id: str = DEFAULT_USER_ID, now: datetime
) -> int:
    """Delivered standups already recorded for the current UTC day."""
    day = now.date().isoformat()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM standup_messages "
            "WHERE user_id = ? AND status = ? AND substr(created_at, 1, 10) = ?",
            (user_id, STATUS_DELIVERED, day),
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def last_delivered_at_for_ticker(
    conn: sqlite3.Connection, *, user_id: str = DEFAULT_USER_ID, ticker: str
) -> str | None:
    """The created_at of the most recent DELIVERED standup for a ticker (the
    per-name cooldown anchor), or None if it has never had one."""
    try:
        row = conn.execute(
            "SELECT created_at FROM standup_messages "
            "WHERE user_id = ? AND UPPER(ticker) = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, ticker.upper(), STATUS_DELIVERED),
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None


def within_cooldown(
    conn: sqlite3.Connection,
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None,
    now: datetime,
    cooldown_days: int,
) -> bool:
    """True when this ticker had a delivered standup inside the cooldown window.
    Book-level (ticker-less) signals are never cooldown-suppressed by name; a
    zero/negative window disables the cooldown entirely."""
    if not ticker or cooldown_days <= 0:
        return False
    last = last_delivered_at_for_ticker(conn, user_id=user_id, ticker=ticker)
    if last is None:
        return False
    age_cutoff = _now_iso(now - timedelta(days=cooldown_days))
    return last >= age_cutoff


def record(
    conn: sqlite3.Connection,
    *,
    user_id: str = DEFAULT_USER_ID,
    signal: StandupSignal,
    status: str,
    now: datetime,
    score: float | None = None,
    session_id: str | None = None,
    turn_id: int | None = None,
    conclusion: str | None = None,
) -> int:
    """Insert one ledger row for a compose attempt and return its id."""
    cur = conn.execute(
        "INSERT INTO standup_messages("
        "  user_id, ticker, signal_kind, signature_sha, status, score, "
        "  session_id, turn_id, conclusion, headline, evidence_json, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            signal.ticker,
            signal.kind,
            signature_sha(signal),
            status,
            score,
            session_id,
            turn_id,
            conclusion,
            signal.headline,
            json.dumps(signal.evidence, default=str, ensure_ascii=False),
            _now_iso(now),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


__all__ = [
    "STATUS_COMPOSE_FAILED",
    "STATUS_DELIVERED",
    "STATUS_SUPPRESSED_EVAL",
    "delivered_today_count",
    "last_delivered_at_for_ticker",
    "recently_composed_signatures",
    "record",
    "signature_sha",
    "within_cooldown",
]
