"""Read/write API for macro_series + macro_sensitivities.

Mirrors the shape of `predictions_store.py`:
  - Single-file write surface, every function takes an optional db_path so
    callers (tests, alt-roots) can target a non-default DB.
  - Returns None / [] on missing DB rather than raising — callers compose
    these into pipelines that should degrade quietly when a table isn't
    migrated yet.
  - Idempotent inserts via the natural key (`uq_macro_series`,
    `uq_macro_sens`); a re-fetch updates the value in place.

Public surface:
  upsert_series_value(series_id, rate_date, value, source) -> id | None
  fetch_series(series_id, lookback_days=None) -> list[(date, value)]  newest first
  upsert_sensitivity(ticker, series_id, beta, r_squared, lookback) -> id | None
  fetch_sensitivities(ticker) -> list[Sensitivity]
  compute_sensitivities(ticker, ticker_prices, series_ids, lookback_days)
      -> dict[series_id, (beta, r_squared, n_obs)]

The compute_sensitivities helper is pure: it consumes ticker price history +
the registry's series ids and regresses weekly returns. No DB writes — the
caller decides whether to persist (and the CLI driver does). This keeps the
math testable without setting up the schema.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from db_paths import resolve_db_path
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SeriesPoint:
    rate_date: date
    value: float
    source: str


@dataclass(slots=True)
class Sensitivity:
    id: int
    ticker: str
    series_id: str
    beta: float
    r_squared: float | None
    lookback_window_days: int
    computed_at: datetime
    # Observations behind the regression (migration 0184). None on legacy rows
    # written before the column existed — consumers treat None as "unknown"
    # (the r² floor still applies; the n floor only when n is known).
    n_obs: int | None = None


# ---------------------------------------------------------------------------
# Connection helpers — same pattern as predictions_store
# ---------------------------------------------------------------------------


def _open(db_path: Path | str | None, *, expect_table: str) -> sqlite3.Connection | None:
    try:
        path = resolve_db_path(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = connect_sqlite(
            path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=True,
        )
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (expect_table,),
        ).fetchone()
        if present is None:
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError):
        return None


def _parse_date(raw: object) -> date | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    try:
        s = str(raw)
        # SQLite returns DATE as 'YYYY-MM-DD'; tolerate full ISO timestamps too.
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# macro_series — write + read
# ---------------------------------------------------------------------------


def upsert_series_value(
    *,
    series_id: str,
    rate_date: date,
    value: float,
    source: str = "FMP",
    db_path: Path | str | None = None,
) -> int | None:
    """Insert or update one (series_id, rate_date) row. Returns the row id.

    Re-fetches with a different value update the row rather than creating
    a duplicate (idempotent on the natural key)."""
    conn = _open(db_path, expect_table="macro_series")
    if conn is None:
        return None
    try:
        existing = conn.execute(
            "SELECT id FROM macro_series WHERE series_id = ? AND rate_date = ?",
            (series_id, rate_date.isoformat()),
        ).fetchone()
        now = datetime.now(UTC).isoformat()
        if existing is not None:
            conn.execute(
                "UPDATE macro_series SET value = ?, source = ? WHERE id = ?",
                (float(value), source, int(existing["id"])),
            )
            conn.commit()
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO macro_series(series_id, rate_date, value, source, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (series_id, rate_date.isoformat(), float(value), source, now),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "macro_series_upsert_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def fetch_series(
    *,
    series_id: str,
    lookback_days: int | None = None,
    db_path: Path | str | None = None,
) -> list[SeriesPoint]:
    """Return rows for one series_id, newest first. Optional lookback caps the window."""
    conn = _open(db_path, expect_table="macro_series")
    if conn is None:
        return []
    try:
        if lookback_days is not None and lookback_days > 0:
            rows = conn.execute(
                """
                SELECT rate_date, value, source FROM macro_series
                WHERE series_id = ? AND rate_date >= date('now', ?)
                ORDER BY rate_date DESC
                """,
                (series_id, f"-{int(lookback_days)} days"),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT rate_date, value, source FROM macro_series
                WHERE series_id = ?
                ORDER BY rate_date DESC
                """,
                (series_id,),
            ).fetchall()
        out: list[SeriesPoint] = []
        for r in rows:
            d = _parse_date(r["rate_date"])
            if d is None:
                continue
            out.append(SeriesPoint(rate_date=d, value=float(r["value"]), source=str(r["source"])))
        return out
    finally:
        conn.close()


def latest_series_value(*, series_id: str, db_path: Path | str | None = None) -> SeriesPoint | None:
    pts = fetch_series(series_id=series_id, lookback_days=None, db_path=db_path)
    return pts[0] if pts else None


def series_row_count(*, series_id: str, db_path: Path | str | None = None) -> int:
    conn = _open(db_path, expect_table="macro_series")
    if conn is None:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM macro_series WHERE series_id = ?", (series_id,)
        ).fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# macro_sensitivities — write + read
# ---------------------------------------------------------------------------


def upsert_sensitivity(
    *,
    ticker: str,
    series_id: str,
    beta: float,
    lookback_window_days: int,
    r_squared: float | None = None,
    n_obs: int | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Insert or replace the (ticker, series_id, lookback) sensitivity row.

    ``n_obs`` (migration 0184) persists the regression's observation count so
    the read-side quality floor can distinguish a thin fit from a real one;
    silently ignored on a pre-0184 schema (the write must not break old data
    dirs)."""
    conn = _open(db_path, expect_table="macro_sensitivities")
    if conn is None:
        return None
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(macro_sensitivities)")}
        has_n = "n_obs" in cols
        now = datetime.now(UTC).isoformat()
        existing = conn.execute(
            """
            SELECT id FROM macro_sensitivities
            WHERE ticker = ? AND series_id = ? AND lookback_window_days = ?
            """,
            (ticker.upper(), series_id, int(lookback_window_days)),
        ).fetchone()
        if existing is not None:
            if has_n:
                conn.execute(
                    "UPDATE macro_sensitivities "
                    "SET beta = ?, r_squared = ?, n_obs = ?, computed_at = ? WHERE id = ?",
                    (float(beta), r_squared, n_obs, now, int(existing["id"])),
                )
            else:
                conn.execute(
                    "UPDATE macro_sensitivities "
                    "SET beta = ?, r_squared = ?, computed_at = ? WHERE id = ?",
                    (float(beta), r_squared, now, int(existing["id"])),
                )
            conn.commit()
            return int(existing["id"])
        n_cols = ", n_obs" if has_n else ""
        n_vals = ", ?" if has_n else ""
        params: list[object] = [
            ticker.upper(),
            series_id,
            float(beta),
            r_squared,
            int(lookback_window_days),
            now,
        ]
        if has_n:
            params.append(n_obs)
        cur = conn.execute(
            "INSERT INTO macro_sensitivities("
            f"ticker, series_id, beta, r_squared, lookback_window_days, computed_at{n_cols}"
            f") VALUES (?, ?, ?, ?, ?, ?{n_vals})",
            params,
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "macro_sensitivity_upsert_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def fetch_sensitivities(
    *,
    ticker: str,
    lookback_window_days: int | None = None,
    db_path: Path | str | None = None,
) -> list[Sensitivity]:
    conn = _open(db_path, expect_table="macro_sensitivities")
    if conn is None:
        return []
    try:
        if lookback_window_days is not None:
            rows = conn.execute(
                """
                SELECT * FROM macro_sensitivities
                WHERE ticker = ? AND lookback_window_days = ?
                ORDER BY series_id
                """,
                (ticker.upper(), int(lookback_window_days)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM macro_sensitivities
                WHERE ticker = ?
                ORDER BY series_id, lookback_window_days
                """,
                (ticker.upper(),),
            ).fetchall()
        out: list[Sensitivity] = []
        for r in rows:
            ca = r["computed_at"]
            ca_dt = datetime.fromisoformat(str(ca)) if ca is not None else datetime.now(UTC)
            out.append(
                Sensitivity(
                    id=int(r["id"]),
                    ticker=str(r["ticker"]),
                    series_id=str(r["series_id"]),
                    beta=float(r["beta"]),
                    r_squared=float(r["r_squared"]) if r["r_squared"] is not None else None,
                    lookback_window_days=int(r["lookback_window_days"]),
                    computed_at=ca_dt,
                    # sqlite3.Row is a SEQUENCE: bare `in` tests VALUES, so the
                    # column-presence probe genuinely needs .keys() here.
                    n_obs=(
                        int(r["n_obs"])
                        if "n_obs" in r.keys() and r["n_obs"] is not None  # noqa: SIM118
                        else None
                    ),
                )
            )
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sensitivity math — pure, testable
# ---------------------------------------------------------------------------


def _weekly_returns(points: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Down-sample a sorted-by-date list to weekly closes (Friday-anchored
    via ISO week), then compute log returns. Returns [(week_end_date, ret), ...]
    in chronological order. Skips weeks where the prior week is missing.

    Input must be sorted ascending by date. Filters non-positive values
    (an FX rate of 0 would blow up the log)."""
    if len(points) < 3:
        return []
    # Bucket by ISO (year, week) — keep the latest in-bucket observation.
    last_per_week: dict[tuple[int, int], tuple[date, float]] = {}
    for d, v in points:
        if v <= 0:
            continue
        iso = d.isocalendar()
        key = (iso.year, iso.week)
        existing = last_per_week.get(key)
        if existing is None or d > existing[0]:
            last_per_week[key] = (d, v)
    weekly = sorted(last_per_week.values(), key=lambda t: t[0])
    if len(weekly) < 2:
        return []
    import math

    out: list[tuple[date, float]] = []
    for i in range(1, len(weekly)):
        prev_v = weekly[i - 1][1]
        cur_v = weekly[i][1]
        if prev_v <= 0 or cur_v <= 0:
            continue
        out.append((weekly[i][0], math.log(cur_v / prev_v)))
    return out


def _ols_beta(xs: list[float], ys: list[float]) -> tuple[float, float, int]:
    """Simple OLS regression of y on x. Returns (beta, r_squared, n).

    Two-by-two arithmetic; no numpy. Returns (0.0, 0.0, 0) when the design
    is degenerate (n < 2 or x has zero variance)."""
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0, 0.0, 0
    mean_x = sum(xs[:n]) / n
    mean_y = sum(ys[:n]) / n
    var_x = sum((xs[i] - mean_x) ** 2 for i in range(n))
    var_y = sum((ys[i] - mean_y) ** 2 for i in range(n))
    cov_xy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    if var_x <= 0:
        return 0.0, 0.0, n
    beta = cov_xy / var_x
    if var_y <= 0:
        return beta, 0.0, n
    # r^2 = (cov_xy)^2 / (var_x * var_y)
    r_squared = (cov_xy * cov_xy) / (var_x * var_y)
    # Clamp floating-noise overshoots — r^2 is mathematically in [0, 1].
    if r_squared < 0.0:
        r_squared = 0.0
    elif r_squared > 1.0:
        r_squared = 1.0
    return beta, r_squared, n


def compute_sensitivities(
    *,
    ticker_prices: list[tuple[date, float]],
    series_lookups: dict[str, list[tuple[date, float]]],
    lookback_days: int = 252,
    min_observations: int = 12,
) -> dict[str, tuple[float, float, int]]:
    """Regress weekly log-returns of `ticker_prices` against each macro series.

    Inputs are date-sorted ascending. Output: per series_id → (beta, r^2, n_obs).
    Series with too few overlapping weeks (< min_observations) are SKIPPED rather
    than returned with junk numbers.

    The lookback_days cap is applied to the input window (latest N days),
    not the regression — this lets the caller pass historical archives and
    have the math automatically use a recent window."""
    if not ticker_prices:
        return {}
    # Apply lookback to the latest N days of the ticker price series.
    if lookback_days and lookback_days > 0:
        cutoff_idx = len(ticker_prices) - 1
        latest = ticker_prices[-1][0]
        from datetime import timedelta

        cutoff = latest - timedelta(days=lookback_days)
        ticker_prices = [pt for pt in ticker_prices if pt[0] >= cutoff]
        del cutoff_idx
    tkr_weekly = _weekly_returns(ticker_prices)
    if len(tkr_weekly) < min_observations:
        return {}
    tkr_by_week: dict[tuple[int, int], float] = {}
    for d, r in tkr_weekly:
        iso = d.isocalendar()
        tkr_by_week[(iso.year, iso.week)] = r

    out: dict[str, tuple[float, float, int]] = {}
    for series_id, raw in series_lookups.items():
        if not raw:
            continue
        if lookback_days and lookback_days > 0:
            from datetime import timedelta

            latest = ticker_prices[-1][0] if ticker_prices else raw[-1][0]
            cutoff = latest - timedelta(days=lookback_days)
            series = [pt for pt in raw if pt[0] >= cutoff]
        else:
            series = list(raw)
        ser_weekly = _weekly_returns(series)
        if len(ser_weekly) < min_observations:
            continue
        ser_by_week = {(d.isocalendar().year, d.isocalendar().week): r for d, r in ser_weekly}
        # Match on overlapping weeks.
        common = sorted(set(tkr_by_week) & set(ser_by_week))
        if len(common) < min_observations:
            continue
        xs = [ser_by_week[k] for k in common]  # macro = x
        ys = [tkr_by_week[k] for k in common]  # ticker = y
        beta, r_sq, n = _ols_beta(xs, ys)
        if n < min_observations:
            continue
        out[series_id] = (beta, r_sq, n)
    return out
