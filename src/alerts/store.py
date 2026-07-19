"""Read/write API for ``alerts`` (0063) + ``queued_actions`` (0064).

Tight pair: every fired alert in ``alerts`` can fan out into one or more
draft actions in ``queued_actions`` (hard FK). The trigger framework (PR-N2
stub, PR-N8+ real implementations) calls ``fire_alert`` and ``queue_action``
once it has produced evidence and drafted payloads; the dashboard (PR-N4)
calls the list/transition functions to render the feed and act on it.

State machines:
    alerts.status         : pending → {approved, dismissed, expired}
    queued_actions.status : pending → {applied, cancelled}

Transitions are explicit: ``approve_alert`` / ``dismiss_alert`` /
``apply_action`` / ``cancel_action`` each raise rather than silently no-op
when the row is already in a non-pending state — the caller has a stale
view of the world and the safer default is to surface the conflict.

``compute_signature_sha`` lives here too (the dedup contract belongs with
the alerts table, even though sensors are the actual callers). The hash
is deterministic across dict ordering — see the test file for guarantees.

This module fails loudly on missing DB / missing tables. It is not
telemetry; primary-write semantics demand the caller hears about
substrate-not-installed errors rather than dropping the writes silently.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from clock import now_iso
from db_paths import resolve_db_path
from identity import DEFAULT_USER_ID

ALERT_STATUS_PENDING = "pending"
ALERT_STATUS_APPROVED = "approved"
ALERT_STATUS_DISMISSED = "dismissed"
ALERT_STATUS_EXPIRED = "expired"

ACTION_STATUS_PENDING = "pending"
ACTION_STATUS_APPLIED = "applied"
ACTION_STATUS_CANCELLED = "cancelled"

# Canonical enum vocabularies — the single source of truth the DB CHECK
# constraints (alembic 0068) mirror. Validated at the write path below so a bad
# value fails with a clear ValueError before the INSERT, and is rejected even on
# a DB that predates the 0068 constraints.
ALERT_STATUSES: frozenset[str] = frozenset(
    {ALERT_STATUS_PENDING, ALERT_STATUS_APPROVED, ALERT_STATUS_DISMISSED, ALERT_STATUS_EXPIRED}
)
ACTION_STATUSES: frozenset[str] = frozenset(
    {ACTION_STATUS_PENDING, ACTION_STATUS_APPLIED, ACTION_STATUS_CANCELLED}
)
# The dashboard's trigger-kind vocabulary (src/dashboard/evidence_drawer.py):
# the five live sensor `kind` classvars plus 'thesis_drift', which was folded
# into the kpi_inflection sensor rather than shipped on its own but is still a
# recognized alert kind the feed/drawer render. 'decision_condition' is the
# falsifiable-conditions evaluator (0086); 'restatement' fires when a held-name
# fact is materially superseded (L10, 0108). 'owner_capacity_breach' (tenet-2
# Phase 3, 0171) is the governor's capacity_breach moment class writing its own
# alert row — a human-capital cap or cash-floor policy crossed — so the SAME
# tier-1 severity/ranking path (decisive_alert_reason) covers an owner-policy
# breach exactly like an owner-falsifier breach. 'data_feed_stale' (0183) is
# the dead-man switch for silently-dead data feeds (news frozen for weeks,
# macro series 429ing daily with "populated": 0) — a feed job that produced
# nothing fires ONE book-level row (sentinel ticker 'PORTFOLIO') so the outage
# reaches the alert feed instead of a cron log. Keep in lockstep with the
# ck_alerts_trigger_kind CHECK (0068, widened in 0086 + 0108 + 0171 + 0183).
TRIGGER_KINDS: frozenset[str] = frozenset(
    {
        "kpi_inflection",
        "earnings_tone",
        "saydo_due",
        "thesis_drift",
        "material_news",
        "decision_condition",
        "restatement",
        "owner_capacity_breach",
        "data_feed_stale",
    }
)
# Mirrors the QueuedActionDraft.action_kind vocabulary (src/triggers/base.py).
ACTION_KINDS: frozenset[str] = frozenset(
    {"thesis_update", "bear_append", "sizing_update", "earnings_prep_append"}
)


@dataclass(slots=True)
class AlertRow:
    """One row of the ``alerts`` table — every column round-tripped to a
    typed Python value. ``evidence_json`` stays as the serialized string the
    sensor wrote; the per-trigger consumer decodes it (shape is owned per
    ``trigger_kind``)."""

    id: int
    # Canonical tenant id — TEXT, FK to tenants.id (alembic 0073). Same str
    # space as Company.user_id after the identity reconciliation.
    user_id: str
    ticker: str
    trigger_kind: str
    fired_at: datetime
    status: str
    memo_artifact_id: int | None
    evidence_json: str
    signature_sha: str
    dismissed_at: datetime | None
    approved_at: datetime | None
    # Optional, defaulted (0142): existing keyword-arg call sites (several
    # test fixtures build AlertRow directly) stay byte-compatible.
    dismiss_reason: str | None = None


@dataclass(slots=True)
class QueuedActionRow:
    """One row of the ``queued_actions`` table. ``payload`` is the decoded
    JSON payload (typically a dict; the schema is owned per ``action_kind``).
    The original storage column ``payload_json`` is not re-exposed because
    the dataclass already carries the decoded form."""

    id: int
    alert_id: int
    action_kind: str
    payload: dict[str, object]
    status: str
    created_at: datetime
    applied_at: datetime | None
    cancelled_at: datetime | None


# ----------------------------------------------------------------------------
# Signature hash
# ----------------------------------------------------------------------------


def compute_signature_sha(
    trigger_kind: str,
    ticker: str,
    key_evidence: Mapping[str, object],
) -> str:
    """Stable content hash used by sensors to dedup re-fires.

    Deterministic on ``key_evidence`` insertion order: ``json.dumps`` uses
    ``sort_keys=True`` and ``default=str`` so that datetimes / Decimals /
    custom objects produce a consistent representation.

    Convention: the sensor's ``key_evidence`` contains only the fields that
    uniquely identify *this firing event* (e.g. ``{"kpi": "...", "period":
    "..."}``); transient context (raw_value, computed deltas) should NOT
    enter the hash or the same KPI breach refiring with a slightly-different
    raw value would dodge dedup.
    """
    payload = f"{trigger_kind}|{ticker}|" + json.dumps(
        dict(key_evidence), sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# Alerts
# ----------------------------------------------------------------------------


def fire_alert(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str,
    trigger_kind: str,
    fired_at: datetime,
    evidence_json: str,
    signature_sha: str,
    memo_artifact_id: int | None = None,
    db_path: Path | str | None = None,
) -> AlertRow:
    """Insert one alert row. ``status`` lands as 'pending' (the column default).

    Does NOT perform dedup itself — sensors call ``find_by_signature`` first
    and skip the insert when a non-expired prior alert with the same
    signature already exists. Keeping dedup in the sensor lets the sensor
    decide whether a slightly-different evidence shape merits a fresh fire.
    """
    if trigger_kind not in TRIGGER_KINDS:
        raise ValueError(
            f"unknown trigger_kind {trigger_kind!r}; expected one of {sorted(TRIGGER_KINDS)}"
        )
    conn = _open(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO alerts(
                user_id, ticker, trigger_kind, fired_at, status,
                memo_artifact_id, evidence_json, signature_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                ticker,
                trigger_kind,
                fired_at.isoformat(),
                ALERT_STATUS_PENDING,
                memo_artifact_id,
                evidence_json,
                signature_sha,
            ),
        )
        row_id = int(cur.lastrowid or 0)
        conn.commit()
        return _fetch_alert(conn, row_id)
    finally:
        conn.close()


def find_by_signature(
    *,
    user_id: str = DEFAULT_USER_ID,
    signature_sha: str,
    db_path: Path | str | None = None,
) -> AlertRow | None:
    """Most recent non-expired alert with ``signature_sha``, or None.

    Used by sensors for re-fire dedup: if a prior alert with this signature
    is still pending/approved/dismissed (anything but 'expired'), the
    sensor suppresses the duplicate. Once the prior alert expires, the
    next fire can re-create.
    """
    conn = _open(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM alerts
            WHERE user_id = ? AND signature_sha = ? AND status != ?
            ORDER BY fired_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, signature_sha, ALERT_STATUS_EXPIRED),
        ).fetchone()
        return None if row is None else _row_to_alert(row)
    finally:
        conn.close()


def list_pending_alerts(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    since: datetime | None = None,
    db_path: Path | str | None = None,
) -> list[AlertRow]:
    """Pending alerts (newest first), optionally scoped to a ticker / time window."""
    return list_alerts(
        user_id=user_id,
        ticker=ticker,
        status=ALERT_STATUS_PENDING,
        since=since,
        db_path=db_path,
    )


def list_alerts(
    *,
    user_id: str = DEFAULT_USER_ID,
    ticker: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    limit: int | None = 200,
    db_path: Path | str | None = None,
) -> list[AlertRow]:
    """General-purpose alerts list — newest first. Drives the dashboard feed.

    Filters compose with AND. ``status=None`` returns rows in any state;
    ``ticker=None`` returns across the user's whole portfolio. ``limit=None``
    returns every match — the inbox uses it to fetch the PENDING queue whole,
    because a recency ``LIMIT`` there silently hid old pending alerts behind
    newer dismissed ones (red-team wave A).
    """
    where: list[str] = ["user_id = ?"]
    params: list[object] = [user_id]
    if ticker is not None:
        where.append("ticker = ?")
        params.append(ticker)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if since is not None:
        where.append("fired_at >= ?")
        params.append(since.isoformat())
    where_sql = " AND ".join(where)
    # SQLite treats a negative LIMIT as "no limit" — the documented idiom.
    params.append(-1 if limit is None else int(limit))

    conn = _open(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM alerts
            WHERE {where_sql}
            ORDER BY fired_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_row_to_alert(r) for r in rows]
    finally:
        conn.close()


def approve_alert(alert_id: int, db_path: Path | str | None = None) -> AlertRow:
    """Transition pending → approved. Stamps ``approved_at`` to now().

    Raises ``LookupError`` if no alert with ``alert_id`` exists, or
    ``ValueError`` if the alert is in any state other than 'pending' (the
    caller's view is stale and the safer default is to surface the
    conflict, not silently re-stamp).
    """
    return _transition_alert(
        alert_id=alert_id,
        target_status=ALERT_STATUS_APPROVED,
        stamp_column="approved_at",
        db_path=db_path,
    )


def dismiss_alert(
    alert_id: int, db_path: Path | str | None = None, *, reason: str | None = None
) -> AlertRow:
    """Transition pending → dismissed. Stamps ``dismissed_at`` to now().

    ``reason`` (0142) is the owner's own words on why the alert stopped
    mattering — signal capture, not a required field: the dismiss itself
    never blocks on it, so a blank/None reason just leaves the column NULL.

    See ``approve_alert`` for the exception semantics — symmetric.
    """
    return _transition_alert(
        alert_id=alert_id,
        target_status=ALERT_STATUS_DISMISSED,
        stamp_column="dismissed_at",
        db_path=db_path,
        reason=reason,
    )


def _transition_alert(
    *,
    alert_id: int,
    target_status: str,
    stamp_column: str,
    db_path: Path | str | None,
    reason: str | None = None,
) -> AlertRow:
    """Shared pending → terminal-status transition.

    ``stamp_column`` is one of ``approved_at`` / ``dismissed_at`` — hard-coded
    to the matching column for the target status (no SQL injection surface;
    callers come from this module only). ``reason`` only ever applies to a
    dismiss (approve carries no reason field); ignored otherwise.
    """
    if stamp_column not in {"approved_at", "dismissed_at"}:
        raise ValueError(f"unsupported stamp column: {stamp_column!r}")
    conn = _open(db_path)
    try:
        now = _now_iso()
        reason_clean = (reason or "").strip() or None
        if stamp_column == "dismissed_at" and reason_clean is not None:
            cur = conn.execute(
                f"""
                UPDATE alerts
                SET status = ?, {stamp_column} = ?, dismiss_reason = ?
                WHERE id = ? AND status = ?
                """,
                (target_status, now, reason_clean, alert_id, ALERT_STATUS_PENDING),
            )
        else:
            cur = conn.execute(
                f"""
                UPDATE alerts
                SET status = ?, {stamp_column} = ?
                WHERE id = ? AND status = ?
                """,
                (target_status, now, alert_id, ALERT_STATUS_PENDING),
            )
        if cur.rowcount == 0:
            _raise_transition_conflict(conn, "alerts", alert_id, ALERT_STATUS_PENDING)
        conn.commit()
        return _fetch_alert(conn, alert_id)
    finally:
        conn.close()


def set_alert_dismiss_reason(
    alert_id: int, reason: str, db_path: Path | str | None = None
) -> AlertRow:
    """Attach (or replace) the dismiss reason on an ALREADY-dismissed alert.

    The dashboard's "why?" affordance renders only after the dismiss chip has
    already swapped in (0142's deferred-signal-capture UX), so this is a
    separate write from ``dismiss_alert`` itself — the dismiss never blocks on
    a reason, and this call must not re-run (or fail on) the pending→dismissed
    transition. Raises ``LookupError`` if the alert doesn't exist, or
    ``ValueError`` if it was never dismissed (a reason on a pending/approved
    alert has no meaning)."""
    conn = _open(db_path)
    try:
        row = conn.execute("SELECT status FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if row is None:
            raise LookupError(f"alerts id={alert_id} not found")
        if str(row["status"]) != ALERT_STATUS_DISMISSED:
            raise ValueError(
                f"alerts id={alert_id} status is {row['status']!r}, "
                f"expected {ALERT_STATUS_DISMISSED!r} to attach a dismiss reason"
            )
        conn.execute(
            "UPDATE alerts SET dismiss_reason = ? WHERE id = ?",
            ((reason or "").strip() or None, alert_id),
        )
        conn.commit()
        return _fetch_alert(conn, alert_id)
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Queued actions
# ----------------------------------------------------------------------------


def queue_action(
    *,
    alert_id: int,
    action_kind: str,
    payload: Mapping[str, object],
    db_path: Path | str | None = None,
) -> QueuedActionRow:
    """Insert one queued action linked to ``alert_id``.

    The FK ``queued_actions.alert_id → alerts.id`` is enforced (the
    ``_open`` helper turns on ``PRAGMA foreign_keys = ON``), so a nonexistent
    ``alert_id`` raises sqlite3.IntegrityError. ``payload`` is serialized
    to JSON for storage and decoded back into ``QueuedActionRow.payload``
    on the return.
    """
    if action_kind not in ACTION_KINDS:
        raise ValueError(
            f"unknown action_kind {action_kind!r}; expected one of {sorted(ACTION_KINDS)}"
        )
    payload_json = json.dumps(dict(payload), default=str)
    conn = _open(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO queued_actions(
                alert_id, action_kind, payload_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                action_kind,
                payload_json,
                ACTION_STATUS_PENDING,
                _now_iso(),
            ),
        )
        row_id = int(cur.lastrowid or 0)
        conn.commit()
        return _fetch_action(conn, row_id)
    finally:
        conn.close()


def list_queued_actions_for_alert(
    alert_id: int, db_path: Path | str | None = None
) -> list[QueuedActionRow]:
    """All queued actions for one alert, oldest first (the natural draft order)."""
    conn = _open(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM queued_actions
            WHERE alert_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (alert_id,),
        ).fetchall()
        return [_row_to_action(r) for r in rows]
    finally:
        conn.close()


def list_queued_actions_for_alerts(
    alert_ids: list[int], db_path: Path | str | None = None
) -> dict[int, list[QueuedActionRow]]:
    """Queued actions for many alerts in ONE ``IN`` query — the batched form of
    ``list_queued_actions_for_alert`` for callers rendering a stream of alerts
    (the unified inbox builds one row per alert and previously opened a fresh
    connection + ran one query PER alert: an N+1 on the GET / boot path).

    Returns ``{alert_id: [actions oldest-first]}`` with an entry for EVERY
    requested id (empty list when an alert has no drafts), so callers can index
    without a ``.get`` guard. Empty input short-circuits to ``{}`` (no
    connection opened)."""
    if not alert_ids:
        return {}
    out: dict[int, list[QueuedActionRow]] = {aid: [] for aid in alert_ids}
    marks = ",".join("?" for _ in alert_ids)
    conn = _open(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM queued_actions
            WHERE alert_id IN ({marks})
            ORDER BY alert_id ASC, created_at ASC, id ASC
            """,
            [int(a) for a in alert_ids],
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        action = _row_to_action(r)
        out[action.alert_id].append(action)
    return out


def list_pending_actions(
    *,
    user_id: str = DEFAULT_USER_ID,
    limit: int = 200,
    db_path: Path | str | None = None,
) -> list[QueuedActionRow]:
    """Pending queued actions across the user's alerts, newest first.

    Joins ``queued_actions`` → ``alerts`` to filter by ``user_id`` (the
    queued_actions table itself doesn't carry user_id; ownership flows
    through the parent alert).
    """
    conn = _open(db_path)
    try:
        rows = conn.execute(
            """
            SELECT qa.*
            FROM queued_actions qa
            JOIN alerts a ON a.id = qa.alert_id
            WHERE qa.status = ? AND a.user_id = ?
            ORDER BY qa.created_at DESC, qa.id DESC
            LIMIT ?
            """,
            (ACTION_STATUS_PENDING, user_id, int(limit)),
        ).fetchall()
        return [_row_to_action(r) for r in rows]
    finally:
        conn.close()


def get_action(action_id: int, db_path: Path | str | None = None) -> QueuedActionRow:
    """Fetch one queued_action row by primary key.

    Raises ``LookupError`` if no row exists. The dashboard's approve CLI
    needs this before mutating: it has to read ``action_kind`` and
    ``payload`` to dispatch the downstream user-state write (ledger vs
    sizing-intent) BEFORE flipping the action to 'applied'.
    """
    conn = _open(db_path)
    try:
        return _fetch_action(conn, action_id)
    finally:
        conn.close()


def get_alert(alert_id: int, db_path: Path | str | None = None) -> AlertRow:
    """Fetch one alert row by primary key. Raises ``LookupError`` if missing."""
    conn = _open(db_path)
    try:
        return _fetch_alert(conn, alert_id)
    finally:
        conn.close()


def apply_action(action_id: int, db_path: Path | str | None = None) -> QueuedActionRow:
    """Transition pending → applied. Stamps ``applied_at`` to now().

    Raises ``LookupError`` if no action exists with ``action_id``, or
    ``ValueError`` if the action is in any state other than 'pending'.
    The applier (a future PR) is expected to run the downstream write to
    the destination table BEFORE calling this — this row stamps the
    "downstream write succeeded" fact.
    """
    return _transition_action(
        action_id=action_id,
        target_status=ACTION_STATUS_APPLIED,
        stamp_column="applied_at",
        db_path=db_path,
    )


def cancel_action(action_id: int, db_path: Path | str | None = None) -> QueuedActionRow:
    """Transition pending → cancelled. Stamps ``cancelled_at`` to now().

    See ``apply_action`` for the exception semantics — symmetric.
    """
    return _transition_action(
        action_id=action_id,
        target_status=ACTION_STATUS_CANCELLED,
        stamp_column="cancelled_at",
        db_path=db_path,
    )


def uncancel_action(action_id: int, db_path: Path | str | None = None) -> QueuedActionRow:
    """Undo a dismiss: transition cancelled → pending and clear ``cancelled_at``.
    The reverse of :func:`cancel_action`, for the inbox's optimistic-dismiss
    Undo (Wave 3b). 409 (KeyError) when the action is not currently cancelled —
    a stale or already-restored card."""
    conn = _open(db_path)
    try:
        cur = conn.execute(
            "UPDATE queued_actions SET status = ?, cancelled_at = NULL WHERE id = ? AND status = ?",
            (ACTION_STATUS_PENDING, action_id, ACTION_STATUS_CANCELLED),
        )
        if cur.rowcount == 0:
            _raise_transition_conflict(conn, "queued_actions", action_id, ACTION_STATUS_CANCELLED)
        conn.commit()
        return _fetch_action(conn, action_id)
    finally:
        conn.close()


def _transition_action(
    *,
    action_id: int,
    target_status: str,
    stamp_column: str,
    db_path: Path | str | None,
) -> QueuedActionRow:
    if stamp_column not in {"applied_at", "cancelled_at"}:
        raise ValueError(f"unsupported stamp column: {stamp_column!r}")
    conn = _open(db_path)
    try:
        now = _now_iso()
        cur = conn.execute(
            f"""
            UPDATE queued_actions
            SET status = ?, {stamp_column} = ?
            WHERE id = ? AND status = ?
            """,
            (target_status, now, action_id, ACTION_STATUS_PENDING),
        )
        if cur.rowcount == 0:
            _raise_transition_conflict(conn, "queued_actions", action_id, ACTION_STATUS_PENDING)
        conn.commit()
        return _fetch_action(conn, action_id)
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------------


def _open(db_path: Path | str | None) -> sqlite3.Connection:
    """Open a connection with FK enforcement on. Fails loudly when DB or
    table is missing — these are primary writes, not telemetry."""
    path = resolve_db_path(db_path)
    if path is None:
        raise RuntimeError(
            "DB path not configured: pass db_path explicitly or ensure db.DB_PATH "
            "is importable from src/."
        )
    if not path.exists():
        raise FileNotFoundError(f"SQLite DB does not exist: {path}")
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now_iso() -> str:
    # Naive-UTC, matching ``alerts.fired_at`` (written by triggers as
    # ``datetime.now(UTC).replace(tzinfo=None)``) and the repo-wide
    # convention. An aware offset here is the landmine that crashes any
    # consumer comparing a store stamp against a naive datetime.
    return now_iso()


def _parse_dt(raw: object) -> datetime:
    """Parse a stored timestamp into a **naive-UTC** datetime.

    The store's convention is naive-UTC throughout (see ``_now_iso`` and the
    trigger ``fired_at`` writers). Rows written before that convention was
    enforced carry an aware (``+00:00``) offset — e.g. ``queued_actions.
    created_at`` rows stamped by the pre-#222 ``_now_iso``. Those are
    converted to UTC and stripped to naive here, so every datetime the store
    hands back is uniformly naive-UTC and no consumer has to reconcile a mix
    of aware (legacy) and naive (new) rows.
    """
    dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _parse_dt_opt(raw: object) -> datetime | None:
    if raw is None or raw == "":
        return None
    return _parse_dt(raw)


def _fetch_alert(conn: sqlite3.Connection, alert_id: int) -> AlertRow:
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if row is None:
        raise LookupError(f"alerts id={alert_id} not found")
    return _row_to_alert(row)


def _fetch_action(conn: sqlite3.Connection, action_id: int) -> QueuedActionRow:
    row = conn.execute("SELECT * FROM queued_actions WHERE id = ?", (action_id,)).fetchone()
    if row is None:
        raise LookupError(f"queued_actions id={action_id} not found")
    return _row_to_action(row)


def _row_to_alert(row: sqlite3.Row) -> AlertRow:
    raw_memo = row["memo_artifact_id"]
    # dismiss_reason (0142) is read tolerantly: a hand-built pre-0142 fixture
    # table (several trigger/attribution tests DDL 'alerts' directly, never
    # through this column) must not IndexError on a plain SELECT *.
    keys = row.keys()
    reason = row["dismiss_reason"] if "dismiss_reason" in keys else None
    return AlertRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        ticker=str(row["ticker"]),
        trigger_kind=str(row["trigger_kind"]),
        fired_at=_parse_dt(row["fired_at"]),
        status=str(row["status"]),
        memo_artifact_id=(None if raw_memo is None else int(raw_memo)),
        evidence_json=str(row["evidence_json"]),
        signature_sha=str(row["signature_sha"]),
        dismissed_at=_parse_dt_opt(row["dismissed_at"]),
        approved_at=_parse_dt_opt(row["approved_at"]),
        dismiss_reason=(str(reason) if reason is not None else None),
    )


def _row_to_action(row: sqlite3.Row) -> QueuedActionRow:
    payload_raw = row["payload_json"]
    if payload_raw is None:
        payload: dict[str, object] = {}
    else:
        loaded = json.loads(str(payload_raw))
        if not isinstance(loaded, dict):
            raise ValueError(
                f"queued_actions id={int(row['id'])} payload_json is not a JSON object"
            )
        # JSON-boundary cast: isinstance(..., dict) just confirmed the runtime
        # shape; the cast tells pyright we've validated dict[str, object] and
        # can use it without further narrowing.
        payload = cast("dict[str, object]", loaded)
    return QueuedActionRow(
        id=int(row["id"]),
        alert_id=int(row["alert_id"]),
        action_kind=str(row["action_kind"]),
        payload=payload,
        status=str(row["status"]),
        created_at=_parse_dt(row["created_at"]),
        applied_at=_parse_dt_opt(row["applied_at"]),
        cancelled_at=_parse_dt_opt(row["cancelled_at"]),
    )


def _raise_transition_conflict(
    conn: sqlite3.Connection, table: str, row_id: int, required_status: str
) -> None:
    """Helper for the two transition functions: figure out *why* the UPDATE
    matched 0 rows and raise an informative exception. Either the row
    doesn't exist (LookupError) or its status is not the required one
    (ValueError)."""
    row = conn.execute(f"SELECT status FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise LookupError(f"{table} id={row_id} not found")
    raise ValueError(
        f"{table} id={row_id} status is {str(row['status'])!r}, "
        f"cannot transition (expected {required_status!r})"
    )
