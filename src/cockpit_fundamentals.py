"""Precomputed per-ticker fundamentals for the cockpit evaluation table.

Moves the ~1.2s _eval_fundamentals double-scan of financial_facts off the
GET / render path by materialising results once per morning-pipeline run.
The render calls read_materialized_fundamentals (a disk read) instead of the
ROW_NUMBER() window over 726k rows.

Pattern mirrors portfolio_weights.py:
- materialize_fundamentals(conn, repo_root) → writes data/cockpit_fundamentals.json
- read_materialized_fundamentals(repo_root) → reads it (render path, never the DB)
- compute_from_db(conn) → the actual SQL scan; called by the materialiser and as
  fallback when no cache exists (fresh install, test env, dev run without morning
  pipeline).

Materialization is atomic (temp file + os.replace). A missing or unreadable
cache returns {} so the columns gracefully degrade to em-dashes, exactly as
when financial_facts is absent.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

__all__ = [
    "compute_from_db",
    "materialize_fundamentals",
    "read_materialized_fundamentals",
]

_CACHE_REL: tuple[str, ...] = ("data", "cockpit_fundamentals.json")

# Half-year period-end spacing guard (Jun-30 ↔ Dec-31 is 181-184d).
_SEMI_ANNUAL_GAP_DAYS = (175, 200)

# ---------------------------------------------------------------------------
# SQL (mirrors the logic in _eval_fundamentals; reads financial_facts directly
# to avoid the metrics/ratios view ROW_NUMBER() scan over ALL line items).
# ---------------------------------------------------------------------------

_QUARTER_FUNDAMENTALS_SQL = """
WITH dedup AS (
    SELECT ticker,
           CAST(substr(period_end, 1, 4) AS INTEGER) AS fiscal_year,
           fiscal_period_type, line_item, value, period_end,
           ROW_NUMBER() OVER (
               PARTITION BY ticker, CAST(substr(period_end, 1, 4) AS INTEGER),
                            fiscal_period_type, line_item
               ORDER BY source_doc_id DESC, period_end DESC
           ) AS rn
    FROM financial_facts
    WHERE fiscal_period_type LIKE 'Q%'
      AND line_item IN ('revenue', 'free_cash_flow', 'operating_cash_flow',
                        'capital_expenditure')
)
SELECT ticker,
       MAX(period_end) AS period_end,
       MAX(CASE WHEN line_item = 'revenue' THEN value END) AS revenue,
       MAX(CASE WHEN line_item = 'free_cash_flow' THEN value END) AS free_cash_flow,
       MAX(CASE WHEN line_item = 'operating_cash_flow' THEN value END) AS operating_cash_flow,
       MAX(CASE WHEN line_item = 'capital_expenditure' THEN value END) AS capex
FROM dedup
WHERE rn = 1
GROUP BY ticker, fiscal_year, fiscal_period_type
HAVING MAX(CASE WHEN line_item = 'revenue' THEN value END) IS NOT NULL
ORDER BY ticker, period_end DESC
"""

_TTM_FCF_MARGIN_SQL = """
WITH dedup AS (
    SELECT ticker,
           CAST(substr(period_end, 1, 4) AS INTEGER) AS fiscal_year,
           line_item, value,
           ROW_NUMBER() OVER (
               PARTITION BY ticker, CAST(substr(period_end, 1, 4) AS INTEGER),
                            fiscal_period_type, line_item
               ORDER BY source_doc_id DESC, period_end DESC
           ) AS rn
    FROM financial_facts
    WHERE fiscal_period_type = 'TTM'
      AND line_item IN ('revenue', 'free_cash_flow')
)
SELECT ticker,
       CAST(MAX(CASE WHEN line_item = 'free_cash_flow' THEN value END) AS REAL)
           / NULLIF(MAX(CASE WHEN line_item = 'revenue' THEN value END), 0) AS fcf_margin
FROM dedup
WHERE rn = 1
GROUP BY ticker, fiscal_year
ORDER BY ticker, fiscal_year
"""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _safe_rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    try:
        cur = conn.execute(sql)
    except sqlite3.OperationalError:
        return []
    cur.row_factory = sqlite3.Row
    return cur.fetchall()


def _quarter_fcf(row: sqlite3.Row) -> float | None:
    try:
        if row["free_cash_flow"] is not None:
            return float(row["free_cash_flow"])
        ocf, capex = row["operating_cash_flow"], row["capex"]
        if ocf is not None and capex is not None:
            return float(ocf) + float(capex)
    except (TypeError, ValueError):
        return None
    return None


def _semi_annual_pair(
    rows: list[tuple[str, float, float | None]],
    window: list[tuple[datetime, float, float]],
) -> list[tuple[datetime, float, float]]:
    if len(window) < 3:
        return []
    lo, hi = _SEMI_ANNUAL_GAP_DAYS
    if not all(lo <= (window[i][0] - window[i + 1][0]).days <= hi for i in (0, 1)):
        return []
    newer, older = window[0][0], window[1][0]
    for end, _, _ in rows:
        try:
            dt = datetime.fromisoformat(end[:10])
        except ValueError:
            continue
        if older < dt < newer:
            return []
    return window[:2]


def _ttm_fcf_margin(rows: list[tuple[str, float, float | None]]) -> float | None:
    window: list[tuple[datetime, float, float]] = []
    for end, rev, q_fcf in rows:
        if q_fcf is None:
            continue
        try:
            dt = datetime.fromisoformat(end[:10])
        except ValueError:
            continue
        if window and window[-1][0] == dt:
            continue
        window.append((dt, rev, q_fcf))
        if len(window) == 4:
            break
    if len(window) == 4 and (window[0][0] - window[-1][0]).days <= 330:
        ttm = window
    else:
        ttm = _semi_annual_pair(rows, window)
    if not ttm:
        return None
    rev_sum = sum(rev for _, rev, _ in ttm)
    if rev_sum <= 0:
        return None
    return sum(f for _, _, f in ttm) / rev_sum * 100.0


def _cache_path(repo_root: Path) -> Path:
    return repo_root.joinpath(*_CACHE_REL)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_from_db(
    conn: sqlite3.Connection,
) -> dict[str, tuple[float | None, float | None]]:
    """(rev_yoy_pct, fcf_margin_pct) per ticker — direct DB scan.

    Revenue YoY: latest quarterly row vs the first row at least ~11 months
    older. FCF margin: TTM row when present, else summed from the newest four
    quarterly rows (semi-annual reporters sum the newest two half-years).
    Degrades to {} when financial_facts is absent.
    """
    fcf: dict[str, float] = {}
    for r in _safe_rows(conn, _TTM_FCF_MARGIN_SQL):
        try:
            if r["fcf_margin"] is not None:
                fcf[str(r["ticker"]).upper()] = float(r["fcf_margin"]) * 100.0
        except (TypeError, ValueError):
            continue

    by_ticker: dict[str, list[tuple[str, float, float | None]]] = {}
    for r in _safe_rows(conn, _QUARTER_FUNDAMENTALS_SQL):
        try:
            rev = float(r["revenue"])
        except (TypeError, ValueError):
            continue
        by_ticker.setdefault(str(r["ticker"]).upper(), []).append(
            (str(r["period_end"]), rev, _quarter_fcf(r))
        )

    out: dict[str, tuple[float | None, float | None]] = {}
    for t, rows in by_ticker.items():
        rev_yoy: float | None = None
        if rows:
            latest_end, latest_rev, _ = rows[0]
            try:
                latest_dt = datetime.fromisoformat(latest_end[:10])
            except ValueError:
                latest_dt = None
            if latest_dt is not None and latest_rev:
                for end, rev, _ in rows[1:]:
                    try:
                        dt = datetime.fromisoformat(end[:10])
                    except ValueError:
                        continue
                    age = (latest_dt - dt).days
                    if 330 <= age <= 430 and rev:
                        rev_yoy = (latest_rev / rev - 1.0) * 100.0
                        break
        margin = fcf.get(t)
        if margin is None:
            margin = _ttm_fcf_margin(rows)
        out[t] = (rev_yoy, margin)
    for t, margin in fcf.items():
        out.setdefault(t, (None, margin))
    return out


def materialize_fundamentals(conn: sqlite3.Connection, repo_root: Path) -> int:
    """Compute and write the fundamentals cache atomically.

    Returns the number of tickers materialised. The cache is only overwritten
    on a successful computation — a DB error leaves the last-good file intact.
    """
    data = compute_from_db(conn)
    serialisable = {t: list(v) for t, v in data.items()}
    payload = {
        "computed_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "fundamentals": serialisable,
    }
    path = _cache_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix="cockpit_fundamentals."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return len(data)


def read_materialized_fundamentals(
    repo_root: Path,
) -> dict[str, tuple[float | None, float | None]]:
    """Read the cache; {} when absent, unreadable, or malformed.

    This is the render path's only fundamentals source — a pure disk read,
    never a DB query.
    """
    try:
        raw = _cache_path(repo_root).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    fund = cast("dict[str, object]", payload).get("fundamentals")
    if not isinstance(fund, dict):
        return {}
    out: dict[str, tuple[float | None, float | None]] = {}
    for ticker, pair in cast("dict[str, object]", fund).items():
        if not isinstance(pair, list) or len(cast("list[object]", pair)) != 2:
            continue
        rev_yoy_raw, fcf_margin_raw = cast("list[object]", pair)
        rev_yoy: float | None = (
            float(cast("float", rev_yoy_raw))
            if isinstance(rev_yoy_raw, (int, float)) and not isinstance(rev_yoy_raw, bool)
            else None
        )
        fcf_margin: float | None = (
            float(cast("float", fcf_margin_raw))
            if isinstance(fcf_margin_raw, (int, float)) and not isinstance(fcf_margin_raw, bool)
            else None
        )
        out[str(ticker).upper()] = (rev_yoy, fcf_margin)
    return out
