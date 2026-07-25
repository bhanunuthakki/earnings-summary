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

import contextlib
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

_TABLE = "portfolio_risk_snapshots"
_HISTORY_TABLE = "portfolio_risk_snapshot_history"
_DEFAULT_USER = "bhanu"

# PRD §7.1 requirement 9 (acceptance gap closed 2026-07-24, migration 0199):
# "Historical comparisons use snapshots produced by the same metric/version
# definition. A metric-version change must be explicit and must not render a
# false delta against an incomparable prior."
#
# METRIC_VERSION identifies the MEANING of the persisted metric columns, not
# their current value. Bump it whenever that meaning changes — a new factor
# leg added to the roll-up, a changed annualization convention, a redefined
# drawdown window, a different benchmark composition. Do NOT bump it when a
# value simply moves day to day; that is exactly the delta this stamp exists
# to make trustworthy. A bump is a deliberate, reviewed code change (this
# constant), never something inferred at write time.
METRIC_VERSION = "v1"

# How the analytics window backing a snapshot was established — derived from
# the tracker's own signal (PerformanceSeries.backfill_start_unreliable), not
# guessed. See execution/refresh_portfolio_risk_snapshot.py.
RebaseBasis = Literal["observed", "modeled_backfill"]

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
    is optional (a partial tracker response still records what it has).

    ``metric_version`` / ``rebase_basis`` are PROVENANCE, not metric content —
    deliberately kept out of :data:`_METRIC_COLUMNS` (which defines the
    content hash) so that two captures of the same numbers under different
    definitions still hash identically; :func:`comparable` is what makes
    them distinguishable for the purpose of rendering a "vs prior" delta."""

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
    # Provenance (migration 0199) — kept out of _METRIC_COLUMNS on purpose.
    metric_version: str | None = None
    rebase_basis: str | None = None


def snapshot_input_sha(snap: RiskSnapshot) -> str:
    """Deterministic content hash over the metric columns (not ``captured_at``,
    which is a write-time stamp). Two captures of the same analytics input
    hash identically, which is what makes the history append idempotent
    (PRD §7.1.8: the `{user_id}_{portfolio_as_of}_{analytics_input_sha}` key —
    ``portfolio_as_of`` rides inside via ``window_start``/``window_end``)."""
    payload = {col: getattr(snap, col) for col in _METRIC_COLUMNS}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class RiskBudgetSnapshot(BaseModel):
    """PRD §7.1.5 validation gate for the AUTHORITATIVE (scheduled) writer.

    Wraps a built :class:`RiskSnapshot` plus the context only the refresh run
    knows (per-name weights, per-endpoint fetch errors) and answers "is this
    capture fit to become the latest truth?" — the render-path opportunistic
    write keeps its simpler performance+positioning gate, but the scheduled
    writer must not persist anything that fails here."""

    as_of: str  # window_end of the analytics fetch (portfolio as-of)
    num_positions: int = Field(gt=0)
    weights_pct: tuple[float, ...] = ()  # per-name weights, percent units
    source_freshness: dict[str, str] = Field(default_factory=dict)
    source_errors: tuple[str, ...] = ()

    def validate_against(self, snap: RiskSnapshot) -> list[str]:
        """Reasons the capture is invalid; empty list = fit to persist."""
        reasons: list[str] = []
        if not self.as_of:
            reasons.append("as_of missing")
        for w in self.weights_pct:
            if not math.isfinite(w):
                reasons.append("non-finite position weight")
                break
        if self.weights_pct:
            total = sum(self.weights_pct)
            # Long-only book in percent units; partial pricing shrinks the sum,
            # so the floor is loose — the point is catching fraction-vs-percent
            # mixups (sum≈1) and doubled payloads (sum≈200), not enforcing 100.
            if not 25.0 <= total <= 130.0:
                reasons.append(f"weights implausibly normalized (sum={total:.1f})")
        concentration = (snap.top1_weight_pct, snap.hhi, snap.effective_holdings)
        if all(v is None for v in concentration):
            reasons.append("core concentration fields all null")
        downside = (snap.max_drawdown_pct, snap.current_drawdown_pct)
        if all(v is None for v in downside):
            reasons.append("core downside fields all null")
        if not self.source_freshness:
            reasons.append("source freshness not explicit")
        return reasons


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
    metric_version: str = METRIC_VERSION,
    rebase_basis: RebaseBasis | None = None,
) -> bool:
    """Upsert the single last-known snapshot for ``user_id`` AND append the
    same capture to ``portfolio_risk_snapshot_history`` (migration 0185).

    The single-row table stays the cheap latest-view the offline surfaces
    read; the history table is the drift substrate ("exposure vs 30 days
    ago"), which the 0105 upsert made structurally impossible. The history
    append is best-effort — a pre-0185 DB still gets its latest-view upsert.

    ``metric_version`` / ``rebase_basis`` (migration 0199, PRD §7.1.9) are
    stamped on BOTH the latest-view row and the history row so a downstream
    "vs prior" delta can tell a real risk move from a definition change
    (:func:`comparable`). They are provenance, NOT metric content — they do
    NOT enter :func:`snapshot_input_sha`, so the existing content-hash dedup
    (0186) is unchanged: a re-run with the identical numbers still dedupes
    in history even if ``metric_version`` differs from the prior write.

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
    prov_cols = ", ".join((cols, "metric_version", "rebase_basis"))
    prov_placeholders = ", ".join((placeholders, "?", "?"))
    prov_updates = ", ".join(
        (updates, "metric_version=excluded.metric_version", "rebase_basis=excluded.rebase_basis")
    )
    sha = snapshot_input_sha(snap)
    try:
        try:
            # 0199+: latest-view upsert also stamps provenance.
            conn.execute(
                f"INSERT INTO {_TABLE} ({prov_cols}) VALUES ({prov_placeholders}) "
                f"ON CONFLICT(user_id) DO UPDATE SET {prov_updates}",
                [user_id, captured_at, *values, metric_version, rebase_basis],
            )
        except sqlite3.OperationalError:  # pre-0199 DB — no provenance columns yet
            conn.execute(
                f"INSERT INTO {_TABLE} ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(user_id) DO UPDATE SET {updates}",
                [user_id, captured_at, *values],
            )
        with contextlib.suppress(sqlite3.Error):  # pre-0185 DB — latest-view still lands
            try:
                # 0186+: content-hash dedup — a re-run on the same analytics
                # input (or the render path re-persisting what the scheduled
                # writer already captured) is a no-op, not a duplicate row.
                conn.execute(
                    f"INSERT OR IGNORE INTO {_HISTORY_TABLE} ({prov_cols}, input_sha) "
                    f"VALUES ({prov_placeholders}, ?)",
                    [user_id, captured_at, *values, metric_version, rebase_basis, sha],
                )
            except sqlite3.OperationalError:
                try:
                    # 0186-0198 DB — input_sha present, no provenance columns yet.
                    conn.execute(
                        f"INSERT OR IGNORE INTO {_HISTORY_TABLE} ({cols}, input_sha) "
                        f"VALUES ({placeholders}, ?)",
                        [user_id, captured_at, *values, sha],
                    )
                except sqlite3.OperationalError:  # pre-0186 DB — legacy append
                    conn.execute(
                        f"INSERT INTO {_HISTORY_TABLE} ({cols}) VALUES ({placeholders})",
                        [user_id, captured_at, *values],
                    )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _snapshot_from_row(row: sqlite3.Row) -> RiskSnapshot:
    """Build a :class:`RiskSnapshot` from a fetched row, tolerating a
    pre-0199 row shape (no ``metric_version``/``rebase_basis`` columns
    selected) by falling back to ``None`` — "unknown", per :func:`comparable`."""
    keys = row.keys()
    return RiskSnapshot(
        captured_at=str(row["captured_at"]),
        **{col: row[col] for col in _METRIC_COLUMNS},
        metric_version=row["metric_version"] if "metric_version" in keys else None,
        rebase_basis=row["rebase_basis"] if "rebase_basis" in keys else None,
    )


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
        try:
            rows = conn.execute(
                f"SELECT captured_at, {', '.join(_METRIC_COLUMNS)}, metric_version, "
                f"rebase_basis FROM {_HISTORY_TABLE} WHERE {' AND '.join(clauses)} "
                "ORDER BY captured_at DESC LIMIT ?",
                params,
            ).fetchall()
        except sqlite3.OperationalError:  # pre-0199 DB — no provenance columns yet
            rows = conn.execute(
                f"SELECT captured_at, {', '.join(_METRIC_COLUMNS)} FROM {_HISTORY_TABLE} "
                f"WHERE {' AND '.join(clauses)} ORDER BY captured_at DESC LIMIT ?",
                params,
            ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_snapshot_from_row(r) for r in rows]


def history_has_sha(
    sha: str,
    *,
    user_id: str = _DEFAULT_USER,
    db_path: Path | str | None = None,
) -> bool:
    """True when the history already carries a capture with this content hash
    — the scheduled writer's "already done" check (PRD §7.1: re-running the
    same input does not duplicate history). False on pre-0186 DBs / failure."""
    conn = _open(db_path)
    if conn is None:
        return False
    try:
        row = conn.execute(
            f"SELECT 1 FROM {_HISTORY_TABLE} WHERE user_id = ? AND input_sha = ? LIMIT 1",
            (user_id, sha),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


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
        try:
            row = conn.execute(
                f"SELECT captured_at, {', '.join(_METRIC_COLUMNS)}, metric_version, "
                f"rebase_basis FROM {_TABLE} WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        except sqlite3.OperationalError:  # pre-0199 DB — no provenance columns yet
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
    return _snapshot_from_row(row)


def comparable(a: RiskSnapshot, b: RiskSnapshot) -> bool:
    """PRD §7.1.9: True only when ``a`` and ``b`` were produced by the same
    metric-version definition AND the same analytics-window rebase basis.
    ``None`` (unknown provenance — a pre-0199 row, or a capture that hasn't
    stamped one of the two fields) is NEVER comparable to anything, including
    another ``None`` — an unknown definition might silently differ from
    another unknown definition, so treating two unknowns as a match would be
    exactly the false-delta risk this function exists to prevent."""
    if a.metric_version is None or b.metric_version is None:
        return False
    if a.rebase_basis is None or b.rebase_basis is None:
        return False
    return a.metric_version == b.metric_version and a.rebase_basis == b.rebase_basis


def incomparable_reason(current: RiskSnapshot, prior: RiskSnapshot) -> str | None:
    """A human sentence for the UI explaining why ``current`` cannot be
    diffed against ``prior``, or ``None`` when it can be (:func:`comparable`
    is True). Reads "prior → current" (e.g. ``"metric definition changed
    (v1 → v2)"``). When the mismatch spans both fields, the metric-version
    story leads — it is the more consequential change (it can move every
    metric at once)."""
    if comparable(current, prior):
        return None
    if current.metric_version != prior.metric_version:
        prior_v = prior.metric_version or "unknown"
        current_v = current.metric_version or "unknown"
        return f"metric definition changed ({prior_v} → {current_v})"
    prior_basis = prior.rebase_basis or "unknown"
    current_basis = current.rebase_basis or "unknown"
    return f"analytics window basis changed ({prior_basis} → {current_basis})"
