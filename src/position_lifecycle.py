"""Position-lifecycle ledger — reconciler + read/write API (fund-grade S5, PR 2).

``position_entries`` (alembic 0088) is the durable entry/exit record: what the
analyst believed at entry (thesis excerpt, conviction, the falsifiable
conditions of #468), what they paid, when and why they exited, and what the
exit taught. This module populates and serves it.

Population is STATE-BASED, not event-based: there is no single chokepoint for
``tracked_companies.list_type`` writes (scripts, manual SQL, future UI all
flip it), so ``sync_position_lifecycle`` reconciles the current world — the
portfolio list + the tracker's live holdings/transactions — against the open
ledger rows on every morning-pipeline run:

  * portfolio ticker without an open row  → OPEN one. Entry date/price come
    from the tracker's buy transactions / avg cost when available; conviction
    from the latest sizing intent or decisions row; thesis excerpt from the
    holdings JSON; entry_conditions snapshots the ticker's open falsifiable
    conditions (decisions.decision_conditions).
  * open row whose ticker left the portfolio → CLOSE it. Exit date/price from
    the tracker's sell transactions when visible, else the detection date.
    ``exit_reason`` / ``lessons`` / ``outcome_vs_thesis`` stay NULL — they are
    the analyst's post-exit grading, written from the holding-page timeline.

The tracker may be OFFLINE (``LivePortfolio.available=False``): the reconciler
still runs — membership comes from ``tracked_companies`` alone and the
price/date enrichments are simply skipped (NULL, honest).

``--backfill`` (``assume_preexisting=True``) seeds rows for positions that
were already held before this ledger existed: ``source='backfill'`` and
``entry_date`` stays NULL unless a buy transaction is actually visible —
"held, opening unknown" beats an invented date.

Best-effort posture against a missing DB / pre-0088 schema (returns a
``db_unavailable`` tally), matching decision_extractor / decision_conditions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import LivePortfolio, fetch_live_portfolio

log = logging.getLogger(__name__)

OUTCOME_VOCAB: frozenset[str] = frozenset({"played_out", "broke", "mixed", "unrelated"})
_CONVICTION_VOCAB: frozenset[str] = frozenset({"low", "medium", "high"})

# Tracker transaction `type` values that mark an opening buy / closing sell.
# Plaid investment-transaction types; subtype refines but type is the anchor.
_BUY_TYPES: frozenset[str] = frozenset({"buy"})
_SELL_TYPES: frozenset[str] = frozenset({"sell"})

_THESIS_EXCERPT_CAP = 600


@dataclass(frozen=True, slots=True)
class PositionEntry:
    """One row of position_entries, decoded."""

    id: int
    user_id: str
    ticker: str
    entry_date: str | None
    entry_price: float | None
    entry_conviction: str | None
    entry_thesis_excerpt: str | None
    entry_conditions: str | None  # raw JSON (the holding-page renderer decodes)
    exit_date: str | None
    exit_price: float | None
    exit_reason: str | None
    lessons: str | None
    outcome_vs_thesis: str | None
    source: str
    created_at: str
    updated_at: str

    @property
    def is_open(self) -> bool:
        return self.exit_date is None


# ---------------------------------------------------------------------------
# DB plumbing
# ---------------------------------------------------------------------------


def _open(db_path: Path | str) -> sqlite3.Connection | None:
    """Best-effort open: None when the DB or position_entries is unavailable."""
    try:
        path = Path(db_path)
        if not path.exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='position_entries'"
            ).fetchone()
            is None
        ):
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError):
        return None


def _now_iso() -> str:
    # Naive-UTC, the repo-wide convention.
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _today_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).date().isoformat()


def _row_to_entry(row: sqlite3.Row) -> PositionEntry:
    return PositionEntry(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        ticker=str(row["ticker"]),
        entry_date=row["entry_date"],
        entry_price=float(row["entry_price"]) if row["entry_price"] is not None else None,
        entry_conviction=row["entry_conviction"],
        entry_thesis_excerpt=row["entry_thesis_excerpt"],
        entry_conditions=row["entry_conditions"],
        exit_date=row["exit_date"],
        exit_price=float(row["exit_price"]) if row["exit_price"] is not None else None,
        exit_reason=row["exit_reason"],
        lessons=row["lessons"],
        outcome_vs_thesis=row["outcome_vs_thesis"],
        source=str(row["source"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


# ---------------------------------------------------------------------------
# Read / write API
# ---------------------------------------------------------------------------


def list_entries(
    *,
    db_path: Path | str,
    ticker: str | None = None,
    user_id: str = DEFAULT_USER_ID,
    limit: int = 100,
) -> list[PositionEntry]:
    """Lifecycle rows, open first then newest-closed-first — the timeline order."""
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        clauses = ["user_id = ?"]
        params: list[object] = [user_id]
        if ticker is not None:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        params.append(int(limit))
        rows = conn.execute(
            f"""
            SELECT * FROM position_entries
            WHERE {" AND ".join(clauses)}
            ORDER BY (exit_date IS NULL) DESC,
                     COALESCE(exit_date, '') DESC,
                     COALESCE(entry_date, '') DESC,
                     id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_row_to_entry(r) for r in rows]
    finally:
        conn.close()


def update_exit_fields(
    *,
    db_path: Path | str,
    entry_id: int,
    exit_reason: str | None = None,
    lessons: str | None = None,
    outcome_vs_thesis: str | None = None,
) -> bool:
    """Write the analyst's post-exit grading onto one row. Only supplied
    (non-None) fields are touched; empty strings clear a field. Raises
    ``ValueError`` on an unknown outcome label, ``LookupError`` when the row
    doesn't exist; returns False on DB unavailability."""
    if outcome_vs_thesis not in (None, "") and outcome_vs_thesis not in OUTCOME_VOCAB:
        raise ValueError(
            f"unknown outcome_vs_thesis {outcome_vs_thesis!r}; "
            f"expected one of {sorted(OUTCOME_VOCAB)}"
        )
    conn = _open(db_path)
    if conn is None:
        return False
    try:
        sets: list[str] = []
        params: list[object] = []
        for column, value in (
            ("exit_reason", exit_reason),
            ("lessons", lessons),
            ("outcome_vs_thesis", outcome_vs_thesis),
        ):
            if value is None:
                continue
            sets.append(f"{column} = ?")
            params.append(value if value != "" else None)
        if not sets:
            return True
        sets.append("updated_at = ?")
        params.append(_now_iso())
        params.append(entry_id)
        cur = conn.execute(f"UPDATE position_entries SET {', '.join(sets)} WHERE id = ?", params)
        if cur.rowcount == 0:
            raise LookupError(f"position_entries id={entry_id} not found")
        conn.commit()
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Enrichment sources (each defensive — absent tables / files degrade to None)
# ---------------------------------------------------------------------------


def _portfolio_tickers(conn: sqlite3.Connection) -> set[str] | None:
    """Active portfolio per tracked_companies, or None when the table is
    absent (a partial test DB) — None makes the reconciler a no-op rather
    than mass-closing every open row on missing data."""
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE list_type = 'portfolio' AND archived_at IS NULL"
        ).fetchall()
    except sqlite3.Error:
        return None
    return {str(r["ticker"]).upper() for r in rows}


def _latest_conviction(conn: sqlite3.Connection, ticker: str, user_id: str) -> str | None:
    """Best-effort stated conviction: the latest sizing intent whose
    intent_kind is conviction-shaped, else the latest decisions row's
    conviction (the #468 ledger)."""
    try:
        row = conn.execute(
            "SELECT intent_value, narrative FROM position_sizing_intent "
            "WHERE user_id = ? AND ticker = ? AND intent_kind = 'conviction' "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (user_id, ticker),
        ).fetchone()
        if row is not None:
            for candidate in (row["narrative"], row["intent_value"]):
                label = str(candidate or "").strip().lower()
                if label in _CONVICTION_VOCAB:
                    return label
    except sqlite3.Error:
        pass
    try:
        row = conn.execute(
            "SELECT conviction FROM decisions "
            "WHERE ticker = ? AND conviction IS NOT NULL "
            "ORDER BY made_at DESC, id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if row is not None and str(row["conviction"]).lower() in _CONVICTION_VOCAB:
            return str(row["conviction"]).lower()
    except sqlite3.Error:
        pass
    return None


def _thesis_excerpt(repo_root: Path, ticker: str) -> str | None:
    """The holdings JSON thesis statement, capped — the entry-time snapshot."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    thesis = cast("dict[str, object]", payload).get("thesis")
    if not isinstance(thesis, str) or not thesis.strip():
        return None
    return thesis.strip()[:_THESIS_EXCERPT_CAP]


def _open_conditions_snapshot(conn: sqlite3.Connection, ticker: str) -> str | None:
    """JSON snapshot of the ticker's open falsifiable conditions (#468),
    flattened across its open decisions. None when there are none."""
    try:
        from decision_conditions import load_open_decisions

        conditions = [
            cond.as_json_obj()
            for decision in load_open_decisions(conn, ticker)
            for cond in decision.conditions
        ]
    except Exception:  # pre-0086 schema / import problem — snapshot is optional
        return None
    if not conditions:
        return None
    return json.dumps(conditions, ensure_ascii=False)


def _txn_dates_and_price(
    portfolio: LivePortfolio, ticker: str, *, kinds: frozenset[str], earliest: bool
) -> tuple[str | None, float | None]:
    """(date, per-share price) of the earliest/latest visible buy or sell for
    ``ticker`` in the tracker's transaction window. The window is short
    (~25 rows) so an opening buy is only visible for RECENT entries — exactly
    when it's most useful; older entries honestly get None."""
    matches = [
        t
        for t in portfolio.transactions
        if (t.ticker or "").upper() == ticker.upper() and t.type.lower() in kinds and t.date
    ]
    if not matches:
        return None, None
    matches.sort(key=lambda t: t.date)
    txn = matches[0] if earliest else matches[-1]
    price: float | None = None
    if txn.amount is not None and txn.quantity:
        price = round(abs(txn.amount) / abs(txn.quantity), 4)
    return txn.date[:10], price


def _avg_cost(portfolio: LivePortfolio, ticker: str) -> float | None:
    for pos in portfolio.positions:
        if (pos.ticker or "").upper() == ticker.upper():
            if pos.cost_basis is not None and pos.quantity:
                return round(pos.cost_basis / pos.quantity, 4)
            return None
    return None


# ---------------------------------------------------------------------------
# The reconciler
# ---------------------------------------------------------------------------


def sync_position_lifecycle(
    *,
    db_path: Path | str,
    user_id: str = DEFAULT_USER_ID,
    portfolio: LivePortfolio | None = None,
    assume_preexisting: bool = False,
) -> dict[str, object]:
    """Reconcile position_entries against the current portfolio state.

    ``portfolio`` injects a tracker snapshot (tests / reuse); None fetches
    live, degrading per the client's never-raise contract. With
    ``assume_preexisting`` (the one-time backfill), newly-opened rows are
    ``source='backfill'`` and ``entry_date`` stays NULL unless a buy
    transaction is actually visible. Idempotent: a second run against the
    same world changes nothing.
    """

    def _unavailable() -> dict[str, object]:
        return {
            "opened": 0,
            "closed": 0,
            "unchanged": 0,
            "tracker_available": False,
            "db_unavailable": 1,
        }

    conn = _open(db_path)
    if conn is None:
        return _unavailable()
    opened = closed = 0
    try:
        current = _portfolio_tickers(conn)
        if current is None:
            # tracked_companies absent — nothing trustworthy to reconcile against.
            return _unavailable()
        if portfolio is None:
            portfolio = fetch_live_portfolio()
        if not portfolio.available:
            log.info({"event": "position_lifecycle_tracker_offline", "error": portfolio.error})

        open_rows = {
            str(r["ticker"]).upper(): r
            for r in conn.execute(
                "SELECT * FROM position_entries WHERE user_id = ? AND exit_date IS NULL",
                (user_id,),
            ).fetchall()
        }
        repo_root = Path(db_path).resolve().parent.parent

        now = _now_iso()
        today = _today_iso()

        for ticker in sorted(current - set(open_rows)):
            entry_date, entry_price = (None, None)
            if portfolio.available:
                entry_date, entry_price = _txn_dates_and_price(
                    portfolio, ticker, kinds=_BUY_TYPES, earliest=True
                )
                if entry_price is None:
                    entry_price = _avg_cost(portfolio, ticker)
            if entry_date is None and not assume_preexisting:
                # Live transition detected this run — today is the honest date.
                entry_date = today
            conn.execute(
                """
                INSERT INTO position_entries(
                    user_id, ticker, entry_date, entry_price, entry_conviction,
                    entry_thesis_excerpt, entry_conditions, source,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    ticker,
                    entry_date,
                    entry_price,
                    _latest_conviction(conn, ticker, user_id),
                    _thesis_excerpt(repo_root, ticker),
                    _open_conditions_snapshot(conn, ticker),
                    "backfill" if assume_preexisting else "reconciler",
                    now,
                    now,
                ),
            )
            opened += 1

        for ticker in sorted(set(open_rows) - current):
            exit_date, exit_price = (None, None)
            if portfolio.available:
                exit_date, exit_price = _txn_dates_and_price(
                    portfolio, ticker, kinds=_SELL_TYPES, earliest=False
                )
            conn.execute(
                """
                UPDATE position_entries
                SET exit_date = ?, exit_price = ?, updated_at = ?
                WHERE id = ?
                """,
                (exit_date or today, exit_price, now, int(open_rows[ticker]["id"])),
            )
            closed += 1

        # Enrichment pass: an open row missing entry_price gains the tracker's
        # avg cost once the tracker comes back online.
        if portfolio.available:
            for ticker, row in open_rows.items():
                if ticker not in current or row["entry_price"] is not None:
                    continue
                price = _avg_cost(portfolio, ticker)
                if price is not None:
                    conn.execute(
                        "UPDATE position_entries SET entry_price = ?, updated_at = ? WHERE id = ?",
                        (price, now, int(row["id"])),
                    )

        conn.commit()
        return {
            "opened": opened,
            "closed": closed,
            "unchanged": len(current & set(open_rows)),
            "tracker_available": portfolio.available,
            "db_unavailable": 0,
        }
    finally:
        conn.close()
