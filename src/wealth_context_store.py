"""wealth_context_snapshot_history persistence — PRD §7.6 (P0-F).

Append-only aggregates timeseries (migration 0187). Same degrade-don't-crash
contract as :mod:`portfolio_risk_snapshot_store`: every function returns
``False`` / ``None`` / ``[]`` instead of raising on a missing DB / table.
Appends are idempotent via the UNIQUE ``input_sha`` (INSERT OR IGNORE) — a
re-run on the same observed values is a no-op, not a duplicate row.

Consumers (per §7.6.5): trend/delta context for the Risk Budget and Senior
Partner Brief, Workstream-C drift alerts, and the aged fallback when the
tracker/wealthplan are unreachable — always rendered with its age. Advice-
time reads stay LIVE; this table supplies history, not current truth.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from integrations.wealth_context import WealthContextSnapshot
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_TABLE = "wealth_context_snapshot_history"
_DEFAULT_USER = "bhanu"


@dataclass(slots=True)
class WealthSnapshotRow:
    """A persisted observation read back for trend/fallback rendering."""

    captured_at: str
    as_of: str
    tracker_as_of: str | None
    wealthplan_as_of: str | None
    currency: str
    net_worth_total: float | None
    liquid_total: float | None
    investable_total: float | None
    home_equity: float | None
    snapshot: WealthContextSnapshot | None  # parsed snapshot_json, None if corrupt
    input_sha: str


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    if db_path is None or not Path(db_path).exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (_TABLE,),
        ).fetchone()
        if present is None:
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        return None


def append_snapshot(
    snap: WealthContextSnapshot,
    *,
    user_id: str = _DEFAULT_USER,
    db_path: Path | str | None = None,
) -> tuple[bool, bool]:
    """Append one observation. Returns ``(written, deduped)`` — ``(False,
    True)`` means the identical observation was already recorded (idempotent
    re-run), ``(False, False)`` means the DB/table is unavailable or the
    write failed. Never raises."""
    conn = _open(db_path)
    if conn is None:
        return False, False
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    sha = snap.input_sha()
    try:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {_TABLE} "
            "(user_id, captured_at, as_of, tracker_as_of, wealthplan_as_of, currency, "
            " net_worth_total, liquid_total, investable_total, home_equity, "
            " snapshot_json, input_sha, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                now,
                snap.as_of,
                snap.tracker_as_of,
                snap.wealthplan_as_of,
                snap.currency,
                snap.net_worth_total,
                snap.liquid_total,
                snap.investable_total,
                snap.home_equity,
                json.dumps(snap.model_dump(mode="json"), sort_keys=True),
                sha,
                now,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            return False, True  # identical observation already on record
        return True, False
    except sqlite3.Error:
        return False, False
    finally:
        conn.close()


def _row_to_dataclass(r: sqlite3.Row) -> WealthSnapshotRow:
    snapshot: WealthContextSnapshot | None = None
    try:
        snapshot = WealthContextSnapshot.model_validate(json.loads(r["snapshot_json"]))
    except Exception:
        snapshot = None
    return WealthSnapshotRow(
        captured_at=str(r["captured_at"]),
        as_of=str(r["as_of"]),
        tracker_as_of=r["tracker_as_of"],
        wealthplan_as_of=r["wealthplan_as_of"],
        currency=str(r["currency"]),
        net_worth_total=r["net_worth_total"],
        liquid_total=r["liquid_total"],
        investable_total=r["investable_total"],
        home_equity=r["home_equity"],
        snapshot=snapshot,
        input_sha=str(r["input_sha"]),
    )


def read_latest(
    *,
    user_id: str = _DEFAULT_USER,
    db_path: Path | str | None = None,
) -> WealthSnapshotRow | None:
    """Most recent observation by ``as_of`` (ties broken by ``captured_at``),
    or ``None`` when none exists / the DB is unavailable."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            f"SELECT * FROM {_TABLE} WHERE user_id = ? "
            "ORDER BY as_of DESC, captured_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return _row_to_dataclass(row) if row is not None else None


def read_history(
    *,
    user_id: str = _DEFAULT_USER,
    since: str | None = None,
    limit: int = 400,
    db_path: Path | str | None = None,
) -> list[WealthSnapshotRow]:
    """Observations newest-first; ``since`` (ISO) floors the ``as_of`` window.
    ``[]`` on a pre-0187 DB / any failure."""
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        clauses = ["user_id = ?"]
        params: list[object] = [user_id]
        if since:
            clauses.append("as_of >= ?")
            params.append(since)
        params.append(int(limit))
        rows = conn.execute(
            f"SELECT * FROM {_TABLE} WHERE {' AND '.join(clauses)} "
            "ORDER BY as_of DESC, captured_at DESC LIMIT ?",
            params,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_row_to_dataclass(r) for r in rows]


__all__ = [
    "WealthSnapshotRow",
    "append_snapshot",
    "read_history",
    "read_latest",
]
