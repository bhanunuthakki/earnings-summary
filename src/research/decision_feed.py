"""Owner-decision persistence + the tracker retro net (brokerage ground truth).

Two 2026-07-02 grill decisions land here:

- **The tracker feed is the decisions ground truth.** The seed interview proved
  recall drifts (~1 year on the MU exit) — so position changes are detected
  from the tracker's transaction feed, and the owner's words add only what the
  feed can't know (conviction + falsifier).
- **The persist/link seams.** ``capture_decision`` (#709/#718) shipped with
  ``persist_fn``/``link_fn`` defaulting to None — a core with no body.
  ``persist_owner_decision`` / ``link_decision_to_note`` are those adapters,
  writing the 0130 owner shape (conviction and falsifier are WRITE-ONCE: set
  at insert, never updated here).

``detect_unannounced_fills`` is the retro half of the pledge+retro-net entry
coaching: any tracker fill above the dollar threshold with no matching owner
decision within the window becomes an ``owner`` decision stub awaiting
annotation — the coach's follow-up prompt (W2) asks for the conviction +
falsifier the feed can't supply. Offline-tolerant: an unreachable tracker is a
tally entry, never an exception (the materialized-weights posture).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from user_state._db import now_iso, open_conn

if TYPE_CHECKING:
    from integrations.portfolio_tracker_client import LivePortfolio

# capture_decision direction vocab → the grader's recommendation kinds
_DIRECTION_TO_KIND = {
    "buy": "initiate",
    "add": "add",
    "trim": "trim",
    "sell": "sell",
    "hold": "hold",
}

# A fill direction matches any of these already-recorded owner kinds.
_FILL_MATCH_KINDS = {
    "buy": ("initiate", "add"),
    "sell": ("trim", "sell"),
}

_RETRO_MARKER = "retro-net:"


def persist_owner_decision(
    *,
    ticker: str,
    direction: str,
    size_pct: float | None = None,
    conviction: str | None = None,
    falsifier: str | None = None,
    size_usd: float | None = None,
    instrument: str | None = None,
    account: str | None = None,
    made_at: str | None = None,
    rationale: str | None = None,
    user_notes: str | None = None,
    note_id: int | None = None,  # accepted for the capture_decision contract
    db_path: Path | str | None = None,
) -> int:
    """INSERT one ``decided_by='owner'`` decision (0130 shape); returns its id.

    Conviction and falsifier are WRITE-ONCE by design — this writer only ever
    inserts; nothing in this module updates them afterwards."""
    kind = _DIRECTION_TO_KIND.get((direction or "").strip().lower())
    if kind is None:
        raise ValueError(
            f"unknown direction {direction!r}; expected one of {sorted(_DIRECTION_TO_KIND)}"
        )
    clean_ticker = (ticker or "").strip().upper()
    if not clean_ticker:
        raise ValueError("ticker required for an owner decision")
    stamp = now_iso()
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO decisions
                (ticker, recommendation_kind, conviction, decided_by, scope,
                 instrument, account, size_usd, size_pct, falsifier,
                 rationale_excerpt, user_notes, made_at, created_at)
            VALUES (?, ?, ?, 'owner', 'ticker', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_ticker,
                kind,
                (conviction or None),
                instrument,
                account,
                size_usd,
                size_pct,
                (falsifier or None),
                (rationale or "")[:512] or None,
                user_notes,
                made_at or stamp,
                stamp,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def link_decision_to_note(
    *, note_id: int, decision_id: int, db_path: Path | str | None = None
) -> None:
    """Point the originating musing at its decision (the 0093 link column —
    plain INTEGER + code-level validation, no FK by repo invariant)."""
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE analyst_notes SET decision_id = ?, updated_at = ? WHERE id = ?",
            (decision_id, now_iso(), note_id),
        )
        conn.commit()
    finally:
        conn.close()


def _roth_account(account_name: str | None) -> str | None:
    if account_name and "roth" in account_name.lower():
        return "roth"
    return None


def detect_unannounced_fills(
    *,
    db_path: Path | str | None = None,
    portfolio: LivePortfolio | None = None,
    lookback_days: int = 7,
    match_window_days: int = 3,
    min_usd: float = 1000.0,
    transactions_limit: int = 100,
    now: datetime | None = None,
) -> dict[str, int]:
    """The retro net: create annotation-pending owner stubs for tracker fills
    that no owner decision announced.

    A fill matches an existing owner decision when the same ticker has a
    direction-compatible ``decided_by='owner'`` row within ±``match_window_days``
    of the fill date. Unmatched fills ≥ ``min_usd`` become stub rows
    (conviction/falsifier NULL — the coach asks for them) marked
    ``retro-net:<ticker>:<date>:<direction>`` in ``user_notes``, which is also
    the idempotency key. Never raises on tracker/DB trouble."""
    tally = {
        "fills_seen": 0,
        "stubs_created": 0,
        "matched": 0,
        "below_threshold": 0,
        "skipped_existing": 0,
        "tracker_unavailable": 0,
        "errored": 0,
    }
    if portfolio is None:
        try:
            from integrations.portfolio_tracker_client import fetch_live_portfolio

            portfolio = fetch_live_portfolio(transactions_limit=transactions_limit)
        except Exception:
            tally["tracker_unavailable"] = 1
            return tally
    if not portfolio.available:
        tally["tracker_unavailable"] = 1
        return tally

    now_naive = (now or datetime.now()).replace(tzinfo=None)
    cutoff = (now_naive - timedelta(days=lookback_days)).date()
    conn = open_conn(db_path)
    try:
        for txn in portfolio.transactions:
            direction = (txn.type or "").strip().lower()
            ticker = (txn.ticker or "").strip().upper()
            if direction not in _FILL_MATCH_KINDS or not ticker:
                continue
            day = (txn.date or "")[:10]
            try:
                fill_date = datetime.fromisoformat(day).date()
            except ValueError:
                continue
            if fill_date < cutoff:
                continue
            tally["fills_seen"] += 1
            amount = abs(float(txn.amount or 0.0))
            if amount < min_usd:
                tally["below_threshold"] += 1
                continue
            marker = f"{_RETRO_MARKER}{ticker}:{day}:{direction}"
            if conn.execute(
                "SELECT 1 FROM decisions WHERE user_notes LIKE ?", (f"%{marker}%",)
            ).fetchone():
                tally["skipped_existing"] += 1
                continue
            kinds = _FILL_MATCH_KINDS[direction]
            placeholders = ",".join("?" * len(kinds))
            lo = (fill_date - timedelta(days=match_window_days)).isoformat()
            hi = (fill_date + timedelta(days=match_window_days + 1)).isoformat()
            announced = conn.execute(
                f"""
                SELECT 1 FROM decisions
                WHERE decided_by = 'owner' AND ticker = ?
                  AND recommendation_kind IN ({placeholders})
                  AND made_at >= ? AND made_at < ?
                """,
                (ticker, *kinds, lo, hi),
            ).fetchone()
            if announced:
                tally["matched"] += 1
                continue
            stamp = now_iso()
            conn.execute(
                """
                INSERT INTO decisions
                    (ticker, recommendation_kind, decided_by, scope, account,
                     size_usd, rationale_excerpt, user_notes, made_at, created_at)
                VALUES (?, ?, 'owner', 'ticker', ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker,
                    "initiate" if direction == "buy" else "sell",
                    _roth_account(txn.account_name),
                    amount,
                    "Detected from the tracker transaction feed; awaiting your "
                    "conviction + falsifier (post-hoc).",
                    f"{marker} · unannounced fill, annotation pending",
                    f"{day}T00:00:00",
                    stamp,
                ),
            )
            tally["stubs_created"] += 1
        conn.commit()
    except Exception:
        # A daily best-effort net: a mid-loop DB surprise degrades to a tally
        # flag (already-committed stubs stand; the marker keys keep reruns safe).
        tally["errored"] = 1
    finally:
        conn.close()
    return tally
