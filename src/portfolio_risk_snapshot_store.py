"""Last-known whole-book risk snapshot — persistence (L5 PR2).

A single-row-per-user cache of the benchmark-risk stats, concentration, book
drawdown, and factor exposure read from the :8000 tracker on a successful fetch.
The Risk + Performance surfaces read it back when the tracker is offline so they
render cached values stamped "as of <captured_at>" instead of going blank.

Dumb persistence over a flat :class:`RiskSnapshot` — the panel does the mapping
from the tracker payloads. Never raises on a missing DB / table (returns
``False`` / ``None``), matching the tracker-client's degrade-don't-crash contract.

The offline-snapshot pattern here is shared infrastructure: sibling L15 (tax-lot
Ask pack) reuses :func:`read_latest_snapshot` to answer when the tracker is down.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_TABLE = "portfolio_risk_snapshots"
_HISTORY_TABLE = "portfolio_risk_snapshot_history"
_DEFAULT_USER = "bhanu"

# The metric columns, in insert order — kept in one list so write/read stay in
# lockstep with the migration and adding a metric is a one-line change.
_METRIC_COLUMNS: tuple[str, ...] = (
    "window_start",
    "window_end",
    "benchmark",
    "beta",
    "alpha_annualized_pct",
    "sharpe",
    "sortino",
    "information_ratio",
    "tracking_error_annualized",
    "portfolio_volatility_annualized",
    "r_squared",
    "weighted_avg_correlation_spy",
    "num_positions",
    "top1_weight_pct",
    "top5_weight_pct",
    "top10_weight_pct",
    "hhi",
    "effective_holdings",
    "max_drawdown_pct",
    "current_drawdown_pct",
    "drawdown_recovered",
    "days_to_recovery",
    "spy_beta",
    "qqq_beta",
    "growth_tilt",
    "avg_correlation_spy",
    "rate_beta_10y",
    "names_priced",
    "names_total",
)


@dataclass(slots=True)
class RiskSnapshot:
    """A flat snapshot of the whole-book risk surface. ``captured_at`` is set by
    :func:`write_snapshot` (naive-UTC ISO) and echoed back on read; every metric
    is optional (a partial tracker response still records what it has)."""

    captured_at: str = ""
    window_start: str | None = None
    window_end: str | None = None
    benchmark: str | None = None
    beta: float | None = None
    alpha_annualized_pct: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    information_ratio: float | None = None
    tracking_error_annualized: float | None = None
    portfolio_volatility_annualized: float | None = None
    r_squared: float | None = None
    weighted_avg_correlation_spy: float | None = None
    num_positions: int | None = None
    top1_weight_pct: float | None = None
    top5_weight_pct: float | None = None
    top10_weight_pct: float | None = None
    hhi: float | None = None
    effective_holdings: float | None = None
    max_drawdown_pct: float | None = None
    current_drawdown_pct: float | None = None
    drawdown_recovered: int | None = None
    days_to_recovery: int | None = None
    spy_beta: float | None = None
    qqq_beta: float | None = None
    growth_tilt: float | None = None
    avg_correlation_spy: float | None = None
    rate_beta_10y: float | None = None
    names_priced: int | None = None
    names_total: int | None = None


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    """Open the DB read-write if it exists and carries the snapshot table."""
    if db_path is None or not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
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


def write_snapshot(
    snap: RiskSnapshot,
    *,
    user_id: str = _DEFAULT_USER,
    db_path: Path | str | None = None,
) -> bool:
    """Upsert the single last-known snapshot for ``user_id`` AND append the
    same capture to ``portfolio_risk_snapshot_history`` (migration 0185).

    The single-row table stays the cheap latest-view the offline surfaces
    read; the history table is the drift substrate ("exposure vs 30 days
    ago"), which the 0105 upsert made structurally impossible. The history
    append is best-effort — a pre-0185 DB still gets its latest-view upsert.

    Stamps ``captured_at`` to now (naive-UTC) — the caller's value is ignored.
    Returns ``True`` on a successful write, ``False`` when the DB / table is
    absent or the write fails (never raises)."""
    conn = _open(db_path)
    if conn is None:
        return False
    captured_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    values = [getattr(snap, col) for col in _METRIC_COLUMNS]
    cols = ", ".join(("user_id", "captured_at", *_METRIC_COLUMNS))
    placeholders = ", ".join(["?"] * (2 + len(_METRIC_COLUMNS)))
    updates = ", ".join(f"{col}=excluded.{col}" for col in ("captured_at", *_METRIC_COLUMNS))
    try:
        conn.execute(
            f"INSERT INTO {_TABLE} ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(user_id) DO UPDATE SET {updates}",
            [user_id, captured_at, *values],
        )
        try:
            conn.execute(
                f"INSERT INTO {_HISTORY_TABLE} ({cols}) VALUES ({placeholders})",
                [user_id, captured_at, *values],
            )
        except sqlite3.Error:
            pass  # pre-0185 DB — latest-view still lands
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def read_history(
    *,
    user_id: str = _DEFAULT_USER,
    since: str | None = None,
    limit: int = 200,
    db_path: Path | str | None = None,
) -> list[RiskSnapshot]:
    """History captures for ``user_id``, newest first. ``since`` (ISO string)
    floors the window. [] on a pre-0185 DB / any failure — degrade-don't-crash
    like every reader here. Workstream C8's drift trigger reads this."""
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        clauses = ["user_id = ?"]
        params: list[object] = [user_id]
        if since:
            clauses.append("captured_at >= ?")
            params.append(since)
        params.append(int(limit))
        rows = conn.execute(
            f"SELECT captured_at, {', '.join(_METRIC_COLUMNS)} FROM {_HISTORY_TABLE} "
            f"WHERE {' AND '.join(clauses)} ORDER BY captured_at DESC LIMIT ?",
            params,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [
        RiskSnapshot(
            captured_at=str(r["captured_at"]),
            **{col: r[col] for col in _METRIC_COLUMNS},
        )
        for r in rows
    ]


def read_latest_snapshot(
    *,
    user_id: str = _DEFAULT_USER,
    db_path: Path | str | None = None,
) -> RiskSnapshot | None:
    """The last-known snapshot for ``user_id``, or ``None`` when none exists /
    the DB is unavailable."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            f"SELECT captured_at, {', '.join(_METRIC_COLUMNS)} FROM {_TABLE} WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return RiskSnapshot(
        captured_at=str(row["captured_at"]),
        **{col: row[col] for col in _METRIC_COLUMNS},
    )
