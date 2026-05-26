"""DB-aware loaders that materialize a Series for the primitives.

Two public functions, both best-effort (return [] when the DB is missing
or the query returns nothing — never raise):

  load_kpi_series(ticker, kpi_name, ...)        — kpi_facts JOIN kpi_definitions
  load_financial_series(ticker, line_item, ...) — financial_facts
  load_segment_series(ticker, segment, metric)  — segment_facts (bonus)

Dedup: financial_facts often has multiple rows per (ticker, line_item,
period_end, fiscal_period_type) — one per source_doc_id (FMP base statement,
FMP "as_reported", SEC XBRL, etc.). The loaders collapse those duplicates
by max(id) so the most recently ingested observation wins. This matches the
holding_scorecard convention.

Period filtering: by default we restrict to standalone quarterly periods
(Q1/Q2/Q3/Q4); the FY rows are aggregates that would double-count in a
quarterly trend. Override via the period_types argument when you genuinely
want annual data (e.g. lookback_quarters=40 on a series that only has
annual coverage pre-2010).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from timeseries.primitives import Observation

log = logging.getLogger(__name__)

DEFAULT_PERIOD_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")


def _resolve_db_path(repo_root: Path | None, db_path: Path | None) -> Path | None:
    """Either argument may be set; db_path wins when both are."""
    if db_path is not None:
        return Path(db_path)
    if repo_root is not None:
        return Path(repo_root) / "data" / "portfolio.db"
    return None


def _open(db_path: Path) -> sqlite3.Connection | None:
    """Open the DB or None when missing — never raises."""
    if not db_path.exists():
        log.debug({"event": "timeseries_loader_db_missing", "path": str(db_path)})
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        log.warning({"event": "timeseries_loader_open_failed", "error": str(exc)})
        return None


def _parse_period_end(raw: object) -> datetime | None:
    """Sqlite returns DATETIME as a string. Tolerant parse for common shapes."""
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Try ISO + sqlite default
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # Fallback: just the date prefix
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _rows_to_series(rows: Iterable[sqlite3.Row]) -> list[Observation]:
    """Convert rows of (period_end, value) into an ascending Observation list,
    deduplicating by (period_end) — last value wins after the SQL has already
    chosen the canonical row via max(id)."""
    by_period: dict[datetime, float] = {}
    for r in rows:
        pe = _parse_period_end(r["period_end"])
        if pe is None:
            continue
        try:
            v = float(r["value"])
        except (TypeError, ValueError):
            continue
        by_period[pe] = v
    return [Observation(period_end=pe, value=v) for pe, v in sorted(by_period.items())]


def load_financial_series(
    ticker: str,
    line_item: str,
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    period_types: Iterable[str] = DEFAULT_PERIOD_TYPES,
) -> list[Observation]:
    """Load a chronological quarterly series for `ticker.line_item`.

    Picks max(id) per (period_end, fiscal_period_type) so the most recently
    ingested fact wins when duplicates exist across source_doc_ids. Returns
    [] when the DB is missing, the table doesn't exist, or no rows match.
    """
    resolved = _resolve_db_path(repo_root, db_path)
    if resolved is None:
        return []
    conn = _open(resolved)
    if conn is None:
        return []
    period_list = list(period_types) or list(DEFAULT_PERIOD_TYPES)
    placeholders = ",".join("?" * len(period_list))
    try:
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='financial_facts'"
            ).fetchone()
            is None
        ):
            return []
        rows = conn.execute(
            f"""
            SELECT ff.period_end, ff.value
            FROM financial_facts ff
            WHERE ff.ticker = ?
              AND ff.line_item = ?
              AND ff.fiscal_period_type IN ({placeholders})
              AND ff.id = (
                SELECT MAX(ff2.id) FROM financial_facts ff2
                WHERE ff2.ticker = ff.ticker
                  AND ff2.line_item = ff.line_item
                  AND ff2.period_end = ff.period_end
                  AND ff2.fiscal_period_type = ff.fiscal_period_type
              )
            ORDER BY ff.period_end ASC
            """,
            (ticker.upper(), line_item, *period_list),
        ).fetchall()
        return _rows_to_series(rows)
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "timeseries_load_financial_failed",
                "ticker": ticker,
                "line_item": line_item,
                "error": str(exc),
            }
        )
        return []
    finally:
        conn.close()


def load_kpi_series(
    ticker: str,
    kpi_name: str,
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    period_types: Iterable[str] = DEFAULT_PERIOD_TYPES,
) -> list[Observation]:
    """Load a chronological quarterly series for `ticker.kpi_name`.

    JOIN kpi_facts.kpi_definition_id = kpi_definitions.id WHERE kd.name = ?
    so the caller specifies the human-readable KPI name (the same string
    used in holdings JSON tier_1_kpis). Picks max(id) per period.
    """
    resolved = _resolve_db_path(repo_root, db_path)
    if resolved is None:
        return []
    conn = _open(resolved)
    if conn is None:
        return []
    period_list = list(period_types) or list(DEFAULT_PERIOD_TYPES)
    placeholders = ",".join("?" * len(period_list))
    try:
        for required in ("kpi_facts", "kpi_definitions"):
            if (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (required,),
                ).fetchone()
                is None
            ):
                return []
        rows = conn.execute(
            f"""
            SELECT kf.period_end, kf.value
            FROM kpi_facts kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            WHERE kf.ticker = ?
              AND kd.name = ?
              AND kf.fiscal_period_type IN ({placeholders})
              AND kf.id = (
                SELECT MAX(kf2.id) FROM kpi_facts kf2
                WHERE kf2.ticker = kf.ticker
                  AND kf2.kpi_definition_id = kf.kpi_definition_id
                  AND kf2.period_end = kf.period_end
                  AND kf2.fiscal_period_type = kf.fiscal_period_type
              )
            ORDER BY kf.period_end ASC
            """,
            (ticker.upper(), kpi_name, *period_list),
        ).fetchall()
        return _rows_to_series(rows)
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "timeseries_load_kpi_failed",
                "ticker": ticker,
                "kpi": kpi_name,
                "error": str(exc),
            }
        )
        return []
    finally:
        conn.close()


def load_segment_series(
    ticker: str,
    segment_name: str,
    metric: str,
    repo_root: Path | None = None,
    *,
    db_path: Path | None = None,
    period_types: Iterable[str] = DEFAULT_PERIOD_TYPES,
) -> list[Observation]:
    """Load a segment-level series (e.g. Cloud revenue, Family-of-Apps OI).

    segment_facts is keyed on (ticker, period_end, segment_name, metric)
    so we filter on all three. Same dedup by max(id) as the others.
    """
    resolved = _resolve_db_path(repo_root, db_path)
    if resolved is None:
        return []
    conn = _open(resolved)
    if conn is None:
        return []
    period_list = list(period_types) or list(DEFAULT_PERIOD_TYPES)
    placeholders = ",".join("?" * len(period_list))
    try:
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='segment_facts'"
            ).fetchone()
            is None
        ):
            return []
        rows = conn.execute(
            f"""
            SELECT sf.period_end, sf.value
            FROM segment_facts sf
            WHERE sf.ticker = ?
              AND sf.segment_name = ?
              AND sf.metric = ?
              AND sf.fiscal_period_type IN ({placeholders})
              AND sf.id = (
                SELECT MAX(sf2.id) FROM segment_facts sf2
                WHERE sf2.ticker = sf.ticker
                  AND sf2.segment_name = sf.segment_name
                  AND sf2.metric = sf.metric
                  AND sf2.period_end = sf.period_end
                  AND sf2.fiscal_period_type = sf.fiscal_period_type
              )
            ORDER BY sf.period_end ASC
            """,
            (ticker.upper(), segment_name, metric, *period_list),
        ).fetchall()
        return _rows_to_series(rows)
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "timeseries_load_segment_failed",
                "ticker": ticker,
                "segment": segment_name,
                "metric": metric,
                "error": str(exc),
            }
        )
        return []
    finally:
        conn.close()
